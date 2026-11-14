"""Hosted models are answered from the control node: its keys, its checks, its log, and compute never sees them."""
import asyncio
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import machine_key
from loadcoach import statement
from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.app import Settings, build_app

FACTS = {"models": {"small": {"hosted": False, "why_not": None, "sees": False, "tools": True, "room": 4096, "warm": False, "resident": False, "speed": None,
                              "load": None, "placed": True, "context": 4096, "fit": "wait for GPU admission", "cards": [], "evictions": [],
                              "model_id": "model_small", "fingerprint": "f" * 8},
                   "busy": {"hosted": False, "why_not": None, "sees": False, "tools": True, "room": 4096, "warm": True, "resident": True, "speed": None,
                            "load": None, "placed": True, "context": 4096, "fit": "resident", "cards": [], "evictions": [],
                            "model_id": "model_busy", "fingerprint": "e" * 8, "free_now": True},
                   "cold": {"hosted": False, "why_not": None, "sees": False, "tools": True, "room": 4096, "warm": False, "resident": False, "speed": None,
                            "load": None, "placed": True, "context": 4096, "fit": "fits now", "cards": [], "evictions": [],
                            "model_id": "model_cold", "fingerprint": "c" * 8, "free_now": False},
                   "queued": {"hosted": False, "why_not": None, "sees": False, "tools": True, "room": 4096, "warm": True, "resident": True, "speed": None,
                              "load": None, "placed": True, "context": 4096, "fit": "resident", "cards": [], "evictions": [],
                              "model_id": "model_queued", "fingerprint": "q" * 8, "free_now": False}}, "environment": {}}


class Rig:
    """Turnstile with a made-up compute and a made-up outside service, each recording what it was asked."""

    def __init__(self, tmp_path, *, key=True, ceiling=None, spend=None, prices=None):
        self.compute_calls, self.service_calls = [], []

        async def compute(request: Request):
            self.compute_calls.append(request.url.path)
            if request.url.path == "/loadcoach/facts":
                return JSONResponse(FACTS)
            if request.url.path == "/v1/models":
                return JSONResponse({"object": "list", "data": [{"id": "small", "object": "model", "owned_by": "loadcoach"}]})
            if request.url.path == "/loadcoach/status":
                return JSONResponse({"running": [], "waiting": [], "hosted": [], "holds": [], "busy": 0})
            return JSONResponse({"answered_by": "compute", "model": json.loads(await request.body() or b"{}").get("model")})

        def service(request: httpx.Request):
            self.service_calls.append((str(request.url), request.headers.get("authorization"), json.loads(request.content or b"{}")))
            said = json.dumps({"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(said))

        key_file = machine_key(tmp_path)
        wired = statement.Statements(Starlette(routes=[Route("/{path:path}", compute, methods=["GET", "POST"])]), key_file)

        class Wire(httpx.AsyncBaseTransport):
            async def handle_async_request(inner, request):
                return await httpx.ASGITransport(app=wired).handle_async_request(request)

        self.users = Users(str(tmp_path / "accounts.db"), {
            "plain": Group(can=frozenset({"load", "hosted"}), max_hosted_spend=spend or 0, max_sensitivity="restricted"),
            "admin": Group(can=frozenset({"load", "hosted", "manage-hosted", "manage-models"})),
            "nohost": Group(can=frozenset({"load"}))})
        for name, group in (("pat", "plain"), ("ada", "admin"), ("sam", "nohost")):
            self.users.add(name, group)
        self.keys = {name: {"authorization": "Bearer " + self.users.new_key(name)} for name in ("pat", "ada", "sam")}
        if key:
            (tmp_path / "gpt.key").write_text("sk-front-only")
        settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=key_file, auth_key_file="")
        self.app = build_app(settings, httpx.AsyncClient(transport=Wire()), lambda *sent: None, users=self.users,
                             hosted_client=httpx.AsyncClient(transport=httpx.MockTransport(service)))
        self.spec = {"url": "https://outside.example/v1", "models": {"gpt": {"theirs": "vendor-gpt", "in": 1.0, "out": 2.0}}}
        if ceiling is not None:
            self.spec["ceiling"] = ceiling


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    with TestClient(made.app, base_url="http://front:8442") as coach:
        assert coach.post("/loadcoach/services/gpt", json=made.spec, headers=made.keys["ada"]).status_code == 201
        made.coach = coach
        yield made


def ask(rig, model="gpt", who="pat", **headers):
    return rig.coach.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}]}, headers={**rig.keys[who], **headers})


