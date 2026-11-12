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
    "front_tasks": ("the front always keeps task records now. Whether callers may choose a node is "
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


def rename_installation(settings_folder: Path, say=_say) -> None:
    """Rename old keys in an installation's settings files, as `owg update` does after fetching code."""
    for name, table in FILES.items():
        path = settings_folder / name
        if path.is_file():
            for old in rename_in_file(path, table):
                say(f"{path}: renamed {old} to {RENAMED[old]} (the old file is kept as {path.name}{BACKUP_SUFFIX}).")
