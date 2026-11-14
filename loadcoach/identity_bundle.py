"""Offline, encrypted identity portability; never restores request or conversation history.

Run ``python -m loadcoach.identity_bundle --help`` on each host. Export and apply
require stopped openWeightGym services; inspect and restore preview never write destinations.
Only OS-level administrators can use this tool. No web endpoint exposes raw credentials.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import time
import tomllib
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path

from db import DatabaseRegistry, migrate
from db.schemas import GROUPS, OUTSIDE_SERVICES, SETTINGS, USERS

MAGIC = b"SUITE-IDENTITY\x00\x01"
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE = 1024 * 1024
MAX_FILES = 4096
TABLES = {
    "groups": ("name", "settings"),
    "users": (
        "name",
        "grp",
        "enabled",
        "password",
        "must_change",
        "totp",
        "choices",
        "own",
        "identity",
    ),
    "keys": ("hash", "user", "label", "created", "expires", "via"),
    "spending_allowances": ("scope", "subject", "amount"),
    "services": ("name", "settings", "position"),
    "settings_changed": ("program", "name", "value", "by", "at"),
}
SECRET_SUFFIXES = (
    "key_file",
    "token_file",
    "secret_file",
    "password_file",
    "dsn_file",
    "env_file",
)


class BundleError(ValueError):
    """A safe, secret-free explanation of a rejected export or restore."""


def _json(value: object) -> bytes:
    """Encode bounded portable metadata without executable pickle or SQL payloads."""
    result = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(result) > MAX_BYTES:
        raise BundleError("Identity bundle exceeds the 32 MiB limit.")
    return result


def _read(path: Path, limit: int = MAX_FILE) -> bytes:
    """Read a bounded regular file without following a final symlink or device."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BundleError("A selected file is not a regular file.")
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise BundleError("A selected file exceeds its size limit.")
        return content
    except OSError:
        raise BundleError(
            "A selected file is unavailable or is a symbolic link."
        ) from None


def _safe_path(path: str | Path) -> Path:
    """Reject traversal and every existing symlink component before local file access.

    Restoration requires an administrator-owned filesystem with no concurrent changes;
    the advisory lock serializes this tool, not arbitrary external filesystem writers.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise BundleError("Bundle paths must be absolute and cannot contain traversal.")
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise BundleError("Symbolic links are not allowed in identity file paths.")
    return path


def _seal(payload: dict, passphrase: str) -> bytes:
    """Encrypt with a fresh salt/nonce, fixed scrypt cost and AES-256-GCM authentication."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if not 16 <= len(passphrase.encode()) <= 1024:
        raise BundleError("Use an export passphrase of 16 to 1024 UTF-8 bytes.")
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(passphrase.encode())
    return header + AESGCM(key).encrypt(nonce, _json(payload), header)


