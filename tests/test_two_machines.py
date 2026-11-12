"""Two signed LoadCoaches behind one front door, with one model available on each."""

import json
import re
import time
import base64
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import door, machine_key
from loadcoach import statement
from loadcoach.config import load_config
from loadcoach.groups import Group
from loadcoach.log import Log, public_number
from loadcoach.users import Users
from turnstile.app import Settings, build_app
from weightroom.app import Settings as WeightRoomSettings, build_app as build_weightroom


def test_second_instance_settings_keep_its_identity_port_card_and_store(tmp_path):
    file = tmp_path / "loadcoach-rig.toml"
    file.write_text('host = "127.0.0.1"\nport = 9442\ndata = "rig"\nmachine_name = "rig"\ngpus = ["CUDA1"]\n')
    config = load_config(file)
    assert (config.machine_name, config.port, config.gpus) == ("rig", 9442, ("CUDA1",))
    assert str(tmp_path / "rig") in config.token_file


class Two(httpx.AsyncBaseTransport):
    def __init__(self, primary, rig):
        self.wires = {8442: httpx.ASGITransport(app=primary), 9442: httpx.ASGITransport(app=rig)}
        self.fail_rig = False
        self.fail_files_once = False
        self.missing_catalogues = set()
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append((request.url.port, request.url.path))
        if request.url.path == '/v1/models' and request.url.port in self.missing_catalogues:
            raise httpx.ReadTimeout('This node did not finish its inventory in time')
        if self.fail_files_once and request.url.port == 9442 and request.url.path == "/loadcoach/files":
            self.fail_files_once = False
            raise httpx.ReadTimeout("the file scan exceeded the short inventory deadline")
        if self.fail_rig and request.url.port == 9442 and request.url.path == "/v1/chat/completions":
            if self.fail_rig == "body":
                encoded = request.headers[statement.HEADER].partition(".")[0]
                nonce = json.loads(base64.urlsafe_b64decode(encoded))["nonce"]

                class EmptyFailure(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        raise httpx.ReadError("rig stopped before the first answer byte")
                        yield b""  # pragma: no cover

                return httpx.Response(200, headers={statement.PROOF: statement.proof("r" * 40, nonce)},
                                      stream=EmptyFailure(), request=request)
            raise httpx.ConnectError("rig disappeared")
        return await self.wires[request.url.port].handle_async_request(request)


def worker(key_file, name, warm, state=None, model="small", content="a" * 64):
    control = state if state is not None else {}
    async def answer(request: Request):
        path = request.url.path
        if path == "/loadcoach/status":
            return JSONResponse({"memory": {"CUDA0": {"total": 16000, "free": 10000}}, "loaded": [],
                                 "running": [{"id": 7, "user": "pat", "model": "small"}] if name == "rig" and not control.get("no_running") else [],
                                 "waiting": [], "holds": [], "busy": int(name == "rig"), "batches": 0,
                                 "draining": {"why": control.get("why", ""), "by": "pat", "since": 1} if control.get("drained") else None})
        if path == "/v1/models":
            return JSONResponse({"object": "list", "data": [{"id": model, "owned_by": "loadcoach", "model_id": name,
                                                               "content_sha256": content, "capabilities": control.get("capabilities", {})}]})
        if path == "/loadcoach/me":
            capabilities = control.get("capabilities", {})
            return JSONResponse({"models": [model], "chat": {"models": {model: {
                "model_id": name, "context": 4096, "vision": False,
                "image_generation": capabilities.get("image_generation", False), "audio": capabilities.get("audio", False)}}}})
        if path == "/loadcoach/models":
            return JSONResponse({"defaults": {}, "cards": [], "build": "test", "to_measure": [], "measuring": None,
                                 "models": [{"name": model, "model_id": name, "flags": {"model": model + ".gguf", "ctx-size": 4096},
                                             "gpus": 0, "order": 10, "sha256": content, "fingerprint": name,
                                             "measurements": [], "last_used": None}]})
        if path == "/loadcoach/files" and control.get("files"):
            return JSONResponse(control["files"])
        if path == "/loadcoach/files/cleanup":
            control["cleanup_request"] = json.loads(await request.body())
            return JSONResponse({"state": "running"}, status_code=202)
        if path == "/loadcoach/model-lock":
            return JSONResponse({"machine": name, "lock": json.loads(await request.body())})
        if path == '/loadcoach/loadouts':
            if request.method == 'GET':
                return JSONResponse({'loadouts': control.get('loadouts', []), 'leases': [], 'machine': name})
            body = json.loads(await request.body())
            control.setdefault('loadouts', []).append(body)
            return JSONResponse({'machine': name, 'body': body, 'caller': request.state.said['caller']['name'],
                                 'lock': request.headers.get('x-loadcoach-lock'), 'priority': request.headers.get('x-loadcoach-priority')})
        if path == "/loadcoach/drain":
            body = json.loads(await request.body())
            control["drained"] = body.get("on", True)
            control["why"] = body.get("why", "")
            return JSONResponse({"machine": name, "busy": 0})
        if path == "/loadcoach/clear":
            control["cleared"] = control.get("cleared", 0) + 1
            return JSONResponse({"machine": name, "removed": 2})
        if path == "/loadcoach/unload":
            control["unloaded"] = control.get("unloaded", 0) + 1
            return JSONResponse({"machine": name, "unloaded": True})
        if path == "/loadcoach/facts":
            return JSONResponse({"environment": {}, "models": {model: {"hosted": False, "why_not": None,
                "sees": False, "tools": True, "room": 4096, "warm": warm, "resident": warm,
                "speed": 20, "load": 1, "placed": not warm, "context": 4096,
                "fit": "resident" if warm else "fits now", "cards": [0], "evictions": [],
                "model_id": name, "fingerprint": name, "prompt_speed": 100}}})
        if path == "/v1/chat/completions":
            if state and state.get("refuse"):
                return JSONResponse({"error": "admission refused"}, status_code=503, headers={"retry-after": "5"})
            return JSONResponse({"machine": name, "model": json.loads(await request.body())["model"],
                                 "sensitivity": request.headers.get("x-loadcoach-sensitivity", "normal")},
                                headers={"x-loadcoach-request-id": "7", "x-loadcoach-answered-model": "small",
                                         "x-loadcoach-answered-model-id": name, "x-loadcoach-answered-fingerprint": name})
        if path in ("/loadcoach/stop/7", "/loadcoach/peek/7", "/loadcoach/diagnostics/7"):
            return JSONResponse({"machine": name, "path": path})
        return JSONResponse({"error": path}, status_code=404)
    return statement.Statements(Starlette(routes=[Route("/{path:path}", answer, methods=["GET", "POST"])]), key_file)


@pytest.fixture
def metadata_front(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {"plain": Group(can=frozenset({"load", "manage-models", "manage-settings", "benchmark"})),
                                                "system": Group(can=frozenset({"follow-settings"}))})
    users.add("pat", "plain")
    users.add("weightroom", "system")
    key = users.new_key("pat")
    own = users.new_key("weightroom")
    (tmp_path / "weightroom.key").write_text(own)
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)

    class Wire(Two):
        down = False
        blocked = False
        refreshes = 0

        def __init__(self):
            super().__init__(worker(primary_key, "compute", False), worker(rig_key, "rig", False))
            self.release = asyncio.Event()

        async def handle_async_request(self, request):
            if self.down:
                self.calls.append((request.url.port, request.url.path))
                raise httpx.ConnectTimeout("unavailable worker")
            if self.blocked and request.url.path == '/loadcoach/me':
                self.refreshes += 1
                await self.release.wait()
            return await super().handle_async_request(request)

    wire = Wire()
    app = build_app(Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                             auth_key_file=str(tmp_path / "weightroom.key"), machines_poll_seconds=3600,
                             machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}}),
                    httpx.AsyncClient(transport=wire), users=users)
    with TestClient(app, base_url="http://front:8442") as front:
        front.portal.call(app.state.machines.poll)
        assert front.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}).json()['models'] == ['small']
        yield front, wire, users, key, own


