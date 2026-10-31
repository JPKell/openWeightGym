"""Generation defaults shared by a logical model; all loading settings stay local."""
from copy import deepcopy

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
SETTING_KEYS = ("engine", "flags", "strata", "sdcpp", "audiocpp", "gpus", "alone",
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
