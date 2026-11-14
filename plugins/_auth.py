"""Local snapshots of compute's model list, for turnstile's plugin path and the plugin management page.

Who a caller is, is turnstile's to say from the accounts on the control node. Compute's snapshot no longer
carries any key verifiers: its `keys` list is always empty.
"""

from __future__ import annotations

import fcntl
import math
import os
import time
from pathlib import Path

import httpx

from plugins._sdk import atomic_json, read_json


class Authorization:
    """Persist compute's model list and permission checks, never openWeightGym credentials."""

    def __init__(self, path: Path) -> None:
        """Configure adapter token verification and local authorization policy."""
        self.path = path

    def snapshot(self) -> dict:
        """Read the latest complete snapshot shared by WeightRoom and turnstile."""
        empty = {"generated_at": 0, "keys": [], "models": [], "unlogged": False}
        try:
            value = read_json(self.path, empty)
            if not isinstance(value, dict) or not isinstance(value.get("keys"), list):
                return empty
            if type(value.get("generated_at", 0)) not in (int, float) or not math.isfinite(value.get("generated_at", 0)):
                return empty
            if not all(isinstance(entry, dict) and isinstance(entry.get("hash"), str) and
                       isinstance(entry.get("user"), dict) and entry["user"].get("identity") and
                       (entry.get("expires") is None or type(entry["expires"]) in (int, float) and math.isfinite(entry["expires"])) for entry in value["keys"]):
                return empty
            for entry in value["keys"]:
                user = entry["user"]
                allowed = user.get("model_access")
                if not isinstance(user.get("name"), str) or not isinstance(user.get("identity"), str) or not isinstance(user.get("can"), list) or not all(isinstance(name, str) for name in user["can"]) or not (allowed == "*" or isinstance(allowed, list) and all(isinstance(name, str) for name in allowed)) or type(user.get("max_requests", 0)) is not int or user.get("max_requests", 0) < 0:
                    return empty
            return {**empty, **value}
        except (OSError, ValueError):
            return empty

    def replace(self, snapshot: dict) -> None:
        """Apply an authoritative refresh, including revoked keys and disabled accounts."""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("keys"), list):
            raise ValueError("Invalid authorization snapshot.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if snapshot.get("generated_at", 0) >= self.snapshot().get("generated_at", 0):
                atomic_json(self.path, snapshot)
        finally:
            os.close(descriptor)

    async def refresh(self, client: httpx.AsyncClient, url: str, key_file: str = "", *, key: str = "") -> bool:
        """Fetch without a wake path; retain the last copy only for a genuine outage."""
        if not key_file and not key:
            return False
        try:
            key = key or Path(key_file).read_text().strip()
            response = await client.get(url, headers={"authorization": "Bearer " + key, "x-turnstile-no-wake": "1"}, timeout=2)
            if response.status_code in (401, 403):
                self.replace({"generated_at": time.time(), "keys": [], "models": [], "unlogged": False})
                return False
            response.raise_for_status()
            self.replace(response.json())
            return True
        except (OSError, httpx.HTTPError, ValueError):
            return False

    def permits(self, user: dict, alias: str) -> bool:
        """Apply the effective account/app permission and public-model allowlist."""
        allowed = user.get("model_access", [])
        return "front-plugins" in user.get("can", []) and (allowed == "*" or alias in allowed)

    def status(self) -> dict:
        """Expose synchronization age without returning key verifiers or account records."""
        at = self.snapshot()["generated_at"]
        return {"synchronized_at": at or None, "age_seconds": round(time.time() - at, 1) if at else None}
