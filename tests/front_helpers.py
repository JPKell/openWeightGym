"""Turnstile in front of a program on compute, in one process, as the real path is.

A test asks turnstile with a key. Turnstile checks it against its accounts and sends the request
on with a signed statement; the program on compute checks the statement and answers with its
proof. Both lifespans run, so the program starts and stops as it would.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from db import is_memory
from loadcoach.users import Users
from turnstile.app import Settings, build_app

KEY = "the machine key the tests share"


def serve_browser_request(client, origin, route):
    """Bridge finite browser requests; use one real strip snapshot for unrelated SSE.

    TestClient buffers responses until they end, so forwarding the perpetual telemetry
    stream would deadlock a synchronous Playwright route handler.
    """
    request=route.request
    path=request.url.removeprefix(origin)
    headers={'HX-Request':request.headers.get('hx-request','false')}
    if path.split('?',1)[0]=='/strip/stream':
        response=client.get('/strip',headers=headers)
        route.fulfill(status=response.status_code,content_type='text/event-stream',
                      body='data: '+json.dumps(response.text)+'\n\n')
        return
    response=client.request(request.method,path,content=request.post_data_buffer,headers=headers)
    route.fulfill(status=response.status_code,
                  content_type=response.headers.get('content-type','text/html'),body=response.content)

# Compute asks control node for the accounts at every turn in tests, not once in ten seconds, so that a
# check made just after a change sees it. The ten seconds are a limit on the real machines only.
import loadcoach.front  # noqa: E402
import turnstile.roles  # noqa: E402

loadcoach.front.FRESH_FOR = 0
turnstile.roles.FACTS_FOR = 0  # what compute says of its models, asked afresh for every request, as the checks expect


def machine_key(folder: Path) -> str:
    """Write the machine key into `folder` and give its path."""
    path = folder / "machine.key"
    path.write_text(KEY)
    return str(path)


class Streamed(httpx.AsyncBaseTransport):
    """Like httpx.ASGITransport, but the answer is handed on as the app sends it, as over a real connection.

    httpx's own reads the whole answer first, which would hide a stream turnstile passes on as it comes.
    Closing the answer early tells the app the caller has hung up.
    """

    def __init__(self, app, client=("127.0.0.1", 123)) -> None:
        self.app, self.client = app, client

    async def handle_async_request(self, request):
        body, url = await request.aread(), request.url
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": request.method, "scheme": url.scheme,
                 "headers": [(name.lower(), value) for name, value in request.headers.raw], "path": url.path,
                 "raw_path": url.raw_path.split(b"?")[0], "query_string": url.query, "root_path": "",
                 "server": (url.host, url.port), "client": self.client}
        started, pieces, gone, given, head = asyncio.Event(), asyncio.Queue(), asyncio.Event(), [], {}

        async def receive():
            if not given:
                given.append(True)
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                head.update(message)
                started.set()
            elif message["type"] == "http.response.body":
                await pieces.put(message.get("body", b""))
                if not message.get("more_body"):
                    await pieces.put(None)

        running = asyncio.create_task(self.app(scope, receive, send))
        running.add_done_callback(lambda _: (started.set(), pieces.put_nowait(None)))
        await started.wait()
        if not head:
            raise running.exception() or RuntimeError("the app sent no answer")

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                while (piece := await pieces.get()) is not None:
                    yield piece

            async def aclose(self):
                gone.set()

        return httpx.Response(head["status"], headers=head.get("headers", []), stream=Body())


def own_copy(users: Users) -> Users:
    """The same accounts on a connection of turnstile's own, as it has in its own process. A test's thread and the app's must not share one."""
    source = users.db.database
    return users if is_memory(source) else Users(source, {})


class Later:
    """An app given its app afterwards: compute and turnstile each need a way to the other."""

    app = None

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


