"""Encrypted, manifest-backed recovery archives for the two openWeightGym machines.

Backups deliberately stay on the machine that made them.  Database snapshots are made with
SQLite's online backup API, every included file is hashed, and the finished archive is restored
through age into a private temporary directory and checked before it is called verified. Raw keys, environment
files and runtime tokens are not inputs to this module.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from db import DatabaseSource, connect, local_path
from db.recovery import RecoveryStore, archive_name, backup_store, inspect_store, sqlite_metadata
from .backup_crypto import ARCHIVE, decrypt, executable, private_directory, read_regular, recipients, safe_path, temporary_identity
from typing import Iterable


BACKUP_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-(?:[a-f0-9]{6}|[a-f0-9]{24})")
SECRET_FIELD = re.compile(r"(^|_)(key|token|secret|password)$", re.IGNORECASE)
DATABASE_COUNTS = {
    "loadcoach.db": ("models", "events"),
    "accounts.db": ("users", "events"),
    "chat.db": ("conversations", "messages"),
    "benchmarks.db": ("benchmark_runs", "benchmark_samples"),
    "spotter.db": ("readings",),
}


@dataclass(frozen=True)
class BackupFile:
    """One file in an archive, under a stable relative name."""

    source: Path
    archive: str
    kind: str
    database: bool = False
    contains_protected_identity: bool = False


def new_id(now: float | None = None, random: bytes | None = None) -> str:
    """A sortable recovery-set ID.  The random suffix permits two sets in one second."""
    stamp = datetime.fromtimestamp(time.time() if now is None else now, UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{(random or os.urandom(3)).hex()}"


def backup_root(data: str | Path) -> Path:
    """Return the recovery-set directory beneath the installation's data root."""
    return Path(data) / "backups"


def _sha256(path: Path) -> str:
    """Stream the file into a SHA-256 checksum."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(source: Path, target: Path, database: bool) -> None:
    """Copy a file or take an online SQLite snapshot into a private recovery payload."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if database:
        original = connect(f"file:{source.resolve()}?mode=ro", uri=True)
        copied = connect(target)
        try:
            original.backup(copied)
            copied.execute("PRAGMA journal_mode=DELETE")
        finally:
            copied.close()
            original.close()
    else:
        shutil.copy2(source, target)
    target.chmod(0o600)


def _inline_secret(path: Path) -> str | None:
    """Name the first inline secret field in a TOML/JSON settings file, if one exists."""
    try:
        value = tomllib.loads(path.read_text()) if path.suffix == ".toml" else json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None

    def visit(one, prefix: str = "") -> str | None:
        """Recursively find inline secret fields in a settings document."""
        if isinstance(one, dict):
            for name, nested in one.items():
                dotted = f"{prefix}.{name}" if prefix else str(name)
                if SECRET_FIELD.search(str(name)) and nested not in (None, ""):
                    return dotted
                if found := visit(nested, dotted):
                    return found
        elif isinstance(one, list):
            for number, nested in enumerate(one):
                if found := visit(nested, f"{prefix}[{number}]"):
                    return found
        return None

    return visit(value)


def _database_counts(path: Path) -> dict[str, int]:
    """Verify SQLite integrity and count the relevant retained tables."""
    db = connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"{path.name}: SQLite integrity check did not pass")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        wanted = DATABASE_COUNTS.get(path.name, ())
        return {table: int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in wanted if table in tables}
    finally:
        db.close()


