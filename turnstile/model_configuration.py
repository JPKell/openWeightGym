"""Settings a logical model shares across its copies (generation defaults, projector, adapters, list
position); every loading setting stays local to its node."""
from copy import deepcopy
from pathlib import Path

from engines.llama_cpp.model import FIRST_PLACE

GLOBAL_FLAGS = frozenset({
    "temp", "temperature", "top-k", "top-p", "min-p", "typical", "typical-p",
    "repeat-penalty", "repeat-last-n", "presence-penalty", "frequency-penalty",
    "dry-multiplier", "dry-base", "dry-allowed-length", "dry-penalty-last-n",
    "dry-sequence-breaker", "mirostat", "mirostat-lr", "mirostat-ent", "samplers",
    "sampling-seq", "seed", "predict", "n-predict", "ignore-eos", "logit-bias",
    "grammar", "grammar-file", "json-schema", "reasoning", "reasoning-effort",
    "reasoning-budget", "reasoning-format", "chat-template", "chat-template-file",
    "chat-template-kwargs", "jinja", "penalize-nl", "xtc-probability", "xtc-threshold",
    "dynatemp-range", "dynatemp-exp", "top-n-sigma", "adaptive-target", "adaptive-decay",
})
SETTING_KEYS = ("engine", "flags", "strata", "sdcpp", "audiocpp", "whispercpp", "gpus", "alone",
                "allow_split", "adapters", "min_context", "order")
PROFILE_GLOBALS = {"sdcpp": ("defaults", "limits", "reference_megapixels"),
                   "audiocpp": ("defaults", "limits")}


def configuration(row):
    """Strip inventory state before sending settings back to a worker."""
    return deepcopy({key: row[key] for key in SETTING_KEYS if key in row})


def generation(settings):
    """Return only generation behavior, independent of device, context or concurrency."""
    result = {"flags": {key: value for key, value in settings.get("flags", {}).items() if key in GLOBAL_FLAGS}}
    for profile, keys in PROFILE_GLOBALS.items():
        if profile in settings:
            result[profile] = {key: deepcopy(settings[profile][key]) for key in keys if key in settings[profile]}
    return result


def with_generation(target, shared):
    """Replace global defaults while retaining every destination loading option and file."""
    result = deepcopy(target)
    result["flags"] = {key: value for key, value in result["flags"].items() if key not in GLOBAL_FLAGS} | shared["flags"]
    for profile, keys in PROFILE_GLOBALS.items():
        if profile in result:
            result[profile] = {key: value for key, value in result[profile].items() if key not in keys} | shared.get(profile, {})
    return result


def shared(settings, order=None):
    """What every copy of a logical model keeps the same: generation defaults, the projector,
    the adapters and the list position. File paths are as the copy being read keeps them."""
    adapters = {name: dict(adapter) for name, adapter in (settings.get("adapters") or {}).items()}
    order = settings.get("order") if order is None else order
    return {"generation": generation(settings), "projector": settings.get("flags", {}).get("mmproj"),
            "adapters": adapters, "order": FIRST_PLACE if order is None else order}


def with_shared(target, values, translate):
    """Give a copy the shared settings while keeping its own loading options and model file.
    `translate(path)` names the same file on the copy's node; it raises when that node lacks it."""
    result = with_generation(target, values["generation"])
    if values["projector"]:
        result["flags"]["mmproj"] = translate(values["projector"])
    else:
        result["flags"].pop("mmproj", None)
    adapters = {name: {**adapter, "file": translate(adapter["file"])} for name, adapter in values["adapters"].items()}
    if adapters:
        result["adapters"] = adapters
    else:
        result.pop("adapters", None)
    result["order"] = values["order"]
    return result


def comparable(values):
    """Shared settings with each file named by its place-relative name, so copies can be compared."""
    name = lambda path: Path(path).name if path else path  # noqa: E731
    return {**values, "projector": name(values["projector"]),
            "adapters": {key: {**adapter, "file": name(adapter["file"])} for key, adapter in values["adapters"].items()}}


def translate(path, source, target, nodes):
    """The same managed file on another node: the same place name and relative path, as an
    installation places it, or the very same path. None when the target has no such file."""
    if source == target:
        return path
    found = {node["name"]: node for node in nodes}
    origin, destination = found.get(source, {}), found.get(target, {})
    present = {file["path"] for file in destination.get("files", [])}
    file = next((one for one in origin.get("files", []) if one["path"] == path), None)
    if file is not None:
        root = next((place["folder"] for place in origin.get("places", []) if place["name"] == file["place"]), None)
        folder = next((place["folder"] for place in destination.get("places", []) if place["name"] == file["place"]), None)
        if root and folder:
            moved = str(Path(folder) / Path(file["path"]).relative_to(root))
            if moved in present:
                return moved
    return path if path in present else None


def relocate(settings, paths):
    """Relocate a complete bundle without losing relative audio package layout."""
    result = deepcopy(settings)
    for key in ("model", "mmproj"):
        if result["flags"].get(key) in paths:
            result["flags"][key] = paths[result["flags"][key]]
    for adapter in result.get("adapters", {}).values():
        adapter["file"] = paths.get(adapter["file"],adapter["file"])
    image = result.get("sdcpp", {})
    image["components"] = {key: paths.get(path,path) for key, path in image.get("components", {}).items()} if "components" in image else {}
    if image.get("upscaler"):
        image["upscaler"] = paths.get(image["upscaler"],image["upscaler"])
    for lora in image.get("loras", {}).values():
        lora["file"] = paths.get(lora["file"],lora["file"])
    if result.get('strata'):
        from pathlib import Path
        vocab = str(Path(result['strata']['tokenizer']) / 'vocab.json')
        result['strata']['tokenizer'] = str(Path(paths[vocab]).parent)
    audio = result.get("audiocpp", {})
    if audio:
        from pathlib import Path
        old = Path(settings["flags"]["model"])
        new = Path(result["flags"]["model"])
        root = Path(audio["path"])
        audio["path"] = str(new) if root == old else str(new.parents[len(old.relative_to(root).parts) - 1])
    return result
