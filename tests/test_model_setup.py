"""C05 checks use metadata fixtures and fake inference; no real model or GPU is loaded."""

import copy
import json
import html
import re
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from db import Database
from db.transfer import copy_store
from loadcoach.config import Config
from loadcoach.model_setup import preview
from loadcoach.models import Models, flags_in
from loadcoach.runner import Runner, Server, Ticket
from test_app import add_user, alive, fake_llama_server, gateway, in_an_empty_folder, model, wait_for_status  # noqa: F401
from test_postgresql import postgres  # noqa: F401
from test_weightroom import desk, gguf, sign_in  # noqa: F401


HELP = """----- setup test params -----
--host HOST                   bind address
--port N                      listener
--device DEVICES              devices
--model FILE                  model file
--mmproj FILE                 projector file
--ctx-size N                  context tokens
--parallel N                  concurrent requests
--kv-unified                  shared pool
--no-kv-unified               fixed pool
--kv-unified-per-slot N        per request cap
--flash-attn [on|off|auto]     attention
--threads N                   CPU threads
--temperature N               temperature
--reasoning [on|off|auto]      reasoning mode
--split-mode MODE             split
--tensor-split N,N            weights
--lora-scaled FILE:SCALE       adapters
"""
SUPPORTED = flags_in(HELP)


def configuration(settings, order=10):
    return {"settings": settings, "order": order}


@pytest.fixture
def native(tmp_path, fake_llama_server):
    executable = tmp_path / "setup-native"
    executable.write_text(f"#!{sys.executable}\nimport os,sys\nif sys.argv[1:]==['--help']:\n print({HELP!r})\nelse:\n os.execv({fake_llama_server!r}, [{fake_llama_server!r}, *sys.argv[1:]])\n")
    executable.chmod(0o755)
    return str(executable)


SEES = "{% if content.type == 'image' %}<|image_pad|>{% endif %}"  # a chat template that handles images


def fixture_model(tmp_path):
    path = tmp_path / "model.gguf"
    gguf(path, general__architecture="qwen", general__name="Example", qwen__context_length=8192, tokenizer__chat_template=SEES)
    return model(str(path), 0)


def rig(tmp_path, **config):
    library = Models(tmp_path / "models.db", defaults={"threads": 4})
    settings = fixture_model(tmp_path)
    library.put("example", settings)
    runner = Runner(Config(gpus=("GPU0",), **config), None, models=library)
    runner.seen = {"cards": {0: {"total": 16000, "used": 0}}, "pids": {}}
    return library, runner, settings


def test_preview_is_read_only_and_shows_effective_settings_and_metadata(tmp_path):
    library, runner, settings = rig(tmp_path)
    projector = tmp_path / "mmproj.gguf"
    gguf(projector, general__architecture="clip", general__name="Example")
    candidate = configuration({**settings, "flags": {**settings["flags"], "mmproj": str(projector), "ctx-size": 16384, "threads": None}})
    before = library.db.total_changes
    report = preview("example", candidate, library, runner, SUPPORTED)
    assert not report["errors"] and report["token"]
    assert "threads" not in report["effective"]["flags"]
    assert report["placement"]["request_context"] == 16384
    assert report["artifacts"][1]["compatibility"] == "metadata names agree; runtime unqualified"
    assert any("trained context" in w for w in report["warnings"])
    assert "--device none" in report["command"] and "--mmproj" in report["command"]
    assert next(c for c in report["changes"] if c["field"] == "flags.ctx-size")["after"] == 16384
    suppressed = next(c for c in report["changes"] if c["field"] == "flags.threads")
    assert not suppressed["before_set"] and suppressed["after_set"] and suppressed["after"] is None
    assert library.db.total_changes == before and library.stored("example") == settings
    assert not runner.servers and not runner.running and not runner.waiting
    assert "pending" in report["qualification"]


