"""Settings changed from the site.

Every program here reads its settings from a TOML file of its own when it starts. A setting can
also be changed on the Settings or Compute nodes page. A change is kept in LoadCoach's database
and lies over the file: a program runs with the changed value if there is one, and with its
file's if not. Taking a change away brings the file's value back.

A setting is one top-level key of a program's file. Its value may be a whole table.

LoadCoach keeps the changes and hands them out. Each program lays them over its own file, takes
on what it can while it runs, and says back what it is running with and what it would not take.
So LoadCoach need not know another program's rules, and the page can say of every setting
whether it is in force.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import tomllib
from dataclasses import MISSING, fields, replace
from datetime import datetime
from pathlib import Path

import httpx

from loadcoach.platform_services import SERVICE_SETTINGS

FRONT_PROGRAMS = frozenset({'weightroom', 'turnstile', 'voice-gateway'})

from db import DatabaseSource, database, migrate
from db.schemas import SETTINGS


def front_programs(config, retired: bool = False) -> frozenset[str]:
    """Programs whose settings this node refuses because the control node keeps them.

    `retired` is whether the node's store went through the DB1 retirement (`store_retired`).
    """
    return (FRONT_PROGRAMS if retired else frozenset()) | ({'toollocker'} if config.toollocker_runs_on_front else frozenset())


def store_retired(source) -> bool:
    """Whether a node's store has retired its copies of the records the control node keeps (DB1)."""
    from db import schema_version
    with database(source).connection() as connection:
        return bool(schema_version(connection, "loadcoach.retirement"))


NOW, RESTART = "now", "restart"
LEGACY_PROGRAMS = {"frontdesk": "weightroom", "tooldoor": "toollocker", "relay": "turnstile"}


def program_name(program: str) -> str:
    """Canonical service names, including existing settings clients during upgrades."""
    return LEGACY_PROGRAMS.get(program, program)

# Compatibility shape for existing followers; the catalogue is the authority.
from loadcoach.setting_catalog import changeable, changeable_for, get as catalog_entry, validate, validate_values

CHANGEABLE: dict[str, dict[str, str]] = changeable()
SECRET = re.compile(r"(^|_)(key|token|secret|password)$")  # a setting with such a name is never handed out


def live(program: str, name: str, fallback, *, changes=None):
    """Read a validated live override; reset, invalid and restart values use the fallback.

    Control node callers bind their ConsoleSettings.changes. Dict services can pass
    their followed mapping; node dataclasses use the same catalogue via taken().
    """
    entry = catalog_entry(program, name)
    if not entry or entry.tier != NOW or entry.retired or entry.derived:
        return fallback
    values = changes.of(program) if hasattr(changes, 'of') else changes or {}
    if name not in values:
        return fallback
    try:
        return validate(program, name, values[name])
    except ValueError:
        return fallback


def apply_live(program: str, settings, changed: dict):
    """Copy all valid live dataclass fields, restoring file/default values on reset."""
    definitions = {field.name: field for field in fields(settings)}
    wanted, refused = {}, {}
    for name, tier in CHANGEABLE.get(program, {}).items():
        if tier != NOW or name not in definitions:
            continue
        field = definitions[name]
        fallback = (field.default_factory() if field.default_factory is not MISSING else
                    field.default if field.default is not MISSING else getattr(settings, name))
        value = changed.get(name, settings.file.get(name, fallback))
        try:
            value = validate(program, name, value)
            wanted[name] = tuple(value) if isinstance(getattr(settings, name), tuple) else value
        except ValueError as problem:
            refused[name] = str(problem)
    return replace(settings, **wanted), refused


def nested_value(values, name, fallback=None):
    for part in name.split('.'):
        if not isinstance(values, dict) or part not in values:
            return fallback
        values = values[part]
    return values


def set_nested(values, name, value):
    parts = name.split('.')
    for part in parts[:-1]:
        values = values.setdefault(part, {})
    values[parts[-1]] = value


def laid_over(told: dict, changed: dict, program: str) -> dict:
    """What a file says, with what was changed on the site laid over it. A change to a setting that may not be changed is left out."""
    selected={name:value for name,value in changed.items() if name in changeable_for(program, told)}
    table={'loadcoach':'engines','voice':'profiles'}.get(program)
    if table and isinstance(selected.get(table),dict) and isinstance(told.get(table),dict):
        # A new optional engine or voice profile in the file must not be hidden by an older
        # web edit to another one. Explicit overrides of the same entry still win.
        selected[table]={**told[table],**selected[table]}
    from copy import deepcopy
    result = deepcopy({**told, **{name: value for name, value in selected.items() if '.' not in name}})
    for name, value in selected.items():
        if '.' in name:
            set_nested(result, name, deepcopy(value))
    return result


