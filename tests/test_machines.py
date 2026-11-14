"""Control node's inventory of compute machines: what each last said, and how long ago."""
import asyncio
import time

import httpx
import pytest
from starlette.testclient import TestClient

from front_helpers import machine_key
from loadcoach.groups import Group
from loadcoach.users import Users
from test_turnstile import Compute
from test_weightroom import desk  # noqa: F401
from turnstile.app import Settings, build_app, load_settings
from turnstile.machines import Machines, logical_catalogue

STATUS = {"memory": {"CUDA0": {"total": 16000, "others": 100, "free": 15000}}, "loaded": [{"model": "small", "state": "ready", "gpus": [0], "locked": False}],
          "running": [{}], "waiting": [{}, {}], "busy": 1, "draining": None}
MODELS = {"data": [{"id": "small", "owned_by": "loadcoach", "content_sha256": "a" * 64}, {"id": "OR-free", "owned_by": "hosted"}]}


def test_a_machine_is_ready_then_not_answering_with_its_last_facts_and_unknown_before_any():
    clock, answers = [1000.0], {"a": {"/loadcoach/status": STATUS, "/v1/models": MODELS}}

    async def ask(name, path):
        return answers.get(name, {}).get(path)

    machines = Machines(["a", "b"], ask, every=10, now=lambda: clock[0])
    assert machines.available("a") is None
    assert [m["state"] for m in machines.report()] == ["unknown", "unknown"]
    asyncio.run(machines.poll())
    first, second = machines.report()
    assert machines.available("a") is True and machines.available("b") is False
    assert first["state"] == "ready" and first["age"] == 0 and second["state"] == "unknown" and "cards" not in second
    assert first["cards"] == [{"card": "CUDA0", "total": 16000, "free": 15000, "others": 100}] and first["models"] == [{"name": "small", "content": "a" * 64}]  # a hosted model is not this machine's
    assert (first["running"], first["waiting"], first["busy"], first["draining"]) == (1, 2, 1, False)
    assert first["loaded"] == [{"model": "small", "state": "ready", "cards": [0], "locked": False}]

    answers.clear()  # it stops answering: its last facts stay, with their age
    clock[0] += 25
    asyncio.run(machines.poll())
    assert machines.report()[0]["state"] == "ready" and machines.report()[0]["age"] == 25
    assert machines.available("a") is False  # the latest failure takes effect before history expires
    clock[0] += 10
    stale = machines.report()[0]
    assert stale["state"] == "not answering" and stale["age"] == 35 and stale["models"] == [{"name": "small", "content": "a" * 64}]


def test_the_route_asks_with_a_key_and_the_poller_asks_as_fronts_own_account(tmp_path):
    users = Users(str(tmp_path / "accounts.db"), {"plain": Group(can=frozenset({"load"})), "system": Group(can=frozenset({"follow-settings"}))})
    users.add("pat", "plain")
    users.add("weightroom", "system")
    pat = {"authorization": "Bearer " + users.new_key("pat")}
    (tmp_path / "weightroom.key").write_text(users.new_key("weightroom"))
    compute = Compute(machine_key(tmp_path))
    settings = Settings(compute="compute", data=str(tmp_path), machine_key_file=str(tmp_path / "machine.key"), auth_key_file=str(tmp_path / "weightroom.key"),
                        machines_poll_seconds=0.05)
    with TestClient(build_app(settings, httpx.AsyncClient(transport=compute), lambda *sent: None, users=users), base_url="http://front:8442") as coach:
        assert coach.get("/turnstile/machines").status_code == 401
        for _ in range(100):  # the first poll runs as turnstile starts
            seen = coach.get("/turnstile/machines", headers=pat).json()
            if seen["machines"][0]["state"] == "ready":
                break
            time.sleep(0.05)
    assert [(m["name"], m["state"]) for m in seen["machines"]] == [("compute", "ready")] and seen["poll_seconds"] == 0.05
    assert "key" not in str(seen).lower()


