"""Machine keys: manual/scheduled rotation from Compute nodes and emergency replacement."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from front_helpers import KEY, OnCompute, front_of
from loadcoach import statement
from loadcoach.app import build_app
from loadcoach.cli import app as loadcoach_cli, serve
from loadcoach.config import Config, load_config
from loadcoach.log import Log
from loadcoach.users import Users
from test_app import GROUPS, add_user, fake_llama_server  # noqa: F401  (the last sets a check up)
from test_weightroom import desk, sign_in  # noqa: F401  (the first sets a check up)


def test_the_key_before_counts_for_a_while_and_a_sealed_key_opens_only_with_the_old_one(tmp_path, monkeypatch):
    path = str(tmp_path / "machine.key")
    with pytest.raises(ValueError, match="at least 32"):
        statement.replace_key(path, "short")
    statement.replace_key(path, "first-" + "a" * 40)
    statement.replace_key(path, "second-" + "b" * 40)
    assert statement.keys(path) == ["second-" + "b" * 40, "first-" + "a" * 40]
    assert (tmp_path / "machine.key").stat().st_mode & 0o777 == 0o600 == (tmp_path / "machine.key.previous").stat().st_mode & 0o777
    monkeypatch.setattr("loadcoach.statement.OVERLAP", 0)
    assert statement.keys(path) == ["second-" + "b" * 40]  # its time is up

    sealed = statement.seal("old key", "salt", "x" * 43)  # as long as a key secrets.token_urlsafe(32) makes
    assert "x" * 10 not in bytes.fromhex(sealed).decode("latin-1") and statement.unseal("old key", "salt", sealed) == "x" * 43
    try:
        opened = statement.unseal("another key", "salt", sealed)
    except UnicodeDecodeError:
        opened = None  # nothing that reads as a key, as compute's answer to it says
    assert opened != "x" * 43


def test_a_new_key_goes_to_compute_first_sealed_and_then_is_used_here(fake_llama_server, tmp_path):
    compute_key = tmp_path / "compute" / "machine.key"  # each machine its own file, as on two machines
    compute_key.parent.mkdir()
    compute_key.write_text(KEY)
    settings = Config(models={}, gpus=("GPU0",), llama_server=fake_llama_server, nvidia_smi="no-such-program", database=str(tmp_path / "core.db"),
                      machine_key_file=str(compute_key), front="http://front:8442")
    with front_of(lambda front: build_app(settings, None, front), Users(":memory:", GROUPS), tmp_path) as caller:
        admin = add_user(caller, "jordan", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        assert caller.post("/turnstile/machine-key", headers=plain).status_code == 403
        status = caller.get("/turnstile/machines/compute/machine-key", headers=admin).json()
        assert status["changed"] > 0 and status["rotation_days"] == 0
        old = statement.make(KEY, "GET", "/loadcoach/status", b"", statement.caller_of(caller.users.by_name("jordan")))[0]

        made = caller.post("/turnstile/machine-key", headers=admin)
        new = statement.read_key(str(tmp_path / "machine.key"))
        assert made.status_code == 200 and new != KEY and statement.read_key(str(compute_key)) == new  # the same new key on both
        assert statement.read_key(str(compute_key) + ".previous") == KEY
        assert caller.get("/loadcoach/status", headers=admin).status_code == 200  # signed with the new key now
        assert OnCompute(caller).get("/loadcoach/status", headers={statement.HEADER: old}).status_code == 200  # one already on its way still counts
        assert OnCompute(caller).post("/loadcoach/machine-key", json={"salt": "s", "sealed": "00"}).status_code == 401  # only through turnstile
        scheduled = caller.post("/turnstile/machines/compute/machine-key", headers=admin, json={"rotation_days": 30})
        assert scheduled.status_code == 200 and scheduled.json()["rotation_days"] == 30
        with caller.users.db:
            caller.users.db.execute("UPDATE machine_key_rotations SET next_at=0 WHERE machine='compute'")
        before_schedule = statement.read_key(str(tmp_path / "machine.key"))
        caller.portal.call(caller.front.state.rotate_due_keys_once)
        assert statement.read_key(str(tmp_path / "machine.key")) != before_schedule
        compute_log = [entry["what"] for entry in Log(str(tmp_path / "core.db")).read(kind="machine-key")]
    assert compute_log == ["replaced", "replaced"] and [entry["user"] for entry in caller.log.read(kind="machine-key")] == ["jordan", "jordan"]


def test_the_settings_page_makes_a_new_key_for_someone_who_may(desk):
    browser, _, _ = desk
    sign_in(browser, "pat")
    assert "machine key" not in browser.get("/settings").text.lower() or browser.get("/settings").status_code == 403
    sign_in(browser, "jordan")
    page = browser.get("/compute-nodes").text
    assert 'id="machine-key"' in page and "loadcoach --replace-key" in page
    assert "Last rotated" in page and 'name="rotation_days"' in page
    assert "every 14 days" in browser.post("/compute-nodes/machine-key/schedule", data={"machine": "compute", "rotation_days": "14"}).text
    made = browser.post("/compute-nodes/machine-key", data={"machine": "compute"})
    assert made.status_code == 200 and "A new machine key is in use on front and compute." in made.text


def test_the_key_is_set_by_hand_and_an_emergency_answers_anyone_here_and_keeps_no_log(fake_llama_server, tmp_path, monkeypatch):
    file = tmp_path / "loadcoach.toml"
    file.write_text('host = "10.0.0.9"\nmachine_key_file = "machine.key"\n')
    (tmp_path / "machine.key").write_text(KEY)
    typed = CliRunner().invoke(loadcoach_cli, ["--config", str(file), "--replace-key"], input="c" * 43 + "\n")
    assert typed.exit_code == 0 and "Replaced" in typed.output
    assert statement.keys(str(tmp_path / "machine.key")) == ["c" * 43, KEY]

    monkeypatch.setattr("loadcoach.cli.subprocess.run", lambda *args, **more: subprocess.CompletedProcess(args, 0))
    with pytest.raises(SystemExit, match="Stop it first"):  # the service still has the cards
        serve(file, emergency=True)

    config = replace(load_config(file), host="127.0.0.1", machine_key_file="", front="", llama_server=fake_llama_server, nvidia_smi="no-such-program")
    from starlette.testclient import TestClient
    with TestClient(build_app(config, emergency=True)) as here:
        assert here.get("/loadcoach/status").status_code == 200  # no key, no statement
        assert here.post("/loadcoach/drain", json={"why": "a test"}).status_code == 200
    assert Log(config.database).read(kind="line") == []  # nothing of it was kept
