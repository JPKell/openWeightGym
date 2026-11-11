"""Read-only DB1 comparison of offline SQLite snapshots. Never authorizes deletion.

Run with ``python -m db.consolidation --help``. Reports contain opaque row references,
content digests and changed column names, never credentials, prompts or record values.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3


# Even an identical front copy does not make these tables disposable. In particular
# events/texts/traces/seen/forwarded are a live spool and sensitivity state, not a replica.
RETAIN = {
    "events", "texts", "traces", "seen", "forwarded", "settings_changed",
    "settings_seen", "schedule_grants", "file_hashes", "models", "fingerprints",
    "model_identities", "model_names", "model_setup_undo", "model_loadouts",
    "model_presets", "shared_model_presets", "tool_schemas", "measuring_run",
    "suite_schema_migrations",
}
DESTINATIONS = {
    **dict.fromkeys(("users", "groups", "keys", "services", "events", "texts",
                    "traces", "seen", "files", "batches", "batch_lines",
                    "machine_telemetry", "machine_hosts", "machine_lifecycle", "node_discovery"), "accounts"),
    **dict.fromkeys(("roles", "role_benchmarks", "role_evidence",
                    "reliability_state", "reliability_outcomes"), "roles"),
    **dict.fromkeys(("benchmark_definitions", "benchmark_state", "benchmark_runs",
                    "benchmark_samples"), "benchmarks"),
    "external_tasks": "accounts",
}
JSON_COLUMNS = {
    "settings", "choices", "own", "detail", "text", "asked", "said", "definition",
    "evidence", "body", "result", "metadata", "subject", "environment", "messages",
    "measurements", "numbers", "names",
}


def quoted(name):
    """Quote a SQLite identifier, escaping embedded double quotes."""
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def readonly(path):
    """No constructors/migrations, file creation, checkpoint or journal writes."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("An existing offline SQLite snapshot is required")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Use an offline backup, without SQLite journal sidecars")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def tables(connection):
    """List user tables in a SQLite source store."""
    return sorted(name for name, in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))


def columns(connection, table):
    """Return SQLite column metadata, including each column's primary-key position."""
    return connection.execute(f"PRAGMA table_info({quoted(table)})").fetchall()


