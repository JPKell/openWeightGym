"""Audio model settings and resource profiles for LoadCoach, independent of chat flags."""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import tomllib
from functools import lru_cache

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
OPTION = re.compile(r"[a-z][a-z0-9_.]{0,79}")
CATALOG = tomllib.loads(Path(__file__).with_name("catalog.toml").read_text())["families"]
FAMILIES = tuple(CATALOG)
FAMILY = re.compile(r"[a-z][a-z0-9_]{0,79}")
# openWeightGym's kind of work, and the audio.cpp task route that does it.
TASKS = {"music": "gen", "sound": "gen", "speech": "tts", "clone": "clon", "design": "vdes",
         "transcription": "asr", "alignment": "align", "diarization": "diar"}
GENERATION = ("music", "sound")
LIMITS = {"max_seconds": (1, 3600, 600), "max_takes": (1, 16, 8), "max_text_chars": (1, 200000, 20000),
          "max_audio_mib": (1, 64, 32)}


def path(value, label):
    """Require an explicit absolute path; existence is checked before loading."""
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise ValueError(f"Audio {label} must be an absolute path.")
    return value


def scalars(value, label):
    """Native options are a flat map of names to JSON scalars, never nested native arguments."""
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError(f"Audio {label} must be an object of at most 64 options.")
    for key, item in value.items():
        if not isinstance(key, str) or not OPTION.fullmatch(key):
            raise ValueError(f"Invalid audio {label} name {key!r}.")
        if not (item is None or type(item) in (bool, int, str) or type(item) is float and math.isfinite(item)):
            raise ValueError(f"Audio {label} {key} must be a number, string or true/false.")
        if isinstance(item, str) and len(item) > 4096:
            raise ValueError(f"Audio {label} {key} is too long.")
    return value


def limits(settings):
    """Resolve per-profile limits that a request cannot expand."""
    return {key: default for key, (_, _, default) in LIMITS.items()} | settings.get("audiocpp", {}).get("limits", {})


def kind(settings):
    """Return openWeightGym's kind of work for an audio profile."""
    return settings.get("audiocpp", {}).get("kind", "")


