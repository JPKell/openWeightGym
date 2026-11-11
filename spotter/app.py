"""spotter: a small helper on compute that watches the machine and can revive what runs on it.

It reads the machine's telemetry, keeps weeks of it, says whether the services are up, and can
start, stop or restart the ones named in its settings. It can put the machine to sleep, restart
it and switch it off, and it puts the machine to sleep by itself when nothing has used it for a
while. It is a program of its own so that it still answers when LoadCoach does not, which is
exactly when it is wanted.

Its rule is a short, fixed list of things it can do. A caller names a service from spotter's own
settings and one of three actions, or one of three things to do to the machine. Nothing a caller
sends is ever run as a command, used as a path, or taken as the name of a unit.

Who may ask is turnstile's to say, on the front machine: it checks the caller's key and sends a
signed statement in its place (see loadcoach/statement.py), so the same keys work here, through
turnstile, whether or not LoadCoach is running.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE, STDOUT
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from db import DatabaseRegistry, DatabaseSource, migrate
from db.maintenance import Barrier, Gate
from db.schemas import SPOTTER

import httpx
import typer
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.config import CLI, data_folder, kept_by_loadcoach, model_places, read_settings, said_in
from loadcoach.files import SPARE
from loadcoach.file_index import NODE_INDEX, FileIndex
from loadcoach.log import Log
from loadcoach import statement, observability as observation
from loadcoach.groups import ABILITIES
from loadcoach.statement import Statements, user_of
from loadcoach.settings import Changes, changed_for, laid_over, said, taken
from loadcoach.users import User
from spotter.screen import at_the_screen

ACTIONS = ("start", "stop", "restart")  # everything spotter can do to a service
# Everything spotter can do to the machine: what it is asked for, systemd's word for it, and what
# callers who are turned away meanwhile are told.
KERNEL = "kernel"  # not a service: the name under which the kernel's own lines are asked for
# Which of the kernel's lines: the NVIDIA driver's, and those about memory running out, which is how a model server dies unasked.
# Asked for as a kind of message and not with -k, which would give the time since this start only.
KERNEL_LINES = ("_TRANSPORT=kernel", "-g", "NVRM|Xid|nvidia|Out of memory|oom-kill|Killed process")
POWER = {
    "sleep": ("suspend", "compute is going to sleep."),
    "reboot": ("reboot", "compute is being restarted."),
    "off": ("poweroff", "compute is being switched off."),
}
FRESH = 2  # seconds a reading is good for. Asked again sooner, the same one is given, so ten open pages cost one reading.


@dataclass(frozen=True)
class Settings:
    """Everything spotter is told. `machine_key_file` and `database` come from LoadCoach's own file."""

    services: dict[str, str]  # a name of ours -> the user unit it stands for, such as loadcoach.service
    system_services: dict[str, str] = field(default_factory=dict)  # the same for root's units, such as ollama.service
    compute: str = "http://127.0.0.1:8442"  # where LoadCoach answers on this machine
    token_file: str = ""  # what a running LoadCoach leaves for programs on this machine to sign in with
    sleep_after: float = 0  # minutes with nothing using the machine before it is put to sleep. 0 means never
    busy_gpu: float = 10  # a GPU at this percentage or more counts as the machine being used
    busy_processor: float = 25  # and so does the processor at this
    machine_key_file: str = ""  # the key turnstile signs with, and spotter signs its notice to turnstile with
    database: DatabaseSource = ":memory:"  # resolved identity store; legacy paths remain accepted
    history: str = ":memory:"  # where spotter keeps its readings: a file of its own
    databases: dict = field(default_factory=dict)  # optional named identity/history store bindings
    host: str = "127.0.0.1"
    port: int = 8444
    sample_every: float = 30  # seconds between the readings that are kept
    keep_days: float = 28  # how long they are kept
    data: str = "."  # the folder spotter keeps its readings in, counted from the settings file
    path: str = ""  # the settings file itself
    file: dict = field(default_factory=dict)  # what that file says, before anything changed on the site is laid over it
    says: dict = field(default_factory=dict)  # the comment above each setting in the file, shown beside it on the site
    disks: tuple[str, ...] = ("/",)  # folders whose drives are watched for room
    turnstile_notice: str = ""  # turnstile's /turnstile/power URL, empty disables the notice. Signed with the machine key
    places: dict[str, str] = field(default_factory=dict)  # LoadCoach's allowed model-file destinations
    files_every: float = 10  # minutes between looks through the places for the Files page's index. 0 means only when asked
    file_index: str = ""  # that index, in LoadCoach's data folder; empty keeps none


def load_settings(path: Path) -> Settings:
    """Read the TOML file, and lay over it what was changed on the site. Anything wrong with the file stops startup with a message naming the cause."""
    told = read_settings(path)
    shared = kept_by_loadcoach(path.parent / told.get("loadcoach", "loadcoach.toml"))[1]
    identity = (DatabaseRegistry.from_config(told.get("databases"), path.parent,
                {"spotter.identity": shared, "spotter.history": str(data_folder(path, told) / f"{path.stem}.db")})
                .database("spotter.identity") if told.get("databases") else shared)
    changed = changed_for("spotter", identity)
    try:
        return settings_from(path, laid_over(told, changed, "spotter"), told)
    except SystemExit as problem:
        if not changed:
            raise
        print(f"spotter: a setting changed on the site cannot be used: {problem}. Starting with the file's own settings.", file=sys.stderr, flush=True)
        return settings_from(path, told, told)


