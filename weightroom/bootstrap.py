"""Create narrowly scoped voice identities without replacing existing keys or account policy."""

from __future__ import annotations

from pathlib import Path

from db import DatabaseRegistry
from loadcoach.config import read_settings
from loadcoach.groups import read_group
from loadcoach.log import Log
from loadcoach.users import Users


def service_identity(config: Path, name: str, group: str, key_file: Path, abilities: tuple[str, ...]) -> str:
    """Make or reuse one service key; reject revoked credentials and unrelated name collisions."""
    told = read_settings(config)
    data = config.parent / told.get("data", ".")
    source = DatabaseRegistry.from_config(told.get("databases"), config.parent,
                                         {"weightroom.accounts": data / "accounts.db"}).database("weightroom.accounts")
    users = Users(source, {key: read_group(key, value) for key, value in told.get("groups", {}).items()})
    log = Log(source, keep_days=0)
    try:
        return ensure_service_identity(users, log, name, group, key_file, abilities)
    finally:
        users.db.close()
        log.db.close()


def ensure_service_identity(users, log, name, group, key_file, abilities, actor="installer"):
    """Shared installer/panel operation, preserving existing account policy and credentials."""
    if users.exists(name):
        user = users.by_name(name)
        if user is None or user.group_name != group:
            raise ValueError(f"The service account {name} is disabled or belongs to another group; it was left unchanged.")
    else:
        users.add(name, group)
        log.add("user", "added", actor, name=name, group=group)
    if key_file.exists():
        key = key_file.read_text().strip()
        owner = users.by_key(key)
        if owner is None or owner.name != name:
            raise ValueError(f"{key_file} is not an active key for {name}; restore its credential before retrying.")
        key_file.chmod(0o600)
    else:
        key = users.new_key(name, actor)
        from voice.telephone_keys import atomic_secret
        from loadcoach.users import hash_key
        try:
            atomic_secret(key_file, key)
        except OSError:
            users.remove_key(name, hash_key(key))
            raise
        log.add("user", "key-added", actor, name=name, label=actor)
    own = users.own(name)
    changed = {**own, "add": sorted(set(own.get("add", ())) | set(abilities))}
    changed.setdefault("max_requests", 4)
    if changed != own:
        users.change(name, own=changed)
        log.add("user", "changed", actor, name=name, own=changed)
    return key


def voice_service_identity(config: Path, machine: str = "compute", key_file: Path | None = None) -> str:
    """Give a paired worker its own hold/load identity, separate from the machine signing key."""
    keys = config.parent / "../keys"
    if machine == "compute":
        name, path = "voice-service", keys / "voice-service.key"
    else:
        name, path = "voice-service-" + machine, keys / "voice-services" / (machine + ".key")
    return service_identity(config, name, "system", key_file or path, ("hold", "load", "see-all"))
