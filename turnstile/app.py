"""turnstile: the way in on the front machine. It checks every caller, and passes their requests on to compute.

A harness talks to turnstile as if it were LoadCoach or the ToolLocker: the same port, the same
paths, the same key. Turnstile holds the accounts (see accounts.py). It checks each request's key
here, before anything is sent on or woken, and turns a bad, disabled or revoked key away at once.

An accepted request goes on to compute without its key. In its place goes a statement, signed
with the machine key that front and compute share, of who the caller is and what they may do
(see loadcoach/statement.py). Compute believes nothing else. Its answer carries a proof that it
holds the same key, and an answer without one is not passed back.

If compute is asleep, turnstile sends the packet that wakes it and holds the request until compute
answers. Any valid key can wake it. A request that says `x-turnstile-no-wake`, as every request
from weightroom's pages does, is told at once that compute is not answering.

It is a program of its own so that restarting weightroom's pages never cuts off an answer that is
on its way through here.
"""

from __future__ import annotations

from loadcoach import observability as observation
from turnstile import observations

import asyncio
from collections import Counter
import contextvars
import threading
import base64
import binascii
import json
import secrets
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
import typer
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from loadcoach.client_origin import ClientOrigin
from turnstile.accesses import ClientAccesses
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from db import DatabaseRegistry
from db.maintenance import Barrier, Gate
from loadcoach import statement
from loadcoach.config import CLI, read_settings, model_places
from loadcoach.groups import read_group
from loadcoach.log import FRONT, Log, machine_slot, public_number, origin_number
from loadcoach.files import PIECE, SPARE, sha256_of
from loadcoach.settings import FRONT_PROGRAMS, comments, copy_of, laid_over, said
from loadcoach.setting_names import current_names
from loadcoach.users import ANONYMOUS, User, Users
from plugins._registry import Registry
from plugins._sdk import AdapterError
from plugins._turnstile import Router as PluginRouter
from turnstile import accounts, events, roles, allowances
from loadcoach.outside import Services
from loadcoach.batches import Batches
from turnstile.batches import FrontBatches
from turnstile.tasks import TaskRecords
from turnstile.settings import ConsoleSettings
from turnstile.artifacts import FrontFiles, checked_roots
from loadcoach.file_index import FRONT_INDEX
from turnstile.hosted import Hosted
from turnstile.fallthrough import Fallthrough
from turnstile.machines import Machines, PassiveReads, logical_catalogue, placement_order
from turnstile.model_locations import ModelLocations
from engines import engine_for
from turnstile.model_configuration import comparable, configuration, generation, shared, translate, with_generation, with_shared
from turnstile.model_install import plan as installation_plan, execute as install_files
from turnstile import library
from turnstile.accounts import LOOPBACK, key_of, signed_in
from turnstile.reporting import ReportTimeout, run as run_report

# Headers that belong to one connection, not to the request. They are made afresh on each side.
HOP_HEADERS = {"connection", "content-length", "host", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}
NO_WAKE = "x-turnstile-no-wake"  # a request with this header is never held to wake compute
NO_WAKE_PATHS = frozenset({"/loadcoach/loadouts", "/loadcoach/plugins/mirror", "/loadcoach/alert-evidence",
                           "/spotter/alert-evidence", "/loadcoach/observation-metrics"})
TRY_EVERY = 2  # seconds between tries while compute is waking


@dataclass(frozen=True)
class Settings:
    """Everything turnstile is told. It reads the front machine's one file, settings/weightroom.toml."""

    compute: str  # compute's address without a port, such as 10.77.10.84. Each port is passed on to the same port there
    data: str = "."  # the folder the accounts are kept in, unless [databases] says otherwise
    host: str = "127.0.0.1"
    ports: tuple[int, ...] = (8442, 8443, 8444)  # LoadCoach's, the ToolLocker's and spotter's
    wake_mac: str = ""  # the hardware address of compute's network card. Empty: turnstile never wakes it
    wake_address: str = "255.255.255.255"  # where the waking packet is sent: everyone on the home network
    wait: float = 180  # seconds to wait for compute to wake before giving up
    max_in_flight: int = 200  # requests turnstile carries to compute at once. One more is turned away at once with 503 and Retry-After, rather than held here out of LoadCoach's line. A change needs a restart
    busy_retry_seconds: float = 5  # the Retry-After of that 503
    hosted_keys: str = ""  # the folder with outside services' private NAME.key files. Empty: the folder of machine_key_file
    machines: dict = field(default_factory=dict)  # further LoadCoach instances, optionally with a Spotter URL for host telemetry
    machines_poll_seconds: float = 10  # how often each machine is asked about itself, and how long a fact stays "ready" (three times this)
    keep_days: float = 90  # days the event log is kept here. 0 keeps everything
    voice_port: int = 8445
    machine_key_file: str = ""  # the key front shares with compute, which statements are signed with
    plugins_dir: str = ""  # plugin folders on front; empty leaves the plugin path out
    auth_key_file: str = ""  # front's own key, which turnstile asks compute for the plugin model list with
    path: str = ""  # shared settings path for the persistent maintenance barrier
    databases: dict = field(default_factory=dict)  # shares weightroom's named accounts and plugin stores
    groups: dict = field(default_factory=dict)  # the first groups, taken into the accounts once, while there are none
    keep_console_settings_on_front: bool = False  # the front stores and serves the settings of its own programs (weightroom, turnstile, voice-gateway); formerly front_settings
    allow_caller_to_specify_destination: bool = True  # callers may name the node a task or request runs on. Off: the front picks. Settings → General
    toollocker_url: str = ""  # optional front ToolLocker upstream; port 8443 never wakes compute
    artifact_machine: str = ""  # front-side LoadCoach that owns downloads; empty prefers front_cpu
    models_folder: str = ""  # optional model cache on front; no inference service is required
    models_storage_folder: str = ""  # optional model library on front; empty disables this tier
    image_adult_previews: bool = False  # permit individual users to opt in to adult Civitai previews
    files_every: float = 10  # minutes between looks through front's own model folders for the Files page. 0: only when asked
    compute_offset: int = 0  # how far compute's ports sit from `ports`, from the port in compute's URL. 0 unless both run on one machine

    def upstream(self, port: int) -> str:
        """The main compute machine's address for one of `ports`, moved by `compute_offset`."""
        return f"http://{self.compute}:{port + self.compute_offset}"


def load_settings(path: Path) -> Settings:
    """Read the front machine's TOML file. Anything wrong with it stops startup with a message naming the cause."""
    told = read_settings(path)
    data = path.parent / told.get("data", ".")
    try:
        file = current_names(told.get("turnstile", {}), f"{path} [turnstile]")
    except ValueError as problem:
        raise SystemExit(str(problem)) from None
    # What was changed on the site is kept on compute. weightroom keeps a copy in the folder the two share.
    changed = copy_of(data / "settings.json").get("turnstile", {})
    for mine in ([laid_over(file, changed, "turnstile")] if changed else []) + [file]:  # with the changes, and failing that as the file has it
        try:
            ports = tuple(mine.get("ports", (8442, 8443, 8444)))
            # A port in compute's URL other than LoadCoach's first one moves every port by the same amount:
            # compute = "http://127.0.0.1:9442" lets turnstile keep 8442-8444 on a machine that also runs LoadCoach.
            compute_port = urlsplit(told["compute"]).port
            settings = Settings(
                compute=urlsplit(told["compute"]).hostname,
                compute_offset=compute_port - ports[0] if compute_port else 0,
                path=str(path),
                databases=told.get("databases", {}),
                groups={name: read_group(name, section) for name, section in told.get("groups", {}).items()},
                machines={name: {"compute": section.get("compute"), "spotter": section.get("spotter", ""),
                                 "machine_key_file": str((path.parent / section.get("machine_key_file", "../keys/machine.key")).resolve()),
                                 "wake_mac": section.get("wake_mac", ""), "wake_address": section.get("wake_address", "255.255.255.255")}
                          for name, section in told.get("machines", {}).items()},
                data=str(data),
                wake_mac=told.get("wake_mac", ""),
                wake_address=told.get("wake_address", "255.255.255.255"),
                **{**mine, "ports": ports,
                   # This live policy reads front's current saved override, including resets.
                   "image_adult_previews": file.get("image_adult_previews", False),
                   "plugins_dir": str((path.parent / mine.get("plugins_dir", told.get("plugins_dir", "../plugins"))).resolve()),
                   "hosted_keys": str((path.parent / mine["hosted_keys"]).resolve()) if mine.get("hosted_keys") else "",
                   "machine_key_file": str((path.parent / mine.get("machine_key_file", "../keys/machine.key")).resolve()),
                   "auth_key_file": str((path.parent / mine.get("auth_key_file", told.get("key_file", "../keys/weightroom.key"))).resolve())},
            )
            if type(settings.wait) not in (int, float) or settings.wait <= 0:
                raise TypeError("wait is a number of seconds above 0")
            if type(settings.keep_console_settings_on_front) is not bool:
                raise TypeError("keep_console_settings_on_front must be a boolean")
            if type(settings.allow_caller_to_specify_destination) is not bool:
                raise TypeError("allow_caller_to_specify_destination must be a boolean")
            if type(settings.image_adult_previews) is not bool:
                raise TypeError("image_adult_previews must be a boolean")
            if type(settings.files_every) not in (int, float) or settings.files_every < 0:
                raise TypeError("files_every is a number of minutes, 0 or more")
            if not isinstance(settings.toollocker_url, str):
                raise TypeError("toollocker_url must be a service base URL")
            if settings.toollocker_url:
                url = urlsplit(settings.toollocker_url)
                if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
                    raise TypeError("toollocker_url must be an HTTP service base URL without credentials, path or query")
                if 8443 not in settings.ports or not settings.keep_console_settings_on_front:
                    raise TypeError("toollocker_url requires port 8443 and keep_console_settings_on_front")
            if type(settings.keep_days) not in (int, float) or settings.keep_days < 0:
                raise TypeError("keep_days is a number of days, 0 or more")
            if type(settings.max_in_flight) is not int or settings.max_in_flight < 1:
                raise TypeError("max_in_flight is a whole number of requests, 1 or more")
            if type(settings.busy_retry_seconds) not in (int, float) or settings.busy_retry_seconds <= 0:
                raise TypeError("busy_retry_seconds is a number of seconds above 0")
            if type(settings.machines_poll_seconds) not in (int, float) or settings.machines_poll_seconds <= 0:
                raise TypeError("machines_poll_seconds is a number of seconds above 0")
            if "compute" in settings.machines or any(type(one["compute"]) is not str or not one["compute"].startswith("http") for one in settings.machines.values()):
                raise TypeError("[machines.NAME] needs compute = a URL such as http://host:8442, and the name compute is the main machine's own")
            if any(one.get("spotter") and (type(one["spotter"]) is not str or not one["spotter"].startswith("http")) for one in settings.machines.values()):
                raise TypeError("[machines.NAME].spotter must be a URL such as http://host:8444")
            checked_roots({'models_folder': settings.models_folder, 'models_storage_folder': settings.models_storage_folder})
            if settings.artifact_machine and settings.artifact_machine not in {"front", "compute", *settings.machines}:
                raise TypeError("artifact_machine must name front, compute or a configured machine")
            slots = [machine_slot(name) for name in (*settings.machines, FRONT)]
            if FRONT in settings.machines or len(slots) != len(set(slots)):
                raise TypeError("Configured machine names collide in the public request-ID range; rename one machine")
            return settings
        except (KeyError, TypeError, ValueError) as problem:
            why = problem
    raise SystemExit(f"{path}: {type(why).__name__}: {why}. turnstile needs `compute`, and reads [turnstile] for the rest.") from None


def say_what_runs(path: Path, settings: Settings) -> None:
    """Leave what turnstile runs with in the folder it shares with weightroom, which passes it on to LoadCoach for the Settings page."""
    defaults = path.parent / "defaults" / path.name
    text = (defaults.read_text() if defaults.is_file() else "") + path.read_text()  # its settings are explained in either file
    file = current_names(read_settings(path).get("turnstile", {}), str(path), say=lambda text: None)
    artifact_machine = settings.artifact_machine or ("front_cpu" if "front_cpu" in settings.machines else "compute")
    running = {**file, "host": settings.host, "ports": list(settings.ports), "wait": settings.wait,
               "voice_port": settings.voice_port, "artifact_machine": artifact_machine,
               "models_folder": settings.models_folder, "models_storage_folder": settings.models_storage_folder,
               "image_adult_previews": settings.image_adult_previews, "files_every": settings.files_every,
               "allow_caller_to_specify_destination": settings.allow_caller_to_specify_destination}
    (Path(settings.data) / "settings-seen-turnstile.json").write_text(json.dumps(said(running, file, comments(text.partition("[turnstile]")[2]) | comments(text.rpartition("[turnstile]")[2]))))


def front_stores(databases: dict, base: Path, data: Path) -> DatabaseRegistry:
    """The named stores turnstile and weightroom share on the front machine: the accounts, the plugins', and the roles with their evidence and reliability."""
    return DatabaseRegistry.from_config(databases, base, {"weightroom.accounts": data / "accounts.db", "weightroom.plugins": data / "plugins.db",
                                                          "weightroom.roles": data / "roles.db"})


def wake(mac: str, address: str = "255.255.255.255", port: int = 9) -> None:
    """Send the packet that wakes a sleeping machine: six bytes of 255, then its hardware address sixteen times.

    The machine's network card listens for exactly this while the rest of it sleeps. Nothing
    answers, so there is no telling from here whether it was heard.
    """
    packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as out:
        out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        out.sendto(packet, (address, port))


