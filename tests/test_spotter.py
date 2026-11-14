"""spotter's checks. The programs it runs, nvidia-smi, systemctl and journalctl, are stood in for
by small scripts put first on the PATH, so that nothing real is read or restarted.
"""

import asyncio
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from loadcoach.log import Log
from loadcoach.groups import Group
from loadcoach.settings import Changes
from loadcoach.users import Users
from front_helpers import OnCompute, front_of
from spotter.app import Settings, build_app, gathered, load_settings, number, series
from spotter import screen

GROUPS = {
    "admin": Group(can=frozenset({"restart-services", "power", "load", "manage-models"})),
    "plain": Group(can=frozenset({"load"})),
    "front": Group(can=frozenset({"sign-in"})),
}


@contextmanager
def made_up(tmp_path, monkeypatch, **more):
    """spotter on a machine whose cards, services and desktop are made up, beside a LoadCoach that is made up too.

    Yields the caller, each user's headers, the file every systemctl command lands in, the users,
    and `coach`: how busy the made-up LoadCoach says it is, and what it has been asked.
    """
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    asked = tmp_path / "asked.txt"  # every systemctl command that was run, one to a line

    def stand_in(name: str, script: str) -> None:
        (stand_ins / name).write_text("#!/bin/sh\n" + script)
        (stand_ins / name).chmod(0o755)

    # The second card's fan says nothing, as a card with no fan sensor does.
    stand_in("nvidia-smi", 'echo "0, RTX Made Up, 51, 36, 77.31, 45, 15756, 16311, 180.00"\necho "1, RTX Made Up, 53, [N/A], 86.14, 55, 1024, 16311, 180.00"\n')
    # systemd as it is here: it will sleep and restart the machine, and will not switch it off.
    stand_in("systemctl", f'echo "$@" >> {asked}\ncase "$2" in show) printf "ActiveState=active\\nSubState=running\\nNRestarts=2\\nMemoryCurrent=1048576\\nActiveEnterTimestamp=Thu 2026-10-08 10:00:00 PDT\\n";; '
                          'restart|suspend|reboot) ;; poweroff) echo "Interactive authentication required."; exit 1;; *) echo "Failed to $2: no such thing"; exit 5;; esac\n')  # fmt: skip
    stand_in("journalctl", 'echo "2026-10-08T10:00:00 $* started"\necho "2026-10-08T10:00:01 loadcoach ready"\n')
    (tmp_path / "idle").write_text("(uint64 4000,)")  # the desktop's answer: a key was touched four seconds ago
    stand_in("gdbus", f"cat {tmp_path / 'idle'}\n")
    stand_in("loginctl", 'printf "1000 jordan\n1001 pat\n1000 jordan\n"\n')
    monkeypatch.setenv("PATH", f"{stand_ins}:/usr/bin:/bin")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))  # so that a real desktop on the machine running the checks is never asked

    coach = {"busy": 0, "asked": []}

    async def answer(request):
        body = json.loads(await request.body() or b"{}")
        if request.headers.get("authorization") != "Bearer made-up-token":
            return JSONResponse({"error": {"message": "no"}}, status_code=401)
        if request.method == "POST":
            coach["asked"].append((request.url.path.rsplit("/", 1)[1], body))
        return JSONResponse({"busy": coach["busy"], "downloads": coach.get("downloads", 0), "batches": coach.get("batches", 0)})

    loadcoach = Starlette(routes=[Route("/loadcoach/{what}", answer, methods=["GET", "POST"])])
    (tmp_path / "token").write_text("made-up-token")

    database = str(tmp_path / "loadcoach.db")
    users = Users(str(tmp_path / "accounts.db"), GROUPS)  # the accounts are the control node's: turnstile is in the control node, as there
    keys = {}
    for name, group in (("jordan", "admin"), ("pat", "plain"), ("panel", "front")):
        users.add(name, group)
        keys[name] = {"authorization": "Bearer " + users.new_key(name)}
    users.change("pat", password="correct horse")
    settings = Settings(**{"services": {"loadcoach": "loadcoach.service"}, "system_services": {"ollama": "ollama.service"}, "machine_key_file": str(tmp_path / "machine.key"), "database": database,
                           "history": str(tmp_path / "spotter.db"), "token_file": str(tmp_path / "token"), "sample_every": 0.05,
                           "disks": (str(tmp_path), "/no/such/folder"), **more})  # fmt: skip
    link = httpx.AsyncClient(transport=httpx.ASGITransport(app=loadcoach), base_url="http://loadcoach")
    with front_of(lambda front: build_app(settings, link), users, tmp_path, port=8444) as caller:
        yield caller, keys, asked, users, coach


