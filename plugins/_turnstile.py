"""Generic adapter routing at turnstile's model door, independent of compute availability."""

from __future__ import annotations

import asyncio
from loadcoach import observability as observation
import json
import re
from collections import defaultdict

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from plugins._controller import Controller
from plugins._registry import LEVELS, Registry
from plugins._runtime import failure
from plugins._sdk import AdapterError

HOP = {"connection", "content-length", "host", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


class Router:
    """Route manifest-backed models, enforce mirrored permissions, and record only adaptor use."""

    def __init__(self, registry: Registry, adapters: httpx.AsyncClient, identify, ask) -> None:
        """`identify(authorization)` names the caller from the control node's accounts, or raises AdapterError.
        `ask(authorization, method, path)` asks compute as that caller without waking it, and gives None if it is not answering."""
        self.registry, self.adapters, self.identify, self.ask = registry, adapters, identify, ask
        self.controller = Controller(registry, adapters)
        self.active = defaultdict(int)

    async def catalog(self, request: Request, user: dict) -> Response:
        """Merge live/cached native entries with permitted adaptor entries, without waking compute."""
        native = None
        unavailable = []
        available = None
        try:
            response = await self.ask(request.headers.get("authorization", ""), "GET", "/v1/models")
            if response is not None and response.status_code in (401, 403):
                return JSONResponse(response.json(), status_code=response.status_code)
            if response is not None and response.is_success:
                catalogue = response.json()
                native = catalogue.get("data")
                unavailable = catalogue.get("unavailable_machines", [])
                available = catalogue.get("available_machines")
        except (ValueError, AttributeError):
            pass
        if native is None:
            available = []
            unavailable = unavailable or ['compute']
            native = []
            for model in self.registry.auth.snapshot().get("models", []):
                permitted = model["id"] in user.get("roles", []) if model.get("owned_by") == "role" else model["id"] in user.get("models", [])
                if permitted:
                    native.append({**model, "available": False})
        return JSONResponse({"object": "list", "data": [*native, *self.registry.models(user)],
                             "unavailable_machines": unavailable,
                             **({"available_machines": available} if available is not None else {})})

    @observation.traced("adapter_dispatch")
    async def chat(self, request: Request, body: dict, user: dict, found: tuple) -> Response:
        """Dispatch only to the selected instance with its key and a trusted owner identity."""
        plugin, instance, profile, config = found
        alias = body["model"]
        if not self.registry.auth.permits(user, alias):
            raise AdapterError("The model does not exist.", 404, "model_not_found")
        level = request.headers.get("x-loadcoach-sensitivity", "normal")
        if level not in LEVELS or LEVELS.index(level) > min(LEVELS.index(user.get("max_sensitivity", "normal")), LEVELS.index(config.get("max_sensitivity", "normal"))):
            raise AdapterError("This account/instance does not permit that sensitivity.", 403, "sensitivity_refused")
        quiet = request.headers.get("x-loadcoach-log")
        if quiet is not None and (quiet != "off" or "unlogged" not in user.get("can", []) or not self.registry.auth.snapshot().get("unlogged")):
            raise AdapterError("Unlogged adaptor use is not permitted.", 403, "unlogged_refused")
        if quiet:
            observation.silence()
        limit = user.get("max_requests", 0)
        if limit and self.active[user["identity"]] >= limit:
            raise AdapterError("Your adaptor request limit is in use.", 429, "request_limit")
        self.active[user["identity"]] += 1
        session = opaque_request = None
        release = True
        try:
            if config["revision"] != config.get("applied_revision"):
                await self.controller.sync(plugin, instance)
                config = self.registry.instance(plugin, instance)
            forwarded = {"authorization": "Bearer " + self.registry.pairing_key(plugin, instance),
                         "content-type": "application/json", "X-Suite-User-ID": user["identity"],
                         "X-Suite-Instance": instance, "X-Suite-Model": alias}
            for name in ("x-suite-session-mode", "x-suite-session-id", "idempotency-key"):
                if name in request.headers:
                    forwarded[name] = request.headers[name]
            outgoing = self.adapters.build_request("POST", config["url"] + "/v1/chat/completions",
                headers=forwarded, json={**body, "model": profile})
            from loadcoach.client_origin import forward
            await forward(outgoing, self.registry.pairing_key(plugin, instance))
            upstream = await self.adapters.send(outgoing, stream=True)
            session = upstream.headers.get("x-suite-session-id")
            opaque_request = upstream.headers.get("x-suite-request-id")
            if not quiet:
                self.registry.record(user, alias, plugin, instance, profile, "started" if upstream.is_success else "refused", session, opaque_request)
            passed = {name: value for name, value in upstream.headers.items() if name not in HOP}
            passed.update({"x-suite-adaptor": plugin, "x-suite-instance": instance, "cache-control": "no-store"})

            async def answer():
                """Stream adapter events while releasing the upstream request on exit."""
                outcome, tail = "interrupted", bytearray()
                try:
                    async for piece in upstream.aiter_raw():
                        tail.extend(piece)
                        if len(tail) > 8192:
                            del tail[:-8192]
                        yield piece
                    failed = not upstream.is_success
                    for line in tail.decode(errors="replace").splitlines():
                        if line.startswith("data:"):
                            try:
                                failed |= bool(json.loads(line[5:]).get("error"))
                            except (ValueError, AttributeError):
                                pass
                    outcome = "failed" if failed else "completed"
                except httpx.HTTPError:
                    outcome = "failed"
                    if body.get("stream"):
                        yield "data: " + json.dumps(failure(AdapterError("Adapter connection ended unexpectedly.", 502, "adapter_disconnected"))) + "\n\ndata: [DONE]\n\n"
                finally:
                    await upstream.aclose()
                    self.active[user["identity"]] -= 1
                    if not quiet:
                        self.registry.record(user, alias, plugin, instance, profile, outcome, session, opaque_request)

            release = False
            return StreamingResponse(answer(), status_code=upstream.status_code, headers=passed)
        except httpx.HTTPError:
            if not quiet:
                self.registry.record(user, alias, plugin, instance, profile, "unavailable", session, opaque_request)
            raise AdapterError("The adaptor is not answering.", 503, "adapter_unavailable") from None
        finally:
            if release:
                self.active[user["identity"]] -= 1

    async def intercept(self, request: Request) -> Response | None:
        """Handle known plugin names and merged catalogs; preserve all other turnstile behavior."""
        path = request.url.path
        candidate = None
        if request.method == "POST" and path == "/v1/chat/completions":
            raw = await request.body()
            try:
                candidate = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(candidate, dict) or not self.registry.owns_prefix(candidate.get("model")):
                return None
        elif request.method == "GET" and path.startswith("/v1/models/"):
            if not self.registry.owns_prefix(path.removeprefix("/v1/models/")):
                return None
        elif request.method == "GET" and path == "/v1/models":
            if not any(row["valid"] and row["enabled"] for row in self.registry.scan()):
                return None
        elif path not in ("/turnstile/plugins/status", "/turnstile/plugins/activity") and not path.startswith("/turnstile/plugins/runs/"):
            return None
        try:
            user = self.identify(request.headers.get("authorization", ""))
            if path == "/turnstile/plugins/status":
                return JSONResponse({**self.registry.auth.status(), "models": self.registry.models(user)})
            if path == "/turnstile/plugins/activity":
                return JSONResponse({"uses": self.registry.activity(user)})
            if path.startswith("/turnstile/plugins/runs/"):
                pieces = path.removeprefix("/turnstile/plugins/runs/").split("/")
                if request.method != "GET" or len(pieces) != 3 or not re.fullmatch(r"pr_[A-Za-z0-9_-]{20,80}", pieces[2]):
                    raise AdapterError("Invalid request receipt path.", 400, "invalid_request")
                plugin, instance, rid = pieces
                config = self.registry.instance(plugin, instance)
                offered = {row["adaptor"]["profile"] for row in self.registry.models(user)
                           if row["adaptor"]["plugin"] == plugin and row["adaptor"]["instance"] == instance}
                if not offered:
                    raise AdapterError("You have no such request.", 404, "request_not_found")
                response = await self.adapters.get(config["url"] + "/adapter/v1/runs/" + rid, headers={
                    **self.controller.headers(plugin, instance), "X-Suite-User-ID": user["identity"]}, timeout=5)
                if not response.is_success:
                    return JSONResponse(failure(AdapterError("You have no such request.", response.status_code, "request_not_found")), status_code=response.status_code)
                receipt = response.json()
                if receipt.get("profile") not in offered:
                    raise AdapterError("You have no such request.", 404, "request_not_found")
                return JSONResponse(receipt, headers={"cache-control": "no-store"})
            if path == "/v1/models":
                return await self.catalog(request, user)
            if candidate is None:
                alias = path.removeprefix("/v1/models/")
                entry = next((row for row in self.registry.models(user) if row["id"] == alias), None)
                if not entry:
                    raise AdapterError("The model does not exist.", 404, "model_not_found")
                return JSONResponse(entry)
            if len(raw) > 24 * 1024 * 1024:
                raise AdapterError("Request exceeds 24 MiB.", 413, "request_too_large")
            found = self.registry.resolve(candidate["model"])
            if not found:
                raise AdapterError("The model does not exist.", 404, "model_not_found")
            return await self.chat(request, candidate, user, found)
        except AdapterError as problem:
            return JSONResponse(failure(problem), status_code=problem.status, headers={"cache-control": "no-store"})
        except (httpx.HTTPError, ValueError):
            return JSONResponse(failure(AdapterError("Adapter is unavailable.", 503, "adapter_unavailable")), status_code=503)

    async def mirror(self, key: str, interval: float = 15) -> None:
        """Keep compute's model list, for when it is not answering; never use a wake-capable endpoint."""
        while True:
            try:
                response = await self.ask("Bearer " + key, "GET", "/loadcoach/plugins/mirror")
                if response is not None and response.is_success:
                    self.registry.auth.replace(response.json())
            except ValueError:
                pass
            await asyncio.sleep(interval)