def _check_payload(folder: Path, manifest: dict) -> dict[str, int]:
    """Verify restored file hashes and store metadata against the recovery manifest."""
    _validate_manifest(manifest)
    expected_paths = {entry["archive"] for entry in manifest["files"]}
    if manifest['schema'] == 2:
        # Historical archives could contain untracked journals created by read-only
        # SQLite verification. Never replay those journals over the hashed snapshot.
        for entry in manifest['files']:
            if entry['database'] and entry.get('engine', 'sqlite') == 'sqlite':
                for suffix in ('-wal', '-shm'):
                    companion = entry['archive'] + suffix
                    path = folder / companion
                    if companion not in expected_paths and path.is_file():
                        path.unlink()
    actual_paths = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}
    if actual_paths != expected_paths:
        raise ValueError("The recovery payload does not match its manifest file inventory.")
    counts: dict[str, int] = {}
    for entry in manifest["files"]:
        restored = folder / entry["archive"]
        if not restored.is_file():
            raise ValueError(f"{entry['archive']} is missing from the restored archive")
        if restored.stat().st_size != entry["size"] or _sha256(restored) != entry["sha256"]:
            raise ValueError(f"{entry['archive']} does not match its manifest")
        if entry["database"] and entry.get("engine", "sqlite") == "sqlite":
            counts.update(_database_counts(restored))
    for store in manifest.get("stores", []):
        restored = folder / store["archive"]
        expected = store.get("verification", {})
        if store["engine"] == "sqlite":
            checked = sqlite_metadata(restored)
            if checked["counts"] != expected.get("counts", {}) or checked["schema_versions"] != expected.get("schema_versions", {}):
                raise ValueError(f"{store['name']} does not match its verified store manifest")
            counts.update(checked["counts"])
        else:
            counts.update(expected.get("counts", {}))
    return counts


def _write_json(path: Path, value: dict) -> None:
    """Write an owner-readable JSON recovery manifest."""
    with path.open("x") as out:
        path.chmod(0o600)
        out.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def _archive_path(name: str) -> str:
    """Accept canonical, bounded relative paths only, without traversal or ambiguous aliases."""
    if (not isinstance(name, str) or len(name) > 4096 or "\\" in name or "\x00" in name
            or any(part in ("", ".", "..") for part in name.split("/")) or name.startswith("/")):
        raise ValueError("The recovery archive contains an unsafe path.")
    return name


def _validate_manifest(manifest: dict) -> None:
    """Validate all paths before using decrypted metadata to open files."""
    if (not isinstance(manifest, dict) or manifest.get("schema") not in (2, 3)
            or not isinstance(manifest.get("files"), list) or not manifest["files"]
            or not isinstance(manifest.get("stores", []), list)
            or not isinstance(manifest.get("machine"), str)
            or not isinstance(manifest.get("verification"), dict)
            or not BACKUP_ID.fullmatch(str(manifest.get("id", "")))):
        raise ValueError("Invalid recovery manifest.")
    if manifest["schema"] == 3 and (not isinstance(manifest.get("protection"), dict)
            or manifest["protection"].get("format") != "age" or manifest["protection"].get("encrypted") is not True):
        raise ValueError("Invalid recovery encryption metadata.")
    names = set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid recovery file metadata.")
        name = _archive_path(item["archive"])
        if (name in names or type(item.get("size")) is not int or item["size"] < 0
                or type(item.get("database")) is not bool
                or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))):
            raise ValueError("Invalid or duplicate recovery file metadata.")
        names.add(name)
    for item in manifest.get("stores", []):
        if _archive_path(item["archive"]) not in names:
            raise ValueError("A recovery store is missing from the file inventory.")


def _safe_extract(archive: Path, target: Path, max_bytes: int = 1024**4) -> None:
    """Extract regular files privately, refusing links, duplicates, special files and bombs."""
    seen, total = set(), 0
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = _archive_path(member.name)
            if (name in seen or len(seen) >= 1_000_000 or member.size < 0
                    or not (member.isfile() or member.isdir()) or member.sparse is not None
                    or name != "manifest.json" and name != "payload" and not name.startswith("payload/")):
                raise ValueError("The recovery archive contains an unsafe path or file type")
            seen.add(name)
            total += member.size
            if total > max_bytes or name == "manifest.json" and member.size > 16 * 1024**2:
                raise ValueError("The recovery archive exceeds the extraction size limit.")
            destination = target / name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with bundle.extractfile(member) as source, destination.open("xb") as out:
                    destination.chmod(0o600)
                    shutil.copyfileobj(source, out)


