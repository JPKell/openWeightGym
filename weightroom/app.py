"""weightroom: the app on the front machine. People sign in here and use everything from a browser.

The users, their keys and the groups are kept on this machine, in the accounts store weightroom
shares with turnstile. Models live on compute; retained request history lives on front.
Compute-dependent pages ask through turnstile with the signed-in person's key. It is a session key: when someone
signs in, weightroom passes what they typed to turnstile, which checks it and hands back a key
that ends after some hours. The browser keeps it in a cookie. weightroom keeps no list of who is
signed in. The `weightroom user` and `weightroom group` commands look after the accounts.

What it does keep, in its `data` folder, is the last status it saw, for when compute is off. It
also hosts the benchmark worker and its separate results database, and web chat's history and
turns. Chats live in data/chat.db and are answered over HTTP with the caller's own key.

    pages/    the HTML, as templates
    static/   style sheets, icons and browser scripts
"""

from __future__ import annotations

from loadcoach import observability as observation

import asyncio
import json
from copy import deepcopy
import re
import math
import os
import secrets
import socket
import uuid
import sqlite3
import subprocess
import time
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import cache
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, parse_qs, quote, unquote, urlencode, urlsplit, urljoin
from zoneinfo import ZoneInfo

import httpx
import typer
from markdown_it import MarkdownIt
from markupsafe import Markup
import uvicorn
from weightroom.forms import refill, error_message
from weightroom.model_catalogue import catalogue as model_catalogue
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.requests import Request
from starlette.websockets import WebSocket
from loadcoach.client_origin import ClientOrigin, forward as forward_origin
from loadcoach.request_filters import FIELDS as REQUEST_FILTER_FIELDS, parse as parse_request_filters
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from db import DatabaseRegistry
from db.recovery import RecoveryStore
from db.maintenance import Barrier, Gate
from db.migration import (MigrationError, load_profile as migration_profile, profile_names as migration_profiles,
                          preview as migration_preview, launch as migration_launch, reports as migration_reports)
from loadcoach.config import CLI, read_settings, said_in
from loadcoach.hub import search_order as hub_search_order
from loadcoach.backups import (
    backup_root,
    create_backup,
    front_plan,
    inventory as backup_inventory,
    manifest as backup_manifest,
    manifests as backup_manifests,
    new_id as new_backup_id,
    store_inventory,
)
from loadcoach.backup_crypto import encryption_status, recipients as backup_recipients
from loadcoach.database_admin import catalogs as database_catalogs, configured_sources, dump_tools, make_sql_dump, safe_filename
from db.health import stores_health, async_stores_health
from loadcoach.database_operations import DatabaseOperations
from weightroom.database_console import DatabaseConsole, owner_of
from weightroom.conversations import build_app as build_conversations
from weightroom import pairing
from weightroom.plugin_pages import build_routes as plugin_routes
from weightroom.benchmark_views import latest_results, comparison_ranks, history_page, run_lanes, run_model_rows, measurement_matrix, model_kind as benchmark_model_kind
from benchmarks.identity import variant
from turnstile.model_configuration import (configuration as model_configuration, generation as generation_settings, comparable, shared as shared_values,
                                           translate, GLOBAL_FLAGS, PROFILE_GLOBALS)
from turnstile import library
from plugins._registry import Registry as PluginRegistry
from plugins._controller import Controller as PluginController
from loadcoach.groups import NAME
from loadcoach.roles import STRENGTHS, benchmark_shares, check_loaded_bonuses, check_weights, loaded_factors, pick
from loadcoach.waiting import completion
from loadcoach.settings import copy_of, laid_over, read_value, said, written
from benchmarks.app import Settings as BenchmarkSettings, build_app as build_benchmarks
from benchmarks.store import Benchmarks
from loadcoach.batches import ENDPOINTS, Batches
from loadcoach.outside import Services
from loadcoach.reliability import WINDOW, Reliability
from loadcoach.role_evidence import RoleEvidence
from loadcoach.roles import Roles
from weightroom import voice_settings, display
from weightroom.navigation import navigation
from weightroom.system_docs import BY_SLUG as DOC_SECTIONS, MOVED as MOVED_DOCS, api_reference, source_files, source_reference, visible as visible_docs, written as written_doc
from weightroom import doc_markdown
from weightroom.alerts import Alerts, machine_conditions
from weightroom.schedules import Schedules, ScheduleWorker
from weightroom import schedule_views
from loadcoach.schedule_time import validate as validate_schedule
from weightroom.pagination import controls, pagination
from engines import engine_for
from engines.llama_cpp.model import FIRST_PLACE
from weightroom import setting_labels
from weightroom.model_settings import PARALLEL_FLAGS, UNUSABLE_MODEL_FLAGS, parallel_fields, usable_model_flags
from loadcoach.cli import groups_cli, log_cli, users_cli
from loadcoach.groups import read_group
from loadcoach.log import COLUMNS, Log
from loadcoach.users import Users
from turnstile.app import NO_WAKE, front_stores, wake

HERE = Path(__file__).parent
COOKIE = "weightroom"
LEGACY_COOKIE = "frontdesk"
# What the front machine itself lets people do, in the words LoadCoach uses for its own. Someone who can do everything can do these.
FRONT_ABILITIES = {"front-wake": "wake compute from the first page while it is off", "front-voice": "use voice conversations"}
LOOK_EVERY = 15  # seconds between looks at compute
# The addresses turnstile answers itself, from the accounts. Their answers say nothing about whether compute is up.
# Answered by turnstile from what is kept on front, so an answer to them says nothing about compute: the accounts, and the log (milestone 91).
ACCOUNTS = ("/loadcoach/me", "/loadcoach/users", "/loadcoach/groups", "/loadcoach/sign-", "/turnstile/",
            "/loadcoach/observation",
            "/loadcoach/requests", "/loadcoach/events", "/loadcoach/event-choices", "/loadcoach/usage", "/loadcoach/account-events", "/loadcoach/allowances")
THIRD_PARTY_SERVICES = (("ollama", "Ollama"), ("docker", "Docker"), ("apache", "Apache"), ("nginx", "Nginx"))
THIRD_PARTY_NAMES = {name for name, _ in THIRD_PARTY_SERVICES}


@dataclass(frozen=True)
class Settings:
    """Everything weightroom is told. One TOML file, one key per field, except `key`."""

    compute: str  # LoadCoach's address, such as http://10.77.10.84:8442. Its host is compute's; everything is asked through turnstile
    key: str  # weightroom's own key, read from the file `key_file` names
    door: str = "http://127.0.0.1"  # turnstile on this machine, without a port: every request to compute goes through it
    data: str = "."  # the folder for what weightroom keeps
    host: str = "127.0.0.1"
    port: int = 8440
    secure: bool = False  # set true once it is served over HTTPS: the cookie then travels only that way
    spotter: str = ""  # spotter's address on compute, such as http://10.77.10.84:8444. Empty: primary service/power controls and logs unavailable. Asked through turnstile
    chat_model_url: str = ""  # model door for web chat; turnstile on this machine unless said
    toollocker: str = ""  # MCP service base URL; empty uses turnstile on this machine, port 8443
    toollocker_runs_on_front: bool = False  # derived from Turnstile routing for recovery ownership
    plugins_dir: str = ""  # root plugin folder; defaults beside this installation's data folder
    voice_gateway: str = "http://127.0.0.1:8446"  # private gateway administration; never its public/Twilio listener
    timezone: str = ""  # the zone times are shown in, such as America/Vancouver. Empty: this machine's own
    mock_input_rate: float = 2.0  # USD per million input tokens when no recorded or model rate is known
    mock_output_rate: float = 10.0  # USD per million output tokens when no recorded or model rate is known
    third_party_services: tuple[str, ...] = ("ollama", "docker")  # optional system services shown on Machine and in System Logs
    backup_retention: int = 10  # verified recovery sets kept on each machine; zero keeps all
    databases: dict = field(default_factory=dict)  # named local/server stores and service bindings
    alert_gpu_temperature: float = 85  # degrees Celsius; live threshold for operational alerts
    observation_error_percent: float = 0.0  # zero disables the measured-window error-rate alert
    observation_p95_seconds: float = 0.0  # zero disables the approximate histogram latency alert
    observation_min_samples: int = 100  # suppress rate/latency alerts below this sample count
    observation_window_seconds: int = 300  # fixed metric window for configured application alerts
    observation_alert_hold_seconds: int = 60  # sustained condition and positive recovery window
    observation_keep_days: int = 7  # days of spans kept on front
    observation_success_percent: int = 100  # share of all-successful traces whose spans are kept; others always are
    observation_max_spans: int = 0  # cap on stored spans; 0 is none
    observation_quiet_routes: tuple[str, ...] = observation.POLLS  # a successful trace that starts at one of these is not kept
    audio_lyrics_model: str = "chat"  # chat model or role that writes lyrics and style on the Music page
    audio_music_model: str = ""  # music model the Music page selects first; empty: the first available
    audio_sound_model: str = ""  # sound model the Sounds page selects first
    audio_speech_model: str = ""  # speech model the Speech page selects first
    audio_transcription_model: str = ""  # transcription model the Speech page selects first
    wake_mac: str = ""  # the hardware address of compute's network card. Empty: no Wake button
    wake_address: str = "255.255.255.255"  # where the waking packet is sent: everyone on the home network
    file: dict = field(default_factory=dict)  # what the settings file says, before anything changed on the site is laid over it
    says: dict = field(default_factory=dict)  # the comment above each setting in the file, shown beside it on the site
    path: str = ""  # the settings file itself, used by the recovery inventory


def load_settings(path: Path) -> Settings:
    """Read the TOML file. Anything wrong with it stops startup with a message naming the cause."""
    told = read_settings(path)
    told["plugins_dir"] = str((path.parent / told.get("plugins_dir", "../plugins")).resolve())
    told.pop("benchmarks", None)  # retired standalone-service URL; benchmarks now runs here
    told.pop("groups", None)  # the first groups: turnstile's, taken into the accounts
    told.pop("machines", None)  # turnstile's inventory of compute machines, in this same file
    told.pop("machines_poll_seconds", None)
    told.pop("voice_url", None)  # retired: the Voice page here is the browser voice chat now
    told.pop("deploy", None)  # retired SSH deployment page; node installation uses pairing
    # turnstile's part. weightroom reaches compute only through it, at the address it listens on.
    turnstile = told.pop("turnstile", {})
    host = turnstile.get("host", "127.0.0.1")
    told["door"] = "http://" + ("127.0.0.1" if host in ("0.0.0.0", "::", "") else host)
    told.setdefault("chat_model_url", told["door"] + ":8442")
    data = path.parent / told.get("data", ".")
    # What was changed on the site is kept on compute. A copy of it is here, so that it holds while compute is off.
    saved = copy_of(data / "settings.json")
    changed = saved.get("weightroom", saved.get("frontdesk", {}))
    for some in ([laid_over(told, changed, "weightroom")] if changed else []) + [told]:  # with the changes, and failing that as the file has it
        try:
            key = (path.parent / some["key_file"]).read_text().strip()
            if some.get("timezone"):
                ZoneInfo(some["timezone"])  # a zone that does not exist is found here, not when a page is asked for
            rest = {name: value for name, value in some.items() if name != "key_file"}
            loaded = Settings(**{**rest, "key": key, "data": str(data), "file": told, "says": said_in(path), "path": str(path),
                                 "toollocker_runs_on_front": bool(turnstile.get('toollocker_url'))})
            if any(type(rate) not in (int, float) or not 0 <= rate <= 1_000_000
                   for rate in (loaded.mock_input_rate, loaded.mock_output_rate)):
                raise ValueError("mock token rates must be numbers from 0 to 1,000,000 USD per million tokens")
            selected = loaded.third_party_services
            if (not isinstance(selected, (list, tuple)) or any(not isinstance(name, str) for name in selected)
                    or len(selected) != len(set(selected)) or set(selected) - THIRD_PARTY_NAMES):
                raise ValueError("third_party_services may contain ollama, docker, apache and nginx once each")
            if type(loaded.backup_retention) is not int or not 0 <= loaded.backup_retention <= 100:
                raise ValueError("backup_retention must be a whole number from 0 through 100")
            if type(loaded.alert_gpu_temperature) not in (int, float) or not 50 <= loaded.alert_gpu_temperature <= 110:
                raise ValueError("alert_gpu_temperature must be from 50 through 110 degrees Celsius")
            for name in observation.ALERT_LIMITS:
                observation.alert_value(name, getattr(loaded, name))
            from loadcoach.observation_store import check_policy
            check_policy(loaded.observation_keep_days, loaded.observation_success_percent, loaded.observation_max_spans,
                         loaded.observation_quiet_routes)
            return replace(loaded, third_party_services=tuple(selected))
        except (KeyError, OSError, TypeError, ValueError) as problem:
            why = problem
    raise SystemExit(f"{path}: {type(why).__name__}: {why}. It needs `compute` and `key_file`.") from None


class ComputeOff(Exception):
    """LoadCoach did not answer."""


class MachineOff(Exception):
    """spotter did not answer, or weightroom was not told where it is."""


@dataclass
class Compute:
    """What weightroom knows of compute right now, and the last it saw of it."""

    up: bool | None = None  # None until the first look
    since: float = field(default_factory=time.time)  # when `up` last changed
    status: dict | None = None  # the last /loadcoach/status that came back
    seen: float | None = None  # when that was


@dataclass
class Person:
    """Whoever sent this request, once it is known who that is."""

    name: str
    can: list[str]
    key: str | None = None  # their session key at LoadCoach; None if they signed in while compute was off
    me: dict = field(default_factory=dict)  # what LoadCoach says about them

    def __post_init__(self) -> None:
        # LoadCoach spells "superuser" out into its own abilities. The front machine's are spelled out here.
        """Expand superuser access to include front-only operator abilities."""
        if "superuser" in self.can:
            self.can = [*self.can, *FRONT_ABILITIES]


def worded(name: str, value: float) -> str:
    """One telemetry number as a person reads it. The unit is told by what the number is a reading of."""
    if "memory" in name or name.startswith("free on"):
        return size(int(value))
    if "temperature" in name:
        return f"{value:.0f}\u00b0"
    return f"{value:.0f} W" if "power" in name else f"{value:.0f}%"


def current_batch_jobs(now: dict, person: Person, worker, store) -> list[dict]:
    """Show executing batches and the one supervised benchmark, never the queue."""
    jobs = {job['id']: dict(job) for job in now.get('active_batches', [])}
    number = worker.current
    if number and worker.task and not worker.task.done() and not worker.task.cancelling():
        run = store.running_job(number)
        if run and (run['user'] == person.name or 'see-all' in person.can):
            jobs[number] = {**jobs.get(number, {}), 'id': number, 'model': run['model'], 'user': run['user'],
                'caller': 'benchmark', 'seconds': max(0, time.time() - (run['started'] or time.time())),
                'request_ids': [], 'href': '/benchmarks/run/' + number,
                'description': 'running batch · ' + run['benchmark']}
    return list(jobs.values())


def chart(name: str, values: list, top: float | None = None) -> dict:
    """Prepare the scaled samples, units and heat range used by Telemetry charts.

    A step with no reading stays None; the browser bridges missing samples without inventing a value.
    `heat` is the pair of values at which the line is fully green and fully red. Without one, the line is one colour.
    """
    if not any(value is not None for value in values):
        return {"name": name, "told": None}
    sizes = "memory" in name or name.startswith("free on")
    per = 2**30 if sizes else 1  # a size is drawn in GB, so that the marks up the side fall on whole ones
    top = top and round(top / per, 1)
    if "temperature" in name:
        heat = [45, 90 if name.startswith("processor") else 85]
    elif name.startswith("free on"):
        heat = [top, 0] if top else None  # room on a drive is the other way up: red when little is left
    else:
        heat = [0, top] if top else None
    unit = " GB" if sizes else "\u00b0" if "temperature" in name else " W" if "power" in name else "%"
    told = {"values": [None if value is None else round(value / per, 2) for value in values], "unit": unit, "top": top, "heat": heat}
    return {"name": name, "told": told}


def size(count: int | None) -> str:
    """A size in bytes as a person reads it: in MB, and in GB from 1000 MB up. None means the file is not there."""
    return "missing" if count is None else f"{count / 2**30:.1f} GB" if count >= 1000 * 2**20 else f"{count / 2**20:.0f} MB"


def memory_size(count: int | None) -> str:
    """Observed memory bytes, with explicit binary units matching MiB telemetry."""
    return "unavailable" if count is None else f"{count / 2**30:.1f} GiB" if count >= 2**30 else f"{count / 2**20:.0f} MiB"


def status_nodes(inventory: dict, poll_seconds: float, now: dict | None = None) -> list[dict]:
    """Combine the fresh caller-scoped status with inventory and shared-host Spotter RAM."""
    hosts = {host["fingerprint"]: host for host in inventory.get("hosts", []) if host.get("fingerprint")}
    current: dict[str, dict] = {}
    if now:
        for key, room in (now.get("memory") or {}).items():
            # PCI addresses contain colons too; only a prefix before the vendor is a node name.
            prefix = key.split(":", 1)[0]
            machine, card = key.split(":", 1) if ":" in key and "@" not in prefix else ("compute", key)
            # Keep an empty service entry so excluded devices do not return from cached inventory.
            current.setdefault(machine, {"cards": [], "loaded": []})
            if "model_cards" in now and key not in now["model_cards"]:
                continue
            current[machine]["cards"].append(
                {"card": card, "total": (room or {}).get("total"), "free": (room or {}).get("free"),
                 "others": (room or {}).get("others")})
        for model in now.get("loaded", []):
            machine = model.get("machine", "compute")
            current.setdefault(machine, {"cards": [], "loaded": []})["loaded"].append(
                {**model, "cards": model.get("gpus", [])})
    machines = inventory.get("machines", [])
    if not machines and now:
        machines = [{"name": name, "hostname": now.get("hostname") if name == "compute" else name,
                     "fingerprint": now.get("machine_fingerprint") if name == "compute" else None,
                     "state": "unknown", "status_live": False, "telemetry": None, "telemetry_age": None}
                    for name in (current or {"compute": {}})]
    shown = []
    for node in machines:
        fresh = current.get(node["name"])
        if fresh or (now and len(machines) == 1 and node["name"] == "compute"):
            fresh = fresh or {"cards": [], "loaded": []}
            node = {**node, **fresh, "state": "ready", "status_live": True}
        # Older workers can still report PCI-only integrated graphics as model capacity.
        # Apply this display filter to both live and cached inventory, independently of policy metadata.
        node = {**node, "cards": [card for card in node.get("cards", [])
                                  if not (card["card"].startswith(("AMD@", "Intel@")) and not card.get("total"))]}
        host = hosts.get(node.get("fingerprint"))
        host_fresh = host and host.get("telemetry") and host.get("age") is not None and host["age"] <= poll_seconds * 3
        own_stale = node.get("telemetry_age") is None or node["telemetry_age"] > poll_seconds * 3
        shown.append({**node, "telemetry": host["telemetry"], "telemetry_age": host["age"]} if host_fresh and own_stale else node)
    return shown


def disk_size(count: int | None) -> str:
    """A storage allocation with useful precision for small tables as well as large files."""
    if count is None:
        return "unavailable"
    if count < 1024:
        return f"{count} B"
    if count < 1024**2:
        return f"{count / 1024:.1f} KiB"
    if count < 1024**3:
        return f"{count / 1024**2:.1f} MiB"
    return f"{count / 1024**3:.1f} GiB"


def shown(value, indent: int | None = None) -> str:
    """A value as it is typed into a box or read on a page: text as it is, anything else as JSON, such as true or [1, 1]."""
    return value if isinstance(value, str) else json.dumps(value, indent=indent, ensure_ascii=False)


def spoken(content) -> str:
    """What a message says, as text. A message in parts gives the text of each, and a note for a part that is not text, such as a picture."""
    if isinstance(content, list):
        return "\n".join(part.get("text") or f"[{part.get('type', 'a part')}, not shown]" if isinstance(part, dict) else str(part) for part in content)
    return content if isinstance(content, str) else "" if content is None else json.dumps(content)


# What a model is told when it is asked about a log.
ASKING = "You are helping the owner of a machine that runs language models to read a log from it. Say what went wrong, and what to try. Be brief and plain. If the log does not show the cause, say so."
# How many characters of a log are sent unless the person asking says otherwise: about ten thousand tokens.
# ponytail: a model with a smaller context refuses that, in its own words, and the person types a smaller number.
# Fill the box from the chosen model's own context if that gets in the way.
ASK_AT_MOST = 40_000


def tail(text: str, most: int) -> str:
    """The end of a log, `most` characters at most, beginning where a line begins. If the last line alone is longer, the end of that."""
    if len(text) <= most:
        return text
    end = text[-most:]
    whole = end if text[-most - 1] == "\n" else end.partition("\n")[2]  # a first line that was cut in two is left out
    return whole or end

# What every log entry has. Whatever else an entry holds is particular to its kind, and a page shows it as it is.
EVERY_ENTRY = {"id", "at", "kind", "what", "user", "caller", "model", "trace", "level", "of", "text"}


# What turns a model's answer into HTML. An answer is written in Markdown. Raw HTML in it is not passed on but shown as
# the characters it is, so that nothing a model writes is run by the browser. A link to anything but a web address is left as text.
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")


def names_in(text: str) -> list[str]:
    """Names typed one to a line, or with commas between them, without the empty ones."""
    return [name.strip() for line in text.splitlines() for name in line.split(",") if name.strip()]


def read(text: str):
    """What was typed into a box, as a value: a number, true, false or a list if it is one, else the text itself."""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return text if value is None or isinstance(value, dict) else value


SHOWN_SETTINGS = ("engine", "flags", "strata", "gpus", "alone", "allow_split", "adapters", "min_context", "order")
# Fields of a model's form that every node's copy shares. They appear once, unprefixed.
SHARED_FIELDS = ("engine", "projector", "adapter", "adapter_file", "adapter_scale", "order")


def node_form(form: dict[str, list[str]], machine: str) -> dict[str, list[str]]:
    """One other node's fields of the node matrix, as the single-node form would send them.

    Its own fields are named n:<node>:<field>. The shared ones, and the generation flags among the
    unprefixed flag rows, are added to them; a generation flag comes last so the shared value wins."""
    prefix = f"n:{machine}:"
    own = {key.removeprefix(prefix): values for key, values in form.items() if key.startswith(prefix)}
    pairs = [(flag, value) for flag, value in zip(own.get("flag", []), own.get("value", []))]
    pairs += [(flag, value) for flag, value in zip(form.get("flag", []), form.get("value", [])) if flag.strip().lstrip("-") in GLOBAL_FLAGS]
    suppressed = own.get("suppress_flag", []) + [flag for flag in form.get("suppress_flag", []) if flag in GLOBAL_FLAGS or flag == "mmproj"]
    return own | {key: form[key] for key in SHARED_FIELDS if key in form} | {"flag": [flag for flag, _ in pairs], "value": [value for _, value in pairs], "suppress_flag": suppressed}


def matrix_nodes(form: dict[str, list[str]]) -> list[str]:
    """The other nodes whose columns the node matrix sent, in the order they appear."""
    return list(dict.fromkeys(key[2:].rpartition(":")[0] for key in form if key.startswith("n:") and key[2:].rpartition(":")[0]))


def suggested(settings: dict, defaults: dict, cards: list[str]) -> list[dict]:
    """Build suggested form values from current model and engine metadata."""
    return engine_for(settings).suggestions(settings, defaults, cards)


def fit_line(model: dict, cards: list[str]) -> dict:
    """What the dry fit page needs of a model: the cards it may go on, how many of them it takes, and the points its memory is drawn through.

    A point is a context with the MiB that was measured on each card at it. Between two points
    the page draws a straight line, which is how memory grows with context. A model measured
    at one context only has a second point guessed for it: with no context at all it would
    take about what its file holds, divided over its cards as the measure was. On this machine
    that guess came out 40 to 280 MiB low, which is what llama-server needs to compute. The
    page marks such a model.
    """
    wanted = model.get("placement_gpus", 1) if model.get("gpus") == "any" else model.get("gpus", len(cards))
    need = len(wanted) if isinstance(wanted, list) else wanted
    points = [[point["context"], point["mib"]] for point in model.get("measurements", []) if point["mib"] and len(point["mib"]) == need]
    fixed = model.get('engine') in ('sdcpp', 'audiocpp')
    if fixed:
        profile = model.get('image_profile') or {}
        if model.get('engine') == 'sdcpp' and not profile.get('qualified'):
            points = []
        if points:
            points = [[0, [max(point[1][index] for point in points) for index in range(need)]]]
        return {'cards': wanted if isinstance(wanted, list) else cards, 'need': need,
                'points': points, 'guessed': False, 'fixed': True}
    guessed = len(points) == 1 and bool(model.get("size"))
    if guessed:
        # A file larger than what was measured is partly on the processor. Then a smaller context is taken to free nothing.
        share = min(1, model["size"] / 2**20 / sum(points[0][1]))
        points.insert(0, [0, [round(one * share) for one in points[0][1]]])
    return {"cards": wanted if isinstance(wanted, list) else cards, "need": need, "points": points, "guessed": guessed}


def fit_cpu_mib(model: dict) -> tuple[int, bool]:
    """Measured CPU resident peak, or the same conservative first-load bound admission uses."""
    measured = [point.get("rss_mib") for point in model.get("measurements", []) if point.get("rss_mib")]
    if measured:
        return max(measured), False
    size = model.get("size")
    return (int((size or 0) / 2**20) + 256 if size is not None else 0), True


def fit_cpu_limit(memory: dict) -> int:
    """Process-RSS planning limit; cgroup charge is checked separately at load."""
    return min(value for value in (
        max(0, (memory.get("total") or 0) - (memory.get("headroom") or 0)),
        memory.get("configured_limit")) if value is not None)


def fit_node(known: dict, machine: str) -> dict:
    """Turn a logical catalogue into one node's local dry-fit catalogue."""
    nodes = known.get("nodes") or {"compute": {key: known.get(key) for key in ("defaults", "cards", "build", "to_measure", "measuring")}}
    node = nodes[machine]
    models = []
    for logical in known.get("models", []):
        locations = logical.get("locations") or [{**logical, "machine": "compute"}]
        location = next((one for one in locations if one.get("machine") == machine), None)
        if location is not None:
            models.append({**location, "name": logical["name"], "local_name": location.get("name", logical["name"])})
    return {**node, "models": models, "nodes": nodes}


def node_measuring(known: dict, node: str = "") -> dict[str, dict]:
    """Each compute node's models still to be measured, by their names there, and its measuring run.

    `known` is the merged catalogue, which carries every node under `nodes`, or one node's own
    catalogue when `node` names it. With `node`, only that node is kept."""
    nodes = known.get("nodes") or {node or "compute": known}
    return {name: {"to_measure": list(one.get("to_measure") or []), "measuring": one.get("measuring")}
            for name, one in nodes.items() if not node or name == node}


def measure_plan(nodes: dict[str, dict], copies: set[tuple[str, str]] | None = None) -> dict[str, list[str]]:
    """The nodes that have something to measure, each with its models' names there. `copies` keeps only those (node, name) pairs."""
    plan = {name: [model for model in one["to_measure"] if copies is None or (name, model) in copies] for name, one in nodes.items()}
    return {name: models for name, models in plan.items() if models}


def model_copies(model: dict, node: str = "") -> set[tuple[str, str]]:
    """Every (node, name there) of one model in the catalogue; a catalogue of one node has no locations and is that node's."""
    return {(place.get("machine", "compute"), place.get("name", model["name"]))
            for place in model.get("locations") or [{"machine": node or "compute", "name": model["name"]}]}


def measure_button(models: list[dict], nodes: dict[str, dict], node: str = "") -> dict:
    """What the Measure memory button says: the models with a copy still to measure, and the runs under way."""
    wanted = {(name, model) for name, models_there in measure_plan(nodes).items() for model in models_there}
    runs = [{"node": name, **one["measuring"]} for name, one in nodes.items() if one.get("measuring")]
    return {"models": [model["name"] for model in models if model_copies(model, node) & wanted],
            "running": [run for run in runs if not run.get("ended")], "runs": runs}


# ---- abilities, for the Users and Groups pages -------------------------------------------------
#
# `known` is what LoadCoach says at /loadcoach/groups: every ability with its words, and under
# "own_key" the few that are meant for a program's own key.


def covered(known: dict) -> set[str]:
    """Every ability that "superuser" stands for: LoadCoach's and the front machine's, but not those meant for a program's own key."""
    return (set(known["abilities"]) | set(FRONT_ABILITIES)) - set(known["own_key"]) - {"superuser"}


def held(can, known: dict) -> set[str]:
    """A list of abilities as it works out: with "superuser" spelled out."""
    return set(can) | (covered(known) if "superuser" in can else set())


def tick_list(person: Person, known: dict, can, group_can=()) -> list[dict]:
    """One row for every ability there is, for a list of tick boxes.

    A row is `ticked` if `can` holds its ability, marked `group` if the account's group gives
    it, and `greyed` if the one looking may not do it themselves, and so may not hand it out.
    """
    every = {**known["abilities"], **FRONT_ABILITIES}
    every |= {name: "" for name in (*can, *group_can) if name not in every}  # one this page has no words for is still shown, so that saving does not lose it
    have, given, mine = held(can, known), held(group_can, known), set(person.can)
    return [
        {"name": name, "words": words, "ticked": name in have, "group": name in given, "greyed": "superuser" not in mine and name not in mine, "own_key": name in known["own_key"]}
        for name, words in every.items()
    ]


def of_its_own(ticked, group_can, known: dict) -> dict:
    """What an account must be given, and what must be taken from it, to hold exactly the abilities ticked."""
    want, has = held(ticked, known), held(group_can, known)
    add, remove = want - has, has - want
    if "superuser" in add:
        add -= covered(known)  # the one word says them all
    if "superuser" in remove:
        # Its group can do everything and it is to do less. The front machine read its own
        # abilities into that word, and they go with it: the ones still wanted must now be
        # given by name, and the others need no taking away.
        implied = set(FRONT_ABILITIES) - set(group_can)
        add, remove = add | (want & implied), remove - implied
    return {"add": sorted(add), "remove": sorted(remove)}