def test_offline_navigation_reuses_metadata_without_retrying_workers(metadata_front, tmp_path):
    front, wire, users, key, own = metadata_front
    wire.down = True
    front.portal.call(front.app.state.machines.poll)
    before = list(wire.calls)
    browser = TestClient(build_weightroom(WeightRoomSettings(compute='http://compute', key=own,
        data=str(tmp_path / 'web')), door(front.app, 8442)))
    browser.cookies.set('weightroom', key)
    for path in ('/docs/web-app', '/account/appearance', '/status', '/status?part=status', '/status?part=requests',
                 '/chat', '/models', '/compute-nodes', '/settings', '/benchmarks/runs',
                 '/usage?part=report', '/requests?part=page', '/log?part=page'):
        response = browser.get(path)
        assert response.status_code == 200
        if '?' not in path:
            assert 'Node unavailable: compute, rig' in response.text
        assert wire.calls == before, (path, wire.calls[len(before):])
    assert wire.calls == before
    # Explicit reads still probe a node without the page's passive-read header.
    front.get('/v1/models', headers={'authorization': 'Bearer ' + key})
    assert len(wire.calls) > len(before)
    before = list(wire.calls)
    # A changed policy cannot reuse model metadata from the previous scope.
    users.groups.put('plain', {'can': ['load'], 'models': ['different']})
    me = front.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}).json()
    assert me['models'] == [] and me['unavailable_machines'] == ['compute', 'rig']
    users.change('pat', enabled=False)
    assert front.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}).status_code == 401
    assert wire.calls == before

    users.change('pat', enabled=True)
    wire.down = False
    front.portal.call(front.app.state.machines.poll)
    assert front.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}).json()['unavailable_machines'] == []


