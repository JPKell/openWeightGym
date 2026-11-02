"""Pairing: a GPU node joins a console with a one-time code, and the console records it."""

from __future__ import annotations

import shutil
import stat
import time
import tomllib
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from turnstile.app import load_settings as turnstile_settings
from weightroom import pairing
from weightroom.app import Settings, build_app

FRONT_ONLY = '''compute = "http://127.0.0.1:9442"
spotter = ""
key_file = "../keys/weightroom.key"
data = "../data"

[turnstile]
host = "0.0.0.0"
ports = [8442, 8443, 8444]
'''


@pytest.fixture
def console(tmp_path):
    """A console with no GPU node yet: its settings, data and machine key."""
    for folder in ("settings", "data", "keys"):
        (tmp_path / folder).mkdir()
    (tmp_path / "keys" / "machine.key").write_text("the-machine-key\n")
    (tmp_path / "keys" / "weightroom.key").write_text("lc_front\n")
    config = tmp_path / "settings" / "weightroom.toml"
    config.write_text(FRONT_ONLY)
    return config


def test_a_code_works_once_however_it_is_typed_and_only_while_in_date(tmp_path):
    code, expires = pairing.make_code(tmp_path, "sam")
    assert len(code) == 9 and code[4] == "-" and expires > time.time()
    assert not set(code.replace("-", "")) & set("01ILO")  # nothing that reads as something else
    assert pairing.take_code(tmp_path, code.lower().replace("-", " ")) is True
    assert pairing.take_code(tmp_path, code) is False  # used
    assert "made_by" not in (tmp_path / "pairing.json").read_text() or code not in (tmp_path / "pairing.json").read_text()
    assert stat.S_IMODE((tmp_path / "pairing.json").stat().st_mode) == 0o600

    stale, _ = pairing.make_code(tmp_path, "sam", minutes=-1)
    assert pairing.take_code(tmp_path, stale) is False
    assert pairing.take_code(tmp_path, "short") is False


def test_the_first_node_becomes_the_main_compute_node_with_the_existing_key(console):
    joined = pairing.join(console, "10.0.0.5", "gpu-box")
    assert joined == {"name": "compute", "machine_key": "the-machine-key", "changed": True}
    told = tomllib.loads(console.read_text())
    assert (told["compute"], told["spotter"]) == ("http://10.0.0.5:8442", "http://10.0.0.5:8444")
    assert list(console.parent.glob("weightroom.toml.before-pairing-*"))  # the settings as they were
    assert turnstile_settings(console).upstream(8442) == "http://10.0.0.5:8442"
    # The same node pairing again changes nothing and gets the same key.
    assert pairing.join(console, "10.0.0.5") == {"name": "compute", "machine_key": "the-machine-key", "changed": False}


def test_further_nodes_get_a_name_and_a_key_of_their_own(console):
    (console.parent / "loadcoach.toml").write_text('port = 9442\n')  # this console runs a GPU node of its own
    console.write_text(FRONT_ONLY.replace('spotter = ""', 'spotter = "http://127.0.0.1:9444"'))
    first = pairing.join(console, "10.0.0.6", "GPU Box!")
    second = pairing.join(console, "10.0.0.7", "gpu-box")
    assert (first["name"], second["name"]) == ("gpu-box", "gpu-box-2")
    assert first["machine_key"] != second["machine_key"] != "the-machine-key"
    key = console.parent.parent / "keys" / "machines" / "gpu-box.key"
    assert key.read_text().strip() == first["machine_key"] and stat.S_IMODE(key.stat().st_mode) == 0o600
    told = tomllib.loads(console.read_text())
    assert told["compute"] == "http://127.0.0.1:9442"  # the console's own node is left alone
    assert told["machines"]["gpu-box"] == {"compute": "http://10.0.0.6:8442", "spotter": "http://10.0.0.6:8444",
                                           "machine_key_file": "../keys/machines/gpu-box.key"}
    assert set(turnstile_settings(console).machines) == {"gpu-box", "gpu-box-2"}
    assert pairing.join(console, "10.0.0.6", "other")["machine_key"] == first["machine_key"]  # paired again: as before