def check_model(name, settings, cards):
    """Validate the package location, kind of work, native options and device choice."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Invalid audio model name.")
    if not isinstance(settings, dict) or settings.keys() - {"engine", "flags", "audiocpp", "gpus", "alone"}:
        raise ValueError("Audio models accept engine, flags, audiocpp, gpus and alone.")
    flags = settings.get("flags")
    if settings.get("engine") != "audiocpp" or not isinstance(flags, dict) or flags.keys() != {"model", "ctx-size"} or flags["ctx-size"] != 0:
        raise ValueError("Audio models require engine=audiocpp, flags.model and ctx-size=0 (no language-token context).")
    path(flags["model"], "model file")
    options = settings.get("audiocpp")
    allowed = {"family", "kind", "package", "path", "files", "load_options", "session_options", "defaults",
               "limits", "host_ram_mib", "threads", "timeout"}
    if not isinstance(options, dict) or options.keys() - allowed or not {"family", "kind", "path"} <= options.keys():
        raise ValueError("Audio models need family, kind and path, and accept only known audiocpp options.")
    if not isinstance(options['family'], str) or not FAMILY.fullmatch(options['family']):
        raise ValueError('Choose a valid audio.cpp model family.')
    if options['kind'] not in TASKS:
        raise ValueError('Choose an audio task supported by openWeightGym.')
    if options['family'] in CATALOG and options["kind"] not in CATALOG[options["family"]]["kinds"]:
        raise ValueError(f"{options['family']} does not do {options['kind']}; it does {', '.join(CATALOG[options['family']]['kinds'])}.")
    path(options["path"], "package path")
    if not isinstance(options.get("package", ""), str) or not isinstance(options.get("files", []), list):
        raise ValueError("Audio package must be a name and files a list.")
    for file in options.get("files", []):
        if not isinstance(file, str) or not file or file.startswith("/") or ".." in Path(file).parts or "\0" in file:
            raise ValueError("Audio package files must be relative paths inside the package.")
    for key in ("load_options", "session_options", "defaults"):
        scalars(options.get(key, {}), key.replace("_", " "))
    configured = options.get("limits", {})
    if not isinstance(configured, dict) or configured.keys() - LIMITS.keys():
        raise ValueError("Unknown audio limit.")
    for key, value in limits(settings).items():
        low, high, _ = LIMITS[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Audio limit {key} must be {low}–{high}.")
    for key, low, high, default in (("host_ram_mib", 1024, 131072, 8192), ("threads", 1, 32, 4), ("timeout", 30, 7200, 1800)):
        value = options.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Audio {key} must be {low}–{high}.")
    wanted = settings.get("gpus", 1)
    if not (wanted in (0, 1) or isinstance(wanted, list) and len(wanted) == 1 and wanted[0] in cards):
        raise ValueError("An audio model runs on one GPU (1, or one named card) or explicitly on the CPU (0).")
    if type(settings.get("alone", True)) is not bool or options["kind"] in GENERATION and settings.get("alone", True) is not True:
        raise ValueError("Music and sound profiles reserve their card exclusively until their peaks are qualified.")


def effective(settings, default_flags):
    """Keep audio configuration independent of global llama.cpp defaults."""
    return {"alone": True, **deepcopy(settings)}


def files_of(settings):
    """List every package file so Files protects and fingerprints the complete bundle."""
    options = settings.get("audiocpp", {})
    path = Path(options.get("path", "/"))
    root = path.parent if path.suffix == ".gguf" else path  # files are relative to the package folder
    result = {"model": settings["flags"]["model"]}
    result.update({"package:" + file: str(root / file) for file in options.get("files", [])})
    return result


def file_selection(settings):
    """Return all package file choices for revision tracking."""
    return tuple(files_of(settings).items())


def base(settings):
    """Audio profiles do not share resident engines across configurations yet."""
    return deepcopy(settings)


def kin_base(settings):
    """Keep every file, option and placement in the resident identity."""
    return base(settings)


def fingerprint_settings(settings, hashes):
    """Replace filesystem locations with the bundle's content identities."""
    result = base(settings)
    result["flags"]["model"] = hashes["model"]
    result["audiocpp"]["path"] = ""
    result["audiocpp"]["files"] = {file: hashes["package:" + file] for file in settings["audiocpp"].get("files", [])}
    return result


def retarget(settings, target):
    """Apply relocated package paths while retaining options and resources."""
    result = base(settings)
    result["flags"]["model"] = target["flags"]["model"]
    for key in ("path", "files"):
        if key in target.get("audiocpp", {}):
            result["audiocpp"][key] = deepcopy(target["audiocpp"][key])
    return result


def contexts(settings):
    """Audio profiles have no language context curve."""
    return [0]


def at_context(settings, context):
    """Retain the audio profile during generic runner operations."""
    return base(settings)


def memory_base(settings):
    """Key measurements by the complete package, options and layout."""
    return base(settings)


def parallel_capacity(settings):
    """One admitted request owns the runtime; takes are generated in turn."""
    return 1


def embedding(settings):
    """Audio models cannot serve embedding requests."""
    return False


def required_devices(settings, cards):
    """Return the explicit one-card or CPU choice."""
    return settings.get("gpus", 1)


def layouts(settings, cards):
    """Never silently change the requested device."""
    return [settings]


def fixed_split(settings, count):
    """audio.cpp does not split a model across cards."""
    return False


def answered_settings(loaded, wanted):
    """Report the audio profile that actually received admission."""
    return base(loaded)


def specs_folder(manifest):
    """The pinned release's model_specs folder, beside its installation manifest."""
    return Path(manifest).parent / "model_specs" if manifest else None


def spec(manifest, family, model_path=''):
    """Read one family's upstream spec from the pinned release."""
    folder = specs_folder(manifest)
    if not isinstance(family, str) or not FAMILY.fullmatch(family):
        raise ValueError('Invalid audio.cpp family.')
    if folder is not None and (folder / (family + '.json')).is_file():
        return json.loads((folder / (family + '.json')).read_text())
    found = embedded_spec(model_path)
    if found.get('family') == family:
        return found
    raise ValueError('No audio.cpp contract is available for ' + family + '. Use a compatible installed runtime or an audio.cpp GGUF with an embedded contract.')


