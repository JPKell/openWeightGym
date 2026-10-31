"""Parallel form controls, saved server flags and the effective request context window."""

import pytest
import re
from starlette.testclient import TestClient

from weightroom.model_settings import PARALLEL_FLAGS, UNUSABLE_MODEL_FLAGS, parallel_fields, usable_model_flags
from loadcoach.models import Models, request_context
from loadcoach.runner import command_line
from test_app import add_user, fake_llama_server, gateway, gguf, model  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401


def test_parallel_fields_start_with_the_training_limit_and_read_existing_settings():
    assert parallel_fields({"ctx-size": 8000}, 4000) == {"enabled": False, "count": 2, "shared": True, "context": 4000}
    assert parallel_fields({"ctx-size": 8000})["context"] == 4000
    legacy = parallel_fields({"ctx-size": 12000, "parallel": 3})
    assert legacy == {"enabled": True, "count": 3, "shared": False, "context": 4000}


def test_model_flag_catalogue_excludes_commands_and_incompatible_server_ownership():
    names = ["help", "version", "cache-list", "completion-bash", "list-devices", "threads",
             "hf-repo", "models-dir", "api-key", "ssl-cert-file", "reasoning"]
    kept = usable_model_flags([{"name": name} for name in names])
    assert [flag["name"] for flag in kept] == ["threads", "reasoning"]
    assert {"help", "hf-repo", "models-dir", "api-key", "ssl-cert-file"} <= UNUSABLE_MODEL_FLAGS


@pytest.mark.parametrize("cards, field, expected", [
    ("1", "keep_gpu_count", 1),
    ("2", "keep_gpu_count", 2),
    ("all", "keep_default_gpus", None),
])
def test_rendered_placement_control_preserves_legacy_mode_on_unrelated_save(desk, tmp_path, cards, field, expected):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    file = gguf(tmp_path / "placement.gguf")
    Models(str(tmp_path / "loadcoach.db")).put("placement", {"flags":{"model":file,"ctx-size":2048}, **({"gpus":expected} if expected is not None else {})})
    shown = browser.get("/models/placement").text
    assert 'name="placement_controls" value="yes"' in shown
    control = re.search(rf'<input[^>]+name="{field}"[^>]+>', shown)
    assert control and " checked" in control.group()
    value = re.search(r'value="([^"]+)"', control.group()).group(1)
    form = {"do": "save", "file": file, "ctx": "4096", "placement_controls": "yes", "gpu": "yes", field: value}
    saved = browser.post("/models/placement", data=form)
    assert saved.status_code == 200 and "Saved." in saved.text
    kept = Models(str(tmp_path / "loadcoach.db")).stored("placement")
    assert kept["flags"]["ctx-size"] == 4096
    assert kept.get("gpus") == expected


