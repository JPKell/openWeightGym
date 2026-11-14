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
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx

from loadcoach.platform_services import SERVICE_SETTINGS

FRONT_PROGRAMS = frozenset({'weightroom', 'turnstile', 'voice-gateway'})

from db import DatabaseSource, database, migrate
from db.schemas import SETTINGS


def front_programs(config, retired: bool = False) -> frozenset[str]:
    """Programs whose settings this node refuses because the front keeps them.

    `retired` is whether the node's store went through the DB1 retirement (`store_retired`).
    """
    return (FRONT_PROGRAMS if retired else frozenset()) | ({'toollocker'} if config.toollocker_runs_on_front else frozenset())


def store_retired(source) -> bool:
    """Whether a node's store has retired its copies of the records the front keeps (DB1)."""
    from db import schema_version
    with database(source).connection() as connection:
        return bool(schema_version(connection, "loadcoach.retirement"))


NOW, RESTART = "now", "restart"
LEGACY_PROGRAMS = {"frontdesk": "weightroom", "tooldoor": "toollocker", "relay": "turnstile"}


def program_name(program: str) -> str:
    """Canonical service names, including existing settings clients during upgrades."""
    return LEGACY_PROGRAMS.get(program, program)

# For each program, the settings that can be changed from the site, and when a change holds:
# "now", the running program takes it on within a few seconds, or "restart", from its next
# start. Their starting values are in the program's defaults file, settings/defaults/NAME.toml.
#
# A setting that is not named here belongs to the machine: an address, a port, a path, the
# cards, where things are kept. Those are in the program's own file, settings/NAME.toml, are
# shown on the site, and cannot be changed there, since they could do harm from a browser.
# Engine executable paths are an explicit manage-settings exception, pending restart.
CHANGEABLE: dict[str, dict[str, str]] = {
    "loadcoach": {"engines": RESTART, "llama_server": RESTART, "disabled_gpus": NOW, "engine_gpus": NOW, "load_timeout": NOW, "max_wait": NOW, "max_estimated_wait": NOW, "keep_days": NOW, "keep_text": NOW, "batch_keep_days": NOW, "unlogged": NOW, "card_margin": NOW, "model_idle_minutes": NOW, "host_ram_headroom_mib": NOW, "host_ram_limit_mib": NOW, "cpu_idle_minutes": NOW, "disable_ram_offload": RESTART, "models_folder": RESTART, "models_storage_folder": RESTART, "model_defaults": NOW},
    "toollocker": {"call_timeout": NOW, "toolsets": NOW, "tool_levels": NOW},
    "spotter": {"sample_every": NOW, "keep_days": NOW, "sleep_after": NOW, "busy_gpu": NOW, "busy_processor": NOW, "files_every": NOW, "component_versions_every": NOW},
    "voice": {"max_active_sessions": NOW, "max_starting_sessions": NOW, "max_session_minutes": NOW, "idle_seconds": NOW, "hold_seconds": NOW,
              "hold_every": NOW, "hold_after_last_call": NOW, "speech_threshold": NOW, "profiles": NOW, "greetings": NOW, "model_parallel": RESTART},  # fmt: skip
    "voice-gateway": {"modes": NOW, "profile": NOW, "soundroom_profile": NOW, "startup_seconds": NOW, "max_active_sessions": NOW, "max_starting_sessions": NOW,
                      "hermes_model": NOW, "hermes_system_prompt": NOW, "hermes_max_turns": NOW, "hermes_context_tokens": NOW,
                      "hermes_max_output_tokens": NOW, "hermes_reasoning_effort": NOW,
                      "telephone": NOW, "public_port": RESTART, "public_host": RESTART},
    "weightroom": {**dict.fromkeys(SERVICE_SETTINGS, NOW), "timezone": RESTART, "mock_input_rate": RESTART, "mock_output_rate": RESTART, "third_party_services": NOW, "backup_retention": NOW, "alert_gpu_temperature": NOW,
                   "observation_error_percent": NOW, "observation_p95_seconds": NOW, "observation_min_samples": NOW,
                   "observation_window_seconds": NOW, "observation_alert_hold_seconds": NOW,
                   **{f"audio_{kind}_model": RESTART for kind in ("lyrics", "music", "sound", "speech", "transcription")}},
    "turnstile": {"response_keep_days": NOW, "wait": RESTART, "artifact_machine": RESTART, "image_adult_previews": NOW, "files_every": NOW, "component_versions_every": NOW,
                  "models_folder": NOW, "models_storage_folder": NOW, "allow_caller_to_specify_destination": NOW, "preferred_voice_node": NOW},
}
SECRET = re.compile(r"(^|_)(key|token|secret|password)$")  # a setting with such a name is never handed out


