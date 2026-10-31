"""Bounded task-local tracing; credentials and inference content never enter telemetry.

External IDs are correlation only. Internal ingress continuity is accepted exclusively
from verified openWeightGym statements. Optional sinks never alter required audit semantics.
"""

from __future__ import annotations

import contextvars
import asyncio
import json
import functools
import inspect
import logging
import re
import secrets
import hashlib
import sys
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

CURRENT = contextvars.ContextVar("suite_observation", default=None)
PROCESS = secrets.token_hex(16)
RELEASE = next(
    (
        path.name
        for path in Path(__file__).resolve().parents
        if path.parent.name == "releases"
    ),
    "checkout",
)
TRACE = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})\Z")
LABEL = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
ID = re.compile(r"[0-9a-f]{32}\Z")
SPAN = re.compile(r"[0-9a-f]{16}\Z")
FIELDS = frozenset(
    (
        "request_number",
        "model_id",
        "build",
        "attempt",
        "job",
        "job_trace_id",
        "outcome",
        "error_code",
        "first_output_seconds",
        "prompt_tokens",
        "answer_tokens",
        "queue_seconds",
        "readiness_seconds",
        "benchmark",
        "tool_executed",
    )
)
ALERT_LIMITS = {
    "observation_error_percent": (0, 100),
    "observation_p95_seconds": (0, 86400),
    "observation_min_samples": (1, 1000000),
    "observation_window_seconds": (60, 30 * 86400),
    "observation_alert_hold_seconds": (1, 86400),
}


def alert_value(name: str, value):
    """Validate finite alert thresholds once for startup, live editing and followers."""
    low, high = ALERT_LIMITS[name]
    integer = name not in ("observation_error_percent", "observation_p95_seconds")
    if (
        type(value) not in ((int,) if integer else (int, float))
        or not low <= value <= high
    ):
        raise ValueError(
            f"{name} must be {'a whole number' if integer else 'a number'} from {low} through {high}."
        )
    return value


def trace_parent(raw: bytes):
    """Parse bounded W3C v00/future-version context; invalid input starts a fresh trace."""
    if not 55 <= len(raw) <= 512:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    fields = text.split("-")
    if (
        len(fields) < 4
        or not re.fullmatch("[0-9a-f]{2}", fields[0])
        or fields[0] == "ff"
    ):
        return None
    if fields[0] == "00" and (len(fields) != 4 or len(text) != 55):
        return None
    if (
        not ID.fullmatch(fields[1])
        or not SPAN.fullmatch(fields[2])
        or not re.fullmatch("[0-9a-f]{2}", fields[3])
    ):
        return None
    return (fields[1], fields[2]) if int(fields[1], 16) and int(fields[2], 16) else None


@dataclass
class Root:
    """Shared request policy and budget, resolved before any optional persistence."""

    request_id: str = field(default_factory=lambda: secrets.token_hex(16))
    trace_id: str = field(default_factory=lambda: secrets.token_hex(16))
    owner_id: str = ""
    user: str = ""
    key_id: str = ""
    application: str = ""
    application_id: str = ""
    work: str = ""
    quiet: bool = False
    requested_quiet: bool = False
    resolved: bool = False
    spans: int = 0
    size: int = 0
    truncated: bool = False
    benchmark: bool = False


@dataclass
class Context:
    """One service/operation span with a shared request policy and bounded metadata."""

    root: Root
    service: str
    operation: str
    sink: object = None
    parent_id: str = ""
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    began: float = field(default_factory=time.monotonic)
    at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    ingress: bool = True
    started: bool = False


def wire() -> dict:
    """Return only validated correlation fields for an authenticated internal statement."""
    current = CURRENT.get()
    if current is None:
        return {}
    root = current.root
    return {
        "request_id": root.request_id,
        "trace_id": root.trace_id,
        "parent_id": current.span_id,
        "application": root.application,
        "work": root.work,
        "quiet": root.quiet,
        "job": current.metadata.get("job", ""),
        "benchmark": root.benchmark,
    }


def adopt(value) -> None:
    """Adopt context only after the caller has verified its request-bound signature."""
    current = CURRENT.get()
    if current is None or not isinstance(value, dict):
        return
    if not all(
        isinstance(value.get(k), str)
        and pattern.fullmatch(value[k])
        and int(value[k], 16)
        for k, pattern in (("request_id", ID), ("trace_id", ID), ("parent_id", SPAN))
    ):
        return
    current.root.request_id = value["request_id"]
    current.root.trace_id = value["trace_id"]
    current.parent_id = value["parent_id"]
    current.ingress = False
    current.root.quiet = value.get("quiet") is True
    current.root.benchmark = value.get("benchmark") is True
    for name in ("application", "work"):
        text = value.get(name, "")
        if isinstance(text, str) and LABEL.fullmatch(text):
            setattr(current.root, name, text)
    if isinstance(value.get("job"), str) and LABEL.fullmatch(value["job"]):
        current.metadata["job"] = value["job"]