def test_a_hosted_model_is_answered_from_front_with_front_s_key_and_compute_never_sees_it(rig):
    answered = ask(rig)
    assert answered.status_code == 200 and answered.headers["x-loadcoach-hosted"] == "gpt" and answered.headers["x-loadcoach-service"] == "gpt"
    assert rig.service_calls == [("https://outside.example/v1/chat/completions", "Bearer sk-front-only",
                                  {"model": "vendor-gpt", "messages": [{"role": "user", "content": "hi"}]})]
    assert "/v1/chat/completions" not in rig.compute_calls  # compute was never asked
    entries = [json.loads(row[0]) for row in rig.users.db.execute("SELECT detail FROM events WHERE kind='request' ORDER BY id")]
    assert [(e.get("hosted"), e.get("service")) for e in entries[:1]] == [("vendor-gpt", "gpt")]
    done = entries[-1]
    assert done["prompt_tokens"] == 10 and done["answer_tokens"] == 5 and done["cost"] == pytest.approx(0.00002) and done["status"] == 200


def test_the_models_and_status_show_hosted_ones_and_the_service_pages_answer_here(rig):
    listed = rig.coach.get("/v1/models", headers=rig.keys["pat"]).json()
    assert {(one["id"], one["owned_by"]) for one in listed["data"]} == {("small", "loadcoach"), ("gpt", "hosted")}
    assert rig.coach.get("/v1/models/gpt", headers=rig.keys["pat"]).json()["owned_by"] == "hosted"
    assert [one["id"] for one in rig.coach.get("/v1/models", headers=rig.keys["sam"]).json()["data"]] == ["small"]  # a group that may not use hosted ones
    shown = rig.coach.get("/loadcoach/services", headers=rig.keys["ada"]).json()["services"]
    assert shown[0]["name"] == "gpt" and shown[0]["key"] is True and shown[0]["spent"] == 0
    assert rig.coach.get("/loadcoach/services", headers=rig.keys["pat"]).status_code == 403
    assert rig.coach.get("/loadcoach/status", headers=rig.keys["ada"]).json()["hosted"] == []


@pytest.mark.parametrize("make,status,why", [
    (dict(key=False), 503, "has no key on the control node"),
    (dict(ceiling=0.01), 429, "Its ceiling is 0.01"),
    (dict(spend=0.01), 429, "Your group's limit is 0.01"),
])
def test_what_may_leave_is_checked_before_anything_is_sent(tmp_path, make, status, why):
    rig = Rig(tmp_path, **make)
    with TestClient(rig.app, base_url="http://front:8442") as coach:
        rig.coach = coach
        assert coach.post("/loadcoach/services/gpt", json=rig.spec, headers=rig.keys["ada"]).status_code == 201
        # Cost already on the log: one answer that cost a dollar.
        rig.users.db.execute("INSERT INTO events (at, kind, what, user, caller, detail) VALUES (strftime('%s','now'), 'request', 'done', 'pat', '', ?)",
                             (json.dumps({"cost": 1.0, "service": "gpt"}),))
        rig.users.db.commit()
        refused = ask(rig)
        assert refused.status_code == status and why in refused.json()["error"]["message"]
        assert rig.service_calls == []


def test_sensitive_work_and_unlogged_requests_stay_off_hosted_models(rig):
    assert ask(rig, **{"x-loadcoach-sensitivity": "sensitive"}).status_code == 403
    assert ask(rig, **{"x-loadcoach-log": "off"}).status_code == 400
    assert rig.service_calls == []


def test_offload_is_decided_here_from_what_compute_says_of_the_local_model(rig):
    waits = ask(rig, "small", **{"x-loadcoach-offload": "gpt"})  # not warm and no room: the hosted model answers in its place
    assert waits.headers["x-loadcoach-hosted"] == "gpt" and len(rig.service_calls) == 1
    assert "/v1/chat/completions" not in rig.compute_calls
    warm = ask(rig, "busy", **{"x-loadcoach-offload": "gpt"})  # warm: it stays where it is
    assert warm.json()["answered_by"] == "compute" and len(rig.service_calls) == 1
    entry = [json.loads(row[0]) for row in rig.users.db.execute("SELECT detail FROM events WHERE kind='request' AND what='asked' ORDER BY id")][0]
    assert entry["instead_of"] == "small"
    # As compute had it: a model that would not answer at once is stood in for, even one that would fit if loaded, or one loaded but with others in line.
    for name in ("cold", "queued"):
        assert ask(rig, name, **{"x-loadcoach-offload": "gpt"}).headers.get("x-loadcoach-hosted") == "gpt"
    # Compute is never sent the header: it does not answer for hosted models.
    assert ask(rig, "small", **{"x-loadcoach-offload": "nope"}).status_code == 400