def test_repair_moves_a_colliding_legacy_key_without_replacing_front_auth(console):
    console.write_text(FRONT_ONLY + '''
[machines.weightroom]
compute = "http://10.0.0.6:8442"
machine_key_file = "../keys/weightroom.key"
''')
    (console.parent / "loadcoach.toml").write_text("port = 9442\n")
    joined = pairing.join(console, "10.0.0.6")
    assert joined["name"] == "weightroom" and joined["machine_key"] != "lc_front" and joined["changed"]
    assert (console.parent.parent / "keys/weightroom.key").read_text() == "lc_front\n"
    assert tomllib.loads(console.read_text())["machines"]["weightroom"]["machine_key_file"] == "../keys/machines/weightroom.key"
    backup = next(console.parent.glob("weightroom.toml.before-pairing-*"))
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_node_names_are_safe_and_never_the_main_one():
    assert pairing.node_name("Jordan's GPU", set()) == "jordan-s-gpu"
    assert pairing.node_name("compute", set()) == "node"
    assert pairing.node_name("", {"node", "node-2"}) == "node-3"
    assert pairing.valid_address("10.0.0.5") and pairing.valid_address("fe80::1") and pairing.valid_address("gpu.lan")
    assert not pairing.valid_address("bad host; rm") and not pairing.valid_address("")
    assert pairing.command("http://10.0.0.1:8440", "ABCD-EF23").endswith("--join http://10.0.0.1:8440 --code ABCD-EF23")


def test_pairing_over_http_gives_the_key_once_and_turns_away_guessing(console, monkeypatch):
    restarts = []
    monkeypatch.setattr(pairing, "restart_later", lambda *units, **kwargs: restarts.append(units) or True)
    compute_off = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"error": {"message": "off"}})),
                                    base_url="http://front")
    settings = Settings(compute="http://127.0.0.1:9442", key="lc_front", data=str(console.parent.parent / "data"), path=str(console))
    code, _ = pairing.make_code(console.parent.parent / "data", "sam")
    with TestClient(build_app(settings, compute_off, tools_client=compute_off, voice_client=compute_off),
                    client=("10.0.0.5", 50000), base_url="http://console.lan:8440") as node:
        assert node.post("/pair", json={"code": "ABCD-EFGH"}).status_code == 403
        answer = node.post("/pair", json={"code": code, "name": "gpu-box"})
        assert answer.status_code == 200
        assert answer.json() == {"name": "compute", "machine_key": "the-machine-key", "restarting": True,
                                 "front": "http://console.lan:8442"}
        assert node.post("/pair", json={"code": code}).status_code == 403  # once only
        for _ in range(10):
            node.post("/pair", json={"code": "WXYZ-WXYZ"})
        assert node.post("/pair", json={"code": "WXYZ-WXYZ"}).status_code == 429
        assert node.post("/pair", content=b"not json").status_code in (400, 429)
    assert tomllib.loads(console.read_text())["compute"] == "http://10.0.0.5:8442"
    assert len(restarts) == 1


def test_voice_pairing_supplies_a_scoped_front_key_and_updates_gateway_routing(console, monkeypatch):
    from loadcoach.users import Users
    shutil.copytree(Path(__file__).parents[1] / "settings/defaults", console.parent / "defaults")
    gateway = console.parent / "voice-gateway.toml"
    gateway.write_text('voice = "http://127.0.0.1:9445"\n')
    restarts = []
    monkeypatch.setattr(pairing, "restart_later", lambda *units, **kwargs: restarts.append(units) or True)
    unavailable = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)), base_url="http://front")
    settings = Settings(compute="http://127.0.0.1:9442", key="lc_front", data=str(console.parent.parent / "data"), path=str(console))
    code, _ = pairing.make_code(console.parent.parent / "data", "sam")
    with TestClient(build_app(settings, unavailable, tools_client=unavailable, voice_client=unavailable),
                    client=("10.0.0.5", 50000), base_url="http://console.lan:8440") as node:
        answer = node.post("/pair", json={"code": code, "voice": True})
        assert answer.status_code == 200
        key = answer.json()["voice_key"]
    users = Users(str(console.parent.parent / "data/accounts.db"), {})
    try:
        user = users.by_key(key)
        assert user.name == "voice-service" and user.can("hold") and user.can("load") and not user.can("superuser")
    finally:
        users.db.close()
    assert key != "the-machine-key" and 8445 in turnstile_settings(console).ports
    assert tomllib.loads(gateway.read_text())["voice"] == "http://10.0.0.5:8445"
    assert restarts == [(("turnstile.service", "voice-gateway.service", "weightroom.service"),)]