def _open(content: bytes, passphrase: str) -> dict:
    """Authenticate before parsing; reject malformed versions and unbounded KDF parameters."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    size = len(MAGIC) + 28
    if (
        len(content) > MAX_BYTES + size + 16
        or len(content) < size + 16
        or not content.startswith(MAGIC)
    ):
        raise BundleError("Not a supported openWeightGym identity bundle.")
    if not 1 <= len(passphrase.encode()) <= 1024:
        raise BundleError("Invalid passphrase length.")
    salt, nonce = content[len(MAGIC) : len(MAGIC) + 16], content[len(MAGIC) + 16 : size]
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(passphrase.encode())
    try:
        plain = AESGCM(key).decrypt(nonce, content[size:], content[:size])
    except InvalidTag:
        raise BundleError(
            "Wrong passphrase or damaged identity bundle; nothing was restored."
        ) from None
    try:
        value = json.loads(plain)
    except (ValueError, RecursionError):
        raise BundleError("Invalid identity bundle contents.") from None
    _validate(value)
    return value


def _accounts(config: Path):
    """Resolve the configured control node account authority without loading keys or starting services."""
    told = tomllib.loads(_read(config).decode())
    data = config.parent / told.get("data", ".")
    return DatabaseRegistry.from_config(
        told.get("databases", {}),
        config.parent,
        {"weightroom.accounts": data / "accounts.db"},
    ).database("weightroom.accounts")


def _tables(source) -> dict:
    """Snapshot identity-only rows in one transaction; never migrate the source on export."""
    if source.path is not None:
        _safe_path(Path(os.path.abspath(source.path)))
    if source.path is not None and not source.path.exists():
        raise BundleError("The configured account store does not exist.")
    with source.connection() as connection:
        connection.execute(
            "BEGIN"
            if source.engine == "sqlite"
            else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        try:
            found = set(source.tables(connection))
            result = {}
            total = 0
            for name, columns in TABLES.items():
                if name not in found:
                    if name in ("groups", "users", "keys"):
                        raise BundleError(
                            "The account store has no current identity schema."
                        )
                    result[name] = []
                    continue
                sql = ",".join(source.quote_identifier(c) for c in columns)
                result[name] = []
                for row in connection.execute(
                    f"SELECT {sql} FROM {source.quote_identifier(name)}"
                ):
                    row = list(row)
                    total += len(_json(row))
                    if total > MAX_BYTES or len(result[name]) >= 100000:
                        raise BundleError("Identity rows exceed the export limits.")
                    result[name].append(row)
            return result
        finally:
            connection.rollback()


def _references(value: object):
    """Yield credential-file references recursively, excluding unrelated model/runtime paths."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and item and str(key).endswith(SECRET_SUFFIXES):
                yield item
            elif isinstance(item, (dict, list)):
                yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def collect(
    root: Path,
    home: Path,
    includes: list[Path] = (),
    accounts_config: Path | None = None,
) -> dict:
    """Gather one stopped host's identity projection, configurations and credential files.

    Source paths stay exact so restored service references remain valid. Custom external
    credentials can be selected with --include. Runtime tokens, histories and model assets
    are never discovered recursively outside designated key directories.
    """
    root, home = _safe_path(root.absolute()), _safe_path(home.absolute())
    files: dict[str, dict] = {}
    file_bytes = 0
    configs: list[Path] = []
    references: set[Path] = set()

    def add(path: Path, kind: str) -> None:
        """Deduplicate selected files and retain only encrypted payload bytes and integrity metadata."""
        nonlocal file_bytes
        path = _safe_path(path.absolute())
        if str(path) in files:
            return
        content = _read(path)
        if content.startswith(b"SQLite format 3\x00") or path.name in (
            ".identity-import.lock",
            ".identity-import-incomplete",
        ):
            raise BundleError(
                "Database files and import control files cannot be selected as credentials."
            )
        file_bytes += ((len(content) + 2) // 3) * 4 + len(str(path)) + 256
        if file_bytes > MAX_BYTES:
            raise BundleError("Credential files exceed the bundle limit.")
        if len(files) >= MAX_FILES:
            raise BundleError("Too many credential/configuration files.")
        files[str(path)] = {
            "path": str(path),
            "kind": kind,
            "sha256": hashlib.sha256(content).hexdigest(),
            "content": base64.b64encode(content).decode(),
        }

    def directory(folder: Path, kind: str) -> None:
        """Read designated private directories only, refusing symlinks and special files."""
        if not folder.exists() and not folder.is_symlink():
            return
        _safe_path(folder)
        for path in sorted(folder.rglob("*")):
            if (
                path.is_symlink()
                and path.name == "frontdesk.key"
                and os.readlink(path) == "weightroom.key"
            ):
                canonical = path.parent / "weightroom.key"
                if canonical.is_file() and not canonical.is_symlink():
                    continue  # redundant legacy alias; any remaining configuration reference still fails below
            _safe_path(path)
            if path.is_dir():
                continue
            add(path, kind)

    directory(root / "keys", "credential")
    # Settings are needed to preserve which service owns each key; no source code is copied.
    configs.extend(sorted((root / "settings").glob("*.toml")))
    configs.extend(sorted((root / "plugins").glob("*/settings.json")))
    config_root = home / ".config/toollocker-host"
    data_root = home / ".local/share"
    if home == Path.home():
        config_root = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
            / "toollocker-host"
        )
        data_root = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
    tool = config_root
    if tool.exists():
        directory(tool, "credential")
        if (tool / "tools.toml").is_file():
            configs.append(tool / "tools.toml")
    adapters = data_root / "suite-adapters"
    for config in sorted(adapters.glob("*/*/settings.json")):
        configs.append(config)
        directory(config.parent / "keys", "credential")
    config = accounts_config or root / "settings/weightroom.toml"
    identity = None
    if config.exists():
        config = _safe_path(config.absolute())
        configs.append(config)
        told = tomllib.loads(_read(config).decode())
        if told.get("plugins_dir"):
            configs.extend(
                sorted((config.parent / told["plugins_dir"]).glob("*/settings.json"))
            )
        if told.get("turnstile", {}).get("hosted_keys"):
            directory(
                Path(os.path.abspath(config.parent / told["turnstile"]["hosted_keys"])),
                "credential",
            )
        source = _accounts(config)
        if source.engine != "sqlite":
            raise BundleError(
                "Identity portability currently requires a SQLite account store."
            )
        identity = {"config": str(config), "tables": _tables(source)}
        for program, name, raw, _, _ in identity["tables"]["settings_changed"]:
            value = json.loads(raw)
            references.update(
                root / "settings" / ref for ref in _references({name: value})
            )
        cached = Path(
            os.path.abspath(config.parent / told.get("data", ".") / "settings.json")
        )
        if cached.exists():
            configs.append(cached)
    for config in dict.fromkeys(configs):
        if config.is_symlink() and config.name in ("frontdesk.toml", "tooldoor.toml"):
            canonical = config.resolve()
            if (
                canonical.parent == config.parent
                and canonical.suffix == ".toml"
                and canonical.is_file()
            ):
                continue  # installer-owned compatibility alias; canonical file is selected above
        add(config, "configuration")
        raw = _read(config).decode()
        value = json.loads(raw) if config.suffix == ".json" else tomllib.loads(raw)
        references.update(config.parent / ref for ref in _references(value))
    for path in sorted(references):
        # Configured paths routinely contain ../keys; canonicalize this trusted local input,
        # but reject any symlink component before resolving it.
        absolute = Path(os.path.abspath(path))
        add(absolute, "credential")
    for path in includes:
        add(Path(os.path.abspath(path)), "explicit")
    payload = {
        "version": 1,
        "created": time.time(),
        "root": str(root),
        "home": str(home),
        "identity": identity,
        "files": list(files.values()),
    }
    _validate(payload)
    target_store(payload)
    _json(payload)
    return payload


