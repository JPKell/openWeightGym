"""Credential-safe PostgreSQL dump, restore and inspection helpers.

Native PostgreSQL tools receive a connection string with its password removed.  The password is
present only in the child process environment and is never returned in metadata or error text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
import time
from pathlib import Path

from .database import Database, DatabaseConfigurationError, DriverUnavailable


class PostgreSQLToolError(RuntimeError):
    """A required native tool or a PostgreSQL restore check failed."""


def _parameters(database: Database, dbname: str | None = None) -> tuple[dict, str]:
    """Build PostgreSQL connection arguments from the configured secret reference."""
    if database.engine != "postgresql":
        raise ValueError("PostgreSQL tools require a PostgreSQL store")
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        raise DriverUnavailable("PostgreSQL tools require openWeightGym's postgresql extra") from None
    secret = Path(database.store.dsn_file)
    try:
        if secret.stat().st_mode & 0o077:
            raise DatabaseConfigurationError(f"{secret}: PostgreSQL DSN file must be owner-readable only")
        raw = secret.read_text().strip()
    except OSError as problem:
        raise DatabaseConfigurationError(f"{secret}: PostgreSQL DSN file cannot be read: {problem}") from None
    if not raw:
        raise DatabaseConfigurationError(f"{secret}: PostgreSQL DSN file is empty")
    try:
        parameters = conninfo_to_dict(raw)
    except Exception:
        raise DatabaseConfigurationError(f"{secret}: PostgreSQL DSN is invalid") from None
    password = str(parameters.pop("password", ""))
    if dbname is not None:
        parameters["dbname"] = dbname
    return parameters, password


def redacted_error(database: Database, problem: BaseException) -> str:
    """Return bounded diagnostic text with DSN and password forms removed."""
    message = str(problem)
    if database.engine == "postgresql":
        try:
            raw = Path(database.store.dsn_file).read_text().strip()
            if raw:
                message = message.replace(raw, "[redacted DSN]")
        except OSError:
            pass
        message = re.sub(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@", r"\1[redacted]@", message, flags=re.IGNORECASE)
        message = re.sub(r"(?i)(password\s*=\s*)(?:'[^']*'|\"[^\"]*\"|\S+)", r"\1[redacted]", message)
    return re.sub(r"\s+", " ", message).strip()[:500]


def _connection(database: Database, dbname: str | None = None) -> tuple[str, dict[str, str], str]:
    """Open a PostgreSQL connection using the selected store's credentials."""
    try:
        from psycopg.conninfo import make_conninfo
    except ImportError:
        raise DriverUnavailable("PostgreSQL tools require openWeightGym's postgresql extra") from None
    parameters, password = _parameters(database, dbname)
    safe = make_conninfo(**parameters)
    environment = os.environ.copy()
    if password:
        environment["PGPASSWORD"] = password
    environment.setdefault("PGCONNECT_TIMEOUT", "10")
    return safe, environment, password


def _tool(name: str) -> str:
    """Locate the required PostgreSQL backup or restore executable."""
    if found := shutil.which(name):
        return found
    raise PostgreSQLToolError(f"{name} is required but is not installed")


def _server_tool(database: Database, name: str) -> str:
    """Use the server's major version: newer dumps may not restore to it."""
    with database.connection(connect_timeout=10) as connection:
        major = int(connection.execute('SHOW server_version_num').fetchone()[0]) // 10000
    candidates = [Path(f'/usr/lib/postgresql/{major}/bin/{name}'),
                  Path(f'/usr/pgsql-{major}/bin/{name}')]
    if found := shutil.which(name):
        candidates.append(Path(found))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            version = tool_version(str(candidate))
        except PostgreSQLToolError:
            continue
        if re.search(r'\(PostgreSQL\)\s+' + str(major) + r'(?:\.|\s|$)', version):
            return str(candidate)
    raise PostgreSQLToolError(f'PostgreSQL {major} needs matching {name} client tools for restorable backups. '
                              f'Install the PostgreSQL {major} client tools on this node and retry.')