def test_metadata_refresh_is_deduplicated_and_does_not_block_authentication(metadata_front):
    front, wire, _, key, _ = metadata_front
    front.app.state.log.db.execute('UPDATE node_discovery SET at=0')
    wire.blocked = True

    async def reads():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=front.app), base_url='http://front:8442') as client:
            for _ in range(3):
                response = await asyncio.wait_for(client.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}), .5)
                assert response.status_code == 200 and response.json()['models'] == ['small']
        assert wire.refreshes == 2  # one pending refresh per node, shared by all three reads
        wire.release.set()

    front.portal.call(reads)


def test_chat_and_roles_use_background_inventory_without_request_fanout(metadata_front, monkeypatch, tmp_path):
    front, wire, users, _, _ = metadata_front
    # A monitor refresh sees a different resident without any chat asking the fleet.
    wire.wires[9442] = httpx.ASGITransport(app=worker(str(tmp_path / "rig.key"), "rig", True))
    front.app.state.log.db.execute("UPDATE node_discovery SET at=0")
    front.portal.call(front.app.state.machines.poll)
    users.add("newcomer", "plain")
    headers = {"authorization": "Bearer " + users.new_key("newcomer")}
    # This user has never fetched model metadata. Expiring the old short cache
    # must not make either direct-model or role requests contact the other node.
    monkeypatch.setattr("turnstile.roles.FACTS_FOR", 0)
    role = front.post("/loadcoach/roles/chat", headers=headers, json={"models": {"small": 1}})
    assert role.status_code == 201
    wire.calls.clear()
    spans = []
    monkeypatch.setattr(front.app.state.observations, "submit", spans.append)
    for model in ("small", "small", "chat", "chat:fast"):
        answer = front.post("/v1/chat/completions", headers=headers, json={"model": model, "messages": []})
        assert answer.status_code == 200 and answer.json()["machine"] == "rig"
    assert wire.calls and {port for port, _ in wire.calls} == {9442}
    assert all(path != "/loadcoach/facts" for _, path in wire.calls)
    assert sum(path == "/v1/chat/completions" for _, path in wire.calls) == 4
    assert spans and all(span["user"] == "newcomer" for span in spans)

    # Shared monitoring metadata never grants the caller additional models.
    users.groups.put("plain", {"can": ["load"], "models": []})
    wire.calls.clear()
    assert front.get("/loadcoach/pick?model=chat", headers=headers).status_code == 404
    assert wire.calls == []


def test_missing_routing_inventory_waits_for_monitor_without_probing_fleet(metadata_front):
    front, wire, _, key, _ = metadata_front
    front.app.state.log.db.execute("DELETE FROM node_discovery")
    wire.calls.clear()
    answer = front.post("/v1/chat/completions", headers={"authorization": "Bearer " + key},
                        json={"model": "small", "messages": []})
    assert answer.status_code == 503 and answer.headers["retry-after"] == "5"
    assert "inventory is not ready" in answer.text and wire.calls == []


def test_required_file_store_gets_one_longer_retry_after_a_short_timeout(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {
        "manager": Group(can=frozenset({"manage-models"})),
        "system": Group(can=frozenset({"follow-settings"})),
    })
    users.add("jordan", "manager")
    users.add("weightroom", "system")
    headers = {"authorization": "Bearer " + users.new_key("jordan")}
    (tmp_path / "weightroom.key").write_text(users.new_key("weightroom"))
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)
    files = {"places": [{"name": "models", "folder": "/models", "free": 100}],
             "files": [{"path": "/models/one.gguf", "place": "models", "size": 10}]}
    wire = Two(worker(primary_key, "compute", False),
               worker(rig_key, "rig", False, {"files": files}))
    wire.fail_files_once = True
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                        auth_key_file=str(tmp_path / "weightroom.key"), artifact_machine="rig",
                        machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}})
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users),
                    base_url="http://front:8442") as front:
        listed = front.get("/turnstile/artifacts/files", headers=headers)
    assert listed.status_code == 200 and listed.json()["places"] == files["places"]
    assert wire.calls.count((9442, "/loadcoach/files")) == 2


