"""C09 extraction contracts exercised with isolated mock engines after test clearance.

Native integration uses openWeightGym's fake server only. No installed engine or model is updated.
"""
import asyncio
import hashlib
import json
import re
import tomllib

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from engines import catalogue, checked_installations, engine_for
from engines.llama_cpp.adapter import command_line
from loadcoach.config import Config, load_config
from loadcoach.models import Models, base, check_model, kin_base, memory_base
from loadcoach.runner import Runner, Ticket, Refusal
from loadcoach.probes import Probes
from loadcoach.cli import as_toml
from loadcoach import statement
from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.app import Settings as FrontConfig, build_app as build_front
from front_helpers import machine_key
from test_app import add_user, fake_llama_server, gateway, in_an_empty_folder, model  # noqa: F401
from test_two_machines import Two
from test_weightroom import desk, sign_in  # noqa: F401


def test_implicit_llama_selection_preserves_fingerprints_measurements_and_file_revision(tmp_path):
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(b"fingerprint fixture")
    settings = {"gpus": 1, "flags": {"model": str(weights), "ctx-size": 2048}}
    library = Models(str(tmp_path / "models.db"))
    library.put("before", settings)
    stat = weights.stat()
    library.keep_hash(str(weights), hashlib.sha256(weights.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime)
    identity, fingerprint = library.identity("before"), library.fingerprint(library.get("before"), "before")
    library.measured("before", settings, [512])
    explicit = {"engine": "llama_cpp", **settings}
    assert base(settings) == base(explicit)
    assert kin_base(settings) == kin_base(explicit)
    assert memory_base(settings) == memory_base(explicit)
    library.put("before", explicit)
    assert library.identity("before") == identity and library.file_revision("before") == 0
    assert library.fingerprint(library.get("before"), "before") == fingerprint
    assert library.measure("before")["mib"] == [512]
    assert library.stored("before")["engine"] == "llama_cpp"
    exported = tomllib.loads(as_toml("before", explicit))
    assert exported["models"]["before"] == explicit
    library.db.close()


def test_engine_registry_is_explicit_and_rejects_unimplemented_engines():
    assert engine_for({}).id == engine_for({"engine": "llama_cpp"}).id == "llama_cpp"
    assert engine_for("strata").installation(Config()).enabled is False
    for invalid in ("missing", None, ["llama_cpp"]):
        with pytest.raises(ValueError, match="Unknown inference engine"):
            engine_for({"engine": invalid})
    with pytest.raises(ValueError, match="absolute path"):
        check_model("example", {"engine": "strata", "flags": {"model": "model.gguf", "ctx-size": 2048}}, ())


def test_engine_node_settings_validate_without_executing_programs(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Node settings inspection must not execute a program")
    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    config_file = tmp_path / "loadcoach.toml"
    config_file.write_text('gpus = []\n[engines.llama_cpp]\nenabled = true\nexecutable = "/missing/llama-server"\n')
    configured = load_config(config_file)
    assert configured.llama_server == "/missing/llama-server"
    entry, strata, image, audio, whisper = catalogue(configured)
    assert image["enabled"] is False and image["capabilities"]["image_generation"] is True
    assert audio["enabled"] is False and audio["capabilities"]["audio"] and not audio["capabilities"]["chat"]
    assert strata["enabled"] is False and strata["available"] is False
    assert entry["id"] == "llama_cpp" and entry["available"] is False
    assert entry["schema"]["create_template"].endswith("create_fields.html")
    checked_installations({"strata": {}}, "llama-server")
    for invalid in ({"llama_cpp": {"enabled": "yes"}}, {"llama_cpp": {"shell": "true"}}, {"strata": {"hooks": "unsafe"}}):
        with pytest.raises(ValueError):
            checked_installations(invalid, "llama-server")
    config_file.write_text('gpus = []\nllama_server = "/old/server"\n[engines.llama_cpp]\nexecutable = "/new/server"\n')
    with pytest.raises(SystemExit, match="conflicts"):
        load_config(config_file)


def test_engine_argv_and_environment_preserve_llama_cpu_gpu_and_lora_contracts():
    configured = Config(llama_server="/private/llama-server")
    settings = {"flags": {"model": "model.gguf", "ctx-size": 2048, "flash-attn": "on"},
                "adapters": {"terse": {"file": "terse.gguf", "scale": .5}}}
    launched = engine_for(settings).launch(configured, 54321, settings, ("CUDA0", "CUDA1"))
    assert launched.command == command_line(configured.llama_server, 54321, settings["flags"], ("CUDA0", "CUDA1"), settings["adapters"])
    assert launched.environment["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert launched.environment["GGML_CUDA_ENABLE_UNIFIED_MEMORY"] == "0"
    assert launched.environment["GGML_CUDA_DISABLE_GRAPHS"] == "1"
    cpu = engine_for().launch(configured, 54321, {"flags": {"model": "model.gguf", "ctx-size": 512}}, ())
    assert cpu.command[5:7] == ["--device", "none"]


def test_disabled_engine_refuses_before_queueing_or_launching(tmp_path):
    library = Models(str(tmp_path / "models.db"))
    library.put("example", {"gpus": 0, "flags": {"model": "missing.gguf", "ctx-size": 2048}})
    runner = Runner(Config(engines={"llama_cpp": {"enabled": False}}), None, models=library)
    with pytest.raises(Refusal, match="disabled"):
        asyncio.run(runner.acquire(Ticket("example", "owner", "test", "normal")))
    with pytest.raises(ValueError, match="unavailable"):
        Probes(runner, lambda: False, lambda _: True).subject("example")
    assert runner.waiting == [] and runner.servers == {}
    library.db.close()


def test_engine_version_cache_is_scoped_to_installation(monkeypatch, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    configured = Config(llama_server=str(first))
    runner = Runner(configured, None)
    calls = []
    async def version(executable):
        calls.append(executable)
        return executable
    monkeypatch.setattr(engine_for(), "version", version)
    async def inspect():
        assert await runner.version() == str(first)
        assert await runner.version({"engine": "llama_cpp"}) == str(first)
        configured.llama_server = str(second)
        assert await runner.version() == str(second)
    asyncio.run(inspect())
    assert calls == [str(first), str(second)]
    runner.models.db.close()


def test_engine_catalogue_and_explicit_model_api_keep_permissions(fake_llama_server):
    with gateway(fake_llama_server, {"example": model("missing.gguf", 0)}) as caller:
        owner = add_user(caller, "owner", "admin")
        guest = add_user(caller, "guest", "plain", by=owner)
        assert caller.get("/loadcoach/engines", headers=guest).status_code == 403
        entry, strata, image, audio, whisper = caller.get("/loadcoach/engines", headers=owner).json()["engines"]
        assert image["enabled"] is False
        assert strata["enabled"] is False
        assert entry["id"] == "llama_cpp"
        response = caller.post("/loadcoach/models/explicit", headers=owner,
                               json={"engine": "llama_cpp", "gpus": 0, "flags": {"model": "missing.gguf", "ctx-size": 2048}})
        assert response.status_code == 201
        assert caller.post("/loadcoach/models/invalid", headers=owner,
                           json={"engine": "strata", "gpus": 0, "flags": {"model": "missing.gguf", "ctx-size": 2048}}).status_code == 400


def test_console_model_and_node_forms_use_engine_schema_and_keep_changes_pending(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    page = browser.get("/models?node=compute&complete=1").text
    assert 'name="engine"' in page and 'value="llama_cpp"' in page
    made = browser.post("/models?node=compute", data={"engine": "llama_cpp", "name": "explicit", "file": "missing.gguf", "ctx": "2048", "cards": "0"})
    assert made.status_code == 200 and made.url.params.get("node") == "compute"
    # The engine is fixed on the page; generation defaults apply on every node, and the page says which node it configures.
    assert '<input type="hidden" name="engine" value="llama_cpp">' in made.text and 'Generation defaults' in made.text
    assert 'class="fw-semibold text-reset" aria-current="true">compute</a>' in made.text  # the node matrix's column for compute
    assert '?node=compute&amp;part=state' in made.text
    listing = browser.get("/models?node=compute&part=list").text
    assert '/models?part=list&amp;node=compute' in listing
    assert '/models/explicit?node=compute' in listing
    # desk omits the worker lifespan; the GPU endpoint refreshes its settings report.
    assert browser.post("/compute-nodes/gpus", data={"machine": "compute"}).status_code == 200
    saved = browser.post("/compute-nodes/engines", data={"machine": "compute", "engine": "llama_cpp", "enabled": "on", "executable": "/pending/server"})
    assert saved.status_code == 200, re.findall(r'<div class="alert[^>]+>(.*?)</div>', saved.text)
    assert "pending restart" in saved.text.lower()
    settings = browser.get("/compute-nodes?machine=compute").text
    assert '/pending/server' in settings and "Changes pending restart" in settings
    # Unknown node selection must not write to the primary instance.
    refused = browser.post("/models?node=missing", data={"engine": "llama_cpp", "name": "wrong-node", "file": "wrong.gguf", "ctx": "512", "cards": "0"})
    assert refused.status_code >= 400
    library = Models(str(tmp_path / "loadcoach.db"))
    assert library.stored("wrong-node") is None
    library.db.close()


def test_engine_changes_wait_for_restart_and_file_reset_keeps_namespace_path(fake_llama_server, tmp_path):
    database = str(tmp_path / "worker.db")
    installation = {"llama_cpp": {"enabled": True, "executable": fake_llama_server}}
    file = {"engines": installation}
    with gateway(fake_llama_server, {"example": model("example.gguf", 0)},
                 database=database, engines=installation, file=file) as caller:
        owner = add_user(caller, "owner", "admin")
        first = caller.post("/v1/chat/completions", headers=owner, json={"model": "example"})
        assert first.status_code == 200
        disabled = {"llama_cpp": {"enabled": False, "executable": fake_llama_server}}
        saved = caller.post("/loadcoach/settings/loadcoach", headers=owner,
                            json={"engines": disabled, "llama_server": fake_llama_server})
        assert saved.status_code == 200, saved.text
        assert caller.get("/loadcoach/engines", headers=owner).json()["engines"][0]["enabled"] is True
        second = caller.post("/v1/chat/completions", headers=owner, json={"model": "example"})
        assert second.status_code == 200 and second.json()["pid"] == first.json()["pid"]
        config_file = tmp_path / "loadcoach.toml"
        config_file.write_text(f'gpus = []\ndatabase = {json.dumps(database)}\n[engines.llama_cpp]\nenabled = true\nexecutable = {json.dumps(fake_llama_server)}\n')
        restarted = load_config(config_file)
        assert restarted.engines["llama_cpp"]["enabled"] is False
        reset = caller.post("/loadcoach/settings/loadcoach", headers=owner,
                            json={"engines": None, "llama_server": None})
        assert reset.status_code == 200, reset.text
        restored = load_config(config_file)
        assert restored.engines["llama_cpp"]["enabled"] is True
        assert restored.llama_server == fake_llama_server


def test_disabled_engine_is_visible_to_managers_but_unavailable_for_calls(fake_llama_server):
    with gateway(fake_llama_server, {"example": model("example.gguf", 0)},
                 engines={"llama_cpp": {"enabled": False}}) as caller:
        owner = add_user(caller, "owner", "admin")
        assert caller.get("/v1/models", headers=owner).json()["data"] == []
        entry, *_ = caller.get("/loadcoach/engines", headers=owner).json()["engines"]
        assert entry["enabled"] is False and "Disabled" in entry["why"]
        assert caller.get("/loadcoach/models", headers=owner).json()["models"][0]["engine"] == "llama_cpp"
        assert caller.get("/loadcoach/flags", headers=owner).status_code == 503
        response = caller.post("/v1/chat/completions", headers=owner, json={"model": "example"})
        # Disabled locations leave the caller-visible catalogue, preserving the usual
        # not-found response rather than leaking an unavailable model through front.
        assert response.status_code == 404
        assert caller.get("/loadcoach/status", headers=owner).json()["loaded"] == []


def test_named_engine_management_is_signed_scoped_and_never_falls_back(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {
        "operator": Group(can=frozenset({"manage-models", "manage-settings"})),
        "guest": Group(can=frozenset({"load"})),
    })
    users.add("owner", "operator")
    users.add("guest", "guest")
    owner = {"authorization": "Bearer " + users.new_key("owner")}
    guest = {"authorization": "Bearer " + users.new_key("guest")}
    primary_key, rig_key = machine_key(tmp_path), str(tmp_path / "rig.key")
    statement.replace_key(rig_key, "r" * 40)
    writes = []

    def worker(name, key):
        async def answer(request):
            path = request.url.path
            if path == "/loadcoach/engines":
                return JSONResponse({"engines": catalogue(Config()), "machine": name})
            if path == "/loadcoach/models":
                return JSONResponse({"models": []})
            if path == "/loadcoach/status":
                return JSONResponse({"memory": {}, "loaded": [], "running": [], "waiting": [], "holds": [], "busy": 0})
            if path == "/v1/models":
                return JSONResponse({"object": "list", "data": []})
            if path == "/loadcoach/facts":
                return JSONResponse({"environment": {}, "models": {}})
            if path.startswith("/loadcoach/models/") and request.method == "POST":
                writes.append((name, path, request.url.query, json.loads(await request.body())))
                return JSONResponse({"machine": name}, status_code=201)
            return JSONResponse({"error": {"message": "Not found"}}, status_code=404)
        return statement.Statements(Starlette(routes=[Route("/{path:path}", answer, methods=["GET", "POST"])]), key)

    class Wire(Two):
        offline = False

        async def handle_async_request(self, request):
            if self.offline and request.url.port == 9442:
                self.calls.append((9442, request.url.path))
                raise httpx.ConnectError("rig unavailable")
            return await super().handle_async_request(request)

    wire = Wire(worker("compute", primary_key), worker("rig", rig_key))
    settings = FrontConfig(compute="compute", data=str(tmp_path), machine_key_file=primary_key,
                             machines={"rig": {"compute": "http://compute:9442", "machine_key_file": rig_key}},
                             machines_poll_seconds=3600)
    with TestClient(build_front(settings, httpx.AsyncClient(transport=wire), users=users)) as front:
        target = "/turnstile/machines/rig"
        assert front.get(target + "/engines", headers=guest).status_code == 403
        shown = front.get(target + "/engines", headers=owner)
        assert shown.json()["machine"] == "rig"
        body = {"engine": "llama_cpp", "gpus": 0, "flags": {"model": "rig.gguf", "ctx-size": 512}}
        response = front.post(target + "/model-api/models/local?new=1", headers=owner, json=body)
        assert response.status_code == 201 and writes == [("rig", "/loadcoach/models/local", "new=1", body)]
        assert front.post(target + "/model-api/models/denied", headers=guest, json=body).status_code == 403
        assert front.post(target + "/model-api/clear", headers=owner, json={}).status_code == 405
        assert front.get("/turnstile/machines/missing/engines", headers=owner).status_code == 404
        wire.calls.clear()
        wire.offline = True
        retained = front.get(target + "/engines", headers=owner)
        assert retained.status_code == 200
        assert retained.json()["engines"] == shown.json()["engines"]
        assert front.post(target + "/model-api/models/offline", headers=owner, json=body).status_code == 503
        assert len(writes) == 1
        assert all(port == 9442 for port, path in wire.calls if path in ("/loadcoach/engines", "/loadcoach/models/offline"))
    users.db.close()
