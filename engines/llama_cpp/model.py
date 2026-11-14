"""llama.cpp model validation, artifacts, contexts and server sharing semantics."""

import re

# Flags LoadCoach sets itself. A model server must stay on this machine, on a port we choose,
# and on the cards we gave it.
OUR_FLAGS = {"host", "port", "device"}

FILES = ("model", "mmproj")  # the flags that name a file: a model, and the projector some carry. What is in such a file is part of what the model is
ADAPTER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # the short name an adapter has within its model, which a request narrows by
FIRST_PLACE = 10  # the sort order every model starts with, so that there is room to put one before the rest and one after


def check_order(order: int | None) -> None:
    """The portable integer range shared by SQLite, PostgreSQL and configuration exports."""
    if order is not None and (type(order) is not int or not -(2**31) <= order < 2**31):
        raise ValueError("order is a whole number from -2147483648 through 2147483647; the lowest comes first. Every model starts at 10.")
ADAPTER_FLAGS = {"lora", "lora-scaled", "lora-init-without-apply"}  # llama-server's own flags for adapters. LoadCoach writes them, from `adapters`


def files_of(settings: dict) -> dict[str, str]:
    """Every file a model is made of, by what it is: `model`, `mmproj` if it has a projector, and `adapter NAME` for each adapter."""
    found = {flag: settings["flags"][flag] for flag in FILES if isinstance(settings["flags"].get(flag), str)}
    return found | {f"adapter {name}": adapter["file"] for name, adapter in settings.get("adapters", {}).items()}


def file_selection(settings: dict) -> tuple:
    """The selected artifact paths, independent of labels assigned to LoRA files."""
    return (tuple(settings['flags'].get(flag) for flag in FILES),
            tuple(sorted(adapter['file'] for adapter in settings.get('adapters',{}).values())))


def base(settings: dict) -> dict:
    """A model's settings without its adapters. What a model measured, and its fingerprint, go by these."""
    return {key: value for key, value in settings.items() if key not in ("adapters", "engine")}


def effective(settings: dict, defaults: dict) -> dict:
    """Legacy llama.cpp defaults; null removes an inherited flag."""
    flags = {key: value for key, value in {**defaults, **settings["flags"]}.items() if value is not None}
    from loadcoach.classifier import model_facts
    decision = model_facts(settings)
    if decision:
        for key in ('jinja', 'chat-template', 'chat-template-file', 'chat-template-kwargs', 'reasoning-format',
                    'reasoning-budget', 'embedding', 'temp', 'top-k', 'top-p', 'min-p'):
            flags.pop(key, None)
        flags.setdefault('parallel', 1)
        if decision['decision_type'] in ('laya', 'clef'):
            budget = flags.get('ctx-size', decision.get('trained_context') or 8192)
            flags['batch-size'] = max(flags.get('batch-size', 0), budget)
            flags['ubatch-size'] = max(flags.get('ubatch-size', 0), flags['batch-size'])
    return {**settings, 'flags': flags}


def fingerprint_settings(settings: dict, hashes: dict) -> dict:
    """Keep the historical llama.cpp fingerprint serialization exactly unchanged."""
    applied = settings.get("adapters", {})
    flags = dict(settings["flags"])
    for flag in FILES:
        if isinstance(flags.get(flag), str):
            flags[flag] = hashes[flag]
    adapters = sorted([hashes[f"adapter {name}"], float(adapter.get("scale", 1))]
                      for name, adapter in applied.items())
    return {**base(settings), "flags": flags} | ({"adapters": adapters} if adapters else {})


def retarget(settings: dict, target: dict) -> dict:
    """Apply preset tuning without changing the destination's inference artifacts."""
    flags = {key: value for key, value in settings["flags"].items() if key not in FILES}
    flags.update({key: target["flags"][key] for key in FILES if key in target["flags"]})
    return {key: value for key, value in settings.items() if key != "adapters"} | {"flags": flags} \
        | ({"adapters": target["adapters"]} if "adapters" in target else {})


def kin_base(settings: dict) -> dict:
    """A model's settings without its adapters and without its projector. Models with the same are kin: the same file, started the same way.

    One server can answer for all kin. A projector is left out here and not in `base()`: it
    takes room on a card, so a model with one is measured apart from the same model without.
    """
    return base(settings) | {"flags": {flag: value for flag, value in settings.get("flags", {}).items() if flag != "mmproj"}}