def test_clear_cache_keeps_files_used_on_other_nodes_and_refuses_an_unknown_node(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {
        "manager": Group(can=frozenset({"manage-models"})),
        "system": Group(can=frozenset({"follow-settings"})),
    })
    users.add("jordan", "manager")
    users.add("weightroom", "system")
    headers = {"authorization": "Bearer " + users.new_key("jordan")}
    (tmp_path / "weightroom.key").write_text(users.new_key("weightroom"))
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)
    facts = {"what": "model", "name": "One", "architecture": "tiny", "size_label": "1B"}
    primary_state = {"files": {"places": [{"name": "llm", "folder": "/compute"}],
                               "files": [{"path": "/compute/one.gguf", "place": "llm", "size": 10,
                                          "facts": facts, "used_by": ["one"]}]}}
    artifact_state = {"files": {"places": [{"name": "llm", "folder": "/cache"}],
                                "files": [{"path": "/cache/one.gguf", "place": "llm", "tier": "cache",
                                           "size": 10, "facts": facts, "used_by": []}]}}
    wire = Two(worker(primary_key, "compute", False, primary_state),
               worker(rig_key, "rig", False, artifact_state))
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                        auth_key_file=str(tmp_path / "weightroom.key"), artifact_machine="rig",
                        machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}})
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users),
                    base_url="http://front:8442") as front:
        started = front.post("/turnstile/artifacts/files/clear-cache", headers=headers)
        assert started.status_code == 202
        assert artifact_state["cleanup_request"] == {"mode": "clear-cache", "keep": ["/cache/one.gguf"]}
        primary_state.pop("files")
        refused = front.post("/turnstile/artifacts/files/clear-cache", headers=headers)
        assert refused.status_code == 409 and "compute" in refused.json()["error"]["message"]


def test_consolidate_cache_promotes_storage_copies_of_files_used_on_any_node(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {
        "manager": Group(can=frozenset({"manage-models"})),
        "system": Group(can=frozenset({"follow-settings"})),
    })
    users.add("jordan", "manager")
    users.add("weightroom", "system")
    headers = {"authorization": "Bearer " + users.new_key("jordan")}
    (tmp_path / "weightroom.key").write_text(users.new_key("weightroom"))
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)
    one = {"what": "model", "name": "One", "architecture": "tiny", "size_label": "1B"}
    two = {**one, "name": "Two"}
    primary_state = {"files": {"places": [{"name": "llm", "folder": "/compute"}],
                               "files": [{"path": "/compute/one.gguf", "place": "llm", "size": 10, "facts": one, "used_by": ["one"]},
                                         {"path": "/compute/two.gguf", "place": "llm", "size": 10, "facts": two, "used_by": ["two"]},
                                         {"path": "/compute/gone.gguf", "place": "llm", "size": 10, "facts": one, "used_by": ["gone"]}]}}
    artifact_state = {"files": {"places": [{"name": "llm", "folder": "/cache"}],
                                "files": [{"path": "/cache/two.gguf", "place": "llm", "tier": "cache", "size": 10, "facts": two, "used_by": []},
                                          {"path": "/storage/one.gguf", "place": "storage_llm", "tier": "storage", "size": 10, "facts": one, "used_by": []},
                                          {"path": "/storage/two.gguf", "place": "storage_llm", "tier": "storage", "size": 10, "facts": two, "used_by": []}]}}
    wire = Two(worker(primary_key, "compute", False, primary_state),
               worker(rig_key, "rig", False, artifact_state))
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                        auth_key_file=str(tmp_path / "weightroom.key"), artifact_machine="rig",
                        machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}})
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users),
                    base_url="http://front:8442") as front:
        started = front.post("/turnstile/artifacts/files/consolidate-cache", headers=headers)
        assert started.status_code == 202 and started.json()["absent"] == ["gone.gguf"]
        assert artifact_state["cleanup_request"] == {"mode": "promote", "promote": ["/storage/one.gguf"]}


