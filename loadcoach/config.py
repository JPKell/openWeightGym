"""LoadCoach's settings: reading the configuration file and refusing one that makes no sense.

Everything else is handed a `Config` and never reads the file itself.
"""

from __future__ import annotations

import math
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from db import DatabaseConfigurationError, DatabaseRegistry, DatabaseSource
from loadcoach.models import check_model, check_order, effective
from loadcoach.log import LEGACY
from loadcoach.devices import discover
from loadcoach.gpu_type import is_integrated
from engines import checked_installations
from loadcoach.settings import changed_for, comments, laid_over
from loadcoach.setting_names import current_names

@dataclass
class Config:
    """Everything LoadCoach is told. One TOML file, one key per field.

    A few settings can be changed from the site while LoadCoach runs. Those are set on this one
    object, which everything holds, so a change is seen everywhere at once. See settings.py.
    """

    # The models written in the file. They are only where the models start from: the first time
    # LoadCoach runs they are taken over into the database, and from then on that is where models
    # are kept and changed. See models.py.
    models: dict[str, dict] = field(default_factory=dict)  # name -> {"flags": ..., "gpus": ...}
    model_defaults: dict = field(default_factory=dict)  # flags every model gets unless it sets its own
    # The places model files are kept: a name for each, and its folder. Left empty, the folders
    # that the models already added are in, each named by its own path.
    places: dict[str, str] = field(default_factory=dict)
    models_folder: str = ""  # optional active-cache root; its llm, adapters, projectors, image and audio folders replace [places]
    models_storage_folder: str = ""  # optional complete library root; empty leaves this tier disabled
    hub_token_file: str = ""  # the file that holds the token of an account at Hugging Face, for repositories not open to all. Empty for none
    civitai_token_file: str = ""  # optional private Civitai download token; server-side file, never browser content
    gpus: tuple[str, ...] = ()  # the node's physical GPU inventory; independent of engine installation
    disabled_gpus: list[str] = field(default_factory=list)  # detected cards excluded under Compute nodes
    engine_gpus: dict[str, list[str]] = field(default_factory=dict)  # absent engine: all compatible enabled cards; []: CPU only
    gpu_details: list[dict] = field(default_factory=list)  # descriptions from device discovery
    machine_key_file: str = ""  # the key compute shares with front: turnstile signs what it sends on with it. Empty: only this machine may ask, as anyone
    machine_name: str = LEGACY  # the stable name this instance uses when it sends logs and asks front for accounts
    former_machine_name: str = ""  # pre-peer name retained for historical links and stored references
    front: str = ""  # turnstile's address, such as http://10.77.10.42:8442, which compute asks for the accounts
    toollocker_runs_on_front: bool = False  # ToolLocker runs on the front: this node refuses its settings and reports. Formerly front_toollocker
    database: DatabaseSource = ":memory:"  # resolved core store; a path remains accepted for compatibility
    databases: dict = field(default_factory=dict)  # named stores and logical bindings
    token_file: str = ""  # where a running LoadCoach leaves a token for the `loadcoach` command
    keep_days: float = 90  # how long event log entries are kept; 0 keeps them for ever
    batch_keep_days: float = 7  # days after a batch has ended that its files are kept: what was handed in, and the answers. 0 keeps them for ever
    keep_text: int = 50  # how many of the latest requests are kept in full, with what was sent and what came back; 0 keeps none
    unlogged: bool = False  # True lets those with the ability "unlogged" send a request that leaves nothing behind
    host: str = "127.0.0.1"
    port: int = 8442
    llama_server: str = "llama-server"  # compatibility alias for engines.llama_cpp.executable
    engines: dict = field(default_factory=dict)  # per-node engine enablement and executable paths
    nvidia_smi: str = "nvidia-smi"  # the program that says what each card holds and what each process uses of it
    card_margin: int = 1024  # MiB of each card that are kept free, on top of what the models on it measured
    model_idle_minutes: float = 0  # optional minimum idle time before making room; 0 replaces idle models immediately
    host_ram_headroom_mib: int = 512  # reserve host RAM; systemd memory ceilings still apply
    host_ram_limit_mib: int = 0  # optional CPU-model RAM budget; 0 uses host/service limits
    disable_ram_offload: bool = True  # disable CUDA unified-memory spill for GPU model servers
    cpu_idle_minutes: float = 15  # unload idle CPU models; 0 disables the timer
    load_timeout: int = 300
    max_wait: int = 600
    max_estimated_wait: float = 0  # optional admission limit in seconds; 0 keeps existing admission
    data: str = "."  # the folder LoadCoach keeps its database and its token in, counted from the settings file
    path: str = ""  # the settings file itself
    file: dict = field(default_factory=dict)  # what the defaults and that file say, before anything changed on the site is laid over them
    says: dict = field(default_factory=dict)  # the comment above each setting in the file, shown beside it on the site