def build_app(settings: Settings, client: httpx.AsyncClient | None = None, watcher: httpx.AsyncClient | None = None,
              tools_client: httpx.AsyncClient | None = None, voice_client: httpx.AsyncClient | None = None) -> Starlette:
    """Build the web app. `client` is how it reaches LoadCoach and `watcher` how it reaches spotter, both through turnstile; tests hand in their own."""
    loadcoach = client or httpx.AsyncClient(base_url=settings.door + ":8442", timeout=httpx.Timeout(10, connect=2))
    tools_url = settings.toollocker or settings.door + ":8443"
    tool_http = tools_client or httpx.AsyncClient(base_url=tools_url, timeout=httpx.Timeout(130, connect=2))
    voice_http = voice_client or httpx.AsyncClient(base_url=settings.voice_gateway, timeout=httpx.Timeout(130, connect=2))
    spotter = watcher or (httpx.AsyncClient(base_url=settings.door + ":8444", timeout=httpx.Timeout(15, connect=2)) if settings.spotter else None)
    zone = ZoneInfo(settings.timezone) if settings.timezone else None

    def clock(at) -> str:
        """Plain UTC text for attributes and native option labels; visible times use timestamp."""
        moment = display.moment(at)
        return moment.strftime('%d %b %Y, %H:%M UTC') if moment else 'never'

    pages = Jinja2Templates(directory=[HERE / "pages", HERE.parent / "mirrorwall" / "pages", HERE.parent / "soundroom" / "pages"])  # values put into a page are escaped, always
    pages.env.globals['model_catalogue'] = model_catalogue
    pages.env.globals.update(setting_label=setting_labels.describe, settings_sections=setting_labels.sections,
                             server_timezone=settings.timezone or display.server_zone(),
                             log_timezone=settings.timezone or "UTC", input_utc=display.input_utc)
    pages.env.filters["clock"] = clock
    pages.env.filters.update(display=display.value, count=display.count, timestamp=display.timestamp, log_line=display.log_line)
    pages.env.filters["worded"] = lambda value, name: "\u2014" if value is None else worded(name, value)  # a dash for a reading that was not given
    pages.env.filters["size"] = size
    pages.env.filters["disk_size"] = disk_size
    pages.env.filters["memory_size"] = memory_size
    pages.env.filters["memory_mib"] = lambda count: memory_size(count * 2**20 if isinstance(count, (int, float)) else None)
    pages.env.filters["mib"] = lambda count: size(count * 2**20)  # the same, for a figure that is in MiB already, as a card's memory is
    pages.env.filters["shown"] = shown
    pages.env.filters["spoken"] = spoken
    pages.env.filters["written"] = written  # a setting's value as its file would hold it
    pages.env.filters["query"] = lambda value: quote(str(value), safe="")
    pages.env.filters["markdown"] = lambda text: Markup(MARKDOWN.render(text))  # safe to put into a page as it is: see MARKDOWN
    pages.env.globals["ASK_AT_MOST"] = ASK_AT_MOST
    pages.env.filters["rest"] = lambda entry: {key: value for key, value in entry.items() if key not in EVERY_ENTRY and value not in (None, "")}
    gate = Gate(settings.path)
    for private_client in (loadcoach, tool_http, spotter):
        if private_client is not None:
            private_client.event_hooks.setdefault("request", []).append(gate.outgoing)
    data = Path(settings.data)
    data.mkdir(parents=True, exist_ok=True)
    database_registry = DatabaseRegistry.from_config(
        settings.databases,
        Path(settings.path).parent if settings.path else Path.cwd(),
        {
            "weightroom.accounts": data / "accounts.db",  # turnstile's, and the weightroom command's; here for backups and the database page
            "weightroom.roles": data / "roles.db",  # turnstile's: roles, their evidence and reliability; here for backups and the database page
            "weightroom.chat": data / "chat.db",
            "weightroom.benchmarks": data / "benchmarks.db",
            "weightroom.plugins": data / "plugins.db",
            "weightroom.alerts": data / "alerts.db",
            "weightroom.schedules": data / "schedules.db",
        },
    )
    plugins = PluginRegistry(
        Path(settings.plugins_dir) if settings.plugins_dir else data.parent / "plugins",
        data,
        database_registry.database("weightroom.plugins"),
    )
    plugin_http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=2))
    plugin_controller = PluginController(plugins, plugin_http)
    # Internal ASGI calls use no socket or port. Only the worker's LoadCoach client uses HTTP.
    benchmarks = build_benchmarks(
        BenchmarkSettings(compute=settings.door + ":8442", database=database_registry.database("weightroom.benchmarks")), client  # through turnstile, like every page
    )
    benchmark_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=benchmarks), base_url='http://benchmarks')
    chat_inference = (httpx.AsyncClient(base_url=settings.chat_model_url, timeout=httpx.Timeout(None, connect=2))
                      if settings.chat_model_url and client is None else loadcoach)

    async def carry_origin(request: httpx.Request) -> None:
        """Authenticate this browser's IP across local front HTTP hops without sharing context between users."""
        await forward_origin(request, settings.key)

    origin_clients = (loadcoach, tool_http, spotter, chat_inference)
    for origin_client in {id(item): item for item in origin_clients if item is not None}.values():
        origin_client.event_hooks.setdefault('request', []).append(carry_origin)
    conversations = build_conversations(
        database_registry.database("weightroom.chat"), loadcoach, chat_inference, plugins=plugins
    )
    benchmarks.state.worker.maintenance = gate
    conversations.state.knowledge.maintenance = gate
    alerts = Alerts(database_registry.database("weightroom.alerts"))
    schedules = Schedules(database_registry.database('weightroom.schedules'))
    alert_lock = asyncio.Lock()
    observation_alert_previous = {}
    chat_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=conversations), base_url="http://chat")
    compute = Compute()
    slow: set[asyncio.Task] = set()  # things asked of LoadCoach that nobody is waiting for, such as an unload

    def kept(name: str, otherwise):
        """Something weightroom wrote to its data folder earlier, or `otherwise` if it never did."""
        try:
            return json.loads((data / name).read_text())
        except (OSError, ValueError):
            return otherwise

    def keep(name: str, thing) -> None:
        """Write something to the data folder, readable by weightroom's own account only."""
        with open(os.open(data / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            json.dump(thing, file)

    last = kept("last.json", {})
    compute.status, compute.seen = last.get("status"), last.get("seen")

    def noted(up: bool) -> None:
        """Retain the latest observed compute availability and status."""
        if compute.up is not up:
            compute.up, compute.since = up, time.time()

    async def ask(method: str, path: str, key: str, **more) -> httpx.Response:
        """Call local benchmark handlers, or compute through turnstile, with the caller's key; track compute outages.

        A page never waits for compute to wake: turnstile says at once that it is not answering.
        The accounts are turnstile's own, so asking about them says nothing about compute.
        """
        headers = {"authorization": f"Bearer {key}", NO_WAKE: "1", **more.pop("headers", {})}
        try:
            target = (chat_client if path.startswith(("/loadcoach/conversations", "/loadcoach/collections", "/loadcoach/workflows")) else
                      benchmark_client if path.startswith('/benchmarks') else loadcoach)
            answer = await target.request(method, path, headers=headers, **more)
            if method == "DELETE" and path.startswith("/loadcoach/users/") and answer.is_success:
                if identity := answer.json().get("identity"):
                    await conversations.state.forget(identity)
        except httpx.HTTPError:
            if path.startswith('/benchmarks'):
                return httpx.Response(503,json={'error':{'message':'Benchmark processing is unavailable.'}})
            noted(False)
            raise ComputeOff from None
        if answer.headers.get("x-turnstile") == "compute-off":
            noted(False)
            raise ComputeOff
        if answer.headers.get('x-suite-data-owner') != 'front' and not path.startswith(('/benchmarks', '/turnstile/', '/loadcoach/conversations', '/loadcoach/collections', '/loadcoach/workflows', *ACCOUNTS)):
            noted(True)
        return answer

    async def ask_spotter(method: str, path: str, key: str | None, **more) -> httpx.Response:
        """One request to spotter, through turnstile, signed with `key`. Raises MachineOff if it does not answer."""
        if spotter is None or key is None:
            raise MachineOff
        try:
            answer = await spotter.request(method, path, headers={"authorization": f"Bearer {key}", NO_WAKE: "1"}, **more)
        except httpx.HTTPError:
            raise MachineOff from None
        if answer.headers.get("x-turnstile") == "compute-off":
            raise MachineOff
        return answer

    said_last: dict = {}  # what LoadCoach was last told that each program here runs with

    async def in_step() -> None:
        """Fetch what was changed in the settings of the front machine's programs, keep a copy for their next start, and say what they run with.

        weightroom does this for itself and for turnstile, which has no key of its own. Each takes a
        change on when it next starts. See loadcoach/settings.py.
        """
        nonlocal settings
        copied = kept("settings.json", {})
        for program in ("weightroom", "turnstile"):
            changed = await ask("GET", f"/loadcoach/settings/{program}/changes", settings.key)
            if changed.status_code != 200:  # this key may not, or LoadCoach is older than this
                return
            copied[program] = changed.json()
        if copied != kept("settings.json", {}):
            keep("settings.json", copied)
        effective = laid_over(settings.file, copied.get("weightroom", {}), "weightroom")
        selected = effective.get("third_party_services", ("ollama", "docker"))
        if (isinstance(selected, list) and len(selected) == len(set(selected)) and not set(selected) - THIRD_PARTY_NAMES
                and tuple(selected) != settings.third_party_services):
            settings = replace(settings, third_party_services=tuple(selected))
        retention = effective.get("backup_retention", settings.backup_retention)
        if type(retention) is int and 0 <= retention <= 100 and retention != settings.backup_retention:
            settings = replace(settings, backup_retention=retention)
        heat = effective.get("alert_gpu_temperature", settings.alert_gpu_temperature)
        if type(heat) in (int, float) and 50 <= heat <= 110:
            settings = replace(settings, alert_gpu_temperature=heat)
        for name in observation.ALERT_LIMITS:
            value = effective.get(name, getattr(settings, name))
            try:
                settings = replace(settings, **{name:observation.alert_value(name, value)})
            except ValueError:
                pass  # Invalid persisted settings never become effective runtime thresholds.
        named = ("host", "port", "compute", "spotter", "timezone", "mock_input_rate", "mock_output_rate",
                 "third_party_services", "backup_retention", "alert_gpu_temperature", "wake_mac", "wake_address", "secure", "chat_model_url", "plugins_dir")
        running = {**settings.file, **{name: getattr(settings, name) for name in (*named, *observation.ALERT_LIMITS)}}
        # turnstile writes what it runs with into the folder the two share, each time it starts.
        for program, told in (("weightroom", said(running, settings.file, settings.says)), ("turnstile", kept("settings-seen-turnstile.json", None))):
            if told and said_last.get(program) != told:
                if (await ask("POST", f"/loadcoach/settings/{program}/seen", settings.key, json={"told": told})).status_code == 200:
                    said_last[program] = told

    async def watch() -> None:
        """Look at compute every so often, for as long as weightroom runs.

        Each look keeps the status, so there is something to show when compute stops answering.
        """
        while True:
            await gate.wait()
            try:
                status = await ask("GET", "/loadcoach/status", settings.key)
                if status.status_code == 200:
                    compute.status, compute.seen = status.json(), time.time()
                    keep("last.json", {"status": compute.status, "seen": compute.seen})
                await plugins.auth.refresh(loadcoach, "/loadcoach/plugins/mirror", key=settings.key)
                await in_step()
            except ComputeOff:
                pass
            await asyncio.sleep(LOOK_EVERY)

    async def collect_alerts(key):
        """Observe existing APIs without inference, service actions or waking compute."""
        nonlocal observation_alert_previous
        async with alert_lock:
            if gate.token():
                return
            gate.active += 1
            try:
                async def evidence(client, path, params=None):
                    """Read one monitoring source and record its freshness with the alert observation."""
                    if client is None or key is None:
                        return None
                    try:
                        answer = await client.get(path, headers={'authorization': f'Bearer {key}', NO_WAKE: '1'}, params=params, timeout=10)
                        return answer.json() if answer.status_code == 200 else None
                    except (httpx.HTTPError, ValueError):
                        return None
                coach, machine, operation_health = await asyncio.gather(
                    evidence(loadcoach, '/loadcoach/alert-evidence'),
                    evidence(spotter, '/spotter/alert-evidence', {'system_services': ','.join(settings.third_party_services)}),
                    evidence(loadcoach, '/loadcoach/observation-metrics', {'seconds':settings.observation_window_seconds}))
                if gate.token():
                    return  # discard reads taken across a maintenance boundary
                now = time.time()
                from weightroom.alerts import observation_conditions
                observation_readings, observation_alert_previous = observation_conditions(operation_health, observation_alert_previous, now,
                    settings.observation_error_percent, settings.observation_p95_seconds,
                    settings.observation_min_samples, settings.observation_alert_hold_seconds)
                alerts.observe('observability', observation_readings, now, missing_recovers=False)
                for source in ('model failures', 'spending'):
                    alerts.observe(source, coach.get(source) if coach else None, now)
                if machine:
                    services, heat = machine_conditions(machine, settings.alert_gpu_temperature)
                    alerts.observe('services', services, now, missing_recovers=False)
                    alerts.observe('GPU temperature', heat, now, missing_recovers=False)
                    # Unit results remain usable if kernel access fails; absence does not
                    # close any event, so this partial snapshot is safe to retain.
                    alerts.observe('memory kills', machine['memory kills'], now)
                    alerts.observe('kernel journal', [] if machine['kernel_available'] else None, now)
                else:
                    for source in ('services', 'GPU temperature', 'memory kills', 'kernel journal'):
                        alerts.observe(source, None, now)
            finally:
                gate.active -= 1

    async def watch_alerts():
        """Periodically reconcile operational alerts within the existing WeightRoom process."""
        while True:
            await gate.wait()
            try:
                await collect_alerts(settings.key)
            except Exception:
                # An optional monitoring failure must not kill the console or its
                # settings follower. Never print driver exceptions or credentials.
                print('weightroom: operational alert collection failed; retrying.', file=sys.stderr)
            await asyncio.sleep(30)

    # ---- who is asking ----------------------------------------------------------------------

    async def who(request: Request) -> Person | None:
        """Whoever this request's cookie belongs to, or None. Raises ComputeOff if turnstile does not answer.

        Turnstile answers /loadcoach/me from the accounts, which are on this machine, so this holds
        while compute is off. Only what compute adds to it, such as the models, is missing then.
        """
        cookie = request.cookies.get(COOKIE, request.cookies.get(LEGACY_COOKIE, ""))
        if not cookie:
            return None
        me = await ask("GET", "/loadcoach/me", cookie)
        if me.status_code != 200:
            return None
        info = plugins.enrich(me.json())
        if "unavailable_machines" in info:
            noted("compute" not in info["unavailable_machines"])
        from types import SimpleNamespace
        observation.identify(SimpleNamespace(identity=info.get('identity'), name=info['name'], key_identity=None,
                                             can=lambda ability: ability in info['can']))
        person = Person(info["name"], info["can"], cookie, info)
        request.state.person = person
        return person

    def page(request: Request, template: str, person: Person | None = None, status: int = 200, **more) -> Response:
        """One of the pages in pages/, filled in. Every page is told who is signed in and how compute is."""
        # The strip of meters is shown to someone signed in at compute, unless they chose not to see it.
        strip = bool(person and person.key and person.me.get("choices", {}).get("show_telemetry", True))
        nav = navigation(request.url.path, person.can, bool(spotter)) if person else None
        error_request_id = ''
        if template == 'refused.html' and status >= 500:
            context = getattr(request.state, 'observation', None)
            error_request_id = context.root.request_id if context else uuid.uuid4().hex
        response = pages.TemplateResponse(request, template, {"person": person, "compute": compute, "strip": strip, "spotter_available": bool(spotter), "wake_button": bool(settings.wake_mac), "nav": nav, "error_status": status, "error_request_id": error_request_id, **more}, status_code=status)
        if error_request_id:
            response.headers['x-request-id'] = error_request_id
        response.headers['cache-control'] = 'no-store'
        response.headers['x-content-type-options'] = 'nosniff'
        if status >= 400:
            values = (getattr(request.state, 'form_values', None) if request.method == 'POST' else
                      {key: request.query_params.getlist(key) for key in request.query_params} if request.method == 'GET' else None)
            if values is not None:
                restored, found = refill(response.body.decode('utf-8'), str(request.url), request.url.path, request.method, values)
                if found:
                    response.body = restored.encode('utf-8')
                    response.headers['content-length'] = str(len(response.body))
                    response.headers['cache-control'] = 'no-store'
                    response.form_retained = True
        return response

    def fragment_url(request: Request, part: str) -> str:
        """Preserve bookmarkable filters while selecting one independently loaded section."""
        return request.url.path + '?' + urlencode([(key, value) for key, value in request.query_params.multi_items()
                                                   if key not in ('part', 'complete')] + [('part', part)])

    def initial_page(request: Request) -> bool:
        """Plain navigations render immediately; fragments and explicit full-data views do work separately."""
        return (request.method == 'GET' and not request.query_params.get('part')
                and request.query_params.get('complete') != '1' and request.headers.get('hx-request') != 'true')

    def fragment_error(request: Request, person: Person, region: str, part: str, answer: Response | httpx.Response) -> Response:
        """Keep a delayed-data refusal and its retry inside the original section."""
        why = 'This section could not be loaded. Check the filters and retry.'
        if isinstance(answer, httpx.Response):
            try:
                why = answer.json().get('error', {}).get('message') or why
            except (ValueError, AttributeError):
                pass
        return page(request, 'deferred_error.html', person, status=answer.status_code,
                    region=region, why=why, retry_url=fragment_url(request, part))

    async def http_error(request: Request, problem: HTTPException) -> Response:
        """Keep missing pages and HTTP refusals inside the console, without changing their status."""
        person = getattr(request.state, 'person', None)
        if person is None:
            try:
                person = await who(request)
            except ComputeOff:
                pass  # The error page must remain usable when sign-in cannot be checked.
        why = 'There is no page at this address.' if problem.status_code == 404 else str(problem.detail)
        answer = page(request, 'refused.html', person, status=problem.status_code, why=why)
        answer.headers.update(problem.headers or {})
        return answer

    async def server_error(request: Request, problem: Exception) -> Response:
        """Show a safe error and its existing correlation ID; never display exception details."""
        observation.annotate(error_code=type(problem).__name__)
        if request.url.path.startswith('/loadcoach/'):
            return JSONResponse({'error': {'message': 'Internal server error.'}}, status_code=500)
        return page(request, 'refused.html', getattr(request.state, 'person', None), status=500,
                    why='This page could not be loaded. Try again, or contact your administrator.')

    def go(where: str) -> RedirectResponse:
        """Return a redirect that follows form submissions with a GET request."""
        return RedirectResponse(where, status_code=303)  # 303: come back with a GET, whatever was just sent

    def browser_redirect(request: Request, where: str) -> Response:
        """An authentication redirect must replace the page, never a polling fragment."""
        if request.headers.get('hx-request') == 'true':
            return Response(status_code=204, headers={'HX-Redirect': where, 'Cache-Control': 'no-store'})
        return go(where)

    async def typed_lists(request: Request) -> dict[str, list[str]]:
        """The fields of a form that was sent. Each name gives a list, since a form may use a name many times."""
        maximum = (24 * 1024 * 1024 if request.url.path.startswith('/chat') else
                   6 * 1024 * 1024 if request.url.path.startswith('/benchmarks') else 256 * 1024)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                raise HTTPException(413, "The submitted form is too large.")
            body.extend(chunk)
        try:
            values = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=4096, errors='strict')
        except (UnicodeError, ValueError):
            raise HTTPException(400, "Use a UTF-8 form with at most 4096 fields.") from None
        request.state.form_values = values
        return values

    async def typed(request: Request) -> dict[str, str]:
        """The fields of a form that was sent, each by its name, for a form that uses each name once."""
        return {name: values[-1] for name, values in (await typed_lists(request)).items()}

    def ours(request: Request) -> bool:
        """True unless another site made the browser send this. A browser says where a form came from."""
        origin = request.headers.get("origin")
        return origin is None or urlsplit(origin).netloc == request.headers.get("host")

    def for_people(handler):
        """Wrap a handler so that it runs only for someone signed in, who is handed to it."""

        async def checked(request: Request) -> Response:
            """Authenticate the browser, enforce form origin and retain failed submissions."""
            if request.method == "POST" and not ours(request):
                return Response("This form was not sent from weightroom's own page.", status_code=403)
            try:
                person = await who(request)
            except ComputeOff:
                # Their sign-in is turnstile's to confirm, and turnstile is not answering.
                return page(request, "off.html", status=503)
            if person is None:
                return browser_redirect(request, sign_in_url(request))
            try:
                answer = await handler(request, person)
            except ComputeOff:
                answer = page(request, "off.html", person, status=503)
            if (request.method == 'GET' and answer.status_code in (403, 404)
                    and answer.headers.get('content-type', '').split(';')[0] in ('', 'text/plain')):
                answer = page(request, 'refused.html', person, status=answer.status_code,
                              why=answer.body.decode('utf-8'))
            if request.method == 'POST' and answer.status_code == 303:
                destination = urlsplit(urljoin(str(request.url), answer.headers.get('location', '')))
                if destination.netloc == request.url.netloc and 'failed' in parse_qs(destination.query):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observation.internal(app), client=tuple(request.client) if request.client else ('unknown', 0)), base_url=str(request.base_url)) as here:
                        returned = await here.get(destination.path + '?' + destination.query,
                                                  headers={'cookie': request.headers.get('cookie', ''), 'x-weightroom-form-return': '1'})
                    if returned.headers.get('content-type', '').startswith('text/html'):
                        answer = Response(returned.content, status_code=max(400, returned.status_code), media_type='text/html')
            if answer.status_code >= 400 and answer.headers.get('content-type', '').startswith('text/html'):
                answer = await retain_failed_form(request, answer)
            return answer

        return checked

    async def retain_failed_form(request: Request, answer: Response) -> Response:
        """A failed submission retains raw values, regardless of which handler rejected it."""
        if getattr(answer, 'form_retained', False) or request.headers.get('x-weightroom-form-return') == '1' or not hasattr(answer, 'body'):
            return answer
        if request.method == 'POST':
            values = getattr(request.state, 'form_values', None)
            if values is None:
                return answer
        elif request.method == 'GET':
            values = {key: request.query_params.getlist(key) for key in request.query_params}
        else:
            return answer
        html = answer.body.decode('utf-8')
        restored, found = refill(html, str(request.url), request.url.path, request.method, values)
        if not found:
            source = urlsplit(request.headers.get('referer', ''))
            if source.netloc != request.url.netloc or not source.path:
                return answer
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observation.internal(app), client=tuple(request.client) if request.client else ('unknown', 0)), base_url=str(request.base_url)) as here:
                original = await here.get(source.path + ('?' + source.query if source.query else ''),
                                          headers={'cookie': request.headers.get('cookie', ''),
                                                   'x-weightroom-form-return': '1'}, follow_redirects=True)
            if original.status_code != 200 or not original.headers.get('content-type', '').startswith('text/html'):
                return answer
            restored, found = refill(original.text, str(request.url), request.url.path, request.method, values)
            if not found:
                return answer
            message = error_message(html) or 'The form could not be submitted. Please check its values and try again.'
            marker = '<main id="page-content"'
            position = restored.find('>', restored.find(marker)) + 1
            if position > 0 and marker in restored:
                from html import escape
                restored = restored[:position] + '<div class="alert alert-danger" role="alert">' + escape(message) + '</div>' + restored[position:]
        answer.body = restored.encode('utf-8')
        answer.headers['content-length'] = str(len(answer.body))
        answer.headers['cache-control'] = 'no-store'
        return answer

    # ---- signing in and out -----------------------------------------------------------------

    def welcome(cookie: str, seconds: float, where: str = "/status") -> RedirectResponse:
        """Send the browser to the first page, carrying its cookie."""
        answer = go(where)
        # httponly: no script on a page can read it. lax: another site cannot make the browser send it with a form.
        answer.set_cookie(COOKIE, cookie, max_age=int(seconds), httponly=True, samesite="lax", secure=settings.secure)
        return answer

    def sign_in_next(value: str) -> str:
        """Return only a local absolute path, including its query, for a successful sign-in."""
        decoded = unquote(value)
        if (not value.startswith('/') or decoded.startswith('//') or '\\' in decoded
                or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
                or urlsplit(value).netloc or urlsplit(value).scheme):
            return '/status'
        return '/status' if value == '/' else value

    def sign_in_url(request: Request) -> str:
        """Keep deep links; background fragments return to their current console page."""
        path = request.url.path + ('?' + request.url.query if request.url.query else '')
        if request.headers.get('hx-request') == 'true' or request.method != 'GET':
            current = urlsplit(request.headers.get('hx-current-url') or request.headers.get('referer', ''))
            path = current.path + ('?' + current.query if current.query else '') if current.netloc == request.url.netloc else '/'
        if request.url.path in ('/strip', '/strip/stream', '/pulse'):
            path = '/'
        parsed = urlsplit(path)
        query = [(name, value) for name, value in parse_qsl(parsed.query) if name != 'part']
        path = sign_in_next(parsed.path + ('?' + urlencode(query) if query else ''))
        return '/sign-in' + ('?next=' + quote(path, safe='') if path != '/status' else '')

    async def sign_in_page(request: Request) -> Response:
        """Render the sign-in form without retaining its response in caches."""
        return page(request, "sign_in.html", back="back" in request.query_params, next=sign_in_next(request.query_params.get("next", "/")))

    async def sign_in(request: Request) -> Response:
        """Validate the browser form and request a session from the front account authority."""
        if not ours(request):
            return Response("This form was not sent from weightroom's own page.", status_code=403)
        form = await typed(request)
        name, password = form.get("name", "").strip(), form.get("password", "")
        again = lambda why, **more: page(request, "sign_in.html", name=name, password=password, why=why, next=sign_in_next(form.get("next", "/")), status=401, **more)  # noqa: E731
        if form.get("new_password", "") != form.get("new_again", ""):
            return again("The two new passwords are not the same.", wants="password")
        asked = {"name": name, "password": password}
        asked |= {field: form[field].strip() for field in ("code", "new_password") if form.get(field)}
        try:
            # Turnstile checks it, from the accounts on this machine. weightroom only says where the person is, for the log.
            from_where = {"x-loadcoach-from": request.client.host if request.client else "?"}
            answer = await ask("POST", "/loadcoach/sign-in", settings.key, json=asked, headers=from_where)
        except ComputeOff:
            print('weightroom: sign-in unavailable; administrator: systemctl --user start turnstile', file=sys.stderr)
            return again("Sign-in is unavailable right now. Try again in a minute, or tell your administrator.")
        told = answer.json()
        if answer.status_code == 201:
            return welcome(told["key"], datetime.fromisoformat(told["ends"]).timestamp() - time.time(), sign_in_next(form.get("next", "/")))
        why = told.get("error", {})
        if why.get("code") == "password_change":
            return again(why["message"], wants="password")
        if why.get("code") in ("second_step_setup", "code_required", "code_wrong"):
            return again(why["message"], wants="code", secret=why.get("secret"), uri=why.get("uri"))
        return again(why.get("message", f"The sign-in service returned status {answer.status_code}."))

    async def sign_out(request: Request) -> Response:
        """Revoke the browser's session key and clear its authentication cookies."""
        cookie = request.cookies.get(COOKIE, request.cookies.get(LEGACY_COOKIE, ""))
        if ours(request) and cookie:
            try:
                await ask("POST", "/loadcoach/sign-out", cookie)  # so the key stops working, not only the cookie
            except ComputeOff:
                pass
        answer = go("/sign-in")
        answer.delete_cookie(COOKIE)
        answer.delete_cookie(LEGACY_COOKIE)
        return answer

    # ---- the pages --------------------------------------------------------------------------

    async def landing(request: Request, person: Person) -> Response:
        """Keep existing root bookmarks pointing at the Status landing page."""
        return go('/status' + ('?' + request.url.query if request.url.query else ''))

    async def system_status(request: Request, person: Person) -> Response:
        """What compute is doing right now, kept true as it changes, and the controls that steer it."""
        now = None
        nodes, poll_seconds = [], 10
        told = {}
        try:
            inventory = await ask("GET", "/turnstile/machines", person.key)
            if inventory.status_code == 200:
                told = inventory.json()
                poll_seconds = told.get("poll_seconds", 10)
        except ComputeOff:
            pass
        # Never wait for a node already known to be unavailable. Live status stays
        # caller-scoped; shared monitoring data is used only for availability.
        available = [node for node in told.get("machines", [])
                     if node.get("state") == "ready" and node.get("status_live")]
        missing = person.me.get("unavailable_machines", [])
        if available or not told.get("machines") or any(node["name"] not in missing for node in told["machines"]):
            try:
                answer = await ask("GET", "/loadcoach/status", person.key)
                if answer.status_code == 200:
                    now = answer.json()
                    now['active_batches'] = current_batch_jobs(now, person, benchmarks.state.worker, benchmarks.state.store)
            except ComputeOff:
                pass
        nodes = status_nodes(told, poll_seconds, now)
        from weightroom.status_workspace import present
        summary = None
        try:
            counts = await ask('GET', '/loadcoach/requests/counts', person.key)
            if counts.status_code == 200:
                summary = counts.json()
        except ComputeOff:
            pass
        workspace = present(now, nodes, summary)
        context = dict(now=now, nodes=nodes, poll_seconds=poll_seconds, address=model_address, workspace=workspace)
        if request.query_params.get('part') == 'status':
            return page(request, 'status.html', person, **context)
        if request.query_params.get('part') == 'requests':
            return page(request, 'current_requests.html', person, **context)
        asked = request.query_params
        return page(request, 'home.html', person, **context,
                    said=asked.get('said', ''), failed='failed' in asked)

    async def carry_on(method: str, path: str, key: str) -> None:
        """Ask LoadCoach for something that may take minutes, without anyone waiting for the answer."""
        try:
            await ask(method, path, key, timeout=None)
        except ComputeOff:
            pass

    async def control(request: Request, person: Person) -> Response:
        """The buttons that steer a selected LoadCoach. That node checks the caller's ability."""
        form = await typed(request)
        do, model, machine = form.get("do", ""), form.get("model", ""), form.get("machine", "compute")
        def node_path(action: str) -> str:
            """Resolve the selected node's status-control API path."""
            return ("/loadcoach/" + action if machine == "compute" else
                    "/turnstile/machines/" + quote(machine, safe="") + "/control/" + action)

        if do == "unload":
            # Ask the selected node about locks before starting its potentially long queued unload.
            seen = await ask("GET", node_path("status"), person.key)
            if seen.status_code != 200:
                return go("/status?failed=1&said=" + quote(seen.json().get("error", {}).get("message", f"LoadCoach {machine} is not answering.")))
            locked = [one["model"] for one in seen.json().get("loaded", []) if one.get("locked")] if seen.status_code == 200 else []
            if locked:
                return go("/status?failed=1&said=" + quote(f"Locked in place: {', '.join(locked)}. Unlock first, then unload."))
            # Unloading waits in line like any request, so that no answer is cut off. That can take
            # minutes, so it is sent on its way and this page does not wait for it.
            task = asyncio.create_task(carry_on("POST", node_path("unload"), person.key))
            slow.add(task)
            task.add_done_callback(slow.discard)
            return go("/status?said=" + quote(f"Asked for every model to be unloaded. It waits its turn in line, so no answer is cut off. LoadCoach: {machine}."))
        if do in ("drain", "open"):
            # Taking no new requests, so that the line empties, or taking them again.
            asked = {"on": False} if do == "open" else {"why": form.get("why", "").strip()}
            answer = await ask("POST", node_path("drain"), person.key, json=asked)
        elif do in ("lock-model", "unlock-model"):
            path = "/loadcoach/model-lock" if machine == "compute" else "/turnstile/machines/" + quote(machine, safe="") + "/model-lock"
            answer = await ask("POST", path, person.key, json={"name": model, "on": do == "lock-model"})
        else:
            answer = await ask("POST", node_path("clear") if do == "clear" else "/loadcoach/unlock" if do == "unlock" else "/loadcoach/nothing", person.key)
        if answer.status_code != 200:
            return go("/status?failed=1&said=" + quote(answer.json().get("error", {}).get("message", "LoadCoach would not do that.")))
        removed, here = answer.json().get("removed", 0), answer.json().get("busy", 0)
        done = {
            "clear": f"Turned away {removed} waiting request{'' if removed == 1 else 's'}. Answers under way carry on. LoadCoach: {machine}.",
            "unlock": "The lock is broken.",
            "lock-model": f"Locked {model} in place. It stays loaded until it is unlocked here." + (f" LoadCoach: {machine}." if machine != "compute" else ""),
            "unlock-model": f"Unlocked {model}. It can be unloaded again." + (f" LoadCoach: {machine}." if machine != "compute" else ""),
            "drain": (f"No new requests are taken. The {here} already here will be finished." if here else "No new requests are taken. Nothing was under way.") + f" LoadCoach: {machine}.",
            "open": f"Requests are taken again. LoadCoach: {machine}.",
        }[do]
        return go("/status?said=" + quote(done))

    async def wake_compute(request: Request, person: Person) -> Response:
        """Send compute the packet that wakes it. For those whose group may, signed in here or at compute."""
        if "front-wake" not in person.can or not settings.wake_mac:
            return go("/status?failed=1&said=" + quote("Your group may not wake compute." if settings.wake_mac else "weightroom has not been told compute's hardware address."))
        wake(settings.wake_mac, settings.wake_address)
        return go("/status?said=" + quote("Asked compute to wake. From sleep it takes about twenty seconds. This page says so when it answers."))

    # ---- one request ------------------------------------------------------------------------

    async def requests_page(request: Request, person: Person) -> Response:
        """Render authorized retained requests and optional live diagnostics."""
        if request.query_params.get('export') in ('jsonl', 'receipt'):
            params = {key:value for key,value in request.query_params.items() if key != 'export'}
            answer = await ask('GET', '/loadcoach/observations', person.key,
                               params={**params, 'format':request.query_params['export'], 'limit':1000})
            return Response(answer.content, status_code=answer.status_code, headers={
                'content-type':answer.headers.get('content-type', 'application/json'), 'cache-control':'no-store',
                'x-content-type-options':'nosniff'})
        if initial_page(request):
            return page(request, 'requests.html', person, deferred=True, fragment_url=fragment_url(request, 'page'))
        limit, current = controls(request.query_params)
        completed = request.query_params.get('completed') == '1'
        filters = {key: request.query_params[key] for key in REQUEST_FILTER_FIELDS if request.query_params.get(key)}
        request_timezone = settings.timezone or 'Server local time'
        sent_filters = dict(filters)
        filter_error = ''
        try:
            for key in ('date_min', 'date_max'):
                if key in sent_filters:
                    moment = datetime.fromisoformat(sent_filters[key])
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=zone) if zone else moment.astimezone()
                    sent_filters[key] = moment.isoformat()
            parse_request_filters(sent_filters)
        except (ValueError, OverflowError, OSError) as problem:
            filter_error = str(problem)
        found = await fetch(request, person, '/loadcoach/requests', params={
            'limit': limit, 'page': current, 'completed': int(completed),
            **(sent_filters if not filter_error else {})})
        if isinstance(found, Response):
            return fragment_error(request, person, 'requests-report', 'page', found) if request.query_params.get('part') == 'page' else found
        choices = found.get('choices', {})
        choices['model'] = sorted(set(choices.get('model', [])) | set(person.me.get('models', [])))
        if filter_error:
            found.update(requests=[], total=0, page=1)
        for row in found['requests']:
            if 'timestamp' in row:
                row['at'] = datetime.fromtimestamp(row['timestamp'], zone).isoformat(timespec='seconds')
        for access in found.get('accesses', []):
            for key in ('first_at', 'last_at'):
                access[key + '_display'] = datetime.fromtimestamp(access[key], zone).strftime('%Y-%m-%d %H:%M:%S')
        filters_context = dict(request_filters=filters, request_choices=choices,
                               request_timezone=request_timezone, request_filter_error=filter_error,
                               request_poll_url='/requests?' + urlencode({**request.query_params, 'part': 'list', 'page': 1}))
        observation_report, operation_report = None, None
        if request.query_params.get('part') != 'list':
            wanted = {key: request.query_params[key] for key in ('request_id', 'trace_id', 'service', 'machine', 'application', 'outcome', 'error_code', 'since', 'until') if request.query_params.get(key)}
            if wanted or request.query_params.get('observe') == '1':
                observation_report = await fetch(request, person, '/loadcoach/observations', params={**wanted, 'limit':100})
                if isinstance(observation_report, Response):
                    observation_report = {'spans':[], 'error':'Observation metadata unavailable or filters invalid. Existing requests remain readable.'}
                operation_report = await fetch(request, person, '/loadcoach/observation-metrics')
                if isinstance(operation_report, Response):
                    operation_report = None
        template = ('requests_list.html' if request.query_params.get('part') == 'list' else
                    'requests_content.html' if request.query_params.get('part') == 'page' and request.query_params.get('complete') != '1' else 'requests.html')
        return page(request, template, person,
                    observation_report=observation_report, operation_report=operation_report,
                    recent=[] if filter_error else found['requests'], completed=completed, embedded=request.query_params.get('embedded') == '1',
                    accesses=found.get('accesses', []), **filters_context,
                    paging=pagination('/requests', request.query_params, found['total'], limit, found['page']))

    async def request_page(request: Request, person: Person) -> Response:
        """The one page for a request: what is on record about it, and, while it runs, what it has been answered so far."""
        number = request.path_params["number"]
        answer = await ask("GET", f"/loadcoach/requests/{number}", person.key,
                           params={'summary': '0' if request.query_params.get('part') == 'live' else '1'})
        if answer.status_code != 200:
            return page(request, "refused.html", person, status=answer.status_code, why=answer.json()["error"]["message"])
        facts, so_far, unreadable = answer.json(), {}, ""
        if facts["state"]:  # still under way: look at what has come back so far
            try:
                look = await ask("GET", f"/loadcoach/peek/{number}", person.key)
                if look.status_code == 200:
                    so_far = look.json()["said"]
                elif look.status_code == 403:
                    unreadable = "Reading what someone else's request is being answered needs the ability to read kept text."
            except ComputeOff:
                unreadable = "Compute is unavailable. Retained records remain readable; live progress is unknown."
        template = "request_live.html" if request.query_params.get("part") == "live" else "request.html"  # the page asking for itself again
        feedback = {}
        if template == 'request.html' or not facts['state']:
            try:
                response = await ask('GET', f'/loadcoach/requests/{number}/feedback', person.key)
                feedback = response.json() if response.status_code == 200 else {'unavailable': True}
            except ComputeOff:
                feedback = {'unavailable': True}
        return page(request, template, person, facts=facts, so_far=so_far, unreadable=unreadable,
                    feedback=feedback,
                    said=request.query_params.get("said", ""), address=model_address)  # fmt: skip

    async def stop_request(request: Request, person: Person) -> Response:
        """Request cancellation of one authorized logged inference request."""
        number = request.path_params["number"]
        answer = await ask("POST", f"/loadcoach/stop/{number}", person.key)
        said = f"Stopped request {number}. The others carry on." if answer.status_code == 200 else answer.json()["error"]["message"]
        back = "/status" if (await typed(request)).get("back") in ("home", "status") else f"/requests/{number}"
        return go(f"{back}?{'failed=1&' if answer.status_code != 200 else ''}said=" + quote(said))

    async def stop_unlogged_request(request: Request, person: Person) -> Response:
        """Cancel an unlogged request using its opaque control handle."""
        form = await typed(request)
        answer = await ask('POST', '/loadcoach/stop-unlogged', person.key, json={'handle': form.get('handle', '')})
        said = 'Stopped the unlogged request. The others carry on.' if answer.is_success else answer.json()['error']['message']
        return go('/status?' + ('failed=1&' if not answer.is_success else '') + 'said=' + quote(said))

    async def feedback_do(request: Request, person: Person) -> Response:
        """Validate and submit feedback for an authorized completed request."""
        number = request.path_params['number']
        form = await typed(request)
        try:
            value = int(form.get('value', ''))
        except ValueError:
            return go(f'/requests/{number}?failed=1&said=Choose+a+feedback+rating.')
        result = await ask('POST', f'/loadcoach/requests/{number}/feedback', person.key,
                           json={'value': value, 'explanation': form.get('explanation', '')})
        message = 'Feedback saved.' if result.status_code == 200 else result.json()['error']['message']
        return go(f'/requests/{number}?{"failed=1&" if result.status_code != 200 else ""}said=' + quote(message))

    async def account(request: Request, person: Person, said: str = "", new_key: str = "", failed: bool = False, opened: str = "") -> Response:
        """Identity, password and API keys on the account's main page."""
        asked = request.query_params
        me = (await ask("GET", "/loadcoach/me", person.key)).json()
        return page(request, "account.html", person, status=400 if failed else 200, me=me,
                    said=said or asked.get("said", ""), new_key=new_key, failed=failed)

    async def account_appearance(request: Request, person: Person, said: str = '', failed: bool = False) -> Response:
        """Render the caller's personal display preferences."""
        return page(request, 'account_appearance.html', person, status=400 if failed else 200, me=person.me,
                    said=said or request.query_params.get('said', ''), failed=failed,
                    nav=navigation('/account/appearance', person.can, bool(spotter)))

    async def account_logs(request: Request, person: Person) -> Response:
        """Render account-specific retained activity and usage."""
        asked = request.query_params
        limit, current = controls(asked)
        found = await fetch(request, person, '/loadcoach/account-events', params={'limit': limit, 'page': current})
        if isinstance(found, Response):
            return found
        context = dict(events=found['events'], days=asked.get('days', '28'),
                       paging=pagination('/account/logs', asked, found['total'], limit, found['page']))
        if asked.get('part') == 'list':
            return page(request, 'account_logs_list.html', person, **context)
        mine = await fetch(request, person, f'/loadcoach/users/{person.name}', params={'days': context['days']})
        if isinstance(mine, Response):
            return mine
        return page(request, 'account_logs.html', person, me=person.me, usage=mine.get('usage', []),
                    keep_days=mine.get('keep_days'), **context)

    async def change_password(request: Request, person: Person) -> Response:
        """Validate the password form and submit the caller's account update."""
        form = await typed(request)
        if form.get("new_password", "") != form.get("new_again", ""):
            return await account(request, person, "The two new passwords are not the same.", failed=True, opened="password")
        asked = {"password": form.get("new_password", ""), "current_password": form.get("current_password", "")}
        answer = await ask("POST", f"/loadcoach/users/{person.name}", person.key, json=asked)
        if answer.status_code != 200:
            return await account(request, person, answer.json()["error"]["message"], failed=True, opened="password")
        return await account(request, person, "Password changed. Your other sign-ins have ended.")

    async def make_key(request: Request, person: Person) -> Response:
        """Create a caller-owned API key and display its value once."""
        label = (await typed(request)).get("label", "").strip()[:60]
        answer = await ask("POST", f"/loadcoach/users/{person.name}/keys", person.key, json={"label": label})
        if answer.status_code != 201:
            return await account(request, person, answer.json()["error"]["message"], failed=True, opened="keys")
        return await account(request, person, "Copy this key now. It is not shown again.", new_key=answer.json()["key"], opened="keys")

    async def remove_key(request: Request, person: Person) -> Response:
        """Revoke the selected caller-owned API key."""
        answer = await ask("DELETE", f"/loadcoach/users/{person.name}/keys/{request.path_params['key_id']}", person.key)
        said = "Key removed." if answer.status_code == 200 else answer.json()["error"]["message"]
        return await account(request, person, said, failed=answer.status_code != 200, opened="keys")

    # ---- models -----------------------------------------------------------------------------

    def model_address(name: str) -> str:
        """Build a model-management address preserving the selected compute node."""
        return "/models/" + quote(name, safe="/:")  # a name may hold a slash or a colon, and little else

    async def fetch(request: Request, person: Person, path: str, **more) -> dict | Response:
        """What LoadCoach answers when asked for something a page shows, or the page that says why it would not."""
        answer = await ask("GET", path, person.key, **more)
        if answer.status_code != 200:
            return page(request, "refused.html", person, status=answer.status_code, why=answer.json()["error"]["message"])
        return answer.json()

    def node_model_path(request: Request, path: str) -> str:
        """Build the node-specific path for a model-management operation."""
        node = request.query_params.get("node", "")
        return ("/turnstile/machines/" + quote(node, safe="") + "/model-api/" + path.removeprefix("/loadcoach/")
                if node else path)

    def node_model_address(request: Request, name: str) -> str:
        """Build the browser URL for a model on the selected compute node."""
        node = request.query_params.get("node", "")
        return model_address(name) + ("?node=" + quote(node, safe="") if node else "")

    def matrix_columns(name: str, model: dict, selected: str, showing: dict, known: dict, nodes: dict,
                       choices: list[dict], typed: dict, trained: int | None) -> list[dict]:
        """One column of the node matrix for each node holding a copy of the model. The selected
        node's column keeps the single-node form's names and ids; another's are prefixed."""
        copies = model.get("locations") or []
        if not any(copy["machine"] == selected for copy in copies):
            copies = [{**model, "machine": selected, "enabled": True}, *copies]
        states = {node["name"]: node.get("state", "unknown") for node in choices}
        llama = model.get("engine", "llama_cpp") == "llama_cpp"
        columns = []
        for index, copy in enumerate(copies):
            machine, here = copy["machine"], copy["machine"] == selected
            facts = nodes.get(machine) or {}
            defaults = known["defaults"] if here else facts.get("defaults") or {}
            cards = known["cards"] if here else facts.get("cards") or []
            settings = showing if here else typed.get(machine) or {key: copy[key] for key in SHOWN_SETTINGS if key in copy}
            local = copy.get("name", name)
            columns.append({"machine": machine, "selected": here, "name": local, "logical": name,
                            "model_id": copy.get("model_id"), "enabled": copy.get("enabled", True),
                            "state": states.get(machine, "unknown"), "showing": settings, "cards": cards, "defaults": defaults if llama else {},
                            "prefix": "" if here else f"n:{machine}:", "idp": "" if here else f"node{index}-",
                            "on": f" on {machine}" if len(copies) > 1 else "",
                            "link": model_address(local) + "?node=" + quote(machine, safe=""),
                            "parallel": parallel_fields({**defaults, **settings.get("flags", {})}, trained) if llama else {},
                            "suggestions": suggested(settings, defaults, cards) if llama else []})
        return columns

    async def model_api(request: Request, person: Person, method: str, path: str, **more):
        """Send a management request to the explicitly selected compute node."""
        source_machine = more.pop("source_machine", "")
        machines = more.pop("machines", None)
        if more.pop("prepare_files", False):
            return await ask("POST", "/turnstile/models/install", person.key, json={
                "name": unquote(path.removeprefix("/loadcoach/models/")),
                **({"machines":machines} if machines is not None else {"machine":request.query_params.get("node") or "compute"}),
                "settings": more["json"], "source_machine": source_machine}, timeout=None)
        if method == "POST" and (path.startswith("/loadcoach/models/") or path == "/loadcoach/model-setup"):
            node = request.query_params.get("node") or "compute"
            return await ask(method, "/turnstile/machines/" + quote(node, safe="") + "/model-api/" + path.removeprefix("/loadcoach/"), person.key, **more)
        return await ask(method, node_model_path(request, path), person.key, **more)

    async def all_models(request: Request, person: Person) -> dict | Response:
        """One logical model with all verified compute locations, with an old-server fallback."""
        if request.query_params.get("node"):
            return await fetch(request, person, node_model_path(request, "/loadcoach/models"))
        answer = await ask("GET", "/turnstile/models", person.key)
        if answer.status_code == 404:
            return await fetch(request, person, "/loadcoach/models")
        if answer.status_code != 200:
            return page(request, "refused.html", person, status=answer.status_code, why=answer.json()["error"]["message"])
        return answer.json()

    def settings_from(form: dict[str, list[str]]) -> dict:
        """Read model settings from the selected compute node's catalogue."""
        selected = form.get("engine", ["llama_cpp"])[-1].strip() or "llama_cpp"
        return engine_for(selected).parse_form(form, read)

    benchmark_inventory = {}

    async def benchmark_page(request: Request, person: Person) -> Response:
        """Render benchmark evidence using the caller's catalogue and authorised run list."""
        section = 'detail' if 'number' in request.path_params else request.path_params.get("section")
        if section is None:
            target = 'results' if any(key in request.query_params for key in ("run", "model")) else 'runs'
            return go('/benchmarks/' + target + ('?' + str(request.query_params) if request.query_params else ''))
        if section == 'embedders':  # the retired Embedders page: its measurements and runs are Node measurements' Embedders type
            return go('/benchmarks/node-measurements?kind=embedding')
        if section not in {"node-measurements", "results", "runs", "compare", "manage", "detail"}:
            return Response("No such benchmark page.", status_code=404)
        if section == "manage" and "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="Your account may not manage benchmark settings.")
        found = await fetch(request, person, "/benchmarks")
        if isinstance(found, Response):
            return found
        catalogue = found['catalogue']
        catalogue_warning = ("Compute is not answering. Saved benchmark results remain available, but no new run can be started."
                             if found['compute_unavailable'] else "")
        models = [model for model in catalogue["data"] if model.get("owned_by") == "loadcoach"]  # in LoadCoach's order: by sort order, then by name
        definitions, runs = found['definitions'], found['runs']
        wanted = next((bench for bench in definitions if bench['id'] == request.query_params.get('benchmark')), {})
        kind = request.query_params.get('kind', 'llm') if section == 'node-measurements' else 'llm'
        if kind not in ('llm', 'embedding', 'image', 'audio'):
            kind = 'llm'
        resource_ids = {'context', 'speed', 'load', 'embedding_context', 'embedding_speed', 'embedding_load'}
        embedding_ids = {bench['id'] for bench in definitions if bench.get('embedding')}
        embedding_family = bool(request.query_params.get('family') == 'embedding'
                                or section == 'node-measurements' and wanted.get('embedding'))
        if section == 'node-measurements':
            if 'kind' not in request.query_params and embedding_family:
                kind = 'embedding'
            embedding_family = kind == 'embedding'
            models = [model for model in models if benchmark_model_kind(model) == ('llm' if kind == 'embedding' else kind)]
            if kind in ('image', 'audio'):
                embedding_family = False
                definitions = [bench for bench in definitions if bench['id'] == 'load']
        elif section == 'results':
            embedding_family = False
            models = [model for model in models if benchmark_model_kind(model) == 'llm']
        if section in ('node-measurements', 'results', 'compare'):
            definitions = [bench for bench in definitions if bool(bench.get('embedding')) == bool(embedding_family)]
            models = [model for model in models if bool(model.get('embedding')) == bool(embedding_family)]
        if section == 'node-measurements':
            definitions = [bench for bench in definitions if bench['id'] in resource_ids]
        if section == 'results':
            definitions = [bench for bench in definitions if bench['id'] not in resource_ids]
            models = list({model['id']: model for model in models}.values())
        requested = request.query_params.get('benchmark', '')
        if section == 'node-measurements' and requested:
            requested = ('embedding_' if embedding_family else '') + requested.removeprefix('embedding_')
        selected = next((bench for bench in definitions if bench["id"] == requested), definitions[0])
        resource_benchmarks = [bench for bench in definitions if bench['id'] in resource_ids]
        score_benchmarks = [bench for bench in definitions if bench['id'] not in resource_ids]
        resource_selected = next((bench for bench in resource_benchmarks if bench['id'] == selected['id']),
                                 resource_benchmarks[0] if resource_benchmarks else {})
        measurement = resource_selected.get('id', 'context').removeprefix('embedding_')
        type_benchmarks = {'llm': measurement, 'embedding': 'embedding_' + measurement, 'image': 'load', 'audio': 'load'}
        if section == 'manage' and request.query_params.get('export') and selected.get('custom'):
            answer = await ask('GET', f"/benchmarks/datasets/{selected['id']}/{selected['version']}", person.key)
            return Response(answer.content, status_code=answer.status_code, media_type='application/json',
                            headers={'content-disposition': f'attachment; filename="{selected["id"]}-v{selected["version"]}.json"'})
        latest = latest_results(runs)
        location_latest = {}
        for run in runs:  # performance evidence is never collapsed across compute locations
            if run['state'] != 'completed' or run.get('retired') or not run.get('location'):
                continue
            key = (run['model'], run['location'], run['benchmark'])
            prior = location_latest.get(key)
            if prior is None or run.get('current_fingerprint') and not prior.get('current_fingerprint'):
                location_latest[key] = run
        inventory = {'machines': list(benchmark_inventory.values())}
        inventory_warning = ''
        if section in ('runs', 'node-measurements'):
            try:
                answer = await ask('GET', '/turnstile/machines', person.key)
                if answer.is_success:
                    # The monitor's model/loaded lists are not caller-scoped. Only
                    # public node topology is consumed; choices use the catalogue.
                    inventory = {'machines': [{**{key: node[key] for key in
                        ('name', 'state', 'hostname', 'lifecycle', 'accepting_work') if key in node},
                        'cards': [{key: card[key] for key in ('card', 'total') if key in card} for card in node.get('cards') or []]}
                        for node in answer.json().get('machines', [])]}
                    benchmark_inventory.clear()
                    benchmark_inventory.update({node['name']: node for node in inventory['machines']})
                else:
                    inventory_warning = 'Node inventory could not be refreshed. Showing last known nodes and saved evidence.'
            except (ComputeOff, httpx.HTTPError):
                inventory_warning = 'Node inventory could not be refreshed. Showing last known nodes and saved evidence.'
        visible_models = {model['id'] for model in models}
        colours = {}
        for bench in definitions:
            values = [item[bench['id']]['score'] for model, item in latest.items()
                      if model in visible_models and bench['id'] in item and item[bench['id']]['score'] is not None]
            for model, item in latest.items():
                run = item.get(bench['id'])
                if model in visible_models and run and run['score'] is not None:
                    fraction = (run['score'] - min(values)) / (max(values) - min(values)) if max(values) != min(values) else .5
                    if bench.get('green') == 'low':
                        fraction = 1 - fraction
                    colours[(model, bench['id'])] = 'rgb(%d,%d,%d)' % tuple(round(a + fraction * (b-a)) for a,b in zip((223,134,57),(41,152,121)))
        picks = set(request.query_params.getlist('model'))
        chosen = [model for model in models if model['id'] in picks]
        detail = None
        regression, regression_error = None, ''
        number = request.path_params.get('number')
        if section == 'results' and request.query_params.get('run'):
            return go('/benchmarks/run/' + quote(request.query_params['run'], safe=''))
        if number:
            detail = await fetch(request, person, '/benchmarks/' + quote(number, safe=''))
            if isinstance(detail, Response):
                return detail
            recorded = next((run for run in runs if run['id'] == number), {})
            detail.update({key: recorded[key] for key in ('current_fingerprint', 'rerun') if key in recorded})
            if request.query_params.get('baseline'):
                compared = await ask('GET', '/benchmarks/regressions', person.key,
                                     params={'run':number, 'baseline':request.query_params['baseline']})
                if compared.is_success:
                    regression = compared.json()
                else:
                    regression_error = compared.json().get('error', {}).get('message', 'Comparison unavailable.')
        def identity(run):
            """Extract a stable identity for catalogue and retained-evidence matching."""
            return (run.get('model_id') or run['model'], run['fingerprint'], run['environment'].get('engine_hash'), run['environment'].get('machine_hash'))
        history, identities = [], set()
        for run in runs:
            if section == 'compare' and (run['benchmark'] in embedding_ids) != embedding_family:
                continue
            if run['state'] == 'completed' and identity(run) not in identities:
                identities.add(identity(run))
                history.append(run)
        selected_runs = [run for run in history if run['id'] in request.query_params.getlist('run')]
        columns = [{'model': model['id'], 'fingerprint': model.get('fingerprint'), 'environment': {}, 'results': latest.get(model['id'], {})} for model in chosen]
        for run in selected_runs:
            results = {}
            for candidate in runs:
                if candidate['state'] == 'completed' and identity(candidate) == identity(run):
                    results.setdefault(candidate['benchmark'], candidate)
            columns.append({'model': run['model'], 'fingerprint': run['fingerprint'], 'environment': run['environment'], 'results': results})
        comparisons = request.query_params.getlist('bench') if 'selection' in request.query_params else [bench['id'] for bench in definitions]
        comparison_ranks(columns, definitions, latest, comparisons)
        history_filter = request.query_params.get('history_filter', 'all')
        if history_filter not in ('all', 'fingerprints', 'quants', 'retired'):
            history_filter = 'all'
        variants = []
        for model in chosen:
            saved = next((run for run in runs if run.get('model_id') and run['model_id'] == model.get('model_id')
                          and run.get('current_fingerprint')), None)
            variants.append({**model, **({'family': saved.get('family', variant(model['id'])['family']), 'quant': saved.get('quant', variant(model['id'])['quant'])} if saved else variant(model['id']))})
        variants.extend(selected_runs)
        def matches(run):
            """Match retained benchmark evidence to the selected model identity."""
            if history_filter == 'retired':
                return run.get('retired')
            if history_filter == 'all':
                return True
            return any(run.get('family') == model.get('family') and run.get('quant') and model.get('quant') and
                       (run['quant'] != model['quant'] if history_filter == 'quants' else
                        run['quant'] == model['quant'] and run['fingerprint'] != model.get('fingerprint')) for model in variants)
        filtered_history = [run for run in history if matches(run)]
        measurement_models = [model for model in models if bool(model.get('embedding')) == bool(selected.get('embedding'))
                              and (model.get('capabilities', {}).get('native_probes') is not False
                                   or selected['id'] == 'load' and model.get('capabilities', {}).get('load_probes'))
                              and (not model.get('capabilities', {}).get('load_probes') or selected['id'] == 'load')]
        may_run = ('benchmark' in person.can or 'superuser' in person.can) and not found['compute_unavailable']
        lanes = run_lanes(catalogue, inventory, models, selected, runs, location_latest, latest, may_run)
        if section == 'node-measurements':
            lanes = [node for node in lanes if node['service']]
        matrix = measurement_matrix(catalogue, inventory, models, resource_benchmarks, resource_selected, runs, location_latest) if section == 'node-measurements' else None
        queue = sorted((run for run in runs if run['state'] not in ('running', 'completed', 'failed', 'cancelled')),
                       key=lambda run: run['created'])
        active_runs = [run for run in runs if run['state'] == 'running']
        live_runs = []
        for active in active_runs:
            data = await fetch(request, person, '/benchmarks/' + active['id'])
            live_runs.append({'run': active, 'detail': None if isinstance(data, Response) else data})
        live = next((item['detail'] for item in live_runs if item['detail']), None)
        pagination = history_page(runs, request.query_params)
        history_query = urlencode({'benchmark': selected['id'], 'per_page': pagination['size'],
                                  'history_node': pagination['node'], 'outcome': pagination['outcome'], 'q': pagination['search']})
        live_query = (urlencode({'benchmark': selected['id'], 'kind': kind, 'part': 'live'}) if section == 'node-measurements'
                      else history_query + '&page=' + str(pagination['page']) + '&part=live')
        return page(request, 'benchmark_live.html' if section in ('runs', 'node-measurements') and request.query_params.get('part') == 'live' else 'benchmarks.html', person,
                    section=section, benchmarks=definitions, benchmark=selected, models=models, selected_models=chosen,
                    comparisons=comparisons, columns=columns, history=filtered_history, history_filter=history_filter,
                    selected_runs=selected_runs, detail=detail, queue=queue, pagination=pagination,
                    history_query=history_query, live_query=live_query,
                    lanes=lanes, run_models=run_model_rows(lanes), live_runs=live_runs, selected_target=(request.query_params.get('pick_model'), request.query_params.get('pick_node')),
                    regression=regression, regression_error=regression_error,
                    latest=latest, measurement_models=measurement_models,
                    resource_benchmarks=resource_benchmarks, score_benchmarks=score_benchmarks,
                    score_models=[model for model in models if benchmark_model_kind(model) == 'llm' and not model.get('embedding')],
                    resource_selected=resource_selected, matrix=matrix, inventory_warning=inventory_warning,
                    colours=colours, runs=runs, paused=found['paused'], live=live,
                    embedding_family=embedding_family, kind=kind, type_benchmarks=type_benchmarks,
                    may_run=may_run, catalogue_warning=catalogue_warning,
                    said=request.query_params.get('said',''), failed='failed' in request.query_params,
                    partial='partial' in request.query_params)

    async def benchmark_do(request: Request, person: Person) -> Response:
        """Pass forms to compute, which enforces run and definition permissions."""
        section = request.path_params['section']
        form = await typed_lists(request)
        one = lambda name: form.get(name, [''])[-1]
        measurement_setup = section == 'node-measurements' or section == 'control' and one('return_page') == 'node-measurements'

        def run_back(said, flag=''):
            query = {'benchmark': one('benchmark'), **({flag: '1'} if flag else {}), 'said': said}
            if measurement_setup:
                query['kind'] = one('kind') if one('kind') in ('llm', 'embedding', 'image', 'audio') else 'llm'
            return go('/benchmarks/' + ('node-measurements' if measurement_setup else 'runs') + '?' + urlencode(query)
                      + ('#measurement-runs' if measurement_setup else ''))

        if section == 'manage' and one('action') == 'import':
            answer = await ask('POST', '/benchmarks/datasets', person.key, content=one('dataset').encode('utf-8'))
            if not answer.is_success:
                return go('/benchmarks/manage?failed=1&said=' + quote(answer.json()['error']['message']))
            return go('/benchmarks/manage?benchmark=' + quote(answer.json()['benchmark']) + '&said=' + quote('Imported immutable case-set version.'))
        if section == 'manage' and one('action') == 'delete-dataset':
            answer = await ask('DELETE', '/benchmarks/datasets/' + quote(one('benchmark'), safe=''), person.key)
            if not answer.is_success:
                return go('/benchmarks/manage?failed=1&benchmark=' + quote(one('benchmark')) + '&said=' + quote(answer.json()['error']['message']))
            return go('/benchmarks/manage?said=' + quote('Deleted the case set.'))
        if section in ('runs', 'node-measurements'):
            if section == 'runs' and one('action') == 'outstanding':
                path, body, back = '/benchmarks/outstanding', {'model': one('model')}, 'runs'
            else:
                targets = []
                try:
                    targets = [json.loads(value) for value in form.get('run-target', [])]
                    if len(targets) > 100 or any(not isinstance(target, dict) or not isinstance(target.get('model'), str)
                        or not target.get('model') or not isinstance(target.get('machine'), str) or not target.get('machine') for target in targets):
                        raise ValueError('Invalid target')
                    if targets and form.get('run-model'):
                        raise ValueError('Mixed target formats')
                except (ValueError, TypeError):
                    return run_back('Choose one to 100 valid model and node targets. No runs were submitted.', 'failed')
                path, body, back = '/benchmarks', {'benchmark': one('benchmark'),
                                                    **({'targets': targets} if targets else {'models': form.get('run-model', [])})}, 'runs'
        elif section == 'control':
            path, body, back = '/benchmarks/control', {'action': one('action'), **({'run': one('run')} if one('run') else {})}, 'runs'
        elif section == 'manage':
            path, body, back = '/benchmarks/definitions', {'benchmark': one('benchmark'), 'formula': one('formula'), 'enabled': 'enabled' in form, 'green': one('green') or None, 'server': one('server') or None}, 'manage'
        else:
            return Response('No such benchmark action.', status_code=404)
        answer = await ask('POST', path, person.key, json=body)
        if not answer.is_success:
            if measurement_setup:
                return run_back(answer.json()['error']['message'], 'failed')
            came = urlsplit(request.headers.get('referer', '')).path  # the page the form is on: Start a run is on two of them
            if came.rstrip('/') == '/benchmarks':
                came = '/benchmarks/runs'
            return go((came if came.startswith('/benchmarks') else '/benchmarks/' + back) + '?failed=1&said=' + quote(answer.json()['error']['message'])
                      + ('&benchmark=' + quote(one('benchmark'), safe='') if section == 'manage' else ''))
        if section == 'runs' and one('action') == 'outstanding':
            result = answer.json()
            said = f"Queued {result['queued']} outstanding benchmark{'s' if result['queued'] != 1 else ''}."
            if result.get('fresh'):
                said += f" {len(result['fresh'])} already current."
            if result.get('active'):
                said += f" {len(result['active'])} already queued or running."
            unavailable = result.get('unavailable') or []
            errors = result.get('errors') or []
            pending = result.get('pending') or []
            if unavailable:
                said += ' Environment unavailable: ' + ', '.join(unavailable) + '.'
            if errors:
                said += ' Could not queue: ' + ', '.join(
                    f"{item['benchmark']} ({item.get('message') or 'compute rejected the request'})" for item in errors) + '.'
            if pending:
                said += f' {len(pending)} task acknowledgements pending; existing task IDs will be checked before retrying.'
            return go('/benchmarks/runs?' + ('partial=1&' if unavailable or errors or pending else '') + 'said=' + quote(said))
        if section in ('runs', 'node-measurements'):
            result = answer.json()
            count = len(result.get('runs', []))
            said = f"Queued {count} run{'s' if count != 1 else ''}."
            if result.get('active'):
                said += f" {len(result['active'])} already queued or running."
            if result.get('pending'):
                said += f" {len(result['pending'])} task acknowledgements pending; existing task IDs will be checked before retrying."
            if result.get('errors'):
                said += ' Could not queue: ' + '; '.join(
                    f"{item['model']} at {item['machine']}: {item['message']}" for item in result['errors']) + '.'
            if result.get('skipped'):
                said += ' Incompatible model type: ' + ', '.join(result['skipped']) + '.'
            flag = 'partial' if result.get('errors') or result.get('pending') or result.get('skipped') else ''
            return run_back(said, flag)
        if measurement_setup:
            return run_back('Saved.')
        return go('/benchmarks/' + back + '?said=' + quote('Saved.') + ('&benchmark=' + quote(one('benchmark'), safe='') if section == 'manage' else ''))

    async def models_page(request: Request, person: Person, said: str = "", failed: bool = False, form: dict | None = None) -> Response:
        """Render the merged authorized model catalogue in configured order.

        `node` is the pill above the page: one compute node's own models, or all of them merged.
        Add a model has its own choice of nodes (`add_to`), several at once; with none given it
        starts from the pill's node, or the only node there is.
        """
        if 'manage-models' not in person.can:
            return page(request, 'refused.html', person, status=403, why='Your account may not add, change and remove models.')
        asked = request.query_params
        kind = asked.get('kind', 'llm')
        if kind not in ('all', 'llm', 'image', 'audio'):
            kind = 'llm'
        part = asked.get('part') if asked.get('complete') != '1' else None
        shown_node = asked.get('node', '')
        if form is None and any(key in asked for key in ('file', 'name', 'projector')):
            # "Add" was pressed beside a file on the Files page, or the engine was changed. The file goes
            # into the form, with a name made from the file's own, for the person to change or keep.
            form = {"file": asked.get("file", ""), "trained": asked.get("trained", ""), "projector": asked.get("projector", ""),
                    "name": asked.get("name") or (Path(asked["file"]).stem.lower() if asked.get("file") else "")}
        node_choices = []
        if part != 'list':
            nodes_answer = await ask("GET", "/turnstile/machines", person.key)
            node_choices = nodes_answer.json().get("machines", []) if nodes_answer.is_success else []
            names = [node["name"] for node in node_choices]
            if node_choices and not {shown_node, *asked.getlist('add_to')} - {''} <= set(names):
                return page(request, "refused.html", person, status=404, why="That compute node is not configured.")
        if initial_page(request) and not failed:
            return page(request, 'models.html', person, deferred=True, known={}, kind=kind,
                        list_url=fragment_url(request, 'list'), editor_url=fragment_url(request, 'editor'),
                        node_choices=node_choices, shown_node=shown_node, form=form or {},
                        said=said or asked.get('said', ''), failed=False)
        try:
            known = await all_models(request, person) if part != 'editor' else {'models': []}
        except ComputeOff:
            known = {"models": [], "to_measure": [], "measuring": None}
        if isinstance(known, Response):
            return fragment_error(request, person, 'models', 'list', known) if part == 'list' else known
        kinds = {"sdcpp": "image", "audiocpp": "audio"}
        known['models'] = [one for one in known['models'] if kind == 'all' or kinds.get(one.get('engine'), 'llm') == kind]
        measuring = measure_button([one for one in known['models'] if kinds.get(one.get('engine'), 'llm') == 'llm'],
                                   node_measuring(known, shown_node), shown_node)
        wanted_jobs = asked.getlist("install")
        progress = await ask("GET", "/turnstile/artifacts/distributions", person.key) if wanted_jobs else None
        installations = [job for job in progress.json().get("distributions", []) if job["id"] in wanted_jobs] if progress and progress.is_success else []
        if part == "list":  # the list asking for itself again
            return page(request, "models_list.html", person, known=known, address=lambda label: node_model_address(request, label),
                        kind=kind, installations=installations, adaptor_models=plugins.models(person.me) if kind in ('all', 'llm') else [],
                        measuring=measuring, measure_oob=True)
        names = [node["name"] for node in node_choices]
        add_nodes = ((form or {}).get("nodes") or asked.getlist("add_to") or ([shown_node] if shown_node else names if len(names) == 1 else []))
        # Each node's engines; an engine can be chosen when it is enabled on every node it is added to.
        async def engines_of(name):
            """One node's engines; a node too old to list them has llama.cpp alone."""
            answer = await ask("GET", "/turnstile/machines/" + quote(name, safe="") + "/engines", person.key)
            return answer.json().get("engines", []) if answer.is_success else [
                {"id": "llama_cpp", "label": "llama.cpp", "enabled": True, "schema": engine_for().schema()}] if answer.status_code == 404 else []
        engine_nodes = names or ["compute"]
        engine_choices = {}
        for name, rows in zip(engine_nodes, await asyncio.gather(*(engines_of(name) for name in engine_nodes))):
            for row in rows:
                if not row.get('schema', {}).get('selectable', True):
                    continue
                merged = engine_choices.setdefault(row["id"], {**row, "enabled": False, "nodes": [], "cards": []})
                if row["enabled"]:
                    merged.update(enabled=True)
                    merged["nodes"].append(name)
                if name in (add_nodes or engine_nodes):
                    merged["cards"] += [card for card in row.get("cards", []) if card not in merged["cards"]]
        engine_choices = list(engine_choices.values())
        chosen_engine = (form or {}).get("engine") or asked.get("engine", "llama_cpp")
        enabled_engines = [row for row in engine_choices if row["enabled"]]
        engine_schema = next((row["schema"] for row in enabled_engines if row["id"] == chosen_engine), None)
        if engine_schema is None and enabled_engines:
            said, failed = "Choose an engine available on the chosen nodes.", True
            chosen_engine, engine_schema = enabled_engines[0]["id"], enabled_engines[0]["schema"]
        chosen = next((row for row in engine_choices if row['id'] == chosen_engine), {})
        known['cards'] = chosen.get('cards') or [card['card'] for node in node_choices if node['name'] in (add_nodes or names) for card in node.get('cards', [])]
        available = await ask("GET", "/turnstile/models/files", person.key) if kind in ('all', 'llm') else None
        file_nodes = available.json().get("nodes", []) if available and available.is_success else []
        # The chosen nodes' own files come first; a file anywhere else is copied to them.
        ordered = sorted(file_nodes, key=lambda row: row['name'] not in add_nodes)
        def files_that_are(what):
            """The usable files of one kind ("model" or "projector"), each with the node it is on."""
            return [{**file, "machine": node["name"]} for node in ordered for file in node.get("files", [])
                    if file.get("facts", {}).get("what") == what and not file.get("moving")]
        return page(request, 'models_editor.html' if part == 'editor' else "models.html", person, status=400 if failed else 200, known=known,
                    kind=kind, installations=installations, file_choices=files_that_are("model"), projector_choices=files_that_are("projector"),
                    file_nodes=file_nodes, node_choices=node_choices, shown_node=shown_node, add_nodes=add_nodes,
                    engine_choices=engine_choices, selected_engine=chosen_engine, engine_schema=engine_schema,
                    said=said or asked.get("said", ""), failed=failed, form=form or {}, address=lambda label: node_model_address(request, label),
                    adaptor_models=plugins.models(person.me) if kind in ('all', 'llm') else [], measuring=measuring)  # fmt: skip

    async def measure_on_nodes(person: Person, nodes: dict[str, dict], stop: bool, copies: set[tuple[str, str]] | None = None,
                               merged: bool = True) -> str | None:
        """Start measuring on every node in `nodes` (see node_measuring) with a model to measure, or stop the runs under way.
        `copies` keeps one model's copies. `merged` is false for a front too old to merge nodes, which knows compute alone.

        Each node measures its own copies, one after another, and gives way to any request.
        Says why nothing could be started, or None when every node that was asked agreed."""
        if stop:
            targets = {name: None for name, one in nodes.items() if one.get("measuring") and not one["measuring"].get("ended")}
        else:
            targets = {name: (models if copies is not None else None) for name, models in measure_plan(nodes, copies).items()}
        if not targets:
            return None if stop else "Every model already has its memory measured at its minimum and maximum context."
        def path(name: str) -> str:
            return "/turnstile/machines/" + quote(name, safe="") + "/model-api/model-measure-all" if merged else "/loadcoach/model-measure-all"
        answers = await asyncio.gather(*(ask("POST", path(name), person.key, json={"on": not stop, **({"names": models} if models else {})})
                                         for name, models in targets.items()))
        refused = [f"{name}: {answer.json().get('error', {}).get('message', answer.status_code)}"
                   for name, answer in zip(targets, answers) if not answer.is_success]
        return "; ".join(refused) or None

    async def measure_models(request: Request, person: Person) -> Response:
        """Start measuring every model with a copy that has no measure, on each node it is on, or stop the runs under way. Back to the list, which shows how far they have got."""
        stop = (await typed(request)).get("do") == "stop"
        known = await all_models(request, person)
        if isinstance(known, Response):
            return known
        node = request.query_params.get("node", "")
        refused = await measure_on_nodes(person, node_measuring(known, node), stop, merged=bool(known.get("nodes") or node))
        if refused:
            return await models_page(request, person, refused, failed=True)
        return go("/models?" + ("node=" + quote(node, safe="") + "&" if node else "") + "said=" + quote("Asked for the measuring to stop." if stop else "Measuring the models that have no measure. The list shows how far it has got."))

    async def model_files_picker(request: Request, person: Person) -> Response:
        """Expose managed file choices through the signed-in session, without browser API keys."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        answer = await ask("GET", "/turnstile/models/files", person.key)
        return Response(answer.content, answer.status_code, media_type="application/json", headers={"cache-control":"no-store"})

    async def fit_page(request: Request, person: Person, said: str = '', failed: bool = False, review: dict | None = None, machine_override: str = '') -> Response:
        """Render the node-local split browser and combined memory estimate without loading models.

        The bars, the sliders and the automatic fit are static/fit.js, which works from what
        this page hands it. The roles are worked out here, by the same rule LoadCoach picks
        with: each model of a role counts as warm if it is ticked. With `?part=roles` and the
        ticked models as `on`, only that piece is sent. The page asks for it at every tick.
        """
        known = await all_models(request, person)
        if isinstance(known, Response):
            return known
        asked = request.query_params
        multi_node = bool(known.get("nodes")) or bool(known.get('unavailable_machines'))
        nodes = known.get("nodes") or {"compute": {key: known.get(key) for key in ('cards', 'defaults', 'engine_cards', 'devices', 'memory_policy')}}
        nodes = {**nodes, **{name: {} for name in known.get('unavailable_machines', []) if name not in nodes}}
        known = {**known, 'nodes': nodes}
        machine = machine_override or asked.get("machine", "compute" if "compute" in nodes else next(iter(nodes)))
        if machine not in nodes:
            return page(request, "refused.html", person, status=404, why=f"There is no answering compute node named {machine}.")
        known = fit_node(known, machine)
        local = {model["name"] for model in known["models"]}
        # The page opens with what is loaded right now ticked, each model at the context it was loaded with.
        on = set(asked.getlist("on")) if "part" in asked else {model["name"] for model in known["models"] if model.get("loaded") == 'ready'}
        found = await fetch(request, person, "/loadcoach/roles")
        if isinstance(found, Response):
            found = {'roles': []}
        for role in found["roles"]:
            answer = await ask("GET", "/loadcoach/pick", person.key, params={"model": role["name"]})
            seen = answer.json()["models"] if answer.status_code == 200 else []  # refused: a role with no model this person may use
            # A model on this machine is warm if it is ticked. One at an outside service needs no loading, and stays as LoadCoach said.
            able = [{**one, 'resident': one['name'] in local and one['name'] in on,
                     "warm": one["name"] in on if one["name"] in local else one["warm"],
                     **loaded_factors(role, one['name'] in local and one['name'] in on)} for one in seen if not one["why_not"]]
            for one in able:
                if one['name'] in local and 'estimate' in one:
                    one['estimate'] = completion({'seconds': 0 if one['name'] in on else one['load']},
                        one['estimate']['work'], one['speed'], one['prompt_speed'])
            role["picks"] = {":".join(filter(None, [role["name"], strength])): pick(strength, able) for strength in (None, *STRENGTHS)}
        if "part" in asked:
            return page(request, "fit_roles.html", person, roles=found["roles"], on=on, address=model_address)
        status_path = f"/turnstile/machines/{quote(machine, safe='')}/control/status" if multi_node else "/loadcoach/status"
        now_answer = await ask("GET", status_path, person.key)
        available = now_answer.status_code == 200
        now = now_answer.json() if available else {'memory': {}, 'margin': 0, 'system_memory': {}}
        loadout_path = f'/turnstile/machines/{quote(machine, safe="")}/control/loadouts' if multi_node else '/loadcoach/loadouts'
        saved = await ask('GET', loadout_path, person.key) if available else None
        loadouts = saved.json() if saved is not None and saved.is_success else {'loadouts': [], 'leases': [], 'why': saved.json().get('error', {}).get('message', 'Matching compute update is required.') if saved is not None else f'Node {machine} is unavailable. Reconnect to save, review or load its sets.'}
        known['cards'] = known.get('cards') or []
        from weightroom.fit import workspace as fit_workspace
        workspace = fit_workspace(known, now, fit_line, fit_cpu_mib)
        workspace.update(machine=machine, api_path=loadout_path, available=available)
        return page(request, "fit.html", person, status=400 if failed else 200, known=known, roles=found["roles"], on=on,
                    machine=machine, nodes=nodes,
                    loadouts=loadouts, review=review, said=said or asked.get('said', ''), failed=failed, workspace=workspace,
                    address=lambda name: model_address(name) + '?node=' + quote(machine, safe=''))

    async def fit_do(request: Request, person: Person) -> Response:
        """Validate and apply the selected Dry fit operation."""
        form = await typed(request)
        machine = form.get('machine') or request.query_params.get('machine') or 'compute'
        action = form.get('do', '')
        if action == 'preview-undo':
            action = 'preview'
            form['undo'] = '1'
        body = {'action': action, 'name': form.get('name', '')}
        try:
            if action == 'save':
                body['models'] = json.loads(form.get('selection', '[]'))
            if form.get('version'):
                body['version'] = int(form['version'])
            if action in ('preview', 'apply', 'request'):
                body.update(undo=form.get('undo') == '1', token=form.get('token', ''), acknowledge='acknowledge' in form)
            if action in ('renew', 'release'):
                body['lease'] = form.get('lease', '')
        except (ValueError, RecursionError):
            return await fit_page(request, person, 'The selection or version is not valid. Review the controls and try again.', True, machine_override=machine)
        # Always target the named node, including primary compute. No fallback can apply another node's set.
        answer = await ask('POST', f'/turnstile/machines/{quote(machine, safe="")}/control/loadouts', person.key, json=body, timeout=None)
        if not answer.is_success:
            return await fit_page(request, person, answer.json().get('error', {}).get('message', 'The operation failed.'), True, machine_override=machine)
        if action == 'preview':
            return await fit_page(request, person, review=answer.json(), machine_override=machine)
        message = {'save': 'Saved selection. Preview it before applying or loading.', 'apply': 'Restored the reviewed configuration.' if body.get('undo') else 'Applied the complete reviewed configuration.',
                   'request': 'All load-out members are ready and held for ten minutes.', 'renew': 'Renewed this set hold for ten minutes.',
                   'release': 'Released this set hold.', 'delete': 'Deleted the saved selection.'}.get(action, 'Done.')
        return go('/dry-fit?' + urlencode({'machine': machine, 'said': message}))

    async def add_model(request: Request, person: Person) -> Response:
        """Parse the engine-specific form and add the model on each chosen node."""
        form = await typed_lists(request)
        if form.get("artifact"):
            try:
                artifact = json.loads(form["artifact"][-1])
                form["file"], form["source_machine"] = [artifact["path"]], [artifact["machine"]]
            except (ValueError, KeyError, TypeError):
                return await models_page(request, person, "Choose a model file from cache/storage.", True)
        name = form.get("name", [""])[-1].strip()
        try:
            made = settings_from(form)
        except ValueError as problem:
            return await models_page(request, person, str(problem), failed=True, form={key: values[-1] for key, values in form.items()} | {"nodes": form.get("node", [])})
        if made.get("engine", "llama_cpp") == "llama_cpp":
            made.update(gpus="any", allow_split=True)
        # `new`: add only. Without it LoadCoach would replace a model that has this name already.
        if name and form.get("prepare_files"):
            # The nodes ticked on the form, each given the model. Turnstile checks them all before starting any.
            nodes = list(dict.fromkeys(node for node in form.get("node", []) if node)) or (
                [request.query_params["node"]] if request.query_params.get("node") else [])
            kept = {key: values[-1] for key, values in form.items()} | {"nodes": nodes}
            if not nodes:
                return await models_page(request, person, "Choose at least one compute node.", failed=True, form=kept)
            answer = await ask("POST", "/turnstile/models/install", person.key, json={"name": name, "machines": nodes,
                               "settings": made, "source_machine": form.get("source_machine", [""])[-1]}, timeout=None)
            if answer.status_code == 202:
                shown = form.get("shown_node", [""])[-1]
                where = nodes[0] if len(nodes) == 1 else ", ".join(nodes[:-1]) + " and " + nodes[-1]
                said = f"Preparing model files for {where}. The model is added to each only after its files are verified."
                return go("/models?" + urlencode([("install", job["id"]) for job in answer.json()["jobs"]]
                                                 + ([("node", shown)] if shown else []) + [("said", said)]))
            return await models_page(request, person, answer.json()["error"]["message"], failed=True, form=kept)
        else:
            answer = await model_api(request, person, "POST", f"/loadcoach/models/{quote(name, safe='/:')}", json=made, params={"new": "1"}) if name else None
        if answer is None or answer.status_code != 201:
            why = "Give it a name." if answer is None else answer.json()["error"]["message"]
            return await models_page(request, person, why, failed=True, form={key: values[-1] for key, values in form.items()})
        return go(node_model_address(request, name) + ("&said=" if request.query_params.get("node") else "?said=") + quote(f"Added {name}. It can be asked for from now on."))

    async def model_page(request: Request, person: Person, said: str = "", failed: bool = False, typed_in: dict | None = None,
                         typed_nodes: dict | None = None) -> Response:
        """Render one model's settings on every node that has a copy, and its retained benchmark evidence.

        `typed_in` and `typed_nodes` hold what was typed, or a loaded preset, for this node and the others."""
        known = await all_models(request, person)
        if isinstance(known, Response):
            return known
        name = request.path_params["name"]
        model = next((one for one in known["models"] if one["name"] == name), None)
        if model is None:
            return page(request, "refused.html", person, status=404, why=f"There is no model named {name}.")
        selected_node = request.query_params.get("node") or "compute"
        if not request.query_params.get("node") and model.get("locations") and model["locations"][0]["machine"] != "compute":
            location = model["locations"][0]
            return go(model_address(location["name"]) + "?node=" + quote(location["machine"]))
        logical_nodes = known.get("nodes") or {}  # each node's defaults and cards, when the catalogue is the logical one
        if request.query_params.get("node"):
            logical = await ask("GET", "/turnstile/models", person.key)
            if logical.is_success:
                copies = next((row.get("locations", []) for row in logical.json()["models"] if any(
                    loc["machine"] == selected_node and loc.get("model_id") == model.get("model_id")
                    for loc in row.get("locations", []))), [])
                model["locations"] = copies
                logical_nodes = logical.json().get("nodes") or {}
        nodes = await ask("GET", "/turnstile/machines", person.key)
        node_choices = nodes.json().get("machines", []) if nodes.is_success else []
        if request.query_params.get("part") == "state":  # the top of the page asking for itself again
            return page(request, "model_state.html", person, model=model, build=known.get("build"), address=lambda label: node_model_address(request, label), file_address=file_address)
        # Prefer current measurements, retaining saved results when settings have changed.
        scored = await ask("GET", "/benchmarks", person.key)
        benchmarks = scored.json().get("definitions", [])
        multimedia = model.get("engine") in ("sdcpp", "audiocpp")
        if multimedia:
            benchmarks = [bench for bench in benchmarks if bench['id'] == 'load']
        benchmarks = [bench for bench in benchmarks if bool(bench.get('embedding')) == bool(model['flags'].get('embedding'))]
        local_runs = [{**run, 'model':name} for run in scored.json().get('runs', [])
                      if (run.get('location') or 'compute') == selected_node
                      and (run.get('model_id') == model.get('model_id') if run.get('model_id') and model.get('model_id') else run['model'] == name)]
        results = latest_results(local_runs).get(name, {})
        # The benchmark whose figures are shown: the one asked for, or the first this model has run, or the first there is.
        ids = [bench["id"] for bench in benchmarks]
        chosen = next((one for one in [request.query_params.get("benchmark"), *results] if one in ids), ids[0] if ids else "")
        scores = {"benchmarks": benchmarks, "results": results, "chosen": chosen, "unscored": "" if scored.status_code == 200 else scored.json()["error"]["message"]}
        if request.query_params.get("part") == "benchmark":  # a pill was pressed: the figures of that benchmark alone
            return page(request, "model_benchmark.html", person, model=model, address=lambda label: node_model_address(request, label), **scores)
        if multimedia:
            profile = model[model['engine']]
            global_keys = PROFILE_GLOBALS[model['engine']]
            return page(request, 'multimedia_model.html', person, model=model, selected_node=selected_node,
                        node_choices=node_choices, installation=None, global_options={key: profile[key] for key in global_keys if key in profile},
                        node_options={key: value for key, value in profile.items() if key not in global_keys},
                        said=said or request.query_params.get('said',''), failed=failed, status=400 if failed else 200,
                        address=lambda label: node_model_address(request,label), file_address=file_address, **scores)
        # What the form shows: what was just typed if saving it failed, otherwise what is kept.
        showing = typed_in or {key: model[key] for key in SHOWN_SETTINGS if key in model}
        if 'engine_cards' in known:
            known['cards'] = known['engine_cards'].get(model.get('engine', 'llama_cpp'), known['cards'])
        # What the file says about itself, or why it could not be read.
        read_it = await model_api(request, person, "GET", "/loadcoach/file", params={"path": model["flags"]["model"]})
        told, unread = (read_it.json(), "") if read_it.status_code == 200 else (None, read_it.json()["error"]["message"])
        # The adapters and projectors there are, to choose from: each with whether its file says it was made for this model's file.
        files = await model_api(request, person, "GET", "/loadcoach/files")
        parts = [
            {"path": one["path"], "what": one["facts"]["what"], "says": one["facts"].get("name") or "", "made_for": one["facts"].get("made_for") or "",
             "fits": model["flags"]["model"] in [fit["path"] for fit in one.get("goes_with", [])]}
            for one in (files.json()["files"] if files.status_code == 200 else [])
            if one.get("facts", {}).get("what") in ("adapter", "projector")
        ]  # fmt: skip
        # Every flag the llama-server on compute knows, in its own words, or why that could not be had.
        listed = await model_api(request, person, "GET", "/loadcoach/flags", params={"engine": model.get("engine", "llama_cpp")})
        every = listed.json() if listed.status_code == 200 else {"flags": [], "ours": [], "why": listed.json().get("error", {}).get("message", "")}
        every["flags"] = usable_model_flags(every["flags"])
        setups = await model_api(request, person, "GET", "/loadcoach/model-setup", params={"name": name})
        setup = setups.json() if setups.status_code == 200 else {"presets": [], "undo": None, "can_undo": False, "why": setups.json().get("error", {}).get("message", "")}
        measure_nodes = node_measuring({"nodes": logical_nodes}) if logical_nodes else node_measuring(known, request.query_params.get("node", ""))
        measure = {"nodes": sorted(measure_plan(measure_nodes, model_copies(model, selected_node))),
                   "busy": any(one["measuring"] and not one["measuring"].get("ended") for one in measure_nodes.values())}
        columns = matrix_columns(name, model, selected_node, showing, known, logical_nodes, node_choices, typed_nodes or {},
                                 (told or {}).get("facts", {}).get("trained_context"))
        return page(request, "model.html", person, columns=columns,
                    targets=[node for node in node_choices if node["name"] not in {column["machine"] for column in columns}], status=400 if failed else 200, model=model, build=known.get("build"), parts=parts, showing=showing, known=known, told=told, every=every,
                    selected_node=selected_node, node_choices=node_choices, global_flags=GLOBAL_FLAGS, engine_schema=engine_for(model).schema(),
                    setup=setup, measure=measure, has_draft=typed_in is not None or bool(typed_nodes),
                    **scores,
                    suggestions=suggested(showing, known["defaults"], known["cards"]),
                    parallel=parallel_fields({**known["defaults"], **showing["flags"]}, (told or {}).get("facts", {}).get("trained_context")),
                    unusable_flags=UNUSABLE_MODEL_FLAGS,
                    unread=unread, said=said or request.query_params.get("said", ""), failed=failed, address=lambda label: node_model_address(request, label), file_address=file_address)  # fmt: skip

    async def preview_nodes(person: Person, form: dict[str, list[str]], name: str, made: dict, others: list[str], here: str) -> tuple[list, dict, str, bool]:
        """Preview the node matrix's other columns, each on its own node: the reviews, what was typed
        for each node, why it could not be done, and whether the shared settings change on any copy
        (then every copy is saved). Shared files are named as each node keeps them."""
        found = await ask("GET", "/turnstile/models", person.key)
        if not found.is_success:
            return [], {}, found.json()["error"]["message"], False
        copies = next((row["locations"] for row in found.json()["models"]
                       if any(copy["machine"] == here and copy["name"] == name for copy in row.get("locations", []))), [])
        kept = lambda copy: comparable(shared_values(model_configuration(copy)))  # noqa: E731
        ours = comparable(shared_values(made))
        reviews, typed, problems, inventory = [], {}, [], None
        drifted = any(kept(copy) != ours for copy in copies)
        for machine in others:
            copy = next((one for one in copies if one["machine"] == machine), None)
            if copy is None:
                problems.append(f"{machine} no longer has a copy of this model.")
                continue
            fields = node_form(form, machine)
            try:
                theirs = settings_from(fields)
            except ValueError as problem:
                problems.append(f"{machine}: {problem}")
                continue
            theirs["order"] = made["order"]
            if fields.get("parallel_controls"):
                defaults = (found.json().get("nodes", {}).get(machine) or {}).get("defaults") or {}
                suppressed = ("no-kv-unified",) if fields.get("parallel_enabled") else PARALLEL_FLAGS
                theirs["flags"].update({flag: None for flag in suppressed if flag in defaults})
            typed[machine] = theirs
            settings = deepcopy(theirs)
            parts = [settings["flags"].get("mmproj"), *(adapter["file"] for adapter in settings.get("adapters", {}).values())]
            if any(parts):
                if inventory is None:
                    listed = await ask("GET", "/turnstile/models/files", person.key)
                    inventory = listed.json().get("nodes", []) if listed.is_success else []
                missing = [path for path in parts if path and translate(path, here, machine, inventory) is None]
                if missing:
                    problems.append(f"{machine} has no copy of {', '.join(missing)}. Copy it there in Files first.")
                    continue
                if settings["flags"].get("mmproj"):
                    settings["flags"]["mmproj"] = translate(settings["flags"]["mmproj"], here, machine, inventory)
                for adapter in settings.get("adapters", {}).values():
                    adapter["file"] = translate(adapter["file"], here, machine, inventory)
            order = settings.pop("order")
            answer = await ask("POST", "/turnstile/machines/" + quote(machine, safe="") + "/model-api/model-setup", person.key,
                               json={"name": copy["name"], "action": "preview",
                                     "configuration": {"settings": settings, "order": 10 if order is None else order}})
            if answer.status_code != 200:
                problems.append(f"{machine}: {answer.json()['error']['message']}")
                continue
            reviews.append({"machine": machine, "name": copy["name"], "review": answer.json()})
        return reviews, typed, " ".join(problems), drifted

    async def apply_setup(request: Request, person: Person, name: str, source: dict) -> dict | str:
        """Check a configuration, preset or undo on the selected node and apply it at once, keeping an undo receipt.
        Returns what LoadCoach found, with a note of its warnings, or why it could not be applied."""
        checked = await model_api(request, person, "POST", "/loadcoach/model-setup", json={"name": name, "action": "preview", **source})
        if checked.status_code != 200:
            return checked.json()["error"]["message"]
        review = checked.json()
        if review["errors"]:
            return " ".join(review["errors"])
        if not review.get("changes"):
            return {**review, "note": "", "unchanged": True}
        answer = await model_api(request, person, "POST", "/loadcoach/model-setup",
                                 json={"name": name, "action": "apply", **source, "token": review["token"], "acknowledge": True})
        if answer.status_code != 200:
            return answer.json()["error"]["message"]
        return {**review, "note": warned(review), "unchanged": False}

    def warned(review: dict) -> str:
        """The warnings of a check, to show after saving."""
        return (" Note: " + " ".join(review["warnings"])) if review.get("warnings") else ""

    async def save_settings(request: Request, person: Person, form: dict[str, list[str]], name: str, made: dict) -> Response:
        """Save the node matrix: the selected node's column, and every other node whose column changed, together.
        A shared setting that changes is saved on every copy."""
        candidate = {"settings": {k: v for k, v in made.items() if k != "order"}, "order": FIRST_PLACE if made["order"] is None else made["order"]}
        others = matrix_nodes(form)
        def done(where: list[str], note: str = "") -> RedirectResponse:
            text = "No changes to save." if not where else "Saved." if where == [here] else "Saved on " + (
                where[0] if len(where) == 1 else ", ".join(where[:-1]) + " and " + where[-1]) + "."
            return go(node_model_address(request, name) + ("&said=" if request.query_params.get("node") else "?said=") + quote(text + note))
        here = request.query_params.get("node") or "compute"
        if not others:
            applied = await apply_setup(request, person, name, {"configuration": candidate})
            if isinstance(applied, str):
                return await model_page(request, person, applied, failed=True, typed_in=made)
            return done([] if applied["unchanged"] else [here], applied["note"])
        checked = await model_api(request, person, "POST", "/loadcoach/model-setup", json={"name": name, "action": "preview", "configuration": candidate})
        if checked.status_code != 200:
            return await model_page(request, person, checked.json()["error"]["message"], failed=True, typed_in=made)
        reviewed, typed_nodes, problem, drifted = await preview_nodes(person, form, name, made, others, here)
        if problem:
            return await model_page(request, person, problem, failed=True, typed_in=made, typed_nodes=typed_nodes)
        reviews = [{"machine": here, "name": name, "review": checked.json()}, *reviewed]
        changed = [one for one in reviews if one["review"].get("changes")]
        if drifted and any(one["machine"] != here for one in changed):  # a shared setting changes, so every copy is saved with it
            changed = reviews
        refused = [f"{one['machine']}: {' '.join(one['review']['errors'])}" for one in reviews if one["review"].get("errors")]
        if refused:
            return await model_page(request, person, " ".join(refused), failed=True, typed_in=made, typed_nodes=typed_nodes)
        note = "".join(warned(one["review"]) for one in changed)
        if not any(one["machine"] != here for one in changed):  # this node alone: turnstile keeps the shared settings in step
            applied = await apply_setup(request, person, name, {"configuration": candidate})
            if isinstance(applied, str):
                return await model_page(request, person, applied, failed=True, typed_in=made, typed_nodes=typed_nodes)
            return done([] if applied["unchanged"] else [here], applied["note"])
        answer = await ask("POST", "/turnstile/models/configure", person.key, timeout=None, json={"acknowledge": True, "changes": [
            {"machine": one["machine"], "name": one["name"], "configuration": one["review"]["candidate"], "token": one["review"]["token"]}
            for one in changed]})
        if answer.status_code != 200:
            return await model_page(request, person, answer.json()["error"]["message"], failed=True, typed_in=made, typed_nodes=typed_nodes)
        return done(answer.json()["applied"], note)

    async def change_model(request: Request, person: Person) -> Response:
        """Validate and submit one engine-specific model configuration update."""
        name, form = request.path_params["name"], await typed_lists(request)
        where = f"/loadcoach/models/{quote(name, safe='/:')}"
        action = form.get("do", [""])[-1]
        if action in ("location-add", "save-multimedia"):
            known = await all_models(request, person)
            if isinstance(known, Response):
                return known
            model = next((row for row in known['models'] if row['name'] == name), None)
            if model is None:
                return await model_page(request, person, "The source model is no longer available.", True)
            candidate = model_configuration(model)
            if action == "location-add":
                destinations = list(dict.fromkeys(one for one in form.get('machine', []) if one))
                move = form.get('transfer', ['copy'])[-1] == 'move'
                if not destinations:
                    return await model_page(request, person, "Choose a node to copy the model to.", True)
                if move and len(destinations) != 1:
                    return await model_page(request, person, "A move takes the model to one node. Choose one, or copy it instead.", True)
                # Loading choices belong to the destination. Start with portable defaults.
                engine = candidate.get('engine', 'llama_cpp')
                candidate['gpus'] = 0 if form.get('device', ['gpu'])[-1] == 'cpu' else 'any' if engine == 'llama_cpp' else 1
                candidate.pop('allow_split', None)
                candidate['flags'] = {key: value for key, value in candidate['flags'].items() if key not in ('tensor-split', 'split-mode', 'main-gpu')}
                if engine == 'sdcpp':
                    candidate['sdcpp']['split_mode'] = 'single'
                    for key in ('layer_weights', 'vram_limit_gib'):
                        candidate['sdcpp'].pop(key, None)
                answer = await ask('POST', '/turnstile/models/install', person.key, json={
                    'name': name, 'settings': candidate, 'source_model_id': model.get('model_id'),
                    **({'machines': destinations} if len(destinations) > 1 else {'machine': destinations[0]}),
                    **({'move': True} if move else {}),
                    'generation': generation_settings(engine_for(model).model.effective(model_configuration(model), known.get('defaults') or {})),
                    'source_machine': request.query_params.get('node') or (model.get('locations') or [{'machine':'compute'}])[0]['machine']}, timeout=None)
                if answer.status_code == 202:
                    kind = {'sdcpp':'image','audiocpp':'audio'}.get(engine,'llm')
                    jobs = answer.json().get('jobs') or [answer.json()]
                    return go('/models?' + urlencode([('kind', kind), *(('install', job['id']) for job in jobs)]))
            else:
                try:
                    engine = candidate['engine']
                    loading = json.loads(form.get('node_options', ['{}'])[-1])
                    shared = json.loads(form.get('global_options', ['{}'])[-1])
                    if not isinstance(loading, dict) or not isinstance(shared, dict) or shared.keys() - set(PROFILE_GLOBALS[engine]) or loading.keys() & set(PROFILE_GLOBALS[engine]):
                        raise ValueError('Keep generation defaults in Global settings and loading options in Node settings.')
                    candidate[engine] = {**loading, **shared}
                    candidate['gpus'] = read(form.get('gpus', ['1'])[-1])
                    candidate['alone'] = bool(form.get('alone'))
                except (ValueError, KeyError, TypeError) as problem:
                    return await model_page(request, person, str(problem), True)
                answer = await model_api(request, person, 'POST', where, json=candidate)
                if answer.is_success:
                    return go(node_model_address(request, name) + ('&' if request.query_params.get('node') else '?') + 'said=Saved.')
            return await model_page(request, person, answer.json()['error']['message'], True)
        if action in ("location-enable", "location-disable"):
            machine = form.get("machine", [""])[-1]
            model_id = form.get("model_id", [""])[-1]
            answer = await ask("POST", "/turnstile/models/location", person.key,
                               json={"machine": machine, "model_id": model_id,
                                     "enabled": action == "location-enable"})
            if answer.status_code != 200:
                return await model_page(request, person, answer.json()["error"]["message"], failed=True)
            state = "Enabled" if action == "location-enable" else "Disabled"
            return go(node_model_address(request, name) + ("&said=" if request.query_params.get("node") else "?said=") + quote(f"{state} {name} on {machine}."))
        def said(text: str) -> RedirectResponse:
            return go(node_model_address(request, name) + ("&said=" if request.query_params.get("node") else "?said=") + quote(text))
        if action == "delete-preset":
            answer = await model_api(request, person, "POST", "/loadcoach/model-setup",
                                     json={"name": name, "action": "delete-preset", "preset": form.get("preset", [""])[-1]})
            if answer.status_code != 200:
                return await model_page(request, person, answer.json()["error"]["message"], failed=True)
            return said("Preset deleted.")
        if action == "load-preset":
            # A preset fills the settings in, adapted to this model's own file, projector and adapters. Save applies it.
            label = form.get("preset", [""])[-1]
            answer = await model_api(request, person, "POST", "/loadcoach/model-setup", json={"name": name, "action": "preview", "preset": label})
            if answer.status_code != 200:
                return await model_page(request, person, answer.json()["error"]["message"], failed=True)
            candidate = answer.json()["candidate"]
            return await model_page(request, person, f"Loaded the preset {label}. Save to apply it.", typed_in={**candidate["settings"], "order": candidate["order"]})
        if action == "undo":
            applied = await apply_setup(request, person, name, {"undo": form.get("undo", [""])[-1]})
            if isinstance(applied, str):
                return await model_page(request, person, applied, failed=True)
            return said("Configuration restored." + applied["note"])
        if form.get("do", [""])[-1] == "remove":
            answer = await model_api(request, person, "DELETE", where)
            if answer.status_code != 200:
                return await model_page(request, person, answer.json()["error"]["message"], failed=True)
            left = answer.json()["still_finishing"]
            finishing = f" {left} request{'' if left == 1 else 's'} for it will still be answered, and then it is unloaded." if left else ""
            return go("/models?said=" + quote(f"Removed {name}.{finishing} Its file is left where it is."))
        if form.get("do", [""])[-1] == "measure":
            # Every copy of this model, on whichever node, that has no measure at its minimum and maximum context.
            node = request.query_params.get("node") or "compute"
            logical = await ask("GET", "/turnstile/models", person.key)
            if logical.is_success:
                catalogue = logical.json()
                row = next((row for row in catalogue["models"] if any(place["machine"] == node and place.get("name") == name
                                                                       for place in row.get("locations", []))), None)
                nodes, merged = node_measuring(catalogue), True
            else:
                catalogue = await all_models(request, person)
                if isinstance(catalogue, Response):
                    return catalogue
                row = next((row for row in catalogue["models"] if row["name"] == name), None)
                nodes, merged = node_measuring(catalogue, request.query_params.get("node", "")), bool(request.query_params.get("node"))
            if row is None:
                return page(request, "refused.html", person, status=404, why=f"There is no model named {name}.")
            refused = await measure_on_nodes(person, nodes, False, model_copies(row, node), merged=merged)
            if refused:
                return await model_page(request, person, refused, failed=True)
            return go(node_model_address(request, name) + ("&said=" if request.query_params.get("node") else "?said=") + quote(
                f"Measuring {name}'s memory at its minimum and maximum context. It gives way as soon as anyone asks for a model; the Models list shows how far it has got."))
        if form.get("do", [""])[-1] == "forget-measure":
            await model_api(request, person, "POST", "/loadcoach/model-forget-measure", json={"name": name})
            return go(node_model_address(request, name) + ("&said=" if request.query_params.get("node") else "?said=") + quote(f"Forgot what {name} measured. The next time it loads it has its cards to itself, and is measured afresh."))
        if form.get("do", [""])[-1] == "rename":
            new = form.get("to", [""])[-1].strip()
            answer = await model_api(request, person, "POST", "/loadcoach/model-rename", json={"name": name, "to": new})
            if answer.status_code != 200:
                return await model_page(request, person, answer.json()["error"]["message"], failed=True)
            followed = answer.json()["followed"]
            also = f" {', '.join(followed)} now list{'s' if len(followed) == 1 else ''} it by its new name." if followed else ""
            return go(node_model_address(request, new) + ("&said=" if request.query_params.get("node") else "?said=") + quote(f"Renamed {name} to {new}. Whatever asks for it must use the new name from now on.{also}"))
        try:
            made = settings_from(form)
        except ValueError as problem:
            return await model_page(request, person, str(problem), failed=True)
        # The sort order is saved with the settings and is not one of them. A box left empty puts it back at what every model starts with.
        order = form.get("order", [""])[-1].strip()
        made["order"] = read(order) if order else None
        if form.get("parallel_controls"):
            known = await all_models(request, person)
            if isinstance(known, Response):
                return known
            # Disabled controls suppress defaults; enabled controls clear an inherited
            # negative pool flag so it cannot contradict the checkbox's chosen value.
            suppressed = ("no-kv-unified",) if form.get("parallel_enabled") else PARALLEL_FLAGS
            made["flags"].update({flag: None for flag in suppressed if flag in known["defaults"]})
        if action == "save-preset":
            label = form.get("preset", [""])[-1].strip()
            if not label:
                return await model_page(request, person, "Give the preset a name.", failed=True, typed_in=made)
            candidate = {"settings": {k: v for k, v in made.items() if k != "order"}, "order": FIRST_PLACE if made["order"] is None else made["order"]}
            checked = await model_api(request, person, "POST", "/loadcoach/model-setup", json={"name": name, "action": "preview", "configuration": candidate})
            if checked.status_code == 200 and checked.json()["errors"]:
                return await model_page(request, person, " ".join(checked.json()["errors"]), failed=True, typed_in=made)
            answer = checked if checked.status_code != 200 else await model_api(request, person, "POST", "/loadcoach/model-setup", json={
                "name": name, "action": "save-preset", "preset": label, "configuration": candidate, "token": checked.json()["token"],
                "acknowledge": True, "replace": bool(form.get("replace"))})
            if answer.status_code != 200:
                return await model_page(request, person, answer.json()["error"]["message"], failed=True, typed_in=made)
            return await model_page(request, person, f"Saved the preset {label}.", typed_in=made)
        if action == "save-settings":
            return await save_settings(request, person, form, name, made)
        answer = await model_api(request, person, "POST", where, json=made)
        if answer.status_code != 200:
            return await model_page(request, person, answer.json()["error"]["message"], failed=True, typed_in=made)
        return go(node_model_address(request, name) + ("&said=" if request.query_params.get("node") else "?said=") + quote("Saved."))

    # ---- batches -----------------------------------------------------------------------------

    async def batches_page(request: Request, person: Person, said: str = "", failed: bool = False) -> Response:
        """The batches this person may see: their own, or everyone's for someone who may see all requests."""
        found = await fetch(request, person, "/loadcoach/batches")
        if isinstance(found, Response):
            return found
        template = "batches_list.html" if request.query_params.get("part") == "list" else "batches.html"  # the list asking for itself again
        limit, current = controls(request.query_params)
        current = min(current, max(1, (len(found['batches']) + limit - 1) // limit))
        return page(request, template, person, status=400 if failed else 200,
                    batches=found['batches'][(current - 1) * limit:current * limit],
                    paging=pagination('/batches', request.query_params, len(found['batches']), limit, current),
                    said=said or request.query_params.get("said", ""), failed=failed)

    async def batch_do(request: Request, person: Person) -> Response:
        """Cancel a batch. What was answered so far is kept, and the rest is not asked."""
        form = await typed(request)
        if form.get("do") == "cancel-all":
            found = await fetch(request, person, "/loadcoach/batches")
            if isinstance(found, Response):
                return found
            cancelled, failed = 0, 0
            for batch in found['batches']:
                if batch['status'] != 'in_progress' or (batch['user'] != person.name and 'stop' not in person.can):
                    continue
                answer = await ask("POST", f"/v1/batches/{quote(batch['id'], safe='')}/cancel", person.key)
                if answer.status_code == 200:
                    cancelled += 1
                else:
                    failed += 1
            message = f"Stopped {cancelled} {'batch' if cancelled == 1 else 'batches'}. Completed answers are kept."
            if failed:
                return await batches_page(request, person, message + f" Could not stop {failed} batches.", failed=True)
            return go("/batches?said=" + quote(message))
        number = form.get("batch", "")
        answer = await ask("POST", f"/v1/batches/{quote(number, safe='')}/cancel", person.key)
        if answer.status_code != 200:
            return await batches_page(request, person, answer.json()["error"]["message"], failed=True)
        return go("/batches?said=" + quote("Cancelled. The answers there were are kept, and the rest is not asked."))

    async def batch_file(request: Request, person: Person) -> Response:
        """A batch's answers, or its errors, as a file to keep: one line for each request."""
        number = request.path_params["number"]
        answer = await ask("GET", f"/v1/files/{quote(number, safe='')}/content", person.key)
        if answer.status_code != 200:
            return page(request, "refused.html", person, status=answer.status_code, why=answer.json()["error"]["message"])
        return Response(answer.content, media_type="application/jsonl", headers={"content-disposition": f'attachment; filename="{number}.jsonl"'})

    # ---- cloud APIs ---------------------------------------------------------------------------

    async def cloud_apis_page(request: Request, person: Person, said: str = "", failed: bool = False) -> Response:
        """The cloud APIs, each with its models here. With `?offers=NAME`, also what that service itself says it has, to tick from."""
        found = await fetch(request, person, "/loadcoach/services")
        if isinstance(found, Response):
            return found
        asked, offers, why = request.query_params.get("offers", ""), [], ""
        if asked:
            answer = await ask("GET", f"/loadcoach/services/{quote(asked, safe='')}/models", person.key)
            offers, why = (answer.json()["models"], "") if answer.status_code == 200 else ([], answer.json()["error"]["message"])
        return page(request, "outside.html", person, status=400 if failed else 200, services=found["services"], asked=asked, offers=offers, why=why,
                    said=said or request.query_params.get("said", ""), failed=failed)  # fmt: skip

    async def cloud_apis_do(request: Request, person: Person) -> Response:
        """Manage a cloud API, its credential and its offered models."""
        form = await typed_lists(request)
        one = lambda field: form.get(field, [""])[-1].strip()  # noqa: E731
        name, do, where = one("name"), one("do"), f"/loadcoach/services/{quote(one('name'), safe='')}"
        if do == "key":
            answer = await ask("POST", where + "/key", person.key, json={"key": one("api_key")})
            done = f"API key saved for {name}."
        elif do == "remove":
            answer, done = await ask("DELETE", where, person.key), f"Removed {name}. Its models cannot be asked for any more."
        else:
            # LoadCoach is given a service whole. What this form does not carry is taken from the service as it is.
            known = await ask("GET", "/loadcoach/services", person.key)
            now = next((service for service in known.json().get("services", []) if service["name"] == name), {})
            models, url, ceiling = dict(now.get("models", {})), now.get("url", ""), now.get("ceiling")
            if do in ("add", "save"):
                url, ceiling = one("url"), read(one("ceiling")) if one("ceiling") else None
            if do == "models":  # the table of its models, as it was sent: a row with a name emptied is a model taken away
                models = {}
                for ours, theirs, cost_in, cost_out in zip(form.get("ours", []), form.get("theirs", []), form.get("in", []), form.get("out", [])):
                    if ours.strip() and theirs.strip():
                        models[ours.strip()] = {"theirs": theirs.strip()} | {way: read(text.strip()) for way, text in (("in", cost_in), ("out", cost_out)) if text.strip()}
            if do == "offer":  # ticked in the service's own list: each is offered under the service's name for it, to be renamed afterwards
                have = [model["theirs"] for model in models.values()]
                models |= {theirs: {"theirs": theirs} for theirs in form.get("offer", []) if theirs not in have}
            body = {"url": url, "models": models} | ({"ceiling": ceiling} if ceiling is not None else {})
            answer = await ask("POST", where, person.key, json=body, params={"new": "1"} if do == "add" else None)
            done = f"Added {name}. Install its API key below, then ask it what it has." if do == "add" else "Saved."
        if not answer.is_success:
            return await cloud_apis_page(request, person, answer.json()["error"]["message"], failed=True)
        return go("/cloud-apis?said=" + quote(done))

    async def outside_redirect(request: Request, person: Person) -> Response:
        """Keep old bookmarks working while making Cloud APIs the canonical page."""
        query = ("?" + request.url.query) if request.url.query else ""
        return go("/cloud-apis" + query)

    # ---- roles --------------------------------------------------------------------------------

    def asked_as(person: Person, of: list[str] | None = None) -> list[str]:
        """Every way this person can ask for a role: by its name, and by its name with each strength. `of` narrows it to some roles."""
        return [role + strength for role in (of if of is not None else person.me.get("roles", [])) for strength in ("", ":best", ":available", ":fast")]

    async def roles_page(request: Request, person: Person, said: str = "", failed: bool = False, chosen: str = "") -> Response:
        """The roles as pills, and for the chosen one its models and their weights, and what it would pick at this moment, each way of asking, and why."""
        published = await ask("POST", "/benchmarks/publish", person.key)
        found = await fetch(request, person, "/loadcoach/roles")
        if isinstance(found, Response):
            return found
        names = [role["name"] for role in found["roles"]]
        chosen = chosen or request.query_params.get("role", "")
        role = next((one for one in found["roles"] if one["name"] == chosen), found["roles"][0] if found["roles"] else None)
        picks = {}
        if role:
            role['benchmark_shares'] = benchmark_shares(role.get('benchmarks', {}))
            for asked in asked_as(person, [role["name"]]):
                answer = await ask("GET", "/loadcoach/pick", person.key, params={"model": asked})
                picks[asked] = answer.json() if answer.status_code == 200 else {"picked": None, "because": answer.json()["error"]["message"], "models": []}
        return page(request, "roles.html", person, status=400 if failed else 200, names=names, role=role, picks=picks, models=person.me.get("models", []),
                    said=said or request.query_params.get("said", ""), failed=failed, address=model_address,
                    benchmark_definitions=[{**one, 'summary': next((definition['summary'] for definition in benchmarks.state.store.definitions() if definition['id'] == one['id']), '')} for one in found.get("benchmarks", [])],
                    benchmark_presets={"coding": {"code_retrieval": 4, "json_schema": 2, "ifeval": 2, "extraction": 1, "arithmetic": 1},
                                       "chat": {"ifeval": 4, "retrieval": 2, "speed": 2}},
                    publication_warning="" if published.is_success else "Evidence could not be refreshed. LoadCoach keeps the last accepted snapshot.")  # fmt: skip

    async def roles_do(request: Request, person: Person) -> Response:
        """Add a role, set the weights of its models, or remove it. A model whose weight is emptied is taken out of the role."""
        form = await typed_lists(request)
        name, do = form.get("name", [""])[-1].strip(), form.get("do", [""])[-1]
        where = f"/loadcoach/roles/{quote(name, safe='')}"
        if do == "remove":
            answer, done = await ask("DELETE", where, person.key), f"Removed the role {name}."
        else:
            models = {model: read(weight.strip()) for model, weight in zip(form.get("model", []), form.get("weight", [])) if model and weight.strip()}
            payload = {"models": models}
            try:
                check_weights(models)
                for key in ('loaded_bonus', 'fast_loaded_bonus'):
                    if key in form:
                        payload[key] = read(form[key][-1])
                check_loaded_bonuses(payload)
                if "evidence-settings" in form:
                    benchmarks = {key: read(weight.strip()) for key, weight in zip(form.get("benchmark", []), form.get("benchmark-weight", [])) if key and weight.strip() and read(weight.strip()) != 0}
                    check_weights(benchmarks)
                    payload.update(benchmarks=benchmarks, missing=form.get("missing", ["manual"])[-1], stale=form.get("stale", ["warn"])[-1],
                                   max_age_days=read(form.get("max_age_days", ["0"])[-1]),
                                   requires={"pictures": "requires-pictures" in form, "tools": "requires-tools" in form, "stays": "requires-stays" in form,
                                             "tokens": read(form.get("requires-tokens", ["0"])[-1])})
            except ValueError as problem:
                return await roles_page(request, person, str(problem), failed=True, chosen=name)
            answer, done = await ask("POST", where, person.key, json=payload, params={"new": "1"} if do == "add" else None), "Saved."
        if not answer.is_success:
            return await roles_page(request, person, answer.json()["error"]["message"], failed=True, chosen=name)
        return go("/roles?said=" + quote(done) + ("" if do == "remove" else "&role=" + quote(name, safe="")))

    # ---- backup and recovery ---------------------------------------------------------------

    def front_recovery_stores() -> list[RecoveryStore]:
        """Enumerate the configured stores the front keeps, for paired recovery sets."""
        stores = []
        for bound in database_registry.bound_stores():
            if not bound.bindings:
                continue
            archive = ""
            if bound.database.path is not None and bound.database.path.parent.resolve() == data.resolve():
                archive = f"data/{bound.database.path.name}"
            bindings = bound.bindings
            if 'weightroom.accounts' in bindings and settings.toollocker_runs_on_front:
                bindings = (*bindings, 'toollocker.identity')
            stores.append(RecoveryStore(f"weightroom.{bound.name}", bound.database, bindings, archive))
        return stores

    def front_backup_context() -> tuple[Path, list, list[dict], list[dict], list[RecoveryStore]]:
        """Assemble front backup inputs, exclusions and service ownership."""
        stores = front_recovery_stores()
        paths = [store.database.path for store in stores if store.database.path is not None]
        files, excluded = front_plan(settings.data, settings.path, settings.plugins_dir, paths)
        rows = [*store_inventory(stores), *backup_inventory(files, excluded)]
        return backup_root(settings.data), files, excluded, rows, stores

    def recovery_sets(front: list[dict], remote: list[dict]) -> list[dict]:
        """Join the independently stored machine archives by their shared recovery-set ID."""
        joined: dict[str, dict] = {}
        for machine, rows in (("front", front), ("compute", remote)):
            for row in rows:
                joined.setdefault(row["id"], {"id": row["id"]})[machine] = row
        answer = []
        for backup_id, row in joined.items():
            halves = [row.get("front"), row.get("compute")]
            verified = [part for part in halves if part and part.get("verification", {}).get("state") == "verified"]
            legacy = any(part and part.get("verification", {}).get("state") == "legacy-unencrypted" for part in halves)
            state = "verified" if len(verified) == 2 else "partial" if verified else "legacy-unencrypted" if legacy else "failed"
            counts: dict[str, int] = {}
            for part in verified:
                counts.update(part["verification"].get("counts", {}))
            answer.append(
                {
                    **row,
                    "id": backup_id,
                    "state": state,
                    "created_at": max((part.get("created_at", "") for part in halves if part), default=""),
                    "archive_size": sum(part.get("archive_size", 0) for part in halves if part),
                    "payload_size": sum(part.get("payload_size", 0) for part in halves if part),
                    "counts": counts,
                }
            )
        return sorted(answer, key=lambda item: (item['created_at'], item['id']), reverse=True)

    async def backups_page(request: Request, person: Person, said: str = "", failed: bool = False) -> Response:
        """Render retained paired recovery sets and their verification state."""
        if "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="You may not manage backups.")
        root, _, excluded, local_inventory, _ = await asyncio.to_thread(front_backup_context)
        remote_error = ""
        try:
            answer = await ask("GET", "/loadcoach/backups", person.key)
            if answer.status_code == 200:
                remote = answer.json()
            else:
                remote, remote_error = {"inventory": [], "backups": [], "archive_root": ""}, answer.json()["error"]["message"]
        except ComputeOff:
            remote, remote_error = {"inventory": [], "backups": [], "archive_root": ""}, "Compute is not answering. Front backups remain visible."
        sets = recovery_sets(backup_manifests(root), remote["backups"])
        last_verified = next((one for one in sets if one["state"] == "verified"), None)
        return page(
            request,
            "backups.html",
            person,
            status=400 if failed else 200,
            said=said or request.query_params.get("said", ""),
            failed=failed,
            retention=settings.backup_retention,
            sets=sets,
            last_verified=last_verified,
            front_inventory=local_inventory,
            front_excluded=excluded,
            front_root=str(root),
            compute_inventory=remote["inventory"],
            compute_root=remote["archive_root"],
            compute_error=remote_error,
            front_protection=await asyncio.to_thread(encryption_status, root),
            compute_protection=remote.get("protection", {"ready": False, "reason": "Compute encryption readiness is unavailable."}),
        )

    async def backup_create(request: Request, person: Person) -> Response:
        """Create and verify a paired front and compute recovery set."""
        if "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="You may not manage backups.")
        try:
            await asyncio.to_thread(backup_recipients, backup_root(settings.data))
        except (OSError, ValueError, subprocess.SubprocessError) as problem:
            return await backups_page(request, person, str(problem), failed=True)
        backup_id = new_backup_id()
        try:
            remote = await ask("POST", "/loadcoach/backups", person.key,
                               json={"id": backup_id, "retention": settings.backup_retention})
        except ComputeOff:
            return await backups_page(request, person, "Compute is not answering, so no recovery set was created.", failed=True)
        if remote.status_code != 201:
            return await backups_page(request, person, remote.json()["error"]["message"], failed=True)
        root, files, excluded, rows, stores = front_backup_context()
        try:
            await asyncio.to_thread(
                create_backup,
                root,
                backup_id,
                "front",
                files,
                rows,
                excluded,
                ["weightroom.service", "turnstile.service", "voice-gateway.service",
                 *(["toollocker.service"] if settings.toollocker_runs_on_front else [])],
                settings.backup_retention,
                stores=stores,
            )
        except (OSError, ValueError, TypeError, sqlite3.Error, subprocess.SubprocessError, RuntimeError) as problem:
            return await backups_page(
                request,
                person,
                f"Compute was backed up, but the front backup failed: {problem}. The set is shown as partial.",
                failed=True,
            )
        return go("/backups?said=" + quote(f"Recovery set {backup_id} was created and restored into isolated folders successfully."))

    async def backup_retention(request: Request, person: Person) -> Response:
        """Validate and save the configured recovery-set retention limit."""
        nonlocal settings
        if "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="You may not manage backups.")
        raw = (await typed(request)).get("retention", "").strip()
        try:
            retention = int(raw)
            if not 0 <= retention <= 100:
                raise ValueError
        except ValueError:
            return await backups_page(request, person, "Retention must be a whole number from 0 through 100.", failed=True)
        answer = await ask("POST", "/loadcoach/settings/weightroom", person.key, json={"backup_retention": retention})
        if answer.status_code != 200:
            return await backups_page(request, person, answer.json()["error"]["message"], failed=True)
        settings = replace(settings, backup_retention=retention)
        return go("/backups?said=" + quote("Retention saved. It is applied after each successful backup."))

    async def backup_preview(request: Request, person: Person) -> Response:
        """Render a recovery manifest and its restore preview without switching stores."""
        if "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="You may not manage backups.")
        backup_id = request.path_params["backup_id"]
        root, _, _, _, _ = front_backup_context()
        front = backup_manifest(root, backup_id)
        compute_part, compute_error = None, ""
        try:
            answer = await ask("GET", f"/loadcoach/backups/{backup_id}", person.key)
            if answer.status_code == 200:
                compute_part = answer.json()
            elif answer.status_code != 404:
                compute_error = answer.json()["error"]["message"]
        except ComputeOff:
            compute_error = "Compute is not answering, so its preview is unavailable."
        if front is None and compute_part is None:
            return page(request, "refused.html", person, status=404, why="There is no such recovery set.")
        return page(request, "backup.html", person, backup_id=backup_id, front=front, compute_part=compute_part, compute_error=compute_error)

    # ---- database inventory and exports -----------------------------------------------------

    def front_database_sources():
        """Bound front database IDs to configured stores and discovered SQLite files."""
        return configured_sources(
            database_registry,
            data,
            {
                "accounts.db": "Users, keys, groups and the log of what was done to them",
                "benchmarks.db": "Benchmark definitions, plans, runs, samples and evidence",
                "chat.db": "WeightRoom private chats, collections, prompt versions and workflows",
                "plugins.db": "Plugin usage records",
                "alerts.db": "Operational alert episodes and source freshness",
                "schedules.db": "Recurring schedules and durable occurrence history",
            },
        )

    def migration_locations():
        """List the configured migration profiles available to the operator."""
        return (Path(settings.path).parent / "migrations" if settings.path else data / "migration-profiles",
                data / "migrations")

    async def database_migration_preview(request: Request, person: Person) -> Response:
        """Validate the selected profile and render proposed store-binding changes."""
        if "manage-settings" not in person.can:
            return Response("You may not migrate databases.", status_code=403)
        folder, _ = migration_locations()
        name = request.path_params["profile"]
        try:
            profile = migration_profile(folder, name)
            preview = await asyncio.to_thread(migration_preview, profile)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
            # Do not return arbitrary driver/configuration errors containing secret values.
            why = str(problem) if isinstance(problem, MigrationError) else "Preflight failed. Check the profile, SSH, database connectivity and native tools."
            return page(request, "database_migration.html", person, status=400, profile=name, preview=None, why=why)
        return page(request, "database_migration.html", person, profile=name, preview=preview, why="")

    async def database_migration_start(request: Request, person: Person) -> Response:
        """Start the reviewed migration using the existing migration coordinator."""
        if "manage-settings" not in person.can:
            return Response("You may not migrate databases.", status_code=403)
        folder, reports = migration_locations()
        name = request.path_params["profile"]
        approval = (await typed(request)).get("approval", "")
        try:
            job = await asyncio.to_thread(migration_launch, folder, name, approval, reports)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
            why = str(problem) if isinstance(problem, MigrationError) else "Migration could not start. Another job may hold the lease; inspect the migration report and user service journal."
            return page(request, "database_migration.html", person, status=409, profile=name, preview=None, why=why)
        return go("/databases?said=" + quote(f"Migration {job} started. The console will close during maintenance; return here after services restart."))

    async def front_store_health():
        """Probe front stores independently of compute and reuse recorded recovery evidence."""
        return await async_stores_health(front_recovery_stores(), 'front', backup_manifests(backup_root(settings.data)))

    database_operations = DatabaseOperations(front_database_sources, data / 'database-maintenance', machine='front', recovery_prefix='weightroom.')
    database_console = DatabaseConsole(front_database_sources, front_store_health, database_operations, ask, loadcoach, data / 'database-jobs')

    async def databases_page(request: Request, person: Person) -> Response:
        """Operations desk over configured nodes and proven physical store identities."""
        if "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="You may not inspect databases.")
        snapshot = await database_console.snapshot(person)
        return page(request, 'databases.html', person, snapshot=snapshot,
                    databases=snapshot['databases'], store_health=snapshot['store_health'], store_health_error=snapshot['inventory_error'],
                    migration_profiles=migration_profiles(migration_locations()[0]),
                    migration_reports=migration_reports(migration_locations()[1]), said=request.query_params.get('said', ''))

    async def database_console_api(request: Request, person: Person) -> Response:
        return await database_console.api(request, person)

    async def database_node_operation(request: Request, person: Person) -> Response:
        if 'manage-settings' not in person.can:
            return JSONResponse({'error': {'message': 'Database operations require manage-settings.'}}, status_code=403)
        machine = request.path_params['machine']
        if machine == 'front':
            return await database_operations.handle(request, owner_of(person))
        operation = request.path_params['operation']
        if request.method == 'GET' and operation.endswith('/recovery'):
            try:
                return await database_console.stream(person, machine, operation)
            except (httpx.HTTPError, ValueError):
                return Response('The owning node cannot provide this recovery copy.', status_code=503)
        try:
            answer = await ask(request.method, database_console.remote_path(machine, operation), person.key,
                               **({'json': await request.json()} if request.method == 'POST' else {}))
            return Response(answer.content, status_code=answer.status_code, media_type=answer.headers.get('content-type'), headers={'cache-control': 'no-store'})
        except (ComputeOff, httpx.HTTPError):
            return JSONResponse({'error': {'message': 'The owning node is unavailable.'}}, status_code=503)

    async def database_details(request: Request, person: Person) -> Response:
        if 'manage-settings' not in person.can:
            return Response('Database details require manage-settings.', status_code=403)
        machine, identifier = request.path_params['machine'], request.path_params['database']
        if machine == 'front':
            delegated = Request({**request.scope, 'path_params': {'operation': 'details/' + identifier}}, request.receive)
            return await database_operations.handle(delegated, owner_of(person))
        try:
            answer = await ask('GET', database_console.remote_path(machine, 'stores/' + quote(identifier, safe='') + '/details'), person.key, timeout=60)
            return Response(answer.content, status_code=answer.status_code, media_type='application/json', headers={'cache-control': 'no-store'})
        except ComputeOff:
            return Response('The owning node is unavailable.', status_code=503)

    async def database_dump(request: Request, person: Person) -> Response:
        if 'manage-settings' not in person.can:
            return Response('Database exports require manage-settings.', status_code=403)
        machine, identifier = request.path_params['machine'], request.path_params['database']
        table = request.query_params.get('table') or None
        if machine == 'front':
            source = front_database_sources().get(identifier)
            if source is None:
                return Response('There is no such front database.', status_code=404)
            try:
                path, method = await asyncio.to_thread(make_sql_dump, source, table)
            except KeyError:
                return Response('There is no such table in that database.', status_code=404)
            except Exception:
                return Response('Database export failed. Check permissions, connectivity, disk space and native tools.', status_code=500)
            return FileResponse(path, media_type='application/sql', filename=safe_filename(source, table),
                headers={'x-suite-dump-method': method, 'cache-control': 'no-store'}, background=BackgroundTask(path.unlink, missing_ok=True))
        operation = 'stores/' + quote(identifier, safe='') + '/dump'
        if table:
            operation += '?' + urlencode({'table': table})
        try:
            return await database_console.stream(person, machine, operation, 'application/sql')
        except (ValueError, httpx.HTTPError):
            return Response('The owning node cannot export this database.', status_code=503)

    async def fingerprints_page(request: Request, person: Person) -> Response:
        """Browse archived configurations, preview their files, or inspect a permanent snapshot."""
        from .fingerprint_views import fingerprint_context

        found = await fetch(request, person, "/loadcoach/fingerprints")
        if isinstance(found, Response):
            return found
        context = fingerprint_context(found["fingerprints"], request.query_params)
        return page(request, "fingerprint_peek.html" if context['peek_requested'] else "fingerprints.html", person, **context)

    # ---- settings ---------------------------------------------------------------------------
    #
    # Every program's settings, as LoadCoach has them from the programs themselves. A value is
    # shown and typed the way its file would hold it. See loadcoach/settings.py.

    # ---- voice ------------------------------------------------------------------------------
    #
    # The gateway's own browser page, served here for the signed-in person: the gateway reserves
    # each call with their key, and its audio socket is carried through, so the browser never
    # reaches the gateway. Its cookie stays here, kept for the one person who asked for it.

    voice_calls: dict[str, tuple[str, str]] = {}  # session id -> (gateway cookie, person's name)

    def may_talk(person: Person) -> bool:
        """True for someone who may hold voice calls."""
        return bool(set(person.can) & {'front-voice', 'superuser'})

    async def voice_chat(request: Request, person: Person) -> Response:
        """The browser voice chat."""
        if not may_talk(person):
            return Response('Your account may not use voice.', status_code=403)
        return page(request, 'voice_chat.html', person)

    async def voice_download(request: Request, person: Person) -> Response:
        """The Voice desktop app download and its setup."""
        if not may_talk(person):
            return Response('Your account may not use voice.', status_code=403)
        return page(request, 'voice_download.html', person)

    async def voice_config(request: Request, person: Person) -> Response:
        """The gateway's modes and voices, which name no credential."""
        if not may_talk(person):
            return JSONResponse({'error': 'Your account may not use voice.'}, status_code=403)
        try:
            reply = await voice_http.get('/browser/config', headers={'authorization': 'Bearer ' + person.key}, timeout=10)
        except httpx.HTTPError:
            return JSONResponse({'error': 'Voice gateway is not answering'}, status_code=503)
        return Response(reply.content, status_code=reply.status_code, media_type='application/json')

    async def voice_start(request: Request, person: Person) -> Response:
        """Reserve one call at the gateway, as this person."""
        from voice.options import MAX_CALL_BYTES
        if not may_talk(person):
            return JSONResponse({'error': 'Your account may not use voice.'}, status_code=403)
        body = await request.body()
        if len(body) > MAX_CALL_BYTES:
            return JSONResponse({'error': 'Voice settings are too large.'}, status_code=413)
        try:
            reply = await voice_http.post('/browser/sessions', content=body, timeout=10,
                                          headers={'authorization': 'Bearer ' + person.key, 'content-type': 'application/json'})
        except httpx.HTTPError:
            return JSONResponse({'error': 'Voice gateway is not answering'}, status_code=503)
        if not reply.is_success:
            return Response(reply.content, status_code=reply.status_code, media_type='application/json')
        reserved = reply.json()
        voice_calls[reserved['session_id']] = (reserved['cookie'], person.name)
        return JSONResponse({'session_id': reserved['session_id']}, headers={'cache-control': 'no-store'})

    async def voice_stream(socket: WebSocket) -> None:
        """Carry one reserved call's audio and events between the browser and the gateway."""
        import websockets  # installed with voice-gateway, which front always has

        sid = socket.path_params['sid']
        try:
            person = await who(socket)
        except ComputeOff:
            person = None
        call = voice_calls.pop(sid, None)
        if person is None or not may_talk(person) or not ours(socket) or call is None or call[1] != person.name:
            await socket.close(code=1008)
            return
        address = urlsplit(settings.voice_gateway)
        try:
            gateway = await websockets.connect(f'ws://{address.netloc}/browser/sessions/{sid}/stream',
                                               additional_headers={'cookie': f'voice_{sid}={call[0]}'}, max_size=None)
        except (OSError, websockets.WebSocketException):
            await socket.close(code=1011)
            return
        await socket.accept()

        async def to_gateway():
            """The browser's microphone and controls, until it hangs up."""
            while True:
                said = await socket.receive()
                if said['type'] == 'websocket.disconnect':
                    return
                await gateway.send(said['bytes'] if said.get('bytes') is not None else said['text'])

        async def to_browser():
            """The gateway's speech and events, until it ends the call."""
            async for message in gateway:
                await (socket.send_bytes(message) if isinstance(message, bytes) else socket.send_text(message))

        async with gateway:
            ways = [asyncio.create_task(to_gateway()), asyncio.create_task(to_browser())]
            await asyncio.wait(ways, return_when=asyncio.FIRST_COMPLETED)
            for way in ways:
                way.cancel()
            await asyncio.gather(*ways, return_exceptions=True)
        with suppress(RuntimeError):
            await socket.close()

    async def voice_admin_request(person, action, method='GET', **more):
        """One private gateway, using the operator's existing session and fresh authorization."""
        return await voice_http.request(method, '/admin/' + action,
            headers={'authorization': 'Bearer ' + person.key}, **more)

    async def voice_admin_page(request: Request, person: Person, said='', failed=False, typed_in=None, opened='', **unused) -> Response:
        """Render current voice profiles, greetings and diagnostics."""
        if 'manage-settings' not in person.can:
            return Response('Your account may not manage voice.', status_code=403)
        status, problem, programs, models = {}, '', [], []
        try:
            reply = await voice_admin_request(person, 'status', timeout=10)
            if reply.is_success:
                status = reply.json()
            else:
                problem = f'Voice gateway administration is unavailable (HTTP {reply.status_code}).'
        except (httpx.HTTPError, ValueError):
            problem = 'Voice gateway is not answering. Check voice-gateway.service on front and its private address in Settings.'
        try:
            found = await ask('GET', '/loadcoach/settings', person.key)
            if found.is_success:
                catalog = await ask('GET', '/v1/models', person.key)
                models = [row['id'] for row in catalog.json().get('data', [])] if catalog.is_success else []
                for program in found.json()['programs']:
                    if program['program'] in ('voice', 'voice-gateway'):
                        fields = voice_settings.fields if program['program'] == 'voice' else voice_settings.gateway_fields
                        program['settings'] = fields(program['settings'], models)
                        programs.append(program)
            else:
                problem += f' Voice configuration is unavailable (HTTP {found.status_code}).'
        except ComputeOff:
            problem += ' Compute is unavailable; settings cannot be read or changed.'
        synthesis = next((program['settings'] for program in programs if program['program'] == 'voice'), [])
        return page(request, 'voice_status.html' if request.url.path == '/system/voice/status' else 'voice_admin.html', person, status=400 if failed else 200,
            voice_admin=True, voice_status=status, voice_problem=problem, speakers=voice_settings.speaker_choices(synthesis),
            programs=programs, said=said or request.query_params.get('said', ''), failed=failed,
            typed_in=typed_in or {}, opened=opened or request.query_params.get('open', 'voice'),
            greeting_names=list(voice_settings.TEXTS))

    async def voice_admin_do(request: Request, person: Person) -> Response:
        """Validate and submit an authorized voice administration operation."""
        if 'manage-settings' not in person.can:
            return Response('Your account may not manage voice.', status_code=403)
        action = request.path_params['action']
        if (action, request.method) not in (('recordings', 'GET'), ('preview', 'POST'), ('regenerate', 'POST')):
            return Response(status_code=404)
        form = await typed(request) if request.method == 'POST' else {}
        if action == 'regenerate':
            try:
                configured = await ask('GET', '/loadcoach/settings', person.key)
                if not configured.is_success:
                    return await voice_admin_page(request, person, 'Voice configuration cannot be checked. Try again when compute is ready.', failed=True)
                pending = [setting for program in configured.json()['programs'] if program['program'] == 'voice'
                           for setting in program['settings'] if setting['name'] in ('profiles', 'greetings')
                           and setting.get('changed') and setting['changed']['value'] != setting['value']]
                if pending:
                    return await voice_admin_page(request, person, 'Voice has not reported the saved speaker or greeting changes yet. Wait, refresh status, then regenerate.', failed=True)
            except ComputeOff:
                return await voice_admin_page(request, person, 'Compute is unavailable. Previous recordings remain on front.', failed=True)
        try:
            more = {}
            if action == 'preview':
                try:
                    speaker = int(form.get('speaker', ''))
                except ValueError:
                    return Response('Choose a voice.', status_code=400)
                more['json'] = {'speaker': speaker, 'text': form.get('text', '')}
            elif action == 'recordings':
                more['params'] = {'name': request.query_params.get('name', '')}
            result = await voice_admin_request(person, action, request.method, **more)
        except httpx.HTTPError:
            if action != 'regenerate':
                return Response('Voice service is unavailable. Try again when compute is ready.', status_code=503)
            return await voice_admin_page(request, person, 'Greeting regeneration did not finish. The previous recordings remain available; refresh status before retrying.', failed=True)
        if result.is_success:
            if action == 'regenerate':
                return go('/system/voice?said=' + quote('Regenerated all greetings and saved the complete set on front.'))
            return Response(result.content, media_type='audio/wav', headers={'cache-control': 'no-store'})
        message = 'Voice could not complete that action (HTTP ' + str(result.status_code) + ').'
        try:
            code = result.json().get('error', {}).get('code', '')
            if code:
                message += ' ' + code.replace('_', ' ') + '.'
        except (ValueError, AttributeError):
            pass
        if action == 'regenerate':
            return await voice_admin_page(request, person, message, failed=True)
        return Response(message, status_code=result.status_code)

    async def settings_page(request: Request, person: Person, said: str = "", failed: bool = False, typed_in: dict | None = None, opened: str = "", **unused) -> Response:
        """Render program settings with secret fields removed."""
        if 'manage-settings' not in person.can:
            return page(request, 'refused.html', person, status=403, why='Your account may not manage settings.')
        try:
            found = await fetch(request, person, "/loadcoach/settings")
        except ComputeOff:
            found = {'programs': []}
        if isinstance(found, Response):
            return found
        found["programs"] = [program for program in found["programs"] if program["program"] not in ("loadcoach", "spotter")]
        found['programs'] = [program for program in found['programs'] if program['program'] not in ('voice', 'voice-gateway')]
        for program in found['programs']:
            program['settings'] = [row for row in program['settings'] if row['name'] != 'image_adult_previews'
                                   and not (program['program'] == 'turnstile' and row['name'] in ('models_folder', 'models_storage_folder'))
                                   and not (program['program'] == 'weightroom' and row['name'].startswith('audio_') and row['name'].endswith('_model'))]
        asked = request.query_params
        machines_answer = await ask("GET", "/turnstile/machines", person.key)
        machine_report = machines_answer.json() if machines_answer.status_code == 200 else {}
        machine_choices = [{'name': 'front', 'state': 'ready'}, *machine_report.get("machines", [])]
        names = {row.get("name") for row in machine_choices}
        default_machine = machine_report.get("artifact_machine", "front_cpu" if "front_cpu" in names else "compute")
        selected_machine = (typed_in or {}).get("file_machine") or asked.get("file_machine", 'front')
        file_locations, location_error, file_pending, file_offline, file_delivery_error = {}, "", False, False, ''
        if selected_machine not in names:
            location_error = "That model-file machine is not configured."
        elif selected_machine == 'front':
            location_answer = await ask('GET', '/turnstile/artifacts/settings', person.key)
            if location_answer.is_success:
                file_locations = location_answer.json()['locations']
            else:
                location_error = 'Front file locations could not be read. Refresh this page to try again.'
        else:
            location_answer = await ask("GET", "/turnstile/machines/" + quote(selected_machine, safe="") + "/settings", person.key)
            if location_answer.status_code == 200:
                file_pending = location_answer.json().get('pending', False)
                file_delivery_error = location_answer.json().get('pending_error', '')
                file_offline = location_answer.json().get('unavailable', False)
                settings = next((row["settings"] for row in location_answer.json()["programs"] if row["program"] == "loadcoach"), [])
                for name in ("models_folder", "models_storage_folder"):
                    setting = next((row for row in settings if row["name"] == name), {})
                    file_locations[name] = setting.get("changed", {}).get("value") if setting.get("changed") else setting.get("value", "")
            else:
                location_error = f"LoadCoach {selected_machine} is not answering; its file paths cannot be changed now."
        return page(request, "settings.html", person, status=400 if failed else 200, programs=found["programs"], said=said or asked.get("said", ""), failed=failed,
                    typed_in=typed_in or {}, opened=opened or asked.get("open", ""), third_party_services=THIRD_PARTY_SERVICES,
                    selected_machine="", machine_choices=[], file_machine=selected_machine, file_machine_choices=machine_choices,
                    file_artifact_machine=default_machine, file_locations=file_locations, location_error=location_error,
                    file_pending=file_pending, file_offline=file_offline, file_delivery_error=file_delivery_error)  # fmt: skip

    async def image_settings_page(request: Request, person: Person) -> Response:
        """Edit the existing front-wide image policy without changing personal consent."""
        if 'manage-settings' not in person.can:
            return Response('You may not manage image settings.', status_code=403)
        if request.method == 'POST':
            form = await typed(request)
            answer = await ask('POST', '/loadcoach/settings/turnstile', person.key,
                               json={'image_adult_previews': form.get('adult_previews') == 'yes'})
            if not answer.is_success:
                return page(request, 'refused.html', person, status=answer.status_code,
                            why=answer.json()['error']['message'])
            return go('/settings/image?said=' + quote('Image settings saved.'))
        preferences = await fetch(request, person, '/mirrorwall/preferences')
        if isinstance(preferences, Response):
            return preferences
        return page(request, 'image_settings.html', person, preferences=preferences,
                    said=request.query_params.get('said', ''))

    async def change_file_locations(request: Request, person: Person) -> Response:
        """Validate and save the selected node's model-file storage roots."""
        form = await typed(request)
        machine = form.get("file_machine", "")
        cache, storage = form.get("models_folder", "").strip(), form.get("models_storage_folder", "").strip()
        if any(path and not path.startswith("/") for path in (cache, storage)):
            return await settings_page(request, person, "Cache and storage paths must be full paths, or empty.", True, typed_in=form)
        endpoint = '/turnstile/artifacts/settings' if machine == 'front' else "/turnstile/machines/" + quote(machine, safe="") + "/settings/loadcoach"
        answer = await ask("POST", endpoint, person.key,
                           json={"models_folder": cache, "models_storage_folder": storage})
        if answer.status_code not in (200, 202):
            return await settings_page(request, person, answer.json()["error"]["message"], True, typed_in=form)
        message = ('Saved. Front file locations are available now.' if machine == 'front' else
                   'Saved on front. The node will receive these paths when it reconnects; restart its compute service to apply them.' if answer.status_code == 202 else
                   'Saved. Restart LoadCoach on that machine to apply the file paths.')
        return go("/settings?file_machine=" + quote(machine, safe="") + "&said=" + quote(message))

    async def compute_nodes_page(request: Request, person: Person, said: str = "", failed: bool = False,
                                 typed_in: dict | None = None, opened: str = "", gpu_disabled: list | None = None,
                                 pairing_code: dict | None = None) -> Response:
        """Render configured compute nodes and their active engine availability."""
        form = typed_in or {}
        selected = form.get("machine") or request.query_params.get("machine")
        machines_answer = await ask("GET", "/turnstile/machines?include_retired=1", person.key)
        if machines_answer.status_code == 403:
            return page(request, "refused.html", person, status=403, why="Compute node settings require manage-settings.")
        if not machines_answer.is_success:
            return page(request, 'refused.html', person, status=machines_answer.status_code,
                        why='Compute node inventory is unavailable. Try again.')
        choices = machines_answer.json().get("machines", []) if machines_answer.status_code == 200 else []
        for row in choices:
            if row.get("lifecycle") == "retired":
                row["state"] = "monitoring stopped"
            elif row.get("state") == "ready" and row.get("status_live") is False:
                row["state"] = "not answering"
        if not selected:
            selected = 'compute' if any(row.get('name') == 'compute' for row in choices) else choices[0]['name'] if choices else ''
        if selected and selected not in {row.get("name") for row in choices}:
            return Response("That compute node is not configured.", status_code=404)
        if not choices:
            return page(request, 'compute_nodes.html', person, status=400 if failed else 200,
                        said=said or request.query_params.get('said', ''), failed=failed, selected_machine='',
                        selected_node=None, machine_choices=[], programs=[], typed_in=form, opened=opened or request.query_params.get('open', ''),
                        may_pair='superuser' in person.can, pairing_code=pairing_code)
        selected_node = next(row for row in choices if row["name"] == selected)
        if selected_node.get("lifecycle") == "retired" or not selected_node.get("configured", True):
            return page(request, "compute_nodes.html", person, status=400 if failed else 200,
                        said=said or request.query_params.get("said", ""), failed=failed,
                        selected_machine=selected, selected_node=selected_node, machine_choices=choices,
                        programs=[], typed_in=form, opened=opened or request.query_params.get('open', ''),
                        may_pair="superuser" in person.can, pairing_code=pairing_code)
        base = "/turnstile/machines/" + quote(selected, safe="")
        settings_answer = await ask("GET", base + "/settings", person.key)
        if settings_answer.status_code not in (200, 503):
            return page(request, "refused.html", person, status=settings_answer.status_code, why=settings_answer.json()["error"]["message"])
        found = settings_answer.json() if settings_answer.is_success else {"programs": []}
        hidden = {"engines", "llama_server", "disabled_gpus", "engine_gpus", "gpus", "models", "hub_token_file", "toollocker_runs_on_front",
                  "front_tasks", "front_toollocker"}  # the last two: names before the rename, still in older node reports
        resources, llama_settings = {}, []
        engine_setting = next((setting for program in found["programs"] if program["program"] == "loadcoach"
                               for setting in program["settings"] if setting["name"] == "engines"), None)
        pending_engines = engine_setting["changed"]["value"] if engine_setting and engine_setting.get("changed") else None
        engine_answer = await ask("GET", base + "/engines", person.key)
        engine_choices = engine_answer.json().get("engines", []) if engine_answer.is_success else []
        for entry in engine_choices:
            pending = (pending_engines or {}).get(entry["id"], {})
            active = entry.get("settings", {"enabled": entry["enabled"], "executable": entry["executable"]})
            entry["configured"] = {field["name"]: pending.get(field["name"], active.get(field["name"], field.get("default")))
                                   for field in entry["schema"]["node"]}
            entry["pending"] = bool(pending_engines is not None and entry["configured"] != active)
            if form.get("engine") == entry["id"] and form.get("do") != "reset":
                entry["configured"] = {field["name"]: field["name"] in form if field["type"] == "boolean" else form.get(field["name"], "")
                                       for field in entry["schema"]["node"]}
        for program in found["programs"]:
            if program["program"] == "loadcoach":
                for setting in program["settings"]:
                    if setting["name"] in ("card_margin", "disable_ram_offload", "host_ram_headroom_mib", "host_ram_limit_mib"):
                        resources[setting["name"]] = setting
                    elif setting['name'] == 'model_defaults':
                        llama_settings.append(setting)
                program["settings"] = [setting for setting in program["settings"]
                                       if setting["name"] not in hidden | set(resources) | {'model_defaults'}]
        gpu_inventory, gpu_error = {}, ""
        cards = await ask("GET", base + "/cards", person.key)
        if cards.is_success:
            gpu_inventory = cards.json()
            # Older workers may still include integrated graphics in their PCI inventory.
            from loadcoach.gpu_type import is_integrated
            gpu_inventory['cards'] = [device for device in gpu_inventory.get('cards', []) if not is_integrated(device)]
        else:
            gpu_error = f"GPU controls are unavailable (HTTP {cards.status_code}). Check LoadCoach on {selected}."
        known = await ask("GET", base + "/machine-key", person.key)
        machine_key = known.json() if known.status_code == 200 else None
        asked = request.query_params
        return page(request, "compute_nodes.html", person, status=400 if failed else 200,
                    programs=found["programs"], said=said or asked.get("said", ""), failed=failed,
                    typed_in=form, opened=opened or asked.get("open", ""), selected_machine=selected,
                    machine_choices=choices, selected_node=selected_node, gpu_inventory=gpu_inventory, gpu_error=gpu_error,
                    gpu_disabled=gpu_disabled, machine_key=machine_key, resource_settings=resources, llama_settings=llama_settings,
                    may_pair="superuser" in person.can, pairing_code=pairing_code,
                    node_settings_pending=found.get('pending', False), node_settings_error=found.get('pending_error', ''),
                    engine_choices=engine_choices, engine_error="" if engine_answer.is_success else "Engine controls require a matching LoadCoach update.")  # fmt: skip

    async def change_node_lifecycle(request: Request, person: Person) -> Response:
        """Save lifecycle on front without requiring the selected compute node to answer."""
        form = await typed(request)
        machine = form.get("machine", "")
        state = form.get("state", "")
        answer = await ask("POST", "/turnstile/machines/" + quote(machine, safe="") + "/lifecycle",
                           person.key, json={"state": state})
        if not answer.is_success:
            return await compute_nodes_page(request, person, answer.json()["error"]["message"], True, typed_in=form)
        message = {"active": "Node enabled for new work.",
                   "disabled": "Node disabled for new work. Telemetry remains visible; existing work can finish.",
                   "retired": "Node retired and hidden from telemetry. Historical logs are preserved."}[state]
        return go("/compute-nodes?machine=" + quote(machine, safe="") + "&said=" + quote(message))

    async def change_node_engine(request: Request, person: Person) -> Response:
        """Save validated node engine settings with restart requirements visible."""
        form = await typed(request)
        machine, engine_id = form.get("machine", ""), form.get("engine", "")
        try:
            if not engine_id:
                raise ValueError("Choose an inference engine.")
            selected_engine = engine_for(engine_id)
        except ValueError as problem:
            return await compute_nodes_page(request, person, str(problem), True, typed_in=form)
        base = "/turnstile/machines/" + quote(machine, safe="")
        settings_answer = await ask("GET", base + "/settings", person.key)
        if not settings_answer.is_success:
            return await compute_nodes_page(request, person, settings_answer.json()["error"]["message"], True, typed_in=form)
        entries = next((row["settings"] for row in settings_answer.json()["programs"] if row["program"] == "loadcoach"), [])
        current = next((row for row in entries if row["name"] == "engines"), None)
        configured = dict((current["changed"]["value"] if current.get("changed") else current["value"]) or {}) if current else {}
        if form.get("do") == "reset":
            configured.pop(engine_id, None)
            file_engine = (current.get("file") or {}).get(engine_id) if current else None
            if file_engine is not None:
                configured[engine_id] = file_engine
            body = {"engines": configured or None}
            if engine_id == "llama_cpp":
                body["llama_server"] = None
        else:
            try:
                options = {}
                for field in selected_engine.schema()["node"]:
                    name = field["name"]
                    options[name] = name in form if field["type"] == "boolean" else int(form.get(name, "0")) if field["type"] == "integer" else form.get(name, "").strip()
                from engines import checked_installations
                checked_installations({engine_id: options}, "llama-server")
            except ValueError as problem:
                return await compute_nodes_page(request, person, str(problem), True, typed_in=form)
            configured[engine_id] = options
            body = {"engines": configured}
            if engine_id == "llama_cpp":
                body["llama_server"] = options["executable"]
        answer = await ask("POST", base + "/settings/loadcoach", person.key, json=body)
        if not answer.is_success:
            return await compute_nodes_page(request, person, answer.json()["error"]["message"], True, typed_in=form)
        return go("/compute-nodes?machine=" + quote(machine, safe="") + "&said=" + quote("Saved. Engine settings apply after LoadCoach restarts.") + "#inference-engines")

    async def make_pairing_code(request: Request, person: Person) -> Response:
        """Make a one-time code that lets a GPU node join, and show the command to run there."""
        if "superuser" not in person.can:
            return page(request, "refused.html", person, status=403, why="Adding a GPU node requires the superuser ability.")
        code, expires = pairing.make_code(Path(settings.data), person.name)
        console = pairing.console_url(request.url.hostname or "", settings.port)
        form = await typed(request)
        return await compute_nodes_page(request, person, typed_in={'machine': form.get('machine', '')}, opened="add-node", pairing_code={
            "code": code, "expires": expires, "command": pairing.command(console, code)})

    pairing_failures: dict[str, list[float]] = {}

    async def pair_node(request: Request) -> Response:
        """Let a GPU node join with a one-time code: it is given its name, its machine key and turnstile's address.

        No sign-in: the code is the permission. Ten wrong codes from one address in ten minutes
        and that address waits. The node is recorded at the address its request came from.
        """
        client = request.client.host if request.client else ""
        now = time.time()
        recent = [moment for moment in pairing_failures.get(client, []) if now - moment < 600]
        if len(recent) >= 10:
            return JSONResponse({"error": {"message": "Too many wrong codes from here. Wait ten minutes, then make a new code."}}, status_code=429)
        try:
            body = await request.json()
            code, wanted, offered = str(body.get("code", "")), str(body.get("name", "")), str(body.get("address", ""))
        except (ValueError, AttributeError):
            return JSONResponse({"error": {"message": "Send JSON with the pairing code."}}, status_code=400)
        if not pairing.take_code(Path(settings.data), code):
            pairing_failures[client] = recent + [now]
            return JSONResponse({"error": {"message": "That code is wrong, used or out of date. Make a new one on the console."}}, status_code=403)
        # Seen from here is what turnstile can reach; the node's own word counts only from this machine itself.
        address = offered if client in pairing.LOOPBACK and offered else client
        if not pairing.valid_address(address):
            return JSONResponse({"error": {"message": "The node's address could not be told."}}, status_code=400)
        joined = pairing.join(Path(settings.path), address, wanted, voice=body.get("voice") is True)
        extra = {}
        if body.get("voice") is True:
            from weightroom.bootstrap import voice_service_identity
            extra["voice_key"] = voice_service_identity(Path(settings.path), joined["name"])
        if joined["changed"] and (Path(settings.path).parent / "voice-gateway.toml").is_file():
            restarting = pairing.restart_later(("turnstile.service", "voice-gateway.service", "weightroom.service"))
        else:
            restarting = pairing.restart_later() if joined["changed"] else False
        print(f"weightroom: GPU node {joined['name']} paired from {address}", file=sys.stderr)
        return JSONResponse({"name": joined["name"], "machine_key": joined["machine_key"], "restarting": restarting,
                             "front": f"http://{pairing.url_host(request.url.hostname or pairing.lan_address())}:8442", **extra})

    async def new_machine_key(request: Request, person: Person) -> Response:
        """Have turnstile replace one selected node's key only after that node accepts it."""
        form = await typed(request)
        machine = form.get("machine", "compute")
        answer = await ask("POST", "/turnstile/machines/" + quote(machine, safe="") + "/machine-key", person.key)
        if answer.status_code != 200:
            return await compute_nodes_page(request, person, answer.json()["error"]["message"], failed=True,
                                            typed_in={"machine": machine}, opened="machine-key")
        return go("/compute-nodes?machine=" + quote(machine, safe="") + "&said=" + quote(
            "A new machine key is in use on front and " + machine + ". The old one counts for ten more minutes."))

    async def schedule_machine_key(request: Request, person: Person) -> Response:
        """Validate and save the selected node's key-rotation schedule."""
        form = await typed(request)
        machine = form.get("machine", "compute")
        try:
            days = int(form.get("rotation_days", ""))
            if not 0 <= days <= 3650:
                raise ValueError
        except ValueError:
            return await compute_nodes_page(request, person, "Rotation days must be a whole number from 0 through 3650.",
                                            failed=True, typed_in=form, opened="machine-key")
        answer = await ask("POST", "/turnstile/machines/" + quote(machine, safe="") + "/machine-key",
                           person.key, json={"rotation_days": days})
        if not answer.is_success:
            return await compute_nodes_page(request, person, answer.json()["error"]["message"], failed=True,
                                            typed_in=form, opened="machine-key")
        message = "Scheduled key rotation disabled." if days == 0 else f"Machine key will rotate every {days} days."
        return go("/compute-nodes?machine=" + quote(machine, safe="") + "&said=" + quote(message))

    async def tools_page(request: Request, person: Person, said: str = "", failed: bool = False) -> Response:
        """Render the authorized tool catalogue and gateway configuration."""
        if 'manage-settings' not in person.can:
            return page(request, 'refused.html', person, status=403, why='Your account may not manage tools.')
        try:
            found = await fetch(request, person, '/loadcoach/tools')
        except ComputeOff:
            found = {'program': None, 'tools': []}
        if isinstance(found, Response):
            return found
        config = {entry['name']: entry['value'] for entry in (found['program'] or {}).get('settings', [])}
        return page(request, 'tools.html', person, status=400 if failed else 200, tool_info=found, tool_config=config,
                    said=said or request.query_params.get("said", ""), failed=failed)  # fmt: skip

    async def change_tools(request: Request, person: Person) -> Response:
        """Save the tools' own levels, or the tool sets. Each is one of ToolLocker's settings, sent whole."""
        form = await typed_lists(request)
        one = lambda field: form.get(field, [""])[-1].strip()  # noqa: E731
        if one("do") == "levels":  # a level chosen for a tool; one left at its server's is left out
            asked = {"tool_levels": {name.removeprefix("level:"): values[-1] for name, values in form.items() if name.startswith("level:") and values[-1]}}
        else:  # tool sets: each kept one with its tools as typed, and a new one if named
            sets = {name.removeprefix("set:"): {"tools": names_in(values[-1])} for name, values in form.items()
                    if name.startswith("set:") and f"drop:{name.removeprefix('set:')}" not in form}  # fmt: skip
            if new := one("new_set"):
                if not NAME.fullmatch(new) or new in sets:
                    return await tools_page(request, person, f"{new}: a tool set's name is lowercase letters, digits and dashes, and not one there is.", failed=True)
                sets[new] = {"tools": names_in(one("new_tools"))}
            if any(not toolset["tools"] for toolset in sets.values()):
                return await tools_page(request, person, "A tool set needs at least one tool. Tick Remove to take one away.", failed=True)
            asked = {"toolsets": sets}
        answer = await ask("POST", "/loadcoach/settings/toollocker", person.key, json=asked)
        if answer.status_code != 200:
            return await tools_page(request, person, answer.json()["error"]["message"], failed=True)
        return go("/tools?said=" + quote("Saved. The ToolLocker takes it on within a few seconds."))

    async def tools_message(request: Request, person: Person) -> Response:
        """Forward a tool test as the signed-in person; never expose their session key."""
        if 'manage-settings' not in person.can:
            return JSONResponse({'error': 'You may not manage tools.'}, status_code=403)
        try:
            body = await request.json()
            message, toolset = body['message'], body.get('toolset', '')
            if (not isinstance(message, dict) or message.get('method') not in ('initialize', 'tools/list', 'tools/call')
                    or not isinstance(toolset, str) or (toolset and not NAME.fullmatch(toolset))):
                raise ValueError
            headers = {'authorization': f'Bearer {person.key}'}
            for name in ('mcp-session-id', 'x-loadcoach-trace', 'x-loadcoach-sensitivity'):
                if value := body.get(name):
                    if not isinstance(value, str):
                        raise ValueError
                    headers[name] = value
            answer = await tool_http.post('/mcp' + ('/' + toolset if toolset else ''), json=message, headers=headers)
            return JSONResponse(answer.json(), status_code=answer.status_code,
                headers={'mcp-session-id': answer.headers.get('mcp-session-id', ''), 'cache-control': 'no-store'})
        except (ValueError, KeyError, TypeError):
            return JSONResponse({'error': 'Send a valid tool message and tool set.'}, status_code=400)
        except httpx.HTTPError:
            return JSONResponse({'error': 'ToolLocker is not answering.'}, status_code=503)

    async def cards_page(request: Request, person: Person) -> Response:
        """Keep existing GPU bookmarks pointing to their controls in Settings."""
        if 'manage-settings' not in person.can:
            return Response('You may not manage GPUs.', status_code=403)
        said = request.query_params.get('said', '')
        return go('/compute-nodes' + ('?said=' + quote(said) if said else '') + '#manage-gpus')

    async def change_cards(request: Request, person: Person) -> Response:
        """Save exclusions on compute, preserving exclusions for temporarily missing cards."""
        form = await typed_lists(request)
        machine = form.get("machine", ["compute"])[-1]
        base = "/turnstile/machines/" + quote(machine, safe="")
        found = await fetch(request, person, base + "/cards")
        if isinstance(found, Response):
            return found
        excluded = list(dict.fromkeys(form.get("disabled", []) + found["missing_disabled"]))
        policy = dict(found.get('engine_gpus', {}))
        detected = {key for card in found['cards'] for key in (card['id'], card.get('key', card['id']))}
        for engine in found.get('engines', []):
            engine_id = engine['id']
            if engine_id in form.get('gpu-engine-present', []):
                policy[engine_id] = list(dict.fromkeys(form.get('gpu-engine-' + engine_id, []) +
                    [card for card in policy.get(engine_id, []) if card not in detected]))
        payload = {'disabled_gpus': excluded}
        if form.get('gpu-engine-present'):
            payload['engine_gpus'] = policy
        answer = await ask("POST", base + "/cards", person.key, json=payload)
        if answer.status_code != 200:
            return await compute_nodes_page(request, person, answer.json()["error"]["message"], failed=True,
                                            typed_in={"machine": machine, 'gpu_engines': policy}, gpu_disabled=form.get("disabled", []))
        return go("/compute-nodes?machine=" + quote(machine, safe="") + "&said=" + quote(
            "Saved. GPU assignments apply to all models and benchmarks on this node.") + '#manage-gpus')

    async def change_settings(request: Request, person: Person) -> Response:
        """Save what was typed for one program's settings, or take one change away. Only what differs from what holds now is sent."""
        program, submitted = request.path_params["program"], await typed_lists(request)
        form = {name: values[-1] for name, values in submitted.items()}
        voice_return = program in ('voice', 'voice-gateway')
        selected_machine = form.get("machine", "")
        node_return = bool(selected_machine) or program in ("loadcoach", "spotter")
        render_settings = voice_admin_page if voice_return else compute_nodes_page if node_return else settings_page
        destination = '/system/voice' if voice_return else '/compute-nodes' if node_return else '/settings'
        if selected_machine and program not in ("loadcoach", "spotter"):
            return Response("Only LoadCoach and Spotter use a host selection.", status_code=400)
        settings_path = ("/turnstile/machines/" + quote(selected_machine, safe="") + "/settings"
                         if selected_machine else "/loadcoach/settings")
        destination_query = "machine=" + quote(selected_machine or "compute", safe="") + "&" if node_return else ""
        found = await fetch(request, person, settings_path)
        if isinstance(found, Response):
            return found
        settings = next((one["settings"] for one in found["programs"] if one["program"] == program), [])
        models = []
        if program in ("voice", "voice-gateway"):
            catalog = await fetch(request, person, "/v1/models")
            if isinstance(catalog, Response):
                return catalog
            models = [model["id"] for model in catalog["data"]]
        profile_reset = program == "voice" and form.get("reset", "").startswith((voice_settings.PREFIX, voice_settings.GREETING_PREFIX))
        try:
            asked = voice_settings.profile_change(settings, form, models) if program == "voice" else {}
            if program == 'voice':
                asked.update(voice_settings.greeting_change(settings, form))
            if program == "voice-gateway":
                asked = voice_settings.gateway_change(settings, form, models)
        except ValueError as why:
            return await render_settings(request, person, str(why), failed=True, typed_in=form, opened=program)
        if form.get("reset") and not profile_reset:
            asked = {form["reset"]: None}
        elif program == "weightroom" and "third-party-services-present" in form:
            selected = [name for name, _ in THIRD_PARTY_SERVICES if f"third-party-{name}" in form]
            current = next((setting for setting in settings if setting["name"] == "third_party_services"), None)
            holds = current and (current["changed"]["value"] if current["changed"] else current["value"])
            if current and selected != list(holds):
                asked["third_party_services"] = selected
        for setting in settings if not form.get("reset") else []:
            if program == "voice" and setting["name"] == "profiles":
                continue
            if program == "voice-gateway" and setting["name"] in voice_settings.HERMES_FIELDS:
                continue
            if program == "weightroom" and setting["name"] == "third_party_services":
                continue
            holds = setting["changed"]["value"] if setting["changed"] else setting["value"]
            typed_value = form.get("value-" + setting["name"])
            plain_text = setting_labels.describe(setting)["editor"] == "text"
            unchanged = typed_value == holds if plain_text else typed_value is not None and typed_value.strip() == written(holds).strip()
            if setting["when"] == "file" or typed_value is None or unchanged:
                continue
            try:
                asked[setting["name"]] = typed_value if plain_text else read_value(typed_value, isinstance(holds, dict))
            except ValueError as why:
                return await render_settings(request, person, f"{setting['name']}: {why}", failed=True, typed_in=form, opened=program)
        if not asked:
            return go(f"{destination}?{destination_query}open={quote(program)}&said=" + quote("Nothing was changed."))
        target = settings_path + "/" + quote(program, safe="")
        answer = await ask("POST", target, person.key, json=asked)
        if answer.status_code not in (200, 202):
            return await render_settings(request, person, answer.json()["error"]["message"], failed=True, typed_in=form, opened=program)
        message = 'Saved on front. The node will receive these locations when it reconnects.' if answer.status_code == 202 else 'Saved.'
        return go(f"{destination}?{destination_query}open={quote(program)}&said=" + quote(message))

    # ---- chat -------------------------------------------------------------------------------
    #
    # Conversations and their tasks are hosted here, with their own chat-only database.
    # Compute is reached only for authorization, model requests, and request-log details.

    async def chat_page(request: Request, person: Person, said: str = "", typed_in: dict | None = None) -> Response:
        """The list of one's conversations beside one of them, or beside the box that starts a new one."""
        listed = await fetch(request, person, "/loadcoach/conversations")
        ref = request.path_params.get("ref")
        talk = await fetch(request, person, f"/loadcoach/conversations/{quote(ref, safe='')}") if ref and not isinstance(listed, Response) else None
        for answer in (listed, talk):
            if isinstance(answer, Response):
                return answer
        # The page asking for its middle again, once an answer has ended. It is a page of its own in everything but its frame.
        template = "chat_main.html" if request.query_params.get("part") == "main" else "chat.html"
        return page(request, template, person, status=400 if said else 200, conversations=listed["conversations"], talk=talk,
                    models=[*[name for name in person.me.get("models", []) if not person.me.get("chat", {}).get("models", {}).get(name, {}).get("image_generation") and not person.me.get("chat", {}).get("models", {}).get(name, {}).get("audio")], *asked_as(person)], said=said, typed_in=typed_in or {},
                    adaptor_names={row["id"] for row in plugins.models(person.me)})  # fmt: skip

    async def collections_page(request: Request, person: Person, said='', result=None, form=None, failed=False) -> Response:
        """Render owner-private collections and the selected source/index state."""
        listing = await fetch(request,person,'/loadcoach/collections')
        if isinstance(listing,Response):
            return listing
        number=request.path_params.get('collection')
        selected=await fetch(request,person,'/loadcoach/collections/'+quote(number,safe='')) if number else None
        if isinstance(selected,Response):
            return selected
        listed=await fetch(request,person,'/loadcoach/conversations')
        models=[]
        unavailable=''
        try:
            catalogue=await ask('GET','/v1/models',person.key)
            if catalogue.is_success:
                models=[m for m in catalogue.json()['data'] if m.get('owned_by')=='loadcoach']
            else:
                unavailable='The model catalogue is unavailable. Saved sources remain on front.'
        except ComputeOff:
            unavailable='Compute is unavailable. Saved sources remain on front; indexing and queries need compute.'
        maximum=person.me.get('max_sensitivity','normal')
        levels=['normal','sensitive','restricted']
        levels=levels[:levels.index(maximum)+1] if maximum in levels else []
        response=page(request,'collections.html',person,status=400 if failed else 200,collections=listing['collections'],
            collection=selected,embedders=[m for m in models if m.get('embedding')],
            answer_models=[m for m in models if not m.get('embedding') and not m.get('capabilities', {}).get('image_generation') and not m.get('capabilities', {}).get('audio')],levels=levels,
            said=said,unavailable=unavailable,result=result,form=form or {},
            conversations=listed.get('conversations',[]) if isinstance(listed,dict) else [])
        response.headers['cache-control']='no-store'
        return response

    async def collections_do(request: Request, person: Person) -> Response:
        """Validate a collection form and dispatch its existing private API operation."""
        form=await typed(request)
        action=form.get('do')
        number=request.path_params.get('collection')
        root='/loadcoach/collections'+('/'+quote(number,safe='') if number else '')
        destination='/collections'+('/'+quote(number,safe='') if number else '')
        method='POST'
        suffix=''
        body={}
        if action=='create' and not number:
            body={key:form.get(key,'') for key in ('title','level','model')}
        elif action=='upload' and number:
            suffix='/documents'
            body={key:form.get(key,'') for key in ('name','data')}
        elif action in ('index','cancel','delete_document') and number:
            suffix='/documents/'+quote(form.get('document',''),safe='')
            if action=='delete_document':
                method='DELETE'
            else:
                suffix+='/'+action
        elif action=='delete_collection' and number:
            method='DELETE'
        elif action=='rebuild' and number:
            if form.get('confirmed')!='yes':
                return await collections_page(request,person,'Confirm clearing the vectors before rebuilding.',form=form,failed=True)
            body={'model':form.get('model','')}
        elif action=='query' and number:
            suffix='/query'
            try:
                minimum=float(form.get('minimum','.65'))
                if not 0 <= minimum <= 1:
                    raise ValueError()
            except ValueError:
                return await collections_page(request,person,'Enter a numeric minimum similarity.',form=form,failed=True)
            body={'question':form.get('question',''),'minimum':minimum,'model':form.get('model','')}
        else:
            return Response('Unknown collection action.',status_code=400)
        answer=await ask(method,root+suffix,person.key,json=body)
        if not answer.is_success:
            return await collections_page(request,person,answer.json()['error']['message'],form=form,failed=True)
        if action=='query':
            return await collections_page(request,person,result=answer.json(),form=form)
        if action=='create':
            destination='/collections/'+answer.json()['id']
        elif action=='delete_collection':
            destination='/collections'
        return go(destination)

    async def collection_source(request: Request, person: Person) -> Response:
        """Display one cited collection excerpt only to its authorized owner."""
        number,chunk=request.path_params['collection'],request.path_params['chunk']
        result=await fetch(request,person,'/loadcoach/collections/'+quote(number,safe='')+'/sources/'+quote(chunk,safe=''))
        if isinstance(result,Response):
            return result
        listed=await fetch(request,person,'/loadcoach/conversations')
        response=page(request,'collection_source.html',person,**result,
            conversations=listed.get('conversations',[]) if isinstance(listed,dict) else [])
        response.headers['cache-control']='no-store'
        return response

    async def collection_download(request: Request, person: Person) -> Response:
        """Download an original source only through its owner-scoped API."""
        path='/loadcoach/collections/'+quote(request.path_params['collection'],safe='')+'/documents/'+quote(request.path_params['document'],safe='')
        answer=await ask('GET',path,person.key)
        return Response(answer.content,status_code=answer.status_code,headers={key:value for key,value in answer.headers.items()
            if key in ('content-type','content-disposition','cache-control','x-content-type-options')})

    async def workflows_page(request: Request, person: Person, said='', failed=False, form=None) -> Response:
        """Render owner-private workflows, prompt versions and attempt history."""
        listed=await fetch(request,person,'/loadcoach/workflows')
        if isinstance(listed,Response):
            return listed
        number=request.path_params.get('workflow')
        run=await fetch(request,person,'/loadcoach/workflows/'+quote(number,safe='')) if number else None
        if isinstance(run,Response):
            return run
        collections=await fetch(request,person,'/loadcoach/collections')
        if isinstance(collections,Response):
            return collections
        chats=await fetch(request,person,'/loadcoach/conversations')
        models=[]
        unavailable=''
        try:
            catalog=await ask('GET','/v1/models',person.key)
            if catalog.is_success:
                models=[m for m in catalog.json()['data'] if m.get('owned_by')=='loadcoach' and not m.get('embedding') and not m.get('capabilities', {}).get('image_generation') and not m.get('capabilities', {}).get('audio') and m.get('local_model_guard')]
            else:
                unavailable='The model catalogue is unavailable. Saved prompts and outputs remain on front.'
        except ComputeOff:
            unavailable='Compute is unavailable. Saved prompts and outputs remain on front; generation needs compute.'
        response=page(request,'workflows.html',person,status=400 if failed else 200,workflows=listed['workflows'],
            workflow=run,prompts=listed['prompts'],builtins=listed['builtins'],collections=collections['collections'],
            workflow_models=models,unavailable=unavailable,said=said,form=form or {},
            conversations=chats.get('conversations',[]) if isinstance(chats,dict) else [])
        response.headers['cache-control']='no-store'
        return response

    async def workflows_do(request: Request, person: Person) -> Response:
        """Validate and submit an owner-scoped workflow or prompt operation."""
        form=await typed(request)
        action=form.get('do')
        number=request.path_params.get('workflow')
        root='/loadcoach/workflows'
        destination='/workflows'
        method='POST'
        body={}
        if action=='create' and not number:
            body={key:form.get(key,'') for key in ('title','question','collection','research_model','draft_model','research_prompt','draft_prompt')}
            try:
                body['minimum']=float(form.get('minimum','.65'))
                if not 0<=body['minimum']<=1:
                    raise ValueError()
            except ValueError:
                return await workflows_page(request,person,'Enter a similarity from 0 to 1.',True,form)
        elif action=='prompt' and not number:
            root+='/prompts'
            body={key:form.get(key,'') for key in ('name','kind','body')}
        elif number and action in ('start','cancel','usage','accept','delete'):
            root+='/'+quote(number,safe='')
            destination+='/'+quote(number,safe='')
            if action=='delete':
                method='DELETE'
                destination='/workflows'
            else:
                body={'action':action}
                if action=='accept':
                    body['step']=form.get('step','')
        else:
            return Response('Unknown workflow action.',status_code=400)
        response=await ask(method,root,person.key,json=body)
        if not response.is_success:
            return await workflows_page(request,person,response.json()['error']['message'],True,form)
        if action=='create':
            destination='/workflows/'+response.json()['id']
        return go(destination)

    def tuning_from(form: dict) -> dict:
        """How a conversation is to be answered, as the box under the message gives it: the instructions, whether to think first, and the numbers that were filled in."""
        sampling = {name: read(form[name].strip()) for name in ("temperature", "top_k", "top_p", "min_p") if form.get(name, "").strip()}
        return {"system": form.get("system", ""), "thinking": "thinking" in form, "sampling": sampling,
                "session_mode": form.get("session_mode", "fresh")}

    async def chat_start(request: Request, person: Person) -> Response:
        """Create a caller-owned saved or temporary conversation from the browser form."""
        form = await typed(request)
        asked = {"content": form.get("content", ""), "model": form.get("model", ""), "compare_model": form.get("compare_model", ""), "temporary": "temporary" in form, **tuning_from(form)}
        try:
            asked["attachments"] = json.loads(form.get("attachments") or "[]")
        except ValueError:
            return await chat_page(request, person, "The attachment data was invalid. Select the files again.", form)
        answer = await ask("POST", "/loadcoach/conversations", person.key, json=asked)
        if answer.status_code != 201:
            return await chat_page(request, person, answer.json()["error"]["message"], form)
        return go(f"/chat/{answer.json()['id']}")

    async def chat_do(request: Request, person: Person) -> Response:
        """Every form on a conversation's page comes here. `do` says which."""
        ref, form = request.path_params["ref"], await typed(request)
        do, where = form.get("do", "say"), f"/loadcoach/conversations/{quote(request.path_params['ref'], safe='')}"
        if do == "delete":
            answer = await ask("DELETE", where, person.key)
            if answer.status_code == 200 and 'application/json' not in request.headers.get('accept', ''):
                return go("/chat")
        elif do == "stop":
            answer = await ask("POST", where + "/stop", person.key)
        elif do == "rename":
            answer = await ask("POST", where, person.key, json={"title": form.get("title", "").strip()})
        elif do == 'options':
            answer = await ask('POST', where, person.key, json=tuning_from(form))
            if answer.is_success:
                return JSONResponse({'saved': True})
            return Response(answer.content, status_code=answer.status_code, media_type='application/json')
        else:  # say, again, edit or continue: the local conversation handler knows these actions
            sent = {"do": do, "model": form.get("model", "")} | ({"content": form["content"]} if "content" in form else {})
            try:
                sent["attachments"] = json.loads(form.get("attachments") or "[]")
            except ValueError:
                return await chat_page(request, person, "The attachment data was invalid. Select the files again.", form)
            # The box under the message says how the conversation is to be answered. That is kept first, so that it holds for this answer already.
            answer = await ask("POST", where, person.key, json=tuning_from(form)) if "tuning" in form else None
            if answer is None or answer.status_code == 200:
                answer = await ask("POST", where + "/messages", person.key, json=sent)
        if do in ('rename', 'delete') and 'application/json' in request.headers.get('accept', ''):
            return Response(answer.content, status_code=answer.status_code, media_type='application/json')
        if answer.status_code >= 300 and not (do == "stop" and answer.status_code == 409):  # stopping what has just ended is no failure
            return await chat_page(request, person, answer.json()["error"]["message"], form)
        return go(f"/chat/{quote(ref, safe='')}")

    async def chat_stream(request: Request, person: Person) -> Response:
        """An answer while it is written, for the browser: the local stream, with each step made into the HTML the page shows."""
        piece = pages.get_template("chat_live.html")

        async def events():
            """Forward current service updates as browser server-sent events."""
            try:
                # ASGITransport buffers a StreamingResponse. Read this hosted iterator
                # directly so the browser receives each step while it is being written.
                scope = {**request.scope, "headers": [(b"authorization", f"Bearer {person.key}".encode())]}
                coming = await conversations.state.follow(Request(scope))
                if isinstance(coming, StreamingResponse):
                    named = ""
                    async for frame in coming.body_iterator:
                        for line in (frame.decode() if isinstance(frame, bytes) else frame).splitlines():
                            if line.startswith("event:"):
                                named = line[6:].strip()
                            elif line.startswith("data:") and not named:
                                made = piece.render(answer=json.loads(line[5:]), live=True)
                                yield "".join(f"data: {row}\n" for row in made.splitlines()) + "\n"
            except (httpx.HTTPError, ValueError):
                pass  # compute went away, or said something that is no step. Either way the page asks for itself again
            yield "event: done\ndata:\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"cache-control": "no-cache"})

    # ---- the event log ----------------------------------------------------------------------

    async def usage_page(request: Request, person: Person) -> Response:
        """Show the caller's token history with calendar filters and browser-side chart modes."""
        if initial_page(request):
            return page(request, 'usage_report.html', person, fragment_url=fragment_url(request, 'report'))
        params = {key: request.query_params[key] for key in
                  ("start", "end", "user", "group", "model", "source", "outcome", "group_by", "timezone", "input_rate", "output_rate")
                  if request.query_params.get(key)}
        params.setdefault("timezone", settings.timezone or "UTC")
        params.setdefault("input_rate", settings.mock_input_rate)
        params.setdefault("output_rate", settings.mock_output_rate)
        answer = await ask('GET', '/loadcoach/usage', person.key, params=params)
        if not answer.is_success:
            if request.query_params.get('part') == 'report':
                return fragment_error(request, person, 'usage-report', 'report', answer)
            return page(request, 'refused.html', person, status=answer.status_code, why=answer.json()['error']['message'])
        template = ('usage_content.html' if request.query_params.get('part') == 'report'
                    and request.query_params.get('complete') != '1' else 'usage_report.html')
        return page(request, template, person, report=answer.json())

    async def allowances_page(request: Request, person: Person) -> Response:
        """Advisory remaining USD, manually refreshed without replacing active selections."""
        if request.method == 'POST':
            form = await typed(request)
            try:
                scope, subject = str(form.get('target', '')).split(':', 1)
                value = float(form.get('amount', ''))
            except (ValueError, TypeError):
                return page(request, 'refused.html', person, status=400, why='Choose a target and a numeric allowance.')
            answer = await ask('POST', '/loadcoach/allowances', person.key,
                               json={'scope':scope, 'subject':subject, 'amount':value} if math.isfinite(value) else {})
            if answer.status_code >= 300:
                return page(request, 'refused.html', person, status=answer.status_code, why=answer.json()['error']['message'])
            return go('/allowances')
        result = await fetch(request, person, '/loadcoach/allowances')
        if isinstance(result, Response):
            return result
        response = page(request, 'allowances.html', person, report=result)
        response.headers['cache-control'] = 'no-store'
        return response

    async def log_page(request: Request, person: Person) -> Response:
        """The event log, newest first: one's own entries, or everyone's for someone who may read them.

        What is typed into the boxes above the table is in the address, so a view can be kept
        as a link. `before` goes back from an entry; without it the table is the latest, and
        asks for itself again every few seconds.
        """
        asked = request.query_params
        if initial_page(request):
            return page(request, 'log.html', person, deferred=True, fragment_url=fragment_url(request, 'page'),
                        said=asked.get('said', ''), failed=asked.get('failed') == '1')
        wanted = {key: asked[key].strip() for key in ("kind", "what", "user", "group", "model", "trace", "start", "end") if asked.get(key, "").strip()}
        limit, _ = controls(asked, 100)
        going_back = {"before": asked["before"]} if asked.get("before") else {}
        found = await fetch(request, person, "/loadcoach/events", params={**wanted, **going_back, 'timezone': settings.timezone or 'UTC', "limit": limit})
        if isinstance(found, Response):
            return fragment_error(request, person, 'log-view', 'page', found) if asked.get('part') == 'page' else found
        template = "log_list.html" if asked.get("part") == "list" else "log.html"  # the table asking for itself again
        choices = await fetch(request, person, '/loadcoach/event-choices') if template == 'log.html' else {}
        if isinstance(choices, Response):
            return fragment_error(request, person, 'log-view', 'page', choices) if asked.get('part') == 'page' else choices
        if asked.get('part') == 'page' and asked.get('complete') != '1':
            template = 'log_content.html'
        return page(request, template, person, events=found["events"][::-1], wanted=wanted,
                    said=asked.get('said', ''), failed=asked.get('failed') == '1',
                    narrowed=urlencode({**wanted, 'limit': limit}), going_back=bool(going_back),
                    limit=limit, sizes=sorted(set((10, 25, 50, 100, 250, 500, 1000, limit))), **choices)

    async def reliability_page(request: Request, person: Person) -> Response:
        """Render retained reliability evidence and authorized recovery controls."""
        asked = request.query_params
        report = await fetch(request, person, '/loadcoach/reliability',
                             params={key: asked[key] for key in ('model', 'role') if asked.get(key)})
        if isinstance(report, Response):
            return report
        if asked.get('issues') == '1':
            report['rows'] = [row for row in report['rows']
                              if sum(row['counts'].get(kind, 0) for kind in ('error', 'timeout', 'empty')) > 0]
        choices = await fetch(request, person, '/loadcoach/event-choices')
        if isinstance(choices, Response):
            return choices
        quality = await fetch(request, person, '/loadcoach/quality')
        if isinstance(quality, Response):
            return quality
        return page(request, 'reliability.html', person, reliability=report, quality=quality['rows'], **choices,
                    said=asked.get('said', ''), failed=asked.get('failed') == '1')

    async def reliability_do(request: Request, person: Person) -> Response:
        """Validate and submit a reliability reset or controlled recovery request."""
        form = await typed(request)
        result = await ask('POST', '/loadcoach/reliability', person.key,
                           json={key: form.get(key, '') for key in ('model', 'role', 'fingerprint', 'action')})
        message = ('Reliability reset.' if form.get('action') == 'reset' else 'Recovery check finished: ' + result.json()['reliability']['state'] + '.') if result.status_code == 200 else result.json()['error']['message']
        query = {key: form.get('filter_' + key, '') for key in ('model', 'role', 'issues') if form.get('filter_' + key)}
        query['said'] = message
        if result.status_code != 200:
            query['failed'] = '1'
        return go('/reliability?' + urlencode(query))

    async def entry_page(request: Request, person: Person) -> Response:
        """One entry of the log, whole. A request has a page of its own already, so an entry about one leads there."""
        found = await fetch(request, person, f"/loadcoach/events/{request.path_params['number']}")
        if isinstance(found, Response):
            return found
        entry = found["entry"]
        if entry["kind"] == "request":
            return go(f"/requests/{entry.get('of') or entry.get('source') or entry['id']}")  # a request is known by compute's number
        return page(request, "entry.html", person, entry=entry, after=found["after"], models=await local_models(person))

    # ---- what the programs on compute printed, and asking a model about it --------------------

    async def local_models(person: Person) -> list[str]:
        """The models this person may ask that run on compute itself. A log is not sent to a hosted one."""
        answer = await ask("GET", "/v1/models", person.key)
        return [model["id"] for model in answer.json()["data"] if model.get("owned_by") != "hosted"] if answer.status_code == 200 else []

    journal_cache: dict[tuple, dict] = {}  # Bounded, account-scoped snapshots for offline viewing.

    async def output_page(request: Request, person: Person) -> Response:
        """Read live journals when available, retaining exact-filter snapshots for outages."""
        if "restart-services" not in person.can:
            return page(request, "refused.html", person, status=403,
                        why="Your account needs the restart-services ability to read system logs.")
        asked = request.query_params
        owner = person.me.get('identity') or person.name
        previous = next((value for key, value in reversed(journal_cache.items()) if key[0] == owner), {})
        sources = [name for name in previous.get('sources', ['loadcoach', 'kernel'])
                   if name not in THIRD_PARTY_NAMES or name in settings.third_party_services]
        requested = asked.get('source')
        disabled = requested in THIRD_PARTY_NAMES and requested not in settings.third_party_services
        wanted, why = {"lines": asked.get("lines") or "200"}, ""
        before, after = 60, 15
        try:
            before, after = float(asked.get("before") or 60), float(asked.get("after") or 15)
            if not all(math.isfinite(value) and value >= 0 for value in (before, after)):
                raise ValueError()
            if asked.get("at"):
                moment = datetime.fromisoformat(asked["at"]).replace(tzinfo=zone).timestamp()
                wanted |= {"since": moment - before, "until": moment + after}
        except ValueError:
            before, after, why = 60, 15, "The moment is a date and a time, and the seconds before and after are nonnegative numbers."
        if disabled:
            why = f"{requested} is not selected under WeightRoom settings."

        def selected_source():
            """Choose the requested enabled journal or the available default."""
            return (None if disabled else requested) or ('loadcoach' if 'loadcoach' in sources else sources[0])

        def cache_key():
            """Keep snapshots private and specific to the source, filters and service policy."""
            return owner, source, tuple(sorted(wanted.items())), tuple(settings.third_party_services)

        source, offline, saved = selected_source(), False, {}
        try:
            now = await ask_spotter("GET", "/spotter/now", person.key,
                                    params={"system_services": ",".join(settings.third_party_services)})
            if now.status_code >= 500:
                raise MachineOff
            if now.status_code != 200:
                return page(request, "refused.html", person, status=now.status_code, why=now.json()["error"]["message"])
            sources = [service["name"] for service in now.json()["services"]] + ["kernel"]
            source = selected_source()
            if not why:
                printed = await ask_spotter("GET", f"/spotter/services/{quote(source, safe='')}/journal", person.key, params=wanted)
                if printed.status_code >= 500:
                    raise MachineOff
                if printed.status_code == 200:
                    saved = {**printed.json(), 'sources': sources, 'fetched': time.time()}
                    key = cache_key()
                    journal_cache.pop(key, None)
                    journal_cache[key] = saved
                    while len(journal_cache) > 32:
                        journal_cache.pop(next(iter(journal_cache)))
                else:
                    why = printed.json()["error"]["message"]
        except MachineOff:
            offline = True
            if source not in sources:
                sources = [source, *sources]
            if not why:
                saved = journal_cache.get(cache_key(), {})
        models = []
        if not offline:
            with suppress(ComputeOff):
                models = await local_models(person)
        return page(request, "output.html", person, status=400 if why else 200,
                    sources=sources, source=source, at=asked.get("at", ""), before=before, after=after,
                    most=wanted['lines'], lines=saved.get('lines', []), output_command=saved.get('command', ''),
                    offline=offline, fetched=saved.get('fetched'), said=why, models=models)

    async def ask_model(request: Request, person: Person) -> Response:
        """Send a log and a few words to a model, as this person, and give back its answer once it is whole."""
        form = await typed(request)
        words = form.get("words", "").strip() or "What went wrong here, and what should I try?"
        most = max(int(form["most"]), 1) if form.get("most", "").isdigit() else None
        log_text = form.get('selection') or form.get('log', '')
        if most is not None:  # compatibility with earlier saved forms
            log_text = tail(log_text, most)
        sent = {"model": form.get("model", ""), "messages": [{"role": "system", "content": ASKING}, {"role": "user", "content": f"{words}\n\nThe log:\n{log_text}"}]}
        try:
            # It waits in line like any request, and a model may have to be loaded first, so it is given a long time.
            answer = await loadcoach.post("/v1/chat/completions", json=sent, headers={"authorization": f"Bearer {person.key}"}, timeout=900)
        except httpx.TimeoutException:
            return page(request, "answer.html", person, why="No answer after fifteen minutes. The request may still be under way: the first page shows it if it is.")
        except httpx.HTTPError:
            raise ComputeOff from None
        if answer.status_code != 200:
            return page(request, "answer.html", person, why=answer.json()["error"]["message"])
        message = (answer.json().get("choices") or [{}])[0].get("message") or {}  # an answer in another shape is no answer
        return page(request, "answer.html", person, why="", content=message.get("content") or "", reasoning=message.get("reasoning_content") or "",
                    number=answer.headers.get("x-loadcoach-request-id"), model=sent["model"])  # fmt: skip

    # ---- users ------------------------------------------------------------------------------

    def account_facts(user: dict) -> dict:
        """What the Users directory says of an account beside its name: a person (it has a password) or a program (keys only),
        whether it is active, invited (a temporary password not yet changed) or disabled, and why it may need looking at."""
        person_like = bool(user["password"])
        admin = any(ability == "superuser" or ability.startswith("manage-") for ability in user.get("can", []))
        attention = [words for when, words in (
            (user["password"] == "temporary", "It has not signed in yet to choose its own password."),
            (person_like and admin and not user["second_step"], "It may manage others but signs in without an authenticator."),
            (not person_like and not user["keys"], "It has no password and no API key, so nothing can use it."),
        ) if when and user["enabled"]]
        return {"kind": "person" if person_like else "program", "state": "disabled" if not user["enabled"] else "invited" if user["password"] == "temporary" else "active",
                "attention": attention, "custom": bool(user.get("own")), "initials": re.sub(r"[^a-z0-9]", "", user["name"])[:2].upper() or "?"}  # fmt: skip

    def spark_paths(values: list[int]) -> dict:
        """A day-by-day line and the area under it, in the 120 × 32 box the Usage sparklines are drawn in."""
        most, step = max([*values, 1]), 120 / max(len(values) - 1, 1)
        points = " L".join(f"{index * step:.1f},{30 - 28 * value / most:.1f}" for index, value in enumerate(values))
        return {"line": f"M{points}", "area": f"M0,32 L{points} L120,32 Z", "total": sum(values), "most": max(values, default=0)}

    async def users_page(request: Request, person: Person, said: str = "", failed: bool = False, form: dict | None = None) -> Response:
        """The directory of accounts, with the first one chosen. A refused new account comes back here with what was typed."""
        return await user_page(request, person, said, failed, opened="add" if failed else "", form=form)

    async def add_user(request: Request, person: Person) -> Response:
        """Validate and submit creation of a manageable account."""
        form = await typed(request)
        name = form.get("name", "").strip()
        asked = {"name": name, "group": form.get("group", ""), "temporary_password": "password" in form, "key": "key" in form, "label": form.get("label", "").strip()[:60] or "first"}
        answer = await ask("POST", "/loadcoach/users", person.key, json=asked)
        if answer.status_code != 201:
            return await users_page(request, person, answer.json()["error"]["message"], failed=True, form=form)
        return await user_page(request, person, f"Added {name}.", once=shown_once(answer.json()), name=name)

    def shown_once(made: dict) -> list[tuple[str, str]]:
        """The secrets in an answer of LoadCoach's, each with what to call it. They can be seen this one time."""
        return [(what, made[field]) for what, field in (("Temporary password", "temporary_password"), ("API key", "key")) if field in made]

    async def user_activity(request: Request, person: Person, name: str) -> Response:
        """An account's requests per day over the last 30 days, and its 24-hour spending against its allowance.
        Only for someone who may read the log: for anyone else the Usage report would answer with their own requests."""
        if "see-log" not in person.can:
            return page(request, "refused.html", person, status=403, why="Reading another account's activity needs the see-log ability.")
        params = {"period": "30", "user": name, "group_by": "user", "timezone": settings.timezone or "UTC"}
        usage, allowances = await asyncio.gather(ask("GET", "/loadcoach/usage", person.key, params=params), ask("GET", "/loadcoach/allowances", person.key))
        if not usage.is_success:
            return fragment_error(request, person, "account-trend", "activity", usage)
        return page(request, "user_activity.html", person, trend=spark_paths([day["requests"] for day in usage.json()["daily"]]), spend=spend_of(allowances, "user", name))

    def spend_of(allowances: httpx.Response, scope: str, name: str) -> dict | None:
        """What an account or a group spent in the last 24 hours against its allowance, from LoadCoach's /loadcoach/allowances."""
        row = next((one for one in (allowances.json()["rows"] if allowances.is_success else []) if one["scope"] == scope and one["name"] == name), None)
        if not row:
            return None
        share = 100 * row["recorded"] / row["allowance"] if row["allowance"] else 0
        return {"recorded": row["recorded"], "allowance": row["allowance"], "share": min(100.0, share), "near": share >= 90, "manage": row.get("manage")}

    async def user_page(request: Request, person: Person, said: str = "", failed: bool = False, opened: str = "", once=(), name: str = "", form: dict | None = None) -> Response:
        """The directory of accounts beside the one chosen. `opened` names a panel to show open (`add` for adding an account),
        `once` is what was just made and is shown this one time, and `form` what was typed into a refused new account."""
        name, asked = name or request.path_params.get("name", ""), request.query_params
        if name and asked.get("part") == "activity":
            return await user_activity(request, person, name)
        listing = await fetch(request, person, "/loadcoach/users")
        if isinstance(listing, Response):
            return listing
        accounts = [{**one, **account_facts(one)} for one in listing["users"]]
        name = name or (accounts[0]["name"] if accounts else person.name)
        user, known = await asyncio.gather(fetch(request, person, f"/loadcoach/users/{quote(name, safe='')}", params={"days": asked.get("days", "28")}),
                                           fetch(request, person, "/loadcoach/groups"))  # fmt: skip
        for answer in (user, known):
            if isinstance(answer, Response):
                return answer
        group = next((one for one in known["groups"] if one["name"] == user["group"]), {"can": []})  # its group may have been removed
        # The models to choose from are those the one looking may use, since nobody hands out more. One already chosen stays in the list.
        models = list(dict.fromkeys([*person.me.get("models", []), *user["own"].get("models", [])]))
        return page(request, "users.html", person, status=400 if failed else 200, user=user, facts=account_facts(user), accounts=accounts, group_names=listing["groups"],
                    known=known, ticks=tick_list(person, known, user["can"], group["can"]), models=models, form=form or {},
                    trend_url=f"/users/{quote(name, safe='')}?part=activity", said=said or asked.get("said", ""), failed=failed,
                    opened=opened or asked.get("open", ""), once=once)  # fmt: skip

    async def change_user(request: Request, person: Person) -> Response:
        """Every form on a user's page comes here. `do` says which: the name of its card, and after a dash what to do there."""
        name, form = request.path_params["name"], await typed_lists(request)
        one = lambda field: form.get(field, [""])[-1].strip()  # noqa: E731
        do, where = one("do"), f"/loadcoach/users/{quote(name, safe='')}"
        simple = {"group": {"group": one("group")}, "password-new": {"temporary_password": True}, "password-code": {"second_step": False},
                  "enabled-yes": {"enabled": True}, "enabled-no": {"enabled": False}}  # fmt: skip
        if do == "remove":
            answer = await ask("DELETE", where, person.key)
            if answer.status_code == 200:
                return go("/users?said=" + quote(f"Removed {name}."))
        elif do == "rename":
            answer = await ask("POST", where + "/rename", person.key, json={"name": one("name")})
            if answer.status_code == 200:
                return go(f"/users/{quote(answer.json()['name'], safe='')}?said=" + quote(f"Renamed {name}."))
        elif do == "keys-add":
            answer = await ask("POST", where + "/keys", person.key, json={"label": one("label")[:60]})
        elif do == "keys-remove":
            answer = await ask("DELETE", f"{where}/keys/{quote(one('key'), safe='')}", person.key)
        elif do in simple:
            answer = await ask("POST", where, person.key, json=simple[do])
        else:
            # What the account has of its own is kept as one thing, so it is read, changed in part, and sent back whole.
            now, known = await ask("GET", where, person.key), await ask("GET", "/loadcoach/groups", person.key)
            answer = known if now.status_code == 200 else now
            if answer.status_code == 200:
                own = now.json()["own"]
                # `save` is the account page's one form: its limits, then its abilities, then its tools, as each card's own form would.
                if do not in ("abilities", "tools"):  # limits: fewer models, fewer requests at once, a lower level, and a priority of its own
                    own = {key: value for key, value in own.items() if key in ("add", "remove", "add_tools", "remove_tools")}
                    own |= {"models": form.get("model", [])} if "fewer" in form else {}
                    own |= {key: read(one(key)) for key in ("max_requests", "max_sensitivity", "priority") if one(key)}
                if do in ("abilities", "save"):
                    group_can = next((group["can"] for group in known.json()["groups"] if group["name"] == now.json()["group"]), [])
                    own |= of_its_own(form.get("can", []), group_can, known.json())
                if do in ("tools", "save"):  # tools shown to it as well as its group's, and tools hidden from it
                    own |= {key: names_in(one(key)) for key in ("add_tools", "remove_tools")}
                # A list left empty is left out, so that an account with nothing of its own is its group again. No models at all is something of its own.
                answer = await ask("POST", where, person.key, json={"own": {key: value for key, value in own.items() if value != [] or key == "models"}})
        card = do.partition("-")[0]
        if answer.status_code >= 300:
            return await user_page(request, person, answer.json()["error"]["message"], failed=True, opened=card)
        if once := shown_once(answer.json()):
            return await user_page(request, person, "Done.", opened=card, once=once)
        return go(f"/users/{quote(name, safe='')}?open={card}&said=Saved.")

    # ---- groups -----------------------------------------------------------------------------

    def group_facts(group: dict) -> dict:
        """What the Groups directory says of a group beside its name: whether the one looking may change it, and the words its filter matches."""
        state = "read-only" if group.get("why_not") else "editable"
        superuser = "superuser" in group["can"]
        filters = [state, "members" if group["members"] else "empty", *(["superuser"] if superuser else []), *(["second-step"] if group.get("second_step") else [])]
        return {"kind": "group", "state": state, "superuser": superuser, "filters": " ".join(filters),
                "initials": re.sub(r"[^a-z0-9]", "", group["name"])[:2].upper() or "?"}  # fmt: skip

    async def groups_page(request: Request, person: Person, said: str = "", failed: bool = False, form: dict | None = None) -> Response:
        """The directory of groups, with the first one chosen. A refused new group comes back here with what was typed."""
        return await group_page(request, person, said, failed, opened="add" if failed else "", form=form)

    async def group_activity(request: Request, person: Person, name: str) -> Response:
        """A group's requests per day over the last 30 days, its busiest members, and its 24-hour spending against its allowance."""
        if "see-log" not in person.can:
            return page(request, "refused.html", person, status=403, why="Reading a group's activity needs the see-log ability.")
        params = {"period": "30", "group": name, "group_by": "user", "timezone": settings.timezone or "UTC"}
        usage, allowances = await asyncio.gather(ask("GET", "/loadcoach/usage", person.key, params=params), ask("GET", "/loadcoach/allowances", person.key))
        if not usage.is_success:
            return fragment_error(request, person, "account-trend", "activity", usage)
        found = usage.json()
        busiest = sorted((row for row in found["breakdown"] if row["requests"]), key=lambda row: (-row["requests"], row["name"]))[:5]
        return page(request, "user_activity.html", person, trend=spark_paths([day["requests"] for day in found["daily"]]), spend=spend_of(allowances, "group", name),
                    busiest=busiest, most=max([row["requests"] for row in busiest], default=1))  # fmt: skip

    async def add_group(request: Request, person: Person) -> Response:
        """Validate and submit a new group within the caller's privilege boundary."""
        name = (await typed(request)).get("name", "").strip()
        if not NAME.fullmatch(name):  # it goes into an address, so it is checked before it is sent
            return await groups_page(request, person, "A group name is lowercase letters, digits, dot, dash or underscore, at most 64.", failed=True, form={"name": name})
        # `new`: add only. Without it LoadCoach would replace a group that has this name already.
        answer = await ask("POST", f"/loadcoach/groups/{name}", person.key, json={}, params={"new": "1"})
        if answer.status_code != 201:
            return await groups_page(request, person, answer.json()["error"]["message"], failed=True, form={"name": name})
        return go(f"/groups/{name}?said=" + quote(f"Added {name}. It allows nothing yet: tick what its members may do, and save."))

    async def group_page(request: Request, person: Person, said: str = "", failed: bool = False, typed_in: dict | None = None,
                         opened: str = "", form: dict | None = None) -> Response:
        """The directory of groups beside the one chosen, at /groups and /groups/<name>. `typed_in` is what was typed into the
        chosen group if saving it failed, `opened` names a panel to show open (`add`), and `form` what was typed into a refused new group."""
        name, asked = request.path_params.get("name", ""), request.query_params
        if name and asked.get("part") == "activity":
            return await group_activity(request, person, name)
        wanted = [fetch(request, person, "/loadcoach/groups")]
        if "manage-users" in person.can:  # each member's kind and state, beside their name
            wanted.append(ask("GET", "/loadcoach/users", person.key))
        known, *listing = await asyncio.gather(*wanted)
        if isinstance(known, Response):
            return known
        groups = [{**one, **group_facts(one)} for one in known["groups"]]
        group = next((one for one in groups if one["name"] == name), None) if name else (groups[0] if groups else None)
        if name and group is None:
            return page(request, "refused.html", person, status=404, why=f"There is no group named {name}.")
        accounts = {one["name"]: {**one, **account_facts(one)} for one in listing[0].json()["users"]} if listing and listing[0].is_success else {}
        showing = {**group, **(typed_in or {})} if group else {}  # what was just typed if saving it failed, otherwise what is kept
        models = list(dict.fromkeys([*person.me.get("models", []), *(showing["models"] if isinstance(showing.get("models"), list) else [])]))
        return page(request, "groups.html", person, status=400 if failed else 200, group=group, groups=groups, showing=showing, known=known,
                    ticks=tick_list(person, known, showing["can"]) if group else [], models=models, form=form or {},
                    members=[accounts.get(member, {"name": member}) for member in (group or {}).get("members", [])],
                    trend_url=f"/groups/{quote(group['name'], safe='')}?part=activity" if group else "",
                    said=said or asked.get("said", ""), failed=failed, opened=opened or asked.get("open", ""))  # fmt: skip

    async def change_group(request: Request, person: Person) -> Response:
        """Validate group updates while preserving self-management and privilege limits."""
        name, form = request.path_params["name"], await typed_lists(request)
        one = lambda field: form.get(field, [""])[-1].strip()  # noqa: E731
        where = f"/loadcoach/groups/{quote(name, safe='')}"
        if one("do") == "move-members":
            answer = await ask("POST", where + "/members", person.key, json={"group": one("group")})
            if answer.status_code != 200:
                return await group_page(request, person, answer.json()["error"]["message"], failed=True)
            moved = len(answer.json()["moved"])
            target = answer.json()["group"]
            return go(f"/groups/{quote(name, safe='')}?said=" + quote(f"Moved {moved} member{'' if moved == 1 else 's'} to {target}."))
        if one("do") == "remove":
            answer = await ask("DELETE", where, person.key)
            if answer.status_code != 200:
                return await group_page(request, person, answer.json()["error"]["message"], failed=True)
            return go("/groups?said=" + quote(f"Removed {name}."))
        made, answer = {}, await ask("GET", "/loadcoach/groups", person.key)  # asked for what "superuser" stands for
        if answer.status_code == 200:
            known = answer.json()
            do = one("do")
            limits = ("priority", "max_sensitivity", "max_hold", "max_lock", "max_requests", "max_hosted_spend", "session_hours")
            if do == "abilities":
                can = set(form.get("can", []))
                made = {
                    "can": sorted(can - covered(known) if "superuser" in can else can),  # the one word says them all
                    "models": "*" if "every" in form else form.get("model", []),
                }
            elif do == "tools":
                made = {"tools": "*" if "every_tool" in form else names_in(one("tools"))}
            elif do == "limits":
                made = {
                    "priority": one("priority"),
                    "max_sensitivity": one("max_sensitivity"),
                    "second_step": "second_step" in form,
                    **{number: read(one(number)) for number in limits[2:] if one(number)},
                }
            else:  # do=save: the whole group as its page shows it, which replaces what is kept
                can = set(form.get("can", []))
                made = {
                    "can": sorted(can - covered(known) if "superuser" in can else can),
                    "models": "*" if "every" in form else form.get("model", []),
                    "priority": one("priority"),
                    "max_sensitivity": one("max_sensitivity"),
                    "second_step": "second_step" in form,
                    **{number: read(one(number)) for number in limits[2:] if one(number)},
                    # A form without the tools leaves them out, as the former combined form did.
                    **({"tools": "*" if "every_tool" in form else names_in(one("tools"))} if "tools" in form or "every_tool" in form else {}),
                }
            answer = await ask("PATCH" if do in ("abilities", "limits", "tools") else "POST", where, person.key, json=made)
        if answer.status_code != 200:
            return await group_page(request, person, answer.json()["error"]["message"], failed=True, typed_in=made)
        return go(f"/groups/{quote(name, safe='')}?said=Saved.")

    # ---- files ------------------------------------------------------------------------------

    file_types = (("", "All files"), ("model", "Models"), ("image", "Images"), ("audio", "Audio"),
                  ("adapter", "Adapters"), ("projector", "Projectors"))

    def file_address(path: str) -> str:
        """Build the selected node's model-file management address."""
        return "/file?path=" + quote(path, safe="/")

    async def all_files(request: Request, person: Person) -> dict | Response:
        """Front-cached files and the storage available at every node."""
        return await fetch(request, person, "/turnstile/artifacts/files")

    def files_view(found: dict) -> dict:
        """Add display roots and combine each file's model use across answering nodes."""
        def group(node: dict, tier: str) -> dict | None:
            """Group related model files for the operator's file listing."""
            places = [place for place in node.get("places", []) if place.get("tier", "cache") == tier]
            if not places:
                return None
            folders = [os.path.abspath(place["folder"]) for place in places]
            root = os.path.commonpath(folders)
            if len(folders) == 1:
                name = places[0]["name"].removeprefix("storage_")
                root = str(Path(folders[0]).parent) if Path(folders[0]).name == name else folders[0]
            for place, folder in zip(places, folders):
                relative = os.path.relpath(folder, root)
                place["relative"] = place["name"].removeprefix("storage_") if relative == "." else relative
            return {"tier": tier, "root": root, "places": places}

        def identity(file: dict) -> tuple:
            """Extract a stable identity for catalogue and retained-evidence matching."""
            facts = file.get("facts") or {}
            return (Path(file["path"]).name, file.get("size"), facts.get("what", "model"),
                    facts.get("name"), facts.get("architecture"), facts.get("size_label"))

        uses: dict[tuple, dict[str, set[str]]] = {}
        for node in found.get("nodes", []):
            node["place_groups"] = [one for tier in ("cache", "storage") if (one := group(node, tier))]
            if len(node["place_groups"]) == 2 and all(place.get("shared") for one in node["place_groups"] for place in one["places"]):
                # Cache and storage are one folder: one card, whose cache places carry the files.
                node["place_groups"] = [{**node["place_groups"][0], "tier": "shared"}]
            for file in node.get("files", []):
                if file.get("used_by"):
                    uses.setdefault(identity(file), {}).setdefault(node["name"], set()).update(file["used_by"])

        source = next((node for node in found.get("nodes", []) if node["name"] == found.get("artifact_machine")), {})
        groups = {one["tier"]: one for one in source.get("place_groups", [])}
        paths = {file["path"] for file in found.get("files", [])}
        places = {place["name"]: place for place in found.get("places", [])}
        for file in found.get("files", []):
            tier = file.get("tier", "cache")
            root = (groups.get(tier) or groups.get("shared") or {}).get("root")
            file["relative"] = os.path.relpath(file["path"], root) if root else Path(file["path"]).name
            by_node = uses.get(identity(file), {})
            file["used_by_all"] = sorted({model for models in by_node.values() for model in models})
            file["used_nodes"] = sorted(by_node)
            if tier == "storage":
                cache_name = file["place"].removeprefix("storage_")
                cache = places.get(cache_name)
                file["cache_place"] = cache_name if cache and cache.get("tier") == "cache" else ""
                file["cached"] = bool(cache and str(Path(cache["folder"]) / Path(file["path"]).name) in paths)
        # The Storage view: a row for each file and a box for each place it can be, as turnstile will check them.
        relatives = {file["path"]: file["relative"] for file in found.get("files", [])}
        steps = {(step["row"], step["at"]): step for job in found.get("distributions", []) if job.get("kind") == "arrange"
                 and job["state"] == "running" for step in job["steps"] if step["state"] not in ("done", "failed")}
        found["library_columns"] = library.columns(found)
        found["can_clean_cache"] = any(column['name'] == 'cache' for column in found['library_columns']) and any(
            column['name'] == 'storage' for column in found['library_columns'])
        found["cleanable"] = len(library.cleanup_changes(found))
        found["arranging"] = any(job.get("kind") == "arrange" and job["state"] == "running" for job in found.get("distributions", []))
        found["library"] = []
        for row in library.rows(found):
            row["relative"] = relatives.get(row["path"], row["name"]) if row["machine"] == found.get("artifact_machine") else row["name"]
            row["cells"] = {}
            for column in found["library_columns"]:
                box = library.cell(row, column, found)
                if step := steps.get((row["id"], column["name"])):
                    box.update(step=step, why="Waiting for the bulk change running now.")
                row["cells"][column["name"]] = box
            found["library"].append(row)
        return found

    async def files_page(request: Request, person: Person) -> Response:
        """Render the selected node's model files, storage and active transfers.

        A plain visit gets the page at once and its files afterwards (`part=page`), since reading
        every node's folders can take seconds; `complete=1` gives the whole page in one answer.
        """
        if "manage-models" not in person.can:  # asked here too, so the page that comes first is refused like its files
            return page(request, "refused.html", person, status=403, why="Your account may not manage model files.")
        asked = request.query_params
        tier = asked.get("tier", "storage")
        tier = tier if tier in ("cache", "storage") else "storage"
        file_type = asked.get("type", "")
        file_type = file_type if file_type in dict(file_types) else ""
        machine = asked.get("machine", "")
        said, failed = asked.get("said", ""), asked.get("failed") == "1"
        # Where Refresh comes back to: this view, without what was said on it.
        here = "/files?" + urlencode({"machine": machine} if machine else {"tier": tier} | ({"type": file_type} if file_type else {}))
        if initial_page(request):  # the downloads list asks for itself as soon as it is on the page
            return page(request, "files.html", person, deferred=True, fragment_url=fragment_url(request, "page"),
                        said=said, failed=failed, downloads=[], downloads_later=True, tier=tier, machine=machine, here=here)
        part = asked.get("part") if asked.get("complete") != "1" else None
        found = await all_files(request, person)
        if isinstance(found, Response):
            return fragment_error(request, person, "files-content", "page", found) if part == "page" else found
        found = files_view(found)
        if file_type:
            def matching(files):
                """Filter rows after resolving model usage, cache copies and location totals."""
                return [file for file in files if (file.get("facts") or {}).get("what", "model") == file_type]

            found = {**found, "files": matching(found.get("files", [])),
                     "library": [row for row in found.get("library", []) if row["what"] == file_type],
                     "nodes": [{**node, "files": matching(node.get("files", []))} for node in found.get("nodes", [])]}
        selected_node = next((node for node in found.get("nodes", [])
                              if node["name"] == machine and node["name"] != "front"), None)
        if machine and selected_node is None:
            return page(request, "refused.html", person, status=404, why=f"There is no compute node named {machine}.")
        template = {"list": "files_poll.html", "page": "files_content.html"}.get(part, "files.html")  # list: the pieces, asked for again
        downloads = await coming(person) if template == "files.html" else []
        return page(request, template, person, found=found, said=said, failed=failed, downloads=downloads, here=here,
                    tier=tier, machine=machine, selected_node=selected_node, file_type=file_type, file_types=file_types,
                    address=model_address, file_address=file_address)  # fmt: skip

    async def files_do(request: Request, person: Person) -> Response:
        """Validate and dispatch a model-file cleanup, copy or move operation."""
        fields = await typed_lists(request)
        form = {name: values[-1] for name, values in fields.items()}
        action = form.get("do", "")
        if action == "refresh-files":
            return await refresh_files(person, form.get("back", ""))
        if action == "arrange":
            return await arrange_files(person, fields)
        if action in ("transfer", "clean-unused"):
            body = {"cleanup": True} if action == "clean-unused" else {"transfer": {
                "rows": fields.get("selected", []), "source": form.get("source", ""),
                "destination": form.get("destination", ""), "mode": form.get("operation", "copy")}}
            answer = await ask("POST", "/turnstile/artifacts/arrange", person.key, json=body, timeout=httpx.Timeout(75, connect=2))
            if answer.status_code not in (200, 202):
                return go("/files?failed=1&said=" + quote(answer.json()["error"]["message"]))
            steps = answer.json().get("steps", [])
            message = ("Unused-copy cleanup started. Storage originals and in-use copies stay." if action == "clean-unused"
                       else "Transfer started. Source copies stay until destination copies succeed.") if steps else "Nothing to change. Files are already in place or protected."
            return go("/files?said=" + quote(message))
        if action not in ("clear-cache", "consolidate-cache", "cleanup-nodes"):
            return Response("That file maintenance action is not available.", status_code=400)
        answer = await ask("POST", f"/turnstile/artifacts/files/{action}", person.key)
        if answer.status_code not in (200, 202):
            return go("/files?said=" + quote(answer.json()["error"]["message"]))
        result = answer.json()
        if action == "clear-cache":
            message = "Cache cleanup started. Progress is shown on Files."
        elif action == "consolidate-cache":
            message = "Cache consolidation started. Progress is shown on Files."
            if result.get("absent"):
                message += " Front has no copy of: " + ", ".join(result["absent"]) + "."
        else:
            message = "Compute-node cleanup started. Progress is shown on Files."
            if result.get("unavailable"):
                message += " Not completed on: " + ", ".join(result["unavailable"]) + "."
        return go("/files?said=" + quote(message))

    async def arrange_files(person: Person, fields: dict[str, list[str]]) -> Response:
        """Send turnstile the boxes changed on the Storage view: a copy for each box ticked, a removal for each cleared.

        The page sends the columns in order, and for each file `boxes`, "ID:FLAGS", with a flag per
        column: 1 if the file was there, 0 if not, - for a box that could not be changed. A ticked box
        sends `want`, "ID:COLUMN". Only a box whose state differs from what the page showed is a change,
        so a file that arrived somewhere since the page was drawn is not taken away again.
        """
        file_type = form_type if (form_type := (fields.get("file_type") or [""])[-1]) in dict(file_types) else ""
        back = "/files?" + urlencode({"tier": "storage"} | ({"type": file_type} if file_type else {}))
        columns, wanted, changes = fields.get("column", []), set(fields.get("want", [])), []
        for box in fields.get("boxes", []):
            row, _, flags = box.partition(":")
            for index, flag in enumerate(flags[:len(columns)]):
                if flag in "01" and (flag == "1") != (f"{row}:{index}" in wanted):
                    changes.append({"row": row, "at": columns[index], "keep": flag == "0"})
        if not changes:
            return go(back + "&said=" + quote("Nothing was changed: no box was ticked or cleared."))
        answer = await ask("POST", "/turnstile/artifacts/arrange", person.key, json={"changes": changes},
                           timeout=httpx.Timeout(75, connect=2))
        if answer.status_code not in (200, 202):
            return go(back + "&failed=1&said=" + quote(answer.json()["error"]["message"]))
        steps = answer.json().get("steps", [])
        if not steps:
            return go(back + "&said=" + quote("Nothing to change: the files are already where the boxes say."))
        made, removed = sum(step["action"] == "copy" for step in steps), sum(step["action"] == "remove" for step in steps)
        said = "Started " + " and ".join(part for part in (
            f"{made} {'copy' if made == 1 else 'copies'}" if made else "",
            f"{removed} {'removal' if removed == 1 else 'removals'}" if removed else "") if part) + ". Progress is shown here."
        return go(back + "&said=" + quote(said))

    async def refresh_files(person: Person, back: str) -> Response:
        """Have front and every node look through their model folders again now, then show the view Refresh was pressed on."""
        back = back if back.startswith("/files?") and "//" not in back and "said=" not in back else "/files?"
        # Each node has thirty seconds to look through its folders; the page waits for the slowest.
        answer = await ask("GET", "/turnstile/artifacts/files", person.key, params={"refresh": "1"}, timeout=httpx.Timeout(75, connect=2))
        if answer.status_code != 200:
            return go(back + "&said=" + quote(answer.json()["error"]["message"]))
        silent = [node["name"] for node in answer.json().get("nodes", []) if not node.get("answering")]
        said = "Looked through every model folder again." + (" Not answering, so not looked at: " + ", ".join(silent) + "." if silent else "")
        return go(back + "&said=" + quote(said))

    async def file_page(request: Request, person: Person, said: str = "", failed: bool = False, path: str = "") -> Response:
        """The one page for a file: what it is, what it says about itself, and moving or deleting it."""
        found = await all_files(request, person)
        if isinstance(found, Response):
            return found
        path = path or request.query_params.get("path", "")
        file = next((one for one in found["files"] if one["path"] == path), None)
        if file is None:
            return page(request, "refused.html", person, status=404, why=f"There is no model file at {path} in any of the places.")
        read_it = await ask("GET", "/turnstile/artifacts/file", person.key, params={"path": path})
        told, unread = (read_it.json(), "") if read_it.status_code == 200 else (None, read_it.json()["error"]["message"])
        # An adapter or a projector goes onto a model. The page offers the models there are: first those the file says it was made for, then the rest, marked.
        part = file.get("facts", {}).get("what") in ("adapter", "projector")
        models = await ask("GET", "/turnstile/artifacts/models", person.key) if part else None
        names = [one["name"] for one in models.json()["models"]] if models is not None and models.status_code == 200 else []
        fits = {name for one in file.get("goes_with", []) for name in one["models"]}
        names = [name for name in names if name in fits] + [name for name in names if name not in fits]
        from weightroom.file_views import destinations
        return page(request, "file.html", person, status=400 if failed else 200, file=file, places=found["places"], told=told, unread=unread, models=names, fits=fits,
                    said=said or request.query_params.get("said", ""), failed=failed, nodes=found.get("nodes", []), artifact_machine=found.get("artifact_machine", "front"),
                    destinations=destinations(file, found), address=model_address, file_address=file_address)  # fmt: skip

    async def change_file(request: Request, person: Person) -> Response:
        """Validate and dispatch the selected model-file action."""
        fields = await typed_lists(request)
        form = {name: values[-1] for name, values in fields.items()}
        path, name = form.get("path", ""), Path(form.get("path", "")).name
        if form.get("do") == "adapt":
            # A new model: everything the chosen model has, and this adapter as well.
            new, short, known = form.get("name", "").strip(), form.get("short", "").strip(), await ask("GET", "/turnstile/artifacts/models", person.key)
            base = next((one for one in known.json().get("models", []) if one["name"] == form.get("model")), None)
            if base is None:
                return await file_page(request, person, "Choose the model this adapter is for.", failed=True, path=path)
            made = {key: base[key] for key in ("flags", "gpus", "alone", "allow_split", "min_context") if key in base} | {"adapters": {**base.get("adapters", {}), short: {"file": path}}}
            answer = await ask("POST", f"/turnstile/artifacts/models/{quote(new, safe='/:')}", person.key, json=made, params={"new": "1"}) if new else None
            if answer is None or answer.status_code != 201:
                why = answer.json()["error"]["message"] if answer is not None else "Give the new model a name."
                return await file_page(request, person, why, failed=True, path=path)
            return go(model_address(new) + "?said=" + quote(f"Made {new}: {base['name']} with the adapter {short}. It can be asked for from now on."))
        if form.get("do") == "project":
            # A new model: everything the chosen model has, and this projector, so that it can see. The chosen model stays as it is.
            new, known = form.get("name", "").strip(), await ask("GET", "/turnstile/artifacts/models", person.key)
            base = next((one for one in known.json().get("models", []) if one["name"] == form.get("model")), None)
            if base is None:
                return await file_page(request, person, "Choose the model this projector is for.", failed=True, path=path)
            made = {key: base[key] for key in ("flags", "gpus", "alone", "allow_split", "adapters", "min_context") if key in base}
            seeing = {**made, "flags": {**made["flags"], "mmproj": path}}
            answer = await ask("POST", f"/turnstile/artifacts/models/{quote(new, safe='/:')}", person.key, json=seeing, params={"new": "1"}) if new else None
            if answer is None or answer.status_code != 201:
                why = answer.json()["error"]["message"] if answer is not None else "Give the new model a name."
                return await file_page(request, person, why, failed=True, path=path)
            return go(model_address(new) + "?said=" + quote(f"Made {new}: {base['name']} with the projector {name}, so that it can be sent pictures. {base['name']} stays as it was."))
        if form.get("do") == "distribute":
            machines = fields.get("machine", [])
            if not machines:
                return await file_page(request, person, "Choose at least one destination node.", failed=True, path=path)
            answer = await ask("POST", "/turnstile/artifacts/distribute", person.key, json={"path": path, "machines": machines})
            if answer.status_code != 202:
                return await file_page(request, person, answer.json()["error"]["message"], failed=True, path=path)
            return go("/files?said=" + quote(f"Distributing {name}. Progress is shown on Files."))
        if form.get("do") == "copy":
            answer = await ask("POST", "/turnstile/artifacts/files/copy", person.key, json={"path": path, "to": form.get("to", "")})
            done = f"Copying {name} to {form.get('to', '')}. The library copy is retained."
        elif form.get("do") == "move":
            answer = await ask("POST", "/turnstile/artifacts/files/move", person.key, json={"path": path, "to": form.get("to", "")})
            done = f"Moving {name} to {form.get('to', '')}. The list shows how far it has got. Nobody using it is cut off."
        else:
            # LoadCoach checks the typed name itself, so the proof cannot be skipped by going round this page.
            answer = await ask("POST", "/turnstile/artifacts/files/delete", person.key, json={"path": path, "name": form.get("name", "").strip()})
            done = f"Deleted {name}. {size(answer.json().get('freed'))} freed." if answer.status_code == 200 else ""
        if answer.status_code not in (200, 202):
            return await file_page(request, person, answer.json()["error"]["message"], failed=True, path=path)
        destination = {"tier": "storage"} if form.get("return") == "storage" else {}
        if form.get("file_type") in dict(file_types) and form["file_type"]:
            destination["type"] = form["file_type"]
        return go("/files?" + urlencode({**destination, "said": done}))

    # ---- Hugging Face: finding a model file and bringing it down ------------------------------

    async def coming(person: Person) -> list[dict]:
        """The downloads LoadCoach has: coming, failed, or left half done. None, for someone who may not see them."""
        answer = await ask("GET", "/turnstile/artifacts/downloads", person.key)
        return answer.json()["downloads"] if answer.status_code == 200 else []

    async def hub_page(request: Request, person: Person, said: str = "", failed: bool = False, repository: str = "") -> Response:
        """One page: the downloads, a search, and either what the search found or the files of one repository to tick."""
        words = request.query_params.get("q", "").strip()
        repository = repository or request.query_params.get("repository", "").strip()
        if not repository and "/" in words and " " not in words:  # a repository typed out goes straight to its files
            repository = words
        cursors = request.query_params.getlist('cursor')
        sort, direction = hub_search_order(request.query_params)
        answer = await ask("GET", "/turnstile/artifacts/hub", person.key, params={"repository": repository} if repository else
                           {"search": words, "paged": "1", "cursor": cursors[-1] if cursors else "", "sort": sort, "direction": direction})
        found, status = answer.json(), 400 if failed else answer.status_code
        if answer.status_code != 200:  # LoadCoach's own words: not allowed, no such repository, or Hugging Face not answering
            said, failed, found = found["error"]["message"], True, {}
        elif words and not repository and (found.get('paging') != {'limit': 20, 'sort': sort, 'direction': direction}
                                           or len(found.get('found', [])) > 20):
            said = 'The model search service did not apply the requested ordering or page size. Update the model-file service and retry.'
            failed, found, status = True, {}, 502
        def result_link(trail, order=sort, way=direction):
            """Keep previous provider cursors in bookmarkable local result URLs."""
            return '/hub?' + urlencode([('q', words), ('sort', order), ('direction', way), *(('cursor', value) for value in trail)])
        next_cursor = found.get('next_cursor', '')
        hub_paging = {'previous': result_link(cursors[:-1]) if cursors else '',
                      'next': result_link([*cursors, next_cursor]) if next_cursor and next_cursor not in cursors else '',
                      'page': len(cursors) + 1}
        hub_sort = {'key': sort, 'direction': direction,
                    'links': {key: result_link([], key, '1' if key == sort == 'createdAt' and direction == '-1' else '-1')
                              for key in ('downloads', 'likes', 'createdAt')},
                    'description': {'downloads': 'Most downloaded first', 'likes': 'Most liked first',
                                    'createdAt': 'Oldest first' if direction == '1' else 'Newest first'}[sort]}
        return page(request, "hub.html", person, status=status, found=found, words=words or repository, downloads=await coming(person),
                    said=said or request.query_params.get("said", ""), failed=failed, hub_paging=hub_paging, hub_sort=hub_sort)  # fmt: skip

    async def hub_start(request: Request, person: Person) -> Response:
        """Bring the ticked files down."""
        form = await typed_lists(request)
        repository, files = form.get("repository", [""])[-1], form.get("file", [])
        if not files:
            return await hub_page(request, person, "Tick the files you want.", failed=True, repository=repository)
        answer = await ask("POST", "/turnstile/artifacts/downloads", person.key, json={"repository": repository, "files": files, "place": form.get("place", [""])[-1]})
        if answer.status_code != 202:
            return await hub_page(request, person, answer.json()["error"]["message"], failed=True, repository=repository)
        started = answer.json()
        said = f"{len(started['downloading'])} file{'s' * (len(started['downloading']) != 1)}, {size(started['size'])}, coming down to {started['to']}. How far each has got is shown here and on Files."
        return go(f"/hub?repository={quote(repository, safe='')}&said={quote(said)}")

    async def downloads_part(request: Request, person: Person, told: str = "") -> Response:
        """The list of downloads alone, for the page that shows it to ask for again."""
        return page(request, "downloads.html", person, downloads=await coming(person), told=told)

    async def downloads_do(request: Request, person: Person) -> Response:
        """Stop a download, go on with one, or throw one away. The list comes back, with LoadCoach's words if it said no."""
        form = await typed(request)
        if form.get("do") == "continue":  # asked for again, by its name: LoadCoach goes on from what is there
            body = {"repository": form.get("repository", ""), "files": [form.get("file", "")], "place": form.get("place", "")}
            answer = await ask("POST", "/turnstile/artifacts/downloads", person.key, json=body)
        else:
            answer = await ask("POST", "/turnstile/artifacts/downloads/" + ("stop" if form.get("do") == "stop" else "discard"), person.key, json={"path": form.get("path", "")})
        return await downloads_part(request, person, "" if answer.is_success else answer.json()["error"]["message"])

    # ---- the machine ------------------------------------------------------------------------

    async def strip_piece(request: Request, person: Person) -> Response:
        """One compact pair of meters per physical host in the top navigation."""
        found = await fetch(request, person, "/turnstile/machines")
        return found if isinstance(found, Response) else page(request, "strip.html", person, hosts=found.get("hosts", []))

    async def strip_stream(request: Request, person: Person) -> Response:
        """Stream changed compact telemetry instead of repeatedly replacing it with HTMX."""
        template = pages.get_template("strip.html")

        async def events():
            """Forward current service updates as browser server-sent events."""
            previous = None
            while not await request.is_disconnected():
                found = await fetch(request, person, "/turnstile/machines")
                if isinstance(found, Response):
                    break
                rendered = template.render(person=person, compute=compute, strip=True,
                                           hosts=found.get("hosts", []))
                if rendered != previous:
                    previous = rendered
                    yield f"data: {json.dumps(rendered)}\n\n"
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(5)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "cache-control": "no-cache, no-store",
            "x-accel-buffering": "no",
        })

    async def wake_node(request: Request, person: Person) -> Response:
        """Request an authorized wake packet for exactly the chosen telemetry node."""
        name = request.path_params["name"]
        form = await typed(request)
        destination = form.get("return_to", "/telemetry")
        if not re.fullmatch(r"/telemetry(?:/[0-9a-f]{64})?", destination):
            destination = "/telemetry"
        answer = await ask("POST", "/turnstile/machines/" + quote(name, safe="") + "/wake", person.key)
        said = (f"Asked {name} to wake." if answer.is_success else answer.json()["error"]["message"])
        return go(destination + "?" + ("" if answer.is_success else "failed=1&") + "said=" + quote(said))

    async def physical_host_controls(person: Person, fingerprint: str) -> tuple[dict | None, str]:
        """Read compute's service/power controls only for its verified physical host."""
        try:
            answer = await ask_spotter('GET', '/spotter/now', person.key,
                                       params={'system_services': ','.join(settings.third_party_services)}, timeout=5)
        except MachineOff:
            return None, 'Host controls are unavailable while Spotter is not answering.'
        if answer.status_code != 200:
            return None, 'Host controls are unavailable for this account.'
        now = answer.json()
        if now.get('machine_fingerprint') != fingerprint:
            return None, 'Host controls could not verify this physical host. Refresh its inventory.'
        return now, ''

    def machine_action_destination(form):
        """Keep control outcomes on a local Telemetry page."""
        destination = form.get('return_to', '')
        match = re.fullmatch(r'/telemetry/([0-9a-f]{64})(?:\?[^\r\n#]*)?', destination)
        return (destination, match[1]) if match else ('/telemetry', '')

    def machine_action_result(destination, message, *, failed=False):
        """Append an action result without discarding the selected host or graph range."""
        return go(destination + ('&' if '?' in destination else '?') +
                  ('failed=1&' if failed else '') + 'said=' + quote(message))

    async def telemetry_workspace_page(request: Request, person: Person) -> Response:
        """One physical-host workspace, with bounded history only for the selected host."""
        from weightroom.telemetry_workspace import hosts_view, fleet_summary, history_groups, number, component_pills
        found = await fetch(request, person, "/turnstile/machines")
        if isinstance(found, Response):
            return found
        source_poll = float(found.get("poll_seconds", 10))
        source_poll = source_poll if math.isfinite(source_poll) and source_poll > 0 else 10
        poll = max(5, min(source_poll, 60))
        hosts = hosts_view(found.get("hosts", []), source_poll)
        asked = request.query_params
        identity = request.path_params.get("fingerprint") or asked.get("node")
        host = next((one for one in hosts if one["identity"] == identity), None) if identity else None
        if identity and host is None:
            return page(request, "refused.html", person, status=404, why="That machine fingerprint is not known to front.")
        try:
            hours = float(asked.get("hours") or 1)
            if not math.isfinite(hours):
                raise ValueError("Hours must be finite")
            hours = min(max(hours, 1 / 60), 672)
            until = datetime.fromisoformat(asked["until"]).replace(tzinfo=zone).timestamp() if asked.get("until") else time.time()
        except (ValueError, OverflowError, OSError):
            hours, until = 1, time.time()
        past = {"begin": until - hours * 3600, "end": until, "step": hours * 3600 / 240, "series": {}}
        history_error = None
        if host and host.get("fingerprint"):
            answer = await fetch(request, person, f"/turnstile/telemetry/{host['fingerprint']}/history?end={until}&seconds={hours * 3600}&points=240")
            if isinstance(answer, Response):
                history_error = "History is temporarily unavailable. Current host readings are still shown. Retry with Refresh."
            else:
                past = answer
        summary = fleet_summary(hosts)
        groups = history_groups(host, past, chart) if host else []
        query = [(key, value) for key, value in asked.multi_items() if key not in ('part', 'said', 'failed')]
        refresh_url = request.url.path + '?' + urlencode([*query, ('part', 'workspace')])
        graph_data = {"groups": groups, "begin": past["begin"], "end": past["end"], "step": past["step"],
                      "identity": identity or "", "hours": hours, "until": asked.get("until", ""),
                      "initial_group": 'gpu-' + asked['gpu'] if asked.get('gpu', '').isdigit() else 'processor'}
        template = "telemetry_workspace.html" if asked.get("part") == "workspace" else "telemetry_host.html" if host else "machines.html"
        if host and 'compute' in host['machines'] and host.get('fingerprint'):
            snapshot, _ = await physical_host_controls(person, host['fingerprint'])
            if snapshot:
                uptime = number(snapshot.get('up_for'))
                host['uptime'] = uptime if uptime is not None and uptime >= 0 else None
        if host:
            host['components'] = None
            if 'manage-settings' in person.can:
                try:
                    components, _ = await component_inventory(person)
                except ComputeOff:
                    components = None
                if components is not None:
                    host['components'] = component_pills(host, components)
        return page(request, template, person, hosts=hosts, host=host, summary=summary, graph_data=graph_data,
                    poll=poll, hours=hours, until=asked.get("until", ""), refresh_url=refresh_url,
                    history_error=history_error, snapshot_at=time.time())

    async def machines_page(request: Request, person: Person) -> Response:
        """Open the fleet grid without automatically selecting a host."""
        return await telemetry_workspace_page(request, person)

    async def telemetry_host(request: Request, person: Person) -> Response:
        """Open a bookmarkable host inside the same Telemetry workspace."""
        return await telemetry_workspace_page(request, person)

    async def docs_page(request: Request, person: Person) -> Response:
        """A docs section's overview or one of its topics. Every account may read the Web app section; the rest needs manage-settings."""
        name, part = request.path_params.get("section", "system"), request.path_params.get("topic")
        query = "?" + request.url.query if request.url.query else ""
        if part is None and (name in MOVED_DOCS or "section" in request.path_params and name == "system"):
            return RedirectResponse(MOVED_DOCS.get(name, "/docs") + query, status_code=308)  # a page from before the docs had sections
        section = DOC_SECTIONS.get(name)
        topic = section["by_slug"].get(part) if section and part else None
        if section is None or part and topic is None:
            return Response("Documentation page not found.", status_code=404)
        if not section["public"] and "manage-settings" not in person.can:
            if "section" not in request.path_params:
                return go("/docs/web-app")  # the docs' front page is the system reference, which this account may not read
            return Response("Your account may not view system documentation.", status_code=403)
        reference = api = document = None
        try:
            if topic and topic["source"]:
                reference = await asyncio.to_thread(source_reference, topic["source"], request.query_params.get("module", ""))
            if topic and (name, part) == ("api", "custom"):
                api = await asyncio.to_thread(lambda: api_reference(source_files("api")))
            if topic:
                document = await asyncio.to_thread(written_doc, topic)
        except ValueError:
            return Response("Documentation source not found.", status_code=404)
        except (OSError, SyntaxError):
            return Response("Source documentation is temporarily unavailable.", status_code=503)
        host = request.url.hostname or "FRONT"
        host = "[" + host + "]" if ":" in host else host
        return page(request, "docs_page.html", person, section=section, topic=topic, reference=reference, api=api,
                    document=document, sections=visible_docs(person.can), api_base=f"http://{host}:8442/v1")

    async def guide_moved(request: Request) -> Response:
        """The user guide is now the Web app section of the docs; old links and bookmarks arrive there."""
        return RedirectResponse("/docs/web-app", status_code=308)

    async def component_inventory(person: Person):
        """Read and normalize the same caller-authorized inventory for Components and Telemetry."""
        answer = await ask('GET', '/turnstile/components', person.key)
        if answer.status_code != 200:
            return None, answer
        found = answer.json()
        from loadcoach.components import SERVICES
        service_order = {name: index for index, (name, _, _) in enumerate(SERVICES)}
        for node in found["nodes"]:
            # Keep reports from older nodes in the console's current service order.
            node.get("services", []).sort(key=lambda row: service_order.get(row["id"], len(service_order)))
            # The console showing this page is running, on front's machine when it is the one that answered, unit or not.
            if node["name"] == "front" and node.get("hostname") == socket.gethostname():
                for row in node["services"]:
                    if row["id"] == "weightroom" and not row["installed"]:
                        row.update(installed=True, state="running", detail="running without a systemd unit")
        return found, None

    async def components_page(request: Request, person: Person) -> Response:
        """Every service and engine on front and on each compute node, installed or not. Changes nothing and wakes nothing."""
        if "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="Seeing what each machine has installed requires manage-settings.")
        if initial_page(request):
            return page(request, "components.html", person, deferred=True, fragment_url=fragment_url(request, "page"))
        part = request.query_params.get("part") == "page" and request.query_params.get("complete") != "1"
        found, answer = await component_inventory(person)
        if answer is not None:
            if part:
                return fragment_error(request, person, "components-report", "page", answer)
            return page(request, "refused.html", person, status=answer.status_code, why=answer.json()["error"]["message"])
        return page(request, "components_content.html" if part else "components.html", person, found=found, deferred=False)

    async def act_on_service(request: Request, person: Person) -> Response:
        """Start, stop or restart a service on compute. spotter decides whether this person may, and does it."""
        name, action = request.path_params["name"], request.path_params["action"]
        destination, fingerprint = machine_action_destination(await typed(request))
        if fingerprint:
            _, why = await physical_host_controls(person, fingerprint)
            if why:
                return machine_action_result(destination, why, failed=True)
        if name in THIRD_PARTY_NAMES and name not in settings.third_party_services:
            return machine_action_result(destination, f"{name} is not selected under WeightRoom settings.", failed=True)
        try:
            answer = await ask_spotter("POST", f"/spotter/services/{quote(name, safe='')}/{quote(action, safe='')}", person.key)
        except MachineOff:
            return machine_action_result(destination, 'spotter is not answering.', failed=True)
        if answer.status_code != 200:
            return machine_action_result(destination, answer.json()['error']['message'], failed=True)
        return machine_action_result(destination, f"Asked {name} to {action}. It is now {answer.json()['state']}.")

    async def power_action(request: Request, person: Person) -> Response:
        """Put compute to sleep, restart it, switch it off, or call that off. spotter decides whether this person may, and does it."""
        action, form = request.path_params["action"], await typed(request)
        destination, fingerprint = machine_action_destination(form)
        if fingerprint:
            _, why = await physical_host_controls(person, fingerprint)
            if why:
                return machine_action_result(destination, why, failed=True)
        try:
            answer = await ask_spotter("POST", f"/spotter/power/{quote(action, safe='')}", person.key, json={"force": "force" in form})
        except MachineOff:
            return machine_action_result(destination, 'spotter is not answering.', failed=True)
        if answer.status_code not in (200, 202):
            return machine_action_result(destination, answer.json()['error']['message'], failed=True)
        doing = {"sleep": "go to sleep", "reboot": "restart", "off": "switch off"}.get(action)
        if not doing:
            return machine_action_result(destination, 'Called off. LoadCoach takes requests again.')
        how = "now, whatever LoadCoach is doing" if "force" in form else "once LoadCoach has finished what it has. Until then it takes no new requests"
        return machine_action_result(destination, f"Compute will {doing} {how}.")

    async def choose_display(request: Request, person: Person) -> Response:
        """Keep what someone chose about how things are shown to them. A tick box that is not ticked is simply not sent."""
        shown = "show_telemetry" in await typed(request)
        answer = await ask("POST", "/loadcoach/me", person.key, json={"show_telemetry": shown})
        if answer.status_code != 200:
            return await account_appearance(request, person, answer.json()["error"]["message"], failed=True)
        # Sent to the page afresh, so that the page itself is drawn by the new choice and not the old one.
        return go("/account/appearance?said=Kept.")

    async def pulse(request: Request) -> Response:
        """The small mark in the corner that says whether compute is answering. The page asks for it again by itself."""
        return page(request, "pulse.html")

    async def scheduled_front_backup(identifier, retention):
        """Create the front's half of a scheduled paired recovery set."""
        root, files, excluded, rows, stores = front_backup_context()
        existing = backup_manifest(root, identifier)
        if existing and existing.get('verification', {}).get('state') == 'verified':
            return existing
        work = asyncio.create_task(asyncio.to_thread(create_backup, root, identifier, 'front', files, rows, excluded,
            ['weightroom.service', 'turnstile.service', 'voice-gateway.service'], retention, stores=stores))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await work  # keep the maintenance drain active until this writer stops
            raise

    schedule_worker = ScheduleWorker(schedules, loadcoach, benchmarks.state.store, scheduled_front_backup, gate, benchmarks.state.worker.wake)

    async def schedules_page(request: Request, person: Person, said='', failed=False, editing_id='', selected_id='', new_form=False):
        """Render durable recurring schedules and their occurrence history."""
        if 'manage-settings' not in person.can:
            return Response('You may not manage schedules.', status_code=403)
        now = time.time()
        if request.query_params.get('part') == 'timing':
            try:
                weekdays = [int(day) for day in request.query_params.getlist('weekdays')]
            except ValueError:
                weekdays = []
            return page(request, 'schedule_timing.html', person, **schedule_views.timing(
                dict(time=request.query_params.get('time', ''), timezone=request.query_params.get('timezone', ''), weekdays=weekdays),
                request.query_params.get('preview_enabled') != 'no', now))
        try:
            number = max(1, min(1_000_000, int(request.query_params.get('page', 1))))
        except ValueError:
            return Response('Page must be a whole number.', status_code=400)
        catalogue = []
        if person.key:
            try:
                answer = await ask('GET', '/v1/models', person.key)
                if answer.is_success:
                    catalogue = [row for row in answer.json()['data'] if row['owned_by'] == 'loadcoach' and row.get('model_id')]
            except ComputeOff:
                pass
        rows = schedules.rows()
        for row in rows:
            row.pop('grant_token')
            row['can_edit'] = bool(person.key and row['owner_id'] == person.me.get('identity'))
            row['model_name'] = next((model['id'] for model in catalogue if model['model_id'] == row['spec']['model_id']), row['model_name'])
            row['next_display'] = datetime.fromtimestamp(row['next_at'], ZoneInfo(row['spec']['timezone'])).strftime('%Y-%m-%d %H:%M %Z') if row['enabled'] else 'Paused'
            latest = schedules.db.execute('SELECT state,message FROM schedule_occurrences WHERE schedule_id=? ORDER BY due_at DESC LIMIT 1', (row['id'],)).fetchone()
            row['last'] = dict(zip(('state', 'message'), latest)) if latest else None
            schedule_views.decorate(row, now)
        editing_id = editing_id or request.query_params.get('edit', '')
        editing = next((row for row in rows if row['id'] == editing_id), None)
        if editing_id and not editing:
            return Response('No such schedule.', status_code=404)
        if editing and not editing['can_edit']:
            return Response('Only the schedule owner may edit it with an online sign-in.', status_code=403)
        selected_id = editing_id or selected_id or request.query_params.get('view', '')
        selected = next((row for row in rows if row['id'] == selected_id), None)
        if selected_id and not selected:
            return Response('No such schedule.', status_code=404)
        if not selected and not new_form and request.query_params.get('new') != '1':
            selected = next(iter(rows), None)
        editing = selected if selected and selected['can_edit'] else None
        form_spec = selected['spec'] if selected else {'kind': 'backup', 'model_id': '', 'benchmark': '',
            'time': '03:00', 'timezone': settings.timezone or 'America/Vancouver', 'retention': settings.backup_retention,
            'weekdays': list(range(7)), 'missed': 'skip'}
        upcoming = min((row for row in rows if row['enabled']), key=lambda row: row['next_at'], default=None)
        names = {row['id']: row['name'] for row in schedules.rows(include_deleted=True)}
        history = schedules.occurrences(limit=26, offset=(number-1)*25)
        for row in history:
            row.pop('grant_token')
            row.pop('task_token')
            row['name'] = names.get(row['schedule_id'], row['schedule_id'])
            row['due_display'] = datetime.fromtimestamp(row['due_at'], ZoneInfo(row['spec']['timezone'])).strftime('%Y-%m-%d %H:%M %Z')
            if row['spec']['kind'] in ('benchmark','outstanding'):
                row['href'] = '/benchmarks/run/' + row['id'] if benchmarks.state.store.get(row['id']) else ''
            else:
                from loadcoach.schedule_time import occurrence_ids
                _, backup_id = occurrence_ids(row['schedule_id'], row['due_at'])
                row['href'] = '/backups/' + backup_id if row['state'] == 'completed' else ''
        return page(request, 'schedules.html', person, status=400 if failed else 200,
            schedules=rows, history=history[:25], history_page=number, more_history=len(history)>25,
            catalogue=catalogue, schedule_nodes=sorted({'compute', *(one['machine'] for model in catalogue for one in model.get('locations', []))}), definitions=[row for row in benchmarks.state.store.definitions() if row['enabled']],
            timezone=settings.timezone or 'America/Vancouver', retention=settings.backup_retention,
            editing=editing, selected=selected, form_spec=form_spec, upcoming=upcoming,
            active_count=sum(bool(row['enabled']) for row in rows), paused_count=sum(not row['enabled'] for row in rows),
            attention_count=sum(row['attention'] for row in rows),
            can_save=bool(person.key and (not selected or selected['can_edit'])),
            timing=schedule_views.timing(form_spec, bool(selected['enabled']) if selected else True, now),
            said=said or request.query_params.get('said', ''), failed=failed)

    def schedule_form_values(form):
        """Parse schedule form fields into the validated schedule contract."""
        one = lambda key, default='': form.get(key, [default])[-1]
        name, kind = one('name').strip(), one('kind')
        if not 1 <= len(name) <= 80:
            raise ValueError('Give the schedule a name from 1 to 80 characters.')
        spec = validate_schedule({'kind': kind, 'model_id': one('model') if kind == 'benchmark' else '',
            'benchmark': one('benchmark') if kind == 'benchmark' else '', 'timezone': one('timezone'),
            'time': one('time'), 'weekdays': [int(day) for day in form.get('weekdays', [])],
            'missed': one('missed'), 'retention': int(one('retention', '10')),
            **({'machine':one('machine')} if kind in ('benchmark','outstanding') else {}),
            **({'model_ids':form.get('model_ids',[]), 'benchmarks':form.get('benchmarks',[])} if kind=='outstanding' else {})})
        if kind == 'benchmark' and not any(row['id'] == spec['benchmark'] and row['enabled'] for row in benchmarks.state.store.definitions()):
            raise ValueError('Choose an enabled benchmark.')
        return name, spec

    async def schedule_create(request: Request, person: Person):
        """Register the current owner's validated recurring schedule."""
        if 'manage-settings' not in person.can:
            return Response('You may not manage schedules.', status_code=403)
        form = await typed_lists(request)
        identifier, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        try:
            if len(schedules.rows()) >= 100:
                raise ValueError('At most 100 schedules are supported.')
            if not person.key or not person.me.get('identity'):
                raise ValueError('Create schedules with an online sign-in.')
            name, spec = schedule_form_values(form)
            answer = await ask('POST', '/loadcoach/schedules', person.key, json={'id': identifier, 'token': token, 'spec': spec})
            if not answer.is_success:
                raise ValueError(answer.json()['error']['message'])
            schedules.create(identifier, name, person.me['identity'], person.name, spec, token, model_name=answer.json().get('model_name', ''))
        except (ValueError, TypeError, KeyError, ComputeOff) as problem:
            return await schedules_page(request, person, 'Compute is unavailable.' if isinstance(problem, ComputeOff) else str(problem), failed=True, new_form=True)
        return go('/schedules?edit='+identifier+'&said=Schedule+created.')

    async def schedule_edit(request: Request, person: Person):
        """Replace the selected schedule and rotate its execution grant."""
        if 'manage-settings' not in person.can:
            return Response('You may not manage schedules.', status_code=403)
        identifier = request.path_params['identifier']
        form = await typed_lists(request)
        try:
            async with schedule_worker.lock:
                row = next((row for row in schedules.rows() if row['id'] == identifier), None)
                if not row:
                    return Response('No such schedule.', status_code=404)
                if not person.key or row['owner_id'] != person.me.get('identity'):
                    return Response('Only the schedule owner may edit it with an online sign-in.', status_code=403)
                if schedules.has_active(identifier):
                    raise ValueError('Wait for the claimed occurrence to finish before editing this schedule. Submitted benchmarks can be cancelled from Runs.')
                name, spec = schedule_form_values(form)
                token = secrets.token_urlsafe(32)
                answer = await ask('POST', '/loadcoach/schedules/'+identifier+'/edit', person.key,
                    json={'spec': spec, 'token': token, 'enabled': bool(row['enabled'])})
                if answer.status_code == 404:
                    raise ValueError('Update LoadCoach on compute before editing schedules.')
                if not answer.is_success:
                    raise ValueError(answer.json()['error']['message'])
                schedules.update(identifier, name, spec, token, model_name=answer.json().get('model_name', ''))
        except (ValueError, TypeError, KeyError, ComputeOff) as problem:
            return await schedules_page(request, person, 'Compute is unavailable; the edit was not saved.' if isinstance(problem, ComputeOff) else str(problem), failed=True, editing_id=identifier)
        return go('/schedules?edit='+identifier+'&said=Schedule+saved.')

    async def schedule_action(request: Request, person: Person):
        """Pause, resume or remove a schedule after ownership checks."""
        if 'manage-settings' not in person.can:
            return Response('You may not manage schedules.', status_code=403)
        action = (await typed(request)).get('do')
        if action not in ('pause', 'resume', 'delete'):
            return Response('Choose pause, resume or delete.', status_code=400)
        identifier = request.path_params['identifier']
        try:
            async with schedule_worker.lock:
                if not any(row['id'] == identifier for row in schedules.rows()):
                    return Response('No such schedule.', status_code=404)
                if action == 'delete':
                    if not person.key:
                        raise ValueError('Delete requires an online sign-in to revoke the schedule authorization.')
                    answer = await ask('POST', '/loadcoach/schedules/'+identifier, person.key, json={'enabled': False})
                    if not answer.is_success:
                        raise ValueError(answer.json()['error']['message'])
                    schedules.delete(identifier)
                    return go('/schedules?said=Schedule+deleted.+History+and+submitted+work+are+kept.')
                if action == 'resume':
                    if not person.key:
                        raise ValueError('Resume requires an online sign-in.')
                    answer = await ask('POST', '/loadcoach/schedules/'+identifier, person.key, json={'enabled': True})
                    if not answer.is_success:
                        raise ValueError(answer.json()['error']['message'])
                schedules.enable(identifier, action == 'resume')
        except (ValueError, ComputeOff) as problem:
            return await schedules_page(request, person, 'Compute is unavailable.' if isinstance(problem, ComputeOff) else str(problem), failed=True, selected_id=identifier)
        return go('/schedules?view='+identifier)

    async def alert_page(request: Request, person: Person) -> Response:
        """Render durable alert episodes and monitoring source freshness."""
        if 'manage-settings' not in person.can:
            return Response('You may not read operational alerts.', status_code=403)
        await collect_alerts(person.key)
        try:
            number = min(max(int(request.query_params.get('page', 1)), 1), 1_000_000)
        except ValueError:
            return Response('Page must be a whole number.', status_code=400)
        history = alerts.rows(limit=26, offset=(number-1)*25)
        return page(request, 'alerts.html', person, active=alerts.rows(open_only=True), history=history[:25],
                    sources=alerts.sources(), history_page=number, more_history=len(history)>25,
                    threshold=settings.alert_gpu_temperature)

    async def alert_action(request: Request, person: Person) -> Response:
        """Record an authorized alert acknowledgement or incident review."""
        if 'manage-settings' not in person.can:
            return Response('You may not change operational alerts.', status_code=403)
        form = await typed(request)
        identifier = request.path_params['identifier']
        if form.get('do') == 'acknowledge':
            alerts.acknowledge(identifier, person.name)
        elif form.get('do') == 'review':
            if not alerts.review(identifier, person.name):
                return Response('Only an open memory-kill incident can be reviewed.', status_code=409)
        else:
            return Response('Choose acknowledge or review.', status_code=400)
        return go('/alerts')

    @asynccontextmanager
    async def lifespan(_: Starlette):
        """Start background workers and release service resources on shutdown."""
        async with benchmarks.router.lifespan_context(benchmarks), conversations.router.lifespan_context(conversations):
            watching = asyncio.create_task(watch())
            alert_watching = asyncio.create_task(watch_alerts())
            scheduling = asyncio.create_task(schedule_worker.run())
            try:
                yield
            finally:
                watching.cancel()
                alert_watching.cancel()
                scheduling.cancel()
                await asyncio.gather(watching, alert_watching, scheduling, return_exceptions=True)
        await database_console.jobs.close()
        await database_operations.jobs.close()
        await chat_client.aclose()
        await plugin_http.aclose()
        plugins.db.close()
        alerts.db.close()
        schedules.db.close()
        if chat_inference is not loadcoach:
            await chat_inference.aclose()
        await benchmark_client.aclose()
        await loadcoach.aclose()
        await tool_http.aclose()
        await voice_http.aclose()
        if spotter is not None:
            await spotter.aclose()
        await observation.shutdown(app)

    def route(path: str, handler, method: str = "GET") -> Route:
        """Register the handler behind the service authentication wrapper."""
        return Route(path, for_people(handler), methods=[method])

    from mirrorwall.ui import routes as image_routes
    from soundroom.ui import routes as audio_routes

    app = Starlette(
        lifespan=lifespan,
        exception_handlers={HTTPException: http_error, 500: server_error},
        routes=[
            *image_routes(for_people, page, ask, model_api),
            *audio_routes(for_people, page, ask, model_api, settings),
            *plugin_routes(plugins, plugin_controller, for_people, page, go, typed),
            *conversations.routes,
            Route("/sign-in", sign_in_page),
            Route("/sign-in", sign_in, methods=["POST"]),
            Route("/sign-out", sign_out, methods=["POST"]),
            Route("/pulse", pulse),
            Route("/pair", pair_node, methods=["POST"]),
            Route("/", for_people(landing)),
            Route("/status", for_people(system_status)),
            route("/control", control, "POST"),
            route("/requests/{number:int}", request_page),
            route("/requests", requests_page),
            route("/requests/{number:int}/stop", stop_request, "POST"),
            route('/requests/unlogged/stop', stop_unlogged_request, 'POST'),
            route('/requests/{number:int}/feedback', feedback_do, 'POST'),
            route("/account", account),
            route("/account/appearance", account_appearance),
            route("/account/logs", account_logs),
            route('/voice', voice_chat),
            route('/voice/desktop', voice_download),
            route('/voice/browser/config', voice_config),
            route('/voice/browser/sessions', voice_start, 'POST'),
            WebSocketRoute('/voice/browser/sessions/{sid}/stream', voice_stream),
            route('/system/voice', voice_admin_page),
            route('/system/voice/status', voice_admin_page),
            route('/system/voice/{action}', voice_admin_do),
            route('/system/voice/{action}', voice_admin_do, 'POST'),
            route("/compute-nodes", compute_nodes_page),
            route("/settings", settings_page),
            route('/settings/image', image_settings_page),
            route('/settings/image', image_settings_page, 'POST'),
            Route("/guide", guide_moved),
            route("/docs", docs_page),
            route("/system/components", components_page),
            route("/docs/{section}", docs_page),
            route("/docs/{section}/{topic}", docs_page),
            Route("/backups", for_people(backups_page)),
            route("/backups/create", backup_create, "POST"),
            route("/backups/retention", backup_retention, "POST"),
            route("/backups/{backup_id}", backup_preview),
            Route("/databases", for_people(databases_page)),
            Route("/databases/api/{operation:path}", for_people(database_console_api), methods=["GET", "POST"]),
            Route("/databases/{machine}/operation/{operation:path}", for_people(database_node_operation), methods=["GET", "POST"]),
            route("/databases/{machine}/{database}/details", database_details),
            route("/databases/migrations/{profile}/preview", database_migration_preview, "POST"),
            route("/databases/migrations/{profile}/start", database_migration_start, "POST"),
            route("/databases/{machine}/{database}/dump", database_dump),
            route("/tools", tools_page),
            route("/tools", change_tools, "POST"),
            route("/tools/message", tools_message, "POST"),
            route("/cards", cards_page),
            route("/cards", change_cards, "POST"),
            route("/settings/gpus", change_cards, "POST"),
            route("/compute-nodes/engines", change_node_engine, "POST"),
            route("/compute-nodes/lifecycle", change_node_lifecycle, "POST"),
            route("/compute-nodes/gpus", change_cards, "POST"),
            route("/compute-nodes/machine-key", new_machine_key, "POST"),
            route("/compute-nodes/pair", make_pairing_code, "POST"),
            route("/compute-nodes/machine-key/schedule", schedule_machine_key, "POST"),
            route("/settings/machine-key", new_machine_key, "POST"),
            route("/settings/model-files", change_file_locations, "POST"),
            route("/settings/{program}", change_settings, "POST"),
            route('/workflows',workflows_page),
            route('/workflows',workflows_do,'POST'),
            route('/workflows/{workflow}',workflows_page),
            route('/workflows/{workflow}',workflows_do,'POST'),
            route('/collections',collections_page),
            route('/collections',collections_do,'POST'),
            route('/collections/{collection}',collections_page),
            route('/collections/{collection}',collections_do,'POST'),
            route('/collections/{collection}/sources/{chunk}',collection_source),
            route('/collections/{collection}/documents/{document}/download',collection_download),
            route("/chat", chat_page),
            route("/chat", chat_start, "POST"),
            route("/chat/{ref}", chat_page),
            route("/chat/{ref}", chat_do, "POST"),
            route("/chat/{ref}/stream", chat_stream),
            route("/benchmarks", benchmark_page),
            route("/benchmarks/run/{number}", benchmark_page),
            route("/benchmarks/{section}", benchmark_page),
            route("/benchmarks/{section}", benchmark_do, "POST"),
            route("/log", log_page),
            route('/reliability', reliability_page),
            route('/reliability', reliability_do, 'POST'),
            route('/log/reliability', reliability_do, 'POST'),
            route("/usage", usage_page),
            route("/allowances", allowances_page),
            route("/allowances", allowances_page, "POST"),
            route("/log/{number:int}", entry_page),
            route("/output", output_page),
            route("/ask", ask_model, "POST"),
            route("/users", users_page),
            route("/users", add_user, "POST"),
            route("/users/{name}", user_page),
            route("/users/{name}", change_user, "POST"),
            route("/groups", groups_page),
            route("/groups", add_group, "POST"),
            route("/groups/{name}", group_page),
            route("/groups/{name}", change_group, "POST"),
            route("/models", models_page),
            route("/model-files", model_files_picker),
            route("/models", add_model, "POST"),
            route("/measure-models", measure_models, "POST"),  # not under /models/, where everything after is a model's name
            route("/dry-fit", fit_page),  # nor is this
            route('/dry-fit', fit_do, 'POST'),
            route("/models/{name:path}", model_page),
            route("/models/{name:path}", change_model, "POST"),
            Route("/strip", for_people(strip_piece)),
            Route("/strip/stream", for_people(strip_stream)),
            route("/machines", machines_page),
            route("/telemetry", machines_page),
            route("/telemetry/{fingerprint}", telemetry_host),
            Route("/telemetry/nodes/{name}/wake", for_people(wake_node), methods=["POST"]),
            Route('/alerts', for_people(alert_page)),
            Route('/alerts/{identifier}', for_people(alert_action), methods=['POST']),
            Route('/schedules', for_people(schedules_page)),
            Route('/schedules', for_people(schedule_create), methods=['POST']),
            Route('/schedules/{identifier}/edit', for_people(schedule_edit), methods=['POST']),
            Route('/schedules/{identifier}', for_people(schedule_action), methods=['POST']),
            route("/machine/services/{name}/{action}", act_on_service, "POST"),
            route("/machine/power/{action}", power_action, "POST"),
            Route("/wake", for_people(wake_compute), methods=["POST"]),
            route("/account/display", choose_display, "POST"),
            route("/files", files_page),
            route("/files", files_do, "POST"),
            route("/file", file_page),
            route("/file", change_file, "POST"),
            route("/fingerprints", fingerprints_page),
            route("/roles", roles_page),
            route("/roles", roles_do, "POST"),
            route("/cloud-apis", cloud_apis_page),
            route("/cloud-apis", cloud_apis_do, "POST"),
            route("/outside", outside_redirect),
            route("/outside", cloud_apis_do, "POST"),
            route("/batches", batches_page),
            route("/batches", batch_do, "POST"),
            route("/batches/{number}", batch_file),
            route("/hub", hub_page),
            route("/hub", hub_start, "POST"),
            route("/downloads", downloads_part),
            route("/downloads", downloads_do, "POST"),
            route("/account/password", change_password, "POST"),
            route("/account/keys", make_key, "POST"),
            route("/account/keys/{key_id}/remove", remove_key, "POST"),
            Mount("/mirrorwall-static", StaticFiles(directory=HERE.parent / "mirrorwall" / "static")),
            Mount("/soundroom-static", StaticFiles(directory=HERE.parent / "soundroom" / "static")),
            Mount("/voice/static", StaticFiles(directory=HERE.parent / "voice" / "static")),
            Mount("/static", StaticFiles(directory=HERE / "static")),
        ],
    )

    app.state.conversations = conversations
    app.state.plugins = plugins
    app.state.plugin_controller = plugin_controller
    app.state.benchmarks = benchmarks
    app.state.databases = database_registry
    app.state.alerts = alerts
    app.state.schedules, app.state.schedule_worker = schedules, schedule_worker
    app.add_middleware(ClientOrigin)
    app.add_middleware(Barrier, gate=gate)
    from loadcoach.log import Log
    observation_log = Log(database_registry.database("weightroom.accounts"))
    database_operations.audit = lambda event, owner, **detail: observation_log.add('database', event, owner, machine='front', **detail)
    from loadcoach.observation_store import set_policy
    with observation_log.db:  # front's span policy, which every writer and pruner of this store follows
        set_policy(observation_log.db, settings.observation_keep_days, settings.observation_success_percent,
                   settings.observation_max_spans, tuple(settings.observation_quiet_routes))
    # Handle console failures before observation's generic JSON fallback, retaining its request ID.
    app.add_middleware(ExceptionMiddleware, handlers={Exception: server_error})
    observation.instrument(app, "weightroom", observation_log)
    return app


