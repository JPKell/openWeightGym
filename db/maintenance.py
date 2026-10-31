"""A persistent maintenance barrier shared by services in one installation."""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path


HEADER = "x-suite-maintenance"


class Gate:
    """Persistent maintenance marker and in-process active-work counter."""
    def __init__(self, config_path=""):
        """Locate the persistent maintenance marker and initialize active-work accounting."""
        self.path = Path(config_path).parent / ".suite-maintenance.json" if config_path else None
        self.active = 0

    def token(self):
        """Read the maintenance token, failing closed on an invalid marker."""
        if self.path is None or not self.path.exists():
            return ""
        try:
            token = json.loads(self.path.read_text())["token"]
            return token if isinstance(token, str) and token else "invalid-maintenance-marker"
        except (OSError, ValueError, KeyError):
            return "invalid-maintenance-marker"  # fail closed on corrupt/unreadable markers

    async def wait(self):
        """Wait until the installation's maintenance marker is removed."""
        while self.token():
            await asyncio.sleep(.25)

    async def outgoing(self, request):
        # Only call on private openWeightGym clients, never hosted services or arbitrary adapters.
        """Attach the active maintenance token only to a private openWeightGym request."""
        if token := self.token():
            request.headers[HEADER] = token


class Barrier:
    """ASGI maintenance admission barrier for HTTP and WebSocket work."""
    def __init__(self, app, gate):
        """Bind the downstream ASGI app to its installation maintenance gate."""
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        """Reject work outside the maintenance token and count admitted active requests."""
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        token = self.gate.token()
        headers = dict(scope.get("headers", []))
        accepted = token and hmac.compare_digest(headers.get(HEADER.encode(), b""), token.encode())
        if token and (not accepted or token == "invalid-maintenance-marker"):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1013})
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"30")]})
            return await send({"type": "http.response.body", "body": b'{"error":{"message":"openWeightGym database maintenance is in progress."}}'})
        if scope["type"] == "http" and scope["path"] == "/__suite_maintenance" and accepted:
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": json.dumps({"active": self.gate.active}).encode()})
        self.gate.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.active -= 1
