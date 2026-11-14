"""LoadCoach: one OpenAI-compatible door in front of the GPUs.

A caller sends an ordinary OpenAI request with their API key. LoadCoach works out who they are
and what their group allows, reads the "model" field and puts the request in line. When its turn
comes, `llama-server` is started with that model's settings if it is not already running, the
request is forwarded, and the answer is streamed back.

This file is the web side: it turns each request into calls on the other three.

    config.py   reads and checks the settings
    users.py    who may do what
    runner.py   the line, the lock and the model servers

Opening / in a browser shows a test page.
"""

from __future__ import annotations

from loadcoach import observability as observation

import asyncio
import anyio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from contextlib import asynccontextmanager
from dataclasses import MISSING, dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from urllib.parse import unquote

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from loadcoach.web import diagnostic_page
from starlette.routing import Route

from db import DatabaseRegistry, local_path, schema_version
from db.recovery import RecoveryStore
from db.maintenance import Barrier, Gate
from loadcoach import batches as batch_rules
from loadcoach import hub
from loadcoach.batches import Batches, NoLocalBatches
from loadcoach.backups import (
    backup_root,
    compute_plan,
    create_backup,
    inventory as backup_inventory,
    manifest as backup_manifest,
    manifests as backup_manifests,
    store_inventory,
)
from loadcoach.backup_crypto import encryption_status
from loadcoach.database_admin import catalogs as database_catalogs, async_summaries, dump_tools, make_sql_dump, safe_filename, store_sources
from loadcoach.database_operations import DatabaseOperations
from db.health import stores_health, async_stores_health
from loadcoach.tasks import Tasks
from loadcoach.capture import answer_text, request_text, said_wrong
from loadcoach.config import NO_STORAGE, Config, checked, data_folder, read_settings, shared_places, storage_places
from loadcoach.settings import CHANGEABLE, NOW, Changes, front_programs, program_name, said, wrong_kind
from loadcoach.files import SPARE, carry, model_files_in, room, sha256_of
from loadcoach.file_index import NODE_INDEX, FileIndex, tier_of
from loadcoach.gguf import facts, goes_with, read_metadata
from loadcoach.log import Log, token_counts
from loadcoach.diagnostics import Timeline
from loadcoach import model_setup
from loadcoach.reliability import ANSWERED, OUTCOME, answer_outcome
from loadcoach.role_evidence import RoleEvidence
from loadcoach.schedule_grants import ScheduleGrants
from loadcoach.schedule_time import is_due, occurrence_ids
from engines import engine_for, engine_ids, catalogue as engine_catalogue
from loadcoach.models import request_context, Models, check_model, check_order, effective, files_of, lora_field
from loadcoach import waiting as waits
from loadcoach.loadouts import Conflict as LoadoutConflict, Loadouts
from loadcoach.media_setup import profile_key as loadouts_profile_key
from loadcoach.runner import Lock, Refusal, Runner, Ticket
from loadcoach.groups import ABILITIES, LEVELS
from loadcoach.front import Accounts
from loadcoach.statement import Statements, read_key, replace_key, sign, unseal, user_of
from loadcoach.users import ANONYMOUS, COMMAND_LINE, User

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
TEST_PAGE = Path(__file__).with_name("test.html")
TRACE = re.compile(r"[A-Za-z0-9._:-]{1,128}")  # what a caller may send as X-LoadCoach-Trace
KEEP_AT_MOST = 2_000_000  # bytes. A request larger than this, such as one carrying pictures, is not kept in full: only its size is

# Headers that describe one network hop rather than the message. Never copied across.
HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(eq=False)
class Underway:
    """A request between being let in and ending: what is needed to look at it, or to stop it.

    Where it has got to shows in which of `ticket`, `sending` and `upstream` is set. Stopping it
    means something different at each of the three.
    """

    entry: int  # the id of its `asked` entry in the event log
    user: str
    model: str
    stop_handle: str | None = None  # unlogged requests: ephemeral control, never a log ID
    owner_id: str | None = None  # stable account identity for the ephemeral control
    batch_id: str | None = None  # only an internally submitted batch request
    ticket: Ticket | None = None  # set from when it joins the line
    sending: asyncio.Future | None = None  # set once it has been sent on and nothing has come back yet
    upstream: httpx.Response | None = None  # set once the answer has begun to come back
    kind: str = ""  # the answer's content type
    # The last of the answer so far, in the pieces it arrived in. For a streamed answer a piece
    # is about one token, so this is roughly the last two thousand tokens.
    pieces: deque[bytes] = field(default_factory=lambda: deque(maxlen=2000))
    whole: bytearray = field(default_factory=bytearray)  # the entire answer so far, as it came
    stopped_by: str | None = None  # who stopped it, if anyone did
    looked_at_by: set[str] = field(default_factory=set)  # others who have looked at it, each put on record once
    diagnostics: Timeline | None = None


def reason(body: bytes) -> dict:
    """Why something was refused or went wrong, for the log: the message of an error body, or failing that the body's beginning."""
    try:
        why = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        why = body.decode(errors="replace")
    return {"why": str(why)[:500]} if why else {}


def error(status: int, message: str, headers: dict | None = None, code: str | None = None) -> JSONResponse:
    """An error in the shape OpenAI clients already know how to show.

    `type` says the error is LoadCoach's own. `code` is OpenAI's short name for the problem,
    given where OpenAI has one, because some tools look at it.
    """
    return JSONResponse(
        {"error": {"message": message, "type": "loadcoach_error", "code": code}}, status_code=status, headers=headers
    )