def _validate(value: dict) -> None:
    """Reject unknown sections, paths, row shapes and damaged file data before any restore."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "created", "root", "home", "identity", "files"}
        or value["version"] != 1
    ):
        raise BundleError("Unsupported identity payload schema.")
    _safe_path(value["root"])
    _safe_path(value["home"])
    if not isinstance(value["files"], list) or len(value["files"]) > MAX_FILES:
        raise BundleError("Invalid identity file inventory.")
    paths = set()
    for item in value["files"]:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "kind",
            "sha256",
            "content",
        }:
            raise BundleError("Invalid identity file entry.")
        path = str(_safe_path(item["path"]))
        if path in paths or item["kind"] not in (
            "credential",
            "configuration",
            "explicit",
        ):
            raise BundleError("Duplicate or unsupported identity file entry.")
        paths.add(path)
        try:
            content = base64.b64decode(item["content"], validate=True)
        except (ValueError, TypeError):
            raise BundleError("Invalid encrypted file encoding.") from None
        if content.startswith(b"SQLite format 3\x00") or Path(path).name in (
            ".identity-import.lock",
            ".identity-import-incomplete",
        ):
            raise BundleError(
                "Database files and import control files cannot be restored as credentials."
            )
        if (
            len(content) > MAX_FILE
            or hashlib.sha256(content).hexdigest() != item["sha256"]
        ):
            raise BundleError("Identity file integrity check failed.")
    if any(str(parent) in paths for path in paths for parent in Path(path).parents):
        raise BundleError("Identity files cannot overlap destination directories.")
    identity = value["identity"]
    if identity is None:
        return
    if (
        not isinstance(identity, dict)
        or set(identity) != {"config", "tables"}
        or identity["config"] not in paths
    ):
        raise BundleError("Invalid account configuration reference.")
    if not isinstance(identity["tables"], dict) or set(identity["tables"]) != set(
        TABLES
    ):
        raise BundleError("Invalid identity table selection.")
    for name, columns in TABLES.items():
        rows = identity["tables"][name]
        if (
            not isinstance(rows, list)
            or len(rows) > 100000
            or any(
                not isinstance(row, list)
                or len(row) != len(columns)
                or any(
                    type(v) not in (str, int, float, type(None))
                    or isinstance(v, float)
                    and not math.isfinite(v)
                    for v in row
                )
                for row in rows
            )
        ):
            raise BundleError("Invalid identity table rows.")
        primary = [
            tuple(row[:2])
            if name in ("spending_allowances", "settings_changed")
            else row[0]
            for row in rows
        ]
        if len(primary) != len(set(primary)):
            raise BundleError("Duplicate primary keys in identity rows.")
    names = {row[0] for row in identity["tables"]["users"]}
    if any(row[1] not in names for row in identity["tables"]["keys"]):
        raise BundleError("An API key references a missing account.")


def _same_rows(left: dict, right: dict) -> bool:
    """Compare complete identity projections independent of database row order."""
    return all(
        sorted(map(_json, left[name])) == sorted(map(_json, right[name]))
        for name in TABLES
    )


def _config_from_payload(payload: dict) -> dict | None:
    """Read the encrypted account configuration without staging its plaintext on disk."""
    if not payload["identity"]:
        return None
    path = payload["identity"]["config"]
    item = next(item for item in payload["files"] if item["path"] == path)
    return tomllib.loads(base64.b64decode(item["content"]).decode())


def target_store(payload: dict):
    """Resolve SQLite account destination from the restored configuration, never switch an engine.

    PostgreSQL credential files can be exported, but account restore into a server is refused
    until a separately scoped, database-transaction implementation is qualified.
    """
    told = _config_from_payload(payload)
    if told is None:
        return None
    config = Path(payload["identity"]["config"])
    target = DatabaseRegistry.from_config(
        told.get("databases", {}),
        config.parent,
        {"weightroom.accounts": config.parent / told.get("data", ".") / "accounts.db"},
    ).database("weightroom.accounts")
    if target.engine != "sqlite" or target.path is None:
        raise BundleError(
            "Identity import currently requires a local SQLite account store."
        )
    path = _safe_path(Path(os.path.abspath(target.path)))
    if str(path) in {item["path"] for item in payload["files"]} or path.name in (
        ".identity-import.lock",
        ".identity-import-incomplete",
    ):
        raise BundleError(
            "The account database cannot overlap credential or control files."
        )
    if not path.is_relative_to(Path(payload["root"])):
        raise BundleError(
            "The account destination must be inside this openWeightGym installation."
        )
    return target


def preview(payload: dict, *, replace_files: bool = False) -> dict:
    """Report only paths, counts and conflicts; never print identities, verifiers or file contents."""
    _validate(payload)
    conflicts, paths = [], []
    for item in payload["files"]:
        path = _safe_path(item["path"])
        state = "create"
        if path.exists():
            same = hashlib.sha256(_read(path)).hexdigest() == item["sha256"]
            state = "unchanged" if same else "replace" if replace_files else "conflict"
            if state == "conflict":
                conflicts.append(str(path))
        paths.append({"path": str(path), "kind": item["kind"], "action": state})
    target = target_store(payload)
    identity_state = "not included"
    if target is not None:
        identity_state = "create"
        if target.path.exists():
            with target.connection() as con:
                tables = set(target.tables(con))
                occupied = any(
                    con.execute(
                        f"SELECT 1 FROM {target.quote_identifier(t)} LIMIT 1"
                    ).fetchone()
                    for t in (
                        "users",
                        "keys",
                        "services",
                        "spending_allowances",
                        "settings_changed",
                    )
                    if t in tables
                )
            if occupied:
                try:
                    same = _same_rows(_tables(target), payload["identity"]["tables"])
                except Exception:
                    same = False
                identity_state = "unchanged" if same else "conflict"
                if not same:
                    conflicts.append(
                        "account store already contains different identities or service policies"
                    )
    return {
        "version": 1,
        "root": payload["root"],
        "files": paths,
        "accounts": identity_state,
        "counts": {
            name: len(rows) for name, rows in payload["identity"]["tables"].items()
        }
        if payload["identity"]
        else {},
        "conflicts": conflicts,
        "history_restored": False,
    }


@contextmanager
def _lock(root: Path):
    """Serialize cooperating offline imports without exposing secret data in the lock file."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _safe_path(root / ".identity-import.lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise BundleError("Another identity import is in progress.") from None
    finally:
        os.close(descriptor)


