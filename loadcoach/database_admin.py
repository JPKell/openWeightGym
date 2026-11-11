"""Read-only database inventory and restorable SQL exports for System -> DB."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import hashlib
import json
import time
import asyncio
from dataclasses import replace
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from db import Database, DatabaseRegistry, connect
from db.postgresql_tools import dump_sql as postgresql_dump_sql, redacted_error


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class DatabaseSource:
    """One configured or discovered database with a bounded public identifier."""

    id: str
    path: Path | None
    owner: str
    expected: bool = True
    database: Database | None = None
    bindings: tuple[str, ...] = ()
    stores: tuple[str, ...] = ()


def sqlite_sources(folder: str | Path, owners: dict[str, str], primary: str | Path | None = None) -> dict[str, DatabaseSource]:
    """Discover top-level SQLite files and attach known service ownership."""
    folder = Path(folder)
    paths = {path.resolve() for path in folder.glob("*.db") if path.is_file() and not path.is_symlink()}
    if primary and str(primary) != ":memory:":
        main = Path(primary)
        if main.is_file() and not main.is_symlink():
            paths.add(main.resolve())
    answer: dict[str, DatabaseSource] = {}
    for path in sorted(paths, key=lambda item: item.name):
        identifier = path.name
        if not SAFE_ID.fullmatch(identifier) or identifier in answer:
            continue
        answer[identifier] = DatabaseSource(identifier, path, owners.get(path.name, "Unassigned database file"),
                                            path.name in owners, Database.sqlite(path, identifier))
    return answer


def store_sources(stores, folder: str | Path, owners: dict[str, str]) -> dict[str, DatabaseSource]:
    """Enumerate trusted store objects and discover additional local SQLite files once."""
    answer: dict[str, DatabaseSource] = {}
    configured_paths: set[Path] = set()
    for store in stores:
        target = store.database
        path = target.path
        identifier = path.name if path is not None and SAFE_ID.fullmatch(path.name) else store.name
        if identifier in answer:
            identifier = store.name
        owner = owners.get(path.name, "") if path is not None else ""
        owner = owner or ", ".join(store.bindings) or "Unbound configured store"
        answer[identifier] = DatabaseSource(identifier, path, owner, bool(store.bindings), target, store.bindings, (store.name,))
        if path is not None:
            configured_paths.add(path.resolve())
    for identifier, source in sqlite_sources(folder, owners).items():
        if source.path.resolve() not in configured_paths and identifier not in answer:
            answer[identifier] = replace(source, expected=False)
    return dict(sorted(answer.items()))


def source_identity(source: DatabaseSource) -> str:
    """Identify an actual database, including shared files; never fingerprint credentials."""
    if source.database and source.database.engine == 'postgresql':
        from db.health import postgresql_identity
        with source.database.connection(connect_timeout=2) as connection:
            connection.execute("SET LOCAL statement_timeout = '2000ms'")
            return 'postgresql:' + postgresql_identity(connection)
    if source.path is None or source.path.is_symlink() or not source.path.is_file():
        raise ValueError('The configured SQLite database is missing or not a regular local file.')
    stat = source.path.stat()
    try:
        host = Path('/etc/machine-id').read_text().strip()
    except OSError:
        import socket
        host = socket.gethostname()
    return 'sqlite:' + hashlib.sha256(json.dumps([host, stat.st_dev, stat.st_ino]).encode()).hexdigest()


def summaries(sources: dict[str, DatabaseSource], machine: str) -> list[dict]:
    """Bounded summary probes: no exact row counts, dbstat scans or schema exports."""
    rows = []
    for source in sources.values():
        engine = 'PostgreSQL' if source.database and source.database.engine == 'postgresql' else 'SQLite'
        row = dict(id=source.id, name=source.path.name if source.path else source.database.display_name,
                   machine=machine, owner=source.owner, expected=source.expected, engine=engine,
                   bindings=list(source.bindings), stores=list(source.stores), path=str(source.path) if source.path else source.database.display_name,
                   identity='', size=None, table_count=None, records=None, checked_at=time.time(), error='',
                   tables=[], dump_method='pg_dump plain SQL' if engine == 'PostgreSQL' else 'Python sqlite3 fallback')
        try:
            row['identity'] = source_identity(source)
            if engine == 'SQLite':
                connection = connect(source.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
                try:
                    row['table_count'] = connection.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
                    row['server_version'] = sqlite3.sqlite_version
                finally:
                    connection.close()
                row['size'] = sum(item['size'] for item in _sidecars(source.path))
            else:
                with source.database.connection(connect_timeout=2) as connection:
                    connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute("SET LOCAL statement_timeout = '2000ms'")
                    row['size'], row['database_name'], row['server_version'] = connection.execute(
                        'SELECT pg_database_size(current_database()), current_database(), current_setting(\'server_version\')').fetchone()
                    row['table_count'], row['records'] = connection.execute(
                        "SELECT COUNT(*), COALESCE(SUM(GREATEST(c.reltuples,0)),0)::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE c.relkind='r' AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'").fetchone()
                    row['records_estimated'] = True
        except Exception:
            row['error'] = 'Store probe failed. Check the owning node, configured binding and database permissions.'
        rows.append(row)
    return rows


async def async_summaries(sources: dict[str, DatabaseSource], machine: str) -> list[dict]:
    semaphore = asyncio.Semaphore(4)
    async def probe(identifier, source):
        async with semaphore:
            return await asyncio.to_thread(summaries, {identifier: source}, machine)
    return [row for batch in await asyncio.gather(*(probe(identifier, source) for identifier, source in sources.items())) for row in batch]


def merge_inventory(rows: list[dict]) -> list[dict]:
    """Merge only proven physical identities; retain each node/store route and binding."""
    merged = {}
    for item in rows:
        row = dict(item)
        route = {'machine': row['machine'], 'id': row['id'], 'bindings': row.get('bindings', []), 'stores': row.get('stores', [])}
        key = row.get('identity') or f"unknown:{row['machine']}:{row['id']}"
        if key not in merged:
            row['consumers'] = [route]
            merged[key] = row
        else:
            target = merged[key]
            if row['expected'] and not target['expected']:
                row['consumers'] = target['consumers']
                target = merged[key] = row
            target['consumers'].append(route)
            target['bindings'] = sorted(set(target.get('bindings', [])) | set(row.get('bindings', [])))
            target['stores'] = sorted(set(target.get('stores', [])) | set(row.get('stores', [])))
            target['expected'] = target['expected'] or row['expected']
    return list(merged.values())


def configured_sources(registry: DatabaseRegistry, folder: str | Path, owners: dict[str, str]) -> dict[str, DatabaseSource]:
    """Enumerate every configured registry store and discovered local SQLite files once."""
    return store_sources(registry.bound_stores(), folder, owners)


def dump_tools() -> dict[str, dict]:
    """Report native dump programs without claiming their database engines are supported."""
    tools = {}
    for name in ("sqlite3", "pg_dump", "pg_restore", "psql", "pgloader"):
        path = shutil.which(name)
        tools[name] = {"available": bool(path), "path": path or ""}
    return tools


def _identifier(name: str) -> str:
    """Quote a SQL identifier while escaping embedded quotation marks."""
    return '"' + name.replace('"', '""') + '"'


def _sidecars(path: Path) -> list[dict]:
    """Describe a SQLite database and its existing WAL and shared-memory files."""
    rows = []
    for suffix, kind in (("", "database"), ("-wal", "write-ahead log"), ("-shm", "shared-memory index")):
        candidate = Path(str(path) + suffix)
        if candidate.is_file():
            rows.append({"kind": kind, "path": str(candidate), "size": candidate.stat().st_size})
    return rows


def sqlite_catalog(source: DatabaseSource, machine: str) -> dict:
    """Return exact row counts and allocated table/index bytes from a read-only connection."""
    base = {
        "id": source.id,
        "name": source.path.name,
        "engine": "SQLite",
        "machine": machine,
        "owner": source.owner,
        "expected": source.expected,
        "path": str(source.path),
        "tables": [],
    }
    try:
        sidecars = _sidecars(source.path)
        db = connect(f"file:{source.path}?mode=ro", uri=True, timeout=10)
        try:
            deadline = time.monotonic() + 30
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            db.execute("PRAGMA query_only=ON")
            db.execute('BEGIN')
            page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(db.execute("PRAGMA freelist_count").fetchone()[0])
            schemas = db.execute(
                "SELECT name, sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            page_bytes: dict[str, int] = {}
            table_indexes: dict[str, list[str]] = {}
            try:
                page_bytes = {name: int(size or 0) for name, size in db.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
                for name, table in db.execute("SELECT name, tbl_name FROM sqlite_schema WHERE type='index' AND name IS NOT NULL"):
                    table_indexes.setdefault(table, []).append(name)
            except sqlite3.Error:
                pass
            tables = []
            for name, schema in schemas:
                records = int(db.execute(f"SELECT COUNT(*) FROM {_identifier(name)}").fetchone()[0])
                allocated = page_bytes.get(name)
                if page_bytes:
                    allocated = (allocated or 0) + sum(page_bytes.get(index, 0) for index in table_indexes.get(name, []))
                tables.append(
                    {
                        "name": name,
                        "records": records,
                        "size": allocated,
                        "indexes": len(table_indexes.get(name, [])),
                        "schema": schema or "",
                    }
                )
        finally:
            db.close()
        stat = source.path.stat()
        return {
            **base,
            "size": sum(item["size"] for item in sidecars),
            "main_size": stat.st_size,
            "sidecars": sidecars,
            "allocated": page_count * page_size,
            "free": free_pages * page_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            "tables": tables,
            "dump_method": "Python sqlite3 fallback",
            "error": "",
        }
    except (OSError, sqlite3.Error, ValueError, TypeError) as problem:
        return {**base, "size": None, "main_size": None, "sidecars": [], "allocated": None, "free": None,
                "modified": "", "dump_method": "unavailable", "error": str(problem)[:500]}


def postgresql_catalog(source: DatabaseSource, machine: str) -> dict:
    """Return exact row counts and relation sizes for one configured PostgreSQL store."""
    base = {
        "id": source.id,
        "name": source.database.display_name,
        "engine": "PostgreSQL",
        "machine": machine,
        "owner": source.owner,
        "expected": source.expected,
        "path": source.database.display_name,
        "tables": [],
    }
    try:
        with source.database.connection(connect_timeout=10) as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            db.execute("SET LOCAL statement_timeout = '30000ms'")
            database_name = db.execute("SELECT current_database()").fetchone()[0]
            server_version = db.execute("SHOW server_version").fetchone()[0]
            size = int(db.execute("SELECT pg_database_size(current_database())").fetchone()[0])
            relations = db.execute(
                "SELECT n.nspname, c.relname, pg_total_relation_size(c.oid), "
                "(SELECT COUNT(*) FROM pg_catalog.pg_index i WHERE i.indrelid = c.oid) "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' "
                "AND n.nspname <> 'information_schema' ORDER BY n.nspname, c.relname"
            ).fetchall()
            tables = []
            for schema, table, allocated, indexes in relations:
                qualified = f'{source.database.quote_identifier(schema)}.{source.database.quote_identifier(table)}'
                records = int(db.execute(f"SELECT COUNT(*) FROM {qualified}").fetchone()[0])
                tables.append({"name": f"{schema}.{table}", "records": records, "size": int(allocated),
                               "indexes": int(indexes), "schema": ""})
        return {
            **base,
            "database_name": database_name,
            "server_version": str(server_version),
            "size": size,
            "main_size": size,
            "sidecars": [],
            "allocated": size,
            "free": None,
            "modified": "",
            "tables": tables,
            "dump_method": "pg_dump plain SQL",
            "error": "",
        }
    except Exception as problem:
        return {**base, "size": None, "main_size": None, "sidecars": [], "allocated": None,
                "free": None, "modified": "", "dump_method": "unavailable",
                "error": redacted_error(source.database, problem)}


def catalogs(sources: dict[str, DatabaseSource], machine: str) -> list[dict]:
    """Inspect all configured and discovered sources in stable order."""
    return [postgresql_catalog(source, machine) if source.database and source.database.engine == "postgresql"
            else sqlite_catalog(source, machine) for source in sources.values()]


def _literal(value) -> str:
    """Render one SQLite value as a round-trippable SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    raise TypeError(f"Unsupported SQLite value {type(value).__name__}")


