"""turnstile's checks. Compute is stood in for by a small app behind a switch, which plays compute being asleep."""

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import KEY, machine_key
from loadcoach import statement
from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.app import Settings, build_app, compute_client, listening, load_settings, wake

MAC = "bc:fc:e7:3d:90:6c"


class Compute(httpx.AsyncBaseTransport):
    """The wire to a made-up compute that believes only turnstile's statements. While `asleep` is above nought nothing answers."""

    def __init__(self, key_file: str) -> None:
        async def answer(request: Request):
            said = getattr(request.state, "said", None)
            body = (await request.body()).decode()
            if request.url.path == "/":
                return JSONResponse({"page": "for anyone"})
            if request.url.path == "/forged":
                return JSONResponse({"from": "somewhere else"}, headers={statement.PROOF: "0" * 64})
            if not said:
                return JSONResponse({"error": {"message": "no statement"}}, status_code=401)
            if "stream" in body:
                return StreamingResponse(iter(["data: one\n\n", "data: two\n\n"]), media_type="text/event-stream", headers={"x-loadcoach-hosted": "no"})
            return JSONResponse({"port": request.url.port, "path": request.url.path, "query": request.url.query, "method": request.method, "body": body,
                                 "trace": request.headers.get("x-loadcoach-trace"), "key": request.headers.get("authorization"),
                                 "caller": said["caller"]["name"], "from": request.client.host})

        app = statement.Statements(Starlette(routes=[Route("/{path:path}", answer, methods=["GET", "POST", "DELETE"])]), key_file)
        self.wire, self.asleep, self.tries = httpx.ASGITransport(app=app), 0, 0

    async def handle_async_request(self, request):
        self.tries += 1
        if self.asleep > 0:
            self.asleep -= 1
            raise httpx.ConnectError("compute is asleep")
        return await self.wire.handle_async_request(request)


@pytest.fixture
def front(tmp_path, monkeypatch):
    """turnstile, with one account, in front of a made-up compute. Yields a caller for each port, compute, the packets sent and the good key's headers."""
    monkeypatch.setattr("turnstile.app.TRY_EVERY", 0.01)
    users = Users(str(tmp_path / "accounts.db"), {"plain": Group(can=frozenset({"load"}))})
    users.add("pat", "plain")
    good = {"authorization": "Bearer " + users.new_key("pat")}
    compute, woken = Compute(machine_key(tmp_path)), []
    settings = Settings(compute="compute", data=str(tmp_path), wake_mac=MAC, wake_address="10.0.0.255", wait=0.5, machine_key_file=str(tmp_path / "machine.key"))
    app = build_app(settings, httpx.AsyncClient(transport=compute), lambda *sent: woken.append(sent), users=users)
    # These ports belong to one process: start its lifespan once, not once per client.
    with TestClient(app, base_url="http://front:8442") as coach:
        door = TestClient(app, base_url="http://front:8443")
        other = TestClient(app, base_url="http://front:80")
        try:
            yield coach, door, other, compute, woken, good
        finally:
            door.close()
            other.close()


def test_a_request_goes_on_with_a_statement_in_place_of_its_key(front):
    coach, door, other, compute, woken, good = front
    passed = coach.post("/v1/chat/completions?x=1", content='{"model": "small"}', headers={**good, "x-loadcoach-trace": "job-7"}).json()
    assert passed == {"port": 8442, "path": "/v1/chat/completions", "query": "x=1", "method": "POST", "body": '{"model": "small"}', "trace": "job-7",
                      "key": None, "caller": "pat", "from": "testclient"}  # the key stays here; compute is told who, and from where
    key = good["authorization"].removeprefix("Bearer ")
    through_address = door.post(f"/mcp/files?key={key}&set=a", content="{}").json()
    assert (through_address["port"], through_address["query"], through_address["key"], through_address["caller"]) == (8443, "set=a", None, "pat")  # nor a key in the address
    assert coach.delete("/loadcoach/models/small", headers=good).json()["method"] == "DELETE"
    assert other.get("/v1/models", headers=good).status_code == 404  # a port turnstile was not told to pass on

    # A wrong key is turned away here, and nothing is sent on.
    tries = compute.tries
    refused = coach.get("/v1/models", headers={"authorization": "Bearer lc_wrong"})
    assert refused.status_code == 401 and refused.json()["error"]["message"] == "Missing or wrong API key." and compute.tries == tries
    assert coach.get("/").json() == {"page": "for anyone"}  # no key at all goes on unsigned, and compute says what it allows
    assert coach.get("/v1/models").status_code == 401

    # Compute's answer comes back as it was, a stream as a stream, without the proof that is for turnstile alone.
    stream = coach.post("/v1/chat/completions", content='{"stream": true}', headers=good)
    assert stream.text == "data: one\n\ndata: two\n\n" and stream.headers["x-loadcoach-hosted"] == "no" and statement.PROOF not in stream.headers
    forged = coach.get("/forged", headers=good)
    assert forged.status_code == 502 and "did not prove" in forged.json()["error"]["message"]  # an answer not signed with the machine key is not passed back
    assert woken == []  # compute was there all along