def _write(path: Path, content: bytes, *, replace: bool = False) -> None:
    """Flush a private same-directory temporary file and publish atomically, refusing collisions."""
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".suite-identity-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def restore(payload: dict, *, replace_files: bool = False) -> dict:
    """Apply offline after full preflight, with transactional rows and file rollback on errors.

    A crash marker survives process/power loss: keep services stopped, inspect the marker,
    then rerun the same bundle. Identical rows/files are idempotent; different existing
    accounts are never overwritten. There is no distributed transaction across hosts.
    """
    _validate(payload)
    root = _safe_path(payload["root"])
    with _lock(root), ExitStack() as stack:
        plan = preview(payload, replace_files=replace_files)
        if plan["conflicts"]:
            raise BundleError(
                "Restore has conflicts; inspect the preview. Existing identities were not changed."
            )
        marker = root / ".identity-import-incomplete"
        fingerprint = hashlib.sha256(_json(payload)).hexdigest().encode()
        if marker.exists() and _read(marker) != fingerprint:
            raise BundleError(
                "An interrupted import of another bundle needs recovery first."
            )
        source = target_store(payload)
        con = None
        if source is not None:
            source.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            con = stack.enter_context(source.connection())
            os.chmod(source.path, 0o600)
            for name, migrations in (
                ("loadcoach.users", USERS),
                ("loadcoach.groups", GROUPS),
                ("loadcoach.outside", OUTSIDE_SERVICES),
                ("loadcoach.settings", SETTINGS),
            ):
                migrate(source, con, name, migrations)
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE")
            # Recheck under the write lock so another writer cannot win between preview/apply.
            current = {
                name: [
                    list(row)
                    for row in con.execute(
                        "SELECT "
                        + ",".join(source.quote_identifier(c) for c in cols)
                        + " FROM "
                        + source.quote_identifier(name)
                    )
                ]
                for name, cols in TABLES.items()
            }
            occupied = any(
                current[name]
                for name in (
                    "users",
                    "keys",
                    "services",
                    "spending_allowances",
                    "settings_changed",
                )
            )
            if occupied and not _same_rows(current, payload["identity"]["tables"]):
                raise BundleError(
                    "Account identities changed after preview; nothing was imported."
                )
        if not marker.exists():
            _write(marker, fingerprint)
        written = []
        try:
            for item in payload["files"]:
                path = _safe_path(item["path"])
                previous = _read(path) if path.exists() else None
                content = base64.b64decode(item["content"])
                if previous != content:
                    if previous is not None and not replace_files:
                        raise BundleError(
                            "A destination changed after preview; replacement was not authorized."
                        )
                    written.append((path, previous))
                    _write(path, content, replace=previous is not None)
                path.chmod(0o600)
            if con is not None and not occupied:
                for name in reversed(TABLES):
                    con.execute("DELETE FROM " + source.quote_identifier(name))
                for name, cols in TABLES.items():
                    sql = (
                        "INSERT INTO "
                        + source.quote_identifier(name)
                        + " ("
                        + ",".join(source.quote_identifier(c) for c in cols)
                        + ") VALUES ("
                        + ",".join("?" for _ in cols)
                        + ")"
                    )
                    con.executemany(sql, payload["identity"]["tables"][name])
            if con is not None:
                con.commit()
        except BaseException:
            if con is not None:
                con.rollback()
            for path, previous in reversed(written):
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _write(path, previous, replace=True)
            marker.unlink(missing_ok=True)
            raise
        marker.unlink()
        plan["applied"] = True
        return plan


