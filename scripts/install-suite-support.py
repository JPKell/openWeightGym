#!/usr/bin/env python3
"""Compatibility checks and database configuration for install-suite.sh.

This helper deliberately never accepts a DSN value.  PostgreSQL credentials stay in
owner-only files and only their paths are written to normal openWeightGym settings.
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# Running a script by path puts scripts/, rather than the repository root, on sys.path.
REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from weightroom.settings_file import save_settings, set_toml_value, toml_value  # noqa: E402  (re-exported for the installer's tests)


SERVICE_CONFIG = {
    "loadcoach": "loadcoach.toml",
    "toollocker": "toollocker.toml",
    "spotter": "spotter.toml",
    "voice": "voice.toml",
    "weightroom": "weightroom.toml",
    "turnstile": "weightroom.toml",
    "voice-gateway": "voice-gateway.toml",
}


def firewall_ports(root: Path, services: list[str]) -> list[int]:
    """Read configured external TCP listeners without starting services or opening credential files."""
    defaults = {'weightroom': 8440, 'loadcoach': 8442, 'toollocker': 8443, 'spotter': 8444, 'voice': 8445,
                'voice-gateway': 8446}
    ports = set()

    def add(host, port):
        """Exclude loopback listeners and reject invalid network settings."""
        if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Each listener needs a host and a TCP port from 1 through 65535.')
        if host.lower() == 'localhost':
            return
        try:
            if ipaddress.ip_address(host.strip('[]')).is_loopback:
                return
        except ValueError:
            pass
        ports.add(port)

    for service in services:
        filename = SERVICE_CONFIG[service]
        path = root / 'settings' / filename
        settings = {}
        for source in (path.parent / 'defaults' / filename, path):
            if source != path and not source.exists():
                continue
            with source.open('rb') as file:
                values = tomllib.load(file)
            for key, value in values.items():
                settings[key] = {**settings.get(key, {}), **value} if isinstance(value, dict) else value
        if service == 'turnstile':
            gateway = settings.get('turnstile', {})
            for port in gateway.get('ports', [8442, 8443, 8444]):
                add(gateway.get('host', '127.0.0.1'), port)
        else:
            add(settings.get('host', '127.0.0.1'), settings.get('port', defaults[service]))
            if service == 'voice-gateway':
                for name, host in [('desktop', '127.0.0.1'), ('public', '0.0.0.0')]:
                    if settings.get(name + '_port'):
                        add(settings.get(name + '_host', host), settings[name + '_port'])
    return sorted(ports)


def repair_postgresql(root: Path) -> int:
    """Reconcile retained installer credentials with their local roles and databases."""
    directory = root / "keys/postgresql"
    paths = sorted(directory.glob("*.dsn"))
    if not paths:
        return 0

    def literal(value: str) -> str:
        """Quote a value as an SQL string literal."""
        return "'" + value.replace("'", "''") + "'"

    def identifier(value: str) -> str:
        """Quote a value as an SQL identifier."""
        return '"' + value.replace('"', '""') + '"'

    def admin(statement: str, operation: str) -> str:
        """Run one statement as the local postgres superuser; a failure names the operation, never the SQL."""
        answer = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "postgres"],
            input="SET standard_conforming_strings=on;\n" + statement + ";\n",
            text=True, capture_output=True, timeout=30,
        )
        if answer.returncode:
            # PostgreSQL can include the SQL statement (and a password) in diagnostics.
            raise ValueError(f"PostgreSQL administration failed while {operation}; no credentials were printed")
        return answer.stdout.strip()

    port = int(admin("SHOW port", "checking the local server"))
    entries = []
    for path in paths:
        if path.name not in {f"{name}.dsn" for name in ("core", "telemetry", "accounts", "roles", "chat", "benchmarks", "plugins", "alerts", "schedules")}:
            continue
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(f"PostgreSQL connection file must be a private regular file: {path}")
        raw = path.read_text().strip()
        uri = urlsplit(raw)
        role, password, database = unquote(uri.username or ""), unquote(uri.password or ""), unquote(uri.path.lstrip("/"))
        if (uri.scheme not in {"postgresql", "postgres"} or uri.hostname != "127.0.0.1"
                or not re.fullmatch(r"suite_[A-Za-z0-9_]{1,57}", role)
                or not re.fullmatch(r"[A-Za-z0-9_]{1,63}", database) or not password
                or uri.query or uri.fragment):
            raise ValueError(f"Refusing automatic repair of a connection that is not installer-managed and local: {path}")
        if (uri.port or 5432) != port:
            raise ValueError(f"Refusing to redirect retained PostgreSQL connection {path}: its server port differs from the selected local server")
        owner = admin(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=" + literal(database),
            f"checking ownership of {database}",
        )
        if owner and owner != role:
            raise ValueError(f"Refusing to change database {database}: its owner differs from the retained connection file")
        privilege = admin(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=" + literal(role),
            f"checking role {role}",
        )
        if privilege == "t":
            raise ValueError(f"Refusing to change privileged PostgreSQL role {role}")
        entries.append({"path": path, "raw": raw, "uri": uri, "role": role, "password": password,
                        "database": database, "exists": bool(owner), "role_exists": bool(privilege)})

    def connects(role: str, password: str, database: str) -> bool:
        """Whether this role and password can open the database on the local server."""
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        environment.update(PGHOST="127.0.0.1", PGPORT=str(port), PGUSER=role, PGPASSWORD=password,
                           PGDATABASE=database, PGCONNECT_TIMEOUT="5")
        answer = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", "SELECT 1"],
                                env=environment, text=True, capture_output=True, timeout=10)
        return answer.returncode == 0 and answer.stdout.strip() == "1"

    credentials = {}
    for entry in entries:
        credentials.setdefault(entry["role"], []).append(entry)
    for role, group in credentials.items():
        working = next((entry for entry in group
                        if connects(role, entry["password"], entry["database"] if entry["exists"] else "postgres")), None)
        password = (working or group[0])["password"]
        if working is None:
            verb = "ALTER" if group[0]["role_exists"] else "CREATE"
            admin(f"{verb} ROLE {identifier(role)} LOGIN PASSWORD {literal(password)}", f"restoring role {role}")
        for entry in group:
            database, path = entry["database"], entry["path"]
            if not entry["exists"]:
                admin(f"CREATE DATABASE {identifier(database)} OWNER {identifier(role)}", f"restoring database {database}")
            uri = entry["uri"]
            updated = urlunsplit((uri.scheme,
                                  f"{quote(role, safe='')}:{quote(password, safe='')}@127.0.0.1:{port}",
                                  uri.path, "", ""))
            if updated != entry["raw"]:
                with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as pending:
                    pending_path = Path(pending.name)
                    try:
                        os.fchmod(pending.fileno(), 0o600)
                        pending.write(updated + "\n")
                        pending.flush()
                        os.replace(pending_path, path)
                    finally:
                        pending_path.unlink(missing_ok=True)
            if not connects(role, password, database):
                raise ValueError(f"PostgreSQL authentication still fails for managed role {role} and database {database}")
    print(f"Verified local PostgreSQL credentials for {len(entries)} retained store(s).")
    return 0

SCHEMAS = {
    "loadcoach.retirement": "COMPUTE_RETIREMENT",
    "loadcoach.settings": "SETTINGS",
    "loadcoach.groups": "GROUPS",
    "loadcoach.outside": "OUTSIDE_SERVICES",
    "loadcoach.users": "USERS",
    "loadcoach.log": "LOG",
    "loadcoach.models": "MODELS",
    "loadcoach.roles": "ROLES",
    "loadcoach.reliability": "RELIABILITY",
    "loadcoach.role_evidence": "ROLE_EVIDENCE",
    "loadcoach.batches": "BATCHES",
    "loadcoach.tasks": "TASKS",
    "turnstile.benchmark_tasks": "TASK_RECORDS",
    "mirrorwall.front": "MIRRORWALL",
    "soundroom.front": "SOUNDROOM",
    "weightroom.chat": "CHAT",
    "weightroom.benchmarks": "BENCHMARKS",
    "weightroom.plugins": "PLUGINS",
    "weightroom.alerts": "ALERTS",
    "weightroom.schedules": "SCHEDULES",
    "loadcoach.schedules": "SCHEDULE_GRANTS",
    "plugins.runtime": "PLUGIN_RUNTIME",
    "spotter.history": "SPOTTER",
    "toollocker.tools": "TOOL_SCHEMAS",
}


@dataclass
class Finding:
    """One installation compatibility finding and its operator-facing explanation."""
    level: str
    subject: str
    detail: str


def known_migrations() -> dict[str, dict[int, str]]:
    """List schema migrations understood by the installed source tree."""
    from db import schemas

    result: dict[str, dict[int, str]] = {}
    for stream, constant in SCHEMAS.items():
        if stream=='mirrorwall.front':
            from mirrorwall import schema as owner
        elif stream == 'soundroom.front':
            from soundroom import schema as owner
        else:
            owner=schemas
        result[stream] = {
            migration.version: migration.name for migration in getattr(owner, constant)
        }
    return result


def migration_findings(connection, subject: str) -> list[Finding]:
    """Compare retained schema versions with the supported migration streams."""
    try:
        rows = connection.execute(
            "SELECT stream, version, name FROM suite_schema_migrations ORDER BY stream, version"
        ).fetchall()
    except Exception as problem:
        if "no such table" in str(problem).lower() or "does not exist" in str(problem).lower():
            return []
        return [Finding("ERROR", subject, f"cannot inspect schema migrations: {problem}")]
    known = known_migrations()
    findings: list[Finding] = []
    for stream, version, name in rows:
        versions = known.get(str(stream))
        if versions is None:
            findings.append(Finding("WARN", subject, f"unknown migration stream {stream!r}"))
        elif int(version) not in versions:
            findings.append(Finding(
                "ERROR", subject,
                f"schema {stream} is at version {version}, newer than this version of openWeightGym supports",
            ))
        elif versions[int(version)] != name:
            findings.append(Finding(
                "ERROR", subject,
                f"schema {stream} migration {version} is {name!r}; expected {versions[int(version)]!r}",
            ))
    return findings


def sqlite_findings(path: Path) -> list[Finding]:
    """Inspect configured SQLite stores for compatibility and recovery issues."""
    subject = str(path)
    if not path.exists():
        return [Finding("OK", subject, "new SQLite database")]
    if not path.is_file() or path.is_symlink():
        return [Finding("ERROR", subject, "SQLite target must be a regular file, not a symlink")]
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            findings = [] if row and row[0] == "ok" else [Finding("ERROR", subject, f"quick_check returned {row!r}")]
            findings.extend(migration_findings(connection, subject))
            if not findings:
                findings.append(Finding("OK", subject, f"SQLite database is readable ({path.stat().st_size} bytes)"))
            return findings
        finally:
            connection.close()
    except Exception as problem:
        return [Finding("ERROR", subject, f"cannot open SQLite database: {problem}")]


def path_from(base: Path, value: str) -> Path:
    """Resolve a configured path relative to the installation directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def toollocker_on_front_target(config: Path, told: dict) -> tuple[str, str, Path]:
    """Resolve front's authoritative store before the gateway's runtime packages are installed."""
    if 'loadcoach' in told:
        raise ValueError('Choose front or loadcoach for ToolLocker, not both')
    owner = path_from(config.parent, str(told['front']))
    defaults = owner.parent / 'defaults' / owner.name
    first = tomllib.loads(defaults.read_text()) if defaults.is_file() else {}
    explicit = tomllib.loads(owner.read_text())
    merged = {name: {**first[name], **explicit[name]} for name in first.keys() & explicit.keys()
              if isinstance(first[name], dict) and isinstance(explicit[name], dict)}
    main = {**first, **explicit, **merged}
    from db import DatabaseRegistry
    data = path_from(owner.parent, str(main.get('data', '.')))
    store = DatabaseRegistry.from_config(main.get('databases'), owner.parent,
                                        {'weightroom.accounts': data / 'accounts.db'}).database('weightroom.accounts')
    return 'identity', store.engine, Path(store.store.dsn_file) if store.engine == 'postgresql' else store.path