cli = typer.Typer(**CLI)
cli.add_typer(users_cli, name="user", help="Users, their keys and their passwords, kept on this machine.")
cli.add_typer(groups_cli, name="group", help="Groups, and what their members may do. Alone, it lists them.")
cli.add_typer(log_cli, name="log", help="Read what was done to the accounts. `log verify` checks that it has not been altered.")


@cli.callback(invoke_without_command=True)
def root(ctx: typer.Context, config: Annotated[Path, typer.Option(metavar="FILE", help="The settings file.")] = Path("settings/weightroom.toml")) -> None:
    # Each paragraph is one line here, because Typer keeps the line breaks it is given.
    """weightroom: the pages people use, on the front machine, and the accounts behind them.

    With no command it runs the site, which is what its service does: signing in, the first page with what compute is doing, Models, Files, Machine and My account. It keeps web chat, benchmark evidence and plugin settings on front. Pages ask compute through turnstile, with the key of whoever is signed in.

    The users, their keys and the groups are kept on this machine, in the store weightroom shares with turnstile. The `user` and `group` commands read and write it themselves. They need no key and work whether or not anything is running, which makes them the way back in if every key is lost. Unlike the site, they are held to no rules about who may raise whom: whoever can run them owns the file.

    Every setting is explained in settings/examples/weightroom.toml and settings/defaults/weightroom.toml. Its own key is in the file that key_file names, never in the settings.
    """
    if ctx.invoked_subcommand is None:
        settings = load_settings(config)
        # Long-lived browser streams must never make a routine service restart wait
        # indefinitely. Uvicorn cancels any connection that misses this drain window.
        return uvicorn.run(build_app(settings), host=settings.host, port=settings.port,
                           timeout_graceful_shutdown=5)

    @cache
    def accounts() -> tuple[None, Users, Log]:
        """The accounts on this machine, and the log of what was done to them, for the user, group and log commands."""
        told = read_settings(config)
        store = front_stores(told.get("databases", {}), config.parent, config.parent / told.get("data", ".")).database("weightroom.accounts")
        groups = {name: read_group(name, section) for name, section in told.get("groups", {}).items()}
        return None, Users(store, groups), Log(store, keep_days=0)

    ctx.obj = accounts
    told = read_settings(config) if config.is_file() else {}
    ctx.meta["stores"] = lambda: DatabaseRegistry.from_config(  # the front machine's stores, for roles-from
        told.get("databases", {}), config.parent,
        {"weightroom.roles": config.parent / told.get("data", ".") / "roles.db", "weightroom.benchmarks": config.parent / told.get("data", ".") / "benchmarks.db",
         "weightroom.accounts": config.parent / told.get("data", ".") / "accounts.db"})
    ctx.meta["keys"] = str((config.parent / told.get("turnstile", {}).get("hosted_keys", "") if told.get("turnstile", {}).get("hosted_keys")
                            else (config.parent / told.get("turnstile", {}).get("machine_key_file", "../keys/machine.key")).parent).resolve())


