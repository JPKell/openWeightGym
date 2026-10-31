"""Offline, fail-closed logical transfers between openWeightGym storage engines.

The coordinator must stop writers before calling copy_store. This module never changes
bindings, edits the source, invokes repository constructors or deletes a source database.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .database import Database
from .migrations import run
from .schemas import STREAMS, RETIRED_COMPUTE_STREAMS


class MigrationError(RuntimeError):
    """A migration safety or acceptance condition did not pass."""


@contextmanager
def reader(database):
    """Open a read-only source connection for transfer verification."""
    if database.engine == "sqlite":
        if database.path is None or not database.path.is_file() or database.path.is_symlink():
            raise MigrationError("Source must be an existing regular SQLite file")
        connection = Database.sqlite(database.path.resolve().as_uri() + "?mode=ro").connect(uri=True)
        connection.execute("BEGIN")
    else:
        connection = database.connect(connect_timeout=10)
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        yield connection
    finally:
        connection.close()


def column_info(connection, table):
    """Read portable column metadata for a source table."""
    quote = connection.database.quote_identifier
    if connection.engine == "sqlite":
        return [(r[1], r[2].lower(), r[5]) for r in connection.execute(f"PRAGMA table_info({quote(table)})")]
    return [(r[0], r[1], r[2]) for r in connection.execute(
        "SELECT c.column_name,c.data_type,COALESCE(k.ordinal_position,0) "
        "FROM information_schema.columns c LEFT JOIN information_schema.table_constraints t "
        "ON t.table_schema=c.table_schema AND t.table_name=c.table_name AND t.constraint_type='PRIMARY KEY' "
        "LEFT JOIN information_schema.key_column_usage k ON k.constraint_schema=t.constraint_schema "
        "AND k.constraint_name=t.constraint_name AND k.table_name=t.table_name AND k.column_name=c.column_name "
        "WHERE c.table_schema='public' AND c.table_name=? ORDER BY c.ordinal_position", (table,))]


def tables(connection):
    """List transferable tables while excluding internal engine bookkeeping."""
    if connection.engine == "postgresql":
        # Repositories use unqualified names. Non-public schemas would be ambiguous.
        unexpected = connection.execute(
            "SELECT n.nspname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname NOT IN ('public','information_schema') "
            "AND c.relkind IN ('r','p','v','m','S') LIMIT 1").fetchone()
        if unexpected:
            raise MigrationError("Migration supports application objects in public only")
    return sorted(connection.database.tables(connection))


def schema_streams(connection):
    """Return ordered portable schema migration streams for a store."""
    if "suite_schema_migrations" not in tables(connection):
        raise MigrationError("Source has no openWeightGym schema ledger; start its owning service to upgrade it first")
    ledger = connection.execute("SELECT stream,version,name FROM suite_schema_migrations ORDER BY stream,version").fetchall()
    selected = {}
    for stream, version, name in ledger:
        known = STREAMS.get(stream, ())
        if version < 1 or version > len(known) or known[version - 1].name != name:
            raise MigrationError("Source has an unknown or renamed schema migration")
        selected[stream] = version
    retired = "loadcoach.retirement" in selected
    if not selected or any(version != len(STREAMS[stream])
                           and not (retired and stream in RETIRED_COMPUTE_STREAMS)
                           for stream, version in selected.items()):
        raise MigrationError("Source schema is outdated; upgrade its owning services before migration")
    for stream, version in selected.items():
        if [v for s, v, _ in ledger if s == stream] != list(range(1, version + 1)):
            raise MigrationError("Source schema ledger has missing versions")
    return selected


def create_schema(database, connection, streams):
    # Retired source stores retain their old ledger entries. Reproduce the final
    # schema after creating those legacy streams, so no later stream resurrects them.
    """Apply the store's portable migrations to an empty destination."""
    for stream in sorted(streams, key=lambda name: (name == "loadcoach.retirement", name)):
        run(database, connection, stream, STREAMS[stream][:streams[stream]])