def settings_from(path: Path, told: dict, file: dict | None = None) -> Settings:
    """The settings that `told` makes, checked. `file` is what the file itself says, kept to be shown."""
    told = dict(told)
    main, database, token_file = kept_by_loadcoach(path.parent / told.pop("loadcoach", "loadcoach.toml"))
    main = laid_over(main, changed_for("loadcoach", database), "loadcoach")
    here = main.get("host", "127.0.0.1")
    try:
        settings = Settings(
            **{
                "services": {},
                **told,
                "machine_key_file": str(path.parent / main["machine_key_file"]) if main.get("machine_key_file") else "",
                "compute": f"http://{'127.0.0.1' if here in ('0.0.0.0', '::') else here}:{main.get('port', 8442)}",
                "places": dict(model_places(main.get("models_folder", ""), main.get("places", {}), main.get("models_storage_folder", ""))),
                "file_index": str(data_folder(path.parent / told.get("loadcoach", "loadcoach.toml"), main) / NODE_INDEX),
                "token_file": token_file,
                "database": database,
                "history": str(data_folder(path, told) / f"{path.stem}.db"),  # in the data folder: spotter.toml gives spotter.db
                "disks": tuple(told.get("disks", ("/",))),
                "path": str(path),
                "file": told if file is None else file,
                "says": said_in(path),
            }
        )
    except (TypeError, ValueError) as problem:
        raise SystemExit(f"{path}: {problem}") from None
    if settings.sample_every < 1 or min(settings.keep_days, settings.sleep_after, settings.busy_gpu, settings.busy_processor, settings.files_every) < 0:
        raise SystemExit(f"{path}: sample_every is a second or more, and no number is less than nothing")
    for name, unit in [*settings.services.items(), *settings.system_services.items()]:
        if not isinstance(unit, str) or not unit.endswith(".service") or "/" in unit or " " in unit:
            raise SystemExit(f"{path}: service {name!r} must name a unit, such as loadcoach.service")
    if both := settings.services.keys() & settings.system_services.keys():
        raise SystemExit(f"{path}: {sorted(both)[0]!r} is named under both [services] and [system_services]")
    if KERNEL in settings.services.keys() | settings.system_services.keys():
        raise SystemExit(f"{path}: no service can be named {KERNEL!r}. That name is for what the kernel itself said.")
    return settings


# ---- reading the machine ---------------------------------------------------------------------


def number(text: str) -> float | None:
    """A reading as a number, or None if there was none. A fan that reports nothing is not a fan at zero."""
    try:
        return float(text)
    except ValueError:
        return None


async def cards(include_integrated=False) -> list[dict]:
    """Each GPU as nvidia-smi reports it, in nvidia-smi's own order. None at all if it cannot be asked."""
    wanted = "index,name,temperature.gpu,fan.speed,power.draw,utilization.gpu,memory.used,memory.total,power.limit"
    try:
        asked = await asyncio.create_subprocess_exec(
            "nvidia-smi", f"--query-gpu={wanted}", "--format=csv,noheader,nounits", stdout=PIPE, stderr=DEVNULL
        )
        printed, _ = await asyncio.wait_for(asked.communicate(), 10)
    except (OSError, TimeoutError):
        return []
    found = []
    from loadcoach.gpu_type import is_integrated
    for line in printed.decode().splitlines():
        index, name, temperature, fan, power, load, used, total, limit = (part.strip() for part in line.split(","))
        if not include_integrated and is_integrated({'model': name}):
            continue
        megabytes = lambda text: None if number(text) is None else number(text) * 2**20  # noqa: E731
        found.append({"card": int(index), "model": name, "temperature": number(temperature), "fan": number(fan), "power": number(power),
                      "load": number(load), "memory_used": megabytes(used), "memory_total": megabytes(total), "power_limit": number(limit)})  # fmt: skip
    return found


