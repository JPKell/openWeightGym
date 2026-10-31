"""Stable identity of a physical host, shared by its openWeightGym services.

The machine-id is hashed with an openWeightGym domain separator before leaving the host.
It identifies hardware installation, not a LoadCoach instance or a credential.
"""

import hashlib
import os
import socket
import uuid
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def fingerprint() -> str:
    """Return a stable hashed physical-host identity from available machine identifiers."""
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            value = path.read_text().strip().lower()
        except OSError:
            continue
        if len(value) == 32 and value != "0" * 32 and all(c in "0123456789abcdef" for c in value):
            break
    else:
        # A minimal/container host may lack machine-id. Keep a user-owned ID
        # across restarts, shared by openWeightGym services running as this account.
        path = Path.home() / ".local/share/suite/machine-id"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as out:
                out.write(uuid.uuid4().hex)
        value = path.read_text().strip()
    return hashlib.sha256(("suite-physical-host-v1:" + value).encode()).hexdigest()


@lru_cache(maxsize=1)
def hostname() -> str:
    """Return the operating system's current host label."""
    return socket.gethostname()