def family_kinds(found, family=''):
    """Map native tasks to routes openWeightGym implements, without a model-name allowlist."""
    aliases = {'tts':'speech', 'clone':'clone', 'clon':'clone', 'design':'design', 'vdes':'design',
               'asr':'transcription', 'transcription':'transcription', 'align':'alignment', 'alignment':'alignment',
               'diar':'diarization', 'diarization':'diarization', 'music':'music', 'sfx':'sound', 'sound':'sound'}
    tasks = list(found.get('tasks') or found.get('capabilities', {}))
    if not tasks:
        return CATALOG.get(family, {}).get('kinds', [])
    return list(dict.fromkeys([aliases[task] for task in tasks if task in aliases] + CATALOG.get(family, {}).get('kinds', [])))


@lru_cache(maxsize=256)
def _embedded(path, stamp):
    """Read data-only GGUF headers once per file revision; never execute a model payload."""
    from loadcoach.gguf import read_metadata
    metadata = read_metadata(path)
    found = json.loads(metadata.get('audiocpp.model_spec.json', '{}'))
    return found if isinstance(found, dict) and metadata.get('general.architecture') == 'audiocpp' else {}


def embedded_spec(path):
    """Return an audio.cpp contract embedded in a selected GGUF, when present."""
    if not str(path).lower().endswith('.gguf'):
        return {}
    try:
        stat = Path(path).stat()
        return _embedded(str(path), (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    except (OSError, ValueError):
        return {}


def declared(spec_value, scope):
    """Upstream's declared option names and descriptions for one scope (request, session, load)."""
    return {option["name"]: option for option in spec_value.get("options", {}).get(scope, [])}


def check_options(settings, manifest):
    """Refuse native option names the pinned family does not declare."""
    options = settings["audiocpp"]
    found = spec(manifest, options["family"], settings['flags']['model'])
    family = options["family"]
    if options['kind'] not in family_kinds(found, family):
        raise ValueError(f"{family} does not support the selected {options['kind']} task in this runtime.")
    for key, scope in (("session_options", "session"), ("load_options", "load"), ("defaults", "request")):
        known = declared(found, scope)
        for name in options.get(key, {}):
            if name.removeprefix(family + ".") not in known:
                raise ValueError(f"{family} declares no {scope} option {name}.")
    return found


def audio_asset(path):
    """Files in the audio place are audio.cpp packages, not llama.cpp models."""
    return "audio" in Path(path).parts


def facts(path):
    """Describe an audio package file from its place; formats are checked when a model loads."""
    file = Path(path)
    found = embedded_spec(str(file))
    return {"what": "audio", "name": found.get('display_name') or file.name, "kind": "audio package", "format": file.suffix.lstrip("."),
            'family':found.get('family',''), 'kinds':family_kinds(found), 'embedded_contract':bool(found)}


def profile(settings, manifest):
    """What an audio page needs to know of a profile: its family, kind, modes, licence, limits and declared options."""
    options = settings["audiocpp"]
    family = CATALOG.get(options["family"], {})
    try:
        declared_options = [{key: option[key] for key in ("name", "type", "default", "values", "description", "min", "max") if key in option}
                            for option in spec(manifest, options["family"], settings['flags']['model']).get("options", {}).get("request", [])]
    except (ValueError, OSError):
        declared_options = []
    load = options.get("load_options", {})
    variant = load.get("ace_step.dit_model_path", load.get("dit_model_path"))
    variant = variant or options.get("package", "") + " " + options.get("path", "")
    turbo = options["family"] == "ace_step" and "turbo" in str(variant).lower()
    return {"family": options["family"], "kind": options["kind"], "modes": family.get("modes", ["song"]), "lyrics": bool(family.get("lyrics")),
            "licence": family.get("licence", ""), "commercial": family.get("commercial", ""), "limits": limits(settings),
            "device": "cpu" if settings.get("gpus", 1) == 0 else "gpu", "options": declared_options,
            "defaults": options.get("defaults", {}), "help": family.get("help", ""), "lyrics_required": family.get("lyrics_required", False),
            "turbo": turbo}