@cli.command("pair")
def pair(ctx: typer.Context, minutes: Annotated[int, typer.Option(help="How long the code works.")] = pairing.MINUTES) -> None:
    """Make a one-time code that lets a GPU node join this console, and print what to run on it.

    The code works once, for the minutes given. The node is recorded in this settings file when it
    joins, and turnstile and weightroom restart to use it.
    """
    config = Path(ctx.find_root().params["config"])
    told = read_settings(config)
    code, expires = pairing.make_code(config.parent / told.get("data", "."), "command-line", minutes)
    console = pairing.console_url("", told.get("port", 8440))
    until = datetime.fromtimestamp(expires).strftime("%H:%M")
    print(f"Pairing code {code}, good once until {until}. On the GPU machine, run:\n\n  {pairing.command(console, code)}\n")


@cli.command("accounts-from")
def accounts_from(ctx: typer.Context, file: Annotated[Path, typer.Argument(metavar="FILE", help="A copy of compute's LoadCoach database, such as data/loadcoach.db.")]) -> None:
    """Take the users, their keys and passwords, and the groups from compute's database, once.

    For the move to the front machine in milestone 76. Only hashes are kept, so every key and password goes on working. It refuses if there are users here already. The file is only read: compute's own copy stays as it was, which is the way back.
    """
    _, users, log = ctx.obj()
    if users.any():
        raise SystemExit("There are users here already. This is for the first move only.")
    taken = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    if taken.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='spending_allowances'").fetchone() and taken.execute('SELECT 1 FROM spending_allowances LIMIT 1').fetchone():
        taken.close()
        raise SystemExit('The legacy accounts-only move cannot copy spending policies. Use the verified full-store transfer workflow.')
    with users.db:
        users.db.execute("DELETE FROM groups")  # the first groups from the settings, which the move replaces
        for table in ("groups", "users", "keys"):
            rows = taken.execute(f"SELECT * FROM {table}")
            columns = [one[0] for one in rows.description]
            users.db.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", rows.fetchall())
    count, groups = len(users.memberships()), len(list(users.groups))
    log.add("user", "moved-here", "command-line", users=count, groups=groups, source=str(file))
    print(f"Took {count} users and {groups} groups from {file}.")