def _failure(name: str, problem: subprocess.CalledProcessError | subprocess.TimeoutExpired, password: str) -> PostgreSQLToolError:
    """Return a redacted PostgreSQL tool failure for the operator."""
    stderr = getattr(problem, "stderr", b"") or b""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    message = re.sub(r"\s+", " ", str(stderr)).strip()
    if password:
        message = message.replace(password, "[redacted]")
    return PostgreSQLToolError(f"{name} failed" + (f": {message[:500]}" if message else ""))


def _run(database: Database, name: str, arguments: list[str], *, dbname: str | None = None,
         capture: bool = False, timeout: int = 900, guarded_output: Path | None = None) -> subprocess.CompletedProcess:
    """Run a PostgreSQL maintenance tool with private connection environment."""
    connection, environment, password = _connection(database, dbname)
    command = [_server_tool(database, name), *arguments, f"--dbname={connection}"]
    try:
        if guarded_output is not None:
            started = time.monotonic()
            with subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
                while True:
                    try:
                        _, stderr = process.communicate(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        oversized = guarded_output.exists() and guarded_output.stat().st_size > 2 * 1024 ** 3
                        if oversized or shutil.disk_usage(guarded_output.parent).free < 64 * 1024 ** 2 or time.monotonic() - started > timeout:
                            process.kill()
                            process.communicate()
                            raise PostgreSQLToolError('SQL export exceeded its 2 GiB, disk-space or time budget. Export smaller stores separately.')
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command, stderr=stderr)
                if guarded_output.stat().st_size > 2 * 1024 ** 3:
                    raise PostgreSQLToolError('SQL export exceeded its 2 GiB limit.')
                return subprocess.CompletedProcess(command, 0, stderr=stderr)
        return subprocess.run(
            command,
            env=environment,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as problem:
        raise _failure(name, problem, password) from None


def tool_version(name: str) -> str:
    """Return one native tool's first version line, or raise when it is unavailable."""
    path = _tool(name)
    try:
        answer = subprocess.run([path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True, timeout=15, text=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as problem:
        raise PostgreSQLToolError(f"{name} --version failed") from problem
    return answer.stdout.strip().splitlines()[0][:200]


def dump_custom(database: Database, output: Path) -> dict:
    """Write a custom-format logical backup and return non-secret tool metadata."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.chmod(0o600) if output.exists() else None
    _run(database, "pg_dump", ["--format=custom", "--no-owner", "--no-privileges", f"--file={output}"])
    output.chmod(0o600)
    listing = restore_list(database, output)
    return {
        "method": "pg_dump custom format",
        "tools": {name: tool_version(_server_tool(database, name)) for name in ('pg_dump', 'pg_restore')},
        "restore_list_entries": listing,
    }


def _split_table(table: str) -> tuple[str, str]:
    """Separate a qualified PostgreSQL schema and table identifier."""
    if "." in table:
        schema, name = table.split(".", 1)
    else:
        schema, name = "public", table
    if not schema or not name or "\x00" in schema + name:
        raise KeyError(table)
    return schema, name


def dump_sql(database: Database, output: Path, table: str | None = None) -> str:
    """Write a complete or exact-table plain SQL dump using argv rather than a shell."""
    arguments = ["--format=plain", "--no-owner", "--no-privileges", f"--file={output}"]
    if table is not None:
        schema, name = _split_table(table)
        available = set(postgresql_tables(database))
        qualified = f"{schema}.{name}"
        if qualified not in available:
            raise KeyError(table)
        pattern = f'{database.quote_identifier(schema)}.{database.quote_identifier(name)}'
        arguments.extend(("--strict-names", f"--table={pattern}"))
    _run(database, "pg_dump", arguments, guarded_output=output)
    output.chmod(0o600)
    return "pg_dump plain SQL"


def restore_list(database: Database, archive: Path) -> int:
    """Inspect a custom archive with pg_restore and return its number of data entries."""
    del database  # the archive listing itself does not connect to a server
    try:
        answer = subprocess.run([_tool("pg_restore"), "--list", str(archive)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=True, timeout=120, text=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as problem:
        raise _failure("pg_restore --list", problem, "") from None
    entries = sum(1 for line in answer.stdout.splitlines() if line and not line.startswith(";"))
    if not entries:
        raise PostgreSQLToolError("pg_restore --list found no entries in the archive")
    return entries


def postgresql_tables(database: Database) -> list[str]:
    """List exact schema-qualified application tables in stable order."""
    with database.connection(connect_timeout=10) as connection:
        rows = connection.execute(
            "SELECT schemaname, tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname NOT LIKE 'pg_%' AND schemaname <> 'information_schema' "
            "ORDER BY schemaname, tablename"
        )
        return [f"{schema}.{table}" for schema, table in rows]


def _counts(connection) -> dict[str, int]:
    """Count table rows for backup and restore verification."""
    rows = connection.execute(
        "SELECT schemaname, tablename FROM pg_catalog.pg_tables "
        "WHERE schemaname NOT LIKE 'pg_%' AND schemaname <> 'information_schema' "
        "ORDER BY schemaname, tablename"
    ).fetchall()
    result: dict[str, int] = {}
    for schema, table in rows:
        quoted = f'"{schema.replace(chr(34), chr(34) * 2)}"."{table.replace(chr(34), chr(34) * 2)}"'
        result[f"{schema}.{table}"] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    return result


def _versions(connection) -> dict[str, int]:
    """Read retained schema migration versions for verification."""
    tables = {row[0] for row in connection.execute(
        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = ANY (current_schemas(false))"
    )}
    if "suite_schema_migrations" not in tables:
        return {}
    return {stream: int(version) for stream, version in connection.execute(
        "SELECT stream, MAX(version) FROM suite_schema_migrations GROUP BY stream ORDER BY stream"
    )}


def source_metadata(database: Database) -> dict:
    """Read server, schema-version and exact-count evidence from the source store."""
    from .health import postgresql_identity

    with database.connection(connect_timeout=10) as connection:
        return {
            "source_identity": postgresql_identity(connection),
            "server_version": str(connection.execute("SHOW server_version").fetchone()[0]),
            "schema_versions": _versions(connection),
            "counts": _counts(connection),
        }


def verify_custom_restore(database: Database, archive: Path, expected: dict) -> dict:
    """Restore a custom dump into a new database, validate it, and remove it."""
    try:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
    except ImportError:
        raise DriverUnavailable("PostgreSQL restore verification requires openWeightGym's postgresql extra") from None
    parameters, password = _parameters(database)
    temporary = "suite_verify_" + uuid.uuid4().hex[:20]
    administrative = {**parameters, "dbname": "postgres"}
    safe_admin = make_conninfo(**{key: value for key, value in administrative.items() if key != "password"})
    admin = None
    created = False
    try:
        admin = psycopg.connect(safe_admin, password=password or None, autocommit=True, connect_timeout=10)
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(temporary)))
        created = True
        _run(
            database,
            "pg_restore",
            ["--exit-on-error", "--no-owner", "--no-privileges", str(archive)],
            dbname=temporary,
        )
        restored_parameters = {**parameters, "dbname": temporary}
        safe_restored = make_conninfo(**{key: value for key, value in restored_parameters.items() if key != "password"})
        with psycopg.connect(safe_restored, password=password or None, connect_timeout=10) as restored:
            counts = _counts(restored)
            versions = _versions(restored)
        if counts != expected.get("counts", {}):
            raise PostgreSQLToolError("restored PostgreSQL table counts do not match the source")
        if versions != expected.get("schema_versions", {}):
            raise PostgreSQLToolError("restored PostgreSQL schema versions do not match the source")
        return {"counts": counts, "schema_versions": versions, "temporary_database": "created and removed"}
    finally:
        if admin is not None:
            if created:
                try:
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(temporary)))
                except Exception:
                    pass
            admin.close()