def compute_client(settings: Settings) -> httpx.AsyncClient:
    """How requests reach compute. There is no read timeout: a model can think for minutes before its first token.

    The pool is the count of requests in flight, and it is not waited for (pool=0): one over
    `max_in_flight` is turned away at once, so that it is never held here, where LoadCoach's
    priorities cannot reach it.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=2, pool=0),
                             limits=httpx.Limits(max_connections=settings.max_in_flight, max_keepalive_connections=min(20, settings.max_in_flight)))


def with_power_state(hosts: list[dict], seen: dict[str, dict], notice: dict) -> list[dict]:
    """Mark the primary host asleep only from its signed sleep notice, never from silence alone."""
    if notice.get("action") != "sleep":
        return hosts
    answered_at = (seen.get("compute") or {}).get("at")
    if answered_at is not None and answered_at > notice.get("at", 0):
        notice.update(action=None, at=0.0)
        return hosts
    return [{**host, "state": "sleeping"} if "compute" in host.get("machines", []) else host for host in hosts]


def build_app(settings: Settings, client: httpx.AsyncClient | None = None, waker=wake, adapter_client: httpx.AsyncClient | None = None,
              users: Users | None = None, hosted_client: httpx.AsyncClient | None = None) -> Starlette:
    """Build the web app. `client` is how it reaches compute and `waker` what wakes it; tests hand in their own, and their own `users`."""
    slots = [machine_slot(name) for name in (*settings.machines, FRONT)]
    if "compute" in settings.machines or FRONT in settings.machines or len(slots) != len(set(slots)):
        raise ValueError("Machine names must be distinct from compute and must have distinct public request-ID slots")
    if settings.machines:
        key_files = [settings.machine_key_file, *(one["machine_key_file"] for one in settings.machines.values())]
        if any(not path or not Path(path).is_file() for path in key_files):
            raise ValueError("Every configured compute machine needs its own existing machine-key file on front")
        key_values = [statement.read_key(path) for path in key_files]
        if len(key_values) != len(set(key_values)):
            raise ValueError("Each compute machine must have a distinct machine key")
    compute = client or compute_client(settings)
    # What turnstile asks of compute for itself (the log, facts, the plugin model list) has a pool of its own, so that a full house of callers cannot starve it.
    internal = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=2), limits=httpx.Limits(max_connections=20))
    gate = Gate(settings.path)
    notice = {"action": None, "at": 0.0}
    ready_cache = {}
    nonces = statement.Nonces()  # of compute's own requests to turnstile
    adapters = adapter_client or httpx.AsyncClient(timeout=httpx.Timeout(None, connect=2))
    Path(settings.data).mkdir(parents=True, exist_ok=True)
    stores = front_stores(settings.databases, Path(settings.path).parent if settings.path else Path.cwd(), Path(settings.data))
    model_locations = ModelLocations(stores.database("weightroom.roles"))
    from loadcoach.component_versions import Versions
    from loadcoach.config import kept_by_loadcoach
    component_settings = Path(settings.path).parent / "loadcoach.toml" if settings.path else None
    component_source = (kept_by_loadcoach(component_settings)[1] if component_settings and component_settings.is_file()
                        else stores.database("weightroom.accounts"))
    component_versions = Versions(component_source)
    passive_reads = contextvars.ContextVar("turnstile_passive_reads", default=False)
    users = users or Users(stores.database("weightroom.accounts"), settings.groups)
    # The one event log: what was done here to the accounts, and since milestone 91 every entry compute sends.
    log = Log(stores.database("weightroom.accounts"), keep_days=settings.keep_days)
    log.machine_names = ("compute", *settings.machines)
    fallthrough = Fallthrough()  # what still reaches the default node only because nothing routed it

    def machine_key() -> str:
        """Read the current shared key for the selected compute authority."""
        return statement.read_key(settings.machine_key_file) if settings.machine_key_file else ""

    internal_token = secrets.token_urlsafe(32)  # never leaves this process: turnstile asking itself, for a batch, in the name of whoever handed it in

    @observation.traced("authentication")
    def caller(key: str) -> User | None:
        """The user this key belongs to. With no accounts yet and turnstile on this machine only, anyone may do anything, as a fresh install."""
        if key.startswith(internal_token + ":"):
            user = users.by_name(key.split(":", 1)[1])
        elif settings.host in LOOPBACK and not users.any():
            user = ANONYMOUS
        else:
            user = users.by_key(key) if key else None
        observation.identify(user)
        return user

    def error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
        """Build a JSON failure response using the service error envelope."""
        return JSONResponse({"error": {"message": message, "type": "turnstile_error"}}, status_code=status, headers=headers)

    def off() -> JSONResponse:
        """Compute is not answering. Said in a header too, so that weightroom can tell this from any other refusal."""
        return error(503, "Compute is not answering.", {"x-turnstile": "compute-off"})

    def passive(request: Request) -> bool:
        """Page reads may use the configured monitor instead of retrying offline nodes."""
        return NO_WAKE in request.headers and bool(settings.auth_key_file)

    def signed_headers(user: User | None, method: str, where: str, body: bytes, headers: list, origin: str = "", key_file: str = "") -> tuple[list, str]:
        """The headers to send on: without the caller's key, and with a statement of who they are and where from. Also the statement's nonce."""
        sent = [(name, value) for name, value in headers if name not in HOP_HEADERS and name not in ("authorization", statement.HEADER)]
        key = statement.read_key(key_file) if key_file else machine_key()
        if user is None or not key:
            return sent, ""
        said, nonce = statement.make(key, method, where, body, statement.caller_of(user), origin=origin)
        return [*sent, (statement.HEADER, said)], nonce

    def proven(upstream: httpx.Response, nonce: str, key_file: str = "") -> bool:
        """Whether an answer carries the proof that it comes from the machine holding the key. Nothing to prove for an unsigned request."""
        given = upstream.headers.get(statement.PROOF, "")
        return not nonce or any(secrets.compare_digest(given, statement.proof(key, nonce)) for key in statement.keys(key_file or settings.machine_key_file))

    def lifecycle_refusal(name: str, method: str, where: str) -> str:
        """Stop new work while allowing disabled-node observation and maintenance."""
        state = machines.state(name)
        maintenance = where.partition('?')[0]
        allowed = method == "GET" or maintenance.startswith(("/loadcoach/settings/", "/loadcoach/stop/")) or maintenance in {
            "/loadcoach/drain", "/loadcoach/clear", "/loadcoach/unload", "/loadcoach/cards",
            "/loadcoach/model-lock", "/loadcoach/machine-key", "/loadcoach/send-log"}
        if state == "retired" or (state == "disabled" and not allowed):
            return f"Compute node {name} is {state}. Enable it in Settings → Compute nodes before using it."
        return ""

    async def ask_compute(user: User, method: str, where: str, body: bytes = b"", port: int | None = None,
                          machine: str = "", timeout: float | None = 30, forwarded: tuple = ()) -> httpx.Response | None:
        """Ask compute something as `user`, without waking it, and read the whole answer. None if it is not answering or its answer is not proven.

        Unless another `port` is given, LoadCoach is asked: the first of `ports`. `machine` names one of
        `settings.machines` instead of the main machine, which is signed with its own key.
        """
        if why := lifecycle_refusal(machine or "compute", method, where):
            try:
                releasing = where == '/loadcoach/loadouts' and method == 'POST' and json.loads(body).get('action') == 'release'
            except (ValueError, AttributeError):
                releasing = False
            if not releasing:
                return httpx.Response(409, json={"error": {"message": why}})
        # Log flushing is observation too; its POST must not make offline reports wait.
        observing = method == "GET" or (method == "POST" and where == "/loadcoach/send-log")
        if observing and passive_reads.get() and settings.auth_key_file and machines.available(machine or "compute") is False:
            return None
        if method == "GET":
            where = image_work.discovery_path(user, where)
        port = port or settings.ports[0]
        other = settings.machines.get(machine, {})
        base = (other.get("spotter") if len(settings.ports) > 2 and port == settings.ports[2] and other.get("spotter") else other["compute"]).rstrip("/") if other else settings.upstream(port)
        headers, nonce = signed_headers(user, method, where, body,
                                        ([('content-type', 'application/json')] if body else []) + list(forwarded),
                                        key_file=other.get("machine_key_file", ""))
        try:
            answer = await internal.request(method, f"{base}{where}", headers=headers, content=body,
                                            timeout=httpx.Timeout(timeout, connect=2))
        except httpx.HTTPError:
            return None
        return answer if proven(answer, nonce, other.get("machine_key_file", "")) else None

    # Cache only discovery/configuration, under the complete current caller policy.
    # Runtime status and authorization failures are never replaced with old successes.
    retained_paths = {"/v1/models", "/loadcoach/facts", "/loadcoach/me", "/loadcoach/models", "/loadcoach/settings", "/loadcoach/engines"}
    discovery_refreshes: dict[str, asyncio.Task] = {}

    def discovery_key(user, machine, path):
        """Bind retained node metadata to the current identity, policy and target."""
        import hashlib
        from dataclasses import asdict
        target = settings.machines.get(machine, {}) if machine and machine != "compute" else {"compute": settings.compute}
        scope = json.dumps([user.identity or user.name, asdict(user.group), machine or "compute", target, path],
                           sort_keys=True, default=lambda value: sorted(value))
        return hashlib.sha256(scope.encode()).hexdigest()

    async def remembered(user, method, where, body=b"", **kwargs):
        """Augment front identity with permitted deployed models, including sleeping workers."""
        if method != "GET" or where != "/loadcoach/me":
            return await ask_compute(user, method, where, body, **kwargs)
        async def metadata(name):
            """Reuse policy-scoped model metadata; refresh old observations separately."""
            key = discovery_key(user, name, where)
            row = log.db.execute("SELECT at, data FROM node_discovery WHERE scope=?", (key,)).fetchone()
            available = machines.available(name)
            if row is None or available is None or not settings.auth_key_file:
                return await ask_live(user, method, where, machine=name, prime=True, passive=bool(settings.auth_key_file))
            if available is not False and time.time() - row[0] >= settings.machines_poll_seconds and key not in discovery_refreshes:
                async def refresh():
                    """Renew the retained discovery answer in the background; a failure keeps the old one."""
                    try:
                        await ask_live(user, method, where, machine=name, prime=True)
                    except Exception:
                        print('turnstile: model metadata refresh failed; retrying on next read.', flush=True)
                    finally:
                        discovery_refreshes.pop(key, None)
                discovery_refreshes[key] = asyncio.create_task(refresh(), context=contextvars.Context())
            return httpx.Response(200, json=json.loads(row[1]),
                                  headers={"x-suite-retained": "1"} if available is False else {})

        names = machines.active_names
        answers = await asyncio.gather(*(metadata(name) for name in names))
        good = [answer.json() for answer in answers if answer is not None and answer.status_code == 200]
        if not good:
            return httpx.Response(200, json={"models": [], "chat": {"models": {}}, "unavailable_machines": names})
        result = {**good[0], "models": [], "chat": {**good[0].get("chat", {}), "models": {}}}
        for told in good:
            result["models"].extend(name for name in told.get("models", []) if name not in result["models"])
            result["chat"]["models"].update(told.get("chat", {}).get("models", {}))
        visible = {}
        for name in names:
            row = log.db.execute("SELECT data FROM node_discovery WHERE scope=?",
                                 (discovery_key(user, name, "/v1/models"),)).fetchone()
            if row:
                visible[name] = json.loads(row[0])
        details = {name: answer.json().get("chat", {}).get("models", {}) for name, answer in zip(names, answers)
                   if answer is not None and answer.status_code == 200}
        if visible:
            covered = {alias["id"] for told in visible.values() for alias in told.get("data", [])}
            result["models"] = [name for name in result["models"] if name not in covered]
            result["chat"]["models"] = {name: options for name, options in result["chat"]["models"].items() if name not in covered}
            for logical in logical_catalogue(visible):
                places = [place for place in logical["locations"]
                          if model_locations.enabled(place["machine"], place.get("model_id"))]
                metadata = next((details.get(place["machine"], {}).get(place["id"]) for place in places
                                 if place["id"] in details.get(place["machine"], {})), None)
                if metadata is not None:
                    result["models"].append(logical["id"])
                    result["chat"]["models"][logical["id"]] = metadata
        result["unavailable_machines"] = [name for name, answer in zip(names, answers)
                                          if answer is None or answer.headers.get("x-suite-retained") or answer.status_code != 200]
        return httpx.Response(200, json=result)

    async def ask_live(user: User, method: str, where: str, machine: str = "", port: int | None = None, *, prime: bool = False, passive: bool = False) -> httpx.Response | None:
        """A bounded inventory/placement question; one silent machine must not stall the others."""
        if why := lifecycle_refusal(machine or "compute", method, where):
            return httpx.Response(409, json={"error": {"message": why}})
        try:
            answer = (None if passive and machines.available(machine or "compute") is False else
                      await asyncio.wait_for(ask_compute(user, method, where, machine=machine, port=port), timeout=5))
        except TimeoutError:
            answer = None
        key = discovery_key(user, machine, where)
        if method == "GET" and where in retained_paths and port is None:
            if answer is not None:
                if answer.status_code == 200:
                    value = answer.json()
                    if where == "/loadcoach/me":
                        value = {field: value[field] for field in ("models", "chat") if field in value}
                    if prime and where in ("/v1/models", "/loadcoach/me"):
                        # Retain routing metadata when the caller sees a deployed model,
                        # so first use after sleep can identify its owning worker.
                        facts_key = discovery_key(user, machine, "/loadcoach/facts")
                        seen = log.db.execute("SELECT at FROM node_discovery WHERE scope=?", (facts_key,)).fetchone()
                        if not seen or time.time() - seen[0] > roles.FACTS_FOR:
                            await ask_live(user, "GET", "/loadcoach/facts", machine=machine)
                    log.db.execute("INSERT INTO node_discovery(scope, at, data) VALUES (?, ?, ?) "
                                   "ON CONFLICT(scope) DO UPDATE SET at=excluded.at, data=excluded.data",
                                   (key, time.time(), json.dumps(value)))
                else:
                    log.db.execute("DELETE FROM node_discovery WHERE scope=?", (key,))
            else:
                row = log.db.execute("SELECT data FROM node_discovery WHERE scope=?", (key,)).fetchone()
                if row:
                    value = json.loads(row[0])
                    if where == "/loadcoach/facts":
                        value["compute_unavailable"] = True
                    if where == "/loadcoach/models":
                        value["measuring"], value["to_measure"] = None, []
                        for model in value.get("models", []):
                            model.update(unavailable=True, loaded=None, running=0, waiting=0, context_loaded=None)
                    return httpx.Response(200, json=value, headers={"x-suite-retained": "1"})
        return answer

    async def ask_for(authorization: str, method: str, where: str) -> httpx.Response | None:
        """For the plugin path: ask compute as the caller this key belongs to, without waking it. The model list has the roles added, as on the way through."""
        user = caller(authorization.removeprefix("Bearer ").strip())
        if user and method == "GET" and where == "/v1/models":
            # Plugins use the same scoped catalogue and availability contract as
            # ordinary clients; discovery must not wake an unavailable worker.
            discovery = Request({"type": "http", "method": "GET", "path": where,
                                 "headers": [(NO_WAKE.encode(), b"1")]})
            answer = await combined_view(discovery, user, where)
            return httpx.Response(answer.status_code, content=answer.body, headers=dict(answer.headers))
        return await ask_compute(user, method, where) if user else None

    @observation.traced("reach_and_readiness")
    async def woken(request: Request, user: User | None, attempt, machine: str = ""):
        """What `attempt()` gives once compute answers, waking compute for it if this caller may. Or the response that ends the request.

        `attempt()` gives None while compute is not there. Any valid key may wake it; a request that says NO_WAKE, or has no key, never does.
        """
        policy = settings.machines.get(machine, {}) if machine and machine != "compute" else {
            "wake_mac": settings.wake_mac, "wake_address": settings.wake_address}
        if why := lifecycle_refusal(machine or "compute", request.method, request.url.path):
            return error(409, why)
        got = None if passive(request) and machines.available(machine or "compute") is False else await attempt()
        if got is None:
            if not machines.enabled(machine or "compute"):
                return error(409, f"Compute node {machine or 'compute'} is {machines.state(machine or 'compute')}; wake is disabled.")
            if (not policy.get("wake_mac") or user is None or NO_WAKE in request.headers or
                request.url.path.rstrip('/') in NO_WAKE_PATHS or
                (machine and not request.url.path.startswith("/v1/"))):
                return (error(503, f"Machine {machine} is not answering.", {"retry-after": f"{settings.busy_retry_seconds:g}"})
                        if machine else off())
            print(f"turnstile: waking compute for a request to {request.url.path}", flush=True)
            give_up = time.monotonic() + settings.wait
            while got is None:
                if not machines.enabled(machine or "compute"):
                    return error(409, f"Compute node {machine or 'compute'} is no longer active; wake was cancelled.")
                if time.monotonic() > give_up:
                    return error(504, f"Compute was asked to wake and has not answered in {settings.wait:g} seconds.")
                try:
                    waker(policy["wake_mac"], policy.get("wake_address", "255.255.255.255"))
                except (OSError, ValueError):
                    return error(503, "Could not send the wake packet. Check the node wake configuration.")
                remaining = give_up - time.monotonic()
                if remaining <= 0:
                    return error(504, f"Node {machine or 'compute'} was asked to wake and has not answered in {settings.wait:g} seconds.")
                await asyncio.sleep(min(TRY_EVERY, remaining))
                try:
                    got = await asyncio.wait_for(attempt(), max(0.001, give_up - time.monotonic()))
                except TimeoutError:
                    return error(504, f"Node {machine or 'compute'} was asked to wake and has not answered in {settings.wait:g} seconds.")
        return got

    @observation.traced("transport_headers")
    async def through(request: Request, user: User | None, method: str, where: str, body: bytes, headers: list, port: int | None = None,
                      machine: str = "") -> httpx.Response | Response:
        """Compute's answer to a request, with its body still to come, and proven to be compute's. Or the response that ends the request.

        `where` is the path with its query, as it is signed. Unless another `port` is given, LoadCoach is asked.
        """
        if method == "GET":
            where = image_work.discovery_path(user, where)
        port = port or settings.ports[0]
        other = settings.machines.get(machine, {}) if machine != "compute" else {}
        front_tools = not machine and port == 8443 and bool(settings.toollocker_url)
        if not front_tools and (why := lifecycle_refusal(machine or "compute", method, where)):
            return error(409, why)
        base = settings.toollocker_url.rstrip("/") if front_tools else other["compute"].rstrip("/") if other else settings.upstream(port)
        key_file = other.get("machine_key_file", "")

        async def reach() -> tuple[httpx.Response, str] | Response | None:
            """Send the request to the selected compute service and verify its machine proof."""
            if not front_tools and (why := lifecycle_refusal(machine or "compute", method, where)):
                return error(409, why)
            signed, nonce = signed_headers(user, method, where, body, headers, request.client.host if request.client else "", key_file)
            async def disconnected():
                """Wait for departure after the complete request body has been consumed."""
                while (await request.receive())['type'] != 'http.disconnect':
                    pass

            sending = asyncio.create_task(compute.send(compute.build_request(method, f"{base}{where}", headers=signed, content=body), stream=True))
            gone = asyncio.create_task(disconnected())
            forwarded = False
            try:
                done, _ = await asyncio.wait({sending, gone}, return_when=asyncio.FIRST_COMPLETED)
                if gone in done:
                    return Response(status_code=499)
                answer = await sending
                forwarded = True
                return answer, nonce
            except (httpx.ConnectError, httpx.ConnectTimeout):
                return None
            finally:
                for pending in (sending, gone):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(sending, gone, return_exceptions=True)
                if not forwarded and not sending.cancelled() and sending.exception() is None:
                    await sending.result().aclose()

        try:
            reached = await reach() if front_tools else await woken(request, user, reach, machine)
        except httpx.PoolTimeout:  # max_in_flight are already under way: say so at once, so that the caller retries
            return error(503, f"Turnstile is already carrying {settings.max_in_flight} requests to compute. Try again shortly.",
                         {"x-turnstile": "busy", "retry-after": f"{settings.busy_retry_seconds:g}"})
        except httpx.HTTPError as problem:
            return error(503 if machine else 502, f"Compute stopped answering before a response: {type(problem).__name__}.",
                         {"retry-after": f"{settings.busy_retry_seconds:g}"} if machine else None)
        if reached is None:
            if front_tools:
                return error(503, "Front ToolLocker is not answering.", {"retry-after": f"{settings.busy_retry_seconds:g}"})
            return error(503, f"Machine {machine} is not answering.", {"retry-after": f"{settings.busy_retry_seconds:g}"})
        if isinstance(reached, Response):
            return reached
        upstream, nonce = reached
        if not proven(upstream, nonce, key_file):
            await upstream.aclose()
            return error(502, "The answer did not prove it came from compute: it was not signed with the machine key.")
        if machine:
            upstream.extensions["suite_machine"] = machine
            if number := upstream.headers.get("x-loadcoach-request-id", ""):
                if number.isdigit():
                    upstream.headers["x-loadcoach-request-id"] = str(public_number(machine, int(number)))
        try:
            streaming = json.loads(body).get("stream") is True
        except (ValueError, AttributeError):
            streaming = False
        path = where.partition("?")[0]
        # Read JSON/admin and non-streaming model answers before sending their status. A
        # dropped compute connection can then become a clean 502 (or a retryable 503 on a
        # secondary machine), instead of a success/error status with a broken body.
        buffer_answer = not streaming and (machine or upstream.headers.get("content-type", "").startswith("application/json")
                                           or method == "POST" and path.startswith("/v1/") and not path.startswith(("/v1/files", "/v1/batches")))
        if buffer_answer:
            try:
                complete = await upstream.aread()
            except httpx.HTTPError as problem:
                await upstream.aclose()
                status = 503 if machine else 502
                return error(status, f"Compute stopped before it sent a complete answer: {type(problem).__name__}.",
                             {"retry-after": f"{settings.busy_retry_seconds:g}"} if machine else None)
            await upstream.aclose()
            return httpx.Response(upstream.status_code, headers=upstream.headers, content=complete,
                                  request=upstream.request, extensions={"suite_machine": machine, "suite_complete": complete})
        if machine or streaming and method == "POST" and path.startswith("/v1/"):
            iterator = upstream.aiter_raw()
            try:
                upstream.extensions["suite_first_piece"] = await anext(iterator, b"")
                upstream.extensions["suite_iterator"] = iterator
            except httpx.HTTPError as problem:
                await upstream.aclose()
                status = 503 if machine else 502
                return error(status, f"Compute stopped before it sent an answer: {type(problem).__name__}.",
                             {"retry-after": f"{settings.busy_retry_seconds:g}"} if machine else None)
        return upstream

    def relay(upstream: httpx.Response, judge=None) -> StreamingResponse:
        """Hand compute's answer back as it arrives, so a streamed answer stays a stream. `judge(whole, complete, broke)` is told how it ended, with all of it."""
        whole = bytearray()
        from mirrorwall.stream import Capture as ImageCapture
        from soundroom.stream import Capture as AudioCapture
        media = ImageCapture if getattr(judge, 'image_request', False) else AudioCapture if getattr(judge, 'audio_request', False) else None
        image_capture = media(upstream.headers.get('content-type', '')) if media else None

        def retain(piece):
            """Keep image and audio metadata bounded while ordinary replies retain their diagnostic bytes."""
            if image_capture:
                image_capture.feed(piece)
            else:
                whole.extend(piece)

        async def answer():
            """Stream the proxied reply and close its upstream connection on exit."""
            complete, broke = False, ""
            try:
                if "suite_complete" in upstream.extensions:
                    piece = upstream.extensions["suite_complete"]
                    if judge:
                        retain(piece)
                    yield piece
                elif "suite_iterator" in upstream.extensions and upstream.extensions.get("suite_first_piece"):
                    piece = upstream.extensions["suite_first_piece"]
                    if judge:
                        retain(piece)
                    yield piece
                if "suite_complete" not in upstream.extensions:
                    async for piece in upstream.extensions.get("suite_iterator") or upstream.aiter_raw():
                        if judge:
                            retain(piece)
                        yield piece
                complete = True
            except (httpx.HTTPError, httpx.StreamError) as problem:  # compute stopped answering part-way
                broke = "timeout" if isinstance(problem, httpx.TimeoutException) else "error"
                raise
            finally:
                await upstream.aclose()  # also when the caller hangs up, which is how compute learns of it
                if judge:
                    judge(image_capture.snapshot() if image_capture else bytes(whole), complete, broke)

        # The body is passed on byte for byte, so the length compute gave for it still holds. Saying it
        # lets the answer end with its last byte, where one of unknown length needs a closing piece of its own.
        passed = {name: value for name, value in upstream.headers.items() if (name not in HOP_HEADERS or name == "content-length") and name != statement.PROOF}
        return StreamingResponse(answer(), status_code=upstream.status_code, headers=passed)

    facts_seen: dict[str, tuple[float, dict]] = {}

    async def facts_for(request: Request, user: User) -> dict | Response:
        """What compute says of the models this user may use, kept roles.FACTS_FOR seconds. Compute is woken for it if this caller may wake it."""
        scope = discovery_key(user, "", "/loadcoach/facts")
        seen = facts_seen.get(scope)
        if seen and time.monotonic() - seen[0] < roles.FACTS_FOR:
            return seen[1]
        if not machines.active_names:
            return {"models": hosted.facts(user), "environment": {}, "compute_unavailable": True,
                    "placements": {}, "location_models": {}}
        if not settings.machines:
            answer = await woken(request, user, lambda: ask_live(user, "GET", "/loadcoach/facts"))
            if isinstance(answer, Response):
                return answer
            if answer.status_code != 200:
                return JSONResponse(answer.json(), status_code=answer.status_code)
            said = answer.json()
            said["models"] = {**said["models"], **hosted.facts(user)}
            facts_seen[scope] = (time.monotonic(), said)
            return said
        reports = {one["name"]: one for one in machines.report()}
        names = machines.active_names
        answers = await asyncio.gather(*(ask_live(user, "GET", "/loadcoach/facts", machine="" if name == "compute" else name)
                                         for name in names))
        fresh = {name: answer.json() for name, answer in zip(names, answers) if answer is not None and answer.status_code == 200}
        if not fresh:
            answer = await woken(request, user, lambda: ask_live(user, "GET", "/loadcoach/facts"))
            if isinstance(answer, Response):
                return answer
            if answer.status_code != 200:
                return JSONResponse(answer.json(), status_code=answer.status_code)
            fresh = {"compute": answer.json()}
        catalogue = logical_catalogue({machine_name: {"data": [
            {"id": name, "owned_by": "loadcoach",
             "content_sha256": next((one["content"] for one in reports.get(machine_name, {}).get("models", []) if one["name"] == name), None),
             **{key: fact[key] for key in ("model_id", "fingerprint") if key in fact}}
            for name, fact in told.get("models", {}).items()]}
            for machine_name, told in fresh.items()})
        variants: dict[str, list[tuple[str, dict]]] = {}
        location_models: dict[str, dict[str, str]] = {}
        for logical in catalogue:
            name = logical["id"]
            if name in variants:
                continue  # conflicting unverified aliases retain the first visible machine, as before
            choices = []
            for location in logical.get("locations", []):
                fact = fresh.get(location["machine"], {}).get("models", {}).get(location["id"])
                if fact is not None and model_locations.enabled(location["machine"], fact.get("model_id")):
                    choices.append((location["machine"], fact))
                    location_models.setdefault(name, {})[location["machine"]] = location["id"]
            if choices:
                variants[name] = choices
        live = {name for name, answer in zip(names, answers) if answer is not None and answer.status_code == 200
                and not answer.headers.get("x-suite-retained")}
        ordered = {name: sorted(placement_order(choices, reports), key=lambda item: item[0] not in live)
                   for name, choices in variants.items()}
        models = {name: {**choices[0][1], "machine": choices[0][0],
                         "model_ids": [fact.get("model_id") for _, fact in choices if fact.get("model_id")]}
                  for name, choices in ordered.items()}
        said = {"models": {**models, **hosted.facts(user)}, "compute_unavailable": not bool(live),
                "environment": fresh.get("compute", next(iter(fresh.values()))).get("environment", {}),
                "placements": {name: [machine_name for machine_name, fact in choices if not fact.get("why_not")]
                               for name, choices in ordered.items()},
                "location_models": location_models}
        # A cached, caller-visible alias on an unavailable worker is known but down,
        # rather than an unknown model to forward to the primary for a misleading 404.
        known = {model["name"] for report in reports.values() for model in report.get("models", [])
                 if isinstance(model.get("name"), str) and user.group.may_use(model["name"])}
        said["unavailable_models"] = sorted(known - said["models"].keys())
        facts_seen[scope] = (time.monotonic(), said)
        return said

    routing = roles.Routing(stores.database("weightroom.roles"), log, caller, facts_for, through, relay, placed=bool(settings.machines))

    async def local_names(user: User) -> set[str]:
        """The names compute's models go by now, asked of it, and failing that as the inventory last saw them: an outside service may not take one."""
        answer = await ask_compute(user, "GET", "/v1/models")
        if answer is not None and answer.status_code == 200:
            return {one["id"] for one in answer.json().get("data", []) if one.get("owned_by") == "loadcoach"}
        return {name for one in machines.models() for name in one["names"]}
    # The outside services, their keys and the checks on what may leave are front's. Their keys are files beside the machine key.
    keys_folder = settings.hosted_keys or (str(Path(settings.machine_key_file).parent) if settings.machine_key_file else str(Path(settings.data)))
    hosted = Hosted(Services(stores.database("weightroom.accounts"), keys_folder),
                    log, hosted_client or httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10, pool=0), limits=httpx.Limits(max_connections=settings.max_in_flight)),
                    routing.reliability, users, local_names)
    routing.hosted = hosted
    # Batches are front's: their files and requests are kept in the accounts store, and each request goes in through turnstile's own door.
    batch_work = FrontBatches(Batches(stores.database("weightroom.accounts")), log, lambda *asked: ask_compute(*asked), None, lambda name: f"{internal_token}:{name}")

    async def dispatch_probe(user, path, payload, machine):
        """Send a signed scoped benchmark probe to its owning compute node."""
        other = settings.machines.get(machine, {})
        if machine != "compute" and not other:
            return None  # a removed frozen machine is never silently replaced
        key_file = other.get("machine_key_file") or settings.machine_key_file
        if not key_file:
            return None
        body = json.dumps(payload).encode()
        key = statement.read_key(key_file)
        header, nonce = statement.make(key, "POST", path, body, statement.caller_of(user), purpose="front-probe")
        base = other["compute"].rstrip("/") if other else settings.upstream(settings.ports[0])
        try:
            answer = await compute.post(base + path, content=body,
                headers={statement.HEADER: header, "content-type": "application/json"}, timeout=httpx.Timeout(None, connect=2))
        except httpx.HTTPError:
            return None
        return answer if proven(answer, nonce, key_file) else None

    scheduled_wakes = {}

    async def scheduled_node_ready(owner, name, identifier, due, token):
        """Wake a due schedule's node after current owner and node-policy checks; retry later."""
        if name not in machines.names:
            return error(409, 'The scheduled compute node is no longer configured.')
        if not machines.enabled(name):
            return error(409, f'Compute node {name} is {machines.state(name)}; wake is disabled.')
        answer = await ask_compute(owner, 'GET', '/loadcoach/status', machine=name, timeout=5)
        if answer is not None:
            return None
        # Permissions, grant rotation and node lifecycle may change during the readiness probe.
        if not machines.enabled(name):
            return error(409, f'Compute node {name} is {machines.state(name)}; wake is disabled.')
        try:
            current, _ = task_records.grants.authorized(identifier, token, due)
        except PermissionError as problem:
            return error(403, str(problem))
        if not current.can('front-wake'):
            return error(403, 'Waking a scheduled node requires the owner to retain manage-settings and front-wake.')
        policy = settings.machines.get(name, {}) if name != 'compute' else {
            'wake_mac': settings.wake_mac, 'wake_address': settings.wake_address}
        if not policy.get('wake_mac'):
            return error(503, f'Node {name} is not answering; configure its Wake-on-LAN settings or wake it manually.', {'retry-after': '15'})
        now = time.monotonic()
        if now - scheduled_wakes.get(name, float('-inf')) >= 30:
            try:
                waker(policy['wake_mac'], policy.get('wake_address', '255.255.255.255'))
            except (OSError, ValueError):
                return error(503, f'Could not send the wake packet for {name}. Check its Wake-on-LAN settings.', {'retry-after': '15'})
            scheduled_wakes[name] = now
            log.add('schedule', 'wake-requested', current.name, 'scheduler', schedule=identifier, due_at=due, machine=name)
        return error(503, f'Asked {name} to wake; waiting for the node to answer before submitting scheduled work.', {'retry-after': '15'})

    task_records = TaskRecords(batch_work.store, users, log, routing.evidence, ask_compute,
                               dispatch_probe, ["compute", *settings.machines], scheduled_node_ready,
                               destination_allowed=lambda: console_settings.changes.of('turnstile').get(
                                   'allow_caller_to_specify_destination', settings.allow_caller_to_specify_destination) is True)
    task_records.locations = model_locations
    from mirrorwall.store import Store as ImageStore
    from mirrorwall.front import Front as ImageFront
    console_settings = ConsoleSettings(stores.database('weightroom.accounts'), log, ask_compute, settings.keep_console_settings_on_front,
                                   programs=FRONT_PROGRAMS | {'toollocker'} if settings.toollocker_url else FRONT_PROGRAMS)

    configured_file_roots = {name: getattr(settings, name) for name in ('models_folder', 'models_storage_folder')}
    if settings.path and Path(settings.path).is_file():
        configured = read_settings(Path(settings.path)).get('turnstile', {})
        configured_file_roots = {name: configured.get(name, '') for name in configured_file_roots}

    def front_file_roots():
        """Read current front storage settings without depending on any compute node."""
        changed = console_settings.changes.of('turnstile')
        return {name: changed.get(name, value) for name, value in configured_file_roots.items()}

    def front_files_every() -> float:
        """Seconds between looks through front's own folders, as saved on the site now."""
        minutes = console_settings.changes.of('turnstile').get('files_every', settings.files_every)
        return float(minutes) * 60 if type(minutes) in (int, float) and minutes >= 0 else settings.files_every * 60

    front_files = FrontFiles(front_file_roots, log, index=Path(settings.data) / FRONT_INDEX, every=front_files_every)

    def file_store():
        """Prefer an explicitly selected store, then optional front storage, then legacy workers."""
        return settings.artifact_machine or ('front' if any(front_file_roots().values()) else artifact_machine)
    image_work=ImageFront(ImageStore(batch_work.store.db,settings.data),batch_work,users,
                         adult_previews_allowed=lambda: console_settings.changes.of('turnstile').get(
                             'image_adult_previews', settings.image_adult_previews) is True)
    batch_work.image_work=image_work
    from soundroom.store import Store as AudioStore
    from soundroom.front import Front as AudioFront
    audio_work = AudioFront(AudioStore(batch_work.store.db, settings.data), batch_work, users, log)
    batch_work.audio_work = audio_work
    batch_work.task_owner = lambda user,row: task_records.visible(user,row) and image_work.visible(user,row) and audio_work.visible(user,row)

    def identify(authorization: str) -> dict:
        """For the plugin path: the caller this key belongs to, as the plugins describe a user."""
        scheme, _, key = authorization.partition(" ")
        user = caller(key) if scheme.lower() == "bearer" and key else None
        if user is None or user is ANONYMOUS:
            raise AdapterError("Not authorized.", 401, "invalid_api_key")
        group = user.group
        allowed = "*" if group.models is None else list(group.models)
        known = [model["id"] for model in plugins.registry.auth.snapshot().get("models", [])]
        return {"name": user.name, "identity": user.identity, "can": sorted(group.can), "model_access": allowed,
                "models": [one for one in known if allowed == "*" or one in allowed], "roles": [],
                "max_requests": group.max_requests, "max_sensitivity": group.max_sensitivity, "via": user.via}

    plugins = (PluginRouter(Registry(Path(settings.plugins_dir), Path(settings.data), stores.database("weightroom.plugins")),
                            adapters, identify, ask_for) if settings.plugins_dir else None)

    def request_model_choices(user: User) -> set[str]:
        """Use configured native/provider/adapter catalogs, including models without any requests."""
        names = {name for model in machines.models() for name in model['names'] if user.group.may_use(name)}
        names.update(hosted.offered(user))
        if plugins and user.can('front-plugins'):
            names.update(row['id'] for row in plugins.registry.models({
                'can': ['front-plugins'], 'model_access': '*' if user.group.models is None else list(user.group.models)}))
        return names

    def from_compute(request: Request, body: bytes) -> bool:
        """Verify a configured machine's claim using that machine's key, including its rotation overlap."""
        try:
            encoded = request.headers.get(statement.HEADER, "").partition(".")[0]
            name = json.loads(base64.urlsafe_b64decode(encoded))["machine"]
            key_file = settings.machine_key_file if name == "compute" else settings.machines[name]["machine_key_file"]
        except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
            return False
        if not isinstance(name, str) or not key_file:
            return False
        for key in statement.keys(key_file):
            try:
                said = statement.check(key, request.headers.get(statement.HEADER, ""), request.method,
                                       statement.address(request.url.path, request.url.query), body, nonces)
            except (ValueError, KeyError, TypeError):
                continue
            if said.get("machine") != name:
                return False
            request.state.nonce, request.state.key, request.state.machine = said["nonce"], key, name
            return True
        return False

    def to_compute(request: Request, answer: JSONResponse) -> JSONResponse:
        """Route a private request through the primary compute authority."""
        answer.headers[statement.PROOF] = statement.proof(request.state.key, request.state.nonce)
        return answer

    rotation_lock = asyncio.Lock()

    def rotation_file(name: str) -> str:
        """Resolve the private state file used for machine-key rotation."""
        return settings.machine_key_file if name == "compute" else settings.machines.get(name, {}).get("machine_key_file", "")

    def rotation_status(name: str) -> dict:
        """Read the selected node's saved machine-key rotation status."""
        key_file = rotation_file(name)
        row = users.db.execute(
            "SELECT interval_days,owner_id,next_at,last_error FROM machine_key_rotations WHERE machine=?", (name,)).fetchone()
        return {"machine": name, "changed": Path(key_file).stat().st_mtime, "rotation_days": row[0] if row else 0,
                "next_rotation": row[2] if row else None, "last_error": row[3] if row else ""}

    async def replace_machine_key(name: str, user: User) -> str:
        """Rotate one named node only after it accepts the sealed replacement."""
        key_file = rotation_file(name)
        new, salt = secrets.token_urlsafe(32), secrets.token_hex(16)
        body = json.dumps({"salt": salt, "sealed": statement.seal(statement.read_key(key_file), salt, new)}).encode()
        answer = await ask_compute(user, "POST", "/loadcoach/machine-key", body,
                                   machine="" if name == "compute" else name)
        if answer is None:
            return f"LoadCoach {name} did not answer, so its key was not changed."
        if answer.status_code != 200:
            return answer.json().get("error", {}).get("message", f"LoadCoach {name} refused the new key.")
        statement.replace_key(key_file, new)
        row = users.db.execute("SELECT interval_days FROM machine_key_rotations WHERE machine=?", (name,)).fetchone()
        with users.db:
            if row:
                users.db.execute("UPDATE machine_key_rotations SET next_at=?,last_error='' WHERE machine=?",
                                 (time.time() + row[0] * 86400, name))
        log.add("machine-key", "replaced", user.name, "scheduled" if not user.key_identity else "operator", machine=name)
        return ""

    async def rotate(request: Request) -> Response:
        """Read, schedule or immediately rotate one node's machine key."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        name = request.path_params.get("name") or "compute"
        if name not in machines.names:
            return error(404, "That LoadCoach is not configured.")
        if not user.can("manage-settings") or not rotation_file(name):
            return error(403, "Your group may not change the machine key.")
        if request.method == "GET":
            return JSONResponse(rotation_status(name), headers={"cache-control": "no-store"})
        body = await request.body()
        if body:
            try:
                asked = json.loads(body)
                days = asked["rotation_days"]
                if type(days) is not int or not 0 <= days <= 3650:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return error(400, "Rotation days must be a whole number from 0 through 3650; zero disables the schedule.")
            with users.db:
                if days:
                    users.db.execute("INSERT INTO machine_key_rotations(machine,interval_days,owner_id,next_at,last_error) VALUES(?,?,?,?,?) "
                                     "ON CONFLICT(machine) DO UPDATE SET interval_days=excluded.interval_days,owner_id=excluded.owner_id,next_at=excluded.next_at,last_error=''",
                                     (name, days, user.identity or user.name, rotation_status(name)["changed"] + days * 86400, ""))
                else:
                    users.db.execute("DELETE FROM machine_key_rotations WHERE machine=?", (name,))
            return JSONResponse(rotation_status(name))
        async with rotation_lock:
            if why := await replace_machine_key(name, user):
                return error(503, why)
        return JSONResponse({"replaced": True, **rotation_status(name)})

    async def rotate_due_keys_once(now: float | None = None) -> None:
        """Run currently due rotations without waking or falling back to another node."""
        now = time.time() if now is None else now
        due = users.db.execute("SELECT machine,owner_id FROM machine_key_rotations WHERE next_at<=?", (now,)).fetchall()
        for name, owner_id in due:
            user = users.by_identity(owner_id)
            why = "The schedule owner is unavailable or no longer has manage-settings."
            if machines.state(name) == "retired":
                continue
            if user and user.can("manage-settings") and name in machines.names:
                async with rotation_lock:
                    why = await replace_machine_key(name, user)
            if why:
                with users.db:
                    users.db.execute("UPDATE machine_key_rotations SET next_at=?,last_error=? WHERE machine=?",
                                     (now + 3600, why, name))

    async def rotate_due_keys() -> None:
        """Periodically rotate machine keys whose configured renewal is due."""
        while True:
            await asyncio.sleep(60)
            await rotate_due_keys_once()

    async def log_from_compute(request: Request) -> Response:
        """Compute's log entries, which it sends every few seconds: kept and chained here, the one log (milestone 91)."""
        body = await request.body()
        if not machine_key() or not from_compute(request, body):
            return error(403, "Only compute, with the machine key, may send this.")
        sent = json.loads(body)
        through = log.take(request.state.machine, sent["entries"], sent["texts"], sent["keep_text"])
        from loadcoach.observation_store import receive
        try:
            await asyncio.to_thread(receive, log.db.database, sent.get('observations', []), request.state.machine, sent.get('observation_health', {}))
        except (ValueError, TypeError):
            return error(400, 'Invalid optional observation metadata.')
        return to_compute(request, JSONResponse({"through": through}))

    async def log_facts(request: Request) -> Response:
        """For compute: how fast each model answers, how long it last took to load, and when it was last used, from the log kept here."""
        if not machine_key() or not from_compute(request, await request.body()):
            return error(403, "Only compute, with the machine key, may ask this.")
        try:
            facts = await run_report(log, lambda view: {**view.paces(), "last_used": view.last_used()})
        except ReportTimeout:
            return to_compute(request, error(503, "Model timing history took too long to read."))
        return to_compute(request, JSONResponse(facts))

    async def accounts_list(request: Request) -> Response:
        """For compute: every enabled account, as a statement would carry it. For work done later in someone's name, until that work moves here."""
        if not machine_key() or not from_compute(request, await request.body()):
            return error(403, "Only compute, with the machine key, may ask this.")
        listed = [statement.caller_of(user) for user in map(users.by_name, users.memberships()) if user]
        return to_compute(request, JSONResponse({"accounts": listed}))

    async def pick_for(request: Request) -> Response:
        """For compute, answering a batch's request in someone's name: the model their role would be picked as now."""
        if not machine_key() or not from_compute(request, await request.body()):
            return error(403, "Only compute, with the machine key, may ask this.")
        user = users.by_name(request.query_params.get("for", ""))
        if user is None:
            return to_compute(request, JSONResponse({"picked": None}))
        picked = await routing.pick_for(request, user, request.query_params.get("model", ""))
        return picked if isinstance(picked, Response) else to_compute(request, JSONResponse(picked))

    async def power_notice(request: Request) -> Response:
        """Accept spotter's word, signed with the machine key; silence itself never means asleep."""
        body = await request.body()
        if not machine_key() or not from_compute(request, body):
            return error(403, "Not authorized.")
        try:
            action = json.loads(body)["action"]
            if action not in ("sleep", "reboot", "off", "cancel"):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return error(400, "Expected a power action.")
        notice.update(action=action, at=time.time())
        ready_cache.clear()
        return to_compute(request, JSONResponse({"noted": True}))

    async def ready(request: Request) -> Response:
        """Observe within one second, cache for two, and never call the waking path."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Not authorized.")
        profile = request.query_params.get("profile", "default")
        mode = request.query_params.get("conversation_mode", "direct")
        agent_model = request.query_params.get("agent_model", "") if mode == "hermes" else ""
        cache_key = (user.name, profile, mode, agent_model)
        now = time.monotonic()
        if cache_key in ready_cache and now - ready_cache[cache_key][0] < 2:
            cached = ready_cache[cache_key]
            return JSONResponse({**cached[1], "age_seconds": round(now - cached[0], 3)})
        state, details = "not_known", {}
        try:
            async with asyncio.timeout(1):
                where = "/voice/ready?" + urlencode({"conversation_mode": mode, "agent_model": agent_model})
                result = await ask_compute(user, "GET", where, port=settings.voice_port)
                if result is None:
                    raise TimeoutError
                notice.update(action=None, at=0)
                state = "awake_not_ready"
                if result.is_success:
                    details = result.json()
                    chosen = details.get("profiles", {}).get(profile, {})
                    if chosen.get("can_start") and mode in chosen.get("modes", []):
                        state = "ready"
        except (TimeoutError, ValueError):
            if state == "not_known" and notice["action"] == "sleep":
                state = "asleep"
        result = dict(state=state, profile=profile, conversation_mode=mode, voice=details,
                      checked_at=time.time(), age_seconds=0)
        if len(ready_cache) >= 100:
            ready_cache.clear()
        ready_cache[cache_key] = (now, result)
        return JSONResponse(result)

    async def pass_on(request: Request) -> Response:
        """Check the caller, then send their request on to compute, to the port it came in on, and hand the answer back as it arrives."""
        port = request.scope["server"][1]
        if port not in settings.ports:
            return error(404, f"turnstile passes on ports {', '.join(map(str, settings.ports))}, and this came in on {port}.")
        if plugins and port == 8442:
            handled = await plugins.intercept(request)
            if handled is not None:
                return handled
        key = key_of(request)
        user = caller(key)
        if key and user is None:  # turned away here, before anything is sent on or woken, and on record here
            log.add("refused", "401", "", request.client.host if request.client else "?", path=request.url.path, why="Missing or wrong API key.")
            return error(401, "Missing or wrong API key.")
        # A request with no key at all goes on, naming nobody: a task's or a schedule's own token, or a page that needs none. Compute says whether it holds.
        if request.method == 'POST' and request.url.path in ('/v1/images/generations','/v1/images/edits'):
            from mirrorwall.requests import request_body,MAX_REQUEST_BYTES
            from starlette.datastructures import MutableHeaders,Headers
            content=bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content)>MAX_REQUEST_BYTES:
                    return error(413,'Image request exceeds 60 MiB.')
            try:
                body=json.dumps(request_body(request.headers.get('content-type',''),bytes(content))).encode()
            except (ValueError,TypeError,UnicodeError) as problem:
                return error(400,str(problem))
            headers=MutableHeaders(scope=request.scope)
            headers['content-type']='application/json'
            headers['content-length']=str(len(body))
            request._headers=Headers(scope=request.scope)
            request._body=body
        elif request.method == 'POST' and request.url.path in ('/v1/audio/transcriptions', '/v1/audio/diarizations'):
            from soundroom.requests import request_body as audio_body, MAX_REQUEST_BYTES as AUDIO_BYTES
            from starlette.datastructures import MutableHeaders, Headers
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > AUDIO_BYTES:
                    return error(413, 'Audio request exceeds 96 MiB.')
            try:
                body = json.dumps(audio_body(request.headers.get('content-type', ''), bytes(content))).encode()
            except (ValueError, TypeError, UnicodeError) as problem:
                return error(400, str(problem))
            headers = MutableHeaders(scope=request.scope)
            headers['content-type'] = 'application/json'
            headers['content-length'] = str(len(body))
            request._headers = Headers(scope=request.scope)
            request._body = body
        else:
            body = await request.body()
        query = urlencode([(name, value) for name, value in parse_qsl(request.url.query, keep_blank_values=True) if name != "key"])
        # Sent on, and signed, as the caller sent it: decoding "a%2Fb" to "a/b" would make it another address.
        where = statement.address((request.scope.get("raw_path") or request.url.path.encode()).decode(), query)
        path = request.url.path
        if port == settings.ports[0]:
            handled = await console_settings.handle(request, user)
            if handled is not None:
                handled.headers['x-suite-data-owner'] = 'front'
                return handled
            handled = await task_records.handle(request, user)
            if handled is not None:
                handled.headers['x-suite-data-owner'] = 'front'
                return handled
        if port == settings.ports[0] and user is not None and request.method == "GET" and (path == "/v1/models" or settings.machines and path == "/loadcoach/status"):
            return await combined_view(request, user, path)
        parts = path.split("/")
        if (port == settings.ports[0] and len(parts) == 4 and parts[1] == "loadcoach"
                and parts[2] in ("stop", "peek", "diagnostics") and parts[3].isdigit()
                and int(parts[3]) >= 1 << 40):
            origin = origin_number(int(parts[3]), log.machine_names)
            if origin is None:
                return error(404, "There is no configured machine for that request ID.")
            machine_name, local = origin
            upstream = await through(request, user, request.method,
                                     statement.address(f"/loadcoach/{parts[2]}/{local}", query), body,
                                     list(request.headers.items()), machine=machine_name)
            return upstream if isinstance(upstream, Response) else relay(upstream)
        if port == settings.ports[0] and user is not None and request.method == "GET" and path.startswith("/v1/models/") and path.removeprefix("/v1/models/") in hosted.offered(user):
            return JSONResponse({"id": path.removeprefix("/v1/models/"), "object": "model", "created": 0, "owned_by": "hosted"})
        if port == settings.ports[0] and user is not None and request.method == "GET" and path in ("/loadcoach/models", "/loadcoach/settings", "/loadcoach/engines"):
            answer = await ask_live(user, "GET", path, passive=passive(request))
            if answer is None:
                return off()
            return JSONResponse(answer.json(), status_code=answer.status_code,
                                headers={"cache-control": "no-store", **({"x-suite-retained": "1"} if answer.headers.get("x-suite-retained") else {})})
        if settings.machines and port == settings.ports[0] and user is not None and request.method == "GET" and path.startswith("/v1/models/"):
            catalogue = await combined_view(request, user, "/v1/models")
            if catalogue.status_code != 200:
                return catalogue
            model = next((one for one in json.loads(catalogue.body)["data"] if one["id"] == path.removeprefix("/v1/models/")), None)
            return JSONResponse(model) if model else error(404, "There is no permitted model with that name.")
        if port == settings.ports[0] and user is not None and request.method == "POST" and path.startswith("/v1/") and not path.startswith(("/v1/files", "/v1/batches")):
            response=await routing.answer(request, user, where, body)
            if path == '/v1/audio/generations':
                return await audio_work.capture(request, user, response, json.loads(body))
            return await image_work.capture(request,user,response) if path in ('/v1/images/generations','/v1/images/edits') else response
        fallthrough.note(request.method, path, port)
        upstream = await through(request, user, request.method, where, body, list(request.headers.items()), port)
        if isinstance(upstream, Response):
            return upstream
        finished = request.method == "POST" and path.startswith("/loadcoach/tasks/") and path.endswith("/finish")
        if finished or upstream.status_code == 200 and (request.method, path) in (("POST", "/loadcoach/model-rename"), ("GET", "/v1/models"), ("GET", "/loadcoach/status")):
            return await read_on(request, user, body, upstream)
        return relay(upstream)

    async def combined_view(request: Request, user: User, path: str) -> Response:
        """A caller sees retained deployments and answering machines' active work."""
        names = machines.active_names if path == "/v1/models" else [name for name in machines.names if machines.state(name) != "retired"]
        answers = await asyncio.gather(*(ask_live(user, "GET", path, machine="" if name == "compute" else name, prime=path == "/v1/models", passive=passive(request))
                                         for name in names))
        found = {name: answer.json() for name, answer in zip(names, answers) if answer is not None and answer.status_code == 200}
        if not found and path == "/v1/models" and not names:
            return JSONResponse(routing.with_roles({"object": "list", "data": [
                {"id": name, "object": "model", "created": 0, "owned_by": "hosted"} for name in hosted.offered(user)],
                "unavailable_machines": [], "available_machines": []}))
        if not found and path == "/v1/models":
            if NO_WAKE in request.headers:
                return off()
            upstream = await through(request, user, "GET", path, b"", list(request.headers.items()))
            return upstream if isinstance(upstream, Response) else relay(upstream)
        if not found:
            answer = await woken(request, user, lambda: ask_compute(user, "GET", path))
            if isinstance(answer, Response):
                return answer
            if answer.status_code != 200:
                return JSONResponse(answer.json(), status_code=answer.status_code)
            found = {"compute": answer.json()}
        if path == "/v1/models":
            listed = logical_catalogue(found) if settings.machines else found.get("compute", {}).get("data", [])
            for model in listed:
                for location in model.get("locations", []):
                    location["enabled"] = model_locations.enabled(location["machine"], location.get("model_id"))
            listed = [model for model in listed if
                      (any(one.get("enabled", True) for one in model["locations"]) if model.get("locations") else
                       model_locations.enabled("compute", model.get("model_id")))]
            listed.extend({"id": name, "object": "model", "created": 0, "owned_by": "hosted"} for name in hosted.offered(user))
            missing = sorted(name for name, answer in zip(names, answers)
                             if answer is None or answer.status_code != 200 or answer.headers.get("x-suite-retained"))
            return JSONResponse(routing.with_roles({"object": "list", "data": listed,
                                                    "unavailable_machines": missing,
                                                    "available_machines": sorted(set(names) - set(missing))}))
        primary = found.get("compute") or next(iter(found.values()))
        combined = {**primary}
        for status_field in ("running", "waiting", "holds", "loaded", "active_batches"):
            combined[status_field] = []
        combined["memory"] = {}
        combined["model_cards"] = []
        for name, result in found.items():
            for status_field in ("running", "waiting", "holds", "loaded", "active_batches"):
                for entry in result.get(status_field, []):
                    row = {**entry, "machine": name}
                    if status_field in ("running", "waiting", "holds") and isinstance(row.get("id"), int):
                        row["id"] = public_number(name, row["id"])
                    combined[status_field].append(row)
            combined["memory"].update({f"{name}:{card}": value for card, value in result.get("memory", {}).items()})
            combined["model_cards"].extend(f"{name}:{card}" for card in
                                           result.get("model_cards", result.get("cards", result.get("memory", {}))))
        for status_field in ("others_running", "others_waiting", "others_holds", "batches"):
            combined[status_field] = sum(result.get(status_field, 0) for result in found.values())
        outside = hosted.status(user)
        combined.update(outside)
        batched = batch_work.status([*combined["running"], *combined["hosted"]])
        mine = [one for one in batched["active"] if user.can("see-all") or one["user"] == user.name]
        combined["batches"] += batched["open"]
        combined["active_batches"] = active_with(combined["active_batches"], mine)
        combined["busy"] = sum(result.get("busy", 0) for result in found.values()) + len(outside["hosted"])
        return JSONResponse(combined, headers={"cache-control": "no-store"})

    def active_with(listed: list[dict], mine: list[dict]) -> list[dict]:
        """The nodes' running work beside the front's own batches, each listed once.

        A batch the front runs is listed from `mine`. A benchmark probe is listed as its node
        reports it while its task record here is still in progress: the record is kept here, but
        only the node knows the probe is running.
        """
        own = {one["id"] for one in mine}

        def shown(one: dict) -> bool:
            kept = batch_work.store.get(one["id"])
            return kept is None or one.get("caller") == "benchmark" and kept.get("status") == "in_progress"
        return [one for one in listed if one["id"] not in own and shown(one)] + mine

    async def read_on(request: Request, user: User | None, body: bytes, upstream: httpx.Response) -> Response:
        """Three answers of compute's that what is kept here follows.

        A model was renamed: the groups, accounts and roles that list it follow it. The models a caller
        may use: the roles they may ask for go beside them. A benchmark task has finished: the evidence
        compute checked against the task is kept here.
        """
        said = json.loads(await upstream.aread())
        await upstream.aclose()
        if request.url.path == "/v1/models":
            if user is not None:  # the outside models are front's own: compute does not list them
                said["data"] = [*said["data"], *({"id": name, "object": "model", "created": 0, "owned_by": "hosted"} for name in hosted.offered(user))]
            return JSONResponse(routing.with_roles(said))
        if request.url.path == "/loadcoach/status":
            if user is not None:
                outside = hosted.status(user)
                said.update(outside)
                batched = batch_work.status([*said.get("running", []), *said.get("hosted", [])])  # a hosted answer is executing too
                mine = [one for one in batched["active"] if user.can("see-all") or one["user"] == user.name]
                said["batches"] = said.get("batches", 0) + batched["open"]
                said["active_batches"] = active_with(said.get("active_batches", []), mine)
                said["busy"] = said.get("busy", 0) + len(outside["hosted"])
            return JSONResponse(said, headers={"cache-control": "no-store"})  # as compute said it: this page is never kept
        if request.url.path.endswith("/finish"):
            # A task's own token is compute's to check, so the request went unsigned: compute signs the evidence itself, with the machine key.
            evidence = said.pop("role_evidence", None)
            if upstream.status_code == 200 and evidence and any(secrets.compare_digest(evidence["signed"], statement.sign(key, json.dumps(evidence["snapshot"], sort_keys=True)))
                                                               for key in statement.keys(settings.machine_key_file)):
                routing.evidence.accept(evidence["snapshot"], definitions=False)
            return JSONResponse(said, status_code=upstream.status_code)
        return await renamed(request, user, body, said)

    async def renamed(request: Request, user: User, body: bytes, said: dict) -> Response:
        """Compute has renamed a model. The groups, accounts and roles that list it, which are kept here, follow."""
        asked = json.loads(body)
        routing.roles.renamed(asked["name"], asked["to"])
        said["followed"] = users.model_renamed(asked["name"], asked["to"])
        if said["followed"]:
            log.add("group", "model-renamed", user.name, request.client.host if request.client else "?",
                    model=asked["to"], was=asked["name"], followed=said["followed"])
        return JSONResponse(said)

    maintenance_stop=threading.Event()

    def prune_observations():
        """Sweep optional history on its own connection without blocking front readiness."""
        from loadcoach.observation_store import prune
        source=log.db.database
        if source.engine=='sqlite' and source.path is None:
            prune(log.db,stop=maintenance_stop)
        else:
            with source.connection(isolation_level=None) as connection:
                prune(connection,stop=maintenance_stop)

    async def tidy() -> None:
        """Once a day, remove the session keys that have ended, which no longer work either way, outcomes older than reliability looks back, and log entries older than keep_days."""
        last_pruned = None  # the monotonic clock starts at boot, so a 0 here skipped the first day after a restart
        while True:
            await task_records.retry_publication()
            await publish_file_settings()
            if last_pruned is None or time.monotonic() - last_pruned >= 86400:
                users.forget_sessions()
                log.prune()
                await asyncio.to_thread(prune_observations)
                routing.reliability.prune(7)
                last_pruned = time.monotonic()
            await asyncio.sleep(10)

    async def machine_says(name: str, path: str) -> dict | None:
        """What a machine answers to `path`, asked as front's own account (the one weightroom signs in with), or None. Nothing is woken."""
        keyed = Path(settings.auth_key_file) if settings.auth_key_file else None
        user = caller(keyed.read_text().strip()) if keyed and keyed.is_file() else None
        if user is None:
            return None
        answer = await ask_live(user, "GET", path, machine="" if name == "compute" else name)
        return answer.json() if answer is not None and answer.status_code == 200 else None

    async def telemetry_says(name: str, path: str) -> dict | None:
        """Read retained telemetry settings for the selected physical host."""
        if len(settings.ports) < 3:
            return None
        if name != "compute" and not settings.machines[name].get("spotter"):
            return None
        keyed = Path(settings.auth_key_file) if settings.auth_key_file else None
        user = caller(keyed.read_text().strip()) if keyed and keyed.is_file() else None
        if user is None:
            return None
        answer = await ask_live(user, "GET", path, machine="" if name == "compute" else name, port=settings.ports[2])
        return answer.json() if answer is not None and answer.status_code == 200 else None

    machines = Machines(["compute", *settings.machines], machine_says, settings.machines_poll_seconds,
                        ask_telemetry=telemetry_says, store=log.db)
    artifact_machine = settings.artifact_machine or ("front_cpu" if "front_cpu" in settings.machines else "compute")
    from turnstile.installation_store import Installations
    installation_store = Installations(stores.database('weightroom.accounts'))
    distributions: dict[str, dict] = installation_store.recover()
    distribution_tasks: set[asyncio.Task] = set()
    installation_lock = asyncio.Lock()
    configuration_lock = asyncio.Lock()
    installation_reservations = {}
    installation_placing: Counter = Counter()  # (node, path) of files pending installations will place
    installation_claims: dict[tuple[str, str], asyncio.Lock] = {}

    async def model_file_inventory(user, refresh=False):
        """Read all managed files without requiring the artifact host to be the source. `refresh` looks through every folder again first."""
        listing = "/loadcoach/files" + ("?refresh=1" if refresh else "")
        answers = await asyncio.gather(*(ask_machine(user, "GET", listing, machine=name, timeout=30 if refresh else 5)
                                         for name in machines.names))
        nodes = [{"name": name, "answering": answer is not None and answer.status_code == 200,
                 **(answer.json() if answer is not None and answer.status_code == 200 else {"files": [], "places": []})}
                for name, answer in zip(machines.names, answers)]
        return [*nodes, {'name': 'front', 'answering': True, **await asyncio.to_thread(front_files.inventory, refresh)}]

    async def model_files_available(request):
        """The complete cache/storage picker, including preflight capacity warnings."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Model management is required.")
        return JSONResponse({"nodes": await model_file_inventory(user)}, headers={"cache-control": "no-store"})

    async def image_model_check(request):
        """Check a new image bundle using the owning nodes' cached header identities."""
        user = caller(key_of(request))
        if user is None:
            return error(401, 'Missing or wrong API key.')
        if not user.can('manage-models'):
            return error(403, 'Model management is required.')
        try:
            asked = json.loads(await request.body())
            if not isinstance(asked, dict) or not isinstance(asked.get('settings'), dict):
                raise ValueError('Send the draft image settings.')
            source = asked.get('source_machine', '')
            if not isinstance(source, str):
                raise ValueError('Choose a source machine.')
            nodes = await model_file_inventory(user)
            def image_files():
                """Match installation's source preference for paths present on several nodes."""
                files = [(node, file) for node in nodes if node.get('answering') for file in node.get('files', [])
                         if not file.get('moving') and (file.get('facts', {}).get('what') == 'image'
                                                     or file.get('facts', {}).get('image_component'))]
                return sorted(files, key=lambda pair: (pair[0]['name'] != source, pair[1].get('tier') == 'storage'))
            # Upgrade older indexes once; unchanged language/audio file descriptions stay cached.
            if any('image_identity' not in file['facts'] for _, file in image_files()):
                nodes = await model_file_inventory(user, refresh=True)
            inventory = {}
            for _, file in image_files():
                identity = file['facts'].get('image_identity')
                if isinstance(identity, dict):
                    inventory.setdefault(file['path'], {**identity, 'path':file['path']})
            from mirrorwall.components import check
            checked = check(asked['settings'], '', inventory=list(inventory.values()))
        except (ValueError, TypeError, AttributeError, KeyError) as problem:
            return error(400, str(problem))
        return JSONResponse(checked, headers={'cache-control':'no-store'})

    async def managed_digest(user, machine, path, progress=None):
        """Request the owning worker's full-file SHA-256, never infer equality from filenames."""
        if machine == 'front' and progress is not None:
            source = front_files.path(path)
            progress['progress_available'] = True
            try:
                return await asyncio.to_thread(sha256_of, str(source), front_files.stopping, progress)
            except OSError as problem:
                raise ValueError(f'Could not verify {path} on {machine}: {problem}') from problem
        answer = await ask_machine(user, "GET", "/loadcoach/files/hash?" + urlencode({"path": path}), machine=machine, timeout=None)
        if answer is None or answer.status_code != 200:
            raise ValueError(f"Could not verify {path} on {machine}.")
        return answer.json()["sha256"]

    async def promote_model_file(user, step, progress=None, *, stopped="The cache promotion stopped; the model was not added."):
        """Use Files' atomic copy between two places of one machine and wait for the complete copy.
        `progress`, if given, follows how far it has got."""
        body = json.dumps({"path": step["file"]["path"], "to": step["place"]["name"], "relative": step["relative"]}).encode()
        answer = await ask_machine(user, "POST", "/loadcoach/files/copy", body, machine=step["node"], timeout=30)
        if answer is None or answer.status_code != 202:
            raise ValueError(answer.json()["error"]["message"] if answer is not None else "The source node is not answering.")
        missing = 0
        while True:
            await asyncio.sleep(.5)
            answer = await ask_machine(user, "GET", "/loadcoach/files", machine=step["node"], timeout=30)
            if answer is None or answer.status_code != 200:
                raise ValueError("The copy could not be verified." if progress is not None else "The cache promotion could not be verified; the model was not added.")
            files = answer.json()["files"]
            if any(file["path"] == step["path"] for file in files):
                return
            source = next((file for file in files if file["path"] == step["file"]["path"]), {})
            if progress is not None and source.get("moving"):
                progress["done"] = source["moving"].get("done", 0)
            missing = 0 if source.get("moving") else missing + 1
            if missing >= 3:
                raise ValueError(source.get("transfer_failure") or stopped)

    async def transfer_model_file(user, step, digest, job):
        """Stream a signed source response to the existing hash-verifying Spotter receiver."""
        prepared = json.dumps({"place": step["place"]["name"], "relative": step["relative"],
                               "size": step["file"]["size"], "sha256": digest}).encode()
        answer = await ask_machine(user, "POST", "/spotter/files/prepare", prepared, spotter=True, machine=step["node"])
        if answer is None or answer.status_code != 201:
            raise ValueError(answer.json()["error"]["message"] if answer is not None else spotter_failure(step["node"]))
        source = step["source"]
        if source == 'front':
            path = front_files.path(step['file']['path'])

            async def local_pieces():
                """Stream an approved front file without buffering model weights in memory."""
                with path.open('rb') as file:
                    while piece := await asyncio.to_thread(file.read, PIECE):
                        job['done'] += len(piece)
                        yield piece

            received = await internal.put(spotter_base(step['node']) + answer.json()['upload'],
                                          content=local_pieces(), timeout=None)
            if received.status_code != 201:
                raise ValueError(received.json()['error']['message'])
            return
        async with node_content(user, source, step["file"]["path"]) as upstream:
            async def pieces():
                """Stream verified source bytes while reporting transfer progress."""
                async for piece in upstream.aiter_bytes(PIECE):
                    job["done"] += len(piece)
                    yield piece
            received = await internal.put(spotter_base(step["node"]) + answer.json()["upload"], content=pieces(), timeout=None)
            if received.status_code != 201:
                raise ValueError(received.json()["error"]["message"])

    @asynccontextmanager
    async def node_content(user, machine, path):
        """A compute node's model file as a stream, asked for in `user`'s name and proven to come from that node."""
        other = settings.machines.get(machine, {}) if machine != "compute" else {}
        base = other["compute"].rstrip("/") if other else settings.upstream(settings.ports[0])
        where = "/loadcoach/files/content?" + urlencode({"path": path})
        headers, nonce = signed_headers(user, "GET", where, b"", [], key_file=other.get("machine_key_file", ""))
        async with internal.stream("GET", base + where, headers=headers, timeout=None) as upstream:
            if upstream.status_code != 200 or not proven(upstream, nonce, other.get("machine_key_file", "")):
                raise ValueError("The source file stream could not be authenticated.")
            yield upstream

    async def install_model(request, saved_request=None):
        """Preflight a complete model bundle before queueing verified file placement and creation.

        `machine` names one destination; `machines` names several, which are all checked before any
        is started. Each destination is then a job of its own, and a file two of them need placed in
        the same spot, such as one stored file promoted into its cache, is placed once.
        """
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Model management is required.")
        try:
            asked = saved_request if saved_request is not None else json.loads(await request.body())
            name, wanted = asked["name"], asked["settings"]
            several = "machines" in asked
            destinations = list(dict.fromkeys(asked["machines"] if several else [asked["machine"]]))
            if not isinstance(name, str) or not name or not destinations or not all(isinstance(one, str) for one in destinations):
                raise ValueError("Choose a model name and compute node.")
            if asked.get("move") and (len(destinations) != 1 or not asked.get("source_model_id")
                                      or asked.get("source_machine", "") in ("", destinations[0])):
                raise ValueError("A move takes the model from one node to one other node.")
            for destination in destinations:
                if destination not in machines.names:
                    raise ValueError("Choose a model name and compute node.")
                if not machines.enabled(destination):
                    raise ValueError(f"Compute node {destination} is {machines.state(destination)}. Enable it before installing a model.")
            planned, problems, placing = [], [], Counter()
            async with installation_lock:
                active = [job for job in distributions.values() if job.get('kind') == 'model-install'
                          and job.get('name') == name and job.get('destination', job.get('machine')) in destinations
                          and job.get('state') not in ('complete', 'failed', 'cancelled')]
                if active:
                    raise ValueError(f'{name} is already being installed. Open Models to follow its progress; another copy was not started.')
                nodes = await model_file_inventory(user)
                for destination in destinations:
                    try:
                        planned.append(await prepare_installation(user, name, destination, wanted, asked, nodes, placing))
                    except ValueError as problem:
                        problems.append(f"{destination}: {problem}" if several else str(problem))
                if problems:
                    raise ValueError(" ".join(problems) + (" Nothing was started." if several else ""))
                for one in planned:
                    claim_installation(one["prepared"], 1)
        except (ValueError, KeyError, TypeError) as problem:
            return error(409, str(problem))
        jobs = []
        try:
            for one in planned:
                jobs.append(start_installation(user, name, asked, **one))
        except Exception:
            for one in planned[len(jobs):]:
                claim_installation(one['prepared'], -1)
            raise
        return JSONResponse({"jobs": jobs} if several else jobs[0], status_code=202)

    async def retry_installation(request):
        user=caller(key_of(request))
        if user is None:return error(401,'Missing or wrong API key.')
        if not user.can('manage-models'):return error(403,'Model management is required.')
        number=request.path_params['number']
        try:
            asked=installation_store.retry_request(number,user)
        except KeyError as problem:return error(404,str(problem))
        except PermissionError as problem:return error(403,str(problem))
        except ValueError as problem:return error(409,str(problem))
        answer=await install_model(request,asked)
        if answer.status_code==202:
            distributions[number]['retry_id']=json.loads(answer.body)['id']
            installation_store.checkpoint([distributions[number]])
        return answer

    async def prepare_installation(user, name, destination, candidate, asked, nodes, placing):
        """Check one destination and plan its files. `placing` counts the files that destinations
        planned earlier in the same request will place, so that a shared one is counted once."""
        inventory = await ask_machine(user, "GET", "/loadcoach/models", machine=destination, timeout=5)
        engines = await ask_machine(user, "GET", "/loadcoach/engines", machine=destination, timeout=5)
        if inventory is None or inventory.status_code != 200 or engines is None or engines.status_code != 200:
            raise ValueError("The destination node cannot be checked; no action was started.")
        if any(row["name"] == name for row in inventory.json()["models"]):
            raise ValueError(f"There is already a model named {name} on {destination}.")
        selected = engine_for(candidate)
        if isinstance(asked.get('generation'), dict):
            candidate = with_generation(candidate, asked['generation'])
            for flag in generation({'flags': inventory.json().get('defaults') or {}})['flags']:
                candidate['flags'].setdefault(flag, None)
        installed = next((one for one in engines.json()["engines"] if one["id"] == selected.id), {})
        if not installed.get("enabled") or not installed.get("available"):
            raise ValueError(f"{selected.label} is not available on {destination}.")
        # Node schema validation is read-only and runs before any transfer.
        selected.model.check_model(name, selected.model.effective({key:value for key,value in candidate.items() if key != 'order'}, inventory.json().get("defaults") or {}),
                                   tuple(inventory.json().get("cards") or []))
        prepared = installation_plan(candidate, nodes, destination, asked.get("source_machine", ""))
        for step in prepared["steps"]:
            spot = (step["node"], step["path"])
            if not step["existing"] and (installation_placing[spot] or placing[spot]):
                # Another installation will place this file; its room is counted there already.
                place = step["place"]
                prepared["allocations"][(step["node"], place.get("filesystem", place["folder"]))]["bytes"] -= step["file"]["size"]
        for key, allocation in prepared["allocations"].items():
            if allocation["free"] < allocation["bytes"] + installation_reservations.get(key, 0) + SPARE:
                raise ValueError(f"Not enough cache space on {allocation['node']} after pending transfers; no action was started.")
        placing.update((step["node"], step["path"]) for step in prepared["steps"] if not step["existing"])
        return {"destination": destination, "candidate": candidate, "selected": selected, "inventory": inventory, "prepared": prepared}

    def claim_installation(prepared, sign):
        """Reserve (1) or release (-1) the room and file placements a planned installation needs."""
        for key, allocation in prepared["allocations"].items():
            installation_reservations[key] = installation_reservations.get(key, 0) + sign * allocation["bytes"]
        installation_placing.update({(step["node"], step["path"]): sign for step in prepared["steps"] if not step["existing"]})

    def placement_claim(node, path):
        """One installation at a time places a given file on a given node."""
        return installation_claims.setdefault((node, path), asyncio.Lock())

    def start_installation(user, name, asked, *, destination, candidate, selected, inventory, prepared):
        """Queue one destination's verified file placement, then its model configuration."""
        number = secrets.token_urlsafe(8)
        job = {"id": number, "kind": "model-install", "name": name, "machine": destination,
                                       "destination": destination, "state": "queued", "file": Path(candidate["flags"]["model"]).name, "targets": {}}
        installation_store.add(job,user,asked)
        distributions[number] = job
        async def run():
            """Place a verified bundle, then publish its model configuration."""
            try:
                placed = await install_files(prepared, lambda machine, path: managed_digest(user, machine, path, job),
                                             lambda step: promote_model_file(user, step, job),
                                             lambda step, digest, progress: transfer_model_file(user, step, digest, progress), job,
                                             claim=placement_claim)
                job.update(state='creating', working_machine=destination, done=0, of=0, progress_available=False)
                async with configuration_lock:
                    if asked.get('source_model_id'):
                        source = await ask_machine(user, 'GET', '/loadcoach/models', machine=asked.get('source_machine') or 'compute', timeout=5)
                        row = next((row for row in source.json()['models'] if row.get('model_id') == asked['source_model_id']), None) if source and source.is_success else None
                        if row is None or row['flags']['model'] != candidate['flags']['model'] or engine_for(row).id != selected.id:
                            raise ValueError('The source model changed or is unavailable; verify it before adding this node.')
                        # A long transfer must not publish generation defaults superseded while it copied.
                        placed = with_generation(placed, generation(selected.model.effective(configuration(row), source.json().get('defaults') or {})))
                        for flag in generation({'flags': inventory.json().get('defaults') or {}})['flags']:
                            placed['flags'].setdefault(flag, None)
                    answer = await ask_machine(user, "POST", "/loadcoach/models/" + quote(name, safe="") + "?new=1",
                                               json.dumps(placed).encode(), machine=destination, timeout=30)
                if answer is None or answer.status_code != 201:
                    raise ValueError(answer.json()["error"]["message"] if answer is not None else "The destination stopped answering; verify its model list before retrying.")
                if asked.get("move"):
                    # The copy is verified and offered; only now does the source give up its own.
                    # It stops being offered at once, and finishes the requests it already has.
                    source = asked["source_machine"]
                    job.update(state="removing", machine=source)
                    removed = await ask_machine(user, "DELETE", "/loadcoach/models/" + quote(row["name"], safe=""), machine=source, timeout=30)
                    if removed is None or not removed.is_success:
                        why = removed.json()["error"]["message"] if removed is not None else "it stopped answering"
                        raise ValueError(f"Copied to {destination}, but {source} kept its copy: {why}")
                    job.update(machine=destination, moved_from=source)
                job.update(state="complete", path=placed["flags"]["model"])
                facts_seen.clear()
                log.add("model", "moved" if asked.get("move") else "installed", user.name, "console", name, machine=destination)
            except asyncio.CancelledError:
                job.update(state='failed',interrupted=True,why='Installation interrupted by a service restart. Retry to verify retained files and continue with the saved settings.')
                raise
            except (ValueError, OSError, httpx.HTTPError) as problem:
                job.update(state="failed", why=str(problem))
                if front_files.stopping.is_set():job['interrupted']=True
            finally:
                claim_installation(prepared, -1)
                installation_store.checkpoint([job])
        task = asyncio.create_task(run())
        distribution_tasks.add(task)
        task.add_done_callback(distribution_tasks.discard)
        return job

    async def ask_machine(user: User, method: str, path: str, body: bytes = b"", *, spotter: bool = False,
                          machine: str | None = None, timeout: float | None = 30) -> httpx.Response | None:
        """Send an authenticated request to the selected node without changing ownership."""
        name = file_store() if machine is None else machine
        if name == 'front':
            if method == 'GET':
                path = image_work.discovery_path(user, path)
            return await front_files.request(user, method, path, body)
        return await ask_compute(user, method, path, body, machine="" if name == "compute" else name,
                                 port=settings.ports[2] if spotter else None, timeout=timeout)

    async def artifact_proxy(request: Request) -> Response:
        """Send the front's file/download operations to the designated front LoadCoach."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        suffix = request.path_params.get("suffix", "")
        allowed = {("GET", "hub"), ("GET", "downloads"), ("GET", "file"), ("GET", "models"),
                   ("POST", "downloads"), ("POST", "downloads/stop"), ("POST", "downloads/discard"),
                   ("POST", "files/move"), ("POST", "files/copy"), ("POST", "files/delete")}
        if (request.method, suffix) not in allowed and not (request.method == "POST" and suffix.startswith("models/") and len(suffix) > 7):
            return error(404, "That front artifact operation is not available.")
        query = request.url.query
        path = "/loadcoach/" + suffix + (("?" + query) if query else "")
        body = await request.body()
        if file_store() == 'front' and suffix in ('files/delete', 'files/move'):
            if not user.can('manage-models'):
                return error(403, 'Your account may not manage model files.')
            try:
                selected = json.loads(body).get('path')
            except (ValueError, AttributeError):
                return error(400, 'Choose a model file.')
            nodes = await model_file_inventory(user, refresh=True)  # whether the file is in use is decided on what is there now
            unavailable = [node['name'] for node in nodes if not node['answering']]
            if unavailable:
                return error(409, 'Cannot verify whether this file is in use while these nodes are offline: ' + ', '.join(unavailable) + '.')
            if any(file.get('used_by') for node in nodes if node['name'] != 'front'
                   for file in node['files'] if file['path'] == selected):
                return error(409, 'This file is used by a configured model. Update that model before moving or deleting its file.')
        answer = await ask_machine(user, request.method, path, body, timeout=None)
        if answer is None:
            return error(503, f"Model files on {file_store()} are unavailable. Configure front cache and storage in Settings → General, or connect that node.")
        return JSONResponse(answer.json(), status_code=answer.status_code, headers={"cache-control": "no-store"})

    async def file_inventory(user, refresh: bool = False) -> dict:
        """The front's files plus available storage at every configured compute node, from each one's kept
        index. `refresh` has every node and front look through their folders again first."""
        names, store = ['front', *machines.names], file_store()
        listing = "/loadcoach/files" + ("?refresh=1" if refresh else "")
        wait = 30 if refresh else 5  # a look through every folder can take a while; a kept index answers at once
        answers = list(await asyncio.gather(*(ask_machine(user, "GET", listing, machine=name, timeout=wait) for name in names)))
        if store != 'front' and store in names and answers[names.index(store)] is None:
            # Listing files can outlive the short deadline that keeps an unavailable node from delaying
            # the page. The designated store is required, so it alone gets one normal bounded retry.
            answers[names.index(store)] = await ask_machine(user, "GET", listing, machine=store, timeout=30)
        reports = {one["name"]: one for one in machines.report()}
        nodes = []
        for name, answer in zip(names, answers):
            answering = answer is not None and answer.status_code == 200
            found = answer.json() if answering else {}
            nodes.append({"name": name, "state": 'ready' if name == 'front' else reports.get(name, {}).get("state", "unknown"),
                          "answering": answering, "checked": found.get("checked"),
                          "places": found.get("places", []), "files": found.get("files", []),
                          "problems": found.get('problems', []), "cleanup": found.get("cleanup", {})})
        source = next(node for node in nodes if node['name'] == file_store())
        return {"artifact_machine": file_store(), "places": source["places"], "files": source["files"],
                "front_enabled": any(front_file_roots().values()),
                "store_available": bool(source['places']),
                "cleanup": source.get("cleanup", {}),
                "nodes": nodes, "distributions": shown_jobs()}

    async def artifact_files(request: Request) -> Response:
        """The front's files plus available storage at every configured compute node. `refresh=1` looks again first."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Your account may not manage model files.")
        found = await file_inventory(user, refresh=request.query_params.get("refresh") == "1")
        return JSONResponse(found, headers={"cache-control": "no-store"})

    async def artifact_cleanup(request: Request) -> Response:
        """Clear front's active cache safely, or remove unused files from answering compute nodes."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Your account may not manage model files.")
        action = request.path_params.get("action", request.url.path.rsplit("/", 1)[-1])
        if arranging():
            return error(409, "Wait for the bulk change of files to finish before cleaning up.")
        if action in ("clear-cache", "consolidate-cache"):
            # What is kept and what is copied is decided on what is in the folders now, not on a kept list.
            listed = await file_inventory(user, refresh=True)
            unavailable = [node["name"] for node in listed["nodes"] if not node.get("answering")]
            if unavailable:
                return error(409, "Cache cleanup cannot safely determine every configured model while these nodes are unavailable: "
                             + ", ".join(unavailable) + ".")

            def identity(file: dict) -> tuple:
                """Extract the stable model identity used to compare artifact references."""
                facts = file.get("facts") or {}
                return (Path(file["path"]).name, file.get("size"), facts.get("what", "model"),
                        facts.get("name"), facts.get("architecture"), facts.get("size_label"))

            # Every file a model uses on any node, front included, so front's cache holds the whole working set.
            used = {identity(file) for node in listed["nodes"] for file in node["files"] if file.get("used_by")}
            cached = [file for file in listed["files"] if file.get("tier", "cache") == "cache"]
            if action == "clear-cache":
                body = {"mode": "clear-cache", "keep": [file["path"] for file in cached if identity(file) in used]}
            else:
                missing = used - {identity(file) for file in cached}
                promote = {identity(file): file["path"] for file in listed["files"]
                           if file.get("tier") == "storage" and identity(file) in missing}
                body = {"mode": "promote", "promote": list(promote.values())}
            answer = await ask_machine(user, "POST", "/loadcoach/files/cleanup", json.dumps(body).encode(), timeout=None)
            if answer is None:
                return error(503, f"The front model-file store ({artifact_machine}) is not answering.")
            found = answer.json()
            if action == "consolidate-cache" and answer.status_code == 202:
                found["absent"] = sorted(name for name, *_ in missing - set(promote))  # used somewhere, but front has no copy at all
            return JSONResponse(found, status_code=answer.status_code)
        if action != "cleanup-nodes":
            return error(404, "That file cleanup action is not available.")
        names = [name for name in machines.names if name != file_store()]
        body = json.dumps({"mode": "unused"}).encode()
        answers = await asyncio.gather(*(ask_machine(user, "POST", "/loadcoach/files/cleanup", body,
                                                     machine=name, timeout=None) for name in names))
        cleaned, unavailable = {}, []
        for name, answer in zip(names, answers):
            if answer is None:
                unavailable.append(name)
            elif answer.status_code in (200, 202):
                cleaned[name] = answer.json()
            else:
                unavailable.append(f"{name}: {answer.json().get('error', {}).get('message', 'cleanup failed')}")
        return JSONResponse({"nodes": cleaned, "unavailable": unavailable}, status_code=202)

    def spotter_base(name: str) -> str:
        """Resolve the configured Spotter endpoint for a compute node."""
        if name == "compute":
            return settings.upstream(settings.ports[2])
        return settings.machines[name].get("spotter", "").rstrip("/")

    def spotter_failure(name: str) -> str:
        """Identify the file receiver separately from the node's model runner."""
        return (f"File transfer to {name} could not reach or authenticate Spotter at {spotter_base(name)}. "
                "Check the Spotter service, its machine key and firewall access from front to this port.")

    async def distribute_one(user: User, source: Path, relative: str, digest: str, node: dict, job: dict) -> None:
        """Stream one validated front artifact to the selected compute location."""
        name = node["name"]
        target = next((place for place in node["places"] if place["name"] == job["source_place"]), node["places"][0])
        state = job["targets"][name] = {"state": "preparing", "place": target["name"], "done": 0, "of": job["size"]}
        prepared = json.dumps({"place": target["name"], "relative": relative, "size": job["size"], "sha256": digest}).encode()
        answer = await ask_machine(user, "POST", "/spotter/files/prepare", prepared, spotter=True, machine=name, timeout=30)
        if answer is None or answer.status_code != 201:
            state.update(state="failed", why=(answer.json()["error"]["message"] if answer is not None else spotter_failure(name)))
            return
        upload = answer.json()["upload"]

        async def pieces():
            """Read the source artifact off the event loop in bounded transfer chunks."""
            with open(source, "rb") as file:
                while piece := await asyncio.to_thread(file.read, PIECE):
                    state["done"] += len(piece)
                    yield piece

        state["state"] = "copying"
        try:
            received = await internal.put(spotter_base(name) + upload, content=pieces(), timeout=None)
            if received.status_code != 201:
                state.update(state="failed", why=received.json()["error"]["message"])
                return
            state.update(state="complete", path=received.json()["path"])
        except (OSError, httpx.HTTPError, ValueError) as problem:
            state.update(state="failed", why=f"{type(problem).__name__}: {problem}")

    async def stage_from_storage(user: User, source: Path, file: dict, job: dict) -> tuple[Path, dict, str]:
        """Copy a stored file into the matching front cache and return its verified cache copy."""
        if file.get("tier") != "storage":
            return source, file, await asyncio.to_thread(sha256_of, str(source))
        cache_name = file["place"].removeprefix("storage_")
        cache = next((place for place in job["source_places"] if place["name"] == cache_name and place.get("tier") == "cache"), None)
        if cache is None:
            raise ValueError(f"Storage place {file['place']} has no matching active cache named {cache_name}.")
        target = Path(cache["folder"]) / source.name
        job.update(state="staging", staged_from=str(source), path=str(target), source_place=cache_name,
                   staging={"done": 0, "of": file["size"]})
        if not target.is_file():
            body = json.dumps({"path": str(source), "to": cache_name}).encode()
            started = await ask_machine(user, "POST", "/loadcoach/files/copy", body, timeout=None)
            if started is None or started.status_code not in (202, 409):
                why = started.json()["error"]["message"] if started is not None else "The front model-file store is not answering."
                raise OSError(f"The storage file could not be staged in cache: {why}")
            missing = 0
            while not target.is_file():
                await asyncio.sleep(1)
                listed = await ask_machine(user, "GET", "/loadcoach/files", timeout=None)
                if listed is None or listed.status_code != 200:
                    continue
                source_now = next((one for one in listed.json().get("files", []) if one["path"] == str(source)), None)
                progress = (source_now or {}).get("moving") or {}
                job["staging"]["done"] = progress.get("done", 0)
                missing = 0 if progress else missing + 1
                if missing >= 3:
                    raise OSError("The storage-to-cache copy stopped before the cache file was complete.")
        if target.stat().st_size != file["size"]:
            raise OSError(f"The existing cache file {target.name} is not the same size as the stored file.")
        job["state"] = "checking cache"
        stored_digest, cached_digest = await asyncio.gather(
            asyncio.to_thread(sha256_of, str(source)), asyncio.to_thread(sha256_of, str(target)))
        if stored_digest != cached_digest:
            raise OSError(f"The cache file {target.name} does not match the stored file's SHA-256.")
        job["staging"]["done"] = file["size"]
        cached = {**file, "path": str(target), "place": cache_name, "tier": "cache"}
        return target, cached, cached_digest

    async def run_distribution(user: User, source: Path, file: dict, selected: list[str], job: dict, nodes: list[dict]) -> None:
        """Copy an artifact to selected nodes and retain per-node outcomes."""
        try:
            job["state"] = "checking"
            source, file, digest = await stage_from_storage(user, source, file, job)
            job["sha256"] = digest
            job.update(path=str(source), source_place=file["place"])
            root = Path(next(place["folder"] for place in job["source_places"] if place["name"] == file["place"]))
            relative = str(source.relative_to(root))
            job["state"] = "copying"
            await asyncio.gather(*(distribute_one(user, source, relative, digest, node, job)
                                   for node in nodes if node["name"] in selected))
            job["state"] = "failed" if any(one["state"] == "failed" for one in job["targets"].values()) else "complete"
        except (OSError, ValueError, StopIteration) as problem:
            job.update(state="failed", why=f"{type(problem).__name__}: {problem}")

    async def distribute(request: Request) -> Response:
        """Copy one front model file through each selected node's Spotter."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Your account may not manage model files.")
        try:
            asked = json.loads(await request.body())
        except (ValueError, TypeError):
            return error(400, "The distribution request is not valid.")
        inventory = await artifact_files(request)
        if inventory.status_code != 200:
            return inventory
        listed = json.loads(inventory.body)
        source = next((one for one in listed.get("files", []) if one["path"] == asked.get("path")), None)
        available = [node for node in listed.get("nodes", []) if node["name"] in machines.names and node["name"] != file_store() and node["places"]]
        selected = asked.get("machines") or [node["name"] for node in available]
        if source is None or not isinstance(selected, list) or not selected or any(name not in {node["name"] for node in available} for name in selected):
            return error(400, "Choose a front model file and at least one available destination node.")
        number = secrets.token_urlsafe(8)
        job = distributions[number] = {"id": number, "file": Path(source["path"]).name, "path": source["path"],
                                       "size": source["size"], "source": file_store(), "source_place": source["place"],
                                       "source_places": listed["places"], "state": "queued", "targets": {}}
        task = asyncio.create_task(run_distribution(user, Path(source["path"]), source, selected, job, available))
        distribution_tasks.add(task)
        task.add_done_callback(distribution_tasks.discard)
        log.add("file", "distribution-started", user.name, request.client.host if request.client else "?",
                path=source["path"], machines=selected, size=source["size"])
        return JSONResponse(job, status_code=202)

    async def distribution_list(request: Request) -> Response:
        """List current artifact distribution jobs for an authorized operator."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Your account may not manage model files.")
        return JSONResponse({"distributions": [{**job,'can_retry':job.get('kind')=='model-install' and
            job.get('state')=='failed' and not job.get('retry_id') and installation_store.can_retry(job['id'],user)}
            for job in distributions.values()]}, headers={"cache-control": "no-store"})

    def arranging() -> dict | None:
        """The bulk change of files running now. There is one at a time."""
        return next((job for job in distributions.values() if job.get("kind") == "arrange" and job["state"] == "running"), None)

    def shown_jobs() -> list[dict]:
        """Distributions, and bulk changes until ten minutes after they end."""
        return [job for job in distributions.values()
                if job.get("kind") != "arrange" or job["state"] == "running" or time.time() - job.get("ended", 0) < 600]

    async def arrange(request: Request) -> Response:
        """Make the files in the cache and on each node what the Files page's boxes say, in one background job.

        The body is {"changes": [{"row", "at", "keep"}]}, a change for each box that was changed. They
        are checked against a fresh look through every folder; if one cannot be made, none is.
        """
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Your account may not manage model files.")
        try:
            asked = json.loads(await request.body())
            if not isinstance(asked, dict):
                raise TypeError
            transfer = asked.get("transfer")
            cleaning = asked.get("cleanup") is True
            changes = asked.get("changes", [])
            if transfer is not None and not isinstance(transfer, dict):
                raise TypeError
            if not isinstance(changes, list) or not all(isinstance(one, dict) for one in changes):
                raise TypeError
        except (ValueError, TypeError, KeyError):
            return error(400, "Send the changed boxes as a list of changes.")
        if arranging():
            return error(409, "Another bulk change is still running. Wait for it to finish; Files shows how far it has got.")
        found = await file_inventory(user, refresh=True)  # what is copied and removed is decided on what is there now
        busy = [node["name"] for node in found["nodes"] if (node.get("cleanup") or {}).get("state") == "running"]
        if busy:
            return error(409, "Wait for the cleanup on " + ", ".join(busy) + " to finish.")
        try:
            if transfer is not None:
                changes = library.transfer_changes(found, transfer)
            elif cleaning:
                changes = library.cleanup_changes(found)
            steps = library.plan(found, changes, copy_first=transfer is not None)
        except ValueError as problem:
            return error(409, str(problem))
        if not steps:
            return JSONResponse({"steps": []})
        if arranging():  # asked twice at once: the look through the folders took a while
            return error(409, "Another bulk change is still running. Wait for it to finish; Files shows how far it has got.")
        number = secrets.token_urlsafe(8)
        job = distributions[number] = {"id": number, "kind": "arrange", "state": "running", "by": user.name, "started": time.time(),
                                       "copy_first": transfer is not None,
                                       "steps": [{**step, "state": "queued", "done": 0, "of": step["size"]} for step in steps]}
        task = asyncio.create_task(run_arrangement(user, job))
        distribution_tasks.add(task)
        task.add_done_callback(distribution_tasks.discard)
        log.add("file", "arrange-started", user.name, request.client.host if request.client else "?",
                copies=sum(step["action"] == "copy" for step in steps), removals=sum(step["action"] == "remove" for step in steps))
        return JSONResponse(job, status_code=202)

    async def run_arrangement(user: User, job: dict) -> None:
        """Run machine copy lanes; transfers remove each source only after its destination succeeds."""
        async def remove(step):
            """Delete each copy of the file at that place. LoadCoach refuses a file a model uses."""
            for path in step["paths"]:
                body = json.dumps({"path": path, "name": Path(path).name}).encode()
                answer = await ask_machine(user, "POST", "/loadcoach/files/delete", body, machine=step["machine"], timeout=60)
                if answer is None or answer.status_code != 200:
                    raise ValueError(answer.json()["error"]["message"] if answer is not None else f"{step['machine']} is not answering.")

        async def copy(step):
            """Copy within one machine through its own Files copy, or between machines checked by SHA-256."""
            if step["source"] == step["machine"]:
                await promote_model_file(user, {"file": step["source_file"], "place": step["place"], "relative": step["relative"],
                                                "node": step["machine"], "path": step["path"]}, step, stopped="The copy stopped before it was complete.")
                return
            step["state"] = "checking"
            digest = await managed_digest(user, step["source"], step["source_file"]["path"])
            step["state"] = "copying"
            if step["machine"] == "front":
                async with node_content(user, step["source"], step["source_file"]["path"]) as upstream:
                    await front_files.receive(user, step["place"]["name"], step["relative"], upstream.aiter_bytes(PIECE),
                                              step["size"], digest, step, source=step["source"])
                return
            await transfer_model_file(user, {"node": step["machine"], "place": step["place"], "relative": step["relative"],
                                             "file": step["source_file"], "source": step["source"]}, digest, step)

        async def one(step):
            """Do one step and keep how it went, so one failure leaves the others to run."""
            step["state"] = "copying" if step["action"] == "copy" else "removing"
            try:
                await (copy if step["action"] == "copy" else remove)(step)
                step.update(state="done", done=step["of"])
            except (ValueError, OSError, KeyError, httpx.HTTPError) as problem:
                step.update(state="failed", why=str(problem) or type(problem).__name__)

        async def lane(steps):
            """The copies to one machine, one after another, so its drive writes one file at a time."""
            for step in steps:
                await one(step)

        try:
            for step in job["steps"]:
                if step["action"] == "remove" and not job.get("copy_first"):
                    await one(step)
            lanes: dict[str, list] = {}
            for step in job["steps"]:
                if step["action"] == "copy":
                    lanes.setdefault(step["machine"], []).append(step)
            await asyncio.gather(*(lane(steps) for steps in lanes.values()))
            if job.get("copy_first"):
                failed_rows = {step['row'] for step in job['steps'] if step['state'] == 'failed'}
                for step in job['steps']:
                    if step['action'] == 'remove':
                        if step['row'] in failed_rows:
                            step.update(state='failed', why='Source copy kept because the destination copy failed.')
                        else:
                            await one(step)
        finally:
            for step in job["steps"]:
                if step["state"] not in ("done", "failed"):
                    step.update(state="failed", why="Stopped before it was done.")
            failed = [step for step in job["steps"] if step["state"] == "failed"]
            job.update(state="failed" if failed else "complete", ended=time.time())
            log.add("file", "arranged", user.name, done=sum(step["state"] == "done" for step in job["steps"]), failed=len(failed))

    async def batch_start(request: Request, user: User) -> Response:
        """Create a durable batch, kept on the front, through the existing scoped APIs."""
        return await batch_work.batch_start(request, user, users.by_name)

    async def batch_one(request: Request, user: User) -> Response:
        """A batch of a program's is kept here. Any other number is an external task's, which compute keeps: it answers."""
        if batch_work.store.get(request.path_params["number"]) is None:
            return await pass_on(request)
        return await batch_work.batch_one(request, user)

    async def batches_listed(request: Request, user: User) -> Response:
        """The batches kept here, and with them what compute keeps of external tasks, as one list."""
        ours = json.loads((await batch_work.batch_list(request, user)).body)
        theirs = await ask_compute(user, "GET", request.url.path)
        found = theirs.json() if theirs is not None and theirs.status_code == 200 else {}
        key = "data" if "data" in ours else "batches"
        legacy = [one for one in found.get(key, []) if batch_work.store.get(one['id']) is None]
        merged = sorted([*ours.get(key, []), *legacy], key=lambda one: -one.get("created_at", one.get("created", 0)))
        return JSONResponse({**ours, key: merged})

    async def machine_wake(request: Request) -> Response:
        """Wake exactly the configured node, without needing its services online."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("front-wake"):
            return error(403, "Your group may not wake nodes.")
        name = request.path_params["name"]
        if name not in machines.names:
            return error(404, "Unknown node.")
        if not machines.enabled(name):
            return error(409, f"Compute node {name} is {machines.state(name)}; wake is disabled.")
        policy = settings.machines.get(name, {}) if name != "compute" else {
            "wake_mac": settings.wake_mac, "wake_address": settings.wake_address}
        if not policy.get("wake_mac"):
            return error(409, "Wake-on-LAN is not configured for this node.")
        try:
            waker(policy["wake_mac"], policy.get("wake_address", "255.255.255.255"))
        except (OSError, ValueError):
            return error(503, "Could not send the wake packet. Check the node wake configuration.")
        return JSONResponse({"requested": True, "machine": name, "timeout_seconds": settings.wait}, status_code=202)

    async def machines_list(request: Request) -> Response:
        """The compute machines, what each last said of itself and how long ago. For anyone with a key."""
        if caller(key_of(request)) is None:
            return error(401, "Missing or wrong API key.")
        hosts = with_power_state(machines.hosts(), machines.seen, notice)
        for host in hosts:
            host["wake_nodes"] = [name for name in host["machines"] if machines.enabled(name) and
                (settings.wake_mac if name == "compute" else settings.machines.get(name, {}).get("wake_mac"))]
        include_retired = request.query_params.get("include_retired") == "1"
        if include_retired and not caller(key_of(request)).can("manage-settings"):
            return error(403, "Node lifecycle settings require manage-settings.")
        return JSONResponse({"machines": machines.report(include_retired), "hosts": hosts,
                             "models": machines.models(), "poll_seconds": settings.machines_poll_seconds,
                             "artifact_machine": file_store()},
                            headers={"cache-control": "no-store"})

    async def machine_lifecycle(request: Request) -> Response:
        """Change front's durable registration, even when the compute node is unreachable."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-settings"):
            return error(403, "Node lifecycle settings require manage-settings.")
        name = request.path_params["name"]
        if name not in machines.all_names():
            return error(404, "Unknown compute node.")
        try:
            asked = json.loads(await request.body())
            state = asked["state"]
            previous = machines.state(name)
            machines.change_state(name, state, user.name)
        except (ValueError, KeyError, TypeError):
            return error(400, "Choose active, disabled or retired; enabling requires a configured node.")
        facts_seen.clear()
        ready_cache.clear()
        if previous != state:
            log.add("machine", state, user.name, "operator", machine=name, previous=previous)
        return JSONResponse(next(row for row in machines.report(True) if row["name"] == name),
                            headers={"cache-control": "no-store"})

    async def logical_models(request: Request) -> Response:
        """Manage-page catalogue: one verified logical model with its complete per-node copies."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Your account may not add, change and remove models.")
        names = ["compute", *settings.machines]
        answers = await asyncio.gather(*(ask_live(user, "GET", "/loadcoach/models", machine="" if name == "compute" else name, passive=passive(request))
                                         for name in names))
        detailed = {name: answer.json() for name, answer in zip(names, answers) if answer is not None and answer.status_code == 200}
        if not detailed:
            return JSONResponse({"models": [], "nodes": {}, "defaults": {}, "cards": [],
                                 "to_measure": [], "measuring": None, "unavailable_machines": machines.names})
        summaries = logical_catalogue({name: {"data": [
            {"id": model["name"], "owned_by": "loadcoach", "model_id": model.get("model_id"),
             "fingerprint": model.get("fingerprint"), "content_sha256": model.get("sha256"),
             "engine_id": model.get("engine", "llama_cpp"), "capabilities": model.get("capabilities", {}),
             "order": model.get("order", 10)}
            for model in result.get("models", [])]}
            for name, result in detailed.items()})
        rows = []
        for logical in summaries:
            locations = []
            for place in logical["locations"]:
                model = next(one for one in detailed[place["machine"]]["models"] if one["name"] == place["id"])
                locations.append({**model, "machine": place["machine"],
                                  "enabled": model_locations.enabled(place["machine"], model.get("model_id"))})
            canonical = locations[0]
            rows.append({**canonical, "name": logical["id"], "logical_id": logical.get("logical_id", ""),
                         "machines": logical["machines"], "locations": locations})
        primary = detailed.get("compute") or next(iter(detailed.values()))
        node_fields = ("defaults", "cards", "engine_cards", "devices", "memory_policy", "build", "to_measure", "measuring")
        return JSONResponse({**{key: primary.get(key) for key in node_fields},
                             "nodes": {name: {key: result.get(key) for key in node_fields} for name, result in detailed.items()},
                             "models": rows, 'unavailable_machines': [name for name in names if name not in detailed]}, headers={"cache-control": "no-store"})

    async def model_location(request: Request) -> Response:
        """Enable or disable one configured copy for front-side placement."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Your account may not add, change and remove models.")
        try:
            asked = json.loads(await request.body())
        except (ValueError, TypeError):
            return error(400, "The model location change is not valid.")
        machine, model_id, enabled = asked.get("machine"), asked.get("model_id"), asked.get("enabled")
        if machine not in machines.names or not isinstance(model_id, str) or not model_id or type(enabled) is not bool:
            return error(400, "machine, model_id and enabled are required.")
        model_locations.set(machine, model_id, enabled, user.name)
        facts_seen.clear()
        log.add("model-location", "enabled" if enabled else "disabled", user.name,
                request.client.host if request.client else "?", machine=machine, model_id=model_id)
        return JSONResponse({"machine": machine, "model_id": model_id, "enabled": enabled})

    async def front_file_settings(request: Request) -> Response:
        """Configure optional front storage even before a worker has connected."""
        user = caller(key_of(request))
        if user is None:
            return error(401, 'Missing or wrong API key.')
        if not user.can('manage-settings'):
            return error(403, 'Storage settings require manage-settings.')
        if request.method == 'POST':
            try:
                raw = await request.body()
                if len(raw) > 65536:
                    raise ValueError('Storage settings must be at most 64 KiB.')
                paths = checked_roots({**front_file_roots(), **json.loads(raw)})
                if paths != front_file_roots() and front_files.tasks:
                    return error(409, 'Wait for front file transfers to finish before changing their folders.')
                for folder in model_places(paths['models_folder'], {}, paths['models_storage_folder']).values():
                    await asyncio.to_thread(Path(folder).mkdir, parents=True, exist_ok=True)
            except (OSError, ValueError, TypeError) as problem:
                return error(400, str(problem))
            for name, value in paths.items():
                console_settings.changes.put('turnstile', name, value, user.name)
            log.add('settings', 'changed', user.name, machine='front', program='model-files', **paths)
        return JSONResponse({'machine': 'front', 'locations': front_file_roots(), 'pending': False},
                            headers={'cache-control': 'no-store'})

    def pending_file_settings(name):
        """Read durable model-folder changes queued for exactly one configured node."""
        return console_settings.changes.of('node-files:' + name).get('pending')

    publication_lock = asyncio.Lock()

    async def publish_file_settings():
        """Retry queued roots without waking nodes, rechecking the saving account's authority."""
        async with publication_lock:
            for name in machines.names:
                if machines.state(name) == "retired":
                    continue
                pending = pending_file_settings(name)
                if not pending:
                    continue
                owner = users.by_identity(pending['owner'])
                if owner is None or not owner.can('manage-settings'):
                    continue
                answer = await ask_compute(owner, 'POST', '/loadcoach/settings/loadcoach',
                                           json.dumps(pending['locations']).encode(),
                                           machine='' if name == 'compute' else name, timeout=5)
                if answer is not None and answer.status_code == 200 and pending_file_settings(name) == pending:
                    console_settings.changes.take_away('node-files:' + name, 'pending')
                    log.add('settings', 'delivered', owner.name, machine=name, program='model-files')
                elif answer is not None and answer.status_code >= 400:
                    why = answer.json().get('error', {}).get('message', 'Node refused the saved locations.')
                    if why != pending.get('error'):
                        console_settings.changes.put('node-files:' + name, 'pending', {**pending, 'error': why}, owner.name)
                        log.add('settings', 'delivery-failed', owner.name, machine=name, program='model-files', why=why)

    def settings_with_pending(name, result):
        """Expose editable folder settings even when a node has never reported its configuration."""
        pending = pending_file_settings(name)
        programs = result.setdefault('programs', [])
        program = next((one for one in programs if one['program'] == 'loadcoach'), None)
        if program is None:
            program = {'program': 'loadcoach', 'seen': None, 'settings': []}
            programs.append(program)
        for field in ('models_folder', 'models_storage_folder'):
            row = next((one for one in program['settings'] if one['name'] == field), None)
            if row is None:
                row = {'name': field, 'value': '', 'file': '', 'when': 'restart', 'changed': None,
                       'says': 'Model-file location. Empty disables this location.'}
                program['settings'].append(row)
            if pending and field in pending['locations']:
                desired = pending['locations'][field]
                row['changed'] = {'value': row.get('file', '') if desired is None else desired,
                                  'by': pending['by'], 'at': pending['at']}
        return {**result, 'pending': bool(pending), 'pending_error': (pending or {}).get('error', '')}

    async def machine_settings(request: Request) -> Response:
        """Read or edit one host's LoadCoach/Spotter settings; never wake or fall back to another host."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-settings"):
            return error(403, "Host settings require manage-settings.")
        name, program = request.path_params["name"], request.path_params.get("program")
        if name not in machines.names:
            return error(404, "That LoadCoach is not configured.")
        if program is not None and program not in ("loadcoach", "spotter"):
            return error(403, "Only LoadCoach and Spotter settings belong to the selected host.")
        if (request.method == "GET") != (program is None):
            return error(405, "Unsupported host settings operation.")
        path = "/loadcoach/settings" + ("/" + program if program else "")
        if request.method == 'GET' and pending_file_settings(name):
            await publish_file_settings()
        raw = await request.body() if request.method == 'POST' else b''
        locations = None
        if program == 'loadcoach':
            try:
                body = json.loads(raw)
                if isinstance(body, dict) and body and not body.keys() - {'models_folder', 'models_storage_folder'}:
                    previous = pending_file_settings(name)
                    current = (previous or {}).get('locations', {})
                    locations = {**current, **body}
                    checked_roots({field: value for field, value in locations.items() if value is not None})
            except (ValueError, TypeError) as problem:
                return error(400, str(problem))
        if locations is not None:
            async with publication_lock:
                previous = pending_file_settings(name)
                locations = {**(previous or {}).get('locations', {}), **body}
                try:
                    checked_roots({field: value for field, value in locations.items() if value is not None})
                except (ValueError, TypeError) as problem:
                    return error(400, str(problem))
                raw = json.dumps(locations).encode()
                answer = await ask_compute(user, request.method, path, raw,
                                           machine='' if name == 'compute' else name, timeout=5)
                if answer is None:
                    console_settings.changes.put('node-files:' + name, 'pending',
                                              {'locations': locations, 'owner': user.identity, 'by': user.name,
                                               'at': datetime.now().isoformat(timespec='seconds')}, user.name)
                    return JSONResponse({'machine': name, 'pending': True, 'locations': locations}, status_code=202)
                if answer.status_code == 200:
                    console_settings.changes.take_away('node-files:' + name, 'pending')
        else:
            answer = (await ask_live(user, "GET", path, machine=name) if request.method == "GET" else
                      await ask_compute(user, request.method, path, raw,
                                        machine="" if name == "compute" else name, timeout=5))
        if answer is None:
            if request.method == 'GET':
                return JSONResponse({**settings_with_pending(name, {}), 'machine': name, 'unavailable': True},
                                    headers={'cache-control': 'no-store'})
            return JSONResponse({"error": {"message": f"LoadCoach {name} is not answering; verify its settings before retrying."}}, status_code=503, headers={"Retry-After": "5"})
        if answer.status_code != 200:
            return JSONResponse(answer.json(), status_code=answer.status_code)
        result = answer.json()
        if "programs" in result:
            result["programs"] = [p for p in result["programs"] if p["program"] in ("loadcoach", "spotter")]
        return JSONResponse({**settings_with_pending(name, result), "machine": name, "unavailable": bool(answer.headers.get("x-suite-retained"))},
                            headers={"cache-control": "no-store"})

    async def machine_engines(request: Request) -> Response:
        """Inspect one node's engines under the caller's identity; no wake or fallback."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models") and not user.can("manage-settings"):
            return error(403, "Engine settings require manage-models or manage-settings.")
        name = request.path_params["name"]
        if name not in machines.names:
            return error(404, "That compute node is not configured.")
        answer = await ask_live(user, "GET", "/loadcoach/engines", machine=name)
        if answer is None:
            return error(503, f"LoadCoach {name} is not answering.")
        return JSONResponse(answer.json(), status_code=answer.status_code, headers={"cache-control": "no-store"})

    async def components_list(request: Request) -> Response:
        """Which services and engines front and each compute node have. Changes nothing, wakes nothing; a node that does not answer says so."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-settings"):
            return error(403, "Seeing what is installed requires manage-settings.")
        from loadcoach import components
        shared = Path(settings.path).parent / "loadcoach.toml" if settings.path else None
        front = {"name": "front", "state": "ready", "answering": True, "with_front": True,
                 **await components.here(running_here=("turnstile",), settings=shared, versions=component_versions)}
        reports = {one["name"]: one for one in machines.report()}

        def on_front(name: str) -> bool:
            """Whether this node's LoadCoach is reached on front's own loopback address, so it is the same machine."""
            address = settings.compute if name == "compute" else urlsplit(settings.machines[name]["compute"]).hostname or ""
            return address in LOOPBACK

        async def node(name: str) -> dict:
            """What one compute node says it has, or why it says nothing."""
            found ={"name": name, "state": reports.get(name, {}).get("state", "unknown"), "with_front": on_front(name)}
            answer = await ask_live(user, "GET", "/loadcoach/components", machine=name, passive=True)
            if answer is not None and answer.status_code == 200:
                told = answer.json()
                found["with_front"] = found["with_front"] or told.get("hostname") == front["hostname"]
                return {**found, **told, "answering": True}
            why = ("This node is not answering, so what it has cannot be checked. It is not woken for this." if answer is None
                   else "This node runs an older openWeightGym that cannot say what is installed. Update it to see." if answer.status_code == 404
                   else f"This node did not say what is installed ({answer.status_code}).")
            return {**found, "answering": False, "why": why}

        nodes = await asyncio.gather(*(node(name) for name in machines.names))
        return JSONResponse({"nodes": [front, *nodes], **components.everything()}, headers={"cache-control": "no-store"})

    async def machine_model_api(request: Request) -> Response:
        """Bounded model-management surface for one explicitly selected node."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Model configuration requires manage-models.")
        name, suffix = request.path_params["name"], request.path_params["operation"]
        if name not in machines.names:
            return error(404, "That compute node is not configured.")
        if any(part in (".", "..") for part in suffix.split("/")) or "?" in suffix or "#" in suffix:
            return error(400, "Invalid node model path.")
        allowed = {"models": {"GET"}, "flags": {"GET"}, "file": {"GET"}, "files": {"GET"},
                   "model-setup": {"GET", "POST"}, "model-rename": {"POST"}, "model-forget-measure": {"POST"},
                   "model-measure-all": {"POST"}, "images/check": {"POST"}, "audio/packages": {"GET", "POST"}, "audio/families": {"GET"}}
        methods = {"POST", "DELETE"} if suffix.startswith("models/") else allowed.get(suffix, set())
        if request.method not in methods:
            return error(405, "Unsupported node model operation.")
        path = "/loadcoach/" + suffix
        if request.url.query:
            path += "?" + request.url.query
        raw = await request.body()
        try:
            body = json.loads(raw) if raw and request.method == "POST" else {}
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            return error(400, "Send a model configuration object.")
        if request.method == "POST" and (suffix.startswith("models/") or suffix == "model-setup" and body.get("action") == "apply"):
            async with configuration_lock:
                try:
                    answer = await configure_model(user, name, suffix, path, raw, body)
                except (ValueError, KeyError, TypeError) as problem:
                    return error(400, str(problem))
        else:
            answer = (await ask_live(user, "GET", path, machine=name) if request.method == "GET" else
                      await ask_compute(user, request.method, path, raw,
                                        machine="" if name == "compute" else name, timeout=20))
        if isinstance(answer, Response):
            return answer
        if answer is None:
            return error(503, f"LoadCoach {name} is not answering.")
        if request.method != "GET" and answer.is_success and suffix != "images/check":
            facts_seen.clear()
        return JSONResponse(answer.json(), status_code=answer.status_code, headers={"cache-control": "no-store"})

    async def logical_copies(user, machine, local_name, before):
        """Every verified copy of one node's model, by node: (inventories, locations), or an error.
        A shared-setting change must see every node, so an unreachable one refuses it."""
        inventories = await asyncio.gather(*(ask_machine(user, "GET", "/loadcoach/models", machine=node, timeout=5)
                                              for node in machines.names))
        unavailable = [node for node, answer in zip(machines.names, inventories) if answer is None or answer.status_code != 200]
        if unavailable:
            return error(409, "Shared model settings were not saved: these nodes cannot be checked: " + ", ".join(unavailable) + ".")
        digest = before.get("sha256")
        if not digest and len(machines.names) > 1:
            return error(409, "Wait for the selected model's files to finish hashing before changing shared model settings.")
        found = {node: {"data": [{"id": row["name"], "owned_by": "loadcoach", "content_sha256": row.get("sha256"),
                                  "engine_id": row.get("engine", "llama_cpp")} for row in answer.json()["models"]]}
                 for node, answer in zip(machines.names, inventories)}
        logical = next((row for row in logical_catalogue(found) if any(
            loc["machine"] == machine and loc["id"] == local_name for loc in row["locations"])), {}) if digest else {}
        return ({node: answer.json() for node, answer in zip(machines.names, inventories)},
                logical.get("locations") or [{"machine": machine, "id": local_name}])

    async def shared_for_peers(user, machine, values, locations, inventories, skip=()):
        """Each other copy's saved settings and the same settings carrying `values`, with the
        projector and adapter paths that copy's node keeps; an error when a node lacks a file."""
        nodes = await model_file_inventory(user) if values["projector"] or values["adapters"] else []
        peers, missing = [], []
        for location in locations:
            node = location["machine"]
            if node == machine or node in skip:
                continue
            inventory = inventories[node]
            row = next(row for row in inventory["models"] if row["name"] == location["id"])
            def moved(path, node=node):
                """The shared file's path on this copy's node."""
                there = translate(path, machine, node, nodes)
                if there is None:
                    missing.append(f"{Path(path).name} on {node}")
                return there or path
            saved = configuration(row)
            applied = with_shared(saved, values, moved)
            for flag in generation({"flags": inventory.get("defaults") or {}})["flags"]:
                applied["flags"].setdefault(flag, None)
            peers.append((node, row["name"], saved, applied))
        if missing:
            return error(409, "Shared model settings were not saved: " + ", ".join(missing) + " is missing. Copy it there in Files first.")
        return peers

    async def configure_model(user, machine, suffix, path, raw, body):
        """Apply local loading settings and keep the shared settings the same on every verified copy."""
        local_name = suffix.removeprefix("models/") if suffix.startswith("models/") else body.get("name")
        current = await ask_machine(user, "GET", "/loadcoach/models", machine=machine, timeout=5)
        if current is None or current.status_code != 200:
            return error(503, f"LoadCoach {machine} is not answering.")
        before = next((row for row in current.json()["models"] if row["name"] == local_name), None)
        configured = body if suffix.startswith("models/") else body.get("configuration") or {}
        candidate = configured if suffix.startswith("models/") else configured.get("settings")
        order = configured.get("order") if suffix.startswith("models/") or "order" in configured else None
        # Resolve preset/undo through the same read-only preview the worker will validate on apply.
        if before and candidate is None:
            preview = await ask_machine(user, "POST", "/loadcoach/model-setup",
                                        json.dumps({**body, "action": "preview"}).encode(), machine=machine)
            if preview is None or preview.status_code != 200:
                return error(409, "The model configuration could not be previewed.")
            candidate, order = preview.json()["candidate"]["settings"], preview.json()["candidate"]["order"]
        selected = engine_for(before or candidate or {})
        defaults = current.json().get("defaults") or {}
        values = shared(selected.model.effective(candidate, defaults), order) if isinstance(candidate, dict) and "flags" in candidate else None
        changed = before and values is not None and shared(selected.model.effective(configuration(before), defaults)) != values
        peers = []
        if changed:
            copies = await logical_copies(user, machine, local_name, before)
            if isinstance(copies, Response):
                return copies
            peers = await shared_for_peers(user, machine, values, copies[1], copies[0])
            if isinstance(peers, Response):
                return peers
        answer = await ask_machine(user, "POST", path, raw, machine=machine, timeout=30)
        if answer is None or not answer.is_success:
            return answer
        applied = []
        for node, label, saved, candidate in peers:
            # Include a timed-out write in rollback: it may have reached its worker.
            applied.append((node, label, saved))
            result = await ask_machine(user, "POST", "/loadcoach/models/" + quote(label, safe=""), json.dumps(candidate).encode(), machine=node)
            if result is None or not result.is_success:
                failures = []
                for restore_node, restore_name, restore in [*reversed(applied), (machine, local_name, configuration(before))]:
                    restored = await ask_machine(user, "POST", "/loadcoach/models/" + quote(restore_name, safe=""),
                                                 json.dumps(restore).encode(), machine=restore_node)
                    if restored is None or not restored.is_success:
                        failures.append(restore_node)
                return error(409, "Shared model settings could not be saved. " +
                             ("Verify configuration on: " + ", ".join(failures) + "." if failures else "The previous settings were restored."))
        return answer

    async def configure_models(request: Request) -> Response:
        """Apply reviewed configurations to several copies of one model together, or to none.

        Each change is one node's previewed configuration and its review token. Shared settings
        must agree, and a change to them must include every copy. A failure restores the copies
        already changed, so a node is never left with only part of a reviewed change."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-models"):
            return error(403, "Model configuration requires manage-models.")
        try:
            asked = json.loads(await request.body())
            changes = asked["changes"]
            if not isinstance(changes, list) or not changes or not all(
                    isinstance(one, dict) and one.get("machine") in machines.names and isinstance(one.get("name"), str)
                    and isinstance(one.get("configuration"), dict) and isinstance(one.get("token"), str) for one in changes):
                raise ValueError("Send each change as a node, a model name, a configuration and its review token.")
            if len({one["machine"] for one in changes}) != len(changes):
                raise ValueError("Send one change for each node.")
        except (ValueError, KeyError, TypeError) as problem:
            return error(400, str(problem) if isinstance(problem, ValueError) and str(problem).startswith("Send") else "Send a list of reviewed changes.")
        async with configuration_lock:
            rows, values = [], []
            for one in changes:
                current = await ask_machine(user, "GET", "/loadcoach/models", machine=one["machine"], timeout=5)
                if current is None or current.status_code != 200:
                    return error(503, f"LoadCoach {one['machine']} is not answering. Nothing was changed.")
                before = next((row for row in current.json()["models"] if row["name"] == one["name"]), None)
                settings = one["configuration"].get("settings")
                if before is None or not isinstance(settings, dict) or "flags" not in settings:
                    return error(404, f"{one['name']} is not on {one['machine']}. Nothing was changed.")
                selected, defaults = engine_for(before), current.json().get("defaults") or {}
                rows.append(before)
                values.append((shared(selected.model.effective(configuration(before), defaults)),
                               shared(selected.model.effective(settings, defaults), one["configuration"].get("order"))))
            if any(comparable(after) != comparable(values[0][1]) for _, after in values):
                return error(400, "The copies disagree on shared settings. Preview them again. Nothing was changed.")
            if any(before != after for before, after in values):
                copies = await logical_copies(user, changes[0]["machine"], changes[0]["name"], rows[0])
                if isinstance(copies, Response):
                    return copies
                left = sorted({loc["machine"] for loc in copies[1]} - {one["machine"] for one in changes})
                if left:
                    return error(409, "Shared settings change on every copy; include " + ", ".join(left) + ". Nothing was changed.")
            applied = []
            for one, before in zip(changes, rows):
                # Include a timed-out write in rollback: it may have reached its worker.
                applied.append((one["machine"], one["name"], configuration(before)))
                answer = await ask_machine(user, "POST", "/loadcoach/model-setup", json.dumps({
                    "name": one["name"], "action": "apply", "configuration": one["configuration"], "token": one["token"],
                    "acknowledge": asked.get("acknowledge") is True}).encode(), machine=one["machine"], timeout=30)
                if answer is None or not answer.is_success:
                    why = answer.json().get("error", {}).get("message", "It refused the change.") if answer is not None else "It stopped answering."
                    failures = []
                    for node, label, restore in reversed(applied[:-1] if answer is not None else applied):
                        restored = await ask_machine(user, "POST", "/loadcoach/models/" + quote(label, safe=""),
                                                     json.dumps(restore).encode(), machine=node)
                        if restored is None or not restored.is_success:
                            failures.append(node)
                    return error(409, f"{one['machine']}: {why} " + ("Verify configuration on: " + ", ".join(failures) + "." if failures
                                                                        else "Nothing was changed."))
            facts_seen.clear()
        return JSONResponse({"applied": [one["machine"] for one in changes]})

    async def machine_cards(request: Request) -> Response:
        """Read or change one node's detected-card exclusions without host fallback."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("manage-settings"):
            return error(403, "GPU settings require manage-settings.")
        name = request.path_params["name"]
        if name not in machines.names:
            return error(404, "That LoadCoach is not configured.")
        answer = await ask_compute(user, request.method, "/loadcoach/cards", await request.body(),
                                   machine="" if name == "compute" else name, timeout=5)
        if answer is None:
            return error(503, f"LoadCoach {name} is not answering.", {"Retry-After": "5"})
        return JSONResponse(answer.json(), status_code=answer.status_code, headers={"cache-control": "no-store"})

    async def machine_databases(request: Request) -> Response:
        """Signed, bounded node-specific storage API. Never wake or fall back to another node."""
        from loadcoach.database_admin import SAFE_ID
        from loadcoach.database_jobs import ID
        user = caller(key_of(request))
        if user is None:
            return error(401, 'Missing or wrong API key.')
        if not user.can('manage-settings'):
            return error(403, 'Database operations require manage-settings.')
        name = request.path_params['name']
        if name not in machines.names:
            return error(404, 'That compute node is not configured.')
        if name != 'compute' and name not in settings.machines:
            return error(404, 'That compute node has no configured database endpoint.')
        if machines.state(name) == 'retired':
            return error(409, 'This node is retired; its databases are not queried.')
        parts = request.path_params['operation'].split('/')
        method = request.method
        path = ''
        if parts == ['inventory'] and method == 'GET':
            path = '/loadcoach/databases?summary=1'
        elif len(parts) == 3 and parts[0] == 'stores' and SAFE_ID.fullmatch(parts[1]) and method == 'GET':
            if parts[2] == 'dump':
                path = '/loadcoach/databases/' + quote(parts[1], safe='') + '/dump'
                if table := request.query_params.get('table'):
                    path += '?' + urlencode({'table': table})
                if identity := request.query_params.get('identity'):
                    path += ('&' if '?' in path else '?') + urlencode({'identity': identity})
            elif parts[2] == 'details':
                path = '/loadcoach/database-operations/details/' + quote(parts[1], safe='')
        elif parts in [['preview'], ['execute']] and method == 'POST':
            path = '/loadcoach/database-operations/' + parts[0]
        elif parts == ['jobs'] and method == 'GET':
            path = '/loadcoach/database-operations/jobs'
        elif len(parts) in {2, 3} and parts[0] == 'jobs' and ID.fullmatch(parts[1]):
            if method == 'GET' and (len(parts) == 2 or parts[2] == 'recovery') or method == 'POST' and parts[2:] == ['cancel']:
                path = '/loadcoach/database-operations/' + '/'.join(parts)
        elif parts == ['backups'] and method in {'GET', 'POST'}:
            path = '/loadcoach/backups'
        elif len(parts) == 2 and parts[0] == 'backups' and SAFE_ID.fullmatch(parts[1]) and method == 'GET':
            path = '/loadcoach/backups/' + parts[1]
        if not path:
            return error(405, 'Unsupported node database operation.')
        # Direct signed HTTP does not enter Turnstile's wake/retry machinery.
        other = settings.machines.get(name, {}) if name != 'compute' else {}
        base = other.get('compute', settings.upstream(settings.ports[0])).rstrip('/')
        body = await request.body() if method == 'POST' else b''
        headers, nonce = signed_headers(user, method, path, body, [('content-type', 'application/json')], key_file=other.get('machine_key_file', ''))
        streamed = parts[-1] in {'dump', 'recovery'}
        try:
            upstream = internal.build_request(method, base + path, headers=headers, content=body,
                timeout=httpx.Timeout(905 if streamed or parts[0] == 'backups' and method == 'POST' else 65 if parts[-1] == 'details' else 30 if parts[0] == 'preview' else 12, connect=2))
            answer = await internal.send(upstream, stream=True)
        except httpx.HTTPError:
            return error(503, f'LoadCoach {name} is not answering. No other node was used.')
        if not proven(answer, nonce, other.get('machine_key_file', '')):
            await answer.aclose()
            return error(502, 'This node did not prove its database response.')
        if not streamed or answer.status_code != 200:
            try:
                content = await answer.aread()
                if answer.status_code == 200 and parts == ['inventory']:
                    result = json.loads(content)
                    result['machine'] = name
                    for row in [*result.get('databases', []), *result.get('health', [])]:
                        row['machine'] = name
                    content = json.dumps(result).encode()
                return Response(content, status_code=answer.status_code, media_type=answer.headers.get('content-type'), headers={'cache-control': 'no-store'})
            finally:
                await answer.aclose()

        async def chunks():
            try:
                async for block in answer.aiter_bytes():
                    yield block
            finally:
                await answer.aclose()
        forwarded = {key: value for key, value in answer.headers.items() if key in {'content-type', 'content-disposition', 'x-suite-dump-method'}}
        return StreamingResponse(chunks(), headers={**forwarded, 'cache-control': 'no-store'})

    async def machine_model_lock(request: Request) -> Response:
        """Apply a Status-page lock to the named LoadCoach, which validates the caller's ability."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        name = request.path_params["name"]
        if name not in machines.names:
            return error(404, "That LoadCoach is not configured.")
        answer = await ask_compute(user, "POST", "/loadcoach/model-lock", await request.body(),
                                   machine="" if name == "compute" else name)
        if answer is None:
            return error(503, f"LoadCoach {name} is not answering.")
        return JSONResponse(answer.json(), status_code=answer.status_code)

    async def machine_probe_environment(request: Request) -> Response:
        """Current benchmark environment for one named location, without wake or fallback."""
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        if not user.can("benchmark") and not user.can("manage-models"):
            return error(403, "Benchmark environment requires benchmark or manage-models.")
        name = request.path_params["name"]
        if name not in machines.names:
            return error(404, "That compute location is not configured.")
        answer = await ask_compute(user, "GET", "/loadcoach/probe-environment", machine="" if name == "compute" else name,
                                   params={"engine": request.query_params["engine"]} if "engine" in request.query_params else None, timeout=5)
        if answer is None:
            return error(503, f"LoadCoach {name} is not answering.")
        return JSONResponse(answer.json(), status_code=answer.status_code, headers={"cache-control": "no-store"})

    async def machine_control(request: Request) -> Response:
        """Forward node status, maintenance or GET/POST load-outs under the signed caller.

        Load-outs support list/save/preview/apply/request/renew/release/delete on this node's
        own store; worker permissions, review tokens and 600-second leases remain authoritative.
        This route never wakes a worker or falls back to another node.
        """
        user = caller(key_of(request))
        if user is None:
            return error(401, "Missing or wrong API key.")
        name, action = request.path_params["name"], request.path_params.get("action", 'loadouts')
        if name not in machines.names:
            return error(404, "That LoadCoach is not configured.")
        paths = {"status": "status", "drain": "drain", "clear": "clear", "unload": "unload", 'loadouts': 'loadouts'}
        if action not in paths or (action != 'loadouts' and (request.method == "GET") != (action == "status")):
            return error(404, "That LoadCoach control is not available.")
        body = await request.body()
        if action == 'loadouts' and len(body) > 65536:
            return error(413, 'Load-out requests may be at most 64 KiB.')
        forwarded = tuple((key, request.headers[key]) for key in ('x-loadcoach-lock', 'x-loadcoach-priority') if key in request.headers) if action == 'loadouts' else ()
        answer = await ask_compute(user, request.method, "/loadcoach/" + paths[action],
                                   body, machine="" if name == "compute" else name,
                                   timeout=None if action in ('unload', 'loadouts') else 30, forwarded=forwarded)
        if answer is None:
            return error(503, f"LoadCoach {name} is not answering.")
        return JSONResponse(answer.json(), status_code=answer.status_code,
                            headers={'cache-control': 'no-store', **({'retry-after': answer.headers['retry-after']} if 'retry-after' in answer.headers else {})})

    async def telemetry_history(request: Request) -> Response:
        """Return retained physical-host telemetry within the requested range."""
        if caller(key_of(request)) is None:
            return error(401, "Missing or wrong API key.")
        fingerprint = request.path_params["fingerprint"]
        if fingerprint not in {host["fingerprint"] for host in machines.hosts()}:
            return error(404, "Machine fingerprint is unknown.")
        try:
            end = float(request.query_params.get("end") or time.time())
            seconds = min(max(float(request.query_params.get("seconds") or 3600), 60), 28 * 86400)
            points = min(max(int(request.query_params.get("points") or 240), 2), 1000)
        except ValueError:
            return error(400, "end, seconds and points must be numbers.")
        return JSONResponse(machines.history(fingerprint, end, seconds, points), headers={"cache-control": "no-store"})

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

    async def checkpoint_installations():
        while True:
            installation_store.checkpoint(job for job in distributions.values() if job.get('kind')=='model-install')
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        """Keep the plugin synchronization and the daily tidying with turnstile, independent of the weightroom page process."""
        own = Path(settings.auth_key_file) if settings.auth_key_file else None
        batch_work.door = httpx.AsyncClient(transport=httpx.ASGITransport(app=observation.internal(app), client=("127.0.0.1", 1)), base_url=f"http://turnstile.internal:{settings.ports[0]}")
        tasks = [asyncio.create_task(tidy()), asyncio.create_task(machines.run()), asyncio.create_task(rotate_due_keys()),
                 asyncio.create_task(checkpoint_installations())]
        await task_records.retry_publication()
        batch_work.resume(users.by_name)
        if plugins:
            tasks += [asyncio.create_task(plugins.controller.run())]
            if own and own.is_file():
                tasks.append(asyncio.create_task(plugins.mirror(own.read_text().strip())))
        if users.groups.taken_over:
            log.add("group", "taken-over", count=users.groups.taken_over)
        try:
            yield
        finally:
            await front_files.close()
            maintenance_stop.set()
            for work in list(task_records.working.values()):
                work.cancel()
            await asyncio.gather(*list(task_records.working.values()), return_exceptions=True)
            for task in [*tasks, *batch_work.carrying]:
                task.cancel()
            for task in distribution_tasks:
                task.cancel()
            for task in list(discovery_refreshes.values()):
                task.cancel()
            await asyncio.gather(*tasks, *batch_work.carrying, *distribution_tasks, *list(discovery_refreshes.values()), return_exceptions=True)
            await batch_work.door.aclose()
            console_settings.changes.db.close()
            model_locations.db.close()
            installation_store.db.close()
            component_versions.db.close()
            if plugins:
                plugins.registry.db.close()
            if adapter_client is None:
                await adapters.aclose()
            if client is None:
                await compute.aclose()
                await internal.aclose()
            if hosted_client is None:
                await hosted.client.aclose()
            await observation.shutdown(app)

    app = Starlette(lifespan=lifespan, routes=[*image_work.routes(caller,log), *audio_work.routes(caller, log),Route("/turnstile/ready", ready),
                            Route("/turnstile/power", power_notice, methods=["POST"]),
                            Route("/turnstile/accounts", accounts_list),
                            Route("/turnstile/machines", machines_list),
                            Route("/turnstile/machines/{name}/lifecycle", machine_lifecycle, methods=["POST"]),
                            Route("/turnstile/machines/{name}/wake", machine_wake, methods=["POST"]),
                            Route("/turnstile/artifacts/files", artifact_files),
                            Route("/turnstile/artifacts/settings", front_file_settings, methods=['GET', 'POST']),
                            *[Route("/turnstile/artifacts/files/" + action, artifact_cleanup, methods=["POST"])
                              for action in ("clear-cache", "consolidate-cache", "cleanup-nodes")],
                            Route("/turnstile/artifacts/distribute", distribute, methods=["POST"]),
                            Route("/turnstile/artifacts/distributions", distribution_list),
                            Route("/turnstile/artifacts/arrange", arrange, methods=["POST"]),
                            Route("/turnstile/artifacts/{suffix:path}", artifact_proxy, methods=["GET", "POST"]),
                            Route("/turnstile/models", logical_models),
                            Route("/turnstile/models/files", model_files_available),
                            Route("/turnstile/models/image-check", image_model_check, methods=["POST"]),
                            Route("/turnstile/models/install", install_model, methods=["POST"]),
                            Route('/turnstile/models/install/{number}/retry', retry_installation, methods=['POST']),
                            Route("/turnstile/models/location", model_location, methods=["POST"]),
                            Route("/turnstile/models/configure", configure_models, methods=["POST"]),
                            Route("/turnstile/machines/{name}/settings", machine_settings),
                            Route("/turnstile/machines/{name}/settings/{program}", machine_settings, methods=["POST"]),
                            Route("/turnstile/components", components_list),
                            Route("/turnstile/machines/{name}/engines", machine_engines),
                            Route("/turnstile/machines/{name}/model-api/{operation:path}", machine_model_api, methods=["GET", "POST", "DELETE"]),
                            Route("/turnstile/machines/{name}/cards", machine_cards, methods=["GET", "POST"]),
                            Route('/turnstile/machines/{name}/databases/{operation:path}', machine_databases, methods=['GET', 'POST']),
                            Route("/turnstile/machines/{name}/machine-key", rotate, methods=["GET", "POST"]),
                            Route("/turnstile/machines/{name}/model-lock", machine_model_lock, methods=["POST"]),
                            Route("/turnstile/machines/{name}/probe-environment", machine_probe_environment),
                            Route('/turnstile/machines/{name}/control/loadouts', machine_control, methods=['GET', 'POST']),
                            Route("/turnstile/machines/{name}/control/{action}", machine_control, methods=["GET", "POST"]),
                            Route("/turnstile/telemetry/{fingerprint}/history", telemetry_history),
                            *[Route(path, signed_in(caller, log, handler), methods=methods)
                              for path, handler, methods in (
                                  ("/v1/files", batch_work.file_upload, ["POST"]), ("/v1/files", batch_work.uploads, ["GET"]),
                                  ("/v1/files/{number}", batch_work.upload_one, ["GET", "DELETE"]), ("/v1/files/{number}/content", batch_work.upload_one, ["GET"]),
                                  ("/v1/batches", batch_start, ["POST"]), ("/v1/batches", batches_listed, ["GET"]),
                                  ("/v1/batches/{number}", batch_one, ["GET"]), ("/v1/batches/{number}/cancel", batch_one, ["POST"]),
                                  ("/loadcoach/batches", batches_listed, ["GET"]))],
                            *[Route(path, signed_in(caller, log, handler), methods=methods)
                              for path, handler, methods in (("/loadcoach/services", hosted.service_list, ["GET"]),
                                                             ("/loadcoach/services/{name}", hosted.service_put, ["POST"]),
                                                             ("/loadcoach/services/{name}", hosted.service_remove, ["DELETE"]),
                                                             ("/loadcoach/services/{name}/key", hosted.service_key, ["POST"]),
                                                             ("/loadcoach/services/{name}/models", hosted.service_offers, ["GET"]))],
                            Route("/turnstile/machine-key", rotate, methods=["GET", "POST"]),
                            Route("/turnstile/pick", pick_for),
                            Route("/turnstile/log", log_from_compute, methods=["POST"]),
                            Route("/turnstile/log-facts", log_facts),
                            *events.routes(log, users, caller, ask_compute, lambda: hosted.models(), request_model_choices),
                            *observations.routes(log, caller),
                            *accounts.routes(users, log, caller, remembered, routing.roles_for, hosted.offered, batch_work),
                            *allowances.routes(users, log, caller, ask_compute),
                            *routing.routes(),
                            Route("/{path:path}", pass_on, methods=methods)])
    app.state.plugins, app.state.users, app.state.log, app.state.routing = plugins, users, log, routing
    app.state.machines = machines
    app.add_middleware(PassiveReads, current=passive_reads)
    if plugins:
        plugins.controller.observation_log = log
    app.state.task_records = task_records
    app.state.mirrorwall = image_work
    app.state.soundroom = audio_work
    app.state.console_settings = console_settings
    app.state.fallthrough = fallthrough
    app.state.front_files = front_files
    app.state.publish_file_settings = publish_file_settings
    app.state.rotate_due_keys_once = rotate_due_keys_once
    app.add_middleware(ClientAccesses, users=users, log=log, internal_prefix=internal_token + ':')
    app.add_middleware(ClientOrigin, key_file=settings.auth_key_file)
    app.add_middleware(Barrier, gate=gate)
    observation.instrument(app, "turnstile", log)
    return app


def listening(host: str, port: int) -> socket.socket:
    """A socket bound to one address and port, for the server to listen on.

    It is made as a TCP socket by name. Python only switches off the short wait before sending a
    small piece on sockets made that way, and with the wait left on, every short answer through
    here took 40 thousandths of a second longer. Measured: 42 ms before, under 1 ms after.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # so that a restart need not wait for the port
    sock.bind((host, port))
    return sock


