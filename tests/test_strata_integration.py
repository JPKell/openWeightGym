"""Registered Strata contracts without native inference or production changes."""
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading

import httpx
import pytest

from engines import engine_for
from engines.contracts import Launch
from engines.strata import adapter
from loadcoach.app import build_app
from loadcoach.config import Config
from loadcoach.groups import Group
from loadcoach.models import Models
from loadcoach.runner import Refusal, Runner, Server, Ticket
from loadcoach.users import Users
from front_helpers import front_of
from test_app import GROUPS, fake_llama_server, add_user, in_an_empty_folder  # noqa: F401
from test_strata_settings import settings, node  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401


def test_registered_strata_remains_disabled_and_never_falls_back(settings):
    library = Models(":memory:")
    library.put("strata-model", settings)
    runner = Runner(Config(gpus=("CUDA0",)), None, models=library)
    with pytest.raises(Refusal, match="disabled"):
        asyncio.run(runner.acquire(Ticket("strata-model", "owner", "test", "normal")))
    assert not runner.waiting and not runner.servers
    library.db.close()


@pytest.mark.parametrize("path,extra", [("/v1/embeddings", {}), ("/v1/completions", {}),
    ("/v1/tokenize", {}), ("/v1/chat/completions", {"tools": []}),
    ("/v1/chat/completions", {"response_format": {"type": "json_object"}}),
    ("/v1/chat/completions", {"reasoning_effort": "high"}),
    ("/v1/chat/completions", {"temperature": float("inf")}),
    ("/v1/chat/completions", {"top_k": 65}), ("/v1/chat/completions", {"seed": 0})])
def test_unsupported_requests_are_rejected_without_launch(path, extra):
    with pytest.raises(ValueError):
        engine_for("strata").check_request(path, {"model": "fixture", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, **extra})


def test_ordinary_suite_chat_gets_bounded_output_and_disabled_thinking():
    request = {"model": "fixture", "messages": [{"role": "user", "content": "hi"}],
               "chat_template_kwargs": {"enable_thinking": False}}
    sent = engine_for("strata").prepare_request("/v1/chat/completions", request)
    assert sent["max_tokens"] == 256 and sent["reasoning_effort"] == "none" and "chat_template_kwargs" not in sent
    assert "max_tokens" not in request
    with pytest.raises(ValueError, match="disabled thinking"):
        engine_for("strata").prepare_request("/v1/chat/completions", {**request, "chat_template_kwargs": {"enable_thinking": True}})


