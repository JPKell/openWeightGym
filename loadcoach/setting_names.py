"""Settings that were renamed or retired, and how files that still use the old names keep working.

See docs/peer-nodes-plan.md, Phase 1. A renamed key is read under its old name when the new one
is absent, so rollback and hand edits keep working; `owg update` also renames it in the file
(`rename_in_file`). A retired key is ignored. Each case is said once when the file is read.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

RENAMED = {
    "front_settings": "keep_console_settings_on_front",
    "front_toollocker": "toollocker_runs_on_front",
}
RETIRED = {
    "front_tasks": ("the control node always keeps task records now. Whether callers may choose a node is "
                    "'Allow callers to choose the node' (allow_caller_to_specify_destination) under Settings → General"),
}
BACKUP_SUFFIX = ".before-peer-nodes"


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def current_names(section: dict, where: str, say=_say) -> dict:
    """`section` with renamed keys under their new names and retired keys left out.

    Raises ValueError when an old and a new name are both set to different values.
    """
    result = dict(section)
    for old, new in RENAMED.items():
        if old not in result:
            continue
        value = result.pop(old)
        if new in result and result[new] != value:
            raise ValueError(f"{where}: {old} and {new} are both set, to different values. Keep only {new}.")
        if new not in result:
            result[new] = value
            say(f"{where}: {old} is now called {new}. It is read under its old name until the file is updated.")
    for old, why in RETIRED.items():
        if result.pop(old, None) is not None:
            say(f"{where}: {old} is ignored: {why}.")
    return result


_TABLE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")


def rename_in_file(path: Path, table: str | None, keys=RENAMED) -> list[str]:
    """Rename old keys to their new names in one TOML table of `path`, in place.

    `table` is the table's name, or None for the keys before the first table. Only `key = …`
    lines are changed; comments, order and every other line are kept. A key whose new name is
    already set in the same table is left alone. Before the first change the file is copied to
    `<name>.before-peer-nodes` (an existing copy is never replaced). Returns the renamed keys.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    current: str | None = None
    present: dict[str | None, set[str]] = {}
    for line in lines:
        if found := _TABLE.match(line):
            current = found.group(1).strip()
        elif key := re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line):
            present.setdefault(current, set()).add(key.group(1))
    already = present.get(table, set())
    renamed, current = [], None
    for number, line in enumerate(lines):
        if found := _TABLE.match(line):
            current = found.group(1).strip()
            continue
        if current != table:
            continue
        for old, new in keys.items():
            if new not in already and re.match(rf"^\s*{re.escape(old)}\s*=", line):
                lines[number] = re.sub(rf"^(\s*){re.escape(old)}(\s*=)", rf"\g<1>{new}\g<2>", line, count=1)
                renamed.append(old)
    if not renamed:
        return []
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
        os.chmod(backup, path.stat().st_mode & 0o777)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as written:
            written.write("".join(lines))
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return renamed


# Which settings file and table each renamed key lives in, for `owg update`.
FILES = {"weightroom.toml": "turnstile", "loadcoach.toml": None}

# Phase 2: every node is a peer. The old default node's top-level settings move to [machines.NAME].
LEGACY_NODE = "compute"
NODE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
MOVED_TO_NODE = ("compute", "wake_mac", "wake_address")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
NODE_BACKUP_SUFFIX = BACKUP_SUFFIX  # preserve the original pre-migration file across both phases


class NodeNameProblem(ValueError):
    """The old default node cannot be given its new name; nothing was changed."""


def node_name_for(hostname: str) -> str:
    """A node name from a host name: its first label, lower case. Empty when that is not a valid node name."""
    name = hostname.split(".", 1)[0].strip().lower()
    return name if NODE_NAME.fullmatch(name) and name not in ("front", LEGACY_NODE) else ""