def rows(connection, table):
    """Read selected table rows for the ownership audit."""
    cursor = connection.execute(f"SELECT * FROM {quoted(table)}")
    names = [one[0] for one in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def normalized(value, column=""):
    """Normalize values for deterministic cross-store comparison."""
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if column in JSON_COLUMNS and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def digest(value):
    """Return the SHA-256 digest of the canonical input."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def compare(source, target, table, *, machine="compute"):
    """Every source row appears once, including unmatched rows and conflicts.

    Additional destination columns are reported but cannot erase a source column.
    Event hashes are re-chained by front; identity is (machine, source sequence).
    Text entry IDs use the same forwarding map rather than accidentally matching
    an unrelated front-local request with the same integer ID.
    """
    source_columns = columns(source, table)
    key_columns = [one[1] for one in sorted(source_columns, key=lambda one: one[5]) if one[5]]
    if not key_columns:
        key_columns = [one[1] for one in source_columns]
    source_names = {one[1] for one in source_columns}
    present = target is not None and table in tables(target)
    target_names = {one[1] for one in columns(target, table)} if present else set()
    mapped = table in ("events", "texts")
    ignored = {"hash", "machine", "source", "id"} if table == "events" else {"entry"} if table == "texts" else set()
    comparing = source_names - ignored
    target_rows = rows(target, table) if present else []
    event_map = {}
    if mapped and target is not None and "events" in tables(target):
        event_map = {entry: original for entry, original in target.execute(
            "SELECT id, source FROM events WHERE machine=? AND source IS NOT NULL", (machine,))}
    indexed = {}
    for row in target_rows:
        if table == "events":
            if row.get("machine") != machine:
                continue
            key = (row.get("source"),)
        elif table == "texts":
            if row["entry"] not in event_map:
                continue
            key = (event_map[row["entry"]],)
        else:
            if not all(name in row for name in key_columns):
                continue
            key = tuple(row[name] for name in key_columns)
        indexed.setdefault(key, []).append(row)
    records = []
    for row in rows(source, table):
        key = tuple(row[name] for name in key_columns)
        candidates = indexed.get(key, [])
        if not candidates:
            state, changed = "only_compute", []
        elif len(candidates) != 1:
            state, changed = "different", ["duplicate_destination_key"]
        else:
            other = candidates[0]
            changed = sorted(name for name in comparing if name not in other or
                             normalized(row[name], name) != normalized(other[name], name))
            state = "different" if changed else "identical"
        records.append({"key_sha256": digest([normalized(value) for value in key]),
                        "content_sha256": digest({name: normalized(value, name) for name, value in row.items()}),
                        "state": state, "changed_columns": changed})
    return {"rows": len(records), "destination_rows": len(target_rows),
            "keys": key_columns, "source_only_columns": sorted(source_names - target_names),
            "destination_only_columns": sorted(target_names - source_names),
            "counts": dict(Counter(one["state"] for one in records)),
            "records": sorted(records, key=lambda one: one["key_sha256"])}


def audit(compute, accounts, roles, benchmarks, spotter=None, *, machine="compute", telemetry_host=None):
    """Compare source stores and report ownership, row and digest evidence."""
    paths = {"compute": compute, "accounts": accounts, "roles": roles, "benchmarks": benchmarks}
    if spotter:
        paths["spotter"] = spotter
    with ExitStack() as stack:
        connections = {name: stack.enter_context(readonly(path)) for name, path in paths.items()}
        source = connections["compute"]
        result = {"schema": 1, "machine": machine, "inputs": {}, "tables": {}}
        for name, path in paths.items():
            with Path(path).open("rb") as stream:
                result["inputs"][name] = hashlib.file_digest(stream, "sha256").hexdigest()
        for table in tables(source):
            destination = DESTINATIONS.get(table)
            comparison = compare(source, connections.get(destination), table, machine=machine)
            comparison["ownership"] = "retain_compute" if table in RETAIN else "review_candidate" if destination else "unclassified"
            comparison["destination"] = destination
            result["tables"][table] = comparison
        source_tables = tables(source)
        checks = result["checks"] = {}
        if "batches" in source_tables:
            checks["batch_states"] = dict(source.execute("SELECT status,COUNT(*) FROM batches GROUP BY status"))
        if "benchmark_runs" in tables(connections["benchmarks"]):
            checks["front_run_states"] = dict(connections["benchmarks"].execute(
                "SELECT state,COUNT(*) FROM benchmark_runs GROUP BY state"))
        if {"batches", "files"} <= set(source_tables):
            file_ids = {number for number, in source.execute("SELECT id FROM files")}
            missing = []
            referenced = set()
            for number, *links in source.execute("SELECT id,input_file,output_file,error_file FROM batches"):
                for column, link in zip(("input_file", "output_file", "error_file"), links):
                    if link:
                        referenced.add(link)
                        if link not in file_ids:
                            missing.append({"batch": number, "column": column, "file": link})
            checks["missing_batch_files"] = missing
            checks["unreferenced_files"] = len(file_ids - referenced)
        if "external_tasks" in source_tables:
            known_runs = {number for number, in connections["benchmarks"].execute("SELECT id FROM benchmark_runs")}
            checks["tasks_without_front_run"] = sorted(number for number, in source.execute(
                "SELECT id FROM external_tasks") if number not in known_runs)
        for table in ("settings_changed", "settings_seen"):
            if table in source_tables:
                checks[table + "_programs"] = dict(source.execute(
                    f"SELECT program,COUNT(*) FROM {table} GROUP BY program"))
        if spotter:
            if not telemetry_host:
                raise ValueError("Spotter comparison needs the verified physical host fingerprint")
            # Legacy readings contain series(numbers), just like front's stored history.
            old = connections["spotter"]
            front = connections["accounts"]
            lookup = {at: numbers for at, numbers in front.execute(
                "SELECT at,numbers FROM machine_telemetry WHERE fingerprint=?", (telemetry_host,))}
            records = []
            for at, numbers in old.execute("SELECT at,numbers FROM readings ORDER BY at"):
                state = "only_compute" if at not in lookup else "identical" if normalized(numbers, "numbers") == normalized(lookup[at], "numbers") else "different"
                records.append({"key_sha256": digest([telemetry_host, at]),
                                "content_sha256": digest(normalized(numbers, "numbers")),
                                "state": state, "changed_columns": ["numbers"] if state == "different" else []})
            result["spotter"] = {"host": telemetry_host, "rows": len(records),
                                 "counts": dict(Counter(one["state"] for one in records)),
                                 "span": old.execute("SELECT MIN(at),MAX(at) FROM readings").fetchone(),
                                 "records": records}
        candidates = [one for one in result["tables"].values() if one["ownership"] == "review_candidate"]
        result["gate"] = {
            "unclassified_tables": [name for name, one in result["tables"].items() if one["ownership"] == "unclassified"],
            "different_candidate_rows": sum(one["counts"].get("different", 0) for one in candidates),
            "only_compute_candidate_rows": sum(one["counts"].get("only_compute", 0) for one in candidates),
            "deletion_authorized": False,
        }
        return result


def main():
    """Run the read-only database consolidation audit command."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("compute", "accounts", "roles", "benchmarks"):
        parser.add_argument("--" + name, type=Path, required=True, help="Offline SQLite snapshot")
    parser.add_argument("--spotter", type=Path)
    parser.add_argument("--telemetry-host", help="Verified physical host fingerprint for legacy readings")
    parser.add_argument("--machine", default="compute", help="Forwarding machine name")
    args = parser.parse_args()
    print(json.dumps(audit(**vars(args)), indent=2))


if __name__ == "__main__":
    main()