@pytest.fixture
def machine(tmp_path, monkeypatch):
    with made_up(tmp_path, monkeypatch) as (caller, keys, asked, users, _):
        yield caller, keys, asked, users


def wait_until(wanted, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if found := wanted():
            return found
        time.sleep(0.02)
    raise AssertionError("it did not happen in time")


def test_anyone_signed_in_sees_the_machine_and_a_missing_reading_is_not_a_zero(machine):
    caller, keys, _, _ = machine
    assert caller.get("/spotter/now").status_code == 401
    seen = caller.get("/spotter/now", headers=keys["pat"]).json()

    first, second = seen["cards"]
    assert (first["card"], first["temperature"], first["load"], first["power"], first["power_limit"]) == (0, 51, 45, 77.31, 180)
    assert first["memory_used"] == 15756 * 2**20 and first["memory_total"] == 16311 * 2**20  # in bytes, like every other size
    assert first["fan"] == 36 and second["fan"] is None  # said nothing, which is not the same as standing still
    assert seen["memory"]["total"] > seen["memory"]["used"] > 0 and seen["up_for"] > 0
    assert [disk["path"] for disk in seen["disks"]] == [seen["disks"][0]["path"]] and seen["disks"][0]["free"] > 0  # the folder that is not there is left out

    service = seen["services"][0]
    assert (service["name"], service["unit"], service["state"], service["restarts"], service["memory"]) == ("loadcoach", "loadcoach.service", "active", 2, 1048576)
    assert [(one["name"], one["unit"], one["root"]) for one in seen["services"]] == [("loadcoach", "loadcoach.service", False), ("ollama", "ollama.service", True)]
    core_only = caller.get("/spotter/now?system_services=", headers=keys["pat"]).json()
    selected = caller.get("/spotter/now?system_services=ollama", headers=keys["pat"]).json()
    assert [service["name"] for service in core_only["services"]] == ["loadcoach"]
    assert [service["name"] for service in selected["services"]] == ["loadcoach", "ollama"]
    assert seen["may_act"] is False and caller.get("/spotter/now", headers=keys["jordan"]).json()["may_act"] is True
    assert number("[N/A]") is None and number("12.5") == 12.5


@pytest.mark.parametrize('load,state,ended,installed', [('loaded', 'inactive', 0, True),
    ('not-found', 'inactive', 0, False), ('not-found', 'inactive', 1, False),
    ('loaded', 'failed', 0, True), ('loaded', 'inactive', 1, None)])
def test_service_installation_is_independent_of_running_state(machine, tmp_path, load, state, ended, installed):
    caller, keys, _, _ = machine
    (tmp_path / 'bin/systemctl').write_text(
        f'#!/bin/sh\nprintf "LoadState={load}\\nActiveState={state}\\nSubState=dead\\n"\nexit {ended}\n')
    service = caller.get('/spotter/now?system_services=', headers=keys['pat']).json()['services'][0]
    assert service['installed'] is installed
    assert service['state'] == (state if not ended else 'unknown')


def test_only_a_named_service_and_a_listed_action_and_only_with_the_ability(machine, tmp_path):
    caller, keys, asked, _ = machine
    assert caller.post("/spotter/services/loadcoach/restart", headers=keys["pat"]).status_code == 403
    assert not asked.exists() or "restart" not in asked.read_text()

    done = caller.post("/spotter/services/loadcoach/restart", headers=keys["jordan"])
    assert done.status_code == 200 and done.json()["state"] == "active"
    assert "--user restart loadcoach.service" in asked.read_text().splitlines()

    # Nothing that is not in the settings, and nothing that is not one of the three actions.
    for address in ("/spotter/services/sshd/restart", "/spotter/services/loadcoach/enable", "/spotter/services/loadcoach.service/restart",
                    "/spotter/services/loadcoach/restart%20;reboot", "/spotter/services/..%2Fetc/restart"):  # fmt: skip
        assert caller.post(address, headers=keys["jordan"]).status_code == 404
    ran = [line.split()[1] for line in asked.read_text().splitlines()]
    assert set(ran) <= {"show", "restart"} and ran.count("restart") == 1  # what a caller sent was never run

    failed = caller.post("/spotter/services/loadcoach/stop", headers=keys["jordan"])  # the stand-in refuses to stop
    assert failed.status_code == 502 and "no such thing" in failed.json()["error"]["message"]
    kept = Log(str(tmp_path / "loadcoach.db")).read(kind="service")
    assert [(entry["what"], entry["user"], entry["name"], entry["worked"]) for entry in kept] == [("restart", "jordan", "loadcoach", True), ("stop", "jordan", "loadcoach", False)]

    # One of root's services: asked of the system, not of this account, and never with a question for a password.
    assert caller.post("/spotter/services/ollama/restart", headers=keys["jordan"]).json()["root"] is True
    assert "--no-ask-password restart ollama.service" in asked.read_text().splitlines()
    assert "-u ollama.service" in caller.get("/spotter/services/ollama/journal", headers=keys["jordan"]).json()["lines"][0]
    assert "--user -u loadcoach.service" in caller.get("/spotter/services/loadcoach/journal", headers=keys["jordan"]).json()["lines"][0]

    assert caller.get("/spotter/services/loadcoach/journal", headers=keys["pat"]).status_code == 403
    assert caller.get("/spotter/services/loadcoach/journal?lines=2", headers=keys["jordan"]).json()["lines"][1].endswith("loadcoach ready")
    # A stretch of time, and under a name that is no service, what the kernel said about the cards and about memory.
    stretch = caller.get("/spotter/services/loadcoach/journal?since=1000&until=1090.7&lines=5000", headers=keys["jordan"]).json()["lines"][0]
    assert "--since=@1000 --until=@1091 -n 5000" in stretch
    kernel = caller.get("/spotter/services/kernel/journal", headers=keys["jordan"]).json()
    assert kernel["name"] == "kernel" and "_TRANSPORT=kernel -g NVRM|Xid|nvidia|Out of memory" in kernel["lines"][1]  # turned round: journalctl gives these newest first
    assert caller.get("/spotter/services/loadcoach/journal?since=noon", headers=keys["jordan"]).status_code == 400
    assert caller.post("/spotter/services/kernel/restart", headers=keys["jordan"]).status_code == 404  # it can be read, and nothing else


def test_readings_are_kept_and_given_back_as_steps_over_a_stretch_of_time(machine):
    caller, keys, _, _ = machine
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:  # until spotter has kept a few readings of its own
        told = caller.get("/spotter/history?seconds=60&points=6", headers=keys["pat"]).json()
        if sum(value is not None for value in told["series"].get("GPU 0 temperature", [])) >= 1 and "processor load" in told["series"]:
            break
        time.sleep(0.05)
    assert told["step"] == 10 and len(told["series"]["GPU 0 temperature"]) == 6
    fine = caller.get("/spotter/history?seconds=60&points=1000", headers=keys["pat"]).json()
    assert len(fine["series"]["GPU 0 temperature"]) == 600  # never a step shorter than two readings apart: here, a tenth of a second
    assert told["series"]["GPU 0 temperature"][-1] == 51 and told["series"]["GPU 0 temperature"][0] is None  # nothing was read a minute ago
    assert "GPU 1 fan" not in told["series"] and "GPU 0 fan" in told["series"]  # a reading that was never given has no line
    assert caller.get("/spotter/history?seconds=soon", headers=keys["pat"]).status_code == 400

    # The arithmetic, on readings whose times are known: two in the first step, none in the second, one in the third.
    rows = [(100.0, json.dumps({"heat": 40})), (104.0, json.dumps({"heat": 50})), (125.0, json.dumps({"heat": 70}))]
    assert gathered(rows, 100, 130, 3) == {"begin": 100, "end": 130, "step": 10, "series": {"heat": [45.0, None, 70.0]}}
    reading = {"processor": {"load": None, "temperature": 60}, "memory": {"used": 5}, "cards": [], "disks": [{"path": "/", "free": 9}]}
    assert series(reading) == {"processor temperature": 60, "memory used": 5, "free on /": 9}


def test_a_prepared_model_file_is_verified_and_installed_atomically(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    content = b"a complete model file"
    digest = __import__("hashlib").sha256(content).hexdigest()
    with made_up(tmp_path, monkeypatch, places={"models": str(models)}) as (caller, keys, _, _, _):
        refused = caller.post("/spotter/files/prepare", headers=keys["pat"], json={"place": "models", "relative": "repo/model.gguf", "size": len(content), "sha256": digest})
        prepared = caller.post("/spotter/files/prepare", headers=keys["jordan"], json={"place": "models", "relative": "repo/model.gguf", "size": len(content), "sha256": digest})
        received = caller.put(prepared.json()["upload"], content=content)
    target = models / "repo" / "model.gguf"
    assert refused.status_code == 403 and prepared.status_code == 201 and received.status_code == 201
    assert target.read_bytes() == content and not target.with_name("model.gguf.part").exists()


def test_identical_unused_upload_can_be_retried_only_by_its_owner(tmp_path,monkeypatch):
    models=tmp_path/'models';models.mkdir()
    content=b'complete retry bytes';digest=__import__('hashlib').sha256(content).hexdigest()
    body={'place':'models','relative':'retry.gguf','size':len(content),'sha256':digest}
    with made_up(tmp_path,monkeypatch,places={'models':str(models)}) as (caller,keys,_,users,_):
        first=caller.post('/spotter/files/prepare',headers=keys['jordan'],json=body)
        again=caller.post('/spotter/files/prepare',headers=keys['jordan'],json=body)
        assert first.status_code==again.status_code==201 and first.json()['upload']==again.json()['upload']
        assert caller.post('/spotter/files/prepare',headers=keys['jordan'],json={**body,'sha256':'a'*64}).status_code==409
        users.add('other','admin')
        other={'authorization':'Bearer '+users.new_key('other')}
        assert caller.post('/spotter/files/prepare',headers=other,json=body).status_code==409
        assert caller.put(again.json()['upload'],content=content).status_code==201
        assert (models/'retry.gguf').read_bytes()==content

def test_the_machine_sleeps_once_loadcoach_has_emptied_unless_told_not_to_wait(tmp_path, monkeypatch):
    with made_up(tmp_path, monkeypatch) as (caller, keys, asked, _, coach):
        ran = lambda: [line for line in asked.read_text().splitlines() if " show " not in line] if asked.exists() else []  # noqa: E731
        power = lambda: caller.get("/spotter/now", headers=keys["jordan"]).json()["power"]  # noqa: E731
        ask = lambda action, who="jordan", **body: caller.post(f"/spotter/power/{action}", json=body, headers=keys[who])  # noqa: E731
        coach["busy"] = 2

        assert ask("sleep", "pat").status_code == 403 and ask("hibernate").status_code == 404
        assert power()["logged_in_users"] == 2 and "desktop" not in power() and power()["may"] is True

        assert ask("sleep").status_code == 202
        waiting = wait_until(lambda: power()["pending"] if power()["pending"]["waiting_for"] == 2 else None)
        assert (waiting["action"], waiting["by"], waiting["forced"]) == ("sleep", "jordan", False)
        assert coach["asked"] == [("drain", {"why": "compute is going to sleep."})] and ran() == []  # LoadCoach takes no more, and nothing has been done yet
        assert ask("reboot").status_code == 409  # one thing at a time

        assert ask("cancel").json() == {"pending": None} and ask("cancel").status_code == 409
        assert power()["pending"] is None and coach["asked"][-1] == ("drain", {"on": False}) and ran() == []  # called off: requests are taken again

        coach["asked"].clear()
        ask("sleep")
        time.sleep(0.2)
        assert ran() == []  # still two requests at LoadCoach
        coach["busy"] = 0
        wait_until(lambda: power()["pending"] is None)
        # Emptied, then unloaded so that sleep is quick, then open again so that it wakes ready, then asleep.
        assert coach["asked"][:3] == [("drain", {"why": "compute is going to sleep."}), ("unload", {}), ("drain", {"on": False})]
        assert ran() == ["--no-ask-password suspend --check-inhibitors=no"]

        # Not waiting: whatever LoadCoach is doing, and without a word to it first.
        coach["busy"], coach["asked"][:] = 5, []
        assert ask("reboot", force=True).status_code == 202
        wait_until(lambda: ran()[-1] == "--no-ask-password reboot --check-inhibitors=no")
        assert coach["asked"] == [] and power()["pending"]["forced"] is True  # it stays on its way: the machine is going down
        ask("cancel")

        ask("off", force=True)  # systemd says no, as it does when the rule that lets spotter do this is missing
        refused = wait_until(lambda: power()["failed"])
        assert (refused["action"], refused["said"]) == ("off", "Interactive authentication required.") and power()["pending"] is None

    kept = [(entry["what"], entry["user"], entry.get("worked")) for entry in Log(str(tmp_path / "loadcoach.db")).read(kind="power")]
    assert kept == [("called-off", "jordan", None), ("sleep", "jordan", True), ("reboot", "jordan", True), ("called-off", "jordan", None), ("off", "jordan", False)]


def test_the_machine_puts_itself_to_sleep_when_nothing_has_used_it_for_long_enough(tmp_path, monkeypatch):
    monkeypatch.setattr("spotter.app.FRESH", 0)  # every look is a fresh one, so the test need not wait for a reading to age
    # A machine that sleeps after three tenths of a second unused. The processor is busy running these checks, so it is not counted.
    quick = {"sleep_after": 0.3 / 60, "busy_processor": 101}
    (tmp_path / "one").mkdir(), (tmp_path / "two").mkdir()
    with made_up(tmp_path / "one", monkeypatch, busy_gpu=101, **quick) as (caller, keys, asked, _, coach):
        slept = lambda: [line for line in asked.read_text().splitlines() if "suspend" in line] if asked.exists() else []  # noqa: E731
        used_by = lambda: caller.get("/spotter/now", headers=keys["pat"]).json()["power"]["used_by"]  # noqa: E731
        (tmp_path / "one" / "idle").write_text("(uint64 0,)")  # someone is typing
        time.sleep(0.7)
        assert slept() == [] and used_by() == "the keyboard or mouse"

        coach["busy"] = 1
        (tmp_path / "one" / "idle").write_text("(uint64 9000000,)")  # they left hours ago, but LoadCoach is answering someone
        time.sleep(0.7)
        assert slept() == [] and used_by() == "1 request at LoadCoach"

        coach["busy"], coach["downloads"] = 0, 1  # nobody is asking, and a model file is coming down: hours of it, with the machine barely working
        time.sleep(0.7)
        assert slept() == [] and used_by() == "a download at LoadCoach"

        coach["downloads"], coach["batches"] = 0, 1  # requests that were handed in to be answered later, and have not all been
        time.sleep(0.7)
        assert slept() == [] and used_by() == "a batch at LoadCoach"

        coach["batches"] = 0
        wait_until(slept)
        # Nobody asked for this sleep, so it gives way to anything that has asked the system not to sleep.
        assert slept()[0] == "--no-ask-password suspend --check-inhibitors=yes"
        assert coach["asked"][:3] == [("drain", {"why": "compute is going to sleep."}), ("unload", {}), ("drain", {"on": False})]
    first = Log(str(tmp_path / "one" / "loadcoach.db")).read(kind="power")[0]
    assert (first["what"], first["user"], first["unasked"], first["worked"]) == ("sleep", "spotter", True, True)

    # A card at work holds sleep off, whatever it is working on: the made-up cards are at 45% and 55%.
    with made_up(tmp_path / "two", monkeypatch, **quick) as (caller, keys, asked, _, coach):
        (tmp_path / "two" / "idle").write_text("(uint64 9000000,)")
        time.sleep(0.7)
        assert not asked.exists() or "suspend" not in asked.read_text()
        assert caller.get("/spotter/now", headers=keys["pat"]).json()["power"]["used_by"] == "GPU 0 at 45%"


def test_spotter_believes_only_what_turnstile_signed(machine):
    caller, keys, _, _ = machine
    straight = OnCompute(caller)  # round turnstile, on the network
    refused = straight.get("/spotter/now", headers={**keys["jordan"], "x-request-id": "caller-chosen"})
    assert refused.status_code == 401  # an API key means nothing here
    assert len(refused.headers["x-request-id"]) == 32 and refused.headers["x-request-id"] != "caller-chosen"
    assert caller.get("/spotter/now", headers=keys["pat"]).status_code == 200  # through turnstile, with the same key
    assert caller.post("/spotter/sign-in", json={"name": "pat", "password": "correct horse"}, headers=keys["panel"]).status_code in (404, 405)  # signing in is the control node's


def test_the_settings_name_services_by_their_unit_and_nothing_else(tmp_path):
    (tmp_path / "loadcoach.toml").write_text('machine_key_file = "keys/machine.key"\n')
    file = tmp_path / "spotter.toml"
    file.write_text('port = 9000\ndisks = ["/", "/mnt/ssd"]\n[services]\nloadcoach = "loadcoach.service"\n')
    settings = load_settings(file)
    assert (settings.port, settings.services, settings.disks) == (9000, {"loadcoach": "loadcoach.service"}, ("/", "/mnt/ssd"))
    assert settings.database == str(tmp_path / "loadcoach.db") and settings.history == str(tmp_path / "spotter.db")
    assert settings.machine_key_file == str(tmp_path / "keys/machine.key")  # the key turnstile signs with, from LoadCoach's own file
    # LoadCoach is found from its own file: where it answers, and the token it leaves for programs on this machine.
    assert (settings.compute, settings.token_file, settings.sleep_after) == ("http://127.0.0.1:8442", str(tmp_path / "loadcoach.cli-token"), 0)
    Changes(settings.database).put("loadcoach", "models_folder", "/srv/models", "owner")
    assert load_settings(file).places == {
        "llm": "/srv/models/llm", "adapters": "/srv/models/adapters",
        "projectors": "/srv/models/projectors", "image": "/srv/models/image", "audio": "/srv/models/audio", "encoders":"/srv/models/encoders"}
    Changes(settings.database).put("loadcoach", "models_storage_folder", "/mnt/library", "owner")
    assert load_settings(file).places["storage_llm"] == "/mnt/library/llm"
    assert load_settings(file).places["storage_image"] == "/mnt/library/image"
    (tmp_path / "loadcoach.toml").write_text('host = "10.0.0.5"\nport = 9442\n')
    file.write_text('sleep_after = 60\n[services]\nloadcoach = "loadcoach.service"\n[system_services]\nollama = "ollama.service"\n')
    settings = load_settings(file)
    assert (settings.compute, settings.sleep_after, settings.system_services) == ("http://10.0.0.5:9442", 60, {"ollama": "ollama.service"})
    for wrong in ('[services]\nx = "../../bin/sh"', '[services]\nx = "a b.service"', '[services]\nx = "loadcoach"', "colour = 1",
                  '[system_services]\nx = "/etc/passwd"', '[services]\nx = "a.service"\n[system_services]\nx = "b.service"'):  # fmt: skip
        file.write_text(wrong)
        with pytest.raises(SystemExit):
            load_settings(file)
    assert Path(__file__).parent.parent.joinpath("settings/examples/spotter.toml").exists()


def test_a_wayland_desktop_is_asked_once_and_says_only_when_its_screen_goes_unused_or_is_used_again(tmp_path, monkeypatch):
    monkeypatch.setattr("spotter.screen.IDLE", 1)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    (tmp_path / "wayland-0").touch()  # left behind by a desktop that has gone: nothing answers there
    asked, desktops = [], []

    async def desktop(reader, writer):
        """A made-up desktop that offers a seat, the notifier, and a screen. A request has the same form as an event."""
        desktops.append(writer)
        while True:
            try:
                sender, kind, body = await screen.event(reader)
            except asyncio.IncompleteReadError:
                return
            asked.append((sender, kind, body))
            if (sender, kind) == (1, 1):
                for name, offered, version in ((7, "wl_seat", 9), (8, "ext_idle_notifier_v1", 2), (9, "wl_output", 4)):
                    writer.write(screen.message(2, 0, screen.number(name), screen.text(offered), screen.number(version)))
            elif (sender, kind) == (1, 0):
                writer.write(screen.message(3, 0, screen.number(0)))  # the list has all come

    async def check():
        server = await asyncio.start_unix_server(desktop, tmp_path / "wayland-1")
        watch = screen.Wayland()
        assert watch.seconds() is None  # it starts looking, and knows nothing yet
        while not watch.answering:
            await asyncio.sleep(0.01)
        assert watch.seconds() is None  # asked too recently for its silence to mean anything
        await asyncio.sleep(1)
        assert watch.seconds() == 0  # a second of silence: something was touched within the second
        desktops[0].write(screen.message(6, 0))  # nothing has been touched for a second
        await asyncio.sleep(0.1)
        assert 1 <= watch.seconds() < 1.5
        desktops[0].write(screen.message(6, 1))  # and now something has
        await asyncio.sleep(0.1)
        assert watch.seconds() == 0
        desktops[0].close()  # the desktop goes, as at signing out
        await asyncio.wait_for(watch.listening, 1)
        assert watch.seconds() is None
        server.close()

    asyncio.run(check())
    # It took a seat and the notifier, and asked, as version 2 can, about the keys and the mouse of that seat alone, after a second.
    assert [(sender, kind) for sender, kind, _ in asked] == [(1, 1), (1, 0), (2, 0), (2, 0), (5, 2)]
    assert asked[2][2] == screen.number(7) + screen.text("wl_seat") + screen.number(1) + screen.number(4)
    assert asked[3][2] == screen.number(8) + screen.text("ext_idle_notifier_v1") + screen.number(2) + screen.number(5)
    assert asked[4][2] == screen.number(6) + screen.number(1000) + screen.number(4)


def test_logind_is_believed_only_for_sessions_at_the_screen_that_have_said(tmp_path, monkeypatch):
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    since = int((time.time() - 300) * 1e6)
    # Ours, at the screen, unused for five minutes. Pat's, over SSH, in use. Sam's, at the screen, never said.
    (stand_ins / "loginctl").write_text(f"""#!/bin/sh
case "$1" in
  list-sessions) printf "3 {os.getuid()} jordan seat0 1199 user tty1 no -\n4 1001 pat - 99 user pts/0 no -\n5 1002 sam seat0 7 user tty2 no -\n";;
  show-session) printf "User={os.getuid()}\nSeat=seat0\nRemote=no\nIdleHint=yes\nIdleSinceHint={since}\n\nUser=1001\nSeat=\nRemote=yes\nIdleHint=no\nIdleSinceHint={since}\n\nUser=1002\nSeat=seat0\nRemote=no\nIdleHint=no\nIdleSinceHint=0\n";;
esac
""")
    (stand_ins / "loginctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_ins}:/usr/bin:/bin")
    assert 299 < asyncio.run(screen.logind(others_only=False)) < 302
    assert asyncio.run(screen.logind(others_only=True)) is None  # our own desktop was asked instead


def test_macos_is_asked_for_its_own_count(tmp_path, monkeypatch):
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    (stand_ins / "ioreg").write_text('#!/bin/sh\nprintf \'  | |   "HIDIdleKeyboardTime" = 9\\n  | |   "HIDIdleTime" = 42500000000\\n\'\n')
    (stand_ins / "ioreg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_ins}:/usr/bin:/bin")
    assert asyncio.run(screen.macos()) == 42.5


def test_spotter_refreshes_versions_in_its_background_worker(tmp_path, monkeypatch):
    from loadcoach.component_versions import Versions
    refreshed = []

    async def refresh(self, **kwargs):
        refreshed.append((self.source.store.path, self.every, kwargs))

    monkeypatch.setattr(Versions, "refresh", refresh)
    with made_up(tmp_path, monkeypatch, component_versions_every=5):
        # Control node has its own checker; verify this node's configured store and interval.
        own = lambda: [row for row in refreshed if row[0] == str(tmp_path / "loadcoach.db")]
        wait_until(own)
        assert own() == [(str(tmp_path / "loadcoach.db"), 300, {"settings": None})]