def identify(user, quiet: bool = False) -> None:
    """Resolve ownership from authenticated identity, never from correlation headers."""
    current = CURRENT.get()
    if current is not None and user is not None:
        current.root.owner_id = getattr(user, "identity", None) or user.name
        current.root.user = getattr(user, "info", {}).get("name", user.name)
        current.root.key_id = getattr(user, "key_identity", None) or ""
        configured = getattr(user, "via", None) or getattr(user, "info", {}).get(
            "via", ""
        )
        current.root.application_id = (
            configured
            if isinstance(configured, str) and LABEL.fullmatch(configured)
            else ""
        )
        current.root.quiet |= (
            quiet or current.root.requested_quiet and user.can("unlogged")
        )
        current.root.resolved = True
        start(current)


def start(current: Context) -> None:
    """Persist a bounded pending ingress only after authenticated logging policy is known."""
    if (
        current.sink
        and current.root.resolved
        and not current.started
        and current.operation == "http"
        and not current.root.quiet
    ):
        current.started = True
        finish(current, 0, "underway")


def silence() -> None:
    """Suppress all optional evidence for an authorized no-log operation and its children."""
    if current := CURRENT.get():
        current.root.quiet = True


def annotate(**values) -> None:
    """Attach allowlisted scalar facts; arbitrary exception text and bodies are rejected."""
    if current := CURRENT.get():
        for name, value in values.items():
            if name in FIELDS and (
                isinstance(value, (str, bool, int))
                or isinstance(value, float)
                and 0 <= value < 1e15
            ):
                if name == "benchmark":
                    current.root.benchmark |= value is True
                    value = current.root.benchmark
                current.metadata[name] = (
                    value[:128] if isinstance(value, str) else value
                )


def envelope(current: Context, status: int, outcome: str) -> dict | None:
    """Freeze a content-free span within the shared 256-span/64-KiB request budget."""
    root = current.root
    if root.quiet or not root.resolved:
        return None
    record = {
        "version": 1,
        "release": RELEASE,
        "id": current.span_id,
        "request_id": root.request_id,
        "trace_id": root.trace_id,
        "parent_id": current.parent_id,
        "service": current.service,
        "operation": current.operation,
        "process": PROCESS,
        "at": current.at,
        "seconds": max(0, time.monotonic() - current.began),
        "status": status,
        "outcome": outcome,
        "owner_id": root.owner_id,
        "user": root.user,
        "key_id": root.key_id,
        "application": root.application,
        "application_id": root.application_id,
        "work": root.work,
        "benchmark": root.benchmark,
        **current.metadata,
        "truncated": root.truncated,
        "finished": outcome != "underway",
        "ingress": current.ingress,
    }
    size = len(json.dumps(record, separators=(",", ":")).encode())
    if root.spans >= 250 or root.size + size > 60 * 1024:
        root.truncated = True
        if current.operation != "http":
            return None
        record["truncated"] = True
    if root.spans >= 256 or root.size + size > 64 * 1024:
        return None
    root.spans += 1
    root.size += size
    return record


def finish(current: Context, status: int = 200, outcome: str = "success") -> None:
    """Offer optional evidence to a nonblocking sink; recording never changes a response."""
    if current.sink and (record := envelope(current, status, outcome)):
        try:
            current.sink.submit(record)
        except Exception:
            pass  # Optional instrumentation never replaces the application's outcome.


def internal(app):
    """Mark a trusted in-process render with an object an external header cannot supply."""

    async def invoke(scope, receive, send):
        """Carry only this process's task context into its own ASGI subrequest."""
        scope = {
            **scope,
            "state": {**scope.get("state", {}), "_observation_parent": CURRENT.get()},
        }
        await app(scope, receive, send)

    return invoke


@contextmanager
def span(operation: str, **metadata):
    """Measure one explicit child operation, restoring context on error or cancellation."""
    parent = CURRENT.get()
    if parent is None:
        yield None
        return
    child = Context(parent.root, parent.service, operation, parent.sink, parent.span_id)
    child.ingress = False
    token = CURRENT.set(child)
    annotate(**metadata)
    status, outcome = 200, "success"
    try:
        yield child
    except BaseException as problem:
        status, outcome = (
            500,
            "cancelled"
            if isinstance(problem, (GeneratorExit,))
            or type(problem).__name__ == "CancelledError"
            else "error",
        )
        annotate(error_code=type(problem).__name__)
        raise
    finally:
        finish(child, status, outcome)
        CURRENT.reset(token)
        start(parent)