def test_front_becomes_ready_while_optional_observation_sweep_is_running(tmp_path,monkeypatch):
    import threading
    entered,release=threading.Event(),threading.Event()
    threads=[]
    def sweep(connection,*args,**kwargs):
        threads.append(threading.current_thread().name)
        entered.set();release.wait(3)
    monkeypatch.setattr('loadcoach.observation_store.prune',sweep)
    users=Users(str(tmp_path/'accounts.db'),{'plain':Group(can=frozenset({'load'}))})
    users.add('pat','plain')
    key=users.new_key('pat')
    app=build_app(Settings(compute='compute',data=str(tmp_path),machine_key_file=machine_key(tmp_path)),
                  httpx.AsyncClient(transport=Compute(str(tmp_path/'machine.key'))),users=users)
    timer=threading.Timer(3,release.set);timer.start()
    try:
        with TestClient(app,base_url='http://front:8442') as caller:
            assert caller.get('/loadcoach/me',headers={'authorization':'Bearer '+key}).status_code==200
            assert entered.wait(1) and not release.is_set()
    finally:release.set();timer.cancel()


def test_any_valid_key_wakes_compute_and_no_other(front):
    coach, door, _, compute, woken, good = front
    compute.asleep = 1
    assert coach.get("/v1/models").status_code == 503  # no key: told it is off, and not woken
    assert coach.get("/v1/models", headers={"authorization": "Bearer lc_wrong"}).status_code == 401
    assert woken == []

    compute.asleep = 3  # the first try fails, then two more while it wakes
    answered = door.post("/mcp", content="{}", headers=good)
    assert answered.status_code == 200 and answered.json()["port"] == 8443  # the caller waited, and got its answer
    assert woken == [(MAC, "10.0.0.255")] * 3  # a packet before each further try, since one can be lost

    woken[:] = []
    compute.asleep = 1
    told = coach.get("/v1/models", headers={**good, "x-turnstile-no-wake": "1"})  # as weightroom's pages ask
    assert told.status_code == 503 and told.headers["x-turnstile"] == "compute-off" and woken == []


def test_named_loadouts_never_implicitly_wake_compute(front):
    coach, _, _, compute, woken, good = front
    for action in ('preview', 'request'):
        compute.asleep = 1
        result = coach.post('/loadcoach/loadouts', headers=good, json={'action': action, 'name': 'Work'})
        assert result.status_code == 503 and not woken
    compute.asleep = 1
    assert coach.get('/loadcoach/loadouts/', headers=good).status_code == 503 and not woken

    compute.asleep = 10_000  # it never wakes
    gave_up = coach.get("/v1/models", headers=good)
    assert gave_up.status_code == 504 and "has not answered in 0.5 seconds" in gave_up.json()["error"]["message"] and len(woken) > 3


@pytest.mark.parametrize('path, expected, attempts', [('/loadcoach/alert-evidence', 503, 1),
    ('/spotter/alert-evidence', 503, 1), ('/loadcoach/observation-metrics', 200, 0),
    ('/spotter/alert-evidence/', 503, 1), ('/loadcoach/plugins/mirror', 503, 1)])
def test_monitoring_never_wakes_compute_even_without_client_header(front, path, expected, attempts):
    coach, _, _, compute, woken, good = front
    coach.app.state.users.groups.put('plain', {'can': ['load', 'manage-settings', 'mirror-users']})
    compute.asleep = 10_000
    before = compute.tries
    response = coach.get(path, headers=good)
    assert response.status_code == expected
    if expected == 503:
        assert response.headers['x-turnstile'] == 'compute-off'
    assert compute.tries == before + attempts and not woken