# How every program here sets up its command line, so that their help pages look alike. No
# offer to install shell completion, -h as well as --help, and no values of variables printed
# with an error, since a key or a password may be among them.
CLI = {"add_completion": False, "pretty_exceptions_show_locals": False, "context_settings": {"help_option_names": ["-h", "--help"]}}


def read_toml(path: Path) -> dict:
    """A settings file, read. One that is missing, or is not valid TOML, stops startup with one plain line saying where."""
    try:
        return tomllib.loads(path.read_text())
    except OSError as problem:
        raise SystemExit(f"{path}: cannot be read: {problem.strerror}") from None
    except tomllib.TOMLDecodeError as problem:  # a slip of the keyboard, such as a quote left open
        raise SystemExit(f"{path}: this is not valid TOML. {problem}") from None


def read_settings(path: Path) -> dict:
    """What a program is told: its defaults, with its own settings file laid over them.

    The settings file holds what belongs to the machine: addresses, ports, paths. The
    defaults are in a folder `defaults` beside it, in a file of the same name. They hold the
    starting value of everything that can be changed from the site, and for LoadCoach the
    first groups and models. A setting in both is the settings file's. A table in both has
    its keys put together, the settings file's winning.
    """
    from loadcoach.settings import program_name
    defaults, told = path.parent / "defaults" / (program_name(path.stem) + path.suffix), read_toml(path)
    first = read_toml(defaults) if defaults.is_file() else {}
    both = {name: {**first[name], **told[name]} for name in first.keys() & told.keys() if isinstance(first[name], dict) and isinstance(told[name], dict)}
    return {**first, **told, **both}


def said_in(path: Path) -> dict:
    """The comment above each setting, from the defaults and from the settings file. The settings file's is used where it has one."""
    from loadcoach.settings import program_name
    defaults = path.parent / "defaults" / (program_name(path.stem) + path.suffix)
    first = comments(defaults.read_text()) if defaults.is_file() else {}
    return {**first, **{name: says for name, says in comments(path.read_text()).items() if says or name not in first}}


def data_folder(path: Path, told: dict) -> Path:
    """Where a program keeps what it writes: the folder its settings name as `data`, counted from the settings file. Unsaid, beside that file."""
    return path.parent / told.get("data", ".")


def kept_by_loadcoach(path: Path) -> tuple[dict, DatabaseSource, str]:
    """For the programs that share LoadCoach's users and log: what LoadCoach's settings say, where its database is, and where it leaves its token."""
    told = read_settings(path)
    folder = data_folder(path, told)
    legacy = told.get("database") or str(folder / f"{path.stem}.db")
    if not isinstance(legacy, str):
        raise SystemExit(f"{path}: database must be a path; named stores belong under [databases]")
    try:
        source = (DatabaseRegistry.from_config(told.get("databases"), path.parent, {"loadcoach.core": legacy})
                  .database("loadcoach.core") if told.get("databases") else legacy)
    except DatabaseConfigurationError as problem:
        raise SystemExit(f"{path}: {problem}") from None
    return told, source, str(folder / f"{path.stem}.cli-token")


