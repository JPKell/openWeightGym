"""Independent HTTP adapter server, installable without openWeightGym's compute or control node applications."""

from __future__ import annotations

from loadcoach import observability as observation

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

from db import database as database_target, migrate
from db.schemas import PLUGIN_RUNTIME

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from plugins._sdk import AdapterError, Manifest, MODEL, Run, SLUG, atomic_json, read_json, validate

OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
METRICS = {"tokens", "usage", "cost", "input_tokens", "output_tokens", "total_tokens", "token_usage",
           "prompt_tokens", "completion_tokens", "answer_tokens", "cache_read_tokens", "cache_write_tokens", "tokens_per_second"}
MAX_BODY = 24 * 1024 * 1024
MAX_EVENTS = 16 * 1024 * 1024


def without_metrics(value):
    """Strip adapter accounting fields recursively, including nested event payloads."""
    if isinstance(value, dict):
        return {key: without_metrics(child) for key, child in value.items() if key not in METRICS}
    if isinstance(value, list):
        return [without_metrics(child) for child in value]
    return value


def failure(problem: AdapterError) -> dict:
    """Use the OpenAI error envelope without returning subprocess diagnostics."""
    return {"error": {"message": str(problem), "type": "adapter_error", "code": problem.code}}


async def body_of(request: Request) -> dict:
    """Bound incoming JSON before parsing, including inline image payloads."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise AdapterError("Request exceeds 24 MiB.", 413, "request_too_large")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise AdapterError("Expected JSON.", 400, "invalid_request") from None
    if not isinstance(value, dict):
        raise AdapterError("Expected a JSON object.", 400, "invalid_request")
    return value


class Runtime:
    """Settings snapshots, owned native sessions and durable request receipts for one instance."""

    def __init__(self, config_file: Path, manifest: Manifest, driver=None) -> None:
        """Bind adapter configuration, authorization and active request ownership."""
        self.file, self.manifest = config_file.resolve(), manifest
        self.config = read_json(self.file)
        if self.config.get("plugin") != manifest.id or not SLUG.fullmatch(self.config.get("instance", "")):
            raise ValueError("Adapter configuration needs matching plugin and a valid instance.")
        validate(manifest.schema, self.config["settings"])
        self.key_file = self.file.parent / self.config["key_file"]
        database = self.file.parent / "data/runtime.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        self.lock = os.open(database.parent / "runtime.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.driver = driver or manifest.load()
        except BaseException:
            os.close(self.lock)
            raise
        os.close(os.open(database, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(database, 0o600)
        target = database_target(database)
        self.db = target.connect(check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        migrate(target, self.db, "plugins.runtime", PLUGIN_RUNTIME)
        with self.db:
            self.db.execute("UPDATE requests SET status = 'uncertain', ended = ? WHERE status = 'running'", (time.time(),))
        self.active: dict[str, asyncio.Task] = {}
        self.sessions: set[str] = set()
        self.models_cache = (0.0, [])
        self.listening = (self.config["settings"].get("listen_host", "127.0.0.1"), self.config["settings"].get("listen_port", 8450))

    def authenticate(self, request: Request) -> None:
        """Accept only this instance's pairing key, independent of compute connectivity."""
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        try:
            key = self.key_file.read_text().strip()
        except OSError:
            key = ""
        if scheme.lower() != "bearer" or not key or not secrets.compare_digest(supplied, key):
            raise AdapterError("Not authorized.", 401, "invalid_api_key")
        if request.headers.get("x-suite-instance", self.config["instance"]) != self.config["instance"]:
            raise AdapterError("Wrong adapter instance.", 403, "instance_mismatch")

    def owner(self, request: Request) -> str:
        """Use only the turnstile-supplied immutable identity, never the chat body's user field."""
        owner = request.headers.get("x-suite-user-id", "")
        if not OWNER.fullmatch(owner):
            raise AdapterError("A turnstile user identity is required.", 400, "missing_identity")
        return owner

    async def models(self, force: bool = False) -> list[dict]:
        """Cache bounded profile discovery briefly and invalidate it on settings changes."""
        if force or time.monotonic() - self.models_cache[0] > 30:
            inventory = await self.driver.models(dict(self.config["settings"]))
            if not isinstance(inventory, list) or len(inventory) > 1000 or not all(
                isinstance(item, dict) and isinstance(item.get("id"), str) and MODEL.fullmatch(item["id"]) for item in inventory
            ):
                raise AdapterError("Invalid profile/model inventory.", 502, "invalid_inventory")
            self.models_cache = (time.monotonic(), inventory)
        return self.models_cache[1]

    def metadata(self) -> dict:
        """Expose public applied settings and explicitly identify listener restart requirements."""
        settings = self.config["settings"]
        desired = (settings.get("listen_host", "127.0.0.1"), settings.get("listen_port", 8450))
        return {"revision": self.config["revision"], "settings": settings, "policy": self.config.get("policy", {}),
                "restart_required": desired != self.listening, "listening": {"host": self.listening[0], "port": self.listening[1]}}

    def update(self, body: dict) -> dict:
        """Validate and persist one monotonic revision without changing in-flight run snapshots."""
        if set(body) != {"revision", "settings", "policy"} or type(body["revision"]) is not int or body["revision"] < 1:
            raise AdapterError("Expected revision, settings and policy.", 400, "invalid_settings")
        validate(self.manifest.schema, body["settings"])
        policy = body["policy"]
        if not isinstance(policy, dict) or set(policy) != {"allow_start", "allow_restart"} or not all(type(v) is bool for v in policy.values()):
            raise AdapterError("Invalid control policy.", 400, "invalid_settings")
        previous = self.config["revision"]
        if body["revision"] < previous or (body["revision"] == previous and any(body[key] != self.config.get(key, {}) for key in ("settings", "policy"))):
            raise AdapterError("Conflicting or stale settings revision.", 409, "settings_conflict")
        changed = {**self.config, **body}
        atomic_json(self.file, changed)
        self.config = changed
        self.models_cache = (0.0, [])
        return self.metadata()

    def reserve(self, request: Request, body: dict) -> tuple[dict, dict | None]:
        """Bind an accepted request to its owner/session and prevent duplicate execution."""
        owner, profile = self.owner(request), body["model"]
        mode = request.headers.get("x-suite-session-mode", "fresh")
        session_id = request.headers.get("x-suite-session-id", "")
        if mode not in ("fresh", "persistent") or (mode == "fresh" and session_id):
            raise AdapterError("Use persistent mode to resume a session ID.", 400, "invalid_session_mode")
        idem = request.headers.get("idempotency-key")
        if idem is not None and (not 1 <= len(idem) <= 128 or any(ord(c) < 32 for c in idem)):
            raise AdapterError("Invalid Idempotency-Key.", 400, "invalid_idempotency_key")
        try:
            fingerprint = hashlib.sha256(json.dumps({"body": body, "mode": mode, "session": session_id},
                sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        except ValueError:
            raise AdapterError("Non-finite numbers are invalid JSON input.", 400, "invalid_request") from None
        existing = self.db.execute("SELECT * FROM requests WHERE owner = ? AND profile = ? AND idem = ?",
                                   (owner, profile, idem)).fetchone() if idem else None
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise AdapterError("Idempotency-Key was used with different input.", 409, "idempotency_conflict")
            if existing["status"] == "completed":
                return dict(existing), json.loads(existing["response"])
            raise AdapterError("Previous request is " + existing["status"] + "; it will not be executed again.", 409, "request_already_submitted")
        if len(self.active) >= self.config["settings"].get("max_concurrent", 4):
            raise AdapterError("Adapter capacity is in use.", 429, "adapter_busy")
        if session_id:
            session = self.db.execute("SELECT * FROM sessions WHERE id = ? AND owner = ? AND profile = ?",
                                      (session_id, owner, profile)).fetchone()
            if not session:
                raise AdapterError("You have no such session for this profile/instance.", 404, "session_not_found")
            if session_id in self.sessions:
                raise AdapterError("This session has a turn in progress.", 409, "session_busy")
            if not session["native"] and self.db.execute("SELECT 1 FROM requests WHERE session = ? LIMIT 1", (session_id,)).fetchone():
                raise AdapterError("Native session identity is uncertain; start a fresh session.", 409, "session_uncertain")
        else:
            session_id = "ps_" + secrets.token_urlsafe(24)
            with self.db:
                self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, NULL, ?)", (session_id, owner, profile, time.time()))
            session = self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        entry = {"id": "pr_" + secrets.token_urlsafe(24), "owner": owner, "profile": profile,
                 "session": session_id, "native": session["native"], "persistent": mode == "persistent", "created": time.time()}
        with self.db:
            self.db.execute("INSERT INTO requests (id, owner, profile, session, idem, fingerprint, status, created) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                            (entry["id"], owner, profile, session_id, idem, fingerprint, entry["created"]))
        self.sessions.add(session_id)
        return entry, None

    def finish(self, entry: dict, status: str, response: dict | None = None, code: str | None = None) -> None:
        """Persist the receipt before releasing capacity; no prompt or token report is stored."""
        with self.db:
            self.db.execute("UPDATE requests SET status = ?, ended = ?, response = ?, error_code = ? WHERE id = ?",
                            (status, time.time(), json.dumps(response) if response else None, code, entry["id"]))
        self.sessions.discard(entry["session"])

    def headers(self, entry: dict) -> dict:
        """Return stable opaque identities before the native subprocess emits its session ID."""
        return {"X-Suite-Adaptor": self.manifest.id, "X-Suite-Instance": self.config["instance"],
                "X-Suite-Profile": entry["profile"],
                "X-Suite-Session-ID": entry["session"], "X-Suite-Request-ID": entry["id"], "cache-control": "no-store"}

    async def produce(self, entry: dict, body: dict, public_model: str, queue: asyncio.Queue, result: dict, settings: dict) -> None:
        """Own one CLI task independently from the HTTP sender, with cancellation cleanup."""
        run = Run(entry["id"], entry["owner"], entry["profile"], body["messages"], entry["persistent"],
                  entry["native"], "suite:" + entry["session"], body.get("reasoning_effort"))
        events, reasoning, terminal, size = [], "", None, 0
        try:
            async with aclosing(self.driver.events(run, settings)) as emitted:
                async for original in emitted:
                    event = without_metrics(original)
                    if event.get("type") in ("usage", "tokens", "cost", "token_usage", "billing"):
                        continue
                    size += len(json.dumps(event).encode())
                    if size > MAX_EVENTS:
                        raise AdapterError("Adapter event stream exceeds 16 MiB.", 502, "event_limit")
                    if event.get("type") == "session":
                        native = event.get("native_id")
                        if not isinstance(native, str) or not 1 <= len(native) <= 200 or any(ord(c) < 32 for c in native):
                            raise AdapterError("Invalid native session ID.", 502, "invalid_event")
                        with self.db:
                            self.db.execute("UPDATE sessions SET native = ? WHERE id = ?", (native, entry["session"]))
                        continue
                    if event.get("type") == "result":
                        if terminal is not None or not isinstance(event.get("text"), str):
                            raise AdapterError("Invalid terminal result.", 502, "invalid_event")
                        terminal = event["text"]
                    elif event.get("type") == "error":
                        raise AdapterError("External adapter run failed.", 502, "external_run_failed")
                    elif event.get("type") == "reasoning":
                        reasoning += event.get("text") or ""
                    else:
                        events.append(event)
                    await queue.put(event)
            if terminal is None:
                raise AdapterError("Adapter did not return a final result.", 502, "missing_result")
            message = {"role": "assistant", "content": terminal, "plugin_events": events}
            if reasoning:
                message["reasoning_content"] = reasoning
            response = {"id": "chatcmpl-" + entry["id"], "object": "chat.completion", "created": int(entry["created"]),
                        "model": public_model, "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
            result["response"] = response
            self.finish(entry, "completed", response)
        except asyncio.CancelledError:
            result["error"] = AdapterError("Adapter run was cancelled; work is not automatically retried.", 499, "run_cancelled")
            self.finish(entry, "cancelled", code="run_cancelled")
        except AdapterError as problem:
            result["error"] = problem
            self.finish(entry, "failed", code=problem.code)
        except Exception:
            result["error"] = AdapterError("Adapter driver failed.", 502, "driver_failed")
            self.finish(entry, "failed", code="driver_failed")


def build_app(config_file: Path, manifest: Manifest | None = None, driver=None) -> Starlette:
    """Build the independent server from its own configuration and explicitly selected plugin."""
    config = read_json(config_file)
    manifest = manifest or Manifest.read(Path(__file__).parent / config["plugin"])
    runtime = Runtime(config_file, manifest, driver)
    from loadcoach.log import Log
    from loadcoach.client_origin import ClientOrigin
    observation_log = Log(runtime.db.database, chained=False)

    def guarded(handler):
        """Protect management/run routes with the pairing key and a common error envelope."""
        async def checked(request):
            """Verify adapter authorization and translate protocol errors into public responses."""
            headers = {"X-Suite-Adaptor": manifest.id, "X-Suite-Instance": runtime.config["instance"], "cache-control": "no-store"}
            try:
                runtime.authenticate(request)
                if request.headers.get('x-suite-user-id'):
                    from types import SimpleNamespace
                    owner = runtime.owner(request)
                    observation.identify(SimpleNamespace(identity=owner, name=owner, key_identity=None, can=lambda ability:False))
                response = await handler(request)
            except AdapterError as problem:
                response = JSONResponse(failure(problem), status_code=problem.status)
            response.headers.update(headers)
            return response
        return checked

    async def capabilities(request):
        """Expose declared operations, never the credential or machine configuration."""
        return JSONResponse({"protocol": 1, "plugin": manifest.id, "capabilities": sorted(manifest.capabilities)})

    async def models(request):
        """List underlying entries and their labels for control-node-side alias management."""
        if "models" not in manifest.capabilities:
            raise AdapterError("Model discovery is unsupported.", 405, "unsupported_capability")
        return JSONResponse({"object": "list", "data": await runtime.models(force=True)})

    async def settings(request):
        """Read or atomically apply public configuration; pairing credentials stay separate."""
        return JSONResponse(runtime.update(await body_of(request)) if request.method == "PUT" else runtime.metadata())

    async def health(request):
        """Report runtime heartbeat plus external-service health when the driver declares it."""
        if "health" not in manifest.capabilities:
            raise AdapterError("Health is unsupported.", 405, "unsupported_capability")
        return JSONResponse({"heartbeat": time.time(), "active_runs": len(runtime.active),
                             "external": await runtime.driver.health(dict(runtime.config["settings"]))})

    async def control(request):
        """Require both declared capability and explicit allowance before service control."""
        body = await body_of(request)
        action = body.get("action")
        if action not in ("start", "restart") or action not in manifest.capabilities:
            raise AdapterError("Unsupported control action.", 405, "unsupported_capability")
        if not runtime.config.get("policy", {}).get("allow_" + action):
            raise AdapterError("This control action is disabled in settings.", 403, "control_disabled")
        if runtime.active:
            raise AdapterError("Finish active runs before service control.", 409, "adapter_busy")
        return JSONResponse(await runtime.driver.control(action, dict(runtime.config["settings"])))

    async def status(request):
        """Show only the requesting owner's receipt and any completed final response."""
        row = runtime.db.execute("SELECT * FROM requests WHERE id = ? AND owner = ?",
                                 (request.path_params["id"], runtime.owner(request))).fetchone()
        if not row:
            raise AdapterError("You have no such request.", 404, "request_not_found")
        return JSONResponse({"id": row["id"], "session": row["session"], "profile": row["profile"], "status": row["status"],
                             "response": json.loads(row["response"]) if row["response"] else None, "error_code": row["error_code"]})

    async def observations(request):
        """Return/acknowledge only bounded optional metadata under this private pairing key."""
        if request.method == 'POST':
            body = await body_of(request)
            rows = body.get('ack', [])
            if not isinstance(rows, list) or len(rows) > 500:
                raise AdapterError('Invalid observation acknowledgement.', 400, 'invalid_request')
            from loadcoach.observation_store import acknowledge
            try:
                accepted = await asyncio.to_thread(acknowledge, observation_log.db.database, rows)
            except ValueError:
                raise AdapterError('Invalid observation acknowledgement.', 400, 'invalid_request') from None
            if not accepted:
                raise AdapterError('Optional observation acknowledgement is unavailable.', 503, 'observation_unavailable')
            return JSONResponse({'acknowledged':len(rows)})
        if app.state.observations:
            await asyncio.to_thread(app.state.observations.flush, .5)
        rows = [json.loads(row[0]) for row in observation_log.db.execute(
            'SELECT data FROM observation_spans WHERE sent=0 ORDER BY at LIMIT 500')]
        health = app.state.observations.health() if app.state.observations else {'recording':False}
        count, oldest = runtime.db.execute('SELECT COUNT(*),MIN(at) FROM observation_spans WHERE sent=0').fetchone()
        health.update(backlog_count=count, backlog_age_seconds=max(0, time.time()-oldest) if oldest else 0.)
        return JSONResponse({'observations':rows, 'health':health})

    async def chat(request):
        """Accept one owned run and return an ordinary completion or actual SSE events."""
        body = await body_of(request)
        profile, messages = body.get("model"), body.get("messages")
        if not isinstance(profile, str) or not MODEL.fullmatch(profile):
            raise AdapterError("Invalid model/profile.", 400, "invalid_model")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 1000 or not all(
            isinstance(item, dict) and item.get("role") in ("user", "assistant", "system", "developer", "tool") for item in messages
        ):
            raise AdapterError("Invalid messages.", 400, "invalid_messages")
        if type(body.get("stream", False)) is not bool or type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
            raise AdapterError("stream must be boolean and n must be 1.", 400, "invalid_request")
        if any(body.get(name) for name in ("tools", "tool_choice", "functions", "function_call")):
            raise AdapterError("This agent executes its own tools; caller tool definitions are unsupported.", 400, "unsupported_tools")
        effort = body.get("reasoning_effort")
        if effort is not None and (not isinstance(effort, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", effort)):
            raise AdapterError("Invalid reasoning effort.", 400, "invalid_reasoning")
        if "models" in manifest.capabilities and profile not in {row["id"] for row in await runtime.models()}:
            raise AdapterError("The profile/model does not exist.", 404, "model_not_found")
        entry, replay = runtime.reserve(request, body)
        headers = runtime.headers(entry)
        ignored = [key for key in body if key not in ("model", "messages", "stream", "reasoning_effort", "n") and re.fullmatch(r"[A-Za-z0-9_]{1,64}", key)]
        if ignored:
            headers["X-Suite-Ignored-Parameters"] = ",".join(sorted(ignored))
        public = request.headers.get("x-suite-model", profile)
        queue, result = asyncio.Queue(maxsize=64), {}
        worker = None
        if replay:
            result["response"] = replay
        else:
            snapshot = json.loads(json.dumps(runtime.config["settings"]))
            worker = asyncio.create_task(runtime.produce(entry, body, public, queue, result, snapshot))
            runtime.active[entry["id"]] = worker
            worker.add_done_callback(lambda _: runtime.active.pop(entry["id"], None))

        async def events():
            """Yield bounded queued events; cancel and collect the worker on disconnection."""
            try:
                while worker and (not worker.done() or not queue.empty()):
                    try:
                        yield await asyncio.wait_for(queue.get(), .2)
                    except TimeoutError:
                        continue
                if replay:
                    message = replay["choices"][0]["message"]
                    for event in message.get("plugin_events", []):
                        yield event
                    if message.get("reasoning_content"):
                        yield {"type": "reasoning", "text": message["reasoning_content"]}
                    yield {"type": "result", "text": message["content"]}
            finally:
                if worker and not worker.done():
                    worker.cancel()
                    await asyncio.shield(asyncio.gather(worker, return_exceptions=True))

        if not body.get("stream", False):
            async def disconnected():
                """Watch for caller disconnection so the owned adapter request can be cancelled."""
                while worker and not worker.done():
                    if await request.is_disconnected():
                        worker.cancel()
                        return
                    await asyncio.sleep(.2)
            watcher = asyncio.create_task(disconnected())
            try:
                async for _ in events():
                    pass
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if "error" in result:
                return JSONResponse(failure(result["error"]), status_code=result["error"].status, headers=headers)
            return JSONResponse(result["response"], headers=headers)

        async def stream():
            """Map normalized events to compatible chunks with optional adapter extensions."""
            def chunk(delta, finish=None):
                """Encode one adapter event in the supported OpenAI streaming response shape."""
                return "data: " + json.dumps({"id": "chatcmpl-" + entry["id"], "object": "chat.completion.chunk",
                    "created": int(entry["created"]), "model": public, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
            try:
                yield chunk({"role": "assistant"})
                async for event in events():
                    kind = event.get("type")
                    delta = {"content": event["text"]} if kind == "result" else {
                        "reasoning_content": event.get("text") or ""} if kind == "reasoning" else {"plugin_events": [event]}
                    yield chunk(delta)
                if "error" in result:
                    yield "data: " + json.dumps(failure(result["error"])) + "\n\n"
                else:
                    yield chunk({}, "stop")
                yield "data: [DONE]\n\n"
            finally:
                if worker and not worker.done():
                    worker.cancel()
                    await asyncio.shield(asyncio.gather(worker, return_exceptions=True))

        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    @asynccontextmanager
    async def lifespan(app):
        """Cancel and collect active subprocesses before closing the private receipt store."""
        try:
            yield
        finally:
            pending = list(runtime.active.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            runtime.db.close()
            os.close(runtime.lock)
            await observation.shutdown(app)

    app = Starlette(lifespan=lifespan, routes=[
        Route("/adapter/v1/capabilities", guarded(capabilities)),
        Route("/v1/models", guarded(models)),
        Route("/adapter/v1/settings", guarded(settings), methods=["GET", "PUT"]),
        Route("/adapter/v1/health", guarded(health)),
        Route("/adapter/v1/control", guarded(control), methods=["POST"]),
        Route("/adapter/v1/runs/{id}", guarded(status)),
        Route("/adapter/v1/observations", guarded(observations), methods=['GET','POST']),
        Route("/v1/chat/completions", guarded(chat), methods=["POST"]),
    ])
    app.add_middleware(ClientOrigin, key_file=str(runtime.key_file))
    observation.instrument(app, 'adapter', observation_log, 'adapter:'+config['plugin']+':'+config['instance'])
    app.state.runtime = runtime
    return app


def main() -> None:
    """Run an independent adapter using its installed settings file and listener options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    app = build_app(args.config)
    settings = app.state.runtime.config["settings"]
    uvicorn.run(app, host=settings.get("listen_host", "127.0.0.1"), port=settings.get("listen_port", 8450),
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