def contexts(settings: dict) -> list[int]:
    """The measured context choices: minimum, midpoint and preferred maximum, with no duplicates."""
    maximum = settings["flags"]["ctx-size"]
    minimum = settings.get("min_context", maximum)
    return sorted({minimum, (minimum + maximum) // 2, maximum}) if isinstance(maximum, int) else [maximum]


def at_context(settings: dict, context: int) -> dict:
    """A copy of model settings with just the actual loading context changed."""
    return {**settings, "flags": {**settings["flags"], "ctx-size": context}}


def request_context(flags: dict) -> int | None:
    """The request window within a fixed slot or shared pool, bounded by its explicit cap.

    Use actual loaded flags when available, since fitting may shrink the allocated pool.
    The native server remains responsible for tokenization and its training-context cap.
    """
    total = flags.get("ctx-size")
    if type(total) is not int or total <= 0:
        return None
    count = flags.get("parallel", 1)
    count = count if type(count) is int and count > 0 else 1
    shared = flags.get("kv-unified", False) and not flags.get("no-kv-unified", False)
    room = total if shared else total // count
    cap = flags.get("kv-unified-per-slot")
    return min(room, cap) if type(cap) is int and cap > 0 else room


def memory_base(settings: dict) -> dict:
    """The settings shared by measurements at different context sizes."""
    return {key: value for key, value in base(at_context(settings, 0)).items() if key != "min_context"}


def lora_field(loaded: dict, wanted: dict) -> list[dict]:
    """What tells llama-server which adapters apply to one request.

    `loaded` are the adapters the server was started with, and `wanted` those of the model that
    was asked for. Each loaded adapter is named by its number, with the strength the model
    gives it, or 0 if the model has no such adapter. An adapter is known by its file.
    """
    strength = {adapter["file"]: float(adapter.get("scale", 1)) for adapter in wanted.values()}
    return [{"id": number, "scale": strength.get(adapter["file"], 0.0)} for number, adapter in enumerate(loaded.values())]
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")  # what callers send as "model"


def flags_in(help_text: str) -> list[dict]:
    """Every flag in what `llama-server --help` prints, as it is to be written in a model's settings.

    The help is a list under a few headings. Each flag begins a line with its spellings and what
    it takes, such as `-t,    --threads N`, followed by what it does, which may run on over
    further lines. A flag with long spellings has what it does begin on the next line instead.
    """
    found, group = [], ""
    for line in help_text.splitlines():
        if line.startswith("-----"):
            group = line.strip("- ")
        elif line.startswith("-"):
            # After a comma the help pads with several spaces. With those gone, the first gap of
            # two or more is where the flag's own part ends and what it does begins.
            head, _, says = re.sub(r",\s+", ", ", line).partition("  ")
            words = head.split(" ")
            spellings = [word.rstrip(",") for word in words if word.startswith("-")]
            takes = " ".join(word for word in words if not word.startswith("-"))
            long = [spelling[2:] for spelling in spellings if spelling.startswith("--")]
            # The name to write is the first long spelling that does not switch the thing off.
            name = next((one for one in long if not one.startswith("no-")), (long or [spellings[0].lstrip("-")])[0])
            found.append({"name": name, "also": [one for one in spellings if one != f"--{name}"], "takes": takes, "says": says.strip(), "group": group})
        elif line.startswith(" ") and found:
            found[-1]["says"] = (found[-1]["says"] + " " + line.strip()).strip()
    for flag in found:  # the name of the environment variable that does the same is of no use here
        flag["says"] = re.sub(r"\s*\(env: [^)]*\)", "", flag["says"])
    return found


def check_model(name: str, settings: dict, cards: tuple[str, ...]) -> None:
    """Raise ValueError, saying why, if these settings could not be used for a model.

    Whether the model's file exists is not checked here. A file can go missing later, and that
    is a reason to refuse requests for that one model, not to refuse the settings.
    """
    if not NAME.fullmatch(name):
        raise ValueError(f"{name!r}: a model's name is letters, digits, dots, dashes, colons or slashes")
    if not isinstance(settings, dict) or settings.keys() - {"engine", "flags", "gpus", "alone", "allow_split", "adapters", "min_context"}:
        raise ValueError(f"model {name!r} may set `flags`, `gpus`, `alone`, `allow_split`, `adapters` and `min_context`. llama-server flags go in its flags")
    adapters = settings.get("adapters", {})
    # llama-server is given the adapters as FILE:STRENGTH,FILE:STRENGTH, so a path can hold neither a comma nor a colon.
    good_adapter = lambda short, one: (  # noqa: E731
        isinstance(short, str) and ADAPTER.fullmatch(short) and isinstance(one, dict) and one.keys() <= {"file", "scale"}
        and isinstance(one.get("file"), str) and one["file"] and not set(",:") & set(one["file"]) and type(one.get("scale", 1)) in (int, float)
    )  # fmt: skip
    if not isinstance(adapters, dict) or not all(good_adapter(short, one) for short, one in adapters.items()):
        raise ValueError(f"model {name!r}: adapters is a list of NAME = {{file, scale}}. A name is letters, digits, dots and dashes. "
                         "A file is a path with no comma and no colon in it. scale is a number, 1 unless said")  # fmt: skip
    if not isinstance(settings.get("alone", False), bool):
        raise ValueError(f"model {name!r}: alone is true or false")
    if not isinstance(settings.get("allow_split", False), bool):
        raise ValueError(f"model {name!r}: allow_split is true or false")
    flags = settings.get("flags")
    if not isinstance(flags, dict) or not isinstance(flags.get("model"), str) or "ctx-size" not in flags:
        raise ValueError(f"model {name!r} needs both `model` (the .gguf file) and `ctx-size` in its flags")
    parallel = flags.get("parallel")
    if parallel is not None and (type(parallel) is not int or parallel == 0 or parallel < -1):
        raise ValueError(f"model {name!r}: parallel is a positive whole number of requests, or -1 for server auto")
    for flag in ("kv-unified", "no-kv-unified"):
        if flags.get(flag) is not None and type(flags[flag]) is not bool:
            raise ValueError(f"model {name!r}: {flag} is true or false")
    cap = flags.get("kv-unified-per-slot")
    if cap is not None and (type(cap) is not int or cap < 0):
        raise ValueError(f"model {name!r}: kv-unified-per-slot is a whole number of tokens, 0 or more")
    if "min_context" in settings:
        minimum, maximum = settings["min_context"], flags["ctx-size"]
        if type(minimum) is not int or type(maximum) is not int or not 0 < minimum <= maximum:
            raise ValueError(f"model {name!r}: min_context must be a positive whole number no larger than ctx-size")
    if OUR_FLAGS & flags.keys():
        raise ValueError(f"model {name!r} must not set {sorted(OUR_FLAGS)}. LoadCoach sets those")
    if ADAPTER_FLAGS & flags.keys():
        raise ValueError(f"model {name!r} must not set {sorted(ADAPTER_FLAGS)} among its flags. Its adapters go under `adapters`, each with a name")
    gpus = settings.get("gpus", len(cards))
    if isinstance(gpus, list):
        good = bool(gpus) and len(set(gpus)) == len(gpus) and set(gpus) <= set(cards)
    else:
        good = gpus == "any" or (type(gpus) is int and 0 <= gpus <= len(cards))
    if not good:
        raise ValueError(f"model {name!r}: gpus must be any, a number from 0 to {len(cards)}, or a list of cards from {list(cards)}")
    if gpus == "any" and settings.get("allow_split") and flags.get("split-mode") == "none":
        raise ValueError(f"model {name!r}: allow_split requires a split-mode other than none")
    if "gpus" not in settings and not cards:
        raise ValueError(f"model {name!r}: list your cards in `gpus` at the top of the configuration, or give it gpus = 0")

def parallel_capacity(settings: dict) -> int:
    """Return the configured simultaneous request capacity."""
    return max(1, int(settings["flags"].get("parallel", 1)))


def embedding(settings: dict) -> bool:
    """Return whether this model serves embedding requests."""
    return bool(settings["flags"].get("embedding"))

def required_devices(settings: dict, cards: tuple[str, ...]):
    """Return the devices explicitly required by this model configuration."""
    wanted = settings.get("gpus", len(cards))
    return 1 if wanted == "any" else wanted


def layouts(settings: dict, cards: tuple[str, ...]) -> list[dict]:
    """Enumerate candidate device placements for the configured model."""
    if settings.get("gpus") != "any":
        return [settings]
    flags = dict(settings["flags"])
    if not flags.keys() & {"gpu-layers", "n-gpu-layers"}:
        flags["gpu-layers"] = "all"
    return [{**settings, "flags": {key: value for key, value in flags.items() if key != "tensor-split" or count > 1}, "gpus": count}
            for count in (1, 2) if count <= len(cards) and (count == 1 or settings.get("allow_split"))]


def fixed_split(settings: dict, count: int) -> bool:
    """Return whether the configuration pins a multi-device split."""
    split = settings["flags"].get("tensor-split")
    return (isinstance(split, list) and len(split) == count and
            all(type(weight) in (int, float) and weight > 0 for weight in split))


def answered_settings(loaded: dict, wanted: dict) -> dict:
    """Return effective engine settings suitable for request diagnostics."""
    its_own = {flag: wanted["flags"][flag] for flag in ("mmproj",) if flag in wanted["flags"]}
    return kin_base(loaded) | {"flags": kin_base(loaded)["flags"] | its_own} | ({"adapters": wanted["adapters"]} if wanted.get("adapters") else {})
