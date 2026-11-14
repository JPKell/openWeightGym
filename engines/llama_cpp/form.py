"""Dedicated model form controls and the llama-server flags they represent.

The Embedding model box owns llama.cpp's own `--embedding` flag (saved as `flags.embedding`).
LoadCoach advertises a model with it as an embedder in `/v1/models` (`"embedding": true`), and
Collections, embedder benchmarks and `/v1/embeddings` rely on that mark. It is a llama.cpp flag:
another engine that serves embeddings needs its own control and must set the same mark, or its
models will be missing from every embedder list.
"""

# llama-server's flag for serving embeddings instead of completions. See the module docstring.
EMBEDDING_FLAG = "embedding"

PARALLEL_FLAGS = ("parallel", "kv-unified", "kv-unified-per-slot", "no-kv-unified")

# These cannot form part of LoadCoach's persistent one-model llama-server child. The first group
# prints information instead of serving; the others replace LoadCoach's local-file model source,
# switch llama-server into router mode, or make its fixed internal HTTP client unable to connect.
UNUSABLE_MODEL_FLAGS = frozenset({
    "help", "version", "cache-list", "completion-bash", "list-devices",
    "model-url", "docker-repo", "hf-repo", "hf-file", "hf-token",
    "models-dir", "models-preset", "models-max", "models-autoload",
    "api-prefix", "api-key", "api-key-file", "ssl-key-file", "ssl-cert-file",
})


def usable_model_flags(flags: list[dict]) -> list[dict]:
    """llama-server options that can be saved on a LoadCoach-managed model."""
    return [flag for flag in flags if flag.get("name") not in UNUSABLE_MODEL_FLAGS]