def ensure_stopped(root: Path, home: Path, proc: Path = Path("/proc")) -> None:
    """Refuse known same-user openWeightGym processes on Linux, in addition to operator confirmation.

    This is a best-effort local process guard, not a distributed maintenance lock. The
    operator must also stop custom launchers, schedules and automatic service restarts.
    """
    programs = {
        "loadcoach",
        "weightroom",
        "frontdesk",
        "turnstile",
        "toollocker",
        "tooldoor",
        "spotter",
        "voice",
        "voice-gateway",
        "suite-adapter",
    }
    modules = {
        "loadcoach",
        "loadcoach.cli",
        "weightroom.app",
        "turnstile.app",
        "toollocker.door",
        "toollocker.tools",
        "toollocker.tools.__main__",
        "spotter.app",
        "voice.service",
        "voice.gateway",
        "plugins._runtime",
    }
    scopes = (
        root.absolute(),
        home / ".local/share/suite-adapters",
        home / ".local/share/toollocker-host",
    )
    for process in proc.glob("[0-9]*"):
        if process.name == str(os.getpid()):
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            argv = (process / "cmdline").read_bytes().split(b"\x00")
            args = [a.decode(errors="replace") for a in argv if a]
            if not args:
                continue
            interpreter = Path(args[0]).name
            selected = (
                interpreter in programs
                or (
                    interpreter.startswith("python")
                    and len(args) > 1
                    and Path(args[1]).name in programs
                )
                or any(
                    a == "-m" and i + 1 < len(args) and args[i + 1] in modules
                    for i, a in enumerate(args)
                )
            )
            if not selected:
                continue
            cwd = (process / "cwd").resolve(strict=True)
            locations = [
                cwd,
                *(Path(arg) for arg in args[:2] if Path(arg).is_absolute()),
            ]
            if any(
                location.is_relative_to(scope)
                for location in locations
                for scope in scopes
            ):
                raise BundleError(
                    "An openWeightGym process is still running; stop services, adapters and tool hosts first."
                )
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue


