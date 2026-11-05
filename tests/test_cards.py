"""Card discovery, exclusions and automatic split behavior using fake GPU processes only."""

import pytest

from loadcoach.cards import detect
from loadcoach.config import load_config
from loadcoach.models import Models, check_model
from loadcoach.settings import Changes
from test_app import fake_cards, fake_llama_server, gateway, in_an_empty_folder, model  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401


def test_detection_has_no_fixed_card_count_and_exclusions_survive_restart(tmp_path, monkeypatch, fake_llama_server):
    monkeypatch.setenv("FAKE_DEVICE_COUNT", "4")
    path = tmp_path / "loadcoach.toml"
    path.write_text(f'llama_server = "{fake_llama_server}"\n')
    config = load_config(path)
    assert config.gpus == ("GPU0", "GPU1", "GPU2", "GPU3")
    assert len(config.gpu_details) == 4
    Changes(config.database).put("loadcoach", "disabled_gpus", ["GPU2"], "admin")
    assert load_config(path).disabled_gpus == ["GPU2"]
    monkeypatch.setenv("FAKE_DEVICE_COUNT", "1")
    assert load_config(path).gpus == ("GPU0",)
    assert load_config(path).disabled_gpus == ["GPU2"]
    assert detect("no-such-llama-server") == []


def test_any_prefers_one_and_retries_oom_on_two_only_with_permission(fake_llama_server, fake_cards):
    models = {"small": model("s.gguf", "any", mib=1000),
              "big": {**model("b.gguf", "any", mib=24000, **{"split-total": True}), "allow_split": True},
              "blocked": model("c.gguf", "any", mib=24000, **{"split-total": True})}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0]) as client:
        assert client.post("/v1/chat/completions", json={"model": "small", "messages": []}).status_code == 200
        status = client.get("/loadcoach/status").json()
        assert len(status["loaded"][0]["gpus"]) == 1
        assert client.post("/loadcoach/unload").status_code == 200
        reply = client.post("/v1/chat/completions", json={"model": "big", "messages": []})
        assert reply.status_code == 200, reply.text
        status = client.get("/loadcoach/status").json()
        assert status["loaded"][0]["gpus"] == ["GPU0", "GPU1"]
        big = next(m for m in client.get("/loadcoach/models").json()["models"] if m["name"] == "big")
        assert big["gpus"] == "any" and big["placement_gpus"] == 2 and big["measured"]["mib"] == [12000, 12000]
        assert client.post("/loadcoach/unload").status_code == 200
        assert client.post("/v1/chat/completions", json={"model": "blocked", "messages": []}).status_code == 502
        # A known failed placement must be refused instead of waiting for max_wait.
        assert client.post("/v1/chat/completions", json={"model": "blocked", "messages": []}).status_code == 503


def test_disabled_cards_are_not_loaded_and_idle_models_are_unloaded(fake_llama_server, fake_cards):
    models = {"small": model("s.gguf", "any", mib=1000), "pinned": model("p.gguf", ["GPU0"], mib=1000)}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0]) as client:
        assert client.post("/v1/chat/completions", json={"model": "small", "messages": []}).status_code == 200
        occupied = client.get("/loadcoach/status").json()["loaded"][0]["gpus"][0]
        assert client.post("/loadcoach/cards", json={"disabled_gpus": [occupied]}).status_code == 200
        assert client.get("/loadcoach/status").json()["loaded"] == []
        assert client.post("/v1/chat/completions", json={"model": "small", "messages": []}).status_code == 200
        assert occupied not in client.get("/loadcoach/status").json()["loaded"][0]["gpus"]
        assert client.post("/loadcoach/cards", json={"disabled_gpus": ["GPU0", "GPU1"]}).status_code == 200
        assert client.post("/v1/chat/completions", json={"model": "small", "messages": []}).status_code == 503
        assert client.post("/v1/chat/completions", json={"model": "pinned", "messages": []}).status_code == 503
        assert client.post("/loadcoach/cards", json={"disabled_gpus": ["GPU0", "GPU0"]}).status_code == 400