def test_the_outside_services_move_to_front_once_with_the_database_of_compute_left_as_it_was(tmp_path):
    import sqlite3

    from typer.testing import CliRunner

    from loadcoach.outside import Services
    from weightroom.app import cli as weightroom_cli

    compute = Services(str(tmp_path / "compute.db"), str(tmp_path))
    compute.put("rented", {"url": "https://outside.example/v1", "ceiling": 2, "models": {"gpt": {"theirs": "vendor-gpt", "in": 1}}})
    (tmp_path / "front").mkdir()
    settings = tmp_path / "front.toml"
    settings.write_text('compute = "http://c:1"\ndata = "front"\n')
    move = lambda: CliRunner().invoke(weightroom_cli, ["--config", str(settings), "outside-from", str(tmp_path / "compute.db")])  # noqa: E731
    moved, again = move(), move()
    assert moved.exit_code == 0 and "Took 1 outside services" in moved.output and "rented.key" in moved.output, moved.output
    front = Services(str(tmp_path / "front" / "accounts.db"), str(tmp_path))
    assert front.get("rented") == compute.get("rented") and list(front.models()) == ["gpt"]
    assert "first move only" in str(again.exception)
    assert sqlite3.connect(tmp_path / "compute.db").execute("SELECT COUNT(*) FROM services").fetchone()[0] == 1  # compute's copy is the way back


def hand_in(rig, who, *bodies):
    """Upload a file and start a batch from it through turnstile. Gives the batch as it is answered."""
    content = "\n".join(json.dumps({"custom_id": f"q{n}", "method": "POST", "url": "/v1/chat/completions", "body": body}) for n, body in enumerate(bodies))
    file = rig.coach.post("/v1/files", data={"purpose": "batch"}, files={"file": ("in.jsonl", content)}, headers=rig.keys[who]).json()
    return rig.coach.post("/v1/batches", json={"input_file_id": file["id"], "endpoint": "/v1/chat/completions"}, headers=rig.keys[who]).json()


def until_done(rig, who, number, seconds=8):
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        batch = rig.coach.get(f"/v1/batches/{number}", headers=rig.keys[who]).json()
        if batch["status"] != "in_progress":
            return batch
        time.sleep(0.03)
    raise AssertionError(batch)


def test_a_batch_may_name_a_hosted_model_and_is_answered_from_front_in_the_submitters_name(rig):
    batch = hand_in(rig, "pat", {"model": "gpt", "messages": [{"role": "user", "content": "hi"}]}, {"model": "gpt", "messages": []})
    done = until_done(rig, "pat", batch["id"])
    assert done["status"] == "completed" and done["request_counts"] == {"total": 2, "completed": 2, "failed": 0}
    assert len(rig.service_calls) == 2 and rig.service_calls[0][1] == "Bearer sk-front-only"
    assert "/v1/chat/completions" not in rig.compute_calls  # never reached compute
    answers = [json.loads(line) for line in rig.coach.get(f"/v1/files/{done['output_file_id']}/content", headers=rig.keys["pat"]).text.splitlines()]
    assert [one["custom_id"] for one in answers] == ["q0", "q1"] and answers[0]["response"]["status_code"] == 200
    entries = [(row[0], row[1]) for row in rig.users.db.execute("SELECT user, trace FROM events WHERE kind='request' AND what='asked'")]
    assert entries == [("pat", batch["id"])] * 2  # in pat's name, under the batch's number
    assert rig.coach.get(f"/v1/batches/{batch['id']}", headers=rig.keys["ada"]).status_code == 404  # nobody learns what others have


def test_the_door_turnstile_asks_itself_by_is_not_open_to_a_guess(rig):
    assert rig.coach.get("/loadcoach/me", headers={"authorization": "Bearer guess:pat"}).status_code == 401
    assert rig.coach.get("/v1/models", headers={"authorization": "Bearer :pat"}).status_code == 401