def front_of(build_compute, users: Users, folder: Path, port: int = 8442, address: str = "testclient", **settings) -> TestClient:
    """A client for turnstile in front of the app `build_compute(front)` makes. `front` is how that app reaches turnstile.

    `address` is where the test's requests seem to come from: "127.0.0.1" for turnstile's own machine.
    """
    turnstile = Later()
    compute = build_compute(httpx.AsyncClient(transport=httpx.ASGITransport(app=turnstile)))
    front = build_app(Settings(compute="compute", data=str(folder), machine_key_file=machine_key(folder), ports=(port,), **settings),
                      client=httpx.AsyncClient(transport=Streamed(compute)), users=own_copy(users))
    turnstile.app = front
    own = front.router.lifespan_context

    @asynccontextmanager
    async def both(app):
        async with compute.router.lifespan_context(compute), own(app):
            yield

    front.router.lifespan_context = both

    async def on_port(scope, receive, send):
        """Every request comes in on `port`, whatever address a test asked at, as a harness pointed at turnstile's port does."""
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "server": ((scope.get("server") or ("front", None))[0], port)}
        await front(scope, receive, send)

    client = TestClient(on_port, base_url=f"http://front:{port}", client=(address, 50000))
    client.users, client.compute, client.front, client.door, client.log = users, compute, front, on_port, front.state.log  # the log of what was done to the accounts is the control node's
    return client


class OnCompute:
    """Requests straight to the program on compute, not through turnstile: as the `loadcoach` command on that machine makes them.

    `client` is a front_of client inside its `with`, so that these run beside its requests, in the same loop.
    """

    def __init__(self, client: TestClient) -> None:
        self.client, self.headers = client, {}

    def request(self, method: str, path: str, **more) -> httpx.Response:
        async def asked():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.compute), base_url="http://compute") as straight:
                return await straight.request(method, path, headers={**self.headers, **more.pop("headers", {})}, **more)

        return self.client.portal.call(asked)

    def get(self, path: str, **more) -> httpx.Response:
        return self.request("GET", path, **more)

    def post(self, path: str, **more) -> httpx.Response:
        return self.request("POST", path, **more)


class Ports(httpx.AsyncBaseTransport):
    """The wires from turnstile to compute, one for each port, as LoadCoach, the ToolLocker and spotter each listen on their own."""

    def __init__(self, wires: dict) -> None:
        self.wires = wires

    async def handle_async_request(self, request):
        return await self.wires[request.url.port].handle_async_request(request)


def turnstile_to(folder: Path, users: Users, wires: dict, **settings):
    """Turnstile on the control node, with `users` as its accounts and `wires` to compute's ports. Its lifespan is not run.

    It shares weightroom's data folder, `folder`/data, where the accounts are kept.
    """
    (folder / "data").mkdir(exist_ok=True)
    return build_app(Settings(compute="compute", data=str(folder / "data"), machine_key_file=machine_key(folder), ports=(8442, 8443, 8444), **settings),
                     client=httpx.AsyncClient(transport=Ports(wires)), users=own_copy(users))


def door(turnstile, port: int) -> httpx.AsyncClient:
    """How weightroom reaches turnstile on its own machine, at one port."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=turnstile, client=("127.0.0.1", 1)), base_url=f"http://front:{port}")


def loadcoach_through(folder: Path, users: Users, web=None, **config) -> TestClient:
    """LoadCoach behind turnstile, with `users` as the accounts, as on the two machines. `config` is LoadCoach's."""
    from loadcoach.app import build_app as build_loadcoach
    from loadcoach.config import Config

    outside = {'hosted_keys': config.pop('keys')} if 'keys' in config else {}
    settings = Config(**{"nvidia_smi": "no-such-program", "machine_key_file": str(folder / "machine.key"), "front": "http://front:8442", **config})
    return front_of(lambda front: build_loadcoach(settings, web, front), users, folder, **outside)


class Signed(httpx.Auth):
    """Signs each request with a statement of `caller`, as turnstile does, for a test that asks a program on compute itself."""

    requires_request_body = True

    def __init__(self, caller: dict) -> None:
        self.caller = caller

    def auth_flow(self, request):
        from loadcoach import statement

        where = statement.address(request.url.raw_path.split(b"?")[0].decode(), request.url.query)
        request.headers[statement.HEADER] = statement.make(KEY, request.method, where, request.content, self.caller)[0]
        yield request


def caller(name: str, can=(), models=None) -> dict:
    """A caller as a statement carries them: `can` their abilities, `models` the ones they may use, or every one."""
    from dataclasses import replace

    from loadcoach import statement
    from loadcoach.groups import Group
    from loadcoach.users import User

    group = replace(Group(), can=frozenset(can), models=None if models is None else tuple(models))
    return statement.caller_of(User(name, "group", group, identity=name + "-id"))