def comments(text: str) -> dict[str, str]:
    """What a settings file says about each of its settings: the comment that stands right above it.

    The files are written with a comment before each key or table and an empty line between
    them. That comment is what the page shows beside the setting.
    """
    found: dict[str, str] = {}
    above: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            above.append(line.lstrip("# ").rstrip())
        elif named := re.match(r"\[?([A-Za-z0-9_-]+)", line):
            found.setdefault(named.group(1), " ".join(part for part in above if part))
            above = []
        elif not line.strip():
            above = []
    return found


def redacted(value, name=''):
    """Redact nested secrets, including service reports from older followers."""
    if SECRET.search(name):
        return '(not shown)'
    if isinstance(value, dict):
        return {key: redacted(inner, str(key)) for key, inner in value.items()}
    return [redacted(inner) for inner in value] if isinstance(value, list | tuple) else value


def said(running: dict, file: dict, says: dict, program='') -> dict:
    """What a program hands LoadCoach about itself: for each setting, the value it runs with, its file's if the file names it, and the file's comment.

    Anything that cannot be written as JSON is left out, and so is the value of anything
    whose name says it is a secret.
    """

    from loadcoach.setting_catalog import entries
    running, file = dict(running), dict(file)
    for definition in entries(program):
        if '.' in definition.name and definition.name.split('.')[0] in running:
            value = nested_value(running, definition.name, definition.default)
            if value is not None:
                running[definition.name] = value
                file[definition.name] = nested_value(file, definition.name, definition.default)
    told = {}
    for name, value in running.items():
        if (entry := catalog_entry(program, name)) and entry.kind == 'secret':
            continue
        try:
            entry = {"value": json.loads(json.dumps(redacted(value, name))), "says": says.get(name, "")}
        except TypeError:
            continue
        if name in file:
            entry["file"] = json.loads(json.dumps(redacted(file[name], name)))
        told[name] = entry
    return told


def kind_of(value) -> str:
    """Describe the setting's value type for the operator settings form."""
    return "true or false" if isinstance(value, bool) else "a number" if isinstance(value, int | float) else "a table" if isinstance(value, dict) else "a list" if isinstance(value, list) else "text"


def wrong_kind(new, old) -> str | None:
    """Why `new` cannot take the place of `old`, or None if it can. A value is replaced by one of its own kind: a number by a number, a table by a table."""
    if kind_of(new) != kind_of(old):
        return f"It is {kind_of(old)}, and this is {kind_of(new)}."
    if isinstance(new, int | float) and not isinstance(new, bool) and new < 0:
        return "It cannot be less than nothing."
    if isinstance(old, int) and not isinstance(old, bool) and not isinstance(new, int):
        return "It is a whole number."
    return None


def written(value, table: str = "") -> str:
    """A value as a TOML file would hold it. A table is written as the lines of a small file of its own."""
    if isinstance(value, dict):
        name = lambda key: key if re.fullmatch(r"[A-Za-z0-9_-]+", str(key)) else json.dumps(str(key))  # noqa: E731
        plain = [f"{name(key)} = {written(inner)}" for key, inner in value.items() if not isinstance(inner, dict)]
        tables = [f"[{table}{name(key)}]\n{written(inner, f'{table}{name(key)}.')}" for key, inner in value.items() if isinstance(inner, dict)]
        return "\n".join(plain + tables)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(written(inner) for inner in value) + "]"
    return json.dumps(value, ensure_ascii=False) if isinstance(value, str) else str(value)


def read_value(text: str, table: bool):
    """What was typed on the page, read as a TOML file reads a value. A table is typed as the lines of a small file. Raises ValueError, saying why."""
    try:
        return tomllib.loads(text) if table else tomllib.loads(f"value = {text.strip()}")["value"]
    except tomllib.TOMLDecodeError as problem:
        raise ValueError(f"That is not written as a settings file would hold it: {problem}") from None


