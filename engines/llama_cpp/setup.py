"""llama.cpp setup checks. Placement remains the Runner's decision at request time."""

from __future__ import annotations

import math
import os
import re
import shlex
import time

from loadcoach.gguf import facts, goes_with, read_metadata
from loadcoach.models import ADAPTER_FLAGS, OUR_FLAGS, check_model, files_of, kin_base, request_context
from engines import engine_for
from engines.llama_cpp.adapter import engine_stamp
from loadcoach.model_setup import digest


def preview(name: str, candidate: dict, library, runner, supported: list[dict], engine_error: str = "") -> dict:
    """Check files, inherited flags and a scheduler snapshot without starting or stopping models.

    The review token covers editable state, defaults, file stats and engine help. It is an
    optimistic concurrency guard, not a reservation or a certificate of model quality.
    """
    errors, warnings, artifacts = [], [], []
    before = library.setup(name)
    settings = candidate.get("settings")
    effective = {}
    try:
        if set(candidate) != {"settings", "order"} or type(candidate["order"]) is not int or not -(2**31) <= candidate["order"] < 2**31:
            raise ValueError("A configuration contains settings and a sort order from -2147483648 through 2147483647.")
        if not isinstance(settings, dict) or not isinstance(settings.get("flags"), dict):
            raise ValueError("Settings must contain a flags object.")
        if not isinstance(settings["flags"].get("model"), str) or "ctx-size" not in settings["flags"]:
            raise ValueError("Keep the model file and maximum context explicitly in the configuration.")
        effective = {**settings, "flags": {k: v for k, v in {**library.defaults, **settings["flags"]}.items() if v is not None}}
        check_model(name, effective, runner.config.gpus)
        if not engine_for(effective).installation(runner.config).enabled:
            raise ValueError("The selected inference engine is disabled on this node.")
        if type(effective["flags"].get("ctx-size")) is not int or effective["flags"]["ctx-size"] <= 0:
            raise ValueError("Context must be a positive whole number.")
        if effective["flags"].get("mmproj") and not isinstance(effective["flags"]["mmproj"], str):
            raise ValueError("The projector must be a file path.")
        for flag, value in effective["flags"].items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", flag):
                raise ValueError(f"Invalid flag name: {flag!r}.")
            values = value if isinstance(value, list) else [value]
            if any(type(v) not in (str, int, float, bool) or isinstance(v, float) and not math.isfinite(v) for v in values):
                raise ValueError(f"--{flag} needs text, a finite number, a switch, or a list of those.")
        for adapter in effective.get("adapters", {}).values():
            if not math.isfinite(adapter.get("scale", 1)):
                raise ValueError("Adapter strength must be finite.")
    except (ValueError, TypeError, OverflowError) as problem:
        errors.append(str(problem))
    if errors:
        return {"errors": errors, "warnings": [], "candidate": candidate, "before": before, "token": ""}

    if engine_error or not supported:
        errors.append("Supported flags could not be checked against the installed llama-server. " + engine_error)
    spellings = {spelling.removeprefix("--"): flag for flag in supported
                 for spelling in [flag["name"], *[alias for alias in flag.get("also", []) if alias.startswith("--")]]}
    for required in sorted(OUR_FLAGS):
        if required not in spellings:
            errors.append(f"This llama-server does not advertise the scheduler's required --{required} flag.")
    for flag, value in effective["flags"].items():
        if value is False:
            continue
        if flag not in spellings:
            errors.append(f"--{flag} is not advertised by this llama-server's --help.")
        elif spellings[flag]["name"] in OUR_FLAGS | ADAPTER_FLAGS:
            errors.append(f"--{flag} is managed by LoadCoach, not editable launch flags.")
        else:
            takes = spellings[flag].get("takes", "")
            if takes and not takes.startswith("[") and value is True:
                errors.append(f"--{flag} requires a value ({takes}).")
            elif not takes and value is not True:
                errors.append(f"--{flag} is a switch and does not take a value.")
            choices = re.fullmatch(r"[\[{]([A-Za-z0-9_.-]+(?:[|,][A-Za-z0-9_.-]+)+)[\]}]", takes)
            if choices and value is not True and str(value) not in re.split(r"[|,]", choices[1]):
                errors.append(f"--{flag} must be one of {choices[1]} according to this engine's help.")
    if effective.get("adapters") and "lora-scaled" not in spellings:
        errors.append("This llama-server does not advertise --lora-scaled for adapters.")

    stamps = []
    model_facts = {}
    for role, path in files_of(effective).items():
        item = {"role": role, "path": path, "facts": {}, "compatibility": "not checked"}
        artifacts.append(item)
        try:
            stat = os.stat(path)
            if not os.path.isfile(path):
                errors.append(f"{role}: {path}: not a regular file.")
                continue
            stamps.append([role, path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
            item["facts"] = facts(read_metadata(path))
        except OSError as problem:
            errors.append(f"{role}: {path}: {problem.strerror}.")
            stamps.append([role, path, None])
        except ValueError as problem:
            warnings.append(f"{role}: metadata unavailable ({problem}); runtime compatibility is unknown.")
        if role == "model":
            model_facts = item["facts"]
            item["compatibility"] = "model metadata read; runtime unqualified" if model_facts else "metadata unavailable"
            if model_facts and model_facts.get("what") != "model":
                errors.append("The selected model file describes a projector or adapter, not model weights.")
        elif item["facts"]:
            expected = "projector" if role == "mmproj" else "adapter"
            if item["facts"].get("what") != expected:
                errors.append(f"{role}: this file does not describe a {expected}.")
            match = goes_with(item["facts"], model_facts)
            item["compatibility"] = "metadata names agree; runtime unqualified" if match else "metadata names do not establish compatibility"
            if not match:
                warnings.append(f"{role}: metadata does not establish a match with this model. Projectors are not interchangeable by filename.")
    if not effective["flags"].get("mmproj") and model_facts.get("takes_images"):
        warnings.append("The model's chat template handles images, but no projector is selected. Image input is unqualified; models requiring an external mmproj will reject images.")
    trained = model_facts.get("trained_context")
    if type(trained) in (int, float) and effective["flags"]["ctx-size"] > trained:
        warnings.append(f"Requested context exceeds the metadata's trained context ({trained:,}); runtime support and quality are unqualified.")

    # fit() knows memory admission and eviction costs. Protect all servers it treats as
    # replacement kin before asking it: normal dispatch makes this same safety check.
    related = runner.replacements(name, effective)
    serving = runner.serving(name, effective)
    reusable = serving if serving and serving.ready and runner.answers(serving, effective) else None
    protected = [s for s in related if not s.ready or runner.busy(s) or runner.pinned(s)]
    loading = effective
    if serving and not reusable and kin_base(serving.configured or serving.settings) == kin_base(effective):
        every = [*serving.settings.get("adapters", {}).values(), *runner.lacks(serving, effective)]
        loading = effective | {"adapters": {adapter["file"]: adapter for adapter in every}}
    fitted = None if errors or protected or reusable else runner.fit(name, loading, set(), configured=effective)
    leaves = [] if reusable or not fitted else list(dict.fromkeys(s.model for s in [*related, *fitted[2]]))
    stays = [s.model for s in runner.servers.values() if s.model not in leaves]
    if reusable:
        launch, cards = reusable.settings, reusable.cards
    elif fitted:
        launch, cards = fitted[0], fitted[1]
    else:
        launch, cards = effective, ()
    placement = {"state": "reuse loaded server" if reusable else "candidate placement" if fitted else "deferred / unavailable",
                 "cards": list(cards), "stays": stays, "leaves": leaves,
                 "protected": [s.model for s in protected],
                 "context": launch["flags"]["ctx-size"], "request_context": request_context(launch["flags"]),
                 "measured": bool(library.measure(name, launch)),
                 "note": "Snapshot only: queue reservations, telemetry and holds can change. Admission is checked again for the actual request. No model is loaded by preview or apply."}
    placement["telemetry_age"] = round(max(0, time.monotonic() - runner.seen_at), 1) if runner.seen and runner.seen_at else None
    placement["loaded"] = [{"model": s.model, "cards": list(s.cards), "leaves": s.model in leaves,
                            "reason": "active requests" if runner.busy(s) else "still loading" if not s.ready else
                            "model lock" if runner.locked(s) else "held" if runner.pinned(s) else "idle"}
                           for s in runner.servers.values()]
    if not reusable and related:
        warnings.append("The loaded configuration will need replacement for a subsequent request. Active work, holds and model locks defer replacement.")
    # A concrete device list is meaningful only when the scheduler found one; never present
    # a deferred GPU placement as a CPU launch. The port is assigned only when loading.
    command = shlex.join(engine_for(launch).launch(runner.config, 0, launch, tuple(cards)).command) if reusable or fitted else ""
    token = digest([library.identity(name), library.file_revision(name), before, candidate, library.defaults,
                    runner.config.gpus, runner.engine_cards(effective), engine_stamp(engine_for(effective).installation(runner.config).executable), supported, stamps])
    def fields(stored):
        """Describe the changed model fields for the setup preview."""
        return {**{key: value for key, value in stored.items() if key != "flags"},
                **{f"flags.{key}": value for key, value in stored["flags"].items()}}
    old_fields, new_fields = fields(before["settings"]), fields(settings)
    changes = [{"field": field, "before": old_fields.get(field), "after": new_fields.get(field),
                "before_set": field in old_fields, "after_set": field in new_fields}
               for field in sorted(old_fields.keys() | new_fields.keys())
               if field not in old_fields or field not in new_fields or old_fields[field] != new_fields[field]]
    if before["order"] != candidate["order"]:
        changes.append({"field": "sort order", "before": before["order"], "after": candidate["order"], "before_set": True, "after_set": True})
    return {"candidate": candidate, "before": before, "model_id": library.identity(name), "file_revision": library.file_revision(name), "effective": effective, "errors": errors, "warnings": warnings,
            "artifacts": artifacts, "changes": changes, "placement": placement, "command": command,
            "idle_minutes": runner.config.model_idle_minutes, "token": token,
            "qualification": "Static checks only. A successful real-model load, image request and workload/context qualification remain pending."}