def load_config(path: Path) -> Config:
    """Read the TOML file, and lay over it what was changed on the site. Anything wrong with the file stops startup with a message naming the cause."""
    told, database, _ = kept_by_loadcoach(path)
    changed = changed_for("loadcoach", database)
    try:
        return config_from(path, laid_over(told, changed, "loadcoach"), told)
    except SystemExit as problem:
        if not changed:
            raise
        # Something changed on the site must never keep LoadCoach from starting. The file is what it falls back on.
        print(f"loadcoach: a setting changed on the site cannot be used: {problem}. Starting with the file's own settings. "
              f"See the changes with: loadcoach settings", file=sys.stderr, flush=True)  # fmt: skip
        return config_from(path, told, told)


def config_from(path: Path, data: dict, file: dict) -> Config:
    """The settings that `data` makes, checked. `file` is what the file itself says, kept to be shown."""
    try:
        data = current_names(data, str(path))  # renamed and retired keys (docs/peer-nodes-plan.md)
    except ValueError as problem:
        raise SystemExit(str(problem)) from None
    if "groups" in data:
        raise SystemExit(f"{path}: [groups] is kept on the front machine since milestone 76. Remove it here; add groups there with: weightroom group add NAME")
    if "api_keys" in data:
        raise SystemExit(
            f"{path}: api_keys is gone. Remove it, define a group, and add users with: "
            f"weightroom user add NAME GROUP, on the front machine"
        )
    if not isinstance(data.get("model_defaults", {}), dict) or not isinstance(data.get("models", {}), dict):
        raise SystemExit(f"{path}: [model_defaults] and [models] must be tables")
    machine_name = data.get("machine_name", LEGACY)
    if not isinstance(machine_name, str) or not (machine_name.isidentifier() or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", machine_name)):
        raise SystemExit(f"{path}: machine_name must be a stable identifier, matching [machines.NAME] on front")
    for gone in ("hosted", "keys"):  # outside services are the front machine's since milestone 114: their keys are files there, and compute makes no outside calls
        if data.pop(gone, None) is not None:
            print(f"loadcoach: {path}: [{gone}] is ignored. Outside services and their keys are the front machine's: set them up under System > Outside on the site, "
                  f"and put each key in a file NAME.key in its keys folder.", file=sys.stderr, flush=True)
    try:
        data["places"] = model_places(data.get("models_folder", ""), data.get("places", {}), data.get("models_storage_folder", ""))
        checked_installations(data.get("engines", {}), data.get("llama_server", "llama-server"))
        options = data.get("engines", {}).get("llama_cpp", {})
        selected = options.get("executable", data.get("llama_server", "llama-server"))
        if "executable" in options and "llama_server" in data and data["llama_server"] != selected:
            raise ValueError("llama_server conflicts with engines.llama_cpp.executable; keep one path or matching values")
        data["llama_server"] = selected
        automatic = data.get("gpus", "auto") == "auto"
        detected = discover(selected if options.get("enabled", True) else '', nvidia_smi=data.get('nvidia_smi', 'nvidia-smi'),
                            include_integrated=True) if automatic or data.get('gpus') else []
        usable = [card for card in detected if not is_integrated(card)]
        excluded = {name for card in detected if is_integrated(card)
                    for name in (card['id'], card.get('llama_device'), card.get('uuid'),
                                 f"{card['vendor']}@{card['pci']}" if card.get('pci') else None) if name}
        if isinstance(data.get('disabled_gpus'), list):
            data['disabled_gpus'] = [card for card in data['disabled_gpus'] if card not in excluded]
        if isinstance(data.get('engine_gpus'), dict):
            data['engine_gpus'] = {engine: [card for card in cards if card not in excluded] if isinstance(cards, list) else cards
                                   for engine, cards in data['engine_gpus'].items()}
        if not automatic:
            data['gpus'] = [card for card in data.get('gpus', ()) if card not in excluded]
            # Preserve legacy names while attaching the exact matched device's classification.
            usable = [{**card, 'id': name} for name in data['gpus'] for card in usable
                      if name in (card['id'], card.get('llama_device'),
                                  f"{card['vendor']}@{card['pci']}" if card.get('pci') else None)]
        config = Config(
            **{
                **data,
                "gpus": tuple(card["id"] for card in usable) if automatic else tuple(data.get("gpus", ())),
                "gpu_details": usable,
                "hub_token_file": str(Path(path).parent / data["hub_token_file"]) if data.get("hub_token_file") else "",  # counted from the settings file
                "civitai_token_file": str(Path(path).parent / data["civitai_token_file"]) if data.get("civitai_token_file") else "",
                "machine_key_file": str(Path(path).parent / data["machine_key_file"]) if data.get("machine_key_file") else "",  # counted from the settings file
                "database": kept_by_loadcoach(path)[1],  # in the data folder, unless the file names another place
                "token_file": kept_by_loadcoach(path)[2],
                "path": str(path),
                "file": file,
                "says": said_in(path),
            }
        )
    except (TypeError, ValueError) as problem:  # unknown or missing key, or a group that makes no sense
        raise SystemExit(f"{path}: {problem}") from None
    return checked(config, path)


def model_places(models_folder, places, models_storage_folder="") -> dict[str, str]:
    """The active-cache destinations, plus an optional complete storage library."""
    if models_storage_folder and (not isinstance(models_storage_folder, str) or not models_storage_folder.startswith("/")):
        raise ValueError("models_storage_folder must be a full path")
    if models_folder:
        if not isinstance(models_folder, str) or not models_folder.startswith("/"):
            raise ValueError("models_folder must be a full path")
        root = Path(models_folder)
        made = {name: str(root / name) for name in ("llm", "adapters", "projectors", "image", "audio", "encoders", "classifier")}
    else:
        made = dict(places)
    if models_storage_folder:
        root = Path(models_storage_folder)
        made.update({f"storage_{name}": str(root / name) for name in ("llm", "adapters", "projectors", "image", "audio", "encoders", "classifier")})
    return made


# Files from Hugging Face and Civitai are kept in the storage library alone. The active cache
# holds only copies promoted from storage for use, which cache cleanup can remove again.
NO_STORAGE = ("Downloads from Hugging Face and Civitai go into model storage, never the cache. "
              "Set a model storage folder in Settings → Model file locations first.")


def storage_places(places: dict[str, str]) -> dict[str, str]:
    """The storage places alone: where downloads from Hugging Face and Civitai may go."""
    return {name: folder for name, folder in places.items() if name.startswith("storage_")}


def shared_places(places: dict[str, str]) -> set[str]:
    """The places whose folder is both a cache place and a storage place, as when models_folder and
    models_storage_folder are set to one folder. A file there is in the cache and in storage at once,
    so it is listed once, and nothing may take it for a second copy of itself, to save or to remove."""
    real = {name: os.path.realpath(folder) for name, folder in places.items()}
    cache = {folder for name, folder in real.items() if not name.startswith("storage_")}
    storage = {folder for name, folder in real.items() if name.startswith("storage_")}
    return {name for name, folder in real.items() if folder in cache & storage}


def checked(config: Config, path) -> Config:
    """The same settings, if they make sense together. If not, SystemExit with a message that names the cause.

    Used for a file being read, and for a setting about to be changed from the site.
    """
    try:
        checked_installations(config.engines, config.llama_server)
        selected = config.engines.get("llama_cpp", {}).get("executable", config.llama_server)
        if selected != config.llama_server:
            raise ValueError("llama_server conflicts with engines.llama_cpp.executable")
    except ValueError as problem:
        raise SystemExit(f"{path}: {problem}") from None
    if type(config.toollocker_runs_on_front) is not bool or config.toollocker_runs_on_front and not config.front:
        raise SystemExit(f"{path}: toollocker_runs_on_front is a boolean and requires a front address")
    for name in ("load_timeout", "max_wait", "keep_days", "model_idle_minutes", "cpu_idle_minutes", "batch_keep_days"):
        if type(getattr(config, name)) not in (int, float) or getattr(config, name) < 0:
            raise SystemExit(f"{path}: {name} is a number, 0 or more")
    if not math.isfinite(config.model_idle_minutes) or not math.isfinite(config.cpu_idle_minutes):
        raise SystemExit(f"{path}: model_idle_minutes and cpu_idle_minutes must be finite")
    if type(config.max_estimated_wait) not in (int, float) or not 0 <= config.max_estimated_wait < 1e12 or not math.isfinite(config.max_estimated_wait):
        raise SystemExit(f"{path}: max_estimated_wait is a finite number from 0 to below 1e12; 0 disables estimated-wait refusals")
    if not isinstance(config.model_defaults, dict) or not all(isinstance(card, str) for card in config.gpus):
        raise SystemExit(f"{path}: model_defaults is a table of flags, and gpus a list of the cards' names")
    if not isinstance(config.disabled_gpus, list) or not all(isinstance(card, str) for card in config.disabled_gpus) or len(set(config.disabled_gpus)) != len(config.disabled_gpus):
        raise SystemExit(f"{path}: disabled_gpus is a list of distinct card names")
    if not isinstance(config.engine_gpus, dict):
        raise SystemExit(f"{path}: engine_gpus is a table of engine IDs and card lists")
    from engines import engine_for
    for engine, cards in config.engine_gpus.items():
        try:
            engine_for(engine)
        except ValueError as problem:
            raise SystemExit(f'{path}: {problem}') from None
        if not isinstance(cards, list) or not all(isinstance(card, str) and card for card in cards) or len(cards) != len(set(cards)):
            raise SystemExit(f'{path}: engine_gpus.{engine} is a list of distinct card names')
    if type(config.keep_text) is not int or config.keep_text < 0:
        raise SystemExit(f"{path}: keep_text is a whole number of requests, 0 or more")
    for name in ("host_ram_headroom_mib", "host_ram_limit_mib"):
        if type(getattr(config, name)) is not int or getattr(config, name) < 0:
            raise SystemExit(f"{path}: {name} is a whole number of MiB, 0 or more")
    if type(config.card_margin) is not int or config.card_margin < 0:
        raise SystemExit(f"{path}: card_margin is a whole number of MiB, 0 or more")
    if type(config.disable_ram_offload) is not bool:
        raise SystemExit(f"{path}: disable_ram_offload is true or false")
    if not isinstance(config.unlogged, bool):
        raise SystemExit(f"{path}: unlogged is true or false")
    if not isinstance(config.models_folder, str) or config.models_folder and not config.models_folder.startswith("/"):
        raise SystemExit(f"{path}: models_folder must be a full path, or empty")
    if not isinstance(config.models_storage_folder, str) or config.models_storage_folder and not config.models_storage_folder.startswith("/"):
        raise SystemExit(f"{path}: models_storage_folder must be a full path, or empty")
    if not isinstance(config.places, dict) or not all(isinstance(folder, str) and folder.startswith("/") for folder in config.places.values()):
        raise SystemExit(f'{path}: [places] is a list of name = "/a/folder", each a full path')
    try:
        for name, model in config.models.items():
            if isinstance(model, dict):
                check_order(model.get("order"))
                model = {key: value for key, value in model.items() if key != "order"}
            # Checked with the defaults in place, so that a default is held to the same rules.
            flags = model.get("flags") if isinstance(model, dict) else None
            merged = effective(model, config.model_defaults) if isinstance(flags, dict) else model
            check_model(name, merged, config.gpus)
    except ValueError as problem:
        raise SystemExit(f"{path}: {problem}") from None
    return config