def _python_dump(source: DatabaseSource, output: Path, table: str | None) -> None:
    """Write a consistent exact-table or full SQLite dump without a CLI dependency."""
    db = connect(f"file:{source.path}?mode=ro", uri=True, timeout=30)
    try:
        deadline = time.monotonic() + 300
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        with output.open("w", encoding="utf-8", newline="\n") as target:
            written = 0
            checked_bytes = 0
            def emit(line):
                nonlocal written, checked_bytes
                written += len(line.encode('utf-8'))
                if written > 2 * 1024 ** 3 or time.monotonic() > deadline:
                    raise ValueError('SQL export exceeded its 2 GiB, disk-space or time budget.')
                if written - checked_bytes >= 1024 ** 2:
                    checked_bytes = written
                    if shutil.disk_usage(output.parent).free < 64 * 1024 ** 2:
                        raise ValueError('SQL export exhausted its disk-space reserve.')
                target.write(line)
            if table is None:
                for line in db.iterdump():
                    emit(line + "\n")
                return
            row = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
            if not row or not row[0]:
                raise KeyError(table)
            columns = [item[1] for item in db.execute(f"PRAGMA table_xinfo({_identifier(table)})") if item[6] == 0]
            if not columns:
                raise ValueError(f"Table {table} has no dumpable columns")
            names = ",".join(_identifier(name) for name in columns)
            emit("PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\n")
            emit(row[0].rstrip(";") + ";\n")
            query = f"SELECT {names} FROM {_identifier(table)}"
            prefix = f"INSERT INTO {_identifier(table)}({names}) VALUES("
            for values in db.execute(query):
                emit(prefix + ",".join(_literal(value) for value in values) + ");\n")
            for sql, in db.execute(
                "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
                (table,),
            ):
                emit(sql.rstrip(";") + ";\n")
            emit("COMMIT;\n")
    finally:
        db.close()