def configured_targets(config: Path, service: str) -> list[tuple[str, str, Path]]:
    """Return (name, engine, local path or DSN file) without reading DSN contents."""
    told = tomllib.loads(config.read_text())
    section = told.get("databases")
    if section is not None:
        if not isinstance(section, dict) or set(section) - {"stores", "bindings"}:
            raise ValueError("[databases] may contain stores and bindings only")
        stores, bindings = section.get("stores", {}), section.get("bindings", {})
        if not isinstance(stores, dict) or not isinstance(bindings, dict):
            raise ValueError("database stores and bindings must be tables")
        targets = []
        for name, values in stores.items():
            if not isinstance(values, dict):
                raise ValueError(f"database store {name!r} must be a table")
            engine = str(values.get("engine", "")).lower()
            field = "path" if engine == "sqlite" else "dsn_file"
            if engine not in {"sqlite", "postgresql"} or not values.get(field):
                raise ValueError(f"database store {name!r} has an invalid engine or target")
            targets.append((str(name), engine, path_from(config.parent, str(values[field]))))
        if service == 'weightroom':
            # Omitted bindings still own default stores, including roles and alerts on an
            # existing installation with only the older three repositories bound.
            from db import DatabaseRegistry
            data = path_from(config.parent, str(told.get('data', '../data')))
            registry = DatabaseRegistry.from_config(section, config.parent,
                {'weightroom.' + name: data / (name + '.db') for name in ('accounts', 'roles', 'chat', 'benchmarks', 'plugins', 'alerts', 'schedules')})
            targets = [(bound.name, bound.database.engine,
                        Path(bound.database.store.dsn_file) if bound.database.engine == 'postgresql' else bound.database.path)
                       for bound in registry.bound_stores() if bound.bindings or bound.name in stores]
        return targets

    data = path_from(config.parent, str(told.get("data", "../data")))
    if service == "loadcoach":
        legacy = told.get("database")
        return [("core", "sqlite", path_from(config.parent, legacy) if legacy else data / "loadcoach.db")]
    if service in {"spotter", "toollocker"}:
        if service == "toollocker" and told.get("front"):
            return [toollocker_on_front_target(config, told)]
        loadcoach = path_from(config.parent, str(told.get("loadcoach", "loadcoach.toml")))
        main = tomllib.loads(loadcoach.read_text()) if loadcoach.exists() else {}
        main_data = path_from(loadcoach.parent, str(main.get("data", "../data")))
        identity = path_from(loadcoach.parent, str(main["database"])) if main.get("database") else main_data / "loadcoach.db"
        targets = [("identity", "sqlite", identity)]
        if service == "spotter":
            targets.append(("telemetry", "sqlite", data / "spotter.db"))
        return targets
    if service in {"weightroom", "turnstile"}:
        names = ('accounts', 'roles', 'chat', 'benchmarks', 'plugins', 'alerts', 'schedules') if service == 'weightroom' else ('accounts', 'roles', 'chat', 'benchmarks', 'plugins')
        return [(name, "sqlite", data / f"{name}.db") for name in names]
    return []