def laid_over(told: dict, changed: dict, program: str) -> dict:
    """What a file says, with what was changed on the site laid over it. A change to a setting that may not be changed is left out."""
    selected={name:value for name,value in changed.items() if name in CHANGEABLE.get(program,{})}
    table={'loadcoach':'engines','voice':'profiles'}.get(program)
    if table and isinstance(selected.get(table),dict) and isinstance(told.get(table),dict):
        # A new optional engine or voice profile in the file must not be hidden by an older
        # web edit to another one. Explicit overrides of the same entry still win.
        selected[table]={**told[table],**selected[table]}
    return {**told,**selected}


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


def said(running: dict, file: dict, says: dict) -> dict:
    """What a program hands LoadCoach about itself: for each setting, the value it runs with, its file's if the file names it, and the file's comment.

    Anything that cannot be written as JSON is left out, and so is the value of anything
    whose name says it is a secret.
    """

    def plain(value, name=""):
        """Recursively prepare settings for JSON while hiding secret-valued fields."""
        if SECRET.search(name):
            return "(not shown)"
        if isinstance(value, dict):
            return {key: plain(inner, str(key)) for key, inner in value.items()}
        return [plain(inner) for inner in value] if isinstance(value, list | tuple) else value

    told = {}
    for name, value in running.items():
        try:
            entry = {"value": json.loads(json.dumps(plain(value, name))), "says": says.get(name, "")}
        except TypeError:
            continue
        if name in file:
            entry["file"] = json.loads(json.dumps(plain(file[name], name)))
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
        with self.db:
            self.db.execute("INSERT INTO settings_seen (program, at, told, refused) VALUES (?, ?, ?, ?) ON CONFLICT(program) DO UPDATE SET at=excluded.at,told=excluded.told,refused=excluded.refused", (program, time.time(), json.dumps(told), json.dumps(refused or {})))

    def everything(self) -> list[dict]:
        """Every program that has said what it runs with, and each of its settings as the page shows it."""
        changed: dict[str, dict] = {}
        for program, name, value, by, at in self.db.execute("SELECT program, name, value, by, at FROM settings_changed"):
            changed.setdefault(program, {})[name] = {"value": json.loads(value), "by": by, "at": datetime.fromtimestamp(at).isoformat(timespec="seconds")}
        programs = []
        for program, at, told, refused in self.db.execute("SELECT program, at, told, refused FROM settings_seen ORDER BY program"):
            refusals = json.loads(refused)
            settings = [
                {"name": name, **entry, "when": CHANGEABLE.get(program, {}).get(name, "file"), "changed": changed.get(program, {}).get(name), "refused": refusals.get(name)}
                for name, entry in json.loads(told).items()
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
    copy, says = data_folder(path, told) / f"{path.stem}.changes.json", said_in(path)
    settings = laid_over(told, copy_of(copy), program)
    # What holds from the next start is not in force until then, whatever the dict says meanwhile.
    at_start = {name: settings[name] for name, when in CHANGEABLE[program].items() if when == RESTART and name in settings}

    def take(changed: dict):
        """Read a running setting with saved overrides applied."""
        refused = {name: why for name, value in changed.items() if name in told and (why := wrong_kind(value, told[name]))}
        merged = laid_over(told, {name: value for name, value in changed.items() if name not in refused}, program)
        settings.clear()
        settings.update(merged)
        return said({**merged, **at_start}, told, says), refused

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
