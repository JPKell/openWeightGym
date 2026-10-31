"""Generic reasoning tokens and the single Hermes-specific schema contract."""

import io
import json
import tarfile

import pytest
from starlette.testclient import TestClient

from plugin_helpers import setup, turns
from plugins._installer import bundle
from plugins._sdk import AdapterError, validate
from plugins.hermes.reasoning import reasoning_levels
from weightroom import voice_settings


@pytest.mark.parametrize("effort", [None, "future-v2", "adaptive.depth_3", "X" * 64])
def test_generic_adapter_accepts_future_tokens_without_hermes_enum(tmp_path, effort):
    registry, _, app, headers = setup(tmp_path)
    seen = []

    async def events(run, settings):
        seen.append(run.reasoning)
        yield {"type": "result", "text": "done"}

    app.state.runtime.driver.events = events
    with TestClient(app) as client:
        answer = client.post("/v1/chat/completions", headers=headers,
            json={"model": "gandalf", "messages": [{"role": "user", "content": "hello"}], "reasoning_effort": effort})
        assert answer.status_code == 200, answer.text
        assert seen == [effort]
    registry.db.close()


@pytest.mark.parametrize("effort", ["", "x" * 65, "--high", "high\n", "a b", "$(run)", 2, True, [], {}])
def test_generic_adapter_rejects_malformed_reasoning_before_driver(tmp_path, effort):
    registry, cli, app, headers = setup(tmp_path)
    with TestClient(app) as client:
        answer = client.post("/v1/chat/completions", headers=headers,
            json={"model": "gandalf", "messages": [{"role": "user", "content": "hello"}], "reasoning_effort": effort})
        assert answer.status_code == 400
        assert answer.json()["error"]["code"] == "invalid_reasoning"
        assert not turns(cli)
    registry.db.close()


def test_hermes_levels_share_schema_for_profile_cli_and_voice(tmp_path):
    registry, cli, app, headers = setup(tmp_path)
    schema = registry.manifest("hermes").schema
    levels = tuple(value for value in schema["properties"]["reasoning"]["enum"] if value)
    assert reasoning_levels() == levels
    for level in levels:
        validate(schema, {"reasoning": level, "profiles": {"gandalf": {"reasoning": level}}})
    with pytest.raises(AdapterError):
        validate(schema, {"profiles": {"gandalf": {"reasoning": "future-v2"}}})
    settings = [{"name": "hermes_reasoning_effort", "value": "none", "when": "now"}]
    assert voice_settings.gateway_fields(settings)[0]["choices"] == [(level, level) for level in levels]
    for level in levels:
        changed = voice_settings.gateway_change(settings, {"value-hermes_reasoning_effort": level})
        assert changed == ({} if level == "none" else {"hermes_reasoning_effort": level})
    with TestClient(app) as client:
        request = {"model": "gandalf", "messages": [{"role": "user", "content": "hello"}]}
        accepted = client.post("/v1/chat/completions", headers=headers, json={**request, "reasoning_effort": levels[-1]})
        assert accepted.status_code == 200
        argv = turns(cli)[0]["argv"]
        assert argv[argv.index("--reasoning") + 1] == levels[-1]
        refused = client.post("/v1/chat/completions", headers=headers, json={**request, "reasoning_effort": "future-v2"})
        assert refused.status_code == 400
        assert len(turns(cli)) == 1
    registry.db.close()


@pytest.mark.parametrize("levels", [None, ()])
def test_voice_unknown_and_empty_choices_preserve_current_only(monkeypatch, levels):
    monkeypatch.setattr(voice_settings, "reasoning_levels", lambda: levels)
    settings = [{"name": "hermes_reasoning_effort", "value": "older", "when": "now"}]
    row = voice_settings.gateway_fields(settings)[0]
    assert row["choices"] == [("older", "older (current; not verified)")]
    assert ("unavailable" in row.get("says", "")) == (levels is None)
    assert voice_settings.gateway_change(settings, {"value-hermes_reasoning_effort": "older"}) == {}
    with pytest.raises(ValueError, match="listed reasoning"):
        voice_settings.gateway_change(settings, {"value-hermes_reasoning_effort": "future"})


def test_missing_schema_is_unknown_not_empty(monkeypatch):
    import plugins.hermes.reasoning as contract
    contract.reasoning_levels.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(contract.Path, "read_text", lambda self: json.dumps({}))
        assert contract.reasoning_levels() is None
        contract.reasoning_levels.cache_clear()
        patch.setattr(contract.Path, "read_text", lambda self: json.dumps({"properties": {"reasoning": {"enum": []}}}))
        assert contract.reasoning_levels() == ()
    contract.reasoning_levels.cache_clear()


def test_unavailable_hermes_contract_refuses_before_cli(tmp_path, monkeypatch):
    registry, cli, app, headers = setup(tmp_path)
    monkeypatch.setitem(app.state.runtime.driver.events.__func__.__globals__, "reasoning_levels", lambda: None)
    with TestClient(app) as client:
        answer = client.post("/v1/chat/completions", headers=headers, json={"model": "gandalf",
            "messages": [{"role": "user", "content": "hello"}], "reasoning_effort": "high"})
        assert answer.status_code == 503
        assert answer.json()["error"]["code"] == "reasoning_unavailable"
        assert not turns(cli)
    registry.db.close()


def test_installer_contains_reasoning_contract_resource(tmp_path):
    registry, _, _, _ = setup(tmp_path)
    archive = bundle(registry, "hermes", "local")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
        assert any(name.endswith("/plugins/hermes/reasoning.py") for name in packed.getnames())
    registry.db.close()