def _passphrase(confirm: bool) -> str:
    """Read only from an interactive terminal; never accept secrets in arguments or environment."""
    if not sys.stdin.isatty():
        raise BundleError("Run in a terminal to enter the passphrase privately.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        phrase = getpass.getpass(
            "Export passphrase: " if confirm else "Bundle passphrase: "
        )
        if confirm and phrase != getpass.getpass("Confirm passphrase: "):
            raise BundleError("Passphrases do not match.")
    return phrase


def main(argv: list[str] | None = None) -> None:
    """Expose export, inspect and preview-by-default import without running openWeightGym services."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser(
        "export", help="Export this stopped host; run once per host."
    )
    export.add_argument("bundle", type=Path)
    export.add_argument("--root", type=Path, default=Path.cwd())
    export.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Home containing standalone adapters/tool-host configuration.",
    )
    export.add_argument(
        "--accounts-config",
        type=Path,
        help="Custom control node account configuration (default settings/weightroom.toml).",
    )
    export.add_argument(
        "--include",
        type=Path,
        action="append",
        default=[],
        help="Additional private file, e.g. a service environment file.",
    )
    export.add_argument(
        "--services-stopped",
        action="store_true",
        help="Confirm all participating openWeightGym services and key rotators are stopped.",
    )
    inventory = sub.add_parser(
        "inventory",
        help="Read-only live inventory: paths/counts, no archive or secret output.",
    )
    inventory.add_argument("--root", type=Path, default=Path.cwd())
    inventory.add_argument("--home", type=Path, default=Path.home())
    inventory.add_argument("--accounts-config", type=Path)
    inventory.add_argument("--include", type=Path, action="append", default=[])
    inspect = sub.add_parser("inspect", help="Decrypt and show paths/counts only.")
    inspect.add_argument("bundle", type=Path)
    restore_parser = sub.add_parser(
        "import",
        help="Preview first; --apply restores exact original paths on this host.",
    )
    restore_parser.add_argument("bundle", type=Path)
    restore_parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Must match the bundle installation path; relocation is not supported.",
    )
    restore_parser.add_argument("--apply", action="store_true")
    restore_parser.add_argument("--services-stopped", action="store_true")
    restore_parser.add_argument(
        "--replace-files",
        action="store_true",
        help="Replace conflicting credential/configuration files, never different existing accounts.",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            payload = collect(args.root, args.home, args.include, args.accounts_config)
            print(json.dumps(preview(payload), indent=2))
        elif args.command == "export":
            if not args.services_stopped:
                raise BundleError(
                    "Stop participating services, then confirm with --services-stopped."
                )
            destination = _safe_path(args.bundle.absolute())
            if destination.exists():
                raise BundleError(
                    "Export destination already exists; choose a new filename."
                )
            ensure_stopped(args.root, args.home)
            phrase = _passphrase(True)
            payload = collect(args.root, args.home, args.include, args.accounts_config)
            _write(destination, _seal(payload, phrase))
            print(
                json.dumps(
                    {
                        "exported": str(destination),
                        "files": len(payload["files"]),
                        "accounts": bool(payload["identity"]),
                        "history_exported": False,
                    }
                )
            )
        else:
            if args.command == "import" and args.apply and not args.services_stopped:
                raise BundleError(
                    "Stop participating services before applying, then confirm with --services-stopped."
                )
            payload = _open(
                _read(args.bundle, MAX_BYTES + len(MAGIC) + 44), _passphrase(False)
            )
            if (
                args.command == "import"
                and str(args.root.absolute()) != payload["root"]
            ):
                raise BundleError(
                    "This bundle belongs to a different installation path; use its original --root."
                )
            if args.command == "import" and args.apply:
                ensure_stopped(Path(payload["root"]), Path(payload["home"]))
                result = restore(payload, replace_files=args.replace_files)
            else:
                result = preview(
                    payload, replace_files=getattr(args, "replace_files", False)
                )
            print(json.dumps(result, indent=2))
    except Exception:
        # Driver/config exceptions can embed connection strings or inline settings secrets.
        # Only our own curated errors are suitable for printing.
        error = sys.exception()
        print(
            str(error)
            if isinstance(error, BundleError)
            else "Identity operation failed; check file permissions, configuration and bundle compatibility.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