def test_further_machines_are_read_and_checked(tmp_path):
    file = tmp_path / "weightroom.toml"
    file.write_text('compute = "http://c:8442"\n[machines.rig]\ncompute = "http://c:9442"\nmachine_key_file = "rig.key"\n[turnstile]\nmachines_poll_seconds = 4\n')
    settings = load_settings(file)
    # The old top-level `compute` is one more node, named "compute", ahead of the others: every node is a peer.
    legacy = {"compute": "http://c:8442", "spotter": "http://c:8444", "machine_key_file": str((tmp_path / "../keys/machine.key").resolve()),
              "wake_mac": "", "wake_address": "255.255.255.255"}
    assert settings.machines == {"compute": legacy, "rig": {"compute": "http://c:9442", "spotter": "", "machine_key_file": str(tmp_path / "rig.key"),
                                                            "wake_mac": "", "wake_address": "255.255.255.255"}} and settings.machines_poll_seconds == 4
    file.write_text('compute = "http://c:8442"\n')
    assert load_settings(file).machines == {"compute": legacy} and load_settings(file).machines_poll_seconds == 10
    # After `owg update` moves it, the node has its own name and remembers the old one.
    file.write_text('[machines.jpk-main]\ncompute = "http://c:8442"\nformerly = "compute"\n')
    assert load_settings(file).machines["jpk-main"]["formerly"] == "compute"
    for wrong in ('[machines.compute]\ncompute = "http://c:9442"', '[machines.rig]\ncompute = "c:9442"', '[machines.rig]\nmachine_key_file = "x"',
                  '[turnstile]\nmachines_poll_seconds = 0'):
        file.write_text('compute = "http://c:8442"\n' + wrong)
        with pytest.raises(SystemExit):
            load_settings(file)


def test_the_machines_page_lists_what_turnstile_reports(desk):
    from test_weightroom import sign_in
    browser, _, _ = desk
    sign_in(browser, "jordan")
    shown = browser.get("/machines")
    assert shown.status_code == 200 and "<h1 class=\"page-title wrap\">Telemetry</h1>" in shown.text
    assert "compute" in shown.text and "No telemetry is available" in shown.text


def test_models_are_one_by_file_hash_whatever_each_machine_calls_them():
    said = {
        "a": {"/loadcoach/status": STATUS, "/v1/models": {"data": [
            {"id": "qwen-q4", "owned_by": "loadcoach", "content_sha256": "1" * 64}, {"id": "other", "owned_by": "loadcoach", "content_sha256": "2" * 64},
            {"id": "unread", "owned_by": "loadcoach"}]}},
        "b": {"/loadcoach/status": STATUS, "/v1/models": {"data": [
            {"id": "q4-27b", "owned_by": "loadcoach", "content_sha256": "1" * 64}, {"id": "unread", "owned_by": "loadcoach"}]}},
    }

    async def ask(name, path):
        return said[name][path]

    machines = Machines(["a", "b"], ask)
    asyncio.run(machines.poll())
    by_name = {tuple(one["names"]): one for one in machines.models()}
    assert by_name[("q4-27b", "qwen-q4")]["machines"] == ["a", "b"] and by_name[("q4-27b", "qwen-q4")]["content"] == "1" * 64
    assert by_name[("other",)]["machines"] == ["a"]
    # Two unread files with the same name are not taken to be one model.
    assert [one["machines"] for one in machines.models() if one["names"] == ["unread"]] == [["a"], ["b"]]


def test_logical_catalogue_uses_one_name_and_keeps_each_verified_location():
    found = {
        "compute": {"data": [{"id": "embed-q8", "owned_by": "loadcoach", "model_id": "gpu", "fingerprint": "g", "content_sha256": "1" * 64}]},
        "front_cpu": {"data": [{"id": "embed-cpu", "owned_by": "loadcoach", "model_id": "cpu", "fingerprint": "c", "content_sha256": "1" * 64}]},
        "remote_cpu": {"data": [{"id": "embed-remote", "owned_by": "loadcoach", "model_id": "remote", "fingerprint": "r", "content_sha256": "1" * 64}]},
    }
    model, = logical_catalogue(found)
    assert model["id"] == "embed-q8" and model["machines"] == ["compute", "front_cpu", "remote_cpu"]
    assert [(one["machine"], one["id"], one["model_id"]) for one in model["locations"]] == [
        ("compute", "embed-q8", "gpu"), ("front_cpu", "embed-cpu", "cpu"), ("remote_cpu", "embed-remote", "remote")]

    # Two semantic variants using the same weights on one machine are not guessed to be locations of one model.
    found["compute"]["data"].append({"id": "embed-special", "owned_by": "loadcoach", "content_sha256": "1" * 64})
    assert len(logical_catalogue(found)) == 4


def test_logical_catalogue_keeps_model_sort_order_across_machines():
    found = {
        "compute": {"data": [
            {"id": "alpha", "owned_by": "loadcoach", "order": 10},
            {"id": "zebra", "owned_by": "loadcoach", "order": 1},
            {"id": "Beta", "owned_by": "loadcoach", "order": 10},
        ]},
    }
    assert [model["id"] for model in logical_catalogue(found)] == ["zebra", "alpha", "Beta"]
