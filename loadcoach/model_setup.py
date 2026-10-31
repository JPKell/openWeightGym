"""Engine-dispatched, read-only model setup; shared review-token serialization."""
import hashlib
import json

from engines import engine_for
from engines.llama_cpp.adapter import engine_stamp as engine_stamp  # compatibility export


def digest(value) -> str:
    """Return the SHA-256 digest of the canonical input."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def preview(name: str, candidate: dict, library, runner, supported: list[dict], engine_error: str = "") -> dict:
    """Validate a proposed model setup using the selected engine adapter."""
    settings = candidate.get("settings")
    try:
        selected = engine_for(settings)
    except ValueError as problem:
        return {"errors": [str(problem)], "warnings": [], "candidate": candidate,
                "before": library.setup(name), "token": ""}
    return selected.preview(name, candidate, library, runner, supported, engine_error)
