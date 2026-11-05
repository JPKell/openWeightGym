"""Read-only typed review; never prepare a launch or execute an inference binary."""
import os

from engines.strata import model
from loadcoach.model_setup import digest


def preview(name, candidate, library, runner):
    """Validate candidate model settings and report changes before applying them."""
    from engines import engine_for
    before = library.setup(name)
    errors, stamps, artifacts = [], [], []
    settings = candidate.get("settings")
    try:
        if candidate.keys() != {"settings", "order"} or type(candidate["order"]) is not int or not -(2**31) <= candidate["order"] < 2**31:
            raise ValueError("A configuration needs settings and a whole-number sort order.")
        model.check_model(name, settings, runner.config.gpus)
        availability = engine_for("strata").describe(runner.config)
        if not availability["available"]:
            raise ValueError(availability["why"])
        for role, path in model.files_of(settings).items():
            info = os.stat(path)
            if not os.path.isfile(path):
                raise ValueError(f"{role} must be a regular file.")
            stamps.append([role, path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            artifacts.append({"role": role, "path": path, "facts": {}, "compatibility": "prepared input; checked again at load"})
    except (OSError, ValueError, TypeError) as problem:
        errors.append(str(problem))
    if errors:
        return {"errors": errors, "warnings": [], "candidate": candidate, "before": before, "token": ""}
    fitted = runner.fit(name, settings, set())
    placement = {"state": "candidate placement" if fitted else "deferred / unavailable",
                 "cards": list(fitted[1]) if fitted else [], "context": settings["flags"]["ctx-size"],
                 "request_context": settings["flags"]["ctx-size"], "leaves": [s.model for s in fitted[2]] if fitted else [],
                 "stays": list(runner.servers), "protected": [], "loaded": [],
                 "measured": bool(library.measure(name, settings)), "note": "Snapshot only; GPU and hybrid RAM admission run again at load."}
    changes = [{"field": key, "before": before["settings"].get(key), "after": settings.get(key),
                "before_set": key in before["settings"], "after_set": key in settings}
               for key in sorted(before["settings"].keys() | settings.keys()) if before["settings"].get(key) != settings.get(key)]
    token = digest([library.identity(name), library.file_revision(name), before, candidate, stamps,
                    runner.config.engines, runner.config.gpus, runner.engine_cards(settings)])
    return {"candidate": candidate, "before": before, "errors": [], "warnings": [], "effective": settings,
            "artifacts": artifacts, "changes": changes, "placement": placement, "command": "",
            "idle_minutes": runner.config.model_idle_minutes, "token": token,
            "qualification": "Text chat, one GPU, explicit hybrid RAM only. Build and all artifact contents are verified at load; native benchmarks are unsupported."}
