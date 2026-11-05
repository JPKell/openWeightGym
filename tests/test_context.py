"""Adaptive contexts use observed GPU memory, never model file size or active-call eviction."""

import json
import time

import pytest

from loadcoach.config import Config
from loadcoach.models import Models, at_context, check_model
from loadcoach.runner import Runner, Server, Ticket
from test_app import fake_llama_server, fake_cards, in_an_empty_folder, gateway, model, ask, alive
from test_weightroom import desk, sign_in


def adaptive(name="new", maximum=10000):
    return {**model(name + ".gguf", ["GPU0"], **{"ctx-size": maximum, "mib": 1000, "context-mib": True}), "min_context": 2000}


def context(answer):
    arguments = answer.json()["arguments"]
    return int(arguments[arguments.index("--ctx-size") + 1])


def test_three_contexts_are_measured_and_recent_model_survives_shrinking(fake_llama_server, fake_cards):
    models = {"new": adaptive(), "old": model("old.gguf", ["GPU0"], mib=6000), "twin": adaptive()}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0], model_idle_minutes=3) as caller:
        first = ask(caller, "new")
        assert first.status_code == 200 and context(first) == 10000
        listed = caller.get("/loadcoach/models").json()["models"][0]
        assert [(p["context"], p["mib"]) for p in listed["measurements"]] == [(2000, [3000]), (6000, [7000]), (10000, [11000])]
        caller.post("/loadcoach/unload")
        old = ask(caller, "old").json()
        smaller = ask(caller, "new")
        assert smaller.status_code == 200 and context(smaller) == 6000
        assert alive(old["pid"])
        again = ask(caller, "new")
        assert again.json()["pid"] == smaller.json()["pid"]
        assert ask(caller, "twin").json()["pid"] == smaller.json()["pid"]
        listed = caller.get("/loadcoach/models").json()["models"][0]
        assert listed["context_loaded"] == 6000 and listed["flags"]["ctx-size"] == 10000 and not listed["stale"]


def test_failed_maximum_keeps_smaller_success_and_failure_details(fake_llama_server, fake_cards):
    with gateway(fake_llama_server, {"new": adaptive(maximum=20000)}, nvidia_smi=fake_cards[0]) as caller:
        answer = ask(caller, "new")
        assert answer.status_code == 200 and context(answer) == 11000
        points = caller.get("/loadcoach/models").json()["models"][0]["measurements"]
        assert points[0]["mib"] == [3000] and points[1]["mib"] == [12000]
        assert points[2]["mib"] is None and "out of memory" in points[2]["error"]
        caller.post("/loadcoach/unload")
        assert context(ask(caller, "new")) == 11000


def test_waiting_model_wakes_at_idle_expiry_and_uses_maximum(fake_llama_server, fake_cards):
    models = {"new": adaptive(), "old": model("old.gguf", ["GPU0"], mib=13000)}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0], model_idle_minutes=.02, max_wait=10) as caller:
        assert context(ask(caller, "new")) == 10000
        caller.post("/loadcoach/unload")
        old = ask(caller, "old").json()
        began = time.monotonic()
        answer = ask(caller, "new")
        assert answer.status_code == 200 and context(answer) == 10000
        assert time.monotonic() - began >= 1.1 and not alive(old["pid"])


def test_stale_eviction_minimum_boundary_holds_and_finished_time():
    models = Models(":memory:")
    settings = adaptive()
    models.put("new", settings)
    for ctx, size in [(2000, 3000), (6000, 7000), (10000, 11000)]:
        models.measured("new", at_context(settings, ctx), [size])
    runner = Runner(Config(gpus=("GPU0",), card_margin=1000, model_idle_minutes=3), None, models=models)
    runner.seen = {"cards": {0: {"total": 16000, "used": 0}}, "pids": {}}
    old = Server("old", ("GPU0",), model("old.gguf", 1), ready=True, mib=[6000], used=time.monotonic() - 500)
    runner.servers["old"] = old
    assert runner.fit("new", settings, set())[0]["flags"]["ctx-size"] == 6000
    old.mib = [12000]
    assert runner.fit("new", settings, set())[0]["flags"]["ctx-size"] == 2000
    old.mib = [13000]
    assert runner.fit("new", settings, set()) is None  # never go below the minimum
    old.idle_since -= 181
    plan = runner.fit("new", settings, set())
    assert plan[0]["flags"]["ctx-size"] == 10000 and plan[2] == [old]
    runner.holds["old", "voice"] = time.monotonic() + 100
    assert runner.fit("new", settings, set()) is None
    runner.holds.clear()
    ticket = Ticket("old", "user", "here", "normal", server=old)
    runner.running.append(ticket)
    assert runner.fit("new", settings, set()) is None
    runner.release(ticket)
    assert time.monotonic() - old.idle_since < 1
    assert runner.fit("new", settings, set()) is None  # a long answer just finished: still recent


