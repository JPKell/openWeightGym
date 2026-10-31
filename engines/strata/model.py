"""Typed, single-GPU Strata model settings; no llama.cpp flags are accepted.

`flags.model` and `flags.ctx-size` retain openWeightGym's artifact/context envelope. They are
not passed to Strata as native flags. The registered adapter is disabled by default.
"""
from pathlib import Path

from engines.llama_cpp.model import NAME

TOKENIZER_FILES = ("vocab.json", "merges.txt", "token_type.json", "chat_template.jinja")
OUR_FLAGS = frozenset()


def absolute_path(value, label: str) -> str:
    """Resolve and validate an absolute engine artifact path."""
    if not isinstance(value, str) or not value.strip() or "\0" in value or not Path(value).is_absolute():
        raise ValueError(f"Strata {label} must be a nonempty absolute path.")
    return value


def check_model(name: str, settings: dict, cards: tuple[str, ...]) -> None:
    """Reject model configuration that violates the engine contract."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("A Strata model name uses letters, digits, dots, dashes, colons or slashes.")
    if not isinstance(settings, dict) or settings.keys() - {"engine", "flags", "strata", "gpus", "alone", "min_context"}:
        raise ValueError("Strata accepts engine, flags, strata, gpus, alone and min_context only; no adapters or GPU splitting.")
    if settings.get("engine") != "strata":
        raise ValueError("Strata settings require engine = strata.")
    flags = settings.get("flags")
    if not isinstance(flags, dict) or flags.keys() != {"model", "ctx-size"}:
        raise ValueError("Strata flags contain only model and ctx-size, not llama.cpp flags.")
    absolute_path(flags["model"], "weights")
    maximum = flags["ctx-size"]
    if type(maximum) is not int or not 256 <= maximum <= 8192:
        raise ValueError("Strata context must be a whole number from 256 through 8192; larger contexts are unqualified.")
    options = settings.get("strata")
    if not isinstance(options, dict) or options.keys() != {"tokenizer"}:
        raise ValueError("Strata model settings require only a prepared tokenizer directory.")
    absolute_path(options["tokenizer"], "tokenizer directory")
    minimum = settings.get("min_context", maximum)
    if type(minimum) is not int or not 256 <= minimum <= maximum:
        raise ValueError("Strata min_context must be a whole number between 256 and ctx-size.")
    if type(settings.get("alone", False)) is not bool:
        raise ValueError("Strata alone must be true or false.")
    wanted = settings.get("gpus", "any")
    if not (wanted == "any" or type(wanted) is int and wanted == 1 or
            isinstance(wanted, list) and len(wanted) == 1 and isinstance(wanted[0], str) and wanted[0] in cards):
        raise ValueError("Strata requires one GPU: any, 1, or one enabled card. CPU-only and splitting are unqualified.")


def effective(settings: dict, defaults: dict) -> dict:
    """Never inherit llama.cpp's global model defaults into a Strata model."""
    return {**settings, "flags": dict(settings["flags"])}


def files_of(settings: dict) -> dict[str, str]:
    """Return the model, projector and adapter paths referenced by the settings."""
    folder = Path(settings["strata"]["tokenizer"])
    return {"model": settings["flags"]["model"],
            **{name: str(folder / name) for name in TOKENIZER_FILES}}


def file_selection(settings: dict) -> tuple:
    """Return the file choices used to distinguish model configurations."""
    return tuple(files_of(settings).items())


def base(settings: dict) -> dict:
    # Retain the discriminator: identical GGUFs on different engines cannot share evidence.
    """Return model settings with adapter-specific differences removed."""
    return {**settings, "engine": "strata"}


def fingerprint_settings(settings: dict, hashes: dict) -> dict:
    """Replace every inference artifact's location with its content identity."""
    return {**base(settings), "flags": {**settings["flags"], "model": hashes["model"]},
            "strata": {"tokenizer": {name: hashes[name] for name in TOKENIZER_FILES}}}


def retarget(settings: dict, target: dict) -> dict:
    """Return model settings pointing to the replacement artifact paths."""
    return {**settings, "flags": {**settings["flags"], "model": target["flags"]["model"]},
            "strata": dict(target["strata"])}


def kin_base(settings: dict) -> dict:
    """Return the settings that identify models eligible to share a server."""
    return base(settings)


def contexts(settings: dict) -> list[int]:
    """Return the supported minimum, midpoint and maximum context sizes."""
    maximum = settings["flags"]["ctx-size"]
    minimum = settings.get("min_context", maximum)
    return sorted({minimum, (minimum + maximum) // 2, maximum})


def at_context(settings: dict, context: int) -> dict:
    """Return model settings adjusted to the requested context size."""
    return {**settings, "flags": {**settings["flags"], "ctx-size": context}}


def memory_base(settings: dict) -> dict:
    """Return the settings used to key model memory measurements."""
    return {key: value for key, value in base(at_context(settings, 0)).items() if key != "min_context"}


def parallel_capacity(settings: dict) -> int:
    """Return the configured simultaneous request capacity."""
    return 1


def embedding(settings: dict) -> bool:
    """Return whether this model serves embedding requests."""
    return False


def required_devices(settings: dict, cards: tuple[str, ...]):
    """Return the devices explicitly required by this model configuration."""
    wanted = settings.get("gpus", "any")
    return 1 if wanted == "any" else wanted


def layouts(settings: dict, cards: tuple[str, ...]) -> list[dict]:
    """Enumerate candidate device placements for the configured model."""
    return [{**settings, "gpus": 1}] if settings.get("gpus", "any") == "any" else [settings]


def fixed_split(settings: dict, count: int) -> bool:
    """Return whether the configuration pins a multi-device split."""
    return False


def answered_settings(loaded: dict, wanted: dict) -> dict:
    """Return effective engine settings suitable for request diagnostics."""
    return base(loaded)