cli = typer.Typer(**CLI)


@cli.command()
def serve(config: Annotated[Path, typer.Option(metavar="FILE", help="The settings file.")] = Path("settings/weightroom.toml")) -> None:
    # Each paragraph is one line here, because Typer keeps the line breaks it is given.
    """turnstile: the way in to compute, on the front machine. It checks every caller's key and passes their requests on.

    It listens on the same ports as LoadCoach, the ToolLocker and spotter, and sends each request to that port on compute: the same path, the answer back as it arrives. In place of the caller's key it sends a statement of who they are, signed with the machine key. A harness is pointed here instead of at compute and needs no other change.

    The accounts are kept here, in the store it shares with weightroom. The `weightroom user` and `weightroom group` commands look after them.

    If compute is asleep, turnstile sends the packet that wakes it and holds the request until compute answers. Any valid key can wake it.

    It has no commands. It runs until stopped, normally as a service: systemctl --user status turnstile

    It reads the front machine's one settings file: compute, data, machine_key_file, wake_mac and wake_address, and its own part, \\[turnstile]. Each is explained in settings/examples/weightroom.toml.
    """
    settings = load_settings(config)
    if not Path(settings.machine_key_file).is_file():
        raise SystemExit(f"{settings.machine_key_file} is missing: turnstile signs what it sends on with it. See docs/installation.md.")
    say_what_runs(config, settings)
    # One server listening on several ports. Each request says which port it came in on.
    uvicorn.Server(uvicorn.Config(build_app(settings), access_log=False)).run(sockets=[listening(settings.host, port) for port in settings.ports])


def main() -> None:
    """Run Turnstile's configured model, tool and voice routing listeners."""
    cli()