@cli.command("roles-from")
def roles_from(ctx: typer.Context, file: Annotated[Path, typer.Argument(metavar="FILE", help="A copy of compute's LoadCoach database, such as data/loadcoach.db.")]) -> None:
    """Take the roles and the last seven days of reliability from compute's database, once, and publish the benchmark evidence kept here.

    For the move to the front machine in milestone 83. It refuses if there are roles here already. The file is only read: compute's own copy stays as it was, which is the way back. Run it with turnstile stopped.
    """
    stores, (_, _, log) = ctx.meta["stores"](), ctx.obj()
    roles = Roles(stores.database("weightroom.roles"))
    if roles.names():
        raise SystemExit("There are roles here already. This is for the first move only.")
    taken = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    with roles.db:
        RoleEvidence(roles.db), Reliability(roles.db)  # their tables, made before they are filled
        for table, recent in (("roles", ""), ("reliability_state", ""), ("reliability_outcomes", " WHERE at >= ?")):
            rows = taken.execute(f"SELECT * FROM {table}{recent}", (time.time() - WINDOW,) if recent else ())
            columns = [one[0] for one in rows.description]
            roles.db.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", rows.fetchall())
    # The evidence is made from the benchmark runs kept here, so it is published again from them rather than copied.
    snapshot = Benchmarks(stores.database("weightroom.benchmarks")).role_snapshot()
    RoleEvidence(roles.db).accept(snapshot)
    count, outcomes = len(roles.names()), roles.db.execute("SELECT COUNT(*) FROM reliability_outcomes").fetchone()[0]
    log.add("role", "moved-here", "command-line", roles=count, outcomes=outcomes, evidence=len(snapshot["results"]), source=str(file))
    print(f"Took {count} roles and {outcomes} reliability outcomes from {file}, and published {len(snapshot['results'])} benchmark results.")


