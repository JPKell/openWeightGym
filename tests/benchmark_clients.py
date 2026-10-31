"""Exercise benchmark handlers and LoadCoach through ASGI adapters in one test event loop."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from benchmarks.app import Settings, build_app
from test_app import gateway as loadcoach_gateway


def gateway(fake_llama_server, **kwargs):
    """The benchmark handlers beside turnstile, with LoadCoach behind it, as weightroom has them."""
    through = loadcoach_gateway(fake_llama_server, **kwargs)
    front, door = through.front, through.door  # turnstile, and the way in at its port
    database = str(Path(kwargs.get('database', 'loadcoach.db')).with_suffix('.benchmarks.db'))
    bench = build_app(Settings(compute='http://compute', database=database),
        httpx.AsyncClient(base_url='http://compute', transport=httpx.ASGITransport(app=door),timeout=30))

    @asynccontextmanager
    async def lifespan(app):
        async with front.router.lifespan_context(front):  # turnstile's and LoadCoach's
            async with bench.router.lifespan_context(bench):
                yield

    app = Starlette(routes=[*bench.routes, Mount("/", app=door)], lifespan=lifespan)
    app.state.compute, app.state.benchmarks = through.compute, bench
    client = TestClient(app)
    client.users, client.log = through.users, through.log
    return client