def postgresql_findings(name: str, dsn_file: Path, require_tools: bool = False) -> list[Finding]:
    """Inspect configured PostgreSQL stores for compatibility and tool availability."""
    subject = f"{name} ({dsn_file})"
    if not dsn_file.exists():
        return [Finding("ERROR", subject, "PostgreSQL DSN file does not exist")]
    try:
        mode = dsn_file.stat().st_mode
        if mode & 0o077:
            return [Finding("ERROR", subject, "PostgreSQL DSN file must have mode 600")]
        from db import Database
        database = Database.postgresql(dsn_file, name=name)
        with database.connection(connect_timeout=5) as connection:
            server = connection.execute("SHOW server_version").fetchone()[0]
            server_number = int(connection.execute("SHOW server_version_num").fetchone()[0])
            findings = migration_findings(connection, subject)
        from db.postgresql_tools import tool_version
        for tool in ("pg_dump", "pg_restore"):
            try:
                version_text = tool_version(tool)
                findings.append(Finding("OK", subject, version_text))
                digits = next((part for part in version_text.split() if part[0:1].isdigit()), "0")
                client_major = int(digits.split(".", 1)[0])
                if client_major < server_number // 10000:
                    findings.append(Finding(
                        "ERROR" if require_tools else "WARN", subject,
                        f"{tool} major {client_major} is older than PostgreSQL server major {server_number // 10000}",
                    ))
            except Exception as problem:
                findings.append(Finding("ERROR" if require_tools else "WARN", subject, str(problem)))
        if not findings:
            findings.append(Finding("OK", subject, f"PostgreSQL {server} is reachable"))
        else:
            findings.insert(0, Finding("OK", subject, f"PostgreSQL {server} is reachable"))
        return findings
    except Exception as problem:
        try:
            from db.postgresql_tools import redacted_error
            problem = redacted_error(Database.postgresql(dsn_file, name=name), problem)
        except Exception:
            problem = type(problem).__name__
        return [Finding("ERROR", subject, f"cannot use PostgreSQL: {problem}")]


