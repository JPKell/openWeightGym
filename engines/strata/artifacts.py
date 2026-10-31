"""Content identities for every prepared Strata inference input.

This is preparation, not permission to load. Scheduler admission and a capped memory
scope remain required. Files changing while hashed are refused rather than blessed.
"""
import hashlib
import json
import os
import stat

from engines.strata import model


def digest(value) -> str:
    """Return the SHA-256 digest of the canonical input."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stamp(value) -> tuple:
    """Return artifact file metadata used to detect changed installation inputs."""
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def identity(path: str) -> dict:
    """Hash regular files in bounded chunks; reject replacement, mutation and special files."""
    model.absolute_path(path, "artifact")
    before = os.stat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Strata artifacts must be regular files.")
    # NONBLOCK prevents a malicious/racing replacement with a FIFO from blocking startup.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or stamp(before) != stamp(opened):
            raise ValueError("Strata artifact changed before hashing.")
        hashed = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if stamp(opened) != stamp(after) or stamp(after) != stamp(os.stat(path)):
        raise ValueError("Strata artifact changed while hashing.")
    return {"path": path, "bytes": after.st_size, "sha256": hashed}


def manifest(settings: dict) -> dict:
    """Pin weights, tokenizer vocabulary, merges, token types and chat template together."""
    model.check_model("artifact-check", settings, tuple(settings.get("gpus", ())) if isinstance(settings.get("gpus"), list) else ())
    files = {role: identity(path) for role, path in model.files_of(settings).items()}
    content = {role: entry["sha256"] for role, entry in files.items()}
    return {"schema": 1, "engine": "strata", "files": files, "sha256": digest(content)}


def verify(settings: dict, expected: dict) -> dict:
    """Fail closed on missing/extra inputs, relocated selections or changed contents."""
    if not isinstance(expected, dict) or expected.keys() != {"schema", "engine", "files", "sha256"}:
        raise ValueError("Strata requires a complete versioned artifact manifest.")
    current = manifest(settings)
    if current != expected:
        raise ValueError("Strata inference artifacts changed; prepare and qualify the selected artifacts again.")
    return current
