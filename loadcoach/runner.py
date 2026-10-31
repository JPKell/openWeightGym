"""The line and the model servers: who goes next, and what is loaded on which cards.

A card is room, counted in MiB. A model that was measured takes what it measured, so several
small models share a card. One that was never measured takes a whole card the first time, and
is measured there. `place` decides, and the notes above it say how.

Nothing in this file knows about the web. Requests arrive as tickets, and leave either with a
model server to talk to or with a `Refusal` saying why not.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import socket
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from itertools import combinations
from typing import Callable

import httpx

from loadcoach.config import Config
from loadcoach.models import Models, at_context, base, contexts, kin_base
from loadcoach.waiting import estimate
from loadcoach.host_memory import memory_snapshot, resident_mib
from loadcoach.processes import ProcessGroup, adopt_orphans
from loadcoach.engine_memory import MemoryGroup

# A place in line is decided by rank first, then by arrival. A request to take the lock
# outranks everything; the other three are chosen by the caller.
RANK = {"lock": 0, "high": 1, "normal": 2, "low": 3}
CARD_NUMBER = re.compile(r"\d+$")  # the card llama-server calls CUDA0 is the one nvidia-smi calls 0. See `start`
LOOK_EVERY = 5  # seconds that what nvidia-smi said is believed, before it is asked again



# Compatibility exports for existing callers; implementations live with the engine.
from engines import engine_for
from engines.llama_cpp.adapter import (
    command_line as command_line, server_environment as server_environment, engine_stamp,
)


def free_port() -> int:
    """Ask the operating system for a port nobody is using."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Refusal(Exception):
    """A request cannot go ahead. Carries the HTTP status and a message safe to show the caller."""

    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        """Initialize the failure message and its status metadata."""
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ConfiguredRamCapacity(Refusal):
    """Measured CPU resident RAM exceeded the operator's fixed model budget."""


class ServiceRamCapacity(Refusal):
    """An isolated CPU probe exhausted its own worker unit's service RAM ceiling."""

    def __init__(self, model: str, used_mib: int, limit_mib: int) -> None:
        """Initialize the failure message and its status metadata."""
        super().__init__(503, f"{model} exhausted this worker's service RAM ceiling during its native probe.", 10)
        self.used_mib, self.limit_mib = used_mib, limit_mib


@dataclass(eq=False)
class Lock:
    """The GPU held for one caller. Only requests carrying its token are let through."""

    token: str
    user: str  # who took it
    caller: str  # the address they took it from
    seconds: float  # how long it was asked for
    refuse: bool  # turn other callers away at once instead of letting them wait in line
    unload: bool  # stop the loaded model when the lock is taken
    until: float = 0.0  # when it runs out; set when it is granted


@dataclass(eq=False)
class Server:
    """One inference-engine process serving a model on its reserved GPU or CPU resources."""

    model: str
    cards: tuple[str, ...]  # the GPUs it occupies; empty for a model that runs on the CPU
    settings: dict = field(default_factory=dict)  # the model's settings as they were when it was started
    process: asyncio.subprocess.Process | None = None
    url: str = ""
    ready: bool = False  # False while the model is still loading
    used: float = 0.0  # when a request last started on it; the longest idle is replaced first
    mib: list[int] | None = None  # what the model measured on each of its cards, in their order. None until it has been measured
    rss_mib: int | None = None  # current measured resident host RAM, including child processes
    rss_peak_mib: int | None = None
    image_profile: dict | None = None
    packed: bool = False  # True if other models were on its cards when it began to load
    build: str = ""  # the build of llama-server that runs it, in that program's own words
    said: bytes = b""  # the last few KB the process printed, kept to explain a failure
    listener: asyncio.Task | None = None  # the task that reads what it prints

    configured: dict = field(default_factory=dict)  # requested maximum and minimum; settings holds the context actually loaded
    idle_since: float = field(default_factory=time.monotonic)  # reset after the last active request finishes
    calibrating: bool = False  # reserves whole cards until all context measurements are finished
    probing: bool = False  # native loading finished; reserved for an isolated probe, not ordinary requests
    health_path: str = "/health"
    stop_seconds: float = 10
    health_interval: float = .2  # benchmarks use a finer readiness poll so short loads can be distinguished
    identity: str | None = None  # loaded catalogue entry; deleting/recreating its label cannot inherit the process
    owned_group: ProcessGroup | None = None
    memory_group: MemoryGroup | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    runtime_metadata: dict = field(default_factory=dict)  # observed capabilities for this process only
    stopping: asyncio.Task | None = field(default=None, repr=False)
    launch_cleanup: Callable[[], None] | None = field(default=None, repr=False)

    @property
    def state(self) -> str:
        """Describe isolated work separately from readiness to serve ordinary requests."""
        if self.stopping is not None:
            return "stopping"
        return "probing" if self.probing else "ready" if self.ready else "loading"

    def dead(self) -> bool:
        """True if the process was started and has since exited."""
        return self.process is not None and self.process.returncode is not None

    def output(self) -> str:
        """The last of what the process printed, as text. It is kept with a failure, in the entry's `text`, so that all of it can be read later."""
        return self.said.decode(errors="replace")

    def last_words(self) -> str:
        """Why the process says it failed, in its own words.

        `llama-server` marks its error lines with " E ". The first one is usually the cause and
        the rest follow from it. A mistake on the command line is reported differently, as a line
        starting "error:". Failing both, the last thing it printed is the best there is.
        """
        return engine_for(self.settings).failure_reason(self.output())


@dataclass(eq=False)
class Ticket:
    """One request's place in line."""

    model: str | None  # None for a request to unload or to take the lock
    user: str  # who is asking
    caller: str  # the address the request came from
    priority: str  # a key of RANK
    token: str | None = None  # the lock token the request carries, if any
    wants: Lock | None = None  # the lock this ticket is asking for, if that is what it is
    may_load: bool = True  # False if the user may only use a model that is already loaded
    entry: int = 0  # the id of the request's entry in the event log, which is how it is pointed at
    calibrate: bool = False  # the measurement run may reload an idle server with an incomplete curve
    settings: dict | None = None  # the model's settings when it joined; used only if the model is removed meanwhile
    since: float = field(default_factory=time.monotonic)  # joined the line, then started running
    turn: asyncio.Event = field(default_factory=asyncio.Event)  # set when this request may go
    error: Exception | None = None  # set instead of a turn if it cannot go ahead
    server: Server | None = None  # the model server it was let onto
    stop_handle: str | None = None  # volatile handle for an unlogged request
    observe: Callable[[str], None] | None = None  # diagnostic stage only; never affects admission
    work: dict | None = None  # inferred prompt/output counts only; None for administrative work
    validate_settings: Callable[[dict], None] | None = field(default=None, repr=False)
    estimate: dict | None = None  # the admission snapshot, never a resource reservation