def check_schema(connection, streams):
    """Reject incompatible source and destination table layouts."""
    scratch = Database.sqlite(":memory:")
    with scratch.connection() as expected:
        create_schema(scratch, expected, streams)
        if tables(connection) != tables(expected):
            raise MigrationError("Source contains unrecognized or missing application tables")
        for table in tables(expected):
            actual = {name: pk for name, _, pk in column_info(connection, table)}
            wanted = {name: pk for name, _, pk in column_info(expected, table)}
            if actual != wanted:
                raise MigrationError(f"Source columns or primary keys differ for {table}")
        if connection.engine == "sqlite":
            objects = connection.execute("SELECT type,name FROM sqlite_schema WHERE type IN ('index','trigger','view') AND sql IS NOT NULL ORDER BY type,name").fetchall()
        else:
            objects = [("index", name) for name, in connection.execute(
                "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_index i ON i.indexrelid=c.oid "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
                "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint co WHERE co.conindid=c.oid) ORDER BY c.relname")]
            objects += [("custom", name) for name, in connection.execute(
                "SELECT tgname FROM pg_catalog.pg_trigger WHERE NOT tgisinternal UNION ALL "
                "SELECT table_name FROM information_schema.views WHERE table_schema='public'")]
        expected_objects = expected.execute("SELECT type,name FROM sqlite_schema WHERE type IN ('index','trigger','view') AND sql IS NOT NULL ORDER BY type,name").fetchall()
        if sorted(objects) != sorted(expected_objects):
            raise MigrationError("Source has unrecognized indexes, views or triggers")
    if connection.engine == "sqlite":
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("Source SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise MigrationError("Source SQLite has broken foreign keys")


def check_chain(connection):
    """Verify the retained event hash chain before accepting transferred evidence."""
    if "events" not in tables(connection):
        return {"checked": 0, "anchor": None}
    from loadcoach.log import chain_hash, COLUMNS
    before, checked, anchor = None, 0, None
    for row in connection.execute(f"SELECT {COLUMNS} FROM events WHERE hash IS NOT NULL ORDER BY id"):
        if before is not None and chain_hash(before, row[1:-1]) != row[-1]:
            raise MigrationError(f"Event hash chain is invalid at entry {row[0]}")
        if anchor is None:
            anchor = row[0]
        else:
            checked += 1
        before = row[-1]
    return {"checked": checked, "anchor": anchor}


# Only these columns change representation through PostgreSQL JSONB. Audit evidence
# deliberately stays TEXT; its original serialization is part of the event hash chain.
JSONB_COLUMNS = {("batch_lines", "body"), ("batch_lines", "result"),
                 ("texts", "asked"), ("texts", "said")}


def _object(pairs):
    """Normalize database values for portable deterministic serialization."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError("JSON with duplicate keys cannot be losslessly migrated to JSONB")
        result[key] = value
    return result


def canonical(table, names, row):
    """Serialize row values into a canonical engine-independent representation."""
    values = []
    for name, value in zip(names, row):
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = {"bytes": bytes(value).hex()}
        elif value is not None and (table, name) in JSONB_COLUMNS:
            value = {"json": json.loads(value, object_pairs_hook=_object)}
        elif isinstance(value, (float, int)):
            # SQLite REAL affinity returns 1.0; PostgreSQL may return integer 1.
            value = {"number": str(value) if not float(value).is_integer() else str(int(value))}
        values.append(value)
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def evidence(connection):
    """Compute row counts and content digests for each transferred table."""
    result = {}
    for table in tables(connection):
        names = sorted(name for name, _, _ in column_info(connection, table))
        columns = ",".join(connection.database.quote_identifier(name) for name in names)
        # Sorting row digests removes engine collation differences and also covers tables
        # without a primary key, including duplicates. No row contents enter reports.
        hashes = sorted(hashlib.sha256(canonical(table, names, row)).digest()
                        for row in connection.execute(f'SELECT {columns} FROM "{table}"'))
        result[table] = {"rows": len(hashes), "sha256": hashlib.sha256(b"".join(hashes)).hexdigest()}
    return result


def sequences(connection):
    """Read generated identity positions from the source engine."""
    result = {}
    if connection.engine == "sqlite":
        if connection.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
            result = {table: int(value) for table, value in connection.execute("SELECT name,seq FROM sqlite_sequence")}
    else:
        for table, column in connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND is_identity='YES'"):
            name = connection.execute("SELECT pg_get_serial_sequence(?,?)", (f'public."{table}"', column)).fetchone()[0]
            # Sequence names originate from the catalogue, never a request.
            value, called = connection.execute(f"SELECT last_value,is_called FROM {name}").fetchone()
            result[table] = int(value) if called else max(0, int(value) - 1)
    return result


def repair_sequences(connection, original):
    """Advance destination identities beyond all transferred rows."""
    for table in ("events", "conversations", "messages", "uses"):
        if table not in tables(connection):
            continue
        highest = int(connection.execute(f'SELECT MAX(id) FROM "{table}"').fetchone()[0] or 0)
        value = max(highest, original.get(table, 0))
        if connection.engine == "sqlite":
            connection.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
            connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, value))
        else:
            connection.execute("SELECT setval(pg_get_serial_sequence(?, 'id'), ?, ?::boolean)",
                               (f'public."{table}"', max(1, value), "true" if value else "false"))


def artifacts(connection, extra=()):
    """Inventory external artifacts referenced by the selected stores."""
    paths = {str(Path(path).resolve()) for path in extra}
    if "models" in tables(connection):
        from loadcoach.models import files_of
        for settings, in connection.execute("SELECT settings FROM models"):
            paths.update(str(Path(path).resolve()) for path in files_of(json.loads(settings)).values())
    result = []
    for name in sorted(paths):
        path = Path(name)
        if not path.is_file():
            raise MigrationError("A required artifact is missing")
        stat = path.stat()
        # Large model weights are inventoried without rehashing gigabytes on every preview.
        result.append({"path": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return result


def preflight(source, target, extra_artifacts=()):
    """Validate engines, schemas and destination readiness before transferring rows."""
    if source.engine == target.engine and source.store == target.store:
        raise MigrationError("Source and target must differ")
    if target.engine == "sqlite":
        if target.path is None or target.path.exists() or target.path.is_symlink():
            raise MigrationError("SQLite target must be a new local file")
        if not target.path.parent.is_dir():
            raise MigrationError("Create the SQLite target directory before migration")
    with reader(source) as original:
        streams = schema_streams(original)
        check_schema(original, streams)
        chain = check_chain(original)
        # Also rejects malformed or duplicate-key JSON before a target is created.
        content = evidence(original)
        assets = artifacts(original, extra_artifacts)
        version = sqlite3.sqlite_version if source.engine == "sqlite" else original.execute("SHOW server_version").fetchone()[0]
        counts = {table: int(original.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in tables(original)}
        if source.engine == "postgresql" and target.engine == "postgresql":
            identity = original.execute("SELECT current_database(),inet_server_addr(),inet_server_port()").fetchone()
            with target.connection(connect_timeout=10) as destination:
                if identity == destination.execute("SELECT current_database(),inet_server_addr(),inet_server_port()").fetchone():
                    raise MigrationError("Source and target are the same PostgreSQL database")
    target_version = sqlite3.sqlite_version
    if target.engine == "postgresql":
        with target.connection(connect_timeout=10) as connection:
            objects = connection.execute(
                "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' LIMIT 1").fetchone()
            if objects:
                raise MigrationError("PostgreSQL target must be empty")
            if not connection.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0]:
                raise MigrationError("PostgreSQL target role cannot create its application schema")
            target_version = connection.execute("SHOW server_version").fetchone()[0]
    tools = {}
    if "postgresql" in (source.engine, target.engine):
        from .postgresql_tools import tool_version
        tools = {name: tool_version(name) for name in ("pg_dump", "pg_restore")}
        for database, server in ((source, version), (target, target_version)):
            if database.engine == "postgresql":
                major = int(str(server).split(".")[0])
                import re
                if any(int(re.search(r"(\d+)\.", value)[1]) < major for value in tools.values()):
                    raise MigrationError("PostgreSQL client tools are older than the server")
    required = max((source.path.stat().st_size if source.path else sum(counts.values()) * 4096) * 3, 16 * 1024 * 1024)
    workspace = target.path.parent if target.path else Path(source.store.dsn_file if source.engine == "postgresql" else source.path).parent
    free = shutil.disk_usage(workspace).free
    if free < required:
        raise MigrationError("Insufficient local free space for migration and recovery")
    return {"source_engine": source.engine, "target_engine": target.engine, "source_version": str(version),
            "target_version": str(target_version), "streams": streams, "counts": counts,
            "chain": chain, "content": content, "artifacts": assets, "tools": tools, "free_bytes": free, "required_bytes": required}


def copy_store(source, target, extra_artifacts=()):
    """Copy once to an empty target and validate before any binding is changed."""
    checked = preflight(source, target, extra_artifacts)
    try:
        if target.engine == "sqlite":
            descriptor = os.open(target.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        with reader(source) as original, target.connection() as destination:
            create_schema(target, destination, checked["streams"])
            source_tables = tables(original)
            if source_tables != tables(destination):
                raise MigrationError("Target schema tables differ from source")
            destination.execute("DELETE FROM suite_schema_migrations")
            destination.commit()
            if target.engine == "sqlite":
                destination.execute("PRAGMA foreign_keys=OFF")
            destination.execute("BEGIN")
            destination.execute("SET CONSTRAINTS ALL DEFERRED") if target.engine == "postgresql" else None
            for table in source_tables:
                info = column_info(original, table)
                names = sorted(name for name, _, _ in info)
                if set(names) != {name for name, _, _ in column_info(destination, table)}:
                    raise MigrationError(f"Target columns differ for {table}")
                columns = ",".join(target.quote_identifier(name) for name in names)
                primary = [name for name, _, pk in sorted(info, key=lambda item: item[2]) if pk]
                order = ",".join(target.quote_identifier(name) for name in (primary or names))
                rows = original.execute(f'SELECT {columns} FROM "{table}" ORDER BY {order}')
                sql = f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in names)})'
                while batch := rows.fetchmany(500):
                    destination.executemany(sql, [tuple(bytes(v) if isinstance(v, memoryview) else v for v in row) for row in batch])
            highwater = sequences(original)
            repair_sequences(destination, highwater)
            expected, actual = evidence(original), evidence(destination)
            if expected != actual:
                raise MigrationError("Target row counts or content hashes differ from source")
            chain = check_chain(destination)
            if chain != checked["chain"]:
                raise MigrationError("Target event chain differs from source")
            if artifacts(destination, extra_artifacts) != checked["artifacts"]:
                raise MigrationError("Artifact references or files changed during migration")
            if target.engine == "sqlite" and destination.execute("PRAGMA foreign_key_check").fetchone():
                raise MigrationError("Target foreign key check failed")
            destination.commit()
        # Independent connection sees committed content; a transaction-local check is insufficient.
        with reader(target) as restored:
            if evidence(restored) != expected or sequences(restored) != highwater:
                # Empty never-used sequences may be absent from SQLite's original catalogue.
                if evidence(restored) != expected or any(sequences(restored).get(k, 0) != v for k, v in highwater.items()):
                    raise MigrationError("Committed target verification failed")
            check_chain(restored)
        return {"state": "verified", "tables": expected, "chain": chain,
                "sequences": highwater, "artifacts": checked["artifacts"]}
    except BaseException:
        # Keep failed targets as evidence. Never wipe a database supplied by an operator.
        # Bindings have not been switched and the source is still authoritative.
        raise