def test_compute_may_ask_for_the_accounts_and_say_it_is_going_to_sleep_with_the_machine_key(front):
    coach, _, _, _, _, good = front

    def signed(path, body=b"", key=KEY):
        return {statement.HEADER: statement.make(key, "POST" if body else "GET", path, body, machine="compute")[0], "content-type": "application/json"}

    assert coach.get("/turnstile/accounts", headers=good).status_code == 403  # not for a person's key
    assert coach.get("/turnstile/accounts", headers=signed("/turnstile/accounts", key="another key")).status_code == 403
    listed = coach.get("/turnstile/accounts", headers=signed("/turnstile/accounts"))
    assert listed.status_code == 200 and [one["name"] for one in listed.json()["accounts"]] == ["pat"] and statement.PROOF in listed.headers
    assert good["authorization"].removeprefix("Bearer ") not in listed.text and "hash" not in listed.text  # no key, and no hash of one, leaves
    body = json.dumps({"action": "sleep"}).encode()
    assert coach.post("/turnstile/power", content=body, headers={"content-type": "application/json"}).status_code == 403
    assert coach.post("/turnstile/power", content=body, headers=signed("/turnstile/power", body)).status_code == 200


def test_the_waking_packet_and_the_settings(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as ear:
        ear.bind(("127.0.0.1", 0))
        ear.settimeout(2)
        wake(MAC, "127.0.0.1", ear.getsockname()[1])
        packet = ear.recv(200)
    assert packet == b"\xff" * 6 + bytes.fromhex("bcfce73d906c") * 16 and len(packet) == 102

    # Made as a TCP socket by name: without that, every short answer is held back for 40 ms.
    with listening("127.0.0.1", 0) as made:
        assert made.proto == socket.IPPROTO_TCP and made.getsockname()[1] > 0

    file = tmp_path / "weightroom.toml"
    file.write_text(f'compute = "http://10.77.10.84:8442"\nkey_file = "weightroom.key"\ndata = "data"\nwake_mac = "{MAC}"\n[turnstile]\nhost = "0.0.0.0"\nports = [8442]\n')
    assert load_settings(file) == Settings(compute="10.77.10.84", data=str(tmp_path / "data"), host="0.0.0.0", ports=(8442,), wake_mac=MAC,
                                          plugins_dir=str((tmp_path / '../plugins').resolve()), auth_key_file=str(tmp_path / 'weightroom.key'), path=str(file),
                                          machine_key_file=str((tmp_path / "../keys/machine.key").resolve()))
    file.write_text('compute = "http://10.77.10.84:8442"\n[groups.reader]\ncan = ["load"]\n')  # the first groups are read here too
    assert load_settings(file).ports == (8442, 8443, 8444) and load_settings(file).wake_mac == "" and list(load_settings(file).groups) == ["reader"]
    assert load_settings(file).upstream(8443) == "http://10.77.10.84:8443"  # compute on its own machine: the same ports
    # One machine for both: compute's URL names LoadCoach's own port, and every port moves with it.
    file.write_text('compute = "http://127.0.0.1:9442"\n')
    assert load_settings(file).compute_offset == 1000 and load_settings(file).upstream(8444) == "http://127.0.0.1:9444"
    file.write_text('compute = "http://compute.lan"\n')  # no port: as it always was
    assert load_settings(file).upstream(8442) == "http://compute.lan:8442"
    for wrong in ('key_file = "x"', 'compute = "http://c:1"\n[turnstile]\ncolour = 1', 'compute = "http://c:1"\n[groups.g]\ncan = ["fly"]'):
        file.write_text(wrong)
        with pytest.raises(SystemExit):
            load_settings(file)
    assert Path(__file__).parent.parent.joinpath("services/turnstile.service").exists()


def test_voice_readiness_is_observation_and_sleep_needs_a_signed_notice(tmp_path):
    state, wakes, models = {"up": False, "voice": True}, [], []
    key_file = machine_key(tmp_path)

    async def compute(request):
        if not state["up"]:
            raise httpx.ConnectError("off")
        said = statement.check(KEY, request.headers[statement.HEADER], request.method, statement.address(request.url.path, request.url.query), b"", statement.Nonces())
        models.append(request.url.params.get("agent_model"))
        return httpx.Response(200, json={"profiles": {"default": {"can_start": state["voice"], "modes": ["direct"]}}},
                              headers={statement.PROOF: statement.proof(KEY, said["nonce"])})

    users = Users(str(tmp_path / "accounts.db"), {"plain": Group()})
    users.add("pat", "plain")
    good = {"authorization": "Bearer " + users.new_key("pat")}
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=key_file, ports=(8442, 8445))
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(compute)), lambda *args: wakes.append(args), users=users)
    with TestClient(app, base_url="http://front:8442") as client:
        def notice(action):
            body = json.dumps({"action": action}).encode()
            return client.post("/turnstile/power", content=body, headers={statement.HEADER: statement.make(KEY, "POST", "/turnstile/power", body, machine="compute")[0]})

        assert client.get("/turnstile/ready").status_code == 401
        assert client.get("/turnstile/ready", headers=good).json()["state"] == "not_known"
        assert client.post("/turnstile/power", json={"action": "sleep"}).status_code == 403
        assert notice("sleep").status_code == 200
        assert client.get("/turnstile/ready", headers=good).json()["state"] == "asleep"
        state["up"] = True
        # A different profile/mode cache entry forces an observation immediately.
        assert client.get("/turnstile/ready?conversation_mode=hermes", headers=good).json()["state"] == "awake_not_ready"
        client.get("/turnstile/ready?conversation_mode=hermes&agent_model=first", headers=good)
        client.get("/turnstile/ready?conversation_mode=hermes&agent_model=second", headers=good)
        assert models[-2:] == ["first", "second"]  # forwarding and cache identity both follow the agent model
        notice("cancel")
        assert client.get("/turnstile/ready", headers=good).json()["state"] == "ready"
    assert not wakes