def test_weightroom_status_controls_target_the_selected_loadcoach(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {
        "operator": Group(can=frozenset({"load", "drain", "clear-line", "unload", "benchmark"})),
        "system": Group(can=frozenset({"follow-settings"})),
    })
    users.add("jordan", "operator")
    users.change("jordan", password="correct horse")
    users.add("weightroom", "system")
    web_key = users.new_key("weightroom")
    (tmp_path / "weightroom.key").write_text(web_key)
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)
    primary_state, rig_state = {}, {"no_running": True}
    wire = Two(worker(primary_key, "compute", False, primary_state), worker(rig_key, "rig", False, rig_state))
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                        auth_key_file=str(tmp_path / "weightroom.key"),
                        machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}},
                        machines_poll_seconds=0.05)
    front_app = build_app(settings, httpx.AsyncClient(transport=wire), users=users)
    web_settings = WeightRoomSettings(compute="http://front:8442", key=web_key, data=str(tmp_path / "web"))
    web_app = build_weightroom(web_settings, door(front_app, 8442))
    web_lifespan = web_app.router.lifespan_context
    @asynccontextmanager
    async def both(app):
        # ASGITransport invokes front on WeightRoom's loop; keep its background
        # workers there too, matching the one-process ownership of its DB handles.
        async with front_app.router.lifespan_context(front_app), web_lifespan(app):
            yield
    web_app.router.lifespan_context = both
    with TestClient(web_app) as browser:
        browser.post("/sign-in", data={"name": "jordan", "password": "correct horse"})
        for _ in range(100):
            html = browser.get("/status?part=status").text
            if 'value="rig"' in html:
                break
            time.sleep(.05)
        assert 'name="machine"' in html and 'value="compute"' in html and 'value="rig"' in html
        for _ in range(100):
            benchmarks = browser.get('/benchmarks?benchmark=context').text
            if 'aria-label="Select small at rig"' in benchmarks:
                break
            time.sleep(.05)
        assert 'aria-label="Select small at compute"' in benchmarks and 'aria-label="Select small at rig"' in benchmarks
        results = browser.get('/benchmarks/node-measurements?benchmark=context').text.split('id="benchmark-resource-table"', 1)[1].split('</thead>', 1)[0]
        assert re.findall(r'<th\b[^>]*\bdata-label="([^"]+)"', results) == ['compute', 'rig']
        assert "LoadCoach: rig." in browser.post("/control", data={"machine": "rig", "do": "clear"}).text
        assert rig_state["cleared"] == 1 and not primary_state.get("cleared")
        assert "LoadCoach: rig." in browser.post("/control", data={"machine": "rig", "do": "drain", "why": "maintenance"}).text
        assert rig_state["drained"] and not primary_state.get("drained")
        for _ in range(100):
            if 'name="machine" value="rig"' in browser.get("/status?part=status").text:
                break
            time.sleep(.05)
        else:
            raise AssertionError("Secondary LoadCoach drain was not shown on Status")
        assert "LoadCoach: rig." in browser.post("/control", data={"machine": "rig", "do": "open"}).text
        assert not rig_state["drained"]
        assert "LoadCoach: rig." in browser.post("/control", data={"machine": "rig", "do": "unload"}).text
        for _ in range(100):
            if rig_state.get("unloaded"):
                break
            time.sleep(.05)
        assert rig_state.get("unloaded") == 1 and not primary_state.get("unloaded")