def test_measurement_migration_invalidation_and_range_validation():
    models = Models(":memory:")
    settings = adaptive()
    models.put("new", settings)
    legacy = {"settings": settings, "files": models.stamps(settings), "mib": [11000], "at": 1}
    models.db.execute("UPDATE models SET measured = ?", (json.dumps(legacy),))
    assert models.measure("new")["mib"] == [11000]
    models.measured("new", at_context(settings, 2000), [3000])
    models.measured("new", at_context(settings, 6000), [7000])
    assert models.calibrated("new") and len(models.measurements("new")) == 3
    models.put("new", at_context(settings, 12000))
    assert not models.calibrated("new") and len(models.measurements("new")) == 3
    models.put("new", {**settings, "flags": {**settings["flags"], "cache-type-k": "f16"}})
    assert models.measurements("new") == []
    for minimum in (0, -1, 10001, True, 2.5):
        with pytest.raises(ValueError, match="min_context"):
            check_model("new", {**settings, "min_context": minimum}, ("GPU0",))


def test_context_range_saved_and_shown_on_model_pages(desk, tmp_path):
    browser, _, _ = desk
    file = tmp_path / "example.gguf"
    file.touch()
    sign_in(browser, "jordan")
    Models(str(tmp_path / "loadcoach.db")).put("ranged", {"gpus":1,"min_context":2000,"flags":{"model":str(file),"ctx-size":10000}})
    kept = Models(str(tmp_path / "loadcoach.db"), {"flash-attn": "on"})
    settings = kept.get("ranged")
    assert settings["min_context"] == 2000
    for ctx, size in [(2000, 3000), (6000, 7000), (10000, 11000)]:
        kept.measured("ranged", at_context(settings, ctx), [size])
    page = browser.get("/models/ranged").text
    assert 'name="min_context" type="number" min="1" step="1" class="form-control" value="2000"' in page
    # The minimum and the maximum are said at the top of the page. The midpoint is not.
    assert "2.9 GB <small class=\"text-secondary\">at 2,000 tokens</small>" in page and "10.7 GB <small class=\"text-secondary\">at 10,000 tokens</small>" in page
    assert "6,000" not in page and "Measured at" not in page
    table = browser.get("/models?part=list").text
    assert "Memory at min" in table and "Memory at max" in table and ">Size</th>" not in table
    refused = browser.post("/models/ranged", data={"file": str(file), "ctx": "1000", "min_context": "2000", "cards": "1"})
    assert refused.status_code == 400 and "no larger than ctx-size" in refused.text
    assert kept.get("ranged")["flags"]["ctx-size"] == 10000


def test_idle_setting_changes_live_and_resets_to_three_minutes(fake_llama_server):
    with gateway(fake_llama_server, model_idle_minutes=3) as caller:
        for value, expected in [(7, 7), (0, 0), (None, 3)]:
            answer = caller.post("/loadcoach/settings/loadcoach", json={"model_idle_minutes": value})
            assert answer.status_code == 200
            program = next(p for p in answer.json()["programs"] if p["program"] == "loadcoach")
            setting = next(s for s in program["settings"] if s["name"] == "model_idle_minutes")
            assert setting["value"] == expected and setting["when"] == "now"
        assert caller.post("/loadcoach/settings/loadcoach", json={"model_idle_minutes": -1}).status_code == 400


def test_fixed_multicard_split_shrinks_beside_recent_models():
    settings = {**adaptive(), "gpus": 2}
    settings["flags"]["tensor-split"] = [1, 1]
    models = Models(":memory:", first={"new": settings})
    for ctx, size in [(2000, 4000), (6000, 9000), (10000, 13000)]:
        models.measured("new", at_context(settings, ctx), [size, size])
    runner = Runner(Config(gpus=("GPU0", "GPU1"), card_margin=1000, model_idle_minutes=3), None, models=models)
    runner.seen = {"cards": {0: {"total": 16000, "used": 0}, 1: {"total": 16000, "used": 0}}, "pids": {}}
    old = Server("old", ("GPU0",), model("old.gguf", 1), ready=True, mib=[5000])
    runner.servers["old"] = old
    plan = runner.fit("new", settings, set())
    assert plan[0]["flags"]["ctx-size"] == 6000 and plan[1:] == (("GPU0", "GPU1"), [])
    old.idle_since -= 181
    plan = runner.fit("new", settings, set())
    assert plan[0]["flags"]["ctx-size"] == 10000 and plan[2] == [old]