def test_a_statement_is_refused_if_it_is_wrong_in_any_way(monkeypatch):
    nonces, caller = statement.Nonces(), {"name": "pat"}
    sound, _ = statement.make(KEY, "POST", "/v1/x?a=1", b"{}", caller)
    assert statement.check(KEY, sound, "POST", "/v1/x?a=1", b"{}", nonces)["caller"] == caller
    for key, method, where, body in (("another key", "POST", "/v1/x?a=1", b"{}"), (KEY, "GET", "/v1/x?a=1", b"{}"),
                                     (KEY, "POST", "/v1/x?a=2", b"{}"), (KEY, "POST", "/v1/x?a=1", b"{ }")):
        with pytest.raises(ValueError):
            statement.check(key, statement.make(KEY, "POST", "/v1/x?a=1", b"{}", caller)[0], method, where, body, statement.Nonces())
    with pytest.raises(ValueError, match="seen before"):
        statement.check(KEY, sound, "POST", "/v1/x?a=1", b"{}", nonces)  # the same request a second time
    old, _ = statement.make(KEY, "GET", "/", b"", caller)
    monkeypatch.setattr("loadcoach.statement.time.time", lambda: 10**10)
    with pytest.raises(ValueError, match="run out"):
        statement.check(KEY, old, "GET", "/", b"", statement.Nonces())


def test_a_full_pool_is_turned_away_at_once_and_never_held_here(tmp_path, monkeypatch):
    """Requests over max_in_flight get 503 and Retry-After, not a wait outside LoadCoach's line, and not the message that compute is off."""
    users = Users(str(tmp_path / "accounts.db"), {"plain": Group(can=frozenset({"load"}))})
    users.add("pat", "plain")
    good = {"authorization": "Bearer " + users.new_key("pat")}

    class Full(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.PoolTimeout("no connection is free")

    machine_key(tmp_path)
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=str(tmp_path / "machine.key"), max_in_flight=7, busy_retry_seconds=3)
    with TestClient(build_app(settings, httpx.AsyncClient(transport=Full()), lambda *sent: None, users=users), base_url="http://front:8442") as coach:
        for request in (lambda: coach.get("/v1/models", headers=good), lambda: coach.post("/v1/chat/completions", content='{"model": "small"}', headers=good)):
            busy = request()
            assert busy.status_code == 503 and busy.headers["retry-after"] == "3" and busy.headers["x-turnstile"] == "busy"
            assert "already carrying 7 requests" in busy.json()["error"]["message"]


def test_the_pool_of_the_real_client_holds_max_in_flight_and_refuses_the_next_without_waiting():
    async def slow(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(0.5)
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    async def go():
        server = await asyncio.start_server(slow, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with compute_client(Settings(compute="127.0.0.1", max_in_flight=2)) as client:
            return await asyncio.gather(*[client.get(f"http://127.0.0.1:{port}/") for _ in range(3)], return_exceptions=True)

    answers = asyncio.run(go())
    assert sorted(type(a).__name__ for a in answers) == ["PoolTimeout", "Response", "Response"]


def test_the_in_flight_settings_are_read_and_checked(tmp_path):
    file = tmp_path / "weightroom.toml"
    file.write_text('compute = "http://c:1"\n[turnstile]\nmax_in_flight = 40\nbusy_retry_seconds = 2.5\n')
    assert (load_settings(file).max_in_flight, load_settings(file).busy_retry_seconds) == (40, 2.5)
    file.write_text('compute = "http://c:1"\n')
    assert (load_settings(file).max_in_flight, load_settings(file).busy_retry_seconds) == (200, 5)
    for wrong in ("max_in_flight = 0", "max_in_flight = 1.5", 'max_in_flight = "many"', "busy_retry_seconds = 0", "busy_retry_seconds = -1"):
        file.write_text(f'compute = "http://c:1"\n[turnstile]\n{wrong}\n')
        with pytest.raises(SystemExit):
            load_settings(file)