def test_missing_projector_warns_only_for_a_template_that_handles_images(tmp_path):
    library, runner, settings = rig(tmp_path)
    seeing = preview("example", configuration(settings), library, runner, SUPPORTED)
    assert any("projector is selected" in w for w in seeing["warnings"])
    gguf(settings["flags"]["model"], general__architecture="qwen", general__name="Example", qwen__context_length=8192)
    text_only = preview("example", configuration(settings), library, runner, SUPPORTED)
    assert not any("projector is selected" in w for w in text_only["warnings"])


@pytest.mark.parametrize("changed, expected", [
    ({"flags": {"ctx-size": 0}}, "positive whole"),
    ({"flags": {"ctx-size": True}}, "positive whole"),
    ({"flags": {"threads": float("inf")}}, "finite"),
    ({"flags": {"unknown-engine-flag": 1}}, "not advertised"),
    ({"flags": {"threads": True}}, "requires a value"),
    ({"flags": {"kv-unified": "yes"}}, "true or false"),
    ({"flags": {"host": "0.0.0.0"}}, "must not set"),
    ({"flags": {"ctx-size": 100}, "min_context": 101}, "min_context"),
    ({"gpus": ["disabled"]}, "list of cards"),
])
def test_invalid_configuration_cannot_be_validated(tmp_path, changed, expected):
    library, runner, settings = rig(tmp_path)
    candidate = {**settings, **changed, "flags": {**settings["flags"], **changed.get("flags", {})}}
    report = preview("example", configuration(candidate), library, runner, SUPPORTED)
    assert any(expected in message for message in report["errors"])
    assert library.stored("example") == settings


def test_projectors_are_checked_by_metadata_not_basename_and_missing_files_block(tmp_path):
    library, runner, settings = rig(tmp_path)
    projector = tmp_path / "mmproj.gguf"
    gguf(projector, general__architecture="clip", general__name="Different model")
    candidate = configuration({**settings, "flags": {**settings["flags"], "mmproj": str(projector)}})
    report = preview("example", candidate, library, runner, SUPPORTED)
    assert not report["errors"] and any("not interchangeable" in w for w in report["warnings"])
    projector.unlink()
    missing = preview("example", candidate, library, runner, SUPPORTED)
    assert any("mmproj" in e for e in missing["errors"]) and missing["token"] != report["token"]
    unavailable = preview("example", configuration(settings), library, runner, [], "not installed")
    assert any("not installed" in e for e in unavailable["errors"])


def test_required_editor_fields_cannot_disappear_into_inherited_defaults(tmp_path):
    library, runner, settings = rig(tmp_path)
    library.defaults.update(settings["flags"])
    report = preview("example", configuration({"gpus": 0, "flags": {}}), library, runner, SUPPORTED)
    assert any("explicitly" in error for error in report["errors"])