def _write(path: Path, text: str, original: str) -> None:
    """Replace `path` with `text`, keeping its mode, after copying `original` beside it once."""
    backup = path.with_name(path.name + NODE_BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
        os.chmod(backup, path.stat().st_mode & 0o777)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as written:
            written.write(text)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _top_level(lines: list[str]) -> int:
    """The number of lines before the first table."""
    return next((number for number, line in enumerate(lines) if _TABLE.match(line)), len(lines))


def comment_out(text: str, keys, note: str) -> str:
    """`text` with the top-level lines setting any of `keys` turned into comments ending in `# note`.

    Only lines before the first table are touched, so a key of the same name inside a table stays.
    """
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^\s*({'|'.join(map(re.escape, keys))})\s*=")
    for number in range(_top_level(lines)):
        if pattern.match(lines[number]):
            lines[number] = "# " + lines[number].rstrip("\n") + f"  # {note}\n"
    return "".join(lines)


def _toml(value) -> str:
    """A string or whole number written as TOML."""
    import json
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else json.dumps(str(value))


def default_node_hostname(settings_folder: Path, told: dict) -> str:
    """The old default node's host name, as the control node last saw it: `machine_hosts` in the accounts store.

    The store is read where the control node's settings put it: SQLite in the data folder, or PostgreSQL
    through its `dsn_file` when psycopg is installed (it is in the suite's environment). Empty when
    the store, the row or the driver is missing; the node's name is then set by hand.
    """
    import json
    query = "SELECT hostname, names FROM machine_hosts ORDER BY last_seen DESC"
    databases = told.get("databases", {})
    store = databases.get("stores", {}).get(databases.get("bindings", {}).get("weightroom.accounts", ""), {})
    rows = []
    try:
        if store.get("engine") == "postgresql":
            dsn = store.get("dsn") or (settings_folder / store["dsn_file"]).read_text(encoding="utf-8").strip()
            import psycopg
            with psycopg.connect(dsn, connect_timeout=5) as db:
                rows = db.execute(query).fetchall()
        else:
            import sqlite3
            path = (settings_folder / store['path']).resolve() if store.get('path') else (settings_folder / told.get("data", ".") / "accounts.db").resolve()
            if path.is_file():
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                    rows = db.execute(query).fetchall()
    except Exception:  # noqa: BLE001 - any driver or store problem leaves the name to be set by hand
        return ""
    return next((hostname for hostname, names in rows if LEGACY_NODE in json.loads(names or "[]")), "")


def move_default_node(path: Path, hostname: str, say=_say) -> str:
    """Control node: move the top-level `compute` (and its wake settings) to `[machines.NAME]`, NAME from `hostname`.

    The node is written with `formerly = "compute"`, so rows, links and statements that still use
    the old name keep resolving. The old lines stay in the file as comments. Gives the new name, or
    "" when there is nothing to move. Raises NodeNameProblem, changing nothing, when the name is
    not valid or another node already has it. A loopback `compute` with no LoadCoach beside this file
    is a placeholder for a node never paired: it is commented out, and no node is added.
    """
    import tomllib
    original = path.read_text(encoding="utf-8")
    told = tomllib.loads(original)
    if not isinstance(told.get("compute"), str) or not told["compute"]:
        return ""
    from urllib.parse import urlsplit
    old = urlsplit(told["compute"])
    turnstile = told.get("turnstile", {})
    ports = list(turnstile.get("ports", [8442, 8443, 8444]))
    placeholder = (old.hostname or "") in LOOPBACK and not (path.parent / "loadcoach.toml").is_file()
    name = "" if placeholder else node_name_for(hostname)
    if not placeholder:
        if not name:
            raise NodeNameProblem(f"{path}: the default node's host name {hostname!r} is not a valid node name, or is unknown. "
                                  f"Move `compute` to [machines.NAME] with formerly = \"{LEGACY_NODE}\" by hand, then update again.")
        if name in told.get("machines", {}):
            raise NodeNameProblem(f"{path}: [machines.{name}] already exists, so the default node cannot take the name {name}. "
                                  f"Move `compute` to [machines.NAME] with formerly = \"{LEGACY_NODE}\" by hand, then update again.")
    moved = comment_out(original, (*MOVED_TO_NODE, "spotter"),
                        "never paired" if placeholder else f"moved to [machines.{name}] by owg update")
    if placeholder:
        _write(path, moved, original)
        say(f"{path}: removed the placeholder compute address; pair a node to add one (the old file is kept as {path.name}{NODE_BACKUP_SUFFIX}).")
        return ""
    port = old.port or ports[0]
    offset = port - ports[0]
    host = f"[{old.hostname}]" if ":" in (old.hostname or "") else old.hostname
    spotter = told.get("spotter") or (f"{old.scheme}://{host}:{ports[2] + offset}" if len(ports) > 2 else "")
    table = {"compute": f"{old.scheme}://{host}:{port}", "spotter": spotter,
             "machine_key_file": turnstile.get("machine_key_file", "../keys/machine.key"),
             **{key: told[key] for key in ("wake_mac", "wake_address") if told.get(key)},
             "formerly": LEGACY_NODE}
    text = moved.rstrip("\n") + "\n\n" + "\n".join([
        f"[machines.{name}]",
        f"# The former default node, named from its host by owg update. Its old name, {LEGACY_NODE}, still resolves.",
        *(f"{key} = {_toml(value)}" for key, value in table.items() if value)]) + "\n"
    if "preferred_voice_node" not in turnstile:
        # Voice keeps starting on the node it always used; Settings → General, Speech changes it.
        text = re.sub(r"(?m)^(\[turnstile\][^\n]*\n)", rf"\g<1>preferred_voice_node = {_toml(name)}\n", text, count=1)
    tomllib.loads(text)
    _write(path, text, original)
    say(f"{path}: the default node is now [machines.{name}] (formerly {LEGACY_NODE}); the old file is kept as {path.name}{NODE_BACKUP_SUFFIX}.")
    return name


def rename_node(path: Path, hostname: str, say=_say) -> str:
    """Node: change `machine_name = "compute"` (or no machine_name) to the name from `hostname`. Gives it, or "" if unchanged."""
    import tomllib
    original = path.read_text(encoding="utf-8")
    told = tomllib.loads(original)
    if told.get("machine_name", LEGACY_NODE) != LEGACY_NODE:
        return ""
    name = node_name_for(hostname)
    if not name:
        raise NodeNameProblem(f"{path}: this node's host name {hostname!r} is not a valid node name. Set machine_name by hand "
                              "to the name its [machines.NAME] has on the control node, then update again.")
    lines = original.splitlines(keepends=True)
    end = _top_level(lines)
    line = f"machine_name = {_toml(name)}  # formerly {LEGACY_NODE}; matches [machines.{name}] on the control node\n"
    found = next((number for number in range(end) if re.match(r"^\s*machine_name\s*=", lines[number])), None)
    if found is None:
        lines.insert(end, line)
    else:
        lines[found] = line
    if not told.get("former_machine_name"):
        lines.insert(0, f"former_machine_name = {_toml(LEGACY_NODE)}\n")
    text = "".join(lines)
    tomllib.loads(text)
    _write(path, text, original)
    say(f"{path}: machine_name is now {name} (formerly {LEGACY_NODE}); the old file is kept as {path.name}{NODE_BACKUP_SUFFIX}.")
    return name


def rename_installation(settings_folder: Path, say=_say) -> None:
    """Rename old keys in an installation's settings files, as `owg update` does after fetching code.

    Then every node becomes a peer: the control node's default node gets its own [machines.NAME] and the
    node itself the same machine_name, both named from the node's host name.
    """
    import socket
    import tomllib
    from urllib.parse import urlsplit
    front, node = settings_folder / "weightroom.toml", settings_folder / "loadcoach.toml"
    hostname = socket.gethostname()
    # Validate every pending node rename before writing any settings file.
    if front.is_file():
        told = tomllib.loads(front.read_text(encoding="utf-8"))
        old = urlsplit(told.get('compute', ''))
        if old.hostname and not (old.hostname in LOOPBACK and not node.is_file()):
            source = hostname if old.hostname in LOOPBACK and node.is_file() else default_node_hostname(settings_folder, told)
            proposed = node_name_for(source)
            if not proposed or proposed in told.get('machines', {}):
                raise NodeNameProblem(f"{front}: cannot rename the default node from hostname {source!r}; the name is invalid, unknown or already configured. No settings were changed.")
    if node.is_file():
        told_node = tomllib.loads(node.read_text(encoding='utf-8'))
        if told_node.get('machine_name', LEGACY_NODE) == LEGACY_NODE and not node_name_for(hostname):
            raise NodeNameProblem(f"{node}: hostname {hostname!r} is not a valid node name. No settings were changed.")
    for name, table in FILES.items():
        path = settings_folder / name
        if path.is_file():
            for old in rename_in_file(path, table):
                say(f"{path}: renamed {old} to {RENAMED[old]} (the old file is kept as {path.name}{BACKUP_SUFFIX}).")
    import socket
    import tomllib
    front, node = settings_folder / "weightroom.toml", settings_folder / "loadcoach.toml"
    if front.is_file():
        told = tomllib.loads(front.read_text(encoding="utf-8"))
        if told.get("compute"):
            here = (urlsplit(told["compute"]).hostname or "") in LOOPBACK and node.is_file()
            move_default_node(front, hostname if here else default_node_hostname(settings_folder, told), say)
    if node.is_file():
        rename_node(node, hostname, say)