def test_private_credentials_and_build_survive_the_front_request_path(settings, node, fake_llama_server, tmp_path, monkeypatch):
    engine = engine_for("strata")
    cleanups = []
    monkeypatch.setattr(engine, "check_available", lambda *args: None)
    monkeypatch.setattr("loadcoach.runner.memory_snapshot", lambda: {"total": 32000, "available": 12000, "service_available": 12000})
    def launch(config, port, selected, cards, **kwargs):
        return Launch([fake_llama_server, "--host", "127.0.0.1", "--port", str(port), "--device", ",".join(cards),
                       "--model", selected["flags"]["model"], "--ctx-size", "512"], dict(os.environ),
                      headers={"Authorization": "Bearer fixture-private-engine-key"},
                      cleanup=lambda: cleanups.append(True), build="strata-build-fixture")
    monkeypatch.setattr(engine, "launch", launch)
    config = Config(models={"hybrid": settings}, gpus=("CUDA0",), llama_server=fake_llama_server,
                    nvidia_smi="no-such-program", database=str(tmp_path/"worker.db"),
                    engines={"strata": asdict(replace(node, host_ram_mib=2048))}, max_wait=3,
                    front="http://front:8442", machine_key_file=str(tmp_path/"machine.key"))
    users = Users(":memory:", GROUPS)
    with front_of(lambda front: build_app(config, front=front), users, tmp_path) as caller:
        owner = add_user(caller, "owner", "admin")
        for path, extra in (("/v1/embeddings", {}), ("/v1/chat/completions", {"tools": []})):
            denied = caller.post(path, headers=owner, json={"model": "hybrid", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, **extra})
            assert denied.status_code == 400, denied.text
        assert caller.get("/loadcoach/status", headers=owner).json()["loaded"] == []
        answered = caller.post("/v1/chat/completions", headers=owner,
                              json={"model": "hybrid", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
        assert answered.status_code == 200, answered.text
        assert answered.headers["x-loadcoach-build"] == "strata-build-fixture"
        assert answered.json()["headers"]["authorization"] == "Bearer fixture-private-engine-key"
        listed = caller.get("/v1/models", headers=owner).json()["data"]
        assert listed[0]["engine_id"] == "strata" and listed[0]["build"] == "strata-build-fixture"
        assert listed[0]["capabilities"]["native_probes"] is False
        subject = caller.get("/loadcoach/probe-subject/hybrid", headers=owner)
        assert subject.status_code == 400 and "unavailable" in subject.text
        environment = caller.get("/loadcoach/probe-environment?engine=strata", headers=owner)
        assert environment.status_code == 400 and "not substituted" in environment.text
        assert "fixture-private-engine-key" not in caller.get("/loadcoach/status", headers=owner).text
    assert cleanups == [True]


@pytest.mark.parametrize("fail", [False, True])
def test_cancelled_preparation_is_joined_and_private_handoff_removed(settings, monkeypatch, fail):
    engine = engine_for("strata")
    started, finish = threading.Event(), threading.Event()
    closed = []
    def launch(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        if fail:
            raise ValueError("Failed preparation still must preserve caller cancellation")
        return Launch([], {}, cleanup=lambda: closed.append(True))
    monkeypatch.setattr(engine, "launch", launch)
    async def check():
        runner = Runner(Config(gpus=("CUDA0",)), None, models=Models(":memory:"))
        server = Server("fixture", ("CUDA0",), settings)
        task = asyncio.create_task(runner.start(server))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and not closed
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == ([] if fail else [True]) and server.process is None
        runner.models.db.close()
    asyncio.run(check())


def test_engine_node_form_does_not_modify_llama_alias_and_model_controls_are_typed(desk, tmp_path, monkeypatch):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    assert browser.post("/compute-nodes/gpus", data={"machine": "compute"}).status_code == 200
    saved = browser.post("/compute-nodes/engines", data={"machine": "compute", "engine": "strata", "do": "save",
        "enabled": "on", "source": "/fixture/source", "python": "/fixture/venv/bin/python", "executable": "/fixture/strata",
        "allow_host_ram": "on", "host_ram_mib": "8192"})
    assert saved.status_code == 200, saved.text
    from loadcoach.settings import Changes
    changes = Changes(str(tmp_path/"loadcoach.db"))
    pending = changes.of("loadcoach")
    assert pending["engines"]["strata"]["host_ram_mib"] == 8192 and "llama_server" not in pending
    # Pending settings don't enable the engine until restart.
    page = browser.get("/models?node=compute&engine=strata")
    assert 'option value="strata"' not in page.text
    library = Models(str(tmp_path/"loadcoach.db"))
    library.put("strata-fixture", {"engine": "strata", "flags": {"model": "/fixture.gguf", "ctx-size": 512},
                                  "strata": {"tokenizer": "/fixture/tokenizer"}, "gpus": "any"})
    page = browser.get("/models/strata-fixture?node=compute")
    assert page.status_code == 200, page.text
    assert 'name="tokenizer"' in page.text and 'id="placement-cpu"' not in page.text
    assert 'name="flag"' not in page.text and 'id="allow-split"' not in page.text
    assert '>llama-server</dt>' not in page.text  # never attribute the node's llama.cpp build to Strata
    browser.post("/compute-nodes/engines", data={"machine": "compute", "engine": "llama_cpp", "do": "save",
        "enabled": "on", "executable": "/fixture/llama-server"})
    browser.post("/compute-nodes/engines", data={"machine": "compute", "engine": "strata", "do": "reset"})
    assert changes.of("loadcoach").get("engines", {}).get("strata") is None
    assert changes.of("loadcoach")["engines"]["llama_cpp"]["executable"] == "/fixture/llama-server"
    library.db.close()


def test_native_benchmarks_refuse_before_creating_runs(tmp_path):
    from benchmarks.app import Settings, build_app as benchmark_app
    from starlette.testclient import TestClient
    calls = []
    def answer(request):
        calls.append(request.url.path)
        if request.url.path == "/loadcoach/me":
            return httpx.Response(200, json={"name": "owner", "can": ["benchmark"]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "strata", "owned_by": "loadcoach",
                "capabilities": {"native_probes": False}, "engine_id": "strata"}]})
        raise AssertionError("Unsupported benchmark must not submit a task or ask for llama.cpp evidence")
    web = httpx.AsyncClient(base_url="http://front", transport=httpx.MockTransport(answer))
    app = benchmark_app(Settings(compute="http://front", database=str(tmp_path/"benchmarks.db")), web)
    with TestClient(app) as caller:
        refused = caller.post("/benchmarks", headers={"authorization": "Bearer fixture"}, json={"benchmark": "context", "models": ["strata"]})
        assert refused.status_code == 400 and "no run was queued" in refused.text
        assert not app.state.store.listing()
        assert "/loadcoach/tasks" not in calls


@pytest.fixture
def policy_node(settings, node, fake_llama_server, tmp_path, monkeypatch):
    """Real openWeightGym authentication/admission/logging, with only native output substituted."""
    engine = engine_for("strata")
    sent = []
    monkeypatch.setattr(engine, "check_available", lambda *args: None)
    monkeypatch.setattr("loadcoach.runner.memory_snapshot", lambda: {
        "total": 32000, "available": 12000, "service_available": 12000})

    def launch(config, port, selected, cards, **kwargs):
        return Launch([fake_llama_server, "--host", "127.0.0.1", "--port", str(port),
                       "--device", ",".join(cards), "--model", selected["flags"]["model"]], dict(os.environ),
                      headers={"Authorization": "Bearer private-policy-engine-key"}, build="strata-policy-build")

    monkeypatch.setattr(engine, "launch", launch)
    send = httpx.AsyncClient.send

    async def native(self, request, *args, **kwargs):
        if request.url.host == "127.0.0.1" and request.url.path == "/v1/chat/completions":
            sent.append((dict(request.headers), json.loads(request.content)))
            class Answer(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield json.dumps({
                        "choices": [{"message": {"role": "assistant", "content": "policy-answer"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}).encode()
            return httpx.Response(200, request=request, stream=Answer(), headers={"content-type": "application/json"})
        return await send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", native)
    config = Config(models={"hybrid": settings}, gpus=("CUDA0",), llama_server=fake_llama_server,
        nvidia_smi="no-such-program", database=str(tmp_path/"worker.db"),
        engines={"strata": asdict(replace(node, host_ram_mib=2048))}, max_wait=3, keep_text=0, unlogged=True,
        front="http://front:8442", machine_key_file=str(tmp_path/"machine.key"))
    groups = {**GROUPS, "quiet": Group(can=frozenset({"load", "unlogged"})),
              "cleared": Group(can=frozenset({"load"}), max_sensitivity="sensitive")}
    with front_of(lambda front: build_app(config, front=front), Users(":memory:", groups), tmp_path) as caller:
        owner = add_user(caller, "owner", "admin")
        plain = add_user(caller, "plain", "plain", by=owner)
        quiet = add_user(caller, "quiet", "quiet", by=owner)
        cleared = add_user(caller, "cleared", "cleared", by=owner)
        yield caller, owner, plain, quiet, cleared, sent


def test_strata_keeps_permissions_sensitivity_usage_and_private_headers(policy_node):
    caller, owner, plain, _, cleared, sent = policy_node
    body = {"model": "hybrid", "messages": [{"role": "user", "content": "policy-secret"}], "max_tokens": 8}
    assert caller.post("/v1/chat/completions", json=body).status_code == 401
    assert caller.get("/loadcoach/engines", headers=plain).status_code == 403
    assert caller.post("/loadcoach/models/hybrid", headers=plain, json={}).status_code == 403
    assert caller.post("/v1/chat/completions", headers={**plain, "x-loadcoach-sensitivity": "sensitive"}, json=body).status_code == 403
    assert not sent
    for headers in ({**cleared, "x-loadcoach-sensitivity": "sensitive", "x-loadcoach-trace": "strata-policy"},
                    {**cleared, "x-loadcoach-trace": "strata-policy"}):
        response = caller.post("/v1/chat/completions", headers=headers, json=body)
        assert response.status_code == 200 and response.json()["usage"]["total_tokens"] == 12
        facts = caller.get("/loadcoach/requests/"+response.headers["x-loadcoach-request-id"], headers=owner).json()
        assert facts["asked"]["level"] == "sensitive" and facts["ended"]["level"] == "sensitive"
        assert facts["ended"]["prompt_tokens"] == 9 and facts["ended"]["answer_tokens"] == 3
        assert response.headers["x-loadcoach-build"] == "strata-policy-build"
    story = caller.get("/loadcoach/events", headers=owner, params={"trace": "strata-policy", "text": 1}).json()["events"]
    first = next(entry for entry in story if entry["what"] == "asked")
    assert first["text"]["messages"][0]["message"]["content"] == "policy-secret"
    for headers, payload in sent:
        assert headers["authorization"] == "Bearer private-policy-engine-key"
        assert not any(key.startswith("x-loadcoach") for key in headers)
        assert payload["reasoning_effort"] == "none"
    assert caller.log.verify()[1] is None


def test_strata_unlogged_chat_leaves_no_request_text_trace_or_reliability(policy_node, tmp_path):
    caller, owner, plain, quiet, _, sent = policy_node
    body = {"model": "hybrid", "messages": [{"role": "user", "content": "never-retain-strata-secret"}], "max_tokens": 8}
    assert caller.post("/v1/chat/completions", headers={**plain, "x-loadcoach-log": "off"}, json=body).status_code == 403
    # Load once before comparing durable evidence; model lifecycle events are not requests.
    assert caller.post("/v1/chat/completions", headers=quiet, json={**body, "messages": [{"role": "user", "content": "warmup"}]}).status_code == 200
    import sqlite3
    def counts():
        with sqlite3.connect(tmp_path/"worker.db") as worker, sqlite3.connect(tmp_path/"roles.db") as roles:
            return [worker.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in ("events", "texts", "traces")] + [
                roles.execute("SELECT COUNT(*) FROM reliability_outcomes").fetchone()[0]]
    before = counts()
    response = caller.post("/v1/chat/completions", headers={**quiet, "x-loadcoach-log": "off"}, json=body)
    assert response.status_code == 200 and response.json()["usage"]["total_tokens"] == 12
    assert "x-loadcoach-request-id" not in response.headers
    assert len(sent) == 2 and counts() == before
    for filename in ("worker.db", "roles.db"):
        assert b"never-retain-strata-secret" not in (tmp_path/filename).read_bytes()
    assert "never-retain-strata-secret" not in str(caller.get("/loadcoach/events", headers=owner).json())