class Changes:
    """Two small tables in LoadCoach's database: what was changed on the site, and what each program last said it runs with."""

    def __init__(self, path: DatabaseSource) -> None:
        """Open setting override storage and apply its portable schema migrations."""
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, "loadcoach.settings", SETTINGS)
        with self.db:
            for old, new in LEGACY_PROGRAMS.items():
                for name, value, by, at in self.db.execute(
                    'SELECT name,value,by,at FROM settings_changed WHERE program=?', (old,)).fetchall():
                    existing = self.db.execute('SELECT at FROM settings_changed WHERE program=? AND name=?', (new, name)).fetchone()
                    if not existing or at > existing[0]:
                        self.db.execute('INSERT INTO settings_changed VALUES (?,?,?,?,?) ON CONFLICT(program,name) DO UPDATE SET value=excluded.value,by=excluded.by,at=excluded.at', (new, name, value, by, at))
                row = self.db.execute('SELECT at,told,refused FROM settings_seen WHERE program=?', (old,)).fetchone()
                existing = self.db.execute('SELECT at FROM settings_seen WHERE program=?', (new,)).fetchone()
                if row and (not existing or row[0] > existing[0]):
                    self.db.execute('INSERT INTO settings_seen VALUES (?,?,?,?) ON CONFLICT(program) DO UPDATE SET at=excluded.at,told=excluded.told,refused=excluded.refused', (new, *row))
                self.db.execute('DELETE FROM settings_changed WHERE program=?', (old,))
                self.db.execute('DELETE FROM settings_seen WHERE program=?', (old,))

    def of(self, program: str) -> dict:
        """What was changed for one program: name and value."""
        return {name: json.loads(value) for name, value in self.db.execute("SELECT name, value FROM settings_changed WHERE program = ?", (program_name(program),))}

    def put(self, program: str, name: str, value, by: str) -> None:
        """Persist a validated program setting override and its changing account."""
        program = program_name(program)
        with self.db:
            self.db.execute("INSERT INTO settings_changed (program, name, value, by, at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(program,name) DO UPDATE SET value=excluded.value,by=excluded.by,at=excluded.at", (program, name, json.dumps(value), by, time.time()))

    def take_away(self, program: str, name: str) -> bool:
        """Remove a change, so that the file's value holds again. False if there was none."""
        program = program_name(program)
        with self.db:
            return self.db.execute("DELETE FROM settings_changed WHERE program = ? AND name = ?", (program, name)).rowcount > 0

    def listing(self) -> list[tuple]:
        """Every change there is: program, name, value, who made it and when."""
        rows = self.db.execute("SELECT program, name, value, by, at FROM settings_changed ORDER BY program, name")
        return [(program, name, json.loads(value), by, datetime.fromtimestamp(at).isoformat(timespec="seconds")) for program, name, value, by, at in rows]

    def seen(self, program: str, told: dict, refused: dict | None = None) -> None:
        """Keep what a program says it runs with, in place of what it said before."""
        program = program_name(program)
        safe = {}
        for name, report in told.items():
            entry = catalog_entry(program, name)
            if entry and entry.kind == 'secret':
                continue
            if isinstance(report, dict):
                safe[name] = {key: redacted(value, name) if key in ('value', 'file') else redacted(value)
                              for key, value in report.items()}
        safe_refused = {name: reason for name, reason in (refused or {}).items()
                        if not ((entry := catalog_entry(program, name)) and entry.kind == 'secret')}
        with self.db:
            self.db.execute("INSERT INTO settings_seen (program, at, told, refused) VALUES (?, ?, ?, ?) ON CONFLICT(program) DO UPDATE SET at=excluded.at,told=excluded.told,refused=excluded.refused", (program, time.time(), json.dumps(safe), json.dumps(redacted(safe_refused))))

    def everything(self, *, include_retired=False) -> list[dict]:
        """Every program that has said what it runs with, and each of its settings as the page shows it."""
        changed: dict[str, dict] = {}
        for program, name, value, by, at in self.db.execute("SELECT program, name, value, by, at FROM settings_changed"):
            changed.setdefault(program, {})[name] = {"value": json.loads(value), "by": by, "at": datetime.fromtimestamp(at).isoformat(timespec="seconds")}
        programs = []
        for program, at, told, refused in self.db.execute("SELECT program, at, told, refused FROM settings_seen ORDER BY program"):
            refusals = json.loads(refused)
            settings = [
                {"name": name, **entry, "when": CHANGEABLE.get("machines" if program.startswith("machines:") else program, {}).get(name, "file"), "changed": changed.get(program, {}).get(name), "refused": refusals.get(name)}
                for name, entry in json.loads(told).items() if include_retired or not entry.get("retired")
            ]
            programs.append({"program": program, "seen": datetime.fromtimestamp(at).isoformat(timespec="seconds"), "settings": settings})
        return programs


def changed_for(program: str, source: DatabaseSource) -> dict:
    """What was changed for a program, read at its start. Nothing, if the database cannot be read: a program must start all the same."""
    target = database(source)
    try:
        return Changes(target).of(program)
    except Exception as problem:
        if target.is_error(problem):
            return {}
        raise