def test_warm_machine_is_chosen_then_a_lost_machine_is_replaced_before_output(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {"plain": Group(can=frozenset({"load"}), max_sensitivity="restricted"),
                                               "system": Group(can=frozenset({"follow-settings"}))})
    users.add("pat", "plain")
    users.add("weightroom", "system")
    headers = {"authorization": "Bearer " + users.new_key("pat")}
    (tmp_path / "weightroom.key").write_text(users.new_key("weightroom"))
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)
    rig_state = {"refuse": False}
    wire = Two(worker(primary_key, "compute", False), worker(rig_key, "rig", True, rig_state))
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                        auth_key_file=str(tmp_path / "weightroom.key"), machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}},
                        machines_poll_seconds=0.05)
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users), base_url="http://front:8442") as front:
        for _ in range(100):
            if all(one["state"] == "ready" for one in front.get("/turnstile/machines", headers=headers).json()["machines"]):
                break
            time.sleep(.05)
        else:
            raise AssertionError("Both signed inventories did not become ready")
        first = front.post("/v1/chat/completions", headers={**headers, "x-loadcoach-trace": "sticky-1",
                                                           "x-loadcoach-sensitivity": "restricted"}, json={"model": "small", "messages": []})
        assert first.status_code == 200 and first.json()["machine"] == "rig"
        assert first.json()["sensitivity"] == "restricted"
        assert int(first.headers["x-loadcoach-request-id"]) == public_number("rig", 7)
        rig_state["refuse"] = True
        refused = front.post("/v1/chat/completions", headers=headers, json={"model": "small", "messages": []})
        assert refused.status_code == 200 and refused.json()["machine"] == "compute"  # compute makes the final admission decision
        rig_state["refuse"] = False
        wire.fail_rig = True
        second = front.post("/v1/chat/completions", headers={**headers, "x-loadcoach-trace": "sticky-1"},
                            json={"model": "small", "messages": []})
        assert second.status_code == 200 and second.json()["machine"] == "compute"
        assert second.json()["sensitivity"] == "restricted"
        assert second.headers["x-loadcoach-request-id"] == "7"
        wire.fail_rig = "body"
        before_body = front.post("/v1/chat/completions", headers=headers, json={"model": "small", "messages": [], "stream": True})
        assert before_body.status_code == 200 and before_body.json()["machine"] == "compute"
        wire.fail_rig = False
        listed = front.get("/v1/models", headers=headers).json()["data"]
        assert listed[0]["id"] == "small" and listed[0]["machines"] == ["compute", "rig"]
        locked = front.post("/turnstile/machines/rig/model-lock", headers=headers, json={"name": "small", "on": True})
        assert locked.status_code == 200 and locked.json() == {"machine": "rig", "lock": {"name": "small", "on": True}}
        assert front.post("/turnstile/machines/unknown/model-lock", headers=headers, json={"name": "small", "on": True}).status_code == 404
        assert front.post("/turnstile/machines/rig/model-lock", json={"name": "small", "on": True}).status_code == 401
        assert front.get("/turnstile/machines/rig/control/status", headers=headers).json()["busy"] == 1
        assert front.post("/turnstile/machines/rig/control/drain", headers=headers, json={"why": "maintenance"}).json()["machine"] == "rig"
        assert front.get("/turnstile/machines/rig/control/status", headers=headers).json()["draining"]["why"] == "maintenance"
        assert front.post("/turnstile/machines/rig/control/clear", headers=headers).json() == {"machine": "rig", "removed": 2}
        assert front.post("/turnstile/machines/rig/control/unload", headers=headers).json()["unloaded"] is True
        assert front.post("/turnstile/machines/rig/control/drain", headers=headers, json={"on": False}).status_code == 200
        assert front.get("/turnstile/machines/rig/control/status", headers=headers).json()["draining"] is None
        assert front.post("/turnstile/machines/unknown/control/clear", headers=headers).status_code == 404
        assert front.post("/turnstile/machines/rig/control/unknown", headers=headers).status_code == 404
        assert front.post("/turnstile/machines/rig/control/clear").status_code == 401
        status = front.get("/loadcoach/status", headers=headers).json()
        assert set(status["memory"]) == {"compute:CUDA0", "rig:CUDA0"}
        assert set(status["model_cards"]) == {"compute:CUDA0", "rig:CUDA0"}
        assert status["running"][0]["id"] == public_number("rig", 7)
        stopped = front.post(f"/loadcoach/stop/{public_number('rig', 7)}", headers=headers)
        assert stopped.status_code == 200 and stopped.json() == {"machine": "rig", "path": "/loadcoach/stop/7"}
        peeked = front.get(f"/loadcoach/peek/{public_number('rig', 7)}", headers=headers)
        assert peeked.status_code == 200 and peeked.json()["machine"] == "rig"
        said, nonce = statement.make(statement.read_key(rig_key), "GET", "/turnstile/accounts", b"", machine="rig")
        authorized = front.get("/turnstile/accounts", headers={statement.HEADER: said})
        assert authorized.status_code == 200 and authorized.headers[statement.PROOF] == statement.proof(statement.read_key(rig_key), nonce)
        spoofed, _ = statement.make(statement.read_key(primary_key), "GET", "/turnstile/accounts", b"", machine="rig")
        assert front.get("/turnstile/accounts", headers={statement.HEADER: spoofed}).status_code == 403
        for name, key_file in (("compute", primary_key), ("rig", rig_key)):
            source = Log(str(tmp_path / f"{name}-events.db"), chained=False, keep_text=5)
            entry = source.add("request", "asked", "pat", model="small")
            source.add("request", "done", "pat", model="small", of=entry)
            source.keep_asked(entry, json.dumps({"from": name}))
            entries, texts = source.unsent()
            body = json.dumps({"entries": entries, "texts": texts, "keep_text": 5}).encode()
            signed, nonce = statement.make(statement.read_key(key_file), "POST", "/turnstile/log", body, machine=name)
            delivered = front.post("/turnstile/log", content=body, headers={statement.HEADER: signed})
            assert delivered.status_code == 200 and delivered.headers[statement.PROOF] == statement.proof(statement.read_key(key_file), nonce)
        for name in ("compute", "rig"):
            number = public_number(name, entry)
            facts = front.get(f"/loadcoach/requests/{number}", headers=headers).json()
            assert facts["asked"]["machine"] == name and facts["ended"]["what"] == "done"
            assert facts["text"]["asked"]["from"] == name