def test_cards_page_permissions_and_model_settings_round_trip(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser, "pat")
    assert browser.get("/cards").status_code == 403
    assert browser.post("/cards", data={"disabled": "A"}).status_code == 403
    assert browser.post("/settings/gpus", data={"disabled": "A"}).status_code == 403
    sign_in(browser, "jordan")
    moved = browser.get("/cards")
    assert moved.url.path == "/compute-nodes" and moved.url.fragment == "manage-gpus"
    shown = moved.text
    assert 'aria-label="Disable A"' in shown and 'aria-label="Disable B"' in shown
    assert 'href="/cards"' not in shown and 'id="manage-gpus"' in shown
    assert 'href="/compute-nodes" aria-current="page">Compute nodes' in shown
    assert "Saved." in browser.post("/compute-nodes/gpus", data={"machine": "compute", "disabled": "B"}).text
    assert 'value="B" aria-label="Disable B" checked' in browser.get("/compute-nodes").text
    assert browser.post("/cards", data={"machine": "compute", "disabled": "B"}).url.path == "/compute-nodes"
    file = tmp_path / "one.gguf"
    file.touch()
    assert browser.post("/models", data={"name": "one", "file": str(file), "ctx": "2048", "cards": "any", "allow_split": "yes"}).status_code == 200
    saved = Models(str(tmp_path / "loadcoach.db")).stored("one")
    assert saved["gpus"] == "any" and saved["allow_split"] is True
    shown = browser.get("/models/one").text
    assert "Allow split across GPUs" in shown and 'id="placement-cpu"' in shown and 'id="placement-gpu"' in shown and 'id="allow-split"' in shown
    browser.post("/models/one", data={"do": "save", "file": str(file), "ctx": "2048", "placement_controls": "yes", "cpu": "yes"})
    assert Models(str(tmp_path / "loadcoach.db")).stored("one")["gpus"] == 0
    browser.post("/models/one", data={"do": "save", "file": str(file), "ctx": "2048", "placement_controls": "yes", "gpu": "yes"})
    assert Models(str(tmp_path / "loadcoach.db")).stored("one")["gpus"] == "any"
    assert not Models(str(tmp_path / "loadcoach.db")).stored("one").get("allow_split")
    browser.post("/models/one", data={"do": "save", "file": str(file), "ctx": "2048", "placement_controls": "yes", "gpu": "yes", "gpu_card": ["A", "B"]})
    saved = Models(str(tmp_path / "loadcoach.db")).stored("one")
    assert saved["gpus"] == ["A", "B"] and saved["allow_split"] is True


def test_split_permission_is_boolean():
    with pytest.raises(ValueError, match="allow_split is true or false"):
        check_model("one", {**model("one.gguf", "any"), "allow_split": "yes"}, ("GPU0", "GPU1"))


def test_card_exclusions_do_not_interrupt_held_models_or_isolated_locks(fake_llama_server, fake_cards):
    from test_app import add_user
    with gateway(fake_llama_server, {"one": model("one.gguf", 1, mib=1000)}, nvidia_smi=fake_cards[0]) as client:
        headers = add_user(client, "admin", "admin")
        assert client.post("/v1/chat/completions", json={"model": "one", "messages": []}, headers=headers).status_code == 200
        occupied = client.get("/loadcoach/status", headers=headers).json()["loaded"][0]["gpus"][0]
        assert client.post("/loadcoach/hold", json={"model": "one", "seconds": 30}, headers=headers).status_code == 200
        refused = client.post("/loadcoach/cards", json={"disabled_gpus": [occupied]}, headers=headers)
        assert refused.status_code == 409
        assert not any(card["disabled"] for card in client.get("/loadcoach/cards", headers=headers).json()["cards"])
        client.delete("/loadcoach/hold/one", headers=headers)
        token = client.post("/loadcoach/lock", json={"seconds": 30}, headers=headers).json()["token"]
        assert client.post("/loadcoach/cards", json={"disabled_gpus": [occupied]}, headers=headers).status_code == 409
        client.post("/loadcoach/unlock", headers={**headers, "x-loadcoach-lock": token})
        assert client.post("/loadcoach/cards", json={"disabled_gpus": [occupied]}, headers=headers).status_code == 200


def test_any_waits_for_a_busy_single_card_even_when_a_split_could_share(tmp_path):
    import time
    from loadcoach.config import Config
    from loadcoach.runner import Runner, Server
    path = tmp_path / "one.gguf"
    path.touch()
    settings = {**model(str(path), "any", **{"tensor-split": [1, 1]}), "allow_split": True}
    kept = Models(":memory:", first={"one": settings})
    runner = Runner(Config(gpus=("GPU0", "GPU1"), model_idle_minutes=3), None, models=kept)
    runner.seen = {"cards": {index: {"total": 16000, "used": 0} for index in (0, 1)}, "pids": {}}
    one, two = runner.layouts(settings)
    assert "tensor-split" not in one["flags"]
    kept.measured("one", one, [7000])
    kept.measured("one", two, [2000, 2000])
    for index in (0, 1):
        other = Server(f"other{index}", (f"GPU{index}",), model(str(path) + str(index), 1))
        other.ready, other.mib, other.idle_since = True, [12000], time.monotonic()
        runner.servers[other.model] = other
    # Concrete two-card placement can share. Automatic placement still waits for one card.
    assert runner.fit("one", two, set()) is not None
    assert runner.fit("one", settings, set()) is None