def build_app(config: Config, web: httpx.AsyncClient | None = None, front: httpx.AsyncClient | None = None, emergency: bool = False) -> Starlette:
    """Build the web app: its routes, and the Runner that owns the model servers.

    `web` is how it reaches Hugging Face, and `front` how it reaches turnstile. Tests hand in their own.
    In an `emergency`, the log is kept in memory only, and is gone when LoadCoach stops.
    """
    from loadcoach.devices import without_integrated
    config = without_integrated(config)
    databases = DatabaseRegistry.from_config(
        config.databases,
        Path(config.path).parent if config.path else Path.cwd(),
        {"loadcoach.core": config.database},
    )
    core = databases.database("loadcoach.core")
    from loadcoach.component_versions import Versions
    component_versions = Versions(core)
    # A store retired after the DB1 cutover keeps no task, batch, schedule or console-settings
    # records: the front keeps them. Any other store still serves its older records.
    with core.connection() as connection:
        retired = bool(schema_version(connection, "loadcoach.retirement"))
    gate = Gate(config.path)
    # No read timeout: a model can think for minutes before its first token.
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
    # With a front machine, the log that is kept is front's: entries go there (see front.py), and the chain is made there.
    log = Log(":memory:" if emergency else core, config.keep_days, config.keep_text, chained=not config.front or emergency)
    library = Models(core, config.model_defaults, config.models)  # the models there are, read live
    runner = Runner(config, client, log.add, library)
    loadouts = Loadouts(library, runner)
    # The accounts are front's. These are fetched from it only for work done later in someone's name: see front.py.
    users = Accounts(config.front, config.machine_key_file, front, machine_name=config.machine_name)
    schedule_grants = ScheduleGrants(core.connect(check_same_thread=False), users)

    def reliability_key(name, role=''):
        """The identity a model's reliability is kept under on the front machine: its model ID and the fingerprint of its settings."""
        return (library.identity(name) or '', library.fingerprint(library.get(name), name) or '', role)
    pace = {"at": 0.0, "of": {"speed": {}, "load": {}, "last_used": {}}}  # how fast each model answers and loads, and when it was last used, from front's log
    runner.paces = lambda: pace['of']

    # What model files said about themselves when last read, by path. A file is read again only
    # if its size or its date has changed.
    read_before: dict[str, tuple[tuple, dict]] = {}
    # The files of the places, with their descriptions, kept between looks. spotter looks again every
    # so often; LoadCoach answers from it and puts it right after each change it makes (loadcoach/file_index.py).
    file_index = FileIndex(data_folder(Path(config.path), {"data": config.data}) / NODE_INDEX if config.path else None)
    # Files being carried from one place to another right now, by the path they are leaving:
    # {"to": the place, "done": bytes so far, "of": bytes in all}.
    moving: dict[str, dict] = {}
    cleanup: dict = {}
    carrying: set[asyncio.Task] = set()
    # Set when a model file may be waiting to have its SHA-256 worked out, which a model's fingerprint needs. `read_files` does the work.
    hashing = asyncio.Event()
    # The last run that measured every model with no measure, or the one under way: who asked and since when, the models `left`,
    # `done` and `failed` with why, the one being loaded `now`, and how it `ended` once it has. Empty if there never was one.
    measuring: dict = {}
    stopping = threading.Event()  # set when LoadCoach shuts down, so that a file being read through is let go of
    # Files coming down from Hugging Face, and those that failed since LoadCoach started, by the path each will have:
    # {"repository", "file", "place", "state": coming, checking or failed, "done": bytes so far, "of": bytes in all}.
    # One that is still coming also has its "task".
    fetching: dict[str, dict] = {}
    # A minute with nothing arriving ends a download, which can then be continued. There is no limit on the whole of it.
    web = web or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=True)
    # Every request that has been let in and has not ended, by the id of its `asked` log entry.
    changes = Changes(core)  # what was changed in any program's settings from the site
    if not config.file:  # built with no file, as a test does: what it was built with stands for the file's
        config.file = {name: list(config.gpus) if name == "gpus" else getattr(config, name) for name in CHANGEABLE["loadcoach"]}
    under_way: dict[int, Underway] = {}
    unlogged_under_way: dict[str, Underway] = {}
    # What this llama-server says its flags are, read from its own help once and kept for as
    # long as the program file has the same date.
    flag_list: dict = {}
    # Empty while requests are taken. While draining: who asked for it, why, and since when.
    # New requests are turned away and those already here are finished, so the line empties.
    draining: dict = {}

    def not_now() -> Response:
        """What a new request is told while draining."""
        message = f"LoadCoach is taking no new requests: {draining['why']} Those already here are being finished."
        return error(503, message, {"retry-after": "30"}, code="draining")

    def address(request: Request) -> str:
        """Return the caller address, or a placeholder when unavailable."""
        return request.client.host if request.client else "?"
    # A fresh secret for the `loadcoach` command on this machine. Whoever can read the file it
    # is written to may do anything, which is already true of whoever can read the users file.
    cli_token = secrets.token_urlsafe(32)
    # Another, which never leaves this program. With it LoadCoach asks itself on a user's behalf, when the user is not there with a key:
    # a batch's request, answered hours after it was handed in.
    inner_token = secrets.token_urlsafe(32)
    batches = NoLocalBatches() if retired else Batches(core)
    batches.bind_models(library)

    async def who(request: Request) -> User | Response:
        """The user making this request, or the response that turns them away.

        Who a caller is, is turnstile's to say, on the front machine: it checks their key and sends
        a signed statement in its place (see statement.py). Besides that, only the `loadcoach`
        command on this machine, and LoadCoach itself for a batch, are known here.
        """
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if config.token_file and secrets.compare_digest(key.encode(), cli_token.encode()):
            return COMMAND_LINE
        said = getattr(request.state, "said", None)
        if said and said.get("caller"):
            trace = request.headers.get("x-loadcoach-trace", "")
            if trace.startswith("batch_"):
                request.state.batch_id = trace  # front's batch worker sends its request with this trace
            return user_of(said["caller"])
        if not config.machine_key_file and config.host in LOOPBACK:
            return ANONYMOUS  # no front to say who anyone is: this machine only, everything allowed
        if secrets.compare_digest(key.encode(), inner_token.encode()):  # LoadCoach itself, for the account named. It may do what that account may, as it is now
            request.state.batch_id = request.headers.get('x-loadcoach-trace')
            await users.fresh()
            return users.by_name(request.headers.get("x-loadcoach-for", "")) or error(401, "The account this was handed in by is gone or disabled.")
        return error(401, "Ask through turnstile, on the front machine. It checks your key and says here who you are.")

    def signed_in(handler):
        """Wrap a handler so it is only called for a known user, who is handed to it."""

        async def checked(request: Request) -> Response:
            """Resolve the signed caller before dispatching the protected handler."""
            user = await who(request)
            if not isinstance(user, Response):
                observation.identify(user)
            response = user if isinstance(user, Response) else await handler(request, user)
            # Being turned away is worth a record. A request that got as far as the line has an
            # entry of its own already, and says there how it ended.
            if response.status_code in (401, 403, 429) and not hasattr(request.state, "entry"):
                log.add(
                    "refused",
                    str(response.status_code),
                    "" if isinstance(user, Response) else user.name,
                    address(request),
                    path=request.url.path,
                    why=json.loads(response.body)["error"]["message"],
                )
            return response

        return checked

    def lacks(user: User, ability: str) -> Response | None:
        """The 403 for a user without `ability`, or None if they have it."""
        return None if user.can(ability) else error(403, f"Your group may not {ABILITIES[ability]}.")

    async def read_json(request: Request, allowed: set[str]) -> dict | Response:
        """The request body as a JSON object with only `allowed` fields, or the 400 saying why not."""
        try:
            if "attachments" in allowed:
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 15 * 1024 * 1024:
                        return error(413, "The message and attachments are too large.")
            else:
                body = await request.body()
            asked = json.loads(body or b"{}")
            unknown = asked.keys() - allowed
        except (ValueError, AttributeError):
            return error(400, "The body must be a JSON object.")
        return error(400, f"The body may set {sorted(allowed)}, nothing else.") if unknown else asked

    def offered(user: User) -> list[str]:
        """The models this user may name: the local ones their group may use. Outside models are front's."""
        return [name for name in library.names() if user.group.may_use(name)
                and engine_for(library.get(name)).installation(config).enabled]

    def described(name: str) -> dict:
        """One model, with the same four fields OpenAI gives. `created` is the age of its file."""
        try:
            created = int(Path(library.get(name)["flags"]["model"]).stat().st_mtime)
        except (OSError, TypeError):  # its file is missing, or the model was removed this instant
            created = 0
        # Two fields OpenAI does not have: exactly which model this is, and the llama-server that would run it. A benchmark keeps them with its result.
        marks = {"model_id": library.identity(name), "fingerprint": library.fingerprint(library.get(name), name), "build": runner.known_version(library.get(name))}
        marks['engine_id'] = engine_for(library.get(name)).id
        marks['capabilities'] = engine_for(library.get(name)).capabilities(library.get(name))
        if marks['capabilities'].get('rerank'):
            marks['rerank'] = True
        if marks['capabilities'].get('classifier'):
            marks.update(type='classifier', architecture={'output_modalities': ['decisions']},
                         limits=marks['capabilities']['limits'])
        marks.update(runner.model_metadata(name))
        if marks['capabilities'].get('image_generation'):
            from mirrorwall.model import defaults as image_defaults,limits as image_limits
            options=library.get(name)['sdcpp']
            marks['image_workflow']={'exclusive':library.get(name).get('alone',False),'family':options['family'],'defaults':image_defaults(library.get(name)),
                                     'limits':image_limits(library.get(name)),'split_mode':options.get('split_mode','single'),
                                     'reference_megapixels':options.get('reference_megapixels',1),
                                     'upscaling':bool(options.get('upscaler')),
                                     'references':'image-to-image' if options['family']=='sdxl' else 'conditioning',
                                     'loras':{key:{'scale':entry.get('scale',1)} for key,entry in options.get('loras',{}).items()}}
        if marks['capabilities'].get('audio'):
            from soundroom.model import profile as audio_profile
            if marks["engine_id"] == "whispercpp":
                from engines.whisper_cpp.model import profile as audio_profile
            marks['audio_profile'] = audio_profile(library.get(name), getattr(config, "engines", {}).get(marks["engine_id"], {}).get("manifest", ""))
            marks["translates"] = marks["capabilities"].get("translates", False)
        marks['name_history'] = library.name_history(name)
        marks['local_model_guard'] = True
        # What a model is, across machines: the SHA-256 of its weights file, absent until the file has been read through.
        marks['content_sha256'] = library.file_hash((library.get(name) or {}).get('flags', {}).get('model', ''))
        # The embedder mark that Collections, embedder benchmarks and /v1/embeddings rely on. It is
        # read from llama.cpp's `--embedding` flag (engines/llama_cpp/form.py); a future engine that
        # serves embeddings must set this mark from its own settings too.
        if (library.get(name) or {}).get('flags', {}).get('embedding'):
            marks['embedding'] = True
            configured = library.get(name)
            minimum_flags = {**configured['flags'], 'ctx-size': configured.get('min_context', configured['flags']['ctx-size'])}
            marks['context_tokens'] = request_context(minimum_flags)
        return {"id": name, "object": "model", "created": created, "owned_by": "loadcoach",
                **{key: value for key, value in marks.items() if value}, "order": library.place(name)}

    def no_such_model(name: str, user: User) -> Response:
        # A model the user may not use is answered exactly like one that is not there.
        """Hide unavailable or forbidden models behind the same not-found response."""
        return error(404, f"The model {name!r} does not exist. Available: {', '.join(offered(user))}", code="model_not_found")

    async def models(request: Request, user: User) -> Response:
        """GET /v1/models, as OpenAI has it: the configured models this user may use."""
        return JSONResponse({"object": "list", "data": [described(name) for name in offered(user)]})  # turnstile adds the roles

    # ---- what turnstile picks a role's model with --------------------------------------------
    #
    # Roles, their evidence and reliability are kept on the front machine, and turnstile picks a
    # role's model there (see turnstile/roles.py). Compute is sent a plain model name. What only
    # compute knows of each model at this moment is asked for here.

    async def paced() -> None:
        """How fast each model answers, how long it takes to load and when it was last used: from front's log, once a minute at most. From the day kept here if front does not answer."""
        if time.monotonic() - pace["at"] > 60:
            pace.update(at=time.monotonic(), of=await users.log_facts() or {**log.paces(), "last_used": log.last_used()})

    def supports_model_tools(name, settings):
        """Share catalogue and admission checks for GGUF templates and native tool capabilities."""
        flags = (settings or {}).get('flags', {})
        try:
            tools = settings is None or facts(said_by(flags['model'])).get('takes_tools', False)
        except (OSError, ValueError):
            tools = True  # The native runtime resolves unreadable metadata.
        template = runner.model_metadata(name).get('chat_template_caps', {})
        if 'supports_tools' in template and 'supports_tool_calls' in template:
            tools = template['supports_tools'] and template['supports_tool_calls']
        return tools and engine_for(settings).capabilities(settings).get('tools', True)

    async def model_facts(request: Request, user: User) -> Response:
        """Each model this user may use, with what is known of it now. Turnstile keeps it a few seconds.

        `why_not` says why a model cannot answer anything for this user just now, or is None. What
        one request needs, turnstile checks itself: `sees` for pictures, `tools`, `room` for the
        length of what was sent, and `context` for what the measured fit leaves. `warm` is true
        for one that is loaded with room for a request. `speed` and `load` are from the log.
        Outside models are front's: turnstile adds them.
        """
        await paced()
        found = {}
        for name in offered(user):
            settings = library.get(name)
            flags = (settings or {}).get("flags", {})
            server = None if settings is None else runner.serving(name, settings)
            actual_flags = server.settings["flags"] if server and server.ready and runner.answers(server, settings) else flags
            capabilities = engine_for(settings).capabilities(settings)
            tools = supports_model_tools(name, settings)
            why_not = "its file is missing" if not Path(flags["model"]).is_file() else None
            if settings and not why_not:
                need = runner.needs(settings)
                pool = runner.engine_cards(settings)
                impossible = not set(need) <= set(pool) if isinstance(need, list) else need > len(pool)
                if impossible:
                    why_not = "too few enabled cards for its configured GPU placement"
                elif settings.get('gpus') == 'any' and all(runner.impossible_layout(name, layout) for layout in runner.layouts(settings)):
                    why_not = "its measured minimum does not fit the enabled cards"
            if settings and not engine_for(settings).installation(config).enabled:
                why_not = "its inference engine is disabled on this node"
            if settings and not why_not and hasattr(engine_for(settings), "check_available"):
                try:
                    engine_for(settings).check_available(config, settings)
                except (ValueError, OSError) as problem:
                    why_not = str(problem)
            resident = bool(not why_not and server and server.ready and not server.dead() and runner.answers(server, settings))
            warm = not why_not and bool(resident and runner.busy(server) < runner.room_for(server.settings))
            placement = runner.fit(name, settings, set()) if settings and not why_not and not warm else None
            room = request_context(actual_flags)
            model_id, fingerprint, _ = reliability_key(name)
            found[name] = {"hosted": False, "why_not": why_not, "sees": capabilities.get("images", False), "tools": tools, "room": room,
                           "engine_id": engine_for(settings).id, "capabilities": capabilities, **runner.model_metadata(name),
                           "warm": warm, "resident": resident, "speed": pace["of"]["speed"].get(name), "load": pace["of"]["load"].get(name),
                           "placed": bool(placement), "context": request_context(placement[0]["flags"]) if placement else room,
                           "fit": "resident" if warm else "fits now" if placement else "wait for GPU admission",
                           "cards": list(server.cards) if warm and server else list(placement[1]) if placement else [],
                           "evictions": [one.model for one in placement[2]] if placement else [],
                           "model_id": model_id, "fingerprint": fingerprint,
                           # Answered at once, with no wait at all: loaded, room to spare, nobody in line, no one else's lock. Front's offload goes by it.
                           "free_now": not why_not and runner.free_now(name, request.headers.get("x-loadcoach-lock"))}
            if settings:
                found[name]['wait'] = ({'seconds': None, 'unknown': [why_not], 'basis': 'model unavailable'} if why_not else
                    waits.estimate(runner, Ticket(name, user.name, address(request), user.group.priority,
                        token=request.headers.get('x-loadcoach-lock'), settings=settings), pace['of']))
            found[name]['prompt_speed'] = pace['of'].get('prompt_speed', {}).get(name)
        try:
            environment = await tasks.probes.identify()  # the engine and machine, which benchmark evidence was measured on
        except (OSError, ValueError):
            environment = {}
        return JSONResponse({"models": found, "environment": environment}, headers={"cache-control": "no-store"})

    async def asked_of(request: Request, user: User) -> Response:
        """Answer a request, saying with it what turnstile keeps reliability with: which request it was, which model answered, and how it ended if that is known already."""
        from loadcoach.classifier import NATIVE_PATHS, MAX_BODY, Invalid, validate, shim_request, adapt_response
        native, shim = request.url.path in NATIVE_PATHS, None
        if native or request.url.path == '/v1/chat/completions':
            raw = await request.body()
            try:
                body = json.loads(raw)
                name = body.get('model') if isinstance(body, dict) else None
                settings = library.get(name) if isinstance(name, str) and name in offered(user) else None
                capabilities = engine_for(settings).capabilities(settings) if settings else {}
                classifier = capabilities.get('classifier')
                if native or classifier:
                    if len(raw) > MAX_BODY:
                        raise Invalid('Classifier request exceeds the 64 KiB body limit.')
                    if native:
                        sent = validate(body, capabilities.get('limits', {}).get('choices', 255))
                        if settings and not classifier:
                            raise Invalid('This model is not a Classifier. Choose a decision GGUF with a systemone template.', 501)
                    else:
                        sent, mappings = shim_request(body)
                        validate(sent, capabilities['limits']['choices'])
                        shim = (sent, mappings, body.get('stream', False))
                    scope = {**request.scope, 'path': '/v1/systemone', 'raw_path': b'/v1/systemone'}
                    request = Request(scope, request.receive)
                    request._body = json.dumps(sent).encode()
            except Invalid as problem:
                return error(problem.status, str(problem))
            except (ValueError, TypeError):
                if native:
                    return error(400, 'Classifier requests must be JSON objects.')
        answer = await forward(request, user)
        if shim:
            answer = await adapt_response(answer, *shim)
        if entry := getattr(request.state, "entry", 0):
            answer.headers.setdefault("x-loadcoach-request-id", str(entry))
        if answered := getattr(request.state, "answered", None):
            answer.headers.update(dict(zip(ANSWERED, answered)))
        if outcome := getattr(request.state, "outcome", None):  # a streamed answer is judged by turnstile as it passes
            answer.headers[OUTCOME] = outcome
        if estimate := getattr(request.state, 'wait_estimate', None):
            answer.headers.update(waits.headers(estimate))
        return answer

    async def model(request: Request, user: User) -> Response:
        """GET /v1/models/NAME, as OpenAI has it: that one model, or a 404."""
        name = request.path_params["name"]
        if name not in offered(user):
            return no_such_model(name, user)
        return JSONResponse(described(name))

    async def take_turn(
        request: Request,
        user: User,
        model: str | None,
        wants: Lock | None = None,
        flight: Underway | None = None,
        settings: dict | None = None,
        work: dict | None = None,
        validate_settings=None,
    ) -> Ticket | Response:
        """Wait in line on this request's behalf. Returns the ticket, or the response that ends it."""
        asked = request.headers.get("x-loadcoach-priority")
        if asked not in (None, "high", "normal", "low"):
            return error(400, "X-LoadCoach-Priority must be high, normal or low.")
        if asked == "high" and user.group.priority != "high" and (denied := lacks(user, "high")):
            return denied
        ticket = Ticket(
            model,
            user.name,
            caller=address(request),
            priority="lock" if wants else asked or user.group.priority,
            token=request.headers.get("x-loadcoach-lock"),
            wants=wants,
            may_load=user.can("load"),
            entry=flight.entry if flight else 0,
            stop_handle=flight.stop_handle if flight else None,
            settings=settings,
            observe=flight.diagnostics.stage if flight and flight.diagnostics else None,
            work=work,
            validate_settings=validate_settings,
        )
        if flight:
            flight.ticket = ticket
        waiting = asyncio.ensure_future(runner.acquire(ticket))
        # The body has been read, so the only thing left to arrive is the caller hanging up.
        hung_up = asyncio.ensure_future(request.receive())
        try:
            await asyncio.wait({waiting, hung_up}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            raise
        finally:
            hung_up.cancel()
            await asyncio.gather(hung_up, return_exceptions=True)
        if not waiting.done():
            waiting.cancel()  # the caller left, so its place in line goes too
            await asyncio.wait({waiting})
            return Response(status_code=499)  # nobody is there to read it
        try:
            if ticket.estimate is not None:
                request.state.wait_estimate = ticket.estimate
            return waiting.result()
        except Refusal as refusal:
            retry = {"retry-after": str(refusal.retry_after)} if refusal.retry_after else None
            return error(refusal.status, str(refusal), retry)

    async def forward(request: Request, user: User) -> Response:
        """Authorize, admit and forward inference while retaining permitted request evidence."""
        body = await request.body()
        if draining:
            return not_now()
        # A request that is to leave nothing behind: no entry in the log, no number, no kept text.
        # Asking for that without being allowed is turned away, and that is put on record.
        quiet = "x-loadcoach-log" in request.headers
        if quiet:
            if request.headers["x-loadcoach-log"] != "off":
                return error(400, "X-LoadCoach-Log can only be off.")
            if not config.unlogged:
                return error(403, "Requests that leave nothing behind are switched off here: unlogged is false in LoadCoach's settings.")
            if denied := lacks(user, "unlogged"):
                return denied
            if any(header in request.headers for header in ("x-loadcoach-sensitivity", "x-loadcoach-trace")):
                return error(400, "A request that leaves nothing behind cannot say a level or a piece of work, which are records.")
            observation.silence()
            request.state.entry = 0  # so that from here on nothing is written about it, not even that it was turned away
        try:
            asked = json.loads(body)
            name = asked.get("model")
        except (ValueError, AttributeError):  # not JSON, or JSON that is not an object
            name = None
        if not isinstance(name, str):
            return error(400, 'The request body must be JSON with a "model" field.')
        chose = {}  # for a request that named a role: the role as it was asked for, the model turnstile picked, and why
        if role := request.headers.get("x-loadcoach-role"):
            chose = {"role": role, "picked": name, "because": unquote(request.headers.get("x-loadcoach-because", ""))}
        # The least context the role's model must be admitted with. Under it, turnstile tries the role's next model.
        role_context = int(request.headers.get("x-loadcoach-role-context", "0")) if request.headers.get("x-loadcoach-role-context", "").isdigit() else 0
        if name not in offered(user):
            return no_such_model(name, user)
        from loadcoach.request_tools import tools_refusal
        if denied := tools_refusal(user, asked):
            return denied
        # The model's settings as they are at this moment. They are read again when its turn
        # comes, so a change made while it waits is the one that counts.
        settings = library.get(name)
        from loadcoach.request_tools import wants_tools
        if wants_tools(asked) and not supports_model_tools(name, settings):
            return error(400, 'This model does not support tool calls.', code='unsupported_tools')
        try:
            selected_engine = engine_for(settings)
            if request.url.path == '/v1/rerank':
                if not selected_engine.capabilities(settings).get('rerank'):
                    raise ValueError('Choose a reranker model for rerank requests.')
                from loadcoach.rerank import checked as checked_rerank
                checked_rerank(asked)
            elif selected_engine.capabilities(settings).get('rerank'):
                raise ValueError('This model serves reranking only; use /v1/rerank.')
            if request.url.path == '/v1/audio/translations':
                if not selected_engine.capabilities(settings).get('translates'):
                    raise ValueError('This model cannot translate speech.')
                if 'language' in asked:
                    raise ValueError('Translation always returns English; omit language.')
            if request.url.path.startswith('/v1/audio/') and not selected_engine.capabilities(settings).get('audio'):
                raise ValueError('Choose an audio model for audio requests.')
            if request.url.path.startswith('/v1/images/') and not selected_engine.capabilities(settings).get('image_generation'):
                raise ValueError('Choose an image-generation model for image requests.')
            if hasattr(selected_engine, "check_request"):
                selected_engine.check_request(request.url.path, asked)
        except ValueError as problem:
            return error(400, str(problem), code="unsupported_engine_operation")
        required_model = {field: request.headers.get('x-loadcoach-require-'+header) for field,header in
                          (('model_id','model-id'),('fingerprint','fingerprint'),('build','build'))}
        required_name = request.headers.get('x-loadcoach-require-model-name')
        guarded = any(value is not None for value in required_model.values())
        if guarded:
            if not all(isinstance(value,str) and 0 < len(value) <= 256 for value in required_model.values()):
                return error(400, 'A local model guard needs model-id, fingerprint and build headers together.')
        if required_name is not None and not 0 < len(required_name) <= 256:
            return error(400, 'A local model name guard needs a nonempty model name of at most 256 characters.')
        if settings and not Path(settings["flags"]["model"]).is_file():
            return error(503, f"The file of {name} is missing: {settings['flags']['model']}")
        limit = user.group.max_requests
        if limit and runner.count(user.name) >= limit:
            return error(429, f"You already have {limit} requests in line or running.", {"retry-after": "5"})

        # How sensitive this piece of work is. A level sticks to its trace: once any call in a
        # trace has been at a level, later calls in it are at least that, whatever they say.
        level, trace = request.headers.get("x-loadcoach-sensitivity", "normal"), request.headers.get("x-loadcoach-trace")
        if level not in LEVELS:
            return error(400, f"X-LoadCoach-Sensitivity must be one of {', '.join(LEVELS)}.")
        if trace is not None and not TRACE.fullmatch(trace):
            return error(400, "X-LoadCoach-Trace is up to 128 letters, digits, dots, dashes, underscores or colons.")
        if trace:
            level = max(level, log.trace_level(user.name, trace) or "normal", key=LEVELS.index)
        if LEVELS.index(level) > LEVELS.index(user.group.max_sensitivity):
            return error(403, f"Your group may not work at the {level} level.")
        if trace:
            log.set_trace_level(user.name, trace, level)

        if guarded or required_name is not None:
            actual = {'model_id':library.identity(name),'fingerprint':library.fingerprint(settings,name) if settings else None,
                      'build':runner.known_version(settings) or None}
            if not guarded:
                required_model = actual
            guarded = True
            if 'x-loadcoach-offload' in request.headers or required_model != actual or required_name is not None and required_name != name:
                message = 'The required local model identity, fingerprint or build does not match; no inference was sent.'
                if not quiet:
                    about = (user.name, address(request), name, trace, level)
                    entry = request.state.entry = log.add('request', 'asked', *about, must=level != 'normal',
                        path=request.url.path.removeprefix('/v1/'), group=user.group_name,
                        account_id=user.identity, key_id=user.key_identity, model_id=actual['model_id'])
                    log.add('request', 'refused', *about, of=entry, status=409, why=message,
                        code='local_model_guard_mismatch', mismatches=[field for field in actual if actual[field] != required_model[field]],
                        account_id=user.identity, key_id=user.key_identity)
                request.state.outcome = 'refused'
                return error(409, message, code='local_model_guard_mismatch')

        # Outside models, and the choice of one to answer in place of a local one that would have to wait, are front's.
        if "x-loadcoach-offload" in request.headers:
            return error(400, "X-LoadCoach-Offload is decided on the front machine. Ask through turnstile.")
        going = {}
        going |= chose
        reliability_identity = reliability_key(name)
        request.state.answered = (name, *reliability_identity[:2])
        going['model_id'] = library.identity(name)

        # Every entry about this request says the same who, from where, which model, trace and level.
        about = (user.name, address(request), name, trace, level)
        capturing = level != "normal"  # sensitive work: the text is kept as well as the facts
        try:
            text = None
            if capturing:
                text, results, new = request_text(asked, lambda fingerprint: log.seen(user.name, trace, fingerprint))
                for result in results:  # what a tool returned, as the harness reports it
                    log.add("tool", "result", *about, text=result, must=True, call=result["message"].get("tool_call_id"))
                log.mark_seen(user.name, trace, new)
            entry = request.state.entry = 0 if quiet else log.add(
                "request", "asked", *about, text=text, must=capturing, path=request.url.path.removeprefix("/v1/"), group=user.group_name,
                account_id=user.identity, key_id=user.key_identity, **going
            )
        except Exception as problem:
            if not log.db.database.is_error(problem):
                raise
            return error(503, f"This request is marked {level} and could not be recorded, so it was not run.")
        # The latest requests are kept in full, whatever their level, for finding out what went wrong.
        keeping = bool(entry and config.keep_text)
        # Generated images and audio never go into the log as bytes, in either direction.
        from mirrorwall import stream as image_stream
        from soundroom import stream as audio_stream
        media = image_stream if request.url.path.startswith('/v1/images/') else audio_stream if request.url.path.startswith('/v1/audio/') else None
        retained = json.dumps(media.metadata(asked)).encode() if media else body
        log.keep_asked(entry, retained.decode(errors="replace") if len(retained) <= KEEP_AT_MOST else json.dumps({"too_big": len(retained)}))

        flight = Underway(entry, user.name, name, batch_id=getattr(request.state, 'batch_id', None))
        if entry and not quiet:
            flight.diagnostics = Timeline(asked, settings, False)
        if entry:
            under_way[entry] = flight
        else:
            flight.stop_handle = secrets.token_urlsafe(24)
            flight.owner_id = user.identity
            unlogged_under_way[flight.stop_handle] = flight

        finished_once = False

        def ended(how: str, status: int, text: dict | None = None, **detail) -> None:
            """The second and last entry about this request: how it ended."""
            nonlocal finished_once
            if finished_once:
                return
            finished_once = True
            under_way.pop(entry, None)
            if flight.stop_handle:
                unlogged_under_way.pop(flight.stop_handle, None)
            if quiet:
                return
            if flight.stopped_by:  # whatever it looked like from here, it ended because someone stopped it
                how, detail = "stopped", {**detail, "by": flight.stopped_by}
            outcome = (
                'restart' if stopping.is_set()
                else 'cancelled' if how in ('stopped', 'hung up')
                else 'refused' if how == 'refused' and status != 502
                else detail.pop('reliability_outcome', 'error' if status >= 500 or how == 'failed' else 'refused' if status >= 400 else 'success')
            )
            # Benchmarks use separate probes. Durable benchmark batches, if any, are
            # also excluded from production ranking, regardless of their outcome.
            if flight.batch_id and (batches.get(flight.batch_id) or {}).get('metadata', {}).get('benchmark'):
                outcome = 'benchmark'
            request.state.outcome = detail['reliability_outcome'] = outcome
            detail.update(reliability_model_id=reliability_identity[0], reliability_fingerprint=reliability_identity[1])
            if flight.diagnostics:
                if flight.diagnostics.finished is None:
                    flight.diagnostics.finish()
                detail['diagnostics'] = {**flight.diagnostics.snapshot(), 'role': chose.get('role', '')}
                if rate := flight.diagnostics.native.get('prompt_tokens_per_second'):
                    detail['prompt_tokens_per_second'] = rate
            if flight.ticket and flight.ticket.estimate:
                detail['admission_estimate'] = flight.ticket.estimate
            log.add("request", how, *about, text=text, of=entry, status=status, group=user.group_name,
                    account_id=user.identity, key_id=user.key_identity, **detail)

        asked_at, marks = time.monotonic(), {}
        def validate_settings(current):
            """Reject disabled engines or incompatible local model configuration before admission."""
            selected = engine_for(current)
            if not selected.installation(config).enabled:
                raise Refusal(503, f"{selected.label} is disabled on this compute node.")
            try:
                if hasattr(selected, "check_request"):
                    selected.check_request(request.url.path, asked)
                if hasattr(selected, "check_available"):
                    selected.check_available(config, current)
            except (ValueError, OSError) as problem:
                raise Refusal(400, str(problem)) from None
        preparing = bool(config.max_estimated_wait)
        runner.admitting += int(preparing)
        try:
            if preparing:
                await tasks.probes.yield_to_foreground()
                await runner.look()
            await paced()
            ticket = await take_turn(request, user, name, flight=flight, settings=settings, work=None if media or selected_engine.capabilities(settings).get('image_generation') else waits.workload(asked), validate_settings=validate_settings)
        except asyncio.CancelledError:
            ended('hung up', 499)
            raise
        finally:
            runner.admitting -= int(preparing)
        if isinstance(ticket, Response):
            why = reason(ticket.body)
            outcome = 'timeout' if ticket.status_code == 502 and 'not ready after' in why.get('why', '') else 'error' if ticket.status_code == 502 else 'refused'
            if reliability_key(name) != reliability_identity:
                outcome = 'unscored'  # settings changed while loading; do not blame the previous fingerprint
            ended("hung up" if ticket.status_code == 499 else "refused", ticket.status_code, waited=round(time.monotonic() - asked_at, 1), reliability_outcome=outcome, **why)
            return ticket
        assert ticket.server  # dispatch sets it on every ticket that names a model
        try:
            admitted_engine = engine_for(ticket.settings)
            if hasattr(admitted_engine, "check_request"):
                admitted_engine.check_request(request.url.path, asked)
        except ValueError as problem:
            runner.release(ticket)
            ended("refused", 400, why=str(problem))
            return error(400, str(problem), code="unsupported_engine_operation")
        if flight.diagnostics:
            flight.diagnostics.configure(ticket.settings, ticket.server.cards)
            flight.diagnostics.context['context_tokens'] = request_context(ticket.server.settings['flags'])
        # The settings as its turn came, which a change while it waited may have given another fingerprint.
        reliability_identity = (library.identity(name) or reliability_identity[0], library.fingerprint(ticket.settings, name) or '', '')
        request.state.answered = (name, *reliability_identity[:2])
        admitted_context = request_context(ticket.server.settings["flags"])
        if admitted_context and role_context > admitted_context:
            runner.release(ticket)
            ended("refused", 502, reliability_outcome='refused', why="the admitted GPU context is too small for the role request")
            return error(502, "The admitted GPU context is too small for the role request; trying another role candidate.")
        # A model with no projector may be answered by the server of kin that has one. It still cannot see: only the model that was set up to see is sent pictures.
        sees = "mmproj" in ticket.settings["flags"] or "mmproj" not in ticket.server.settings["flags"]
        if not sees and any(isinstance(part, dict) and part.get("type") == "image_url" for said in asked.get("messages") or [] if isinstance(said, dict) and isinstance(said.get("content"), list) for part in said["content"]):
            runner.release(ticket)
            ended("refused", 400, waited=round(time.monotonic() - asked_at, 1), why="a picture, for a model with no projector")
            return error(400, f"{name} has no projector, so it cannot be sent pictures. Ask a model that was set up to see.")
        # Exactly which model answers, and the llama-server that runs it. `ticket.settings` are the model's as its turn came.
        marks = runner.marks(ticket.server, name, ticket.settings)
        observation.annotate(build=marks.get('build', ''))
        # The guard pins the model as configured, as the catalogue showed it: its settings when its turn came. Not the server as
        # started, whose context a fit may have shrunk at load time; that is still what the answer's own headers report.
        configured = library.fingerprint(ticket.settings, name)
        if guarded and required_model != {'model_id':library.identity(name),'fingerprint':configured,'build':marks.get('build')}:
            runner.release(ticket)
            ended('refused',409,reliability_outcome='refused',why='the required local model changed while waiting')
            return error(409,'The required local model changed while waiting; no inference was sent.')
        # The server may hold adapters for kin of this model as well. It is told which apply to this request: this model's, and no others.
        loaded, wanted = ticket.server.settings.get("adapters", {}), ticket.settings.get("adapters", {})
        if loaded or "lora" in asked:
            # Which adapters apply is LoadCoach's to say, from the model's settings. What a caller wrote there itself is taken out.
            sent = {key: value for key, value in asked.items() if key != "lora"} | ({"lora": lora_field(loaded, wanted)} if loaded else {})
            body = json.dumps(sent).encode()
        if wanted:  # said with every answer and in the log, so that nobody takes a plain model for one with an adapter
            marks = marks | {"adapters": ",".join(wanted)}
        if hasattr(admitted_engine, "prepare_request"):
            body = json.dumps(admitted_engine.prepare_request(request.url.path, asked)).encode()
        # From here the request counts as running, until `finish` is called exactly once.
        finish = partial(runner.release, ticket)
        priority = ticket.priority
        url = ticket.server.url + request.url.path
        # Our key and our own headers stay here. The model server never sees them.
        headers = {
            key: value
            for key, value in request.headers.items()
            if key not in HOP_HEADERS and key != "authorization" and not key.startswith("x-loadcoach-")
        }
        headers.update(ticket.server.headers)  # private engine credential only, never the caller's key
        if admitted_engine.id == 'sdcpp' and not quiet:
            headers['x-loadcoach-image-owner'] = hashlib.sha256((user.identity or user.name).encode()).hexdigest()
        started = time.monotonic()
        waited = round(started - asked_at, 1)
        if flight.diagnostics:
            flight.diagnostics.stage('upstream')

        # Without this, httpx asks for gzip on its own and the caller gets bytes it never asked for.
        headers.setdefault("accept-encoding", "identity")
        if capturing or keeping:
            headers["accept-encoding"] = "identity"  # an answer that is to be kept must be readable
        if request.url.query:
            url += "?" + request.url.query

        # Sent as a task of its own, so that `stop` has something to cancel while the model works
        # on the prompt and has sent nothing back.
        flight.sending = asyncio.ensure_future(
            client.send(client.build_request(request.method, url, headers=headers, content=body), stream=True)
        )
        try:
            upstream = flight.upstream = await flight.sending
        except httpx.HTTPError as problem:
            finish()
            ended("failed", 502, waited=waited, reliability_outcome='timeout' if isinstance(problem, httpx.TimeoutException) else 'error', why=f"{type(problem).__name__}: {problem}"[:500])
            return error(502, f"Model server did not answer: {type(problem).__name__}")
        except asyncio.CancelledError:
            # Either `stop` cancelled the sending, or this request itself was abandoned. Both ways
            # the connection is dropped, which is how the model server learns it can stop.
            flight.sending.cancel()
            finish()
            ended("hung up", 499, waited=waited)
            if not flight.stopped_by:
                raise
            return error(400, f"Stopped by {flight.stopped_by}.", code="stopped")
        flight.kind = upstream.headers.get("content-type", "")
        if flight.diagnostics:
            flight.diagnostics.stage('response')

        complete, broke, timed_out = False, "", False
        media_capture = media.Capture(upstream.headers.get('content-type', '')) if media else None

        async def answer():
            # Runs chunk by chunk. If the caller hangs up, `finally` closes the upstream
            # connection, which is how the model server learns it can stop generating.
            """Stream upstream output and release the connection and admission ticket on exit."""
            nonlocal complete, broke, timed_out
            try:
                async for chunk in upstream.aiter_raw():
                    if flight.diagnostics:
                        flight.diagnostics.chunk(chunk, flight.kind, 'content-encoding' not in upstream.headers)
                    if not quiet:
                        if media_capture:
                            media_capture.feed(chunk)
                        else:
                            flight.pieces.append(chunk)
                            flight.whole.extend(chunk)
                    yield chunk
                complete = True
            except (httpx.HTTPError, httpx.StreamClosed) as problem:  # the connection to the model server broke
                if not flight.stopped_by:  # `stop` closed the connection under us: end quietly
                    broke = f"{type(problem).__name__}: the model server stopped answering part-way."
                    timed_out = isinstance(problem, httpx.TimeoutException)
                    raise
            finally:
                try:
                    await upstream.aclose()
                finally:
                    if media_capture:  # generated media load weights on their first request: measure what the run left resident
                        with anyio.CancelScope(shield=True):
                            if not complete or broke:
                                await runner.stop(ticket.server)
                            else:
                                await runner.measure(ticket.server)
                    finish()
                    if media_capture and not quiet:
                        flight.whole = bytearray(media_capture.snapshot())
                        flight.pieces = [bytes(flight.whole)]
                    readable = "content-encoding" not in upstream.headers  # compressed bytes hold no numbers
                    if flight.diagnostics:
                        flight.diagnostics.finish(bytes(flight.whole), flight.kind, readable)
                    kept, calls = answer_text(bytes(flight.whole), upstream.headers.get("content-type", "")) if capturing or keeping else (None, [])
                    if keeping:
                        log.keep_said(entry, {**kept, "calls": calls} if calls else kept)
                    if not capturing:
                        kept, calls = None, []  # the log itself holds the text of sensitive work only
                    # Why it went wrong, if it did: the model server's own error, or that it stopped part-way.
                    why = {"why": broke} if broke else reason(b"".join(flight.pieces)) if upstream.status_code >= 400 else {}
                    # An answer may say 200 and carry an error inside, as an outside service's does when the model's provider fails. That is a failure, and its words are the reason.
                    inside_it = said_wrong(bytes(flight.whole)) if complete and upstream.status_code < 400 and readable else None
                    why = {"why": inside_it} if inside_it else why
                    ended(
                        "failed" if broke or inside_it else "done" if complete else "hung up",
                        upstream.status_code,
                        kept,
                        waited=waited,
                        ran=round(time.monotonic() - started, 1),
                        priority=priority,
                        reliability_outcome='timeout' if timed_out else 'error' if broke or inside_it or upstream.status_code >= 500 else 'refused' if upstream.status_code >= 400 else (media_capture.outcome() if media_capture else answer_outcome(bytes(flight.whole), flight.kind, request.url.path)) if complete and readable else 'unscored',
                        **marks,
                        **why,
                        **({**token_counts(b"".join(flight.pieces)[-4096:]), **({"answer_tokens": 0} if request.url.path == "/v1/rerank" else {})} if readable else {}),  # the counts come last
                    )
                    for call in calls:  # what the model asked a tool to do, each as its own entry
                        log.add("tool", "asked", *about, text={"arguments": call["arguments"]}, tool=call["tool"], call=call["id"], of=entry)

        headers = {key: value for key, value in upstream.headers.items() if key not in HOP_HEADERS}
        if entry:
            headers["x-loadcoach-request-id"] = str(entry)  # so a caller can point at this entry later
        for role_field, value in chose.items():  # x-loadcoach-role, x-loadcoach-picked and x-loadcoach-because: what was picked for a role, and why
            headers[f"x-loadcoach-{role_field}"] = value
        for mark, value in marks.items():  # x-loadcoach-fingerprint and x-loadcoach-build
            headers[f"x-loadcoach-{mark}"] = value
        if going.get('model_id'):
            headers['x-loadcoach-model-id'] = going['model_id']
        if configured:  # the model as configured, which is what a guard pins; x-loadcoach-fingerprint is the server as started
            headers['x-loadcoach-configured-fingerprint'] = configured
        return StreamingResponse(answer(), status_code=upstream.status_code, headers=headers)

    def status_for(user: User) -> dict:
        """The status as this user may see it: everyone's requests, or only their own and a count."""
        status, now = runner.status(), time.monotonic()
        status["hosted"] = []  # front adds the requests under way at outside services
        for part in ('running', 'waiting'):
            for entry in status[part]:
                flight = under_way.get(entry['id'])
                if flight and flight.batch_id:
                    entry['batch_id'] = flight.batch_id
        # How much is here in all, whoever it belongs to. A number gives nobody away, and it is
        # what says when a draining LoadCoach has emptied.
        status["busy"] = len(status["running"]) + len(status["waiting"]) + len(status["hosted"]) + len(status["holds"])
        status["draining"] = dict(draining) or None
        # Not counted as busy: a drain does not wait for a download, which can be gone on with later. spotter reads it, to keep the machine awake.
        status["downloads"] = sum("task" in one for one in fetching.values())
        status["batches"] = batches.open()  # written down and not yet ended. spotter reads this too: compute stays awake until they are done
        if not user.can("see-all"):
            for part in ("running", "waiting", "hosted", "holds"):
                mine = [entry for entry in status[part] if entry["user"] == user.name]
                status["others_" + part] = len(status[part]) - len(mine)
                status[part] = mine
        # Control handles are never log IDs or credentials. Only the owner or an
        # existing Stop operator sees them; other viewers retain the visible row.
        for part in ('running', 'waiting'):
            for entry in status[part]:
                handle = entry.get('stop_handle')
                flight = unlogged_under_way.get(handle) if handle else None
                if flight and (flight.owner_id == user.identity or user.can('stop')):
                    entry['may_stop'] = True
                else:
                    entry.pop('stop_handle', None)
        # Persisted in_progress batches include queued work. Only actual scheduler
        # execution and native probes qualify for the Current requests table.
        active_requests = {entry['id']: entry for entry in (*status['running'], *status['hosted'])}
        active = {}
        for flight in under_way.values():
            if flight.entry not in active_requests or not flight.batch_id:
                continue
            batch = batches.get(flight.batch_id)
            if not batch or batch['status'] not in batch_rules.OPEN:
                continue
            job = active.setdefault(flight.batch_id, {'id': flight.batch_id, 'model': flight.model,
                'user': flight.user, 'caller': 'batch', 'seconds': active_requests[flight.entry]['seconds'],
                'request_ids': [], 'href': '/batches', 'description': 'running batch'})
            job['request_ids'].append(flight.entry)
        for number, work in tasks.probes.working.items():
            if work.done() or work.cancelling():
                continue
            task = tasks.dispatched.get(number) or tasks.get(number)
            if not task or task['status'] != 'in_progress' or not (user.can('see-all') or task['user'] == user.name):
                continue
            active[number] = {'id': number, 'model': task['model'], 'user': task['user'], 'caller': 'benchmark',
                'seconds': round(now - tasks.probes.working_since.get(number, now), 1),
                'request_ids': [], 'href': '/batches', 'description': 'running batch'}
        status['active_batches'] = list(active.values())
        return status

    async def status(request: Request, user: User) -> Response:
        """Return current admission, resident-model and machine status for the caller."""
        await runner.look()  # what the cards hold, at most a few seconds old
        from loadcoach.machine_identity import fingerprint, hostname
        return JSONResponse({**status_for(user), "machine_fingerprint": fingerprint(), "hostname": hostname()},
                            headers={'cache-control': 'no-store'})

    async def me(request: Request, user: User) -> Response:
        """Who this caller is and what they may do, with what only compute knows: the models and the usage.

        Turnstile answers /loadcoach/me itself, from the accounts, and adds this to it.
        """
        group = user.group
        return JSONResponse(
            {
                "name": user.name,
                "identity": user.identity or user.name,
                "model_access": "*" if user.group.models is None else list(user.group.models),
                "chat": {"unlogged": config.unlogged, "models": {
                    name: {"context": request_context((library.get(name) or {}).get("flags", {})) or 8192,
                           "model_id": library.identity(name), "name_history": library.name_history(name),
                           "vision": bool((library.get(name) or {}).get("flags", {}).get("mmproj")),
                           "json_schema": engine_for(library.get(name)).capabilities(library.get(name)).get('json_schema', False),
                           "image_generation": engine_for(library.get(name)).capabilities(library.get(name)).get("image_generation", False),
                           "audio": bool(engine_for(library.get(name)).capabilities(library.get(name)).get("audio")),
                           "rerank": bool(engine_for(library.get(name)).capabilities(library.get(name)).get("rerank")),
                           "classifier": engine_for(library.get(name)).capabilities(library.get(name)).get('classifier', False),
                           "hosted": False, **runner.model_metadata(name)}
                    for name in offered(user)}},
                "group": user.group_name,
                "can": sorted(group.can),
                "models": offered(user),
                "priority": group.priority,
                "max_lock": group.max_lock,
                "max_hold": group.max_hold,
                "max_requests": group.max_requests,
                "max_sensitivity": group.max_sensitivity,
                "max_hosted_spend": group.max_hosted_spend,
                # The ToolLocker's tools shown to them, as names and patterns, and those taken away. Their levels are the ToolLocker's to apply.
                "tools": "*" if group.tools is None else list(group.tools),
                "hidden_tools": list(group.hidden_tools),
                "hosted_spent": round(log.spent(user.name), 4),  # in the last 24 hours
                # When this sign-in ends. None for an API key, which does not end.
                "session_ends": user.until and datetime.fromtimestamp(user.until).isoformat(timespec="seconds"),
                "via": user.via,  # the app this sign-in was made through, which it can do no more than. None otherwise
                "usage": log.usage(user.name),  # in the last 24 hours
            }
        )

    async def hold(request: Request, user: User) -> Response:
        """Load and pin a permitted model, or renew this account's existing hold."""
        if denied := lacks(user, "hold"):
            return denied
        asked = await read_json(request, {"model", "seconds"})
        if isinstance(asked, Response):
            return asked
        name, seconds = asked.get("model"), asked.get("seconds", 30)
        if not isinstance(name, str) or name not in library.names() or not user.group.may_use(name):
            return error(404, "No permitted local model by that name.")
        if type(seconds) not in (int, float) or not 0 < seconds <= user.group.max_hold:
            return error(400, f"seconds must be above zero and at most {user.group.max_hold:g}.")
        runner.held()
        if (name, user.name) in runner.holds and (server := runner.serving(name)) and not server.dead():
            runner.hold(name, user.name, seconds)
        else:
            if draining:
                return not_now()
            settings = library.get(name)
            if not Path(settings["flags"]["model"]).is_file():
                return error(404, "The model file is missing.")
            ticket = await take_turn(request, user, name, settings=settings)
            if isinstance(ticket, Response):
                return ticket
            runner.hold(name, user.name, seconds)
            runner.release(ticket)
        log.add("hold", "held", user.name, address(request), model=name, seconds=seconds)
        return JSONResponse({"model": name, "seconds": seconds})

    async def unhold(request: Request, user: User) -> Response:
        """Give up one's own hold. Administrative unload waits for remaining holds."""
        name = request.path_params["name"]
        runner.unhold(name, user.name)
        log.add("hold", "released", user.name, address(request), model=name)
        return JSONResponse({"model": name, "released": True})

    def in_place() -> Response | None:
        """The refusal for what would unload every model, while one of them is locked in place."""
        locked = [server.model for server in runner.servers.values() if not server.dead() and runner.locked(server)]
        return error(409, f"Locked in place: {', '.join(locked)}. Unlock on the status page first.") if locked else None

    async def model_lock(request: Request, user: User) -> Response:
        """Lock a model in place, so that once loaded it is not unloaded to make room or for being idle, or with `on` false let it go.

        It is written down, so it holds after a restart. It loads nothing: a locked model that
        is not loaded is kept from when something next loads it. Letting go lets its kin go too,
        since one server answers for them all.
        """
        if denied := lacks(user, "unload"):
            return denied
        asked = await read_json(request, {"name", "on"})
        if isinstance(asked, Response):
            return asked
        name, on = str(asked.get("name", "")), asked.get("on", True)
        if library.stored(name) is None:
            return error(404, f"There is no model named {name!r}.")
        if not isinstance(on, bool):
            return error(400, "on must be true or false.")
        for one in [name] if on else library.kin(name):
            library.set_locked(one, on)
        log.add("model", "locked" if on else "unlocked", user.name, address(request), name)
        runner.wake.set()  # a model let go may now make room for one that waits
        return JSONResponse({"name": name, "locked": on})

    async def unload(request: Request, user: User) -> Response:
        """Free every card. Waits in line like any request, so it never cuts off a running answer."""
        if denied := lacks(user, "unload"):
            return denied
        if refused := in_place():
            return refused
        await request.body()
        ticket = await take_turn(request, user, None)
        if isinstance(ticket, Response):
            return ticket
        runner.release(ticket)
        log.add("model", "unload-all", user.name, address(request))
        return JSONResponse(status_for(user))

    async def drain(request: Request, user: User) -> Response:
        """Stop taking new requests so that the line empties, or with `on` false, take them again.

        Nothing already here is touched: what is running and what is in line are finished.
        The body may set `on` (true unless said) and `why`, a few words that are told to callers
        who are turned away. It is forgotten when LoadCoach restarts.
        """
        if denied := lacks(user, "drain"):
            return denied
        asked = await read_json(request, {"on", "why"})
        if isinstance(asked, Response):
            return asked
        if asked.get("on", True):
            why = str(asked.get("why") or "it is being looked after.")[:200]
            draining.update(by=user.name, why=why, since=time.time())
            log.add("line", "draining", user.name, address(request), why=why)
        elif draining:
            draining.clear()
            log.add("line", "open", user.name, address(request))
        return JSONResponse(status_for(user))

    async def send_log(request: Request, user: User) -> Response:
        """Turnstile is about to show the log: send it what it does not have yet, now. Any caller may ask; it only sends what front would have in a moment."""
        if log.chained:
            return JSONResponse({"sent": False})
        try:
            await users.send_now(log, config.keep_text, dict, always=True)
        except (httpx.HTTPError, ValueError):
            return error(503, "The log could not be sent to the front machine just now.")
        return JSONResponse({"sent": True})

    async def alert_evidence(request: Request, user: User) -> Response:
        """Sanitized operational facts for operators and the trusted front collector: spending. Turnstile adds the model failures, which it keeps.

        No prompt, output, credential, request owner or raw exception is exported.
        Spending uses the existing retained-cost checks, not in-flight reservations.
        """
        if not user.can('manage-settings') and not user.can('mirror-users'):
            return error(403, 'Operational alert evidence requires manage-settings or mirror-users.')
        return JSONResponse({'spending': []})  # what may be spent is front's: the outside services and their keys are there

    async def lock(request: Request, user: User) -> Response:
        """Take the GPUs for this caller alone, or with a token, extend the lock it already holds.

        The body may set `seconds` (how long, default 600), `unload` (free the GPUs when the lock
        is taken) and `refuse` (turn other callers away at once instead of leaving them in line).
        """
        if denied := lacks(user, "lock"):
            return denied
        asked = await read_json(request, {"seconds", "unload", "refuse"})
        if isinstance(asked, Response):
            return asked
        if draining and not request.headers.get("x-loadcoach-lock"):  # a lock already held may still be extended
            return not_now()
        seconds = asked.get("seconds", 600)
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or seconds <= 0:
            return error(400, "seconds must be a number above 0.")
        if user.group.max_lock and seconds > user.group.max_lock:
            return error(403, f"Your group may lock for at most {user.group.max_lock:g} seconds at a time.")

        if asked.get("unload") is True and not request.headers.get("x-loadcoach-lock") and (refused := in_place()):
            return refused
        if token := request.headers.get("x-loadcoach-lock"):  # extending
            if not runner.renew(token, seconds):
                return error(409, "This lock has run out or was released.")
            log.add("lock", "extended", user.name, address(request), seconds=seconds)
            return JSONResponse({"token": token, "seconds": seconds})

        wanted = Lock(
            secrets.token_urlsafe(16),
            user.name,
            address(request),
            seconds,
            refuse=asked.get("refuse") is True,
            unload=asked.get("unload") is True,
        )
        ticket = await take_turn(request, user, None, wanted)
        if isinstance(ticket, Response):
            return ticket
        log.add("lock", "taken", user.name, address(request), seconds=seconds, refuse=wanted.refuse, unload=wanted.unload)
        return JSONResponse({"token": wanted.token, "seconds": seconds})

    async def unlock(request: Request, user: User) -> Response:
        """Let go of the lock this token holds. Without a token, break whatever lock there is."""
        if token := request.headers.get("x-loadcoach-lock"):
            if not runner.unlock(token):
                return error(409, "This lock has run out or was released.")
            log.add("lock", "released", user.name, address(request))
        elif denied := lacks(user, "break-lock"):
            return denied
        elif held := runner.holder():
            runner.break_lock()
            log.add("lock", "broken", user.name, address(request), held_by=held.user)
        return JSONResponse(status_for(user))

    async def clear(request: Request, user: User) -> Response:
        """Turn away everyone waiting in line. Answers under way carry on."""
        if denied := lacks(user, "clear-line"):
            return denied
        removed = runner.clear(user.name)
        log.add("line", "cleared", user.name, address(request), removed=removed)
        return JSONResponse({"removed": removed})

    def flight_for(request: Request, user: User, ability: str) -> Underway | Response:
        """The request under way that the path names, if this user may touch it. Their own always."""
        flight = under_way.get(request.path_params["entry"])
        if flight is None:
            return error(404, f"No request {request.path_params['entry']} is in line or being answered.")
        return flight if flight.user == user.name else lacks(user, ability) or flight

    async def stop(request: Request, user: User) -> Response:
        """End one request, wherever it has got to. The others carry on."""
        flight = flight_for(request, user, "stop")
        if isinstance(flight, Response):
            return flight
        await halt(flight, user.name)
        return JSONResponse({"stopped": flight.entry})

    async def halt(flight: Underway, by: str) -> None:
        """End a request, wherever it has got to."""
        flight.stopped_by = by
        if flight.upstream:  # being answered: closing the connection ends the answer here and there
            await flight.upstream.aclose()
        elif flight.sending:  # the model is working and has sent nothing back yet
            flight.sending.cancel()
        elif flight.ticket:  # still in line
            runner.remove(flight.ticket, by)

    async def stop_unlogged(request: Request, user: User) -> Response:
        """Stop one unlogged request without creating retained request evidence."""
        request.state.entry = 0
        asked = await read_json(request, {'handle'})
        if isinstance(asked, Response):
            return asked
        handle = asked.get('handle')
        flight = unlogged_under_way.get(handle) if isinstance(handle, str) else None
        if flight is None:
            return error(404, 'That unlogged request is no longer active.')
        if flight.owner_id != user.identity and (denied := lacks(user, 'stop')):
            return denied
        await halt(flight, user.name)
        return JSONResponse({'stopped': True}, headers={'cache-control': 'no-store'})

    async def request_diagnostics(request: Request, user: User) -> Response:
        """Live timing metadata has request-log permissions, independently of captured text."""
        flight = flight_for(request, user, 'see-log')
        if isinstance(flight, Response):
            return flight
        return JSONResponse({'state': stage(flight),
                             'diagnostics': flight.diagnostics.snapshot() if flight.diagnostics else None})

    async def peek(request: Request, user: User) -> Response:
        """The last of what a request has been answered so far, read as it passes. Nothing is kept."""
        flight = flight_for(request, user, "see-capture")
        if isinstance(flight, Response):
            return flight
        if flight.user != user.name and user.name not in flight.looked_at_by:
            # Reading someone else's words is worth a record. One record, however long they go on looking:
            # a page that follows the answer asks again every second.
            flight.looked_at_by.add(user.name)
            log.add("request", "peeked", user.name, address(request), flight.model, of=flight.entry)
        raw = b"".join(flight.pieces)
        if len(flight.pieces) == flight.pieces.maxlen:
            raw = raw[raw.find(b"\ndata:") + 1 :]  # the oldest piece kept may begin mid-line
        raw = raw[: raw.rfind(b"\n") + 1]  # and the newest may end mid-line
        said = answer_text(raw, flight.kind)[0] if raw else {}
        return JSONResponse({"id": flight.entry, "user": flight.user, "model": flight.model, "state": stage(flight), "said": said})

    def stage(flight: Underway) -> str:
        """How far a request under way has got, in a few words."""
        return "being answered" if flight.upstream else "sent, nothing back yet" if flight.sending else "in line"

    # ---- managing models ------------------------------------------------------------------

    async def model_list(request: Request, user: User) -> Response:
        """Every model with its settings, its file, and what it is doing right now."""
        if denied := lacks(user, "manage-models"):
            return denied
        await paced()
        listed, last_used = [], pace["of"]["last_used"]
        for name in library.names():
            settings, server = library.stored(name), runner.serving(name)
            memory_settings = runner.measured_settings(name)
            file = Path(settings["flags"]["model"])
            listed.append(
                {
                    "name": name,
                    "model_id": library.identity(name),
                    **settings,
                    "engine": engine_for(settings).id,
                    "capabilities": engine_for(settings).capabilities(library.get(name)),
                    **runner.model_metadata(name),
                    "host_ram_budget_mib": runner.hybrid_budget(settings),
                    'resident_measurements': library.resident_measurements(name, memory_settings),
                    'loadout_profile': loadouts_profile_key(library, name),
                    "size": file.stat().st_size if file.is_file() else None,  # None: the file is missing
                    "order": library.place(name),  # its sort order, 10 unless it was given another. The list is in that order already: see Models.names()
                    "loaded": server.state if server else None,
                    "cards": list(server.cards) if server else [],
                    # Loaded, but with settings that have since been changed. It is loaded afresh when next idle.
                    "stale": bool(server and not runner.answers(server, library.get(name))),
                    # What says exactly which model this is, and the SHA-256 of its file. None until the file has been read through.
                    "fingerprint": library.fingerprint(library.get(name), name),
                    "sha256": library.file_hash(settings["flags"]["model"]),
                    # What it was seen to use on each of its cards, in MiB, and when. None until it has loaded once with these settings.
                    "placement_gpus": runner.needs(memory_settings),
                    "measured": (seen := library.measure(name, memory_settings)) and {"mib": seen["mib"], "at": datetime.fromtimestamp(seen["at"]).isoformat(timespec="seconds")},
                    "image_profile": (server.image_profile if server else None) or (seen or {}).get('image_profile'),
                    "context_loaded": server.settings["flags"]["ctx-size"] if server else None,
                    "measurements": [{**point, "at": datetime.fromtimestamp(point["at"]).isoformat(timespec="seconds")}
                                     for point in library.measurements(name, memory_settings)],
                    "running": sum(ticket.model == name for ticket in runner.running),
                    "waiting": sum(ticket.model == name for ticket in runner.waiting),
                    "last_used": last_used.get(name),
                }
            )
        if any(one["fingerprint"] is None for one in listed):
            hashing.set()  # a file not read yet, such as one a model was given from the command line
        run = {key: value for key, value in measuring.items() if key != "task"} or None
        return JSONResponse({"defaults": config.model_defaults, "cards": list(runner.cards),
                             'devices': config.gpu_details,
                             'memory_policy': {'ram_offload': not config.disable_ram_offload},
                             'engine_cards': {engine: list(runner.engine_cards(engine)) for engine in engine_ids()},
                             "build": await runner.version(), "models": listed,
                             "to_measure": unmeasured(), "measuring": run})  # fmt: skip

    def model_files() -> dict[str, list[str]]:
        """Each model file in use, by its real place on the drive, with the models that use it."""
        used: dict[str, list[str]] = {}
        for name in library.names():
            for file in files_of(library.stored(name)).values():  # a model, the projector some carry, and its adapters
                used.setdefault(os.path.realpath(file), []).append(name)
        return used

    def places() -> dict[str, str]:
        """The places model files are kept, by name. With none configured, the folders the models are in, each named by its path."""
        if config.places:
            return dict(config.places)
        return {folder: folder for folder in sorted({str(Path(library.stored(name)["flags"]["model"]).parent) for name in library.names()})}

    def place_of(path: str) -> str | None:
        """The name of the place a file is in: its own folder, or the folder above that. None if it is in neither."""
        target=Path(path).resolve()
        return next((name for name, folder in places().items() if Path(folder).resolve() in list(target.parents)[:6 if Path(folder).name in ('image', 'audio', 'classifier') else 2]), None)

    def said_by(path: str) -> dict:
        """What a .gguf file says about itself, read once and remembered until the file changes."""
        seen = os.stat(path)
        if read_before.get(path, (None,))[0] != (seen.st_size, seen.st_mtime):
            read_before[path] = ((seen.st_size, seen.st_mtime), read_metadata(path))
        return read_before[path][1]

    async def file_list(request: Request, user: User) -> Response:
        """Every model file in every place, each with what it is, which models use it and when it was last used.

        A file no model uses is one that was downloaded and not added yet. Also the places
        themselves, with how much room is left on the drive under each. The files come from the
        kept index; `refresh=1` looks through the places again first.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        # Everything the database is asked is asked here, before the other thread starts. The
        # connection to it is used from one thread at a time.
        await paced()
        used, last_used, spots = model_files(), pace["of"]["last_used"], places()
        index = await asyncio.to_thread(file_index.current, spots, refresh=request.query_params.get("refresh") == "1")
        found, where, shared = [], [], shared_places(spots)
        for place, folder in spots.items():
            if not Path(folder).is_dir():
                continue  # a place whose folder is not there holds nothing
            # shared: the cache and storage are one folder, so its files are in both at once
            here = {"name": place, "folder": folder, "tier": tier_of(place), "shared": place in shared, **room(folder), "files": 0}
            where.append(here)
            for kept in index["files"].values():
                if kept["place"] != place:
                    continue
                here["files"] += kept["size"]
                models = used.get(os.path.realpath(kept["path"]), [])
                entry = {key: value for key, value in kept.items() if key != "mtime"}
                entry.update(used_by=models, moving=moving.get(kept["path"]), shared=place in shared,
                             # When any model that uses it was last asked for, as far back as the log goes.
                             last_used=max((last_used[name] for name in models if name in last_used), default=None))
                found.append(entry)
        # A part, which is an adapter or a projector, is listed with the model files here that it says it was made for, and the models that use each of those.
        whole = [file for file in found if file.get("facts", {}).get("what") in ("model", "classifier")]
        for part in found:
            if part.get("facts", {}).get("what") in ("adapter", "projector"):
                part["goes_with"] = [{"path": file["path"], "models": file["used_by"]} for file in whole if goes_with(part["facts"], file["facts"])]
        # A finished cleanup is reported for ten minutes, then drops off the Files page.
        shown = cleanup if cleanup.get("state") == "running" or time.time() - cleanup.get("ended", 0) < 600 else {}
        return JSONResponse({"places": where, "files": found, "cleanup": shown, "checked": FileIndex.checked(index),
                             "problems": index.get("problems", []),
                             "conversions": classifier_jobs.jobs.recent(user.identity or user.name)})

    def files_changed(*paths: str | Path) -> None:
        """Put the index right for files LoadCoach has just made, changed or removed."""
        try:
            file_index.update(list(paths), places())
        except OSError as problem:  # the next look puts it right
            print(f"loadcoach: the file index could not be updated: {problem}", file=sys.stderr, flush=True)

    def in_a_place(path: str) -> bool:
        """True for a .gguf file that is there, in one of the places. Only such a file may be moved or deleted."""
        from mirrorwall.assets import supported
        return supported(path) and Path(path).is_file() and place_of(path) is not None

    async def file_move(request: Request, user: User) -> Response:
        """Carry a file to another place. It starts now and runs on; the list of files shows how far it has got.

        The models that use the file are pointed at its new place when it has arrived, and only
        then is the old one taken away. A model that is loaded keeps answering from the old file
        and loads from the new one the next time it is idle. Nobody is cut off.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"path", "to"})
        if isinstance(asked, Response):
            return asked
        path, to = str(asked.get("path", "")), str(asked.get("to", ""))
        if not in_a_place(path):
            return error(404, "That is not a model file in one of the places.")
        if to not in places() or not Path(places()[to]).is_dir():
            return error(400, f"There is no place named {to!r} with a folder that exists. Places: {', '.join(places())}")
        target, size = Path(places()[to]) / Path(path).name, os.stat(path).st_size
        if place_of(path) == to or target.exists():
            return error(409, f"{to} already has a file named {target.name}.")
        if path in moving:
            return error(409, "That file is already being moved.")
        free = room(places()[to])["free"]
        if free < size + SPARE:
            return error(409, f"{to} has {free / 2**30:.1f} GB free. The file is {size / 2**30:.1f} GB, and 1 GB must be left over.")
        moving[path] = {"to": to, "done": 0, "of": size}
        log.add("file", "moving", user.name, address(request), path=path, to=to, size=size)
        task = asyncio.create_task(move(Path(path), target, user.name, address(request)))
        carrying.add(task)
        task.add_done_callback(carrying.discard)
        return JSONResponse({"moving": path, "to": to, "size": size}, status_code=202)

    async def file_copy(request: Request, user: User) -> Response:
        """Copy a file to another place, retaining the source and its model bindings."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"path", "to", "relative"})
        if isinstance(asked, Response):
            return asked
        path, to = str(asked.get("path", "")), str(asked.get("to", ""))
        if not in_a_place(path):
            return error(404, "That is not a model file in one of the places.")
        if to not in places() or not Path(places()[to]).is_dir():
            return error(400, f"There is no place named {to!r} with a folder that exists. Places: {', '.join(places())}")
        relative = asked.get("relative", Path(path).name)
        parts = Path(relative).parts if isinstance(relative, str) else ()
        root = Path(places()[to]).resolve()
        if not parts or Path(relative).is_absolute() or len(parts) > 6 or any(part in (".", "..") for part in parts):
            return error(400, "Choose a relative model file path inside the destination place.")
        target, size = root.joinpath(*parts), os.stat(path).st_size
        if not target.resolve().is_relative_to(root):
            return error(400, "The destination leaves its configured place.")
        if place_of(path) == to or target.exists():
            return error(409, f"{to} already has a file named {target.name}.")
        if path in moving:
            return error(409, "That file is already being copied or moved.")
        free = room(places()[to])["free"]
        if free < size + SPARE:
            return error(409, f"{to} has {free / 2**30:.1f} GB free. The file is {size / 2**30:.1f} GB, and 1 GB must be left over.")
        moving[path] = {"to": to, "done": 0, "of": size, "copy": True}
        log.add("file", "copying", user.name, address(request), path=path, to=to, size=size)
        task = asyncio.create_task(copy_file(Path(path), target, user.name, address(request)))
        carrying.add(task)
        task.add_done_callback(carrying.discard)
        return JSONResponse({"copying": path, "to": to, "target": str(target), "size": size}, status_code=202)

    async def copy_file(source: Path, target: Path, by: str, caller: str) -> None:
        """Carry one copy while leaving the source and model bindings untouched."""
        began = time.monotonic()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(carry, source, target, moving[str(source)])
            if known := library.file_hash(str(source)):
                library.keep_hash(str(target), known, target.stat().st_size, target.stat().st_mtime)
            log.add("file", "copied", by, caller, path=str(target), source=str(source), seconds=round(time.monotonic() - began, 1))
            await asyncio.to_thread(files_changed, target)
        except Exception as problem:
            log.add("file", "copy-failed", by, caller, path=str(source), why=f"{type(problem).__name__}: {problem}")
        finally:
            moving.pop(str(source), None)

    async def file_digest(request: Request, user: User) -> Response:
        """Hash only a complete model file in a configured place for verified distribution."""
        if denied := lacks(user, "manage-models"):
            return denied
        path = request.query_params.get("path", "")
        if not in_a_place(path) or path in moving:
            return error(404, "Choose a complete model file in cache/storage.")
        digest = await asyncio.to_thread(sha256_of, path)
        return JSONResponse({"path": path, "sha256": digest, "size": os.stat(path).st_size})

    async def file_content(request: Request, user: User) -> Response:
        """Stream a managed model file under the existing signed worker identity."""
        if denied := lacks(user, "manage-models"):
            return denied
        path = request.query_params.get("path", "")
        if not in_a_place(path) or path in moving:
            return error(404, "Choose a complete model file in cache/storage.")
        return FileResponse(path, media_type="application/octet-stream", headers={"cache-control": "no-store"})

    async def move(source: Path, target: Path, by: str, caller: str) -> None:
        """Carry one file, point its models at the new place, then take the old one away."""
        began = time.monotonic()
        try:
            await asyncio.to_thread(carry, source, target, moving[str(source)])
            if known := library.file_hash(str(source)):  # the same file in another place: its SHA-256 goes with it
                library.keep_hash(str(target), known, target.stat().st_size, target.stat().st_mtime)
            for name in library.names():  # found by where the file really is, however a model spells its path
                settings = library.stored(name)
                flags, old = settings["flags"], os.path.realpath(source)
                theirs = [flag for flag in ("model", "mmproj") if isinstance(flags.get(flag), str) and os.path.realpath(flags[flag]) == old]
                moved = {short: {**adapter, "file": str(target)} for short, adapter in settings.get("adapters", {}).items() if os.path.realpath(adapter["file"]) == old}
                if theirs or moved:
                    adapters = {"adapters": {**settings["adapters"], **moved}} if moved else {}
                    library.put(name, {**settings, "flags": {**flags, **dict.fromkeys(theirs, str(target))}, **adapters})
            os.unlink(source)
            read_before.pop(str(source), None)
            log.add("file", "moved", by, caller, path=str(target), was=str(source), seconds=round(time.monotonic() - began, 1))
            await asyncio.to_thread(files_changed, source, target)
            runner.wake.set()
        except Exception as problem:  # the original is untouched, and so are the models
            log.add("file", "move-failed", by, caller, path=str(source), why=f"{type(problem).__name__}: {problem}")
        finally:
            moving.pop(str(source), None)

    async def file_delete(request: Request, user: User) -> Response:
        """Delete a model file, at once and for good. `name` must be the file's own name, typed out, as the proof it was meant."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"path", "name"})
        if isinstance(asked, Response):
            return asked
        path = str(asked.get("path", ""))
        if not in_a_place(path):
            return error(404, "That is not a model file in one of the places.")
        if asked.get("name") != Path(path).name:
            return error(400, f"To delete it, give its file name exactly: {Path(path).name}")
        if using := model_files().get(os.path.realpath(path)):
            return error(409, f"It is used by {', '.join(using)}. Remove {'that model' if len(using) == 1 else 'those models'} first.")
        if path in moving:
            return error(409, "That file is being moved.")
        size = os.stat(path).st_size
        os.unlink(path)
        read_before.pop(path, None)
        log.add("file", "deleted", user.name, address(request), path=path, size=size)
        await asyncio.to_thread(files_changed, path)
        return JSONResponse({"deleted": path, "freed": size})

    async def files_cleanup(request: Request, user: User) -> Response:
        """Safely empty unused cache files, optionally preserving every cache file in storage first, or promote storage files into the cache."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"mode", "keep", "promote"})
        if isinstance(asked, Response):
            return asked
        mode = asked.get("mode")
        if mode not in ("clear-cache", "unused", "promote"):
            return error(400, "mode must be clear-cache, unused or promote.")
        if cleanup.get("state") == "running":
            return error(409, "File cleanup is already running.")
        if moving:
            return error(409, "Wait for current file copies or moves to finish before cleaning up.")
        configured, used = places(), set(model_files())
        # A folder that is the cache and storage at once is left alone: its files are the storage copies themselves.
        configured = {name: folder for name, folder in configured.items() if name not in shared_places(configured)}
        cache_places = {name: Path(folder) for name, folder in configured.items() if not name.startswith("storage_")}
        storage_places = {name.removeprefix("storage_"): Path(folder) for name, folder in configured.items() if name.startswith("storage_")}
        if mode == "clear-cache" and not storage_places:
            return error(409, "The cache and storage are one folder: there is no separate cache to clear." if shared_places(places())
                         else "Configure a model storage library before clearing the cache.")
        keep = asked.get("keep", [])
        if not isinstance(keep, list) or any(not isinstance(path, str) for path in keep):
            return error(400, "keep must be a list of cache file paths.")
        roots = [folder.resolve() for folder in cache_places.values()]
        protected = {os.path.realpath(path) for path in keep}
        if any(not any(Path(path).is_relative_to(root) for root in roots) for path in protected):
            return error(400, "Every kept file must be inside an active cache place.")
        used.update(protected)
        promote = asked.get("promote", [])
        if not isinstance(promote, list) or any(not isinstance(path, str) for path in promote):
            return error(400, "promote must be a list of storage file paths.")
        storage_roots = {name: folder.resolve() for name, folder in storage_places.items()}
        promote = [Path(os.path.realpath(path)) for path in promote]
        if any(not path.is_file() or not any(path.is_relative_to(root) for root in storage_roots.values()) for path in promote):
            return error(400, "Every promoted file must be a file inside a storage place.")

        def consolidate() -> dict:
            """Copy each named storage file into the cache place it belongs to, as Promote does for one file."""
            promoted, failures = 0, []
            for source in promote:
                place, root = next((name, root) for name, root in storage_roots.items() if source.is_relative_to(root))
                if place not in cache_places:
                    failures.append(f"{source.name}: no matching cache place")
                    continue
                target = cache_places[place] / source.relative_to(root)
                try:
                    if target.exists():
                        if target.stat().st_size != source.stat().st_size:
                            raise OSError(f"cache has a different-sized {target.name}")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    size = source.stat().st_size
                    if room(str(target.parent))["free"] < size + SPARE:
                        raise OSError(f"{target.parent} does not have enough free space")
                    carry(source, target, {"done": 0, "of": size})  # size-checked and published atomically; nothing is deleted, so no hash pass
                    promoted += 1
                except OSError as problem:
                    failures.append(f"{source.name}: {problem}")
            return {"promoted": promoted, "failures": failures}

        def clean() -> dict:
            """Apply the reviewed model-file cleanup and return counts and failures."""
            saved = removed = freed = 0
            failures = []
            for place, folder in cache_places.items():
                storage = storage_places.get(place)
                for source in model_files_in(str(folder)) if folder.is_dir() else []:
                    real = os.path.realpath(source)
                    in_use = real in used
                    if mode == "clear-cache" and storage is None:
                        failures.append(f"{place}: no matching storage place")
                        continue
                    target = storage / source.relative_to(folder) if storage else None
                    try:
                        if target is not None:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            created = False
                            if target.exists():
                                if target.stat().st_size != source.stat().st_size:
                                    raise OSError(f"storage has a different-sized {target.name}")
                            else:
                                size = source.stat().st_size
                                if room(str(storage))["free"] < size + SPARE:
                                    raise OSError(f"{storage} does not have enough free space")
                                progress = {"done": 0, "of": size}
                                carry(source, target, progress)
                                created = True
                            if sha256_of(str(source), stopping) != sha256_of(str(target), stopping):
                                if created:
                                    target.unlink(missing_ok=True)
                                raise OSError(f"storage copy of {target.name} did not match the cache file")
                            if created:
                                saved += 1
                        if not in_use:
                            size = source.stat().st_size
                            source.unlink()
                            read_before.pop(str(source), None)
                            removed += 1
                            freed += size
                    except OSError as problem:
                        failures.append(f"{source.name}: {problem}")
            return {"saved": saved, "removed": removed, "freed": freed, "protected": len(protected), "failures": failures}

        cleanup.clear()
        cleanup.update(state="running", mode=mode, by=user.name, started=datetime.now().isoformat(timespec="seconds"))

        async def work() -> None:
            """Run cleanup off the event loop and retain its completion or failure status."""
            try:
                result = await asyncio.to_thread(consolidate if mode == "promote" else clean)
                cleanup.update(state="complete", ended=time.time(), **result)
                log.add("file", {"clear-cache": "cache-cleared", "unused": "unused-cleaned", "promote": "cache-consolidated"}[mode],
                        user.name, address(request), **result)
            except (OSError, InterruptedError) as problem:
                cleanup.update(state="failed", ended=time.time(), failure=f"{type(problem).__name__}: {problem}")
            finally:
                await asyncio.to_thread(file_index.stale)

        task = asyncio.create_task(work())
        carrying.add(task)
        task.add_done_callback(carrying.discard)
        return JSONResponse(dict(cleanup), status_code=202)

    async def file_facts(request: Request, user: User) -> Response:
        """Everything one model file says about itself. Only for a file a model uses, or one in the folders looked through."""
        if denied := lacks(user, "manage-models"):
            return denied
        path = request.query_params.get("path", "")
        known = os.path.realpath(path) in model_files() or place_of(path) is not None
        from mirrorwall.assets import supported,image_asset,facts as image_facts
        if not known or not supported(path):
            return error(404, "That is not a model file LoadCoach knows of.")
        from soundroom.model import audio_asset, facts as audio_facts
        if 'classifier' in Path(path).parts and not path.lower().endswith('.gguf'):
            from loadcoach.classifier_sources import source_facts
            if not Path(path).is_file():
                return error(404, 'Classifier source is unavailable.')
            return JSONResponse({'path': path, 'size': os.stat(path).st_size, 'facts': source_facts(path), 'metadata': {}})
        if audio_asset(path):
            if not Path(path).is_file():
                return error(404, 'Audio package file is unavailable.')
            return JSONResponse({'path': path, 'size': os.stat(path).st_size, 'facts': audio_facts(path), 'metadata': {}})
        if image_asset(path):
            if not Path(path).is_file():
                return error(404,'Image component is unavailable.')
            return JSONResponse({'path':path,'size':os.stat(path).st_size,'facts':image_facts(path),'metadata':{}})
        try:
            metadata = await asyncio.to_thread(said_by, path)
        except (OSError, ValueError) as problem:
            return error(422, f"{path} could not be read as a model file: {problem}")
        description = facts(metadata)
        if 'classifier' in Path(path).parts and description.get('what') == 'model':
            from loadcoach.classifier_sources import source_facts
            description = {**description, **source_facts(path)}
        return JSONResponse({"path": path, "size": os.stat(path).st_size, "facts": description, "metadata": metadata})

    # ---- Hugging Face: finding model files and bringing them down -----------------------------
    #
    # A file comes down into PLACE/OWNER--NAME/, the folder named after its repository. That is
    # one folder below the place, where the Files page looks. Once it is whole it is a file like
    # any other, and is added as a model in the usual way.

    def hub_token() -> str:
        """The token of an account at Hugging Face. Read each time it is needed, so a new one needs no restart. Empty if there is none."""
        try:
            return Path(config.hub_token_file).read_text().strip() if config.hub_token_file else ""
        except OSError:
            return ""

    def download_token(repository=''):
        """Keep provider credentials separate and read private token files only on server."""
        if not repository.startswith('civitai/'):
            return hub_token()
        try:
            return Path(config.civitai_token_file).read_text().strip() if config.civitai_token_file else ''
        except OSError:
            return ''

    async def hub_look(request: Request, user: User) -> Response:
        """With `?search=WORDS`, the repositories at Hugging Face that match. With `?repository=OWNER/NAME`, the model files of that one.

        A file is listed with `here`, the place it already is in, and the parts of a split model
        are one row. The storage places are listed too, each with its free room, to choose where a
        file goes: a download is never put in the cache.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        words, repository = request.query_params.get("search", "").strip(), request.query_params.get("repository", "").strip()
        try:
            if not repository:
                if request.query_params.get('paged') == '1':
                    cursor = request.query_params.get('cursor', '')
                    if request.query_params.get('source') == 'civitai':
                        from mirrorwall.civitai import search_page as image_search_page
                        return JSONResponse(await image_search_page(web, words, download_token('civitai/'),
                            request.query_params.get('adult') == 'true', cursor,
                            request.query_params.get('kind', ''), request.query_params.get('base', '')))
                    return JSONResponse(await hub.search_page(web, words, hub_token(), cursor,
                        *hub.search_order(request.query_params), kind=request.query_params.get('kind', '')) if words else {'found': [], 'next_cursor': ''})
                if request.query_params.get('source')=='civitai':
                    from mirrorwall.civitai import search as image_search
                    return JSONResponse({'found':await image_search(web,words,download_token('civitai/'),request.query_params.get('adult')=='true') if words else []})
                return JSONResponse({"found": await hub.search(web, words, hub_token()) if words else []})
            if not hub.REPOSITORY.fullmatch(repository):
                return error(400, "A repository is written OWNER/NAME.")
            listed = hub.rows(await hub.files(web, repository, download_token(repository), classifier=request.query_params.get('kind') == 'classifier'))
        except hub.Refused as problem:
            return error(problem.status, str(problem))
        if request.query_params.get('kind') == 'classifier':
            for row in listed:
                row['decision_ready'] = row['file'].lower().endswith('.gguf') and await hub.decision_header(web, repository, row['file'], hub_token())
        spots = {name: folder for name, folder in places().items() if Path(folder).is_dir()}
        for row in listed:
            names = [Path(part["path"]).name for part in row["parts"]]
            row["here"] = next((name for name, folder in spots.items() if all((Path(folder) / hub.folder_of(repository) / one).exists() for one in names)), None)
            row["parts"] = len(names)
        kept = sorted(storage_places(spots).items())
        return JSONResponse({"repository": repository, "files": listed, "places": [{"name": name, "free": room(folder)["free"]} for name, folder in kept]})

    def left_behind() -> dict[str, dict]:
        """Downloads that were not finished and are not coming now, by the path each would have. Found by the `.part` files they left."""
        found = {}
        for place, folder in places().items():
            for part in Path(folder).glob('*--*/*.part'):
                path = str(part)[: -len(".part")]
                if "task" not in fetching.get(path, {}):
                    found[path] = {"repository": part.parent.name.replace("--", "/"), "file": part.name[: -len(".part")], "place": place, "state": "stopped", "done": part.stat().st_size, "of": None}
        return found

    async def download_list(request: Request, user: User) -> Response:
        """Every download: coming, being checked, failed, or stopped and left half done. `speed` is bytes a second, since it began or was gone on with."""
        if denied := lacks(user, "manage-models"):
            return denied
        listed = []
        for path, one in {**left_behind(), **fetching}.items():  # one that failed says why, which its `.part` file alone could not
            shown = {"path": path, **{key: value for key, value in one.items() if key not in ("task", "start", "since")}}
            if "task" in one:
                shown["speed"] = round((one["done"] - one["start"]) / max(time.monotonic() - one["since"], 1))
            listed.append(shown)
        return JSONResponse({"downloads": listed})

    async def image_check(request: Request, user: User) -> Response:
        """What a draft image profile's files are and what it still needs, read from their headers on this machine."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"settings"})
        if isinstance(asked, Response):
            return asked
        from mirrorwall.components import check as image_parts
        spots = places()
        return JSONResponse(await asyncio.to_thread(image_parts, asked.get("settings") or {}, spots.get("image", ""), list(spots.values())))

    async def download_start(request: Request, user: User) -> Response:
        """Bring `files` of `repository` down into the storage place `place`. Each starts now and runs on; the list of downloads shows how far it has got.

        Naming one part of a split model brings all its parts. A file left half done is gone on
        with from where it stopped. `place` may be left out where there is only one storage place.
        A cache place is refused: downloads are kept in storage alone.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"repository", "files", "place"})
        if isinstance(asked, Response):
            return asked
        repository, names, spots = asked.get("repository"), asked.get("files"), storage_places(places())
        place = asked.get("place") or (next(iter(spots)) if len(spots) == 1 else "")
        if not isinstance(repository, str) or not hub.REPOSITORY.fullmatch(repository) or not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
            return error(400, "Give repository as OWNER/NAME, and files as a list of the files wanted from it.")
        if not spots:
            return error(409, NO_STORAGE)
        if place not in spots or not Path(spots[place]).is_dir():
            return error(400, f"Downloads go into storage, never the cache. There is no storage place named {place!r} with a folder that exists. Storage places: {', '.join(spots)}")
        try:
            wanted = hub.chosen(await hub.files(web, repository, download_token(repository), classifier=Path(spots.get(place, '')).name == 'classifier'), names)
        except hub.Refused as problem:
            return error(problem.status, str(problem))
        folder = Path(spots[place]) / hub.folder_of(repository)
        targets = [folder / Path(entry["path"]).name for entry in wanted]
        if len(set(targets)) != len(targets):
            return error(409, "The selected repository files share a filename. Download them separately into different places.")
        if there := [target.name for target in targets if target.exists()]:
            return error(409, f"{place} already has {', '.join(there)} of that repository.")
        if coming := [target.name for target in targets if "task" in fetching.get(str(target), {})]:
            return error(409, f"Already coming down: {', '.join(coming)}.")
        # What is still to come: these files, less what is there of them, and what other downloads into this place have not brought yet.
        parts = [target.with_name(target.name + ".part") for target in targets]
        retained = [part.stat().st_size if part.exists() else 0 for part in parts]
        needed = sum(entry["size"] - have if have <= entry["size"] else entry["size"]
                     for entry, have in zip(wanted, retained))
        owed = sum(max(0, one["of"] - one["done"]) for one in fetching.values() if "task" in one and one["place"] == place)
        free = room(spots[place])["free"]
        if free < needed + owed + SPARE:
            return error(409, f"{place} has {free / 2**30:.1f} GB free. {(needed + owed) / 2**30:.1f} GB is to come, and 1 GB must be left over.")
        folder.mkdir(exist_ok=True)
        for entry, target in zip(wanted, targets):
            fetching[str(target)] = {"repository": repository, "file": target.name, "place": place, "state": "coming", "done": 0, "of": entry["size"], "start": 0, "since": time.monotonic()}
            log.add("file", "downloading", user.name, address(request), path=str(target), repository=repository, size=entry["size"])
            task = asyncio.create_task(download(repository, entry, target, user.name, address(request)))
            fetching[str(target)]["task"] = task
            carrying.add(task)
            task.add_done_callback(carrying.discard)
        return JSONResponse({"downloading": [str(target) for target in targets], "to": place, "size": sum(entry["size"] for entry in wanted)}, status_code=202)

    async def download(repository: str, entry: dict, target: Path, by: str, caller: str) -> None:
        """Bring one file down, and note in the log how it ended."""
        progress, began = fetching[str(target)], time.monotonic()
        try:
            await hub.fetch(web, repository, entry, target, download_token(repository), progress)
            if entry["sha256"]:  # it was just checked against this, so it need not be worked out again
                library.keep_hash(str(target), entry["sha256"], target.stat().st_size, target.stat().st_mtime)
            log.add("file", "downloaded", by, caller, path=str(target), repository=repository, size=entry["size"], seconds=round(time.monotonic() - began, 1))
            del fetching[str(target)]
            await asyncio.to_thread(files_changed, target)
        except asyncio.CancelledError:  # stopped by someone, or LoadCoach is stopping. What came stays, as a `.part` file
            del fetching[str(target)]
            raise
        except Exception as problem:  # it stays in the list, with the reason, until it is gone on with or thrown away
            why = str(problem) if isinstance(problem, (hub.Refused, OSError)) else f"{type(problem).__name__}: {problem}"
            del progress["task"]
            progress.update(state="failed", why=why)
            log.add("file", "download-failed", by, caller, path=str(target), repository=repository, why=why)

    async def download_stop(request: Request, user: User) -> Response:
        """Stop a download that is coming. What came so far stays, and it can be gone on with."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"path"})
        if isinstance(asked, Response):
            return asked
        path = str(asked.get("path", ""))
        if "task" not in fetching.get(path, {}):
            return error(404, "No download to that path is coming down.")
        fetching[path]["task"].cancel()
        log.add("file", "download-stopped", user.name, address(request), path=path, done=fetching[path]["done"])
        return JSONResponse({"stopped": path})

    async def download_discard(request: Request, user: User) -> Response:
        """Throw away a download that failed or was stopped: what came down of it, and its line in the list."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"path"})
        if isinstance(asked, Response):
            return asked
        path = str(asked.get("path", ""))
        if "task" in fetching.get(path, {}):
            return error(409, "That download is coming down. Stop it first.")
        if path not in {**left_behind(), **fetching}:
            return error(404, "There is no unfinished download to that path.")
        fetching.pop(path, None)
        part = Path(path + ".part")
        freed = part.stat().st_size if part.exists() else 0
        part.unlink(missing_ok=True)
        try:
            part.parent.rmdir()  # the repository's folder goes with it, if nothing else is in it
        except OSError:
            pass
        log.add("file", "download-discarded", user.name, address(request), path=path, size=freed)
        return JSONResponse({"discarded": path, "freed": freed})

    async def engines_list(request: Request, user: User) -> Response:
        """Configured engine schemas and availability, without loading a model or executing help."""
        if not user.can("manage-models") and not user.can("manage-settings"):
            return error(403, "Engine settings require manage-models or manage-settings.")
        listed = engine_catalogue(config)
        for engine in listed:
            engine['cards'] = list(runner.engine_cards(engine['id']))
            engine["build"] = runner.known_version({"engine": engine["id"]})
        return JSONResponse({"engines": listed}, headers={"cache-control": "no-store"})

    async def components_here(request: Request, user: User) -> Response:
        """Which openWeightGym services and engines this machine has. Changes nothing and runs no engine."""
        if denied := lacks(user, "manage-settings"):
            return denied
        from loadcoach import components
        found = await components.here(config, running_here=("loadcoach",), versions=component_versions)
        for engine in found["engines"]:
            engine["build"] = runner.known_version({"engine": engine["id"]}) if engine["installed"] else ""
            if engine['id'] == 'llama_cpp' and engine['installed']:
                build = re.search(r'build (\d+)', engine['build'] or '')
                if build and int(build[1]) < 11361:
                    engine['why'] = (engine.get('why', '') + ' Classifier inference requires a build with /v1/systemone.').strip()
        found['tools'] = [classifier_jobs.status()]
        return JSONResponse(found, headers={"cache-control": "no-store"})

    async def classifier_operations(request: Request, user: User) -> Response:
        """Expose owner-scoped conversion receipts and on-demand CPU tool installation."""
        tools = request.url.path.endswith('classifier-tools')
        if denied := lacks(user, 'manage-settings' if tools else 'manage-models'):
            return denied
        owner = user.identity or user.name
        if request.method == 'GET':
            return JSONResponse({'tools': classifier_jobs.status(), 'jobs': classifier_jobs.jobs.recent(owner)}, headers={'cache-control': 'no-store'})
        try:
            body = await read_json(request, set() if tools else {'path'})
            if isinstance(body, Response):
                return body
            job = await classifier_jobs.install(owner) if tools else await classifier_jobs.convert(owner, str(body.get('path', '')))
            return JSONResponse(job, 202, headers={'cache-control': 'no-store'})
        except (ValueError, OSError) as problem:
            return error(400, str(problem))

    async def flags(request: Request, user: User) -> Response:
        """The selected engine's installed, model-launch option catalogue."""
        if denied := lacks(user, "manage-models"):
            return denied
        try:
            engine = engine_for(request.query_params.get("engine", "llama_cpp"))
            installed = engine.installation(config)
            if not installed.enabled:
                return error(503, f"{engine.label} is disabled on this compute node.")
            supported = await engine.supported_flags(installed.executable, flag_list)
        except ValueError as problem:
            return error(503, str(problem))
        return JSONResponse({"flags": supported, "ours": sorted(engine.model.OUR_FLAGS), "engine": engine.id})

    async def model_put(request: Request, user: User) -> Response:
        """Add a model, or replace the settings of one that exists. Nothing running is disturbed.

        With `?new` in the address it only adds, and refuses a name that is taken.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        name = request.path_params["name"]
        settings = await read_json(request, {"engine", "flags", "strata", "sdcpp", "audiocpp", "whispercpp", "gpus", "alone", "allow_split", "adapters", "min_context", "order"})
        if isinstance(settings, Response):
            return settings
        # `order` is the model's sort order and not one of its settings. Left out, the model keeps the one it has: 10, for one being added. null puts it back at 10.
        ordered_too, order = "order" in settings, settings.pop("order", None)
        try:
            check_order(order)
            engine_for(settings)
            flags = settings.get("flags")
            check_model(name, effective(settings, config.model_defaults) if isinstance(flags, dict) else settings, config.gpus)
        except ValueError as problem:
            return error(400, str(problem))
        existed = library.stored(name) is not None
        if existed and "new" in request.query_params:  # asked to add, and never to replace
            return error(409, f"There is already a model named {name}.")
        library.put(name, settings)
        if ordered_too:
            library.set_place(name, order)
        hashing.set()  # its file may be one that was never read through
        log.add("model", "changed" if existed else "added", user.name, address(request), name, **settings)
        runner.wake.set()  # someone may be waiting for exactly this
        return JSONResponse({"name": name, **settings}, status_code=200 if existed else 201)

    async def model_setup_page(request: Request, user: User) -> Response:
        """Family-shared presets and entry-specific reviewed changes in the ordinary model store."""
        if denied := lacks(user, "manage-models"):
            return denied
        if request.method == "GET":
            name = request.query_params.get("name", "")
            if library.stored(name) is None:
                return error(404, "That local model does not exist.")
            undo = library.setup_undo(name)
            return JSONResponse({"presets": library.presets(name), "preset_group": library.preset_group(name), "undo": undo,
                                 "can_undo": bool(undo and undo["after"] == library.setup(name))})
        body = await read_json(request, {"name", "action", "configuration", "preset", "undo", "token", "acknowledge", "replace"})
        if isinstance(body, Response):
            return body
        name, action = body.get("name"), body.get("action")
        if not isinstance(name, str) or library.stored(name) is None:
            return error(404, "That local model does not exist.")
        if action not in ("preview", "apply", "save-preset", "delete-preset"):
            return error(400, "Choose preview, apply, save-preset or delete-preset.")
        label = body.get("preset", "")
        if not isinstance(label, str):
            return error(400, "A preset name must be text.")
        if action == "delete-preset":
            if not library.delete_preset(name, label):
                return error(404, "That preset does not exist.")
            log.add("model", "preset-deleted", user.name, address(request), name, preset=label)
            return JSONResponse({"deleted": label})
        selected = engine_for(library.get(name))
        described = {"flags": []}
        try:
            installed = selected.installation(config)
            described["flags"] = await selected.supported_flags(installed.executable, flag_list)
        except ValueError as problem:
            described["error"] = {"message": str(problem)}
        # Read editable state after the only await: a simultaneous edit or model deletion
        # while --help runs must not leave a stale source snapshot eligible to apply.
        if library.stored(name) is None:
            return error(404, "That local model no longer exists.")
        undo = body.get("undo", "")
        if not isinstance(undo, str):
            return error(400, "An undo receipt must be text.")
        sources = int("configuration" in body) + int(bool(undo)) + int(bool(label) and action != "save-preset")
        if sources != 1:
            return error(400, "Choose exactly one configuration, saved preset or undo receipt.")
        candidate = body.get("configuration")
        if undo:
            previous = library.setup_undo(name)
            if not previous or previous["receipt"] != undo or previous["after"] != library.setup(name):
                return error(409, "That undo is no longer current. Preview it again.")
            candidate = previous["before"]
        elif label and action != "save-preset":
            preset = next((p for p in library.presets(name) if p["name"] == label), None)
            if preset is None:
                return error(404, "That preset does not exist.")
            candidate = preset["configuration"]
        if not isinstance(candidate, dict):
            return error(400, "Supply a configuration, a saved preset or an undo receipt.")
        try:
            json.dumps(candidate, allow_nan=False)
        except ValueError:
            return error(400, "Configuration numbers must be finite.")
        report = model_setup.preview(name, candidate, library, runner, described.get("flags", []),
                                     described.get("error", {}).get("message", ""))
        if action == "preview":
            return JSONResponse(report)
        token = body.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token) or not secrets.compare_digest(token, report["token"]):
            return error(409, "The configuration, files, defaults or engine changed. Preview it again.")
        if report["errors"]:
            return error(400, " ".join(report["errors"]))
        if report["warnings"] and body.get("acknowledge") is not True:
            return error(400, "Acknowledge the preview's warnings before continuing.")
        if action == "save-preset":
            if any(p["name"] == label.strip() for p in library.presets(name)) and body.get("replace") is not True:
                return error(409, "That preset exists. Explicitly choose Replace existing preset, or use another name.")
            try:
                library.save_preset(name, label, candidate)
            except ValueError as problem:
                return error(400, str(problem))
            log.add("model", "preset-saved", user.name, address(request), name, preset=label.strip())
            return JSONResponse({"saved": label.strip()})
        if candidate == report["before"]:
            return error(400, "There are no configuration changes to apply.")
        try:
            library.change_setup(name, candidate, report["before"], undo, identity=report["model_id"], revision=report["file_revision"])
        except ValueError as problem:
            return error(409, str(problem))
        hashing.set()
        runner.wake.set()
        log.add("model", "setup-undone" if undo else "setup-applied", user.name, address(request), name,
                preset=label, before=report["before"], after=candidate)
        return JSONResponse({"applied": name, "undone": bool(undo)})

    async def loadout_control(request: Request, user: User) -> Response:
        """Named sets use existing setup reviews and ordinary scheduler admission."""
        if request.method == 'GET':
            if denied := lacks(user, 'manage-models'):
                return denied
            runner.held()
            now = time.monotonic()
            return JSONResponse({'loadouts': loadouts.listed(), 'hold_seconds': 600, 'max_members': 16,
                'leases': [{'id': identity, 'name': lease['name'], 'state': lease['state'],
                            'version': lease['version'],
                            'seconds_left': max(0, round(lease['until'] - now, 1)),
                            'models': [library.name_for(mid) for mid in lease['models']]}
                           for identity, lease in runner.loadout_leases.items() if lease['owner_id'] == (user.identity or user.name)]})
        if len(await request.body()) > 65536:
            return error(413, 'Load-out requests may be at most 64 KiB.')
        try:
            body = await read_json(request, {'action', 'name', 'models', 'version', 'token', 'acknowledge', 'undo', 'lease'})
        except RecursionError:
            return error(400, 'Load-out JSON is too deeply nested.')
        if isinstance(body, Response):
            return body
        action, name, lease = body.get('action'), body.get('name', ''), body.get('lease', '')
        if action not in ('save', 'delete', 'preview', 'apply', 'request', 'renew', 'release'):
            return error(400, 'Choose save, delete, preview, apply, request, renew or release.')
        if not isinstance(name, str) or len(name) > 80 or not isinstance(lease, str) or len(lease) > 128:
            return error(400, 'The load-out name and lease ID must be bounded text.')
        if action != 'release' and (denied := lacks(user, 'manage-models')):
            return denied
        owner_id = user.identity or user.name
        try:
            if action == 'release':
                released = loadouts.release(lease, owner_id)
                log.add('loadout', 'released', user.name, address(request), loadout=released['name'])
                return JSONResponse({'released': lease})
            if action == 'renew':
                if denied := lacks(user, 'load'):
                    return denied
                current = runner.loadout_leases.get(lease)
                if current and current['owner_id'] != owner_id:
                    return error(404, 'That ready load-out lease is not yours or has expired.')
                if current and any(not (model := library.name_for(mid)) or not user.group.may_use(model) for mid in current['models']):
                    return error(403, 'You no longer have access to every leased model.')
                renewed = loadouts.renew(lease, owner_id)
                log.add('loadout', 'renewed', user.name, address(request), loadout=renewed['name'], seconds=600)
                return JSONResponse({'lease': lease, 'seconds': 600, 'state': 'ready', 'version': renewed['version']})
            if action == 'save':
                saved = loadouts.save(name, body.get('models'), body.get('version'))
                log.add('loadout', 'saved', user.name, address(request), loadout=saved['name'], version=saved['version'])
                return JSONResponse(saved)
            if action == 'delete':
                loadouts.delete(name, body.get('version'))
                log.add('loadout', 'deleted', user.name, address(request), loadout=name)
                return JSONResponse({'deleted': name})
            if type(body.get('undo', False)) is not bool:
                return error(400, 'undo is true or false.')
            native = await flags(request, user)
            described = json.loads(native.body)
            await runner.look()
            report = loadouts.preview(name, described.get('flags', []), described.get('error', {}).get('message', ''),
                body.get('undo', False), request.headers.get('x-loadcoach-lock', ''))
            if action == 'preview':
                return JSONResponse(report)
            if body.get('token') != report['token']:
                return error(409, 'The load-out or its models changed. Preview the complete selection again.')
            if any(one['warnings'] for one in report['models']) and body.get('acknowledge') is not True:
                return error(400, 'Review and acknowledge the configuration warnings before applying this selection.')
            if action == 'apply':
                loadouts.apply(report)
                hashing.set()
                runner.wake.set()
                log.add('loadout', 'undone' if report['undo'] else 'applied', user.name, address(request), loadout=name,
                        models=[one['model_id'] for one in report['models']])
                return JSONResponse({'applied': name, 'undone': report['undo'], 'undo_available': bool(loadouts.get(name)['undo'])})
            if denied := lacks(user, 'load'):
                return denied
            if draining:
                return not_now()
            if any(not user.group.may_use(one['name']) for one in report['models']):
                return error(403, 'You may not load every model in this selection.')
            if not report['can_request']:
                return error(409, 'The complete selection cannot be loaded: ' + ' '.join(report['errors'] + report['joint']['errors']))
            async def acquire(model_name, settings):
                """Acquire a normal admission ticket for one model in a load-out."""
                ticket = await take_turn(request, user, model_name, settings=settings)
                if isinstance(ticket, Response):
                    raise Refusal(ticket.status_code, reason(ticket.body).get('why', 'Model admission failed.'))
                return ticket
            identity = await loadouts.request(report, user.name, owner_id, acquire)
            hashing.set()
            runner.wake.set()
            log.add('loadout', 'ready', user.name, address(request), loadout=name, seconds=600,
                    models=[one['model_id'] for one in report['models']])
            return JSONResponse({'lease': identity, 'seconds': 600, 'state': 'ready', 'loadout': name, 'version': report['version']})
        except LookupError as problem:
            return error(404, str(problem))
        except LoadoutConflict as problem:
            return error(409, str(problem))
        except ValueError as problem:
            return error(400, str(problem))
        except Refusal as problem:
            log.add('loadout', 'not-ready', user.name, address(request), loadout=name, why=str(problem))
            return error(problem.status, str(problem) + ' Any applied changes retain guarded Undo; this attempt left no set holds.',
                         {'retry-after': str(problem.retry_after or 10)})
        except asyncio.CancelledError:
            log.add('loadout', 'cancelled', user.name, address(request), loadout=name)
            raise

    # ---- measuring every model that has no measure ------------------------------------------
    #
    # A model is measured when it loads (see runner.py). So to measure them all is to load each
    # one in turn, as a request for it would. A run takes its place in line with low priority,
    # one model at a time, and gives way as soon as anyone else asks for anything.

    def unmeasured() -> list[str]:
        """The models a run would load: those on a card, whose file is there, with no measure for the settings they have now."""
        on_a_card = lambda settings: settings.get("gpus", len(config.gpus)) != 0  # noqa: E731
        return [name for name in library.names() if engine_for(library.get(name)).id not in ('sdcpp', 'audiocpp', 'whispercpp') and not library.get(name).get('flags', {}).get('reranking') and on_a_card(library.get(name)) and Path(library.get(name)["flags"]["model"]).is_file() and not library.calibrated(name, runner.measured_settings(name))]

    async def measure_all(by: str, caller: str) -> None:
        """Load each model in `measuring["left"]`, one after another. Each is measured as it loads. Says in `measuring["ended"]` why it ended."""
        try:
            while measuring["left"]:
                # Between two models nothing of this run is in line. So anything there is somebody else's, and they go first.
                if runner.running or runner.waiting or runner.holder() or draining:
                    measuring["ended"] = "It gave way: a request arrived, the lock was taken, or requests are not being taken."
                    return
                name = measuring["now"] = measuring["left"][0]
                ticket = Ticket(name, by, caller=caller, priority="low", calibrate=True)
                try:
                    runner.release(await runner.acquire(ticket))  # its turn comes once the model is loaded, and that is all that is wanted
                    if library.calibrated(name, runner.measured_settings(name)):
                        measuring["done"].append(name)
                    else:
                        measuring["failed"][name] = "It loaded, but what it uses of its cards could not be seen."
                except Refusal as refusal:  # it would not load. The others are still tried
                    measuring["failed"][name] = str(refusal)
                measuring["left"].remove(name)
            measuring["ended"] = "Every model was tried."
        finally:
            measuring["now"] = None
            if measuring["ended"]:  # it ended: by itself, by giving way, or by someone's hand. Stopped with LoadCoach, it is still written down, and is gone on with at the next start
                library.run_ended()
            log.add("model", "measured-all", by, caller, done=measuring["done"], failed=sorted(measuring["failed"]), left=measuring["left"], why=measuring["ended"] or "LoadCoach stopped.")

    async def model_measure_all(request: Request, user: User) -> Response:
        """Start measuring every model that has no measure. With `on` false, stop the run that is under way.
        With `names`, only those of the listed models that still need measuring are measured.

        Each such model is loaded in turn, which pushes idle models off its cards for that one
        load. A run ends by itself when a request arrives that is not its own. Started again,
        it goes on with the models that are left.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"on", "names"})
        if isinstance(asked, Response):
            return asked
        wanted = asked.get("names")
        if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(one, str) for one in wanted)):
            return error(400, "names must be a list of model names.")
        under_way = "task" in measuring and not measuring["task"].done()
        if asked.get("on", True) is False:
            if under_way:
                measuring["ended"] = f"Stopped by {user.name}."
                measuring["task"].cancel()
            return JSONResponse({"stopped": under_way})
        if under_way:
            return error(409, "The models are being measured already.")
        await runner.look(fresh=True)
        if runner.seen is None:
            return error(409, f"{config.nvidia_smi} could not be asked what the cards hold, so nothing can be measured.")
        left = [name for name in unmeasured() if wanted is None or name in wanted]
        if wanted is not None and not left:
            return error(409, "Every model asked for already has its memory measured at its minimum and maximum context.")
        measuring.clear()
        measuring.update(by=user.name, since=datetime.now().isoformat(timespec="seconds"), left=left, done=[], failed={}, now=None, ended=None)
        log.add("model", "measuring-all", user.name, address(request), models=measuring["left"])
        library.run_begun(user.name, address(request), measuring["since"])
        task = measuring["task"] = asyncio.create_task(measure_all(user.name, address(request)))
        carrying.add(task)
        task.add_done_callback(carrying.discard)
        return JSONResponse({"measuring": measuring["left"]}, status_code=202)

    async def fingerprint_list(request: Request, user: User) -> Response:
        """Every fingerprint there has been, the latest first, with what the model was when it was first seen. `?start=` gives only those that begin so.

        Nothing is ever taken out of this list. It is how a fingerprint in the log, or in a
        benchmark's results, is read back after the model was removed and its file deleted.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        found = library.fingerprints(request.query_params.get("start", ""))
        for one in found:
            one["first"] = datetime.fromtimestamp(one["first"]).isoformat(timespec="seconds")
        return JSONResponse({"fingerprints": found})

    async def retired_models(request: Request, user: User) -> Response:
        """Only catalogue managers may inspect deleted identities; absence is not deletion."""
        if denied := lacks(user, "manage-models"):
            return denied
        retired = []
        for identity, in library.db.execute('SELECT id FROM model_identities WHERE removed IS NOT NULL'):
            names = [{'name': name, 'since': since, 'until': until} for name, since, until in library.db.execute(
                'SELECT name,since,until FROM model_names WHERE model_id=? ORDER BY since', (identity,))]
            retired.append({'model_id': identity, 'name_history': names})
        return JSONResponse({'models': retired})

    async def model_forget_measure(request: Request, user: User) -> Response:
        """Throw away what a model measured. It has its cards to itself at its next load, and is measured afresh there."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"name"})
        if isinstance(asked, Response):
            return asked
        name = str(asked.get("name", ""))
        if library.stored(name) is None:
            return error(404, f"There is no model named {name!r}.")
        forgotten = library.forget_measure(name)
        if forgotten:
            log.add("model", "measure-forgotten", user.name, address(request), name)
        return JSONResponse({"name": name, "forgotten": forgotten})

    async def model_rename(request: Request, user: User) -> Response:
        """Give a model another name. Its settings, its file and its place in the list stay.

        The name is what callers ask for, so the old one stops working at once. A group or an
        account that lists the model by name follows. A loaded copy is treated as a removed
        model's is: what it is answering and what is in line for it are finished, then it is
        unloaded, and the model is loaded again when its new name is first asked for.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"name", "to"})
        if isinstance(asked, Response):
            return asked
        name, new = str(asked.get("name", "")), str(asked.get("to", ""))
        if library.stored(name) is None:
            return error(404, f"There is no model named {name!r}.")
        if library.stored(new) is not None:
            return error(409, f"There is already a model named {new!r}.")
        try:
            library.rename(name, new)
        except ValueError as problem:
            return error(400, str(problem))
        followed = []  # the groups, accounts and roles that list it are front's: turnstile has them follow
        log.add("model", "renamed", user.name, address(request), new, was=name, followed=followed)
        runner.wake.set()
        using = sum(ticket.model == name for ticket in runner.running + runner.waiting)
        return JSONResponse({"name": new, "model_id": library.identity(new), "was": name, "followed": followed, "still_finishing": using})

    async def model_remove(request: Request, user: User) -> Response:
        """Take a model off the list. It is no longer offered from this moment.

        Requests for it that are being answered or already in line are completed first. Then it
        is unloaded. Its file is left where it is.
        """
        if denied := lacks(user, "manage-models"):
            return denied
        name = request.path_params["name"]
        identity = library.identity(name)
        if not library.remove(name):
            return error(404, f"There is no model named {name!r}.")
        log.add("model", "removed", user.name, address(request), name, model_id=identity)
        runner.wake.set()
        using = sum(ticket.model == name for ticket in runner.running + runner.waiting)
        return JSONResponse({"removed": name, "still_finishing": using})

    # ---- accounts ---------------------------------------------------------------------------
    #
    # The accounts are kept on the front machine, and turnstile answers for them. Compute is asked
    # only for what it alone knows about one: how much it has asked for, and whether it has work
    # under way here before it is renamed.

    async def user_rename(request: Request, user: User) -> Response:
        """Turnstile is renaming an account. Refuse if it has work under way here; otherwise rename what is kept here under its name."""
        if denied := lacks(user, "manage-users"):
            return denied
        name, asked = request.path_params["name"], await read_json(request, {"name"})
        if isinstance(asked, Response):
            return asked
        new = asked.get("name")
        if not isinstance(new, str):
            return error(400, "Give the new user name as name.")
        runner.held()
        if (
            any(one.user == name for one in under_way.values())
            or any(ticket.user == name for ticket in runner.running + runner.waiting)
            or any(owner == name for _, owner in runner.holds)
            or any(lease['user'] == name for lease in runner.loadout_leases.values())
            or ((held := runner.holder()) and held.user == name)
            or any(batch["status"] in {"in_progress", "cancelling"} for batch in batches.listing(name))
        ):
            return error(409, "This account has active work, a model hold or a lock. Finish or stop it before renaming.")
        if batches.db is not None:
            with batches.db:
                for table in ("files", "batches"):
                    batches.db.execute(f"UPDATE {table} SET user = ? WHERE user = ?", (new, name))
        return JSONResponse({"renamed": name, "name": new})

    async def machine_key(request: Request, user: User) -> Response:
        """Turnstile is replacing the machine key: take the new one, sealed with the key this very request was signed with.

        Only through turnstile, by someone who may change the settings. The old key still counts
        for a while, so that what is on its way is not refused (statement.OVERLAP). The answer is
        proven with the old key, which is the one turnstile still holds until it hears back.
        """
        if denied := lacks(user, "manage-settings"):
            return denied
        said, key = getattr(request.state, "said", None), getattr(request.state, "key", None)
        if not said or not config.machine_key_file:
            return error(403, "The machine key is replaced through turnstile, or on this machine with: loadcoach --replace-key")
        asked = await read_json(request, {"sealed", "salt"})
        if isinstance(asked, Response):
            return asked
        try:
            replace_key(config.machine_key_file, unseal(key, str(asked.get("salt", "")), str(asked.get("sealed", ""))))
        except (ValueError, UnicodeDecodeError):
            return error(400, "The new key could not be opened, or is not a key.")
        log.add("machine-key", "replaced", user.name, address(request))
        return JSONResponse({"replaced": True})

    async def plugin_mirror(request: Request, user: User) -> Response:
        """For turnstile's plugin path: the models there are, for when compute is not answering. Who may use them is front's to say."""
        if denied := lacks(user, "mirror-users"):
            return denied
        inventory = [described(name) for name in library.names()]
        return JSONResponse({"generated_at": time.time(), "keys": [], "models": inventory,
                             "unlogged": config.unlogged}, headers={"cache-control": "no-store"})

    async def test_page(request: Request) -> Response:
        # No key needed to load it. The page holds no secrets, and every request it sends
        # goes through `forward` like any other caller's.
        """Serve the static API exerciser; its requests still require authentication."""
        return diagnostic_page(TEST_PAGE)

    async def forget_old_entries() -> None:
        """Once a day, drop log entries older than the configuration keeps them."""
        while True:
            await gate.wait()
            log.prune()
            from loadcoach.observation_store import prune
            prune(log.db, forwarded=not log.chained)  # compute keeps only what front has not acknowledged
            batches.prune(config.batch_keep_days)
            await asyncio.sleep(86400)

    def publish_task_result(task, snapshot):
        """The evidence a finished benchmark task may publish, checked against the task. Turnstile keeps it, on the front machine. None if its submitter may no longer."""
        user = users.by_name(task['user'])
        if not user or not user.can('benchmark') or not task['model'] or not user.group.may_use(task['model']):
            return None  # revoked access may close its batch, but cannot influence future routing
        snapshot = RoleEvidence.validate(snapshot)
        kind = batches.get(task['id']).get('metadata', {}).get('benchmark')
        if len(snapshot['results']) != 1 or any(one['model_id'] != task['model_id'] or one['fingerprint'] != task['fingerprint'] or one['run'] != task['id'] or one['benchmark'] != kind or
                any(one.get(key) != task['environment'].get(key) for key in ('engine_hash', 'machine_hash')) for one in snapshot['results']):
            raise ValueError('Task evidence must match this task, model, benchmark and environment.')
        used = {one['benchmark'] for one in snapshot['results']}
        snapshot['definitions'] = [one for one in snapshot['definitions'] if one['id'] in used]
        # Signed with the machine key, so that turnstile knows the evidence in this answer is compute's: a task's request carries no statement.
        return {'snapshot': snapshot, 'signed': sign(read_key(config.machine_key_file), json.dumps(snapshot, sort_keys=True))} if config.machine_key_file else None

    tasks = Tasks(batches, runner, users, who, lambda: bool(draining), log, on_result=publish_task_result)
    if config.former_machine_name and config.former_machine_name != config.machine_name:
        from turnstile.nodes import remember_aliases, rename_stored, verified_backup
        log.former = remember_aliases(log.db, {config.former_machine_name: config.machine_name})
        rename_stored('loadcoach.core', log.db, log.former,
                      verified_backup(config.data, machine=config.machine_name))

    # ---- batches are front's ---------------------------------------------------------------------
    #
    # Files and batches (OpenAI's Batch API) are kept and worked through on the front machine since
    # milestone 115: see turnstile/batches.py. Each request of one reaches compute as an ordinary
    # request. What is kept here is only what the external tasks (benchmarks) use: loadcoach/batches.py.

    def as_openai(one: dict) -> dict:
        """A file or a batch as OpenAI describes it: without whose it is, which is ours to know."""
        return {key: value for key, value in one.items() if key != "user"}

    def theirs(user: User, one: dict | None) -> dict | Response:
        """A file or a batch, if it is this user's. Otherwise the 404 that says it is not there: nobody learns what others have."""
        return one if one and one["user"] == user.name else error(404, "There is no such file or batch of yours.")

    async def batch_list(request: Request, user: User) -> Response:
        """GET /v1/batches lists one's own. GET /loadcoach/batches is for the site: with `see-all` it lists everyone's, each with whose it is."""
        if request.url.path.startswith("/v1/"):
            return JSONResponse({"object": "list", "data": [as_openai(one) for one in batches.listing(user.name)], "has_more": False})
        return JSONResponse({"batches": batches.listing(None if user.can("see-all") else user.name)})

    async def batch_one(request: Request, user: User) -> Response:
        """GET /v1/batches/ID says how far a batch has got. POST /v1/batches/ID/cancel ends it: what was answered is kept, the rest is not asked."""
        found = batches.get(request.path_params["number"])
        if request.method == "POST" and found and user.can("stop"):  # whoever may stop someone else's request may cancel their batch
            found = {**found, "user": user.name}
        found = theirs(user, found)
        if isinstance(found, Response):
            return found
        if request.method == "POST" and found["status"] == "in_progress":
            batches.cancelling(found["id"])
            batches.finish(found["id"])  # an external task's: the batches of programs are front's
            log.add("batch", "cancelling", user.name, address(request), batch=found["id"])
        return JSONResponse(as_openai(batches.get(found["id"])))

    async def read_files() -> None:
        """Work out the SHA-256 of each model file that has none, one file at a time, for as long as LoadCoach runs.

        A fingerprint needs it. A file of 20 GB takes about ten seconds from a fast drive, so it
        is done here, apart from any request. Until it is done the model has no fingerprint,
        and is asked for and answered all the same.
        """
        while True:
            await gate.wait()
            await hashing.wait()
            hashing.clear()
            for path in library.unhashed():
                try:
                    seen = os.stat(path)  # as it is before it is read. If it changes meanwhile, what is kept will not match, and it is read again
                    library.keep_hash(path, await asyncio.to_thread(sha256_of, path, stopping), seen.st_size, seen.st_mtime)
                except (OSError, InterruptedError):
                    continue  # gone, unreadable, or LoadCoach is stopping

    @asynccontextmanager
    async def lifespan(_: Starlette):
        """Start background workers and release service resources on shutdown."""
        dispatcher = asyncio.create_task(runner.dispatch())
        housekeeping = asyncio.create_task(forget_old_entries())
        if not log.chained:
            carrying.add(asyncio.create_task(users.send_log(log, config.keep_text, dict)))  # the log entries, to front
        reading = asyncio.create_task(read_files())
        hashing.set()
        await runner.version()  # asked once now, so that it is at hand for every answer
        try:
            await tasks.probes.identify()
        except (OSError, ValueError):
            pass
        async def recover_work():
            """Resume durable tasks only after database maintenance has ended."""
            await gate.wait()
            tasks.recover()
            if run := library.run_under_way():
                measuring.update(by=run["by"], since=run["since"], left=unmeasured(), done=[], failed={}, now=None, ended=None)
                log.add("model", "measuring-all", run["by"], run["caller"], models=measuring["left"], again=True)
                task = measuring["task"] = asyncio.create_task(measure_all(run["by"], run["caller"]))
                carrying.add(task)
                task.add_done_callback(carrying.discard)
        recovery_task = asyncio.create_task(recover_work())
        carrying.add(recovery_task)
        recovery_task.add_done_callback(carrying.discard)
        if config.token_file:  # readable by the owner only, and gone again when LoadCoach stops
            with open(os.open(config.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
                file.write(cli_token)
        # The models in the defaults file are only what an install starts from. That they
        # differ later from what is kept is as it should be, and nothing is said about it.
        if library.taken_over:
            log.add("model", "taken-over", count=library.taken_over)
        for folder in config.places.values():  # a place that was named is made, if it is not there yet
            os.makedirs(folder, exist_ok=True)
        report()
        log.add("loadcoach", "started", models=len(library.names()), cards=list(config.gpus))
        yield
        await classifier_jobs.close()
        await database_operations.jobs.close()
        if config.token_file:
            os.unlink(config.token_file)
        # LoadCoach is shutting down. Stop deciding and loading, then take the model servers with it.
        stopping.set()
        for task in [dispatcher, housekeeping, reading, *runner.loads, *carrying]:
            task.cancel()
        await asyncio.wait({dispatcher, housekeeping, reading, *runner.loads, *carrying})
        for task in list(tasks.probes.working.values()):
            task.cancel()
        await asyncio.gather(*tasks.probes.working.values(), return_exceptions=True)
        await runner.stop_all()
        await client.aclose()
        await web.aclose()
        log.add("loadcoach", "stopped")
        component_versions.db.close()
        await observation.shutdown(app)

    # ---- settings ---------------------------------------------------------------------------
    #
    # What can be changed from the site, for every program, is kept here and handed out. See
    # settings.py. LoadCoach's own settings it also takes on itself, at once where that is safe.

    def report() -> None:
        """Keep what LoadCoach itself runs with, so that the page can show it: by the names its file uses."""
        simple = ("host", "port", "engines", "llama_server", "load_timeout", "max_wait", "card_margin", "model_idle_minutes", "host_ram_headroom_mib", "host_ram_limit_mib", "cpu_idle_minutes", "disable_ram_offload", "models_folder", "models_storage_folder", "keep_days", "keep_text", "unlogged", "database", "places", "model_defaults", "disabled_gpus", "engine_gpus")
        running = {**config.file, **{name: getattr(config, name) for name in simple}, "gpus": list(config.gpus)}
        reported = said(running, config.file, config.says)
        reported["disable_ram_offload"].update(
            label="Disable RAM offload",
            choices=[["true", "Yes - require VRAM"], ["false", "No - allow host RAM spill"]],
        )
        reported["models_folder"].update(
            label="Active model cache",
            says="Models this node actively uses, split into llm, adapters, projectors and image below this root.",
        )
        reported["models_storage_folder"].update(
            label="Model storage library",
            says="The complete used and unused model library. Empty leaves storage disabled; new installations start empty.",
        )
        changes.seen("loadcoach", reported)

    def of_the_file(name: str):
        """What a setting of LoadCoach's is when nothing was changed: the file's value, or what LoadCoach uses when the file says nothing."""
        field = next(one for one in fields(Config) if one.name == name)
        unsaid = field.default_factory() if field.default_factory is not MISSING else field.default  # type: ignore[misc]
        if name == "llama_server":
            return config.file.get("engines", {}).get("llama_cpp", {}).get("executable", config.file.get(name, unsaid))
        return config.file.get(name, unsaid)

    def local_settings():
        """List only settings whose authority remains on this compute node."""
        return [one for one in changes.everything() if one['program'] not in front_programs(config, retired)]

    async def settings_all(request: Request, user: User) -> Response:
        """Every program that has said what it runs with, and each of its settings: its value, the file's, what was changed, and when a change holds."""
        return lacks(user, "manage-settings") or JSONResponse({"programs": local_settings()})

    async def tools_info(request: Request, user: User) -> Response:
        """ToolLocker's published configuration and last observed tool descriptions."""
        if denied := lacks(user, "manage-settings"):
            return denied
        if config.toollocker_runs_on_front:
            return error(410, 'The front keeps ToolLocker\'s records; use the Tools page on the front.')
        program = next((one for one in changes.everything() if one['program'] == 'toollocker'), None)
        exists = "tool_schemas" in log.db.database.tables(log.db)
        schemas = [{"name": name, "schema": json.loads(schema)} for name, schema in
                   log.db.execute("SELECT tool, schema FROM tool_schemas ORDER BY tool")] if exists else []
        return JSONResponse({"program": program, "tools": schemas})

    async def cards_list(request: Request, user: User) -> Response:
        """Every discovered card, its description, exclusion and loaded models."""
        if denied := lacks(user, "manage-settings"):
            return denied
        from loadcoach.devices import compatible, disabled, identity, inventory
        from loadcoach.gpu_type import is_integrated
        details = {card['id']: card for card in config.gpu_details}
        cards = inventory(config)
        excluded = {name for device in config.gpu_details if is_integrated(device)
                    for name in (device['id'], device.get('uuid'),
                                 f"{device['vendor']}@{device['pci']}" if device.get('pci') else None) if name}
        engines = [{'id': engine['id'], 'label': engine['label']} for engine in engine_catalogue(config)]
        return JSONResponse({"cards": [{**details.get(card, {}), "id": card, "description": details.get(card, {}).get('description', ''),
                                        'key': identity(config, card), 'disabled': disabled(config, card),
                                        'compatible_engines': [engine['id'] for engine in engines if compatible(config, engine['id'], card)],
                                        "models": [server.model for server in runner.servers.values() if card in server.cards]}
                                       for card in cards],
                             'engines': engines, 'engine_gpus': {engine: [card for card in assigned if card not in excluded]
                                                               for engine, assigned in config.engine_gpus.items()},
                             "missing_disabled": [card for card in config.disabled_gpus if card not in excluded and card not in
                                 set(cards) | {identity(config, one) for one in cards}]})

    async def settings_change(request: Request, user: User) -> Response:
        """Change settings of one program. The body gives each by name, with its new value, or null to take a change away."""
        if denied := lacks(user, "manage-settings"):
            return denied
        program = program_name(request.path_params.get("program", "loadcoach"))
        if program in front_programs(config, retired):
            return error(410, 'The front keeps this program\'s settings; use the Settings page on the front.')
        if request.url.path == "/loadcoach/cards":
            report()
        known = next((one for one in changes.everything() if one["program"] == program), None)
        if known is None:
            return error(404, f"{program} has not said what it runs with. It is not a program of this suite, or has not run since this was built.")
        allowed = {"disabled_gpus", 'engine_gpus'} if request.url.path == "/loadcoach/cards" else set(CHANGEABLE[program]) | {setting["name"] for setting in known["settings"]}
        asked = await read_json(request, allowed)
        if isinstance(asked, Response):
            return asked
        now = {setting["name"]: setting["value"] for setting in known["settings"]}
        if asked.get('component_versions_every') is not None:
            from loadcoach.component_versions import interval_seconds
            try:
                interval_seconds(asked['component_versions_every'])
            except ValueError as problem:
                return error(400, str(problem))
        if program == 'voice' and asked.get('greetings') is not None:
            from voice.prompt_recordings import texts
            try:
                texts({'greetings': asked['greetings']})
            except ValueError as problem:
                return error(400, str(problem))
        if program == 'weightroom' and asked.get('alert_gpu_temperature') is not None:
            heat = asked['alert_gpu_temperature']
            if type(heat) not in (int, float) or not 50 <= heat <= 110:
                return error(400, 'alert_gpu_temperature must be from 50 through 110 degrees Celsius.')
        for name, value in asked.items():
            if name not in CHANGEABLE[program]:
                return error(403, f"{name} is not changed from here. It is changed in {program}'s file, on its machine.")
            if value is not None and name in now and (why := wrong_kind(value, now[name])):
                return error(400, f"{name}: {why}")
        if program == "loadcoach":  # its own: checked whole before anything is kept, and taken on at once where that is safe
            # Validate the entire next-start configuration, including pending engine aliases.
            # Looking only at this POST wrongly conflicts with another engine's saved path.
            wanted = {name: of_the_file(name) if value is None else value for name, value in {**changes.of(program), **asked}.items()}
            try:
                candidate = checked(replace(config, **{name: tuple(value) if name == "gpus" else value for name, value in wanted.items()}), "settings")
            except (SystemExit, TypeError) as problem:
                return error(400, str(problem).removeprefix("settings: "))
            from loadcoach.devices import allowed as allowed_cards
            revoked = {engine: set(allowed_cards(config, engine)) - set(allowed_cards(candidate, engine))
                       for engine in engine_ids()}
            if any(revoked.values()) and runner.holder():
                return error(409, "The cards are locked, possibly for an isolated probe. Wait for the lock to finish, then save again.")
            affected = [server for server in runner.servers.values() if revoked[engine_for(server.settings).id] & set(server.cards)]
            if any(not server.ready or runner.busy(server) or runner.pinned(server) for server in affected):
                return error(409, "A card whose assignment you changed is loading, answering or held. Wait for it to finish, then save again.")
            for name in asked:
                if CHANGEABLE[program][name] == NOW:
                    setattr(config, name, getattr(candidate, name))
            for server in affected:
                del runner.servers[server.model]
                await runner.stop(server)
                log.add("model", "unloaded", user.name, address(request), model=server.model, why="its card was disabled")
            runner.wake.set()
            log.keep_days, log.keep_text, library.defaults = config.keep_days, config.keep_text, config.model_defaults
        for name, value in asked.items():
            changes.take_away(program, name) if value is None else changes.put(program, name, value, user.name)
        if program == "loadcoach":
            report()
        log.add("settings", "changed", user.name, address(request), program=program, **asked)
        return JSONResponse({"programs": local_settings()})

    async def settings_changes(request: Request, user: User) -> Response:
        """For a program's own key: what was changed for that program, to lay over its file."""
        if not user.can("manage-settings") and (denied := lacks(user, "follow-settings")):
            return denied
        if program_name(request.path_params['program']) in front_programs(config, retired):
            return error(410, 'This program follows settings on front.')
        return JSONResponse(changes.of(request.path_params["program"]))

    async def settings_seen(request: Request, user: User) -> Response:
        """For a program's own key: what it runs with now, and what it would not take of what was changed."""
        if denied := lacks(user, "follow-settings"):
            return denied
        if program_name(request.path_params['program']) in front_programs(config, retired):
            return error(410, 'This program reports settings on front.')
        asked = await read_json(request, {"told", "refused"})
        if isinstance(asked, Response):
            return asked
        if program_name(request.path_params["program"]) not in CHANGEABLE or not isinstance(asked.get("told"), dict) or not isinstance(asked.get("refused", {}), dict):
            return error(400, "told and refused are objects, and the program is one of this suite's.")
        changes.seen(request.path_params["program"], asked["told"], asked.get("refused"))
        return JSONResponse({})

    def model_recovery_inventory() -> list[dict]:
        """Model weights stay outside archives; record enough identity and origin detail to recover them."""
        found: list[dict] = []
        for name in library.names():
            settings = library.get(name) or {}
            path = str(settings.get("flags", {}).get("model", ""))
            file = Path(path) if path else None
            parent = file.parent.name if file else ""
            source = parent.replace("--", "/", 1) if "--" in parent else ""
            found.append(
                {
                    "name": name,
                    "model_id": library.identity(name),
                    "path": path,
                    "size": file.stat().st_size if file and file.is_file() else None,
                    "sha256": library.file_hash(path) if path else None,
                    "recovery_source": source,
                    "present": bool(file and file.is_file()),
                }
            )
        return found

    def compute_data_folder() -> Path:
        """Resolve the compute installation's configured recovery data directory."""
        if not config.path and local_path(core) is not None:
            return local_path(core).parent.resolve()
        folder = Path(config.data)
        return folder if folder.is_absolute() else (Path(config.path).parent / folder).resolve() if config.path else folder.resolve()

    def compute_recovery_stores() -> list[RecoveryStore]:
        """Enumerate compute-owned configured stores for a paired recovery set."""
        folder = compute_data_folder()
        candidates = [("loadcoach", bound) for bound in databases.bound_stores()]
        if config.path:
            settings_folder = Path(config.path).parent
            spotter_path = settings_folder / "spotter.toml"
            if spotter_path.is_file():
                told = read_settings(spotter_path)
                history = str(data_folder(spotter_path, told) / "spotter.db")
                registry = DatabaseRegistry.from_config(
                    told.get("databases"), spotter_path.parent,
                    {"spotter.identity": core, "spotter.history": history},
                )
                candidates.extend(("spotter", bound) for bound in registry.bound_stores())
            locker_path = settings_folder / "toollocker.toml"
            if locker_path.is_file() and not config.toollocker_runs_on_front:
                told = read_settings(locker_path)
                registry = DatabaseRegistry.from_config(
                    told.get("databases"), locker_path.parent, {"toollocker.identity": core}
                )
                candidates.extend(("toollocker", bound) for bound in registry.bound_stores())

        combined: dict[tuple[str, str], RecoveryStore] = {}
        for prefix, bound in candidates:
            if not bound.bindings:
                continue
            key = (bound.database.engine, str(bound.database.path or Path(bound.database.store.dsn_file).resolve()))
            archive = ""
            if bound.database.path is not None and bound.database.path.parent.resolve() == folder.resolve():
                archive = f"data/{bound.database.path.name}"
            if key in combined:
                before = combined[key]
                combined[key] = RecoveryStore(before.name, before.database,
                                               tuple(sorted(set(before.bindings + bound.bindings))), before.archive or archive)
            else:
                combined[key] = RecoveryStore(f"{prefix}.{bound.name}", bound.database, bound.bindings, archive)
        return list(combined.values())

    def compute_backup_context() -> tuple[Path, list, list[dict], list[dict], list[RecoveryStore]]:
        """Assemble compute backup inputs, exclusions and service ownership."""
        folder = compute_data_folder()
        stores = compute_recovery_stores()
        paths = [store.database.path for store in stores if store.database.path is not None]
        files, excluded = compute_plan(core, config.path, folder, paths)
        rows = [*store_inventory(stores), *backup_inventory(files, excluded)]
        return backup_root(folder), files, excluded, rows, stores

    async def backups_list(request: Request, user: User) -> Response:
        """Compute's recoverable inventory and its local halves of recovery sets."""
        if denied := lacks(user, "manage-settings"):
            return denied
        root, _, excluded, rows, _ = compute_backup_context()
        return JSONResponse(
            {
                "machine": config.machine_name,
                "inventory": rows,
                "excluded": excluded,
                "backups": backup_manifests(root),
                "archive_root": str(root),
                "protection": await asyncio.to_thread(encryption_status, root),
            }
        )

    async def backup_create(request: Request, user: User) -> Response:
        """Make and independently restore-check compute's half of a recovery set."""
        if denied := lacks(user, "manage-settings"):
            return denied
        asked = await read_json(request, {"id", "retention"})
        if isinstance(asked, Response):
            return asked
        backup_id, retention = asked.get("id"), asked.get("retention")
        if not isinstance(backup_id, str) or type(retention) is not int or not 0 <= retention <= 100:
            return error(400, "id must be a recovery-set ID and retention a whole number from 0 through 100.")
        root, files, excluded, rows, stores = compute_backup_context()
        try:
            made = await asyncio.to_thread(
                create_backup,
                root,
                backup_id,
                config.machine_name,
                files,
                rows,
                excluded,
                ["loadcoach.service", "spotter.service", "voice.service",
                 *([] if config.toollocker_runs_on_front else ["toollocker.service"])],
                retention,
                model_recovery_inventory(),
                stores=stores,
            )
        except FileExistsError as problem:
            return error(409, str(problem))
        except (OSError, ValueError, TypeError, sqlite3.Error, subprocess.SubprocessError, RuntimeError) as problem:
            log.add("backup", "failed", user.name, address(request), backup=backup_id, machine=config.machine_name, why='Backup failed; inspect the owning node.')
            return error(500, f"Compute backup failed: {problem}")
        log.add("backup", "created", user.name, address(request), backup=backup_id, machine=config.machine_name,
                files=len(made["files"]), verified=made["verification"]["state"])
        return JSONResponse(made, status_code=201)

    async def backup_one(request: Request, user: User) -> Response:
        """Return a recovery manifest only to callers who may manage settings."""
        if denied := lacks(user, "manage-settings"):
            return denied
        root, _, _, _, _ = compute_backup_context()
        found = backup_manifest(root, request.path_params["backup_id"])
        return JSONResponse(found) if found else error(404, "There is no such compute backup.")

    def schedule_subject(spec, user):
        """Validate a schedule and resolve its currently authorized compute subject."""
        from .schedule_time import validate
        spec = validate(spec)
        if not schedule_grants.permitted(user, spec):
            raise PermissionError('Schedules require manage-settings and benchmarks also require benchmark.')
        name = ''
        if spec['kind'] == 'benchmark':
            name = library.name_for(spec['model_id'])
            if not name or not user.group.may_use(name):
                raise PermissionError('The selected local model is not permitted.')
            try:
                tasks.probes.subject(name)  # validate without loading or inference
            except (ValueError, OSError):
                raise ValueError('The model is not ready for GPU benchmarks. Check its file, fingerprint and placement settings.')
        return spec, name

    async def schedule_register(request: Request, user: User) -> Response:
        """Register a hashed schedule grant when compute retains schedule authority."""
        if retired:
            return error(410, 'The front keeps schedules for this node; use turnstile.')
        asked = await read_json(request, {'id', 'token', 'spec'})
        if isinstance(asked, Response):
            return asked
        try:
            spec, name = schedule_subject(asked.get('spec'), user)
            schedule_grants.put(asked.get('id'), asked.get('token'), spec, user)
            log.add('schedule', 'authorized', user.name, address(request), schedule=asked['id'], kind_of_work=spec['kind'])
            return JSONResponse({'id': asked['id'], 'model_name': name}, status_code=201)
        except PermissionError as problem:
            return error(403, str(problem))
        except (ValueError, TypeError, KeyError) as problem:
            return error(400, str(problem))

    async def schedule_edit(request: Request, user: User) -> Response:
        """Replace a compute-owned schedule grant after current permission checks."""
        if retired:
            return error(410, 'The front keeps schedules for this node; use turnstile.')
        asked = await read_json(request, {'token', 'spec', 'enabled'})
        if isinstance(asked, Response):
            return asked
        try:
            spec, name = schedule_subject(asked.get('spec'), user)
            identifier = request.path_params['identifier']
            schedule_grants.replace(identifier, asked.get('token'), spec, user, asked.get('enabled'))
            log.add('schedule', 'edited', user.name, address(request), schedule=identifier, kind_of_work=spec['kind'])
            return JSONResponse({'id': identifier, 'model_name': name})
        except PermissionError as problem:
            return error(403, str(problem))
        except (ValueError, TypeError, KeyError) as problem:
            return error(400, str(problem))

    async def schedule_control(request: Request, user: User) -> Response:
        """Enable or disable a compute-owned schedule grant."""
        if retired:
            return error(410, 'The front keeps schedules for this node; use turnstile.')
        if denied := lacks(user, 'manage-settings'):
            return denied
        asked = await read_json(request, {'enabled'})
        if isinstance(asked, Response):
            return asked
        if type(asked.get('enabled')) is not bool:
            return error(400, 'enabled must be true or false.')
        await users.fresh()  # the owner as front says they are now: whether they may still have a schedule resumed
        try:
            schedule_grants.enable(request.path_params['identifier'], asked['enabled'])
            log.add('schedule', 'resumed' if asked['enabled'] else 'paused', user.name, address(request), schedule=request.path_params['identifier'])
            return JSONResponse({'ok': True})
        except PermissionError as problem:
            return error(403, str(problem))
        except ValueError as problem:
            return error(404, str(problem))

    async def schedule_dispatch(request: Request) -> Response:
        """Only the saved job, under its live owner; no general impersonation or wake path."""
        if retired:
            return error(410, 'The front keeps schedules for this node; dispatch through turnstile.')
        try:
            identifier = request.path_params['identifier']
            await users.fresh()  # the owner as front says they are now
            owner, spec = schedule_grants.authorized(identifier, request.headers.get('x-suite-schedule', ''))
            asked = await read_json(request, {'due_at', 'task_token'})
            if isinstance(asked, Response):
                return asked
            due = asked.get('due_at')
            if not is_due(spec, due) or due > time.time():
                return error(400, 'The timestamp is not a due occurrence of this schedule.')
            owner, spec = schedule_grants.authorized(identifier, request.headers.get('x-suite-schedule', ''), due)
            number, backup_id = occurrence_ids(identifier, due)
            payload = ({'id': number, 'token': asked.get('task_token'), 'model_id': spec['model_id'],
                        'metadata': {'benchmark': spec['benchmark'], 'schedule': identifier, 'due_at': due}}
                       if spec['kind'] == 'benchmark' else {'id': backup_id, 'retention': spec['retention']})
            if spec['kind'] == 'backup':
                existing = backup_manifest(backup_root(compute_data_folder()), backup_id)
                if existing and existing.get('verification', {}).get('state') == 'verified':
                    return JSONResponse(existing)
            encoded = json.dumps(payload).encode()
            async def receive():
                """Provide the encoded scheduled request body to the internal ASGI handler."""
                return {'type': 'http.request', 'body': encoded, 'more_body': False}
            delegated = Request({**request.scope, 'path': '/loadcoach/tasks' if spec['kind'] == 'benchmark' else '/loadcoach/backups'}, receive)
            result = await tasks.create(delegated, user=owner) if spec['kind'] == 'benchmark' else await backup_create(delegated, owner)
            if result.status_code >= 400:
                return error(result.status_code, 'Scheduled work could not be accepted; check owner permissions, model and backup configuration.')
            log.add('schedule', 'dispatched', owner.name, address(request), schedule=identifier, due_at=due, job=number if spec['kind'] == 'benchmark' else backup_id)
            return result
        except PermissionError as problem:
            return error(403, str(problem))
        except (ValueError, TypeError, KeyError, OverflowError):
            return error(400, 'Invalid scheduled occurrence.')

    # ---- database inventory and exports -----------------------------------------------------

    def compute_database_sources():
        """Bound database IDs to configured stores and discovered local SQLite files."""
        return store_sources(
            compute_recovery_stores(),
            compute_data_folder(),
            {
                (local_path(core).name if local_path(core) else "loadcoach.db"): "LoadCoach models, requests, batches, settings and event log",
                "spotter.db": "Spotter telemetry history",
            },
        )

    async def databases_list(request: Request, user: User) -> Response:
        """Inspect compute's SQLite files without accepting a path from the caller."""
        if denied := lacks(user, "manage-settings"):
            return denied
        sources = compute_database_sources()
        rows = (await async_summaries(sources, config.machine_name) if request.query_params.get('summary') == '1'
                else await asyncio.to_thread(database_catalogs, sources, config.machine_name))
        tools = dump_tools()
        postgres_tools = tools["pg_dump"]["available"] and tools["pg_restore"]["available"]
        return JSONResponse(
            {
                "machine": config.machine_name,
                "databases": rows,
                "health": await compute_store_health(),
                "tools": tools,
                "engines": {
                    "sqlite": {"available": True, "state": "supported"},
                    "postgresql": {"available": postgres_tools, "state": "supported" if postgres_tools else "native tools missing",
                                   "reason": "Repository, custom-format backup, isolated restore verification and SQL export support are implemented."},
                },
            }
        )

    async def compute_store_health():
        """Read only bound-store probes and existing manifests, avoiding full inventory scans."""
        return await async_stores_health(compute_recovery_stores(), config.machine_name, backup_manifests(backup_root(compute_data_folder())))

    async def databases_health(request: Request, user: User) -> Response:
        """Expose safe, lightweight operational signals to storage managers."""
        if denied := lacks(user, "manage-settings"):
            return denied
        return JSONResponse({"machine": config.machine_name, "stores": await compute_store_health()})

    from loadcoach.classifier_jobs import ClassifierJobs
    classifier_jobs = ClassifierJobs(compute_data_folder() / 'classifier-tools',
        engine_for('llama_cpp').installation(config).executable, places, files_changed)
    database_operations = DatabaseOperations(compute_database_sources, compute_data_folder() / 'database-operations',
        lambda event, owner, **detail: log.add('database', event, owner, machine=config.machine_name, **detail), machine=config.machine_name)

    async def database_operation(request: Request, user: User) -> Response:
        if denied := lacks(user, 'manage-settings'):
            return denied
        return await database_operations.handle(request, user.identity or user.name)

    async def database_dump(request: Request, user: User) -> Response:
        """Download an exact table or complete compute database as restorable SQL."""
        if denied := lacks(user, "manage-settings"):
            return denied
        source = compute_database_sources().get(request.path_params["database"])
        if source is None:
            return error(404, "There is no such compute database.")
        if expected_identity := request.query_params.get('identity'):
            from loadcoach.database_admin import source_identity
            try:
                if await asyncio.to_thread(source_identity, source) != expected_identity:
                    return error(409, 'This store binding changed. Refresh before exporting.')
            except Exception:
                return error(409, 'This store identity cannot be verified.')
        table = request.query_params.get("table") or None
        try:
            path, method = await asyncio.to_thread(make_sql_dump, source, table)
        except KeyError:
            return error(404, "There is no such table in that database.")
        except (OSError, ValueError, TypeError, sqlite3.Error, subprocess.SubprocessError, RuntimeError) as problem:
            return error(500, 'Database dump failed. Check database permissions, connectivity, disk space and native tools.')
        log.add("database", "dumped", user.name, address(request), database=source.id,
                table=table or "all", machine=config.machine_name, method=method)
        return FileResponse(
            path,
            media_type="application/sql",
            filename=safe_filename(source, table),
            headers={"x-suite-dump-method": method, "cache-control": "no-store"},
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    def route(path: str, handler, method: str = "GET") -> Route:
        """Register the handler behind the service authentication wrapper."""
        return Route(path, signed_in(handler), methods=[method])

    from soundroom.packages import Packages as AudioPackages, routes as audio_package_routes
    audio_packages = AudioPackages(places, web, hub_token, log, lambda: getattr(config, "engines", {}).get("audiocpp", {}).get("manifest", ""),
                                   lambda: set(model_files()))

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/", test_page),
            route("/v1/models", models),
            route("/v1/models/{name:path}", model),
            route("/v1/batches", batch_list),
            route("/v1/batches/{number}", batch_one),
            route("/v1/batches/{number}/cancel", batch_one, "POST"),
            route("/loadcoach/batches", batch_list),
            *tasks.routes(),
            route('/loadcoach/schedules', schedule_register, 'POST'),
            route('/loadcoach/schedules/{identifier}', schedule_control, 'POST'),
            route('/loadcoach/schedules/{identifier}/edit', schedule_edit, 'POST'),
            Route('/loadcoach/schedules/{identifier}/dispatch', schedule_dispatch, methods=['POST']),
            route("/loadcoach/status", status),
            route("/loadcoach/me", me),
            route("/loadcoach/unload", unload, "POST"),
            route("/loadcoach/hold", hold, "POST"),
            route("/loadcoach/hold/{name:path}", unhold, "DELETE"),
            route("/loadcoach/lock", lock, "POST"),
            route("/loadcoach/unlock", unlock, "POST"),
            route("/loadcoach/clear", clear, "POST"),
            route("/loadcoach/drain", drain, "POST"),
            route("/loadcoach/stop/{entry:int}", stop, "POST"),
            route('/loadcoach/stop-unlogged', stop_unlogged, 'POST'),
            route("/loadcoach/peek/{entry:int}", peek),
            route('/loadcoach/diagnostics/{entry:int}', request_diagnostics),
            route('/loadcoach/alert-evidence', alert_evidence),
            route("/loadcoach/settings", settings_all),
            route("/loadcoach/tools", tools_info),
            route("/loadcoach/cards", cards_list),
            route("/loadcoach/cards", settings_change, "POST"),
            route("/loadcoach/settings/{program}", settings_change, "POST"),
            route("/loadcoach/settings/{program}/changes", settings_changes),
            route("/loadcoach/settings/{program}/seen", settings_seen, "POST"),
            route("/loadcoach/backups", backups_list),
            route("/loadcoach/backups", backup_create, "POST"),
            route("/loadcoach/backups/{backup_id}", backup_one),
            route("/loadcoach/databases", databases_list),
            route("/loadcoach/databases/health", databases_health),
            route("/loadcoach/databases/{database}/dump", database_dump),
            Route('/loadcoach/database-operations/{operation:path}', signed_in(database_operation), methods=['GET', 'POST']),
            route("/loadcoach/models", model_list),
            route("/loadcoach/model-setup", model_setup_page),
            route("/loadcoach/model-setup", model_setup_page, "POST"),
            route('/loadcoach/loadouts', loadout_control),
            route('/loadcoach/loadouts', loadout_control, 'POST'),
            route("/loadcoach/engines", engines_list),
            route("/loadcoach/components", components_here),
            route('/loadcoach/classifier-tools', classifier_operations),
            route('/loadcoach/classifier-tools', classifier_operations, 'POST'),
            route('/loadcoach/classifier-conversions', classifier_operations),
            route('/loadcoach/classifier-conversions', classifier_operations, 'POST'),
            route("/loadcoach/flags", flags),
            route("/loadcoach/model-rename", model_rename, "POST"),  # not under /models/, where everything after is a model's name
            route("/loadcoach/model-forget-measure", model_forget_measure, "POST"),
            route("/loadcoach/model-lock", model_lock, "POST"),
            route("/loadcoach/fingerprints", fingerprint_list),
        route("/loadcoach/retired-models", retired_models),
            route("/loadcoach/facts", model_facts),
            route("/loadcoach/send-log", send_log, "POST"),
            route("/loadcoach/model-measure-all", model_measure_all, "POST"),
            route("/loadcoach/files", file_list),
            route("/loadcoach/file", file_facts),
            route("/loadcoach/files/move", file_move, "POST"),
            route("/loadcoach/files/copy", file_copy, "POST"),
            route("/loadcoach/files/hash", file_digest),
            route("/loadcoach/files/content", file_content),
            route("/loadcoach/files/delete", file_delete, "POST"),
            route("/loadcoach/files/cleanup", files_cleanup, "POST"),
            route("/loadcoach/hub", hub_look),
            *audio_package_routes(route, lacks, read_json, error, audio_packages, address),
            route("/loadcoach/downloads", download_list),
            route("/loadcoach/downloads", download_start, "POST"),
            route("/loadcoach/downloads/stop", download_stop, "POST"),
            route("/loadcoach/downloads/discard", download_discard, "POST"),
            route("/loadcoach/images/check", image_check, "POST"),
            route("/loadcoach/models/{name:path}", model_put, "POST"),
            route("/loadcoach/models/{name:path}", model_remove, "DELETE"),
            route("/loadcoach/plugins/mirror", plugin_mirror),
            route("/loadcoach/machine-key", machine_key, "POST"),
            route("/loadcoach/users/{name}/rename", user_rename, "POST"),
            route("/v1/{path:path}", asked_of, "POST"),
        ],
    )
    app.state.databases = databases
    app.add_middleware(Barrier, gate=gate)
    app.add_middleware(Statements, key_file=config.machine_key_file)  # who each caller is, as turnstile signed it
    from loadcoach.rerank import RerankAlias
    app.add_middleware(RerankAlias)
    observation.instrument(app, "loadcoach", log, config.machine_name)
    return app