def test_batches_move_to_front_once_with_compute_s_copy_left_as_it_was(tmp_path):
    import sqlite3

    from typer.testing import CliRunner

    from loadcoach.batches import Batches
    from weightroom.app import cli as weightroom_cli

    compute = Batches(str(tmp_path / "compute.db"))
    file = compute.put_file("pat", "in.jsonl", "batch", b'{"custom_id":"a","method":"POST","url":"/v1/chat/completions","body":{"model":"m"}}')
    made = compute.start("pat", "/v1/chat/completions", file["id"], [{"custom_id": "a", "body": {"model": "m"}}])
    task = compute.start("pat", "/loadcoach/tasks", "", [{"custom_id": "t", "body": {"model": "m"}}], {"benchmark": "x"})  # an external task stays where it is
    (tmp_path / "front").mkdir()
    settings = tmp_path / "front.toml"
    settings.write_text('compute = "http://c:1"\ndata = "front"\n')
    move = lambda: CliRunner().invoke(weightroom_cli, ["--config", str(settings), "batches-from", str(tmp_path / "compute.db")])  # noqa: E731
    moved, again = move(), move()
    assert moved.exit_code == 0 and "Took 1 batches, 1 requests and 1 files" in moved.output, moved.output
    front = Batches(str(tmp_path / "front" / "accounts.db"))
    assert front.get(made["id"])["status"] == "in_progress" and front.get(task["id"]) is None and front.content(file["id"])
    assert "first move only" in str(again.exception)
    assert sqlite3.connect(tmp_path / "compute.db").execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 2


def test_a_batch_request_under_way_at_a_hosted_service_counts_as_executing(tmp_path):
    from loadcoach.batches import Batches
    from turnstile.batches import FrontBatches

    store = Batches(str(tmp_path / "front.db"))
    file = store.put_file("pat", "in.jsonl", "batch", b"")
    made = store.start("pat", "/v1/chat/completions", file["id"], [{"custom_id": "a", "body": {"model": "gpt"}}])
    work = FrontBatches(store, None, None, None, None)
    work.in_flight[made["id"]] = {"line": 0, "model": "gpt", "since": 0.0}
    under_way = [{"id": 6, "model": "gpt", "user": "pat"},  # another request by the same account must not masquerade as the batch
                 {"id": 7, "model": "gpt", "user": "pat", "batch_id": made["id"]}]
    seen = work.status(under_way)
    assert seen["open"] == 1 and [(one["id"], one["request_ids"]) for one in seen["active"]] == [(made["id"], [7])]
    assert "batch_id" not in under_way[0]
    assert work.status([])["active"] == []  # a request waiting, or turned away, is not executing


@pytest.mark.parametrize('missing', [False, True])
def test_third_audio_batch_waits_for_partial_inventory_but_detects_real_removal(tmp_path, monkeypatch, missing):
    from loadcoach.batches import Batches
    from loadcoach.log import Log
    from turnstile.batches import FrontBatches, Pins

    monkeypatch.setattr('loadcoach.batches.AGAIN_AFTER', 0)
    users = Users(str(tmp_path / 'users.db'), {'plain': Group(can=frozenset({'load'}))})
    users.add('pat', 'plain')
    store, log = Batches(str(tmp_path / 'batches.db')), Log(str(tmp_path / 'events.db'))
    store.models = Pins({'voice-clone': 'model_voice'})
    voice = {'id': 'voice-clone', 'model_id': 'model_voice', 'owned_by': 'loadcoach'}
    other = {'id': 'embedder', 'model_id': 'model_embed', 'owned_by': 'loadcoach'}
    queries, dispatched = [], []

    async def answer(request):
        if request.method == 'GET':
            queries.append(True)
            if len(queries) == 3:
                return httpx.Response(200, json={'data': [other], 'unavailable_machines': [] if missing else ['compute']})
            return httpx.Response(200, json={'data': [voice, other], 'unavailable_machines': ['unrelated-node']})
        dispatched.append(json.loads(request.content)['model'])
        return httpx.Response(200, json={'data': [{'asset_id': 'finished'}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url='http://front') as door:
            work = FrontBatches(store, log, None, door, lambda name: 'internal-test-key')
            for index in range(3):
                file = store.put_file('pat', 'audio.jsonl', 'batch', b'')
                made = store.start('pat', '/v1/audio/generations', file['id'],
                                   [{'custom_id': str(index), 'body': {'model': 'voice-clone'}}])
                await work.work_through(made['id'], users.by_name)
                counts = store.get(made['id'])['request_counts']
                assert counts['failed'] == int(missing and index == 2)
                assert counts['completed'] == int(not missing or index < 2)
                assert store.db.execute('SELECT tries FROM batch_lines WHERE batch=?', (made['id'],)).fetchone()[0] == 1
    asyncio.run(exercise())
    assert dispatched == ['voice-clone'] * (2 if missing else 3)
    assert len(queries) == (3 if missing else 4)
    for item in (store, log, users):
        item.db.close()
