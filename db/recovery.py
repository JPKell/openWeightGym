"""Engine-specific store snapshots and restore verification for recovery sets."""

from __future__ import annotations

import hashlib
import platform
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .database import Database
from .postgresql_tools import dump_custom, redacted_error, source_metadata, verify_custom_restore


@dataclass(frozen=True)
class RecoveryStore:
    """A required logical store and its owning repository bindings."""

    name: str
    database: Database
    bindings: tuple[str, ...]
    archive: str = ""


def checksum(path: Path) -> str:
    """Stream the file into a SHA-256 checksum."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_name(store: RecoveryStore) -> str:
    """Return the stable relative recovery path for a named store."""
    if store.archive:
        return store.archive
    safe = "".join(character if character.isalnum() or character in "._-" else "-" for character in store.name)
    suffix = ".db" if store.database.engine == "sqlite" else ".pgdump"
    return f"stores/{safe}{suffix}"


def _sqlite_metadata(connection, path: Path) -> dict:
    """Read schema versions and row counts on the supplied SQLite connection."""
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise ValueError(f"{path.name}: SQLite integrity check did not pass")
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    counts = {table: int(connection.execute(
        f' SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
    ).fetchone()[0]) for table in tables}
    versions = {}
    if "suite_schema_migrations" in tables:
        versions = {stream: int(version) for stream, version in connection.execute(
            "SELECT stream, MAX(version) FROM suite_schema_migrations GROUP BY stream ORDER BY stream"
        )}
    return {"counts": counts, "schema_versions": versions, "integrity": "ok"}


def sqlite_metadata(path: Path) -> dict:
    """Inspect SQLite recovery metadata using a read-only connection."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute('BEGIN')
        return _sqlite_metadata(connection, path)
    finally:
        connection.close()


def sqlite_inventory(path: Path) -> dict:
    """Read only schema versions for listings; backup/restore still checks integrity and counts."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute('BEGIN')
        exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='suite_schema_migrations'"
        ).fetchone()
        return {stream: int(version) for stream, version in connection.execute(
            "SELECT stream, MAX(version) FROM suite_schema_migrations GROUP BY stream ORDER BY stream"
        )} if exists else {}
    finally:
        connection.close()


def owning_services(bindings) -> list[str]:
    """Return the services whose configured bindings reference this store."""
    services = {binding.split(".", 1)[0] for binding in bindings}
    if set(bindings) & {"weightroom.plugins", "weightroom.accounts", "weightroom.roles"}:
        services.add("turnstile")
    return sorted(services)


def inspect_store(store: RecoveryStore) -> dict:
    """Describe reachability and schema versions without exposing a DSN."""
    descriptions = {
        "loadcoach.core": "models, requests, batches and settings",
        "weightroom.accounts": "users, keys, groups and what was done to them",
        "weightroom.roles": "roles, their benchmark evidence and model reliability",
        "weightroom.chat": "private chats, collections, prompt versions and workflows",
        "weightroom.benchmarks": "benchmark definitions and evidence",
        "weightroom.plugins": "plugin usage records",
        "weightroom.alerts": "operational alert episodes and source freshness",
        "weightroom.schedules": "recurring schedules and durable occurrences",
        "spotter.history": "Spotter telemetry history",
        "spotter.identity": "shared identity and event data",
        "toollocker.identity": "shared identity and tool schemas",
    }
    kind = ", ".join(dict.fromkeys(descriptions.get(binding, binding) for binding in store.bindings))
    source = str(store.database.path) if store.database.path is not None else store.database.display_name
    services = owning_services(store.bindings)
    base = {
        "name": store.name,
        "engine": store.database.engine,
        "display_name": store.database.display_name,
        "bindings": list(store.bindings),
        "owning_services": services,
        "archive": archive_name(store),
        "required": True,
        "kind": kind or "configured database store",
        "source": source,
    }
    try:
        if store.database.engine == "sqlite":
            path = store.database.path
            if path is None or not path.is_file() or path.is_symlink():
                return {**base, "status": "missing", "size": None,
                        "error": "required SQLite store is missing or is not a regular file"}
            versions = sqlite_inventory(path)
            return {**base, "status": "included", "size": path.stat().st_size,
                    "schema_versions": versions, "error": ""}
        metadata = source_metadata(store.database)
        return {**base, "status": "included", "size": None,
                "schema_versions": metadata["schema_versions"], "server_version": metadata["server_version"],
                "error": ""}
    except Exception as problem:
        return {**base, "status": "unreachable", "size": None, "error": redacted_error(store.database, problem)}


def backup_store(store: RecoveryStore, payload: Path) -> dict:
    """Create and independently restore-check one required store."""
    archive = archive_name(store)
    target = payload / archive
    target.parent.mkdir(parents=True, exist_ok=True)
    database = store.database
    if database.engine == "sqlite":
        source = database.path
        if source is None or not source.is_file() or source.is_symlink():
            raise ValueError(f"required SQLite store {store.name} is missing or is not a regular file")
        original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        copied = sqlite3.connect(target)
        try:
            # Counts and copied pages must describe one source snapshot even when
            # keys, telemetry or requests change while the online backup runs.
            original.execute('BEGIN')
            source_meta = _sqlite_metadata(original, source)
            original.backup(copied)
            # Recovery snapshots are standalone files, not live WAL databases.
            copied.execute('PRAGMA journal_mode=DELETE')
        finally:
            copied.close()
            original.close()
        target.chmod(0o600)
        restored = sqlite_metadata(target)
        if restored["counts"] != source_meta["counts"] or restored["schema_versions"] != source_meta["schema_versions"]:
            raise ValueError(f"restored SQLite store {store.name} does not match its source")
        tools = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version}
        method = "SQLite online backup API"
        verification = restored
        server_version = sqlite3.sqlite_version
    else:
        source_meta = source_metadata(database)
        dump = dump_custom(database, target)
        verification = verify_custom_restore(database, target, source_meta)
        tools = dump["tools"]
        method = dump["method"]
        server_version = source_meta["server_version"]
        verification["restore_list_entries"] = dump["restore_list_entries"]
    return {
        "name": store.name,
        "engine": database.engine,
        "display_name": str(database.path) if database.path is not None else database.display_name,
        "bindings": list(store.bindings),
        "owning_services": owning_services(store.bindings),
        "archive": archive,
        "method": method,
        "server_version": server_version,
        "schema_versions": source_meta["schema_versions"],
        "source_identity": source_meta.get("source_identity", ""),
        "tools": tools,
        "size": target.stat().st_size,
        "sha256": checksum(target),
        "verification": {"state": "verified", **verification},
    }