def parallel_fields(flags: dict, trained_context: int | None = None) -> dict:
    """Show saved parallel flags, with useful initial values before enabling them.

    Existing explicit slot counts keep their non-unified default. New setups start with a
    shared pool. The training window supplies the initial request limit when known.
    """
    flags = {key: value for key, value in flags.items() if value is not None}
    enabled = any(key in flags for key in PARALLEL_FLAGS)
    count = flags.get("parallel", 1 if enabled else 2)
    divisor = count if type(count) is int and count > 0 else 2
    total = flags.get("ctx-size")
    fallback = max(1, total // divisor) if type(total) is int and total > 0 else ""
    initial = min(trained_context, total) if trained_context and type(total) is int and total > 0 else trained_context or fallback
    context = flags.get("kv-unified-per-slot", initial)
    return {"enabled": enabled, "count": count,
            "shared": flags.get("kv-unified", False if enabled else True) and not flags.get("no-kv-unified", False),
            "context": context,
            **({"exceeds_total": True} if type(context) is int and type(total) is int and total > 0 and context > total else {})}


def suggested(settings: dict, defaults: dict, cards: list[str]) -> list[dict]:
    """The flags a model on cards would do well to set and does not: each with a value and why. Its page offers them.

    There are two, and both are ones LoadCoach itself leans on. Any other flag is the owner's to judge.
    """
    flags, wanted = {**defaults, **settings.get("flags", {})}, settings.get("gpus", len(cards))
    if wanted == "any" and not flags.keys() & {"gpu-layers", "n-gpu-layers"}:
        flags["gpu-layers"] = "all"  # automatic placement supplies this already
    count = 1 if wanted == "any" else len(wanted) if isinstance(wanted, list) else wanted  # how many cards the model is on. 0 is the processor alone
    found = []
    if count and not flags.keys() & {"gpu-layers", "n-gpu-layers"}:
        why = ("Left out, llama-server puts as much of the model on its cards as it finds room for and leaves the rest to the processor, which is many times slower, without a word. "
               "With all, a model that does not fit refuses to load, and says so. The speed benchmarks refuse a model without it.")  # fmt: skip
        found.append({"flag": "gpu-layers", "value": "all", "why": why})
    if count > 1 and "tensor-split" not in flags:
        why = (f"Divides the model evenly over its {count} cards. Left out, llama-server divides it by the room it finds, differently from one load to the next, so the model needs its cards empty. "
               "With the division set, what the model measured on each card holds, and other models can share its cards.")  # fmt: skip
        found.append({"flag": "tensor-split", "value": [1] * count, "why": why})
    return found



def settings_from(form: dict[str, list[str]], read) -> dict:
    """A model's settings, as the form on its page gives them. The same shape LoadCoach keeps."""
    one = lambda name: form.get(name, [""])[-1].strip()  # noqa: E731
    # A box left empty is left out, so that LoadCoach says what is missing in its own words.
    flags = {flag: value for flag, value in (("model", one("file")), ("ctx-size", read(one("ctx")))) if value != ""}
    for flag, value in zip(form.get("flag", []), form.get("value", [])):
        if flag.strip() and value.strip():  # a row with no value is a flag taken away
            flags[flag.strip().lstrip("-")] = read(value.strip())
    for ticked in form.get("suggested", []):  # a suggestion that was ticked: the flag and its value, as the page wrote them. A row typed for the same flag wins
        flag, _, value = ticked.partition("=")
        flags.setdefault(flag, read(value))
    if one("projector"):  # chosen from the list of projectors. "none" is the empty choice, and leaves the flag out
        flags["mmproj"] = one("projector")
    # The model page's box owns the flag, even if a raw row was typed. Its marker says how the flag
    # was saved, so an unticked box keeps an explicit false or an ignored inherited default as it was.
    kept = one("embedding_controls")
    if kept:
        flags.pop(EMBEDDING_FLAG, None)
        if kept in ("false", "none") and not one("embedding"):
            flags[EMBEDDING_FLAG] = False if kept == "false" else None
    if one("embedding"):  # the add form and the model page: an embedder, not a chat model
        flags[EMBEDDING_FLAG] = True
    if one("parallel_controls"):
        for flag in PARALLEL_FLAGS:
            flags.pop(flag, None)  # these fields own their flags, even if a raw row was typed
        if one("parallel_enabled"):
            flags.update({"parallel": read(one("parallel_count")),
                          "kv-unified": bool(one("parallel_shared")),
                          "kv-unified-per-slot": read(one("parallel_context"))})
    wanted, made, chosen = one("cards"), {"flags": flags}, []
    if one("engine"):
        made["engine"] = one("engine")
    if one("placement_controls"):
        chosen = list(dict.fromkeys(card.strip() for card in form.get("gpu_card", []) if card.strip()))
        if one("cpu"):
            made["gpus"] = 0
        elif chosen:
            made["gpus"] = chosen
        elif one("keep_gpu_count"):
            made["gpus"] = read(one("keep_gpu_count"))
        elif not one("keep_default_gpus"):
            made["gpus"] = "any"
    elif wanted and wanted != "all":  # compatibility with the add-model form and older clients
        made["gpus"] = "any" if wanted.lower() == "any" else int(wanted) if wanted.isdigit() else [card.strip() for card in wanted.split(",") if card.strip()]
    if one("min_context"):
        made["min_context"] = read(one("min_context"))
    if one("alone"):  # a box left unticked is left out, which is the same as false
        made["alone"] = True
    if (one("allow_split") or len(chosen) > 1) and made.get("gpus") != 0:
        made["allow_split"] = True
    adapters = {}
    for short, file, scale in zip(form.get("adapter", []), form.get("adapter_file", []), form.get("adapter_scale", [])):
        if short.strip() and file.strip():  # a row with its name or its file emptied is an adapter taken away
            adapters[short.strip()] = {"file": file.strip()} | ({"scale": read(scale.strip())} if scale.strip() else {})
    if adapters:
        made["adapters"] = adapters
    for flag in form.get("suppress_flag", []):
        if flag.strip():
            flags[flag.strip().lstrip("-")] = None
    return made
