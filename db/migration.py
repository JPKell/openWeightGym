"""Operator profiles, reviewable previews and detached offline migration coordination.

Profiles are local owner-only files, not browser-supplied commands or database paths.
All hosts must already have the same openWeightGym version installed. SSH uses the operator's
existing account and the worker only controls known openWeightGym user service units.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import Database, DatabaseRegistry, Store
from .maintenance import HEADER
from .transfer import MigrationError, artifacts, copy_store, preflight, reader

PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
SERVICES = ("weightroom", "turnstile", "voice-gateway", "loadcoach", "toollocker", "spotter", "voice")
CONFIGS = {name: name + ".toml" for name in SERVICES if name != "turnstile"}
SMOKES = {"sign-in", "models", "request", "chat", "benchmark", "tool"}


def digest(value):
    """Return the SHA-256 digest of the canonical input."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private_json(path):
    """Serialize migration state without exposing protected connection values."""
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise MigrationError("Migration profiles and acceptance files must be owner-only regular files")
    return json.loads(path.read_text())


def sync_directory(path):
    """Flush the containing directory after a durable file replacement."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, value):
    """Write private migration state using an atomic file replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6))
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(value, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def load_profile(folder, name):
    """Load and validate the named operator migration profile."""
    if not PROFILE.fullmatch(name):
        raise MigrationError("Invalid migration profile name")
    profile = private_json(Path(folder) / (name + ".json"))
    if not isinstance(profile, dict) or profile.get("schema") != 1 or not isinstance(profile.get("hosts"), dict) or not profile["hosts"]:
        raise MigrationError("Profile needs schema 1 and a complete host inventory")
    if profile.get("owner") not in profile["hosts"] or not isinstance(profile.get("binding"), str):
        raise MigrationError("Profile must select a source host and binding")
    for host, spec in profile["hosts"].items():
        if not isinstance(spec, dict) or not PROFILE.fullmatch(host) or not Path(spec.get("root", "")).is_absolute():
            raise MigrationError("Hosts need bounded names and absolute installation roots")
        if spec.get("ssh") and not re.fullmatch(r"[A-Za-z0-9_.@:-]+", spec["ssh"]):
            raise MigrationError("SSH host must be a configured account/host without options")
    if len({(spec.get("ssh", ""), spec["root"]) for spec in profile["hosts"].values()}) != len(profile["hosts"]):
        raise MigrationError("List each installation once; a combined host can own all openWeightGym services")
    if profile.get("target", {}).get("options"):
        raise MigrationError("Migration targets cannot override connection options")
    if profile.get("target", {}).get("engine") not in ("sqlite", "postgresql"):
        raise MigrationError("Target engine must be sqlite or postgresql")
    if profile.get("target", {}).get("engine") == "postgresql":
        space = profile.get("target_space", {})
        if space.get("host") not in profile["hosts"] or not Path(space.get("path", "")).is_absolute():
            raise MigrationError("PostgreSQL targets require a server filesystem free-space probe on a declared host")
    if not Path(profile.get("smoke_file", "")).is_absolute():
        raise MigrationError("Profile needs an absolute owner-only acceptance file path on the coordinator")
    smoke = private_json(profile["smoke_file"])
    if set(smoke) != SMOKES or any(not isinstance(checks, list) or not checks for checks in smoke.values()):
        raise MigrationError("Acceptance must exercise sign-in, models, request, chat, benchmark and tool")
    for checks in smoke.values():
        if not any(check.get("assert") or check.get("contains") for check in checks):
            raise MigrationError("Every acceptance category needs a content assertion")
        for check in checks:
            for key in ("body_file", "headers_file"):
                if check.get(key):
                    private_json(check[key])
    return profile


def profile_names(folder):
    """List available migration profiles from the configured directory."""
    return sorted(path.stem for path in Path(folder).glob("*.json") if PROFILE.fullmatch(path.stem))


def database(values, base=None):
    """Resolve one profile store into its configured database binding."""
    values = dict(values)
    name = values.pop("name", "migration_target")
    for key in ("path", "dsn_file"):
        if values.get(key) and base is not None:
            values[key] = str((Path(base) / values[key]).resolve())
    return Database(Store(name=name, **values))


def endpoint(target):
    """Build the private service endpoint described by the host profile."""
    if target.engine == "sqlite":
        if target.path is None:
            raise MigrationError("Memory and URI stores cannot be migrated")
        return digest(["sqlite", str(target.path.resolve())])
    with target.connection(connect_timeout=10) as connection:
        row = connection.execute("SELECT current_database(),inet_server_addr()::text,inet_server_port()").fetchone()
    return digest(["postgresql", *row])


def systemctl(*arguments, check=True):
    """Run the allowlisted service operation for the selected installation."""
    answer = subprocess.run(["systemctl", "--user", *arguments], capture_output=True, timeout=180)
    if check and answer.returncode:
        raise MigrationError("openWeightGym service operation failed; inspect the user service journal")
    return answer


def service_uses_installation(service, root):
    """Accept absolute entrypoint/config units even without WorkingDirectory."""
    directory = systemctl("show", "--property=WorkingDirectory", "--value", service + ".service").stdout.decode().strip()
    if directory and Path(directory).resolve() == root.resolve():
        return True
    try:
        pid = int(systemctl("show", "--property=MainPID", "--value", service + ".service").stdout)
        if pid <= 0:
            return False
        arguments = [part.decode() for part in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0") if part]
        executable = root / ".venv" / "bin" / service
        config = root / "settings" / CONFIGS.get(service, "weightroom.toml")
        return (str(executable) in arguments[:2] and "--config" in arguments
                and Path(arguments[arguments.index("--config") + 1]).resolve() == config.resolve())
    except (OSError, ValueError, IndexError, UnicodeError):
        return False


def host_inventory(root):
    """Resolve actual defaults and aliases without running application constructors."""
    from loadcoach.config import read_settings, kept_by_loadcoach
    root = Path(root)
    configs, references, stores, endpoints, files = {}, [], {}, {}, {}
    artifact_paths = set()
    def collect(value, base):
        """Collect one host's store, service and configuration inventory."""
        if isinstance(value, dict):
            for name, entry in value.items():
                if isinstance(entry, str) and entry and name not in {"token_file", "mirror_file"} and (name.endswith("_file") or name in {"encoder", "decoder", "joiner", "tokens", "voices", "lexicon", "data_dir"} or (name == "model" and Path(entry).suffix in {".gguf", ".onnx"})):
                    path = (base / entry).resolve()
                    if path.is_dir():
                        artifact_paths.update(str(file.resolve()) for file in path.rglob("*") if file.is_file())
                    else:
                        artifact_paths.add(str(path))
                elif isinstance(entry, (dict, list)):
                    collect(entry, base)
        elif isinstance(value, list):
            for entry in value:
                collect(entry, base)
    for service, filename in CONFIGS.items():
        path = root / "settings" / filename
        if not path.exists() and service in ("weightroom", "toollocker"):
            path = root / "settings" / {"weightroom": "frontdesk.toml", "toollocker": "tooldoor.toml"}[service]
        if not path.is_file():
            continue
        path = path.resolve()
        told = read_settings(path)
        collect(told, path.parent)
        if isinstance(told.get("keys"), str) and told["keys"]:
            artifact_paths.update(str(file.resolve()) for file in (path.parent / told["keys"]).rglob("*") if file.is_file())
        data = (path.parent / told.get("data", ".")).resolve()
        defaults = {}
        if service == "weightroom":
            defaults = {"weightroom." + kind: str(data / (kind + ".db"))
                        for kind in ("accounts", "roles", "chat", "benchmarks", "plugins", "alerts", "schedules")}
        elif service == "loadcoach":
            defaults = {"loadcoach.core": told.get("database") or str(data / (path.stem + ".db"))}
        elif service in ("spotter", "toollocker"):
            shared = kept_by_loadcoach(path.parent / told.get("loadcoach", "loadcoach.toml"))[1]
            defaults = {service + ".identity": shared}
            if service == "spotter":
                defaults["spotter.history"] = str(data / (path.stem + ".db"))
        registry = DatabaseRegistry.from_config(told.get("databases"), path.parent, defaults)
        configs[str(path)] = {"service": service, "data": str(data), "databases": told.get("databases", {}),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        files[str(path)] = configs[str(path)]["sha256"]
        defaults_file = path.parent / "defaults" / (service + ".toml")
        if defaults_file.is_file():
            files[str(defaults_file)] = hashlib.sha256(defaults_file.read_bytes()).hexdigest()
        for bound in registry.bound_stores():
            # Unbound configured destinations are not part of the active dataset, but all
            # bound stores and discovered databases are required in the recovery set.
            if not bound.bindings:
                continue
            target = dataclasses.asdict(bound.database.store)
            key = endpoint(bound.database)
            stores.setdefault(key, {"name": f"{service}.{bound.name}", "database": target, "bindings": []})
            stores[key].setdefault("name", f"{service}.{bound.name}")
            for binding in bound.bindings:
                stores[key]["bindings"].append(binding)
                references.append({"config": str(path), "binding": binding, "endpoint": key, "database": target})
        host = told.get("host", "127.0.0.1")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if service in ("weightroom", "loadcoach", "spotter", "toollocker"):
            ports = {"weightroom": 8440, "loadcoach": 8442, "spotter": 8444, "toollocker": 8443}
            endpoints[service] = f"http://{host}:{told.get('port', ports[service])}"
        # Legacy files beside the bound stores are included too, using normal discovery.
        for legacy in data.glob("*.db"):
            if legacy.is_file() and not legacy.is_symlink():
                target = Database.sqlite(legacy)
                stores.setdefault(endpoint(target), {"database": dataclasses.asdict(target.store), "bindings": []})
    active = [name for name in SERVICES if systemctl("is-active", "--quiet", name + ".service", check=False).returncode == 0]
    for name in active:
        if name != "turnstile" and not any(c["service"] == name for c in configs.values()):
            raise MigrationError("An active openWeightGym service has no discoverable configuration")
        if not service_uses_installation(name, root):
            raise MigrationError("An active openWeightGym unit belongs to a different installation root")
    artifact_paths.update(str(path.resolve()) for path in (root / "keys").rglob("*") if path.is_file())
    for path in (root / "plugins").glob("*/settings.json"):
        artifact_paths.add(str(path.resolve()))
        collect(json.loads(path.read_text()), path.parent)
    assets = []
    for name in sorted(artifact_paths):
        path = Path(name)
        if not path.is_file():
            raise MigrationError("A configured key, plugin or voice artifact is missing")
        stat = path.stat()
        assets.append({"path": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return {"configs": configs, "references": references, "stores": stores, "active": active,
            "endpoints": endpoints, "files": files, "artifacts": assets, "code": code_version()}


def code_version():
    # Require identical maintenance/copy/schema implementations on every host.
    """Return the current source revision for migration evidence."""
    root = Path(__file__).parent.parent
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in ("db/migration.py", "db/transfer.py", "db/maintenance.py", "db/schemas.py",
                                "db/drivers/postgresql.py", "db/config.py", "db/connection.py", "db/migrations.py",
                                "db/recovery.py", "db/postgresql_tools.py", "db/text_codec.py", "loadcoach/app.py", "loadcoach/log.py",
                                "weightroom/app.py", "toollocker/door.py", "spotter/app.py", "turnstile/app.py", "benchmarks/worker.py", "benchmarks/store.py")})


def config_text(original, section):
    """Replace database tables only; reject unsupported syntax instead of corrupting TOML."""
    result, skipping = [], False
    for line in original.splitlines(keepends=True):
        if line.strip() in ("# BEGIN SUITE INSTALLER DATABASES", "# END SUITE INSTALLER DATABASES"):
            continue
        header = re.match(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$", line)
        if header:
            name = header[1].strip()
            skipping = name == "databases" or name.startswith("databases.")
        if not skipping:
            result.append(line)
    block = []
    for name, values in sorted(section.get("stores", {}).items()):
        block.append(f"[databases.stores.{json.dumps(name)}]")
        for key, value in sorted(values.items()):
            def literal(value):
                """Render a supported setting value as a TOML literal."""
                if isinstance(value, dict):
                    return "{ " + ", ".join(json.dumps(k) + " = " + literal(v) for k, v in sorted(value.items())) + " }"
                return json.dumps(value)
            block.append(f"{key} = {literal(value)}")
        block.append("")
    block.append("[databases.bindings]")
    block.extend(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in sorted(section.get("bindings", {}).items()))
    generated = "\n".join(block) + "\n"
    text = "".join(result).rstrip() + "\n\n" + generated
    parsed = tomllib.loads(text)
    if parsed.get("databases") != section:
        raise MigrationError("Database configuration preview did not round-trip")
    before = tomllib.loads(original)
    if {k: v for k, v in before.items() if k != "databases"} != {k: v for k, v in parsed.items() if k != "databases"}:
        raise MigrationError("Preview would change unrelated configuration")
    return text, generated


def configuration_preview(inventory, source_endpoint, target):
    """Render proposed database bindings without changing live configuration."""
    changes = []
    for path, config in inventory["configs"].items():
        bindings = sorted(ref["binding"] for ref in inventory["references"] if ref["config"] == path and ref["endpoint"] == source_endpoint)
        if not bindings:
            continue
        section = json.loads(json.dumps(config["databases"]))
        section.setdefault("stores", {})
        section.setdefault("bindings", {})
        values = {k: v for k, v in target.items() if k != "name" and v not in ("", {})}
        name = "migrated_" + digest(values)[:12]
        if name in section["stores"] and section["stores"][name] != values:
            raise MigrationError("Generated target store name conflicts with existing configuration")
        section["stores"][name] = values
        section["bindings"].update({binding: name for binding in bindings})
        changes.append({"path": path, "before": config["sha256"], "bindings": bindings, "section": section})
    return changes


def rpc(host, operation, **payload):
    """Send one migration control request to the selected local or remote host."""
    root = Path(host["root"])
    command = [str(root / ".venv/bin/python"), "-m", "db.migration", "--rpc"]
    if host.get("ssh"):
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", host["ssh"],
                   "cd " + shlex.quote(str(root)) + " && " + shlex.join(command)]
    result = subprocess.run(command, input=json.dumps({"root": str(root), "operation": operation, **payload}).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=None if host.get("ssh") else root,
                            timeout=3600)
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        raise MigrationError(f"Host operation {operation} failed; check installation, SSH and native tools") from None
    if result.returncode or "error" in answer:
        raise MigrationError(answer.get("error", "Host operation failed"))
    return answer


def preview(profile, call=rpc):
    # Pin private acceptance before inventory so its new HMAC key cannot stale the preview.
    """Validate the proposed migration and assemble its operator preview."""
    smoke_hash = acceptance_fingerprint(profile["smoke_file"])
    inventories = {name: call(host, "inventory") for name, host in profile["hosts"].items()}
    if any(one["code"] != code_version() for one in inventories.values()):
        raise MigrationError("Install matching openWeightGym migration code on every host before continuing")
    owner = inventories[profile["owner"]]
    selected = [r for r in owner["references"] if r["binding"] == profile["binding"]]
    if len(selected) != 1:
        raise MigrationError("Source binding must resolve once on its owning host")
    source = selected[0]
    target = dataclasses.asdict(database(profile["target"], Path(source["config"]).parent).store)
    checked = call(profile["hosts"][profile["owner"]], "preflight", source=source["database"], target=target,
                   artifacts=profile.get("artifacts", []))
    if target["engine"] == "postgresql":
        space = profile["target_space"]
        available = call(profile["hosts"][space["host"]], "space", path=space["path"])
        if available["free_bytes"] < checked["required_bytes"]:
            raise MigrationError("Insufficient space on the PostgreSQL server filesystem")
        checked["server_space"] = available
    changes = {name: configuration_preview(one, source["endpoint"], target) for name, one in inventories.items()}
    if target["engine"] == "sqlite" and any(changes[name] for name in changes if name != profile["owner"]):
        raise MigrationError("Shared remote stores cannot cut over to SQLite on multiple hosts")
    # Every host must verify credentials/paths used by its future bindings; PostgreSQL
    # uses the same absolute secret-file reference, independently provisioned on each host.
    views = {name: call(profile["hosts"][name], "config-preview", changes=rows) for name, rows in changes.items() if rows}
    result = {"source": source, "target": target, "preflight": checked, "changes": changes, "views": views,
              "hosts": {name: {"active": one["active"], "files": one["files"], "artifacts": one["artifacts"]} for name, one in inventories.items()},
              "profile_hash": digest(profile), "smoke_hash": smoke_hash}
    # Free space and row counts change normally. Pin configuration, code and acceptance
    # intent, then repeat full preflight after stopping writers, against the final snapshot.
    result["approval"] = digest({k: result[k] for k in ("source", "target", "changes", "hosts", "profile_hash", "smoke_hash")})
    return result


def acceptance_fingerprint(filename):
    """Pin private fixtures without publishing an unsalted password-file digest."""
    path = Path(filename)
    key_file = path.parent / ".suite-migration-integrity.json"
    try:
        with open(os.open(key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
            json.dump({"key": secrets.token_hex(32)}, output)
    except FileExistsError:
        pass
    key = bytes.fromhex(private_json(key_file)["key"])
    mac = hmac.new(key, path.read_bytes(), hashlib.sha256)
    checks = private_json(path)
    for check in (check for rows in checks.values() for check in rows):
        for field in ("headers_file", "body_file"):
            if check.get(field):
                private_json(check[field])
                mac.update(check[field].encode())
                mac.update(Path(check[field]).read_bytes())
    return mac.hexdigest()


def _marker(root):
    """Return the installation's persistent maintenance marker path."""
    return Path(root) / "settings/.suite-maintenance.json"


def _job(root, job):
    """Resolve and validate a migration job's private state directory."""
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}", job):
        raise MigrationError("Invalid recovery job ID")
    return Path(root) / "data/migrations" / job


def _write_config(path, content):
    """Atomically replace the installation configuration with reviewed bytes."""
    path = Path(path)
    temp = path.with_name(path.name + ".migration-" + secrets.token_hex(6))
    with open(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, path.stat().st_mode & 0o777), "w") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)


def handle(payload):
    """Handle one host-side migration operation using the supplied profile."""
    root, operation = Path(payload["root"]), payload["operation"]
    if operation == "inventory":
        return host_inventory(root)
    if operation == "space":
        return {"path": payload["path"], "free_bytes": shutil.disk_usage(payload["path"]).free}
    if operation == "artifact-check":
        return {"artifacts": host_inventory(root)["artifacts"]}
    if operation == "store-artifacts":
        with reader(database(payload["database"])) as connection:
            return {"artifacts": artifacts(connection, payload.get("artifacts", []))}
    if operation == "lock-status":
        marker = _marker(root)
        return {"job": private_json(marker)["job"]} if marker.is_file() else {"job": None}
    if operation in ("preflight", "copy"):
        source, target = database(payload["source"]), database(payload["target"])
        return (preflight if operation == "preflight" else copy_store)(source, target, payload.get("artifacts", []))
    if operation == "config-preview":
        views = []
        for row in payload["changes"]:
            path = Path(row["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["before"]:
                raise MigrationError("Settings changed since inventory")
            _, block = config_text(path.read_text(), row["section"])
            registry = DatabaseRegistry.from_config(row["section"], path.parent)
            target = registry.database(row["bindings"][0])
            if target.engine == "postgresql":
                endpoint(target)
            views.append({"path": str(path), "bindings": row["bindings"], "toml": block})
        return {"configurations": views}
    job = payload["job"]
    work, marker = _job(root, job), _marker(root)
    if operation == "lock":
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Atomic create makes independently submitted jobs mutually exclusive per host.
        with open(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
            json.dump({"job": job, "token": payload["token"]}, output)
            output.flush()
            os.fsync(output.fileno())
        sync_directory(marker.parent)
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        return {"locked": True}
    if not marker.is_file() or private_json(marker).get("job") != job:
        raise MigrationError("This job does not hold the host maintenance lock")
    if operation == "idle":
        import httpx
        inventory = host_inventory(root)
        busy = 0
        for name in inventory["active"]:
            if name not in inventory["endpoints"]:
                continue
            with httpx.Client(timeout=10) as client:
                response = client.get(inventory["endpoints"][name] + "/__suite_maintenance", headers={HEADER: private_json(marker)["token"]})
                if response.status_code != 200:
                    raise MigrationError("A service does not support the maintenance barrier; update it before migration")
                busy += response.json()["active"]
        return {"active": busy}
    if operation in ("stop", "start"):
        units = payload["services"]
        if any(unit not in SERVICES for unit in units):
            raise MigrationError("Only known openWeightGym user services may be controlled")
        if units:
            systemctl(operation, *(unit + ".service" for unit in units))
        for unit in units:
            active = systemctl("is-active", "--quiet", unit + ".service", check=False).returncode == 0
            if active != (operation == "start"):
                raise MigrationError("openWeightGym service did not reach the required state")
        return {"services": units, "state": operation}
    if operation == "backup":
        from loadcoach.backups import backup_root, compute_plan, front_plan, create_backup
        from .recovery import RecoveryStore, inspect_store
        inventory = host_inventory(root)
        stores = [RecoveryStore(row.get("name", key[:24]), database(row["database"]), tuple(sorted(set(row["bindings"]))))
                  for key, row in inventory["stores"].items()]
        paths = [store.database.path for store in stores if store.database.path]
        files, excluded = [], []
        for path, config in inventory["configs"].items():
            if config["service"] == "weightroom":
                planned, omitted = front_plan(config["data"], path, str(root / "plugins"), paths)
                files.extend(planned)
                excluded.extend(omitted)
            elif config["service"] == "loadcoach":
                source = next(store.database for store in stores if "loadcoach.core" in store.bindings)
                planned, omitted = compute_plan(source, path, config["data"], paths)
                files.extend(planned)
                excluded.extend(omitted)
        if not stores:
            raise MigrationError("No configured stores were found on a required host")
        files = list({str(file.source): file for file in files}.values())
        recovery_data = next((Path(config["data"]) for config in inventory["configs"].values() if config["service"] == "weightroom"),
                             next((Path(config["data"]) for config in inventory["configs"].values() if config["service"] == "loadcoach"), root / "data"))
        made = create_backup(backup_root(recovery_data), job, payload["machine"], files,
                             [inspect_store(store) for store in stores], excluded,
                             [name + ".service" for name in payload["services"]], 0, stores=stores)
        return {"id": made["id"], "verification": made["verification"], "stores": made["stores"], "artifacts": inventory["artifacts"]}
    if operation == "switch":
        changes = payload["changes"]
        staged = []
        for index, row in enumerate(changes):
            path = Path(row["path"])
            original = path.read_text()
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["before"]:
                raise MigrationError("Configuration changed after approval; refusing cutover")
            text, _ = config_text(original, row["section"])
            staged.append({"path": str(path), "original": original, "replacement": text,
                           "before": row["before"], "after": hashlib.sha256(text.encode()).hexdigest()})
        write_json(work / "configuration.json", staged)
        for row in staged:
            _write_config(row["path"], row["replacement"])
        return {"switched": [row["path"] for row in staged]}
    if operation == "restore":
        snapshot = work / "configuration.json"
        if snapshot.exists():
            rows = private_json(snapshot)
            for row in rows:
                current = hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest()
                if current not in (row["before"], row["after"]):
                    raise MigrationError("Settings were externally edited; automatic rollback cannot overwrite them")
            for row in rows:
                _write_config(row["path"], row["original"])
        return {"restored": True}
    if operation == "verify-config":
        snapshot = work / "configuration.json"
        if snapshot.exists():
            for row in private_json(snapshot):
                if hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest() != row["after"]:
                    raise MigrationError("Configuration changed during target acceptance")
        return {"verified": True}
    if operation == "unlock":
        marker.unlink()
        sync_directory(marker.parent)
        return {"unlocked": True}
    raise MigrationError("Unknown host operation")


def smoke_checks(filename, token, timeout=180):
    """Execute operator-selected functional requests without logging responses or secrets."""
    import httpx
    definitions = private_json(filename)
    if set(definitions) != SMOKES:
        raise MigrationError("All six acceptance categories are required")
    variables, results = {}, {}
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for name in ("sign-in", "models", "request", "chat", "benchmark", "tool"):
            for check in definitions[name]:
                def replace(value):
                    """Substitute isolated acceptance-store addresses in smoke-check configuration."""
                    if isinstance(value, str):
                        for key, saved in variables.items():
                            value = value.replace("${" + key + "}", str(saved))
                    elif isinstance(value, dict):
                        value = {key: replace(saved) for key, saved in value.items()}
                    elif isinstance(value, list):
                        value = [replace(saved) for saved in value]
                    return value
                request = replace(check)
                if request.get("body_file"):
                    request["json"] = replace(private_json(request["body_file"]))
                private_headers = replace(private_json(request["headers_file"])) if request.get("headers_file") else {}
                headers = {**private_headers, **request.get("headers", {}), HEADER: token}
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        response = client.request(request.get("method", "GET"), request["url"], headers=headers, json=request.get("json"))
                        if response.status_code != request.get("status", 200):
                            raise MigrationError("Acceptance request returned an unexpected status")
                        data = response.json() if request.get("assert") or request.get("extract") else None
                        def field(path):
                            """Extract a required acceptance field from the smoke-check result."""
                            value = data
                            for part in path.split(".") if path else []:
                                value = value[int(part)] if isinstance(value, list) else value[part]
                            return value
                        for path, expected in request.get("assert", {}).items():
                            if field(path) != expected:
                                raise MigrationError("Acceptance response did not match expected content")
                        for text in request.get("contains", []):
                            if text not in response.text:
                                raise MigrationError("Acceptance response did not contain expected evidence")
                        for variable, path in request.get("extract", {}).items():
                            variables[variable] = field(path)
                        break
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, MigrationError):
                        if time.monotonic() >= deadline:
                            raise MigrationError(f"Acceptance check failed: {name}") from None
                        time.sleep(1)
            results[name] = "passed"
    return results


def run_job(profile, approved, job, report_path, call=rpc, smoke=smoke_checks, token=None):
    """Coordinate backup, drain, transfer, verification, switch and rollback."""
    report = {"id": job, "state": "preflight", "steps": [], "recovery": {}}
    locked, stopped, switched = [], [], []
    token = token or secrets.token_hex(32)
    plan = None
    def record(state, **extra):
        """Persist a migration stage result in the job's durable status."""
        report.update(state=state, **extra)
        report["steps"].append({"state": state, "at": time.time()})
        write_json(report_path, report)
    try:
        plan = preview(profile, call)
        if plan["approval"] != approved:
            raise MigrationError("Configuration, services or acceptance changed after preview; review again")
        record("locking", bindings={name: [binding for row in rows for binding in row["bindings"]] for name, rows in plan["changes"].items()})
        report["plan"] = plan
        write_json(report_path, report)
        for name, host in profile["hosts"].items():
            call(host, "lock", job=job, token=token)
            locked.append(name)
        record("draining")
        deadline = time.monotonic() + min(max(profile.get("drain_seconds", 600), 1), 3600)
        while True:
            busy = sum(call(profile["hosts"][name], "idle", job=job)["active"] for name in locked)
            if not busy:
                break
            if time.monotonic() >= deadline:
                raise MigrationError("Writer drain timed out; no store was switched")
            time.sleep(.5)
        record("stopping")
        for name in locked:
            # Register BEFORE calling: partial systemctl failures still require recovery.
            stopped.append(name)
            call(profile["hosts"][name], "stop", job=job, services=plan["hosts"][name]["active"])
        record("backup")
        for name in locked:
            recovery = call(profile["hosts"][name], "backup", job=job, machine=name, services=plan["hosts"][name]["active"])
            if recovery.get("verification", {}).get("state") != "verified":
                raise MigrationError("Every recovery half must be verified")
            report["recovery"][name] = recovery
            write_json(report_path, report)
        record("copying")
        if plan["target"]["engine"] == "postgresql":
            space = profile["target_space"]
            if call(profile["hosts"][space["host"]], "space", path=space["path"])["free_bytes"] < plan["preflight"]["required_bytes"]:
                raise MigrationError("PostgreSQL server space fell below the migration reserve")
        copied = call(profile["hosts"][profile["owner"]], "copy", source=plan["source"]["database"],
                      target=plan["target"], artifacts=profile.get("artifacts", []))
        if copied.get("state") != "verified":
            raise MigrationError("Target did not pass independent verification")
        record("cutover", validation=copied)
        for name in locked:
            switched.append(name)
            call(profile["hosts"][name], "switch", job=job, changes=plan["changes"][name])
        record("acceptance")
        for name in reversed(stopped):
            call(profile["hosts"][name], "start", job=job, services=plan["hosts"][name]["active"])
        report["acceptance"] = smoke(profile["smoke_file"], token)
        if set(report["acceptance"]) != SMOKES or set(report["acceptance"].values()) != {"passed"}:
            raise MigrationError("Acceptance is incomplete")
        for name in locked:
            call(profile["hosts"][name], "verify-config", job=job)
            current = call(profile["hosts"][name], "artifact-check")
            if current.get("artifacts", []) != report["recovery"][name].get("artifacts", []):
                raise MigrationError("A required host artifact changed during acceptance")
        current = call(profile["hosts"][profile["owner"]], "store-artifacts", database=plan["target"], artifacts=profile.get("artifacts", []))
        if current.get("artifacts", []) != copied.get("artifacts", []):
            raise MigrationError("A referenced store artifact changed during acceptance")
        record("accepted")
    except BaseException as problem:
        # Keep diagnostics categorical. Driver errors, SQL parameters, HTTP bodies,
        # credentials and the private acceptance file never enter a browser report.
        report["failure"] = str(problem) if isinstance(problem, MigrationError) else type(problem).__name__
        record("rolling-back")
        try:
            for name in stopped:
                call(profile["hosts"][name], "stop", job=job, services=plan["hosts"][name]["active"])
            for name in reversed(switched):
                call(profile["hosts"][name], "restore", job=job)
            for name in reversed(stopped):
                call(profile["hosts"][name], "start", job=job, services=plan["hosts"][name]["active"])
            if stopped:
                report["rollback_acceptance"] = smoke(profile["smoke_file"], token)
                if set(report["rollback_acceptance"]) != SMOKES or set(report["rollback_acceptance"].values()) != {"passed"}:
                    raise MigrationError("Rollback acceptance is incomplete")
            record("rolled-back")
        except BaseException:
            # Never reopen traffic after an incomplete rollback. Stop any partially
            # restarted services, retain host markers and durable configuration snapshots.
            for name in stopped:
                try:
                    call(profile["hosts"][name], "stop", job=job, services=plan["hosts"][name]["active"])
                except BaseException:
                    pass
            record("rollback-failed", operator_action="Keep maintenance closed; repair services/settings using retained configuration snapshots and verified recovery sets.")
            return report
    for name in reversed(locked):
        try:
            call(profile["hosts"][name], "unlock", job=job)
        except BaseException:
            record("unlock-failed", operator_action="Acceptance completed, but a host maintenance marker could not be removed. Inspect its job ID before releasing it.")
            return report
    record("complete" if report["state"] == "accepted" else "rolled-back")
    return report


def launch(folder, name, approval, reports, launcher=subprocess.run):
    """Start the selected migration job as an independent local process."""
    from loadcoach.backups import new_id
    profile = load_profile(folder, name)
    checked = preview(profile)
    if not secrets.compare_digest(checked["approval"], approval):
        raise MigrationError("Preview is stale; review it again")
    reports = Path(reports)
    reports.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A queued lease prevents double-clicks from launching concurrent detached workers.
    lease = reports / "pending.json"
    job = new_id()
    with open(os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
        json.dump({"id": job}, output)
    job_file = reports / (job + ".job.json")
    write_json(job_file, {"profile": profile, "approval": approval, "id": job, "report": str(reports / (job + ".json")), "lease": str(lease)})
    write_json(reports / (job + ".json"), {"id": job, "state": "queued", "steps": [], "recovery": {}})
    try:
        result = launcher(["systemd-run", "--user", "--collect", "--unit=suite-db-migration-" + job,
                           "--property=UMask=0077", "--property=WorkingDirectory=" + str(Path(__file__).resolve().parent.parent),
                           sys.executable, "-m", "db.migration", "--job", str(job_file)],
                          capture_output=True, timeout=15)
        if result.returncode:
            raise MigrationError("Detached migration worker could not be started")
    except BaseException:
        lease.unlink(missing_ok=True)
        job_file.unlink(missing_ok=True)
        write_json(reports / (job + ".json"), {"id": job, "state": "start-failed", "steps": [], "recovery": {}})
        raise
    return job


def reports(folder):
    """List retained migration status reports for the operator."""
    result = []
    for path in sorted(Path(folder).glob("*.json"), reverse=True):
        if path.name == "pending.json" or path.name.endswith(".job.json"):
            continue
        try:
            result.append(private_json(path))
        except (OSError, ValueError, MigrationError):
            continue
    return result[:20]


def recover_job(job, call=rpc, smoke=smoke_checks):
    """Recover a detached worker interrupted by process or machine failure."""
    if not Path(job["report"]).exists():
        # Host locks are acquired only after a durable report and plan are written.
        Path(job["lease"]).unlink(missing_ok=True)
        return {"state": "not-started"}
    report = private_json(job["report"])
    profile, plan = job["profile"], report.get("plan")
    if not plan:
        Path(job["lease"]).unlink(missing_ok=True)
        return report
    locked = []
    for name, host in profile["hosts"].items():
        status = call(host, "lock-status")
        if status["job"] == job["id"]:
            locked.append(name)
        elif status["job"] is not None:
            raise MigrationError("Another job owns a host lock; interrupted-job recovery cannot continue")
    if report["state"] in ("accepted", "complete", "rolled-back", "unlock-failed"):
        # Some traffic may already have reopened. Never roll back an accepted installation
        # after that point, which could discard new production writes.
        if report["state"] == "unlock-failed" and not (report.get("acceptance") or report.get("rollback_acceptance")):
            raise MigrationError("Cannot release an unverified migration")
    else:
        if not locked and report["state"] != "locking":
            raise MigrationError("Interrupted migration has lost its maintenance locks; operator investigation is required")
        try:
            for name in locked:
                call(profile["hosts"][name], "stop", job=job["id"], services=plan["hosts"][name]["active"])
            for name in locked:
                call(profile["hosts"][name], "restore", job=job["id"])
            for name in reversed(locked):
                call(profile["hosts"][name], "start", job=job["id"], services=plan["hosts"][name]["active"])
            if locked:
                acceptance = smoke(profile["smoke_file"], job["token"])
                if set(acceptance) != SMOKES or set(acceptance.values()) != {"passed"}:
                    raise MigrationError("Interrupted-job rollback acceptance is incomplete")
                report["rollback_acceptance"] = acceptance
            report["state"] = "rolled-back"
        except BaseException:
            for name in locked:
                try:
                    call(profile["hosts"][name], "stop", job=job["id"], services=plan["hosts"][name]["active"])
                except BaseException:
                    pass
            report["state"] = "rollback-failed"
            write_json(job["report"], report)
            raise MigrationError("Interrupted-job rollback failed; maintenance remains closed") from None
    for name in reversed(locked):
        call(profile["hosts"][name], "unlock", job=job["id"])
    if report["state"] in ("accepted", "unlock-failed"):
        report["state"] = "rolled-back" if report.get("rollback_acceptance") else "complete"
    write_json(job["report"], report)
    Path(job["lease"]).unlink(missing_ok=True)
    return report


def main():
    """Run the host-side migration command or migration coordinator."""
    if sys.argv[1:] == ["--rpc"]:
        try:
            result = handle(json.load(sys.stdin))
            print(json.dumps(result))
        except BaseException as problem:
            message = str(problem) if isinstance(problem, MigrationError) else type(problem).__name__ + ": host operation failed"
            print(json.dumps({"error": message}))
            return 1
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        job = private_json(sys.argv[2])
        job["token"] = secrets.token_hex(32)
        write_json(sys.argv[2], job)
        result = run_job(job["profile"], job["approval"], job["id"], job["report"], token=job["token"])
        if result["state"] in ("complete", "rolled-back"):
            Path(job["lease"]).unlink(missing_ok=True)
        return 0 if result["state"] == "complete" else 1
    if len(sys.argv) == 3 and sys.argv[1] == "--recover":
        job = private_json(sys.argv[2])
        if systemctl("is-active", "--quiet", "suite-db-migration-" + job["id"] + ".service", check=False).returncode == 0:
            raise MigrationError("Stop the detached worker before recovering its interrupted job")
        result = recover_job(job)
        print(json.dumps({"id": job["id"], "state": result["state"]}))
        return 0
    print("Use the System DB migration wizard with an owner-only operator profile.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