def test_one_logical_model_routes_to_differently_named_location_copies(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {
        "plain": Group(can=frozenset({"load", "manage-models"})), "system": Group(can=frozenset({"follow-settings"}))})
    users.add("pat", "plain")
    users.add("weightroom", "system")
    headers = {"authorization": "Bearer " + users.new_key("pat")}
    (tmp_path / "weightroom.key").write_text(users.new_key("weightroom"))
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)
    wire = Two(worker(primary_key, "compute", False, model="embed-q8"),
               worker(rig_key, "rig", True, model="embed-cpu"))
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                        auth_key_file=str(tmp_path / "weightroom.key"),
                        machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}},
                        machines_poll_seconds=0.05)
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users), base_url="http://front:8442") as front:
        for _ in range(100):
            listed = front.get("/v1/models", headers=headers).json()["data"]
            if listed and len(listed[0].get("locations", [])) == 2:
                break
            time.sleep(.05)
        model = listed[0]
        assert model["id"] == "embed-q8" and [(one["machine"], one["id"]) for one in model["locations"]] == [
            ("compute", "embed-q8"), ("rig", "embed-cpu")]
        managed = front.get("/turnstile/models", headers=headers).json()["models"]
        assert len(managed) == 1 and managed[0]["name"] == "embed-q8" and [one["machine"] for one in managed[0]["locations"]] == ["compute", "rig"]
        assert all(one["enabled"] for one in managed[0]["locations"])
        assert front.get('/v1/models', headers=headers).json()['unavailable_machines'] == []
        wire.missing_catalogues.add(8442)
        partial = front.get('/v1/models', headers=headers).json()
        assert partial['unavailable_machines'] == ['compute'] and partial['data'][0]['id'] == 'embed-q8'
        wire.missing_catalogues.clear()
        answer = front.post("/v1/chat/completions", headers=headers, json={"model": "embed-q8", "messages": []})
        assert answer.status_code == 200 and answer.json()["machine"] == "rig" and answer.json()["model"] == "embed-cpu"
        disabled = front.post("/turnstile/models/location", headers=headers,
                              json={"machine": "rig", "model_id": "rig", "enabled": False})
        assert disabled.status_code == 200
        managed = front.get("/turnstile/models", headers=headers).json()["models"]
        assert [one["enabled"] for one in managed[0]["locations"]] == [True, False]
        answer = front.post("/v1/chat/completions", headers=headers, json={"model": "embed-q8", "messages": []})
        assert answer.status_code == 200 and answer.json()["machine"] == "compute"


def test_known_down_cpu_worker_returns_retryable_503_without_disclosing_forbidden_alias(tmp_path):
    users = Users(str(tmp_path/'accounts.db'), {
        'plain': Group(can=frozenset({'load'})),
        'limited': Group(models=('other',), can=frozenset({'load'})),
        'system': Group(can=frozenset({'follow-settings'}))})
    for name, group in (('pat', 'plain'), ('limited', 'limited'), ('weightroom', 'system')):
        users.add(name, group)
    headers = {'authorization':'Bearer '+users.new_key('pat')}
    limited = {'authorization':'Bearer '+users.new_key('limited')}
    (tmp_path/'weightroom.key').write_text(users.new_key('weightroom'))
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path/'rig.key')
    statement.replace_key(rig_key, 'r'*40)

    async def empty(request):
        if request.url.path == '/loadcoach/facts':
            return JSONResponse({'environment':{}, 'models':{}})
        if request.url.path == '/v1/models':
            return JSONResponse({'data':[]})
        if request.url.path == '/loadcoach/status':
            return JSONResponse({'cards':[], 'loaded':[], 'running':[], 'waiting':[]})
        return JSONResponse({'error': 'The model does not exist.'}, status_code=404)
    primary = statement.Statements(Starlette(routes=[Route('/{path:path}', empty, methods=['GET','POST'])]), primary_key)

    class Outage(Two):
        down = False
        async def handle_async_request(self, request):
            if self.down and request.url.port == 9442:
                raise httpx.ConnectError('CPU worker stopped')
            return await super().handle_async_request(request)
    wire = Outage(primary, worker(rig_key, 'rig', False))
    settings = Settings(compute='compute', data=str(tmp_path), machine_key_file=primary_key,
        auth_key_file=str(tmp_path/'weightroom.key'), machines_poll_seconds=.05,
        machines={'rig':{'compute':'http://compute:9442','machine_key_file':rig_key}})
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users), base_url='http://front:8442') as front:
        for _ in range(100):
            if any(row['names'] == ['small'] for row in front.get('/turnstile/machines', headers=headers).json()['models']):
                break
            time.sleep(.05)
        else:
            raise AssertionError('CPU inventory never became known')
        wire.down = True
        # The first request has no user-facts cache, while inventory still knows the alias.
        answer = front.post('/v1/embeddings', headers=headers, json={'model':'small','input':'outage'})
        assert answer.status_code == 503 and int(answer.headers['retry-after']) > 0
        assert 'not answering' in answer.text
        forbidden = front.post('/v1/embeddings', headers=limited, json={'model':'small','input':'outage'})
        assert forbidden.status_code == 404 and 'worker' not in forbidden.text
        missing = front.post('/v1/embeddings', headers=headers, json={'model':'unknown','input':'outage'})
        assert missing.status_code == 404