def processor_times() -> tuple[int, int]:
    """How long the processor has spent busy, and in all, since the machine started. Two readings apart give a percentage."""
    spent = [int(part) for part in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    return sum(spent) - spent[3] - spent[4], sum(spent)  # everything but idle and waiting, and everything


def processor_temperature() -> float | None:
    """The processor's own sensor, if the system shows one. AMD names it k10temp, Intel coretemp."""
    for sensor in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (sensor / "name").read_text().strip() in ("k10temp", "coretemp"):
                return int((sensor / "temp1_input").read_text()) / 1000
        except (OSError, ValueError):
            continue
    return None


def memory() -> dict:
    """The machine's memory: how much there is and how much is in use, in bytes."""
    lines = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    total, free = (int(lines[name].split()[0]) * 1024 for name in ("MemTotal", "MemAvailable"))
    return {"used": total - free, "total": total}


async def logged_in_users() -> int | None:
    """How many distinct operating-system users have login sessions, if systemd can say."""
    try:
        asked = await asyncio.create_subprocess_exec(
            "loginctl", "list-users", "--no-legend", "--no-pager", stdout=PIPE, stderr=DEVNULL
        )
        printed, _ = await asyncio.wait_for(asked.communicate(), 5)
        if asked.returncode:
            return None
        return len({parts[1] for line in printed.decode(errors="replace").splitlines() if len(parts := line.split()) >= 2})
    except (OSError, TimeoutError):
        return None


def room(path: str) -> dict:
    """How big the drive under a folder is, and how much of it is free, in bytes."""
    drive = shutil.disk_usage(path)
    return {"path": path, "total": drive.total, "free": drive.free}


def series(reading: dict) -> dict[str, float]:
    """One reading laid flat: a name for each number in it, as a chart of that number needs."""
    flat = {"processor load": reading["processor"]["load"], "processor temperature": reading["processor"]["temperature"],
            "memory used": reading["memory"]["used"]}  # fmt: skip
    for card in reading["cards"]:
        for what in ("memory_used", "load", "temperature", "power", "fan"):
            flat[f"GPU {card['card']} {what.replace('_', ' ')}"] = card[what]
    for disk in reading["disks"]:
        flat[f"free on {disk['path']}"] = disk["free"]
    return {name: value for name, value in flat.items() if value is not None}


def gathered(rows: list[tuple[float, str]], begin: float, end: float, points: int) -> dict:
    """Many readings made into `points` steps across a stretch of time, each step the average of the readings in it."""
    step = max((end - begin) / points, 1)
    sums: dict[str, list[float]] = {}
    counts: dict[str, list[int]] = {}
    for at, data in rows:
        place = min(int((at - begin) / step), points - 1)
        for name, value in json.loads(data).items():
            sums.setdefault(name, [0.0] * points)[place] += value
            counts.setdefault(name, [0] * points)[place] += 1
    # A step with no reading in it is None, not zero: the machine was off, or spotter was.
    return {
        "begin": begin, "end": end, "step": step,
        "series": {name: [round(total / count, 2) if count else None for total, count in zip(sums[name], counts[name])] for name in sums},
    }  # fmt: skip


# ---- the app ---------------------------------------------------------------------------------


def build_app(settings: Settings, client: httpx.AsyncClient | None = None) -> Starlette:
    """Build the web app. `client` is how it reaches LoadCoach; tests hand in their own."""
    databases = DatabaseRegistry.from_config(
        settings.databases,
        Path(settings.path).parent if settings.path else Path.cwd(),
        {"spotter.identity": settings.database, "spotter.history": settings.history},
    )
    identity = databases.database("spotter.identity")
    from loadcoach.component_versions import Versions
    component_settings = Path(settings.path).parent / settings.file.get("loadcoach", "loadcoach.toml") if settings.path else None
    component_source = kept_by_loadcoach(component_settings)[1] if component_settings and component_settings.is_file() else identity
    component_versions = Versions(component_source)
    gate = Gate(settings.path)
    log = Log(identity)
    loadcoach = client or httpx.AsyncClient(base_url=settings.compute, timeout=httpx.Timeout(15, connect=2))
    # Every service that can be acted on, by our name for it: its unit, and whether it is this account's own.
    units = {name: (unit, True) for name, unit in settings.services.items()} | {name: (unit, False) for name, unit in settings.system_services.items()}
    # When the machine was last used, and by what. It is put to sleep when that is long enough ago.
    used = {"at": time.time(), "by": "spotter starting"}
    pending: dict = {}  # the sleep, restart or switching off that is on its way, if one is: what, who asked, since when
    failed: dict = {}  # the last one that could not be done, and what systemd said
    going: list[asyncio.Task] = []  # the task carrying out what is pending
    history = databases.database("spotter.history")
    kept = history.connect(check_same_thread=False, isolation_level=None)
    migrate(history, kept, "spotter.history", SPOTTER)
    last: dict = {}  # the newest reading, and the processor's times when it was made
    changes = Changes(identity)  # what was changed in the settings from the site: see loadcoach/settings.py
    in_force: dict = {}  # the changes spotter has looked at last
    uploads: dict[str, dict] = {}  # short-lived, one-file capabilities issued by a signed prepare request

    def in_step() -> None:
        """Take on what was changed on the site since the last look, and say what spotter runs with. Cheap when nothing was."""
        nonlocal settings
        changed = changes.of("spotter")
        if in_force.get("changed") == changed:
            return
        settings, refused = taken("spotter", settings, changed, settings_from)
        in_force["changed"] = changed
        named = ("host", "port", "sample_every", "keep_days", "sleep_after", "busy_gpu", "busy_processor", "files_every", "services", "system_services")
        running = {**settings.file, **{name: getattr(settings, name) for name in named}, "disks": list(settings.disks)}
        changes.seen("spotter", said(running, settings.file, settings.says), refused)
    reading_now = asyncio.Lock()  # so that two askers at once share one reading

    def error(status: int, message: str) -> JSONResponse:
        """Build a JSON failure response using the service error envelope."""
        return JSONResponse({"error": {"message": message, "type": "spotter_error"}}, status_code=status)

    def address(request: Request) -> str:
        """Return the caller address, or a placeholder when unavailable."""
        return request.client.host if request.client else "?"

    def who(request: Request) -> User | None:
        """Whoever turnstile says is asking, in its signed statement. Nobody else is known here."""
        said = getattr(request.state, "said", None)
        return user_of(said["caller"]) if said and said.get("caller") else None

    def signed_in(handler):
        """Wrap telemetry and control handlers with signed caller authentication."""
        async def checked(request: Request) -> Response:
            """Resolve current caller identity before running the protected Spotter operation."""
            user = who(request)
            return error(401, "Missing or wrong API key.") if user is None else await handler(request, user)

        return checked

    async def read() -> dict:
        """The machine as it is now. A reading under two seconds old is given again instead of made again."""
        async with reading_now:
            if last and time.time() - last["reading"]["at"] < FRESH:
                return last["reading"]
            busy, everything = processor_times()
            before = last.get("times")
            load = round(100 * (busy - before[0]) / max(everything - before[1], 1), 1) if before else None
            from loadcoach.gpu_type import is_integrated
            inventory = await cards(include_integrated=True)
            reading = {
                "at": time.time(),
                "up_for": float(Path("/proc/uptime").read_text().split()[0]),
                "cards": [card for card in inventory if not is_integrated(card)],
                "excluded_gpu_ids": [card['card'] for card in inventory if is_integrated(card)],
                "processor": {"load": load, "temperature": processor_temperature()},
                "memory": memory(),
                "disks": [room(path) for path in settings.disks if Path(path).is_dir()],
                "screen": await at_the_screen(),
                "logged_in_users": await logged_in_users(),
            }
            last.update(reading=reading, times=(busy, everything))
            return reading

    async def coach(method: str, path: str, body: dict | None = None) -> dict | None:
        """Ask LoadCoach something, as a program on its own machine. None if it is not there to ask."""
        try:
            token = Path(settings.token_file).read_text()  # read each time: a restarted LoadCoach leaves a new one
            answer = await loadcoach.request(method, path, json=body, headers={"authorization": f"Bearer {token}"})
            return answer.json() if answer.status_code == 200 else None
        except (OSError, httpx.HTTPError, ValueError):
            return None

    async def note_use(reading: dict) -> None:
        """Move `used` on if the machine is being used now. Anything counted here holds sleep off."""
        now, by = time.time(), None
        status = await coach("GET", "/loadcoach/status")
        if status and status["busy"]:
            by = f"{status['busy']} request{'s' * (status['busy'] != 1)} at LoadCoach"
        elif status and status.get("downloads"):  # nothing of the machine is worked hard by it, and it must still not be put to sleep
            by = "a download at LoadCoach"
        elif status and status.get("batches"):  # requests that were handed in to be answered later, and are waiting their turn
            by = "a batch at LoadCoach"
        elif hot := [card for card in reading["cards"] if (card["load"] or 0) >= settings.busy_gpu]:
            by = f"GPU {hot[0]['card']} at {hot[0]['load']:.0f}%"
        elif (reading["processor"]["load"] or 0) >= settings.busy_processor:
            by = f"the processor at {reading['processor']['load']:.0f}%"
        if by:
            used.update(at=now, by=by)
        # A request or a tool call that came and went between two looks is in the log.
        asked = log.db.execute("SELECT at FROM events WHERE kind IN ('request', 'tool') ORDER BY id DESC LIMIT 1").fetchone()
        if asked and asked[0] > used["at"]:
            used.update(at=asked[0], by="a request at LoadCoach")
        if reading["screen"] is not None and now - reading["screen"] > used["at"]:
            used.update(at=now - reading["screen"], by="the keyboard or mouse")

    async def keep_watch() -> None:
        """Take a reading every so often and keep it. Once a day, let go of those older than is wanted.

        The same round notes whether the machine is in use, and puts it to sleep when it has not
        been for long enough.
        """
        cleared = 0.0
        ahead = time.time() - time.monotonic()  # how far the clock on the wall is ahead of the one that stops while asleep
        while True:
            await gate.wait()
            in_step()
            reading = await read()
            kept.execute("INSERT INTO readings (at, numbers) VALUES (?, ?) ON CONFLICT(at) DO UPDATE SET numbers=excluded.numbers", (reading["at"], json.dumps(series(reading))))
            if time.time() - cleared > 86400:
                kept.execute("DELETE FROM readings WHERE at < ?", (time.time() - settings.keep_days * 86400,))
                cleared = time.time()
            # Time that went by without this program seeing it: the machine was asleep. Waking
            # counts as use, or a machine woken after a long sleep would go straight back.
            slept, ahead = time.time() - time.monotonic() - ahead, time.time() - time.monotonic()
            if slept > 5:
                used.update(at=time.time(), by="waking")
                log.add("power", "woke", slept=round(slept))
            await note_use(reading)
            if settings.sleep_after and not pending and time.time() - used["at"] >= settings.sleep_after * 60:
                begin("sleep", "spotter", "", force=False, unasked=True)
            await asyncio.sleep(settings.sample_every)

    async def keep_files() -> None:
        """Look through LoadCoach's places every `files_every` minutes, so the Files page answers from the index.

        Only files that are new or have changed since the last look are read. A look that fails is
        tried again at the next one.
        """
        index = FileIndex(settings.file_index or None)
        while True:
            await gate.wait()
            in_step()
            every = settings.files_every * 60
            if every and settings.places and settings.file_index:
                kept = index.read()
                if kept is None or kept.get("places") != settings.places or kept.get("stale") or time.time() - kept.get("checked", 0) >= every:
                    try:
                        await asyncio.to_thread(index.scan, settings.places)
                    except OSError as problem:
                        print(f"spotter: the model files could not be looked through: {problem}", file=sys.stderr, flush=True)
            await asyncio.sleep(min(every or 60, 60))  # a change to files_every is taken on within a minute

    async def keep_versions() -> None:
        """Check installed suite and native engine versions every 60 minutes, independently of telemetry."""
        while True:
            await gate.wait()
            try:
                await component_versions.refresh(settings=component_settings)
            except (OSError, ValueError) as problem:
                print(f"spotter: component versions could not be checked: {problem}", file=sys.stderr, flush=True)
            await asyncio.sleep(60)  # persisted timestamps determine which hourly checks are due

    async def systemctl(*arguments: str, own: bool = True) -> tuple[int, str]:
        """Run systemctl and return how it ended and what it said. `own` is for this account's services, otherwise root's.

        Root's are only done if a polkit rule says this account may. Nothing here asks for a password.
        """
        asked = await asyncio.create_subprocess_exec("systemctl", "--user" if own else "--no-ask-password", *arguments, stdout=PIPE, stderr=STDOUT)
        printed, _ = await asyncio.wait_for(asked.communicate(), 60)
        return asked.returncode or 0, printed.decode(errors="replace").strip()

    async def state_of(name: str) -> dict:
        """One service: whether it is running, since when, and how often it has had to be started again."""
        unit, own = units[name]
        ended, shown = await systemctl("show", unit, "-p", "ActiveState,SubState,ActiveEnterTimestamp,NRestarts,MemoryCurrent,Result,InvocationID,StateChangeTimestamp", own=own)
        told = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        if ended:
            told = {}  # command errors cannot be treated as cleanly stopped services
        return {
            "name": name,
            "unit": unit,
            "root": not own,
            "state": told.get("ActiveState", "unknown"),
            "detail": told.get("SubState", ""),
            "since": told.get("ActiveEnterTimestamp", ""),
            "restarts": int(told["NRestarts"]) if told.get("NRestarts", "").isdigit() else None,
            "memory": int(told["MemoryCurrent"]) if told.get("MemoryCurrent", "").isdigit() else None,
            "result": told.get("Result", ""),
            "invocation": told.get("InvocationID", ""),
            "changed": told.get("StateChangeTimestamp", ""),
        }

    async def alert_evidence(request: Request, user: User) -> Response:
        """Telemetry plus bounded, confirmed kernel memory-kill facts without raw journal text."""
        if not user.can('manage-settings') and not user.can('mirror-users'):
            return error(403, 'Operational alert evidence requires manage-settings or mirror-users.')
        reading = json.loads((await now(request, user)).body)
        kills = []
        for service in reading['services']:
            if service['result'] == 'oom-kill':
                kills.append({'key': 'oom:unit:' + service['unit'] + ':' + (service['invocation'] or service['changed']),
                              'active': True, 'manual': True, 'title': 'Memory kill: ' + service['name'],
                              'explanation': f"systemd reports oom-kill for {service['unit']}. Review memory pressure and service output; acknowledgement does not resolve this incident."})
        try:
            deadline = asyncio.get_running_loop().time() + 5

            async def journal(arguments):
                """Read bounded allowlisted journal evidence for the requested monitoring source."""
                process = await asyncio.create_subprocess_exec('journalctl', *arguments, stdout=PIPE, stderr=PIPE)
                try:
                    printed, diagnostic = await asyncio.wait_for(
                        process.communicate(), max(0, deadline - asyncio.get_running_loop().time()))
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    raise
                return process.returncode, printed, diagnostic

            command = ['_TRANSPORT=kernel', '--since', '24 hours ago', '-g',
                       'Out of memory: Killed process|Memory cgroup out of memory: Killed process|oom-kill:',
                       '-n', '100', '--no-pager', '-o', 'json']
            ended, printed, diagnostic = await journal(command)
            empty = printed.strip() in (b'', b'-- No entries --')
            if ended != 0 and not (ended == 1 and empty and not diagnostic.strip()):
                raise ValueError('Journal unavailable')
            if empty:
                # --grep exits 1 when nothing matches. Confirm actual kernel access,
                # since an empty result alone can also mean journal permissions are absent.
                checked, sample, _ = await journal(['_TRANSPORT=kernel', '-n', '1', '--no-pager', '-o', 'json'])
                probe = json.loads(sample) if checked == 0 and sample.strip() else None
                if not isinstance(probe, dict) or probe.get('_TRANSPORT') != 'kernel':
                    raise ValueError('Kernel journal access unconfirmed')
            for line in ([] if empty else printed.decode().splitlines()):
                event = json.loads(line)
                cursor = event['__CURSOR']
                at = int(event['__REALTIME_TIMESTAMP']) / 1_000_000
                kills.append({'key': 'oom:kernel:' + cursor, 'at': at, 'active': True, 'manual': True,
                              'title': 'Kernel memory kill',
                              'explanation': 'The kernel recorded an out-of-memory kill. Review memory pressure and kernel output; acknowledgement does not resolve this incident.'})
            available = True
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, asyncio.TimeoutError):
            available = False
        return JSONResponse({**reading, 'memory kills': kills, 'kernel_available': available})

    async def now(request: Request, user: User) -> Response:
        """The machine as it is: its telemetry, its services, and what is known about its power. For anyone signed in."""
        reading = await read()
        shown = units
        if "system_services" in request.query_params:
            selected = set(filter(None, request.query_params["system_services"].split(",")))
            shown = {name: unit for name, unit in units.items() if unit[1] or name in selected}
        power = {
            "may": user.can("power"),
            "pending": {name: value for name, value in pending.items()} or None,
            "failed": dict(failed) or None,
            "logged_in_users": reading["logged_in_users"],
            "sleep_after": settings.sleep_after,
            "unused_for": round(time.time() - used["at"]),
            "used_by": used["by"],
        }
        from loadcoach.machine_identity import fingerprint, hostname
        return JSONResponse({**reading, "machine_fingerprint": fingerprint(), "hostname": hostname(),
                             "services": [await state_of(name) for name in shown], "may_act": user.can("restart-services"), "power": power})

    async def history(request: Request, user: User) -> Response:
        """Telemetry over a stretch of time, as `points` steps, each the average of what was read in it.

        `end` is seconds since 1970 and is now unless said. `seconds` is how far back from it.
        """
        asked = request.query_params
        try:
            end = float(asked.get("end") or time.time())
            seconds = min(max(float(asked.get("seconds", 3600)), 60), settings.keep_days * 86400)
            points = min(max(int(asked.get("points", 240)), 2), 1000)
            # No step shorter than two readings apart. A finer one would have steps with nothing in
            # them, and the line would break where nothing was wrong.
            points = max(min(points, int(seconds / (2 * settings.sample_every))), 2)
        except ValueError:
            return error(400, "end, seconds and points must be numbers.")
        rows = kept.execute("SELECT at, numbers FROM readings WHERE at >= ? AND at <= ? ORDER BY at", (end - seconds, end)).fetchall()
        # The rows are fetched here. Only the sums, which touch no database, go to another thread.
        return JSONResponse(await asyncio.to_thread(gathered, rows, end - seconds, end, points))

    async def act(request: Request, user: User) -> Response:
        """Start, stop or restart one of the services named in the settings. Nothing else can be asked for."""
        if not user.can("restart-services"):
            return error(403, f"Your group may not {ABILITIES['restart-services']}.")
        name, action = request.path_params["name"], request.path_params["action"]
        if name not in units or action not in ACTIONS:
            return error(404, f"spotter knows the services {', '.join(units)} and the actions {', '.join(ACTIONS)}.")
        # The unit comes from spotter's settings and the action from its own list. Neither is what the caller sent.
        unit, own = units[name]
        ended, said = await systemctl(action, unit, own=own)
        log.add("service", action, user.name, address(request), name=name, unit=unit, worked=ended == 0, said=said[:300])
        if ended:
            return error(502, f"systemctl could not {action} {name}: {said[:300]}")
        return JSONResponse(await state_of(name))

    async def journal(request: Request, user: User) -> Response:
        """Lines a service printed: the last of them, or with `since` and `until`, the last of a stretch of time.

        The name `kernel` is not a service. It gives what the kernel said about the cards and
        about running out of memory. Lines can hold more than telemetry does, so they need the
        ability to act.
        """
        if not user.can("restart-services"):
            return error(403, f"Your group may not {ABILITIES['restart-services']}.")
        name, asked = request.path_params["name"], request.query_params
        if name != KERNEL and name not in units:
            return error(404, f"spotter knows the services {', '.join(units)}, and {KERNEL}.")
        try:
            lines = min(max(int(asked.get("lines", 100)), 1), 5000)
            # journalctl reads @ and a number as seconds since 1970, which is how every moment is kept here.
            span = [f"--{edge}=@{float(asked[edge]):.0f}" for edge in ("since", "until") if edge in asked]
        except ValueError:
            return error(400, "lines is a whole number, and since and until are moments in seconds.")
        if name == KERNEL:
            which = KERNEL_LINES
        else:
            unit, own = units[name]
            which = ("--user", "-u", unit) if own else ("-u", unit)
        command = ['journalctl', *which, *span, '-n', str(lines), '--no-pager', '-o', 'short-iso']
        asked = await asyncio.create_subprocess_exec(*command, stdout=PIPE, stderr=STDOUT)
        printed, _ = await asyncio.wait_for(asked.communicate(), 30)
        lines = printed.decode(errors="replace").splitlines()
        # Asked to pick lines out with -g, journalctl gives the newest first. Everything else is oldest first, so these are turned round.
        return JSONResponse({"name": name, "lines": lines[::-1] if name == KERNEL else lines, 'command': shlex.join(command)})

    def begin(action: str, by: str, caller: str, force: bool, unasked: bool = False) -> None:
        """Set a sleep, restart or switching off on its way. `go` carries it out, and can be called off until it has."""
        pending.update(action=action, by=by, since=time.time(), forced=force, waiting_for=None)
        failed.clear()
        going[:] = [asyncio.create_task(go(action, by, caller, force, unasked))]

    async def go(action: str, by: str, caller: str, force: bool, unasked: bool) -> None:
        """Carry out what is pending: let LoadCoach empty first unless told not to, then hand the machine to systemd."""
        word, why = POWER[action]
        try:
            if not force:
                # No new requests from here on. Those already at LoadCoach are finished first.
                await coach("POST", "/loadcoach/drain", {"why": why})
                while (status := await coach("GET", "/loadcoach/status")) and status["busy"]:
                    pending["waiting_for"] = status["busy"]
                    await asyncio.sleep(2)
                pending["waiting_for"] = 0
                if action == "sleep":
                    # With nothing loaded there is no video memory for the driver to save, so sleep
                    # is quick. After waking, a model is loaded again when it is first asked for.
                    await coach("POST", "/loadcoach/unload")
                    await coach("POST", "/loadcoach/drain", {"on": False})  # so that it wakes ready to take requests
            # A sleep nobody asked for gives way to anything that has asked the system not to
            # sleep. One that was asked for does not: whoever asked was told who is at the screen.
            if settings.turnstile_notice and settings.machine_key_file:
                try:
                    body = json.dumps({"action": action}).encode()
                    notice, _ = statement.make(statement.read_key(settings.machine_key_file), "POST", urlsplit(settings.turnstile_notice).path, body, machine="compute")
                    async with httpx.AsyncClient(timeout=1) as turnstile_client:
                        await turnstile_client.post(settings.turnstile_notice, content=body, headers={statement.HEADER: notice, "content-type": "application/json"})
                except (OSError, httpx.HTTPError):
                    pass  # turnstile will truthfully say not known if it missed the notice
            ended, said = await systemctl(word, f"--check-inhibitors={'yes' if unasked else 'no'}", own=False)
            log.add("power", action, by, caller, worked=ended == 0, forced=force, unasked=unasked, said=said[:300])
            if ended:
                failed.update(action=action, said=said[:300], at=time.time())
            if action == "sleep":
                # Asked for or not, done or not, the count starts again. A sleep that systemd
                # would not do is then tried again after another full wait, not every half minute.
                used.update(at=time.time(), by="being put to sleep")
            elif not ended:
                await asyncio.sleep(120)  # the machine is going down, and this program with it
        finally:
            pending.clear()
            await coach("POST", "/loadcoach/drain", {"on": False})

    async def power(request: Request, user: User) -> Response:
        """Put the machine to sleep, restart it or switch it off, or call off whichever of those is on its way.

        The body may set `force`, which does it at once whatever LoadCoach is doing.
        """
        if not user.can("power"):
            return error(403, f"Your group may not {ABILITIES['power']}.")
        action = request.path_params["action"]
        if action == "cancel":
            if not pending:
                return error(409, "Nothing is on its way.")
            log.add("power", "called-off", user.name, address(request), action=pending["action"])
            going[0].cancel()
            await asyncio.wait(going)
            return JSONResponse({"pending": None})
        if action not in POWER:
            return error(404, f"spotter can do these to the machine: {', '.join(POWER)}.")
        try:
            asked = json.loads(await request.body() or b"{}")
            force = asked.get("force") is True
        except (ValueError, AttributeError):
            return error(400, "The body must be a JSON object.")
        if pending:
            return error(409, f"The machine is already going to {pending['action']}, asked by {pending['by']}. Call that off first.")
        begin(action, user.name, address(request), force)
        return JSONResponse({"pending": dict(pending)}, status_code=202)

    async def prepare_file(request: Request, user: User) -> Response:
        """Authorize one bounded model-file upload into a configured LoadCoach place."""
        if not user.can("manage-models"):
            return error(403, f"Your group may not {ABILITIES['manage-models']}.")
        try:
            asked = json.loads(await request.body())
            place, relative, size, digest = asked["place"], asked["relative"], asked["size"], asked["sha256"]
        except (ValueError, TypeError, KeyError):
            return error(400, "place, relative, size and sha256 are required.")
        parts = Path(relative).parts if isinstance(relative, str) else ()
        from mirrorwall.assets import supported
        depth = 6 if place in settings.places and Path(settings.places[place]).name in ("image", "audio", "encoders") else 2
        if (place not in settings.places or not 1 <= len(parts) <= depth or Path(relative).is_absolute()
                or any(part in ("", ".", "..") for part in parts) or not supported(str(Path(settings.places.get(place, "/")) / relative))):
            return error(400, "The destination must be a supported model file inside a configured place.")
        if type(size) is not int or size < 1 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return error(400, "size must be positive and sha256 must be a lowercase SHA-256 digest.")
        root = Path(settings.places[place]).resolve()
        if not root.is_dir():
            return error(409, f"The place {place!r} is not available on this node.")
        target = root.joinpath(*parts)
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            return error(400, "The destination leaves its configured place.")
        if target.exists():
            return error(409, f"{place} already has {relative}.")
        now = time.time()
        for expired in [token for token, transfer in uploads.items() if transfer["until"] < now and not transfer.get("receiving")]:
            uploads.pop(expired, None)
        if any(transfer["target"] == target for transfer in uploads.values()):
            return error(409, "That file is already being received.")
        reserved = sum(transfer["size"] for transfer in uploads.values()
                       if transfer.get("filesystem") == os.stat(root).st_dev)
        if room(str(root))["free"] < size + reserved + SPARE:
            return error(409, f"{place} does not have enough free space while leaving 1 GiB spare.")
        token = secrets.token_urlsafe(32)
        uploads[token] = {"target": target, "size": size, "sha256": digest, "by": user.name,
                          "caller": address(request), "until": now + 900, "filesystem": os.stat(root).st_dev}
        return JSONResponse({"upload": f"/spotter/files/{token}"}, status_code=201)

    async def receive_file(request: Request) -> Response:
        """Receive one prepared file, verify its exact size and hash, then expose it atomically."""
        token = request.path_params["token"]
        transfer = uploads.get(token)
        if transfer is None or transfer.get("receiving") or transfer["until"] < time.time():
            return error(404, "This file transfer is not prepared or has expired.")
        transfer["receiving"] = True
        target = transfer["target"]
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        digest, received = hashlib.sha256(), 0
        try:
            with open(part, "wb") as file:
                async for piece in request.stream():
                    received += len(piece)
                    if received > transfer["size"]:
                        raise ValueError("more bytes arrived than were prepared")
                    digest.update(piece)
                    file.write(piece)
                file.flush()
                os.fsync(file.fileno())
            if received != transfer["size"]:
                raise ValueError(f"received {received} of {transfer['size']} bytes")
            if digest.hexdigest() != transfer["sha256"]:
                raise ValueError("SHA-256 did not match")
            os.link(part, target)
            part.unlink()
            log.add("file", "received", transfer["by"], transfer["caller"], path=str(target), size=received,
                    sha256=transfer["sha256"])
            if settings.file_index:
                await asyncio.to_thread(FileIndex(settings.file_index).update, [target], settings.places)
            return JSONResponse({"path": str(target), "size": received, "sha256": transfer["sha256"]}, status_code=201)
        except (OSError, ValueError) as problem:
            part.unlink(missing_ok=True)
            log.add("file", "receive-failed", transfer["by"], transfer["caller"], path=str(target), why=str(problem))
            return error(422, f"The file was not installed: {problem}.")
        finally:
            uploads.pop(token, None)
            part.unlink(missing_ok=True)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        """Start background workers and release service resources on shutdown."""
        watching, filing, versioning = (asyncio.create_task(work()) for work in (keep_watch, keep_files, keep_versions))
        log.add("spotter", "started", services=list(units), sleep_after=settings.sleep_after)
        yield
        for task in [watching, filing, versioning, *going]:
            task.cancel()
        await asyncio.wait({watching, filing, versioning, *going})
        component_versions.db.close()
        await loadcoach.aclose()
        log.add("spotter", "stopped")

    def route(path: str, handler, method: str = "GET") -> Route:
        """Register the handler behind the service authentication wrapper."""
        return Route(path, signed_in(handler), methods=[method])

    app = Starlette(
        lifespan=lifespan,
        routes=[
            route("/spotter/now", now),
            route("/spotter/history", history),
            route("/spotter/services/{name}/journal", journal),
            route("/spotter/alert-evidence", alert_evidence),
            route("/spotter/services/{name}/{action}", act, "POST"),
            route("/spotter/power/{action}", power, "POST"),
            route("/spotter/files/prepare", prepare_file, "POST"),
            Route("/spotter/files/{token}", receive_file, methods=["PUT"]),
        ],
    )
    app.state.databases = databases
    app.add_middleware(Barrier, gate=gate)
    app.add_middleware(Statements, key_file=settings.machine_key_file)  # who each caller is, as turnstile signed it
    observation.instrument(app, 'spotter')  # IDs only; retained telemetry keeps its existing authority.
    return app


cli = typer.Typer(**CLI)


@cli.command()
def serve(config: Annotated[Path, typer.Option(metavar="FILE", help="The settings file.")] = Path("settings/spotter.toml")) -> None:
    # Each paragraph is one line here, because Typer keeps the line breaks it is given.
    """spotter: the helper on compute. It watches the machine and can revive what runs on it.

    It reads the cards, the processor, the memory and the drives, and keeps four weeks of that in spotter.db beside its settings. It says whether the services named in its settings are up, and starts, stops and restarts them. It puts the machine to sleep, restarts it and switches it off, after LoadCoach has finished what it has, and puts it to sleep by itself when nothing has used it for long enough.

    It has no commands. It runs until stopped, normally as a service: systemctl --user status spotter

    Who may ask it for what is turnstile's to say, on the front machine: callers ask through it, with their keys. Root's services, sleep, restart and switching off also need the polkit rule in spotter.rules.

    Every setting is explained in settings/examples/spotter.toml and settings/defaults/spotter.toml.
    """
    settings = load_settings(config)
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, access_log=False)


def main() -> None:
    """Run configured machine telemetry sampling and protected control endpoints."""
    cli()