def audit(root: Path, services: list[str], require_pg_tools: bool = False) -> int:
    """Collect installation compatibility findings without changing live bindings."""
    findings: list[Finding] = []
    if sys.version_info < (3, 13):
        findings.append(Finding("ERROR", "Python", f"{sys.version.split()[0]} is installed; openWeightGym requires 3.13 or newer"))
    else:
        findings.append(Finding("OK", "Python", sys.version.split()[0]))
    seen_configs: set[Path] = set()
    seen_targets: set[tuple[str, Path]] = set()
    for service in services:
        filename = SERVICE_CONFIG[service]
        config = root / "settings" / filename
        if config in seen_configs:
            continue
        seen_configs.add(config)
        if not config.exists():
            findings.append(Finding("NEW", filename, "no settings yet; the installer will write them from the example"))
            continue
        try:
            tomllib.loads(config.read_text())
            findings.append(Finding("OK", filename, "valid TOML"))
            target_service = "weightroom" if service == "turnstile" else service
            for name, engine, target in configured_targets(config, target_service):
                identity = (engine, target)
                if identity in seen_targets:
                    continue
                seen_targets.add(identity)
                findings.extend(
                    sqlite_findings(target)
                    if engine == "sqlite"
                    else postgresql_findings(name, target, require_pg_tools)
                )
        except (OSError, tomllib.TOMLDecodeError, ValueError) as problem:
            findings.append(Finding("ERROR", filename, str(problem)))
    for finding in findings:
        print(f"{finding.level:<5} {finding.subject}: {finding.detail}")
    errors = sum(item.level == "ERROR" for item in findings)
    warnings = sum(item.level == "WARN" for item in findings)
    print(f"Compatibility: {errors} error(s), {warnings} warning(s)")
    return 2 if errors else 0


