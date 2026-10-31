"""Hosted models, answered from the front machine: the outside services, their keys, and what they may cost.

Compute is for inference, voice and telemetry. A model that lives at an outside service is asked
for from here, with a key that is a file on this machine and goes nowhere else. Every check that
decides whether a request may leave is made here, before anything is sent, against this machine's own log.
"""

import asyncio
import hashlib
import json
import re
import time
from functools import partial

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from loadcoach import observability as observation
from loadcoach.capture import answer_text, said_wrong
from loadcoach.diagnostics import Timeline
from loadcoach.groups import LEVELS
from loadcoach.log import public_number, token_counts
from loadcoach.outside import Services, check_service, cost_of
from loadcoach.reliability import answer_outcome
from loadcoach.users import User
from turnstile.accounts import address, error, lacks, read_json

TRACE = re.compile(r"[A-Za-z0-9._:-]{1,128}")  # what a caller may send as X-LoadCoach-Trace
KEEP_AT_MOST = 2_000_000  # bytes. A request larger than this is not kept in full: only its size is
HOP = {"connection", "content-length", "host", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


def guarded(request: Request) -> bool:
    """Whether the caller requires one exact local model: a hosted model is never that."""
    return any(name.startswith("x-loadcoach-require-") for name in request.headers)


def refuse(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    """An error in the shape every answer here has, with headers (Retry-After) if it says when to come back."""
    return JSONResponse({"error": {"message": message, "type": "loadcoach_error", "code": None}}, status_code=status, headers=headers)


class Hosted:
    """Front-side hosted inference with caller policy, cost and audit enforcement."""
    def __init__(self, services: Services, log, client: httpx.AsyncClient, reliability, users, local_names=None) -> None:
        """`await local_names(user)` are the names compute's models go by, so that a service cannot take one."""
        async def none(user):
            """Return no external roles when no role provider is configured."""
            return set()

        self.services, self.log, self.client, self.reliability, self.users, self.local_names = services, log, client, reliability, users, local_names or none
        self.away: list[dict] = []  # requests being answered by a service right now, for the status page

    # ---- what there is -------------------------------------------------------------------------

    def models(self) -> dict[str, dict]:
        """List hosted model choices available to the caller."""
        return self.services.models()

    def offered(self, user: User) -> list[str]:
        """The hosted models this user may name: their group may use hosted models, and this one."""
        return [name for name in self.models() if user.can("hosted") and user.group.may_use(name)] if user else []

    def identity(self, name: str) -> tuple[str, str]:
        """What reliability is kept under for a hosted model: where it is asked, not any file."""
        there = self.models()[name]
        mark = hashlib.sha256(json.dumps([self.services.get(there["service"])["url"], there["service"], there["theirs"]]).encode()).hexdigest()
        return "hosted:" + mark, mark

    def why_not(self, name: str, user: User | None = None) -> str | None:
        """Why this model cannot be asked just now, in words: its service has no key here, or has cost its ceiling. None if it can."""
        service = self.models()[name]["service"]
        if not self.services.key(service):
            return f"The service {service} has no key on the front machine. It belongs in the file {self.services.key_file(service)}."
        ceiling, cost = self.services.get(service).get("ceiling"), self.log.service_cost(service)
        if ceiling and cost >= ceiling:
            return f"The service {service} has cost {cost:.2f} in the last 24 hours. Its ceiling is {ceiling:.2f}."
        if user and user.group.max_hosted_spend and self.log.spent(user.name) >= user.group.max_hosted_spend:
            return f"Your hosted answers in the last 24 hours cost {self.log.spent(user.name):.2f}. Your group's limit is {user.group.max_hosted_spend:.2f}."
        return None

    def facts(self, user: User) -> dict:
        """What roles are picked with, for each hosted model this user may use, in the shape compute gives a local one's."""
        found = {}
        for name in self.offered(user):
            model_id, fingerprint = self.identity(name)
            found[name] = {"hosted": True, "why_not": self.why_not(name, user), "sees": True, "tools": True, "room": None, "warm": True, "resident": False,
                           "speed": None, "load": None, "placed": False, "context": None, "fit": "hosted", "cards": [], "evictions": [],
                           "model_id": model_id, "fingerprint": fingerprint, "prompt_speed": None,
                           "wait": {"seconds": 0.0, "basis": "no local queue; outside scheduling is unobserved"}}
        return found

    def spending(self) -> list[dict]:
        """Alert evidence: each service with a ceiling, and each account whose group has one, with what it has cost in 24 hours."""
        found = []
        for name in self.services.names():
            if ceiling := self.services.get(name).get("ceiling"):
                cost = self.log.service_cost(name)
                found.append({"key": "spend:service:" + name, "active": cost >= ceiling, "title": "Spending ceiling: " + name,
                              "explanation": f"Recorded last-24-hour cost ${cost:.6f}; ceiling ${ceiling:.6f}. In-flight and unknown costs are excluded.",
                              "recovery": "Recorded 24-hour spending is below the configured ceiling."})
        for name, identity, group in self.users.db.execute("SELECT name, identity, grp FROM users").fetchall():
            if (limit := getattr(self.users.groups.get(group), "max_hosted_spend", 0)):
                cost = self.log.spent(name)
                found.append({"key": "spend:user:" + (identity or name), "active": cost >= limit, "title": "Spending ceiling: " + name,
                              "explanation": f"Recorded last-24-hour cost ${cost:.6f}; per-user group ceiling ${limit:.6f}. In-flight and unknown costs are excluded.",
                              "recovery": "Recorded 24-hour spending is below the configured ceiling."})
        return found

    def status(self, user: User) -> dict:
        """The requests under way at services, as the status page shows them: all of them for someone who may see all, else their own and a count."""
        now = time.monotonic()
        shown = [{**{k: v for k, v in one.items() if k != "since"}, "seconds": round(now - one["since"], 1)} for one in self.away]
        if user.can("see-all"):
            return {"hosted": shown}
        mine = [one for one in shown if one["user"] == user.name]
        return {"hosted": mine, "others_hosted": len(shown) - len(mine)}

    # ---- answering -----------------------------------------------------------------------------

    @observation.traced("hosted_attempt")
    async def answer(self, request: Request, user: User, asked: dict, name: str, chose: dict | None = None, instead_of: str = "", role: str = "") -> Response:
        """Ask the hosted model `name` for what `asked` says, after every check, and hand back its answer as it arrives."""
        there, chose = self.models()[name], chose or {}
        if guarded(request):
            return error(409, "The required local model identity, fingerprint or build does not match; no inference was sent.")
        if "x-loadcoach-log" in request.headers:  # what a hosted answer cost is counted from the log, so it cannot go unrecorded
            return error(400, f"A request that leaves nothing behind stays on this machine, and {name} is a hosted model.")
        limit = user.group.max_requests
        if limit and sum(one["user"] == user.name for one in self.away) >= limit:
            return refuse(429, f"You already have {limit} requests in line or running.", {"retry-after": "5"})
        level, trace = request.headers.get("x-loadcoach-sensitivity", "normal"), request.headers.get("x-loadcoach-trace")
        if level not in LEVELS:
            return error(400, f"X-LoadCoach-Sensitivity must be one of {', '.join(LEVELS)}.")
        if trace is not None and not TRACE.fullmatch(trace):
            return error(400, "X-LoadCoach-Trace is up to 128 letters, digits, dots, dashes, underscores or colons.")
        if trace:
            level = max(level, self.log.trace_level(user.name, trace) or "normal", key=LEVELS.index)
        if LEVELS.index(level) > LEVELS.index(user.group.max_sensitivity):
            return error(403, f"Your group may not work at the {level} level.")
        if trace:
            self.log.set_trace_level(user.name, trace, level)
        if level != "normal":
            return error(403, f"A {level} request may not leave this machine, and {name} is a hosted model.")
        if (why := self.why_not(name, user)):
            ours = "Your hosted answers" in why or "ceiling" in why
            return refuse(429 if ours else 503, why, {"retry-after": "3600"} if ours else None)

        about = (user.name, address(request), name, trace, level)
        going = {"hosted": there["theirs"], "service": there["service"], **({"instead_of": instead_of} if instead_of else {}), **chose}
        entry = self.log.add("request", "asked", *about, path=request.url.path.removeprefix("/v1/"), group=user.group_name,
                             account_id=user.identity, key_id=user.key_identity, **going)
        number = public_number(None, entry) if entry else 0  # the number callers see: front's own range, never compute's
        keeping = bool(entry and self.log.keep_text)  # the latest requests are kept in full, for finding out what went wrong
        self.log.keep_asked(entry, json.dumps(asked) if len(json.dumps(asked)) <= KEEP_AT_MOST else json.dumps({"too_big": len(json.dumps(asked))}))
        model_id, fingerprint = self.identity(name)
        key = (model_id, fingerprint, role)
        epoch = self.reliability.state(key)["epoch"]
        here = {"id": number, "model": name, "user": user.name, "caller": address(request), "priority": "-", "since": time.monotonic(),
                **({"batch_id": trace} if trace and trace.startswith("batch_") else {})}
        self.away.append(here)
        finish = partial(self.away.remove, here)
        started = time.monotonic()
        timeline = Timeline(asked, None, True)
        url = self.services.get(there["service"])["url"].rstrip("/") + request.url.path.removeprefix("/v1")
        if request.url.query:
            url += "?" + request.url.query
        # The service gets its own key, its own name for the model, and nothing else of the caller's.
        headers = {"authorization": f"Bearer {self.services.key(there['service'])}", "content-type": "application/json", "accept-encoding": "identity"}
        body = json.dumps({**asked, "model": there["theirs"]}).encode()

        def ended(how: str, status: int, outcome: str, **detail) -> None:
            """Finalize hosted request usage, cost and permitted audit evidence."""
            self.log.add("request", how, *about, of=entry, status=status, group=user.group_name, account_id=user.identity, key_id=user.key_identity,
                         reliability_outcome=outcome, reliability_model_id=model_id, reliability_fingerprint=fingerprint, service=there["service"], **detail)
            try:
                self.reliability.record(number, user.name, name, key, epoch, outcome, False, owner_id=user.identity or user.name)
            except Exception as problem:  # the answer has gone to the caller already
                if not self.reliability.db.database.is_error(problem):
                    raise

        try:
            upstream = await self.client.send(self.client.build_request(request.method, url, headers=headers, content=body), stream=True)
        except httpx.PoolTimeout:
            finish()
            ended("refused", 503, "refused", why="turnstile is already carrying all it may")
            return refuse(503, "Turnstile is already carrying all the requests it may. Try again shortly.", {"retry-after": "5", "x-turnstile": "busy", "x-loadcoach-request-id": str(number)})
        except httpx.HTTPError as problem:
            finish()
            ended("failed", 502, "timeout" if isinstance(problem, httpx.TimeoutException) else "error", waited=0, why=f"{type(problem).__name__}: {problem}"[:500])
            return error(502, f"The hosted service did not answer: {type(problem).__name__}", {"x-loadcoach-request-id": str(number)})
        except asyncio.CancelledError:
            finish()
            ended("hung up", 499, "cancelled")
            raise

        whole, complete, broke = bytearray(), False, ""
        timeline.stage("response")
        kind = upstream.headers.get("content-type", "")

        async def stream():
            """Forward hosted output while ensuring request accounting and upstream cleanup."""
            nonlocal complete, broke
            try:
                async for piece in upstream.aiter_raw():
                    timeline.chunk(piece, kind, "content-encoding" not in upstream.headers)
                    whole.extend(piece)
                    yield piece
                complete = True
            except (httpx.HTTPError, httpx.StreamClosed) as problem:
                broke = "timeout" if isinstance(problem, httpx.TimeoutException) else "error"
                raise
            finally:
                try:
                    await upstream.aclose()
                finally:
                    finish()
                    readable = "content-encoding" not in upstream.headers
                    timeline.finish(bytes(whole), kind, readable)
                    inside_it = said_wrong(bytes(whole)) if complete and upstream.status_code < 400 and readable else None
                    if broke:
                        outcome = broke
                    elif not complete:
                        outcome = "cancelled"
                    elif inside_it or upstream.status_code >= 500:
                        outcome = "error"
                    elif upstream.status_code >= 400:
                        outcome = "refused"
                    else:
                        outcome = answer_outcome(bytes(whole), kind, request.url.path) if readable else "unscored"
                    kept, calls = answer_text(bytes(whole), kind) if keeping and readable else (None, [])
                    if keeping and kept is not None:
                        self.log.keep_said(entry, {**kept, "calls": calls} if calls else kept)
                    counts = token_counts(bytes(whole)[-4096:]) if readable else {}
                    cost = cost_of(counts, there) if "cost" not in counts else None
                    why = {"why": "the hosted service stopped answering part-way."} if broke else {"why": inside_it} if inside_it else {}
                    ended("failed" if broke or inside_it else "done" if complete else "hung up", upstream.status_code, outcome,
                          waited=0, ran=round(time.monotonic() - started, 1), priority=None, **why, **(counts if cost is None else {**counts, "cost": cost}),
                          diagnostics={**timeline.snapshot(), "role": chose.get("role", "")},
                          **({"prompt_tokens_per_second": rate} if (rate := timeline.native.get("prompt_tokens_per_second")) else {}))

        passed = {k: v for k, v in upstream.headers.items() if k not in HOP}
        passed.update({"x-loadcoach-request-id": str(number), "x-loadcoach-hosted": name, "x-loadcoach-service": there["service"],
                       **{f"x-loadcoach-{k}": v for k, v in chose.items()}})
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=passed)

    # ---- managing the services -----------------------------------------------------------------

    def shown(self, name: str) -> dict:
        """Return public hosted-service metadata without its private credential."""
        return {"name": name, **self.services.get(name), "key": bool(self.services.key(name)), "key_file": str(self.services.key_file(name)),
                "spent": round(self.log.service_cost(name), 6)}

    async def service_list(self, request: Request, user: User) -> Response:
        """List hosted services for a caller with management permission."""
        return lacks(user, "manage-hosted") or JSONResponse({"services": [self.shown(name) for name in self.services.names()]})

    async def service_put(self, request: Request, user: User) -> Response:
        """Validate and save hosted-service configuration and its private key reference."""
        if denied := lacks(user, "manage-hosted"):
            return denied
        name, existing = request.path_params["name"], self.services.get(request.path_params["name"])
        asked = await read_json(request, {"url", "ceiling", "models"})
        if isinstance(asked, Response):
            return asked
        if existing and "new" in request.query_params:
            return error(409, f"There is already a service named {name!r}.")
        wanted = {k: v for k, v in asked.items() if v is not None} | ({"key_env": existing["key_env"]} if existing and "key_env" in existing else {})
        elsewhere = {ours for ours, one in self.models().items() if one["service"] != name} | await self.local_names(user)
        try:
            check_service(name, wanted, elsewhere)
        except ValueError as problem:
            return error(400, str(problem))
        self.services.put(name, wanted)
        self.log.add("service", "changed" if existing else "added", user.name, address(request), name=name, **wanted)
        return JSONResponse(self.shown(name), status_code=200 if existing else 201)

    async def service_remove(self, request: Request, user: User) -> Response:
        """Remove an authorized hosted-service configuration."""
        if denied := lacks(user, "manage-hosted"):
            return denied
        name = request.path_params["name"]
        if not self.services.remove(name):
            return error(404, f"There is no service named {name!r}.")
        self.log.add("service", "removed", user.name, address(request), name=name)
        return JSONResponse({"removed": name})

    async def service_key(self, request: Request, user: User) -> Response:
        """Replace a private provider key without returning it or recording its value."""
        if denied := lacks(user, "manage-hosted"):
            return denied
        name = request.path_params["name"]
        if self.services.get(name) is None:
            return error(404, "There is no such cloud API.")
        asked = await read_json(request, {"key"})
        if isinstance(asked, Response):
            return asked
        try:
            self.services.set_key(name, asked.get("key"))
        except ValueError as problem:
            return error(400, str(problem))
        except OSError:
            return error(500, "The API key could not be saved on the front machine.")
        self.log.add("service", "key-changed", user.name, address(request), name=name)
        return JSONResponse({"name": name, "key": True}, headers={"cache-control": "no-store"})

    async def service_offers(self, request: Request, user: User) -> Response:
        """The models a service says it has, asked of the service itself with its key."""
        if denied := lacks(user, "manage-hosted"):
            return denied
        name = request.path_params["name"]
        if self.services.get(name) is None:
            return error(404, f"There is no service named {name!r}.")
        try:
            answer = await self.client.get(self.services.get(name)["url"].rstrip("/") + "/models", headers={"authorization": f"Bearer {self.services.key(name)}"}, timeout=20)
            ids = sorted(one["id"] for one in answer.json()["data"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return error(502, f"{name} did not give a list of its models. Its address may be wrong, or its key. Its models can still be typed by hand.")
        return JSONResponse({"service": name, "models": ids})