def test_legacy_gpu_count_can_switch_to_auto_pinned_or_cpu(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    file = gguf(tmp_path / "placement-switch.gguf")
    Models(str(tmp_path / "loadcoach.db")).put("switch", {"flags":{"model":file,"ctx-size":2048},"gpus":1})
    shown = browser.get("/models/switch").text
    assert 'name="keep_gpu_count" value="1"' in shown and 'name="gpu_card" value="A"' in shown
    base = {"do": "save", "file": file, "ctx": "2048", "placement_controls": "yes", "gpu": "yes"}
    kept = Models(str(tmp_path / "loadcoach.db"))
    assert browser.post("/models/switch", data=base).status_code == 200
    assert kept.stored("switch")["gpus"] == "any"
    assert browser.post("/models/switch", data={**base, "gpu_card": "A"}).status_code == 200
    assert kept.stored("switch")["gpus"] == ["A"]
    assert browser.post("/models/switch", data={**base, "gpu": "", "cpu": "yes"}).status_code == 200
    assert kept.stored("switch")["gpus"] == 0


@pytest.mark.parametrize("flags, expected", [
    ({"ctx-size": 8000, "parallel": 2}, 4000),
    ({"ctx-size": 8000, "parallel": 2, "kv-unified": True}, 8000),
    ({"ctx-size": 8000, "parallel": 2, "kv-unified": True, "kv-unified-per-slot": 4000}, 4000),
    ({"ctx-size": 32000, "parallel": 2, "kv-unified": True, "kv-unified-per-slot": 4000}, 4000),
    ({"ctx-size": 2000, "parallel": 2, "kv-unified": True, "kv-unified-per-slot": 4000}, 2000),
    ({"ctx-size": 2000, "parallel": 2, "kv-unified": False, "kv-unified-per-slot": 4000}, 1000),
])
def test_request_budget_uses_the_pool_mode_and_explicit_cap(flags, expected):
    assert request_context(flags) == expected


def test_parallel_model_fields_save_validate_and_remove_the_server_flags(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    file = gguf(tmp_path / "parallel.gguf", general__architecture="tiny", tiny__context_length=4000)
    browser.post("/models", data={"name": "one", "file": file, "ctx": "8000", "cards": "1"})
    shown = browser.get("/models/one").text
    assert 'id="parallel-fields" class="row g-3 mt-0" disabled' in shown
    assert 'name="parallel_context" type="number" min="1" step="1" class="form-control" value="4000"' in shown
    form = {"do": "save", "file": file, "ctx": "8000", "cards": "1", "parallel_controls": "yes",
            "parallel_enabled": "yes", "parallel_count": "2", "parallel_shared": "yes", "parallel_context": "4000"}
    saved = browser.post("/models/one", data=form)
    assert saved.status_code == 200 and "Saved." in saved.text
    kept = Models(str(tmp_path / "loadcoach.db"))
    flags = kept.stored("one")["flags"]
    assert flags == {"model": file, "ctx-size": 8000, "parallel": 2, "kv-unified": True, "kv-unified-per-slot": 4000}
    assert 'data-parallel-switch checked' in saved.text and 'name="flag" class="form-control" value="parallel"' not in saved.text
    command = command_line("llama-server", 9999, flags, ("A",))
    assert command[command.index("--parallel") + 1] == "2"
    assert "--kv-unified" in command and command[command.index("--kv-unified-per-slot") + 1] == "4000"
    for field, bad in (("parallel_count", "0"), ("parallel_count", "1.5"), ("parallel_context", "-1"), ("parallel_context", "oops")):
        refused = browser.post("/models/one", data={**form, field: bad})
        assert refused.status_code == 400
        assert kept.stored("one")["flags"] == flags
    # A disabled section cannot be bypassed with a hand-typed raw flag row.
    off = {key: value for key, value in form.items() if key != "parallel_enabled"}
    assert browser.post("/models/one", data={**off, "flag": "parallel", "value": "8"}).status_code == 200
    assert not set(PARALLEL_FLAGS) & kept.get("one")["flags"].keys()
    # Separate slots remain available through the shared-pool checkbox.
    split = {key: value for key, value in form.items() if key != "parallel_shared"}
    assert browser.post("/models/one", data=split).status_code == 200
    assert kept.stored("one")["flags"]["kv-unified"] is False


def test_disabling_parallel_also_suppresses_inherited_model_defaults(desk, tmp_path):
    browser, switch, _ = desk
    sign_in(browser, "jordan")
    defaults = {"parallel": 2, "kv-unified": True, "kv-unified-per-slot": 4000}
    native = TestClient(switch.front, base_url="http://front:8442")
    headers = {"authorization": "Bearer " + browser.cookies["weightroom"]}
    assert native.post("/loadcoach/cards", json={"disabled_gpus": []}, headers=headers).status_code == 200
    assert native.post("/loadcoach/settings/loadcoach", json={"model_defaults": defaults}, headers=headers).status_code == 200
    file = gguf(tmp_path / "inherited.gguf")
    browser.post("/models", data={"name": "inherited", "file": file, "ctx": "8000", "cards": "1"})
    assert 'data-parallel-switch checked' in browser.get("/models/inherited").text
    saved = browser.post("/models/inherited", data={"do": "save", "file": file, "ctx": "8000", "cards": "1", "parallel_controls": "yes"})
    assert saved.status_code == 200 and 'id="parallel-fields" class="row g-3 mt-0" disabled' in saved.text
    effective = Models(str(tmp_path / "loadcoach.db"), defaults).get("inherited")
    assert not set(PARALLEL_FLAGS) & effective["flags"].keys()
    assert not {"--" + flag for flag in PARALLEL_FLAGS} & set(command_line("llama-server", 9999, effective["flags"], ("A",)))
    negative_defaults = {"parallel": 2, "no-kv-unified": True, "kv-unified-per-slot": 4000}
    assert native.post("/loadcoach/settings/loadcoach", json={"model_defaults": negative_defaults}, headers=headers).status_code == 200
    enabled = browser.post("/models/inherited", data={"do": "save", "file": file, "ctx": "8000", "cards": "1", "parallel_controls": "yes",
                           "parallel_enabled": "yes", "parallel_count": "2", "parallel_shared": "yes", "parallel_context": "4000"})
    assert enabled.status_code == 200
    effective = Models(str(tmp_path / "loadcoach.db"), negative_defaults).get("inherited")
    assert effective["flags"]["kv-unified"] is True and "no-kv-unified" not in effective["flags"]


def test_role_selection_respects_the_request_cap_even_with_a_large_shared_pool(fake_llama_server, tmp_path):
    models = {"short": model(str(tmp_path / "short.gguf"), 1, parallel=2, **{"ctx-size": 32000, "kv-unified": True, "kv-unified-per-slot": 16}),
              "long": model(str(tmp_path / "long.gguf"), 1, **{"ctx-size": 32000})}
    with gateway(fake_llama_server, models) as caller:
        headers = add_user(caller, "admin", "admin")
        assert caller.post("/loadcoach/roles/chat", json={"models": {"short": 2, "long": 1}}, headers=headers).status_code == 201
        assert caller.post("/v1/chat/completions", json={"model": "short"}, headers=headers).status_code == 200
        answer = caller.post("/v1/chat/completions", json={"model": "chat:available", "messages": [{"role": "user", "content": "word " * 40}]}, headers=headers)
        assert answer.status_code == 200 and answer.headers["x-loadcoach-picked"] == "long"