def create_backup(
    root: Path,
    backup_id: str,
    machine: str,
    files: Iterable[BackupFile],
    inventory: list[dict],
    excluded: list[dict],
    services: list[str],
    retention: int,
    models: list[dict] | None = None,
    now: float | None = None,
    stores: Iterable[RecoveryStore] = (),
) -> dict:
    """Serialize local creation/rotation; refuse backups without offline recovery recipients."""
    if not BACKUP_ID.fullmatch(backup_id):
        raise ValueError("The recovery-set ID is invalid.")
    if type(retention) is not int or not 0 <= retention <= 100:
        raise ValueError("Retention must be a whole number from 0 through 100.")
    root = private_directory(root)
    configured = recipients(root)
    lock = safe_path(root / ".backup.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another backup is running on this machine; retry after it finishes.") from None
        return _create_backup(root, backup_id, machine, files, inventory, excluded, services,
                              retention, models, now, stores, configured)
    finally:
        os.close(descriptor)


def _create_backup(root, backup_id, machine, files, inventory, excluded, services,
                   retention, models, now, stores, configured) -> dict:
    """Snapshot, encrypt, authenticate and restore-check before atomic publication and rotation."""
    stores = list(stores)
    store_paths = {item.database.path.resolve() for item in stores
                   if item.database.engine == "sqlite" and item.database.path is not None}
    files = [item for item in files if not item.database or item.source.resolve() not in store_paths]
    names = [_archive_path(item.archive) for item in files]
    names.extend(_archive_path(archive_name(store)) for store in stores)
    if len(names) != len(set(names)) or any(str(parent) in names for name in names for parent in Path(name).parents):
        raise ValueError("Backup inputs have duplicate or overlapping archive paths.")
    target = root / backup_id
    if target.exists():
        raise FileExistsError(f"Recovery set {backup_id} already exists on {machine}.")
    created_at = datetime.fromtimestamp(time.time() if now is None else now, UTC).isoformat()
    with tempfile.TemporaryDirectory(prefix=f".{backup_id}-", dir=root) as temporary:
        work = Path(temporary)
        payload = work / "payload"
        payload.mkdir()
        records: list[dict] = []
        for item in files:
            safe_path(item.source)
            if not item.source.is_file():
                continue
            if item.kind in ("settings", "plugin settings", "site-managed settings") and (field := _inline_secret(item.source)):
                raise ValueError(f"Refusing to archive inline secret field {field} in {item.source}; move it to an owner-only key file.")
            destination = payload / item.archive
            _snapshot(item.source, destination, item.database)
            records.append(
                {
                    "archive": item.archive,
                    "source": str(item.source),
                    "kind": item.kind,
                    "database": item.database,
                    "engine": "sqlite" if item.database else "",
                    "contains_protected_identity": item.contains_protected_identity,
                    "size": destination.stat().st_size,
                    "sha256": _sha256(destination),
                }
            )
        for store in stores:
            if store.database.path is not None:
                safe_path(store.database.path)
        store_records = [backup_store(store, payload) for store in stores]
        records.extend(
            {
                "archive": item["archive"],
                "source": item["display_name"],
                "kind": f"{item['name']} logical database store",
                "database": True,
                "engine": item["engine"],
                "contains_protected_identity": any("identity" in binding or binding in ("loadcoach.core", "weightroom.accounts") for binding in item["bindings"]),
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in store_records
        )
        if not records:
            raise ValueError(f"No recoverable files were found on {machine}.")
        manifest = {
            "schema": 3,
            "id": backup_id,
            "machine": machine,
            "created_at": created_at,
            "files": records,
            "inventory": inventory,
            "excluded": excluded,
            "models": models or [],
            "stores": store_records,
            "verification": {"state": "pending"},
            "services_to_stop": services,
            "protection": {"format": "age", "encrypted": True,
                           "recipients_sha256": hashlib.sha256("\n".join(configured).encode()).hexdigest(),
                           "recovery_key_tested": False},
            "secret_policy": {
                "archive_access": "age encrypted and owner-only; private recovery keys are held offline",
                "included": "LoadCoach database credential verifiers, authenticator secrets and job-specific benchmark/schedule tokens needed to recover durable work",
                "excluded": "raw API keys, provider keys, key folders, environment files, CLI tokens and WeightRoom cookie-signing secrets",
                "recovery": "restore raw keys separately from their private source after restoring the openWeightGym data",
            },
        }
        counts = _check_payload(payload, manifest)
        for item in store_records:
            counts.update(item["verification"].get("counts", {}))
        manifest["verification"] = {"state": "verified", "verified_at": created_at, "counts": counts,
                                    "scope": "local ciphertext round trip; offline recovery key drill required"}
        _write_json(work / "manifest.json", manifest)
        identity, temporary_recipient = temporary_identity()
        archive = work / ARCHIVE
        # Stream compressed bytes directly into age; no plaintext tar archive is retained.
        with archive.open("xb") as encrypted:
            archive.chmod(0o600)
            process = subprocess.Popen([executable("age"), *(arg for value in [*configured, temporary_recipient]
                                        for arg in ("-r", value))], stdin=subprocess.PIPE,
                                       stdout=encrypted, stderr=subprocess.DEVNULL)
            try:
                with process.stdin as stream, tarfile.open(fileobj=stream, mode="w|gz") as bundle:
                    bundle.add(work / "manifest.json", arcname="manifest.json")
                    bundle.add(payload, arcname="payload")
                if process.wait(timeout=3600):
                    raise ValueError("Backup encryption failed; no recovery set was published.")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            encrypted.flush()
            os.fsync(encrypted.fileno())

        with tempfile.TemporaryDirectory(prefix="restore-check-", dir=work) as restored:
            restored_path = Path(restored)
            plaintext = restored_path / "check.tar.gz"
            decrypt(archive, identity, plaintext)
            _safe_extract(plaintext, restored_path)
            embedded = json.loads((restored_path / "manifest.json").read_text())
            isolated_counts = _check_payload(restored_path / "payload", embedded)
            if isolated_counts != counts:
                raise ValueError("The isolated restore check did not reproduce the manifest counts.")
        del identity
        manifest["archive_sha256"] = _sha256(archive)

        final = root / ("." + backup_id + "-ready")
        if final.exists():
            raise FileExistsError("A backup publication directory already exists; inspect it before retrying.")
        final.mkdir(mode=0o700)
        shutil.move(str(archive), final / ARCHIVE)
        _write_json(final / "manifest.json", manifest)
        descriptor = os.open(final, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        final.rename(target)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    rotate(root, retention)
    return manifest


def rotate(root: Path, retention: int) -> list[str]:
    """Keep the newest ``retention`` sets. Zero means keep every set."""
    if retention == 0:
        return []
    def created(path):
        # A scheduled ID identifies its due time; an outage can delay creation.
        # Retention must use when the recovery set was actually made.
        """Read actual recovery creation time for retention ordering."""
        try:
            value = json.loads((path / 'manifest.json').read_text())['created_at']
            return datetime.fromisoformat(value).timestamp(), path.name
        except (OSError, ValueError, KeyError, TypeError):
            return datetime.strptime(path.name.split('-')[0], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC).timestamp(), path.name
    # Legacy plaintext sets are preserved for explicit migration/removal by the owner.
    existing = sorted((path for path in root.iterdir() if not path.is_symlink() and path.is_dir()
                       and BACKUP_ID.fullmatch(path.name) and (path / ARCHIVE).is_file()), key=created, reverse=True)
    removed: list[str] = []
    for path in existing[retention:]:
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def manifests(root: Path) -> list[dict]:
    """Read valid sidecar manifests, newest first; a damaged entry is reported honestly."""
    root = safe_path(root)
    if not root.is_dir():
        return []
    found: list[dict] = []
    for folder in sorted((path for path in root.iterdir() if not path.is_symlink() and path.is_dir() and BACKUP_ID.fullmatch(path.name)), reverse=True):
        try:
            manifest = json.loads(read_regular(folder / "manifest.json", 16 * 1024**2))
            _validate_manifest(manifest)
            if manifest["id"] != folder.name:
                raise ValueError("Recovery manifest identity does not match its directory.")
            encrypted = (folder / ARCHIVE).is_file() and not (folder / ARCHIVE).is_symlink()
            archive = folder / (ARCHIVE if encrypted else "archive.tar.gz")
            safe_path(archive)
            manifest["archive_size"] = archive.stat().st_size
            if not encrypted or not manifest.get("protection", {}).get("encrypted"):
                manifest["protection"] = {"encrypted": False, "format": "legacy plaintext"}
                manifest["verification"] = {**manifest.get("verification", {}), "state": "legacy-unencrypted"}
            manifest["payload_size"] = sum(int(item.get("size", 0)) for item in manifest.get("files", []))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            manifest = {"id": folder.name, "machine": "unknown", "created_at": "", "files": [],
                        "verification": {"state": "failed", "error": "Backup metadata or archive is unavailable or invalid; inspect it offline."},
                        "archive_size": 0, "payload_size": 0}
        found.append(manifest)
    return found


def manifest(root: Path, backup_id: str) -> dict | None:
    """Read the manifest for a validated recovery-set identifier, if present."""
    if not BACKUP_ID.fullmatch(backup_id):
        return None
    return next((item for item in manifests(root) if item["id"] == backup_id), None)


def inventory(files: Iterable[BackupFile], excluded: list[dict]) -> list[dict]:
    """Describe both present and missing planned inputs without reading their content."""
    rows = [
        {
            "source": str(item.source),
            "archive": item.archive,
            "kind": item.kind,
            "status": "included" if item.source.is_file() else "missing",
            "size": item.source.stat().st_size if item.source.is_file() else None,
        }
        for item in files
    ]
    return rows + [{"status": "excluded", **item} for item in excluded]


def settings_files(path: str, prefix: str = "settings") -> list[BackupFile]:
    """Only regular TOML configuration files. Key directories, env files and links never enter."""
    if not path:
        return []
    folder = Path(path).parent
    return [
        BackupFile(file, f"{prefix}/{file.name}", "settings")
        for file in sorted(folder.glob("*.toml"))
        if not file.is_symlink()
    ]


def settings_aliases(path: str) -> list[dict]:
    """Record safe same-directory compatibility links without putting links in an archive."""
    if not path:
        return []
    folder = Path(path).parent
    try:
        resolved_folder = folder.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    aliases: list[dict] = []
    for link in sorted(folder.glob("*.toml")):
        if not link.is_symlink():
            continue
        row = {
            "source": str(link),
            "archive": "",
            "kind": "settings compatibility alias",
        }
        try:
            target = link.resolve(strict=True)
            safe = target.is_file() and target.parent == resolved_folder and target.suffix == ".toml"
        except (OSError, RuntimeError):
            target, safe = None, False
        if safe and target is not None:
            aliases.append(
                {
                    **row,
                    "status": "recorded",
                    "target": target.name,
                    "reason": f"recreate as a link to {target.name}; the canonical file is archived once",
                }
            )
        else:
            aliases.append(
                {
                    **row,
                    "reason": "unsafe, missing or external settings link; it is not archived",
                }
            )
    return aliases


def front_plan(data: str | Path, settings_path: str = "", plugins_dir: str = "",
               skip_databases: Iterable[Path] = ()) -> tuple[list[BackupFile], list[dict]]:
    """Inventory the backup files the control node keeps and explain excluded inputs."""
    data = Path(data)
    skipped = {Path(path).resolve() for path in skip_databases}
    files = [
        BackupFile(data / "chat.db", "data/chat.db", "conversations and inline attachments", database=True),
        BackupFile(data / "benchmarks.db", "data/benchmarks.db", "benchmark definitions and evidence", database=True),
        BackupFile(data / "settings.json", "data/settings.json", "site-managed settings"),
    ]
    files = [item for item in files if not item.database or item.source.resolve() not in skipped]
    planned = {item.source.resolve() for item in files} | skipped
    for path in sorted(data.glob("*.db")):
        if not path.is_symlink() and path.resolve() not in planned:
            files.append(BackupFile(path, f"data/{path.name}", "control node service data", database=True))
    files.extend(settings_files(settings_path))
    for folder, what in (('mirrorwall-assets', 'private image originals and thumbnails'), ('soundroom-assets', 'private audio takes, sounds and recordings')):
        images = data/folder
        if images.is_dir() and not images.is_symlink():
            for path in sorted(images.rglob('*')):
                if path.is_file() and not path.is_symlink() and images.resolve() in path.resolve().parents:
                    files.append(BackupFile(path, 'data/'+path.relative_to(data).as_posix(), what))
    if plugins_dir:
        for path in sorted(Path(plugins_dir).glob("*/settings.json")):
            files.append(BackupFile(path, f"plugins/{path.parent.name}/settings.json", "plugin settings"))
    excluded = settings_aliases(settings_path) + [
        {"source": str(data / "secret.json"), "archive": "", "kind": "WeightRoom cookie-signing secret", "reason": "recover sign-ins by signing in again"},
        {"source": str(Path(settings_path).parent.parent / "keys") if settings_path else "keys/", "archive": "", "kind": "raw key folder", "reason": "restore separately from its private source"},
        {"source": ".env", "archive": "", "kind": "environment secrets", "reason": "never included in openWeightGym archives"},
    ]
    return files, excluded


def compute_plan(database: DatabaseSource, settings_path: str = "", data: str | Path | None = None,
                 skip_databases: Iterable[Path] = ()) -> tuple[list[BackupFile], list[dict]]:
    """Inventory compute-owned backup files and explain excluded inputs."""
    main = local_path(database)
    folder = Path(data) if data is not None else main.parent if main is not None else Path(settings_path).parent
    skipped = {Path(path).resolve() for path in skip_databases}
    files = []
    if main is not None and main.resolve() not in skipped:
        files.append(BackupFile(main, "data/loadcoach.db", "users, models, requests, batches and settings",
                                database=True, contains_protected_identity=True))
    for path in sorted(folder.glob("*.db")):
        if path.resolve() not in skipped and (main is None or path.resolve() != main.resolve()):
            files.append(BackupFile(path, f"data/{path.name}", "service data", database=True))
    files.extend(settings_files(settings_path))
    excluded = settings_aliases(settings_path) + [
        {"source": str(folder / (Path(settings_path).stem + ".cli-token")) if settings_path else "*.cli-token", "archive": "", "kind": "runtime CLI token", "reason": "a new token is issued when the service starts"},
        {"source": str(Path(settings_path).parent.parent / "keys") if settings_path else "keys/", "archive": "", "kind": "raw key folder", "reason": "restore separately from its private source"},
        {"source": ".env", "archive": "", "kind": "environment secrets", "reason": "never included in openWeightGym archives"},
        {"source": "configured model files", "archive": "", "kind": "model weights", "reason": "identities and paths are recorded instead of copying large weights"},
    ]
    return files, excluded


def store_inventory(stores: Iterable[RecoveryStore]) -> list[dict]:
    """List every required configured store, including missing or unreachable ones."""
    return [inspect_store(store) for store in stores]
