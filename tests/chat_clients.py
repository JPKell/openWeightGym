"""Exercise WeightRoom chat against a real HTTP compute server in a separate event loop."""

import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from weightroom.conversations import build_app
from loadcoach.runner import free_port
from test_app import gateway as compute_gateway


@contextmanager
def gateway(fake_llama_server, *args, **kwargs):
    """Host chats on front and every account/model operation on a real compute server."""
    compute = compute_gateway(fake_llama_server, *args, **kwargs).app
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(compute, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=10)
            raise AssertionError("Test compute server did not start")
        time.sleep(.01)
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30)
    source = kwargs.get("database", ":memory:")
    database = ":memory:" if source == ":memory:" else str(Path(source).with_suffix(".chat.db"))
    chat = build_app(database, client)

    async def proxy(request):
        """Expose compute APIs beside chat for existing end-to-end test setup and assertions."""
        response = await client.request(request.method, str(request.url.path) +
            ("?" + request.url.query if request.url.query else ""), content=await request.body(), headers={
            key: value for key, value in request.headers.items() if key not in ("host", "content-length")})
        if request.method == "DELETE" and request.url.path.startswith("/loadcoach/users/") and response.is_success:
            await chat.state.forget(response.json()["identity"])
        return Response(response.content, status_code=response.status_code, headers={key: value for key, value
            in response.headers.items() if key not in ("content-length", "transfer-encoding", "connection")})

    @asynccontextmanager
    async def lifespan(app):
        async with chat.router.lifespan_context(chat):
            yield

    app = Starlette(routes=[*chat.routes, Route("/{path:path}", proxy,
        methods=["GET", "POST", "DELETE", "PUT", "PATCH"])], lifespan=lifespan)
    app.state.chat = chat
    try:
        with TestClient(app) as caller:
            yield caller
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        if thread.is_alive():
            raise AssertionError("Test compute server did not shut down")