def responded(response):
    """Attach a returned HTTP outcome to the current operation without inspecting its body."""
    status = getattr(response, "status_code", None)
    if type(status) is int and 100 <= status <= 599 and (current := CURRENT.get()):
        current.metadata["status"] = status
        current.metadata["outcome"] = (
            "error" if status >= 500 else "refused" if status >= 400 else "success"
        )
        if status >= 400:
            current.metadata["error_code"] = "http_" + str(status)
    return response


def traced(operation: str):
    """Decorate a bounded preparation/dispatch operation without wrapping its response bytes."""

    def decorate(function):
        """Preserve the function's signature/documentation for source-derived references."""
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                """Measure the awaited operation and restore the caller's context."""
                with span(operation):
                    return responded(await function(*args, **kwargs))

            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            """Measure a synchronous operation and restore the caller's context."""
            with span(operation):
                return function(*args, **kwargs)

        return synchronous

    return decorate


@contextmanager
def job(operation: str, identifier: str, user=None, sink=None):
    """Start a fresh durable-job attempt; persisted job identity reconstructs its trace on resume."""
    parent = CURRENT.get()
    owner = (getattr(user, "identity", None) or user.name) if user else ""
    stable = hashlib.sha256(
        (operation + ":" + owner + ":" + str(identifier)).encode()
    ).hexdigest()
    if parent and parent.root.resolved:
        with span("job_link", job=str(identifier)[:128], job_trace_id=stable[:32]):
            pass
    root = Root(
        trace_id=stable[:32],
        work=(operation + "-" + str(identifier))[:128],
        quiet=parent.root.quiet if parent else False,
    )
    context = Context(
        root,
        parent.service if parent else "turnstile",
        operation,
        sink or (parent.sink if parent else None),
        ingress=False,
    )
    context.metadata["job"] = str(identifier)[:128]
    token = CURRENT.set(context)
    try:
        identify(user)
        yield context
    finally:
        finish(context)
        CURRENT.reset(token)


def jobbed(operation: str, identifier_index: int = 3):
    """Isolate async job attempts using their existing durable ID and authenticated owner."""

    def decorate(function):
        """Preserve callable documentation while removing stale task-local request state."""

        @functools.wraps(function)
        async def execute(*args, **kwargs):
            """Create fresh attempt identity from the persisted job or document identifier."""
            identifier = args[identifier_index]
            if isinstance(identifier, dict):
                identifier = identifier.get("id", "")
            with job(operation, str(identifier), args[1]):
                return await function(*args, **kwargs)

        return execute

    return decorate


@contextmanager
def remote(value, operation: str):
    """Adopt metadata received on an already authenticated private voice channel, per turn."""
    parent = CURRENT.get()
    root = Root()
    if parent:
        for name in (
            "owner_id",
            "user",
            "key_id",
            "application_id",
            "quiet",
            "resolved",
        ):
            setattr(root, name, getattr(parent.root, name))
    context = Context(
        root,
        parent.service if parent else "voice-gateway",
        operation,
        parent.sink if parent else None,
        ingress=False,
    )
    token = CURRENT.set(context)
    try:
        adopt(value)
        yield context
    finally:
        finish(context)
        CURRENT.reset(token)


def event_context() -> dict:
    """Link existing audit events to the current operation without copying private content."""
    current = CURRENT.get()
    if current is None or current.root.quiet:
        return {}
    return {
        "request_id": current.root.request_id,
        "trace_id": current.root.trace_id,
        "span_id": current.span_id,
        "parent_id": current.parent_id,
    }


# Routine polls. A catch-all route records these exact paths instead of its template, so a trace
# can be recognised as a poll wherever it enters; they hold no IDs, so nothing private is kept.
POLLS = ("/loadcoach/status", "/v1/models", "/loadcoach/facts", "/loadcoach/me", "/loadcoach/send-log",
         "/loadcoach/plugins/mirror", "/loadcoach/observation-metrics", "/loadcoach/alert-evidence",
         "/loadcoach/downloads", "/spotter/now", "/spotter/alert-evidence", "/voice/ready", "/turnstile/ready")