@cli.command("outside-from")
def outside_from(ctx: typer.Context, file: Annotated[Path, typer.Argument(metavar="FILE", help="A copy of compute's LoadCoach database, such as data/loadcoach.db.")]) -> None:
    """Take the outside services and the models they offer from compute's database, once.

    For the move of hosted models to the front machine (milestone 114). It refuses if there are services here already. The file is only read: compute's own copy stays as it was, which is the way back. The keys are not in a database: copy each NAME.key file by hand into the folder beside this machine's machine key, readable by you alone.
    """
    stores, (_, _, log) = ctx.meta["stores"](), ctx.obj()
    kept = Services(stores.database("weightroom.accounts"), ctx.meta["keys"])
    if kept.names():
        raise SystemExit("There are outside services here already. This is for the first move only.")
    taken = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    rows = taken.execute("SELECT name, settings FROM services ORDER BY position, name").fetchall()
    for name, settings in rows:
        kept.put(name, json.loads(settings))
    log.add("service", "moved-here", "command-line", services=len(rows), source=str(file))
    print(f"Took {len(rows)} outside services from {file}. Copy their NAME.key files into the keys folder by hand: " + ", ".join(f"{name}.key" for name, _ in rows) + ".")


@cli.command("batches-from")
def batches_from(ctx: typer.Context, file: Annotated[Path, typer.Argument(metavar="FILE", help="A copy of compute's LoadCoach database, such as data/loadcoach.db.")]) -> None:
    """Take the uploaded files, the batches and their requests from compute's database, once. Batches still under way go on here.

    For the move of batches to the front machine (milestone 115). External tasks (benchmarks) stay where they are. It refuses if there are batches here already. The file is only read: compute's own copy stays as it was, which is the way back. Run it with turnstile stopped.
    """
    stores, (_, _, log) = ctx.meta["stores"](), ctx.obj()
    kept = Batches(stores.database("weightroom.accounts"))
    if kept.db.execute("SELECT 1 FROM batches LIMIT 1").fetchone():
        raise SystemExit("There are batches here already. This is for the first move only.")
    taken = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    ours = ", ".join("?" for _ in ENDPOINTS)
    moved = {}
    with kept.db:
        batch_rows = taken.execute(f"SELECT * FROM batches WHERE endpoint IN ({ours})", ENDPOINTS)
        columns = [one[0] for one in batch_rows.description]
        wanted = batch_rows.fetchall()
        numbers = [row[columns.index("id")] for row in wanted]
        kept.db.executemany(f"INSERT INTO batches ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", wanted)
        moved["batches"] = len(wanted)
        files = {row[columns.index(name)] for row in wanted for name in ("input_file", "output_file", "error_file") if row[columns.index(name)]}
        for table, where, values in (("batch_lines", "batch IN ({})", numbers), ("files", "id IN ({})", sorted(files))):
            if not values:
                continue
            rows = taken.execute(f"SELECT * FROM {table} WHERE {where.format(', '.join('?' * len(values)))}", values)
            names = [one[0] for one in rows.description]
            found = rows.fetchall()
            kept.db.executemany(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", found)
            moved[table] = len(found)
    log.add("batch", "moved-here", "command-line", source=str(file), **moved)
    print(f"Took {moved.get('batches', 0)} batches, {moved.get('batch_lines', 0)} requests and {moved.get('files', 0)} files from {file}.")


@cli.command("log-from")
def log_from(ctx: typer.Context, file: Annotated[Path, typer.Argument(metavar="FILE", help="A copy of compute's LoadCoach database, such as data/loadcoach.db.")]) -> None:
    """Take compute's event log and the requests it keeps in full, once, into the log kept here, chained here as they come.

    For the move to the front machine in milestone 91. Each entry keeps its time and compute's number for it. It refuses if entries from compute are here already. The file is only read: compute's own copy stays as it was, which is the way back. Run it with turnstile stopped, before compute's LoadCoach starts sending its entries.
    """
    _, _, log = ctx.obj()
    if log.db.execute("SELECT 1 FROM events WHERE kind = 'log' AND what = 'moved-here' LIMIT 1").fetchone():
        raise SystemExit("Compute's log was moved here already. This is for the first move only.")
    # A move cut short goes on where it stopped: an entry that is here already is not taken again.
    taken = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    texts = [{"entry": entry, "asked": asked, "said": said} for entry, asked, said in taken.execute("SELECT entry, asked, said FROM texts")]
    rows = taken.execute(f"SELECT {COLUMNS} FROM events ORDER BY id")
    count = 0
    while batch := rows.fetchmany(1000):
        log.take("compute", [dict(zip(COLUMNS.split(", "), row)) for row in batch], [], 0)
        count += len(batch)
    log.take("compute", [], texts, len(texts))
    log.add("log", "moved-here", "command-line", entries=count, texts=len(texts), source=str(file))
    print(f"Took {count} entries and {len(texts)} requests kept in full from {file}.")


def main() -> None:
    """Run the configured WeightRoom console and its existing background workers."""
    try:
        cli()
    except ValueError as problem:  # a mistake in what was asked for: said in one line, not as a traceback
        raise SystemExit(str(problem)) from None