class Runner:
    """Owns engine processes, their GPU/CPU reservations, the admission queue and the lock.

    `servers` is what is loaded, one per model. `waiting` is the line, best rank first, then first
    come first served. `running` is the requests being answered right now. `dispatch` is the only
    code that moves a request from waiting to running, decides which model goes on which cards,
    or grants the lock.
    """

    def __init__(self, config: Config, client: httpx.AsyncClient, note=None, models: Models | None = None) -> None:
        """Initialize admission queues, resident servers and resource reservations."""
        self.config = config
        self.client = client
        # Where the models' settings are read from, afresh each time they are needed, so that a
        # change to a model takes effect without anything being restarted.
        self.models = models or Models(config.database, config.model_defaults, config.models)
        # Called as note(kind, what, ...) for the things only the runner sees happen, such as a
        # model finishing loading. LoadCoach passes the event log's `add`. Without one, nothing.
        self.note = note or (lambda *_, **__: None)
        self.servers: dict[str, Server] = {}
        self.retiring: set[Server] = set()  # removed/replaced names still owning memory/children
        self.loads: set[asyncio.Task] = set()  # models being brought up right now
        self.waiting: list[Ticket] = []
        self.running: list[Ticket] = []
        self.holds: dict[tuple[str, str], float] = {}  # (model, account) -> monotonic expiry
        self.loadout_leases: dict[str, dict] = {}  # independent expiring administrative set holds, by stable model IDs
        self.lock: Lock | None = None  # read it through holder(), which drops it once it runs out
        self.wake = asyncio.Event()  # set whenever something changed that dispatch should look at
        # What nvidia-smi last said, and when: {"cards": {number: {"total", "used"}}, "pids": {pid: {number: MiB}}}.
        # None if it could not be asked, and then no card is shared.
        self.seen: dict | None = None
        self.seen_at = 0.0
        self._gpu_members_seen = None
        self._gpu_members_at = 0.0
        self._gpu_members = {}  # owned session membership for this GPU reading only
        self.builds: dict = {}  # engine/install-specific version caches
        self.build: tuple[list | None, str] | None = None  # the date of the llama-server program when it was last asked its build, and what it said
        self.paces = lambda: {}  # measured timing facts; supplied by the application's existing cache
        self.admitting = 0  # foreground admission preparation; isolated probes yield before it queues

    async def acquire(self, ticket: Ticket) -> Ticket:
        """Put a ticket in line and wait for its turn. Afterwards it must be given to `release`."""
        if ticket.model and (settings := self.models.get(ticket.model)):
            engine = engine_for(settings)
            if engine.id=='sdcpp':
                await self.version(settings)
            if not engine.installation(self.config).enabled:
                raise Refusal(503, f"{engine.label} is disabled on this compute node.")
            if hasattr(engine, "check_available"):
                try:
                    engine.check_available(self.config, settings)
                except (ValueError, OSError) as problem:
                    raise Refusal(503, str(problem)) from None
            if ticket.validate_settings:
                ticket.validate_settings(settings)
        if ticket.work is not None:
            ticket.estimate = estimate(self, ticket, self.paces())
        lock = self.holder()
        if ticket.token and (lock is None or ticket.token != lock.token):
            raise Refusal(409, "This lock has run out or was released.")
        if lock and lock.refuse and ticket.token != lock.token:
            raise self.locked_out(lock)

        if ticket.work is not None and self.config.max_estimated_wait:
            if not ticket.may_load and self.serving(ticket.model) is None:
                raise Refusal(403, f"{ticket.model} is not loaded, and your group may not load models.")
            # A wait that cannot be estimated is admitted (owner, 2026-10-15): otherwise a model never
            # measured could not be measured without taking the limit off. Only a known estimate over it is refused.
            seconds = ticket.estimate['seconds']
            if seconds is not None and seconds > self.config.max_estimated_wait:
                raise Refusal(503, f"Not admitted: Estimated wait {seconds:g} seconds exceeds the {self.config.max_estimated_wait:g}-second limit.",
                              max(1, min(3600, math.ceil(seconds))))

        self.waiting.append(ticket)
        self.waiting.sort(key=lambda waiting: RANK[waiting.priority])  # stable: arrival order kept
        self.wake.set()
        try:
            await asyncio.wait_for(ticket.turn.wait(), self.config.max_wait)
        except BaseException as problem:  # waited too long, or the caller hung up
            if ticket in self.waiting:
                self.waiting.remove(ticket)
                self.wake.set()
                if isinstance(problem, TimeoutError):
                    raise Refusal(
                        503, f"Still in line after {self.config.max_wait} seconds. Try again.", 10
                    ) from None
                raise
            if not isinstance(problem, TimeoutError):  # got its turn in the same instant
                self.release(ticket)
                if ticket.wants and self.lock is ticket.wants:
                    self.lock = None  # nobody is there to use the lock it was just given
                raise
        if ticket.error:
            raise ticket.error
        return ticket

    def release(self, ticket: Ticket) -> None:
        """This request is done with the model. Not async, so it also works mid-cancellation."""
        if ticket in self.running:
            self.running.remove(ticket)
            if ticket.server and not self.busy(ticket.server):
                ticket.server.idle_since = time.monotonic()
            self.wake.set()

    def room_for(self, settings: dict) -> int:
        """How many requests a model takes at once: its `parallel` flag, or one."""
        return engine_for(settings).model.parallel_capacity(settings)

    def needs(self, settings: dict) -> int | list[str]:
        """The required cards. Automatic placement starts with one; unsaid keeps the legacy all."""
        return engine_for(settings).model.required_devices(settings, self.cards)

    @property
    def cards(self) -> tuple[str, ...]:
        """Only enabled devices may be handed to a model, including probes and reloads."""
        return tuple(card for card in self.config.gpus if card not in self.config.disabled_gpus)

    def layouts(self, settings: dict) -> list[dict]:
        """Keep explicit placements; for any, try one card and optionally a two-card split."""
        return engine_for(settings).model.layouts(settings, self.cards)

    def measured_settings(self, model: str, settings: dict | None = None) -> dict:
        """Select one memory curve for display and measuring, keeping different layouts apart."""
        settings = settings or self.models.get(model)
        server = self.serving(model)
        if server and self.answers(server, settings):
            return server.settings
        layouts = self.layouts(settings)
        return next((one for one in layouts if any(point["mib"] for point in self.models.measurements(model, one))),
                    layouts[0] if layouts else settings)

    def impossible_layout(self, model: str, settings: dict) -> bool:
        """Rule out failed calibrations or a single-card layout beyond physical capacity.

        Busy cards and memory used by other programs do not make a layout impossible: those
        requests still wait. File size only supplies a lower bound for full GPU offload.
        """
        points = self.models.measurements(model, settings)
        if self.models.calibrated(model, settings) and not any(point["mib"] for point in points):
            return True
        if settings["gpus"] != 1 or not self.cards:
            return False
        minimum = next((point for point in points if point["context"] == contexts(settings)[0] and point["mib"]), None)
        lower = 0
        offload = settings["flags"].get("gpu-layers", settings["flags"].get("n-gpu-layers"))
        if offload in ("all", -1, "-1"):
            try:
                lower = sum(os.path.getsize(path) for path in (settings["flags"].get("model"), settings["flags"].get("mmproj")) if path) / 2**20
            except OSError:
                pass
        required = minimum["mib"][0] if minimum else lower
        return all(self.known(card) and required > self.known(card)["total"] - self.config.card_margin for card in self.cards)

    def busy(self, server: Server) -> int:
        """How many requests are being answered by this server right now."""
        return sum(ticket.server is server for ticket in self.running)

    def free_now(self, model: str, token: str | None = None) -> bool:
        """True if a request for `model` would be answered at once, with no waiting at all.

        That needs the model loaded as it is now set up, with room to spare, nobody in line, and
        no lock held by someone else. `token` is the lock token the request carries, if any.
        """
        server, lock = self.serving(model), self.holder()
        return bool(
            server
            and server.ready
            and self.answers(server, self.models.get(model) or {})
            and self.busy(server) < self.room_for(server.settings)
            and not self.waiting
            and (lock is None or lock.token == token)
        )

    # ---- kin: one server for models that differ only in their adapters and their projector -----
    #
    # A server is started with adapters and is told for each request which of them apply. So one
    # server can answer for a plain model and for every model that is the same but for its
    # adapters, as long as it was started with the adapters asked for. A projector is the same
    # kind of thing: a server that has it answers for the model that sees and for the one that
    # does not, since it changes nothing about an answer to words alone. It is kept under the name
    # of the model it was loaded for. When one of its kin needs an adapter it was not started
    # with, it is loaded afresh with that one as well as those it had.

    def serving(self, model: str, settings: dict | None = None) -> Server | None:
        """The server that answers for a model: the one loaded under its name, or one loaded for kin of it. None if there is neither."""
        if model in self.servers:
            server, current = self.servers[model], self.models.identity(model)
            if server.identity and current and server.identity != current:
                return None  # a recreated label cannot inherit the old identity's loaded process
            return server
        settings = settings or self.models.get(model)
        return next((server for server in self.servers.values() if kin_base(server.configured or server.settings) == kin_base(settings)), None) if settings else None

    def lacks(self, server: Server, settings: dict) -> list[dict]:
        """The adapters a model has that a server was not started with. An adapter is known by its file."""
        loaded = {adapter["file"] for adapter in server.settings.get("adapters", {}).values()}
        return [adapter for adapter in settings.get("adapters", {}).values() if adapter["file"] not in loaded]

    def model_metadata(self, name: str) -> dict:
        """Return observed metadata only for a ready process matching current settings."""
        settings = self.models.get(name)
        server = self.serving(name, settings) if settings else None
        if server and server.ready and not server.dead() and self.answers(server, settings):
            return server.runtime_metadata
        return {}

    def sees(self, server: Server, settings: dict) -> bool:
        """True unless a model has a projector that a server was not started with. A model with none is answered by a server with or without one."""
        wanted = settings["flags"].get("mmproj")
        return wanted is None or server.settings["flags"].get("mmproj") == wanted

    def answers(self, server: Server, settings: dict) -> bool:
        """True if a server can answer for a model as it is set up now: started the same way, with every adapter the model has, and with its projector if it has one."""
        return server.stopping is None and kin_base(server.configured or server.settings) == kin_base(settings) and not self.lacks(server, settings) and self.sees(server, settings)

    def pinned(self, server: Server) -> bool:
        """True if a model that this server answers for is held, so that the server must stay as it is."""
        self.held()
        leased = any(server is held for lease in self.loadout_leases.values() for held in lease['_servers'].values())
        return leased or self.answers_for(server, [name for name, _ in self.holds]) or self.locked(server)

    def answers_for(self, server: Server, names: list[str]) -> bool:
        """True if this server was loaded as one of these models, or as kin of one."""
        return any(name == server.model or kin_base(self.models.get(name) or {}) == kin_base(server.configured or server.settings) for name in names)

    def locked(self, server: Server) -> bool:
        """True if a model that this server answers for is locked in place. Unlike a hold it has no end and is not counted as work."""
        return self.answers_for(server, self.models.locked())

    def held(self, model: str | None = None) -> bool:
        """Expire abandoned holds and say whether this model (or any model) is held."""
        now = time.monotonic()
        for identity, until in list(self.holds.items()):
            if until <= now:
                del self.holds[identity]
                self.note("hold", "ran-out", identity[1], model=identity[0])
                self.wake.set()
        for identity, lease in list(self.loadout_leases.items()):
            if lease['until'] <= now:
                del self.loadout_leases[identity]
                self.note('loadout', 'expired', lease['user'], loadout=lease['name'])
                self.wake.set()
        return (any(name == model or model is None for name, _ in self.holds) or
                any(model is None or self.models.identity(model) in lease['models'] for lease in self.loadout_leases.values()))

    def hold(self, model: str, user: str, seconds: float) -> None:
        """Pin a loaded model for one account without occupying an inference slot."""
        self.holds[model, user] = time.monotonic() + seconds
        asyncio.get_running_loop().call_later(seconds, self.wake.set)
        self.wake.set()

    def unhold(self, model: str, user: str) -> None:
        """Release only this account's interest; other callers retain theirs."""
        self.holds.pop((model, user), None)
        self.wake.set()

    def holder(self) -> Lock | None:
        """The lock in force, or None. A lock that has run out is dropped here."""
        if self.lock and time.monotonic() >= self.lock.until:
            self.note("lock", "ran-out", self.lock.user)
            self.lock = None
            self.wake.set()
        return self.lock

    def locked_out(self, lock: Lock) -> Refusal:
        """Return whether the active exclusive lock excludes this caller."""
        left = max(1, round(lock.until - time.monotonic()))
        return Refusal(503, f"The GPUs are locked by {lock.user} for up to {left} more seconds.", min(left, 60))

    def renew(self, token: str, seconds: float) -> bool:
        """Give the holder a fresh `seconds` on its lock. False if it no longer holds one."""
        lock = self.holder()
        if lock is None or lock.token != token:
            return False
        lock.seconds, lock.until = seconds, time.monotonic() + seconds
        asyncio.get_running_loop().call_later(seconds, self.wake.set)
        return True

    def unlock(self, token: str) -> bool:
        """Let go of the lock. False if `token` does not hold it."""
        lock = self.holder()
        if lock is None or lock.token != token:
            return False
        self.lock = None
        self.wake.set()
        return True

    def break_lock(self) -> None:
        """End whatever lock is in force, whoever holds it."""
        self.lock = None
        self.wake.set()

    def clear(self, by: str) -> int:
        """Turn away every request waiting in line. Requests being answered are left alone."""
        turned_away = list(self.waiting)
        for ticket in turned_away:
            self.waiting.remove(ticket)
            ticket.error = Refusal(503, f"Removed from the line by {by}.", 10)
            ticket.turn.set()
        return len(turned_away)

    def remove(self, ticket: Ticket, by: str) -> bool:
        """Take one ticket out of the line. False if it is not waiting there any more."""
        if ticket not in self.waiting:
            return False
        self.waiting.remove(ticket)
        ticket.error = Refusal(400, f"Stopped by {by}.")
        ticket.turn.set()
        self.wake.set()
        return True

    def count(self, user: str) -> int:
        """How many requests this user has in line or being answered."""
        return sum(ticket.user == user and ticket.model is not None for ticket in self.waiting + self.running)

    def grant(self, lock: Lock) -> None:
        """Put `lock` in force. Called by dispatch, once nothing is running."""
        lock.until = time.monotonic() + lock.seconds
        self.lock = lock
        # Nothing may happen in between, so make sure dispatch looks again when the lock runs out.
        asyncio.get_running_loop().call_later(lock.seconds, self.wake.set)
        if lock.refuse:
            for ticket in [ticket for ticket in self.waiting if ticket.token != lock.token]:
                self.waiting.remove(ticket)
                ticket.error = self.locked_out(lock)
                ticket.turn.set()

    # ---- the cards as room ---------------------------------------------------------------------
    #
    # What a card can hold is what nvidia-smi says it has, less a margin, less what programs
    # other than LoadCoach's model servers use of it. What a model takes is what it measured.
    # Three kinds of model take a whole card and share it with nobody:
    #
    #   one never measured          so that its first load is the measuring, with nothing beside it
    #   one set `alone`             because its owner said so
    #   one on a card nvidia-smi does not know, or with nvidia-smi not answering
    #
    # A model with an automatic multi-card split loads onto empty cards: its division depends
    # on the room it finds. An explicit tensor-split can share using its per-card measurements.
    # Once loaded, small models can also use what it left.

    async def smi(self, query: str) -> list[list[str]]:
        """One question to nvidia-smi, answered as rows of cells. Raises OSError if it cannot be asked."""
        asked = await asyncio.create_subprocess_exec(
            self.config.nvidia_smi, query, "--format=csv,noheader,nounits", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        printed, _ = await asyncio.wait_for(asked.communicate(), 10)
        if asked.returncode:
            raise OSError(f"{self.config.nvidia_smi} ended with {asked.returncode}")
        return [[cell.strip() for cell in line.split(",")] for line in printed.decode().splitlines() if line.strip()]

    async def look(self, fresh: bool = False) -> None:
        """Ask nvidia-smi what each card holds and what each process uses of it, unless that was asked a moment ago. Kept in `seen`."""
        if not fresh and time.monotonic() - self.seen_at < LOOK_EVERY:
            return
        try:
            cards = await self.smi("--query-gpu=index,uuid,memory.total,memory.used")
            numbers = {uuid: int(number) for number, uuid, _, _ in cards}
            seen: dict = {"cards": {int(number): {"total": int(total), "used": int(used)} for number, _, total, used in cards}, "pids": {}}
            for pid, uuid, mib in await self.smi("--query-compute-apps=pid,gpu_uuid,used_memory"):
                if mib.isdigit() and uuid in numbers:  # for some processes it says [N/A]
                    seen["pids"].setdefault(int(pid), {})[numbers[uuid]] = int(mib)
            self.seen = seen
        except (OSError, ValueError, TimeoutError):  # not there, not an NVIDIA machine, or it printed something else
            self.seen = None
        self.seen_at = time.monotonic()

    def known(self, card: str) -> dict | None:
        """What nvidia-smi last said of a card, or None if it said nothing of it."""
        number = CARD_NUMBER.search(card)
        return self.seen["cards"].get(int(number[0])) if self.seen and number else None

    def uses(self, server: Server) -> list[int] | None:
        """What a model server's process used on each of its cards at the last look, in MiB. None if that is not known for every one."""
        if not server.cards or self.seen is None:
            return None
        if self._gpu_members_seen is not self.seen or self._gpu_members_at != self.seen_at:
            self._gpu_members_seen, self._gpu_members_at = self.seen, self.seen_at
            self._gpu_members.clear()
        if group := server.owned_group:
            if group.closed:
                return None
            cached = self._gpu_members.get(server)
            if cached is None or cached[0] is not group:
                cached = self._gpu_members[server] = (group, tuple(group.members()))
            pids = cached[1]
        else:
            pids = [server.process.pid] if server.process else []
        mine = {}
        for pid in pids:
            for card, size in (self.seen["pids"].get(pid, {}) if self.seen else {}).items():
                mine[card] = mine.get(card, 0) + size
        each = [mine.get(int(number[0])) if (number := CARD_NUMBER.search(card)) else None for card in server.cards]
        return each if each and all(each) else None  # type: ignore[return-value]

    def others(self, card: str) -> int:
        """MiB of a card used by anything but LoadCoach's model servers: a desktop, or another program that computes on it."""
        ours = sum(dict(zip(server.cards, self.uses(server) or [])).get(card, 0) for server in self.reservations())
        return max(0, self.known(card)["used"] - ours)

    def takes(self, server: Server, card: str) -> float:
        """The room a loaded model is counted as taking on one of its cards: what it measured, or all of it."""
        whole = server.calibrating or server.mib is None or server.settings.get("alone") or self.known(card) is None
        if engine_for(server.settings).id=='sdcpp' and not engine_for(server.settings).model.fixed_split(server.settings,len(server.cards)):
            whole=True
        return math.inf if whole else server.mib[server.cards.index(card)]

    def free(self, card: str, without: tuple[Server, ...] | list[Server] = ()) -> float:
        """MiB of a card that one more model could have, with the servers in `without` gone from it."""
        on_it = [server for server in self.reservations() if card in server.cards and server not in without]
        if self.known(card) is None:
            return -math.inf if on_it else math.inf  # room that cannot be counted: a card to itself, or nothing
        return self.known(card)["total"] - self.config.card_margin - self.others(card) - sum(self.takes(server, card) for server in on_it)

    def make_room(self, card: str, size: float | None, leave: list[Server]) -> list[Server] | None:
        """The idle models that must go from a card for a model of `size` MiB to fit. None if it cannot be made to fit.

        `size` None is a model that takes the card whole. `leave` are servers that are going
        anyway. Models go longest unused first, and only as many as are needed. One that is
        answering, still loading or held does not go.
        """
        fits = lambda gone: (not [s for s in self.servers.values() if card in s.cards and s not in gone]) if size is None else self.free(card, gone) >= size  # noqa: E731
        idle = [s for s in self.servers.values() if card in s.cards and s not in leave and s.ready and not self.busy(s) and not self.pinned(s) and time.monotonic() - s.idle_since >= self.config.model_idle_minutes * 60]
        gone = list(leave)
        for server in sorted(idle, key=lambda server: server.idle_since):
            if fits(gone):
                break
            gone.append(server)
        if not fits(gone):
            return None
        for server in [server for server in reversed(gone) if server not in leave]:  # one that went and was not needed stays after all
            if fits([other for other in gone if other is not server]):
                gone.remove(server)
        return [server for server in gone if server not in leave]

    def reservations(self) -> list[Server]:
        """A removed wrapper still owns its reservation until child cleanup confirms exit."""
        return list(dict.fromkeys([*self.servers.values(), *self.retiring]))

    def hybrid_budget(self, settings: dict) -> int:
        """Read the selected engine's host-memory reservation in MiB."""
        return engine_for(settings).host_ram_budget(self.config, settings)

    def manages_ram(self, server: Server) -> bool:
        """Return whether the server consumes tracked CPU or hybrid host memory."""
        return not server.cards or bool(self.hybrid_budget(server.settings))

    def host_memory(self, without: tuple[Server, ...] | list[Server] = (), *, cache_admission: bool = False) -> dict:
        """Keep legacy CPU RSS accounting; hybrid models reserve their entire RAM cap.

        Without per-child cgroup observations, do not credit a hybrid's current RSS against
        its reservation. Double-counting allocated pages is conservative, not a fit claim.
        """
        memory = memory_snapshot()
        service_available = memory.get('service_reclaimable_available', memory['service_available']) if cache_admission else memory['service_available']
        available = max(0, memory['available'] - self.config.host_ram_headroom_mib)
        # Process RSS includes shared file mappings that need not be charged to
        # this cgroup. Its estimate cannot be subtracted from memory.high room.
        if service_available == 0:
            available = 0
        live = [server for server in self.reservations() if server not in without and
                (not server.dead() or server.owned_group and not server.owned_group.closed)]
        hybrid = sum(self.hybrid_budget(server.settings) for server in live)
        configured_used = hybrid + sum(server.rss_mib or 0 for server in live if not server.cards)
        reserved = hybrid
        for server in live:
            if server.ready or server.cards:
                continue
            measured = self.models.resident_measure(server.model, server.settings)
            reserved += max(0, self.ram_required(server.model, server.settings) - (server.rss_mib or 0)) if measured else available
        free = max(0, available - reserved)
        if self.config.host_ram_limit_mib:
            free = min(free, max(0, self.config.host_ram_limit_mib - configured_used - (reserved - hybrid)))
        service_free = None if service_available is None else max(0, service_available - reserved)
        if hybrid:
            free = min(free, service_free or 0)
        return {**memory, 'headroom': self.config.host_ram_headroom_mib,
                'configured_limit': self.config.host_ram_limit_mib or None,
                'configured_used': configured_used,
                'reserved': reserved, 'hybrid_reserved': hybrid, 'service_free': service_free, 'free': free}

    def ram_required(self, model: str, settings: dict) -> int:
        """Measured resident peak plus growth room; first load uses a conservative file bound.

        First loads are serialized. The bound is provisional and readiness rechecks actual
        headroom; neither this estimate nor RSS sampling replaces systemd's memory ceiling.
        """
        if budget := self.hybrid_budget(settings):
            return budget
        measured = self.models.resident_measure(model, settings)
        if measured is not None:
            return math.ceil(measured * 1.1) + 64
        paths = [settings['flags'].get(key) for key in ('model', 'mmproj')]
        size = sum(os.path.getsize(path) for path in paths if path and os.path.isfile(path))
        return max(256, math.ceil(size / 2**20) + 256)

    def cpu_room(self, model: str, settings: dict, leave: list[Server]) -> list[Server] | None:
        """Evict idle unheld/unlocked residents that hold host RAM, oldest first, to fit within it.

        Those are CPU models and engines that reserve a host-RAM budget beside their cards (hybrid,
        image and audio engines); an idle one would otherwise block a CPU load until it timed out.
        """
        budget = self.hybrid_budget(settings)
        # A cold unmeasured load reserves all current room until it has an observation.
        if any(not server.ready and self.manages_ram(server) and server not in leave for server in self.reservations()):
            return None
        required, gone = self.ram_required(model, settings), list(leave)

        def fits():
            """Check whether the candidate placement fits after the proposed removals."""
            memory = self.host_memory(without=gone, cache_admission=bool(budget))
            if not budget and memory['service_available'] == 0:
                return False
            if budget and (memory['service_free'] is None or memory['service_free'] < budget):
                return False  # unknown/insufficient cgroup room never qualifies a hybrid load
            used = sum(self.hybrid_budget(server.settings) or server.rss_mib or 0
                       for server in self.reservations() if server not in gone and self.manages_ram(server)
                       and (not server.dead() or server in self.retiring))
            reclaimed = sum(server.rss_mib or 0 for server in gone if not server.cards)
            return memory['free'] + reclaimed >= required and (not self.config.host_ram_limit_mib or
                    required + used <= self.config.host_ram_limit_mib)
        idle = sorted((server for server in self.servers.values() if self.manages_ram(server) and server not in gone
                       and server.ready and not self.busy(server) and not self.pinned(server)
                       and not self.holder() and time.monotonic() - server.idle_since >= self.config.model_idle_minutes * 60),
                      key=lambda server: server.idle_since)
        for server in idle:
            if fits():
                break
            gone.append(server)
        return [server for server in gone if server not in leave] if fits() else None

    def sample_resident(self, server: Server) -> None:
        """Refresh the resident server's measured host-memory usage."""
        if server.owned_group and not server.owned_group.closed:
            values = [resident_mib(pid, descendants=False) for pid in server.owned_group.members()]
            server.rss_mib = sum(values) if values and all(value is not None for value in values) else None
        elif server.process and not server.dead():
            server.rss_mib = resident_mib(server.process.pid)
        if server.rss_mib is not None and (server.rss_peak_mib is None or server.rss_mib > server.rss_peak_mib):
            server.rss_peak_mib = self.models.resident_measure(server.model, server.settings, server.rss_mib)

    def assert_cpu_memory(self, server: Server, *, isolated_probe: bool = False) -> None:
        """Check observed CPU resident usage against configured, host and service room."""
        if not self.manages_ram(server):
            return
        if server.rss_mib is None:
            raise Refusal(503, f"{server.model} resident RAM could not be measured.", 10)
        budget = self.hybrid_budget(server.settings)
        charged = server.rss_mib
        if budget and server.memory_group:
            # Shared mappings can appear in multiple process RSS totals. A verified
            # bounded engine uses its actual cgroup charge, not that duplicated sum.
            try:
                server.memory_group.verify()
                if server.memory_group.budget_mib != budget:
                    raise ValueError("Engine cgroup does not match the admitted RAM budget.")
                observation = server.memory_group.observation()
                charged = observation['current_mib']
                if observation['events'].get('oom_kill', 0):
                    raise ValueError("Engine memory group reported an OOM kill.")
            except (OSError, ValueError) as problem:
                raise Refusal(503, f"{server.model} memory enforcement could not be verified: {problem}", 10) from None
        if budget and charged > budget:
            raise ConfiguredRamCapacity(503, f"{server.model} exceeded its engine host RAM budget.", 10)
        others = sum(self.hybrid_budget(other.settings) or other.rss_mib or 0 for other in self.reservations()
                     if other is not server and self.manages_ram(other) and (not other.dead() or other in self.retiring))
        limit = self.config.host_ram_limit_mib
        if limit and (budget or server.rss_mib) + others > limit:
            raise ConfiguredRamCapacity(503, f"{server.model} exceeded the configured host RAM limit.", 10)
        memory = memory_snapshot()
        if memory['available'] < self.config.host_ram_headroom_mib:
            raise Refusal(503, f"{server.model} left less than the configured host/service RAM headroom.", 10)
        if memory['service_available'] == 0:
            if (isolated_probe and memory.get('service_scope') == 'self'
                    and type(memory.get('service_used')) is int
                    and type(memory.get('service_limit')) is int):
                raise ServiceRamCapacity(server.model, memory['service_used'], memory['service_limit'])
            raise Refusal(503, f"{server.model} left less than the configured host/service RAM headroom.", 10)

    async def evict_idle_cpu(self) -> None:
        """Timer eviction respects active calls, holds, model locks and the global lock."""
        if not self.config.cpu_idle_minutes or self.holder():
            return
        now = time.monotonic()
        for server in list(self.servers.values()):
            if (not server.cards and server.ready and not self.busy(server) and not self.pinned(server)
                    and not any(self.answers_for(server, [ticket.model]) for ticket in self.waiting if ticket.model)
                    and now - server.idle_since >= self.config.cpu_idle_minutes * 60):
                del self.servers[server.model]
                await self.stop(server)
                self.note('model', 'unloaded', model=server.model, why='CPU idle timer')

    def replacements(self, model: str, settings: dict) -> list[Server]:
        """Servers a load would replace, including kin under a different public name."""
        return [server for server in self.servers.values() if server.model == model or
                kin_base(server.configured or server.settings) == kin_base(settings)]

    def place(self, model: str, settings: dict, taken: set[str], calibrating: bool = False, *, configured: dict | None = None) -> tuple[tuple[str, ...], list[Server]] | None:
        """Choose cards for a model that is not loaded, and the idle models that make way for it. None means it has to wait.

        A card can be used if nobody ahead in line is waiting for it (`taken`) and the model fits
        on it, with idle models gone from it if need be. Of the cards that can be used, those
        where nothing has to go come first, the one with the most room first of all. So two
        small models go one to each card while there is a card for each.
        """
        need = self.needs(settings)
        count = len(need) if isinstance(need, list) else need
        measured = self.models.measure(model, settings)
        if engine_for(settings).id=='sdcpp' and (not measured or not measured.get('image_profile',{}).get('qualified') or measured['image_profile'].get('build')!=self.known_version(settings)):
            measured=None
        leave = self.replacements(model, configured or self.models.get(model) or settings)
        fixed_split = engine_for(settings).model.fixed_split(settings, count)
        # An automatic multi-card split changes with free memory. It must load on empty cards.
        # An explicit split is reproducible, so its measured per-card sizes can safely share.
        whole = (calibrating or measured is None or settings.get("alone") or
                 len(measured["mib"]) != count or (count > 1 and not fixed_split))
        retiring_cards = {card for server in self.retiring for card in server.cards}
        available = [card for card in self.cards if card not in taken | retiring_cards]
        choices = ([tuple(need) if engine_for(settings).id=='sdcpp' else tuple(card for card in self.config.gpus if card in need)]
                   if isinstance(need, list) else combinations(available, count))
        plans = []
        for cards in choices:
            if not set(cards) <= set(available):
                continue
            gone = list(leave)
            for index, card in enumerate(cards):
                more = self.make_room(card, None if whole else measured["mib"][index], gone)
                if more is None:
                    break
                gone.extend(more)
            else:
                if not calibrating and measured and len(measured["mib"]) == len(cards):
                    if any(self.known(card) and size > self.free(card, gone) for card, size in zip(cards, measured["mib"])):
                        continue
                if self.hybrid_budget(settings):
                    host_gone = self.cpu_room(model, settings, gone)
                    if host_gone is None:
                        continue
                    gone.extend(host_gone)
                plans.append((cards, [server for server in gone if server not in leave]))
        if not plans:
            return None
        return min(plans, key=lambda plan: (max((server.idle_since for server in plan[1]), default=0),
                                            -sum(self.free(card, leave + plan[1]) for card in plan[0])))

    def fit(self, model: str, settings: dict, taken: set[str], *, configured: dict | None = None) -> tuple[dict, tuple[str, ...], list[Server]] | None:
        """Prefer maximum context, evicting only stale idle models, then try smaller measured sizes.

        An uncalibrated model first reserves empty cards to measure its three contexts. Until
        those observations exist it never guesses what could fit beside a recent model.
        Multi-card models with an explicit split can share; automatic splits need empty cards.
        """
        # Editing a named entry can make it kin of a second loaded server. Dispatch's
        # named-server check alone cannot protect that other server's active work.
        if any(server.model == model or kin_base(server.configured or server.settings) == kin_base(configured or settings)
               for server in self.retiring):
            return None
        if any(not server.ready or self.busy(server) or self.pinned(server)
               for server in self.replacements(model, configured or self.models.get(model) or settings)):
            return None
        if settings.get("gpus") == "any":
            for layout in self.layouts(settings):
                count = layout["gpus"]
                if self.impossible_layout(model, layout):
                    continue
                placed = self.fit(model, layout, taken, configured=configured)
                if placed:
                    return placed
                if count == 1:
                    return None  # a single card can hold it; wait for that card
            return None
        if self.needs(settings) == 0:
            leave = self.replacements(model, configured or self.models.get(model) or settings)
            gone = self.cpu_room(model, settings, leave)
            return (settings, (), gone) if gone is not None else None
        if engine_for(settings).id == 'sdcpp':
            # Image failures revoke sharing, not the ability to retry on empty cards.
            placed = self.place(model, settings, taken, configured=configured)
            return (settings, *placed) if placed else None
        if not self.models.calibrated(model, settings):
            placed = self.place(model, settings, taken, calibrating=True, configured=configured)
            return (settings, *placed) if placed else None
        for context in reversed(contexts(settings)):
            candidate = at_context(settings, context)
            if not self.models.measure(model, candidate):
                continue  # a failed or unobserved size cannot be used to predict a fit
            placed = self.place(model, candidate, taken, configured=configured)
            if placed:
                return candidate, *placed
        return None

    def load(self, model: str, settings: dict, cards: tuple[str, ...], gone: list[Server]) -> Server:
        """Start a model at the chosen context, retaining its requested range for reuse and holds."""
        configured = self.models.get(model) or settings
        replaced = gone + [server for server in self.servers.values() if server not in gone and
                           (server.model == model or kin_base(server.configured or server.settings) == kin_base(configured))]
        for old in replaced:
            self.retiring.add(old)
            del self.servers[old.model]
        measured = self.models.measure(model, settings)
        server = self.servers[model] = Server(model, cards, settings, configured=configured, identity=self.models.identity(model))
        if engine_for(settings).id=='sdcpp' and (not measured or not measured.get('image_profile',{}).get('qualified') or measured['image_profile'].get('build')!=self.known_version(settings)):
            measured=None
        server.mib = measured["mib"] if measured and len(measured["mib"]) == len(cards) else None
        server.calibrating = bool(cards and len(contexts(settings)) > 1 and not self.models.calibrated(model, settings))
        server.packed = any(set(other.cards) & set(cards) for other in self.servers.values() if other is not server)
        task = asyncio.create_task(self.bring_up(server, replaced))
        self.loads.add(task)
        task.add_done_callback(self.loads.discard)
        return server

    async def calibrate(self, server: Server) -> None:
        """Load minimum, midpoint and maximum on reserved cards and keep each observed size.

        A larger context may fail to load; its error is kept and a smaller successful context
        is restored. No unsuccessful attempt is labelled a memory measurement. Cancellation
        always stops the current child process.
        """
        original = server.settings
        loaded = None
        for context in contexts(original):
            if loaded is not None:
                await self.stop(server)
                loaded = None
            server.process, server.listener, server.said = None, None, b""
            server.settings = at_context(original, context)
            server.mib = None
            try:
                await self.start(server)
                await self.measure(server)
                loaded = context
                if server.mib is None:
                    # Telemetry is unavailable. Keep the successful load exclusive, as before.
                    return
                if any(self.known(card) and self.known(card)["used"] + self.config.card_margin > self.known(card)["total"] for card in server.cards):
                    await self.stop(server)
                    loaded = None
            except Refusal as problem:
                self.models.measured(server.model, server.settings, None, str(problem))
                self.note("model", "context-failed", model=server.model, context=context, why=str(problem))
        await self.look(fresh=True)
        valid = [point for point in self.models.measurements(server.model, original)
                 if point["context"] in contexts(original) and point["mib"] and len(point["mib"]) == len(server.cards)
                 and all(self.known(card) is None or size <= self.known(card)["total"] - self.config.card_margin - self.others(card)
                         for card, size in zip(server.cards, point["mib"]))]
        if not valid:
            if loaded is not None:
                await self.stop(server)
            raise Refusal(503, f"{server.model} cannot fit at its minimum context with the configured memory margin. See its context measurements.")
        best = max(valid, key=lambda point: point["context"])
        if loaded != best["context"]:
            if loaded is not None:
                await self.stop(server)
            server.process, server.listener, server.said = None, None, b""
            server.settings = at_context(original, best["context"])
            await self.start(server)
            await self.measure(server)

    async def version(self, settings: dict | None = None) -> str:
        """Cache the selected engine build by installation, leaving legacy build access intact."""
        engine = engine_for(settings)
        installed = engine.installation(self.config)
        if not installed.enabled:
            return ""
        if hasattr(engine, "build_version"):
            identity = await engine.build_version(self.config)
            self.builds[(engine.id, installed.executable)] = (None, identity)
            return identity
        key = (engine.id, installed.executable)
        stamp = engine_stamp(installed.executable)
        if key not in self.builds or self.builds[key][0] != stamp:
            self.builds[key] = (stamp, await engine.version(installed.executable))
        if engine.id == "llama_cpp":
            self.build = (stamp, self.builds[key][1])
        return self.builds[key][1]

    def known_version(self, settings: dict) -> str:
        """Return the cached engine build identity for these model settings."""
        engine = engine_for(settings)
        if engine.id == "llama_cpp":
            return self.build[1] if self.build else ""
        # Full content identity is verified at preparation, never borrowed from llama.cpp.
        return self.builds.get((engine.id, engine.installation(self.config).executable), (None, ""))[1]

    def marks(self, server: Server, model: str, settings: dict) -> dict:
        """What says exactly which model answered a request: its fingerprint, and the build of llama-server. One that is not known is left out.

        The fingerprint is of the server as it was started, with the adapters and the projector
        of the model that was asked for. Those are what applied, whatever else the server holds
        for its kin.
        """
        as_asked = engine_for(settings).model.answered_settings(server.settings, settings)
        return {key: value for key, value in (("fingerprint", self.models.fingerprint(as_asked, model)), ("build", server.build)) if value}

    async def measure(self, server: Server) -> None:
        """See what a loaded model uses on each of its cards, and keep that with the model. Nothing changes if it cannot be seen."""
        self.sample_resident(server)
        if engine_for(server.settings).id=='sdcpp':
            try:
                response=await self.client.get(server.url+'/health',headers=server.headers,timeout=3)
                profile=response.json().get('image_profile') if response.is_success else None
            except (httpx.HTTPError,ValueError):
                if self.models.stored(server.model):
                    self.models.measured(server.model,server.settings,None,image_profile={'qualified':False,'build':server.build,'problem':'Workflow observation unavailable'})
                    server.mib=None
                return
            if profile:
                before=self.models.measure(server.model,server.settings)
                prior=(before or {}).get('image_profile',{})
                if (profile.get('successful') and profile.get('complete') and prior.get('qualified') and prior.get('build')==server.build and
                        len(prior.get('gpu_peak_mib',[]))==len(profile.get('gpu_peak_mib',[]))==len(server.cards)):
                    profile={**profile,'qualified':True,
                             'gpu_peak_mib':[max(a,b) for a,b in zip(prior['gpu_peak_mib'],profile['gpu_peak_mib'])],
                             'rss_peak_mib':max(prior['rss_peak_mib'],profile['rss_peak_mib'])}
                server.image_profile=profile
                if profile.get('qualified') and len(profile.get('gpu_peak_mib',[]))==len(server.cards) and self.models.stored(server.model):
                    reserved=[math.ceil(value)+256 for value in profile['gpu_peak_mib']]
                    server.mib=self.models.measured(server.model,server.settings,reserved,image_profile={**profile,'build':server.build})
                elif self.models.stored(server.model):
                    self.models.measured(server.model,server.settings,None,image_profile={**profile,'qualified':False,'build':server.build})
                    server.mib=None
            return
        if not server.cards:
            return
        await self.look(fresh=True)
        if (uses := self.uses(server)) and self.models.stored(server.model):
            server.mib = self.models.measured(server.model, server.settings, uses)

    async def bring_up(self, server: Server, replaced: list[Server]) -> None:
        """Stop the models being replaced, start the new one, and tell dispatch how it went."""
        began = time.monotonic()
        try:
            for old in replaced:
                await self.stop(old)
            if self.manages_ram(server):
                # Reclamation uses resident pages as a conservative planning hint. Shared
                # mappings/cgroup charging can reclaim less: observe again after exit.
                budget = self.hybrid_budget(server.settings)
                memory = self.host_memory(without=[server], cache_admission=bool(budget))
                free = memory['free']
                if not budget and memory['service_available'] == 0:
                    raise Refusal(503, f"{server.model} no longer fits the available host/service RAM budget.", 10)
                if budget and (memory['service_free'] is None or
                        memory['service_free'] < self.ram_required(server.model, server.settings)):
                    raise Refusal(503, f"{server.model} no longer fits the available host/service RAM budget.", 10)
                if self.ram_required(server.model, server.settings) > free:
                    raise Refusal(503, f"{server.model} no longer fits the available host/service RAM budget.", 10)
            if server.calibrating:
                await self.calibrate(server)
            else:
                await self.start(server)
                await self.measure(server)
            if self.manages_ram(server):
                # The child is measured before ordinary requests can use it. Recheck host
                # room without its own loading reservation, after replaced children exited.
                self.assert_cpu_memory(server)
            server.calibrating = False
            server.idle_since = time.monotonic()
            server.build = server.build or await self.version(server.settings)
            adapter = engine_for(server.settings)
            if hasattr(adapter, "runtime_metadata"):
                server.runtime_metadata = await adapter.runtime_metadata(self.client, server)
            server.ready = True
            self.note(
                "model",
                "loaded",
                model=server.model,
                cards=list(server.cards),
                seconds=round(time.monotonic() - began, 1),
                replaced=[old.model for old in replaced],
                mib=server.mib,
                context=server.settings["flags"]["ctx-size"],
            )
        except asyncio.CancelledError:
            await self.stop(server)
            if self.servers.get(server.model) is server:
                del self.servers[server.model]
            raise
        except Exception as problem:
            await self.stop(server)
            if self.servers.get(server.model) is server:
                del self.servers[server.model]
            refusal = problem if isinstance(problem, Refusal) else Refusal(502, repr(problem))
            # It was put beside other models and did not load. Most likely there was less room than was counted.
            # Its measure is thrown away, so that it is tried once more with cards to itself, and measured again there.
            again = server.packed and self.models.forget_measure(server.model)
            self.note("model", "failed", model=server.model, why=str(refusal), text={"output": server.output()}, **({"again": "alone"} if again else {}))
            automatic = (server.configured or {}).get("gpus") == "any"
            if automatic and not server.calibrating:
                self.models.measured(server.model, server.settings, None, str(refusal))
            split_retry = bool(automatic and (server.configured or {}).get("allow_split") and len(server.cards) == 1 and len(self.cards) >= 2
                               and ("out of memory" in str(refusal).lower() or "cannot fit" in str(refusal).lower()))
            for ticket in [] if again or split_retry else [ticket for ticket in self.waiting if ticket.model == server.model]:
                self.waiting.remove(ticket)
                ticket.error = refusal
                ticket.turn.set()
        finally:
            self.wake.set()

    async def dispatch(self) -> None:
        """Decide who goes next. Runs for the whole life of LoadCoach.

        Each time something changes, the line is read once from the front. A ticket goes if what
        it needs is free. If not, the cards it needs are marked as taken, so that nobody further
        back can use them first. That keeps the order fair while letting requests for unrelated
        cards go ahead.
        """
        while True:
            # Recheck stale timers and outside GPU users while work waits, even if no request finishes.
            try:
                await asyncio.wait_for(self.wake.wait(), 1 if self.waiting or self.servers else None)
            except TimeoutError:
                pass
            self.wake.clear()
            for server in list(self.servers.values()):
                self.sample_resident(server)
            await self.evict_idle_cpu()
            if any(ticket.model and ticket.model not in self.servers for ticket in self.waiting):
                await self.look()  # a model is about to be placed: what the cards hold must be known first
            # Isolated probes never become ordinary-ready. Reap their exited children
            # too, or an invisible reservation can block foreground admission forever.
            for server in [server for server in self.servers.values() if (server.ready or server.probing) and server.dead()]:
                await self.stop(server)  # a dead wrapper may still own live native children
                del self.servers[server.model]  # it died on its own; the next request reloads it
                self.note("model", "died", model=server.model, why=server.last_words(), text={"output": server.output()})
            # A model taken off the list stays loaded only until nobody is using or waiting for it.
            for server in [server for server in self.servers.values() if server.ready and not self.busy(server)]:
                wanted = any(ticket.model == server.model for ticket in self.waiting)
                if not wanted and not self.pinned(server) and self.models.get(server.model) is None:
                    del self.servers[server.model]
                    await self.stop(server)
                    self.note("model", "unloaded", model=server.model, why="it was removed")
            lock = self.holder()
            taken: set[str] = set()  # cards that a ticket ahead in line is waiting for
            for ticket in list(self.waiting):
                if lock and ticket.token != lock.token:
                    if ticket.observe:
                        ticket.observe('queue')
                    continue  # under a lock, only the holder's tickets are looked at

                if ticket.model is None and self.held():
                    continue  # holds finish first; their own inference must still go through
                if ticket.model is None:  # a request to unload, or to take the lock
                    if self.running or self.loads:
                        break  # it needs everything quiet, so nothing behind it may start
                    if not ticket.wants or ticket.wants.unload:
                        await self.stop_all()
                    if ticket in self.waiting:  # unless it gave up meanwhile
                        self.waiting.remove(ticket)
                        if ticket.wants:
                            self.grant(ticket.wants)
                        ticket.turn.set()
                    self.wake.set()  # who may go has changed; read the line afresh
                    break

                # The settings as they are now. For a model removed while this request waited,
                # the settings it had then, so that the request is still answered.
                settings = self.models.get(ticket.model) or ticket.settings
                if settings is None:  # removed, by a request that did not say what the model was
                    self.waiting.remove(ticket)
                    ticket.error = Refusal(404, f"{ticket.model} has been removed.")
                    ticket.turn.set()
                    continue
                server, loading = self.serving(ticket.model, settings), settings
                if server and server.model == ticket.model and self.pinned(server):
                    # Edits to how it is started apply after the call's hold, not between its turns.
                    settings = base(server.configured or server.settings) | ({"adapters": settings["adapters"]} if "adapters" in settings else {})
                if ticket.validate_settings:
                    try:
                        ticket.validate_settings(settings)
                    except Refusal as problem:
                        self.waiting.remove(ticket)
                        ticket.error = problem
                        ticket.turn.set()
                        continue
                if server and (not self.answers(server, settings) or (ticket.calibrate and server.ready and not self.models.calibrated(ticket.model, server.settings))):  # set up otherwise since it was loaded, or without an adapter this model has
                    if not server.ready or self.busy(server) or self.pinned(server):
                        if ticket.observe:
                            ticket.observe('queue' if server.ready else 'loading')
                        taken |= set(server.cards)  # what it is doing finishes first
                        continue
                    if kin_base(server.configured or server.settings) == kin_base(settings):
                        # Kin: it is loaded afresh with the adapters it had and the ones this model adds, so that those it answered for are still answered.
                        every = [*server.settings.get("adapters", {}).values(), *self.lacks(server, settings)]
                        loading = settings | {"adapters": {adapter["file"]: adapter for adapter in every}}
                    server = None  # idle, so it is loaded afresh below, in its own place
                if server is None and not ticket.may_load:
                    self.waiting.remove(ticket)
                    ticket.error = Refusal(403, f"{ticket.model} is not loaded, and your group may not load models.")
                    ticket.turn.set()
                    continue
                if server is None:
                    need = self.needs(settings)
                    impossible = (not set(need) <= set(self.cards)) if isinstance(need, list) else need > len(self.cards)
                    impossible = impossible or (not self.cards and settings.get("gpus") != 0)
                    if impossible:
                        self.waiting.remove(ticket)
                        ticket.error = Refusal(503, f"{ticket.model} has too few enabled cards. Change its Cards setting or enable a card under System → Compute nodes.")
                        ticket.turn.set()
                        continue
                    placed = self.fit(ticket.model, loading, taken)
                    if placed is None:
                        if ticket.observe:
                            ticket.observe('queue')
                        if settings.get("gpus") == "any" and all(self.impossible_layout(ticket.model, layout) for layout in self.layouts(loading)):
                            self.waiting.remove(ticket)
                            ticket.error = Refusal(503, f"{ticket.model} cannot fit on its allowed enabled cards. See its measurements or allow a two-card split.")
                            ticket.turn.set()
                            continue
                        need = self.needs(settings)
                        taken |= set(need if isinstance(need, list) else self.config.gpus)
                        continue
                    server = self.load(ticket.model, *placed)
                if ticket.observe:
                    ticket.observe('queue' if server.ready else 'loading')
                if server.ready and self.busy(server) < self.room_for(settings) and (self.pinned(server) or not taken & set(server.cards)):
                    self.waiting.remove(ticket)
                    self.running.append(ticket)
                    ticket.server, ticket.settings = server, settings  # the settings it is answered under: its adapters are the ones that apply
                    ticket.since = server.used = time.monotonic()
                    ticket.turn.set()
                else:
                    taken |= set(server.cards)  # still loading, full, or promised to someone ahead

    def status(self) -> dict:
        """What is loaded where, who is in line and who holds the lock, for /loadcoach/status."""
        now = time.monotonic()
        lock = self.holder()
        self.held()

        def describe(tickets: list[Ticket]) -> list[dict]:
            """Describe a server's current readiness and retained resource reservation."""
            return [
                {
                    "id": ticket.entry,
                    **({'stop_handle': ticket.stop_handle, 'unlogged': True} if ticket.stop_handle else {}),
                    "model": ticket.model,
                    "user": ticket.user,
                    "caller": ticket.caller,
                    "priority": ticket.priority,
                    "seconds": round(now - ticket.since, 1),
                }
                for ticket in tickets
            ]

        return {
            "holds": [{"model": name, "user": user, "seconds_left": round(until - now, 1)}
                      for (name, user), until in self.holds.items()] +
                     [{"model": name, "user": lease['user'], "seconds_left": round(lease['until'] - now, 1),
                       "loadout": lease['name'], "lease": identity, "state": lease['state']}
                      for identity, lease in self.loadout_leases.items() for mid in lease['models']
                      if (name := self.models.name_for(mid) or (lease['_servers'][mid].model if mid in lease['_servers'] else None))],
            "cards": list(self.cards),  # enabled cards only; exclusions are listed under Compute nodes
            # Each card's room in MiB as of the last look: all of it, what other programs use, and what one more model could have. None for a card that cannot be measured.
            "memory": {
                card: self.known(card) and {"total": self.known(card)["total"], "others": self.others(card), "free": max(0, self.free(card))}
                for card in self.config.gpus
            },
            "system_memory": self.host_memory(),
            "stopping": [{"model": server.model, "gpus": list(server.cards),
                          "cleanup_failed": bool(server.stopping and server.stopping.done()
                              and not server.stopping.cancelled() and server.stopping.exception())}
                         for server in self.retiring],
            "cpu": {"logical": os.cpu_count(), "concurrency": sum(self.room_for(server.settings) for server in self.servers.values() if not server.cards)},
            "margin": self.config.card_margin,  # MiB of every card that is kept free. The dry fit page draws it
            "loaded": [
                # `mib` is what the model measured on each of its cards, or null for one that has its cards to itself.
                # `answers` are the other models this server answers for: kin of the one it was loaded as.
                {"model": server.model, "gpus": list(server.cards), "state": server.state, "locked": self.locked(server),
                 "answers": [name for name in self.models.kin_of(server.settings) if name != server.model],
                 "rss_mib": server.rss_mib, "rss_peak_mib": server.rss_peak_mib,
                 **({'image_profile':server.image_profile} if engine_for(server.settings).id=='sdcpp' else {}),
                 "mib": None if any(self.takes(server, card) == math.inf for card in server.cards) else server.mib}
                for server in self.servers.values()
                if not server.dead()
            ],
            "lock": lock
            and {
                "user": lock.user,
                "caller": lock.caller,
                "seconds_left": round(lock.until - now, 1),
                "refuse": lock.refuse,
            },
            "running": describe(self.running),
            "waiting": describe(self.waiting),
        }

    async def start(self, server: Server) -> None:
        """Start on enabled cards only, including isolated probes."""
        if not set(server.cards) <= set(self.cards):
            raise Refusal(503, "A card selected for this model has been disabled.")
        port = free_port()
        engine = engine_for(server.settings)
        if server in self.retiring:
            raise Refusal(503, "This engine's previous process tree has not finished stopping.")
        server.stopping = None
        try:
            if engine.id == "strata":
                preparation = asyncio.create_task(asyncio.to_thread(engine.launch, self.config, port,
                    server.settings, server.cards, name=server.model))
                try:
                    launch = await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    # A running thread cannot be cancelled. Join it and destroy its private
                    # handoff before releasing the loading reservation, even on repeated cancel.
                    while not preparation.done():
                        try:
                            await asyncio.shield(preparation)
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            break  # preparation owns its failed handoff; cancellation still wins
                    if not preparation.cancelled() and preparation.exception() is None:
                        prepared = preparation.result()
                        if prepared.cleanup:
                            prepared.cleanup()
                    raise
            else:
                launch = engine.launch(self.config, port, server.settings, server.cards)
        except (OSError, ValueError) as problem:
            raise Refusal(503, str(problem)) from None
        server.launch_cleanup = launch.cleanup
        server.build = launch.build
        if launch.build:
            self.builds[(engine.id, engine.installation(self.config).executable)] = (None, launch.build)
        command = launch.command
        server.health_path, server.stop_seconds = launch.health_path, launch.stop_seconds
        server.headers = dict(launch.headers)
        try:
            if launch.process_group:
                adopt_orphans()
            if launch.memory_mib:
                if not launch.process_group or launch.memory_mib != self.hybrid_budget(server.settings):
                    raise ValueError("Bounded launch requires owned processes and the admitted engine RAM budget.")
                server.memory_group = MemoryGroup.create(launch.memory_mib)
                command = [sys.executable, "-I", "-B", str(Path(__file__).with_name("engine_child.py")),
                           "--cgroup", str(server.memory_group.path), "--memory-mib", str(launch.memory_mib), "--", *command]
            # Everything it prints comes to LoadCoach, which passes it on and remembers the end of it.
            # CUDA numbers the cards fastest first unless told otherwise, and nvidia-smi by where they sit.
            # With this, llama-server's CUDA0 is nvidia-smi's card 0, which is how a measure is matched to a card.
            # With adapters, one more: llama-server was seen to lose memory each time a request changes the adapter, unless CUDA's graphs are off.
            around = launch.environment
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command,
                stdout=asyncio.subprocess.PIPE if launch.capture_output else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT, env=around, start_new_session=launch.process_group))
            try:
                process = server.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                while not spawn.done():
                    try:
                        await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        pass
                server.process = spawn.result()
                if launch.process_group:
                    server.owned_group = ProcessGroup(server.process)
                await self.stop(server)
                raise
            server.owned_group = ProcessGroup(process) if launch.process_group else None
        except (OSError, ValueError) as problem:
            await self.stop(server)
            raise Refusal(502, f"Could not run {command[0]}: {problem}") from None
        except BaseException:
            await self.stop(server)
            raise
        server.listener = asyncio.create_task(self.listen(server, process.stdout)) if launch.capture_output else None
        server.url = f"http://127.0.0.1:{port}"
        try:
            await self.wait_until_ready(server, process)
        except BaseException:  # includes being cancelled: never leave a half-started server
            await self.stop(server)
            raise

    async def listen(self, server: Server, output: asyncio.StreamReader | None) -> None:
        """Pass on everything a model server prints, and keep the last of it.

        This runs for as long as the process does. It must: a process whose output nobody reads
        fills its pipe and then stops, waiting. What it prints still ends up where LoadCoach's own
        output goes, as before. The difference is that LoadCoach has now seen it too.
        """
        while output and (chunk := await output.read(8192)):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            server.said = (server.said + chunk)[-16000:]

    async def wait_until_ready(self, server: Server, process: asyncio.subprocess.Process) -> None:
        """`llama-server` answers /health with 200 once the model is loaded, 503 before that."""
        deadline = time.monotonic() + self.config.load_timeout
        while time.monotonic() < deadline:
            if process.returncode is not None:
                if server.listener:  # let it finish reading, so the reason is not cut short
                    await asyncio.wait({server.listener}, timeout=2)
                raise Refusal(
                    502,
                    f"llama-server exited with code {process.returncode} while loading "
                    f"{server.model}: {server.last_words()}",
                )
            try:
                if engine_for(server.settings).healthy(await self.client.get(server.url + server.health_path, headers=server.headers, timeout=2)):
                    return
            except httpx.HTTPError:
                pass  # not listening yet
            await asyncio.sleep(server.health_interval)
        raise Refusal(
            502,
            f"{server.model} was not ready after {self.config.load_timeout} seconds. "
            f"The last thing llama-server said: {server.last_words()}",
        )

    async def stop(self, server: Server) -> None:
        """Cancellation must not release a reservation before the owned tree exits."""
        failed = bool(server.stopping and server.stopping.done() and
                      not server.stopping.cancelled() and server.stopping.exception())
        if server.stopping is None or failed:
            self.retiring.add(server)
            server.stopping = asyncio.create_task(self._stop_owned(server))
        cancelled = False
        while not server.stopping.done():
            try:
                await asyncio.shield(server.stopping)
            except asyncio.CancelledError:
                cancelled = True
        server.stopping.result()  # failed cleanup deliberately retains the reservation
        if cancelled:
            raise asyncio.CancelledError

    async def _stop_owned(self, server: Server) -> None:
        """Bound final sampling, then stop and reap the complete owned process tree."""
        process = server.process
        released = False
        try:
            if server.owned_group or process and process.returncode is None:
                try:
                    if server.ready:  # once more before it goes: it may have grown while it answered
                        try:
                            async with asyncio.timeout(min(5, server.stop_seconds)):
                                await self.measure(server)
                        except TimeoutError:
                            pass  # final telemetry must not prevent process cleanup
                finally:  # cancellation during measurement must still release this process's memory
                    if server.owned_group:
                        await server.owned_group.stop(server.stop_seconds)
                    else:
                        if process.returncode is None:
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=server.stop_seconds)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                    released = True
            else:
                released = True
        finally:
            if released:
                if server.memory_group:
                    server.memory_group.close()
                if server.launch_cleanup:
                    server.launch_cleanup()
                    server.launch_cleanup = None
                server.headers.clear()
                self.retiring.discard(server)
            self.seen_at = 0  # look again before placing the next model, including after cancellation

    async def stop_all(self) -> None:
        """Stop every resident and retiring server before closing the runner."""
        for server in self.reservations():
            await self.stop(server)
            if self.servers.get(server.model) is server:
                del self.servers[server.model]
            self.note("model", "unloaded", model=server.model)