def taken(program: str, settings, changed: dict, made):
    """The settings to run with, now that `changed` is what the site says, and what could not be taken, with why.

    For a program whose settings are a dataclass with `path` and `file`. `made(path, told)`
    is what builds and checks its settings from what a file says. Only the settings that can
    change while it runs are taken on. The rest wait for its next start.
    """
    if not settings.path:  # built with no file, as a test does: there is nothing to lay a change over
        return settings, {}
    try:
        wanted = made(Path(settings.path), laid_over(settings.file, changed, program))
    except SystemExit as why:
        return settings, {name: str(why) for name in changed}
    return replace(settings, **{name: getattr(wanted, name) for name, when in CHANGEABLE[program].items() if when == NOW}), {}


async def follow(program: str, ask, copy: Path, take, every: float = 10) -> None:
    """Keep a program that reaches LoadCoach over the network in step with what is changed for it. Runs for as long as the program does.

    `ask(method, path, json)` makes a request of LoadCoach with the program's own key and
    gives the answer's JSON, or None if LoadCoach did not answer or refused. `copy` is a
    file where the changes are kept beside the program, so that it can start with them while
    compute is off. `take(changed)` is the program's own: it takes on what it can, and gives
    back what it now runs with, as `said` makes it, and what it would not take.
    """
    told = None
    while True:
        changed = await ask("GET", f"/loadcoach/settings/{program}/changes", None)
        if changed is not None:
            if not copy.exists() or json.loads(copy.read_text()) != changed:
                copy.write_text(json.dumps(changed))
            now, refused = take(changed)
            if (now, refused) != told and await ask("POST", f"/loadcoach/settings/{program}/seen", {"told": now, "refused": refused}) is not None:
                told = (now, refused)
        await asyncio.sleep(every)


def asker(http: httpx.AsyncClient, base: str, key: str):
    """What `follow` asks LoadCoach with: a request made with a program's own key, answered with JSON, or with None if it could not be had."""

    async def ask(method: str, path: str, body):
        """Send a signed private request to the configured settings authority."""
        try:
            answer = await http.request(method, base + path, json=body, headers={"authorization": f"Bearer {key}"}, timeout=5)
        except httpx.HTTPError:
            return None
        return answer.json() if answer.status_code == 200 else None

    return ask


def with_changes(program: str, path: Path):
    """For a program that keeps its settings in a plain dict and reads them there each time: the dict to start with, and what keeps it in step.

    The dict is the file's settings with the changes laid over them, from the copy kept
    beside the file. The second thing is called once the program runs, with its HTTP client,
    LoadCoach's address and its own key. It gives what `follow` does, as something to run.
    A change is put into the same dict, so the program sees it the next time it looks.
    """
    from loadcoach.config import data_folder, read_settings, said_in  # here, since config.py reads this file in turn

    told = read_settings(path)
    validate_values(program, told)
    copy, says = data_folder(path, told) / f"{path.stem}.changes.json", said_in(path)
    saved = copy_of(copy)
    valid = {}
    for name, value in saved.items():
        if name in CHANGEABLE[program]:
            try:
                valid[name] = validate(program, name, value)
            except ValueError:
                pass  # A bad persisted override must not stop a service starting.
    settings = laid_over(told, valid, program)
    from copy import deepcopy
    from loadcoach.setting_catalog import entries
    for entry in entries(program):
        if '.' in entry.name and entry.name.split('.')[0] in settings and nested_value(settings, entry.name) is None and entry.default is not None:
            set_nested(settings, entry.name, deepcopy(entry.default))
    # What holds from the next start is not in force until then, whatever the dict says meanwhile.
    at_start = {name: deepcopy(nested_value(settings, name)) for name, when in CHANGEABLE[program].items() if when == RESTART and nested_value(settings, name) is not None}

    def take(changed: dict):
        """Read a running setting with saved overrides applied."""
        refused = {}
        for name, value in changed.items():
            if name in CHANGEABLE[program]:
                try:
                    validate(program, name, value)
                except ValueError as problem:
                    refused[name] = str(problem)
        merged = laid_over(told, {name: value for name, value in changed.items() if name not in refused}, program)
        settings.clear()
        settings.update({name: value for name, value in merged.items() if CHANGEABLE[program].get(name) != RESTART})
        for name, value in at_start.items():
            set_nested(settings, name, deepcopy(value))
        for entry in entries(program):
            if '.' in entry.name and entry.name.split('.')[0] in settings and nested_value(settings, entry.name) is None and entry.default is not None:
                set_nested(settings, entry.name, deepcopy(entry.default))
        return said(settings, told, says, program), refused

    return settings, lambda http, base, key: follow(program, asker(http, base, key), copy, take)


def copy_of(path: Path) -> dict:
    """The changes kept beside a program the last time LoadCoach was reached. Nothing, if there is no such file yet."""
    try:
        if not path.exists():
            for old, new in LEGACY_PROGRAMS.items():
                if path.name == f'{new}.changes.json':
                    path = path.with_name(f'{old}.changes.json')
                    break
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