def route_of(scope) -> str:
    """Use bounded route templates; never retain query strings or arbitrary URL segments."""
    route = scope.get("route")
    path = scope.get("path", "")
    if route and getattr(route, "path", None):
        return path if "{path" in route.path and path in POLLS else route.path[:128]
    if path in (
        "/",
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/embeddings",
        "/mcp",
    ):
        return path
    first = path.split("/")[1:2]
    return (
        "/"
        + (
            first[0]
            if first
            and first[0]
            in ("loadcoach", "turnstile", "adapter", "voice", "chat", "requests")
            else "other"
        )
        + "/{path}"
    )


class Observe:
    """ASGI ingress IDs and service timing, without buffering requests or response streams."""

    def __init__(self, app, service: str, sink=None, user=None) -> None:
        """Use an optional bounded sink and an authenticated user resolver for this service."""
        self.app, self.service, self.sink, self.user = app, service, sink, user

    async def __call__(self, scope, receive, send):
        """Return IDs on every HTTP outcome and close observation on disconnect/cancellation."""
        if scope["type"] == "lifespan":
            try:
                return await self.app(scope, receive, send)
            finally:
                if self.sink and hasattr(self.sink, "close"):
                    await asyncio.to_thread(self.sink.close)
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        scope = {
            **scope,
            "headers": [
                (k, v)
                for k, v in scope.get("headers", [])
                if k.lower() not in (b"baggage", b"tracestate")
            ],
        }
        parent = scope.get("state", {}).get("_observation_parent")
        parent = parent if isinstance(parent, Context) else None
        # Only a trusted in-process ASGI call may inherit an existing context directly.
        root = parent.root if parent else Root()
        context = Context(
            root,
            self.service,
            "http" if scope["type"] == "http" else "websocket",
            self.sink,
            parent.span_id if parent else "",
        )
        context.ingress = parent is None
        root.requested_quiet |= headers.get(b"x-loadcoach-log") == b"off" and scope.get(
            "path"
        ) in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings")
        if parent is None:
            value = headers.get(b"traceparent", b"")
            if match := trace_parent(value):
                root.trace_id, context.parent_id = match
            for name, header in (
                ("application", b"x-suite-application"),
                ("work", b"x-loadcoach-trace"),
            ):
                raw = headers.get(header, b"")
                if len(raw) <= 128 and LABEL.fullmatch(
                    raw.decode("ascii", errors="ignore")
                ):
                    setattr(root, name, raw.decode("ascii", errors="ignore"))
        token = CURRENT.set(context)
        scope.setdefault("state", {})["observation"] = context
        status, sent, complete, disconnected, failed = 500, False, False, False, False

        async def incoming():
            """Observe disconnect metadata without reading ahead or buffering the body."""
            nonlocal disconnected
            message = await receive()
            disconnected |= message["type"] in (
                "http.disconnect",
                "websocket.disconnect",
            )
            return message

        async def outgoing(message):
            """Attach response identification before the first body byte is handed off."""
            nonlocal status, sent, complete
            if message["type"] == "http.response.start":
                status, sent = message["status"], True
                kept = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-request-id"
                ]
                kept.append((b"x-request-id", root.request_id.encode()))
                if any(k.lower() == b"access-control-allow-origin" for k, _ in kept):
                    exposed = next(
                        (
                            v.decode("latin-1")
                            for k, v in kept
                            if k.lower() == b"access-control-expose-headers"
                        ),
                        "",
                    )
                    kept = [
                        (k, v)
                        for k, v in kept
                        if k.lower() != b"access-control-expose-headers"
                    ]
                    kept.append(
                        (
                            b"access-control-expose-headers",
                            (exposed + ",x-request-id,x-loadcoach-request-id")
                            .lstrip(",")
                            .encode("latin-1"),
                        )
                    )
                message = {**message, "headers": kept}
            elif message["type"] == "websocket.accept":
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"x-request-id", root.request_id.encode()),
                    ],
                }
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                complete = True
                context.metadata["seconds"] = max(0, time.monotonic() - context.began)

        try:
            await self.app(scope, incoming, outgoing)
        except BaseException as problem:
            failed = True
            if not isinstance(problem, Exception):
                annotate(outcome="cancelled", error_code="cancelled")
                raise
            annotate(error_code=type(problem).__name__)
            if not sent:
                await outgoing(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await outgoing(
                    {
                        "type": "http.response.body",
                        "body": b'{"error":{"message":"Internal server error."}}',
                    }
                )
            else:
                # Outer server loggers must not print a driver's exception message or context.
                raise RuntimeError(
                    "openWeightGym response stream failed; request_id=" + root.request_id
                ) from None
        finally:
            if self.user and not root.resolved:
                identify(self.user(scope))
            if scope["type"] == "http":
                if status >= 400:
                    root.resolved = (
                        True  # Independent security metadata has no inferred owner.
                    )
                    if not context.metadata.get("error_code"):
                        context.metadata["error_code"] = "http_" + str(status)
                context.metadata["route"] = route_of(scope)
                outcome = (
                    "cancelled"
                    if context.metadata.get("error_code") == "cancelled"
                    else "error"
                    if failed
                    else "cancelled"
                    if not complete
                    else context.metadata["outcome"]
                    if context.metadata.get("outcome")
                    in ("error", "refused", "cancelled")
                    else "error"
                    if status >= 500
                    else "refused"
                    if status >= 400
                    else context.metadata.get("outcome", "success")
                )
                # Caller delivery failure takes precedence over an earlier backend success.
                context.metadata["outcome"] = outcome
                finish(context, status, outcome)
            elif root.resolved:
                finish(context, 101, "cancelled" if failed else "success")
            CURRENT.reset(token)


def instrument(app, service: str, log=None, machine: str = "front"):
    """Wire one optional writer and outer middleware into an existing service lifecycle."""
    sink = None
    if (
        os.environ.get("SUITE_OBSERVATION_RECORDING", "on") != "off"
        and log is not None
        and (log.db.database.engine != "sqlite" or log.db.database.path is not None)
    ):
        from loadcoach.observation_store import Store

        sink = Store(log.db.database, machine, not log.chained)
        log.observations = sink
    app.state.observations = sink
    app.add_middleware(Observe, service=service, sink=sink)
    return sink


def configured(app, service: str, settings: dict):
    """Reuse an explicitly configured owning store for optional voice observations."""
    log = None
    if settings.get("observability_database") or settings.get("observability_dsn_file"):
        from db import Database
        from loadcoach.log import Log

        target = (
            Database.postgresql(settings["observability_dsn_file"])
            if settings.get("observability_dsn_file")
            else settings["observability_database"]
        )
        log = Log(target, chained=service == "voice-gateway")
    app.state.observation_owned_log = log
    return instrument(
        app,
        service,
        log,
        settings.get(
            "observability_machine",
            "front" if service == "voice-gateway" else "compute",
        ),
    )


async def shutdown(app) -> None:
    """Close optional writers even when an embedding host invokes router lifespan directly."""
    sink = getattr(app.state, "observations", None)
    if sink:
        await asyncio.to_thread(sink.close)
    owned = getattr(app.state, "observation_owned_log", None)
    if owned is not None:
        owned.db.close()
        app.state.observation_owned_log = None


class ContextFilter(logging.Filter):
    """Attach safe correlation attributes to Python records without capturing their message."""

    def filter(self, record):
        """Expose IDs for structured formatters; suppress ordinary logs for no-log work."""
        current = CURRENT.get()
        if current and current.root.quiet:
            return False
        for name, value in event_context().items():
            setattr(record, name, value)
        return True


class SafeFormatter(logging.Formatter):
    """A content-free journal envelope; arbitrary log messages/exceptions are never formatted."""

    def format(self, record):
        """Emit only approved IDs and event classes, never driver messages or credentials."""
        value = {
            "version": 1,
            "release": RELEASE,
            "at": record.created,
            "severity": record.levelname,
            "event": getattr(record, "event_name", "diagnostic"),
            "process": PROCESS,
        }
        for name in (
            "request_id",
            "trace_id",
            "span_id",
            "parent_id",
            "service",
            "error_code",
        ):
            field_value = getattr(record, name, "")
            if isinstance(field_value, str) and len(field_value) <= 128:
                value[name] = field_value
        return json.dumps(value, separators=(",", ":"))


class JournalHandler(logging.StreamHandler):
    """Use the current process stderr, including controlled test/server capture changes."""

    def emit(self, record):
        """Resolve the live stream under the handler lock instead of caching a closed capture."""
        self.stream = sys.stderr
        super().emit(record)


def diagnostic(event: str, error_code: str = "", service: str = "") -> None:
    """Write a sanitized structured diagnostic; this function never accepts message content."""
    logger = logging.getLogger("suite.observation")
    if not logger.handlers:
        handler = JournalHandler()
        handler.setFormatter(SafeFormatter())
        handler.addFilter(ContextFilter())
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
    logger.error(
        "",
        extra={
            "event_name": event[:64] if LABEL.fullmatch(event) else "diagnostic",
            "error_code": error_code[:64],
            "service": service[:64],
        },
    )
