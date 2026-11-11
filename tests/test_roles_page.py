"""Checks of the Roles page: a role made, its models weighted, and what it would pick shown."""

import pytest

from loadcoach.models import Models
from loadcoach.roles import Roles
from test_app import gguf
from test_weightroom import desk, sign_in  # noqa: F401  (the first sets a check up)


def test_the_roles_page_makes_a_role_weights_its_models_and_shows_what_it_would_pick(desk, tmp_path):
    browser, _, _ = desk
    kept, roles = Models(str(tmp_path / "loadcoach.db")), Roles(str(tmp_path / "data" / "roles.db"))  # kept on the front machine, beside the accounts
    for name in ("strong", "quick"):
        kept.put(name, {"gpus": 1, "flags": {"model": gguf(tmp_path / f"{name}.gguf", general__name=name), "ctx-size": 2048}})
    sign_in(browser, "pat")
    assert browser.get("/roles").status_code == 403
    browser.post("/sign-out")
    sign_in(browser, "jordan")
    assert 'href="/roles"' in browser.get("/models").text and "No roles yet." in browser.get("/roles").text

    wrong = browser.post("/roles", data={"do": "add", "name": "a:b", "model": "strong", "weight": "1"})
    assert wrong.status_code == 400 and "with no colon" in wrong.text
    added = browser.post("/roles", data={"do": "add", "name": "chat", "model": "quick", "weight": "1"})
    assert "Saved." in added.text and roles.get("chat") == {"models": {"quick": 1}}
    # Its models, each with a weight. The row at the end adds one. What the page shows is the order the weights give.
    browser.post("/roles", data={"do": "save", "name": "chat", "model": ["quick", "strong"], "weight": ["1", "2.5"]})
    assert roles.get("chat") == {"models": {"quick": 1, "strong": 2.5}}
    shown = browser.get("/roles").text
    assert "mark unsupported answers as missing" in shown and "unsupported answers null" not in shown
    assert shown.index("Required for this role") < shown.index("Benchmark preferences for chat")
    assert shown.index('value="strong"') < shown.index('value="quick"') and "not known yet" in shown and "Not loaded" in shown
    # What it would pick right now, each way of asking, and why. Nothing is loaded here, so each way comes to the top of the list.
    for asked in ("chat", "chat:best", "chat:available", "chat:fast"):
        assert f"<code>{asked}</code>" in shown
    assert shown.count('<a href="/models/strong">strong</a>') == 4 and "the top of the list" in shown and "nothing in the list is warm, so the top of it" in shown

    # A role is among what the chat page offers, each way of asking for it.
    chat = browser.get("/chat").text
    assert '<option value="chat">chat</option>' in chat.replace(" selected", "") and ">chat:best</option>" in chat and ">chat:fast</option>" in chat
    browser.post("/roles", data={"do": "save", "name": "chat", "model": ["quick", "strong"], "weight": ["", "0"]})  # a weight emptied takes a model out; 0 keeps it listed and out of the picking
    assert roles.get("chat") == {"models": {"strong": 0}} and "Paused · weight is 0" in browser.get("/roles").text
    removed = browser.post("/roles", data={"do": "remove", "name": "chat"})
    assert "Removed the role chat." in removed.text and roles.get("chat") is None


@pytest.mark.parametrize("weight", ["NaN", "Infinity", "-Infinity", "1e999", "-1", "true", "invalid"])
def test_invalid_role_weight_shows_an_error_and_preserves_saved_role(desk, tmp_path, weight):
    browser, _, _ = desk
    Models(str(tmp_path / "loadcoach.db")).put("small", {"gpus": 1, "flags": {"model": gguf(tmp_path / "small.gguf"), "ctx-size": 2048}})
    roles = Roles(str(tmp_path / "data" / "roles.db"))  # kept on the front machine, beside the accounts
    roles.put("chat", {"models": {"small": 1}})
    sign_in(browser, "jordan")
    response = browser.post("/roles", data={"do": "save", "name": "chat", "model": "small", "weight": weight})
    assert response.status_code == 400 and "finite number" in response.text
    assert roles.get("chat") == {"models": {"small": 1}}


def test_benchmark_controls_requirements_and_invalid_preferences_preserve_settings(desk, tmp_path):
    browser, _, _ = desk
    kept = Models(str(tmp_path / "loadcoach.db"))
    kept.put("small", {"gpus": 1, "flags": {"model": gguf(tmp_path / "small.gguf"), "ctx-size": 2048}})
    roles = Roles(str(tmp_path / "data" / "roles.db"))  # kept on the front machine, beside the accounts
    roles.put("chat", {"models": {"small": 1}})
    sign_in(browser, "jordan")
    shown = browser.get("/roles")
    assert shown.status_code == 200
    assert 'Benchmark preferences' in shown.text and 'Minimum context tokens' in shown.text
    assert 'name="benchmark-weight"' in shown.text and 'name="missing"' in shown.text
    assert 'id="loaded-chat"' in shown.text and 'id="fast-loaded-chat"' in shown.text
    form = {"do": "save", "name": "chat", "model": "small", "weight": "2", "evidence-settings": "1",
            "benchmark": ["context", "speed"], "benchmark-weight": ["1", "3"], "missing": "exclude", "stale": "warn",
            "max_age_days": "30", "requires-tokens": "4096", "requires-tools": "on", "requires-stays": "on"}
    saved = browser.post("/roles", data=form)
    assert saved.status_code == 200 and 'class="alert alert-success said said-box" role="status">Saved.' in saved.text
    settings = roles.get("chat")
    assert settings == {"models": {"small": 2}, "benchmarks": {"context": 1, "speed": 3}, "missing": "exclude", "stale": "warn", "max_age_days": 30,
                        "requires": {"pictures": False, "tools": True, "stays": True, "tokens": 4096}}
    assert 'no compatible result' in saved.text and 'it does not take tools' in saved.text
    bad = browser.post("/roles", data=dict(form, **{"benchmark-weight": ["NaN", "3"]}))
    assert bad.status_code == 400 and 'finite number' in bad.text and roles.get("chat") == settings
    bad = browser.post("/roles", data=dict(form, **{"requires-tokens": "12.5"}))
    assert bad.status_code == 400 and roles.get("chat") == settings
    for bonuses in ({'loaded_bonus':'NaN'}, {'fast_loaded_bonus':'501'}):
        bad = browser.post('/roles', data={**form, **bonuses})
        assert bad.status_code == 400 and roles.get('chat') == settings


def test_roles_reports_failed_publication_and_keeps_accepted_definitions(desk, tmp_path, monkeypatch):
    browser, _, _ = desk
    Models(str(tmp_path / "loadcoach.db")).put("small", {"gpus": 1, "flags": {"model": gguf(tmp_path / "small.gguf"), "ctx-size": 2048}})
    roles = Roles(str(tmp_path / "data" / "roles.db"))  # kept on the front machine, beside the accounts
    roles.put("chat", {"models": {"small": 1}})
    sign_in(browser, "jordan")
    assert browser.get("/roles").status_code == 200
    def unavailable(*args, **kwargs):
        raise ValueError("Snapshot publication unavailable.")
    monkeypatch.setattr(browser.app.state.benchmarks.state.store, 'role_snapshot', unavailable)
    shown = browser.get("/roles")
    assert shown.status_code == 200 and 'Evidence could not be refreshed' in shown.text
    assert 'LoadCoach keeps the last accepted snapshot' in shown.text and 'id="bench-chat-context"' in shown.text and 'higher is better' in shown.text
    assert 'the top of the list' in shown.text and roles.get("chat") == {"models": {"small": 1}}