def database_state(root: Path, services: list[str]) -> tuple[str, bool]:
    """Return the configured engine summary and whether any selected store has schema/data."""
    engines: set[str] = set()
    populated = False
    seen_configs: set[Path] = set()
    seen_targets: set[tuple[str, Path]] = set()
    for service in services:
        config = root / "settings" / SERVICE_CONFIG[service]
        if config in seen_configs or not config.exists():
            continue
        seen_configs.add(config)
        target_service = "weightroom" if service == "turnstile" else service
        for name, engine, target in configured_targets(config, target_service):
            identity = (engine, target)
            if identity in seen_targets:
                continue
            seen_targets.add(identity)
            engines.add(engine)
            if engine == "sqlite" and target.is_file():
                connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
                try:
                    populated = populated or connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%')"
                    ).fetchone()[0] == 1
                finally:
                    connection.close()
            elif engine == "postgresql":
                from db import Database
                database = Database.postgresql(target, name=name)
                with database.connection(connect_timeout=5) as connection:
                    populated = populated or bool(database.tables(connection))
    engine = next(iter(engines)) if len(engines) == 1 else ("mixed" if engines else "sqlite")
    return engine, populated


def database_block(service: str, engine: str, config: Path, dsns: dict[str, Path]) -> str:
    """Render the selected database stores and service bindings as TOML."""
    if service == "loadcoach":
        stores = {"core": ("loadcoach.core", "../data/loadcoach.db")}
    elif service == "spotter":
        stores = {
            "identity": ("spotter.identity", "../data/loadcoach.db"),
            "telemetry": ("spotter.history", "../data/spotter.db"),
        }
    elif service == "toollocker":
        front = config.exists() and tomllib.loads(config.read_text()).get("front")
        if front:
            # Front's store binding is authoritative; never create a second accounts store.
            raise ValueError("Front ToolLocker inherits WeightRoom's database; configure weightroom's store instead")
        stores = {"identity": ("toollocker.identity", "../data/loadcoach.db")}
    elif service == "weightroom":
        stores = {
            "accounts": ("weightroom.accounts", "../data/accounts.db"),
            "roles": ("weightroom.roles", "../data/roles.db"),
            "chat": ("weightroom.chat", "../data/chat.db"),
            "benchmarks": ("weightroom.benchmarks", "../data/benchmarks.db"),
            "plugins": ("weightroom.plugins", "../data/plugins.db"),
            "alerts": ("weightroom.alerts", "../data/alerts.db"),
            "schedules": ("weightroom.schedules", "../data/schedules.db"),
        }
    else:
        return ""
    lines = ["# BEGIN SUITE INSTALLER DATABASES"]
    for name, (_binding, path) in stores.items():
        lines += [f"[databases.stores.{name}]", f'engine = "{engine}"']
        if engine == "sqlite":
            lines.append(f'path = "{path}"')
        else:
            key = "core" if name == "identity" else name
            target = dsns.get(key)
            if target is None:
                raise ValueError(f"no PostgreSQL DSN file was provided for {key}")
            relative = os.path.relpath(target, config.parent)
            lines.append(f'dsn_file = "{relative}"')
        lines.append("")
    lines.append("[databases.bindings]")
    for name, (binding, _path) in stores.items():
        lines.append(f'"{binding}" = "{name}"')
    lines.append("# END SUITE INSTALLER DATABASES")
    return "\n".join(lines) + "\n"


