"""Bounded, read-only store probes and recorded recovery freshness signals."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from .database import connect
from .recovery import owning_services


BACKUP_MAX_AGE = 86400


def postgresql_identity(connection) -> str:
    """Fingerprint public server/database identity, never connection credentials."""
    row = connection.execute(
        "SELECT current_database(), inet_server_addr()::text, inet_server_port(), "
        "(SELECT oid FROM pg_database WHERE datname = current_database())"
    ).fetchone()
    return hashlib.sha256(json.dumps(list(row)).encode()).hexdigest()


def backup_signal(store, manifests, now, identity="") -> dict:
    """Select verified evidence for this store; failed or rebound stores cannot look fresh."""
    database = store.database
    display = str(database.path) if database.path is not None else database.display_name
    candidates = []
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        verified = manifest.get("verification")
        records = manifest.get("stores", [])
        if (manifest.get("schema") not in (2, 3) or not isinstance(verified, dict) or verified.get("state") != "verified"
                or not isinstance(manifest.get("archive_size"), int) or manifest["archive_size"] <= 0
                or not isinstance(records, list) or not manifest.get("id")):
            continue
        try:
            created = datetime.fromisoformat(manifest["created_at"])
            if created.tzinfo is None:
                continue
            stamp = created.timestamp()
            if stamp > now:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            verified_store, bindings = record.get("verification"), record.get("bindings")
            if (not isinstance(verified_store, dict) or verified_store.get("state") != "verified"
                    or not isinstance(bindings, list) or not all(isinstance(item, str) for item in bindings)):
                continue
            same_source = record.get("display_name") == display
            if database.engine == "sqlite" and database.path is not None and isinstance(record.get("display_name"), str):
                try:
                    same_source = Path(record["display_name"]).resolve() == database.path.resolve()
                except (OSError, ValueError):
                    same_source = False
            if (record.get("name") != store.name or record.get("engine") != database.engine
                    or not same_source
                    or not set(store.bindings).issubset(bindings)):
                continue
            if database.engine == "postgresql" and (not identity or record.get("source_identity") != identity):
                continue
            candidates.append((stamp, manifest))
    if not candidates:
        return {"state": "missing", "id": "", "created_at": "", "created_timestamp": None, "age_seconds": None,
                "max_age_seconds": BACKUP_MAX_AGE}
    stamp, manifest = max(candidates, key=lambda item: item[0])
    age = max(0, int(now - stamp))
    return {"state": "fresh" if now - stamp <= BACKUP_MAX_AGE else "stale", "id": manifest["id"],
            "created_at": manifest["created_at"], "created_timestamp": stamp,
            "age_seconds": age, "max_age_seconds": BACKUP_MAX_AGE}


def store_health(store, machine, manifests=(), now=None) -> dict:
    """Probe one store without counting tables, writing application data, or running recovery tools."""
    now = time.time() if now is None else now
    database = store.database
    display = str(database.path) if database.path is not None else database.display_name
    base = {"name": store.name, "display_name": display, "machine": machine,
            "engine": database.engine, "bindings": list(store.bindings),
            "services": owning_services(store.bindings), "checked_at": now}
    started = time.monotonic()
    facts, identity, warnings = {}, "", []
    try:
        if database.engine == "sqlite":
            path = database.path
            if path is None or not path.is_file() or path.is_symlink():
                raise ValueError("Missing local SQLite file")
            connection = connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchall()
                facts = {"server_version": connection.execute("SELECT sqlite_version()").fetchone()[0]}
            finally:
                connection.close()
        else:
            with database.connection(connect_timeout=2) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL statement_timeout = '2000ms'")
                identity = postgresql_identity(connection)
                version, limit, reserved, vacuum, readonly, standby = connection.execute(
                    "SELECT current_setting('server_version'), current_setting('max_connections')::int, "
                    "current_setting('superuser_reserved_connections')::int + "
                    "COALESCE(current_setting('reserved_connections', true), '0')::int, "
                    "current_setting('autovacuum'), current_setting('default_transaction_read_only'), pg_is_in_recovery()"
                ).fetchone()
                # Ordinary store logins cannot see other roles' backend_type in pg_stat_activity.
                # Public database statistics avoid undercounting without exposing query text.
                used = int(connection.execute("SELECT COALESCE(SUM(numbackends), 0) FROM pg_stat_database").fetchone()[0])
                tls = connection.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
                facts = {"server_version": version, "connections": used, "connection_limit": limit,
                         "reserved_connections": reserved, "autovacuum": vacuum == "on",
                         "tls": bool(tls and tls[0]), "read_only": readonly == "on" or standby}
                if used >= .8 * max(1, limit - reserved):
                    warnings.append("PostgreSQL database backends are at or above 80% of non-reserved client slots (includes database workers).")
                if vacuum != "on":
                    warnings.append("PostgreSQL autovacuum is disabled.")
                if readonly == "on" or standby:
                    warnings.append("PostgreSQL is a standby or defaults to read-only transactions; openWeightGym writers may fail.")
        state, error = ("warning" if warnings else "reachable"), ""
    except Exception:
        # Driver errors can embed credentials or connection strings. No raw exception leaves this boundary.
        state, error = "unavailable", "Store probe failed. Check the owning host, configuration, file permissions and database service."
    backup = backup_signal(store, manifests, now, identity)
    if database.engine == "postgresql" and not identity:
        backup["state"] = "unknown"
    return {**base, "state": state, "error": error, "warnings": warnings, "facts": facts,
            "latency_ms": round((time.monotonic() - started) * 1000, 1), "backup": backup}


def stores_health(stores, machine, manifests=(), now=None) -> list[dict]:
    """Keep an unavailable store from hiding the remaining mixed-engine installation."""
    now = time.time() if now is None else now
    return [store_health(store, machine, manifests, now) for store in stores]
