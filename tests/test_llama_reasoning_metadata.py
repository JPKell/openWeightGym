"""Runtime-discovered template capabilities; no installed engine or GPU is used."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from jinja2 import Environment, FileSystemLoader

from engines.llama_cpp.adapter import LlamaCpp
from loadcoach.config import Config
from loadcoach.models import Models
from loadcoach.runner import Runner, Server
from test_app import add_user, fake_llama_server, gateway, in_an_empty_folder, model  # noqa: F401


@pytest.mark.parametrize("caps", [
    {"supports_reasoning_effort": True, "reasoning_effort_levels": ["brief", "deep", "future"]},
    {"supports_reasoning_effort": False, "reasoning_effort_levels": []},
    {"supports_tools": True},
])
def test_native_props_schema_and_unknown_future_levels(caps):
    async def run():
        calls = []

        def answer(request):
            calls.append(request)
            return httpx.Response(200, json={"chat_template_caps": caps, "model_path": "/private/weights"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            server = SimpleNamespace(url="http://runtime", headers={"x-private": "test"})
            assert await LlamaCpp().runtime_metadata(client, server) == {"chat_template_caps": caps}
        assert len(calls) == 1 and calls[0].url.path == "/props"
        assert calls[0].headers["x-private"] == "test"
        assert calls[0].extensions["timeout"]["read"] == 2

    asyncio.run(run())


@pytest.mark.parametrize("payload,expected", [
    ({}, {}), ([], {}), ({"chat_template_caps": None}, {}),
    ({"chat_template_caps": {"reasoning_effort_levels": ["low", 2]}}, {}),
    ({"chat_template_caps": {"supports_reasoning_effort": "yes"}}, {}),
])
def test_older_or_malformed_props_do_not_invent_capabilities(payload, expected):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
            assert await LlamaCpp().runtime_metadata(client, SimpleNamespace(url="http://runtime", headers={})) == expected
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["old", "invalid-json", "timeout"])
def test_discovery_failures_are_optional(failure):
    def answer(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("unavailable", request=request)
        return httpx.Response(404 if failure == "old" else 200, content=b"not JSON")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            assert await LlamaCpp().runtime_metadata(client, SimpleNamespace(url="http://runtime", headers={})) == {}
    asyncio.run(run())


def test_observations_require_ready_matching_process_and_do_not_make_requests(tmp_path):
    library = Models(str(tmp_path / "models.db"))
    settings = model(str(tmp_path / "model.gguf"), 0)
    library.put("example", settings)
    runner = Runner(Config(), None, models=library)
    observed = {"chat_template_caps": {"reasoning_effort_levels": ["future"]}}
    server = Server("example", (), settings=library.get("example"), runtime_metadata=observed,
                    identity=library.identity("example"))
    assert runner.model_metadata("example") == {}
    runner.servers["example"] = server
    assert runner.model_metadata("example") == {}
    server.ready = True
    assert runner.model_metadata("example") == observed
    server.process = SimpleNamespace(returncode=1)
    assert runner.model_metadata("example") == {}
    server.process = None
    server.stopping = object()
    assert runner.model_metadata("example") == {}
    server.stopping = None
    changed = {**settings, "flags": {**settings["flags"], "chat-template": "changed"}}
    library.put("example", changed)
    assert runner.model_metadata("example") == {}
    library.remove("example")
    library.put("example", settings)
    assert runner.model_metadata("example") == {}
    library.db.close()


def test_loaded_metadata_reaches_catalogues_and_identity_without_reinspection(fake_llama_server, monkeypatch):
    observed = {"chat_template_caps": {"supports_reasoning_effort": True,
                "reasoning_effort_levels": ["brief", "future"], "supports_tools": False, "supports_tool_calls": False}}
    calls = []

    async def discover(self, client, server):
        calls.append(server.model)
        return observed

    monkeypatch.setattr(LlamaCpp, "runtime_metadata", discover)
    with gateway(fake_llama_server, {"example": model("example.gguf", 0)}) as client:
        owner = add_user(client, "owner", "admin")
        assert "chat_template_caps" not in client.get("/loadcoach/models", headers=owner).json()["models"][0]
        assert calls == []
        requested = {"model": "example", "reasoning_effort": "future", "chat_template_kwargs": {"future_setting": True}}
        answer = client.post("/v1/chat/completions", headers=owner, json=requested)
        assert answer.status_code == 200, answer.text
        assert json.loads(answer.json()["body"]) == requested
        row = client.get("/loadcoach/models", headers=owner).json()["models"][0]
        assert row["chat_template_caps"] == observed["chat_template_caps"]
        public = client.get("/v1/models", headers=owner).json()["data"][0]
        assert public["chat_template_caps"] == observed["chat_template_caps"]
        identity = client.get("/loadcoach/me", headers=owner).json()
        assert identity["chat"]["models"]["example"]["chat_template_caps"] == observed["chat_template_caps"]
        assert calls == ["example"]


def test_model_options_suggest_reported_levels_and_escape_them():
    pages = Path(__file__).resolve().parents[1] / "weightroom" / "pages"
    environment = Environment(loader=FileSystemLoader(pages), autoescape=True)
    environment.filters["shown"] = str
    template = environment.get_template("engines/llama_cpp/model_options.html")
    rendered = template.render(model={"chat_template_caps": {"reasoning_effort_levels": ["brief", "<future>"]}},
        showing={"flags": {}, "adapters": {}}, parallel={"enabled": False, "count": 2, "context": 2048},
        global_flags=[], unusable_flags=[], every={"ours": [], "flags": [], "why": ""}, known={"defaults": {}},
        part_choice=lambda *args: "", card=lambda *args, **kwargs: "", suggestions=[], selected_node="compute")
    assert 'list="reasoning-effort-levels"' in rendered
    rendered = environment.get_template("model_reasoning.html").render(
        model={"chat_template_caps": {"reasoning_effort_levels": ["brief", "<future>"]}}, selected_node="compute")
    assert '<option value="brief">' in rendered
    assert '&lt;future&gt;' in rendered and '<future>' not in rendered
    assert 'include "model_reasoning.html"' in (pages / "model_state.html").read_text()