def replace_databases(text: str, block: str) -> str:
    """Replace all [databases...] tables while preserving unrelated TOML and comments."""
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == "# BEGIN SUITE INSTALLER DATABASES":
            skipping = True
            continue
        if skipping and stripped == "# END SUITE INSTALLER DATABASES":
            skipping = False
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            table = stripped.lstrip("[").rstrip("]").strip()
            if table == "databases" or table.startswith("databases."):
                skipping = True
                continue
            if skipping:
                skipping = False
        if not skipping:
            output.append(line)
    result = "".join(output).rstrip() + "\n\n" + block
    tomllib.loads(result)
    return result


def configure(config: Path, service: str, engine: str, dsn_values: list[str]) -> int:
    """Validate installer choices and produce the reviewed installation configuration."""
    dsns: dict[str, Path] = {}
    for value in dsn_values:
        if "=" not in value:
            raise SystemExit("--dsn must be NAME=PATH")
        name, raw = value.split("=", 1)
        dsns[name] = Path(raw).expanduser().resolve()
    if service == "toollocker" and config.exists() and tomllib.loads(config.read_text()).get("front"):
        if toollocker_on_front_target(config, tomllib.loads(config.read_text()))[1] != engine:
            raise ValueError("Configure WeightRoom's owning database before its front ToolLocker")
        return 0
    block = database_block(service, engine, config, dsns)
    if not block:
        return 0
    original = config.read_text()
    config.write_text(replace_databases(original, block))
    return 0


# The examples are written for an account called `you` with the checkout in its home folder.
# Older examples named the original author's folders; they are still understood.
TEMPLATE_PATHS = re.compile(r"/home/you/openWeightGym|/home/jpk/ai/openWeightGym|/home/jpk/ai/suite|/home/jpk/weightroom|/home/you|/home/jpk")

# Turnstile keeps public ports 8442-8445. The front ToolLocker always stays on loopback 9443;
# compute programs also move up by 1000 when everything shares the same machine.
SHARED_HOST_PORTS = {"loadcoach": 9442, "toollocker": 9443, "spotter": 9444, "voice": 9445}


def topology_values(config_name: str, topology: str, root: Path, address: str, front: str, compute: str,
                    llama_server: str, machine_name: str = "", voice: bool = False, voice_model: str = "") -> dict[str, object]:
    """The settings a machine's part decides, by dotted TOML key: one machine (all), front or compute."""
    alone = topology == "all"
    here = "127.0.0.1" if alone else address
    if topology in ('all', 'compute') and not machine_name:
        # One machine: its node is a peer like any other, named for its host (docs/peer-nodes-plan.md).
        import socket
        from loadcoach.setting_names import node_name_for
        machine_name = node_name_for(socket.gethostname())
        if not machine_name:
            raise ValueError('This hostname is not a valid compute-node name; supply --machine-name with its peer name.')
    front = "http://127.0.0.1:8442" if alone else front.rstrip("/")
    compute_host = urlsplit(compute).hostname if compute else ""

    def port(program: str, usual: int) -> int:
        """A program's port: its usual one, or 1000 above it when it shares the machine with turnstile."""
        return SHARED_HOST_PORTS[program] if alone else usual

    if config_name == "loadcoach":
        values: dict[str, object] = {"host": here, "port": port("loadcoach", 8442), "models_folder": str(root / "models")}
        if front:
            values["front"] = front
        if llama_server:
            values["llama_server"] = llama_server
        if machine_name:  # the name a console gave this node when it joined
            values["machine_name"] = machine_name
        return values
    if config_name == "toollocker":
        return {"host": "127.0.0.1", "port": 9443, "front": "weightroom.toml"}
    if config_name == "spotter":
        return {"host": here, "port": port("spotter", 8444)}
    if config_name == "voice":
        profiles = {}
        if voice_model:
            defaults = tomllib.loads((REPOSITORY / "settings/defaults/voice.toml").read_text())
            profiles = {f"profiles.default.{key}": value for key, value in
                        {**defaults["profiles"]["default"], "model": voice_model}.items()}
        return {"host": here, "port": port("voice", 8445), **({"compute": front} if front else {}),
                **profiles}
    if config_name == "weightroom":
        extra = {"turnstile.ports": [8442, 8443, 8444, 8445], "turnstile.voice_port": 8445} if voice else {}
        if alone:
            return {f"machines.{machine_name}.compute": "http://127.0.0.1:9442",
                    f"machines.{machine_name}.spotter": "http://127.0.0.1:9444",
                    f"machines.{machine_name}.machine_key_file": "../keys/machine.key", **extra}
        if compute:
            from loadcoach.setting_names import node_name_for
            import ipaddress
            try:
                ipaddress.ip_address(compute_host)
                hostname = ''
            except ValueError:
                hostname = compute_host
            peer = machine_name or node_name_for(hostname)
            if not node_name_for(peer) or node_name_for(peer) != peer:
                raise ValueError('--compute-url needs --machine-name matching the node, or a URL with its hostname. Pair the node with owg pair instead.')
            host = f'[{compute_host}]' if ':' in compute_host else compute_host
            return {f'machines.{peer}.compute': compute.rstrip('/'),
                    f'machines.{peer}.spotter': f'http://{host}:8444',
                    f'machines.{peer}.machine_key_file': '../keys/machine.key', **extra}
        # No compute node yet: the console runs and says so until one pairs.
        return extra
    if config_name == "voice-gateway":
        voice_url = f"http://{compute_host}:8445" if compute_host and not alone else "http://127.0.0.1:9445"
        return {"voice": voice_url, "origins": ["http://localhost:8446", "http://127.0.0.1:8446"]}
    return {}


