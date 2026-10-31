"""Changing one value in a TOML settings file while keeping every comment and line around it.

Used by the installer, which fills in a machine's settings from the examples, and by pairing,
which records a GPU node that joined. Standard library only: the installer may run this before
openWeightGym's own Python environment exists.
"""

from __future__ import annotations

import json
import re


def toml_value(value: object) -> str:
    """One value written as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {value!r} as TOML")


def set_toml_value(text: str, dotted: str, value: object) -> str:
    """Set one key of one table, keeping every other line and comment as it was.

    The key's own line is replaced; failing that, a commented-out example of it; failing that, it
    is added at the end of its table. Only single-line values are replaced.
    """
    table, _, key = dotted.rpartition(".")
    lines = text.splitlines(keepends=True)
    current, start, end, found, commented = "", 0, None, None, None
    for number, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            if current == table and end is None and number:
                end = number
            current = stripped.strip("[]").strip()
            if current == table:
                start, end = number + 1, None
            continue
        if current != table:
            continue
        if re.match(rf"{re.escape(key)}\s*=", stripped) and found is None:
            found = number
        elif re.match(rf"#\s*{re.escape(key)}\s*=", stripped) and commented is None:
            commented = number
    if current == table and end is None:
        end = len(lines)
    if not table:
        end = next((number for number, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    written = f"{key} = {toml_value(value)}\n"
    if found is not None:
        old = lines[found].split("=", 1)[1].strip()
        if old.count("[") != old.count("]"):
            raise ValueError(f"{dotted} spans several lines; set it by hand")
        lines[found] = written
    elif commented is not None and (not table or start <= commented < (end or len(lines))):
        lines[commented] = written
    elif end is not None:
        while end > start and not lines[end - 1].strip():  # after the table's last line, not its trailing blank ones
            end -= 1
        if end and not lines[end - 1].endswith("\n"):
            lines[end - 1] += "\n"
        lines.insert(end, written)
    else:
        lines.append(f"\n[{table}]\n{written}")
    return "".join(lines)