@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/images/generations", "/v1/audio/generations"])
def test_retained_catalogue_wakes_owning_node_and_times_out(tmp_path, monkeypatch, endpoint):
    monkeypatch.setattr("turnstile.app.TRY_EVERY", .001)
    users = Users(str(tmp_path / "accounts.db"), {"plain": Group(can=frozenset({"load", "front-wake"})),
                                                "limited": Group(can=frozenset({"load"}), models=("other",))})
    users.add("pat", "plain")
    users.add("limited", "limited")
    headers = {"authorization": "Bearer " + users.new_key("pat")}
    limited = {"authorization": "Bearer " + users.new_key("limited")}
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)

    class Outage(Two):
        down = False
        async def handle_async_request(self, request):
            if self.down and request.url.port == 9442:
                raise httpx.ConnectError("sleeping")
            return await super().handle_async_request(request)

    wire = Outage(worker(primary_key, "compute", False, model="other", content="b" * 64),
                  worker(rig_key, "rig", False, model="sleepy", state={"capabilities":
                      {"image_generation": True} if "images" in endpoint else {"audio": "music"} if "audio" in endpoint else {}}))
    packets = []
    restore = [False]
    def wake(*packet):
        packets.append(packet)
        if restore[0]:
            wire.down = False
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=primary_key, wait=.025,
                        wake_mac="00:11:22:33:44:55", machines={"rig": {"compute": "http://compute:9442",
                        "machine_key_file": rig_key, "wake_mac": "00:11:22:33:44:66", "wake_address": "10.0.0.255"}})
    def app():
        return build_app(settings, httpx.AsyncClient(transport=wire), wake, users=users)
    with TestClient(app(), base_url="http://front:8442") as front:
        assert "sleepy" in [row["id"] for row in front.get("/v1/models", headers=headers).json()["data"]]
        assert "sleepy" in front.get("/loadcoach/me", headers=headers).json()["models"]
    wire.down = True
    # Restart front while the worker is still asleep: discovery must survive.
    with TestClient(app(), base_url="http://front:8442") as front:
        listed = front.get("/v1/models", headers={**headers, "x-turnstile-no-wake": "1"}).json()
        assert "sleepy" in [row["id"] for row in listed["data"]]
        assert listed["unavailable_machines"] == ["rig"] and not packets
        me = front.get("/loadcoach/me", headers=headers).json()
        assert "sleepy" in me["models"] and me["unavailable_machines"] == ["rig"]
        if "images" in endpoint:
            assert "sleepy" in [row["id"] for row in front.get("/mirrorwall/state", headers=headers).json()["models"]]
        if "audio" in endpoint:
            assert "sleepy" in [row["id"] for row in front.get("/soundroom/state", headers=headers).json()["models"]]
        assert not packets
        assert front.post("/turnstile/machines/rig/wake", headers=limited).status_code == 403
        assert front.post("/turnstile/machines/missing/wake", headers=headers).status_code == 404
        assert front.post("/turnstile/machines/rig/wake", headers=headers).status_code == 202
        assert packets == [("00:11:22:33:44:66", "10.0.0.255")]
        packets.clear()
        timed_out = front.post(endpoint, headers=headers, json={"model": "sleepy", "messages": [], "prompt": "test"})
        assert timed_out.status_code == 504, timed_out.text
        assert "0.025 seconds" in timed_out.text and packets
        assert set(packets) == {("00:11:22:33:44:66", "10.0.0.255")}
        if endpoint == "/v1/chat/completions":
            restore[0] = True
            success = front.post(endpoint, headers=headers, json={"model": "sleepy", "messages": []})
            assert success.status_code == 200 and success.json()["machine"] == "rig"
        # A changed caller policy cannot read the previous broader catalogue.
        wire.down = True
        users.groups.put("plain", {"can": ["load"], "models": ["other"]})
        listed = front.get("/v1/models", headers=headers).json()
        assert "sleepy" not in [row["id"] for row in listed["data"]]