def render(example: Path, target: Path, root: Path, home: Path, values: dict[str, object]) -> int:
    """Write a machine's settings from an example: its folders, then the values its part decides."""
    def folder(match: re.Match) -> str:
        """The installation folder for a checkout path in the example, the home folder for a home path."""
        return str(root) if match.group(0) != "/home/you" and match.group(0) != "/home/jpk" else str(home)

    text = TEMPLATE_PATHS.sub(folder, example.read_text())
    for dotted, value in values.items():
        text = set_toml_value(text, dotted, value)
    made = tomllib.loads(text)
    for dotted, value in values.items():
        here = made
        for part in dotted.split("."):
            here = here[part]
        if here != value:
            raise ValueError(f"{target.name}: {dotted} came out as {here!r}, not {value!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return 0


def prepare_voice(root: Path, services: list[str], install_models=None) -> int:
    """Provision local voice identities and the pinned speech bundles required by its settings."""
    from weightroom.bootstrap import service_identity, voice_service_identity

    config = root / "settings/weightroom.toml"
    speech = root / "settings/voice.toml"
    if "weightroom" in services or "turnstile" in services:
        told = tomllib.loads(config.read_text())
        door = told.get("turnstile", {})
        voice_port = door.get("voice_port", 8445)
        ports = list(door.get("ports", [8442, 8443, 8444]))
        if voice_port not in ports:
            ports.append(voice_port)
            save_settings(config, set_toml_value(config.read_text(), "turnstile.ports", ports), "voice-install")
        voice_key = path_from(speech.parent, tomllib.loads(speech.read_text())["key_file"]) if "voice" in services else None
        voice_service_identity(config, key_file=voice_key)
        if "voice-gateway" in services:
            gateway = root / "settings/voice-gateway.toml"
            gateway_settings = tomllib.loads(gateway.read_text())
            service_identity(config, "voice-gateway", "system", path_from(gateway.parent, gateway_settings["key_file"]),
                             ("app-sign-in", "load", "front-voice"))
            service_identity(config, "voice-browser", "app", path_from(gateway.parent, gateway_settings["browser_key_file"]), ("front-voice",))
    if "voice" not in services:
        return 0
    settings = tomllib.loads(speech.read_text())
    key_file = Path(settings["key_file"])
    if not key_file.is_absolute():
        key_file = root / "settings" / key_file
    if not key_file.is_file() or not key_file.read_text().strip():
        raise ValueError("Voice needs the service key the front issues; join with a pairing code or pass --voice-key-file.")
    key_file.chmod(0o600)
    manifest = json.loads((root / "voice/models.json").read_text())
    names = {bundle["name"] for bundle in manifest["bundles"]}
    needed: dict[Path, set[str]] = {}
    for section, keys in (("recognition", ("encoder", "decoder", "joiner", "tokens")),
                          ("synthesis", ("model", "voices", "tokens", "data_dir"))):
        for key in keys:
            path = Path(settings[section][key])
            if not path.is_absolute():
                path = root / "settings" / path
            bundle = next((parent for parent in path.parents if parent.name in names), None)
            if bundle is not None:
                needed.setdefault(bundle.parent, set()).add(bundle.name)
            elif not path.exists():
                raise ValueError(f"The configured speech asset is missing: {path}")
    if install_models is None:
        spec = importlib.util.spec_from_file_location("install_voice_models", root / "scripts/voice-models.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        install_models = module.install
    for folder, bundles in needed.items():
        install_models(folder, bundles=sorted(bundles))
    return 0


def main() -> int:
    """Run installation discovery, compatibility audit or configuration planning."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    checking = subparsers.add_parser("audit")
    checking.add_argument("--root", type=Path, required=True)
    checking.add_argument("--services", required=True)
    checking.add_argument("--require-pg-tools", action="store_true")
    changing = subparsers.add_parser("configure")
    changing.add_argument("--config", type=Path, required=True)
    changing.add_argument("--service", choices=("loadcoach", "spotter", "toollocker", "weightroom"), required=True)
    changing.add_argument("--engine", choices=("sqlite", "postgresql"), required=True)
    changing.add_argument("--dsn", action="append", default=[])
    state = subparsers.add_parser("state")
    state.add_argument("--root", type=Path, required=True)
    state.add_argument("--services", required=True)
    ports = subparsers.add_parser("firewall-ports")
    ports.add_argument("--root", type=Path, required=True)
    ports.add_argument("--services", required=True)
    rendering = subparsers.add_parser("render")
    rendering.add_argument("--example", type=Path, required=True)
    rendering.add_argument("--target", type=Path, required=True)
    rendering.add_argument("--root", type=Path, required=True)
    rendering.add_argument("--home", type=Path, required=True)
    rendering.add_argument("--topology", choices=("all", "front", "compute", "keep"), default="keep")
    rendering.add_argument("--address", default="127.0.0.1")
    rendering.add_argument("--front", default="")
    rendering.add_argument("--compute", default="")
    rendering.add_argument("--llama-server", default="")
    rendering.add_argument("--machine-name", default="")
    rendering.add_argument("--voice", action="store_true")
    rendering.add_argument("--voice-model", default="")
    rendering.add_argument("--platform-services", default="images,speech,audio,downloads")
    voice = subparsers.add_parser("voice")
    voice.add_argument("--root", type=Path, required=True)
    voice.add_argument("--services", required=True)
    llama = subparsers.add_parser("llama-server")
    llama.add_argument("--root", type=Path, required=True)
    llama.add_argument("--executable", required=True)
    repairing = subparsers.add_parser("repair-postgresql")
    repairing.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "llama-server":
        executable = Path(args.executable)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            parser.error("llama-server must be an executable file")
        config = args.root / "settings/loadcoach.toml"
        text = config.read_text()
        if tomllib.loads(text).get("llama_server") != str(executable):
            save_settings(config, set_toml_value(text, "llama_server", str(executable)), "llama-install")
        return 0
    if args.command == "repair-postgresql":
        try:
            return repair_postgresql(args.root.resolve())
        except (ValueError, OSError, subprocess.TimeoutExpired) as problem:
            print(f"PostgreSQL recovery failed: {problem}", file=sys.stderr)
            return 2
    if args.command == "voice":
        return prepare_voice(args.root.resolve(), args.services.split(","))
    if args.command == "render":
        from loadcoach.platform_services import SERVICE_SETTINGS
        root = args.root.resolve()
        selected = set() if args.platform_services == 'none' else set(args.platform_services.split(','))
        if selected - set(SERVICE_SETTINGS.values()):
            raise SystemExit('Platform services must be images,speech,audio,downloads or none.')
        values = {} if args.topology == "keep" else topology_values(
            args.example.stem, args.topology, root, args.address, args.front, args.compute, args.llama_server,
            args.machine_name, args.voice, args.voice_model)
        if args.example.stem == 'weightroom':
            values.update({key: service in selected for key, service in SERVICE_SETTINGS.items()})
        return render(args.example, args.target, root, args.home, values)
    if args.command in {"audit", "state", "firewall-ports"}:
        services = [item for item in args.services.split(",") if item]
        unknown = set(services) - SERVICE_CONFIG.keys()
        if unknown:
            parser.error(f"unknown services: {', '.join(sorted(unknown))}")
        if args.command == "audit":
            return audit(args.root.resolve(), services, args.require_pg_tools)
        if args.command == "firewall-ports":
            try:
                for port in firewall_ports(args.root.resolve(), services):
                    print(port)
            except (OSError, ValueError, TypeError, AttributeError) as problem:
                print(f"Cannot read listener settings: {problem}", file=sys.stderr)
                return 2
            return 0
        engine, populated = database_state(args.root.resolve(), services)
        print(f"{engine} {'populated' if populated else 'empty'}")
        return 0
    return configure(args.config.resolve(), args.service, args.engine, args.dsn)


if __name__ == "__main__":
    raise SystemExit(main())