def test_engine_help_cache_refreshes_for_an_executable_found_on_path(native, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    settings = fixture_model(tmp_path)
    with gateway("setup-native", {"example": settings}) as caller:
        target = configuration({**settings, "flags": {**settings["flags"], "threads": 4}})
        assert not call(caller, "preview", configuration=target).json()["errors"]
        from pathlib import Path
        executable = Path(native)
        executable.write_text(executable.read_text().replace("--threads N", "--other-threads N"))
        report = call(caller, "preview", configuration=target).json()
        assert any("--threads is not advertised" in error for error in report["errors"])


@pytest.mark.parametrize("protection", ["active", "held", "locked", "loading"])
def test_preview_never_evicts_a_protected_loaded_configuration(tmp_path, protection):
    library, runner, settings = rig(tmp_path)
    settings["gpus"] = 1
    library.put("example", settings)
    old = Server("example", ("GPU0",), settings, ready=True, mib=[1000])
    runner.servers["example"] = old
    if protection == "active":
        runner.running.append(Ticket("example", "owner", "here", "normal", server=old))
    elif protection == "held":
        runner.holds["example", "voice"] = time.monotonic() + 60
    elif protection == "locked":
        library.set_locked("example", True)
    else:
        old.ready = False
    changed = {**settings, "flags": {**settings["flags"], "ctx-size": 4096}}
    report = preview("example", configuration(changed), library, runner, SUPPORTED)
    assert report["placement"]["state"] == "deferred / unavailable"
    assert report["placement"]["stays"] == ["example"] and not report["placement"]["leaves"]
    assert report["placement"]["protected"] == ["example"] and not report["command"]
    assert runner.servers["example"] is old and library.stored("example") == settings


def test_placement_explains_reuse_and_idle_eviction_using_existing_scheduler(tmp_path):
    library, runner, settings = rig(tmp_path)
    settings["gpus"] = 1
    library.put("example", settings)
    other = model("other.gguf", 1)
    library.put("other", other)
    resident = Server("other", ("GPU0",), other, ready=True, mib=[13000])
    runner.servers["other"] = resident
    report = preview("example", configuration(settings), library, runner, SUPPORTED)
    assert report["placement"]["leaves"] == ["other"] and report["placement"]["cards"] == ["GPU0"]
    runner.holds["other", "voice"] = time.monotonic() + 60
    assert preview("example", configuration(settings), library, runner, SUPPORTED)["placement"]["state"] == "deferred / unavailable"
    runner.servers.clear()
    runner.servers["example"] = Server("example", ("GPU0",), library.get("example"), ready=True, mib=[1000])
    assert preview("example", configuration(settings), library, runner, SUPPORTED)["placement"]["state"] == "reuse loaded server"


@pytest.mark.parametrize("protection", ["active", "held", "locked"])
def test_scheduler_protects_a_second_server_when_an_edit_makes_it_replacement_kin(tmp_path, protection):
    library, runner, original = rig(tmp_path)
    desired = {**original, "gpus": 1}
    library.put("other", desired)
    runner.servers["example"] = Server("example", (), library.get("example"), ready=True)
    other = runner.servers["other"] = Server("other", ("GPU0",), library.get("other"), ready=True, mib=[1000])
    if protection == "active":
        runner.running.append(Ticket("other", "owner", "here", "normal", server=other))
    elif protection == "held":
        runner.holds["other", "voice"] = time.monotonic() + 60
    else:
        library.set_locked("other", True)
    report = preview("example", configuration(desired), library, runner, SUPPORTED)
    assert report["placement"]["state"] == "deferred / unavailable" and not report["placement"]["leaves"]
    library.put("example", desired)  # the same placement check after an actual saved edit
    assert runner.fit("example", library.get("example"), set()) is None
    assert runner.servers["other"] is other


def change(library, name, target, expected, undo=""):
    library.change_setup(name, target, expected, undo, identity=library.identity(name), revision=library.file_revision(name))


def repository_contract(source, tmp_path):
    library = Models(source)
    settings = fixture_model(tmp_path)
    library.put("example", settings)
    before = library.setup("example")
    candidate = configuration({**settings, "flags": {**settings["flags"], "ctx-size": 4096}}, -2)
    library.save_preset("example", "Daily", candidate)
    change(library, "example", candidate, before)
    receipt = library.setup_undo("example")
    assert receipt["before"] == before and library.place("example") == -2
    library.rename("example", "renamed")
    assert library.presets("renamed")[0]["configuration"] == candidate
    library.db.close()
    library = Models(source)
    change(library, "renamed", before, candidate, receipt["receipt"])
    assert library.setup("renamed") == before and library.setup_undo("renamed") is None
    with pytest.raises(ValueError, match="undo"):
        change(library, "renamed", candidate, before, receipt["receipt"])
    change(library, "renamed", candidate, before)
    library.put("renamed", {**settings, "alone": True})
    with pytest.raises(ValueError, match="changed"):
        change(library, "renamed", before, candidate, library.setup_undo("renamed")["receipt"])
    # Failure between the row update and receipt write rolls the entire operation back.
    from db.connection import Connection
    from unittest.mock import patch
    execute = Connection.execute
    current, receipt = library.setup("renamed"), library.setup_undo("renamed")
    def fail_receipt(connection, statement, parameters=()):
        if connection is library.db and "INSERT INTO model_setup_undo" in statement:
            raise RuntimeError("fixture receipt failure")
        return execute(connection, statement, parameters)
    with patch.object(Connection, "execute", fail_receipt), pytest.raises(RuntimeError, match="fixture receipt failure"):
        change(library, "renamed", before, current)
    assert library.setup("renamed") == current and library.setup_undo("renamed") == receipt
    return library


def test_shared_preset_survives_recreated_entry_but_undo_stays_identity_specific(tmp_path):
    library = repository_contract(tmp_path / "models.db", tmp_path)
    library.remove("renamed")
    library.put("renamed", fixture_model(tmp_path))
    assert library.presets("renamed")[0]['name'] == 'Daily' and library.setup_undo("renamed") is None
    library.db.close()


def test_postgresql_preset_undo_and_verified_transfer(postgres, tmp_path):
    library = repository_contract(postgres, tmp_path)
    expected = library.setup_undo("renamed")
    library.db.close()
    target = Database.sqlite(tmp_path / "copy.db")
    assert copy_store(postgres, target)["state"] == "verified"
    restored = Models(target)
    assert restored.setup_undo("renamed") == expected and restored.presets("renamed")[0]["name"] == "Daily"
    restored.db.close()


def test_atomic_change_rejects_stale_identity_and_preserves_file_revisions(tmp_path):
    library, _, settings = rig(tmp_path)
    before = library.setup("example")
    identity, revision = library.identity("example"), library.file_revision("example")
    other = tmp_path / "other.gguf"
    other.write_bytes((tmp_path / "model.gguf").read_bytes())
    candidate = configuration({**settings, "flags": {**settings["flags"], "model": str(other)}})
    change(library, "example", candidate, before)
    assert library.file_revision("example") == revision + 1
    change(library, "example", before, candidate, library.setup_undo("example")["receipt"])
    assert library.file_revision("example") == revision + 2
    library.remove("example"); library.put("example", settings)
    with pytest.raises(ValueError, match="changed"):
        library.change_setup("example", candidate, before, identity=identity, revision=revision)
    assert library.setup("example") == before and library.setup_undo("example") is None


def call(caller, action, **body):
    return caller.post("/loadcoach/model-setup", json={"name": "example", "action": action, **body})


def test_api_review_apply_preset_undo_and_concurrent_edit_guard(native, tmp_path):
    settings = fixture_model(tmp_path)
    with gateway(native, {"example": settings}) as caller:
        target = configuration({**settings, "flags": {**settings["flags"], "ctx-size": 4096}})
        review = call(caller, "preview", configuration=target).json()
        assert not review["errors"]
        assert call(caller, "apply", configuration=target).status_code == 409
        assert call(caller, "apply", configuration=target, token=review["token"]).status_code == 400
        assert call(caller, "save-preset", configuration=target, preset="Daily", token=review["token"], acknowledge=True).status_code == 200
        assert call(caller, "save-preset", configuration=target, preset="Daily", token=review["token"], acknowledge=True).status_code == 409
        assert call(caller, "apply", preset="Daily", token=review["token"], acknowledge=True).status_code == 200
        state = caller.get("/loadcoach/model-setup?name=example").json()
        undo = state["undo"]["receipt"]
        restored = call(caller, "preview", undo=undo).json()
        assert state["can_undo"] and call(caller, "apply", undo=undo, token=restored["token"], acknowledge=True).status_code == 200
        assert call(caller, "preview", undo=undo).status_code == 409
        stale = call(caller, "preview", preset="Daily").json()
        caller.post("/loadcoach/models/example", json={**settings, "alone": True})
        assert call(caller, "apply", preset="Daily", token=stale["token"], acknowledge=True).status_code == 409
        assert call(caller, "delete-preset", preset="Daily").status_code == 200
        assert call(caller, "delete-preset", preset="Daily").status_code == 404


@pytest.mark.parametrize("mutation", ["file", "defaults", "engine", "preset"])
def test_review_expires_when_its_inputs_change(native, tmp_path, mutation):
    settings = fixture_model(tmp_path)
    with gateway(native, {"example": settings}) as caller:
        target = configuration({**settings, "flags": {**settings["flags"], "ctx-size": 4096}})
        reviewed = call(caller, "preview", configuration=target).json()
        assert call(caller, "save-preset", preset="Daily", configuration=target, token=reviewed["token"], acknowledge=True).status_code == 200
        if mutation == "file":
            with open(settings["flags"]["model"], "ab") as stream:
                stream.write(b"changed")
        elif mutation == "engine":
            with open(native, "a") as stream:
                stream.write("\n# newer engine\n")
        elif mutation == "defaults":
            response = caller.post("/loadcoach/settings/loadcoach", json={"model_defaults": {"threads": 2}})
            assert response.status_code == 200, response.text
        else:
            altered = copy.deepcopy(target); altered["settings"]["flags"]["ctx-size"] = 1024
            new = call(caller, "preview", configuration=altered).json()
            assert call(caller, "save-preset", preset="Daily", configuration=altered, token=new["token"], acknowledge=True, replace=True).status_code == 200
        assert call(caller, "apply", preset="Daily", token=reviewed["token"], acknowledge=True).status_code == 409


def test_setup_api_requires_manage_models(native, tmp_path):
    with gateway(native, {"example": fixture_model(tmp_path)}) as caller:
        admin = add_user(caller, "owner", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        assert caller.get("/loadcoach/model-setup?name=example", headers=plain).status_code == 403
        assert caller.post("/loadcoach/model-setup", headers=plain, json={"action": "delete-preset", "name": "example", "preset": "Daily"}).status_code == 403


def test_apply_during_a_request_preserves_work_and_reloads_only_on_later_request(native, tmp_path):
    settings = fixture_model(tmp_path)
    with gateway(native, {"example": settings}) as caller, ThreadPoolExecutor() as work:
        first = caller.post("/v1/chat/completions", json={"model": "example"}).json()
        slow = work.submit(caller.post, "/v1/chat/completions", json={"model": "example", "sleep": .5})
        wait_for_status(caller, lambda status: bool(status["running"]))
        target = configuration({**settings, "flags": {**settings["flags"], "ctx-size": 4096}})
        reviewed = call(caller, "preview", configuration=target).json()
        assert reviewed["placement"]["protected"] == ["example"]
        assert call(caller, "apply", configuration=target, token=reviewed["token"], acknowledge=True).status_code == 200
        assert alive(first["pid"]) and slow.result().json()["pid"] == first["pid"]
        assert alive(first["pid"])  # applying settings alone does not load or unload anything
        following = caller.post("/v1/chat/completions", json={"model": "example"}).json()
        assert following["pid"] != first["pid"]
        assert following["arguments"][following["arguments"].index("--ctx-size") + 1] == "4096"


def test_api_handles_malformed_and_unvalidated_configurations_without_mutation(native, tmp_path):
    settings = fixture_model(tmp_path)
    with gateway(native, {"example": settings}) as caller:
        for candidate in ([], {"settings": []}, configuration({**settings, "flags": {"ctx-size": 0}})):
            response = call(caller, "preview", configuration=candidate)
            assert response.status_code == 400 or response.status_code == 200 and response.json()["errors"]
        invalid = configuration({**settings, "flags": {**settings["flags"], "threads": float("nan")}})
        response = caller.post("/loadcoach/model-setup", content=json.dumps({"name": "example", "action": "preview", "configuration": invalid}), headers={"content-type": "application/json"})
        assert response.status_code == 400 and "finite" in response.text
        for token in ("♥", "not-a-token", [], 5):
            assert call(caller, "apply", configuration=configuration(settings), token=token, acknowledge=True).status_code == 409
        assert caller.get("/loadcoach/model-setup?name=example").json()["undo"] is None


@pytest.fixture
def setup_desk(native, monkeypatch, request):
    from test_weightroom import build_loadcoach
    monkeypatch.setattr("test_weightroom.build_loadcoach", lambda config, *a: build_loadcoach(replace(config, llama_server=native), *a))
    return request.getfixturevalue("desk")


def test_existing_editor_reviews_saves_applies_and_undoes_presets(setup_desk, tmp_path):
    browser, _, _ = setup_desk
    settings = fixture_model(tmp_path)
    library = Models(str(tmp_path / "loadcoach.db"))
    library.put("example", settings)
    sign_in(browser, "jordan")
    reviewed = browser.post("/models/example", data={"do": "setup-preview", "file": settings["flags"]["model"], "ctx": "4096", "cards": "0"})
    def hidden(response):
        return {name: html.unescape(value) for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text)}
    fields = hidden(reviewed)
    assert "Configuration preview" in reviewed.text and fields["token"] and library.stored("example") == settings
    assert browser.post("/models/example", data={**fields, "do": "save-preset", "preset": "Daily", "acknowledge": "yes"}).status_code == 200
    assert len(library.presets("example")) == 1 and library.stored("example") == settings
    assert "Undo is available" in browser.post("/models/example", data={**fields, "do": "apply-reviewed", "acknowledge": "yes"}).text
    undo = library.setup_undo("example")["receipt"]
    reviewed = browser.post("/models/example", data={"do": "preview-undo", "undo": undo})
    fields = hidden(reviewed)
    assert "Configuration restored" in browser.post("/models/example", data={**fields, "do": "apply-reviewed", "acknowledge": "yes"}).text
    assert library.stored("example") == settings and library.setup_undo("example") is None
    assert "Preset deleted" in browser.post("/models/example", data={"do": "delete-preset", "preset": "Daily"}).text
    assert browser.post("/models/example", data={"do": "delete-preset", "preset": "Daily"}, headers={"Origin": "https://elsewhere.test"}).status_code == 403
    library.db.close()


@pytest.mark.parametrize('protection', ['held', 'locked', 'active'])
def test_reviewed_apply_and_undo_preserve_protected_native_process(native, tmp_path, protection):
    settings = fixture_model(tmp_path)
    with gateway(native, {'example': settings}) as caller, ThreadPoolExecutor() as work:
        first = caller.post('/v1/chat/completions', json={'model':'example'}).json()
        active = None
        if protection == 'held':
            assert caller.post('/loadcoach/hold', json={'model':'example','seconds':30}).status_code == 200
        elif protection == 'locked':
            assert caller.post('/loadcoach/model-lock', json={'name':'example','on':True}).status_code == 200
        else:
            active = work.submit(caller.post, '/v1/chat/completions', json={'model':'example','sleep':1})
            wait_for_status(caller, lambda status: bool(status['running']))
        changed = configuration({**settings, 'flags':{**settings['flags'],'ctx-size':4096}})
        review = call(caller, 'preview', configuration=changed).json()
        assert review['placement']['protected'] == ['example'] and not review['placement']['leaves']
        assert call(caller, 'apply', configuration=changed, token=review['token'], acknowledge=True).status_code == 200
        undo = caller.get('/loadcoach/model-setup?name=example').json()['undo']['receipt']
        restore = call(caller, 'preview', undo=undo).json()
        assert call(caller, 'apply', undo=undo, token=restore['token'], acknowledge=True).status_code == 200
        assert alive(first['pid'])
        if active:
            assert active.result().json()['pid'] == first['pid']
        assert caller.post('/v1/chat/completions', json={'model':'example'}).json()['pid'] == first['pid']
        assert call(caller, 'preview', undo=undo).status_code == 409
        if protection == 'held':
            assert caller.delete('/loadcoach/hold/example').status_code == 200
        elif protection == 'locked':
            assert caller.post('/loadcoach/model-lock', json={'name':'example','on':False}).status_code == 200