def make_sql_dump(source: DatabaseSource, table: str | None = None) -> tuple[Path, str]:
    """Create an owner-only temporary SQL dump and name the implementation used."""
    if source.database and source.database.engine == "postgresql":
        descriptor, name = tempfile.mkstemp(prefix="suite-dump-", suffix=".sql")
        os.close(descriptor)
        output = Path(name)
        output.chmod(0o600)
        try:
            return output, postgresql_dump_sql(source.database, output, table)
        except Exception:
            output.unlink(missing_ok=True)
            raise
    if source.path is None:
        raise ValueError("SQLite source has no local path")
    if table is not None:
        db = connect(f"file:{source.path}?mode=ro", uri=True)
        try:
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone():
                raise KeyError(table)
        finally:
            db.close()
    descriptor, name = tempfile.mkstemp(prefix="suite-dump-", suffix=".sql")
    os.close(descriptor)
    output = Path(name)
    output.chmod(0o600)
    try:
        _python_dump(source, output, table)
        return output, "Python sqlite3 fallback"
    except Exception:
        output.unlink(missing_ok=True)
        raise


def safe_filename(source: DatabaseSource, table: str | None = None) -> str:
    """Use only bounded source/table names in a Content-Disposition filename."""
    raw = source.path.stem if source.path is not None else source.id
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-") or "database"
    part = re.sub(r"[^A-Za-z0-9._-]+", "-", table or "all").strip("-") or "table"
    return f"{stem}-{part}.sql"
