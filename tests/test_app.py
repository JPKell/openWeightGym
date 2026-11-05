"""LoadCoach's checks. A small fake stands in for llama-server; no GPU or model is touched.

The fake is a real process on a real port (tests/fake_llama_server.py), so these tests exercise
the true path: building the command, waiting for /health, forwarding, swapping and stopping.
"""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from loadcoach.app import build_app
from loadcoach.capture import answer_text, request_text, resolve
from loadcoach.cli import as_toml, group_command, log_command, main, model_command, server_command, user_command, verify_command
from loadcoach import files
from loadcoach.config import Config, load_config
from loadcoach.files import carry
from loadcoach.gguf import facts, read_metadata
from fake_hub import FakeHub
from loadcoach.log import Log, token_counts
from loadcoach.models import Models
from loadcoach.runner import Runner, Ticket, command_line, free_port
from loadcoach.groups import ABILITIES, OWN_KEY, Group, adjusted, beyond, check_own, read_group, section_of
from loadcoach.users import Users, check_password, code_at
from front_helpers import KEY, OnCompute, front_of
from loadcoach import statement
from typer.testing import CliRunner
from weightroom.app import cli as weightroom_cli

FAKE = Path(__file__).with_name("fake_llama_server.py")


@pytest.fixture
def fake_llama_server(tmp_path):
    """An executable that LoadCoach can start as if it were llama-server."""
    script = tmp_path / "llama-server"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def fake_cards(tmp_path, monkeypatch):
    """Two made-up cards of 16000 MiB each, and an executable that answers for them as nvidia-smi would. Gives the executable and the folder the cards' state is in."""
    folder, script = tmp_path / "cards", tmp_path / "nvidia-smi"
    folder.mkdir()
    monkeypatch.setenv("FAKE_CARDS", str(folder))  # the stand-ins for llama-server find it here too
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE.with_name('fake_nvidia_smi.py')} \"$@\"\n")
    script.chmod(0o755)
    return str(script), folder


@pytest.fixture(autouse=True)
def in_an_empty_folder(tmp_path, monkeypatch):
    """Every test runs in a folder of its own, so the model files it makes up land there."""
    monkeypatch.chdir(tmp_path)


CARDS = ("GPU0", "GPU1")


def model(file, gpus=None, **flags):
    """One model's settings. Without `gpus` it needs every card, so only one such model fits."""
    settings = {"flags": {"model": file, "ctx-size": 2048, **flags}}
    return settings if gpus is None else {**settings, "gpus": gpus}


GROUPS = {
    # Each ability by name, as a file would list them, and the most sensitive work: nobody hands out what they may not do themselves.
    "admin": Group(can=frozenset(ABILITIES) - OWN_KEY - {"superuser"}, max_sensitivity="restricted"),
    "plain": Group(can=frozenset({"load"})),  # may ask for models, and nothing else
    "front": Group(can=frozenset({"sign-in", "mirror-users", "front-wake"})),
}


def gateway(fake_llama_server, models=None, groups=GROUPS, web=None, **config):
    """LoadCoach on a machine with two cards. `small` and `big` each need both, unless told otherwise.

    It starts with no users, which means anyone on this machine may do anything, as a fresh
    install does. Tests about users add some with `add_user`.
    """
    models = models or {"small": model("small.gguf"), "big": model("big.gguf", **{"split-mode": "layer"})}
    for settings in models.values():  # LoadCoach refuses a model whose file is missing; the fake needs no content
        file = Path(settings["flags"]["model"])
        if file.parent.is_dir() and not file.exists():
            file.touch()
    # No nvidia-smi unless a test brings a stand-in: what this machine's real cards hold must not decide a check.
    # Turnstile's files, never in the folder a test happens to run in. Beside LoadCoach's own database when a test starts it again with it, so the roles last as long.
    folder = Path(config["database"]).with_name("front") if "database" in config else Path(tempfile.mkdtemp(prefix="front-"))
    folder.mkdir(exist_ok=True)
    outside = {"hosted_keys": config.pop("keys")} if "keys" in config else {}  # the outside services' key files are front's
    settings = Config(models=models, gpus=CARDS, llama_server=fake_llama_server, machine_key_file=str(folder / "machine.key"), front="http://front:8442",
                      **{"nvidia_smi": "no-such-program", "model_idle_minutes": 0, **config})
    # Turnstile in front, with the accounts, as on the two machines. `web` is how it reaches Hugging Face: a stand-in, where a test needs one.
    # The accounts last as long as LoadCoach's own database does: a test that starts it again with the same `database` finds them again.
    accounts = Users(str(Path(config["database"]).with_name("accounts.db")) if "database" in config else ":memory:", groups)
    return front_of(lambda front: build_app(settings, web, front), accounts, folder, **outside)


def add_user(caller, name, group, by=None):
    """Add a user and return the headers that sign requests as them. `by` is who does the adding."""
    added = caller.post("/loadcoach/users", json={"name": name, "group": group}, headers=by or {})
    assert added.status_code == 201, added.text
    return {"authorization": "Bearer " + added.json()["key"]}


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def gone(pid):
    """True once the process has ended, giving it a moment: stopping one takes a few milliseconds."""
    deadline = time.monotonic() + 3
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not alive(pid)


def test_naming_a_model_starts_it_with_its_settings_and_reuses_it(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        first = caller.post("/v1/chat/completions?x=1", json={"model": "big", "n": 1}).json()
        second = caller.post("/v1/chat/completions", json={"model": "big"}).json()

    started_with = " ".join(first["arguments"])
    assert "--host 127.0.0.1 --port" in started_with
    assert "--device GPU0,GPU1 --model big.gguf --ctx-size 2048 --split-mode layer" in started_with
    assert first["path"] == "/v1/chat/completions?x=1"
    assert first["body"] == '{"model":"big","n":1}'
    assert second["pid"] == first["pid"]  # already loaded, so not started again


def test_asking_for_another_model_stops_the_first_and_shutdown_stops_the_last(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        small = caller.post("/v1/chat/completions", json={"model": "small"}).json()
        big = caller.post("/v1/chat/completions", json={"model": "big"}).json()

        assert "--model big.gguf" in " ".join(big["arguments"])
        assert big["pid"] != small["pid"]
        assert not alive(small["pid"])
        assert alive(big["pid"])

    assert not alive(big["pid"])  # LoadCoach stopped, and took the model server with it


def test_a_streamed_answer_comes_back_as_it_was_sent(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        answer = caller.post("/v1/chat/completions", json={"model": "small", "stream": True})

    assert answer.headers["content-type"] == "text/event-stream"
    assert answer.content == b"data: one\n\ndata: two\n\n"


def test_the_models_endpoints_match_openai(fake_llama_server, tmp_path):
    file = tmp_path / "small.gguf"
    file.touch()
    os.utime(file, (1_700_000_000, 1_700_000_000))
    models = {"small": model(str(file)), "org/big:v2": model("gone/missing.gguf")}  # a name may hold a slash
    groups = {**GROUPS, "small-only": Group(models=("small",))}
    with gateway(fake_llama_server, models, groups=groups) as caller:
        listed = caller.get("/v1/models").json()
        one = caller.get("/v1/models/small").json()
        slashed = caller.get("/v1/models/org/big:v2").json()
        missing = caller.get("/v1/models/nope")
        admin = add_user(caller, "jordan", "admin")
        guest = add_user(caller, "guest", "small-only", by=admin)
        hidden = caller.get("/v1/models/org/big:v2", headers=guest)
        keyless = caller.get("/v1/models/small")

    assert listed.pop("available_machines") == ["compute"]
    assert listed.pop("unavailable_machines") == []
    # Two fields more than OpenAI gives are checked on their own, with the fingerprint. One of them comes only once the file was read.
    for entry in (*listed["data"], one, slashed):
        assert entry['model_id'].startswith('model_') and entry['name_history']
        entry.pop('model_id'), entry.pop('name_history')
        assert entry.pop('local_model_guard') is True
        assert entry.pop('engine_id') == 'llama_cpp'
        assert entry.pop('capabilities')['native_probes'] is True
        assert entry.pop('order') == 10
        entry.pop("fingerprint", None), entry.pop("build", None)
        assert entry.pop("content_sha256", None) in (None, hashlib.sha256(b"").hexdigest())  # the weights file's SHA-256, once it was read through
    assert listed == {
        "object": "list",
        "data": [
            {"id": "org/big:v2", "object": "model", "created": 0, "owned_by": "loadcoach"},
            {"id": "small", "object": "model", "created": 1_700_000_000, "owned_by": "loadcoach"},
        ],
    }
    assert one == listed["data"][1] and slashed == listed["data"][0]
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found" and "Available: org/big:v2, small" in missing.json()["error"]["message"]
    assert hidden.status_code == 404 and "Available: small" in hidden.json()["error"]["message"]  # as if it were not there
    assert keyless.status_code == 401


@pytest.mark.parametrize(
    "body, status, says",
    [
        ({"model": "huge"}, 404, "Available: big, small"),
        ({"messages": []}, 400, '"model" field'),
        ({"model": ["small"]}, 400, '"model" field'),
        ([1, 2], 400, '"model" field'),
    ],
)
def test_unknown_or_missing_model_is_explained_and_starts_nothing(fake_llama_server, body, status, says):
    with gateway(fake_llama_server) as caller:
        answer = caller.post("/v1/chat/completions", json=body)

    assert answer.status_code == status
    assert says in answer.json()["error"]["message"]


def test_a_model_that_will_not_load_is_a_502_that_says_why_and_the_next_request_still_works(fake_llama_server, capfd):
    models = {
        "broken": model("x.gguf", fail=True),
        "mistyped": model("y.gguf", **{"bad-flag": True}),
        "small": model("small.gguf"),
    }
    with gateway(fake_llama_server, models) as caller:
        broken = caller.post("/v1/chat/completions", json={"model": "broken"})
        mistyped = caller.post("/v1/chat/completions", json={"model": "mistyped"})
        working = caller.post("/v1/chat/completions", json={"model": "small"})
        listed = [entry for entry in caller.get("/loadcoach/events", params={"kind": "model"}).json()["events"] if entry["what"] == "failed"]
        failures = [entry["why"] for entry in listed]
        whole = caller.get(f"/loadcoach/events/{listed[0]['id']}").json()["entry"]
        refused = caller.get("/loadcoach/events", params={"kind": "request", "what": "refused"}).json()["events"]
        # What the model server itself refuses is passed on as it is, and its reason is kept with the ending.
        too_long = {"error": {"code": 400, "message": "the request exceeds the available context size", "type": "exceed_context_size_error"}}
        turned_down = caller.post("/v1/chat/completions", json={"model": "small", "status": 400, "reply": too_long})
        ending = caller.get("/loadcoach/events", params={"kind": "request", "limit": 1}).json()["events"][0]
        missing = caller.get("/loadcoach/events/99999").status_code
    passed_on = capfd.readouterr().out

    said = broken.json()["error"]["message"]
    assert broken.status_code == 502 and "exited with code 3 while loading broken" in said
    assert "allocating 12000.00 MiB on device 1: cudaMalloc failed: out of memory" in said  # the cause, not what followed
    assert "exiting due to model loading error" not in said
    assert "error: invalid argument: --bad-flag" in mistyped.json()["error"]["message"]
    assert failures == [said, mistyped.json()["error"]["message"]]  # the same words in the event log
    assert [entry["why"] for entry in refused] == failures  # and with each request that was turned away for it
    # Everything the model server printed before it gave up is kept with the failure, and handed out with that one entry only.
    assert "text" not in listed[0] and "cudaMalloc failed" in whole["text"]["output"] and "exiting due to model loading error" in whole["text"]["output"]
    assert turned_down.status_code == 400 and (ending["what"], ending["status"], ending["why"]) == ("done", 400, "the request exceeds the available context size")
    assert missing == 404
    assert working.status_code == 200
    assert "fake: starting with" in passed_on and "cudaMalloc failed" in passed_on  # still printed where it always was


def test_a_model_server_that_dies_leaves_its_last_words_in_the_log(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        first = ask(caller, "small").json()
        os.kill(first["pid"], 9)
        time.sleep(0.3)
        second = ask(caller, "small").json()
        died = [entry for entry in caller.get("/loadcoach/events", params={"kind": "model"}).json()["events"] if entry["what"] == "died"]
        printed = caller.get(f"/loadcoach/events/{died[0]['id']}").json()["entry"]["text"]["output"]
        # One that dies in the middle of an answer: the request is on record as failed, with why.
        with ThreadPoolExecutor() as pool:
            cut_short = pool.submit(caller.post, "/v1/chat/completions", json={"model": "small", "drip": 5})
            time.sleep(0.4)
            os.kill(second["pid"], 9)
            try:
                cut_short.result()
            except Exception:  # the caller's connection breaks too, in whatever way its client says so
                pass
        ending = caller.get("/loadcoach/events", params={"kind": "request", "limit": 1}).json()["events"][0]

    assert second["pid"] != first["pid"]  # started again for the next request
    assert len(died) == 1 and died[0]["model"] == "small" and "fake: starting with" in died[0]["why"] and "fake: starting with" in printed
    assert ending["what"] == "failed" and "stopped answering part-way" in ending["why"]


def test_test_page_opens_without_a_key_and_reveals_none(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, "jordan", "admin")
        answer = caller.get("/")

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/html")
    assert admin["authorization"].removeprefix("Bearer ") not in answer.text


def ask(caller, model, **body):
    return caller.post("/v1/chat/completions", json={"model": model, **body})


class _signed:
    """Wraps a test client so that its GETs are signed as one user. For wait_for_status."""

    def __init__(self, caller, headers):
        self.caller, self.headers = caller, headers

    def get(self, path):
        return self.caller.get(path, headers=self.headers)


def wait_for_status(caller, wanted):
    """Poll /loadcoach/status until `wanted(status)` is true, and return that status."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = caller.get("/loadcoach/status").json()
        if wanted(status):
            return status
        time.sleep(0.02)
    raise AssertionError(f"status never matched: {status}")


def scheduling_fields(loaded):
    """Check numeric RSS telemetry separately from exact scheduler-state assertions."""
    for model in loaded:
        assert all(model[key] is None or isinstance(model[key], int) and model[key] >= 0
                   for key in ('rss_mib', 'rss_peak_mib'))
    return [{key: value for key, value in model.items() if key not in ('rss_mib', 'rss_peak_mib')}
            for model in loaded]


def test_requests_for_one_model_go_one_at_a_time_and_status_shows_the_line(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        first = others.submit(ask, caller, "small", sleep=0.6)
        second = others.submit(ask, caller, "small", sleep=0.1)
        status = wait_for_status(caller, lambda s: s["running"] and s["waiting"])
        started = time.monotonic()
        first.result(), second.result()

        assert scheduling_fields(status["loaded"]) == [{"model": "small", "gpus": ["GPU0", "GPU1"], "state": "ready", "locked": False, "answers": [], "mib": None}]
        assert [ticket["model"] for ticket in status["running"]] == ["small"]
        assert [ticket["model"] for ticket in status["waiting"]] == ["small"]
        assert status["waiting"][0]["caller"] == "testclient"
        assert time.monotonic() - started > 0.2  # the second really did wait for the first
        assert status["waiting"][0]["priority"] == "normal"
        after = caller.get("/loadcoach/status").json()
        assert (after["lock"], after["running"], after["waiting"]) == (None, [], [])


def test_the_parallel_flag_lets_that_many_run_at_once(fake_llama_server):
    models = {"small": model("small.gguf", parallel=2)}
    with gateway(fake_llama_server, models) as caller, ThreadPoolExecutor() as others:
        both = [others.submit(ask, caller, "small", sleep=0.5) for _ in range(2)]
        status = wait_for_status(caller, lambda s: len(s["running"]) == 2)
        [request.result() for request in both]

    assert status["waiting"] == []


def test_the_line_is_first_come_first_served_across_a_swap(fake_llama_server):
    finished = []

    def ask_and_note(caller, label, model, **body):
        answer = ask(caller, model, **body).json()
        finished.append(label)
        return answer

    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        a = others.submit(ask_and_note, caller, "a", "small", sleep=0.5)
        wait_for_status(caller, lambda s: s["running"])
        b = others.submit(ask_and_note, caller, "b", "big")
        wait_for_status(caller, lambda s: len(s["waiting"]) == 1)
        c = others.submit(ask_and_note, caller, "c", "small")
        a, b, c = a.result(), b.result(), c.result()

    assert finished == ["a", "b", "c"]
    assert "--model big.gguf" in " ".join(b["arguments"])
    assert c["pid"] != a["pid"]  # small was loaded again after big, not served out of turn


def test_waiting_too_long_is_a_503_that_says_to_try_again(fake_llama_server):
    with gateway(fake_llama_server, max_wait=0.3) as caller, ThreadPoolExecutor() as others:
        slow = others.submit(ask, caller, "small", sleep=1.0)
        wait_for_status(caller, lambda s: s["running"])
        impatient = ask(caller, "small")
        left_the_line = caller.get("/loadcoach/status").json()["waiting"]
        slow.result()
        afterwards = ask(caller, "small")

    assert impatient.status_code == 503
    assert impatient.headers["retry-after"] == "10"
    assert "Still in line after 0.3 seconds" in impatient.json()["error"]["message"]
    assert left_the_line == []
    assert afterwards.status_code == 200


def test_unload_frees_the_model_and_the_next_request_loads_it_again(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        before = ask(caller, "small").json()
        unloaded = caller.post("/loadcoach/unload").json()
        gone = not alive(before["pid"])
        after = ask(caller, "small").json()

    assert unloaded["loaded"] == []
    assert gone
    assert after["pid"] != before["pid"]


def device(answer):
    """The cards a fake model server was started on, from its own description of itself."""
    arguments = answer["arguments"]
    return arguments[arguments.index("--device") + 1]


ONE_CARD_EACH = {name: model(f"{name}.gguf", gpus=1) for name in ("a", "b", "c")}


def test_two_models_that_need_one_card_each_stay_loaded_side_by_side(fake_llama_server):
    with gateway(fake_llama_server, ONE_CARD_EACH) as caller:
        a = ask(caller, "a").json()
        b = ask(caller, "b").json()
        a_again = ask(caller, "a").json()
        loaded = caller.get("/loadcoach/status").json()["loaded"]
        both_alive = alive(a["pid"]) and alive(b["pid"])

    assert (device(a), device(b)) == ("GPU0", "GPU1")
    assert a_again["pid"] == a["pid"] and both_alive  # asking for b did not disturb a
    assert scheduling_fields(loaded) == [
        {"model": "a", "gpus": ["GPU0"], "state": "ready", "locked": False, "answers": [], "mib": None},
        {"model": "b", "gpus": ["GPU1"], "state": "ready", "locked": False, "answers": [], "mib": None},
    ]


def test_a_third_model_replaces_the_one_unused_longest(fake_llama_server):
    with gateway(fake_llama_server, ONE_CARD_EACH) as caller:
        a = ask(caller, "a").json()
        b = ask(caller, "b").json()
        ask(caller, "a")  # a is now the more recently used of the two
        c = ask(caller, "c").json()
        kept, replaced = alive(a["pid"]), not alive(b["pid"])

    assert kept and replaced
    assert device(c) == device(b)  # c took the card b had


def test_a_busy_model_does_not_hold_up_a_model_on_the_other_card(fake_llama_server):
    with gateway(fake_llama_server, ONE_CARD_EACH) as caller, ThreadPoolExecutor() as others:
        ask(caller, "b")  # loaded and idle
        slow = others.submit(ask, caller, "a", sleep=0.8)
        wait_for_status(caller, lambda s: s["running"])
        started = time.monotonic()
        quick = ask(caller, "b")
        took = time.monotonic() - started
        slow_still_running = not slow.done()
        slow.result()

    assert quick.status_code == 200 and took < 0.5 and slow_still_running


def test_a_model_needing_both_cards_waits_for_both_and_is_not_overtaken(fake_llama_server):
    models = {"a": model("a.gguf", gpus=1, parallel=2), "big": model("big.gguf", gpus=2)}
    finished = []

    def ask_and_note(caller, label, name, **body):
        answer = ask(caller, name, **body).json()
        finished.append(label)
        return answer

    with gateway(fake_llama_server, models) as caller, ThreadPoolExecutor() as others:
        first = others.submit(ask_and_note, caller, "a-1", "a", sleep=0.5)
        wait_for_status(caller, lambda s: s["running"])
        big = others.submit(ask_and_note, caller, "big", "big")
        wait_for_status(caller, lambda s: len(s["waiting"]) == 1)
        late = others.submit(ask_and_note, caller, "a-2", "a")  # a has room for it right now
        waiting = wait_for_status(caller, lambda s: len(s["waiting"]) == 2)["waiting"]
        first, big, late = first.result(), big.result(), late.result()

    assert [ticket["model"] for ticket in waiting] == ["big", "a"]
    assert finished == ["a-1", "big", "a-2"]  # a-2 could have run at once, but big was ahead of it
    assert device(big) == "GPU0,GPU1"
    assert late["pid"] != first["pid"]  # a was replaced by big, then loaded again


def test_a_pinned_model_gets_its_card_and_a_cpu_model_needs_none(fake_llama_server):
    models = {
        "pinned": model("p.gguf", gpus=["GPU1"]),
        "rival": model("r.gguf", gpus=["GPU1"]),
        "cpu": model("c.gguf", gpus=0),
        "big": model("big.gguf"),
    }
    with gateway(fake_llama_server, models) as caller:
        pinned = ask(caller, "pinned").json()
        cpu = ask(caller, "cpu").json()
        rival = ask(caller, "rival").json()  # same card as pinned, though GPU0 is empty
        pinned_gone = not alive(pinned["pid"])
        ask(caller, "big")  # takes every card, but the CPU model needs none
        cpu_kept = alive(cpu["pid"])

    assert (device(pinned), device(rival), device(cpu)) == ("GPU1", "GPU1", "none")
    assert pinned_gone and cpu_kept


def take_lock(caller, **asked):
    return caller.post("/loadcoach/lock", json=asked).json()["token"]


def holding(token):
    return {"x-loadcoach-lock": token}


def test_a_lock_holds_others_in_line_while_the_holder_goes_ahead(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        token = take_lock(caller, seconds=30)
        other = others.submit(ask, caller, "small")
        status = wait_for_status(caller, lambda s: s["waiting"])
        holder = caller.post("/v1/chat/completions", json={"model": "big"}, headers=holding(token))
        still_waiting = not other.done()
        unlocked = caller.post("/loadcoach/unlock", headers=holding(token))
        other = other.result()

    assert status["lock"]["caller"] == "testclient" and 0 < status["lock"]["seconds_left"] <= 30
    assert holder.status_code == 200
    assert "x-loadcoach-lock" not in holder.json()["headers"]  # our headers stay with us
    assert still_waiting
    assert unlocked.json()["lock"] is None
    assert other.status_code == 200


def test_a_lock_waits_for_the_running_prompt_then_goes_ahead_of_the_line(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        started = time.monotonic()
        running = others.submit(ask, caller, "small", sleep=0.5)
        wait_for_status(caller, lambda s: s["running"])
        queued = others.submit(ask, caller, "small")
        wait_for_status(caller, lambda s: s["waiting"])
        token = take_lock(caller, seconds=30)  # returns once the lock is held
        took = time.monotonic() - started
        queued_ran, line = queued.done(), caller.get("/loadcoach/status").json()["waiting"]
        caller.post("/loadcoach/unlock", headers=holding(token))
        queued.result()

    assert took >= 0.5 and running.result().status_code == 200  # the prompt under way was not cut off
    assert not queued_ran  # but the one behind it was passed
    assert [ticket["model"] for ticket in line] == ["small"]


def test_a_lock_runs_out_on_its_own_and_its_token_stops_working(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        token = take_lock(caller, seconds=0.3)
        started = time.monotonic()
        other = others.submit(ask, caller, "small").result()
        waited = time.monotonic() - started
        late = caller.post("/v1/chat/completions", json={"model": "small"}, headers=holding(token))
        late_unlock = caller.post("/loadcoach/unlock", headers=holding(token))

    assert other.status_code == 200 and waited >= 0.25
    assert late.status_code == 409 and "run out" in late.json()["error"]["message"]
    assert late_unlock.status_code == 409


def test_extending_a_lock_keeps_it(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        token = take_lock(caller, seconds=0.3)
        extended = caller.post("/loadcoach/lock", json={"seconds": 30}, headers=holding(token))
        time.sleep(0.5)
        lock = caller.get("/loadcoach/status").json()["lock"]

    assert extended.json() == {"token": token, "seconds": 30}
    assert lock and lock["seconds_left"] > 20


def test_a_refusing_lock_turns_others_away_at_once(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        running = others.submit(ask, caller, "small", sleep=0.4)
        wait_for_status(caller, lambda s: s["running"])
        queued = others.submit(ask, caller, "small")
        wait_for_status(caller, lambda s: s["waiting"])
        token = take_lock(caller, seconds=30, refuse=True)
        queued = queued.result()
        newcomer = ask(caller, "small")
        holder = caller.post("/v1/chat/completions", json={"model": "small"}, headers=holding(token))

    assert running.result().status_code == 200
    for turned_away in (queued, newcomer):
        assert turned_away.status_code == 503
        assert "locked by anonymous" in turned_away.json()["error"]["message"]
        assert 1 <= int(turned_away.headers["retry-after"]) <= 30
    assert holder.status_code == 200


def test_a_lock_can_free_the_gpu_as_it_is_taken(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        loaded = ask(caller, "small").json()
        take_lock(caller, seconds=30, unload=True)
        status = caller.get("/loadcoach/status").json()

    assert status["loaded"] == [] and status["lock"]
    assert not alive(loaded["pid"])


@pytest.mark.parametrize("body", ['{"seconds": 0}', '{"secs": 5}', "[1]", "not json"])
def test_a_malformed_lock_request_is_a_400(fake_llama_server, body):
    with gateway(fake_llama_server) as caller:
        answer = caller.post("/loadcoach/lock", content=body)
        status = caller.get("/loadcoach/status").json()

    assert answer.status_code == 400
    assert status["lock"] is None


def test_priority_orders_the_line_and_arrival_breaks_ties(fake_llama_server):
    finished = []

    def ask_and_note(caller, label, priority):
        headers = {"x-loadcoach-priority": priority} if priority else {}
        caller.post("/v1/chat/completions", json={"model": "small"}, headers=headers)
        finished.append(label)

    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        running = others.submit(ask, caller, "small", sleep=1.5)  # long enough to line all four up behind
        wait_for_status(caller, lambda s: s["running"])
        line = []
        for label, priority in [("low", "low"), ("normal-1", None), ("high", "high"), ("normal-2", "normal")]:
            line.append(others.submit(ask_and_note, caller, label, priority))
            wait_for_status(caller, lambda s, n=len(line): len(s["waiting"]) == n)
        shown = [ticket["priority"] for ticket in caller.get("/loadcoach/status").json()["waiting"]]
        running.result(), [request.result() for request in line]
        bad = caller.post("/v1/chat/completions", json={"model": "small"}, headers={"x-loadcoach-priority": "lock"})

    assert shown == ["high", "normal", "normal", "low"]
    assert finished == ["high", "normal-1", "normal-2", "low"]
    assert bad.status_code == 400


def test_a_request_that_gives_up_leaves_the_line(fake_llama_server):
    config = Config(
        models={"small": model("s.gguf"), "big": model("b.gguf")}, gpus=CARDS, llama_server=fake_llama_server
    )

    async def scenario():
        runner = Runner(config, httpx.AsyncClient())
        dispatcher = asyncio.create_task(runner.dispatch())
        using_small = await runner.acquire(Ticket("small", "first", "here", "normal"))
        wants_big = asyncio.create_task(runner.acquire(Ticket("big", "second", "here", "normal")))
        await asyncio.sleep(0.05)
        in_line = [ticket.user for ticket in runner.waiting]

        wants_big.cancel()  # what happens when the caller hangs up
        await asyncio.wait({wants_big})
        after_leaving = (list(runner.waiting), list(runner.servers))

        runner.release(using_small)
        dispatcher.cancel()
        await runner.stop_all()
        return in_line, after_leaving

    in_line, after_leaving = asyncio.run(scenario())

    assert in_line == ["second"]
    assert after_leaving == ([], ["small"])  # gone from the line, and no swap was made for it


# ---- users, groups and abilities ---------------------------------------------------------------


def test_with_no_users_anyone_here_may_do_anything_and_the_first_user_ends_that(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        before = caller.get("/loadcoach/me").json()
        admin = add_user(caller, "jordan", "admin")
        keyless = [caller.get("/v1/models"), ask(caller, "small"), caller.get("/loadcoach/status")]
        wrong = caller.get("/v1/models", headers={"authorization": "Bearer lc_wrong"})
        me = caller.get("/loadcoach/me", headers=admin).json()
        answer = caller.post("/v1/chat/completions", json={"model": "small"}, headers=admin)

    assert before["name"] == "anonymous" and "manage-users" in before["can"]
    assert [response.status_code for response in keyless] == [401, 401, 401]
    assert wrong.status_code == 401
    assert (me["name"], me["group"]) == ("jordan", "admin") and "lock" in me["can"]
    assert answer.status_code == 200
    assert "authorization" not in answer.json()["headers"]  # the key never reaches the model server


def test_a_group_limits_which_models_its_users_see_and_use(fake_llama_server):
    groups = {**GROUPS, "small-only": Group(can=frozenset({"load"}), models=("small",))}
    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        guest = add_user(caller, "guest", "small-only", by=admin)
        listed = caller.get("/v1/models", headers=guest).json()["data"]
        allowed = caller.post("/v1/chat/completions", json={"model": "small"}, headers=guest)
        refused = caller.post("/v1/chat/completions", json={"model": "big"}, headers=guest)

    assert [entry["id"] for entry in listed] == ["small"]
    assert allowed.status_code == 200
    assert refused.status_code == 404 and "Available: small" in refused.json()["error"]["message"]


def test_priority_comes_from_the_group_and_high_needs_the_ability(fake_llama_server):
    groups = {**GROUPS, "batch": Group(can=frozenset({"load"}), priority="low")}
    with gateway(fake_llama_server, groups=groups) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        batch = add_user(caller, "nightly", "batch", by=admin)
        running = others.submit(caller.post, "/v1/chat/completions", json={"model": "small", "sleep": 0.4}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        queued = others.submit(caller.post, "/v1/chat/completions", json={"model": "small"}, headers=batch)
        shown = wait_for_status(_signed(caller, admin), lambda s: s["waiting"])["waiting"][0]
        pushy = caller.post(
            "/v1/chat/completions", json={"model": "small"}, headers={**batch, "x-loadcoach-priority": "high"}
        )
        running.result(), queued.result()

    assert (shown["user"], shown["priority"]) == ("nightly", "low")
    assert pushy.status_code == 403 and "high priority" in pushy.json()["error"]["message"]


def test_abilities_gate_lock_unload_and_breaking_a_lock(fake_llama_server):
    groups = {**GROUPS, "locker": Group(can=frozenset({"load", "lock"}), max_lock=60)}
    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        locker = add_user(caller, "lee", "locker", by=admin)

        no_lock = caller.post("/loadcoach/lock", json={"seconds": 5}, headers=plain)
        no_unload = caller.post("/loadcoach/unload", headers=plain)
        too_long = caller.post("/loadcoach/lock", json={"seconds": 61}, headers=locker)
        held = caller.post("/loadcoach/lock", json={"seconds": 60}, headers=locker)
        lock_shown = caller.get("/loadcoach/status", headers=admin).json()["lock"]
        cannot_break = caller.post("/loadcoach/unlock", headers=plain)
        broken = caller.post("/loadcoach/unlock", headers=admin)  # no token: someone else's lock
        token_after = caller.post("/loadcoach/unlock", headers={**locker, "x-loadcoach-lock": held.json()["token"]})

    assert (no_lock.status_code, no_unload.status_code) == (403, 403)
    assert too_long.status_code == 403 and "at most 60 seconds" in too_long.json()["error"]["message"]
    assert held.status_code == 200 and lock_shown["user"] == "lee"
    assert cannot_break.status_code == 403
    assert broken.status_code == 200 and broken.json()["lock"] is None
    assert token_after.status_code == 409


def test_a_user_without_load_may_use_a_loaded_model_but_not_bring_one_in(fake_llama_server):
    groups = {**GROUPS, "rider": Group()}
    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        rider = add_user(caller, "sam", "rider", by=admin)
        caller.post("/v1/chat/completions", json={"model": "small"}, headers=admin)  # small is now loaded
        rides = caller.post("/v1/chat/completions", json={"model": "small"}, headers=rider)
        refused = caller.post("/v1/chat/completions", json={"model": "big"}, headers=rider)
        still = caller.get("/loadcoach/status", headers=admin).json()["loaded"]

    assert rides.status_code == 200
    assert refused.status_code == 403 and "may not load" in refused.json()["error"]["message"]
    assert [entry["model"] for entry in still] == ["small"]  # nothing was replaced on their account


def test_a_group_caps_how_many_requests_one_user_has_at_once(fake_llama_server):
    groups = {**GROUPS, "capped": Group(can=frozenset({"load"}), max_requests=2)}
    with gateway(fake_llama_server, groups=groups) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        capped = add_user(caller, "cam", "capped", by=admin)
        send = lambda **body: caller.post("/v1/chat/completions", json={"model": "small", **body}, headers=capped)  # noqa: E731
        two = [others.submit(send, sleep=0.4) for _ in range(2)]
        wait_for_status(_signed(caller, admin), lambda s: len(s["running"]) + len(s["waiting"]) == 2)
        third = send()
        [request.result() for request in two]
        afterwards = send()

    assert third.status_code == 429 and third.headers["retry-after"] == "5"
    assert afterwards.status_code == 200


def test_without_see_all_a_user_sees_only_their_own_requests_and_a_count(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        mine = others.submit(caller.post, "/v1/chat/completions", json={"model": "small", "sleep": 0.5}, headers=plain)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        theirs = others.submit(caller.post, "/v1/chat/completions", json={"model": "small"}, headers=admin)
        everything = wait_for_status(_signed(caller, admin), lambda s: s["waiting"])
        limited = caller.get("/loadcoach/status", headers=plain).json()
        mine.result(), theirs.result()

    assert [entry["user"] for entry in everything["running"] + everything["waiting"]] == ["pat", "jordan"]
    assert "others_waiting" not in everything
    assert [entry["user"] for entry in limited["running"]] == ["pat"] and limited["waiting"] == []
    assert (limited["others_running"], limited["others_waiting"]) == (0, 1)




def test_rename_api_permissions_validation_and_busy_account(fake_llama_server, tmp_path):
    database = str(tmp_path / "users.db")
    with gateway(fake_llama_server, database=database) as caller:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        rename = lambda name, new, by=admin: caller.post(f"/loadcoach/users/{name}/rename", headers=by, json={"name": new})
        assert rename("pat", "alex", pat).status_code == 403
        assert rename("nobody", "alex").status_code == 404
        for new in (None, 42, "", "jordan", "PAT"):
            assert rename("pat", new).status_code == 400
        # A durable task is active work even between its model requests.
        from loadcoach.batches import Batches
        batches = Batches(database)
        file = batches.put_file("pat", "input.jsonl", "batch", b"")
        batch = batches.start("pat", "/loadcoach/task", file["id"], [])
        assert rename("pat", "alex").status_code == 409
        with batches.db:
            batches.db.execute("UPDATE batches SET status = 'completed' WHERE id = ?", (batch["id"],))
        assert rename("pat", "alex").status_code == 200
        assert caller.get("/loadcoach/me", headers=pat).json()["name"] == "alex"
        assert batches.file(file["id"])["user"] == "alex"
        assert batches.get(batch["id"])["user"] == "alex"
        event = caller.log.read(0, 100, None, "user")  # what was done to an account is on record at the front machine
        assert "renamed" in str(event) and "new_name" in str(event)
        assert Log(database).verify()[1] is None and caller.log.verify()[1] is None


def test_managing_users_over_http(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        users = lambda path="", **kw: caller.request(kw.pop("method", "POST"), "/loadcoach/users" + path, **kw)  # noqa: E731

        not_allowed = [
            users(method="GET", headers=pat),
            users(json={"name": "x", "group": "plain"}, headers=pat),
            users("/pat", json={"group": "admin"}, headers=pat),  # may not promote oneself
            users("/jordan", json={"password": "longenough"}, headers=pat),  # nor touch someone else
            users("/jordan", method="DELETE", headers=pat),
        ]
        bad = [
            users(json={"name": "Has Space", "group": "plain"}, headers=admin),
            users(json={"name": "pat", "group": "plain"}, headers=admin),  # already there
            users(json={"name": "new", "group": "nope"}, headers=admin),
            users("/pat", json={"password": "short"}, headers=admin),
            users("/ghost", json={"enabled": False}, headers=admin),
        ]

        own_password = users("/pat", json={"password": "correct horse"}, headers=pat)  # one's own is allowed
        second_key = users("/pat/keys", json={"label": "laptop"}, headers=pat).json()["key"]
        listing = users(method="GET", headers=admin).json()
        pat_listed = next(user for user in listing["users"] if user["name"] == "pat")

        first_id = next(key["id"] for key in pat_listed["keys"] if key["label"] == "first")
        users(f"/pat/keys/{first_id}", method="DELETE", headers=admin)
        old_key = caller.get("/loadcoach/me", headers=pat)
        new_key = caller.get("/loadcoach/me", headers={"authorization": "Bearer " + second_key})

        users("/pat", json={"group": "admin"}, headers=admin)
        promoted = caller.get("/loadcoach/me", headers={"authorization": "Bearer " + second_key}).json()
        users("/pat", json={"enabled": False}, headers=admin)
        disabled = caller.get("/loadcoach/me", headers={"authorization": "Bearer " + second_key})
        removed = users("/pat", method="DELETE", headers=admin)
        remaining = [user["name"] for user in users(method="GET", headers=admin).json()["users"]]

    assert [response.status_code for response in not_allowed] == [403] * 5
    assert [response.status_code for response in bad] == [400, 400, 400, 400, 404]
    assert own_password.status_code == 200
    assert listing["groups"] == ["admin", "front", "plain"]
    assert pat_listed["password"] is True and [key["label"] for key in pat_listed["keys"]] == ["first", "laptop"]
    assert "hash" not in str(listing) and second_key not in str(listing)
    assert (old_key.status_code, new_key.status_code) == (401, 200)
    assert promoted["group"] == "admin"
    assert disabled.status_code == 401
    assert removed.status_code == 200 and remaining == ["jordan"]


def test_users_are_kept_on_front_and_the_first_is_made_from_the_command_line(tmp_path, monkeypatch, capsys):
    gguf = tmp_path / "m.gguf"
    gguf.touch()
    config = tmp_path / "loadcoach.toml"
    config.write_text(f'host = "0.0.0.0"\n[models.m]\ngpus = 0\n[models.m.flags]\nmodel = "{gguf}"\nctx-size = 1\n')
    monkeypatch.setattr(sys, "argv", ["loadcoach", "--config", str(config)])
    with pytest.raises(SystemExit, match="set machine_key_file"):  # open to the network, and nothing to say who is asking
        main()

    # The accounts are the front machine's, and so are the commands that look after them.
    front = tmp_path / "weightroom.toml"
    front.write_text('compute = "http://c:1"\ndata = "front"\n[groups.admin]\ncan = ["manage-users"]\n')
    (tmp_path / "front").mkdir()
    run = lambda *command: CliRunner().invoke(weightroom_cli, ["--config", str(front), *command])  # noqa: E731
    assert "No group named" in str(run("user", "add", "jordan", "wheel").exception)
    key = run("user", "add", "jordan", "admin").output.strip().splitlines()[-1]
    kept = Users(str(tmp_path / "front" / "accounts.db"), {})  # a fresh look at the file
    assert key.startswith("lc_") and kept.by_key(key).name == "jordan"
    assert key not in (tmp_path / "front" / "accounts.db").read_bytes().decode("latin-1")  # only its hash is stored
    assert "added" in run("log").output and "intact" in run("log", "verify").output

    # The move: compute's accounts, taken once into an empty front, every hash as it was.
    old = Users(str(tmp_path / "compute.db"), {"admin": Group(can=frozenset({"manage-users"}))})
    old.add("pat", "admin")
    pats = old.new_key("pat")
    (tmp_path / "fresh").mkdir()
    fresh = tmp_path / "fresh.toml"
    fresh.write_text('compute = "http://c:1"\ndata = "fresh"\n')
    moved = CliRunner().invoke(weightroom_cli, ["--config", str(fresh), "accounts-from", str(tmp_path / "compute.db")])
    assert moved.exit_code == 0 and "Took 1 users and 1 groups" in moved.output
    assert Users(str(tmp_path / "fresh" / "accounts.db"), {}).by_key(pats).name == "pat"
    again = CliRunner().invoke(weightroom_cli, ["--config", str(fresh), "accounts-from", str(tmp_path / "compute.db")])
    assert "first move only" in str(again.exception)  # never twice


# ---- the loadcoach command -------------------------------------------------------------------


def test_clearing_the_line_turns_waiting_requests_away_and_leaves_the_running_one(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        running = others.submit(ask, caller, "small", sleep=0.5)
        wait_for_status(caller, lambda s: s["running"])
        waiting = [others.submit(ask, caller, "small") for _ in range(2)]
        wait_for_status(caller, lambda s: len(s["waiting"]) == 2)
        cleared = caller.post("/loadcoach/clear").json()
        waiting = [request.result() for request in waiting]
        admin = add_user(caller, "jordan", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        not_allowed = caller.post("/loadcoach/clear", headers=plain)

    assert cleared == {"removed": 2}
    assert running.result().status_code == 200
    for turned_away in waiting:
        assert turned_away.status_code == 503 and turned_away.headers["retry-after"] == "10"
        assert "Removed from the line by anonymous" in turned_away.json()["error"]["message"]
    assert not_allowed.status_code == 403


def test_a_model_is_given_another_name_and_what_lists_it_follows(fake_llama_server, capsys):
    groups = {**GROUPS, "few": Group(can=frozenset({"load"}), models=("small", "big"))}
    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        caller.post("/loadcoach/users", json={"name": "chat", "group": "few", "own": {"models": ["small"]}}, headers=admin)
        plain = add_user(caller, "pat", "plain", by=admin)
        before = caller.post("/v1/chat/completions", json={"model": "small"}, headers=admin).json()  # so that it is loaded
        rename = lambda who, **asked: caller.post("/loadcoach/model-rename", json=asked, headers=who)  # noqa: E731
        refused = [rename(plain, name="small", to="tiny").status_code, rename(admin, name="ghost", to="tiny").status_code,
                   rename(admin, name="small", to="big").status_code, rename(admin, name="small", to="not a name").status_code]  # fmt: skip
        done = rename(admin, name="small", to="tiny")
        listed = [model["name"] for model in caller.get("/loadcoach/models", headers=admin).json()["models"]]
        old, new = caller.post("/v1/chat/completions", json={"model": "small"}, headers=admin), caller.post("/v1/chat/completions", json={"model": "tiny"}, headers=admin)
        loaded = wait_for_status(_signed(caller, admin), lambda s: [model["model"] for model in s["loaded"]] == ["tiny"])  # the copy under the old name is gone
        chat = caller.get("/loadcoach/users/chat", headers=admin).json()
        few = next(group for group in caller.get("/loadcoach/groups", headers=admin).json()["groups"] if group["name"] == "few")
        kept = next(entry for entry in caller.get("/loadcoach/events?kind=model", headers=admin).json()["events"] if entry["what"] == "renamed")

    assert refused == [403, 404, 409, 400]
    assert done.json()['model_id'].startswith('model_')
    assert {key:value for key,value in done.json().items() if key!='model_id'} == {"name": "tiny", "was": "small", "followed": ["the group few", "chat"], "still_finishing": 0}
    assert listed == ["big", "tiny"]  # listed by its new name, with the same settings
    assert (old.status_code, new.status_code) == (404, 200) and new.json()["pid"] != before["pid"] and loaded
    assert chat["own"] == {"models": ["tiny"]} and few["models"] == ["tiny", "big"]  # who may use it still may
    assert (kept["what"], kept["model"], kept["was"], kept["user"]) == ("renamed", "tiny", "small", "jordan")

    # From the command line on compute. The groups are on the front machine, and say so.
    config = Config(gpus=CARDS)
    models = Models(":memory:", first={"small": model("small.gguf")})
    model_command(models, config, "rename", ["small", "tiny"])
    assert "Renamed small to tiny." in (said := capsys.readouterr().out) and "do not follow" in said
    assert models.names() == ["tiny"]
    for wrong in (["tiny", "tiny"], ["ghost", "x"], ["tiny"], ["tiny", "bad name"]):
        with pytest.raises(ValueError):
            model_command(models, config, "rename", wrong)


def test_the_flags_llama_server_knows_are_read_from_its_own_help(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, "jordan", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        told = caller.get("/loadcoach/flags", headers=admin).json()
        again = caller.get("/loadcoach/flags", headers=admin).json()  # kept, not asked for a second time
        not_allowed = caller.get("/loadcoach/flags", headers=plain)
    with gateway("/no/such/llama-server") as caller:
        missing = caller.get("/loadcoach/flags")

    by_name = {flag["name"]: flag for flag in told["flags"]}
    assert list(by_name) == ["help", "threads", "flash-attn", "kv-offload", "split-mode", "host", "temp"] and told == again
    assert by_name["threads"] == {"name": "threads", "also": ["-t"], "takes": "N", "says": "number of CPU threads to use during generation (default: -1)", "group": "common params"}
    assert by_name["flash-attn"]["takes"] == "[on|off|auto]" and "LLAMA_ARG" not in json.dumps(told)  # the environment variable's name is left out
    # A switch takes nothing, is written by the spelling that switches it on, and shows the one that switches it off.
    assert (by_name["kv-offload"]["takes"], by_name["kv-offload"]["also"]) == ("", ["-kvo", "-nkvo", "--no-kv-offload"])
    # Long spellings push what the flag does onto the next lines, and it may run on over several.
    assert by_name["split-mode"]["takes"] == "{none,layer,row,tensor}" and by_name["split-mode"]["says"].endswith("split layers and KV across GPUs")
    assert (by_name["temp"]["also"], by_name["temp"]["group"]) == (["--temperature"], "sampling params")
    assert told["ours"] == ["device", "host", "port"] and not_allowed.status_code == 403
    assert missing.status_code == 503 and "--help could not be run" in missing.json()["error"]["message"]


def test_draining_turns_new_requests_away_and_finishes_those_already_here(fake_llama_server, tmp_path, capsys):
    token_file = tmp_path / "loadcoach.cli-token"
    with gateway(fake_llama_server, token_file=str(token_file)) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        running = others.submit(caller.post, "/v1/chat/completions", json={"model": "small", "sleep": 0.5}, headers=plain)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        waiting = others.submit(caller.post, "/v1/chat/completions", json={"model": "small"}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda s: s["waiting"])

        not_allowed = caller.post("/loadcoach/drain", json={"why": "I say so."}, headers=plain)
        begun = caller.post("/loadcoach/drain", json={"why": "The machine is going to sleep."}, headers=admin).json()
        seen_by_pat = caller.get("/loadcoach/status", headers=plain).json()
        turned_away = caller.post("/v1/chat/completions", json={"model": "small"}, headers=admin)
        no_lock = caller.post("/loadcoach/lock", json={"seconds": 30}, headers=admin)
        finished = (running.result().status_code, waiting.result().status_code)  # both were here already
        emptied = wait_for_status(_signed(caller, admin), lambda s: s["busy"] == 0)
        unloaded = caller.post("/loadcoach/unload", headers=admin)  # looking after the machine still works

        command = OnCompute(caller)  # the loadcoach command asks LoadCoach on its own machine, with its token
        command.headers["authorization"] = "Bearer " + token_file.read_text()
        server_command(command, "status")
        shown = capsys.readouterr().out
        server_command(command, "drain", "off")
        server_command(command, "drain", "new", "drives")
        said = capsys.readouterr().out
        by_command = command.get("/loadcoach/status").json()["draining"]
        ended = caller.post("/loadcoach/drain", json={"on": False}, headers=admin).json()
        taken_again = caller.post("/v1/chat/completions", json={"model": "small"}, headers=admin)
        kept = [(entry["what"], entry["user"]) for entry in caller.get("/loadcoach/events?kind=line", headers=admin).json()["events"]]

    assert not_allowed.status_code == 403
    assert begun["busy"] == 2 and (begun["draining"]["by"], begun["draining"]["why"]) == ("jordan", "The machine is going to sleep.")
    # Anyone may see that it is draining and how much is left, though not whose requests they are.
    assert seen_by_pat["draining"]["by"] == "jordan" and seen_by_pat["busy"] == 2 and seen_by_pat["others_waiting"] == 1
    assert turned_away.status_code == 503 and turned_away.headers["retry-after"] == "30"
    assert turned_away.json()["error"]["code"] == "draining" and "going to sleep" in turned_away.json()["error"]["message"]
    assert no_lock.status_code == 503 and finished == (200, 200)
    assert emptied["draining"] and unloaded.status_code == 200
    assert "DRAINING since" in shown and "asked by jordan" in shown
    assert "Requests are taken again." in said and "No new requests are taken." in said and by_command["why"] == "new drives"
    assert ended["draining"] is None and taken_again.status_code == 200
    assert kept[-4:] == [("draining", "jordan"), ("open", "command-line"), ("draining", "command-line"), ("open", "jordan")]


def test_a_running_loadcoach_leaves_a_token_that_signs_in_the_command_line(fake_llama_server, tmp_path):
    token_file = tmp_path / "loadcoach.cli-token"
    with gateway(fake_llama_server, token_file=str(token_file)) as caller:
        add_user(caller, "jordan", "admin")
        token = token_file.read_text()
        private = token_file.stat().st_mode & 0o777
        command = OnCompute(caller)  # straight to LoadCoach, on its own machine, as the command asks
        with_token = command.get("/loadcoach/me", headers={"authorization": f"Bearer {token}"}).json()
        without = command.get("/loadcoach/me")
        nearly = command.get("/loadcoach/me", headers={"authorization": "Bearer " + token[:-1]})

    assert private == 0o600 and len(token) > 30
    assert with_token["name"] == "command-line" and "break-lock" in with_token["can"]
    assert (without.status_code, nearly.status_code) == (401, 401)
    assert not token_file.exists()  # gone again once LoadCoach stops


def test_status_unlock_and_clear_from_the_command_line(fake_llama_server, tmp_path, capsys):
    token_file = tmp_path / "loadcoach.cli-token"
    with gateway(fake_llama_server, token_file=str(token_file)) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        running = others.submit(caller.post, "/v1/chat/completions", json={"model": "small", "sleep": 0.6}, headers=pat)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        waiting = others.submit(caller.post, "/v1/chat/completions", json={"model": "big"}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda s: s["waiting"])

        # This stands in for the connection the command opens to the LoadCoach running on its own machine.
        command = OnCompute(caller)
        command.headers["authorization"] = "Bearer " + token_file.read_text()
        server_command(command, "status")
        shown = capsys.readouterr().out
        server_command(command, "clear")
        cleared = capsys.readouterr().out
        running.result()
        caller.post("/loadcoach/lock", json={"seconds": 30}, headers=admin)
        server_command(command, "unlock")
        server_command(command, "unlock")
        unlocked = capsys.readouterr().out
        lock_after = command.get("/loadcoach/status").json()["lock"]

    assert "small" in shown and "GPU0+GPU1" in shown and "ready" in shown
    assert "pat" in shown and "jordan" in shown and "big" in shown and "LOCK\n  none" in shown
    assert "Turned away 1 waiting request." in cleared and waiting.result().status_code == 503
    assert "Broke the lock held by jordan." in unlocked and "There was no lock." in unlocked
    assert lock_after is None


def test_every_command_has_a_page_of_help(tmp_path):
    from typer.testing import CliRunner

    from loadcoach.cli import app

    ask = lambda *words: CliRunner().invoke(app, ["--config", str(tmp_path / "none.toml"), *words], prog_name="loadcoach")  # noqa: E731
    front = ask("--help").output  # help needs no settings file
    for command in ("models", "log", "status", "drain", "unlock", "clear", "stop", "peek", "downloads", "fingerprints"):
        assert f" {command} " in front
    assert not {"user", "group"} & {typer_group.name for typer_group in app.registered_groups}  # the accounts are the front machine's
    for group, commands in (("models", ("list", "show", "export", "add", "set", "rename", "remove")), ("log", ("verify",))):  # fmt: skip
        listed = ask(group, "--help").output
        for command in commands:
            page = ask(group, command, "-h")
            assert f" {command} " in listed and page.exit_code == 0 and f"loadcoach {group} {command}" in page.output

    # On the front machine, the weightroom command looks after the accounts.
    import weightroom.app
    from weightroom.app import cli as weightroom_cli
    ask = lambda *words: CliRunner().invoke(weightroom_cli, ["--config", str(tmp_path / "none.toml"), *words], prog_name="weightroom")  # noqa: E731
    front = ask("--help").output
    for command in ("user", "group", "log", "accounts-from", "roles-from"):
        assert f" {command} " in front
    for group, commands in (("user", ("list", "add", "group", "enable", "disable", "remove", "key", "unkey", "password", "temporary", "forget-code", "import", "show", "adjust")),
                            ("group", ("list", "show", "export", "add", "set", "remove")), ("log", ("verify",))):  # fmt: skip
        listed = ask(group, "--help").output
        for command in commands:
            page = ask(group, command, "-h")
            assert f" {command} " in listed and page.exit_code == 0 and f"weightroom {group} {command}" in page.output
    assert "NAME" in ask("user", "add", "-h").output and "GROUP" in ask("user", "add", "-h").output
    mistyped = ask("user", "ad", "pat", "plain")
    assert mistyped.exit_code == 2 and "Did you mean 'add'?" in mistyped.output
    assert weightroom.app.cli.registered_callback is not None and "settings/examples/weightroom.toml" in front.replace("\n", " ")

    # The programs with no commands each have one page, which says where their settings are explained.
    import turnstile.app, spotter.app, toollocker.door  # noqa: E401

    for program, example in ((spotter.app, "settings/examples/spotter.toml"), (turnstile.app, "settings/examples/weightroom.toml"), (toollocker.door, "settings/examples/toollocker.toml")):
        page = CliRunner().invoke(program.cli, ["--help"])
        assert page.exit_code == 0 and example in page.output.replace("\n", " ") and "--config" in page.output


def test_looking_after_users_from_the_command_line(tmp_path, monkeypatch, capsys):
    users = Users(str(tmp_path / "users.db"), GROUPS)

    def run(*command):
        user_command(users, command[0], list(command[1:]))
        return capsys.readouterr().out

    first_key = run("add", "pat", "plain").strip().splitlines()[-1]
    second_key = run("key", "pat", "laptop").strip().splitlines()[-1]
    listed = run("list")
    first_id = next(key["id"] for key in users.listing()[0]["keys"] if key["label"] == "first")
    run("unkey", "pat", first_id)
    after_unkey = (users.by_key(first_key), users.by_key(second_key).name)

    run("group", "pat", "admin")
    promoted = users.by_key(second_key).group_name
    run("disable", "pat")
    disabled = users.by_key(second_key)
    run("enable", "pat")
    enabled = users.by_key(second_key).name

    typed = iter(["correct horse", "correct horse", "short", "short", "one thing 1", "another 22"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(typed))
    run("password", "pat")
    has_password = users.listing()[0]["password"]
    mistakes = []
    for command in (["password", "pat"], ["password", "pat"], ["group", "ghost", "admin"], ["group", "pat", "nope"],
                    ["unkey", "pat", "zzzzzzzz"], ["fly", "pat"], ["add", "pat"]):  # fmt: skip
        with pytest.raises(ValueError) as mistake:
            run(*command)
        mistakes.append(str(mistake.value))
    # An installer hands the password over once, having had it typed twice itself.
    user_command(users, "password", ["pat"], password="from the installer")
    assert users.listing()[0]["password"]
    with pytest.raises(ValueError, match="at least 8"):
        user_command(users, "password", ["pat"], password="short")
    run("remove", "pat")

    assert first_key.startswith("lc_") and second_key.startswith("lc_")
    assert "NAME" in listed and "pat" in listed and "plain" in listed and f"laptop ({users_id(second_key)})" in listed
    assert after_unkey == (None, "pat")
    assert (promoted, disabled, enabled, has_password) == ("admin", None, "pat", True)
    assert ["at least 8" in mistakes[0], "at least 8" in mistakes[1], "No user named" in mistakes[2]] == [True] * 3
    assert "No group named" in mistakes[3] and "no key with id" in mistakes[4]
    assert "weightroom user list" in mistakes[5] and "weightroom user list" in mistakes[6]  # the usage
    assert users.listing() == []


def users_id(key):
    """The id a key is listed under: the start of its hash."""
    from loadcoach.users import hash_key

    return hash_key(key)[:8]


def test_importing_many_users_is_all_or_nothing(tmp_path, capsys):
    users = Users(str(tmp_path / "users.db"), GROUPS)
    users.add("already", "plain")
    file = tmp_path / "people.csv"

    file.write_text("name,group\nann,plain\nbob,nope\nBad Name,plain\nalready,plain\nann,admin\njust-a-name\n")
    with pytest.raises(ValueError) as refused:
        user_command(users, "import", [str(file)])
    after_bad = [user["name"] for user in users.listing()]

    file.write_text("# the new people\nname,group\nann, plain\n\nbob,admin\n")
    user_command(users, "import", [str(file)])
    printed = [line.split(",") for line in capsys.readouterr().out.strip().splitlines()]

    message = str(refused.value)
    assert "Nobody was added" in message
    assert all(part in message for part in ("No group named 'nope'", "'Bad Name'", "already a user", "more than once", "expected name,group"))
    assert after_bad == ["already"]
    assert printed[0] == ["name", "group", "key"]
    assert [(name, group) for name, group, _ in printed[1:]] == [("ann", "plain"), ("bob", "admin")]
    assert [users.by_key(key).name for _, _, key in printed[1:]] == ["ann", "bob"]


# ---- the event log ---------------------------------------------------------------------------


def events(caller, headers=None, **only):
    """The log as one caller is shown it, oldest first."""
    return caller.get("/loadcoach/events", params=only, headers=headers or {}).json()["events"]


def test_a_request_leaves_two_entries_asked_and_how_it_ended(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        answer = ask(caller, "small", sleep=0.2)
        asked, ended = events(caller, kind="request")
        loaded = events(caller, kind="model")[-1]

    assert answer.headers["x-loadcoach-request-id"] == str(asked["source"])  # compute's number, which front keeps beside its own
    assert (asked["what"], asked["path"], asked["user"], asked["caller"], asked["model"]) == (
        "asked", "chat/completions", "anonymous", "testclient", "small",
    )  # fmt: skip
    assert (ended["what"], ended["of"], ended["status"], ended["level"], ended["priority"], ended["trace"]) == (
        "done", asked["source"], 200, "normal", "normal", None,
    )  # fmt: skip
    assert ended["ran"] >= 0.2 and ended["waited"] >= 0
    assert "T" in asked["at"]
    assert "sleep" not in str(asked) + str(ended) and "text" not in asked  # nothing from inside is kept
    assert (loaded["what"], loaded["model"], loaded["cards"], loaded["replaced"]) == ("loaded", "small", ["GPU0", "GPU1"], [])


def test_token_counts_are_read_from_the_end_of_an_answer_and_nothing_else_is():
    stream_end = b'data: {"choices":[{"delta":{"content":"prompt_n is not a number here"}}]}\n\ndata: {"choices":[],"timings":{"prompt_n":35,"predicted_n":11,"predicted_per_second":29.708}}\n\ndata: [DONE]\n\n'
    whole = b'{"choices":[{"message":{"content":"hi"}}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}'

    assert token_counts(stream_end) == {"prompt_tokens": 35, "answer_tokens": 11, "tokens_per_second": 29.7}
    assert token_counts(whole) == {"prompt_tokens": 7, "answer_tokens": 3}
    assert token_counts(b"data: one\n\ndata: two\n\n") == {}


def test_how_each_request_ended_is_on_record(fake_llama_server):
    with gateway(fake_llama_server, max_wait=0.3) as caller, ThreadPoolExecutor() as others:
        slow = others.submit(ask, caller, "small", sleep=0.8)
        wait_for_status(caller, lambda s: s["running"])
        while_running = [entry["what"] for entry in events(caller, kind="request")]
        ask(caller, "small")  # waits too long
        slow.result()
        ask(caller, "huge")  # no such model: turned away before it reaches the line
        told = [(entry["what"], entry.get("status")) for entry in events(caller, kind="request")]

    assert while_running == ["asked"]  # asked, and no ending yet
    assert told == [("asked", None), ("asked", None), ("refused", 503), ("done", 200)]  # nothing for the unknown model


def test_being_turned_away_is_on_record_with_the_reason(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, "jordan", "admin")
        plain = add_user(caller, "pat", "plain", by=admin)
        caller.get("/v1/models")  # no key
        caller.post("/loadcoach/unload", headers=plain)  # no ability
        refused = events(caller, admin, kind="refused")

    assert [(entry["what"], entry["user"], entry["path"]) for entry in refused] == [
        ("401", "", "/v1/models"),
        ("403", "pat", "/loadcoach/unload"),
    ]
    assert "may not unload" in refused[1]["why"]


def test_locks_the_line_and_users_are_on_record(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, "jordan", "admin")
        token = caller.post("/loadcoach/lock", json={"seconds": 30}, headers=admin).json()["token"]
        caller.post("/loadcoach/lock", json={"seconds": 40}, headers={**admin, "x-loadcoach-lock": token})
        caller.post("/loadcoach/unlock", headers={**admin, "x-loadcoach-lock": token})
        caller.post("/loadcoach/lock", json={"seconds": 0.2}, headers=admin)
        time.sleep(0.3)
        caller.get("/loadcoach/status", headers=admin)  # looking is what notices it ran out
        pat = add_user(caller, "pat", "plain", by=admin)
        caller.post("/loadcoach/lock", json={"seconds": 30}, headers=admin)
        caller.post("/loadcoach/unlock", headers=admin)  # no token: breaks it
        caller.post("/loadcoach/clear", headers=admin)
        caller.post("/loadcoach/users/pat", json={"group": "admin", "password": "correct horse"}, headers=admin)
        caller.post("/loadcoach/users/pat/keys", json={"label": "laptop"}, headers=pat)
        caller.delete("/loadcoach/users/pat", headers=admin)
        log = events(caller, admin)
        users = caller.log.read(0, 100, None, "user")  # what was done to the accounts is on record at the front machine

    assert [entry["what"] for entry in log if entry["kind"] == "lock"] == [
        "taken", "extended", "released", "taken", "ran-out", "taken", "broken",
    ]  # fmt: skip
    assert next(entry for entry in log if entry["what"] == "broken")["held_by"] == "jordan"
    assert next(entry for entry in log if entry["what"] == "cleared")["removed"] == 0
    assert [(entry["what"], entry["user"], entry["name"]) for entry in users] == [
        ("added", "anonymous", "jordan"), ("added", "jordan", "pat"), ("changed", "jordan", "pat"),
        ("key-added", "pat", "pat"), ("removed", "jordan", "pat"),
    ]  # fmt: skip
    changed = users[2]
    assert changed["group"] == "admin" and changed["password"] is True
    assert "correct horse" not in str(log + users) and "lc_" not in str(log + users) and token not in str(log)


def test_a_user_reads_their_own_entries_and_see_log_reads_everyones(fake_llama_server):
    groups = {**GROUPS, "auditor": Group(can=frozenset({"see-log"}))}
    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        auditor = add_user(caller, "ada", "auditor", by=admin)
        caller.post("/v1/chat/completions", json={"model": "small"}, headers=pat)
        caller.post("/v1/chat/completions", json={"model": "small"}, headers=admin)

        pats = events(caller, pat)
        pat_asking_for_jordan = events(caller, pat, user="jordan")
        everyone = [entry for entry in events(caller, auditor, kind="request") if entry["what"] == "done"]
        only_jordan = [entry for entry in events(caller, auditor, kind="request", user="jordan") if entry["what"] == "done"]
        newer = [entry for entry in events(caller, auditor, after=everyone[0]["id"], kind="request") if entry["what"] == "done"]
        one = events(caller, auditor, limit=1)
        newest = max(entry["id"] for entry in events(caller, auditor))
        bad = caller.get("/loadcoach/events", params={"after": "soon"}, headers=auditor)

    assert {entry["user"] for entry in pats} == {"pat"} and {entry["user"] for entry in pat_asking_for_jordan} == {"pat"}
    assert [entry["user"] for entry in everyone] == ["pat", "jordan"]
    assert [entry["user"] for entry in only_jordan] == ["jordan"]
    assert [entry["user"] for entry in newer] == ["jordan"]  # only what came after
    assert len(one) == 1 and one[0]["id"] == newest  # with no `after`, the latest
    assert bad.status_code == 400


def test_sensitivity_is_capped_by_the_group_recorded_and_sticks_to_its_trace(fake_llama_server):
    groups = {**GROUPS, "cleared": Group(can=frozenset({"load"}), max_sensitivity="sensitive")}
    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        cleared = add_user(caller, "cleo", "cleared", by=admin)
        plain = add_user(caller, "pat", "plain", by=admin)

        def send(who, **headers):
            return caller.post("/v1/chat/completions", json={"model": "small"}, headers={**who, **headers})

        too_high_for_group = send(plain, **{"x-loadcoach-sensitivity": "sensitive"})
        above_the_cap = send(cleared, **{"x-loadcoach-sensitivity": "restricted"})
        made_up = send(cleared, **{"x-loadcoach-sensitivity": "secret"})
        bad_trace = send(cleared, **{"x-loadcoach-trace": "has spaces"})

        send(cleared, **{"x-loadcoach-trace": "task-1"})  # an ordinary call in a new trace
        send(cleared, **{"x-loadcoach-trace": "task-1", "x-loadcoach-sensitivity": "sensitive"})
        send(cleared, **{"x-loadcoach-trace": "task-1"})  # says nothing, but the trace is sensitive now
        send(cleared, **{"x-loadcoach-trace": "task-2"})  # another piece of work is unaffected
        send(cleared)  # and so is a call in no trace at all
        forwarded = send(cleared, **{"x-loadcoach-trace": "task-1"}).json()["headers"]
        me = caller.get("/loadcoach/me", headers=cleared).json()
        by_trace = [(e["trace"], e["level"]) for e in events(caller, cleared, kind="request") if e["what"] == "asked"]
        one_trace = [entry for entry in events(caller, cleared, trace="task-1") if entry["what"] == "asked"]

    assert (too_high_for_group.status_code, above_the_cap.status_code) == (403, 403)
    assert "sensitive level" in too_high_for_group.json()["error"]["message"]
    assert (made_up.status_code, bad_trace.status_code) == (400, 400)
    assert by_trace == [
        ("task-1", "normal"), ("task-1", "sensitive"), ("task-1", "sensitive"),
        ("task-2", "normal"), (None, "normal"), ("task-1", "sensitive"),
    ]  # fmt: skip
    assert len(one_trace) == 4
    assert me["max_sensitivity"] == "sensitive"
    assert not any(name.startswith("x-loadcoach") for name in forwarded)  # none of it reaches the model server


def test_old_entries_are_removed_and_recent_ones_kept(tmp_path):
    log = Log(str(tmp_path / "log.db"), keep_days=90)
    old = log.add("request", "chat/completions", "pat")
    log.set_trace_level("pat", "old-task", "sensitive")
    log.db.execute("UPDATE events SET at = at - 91 * 86400 WHERE id = ?", (old,))
    log.db.execute("UPDATE traces SET at = at - 91 * 86400")
    recent = log.add("request", "chat/completions", "pat")

    removed = log.prune()
    kept_for_ever = Log(str(tmp_path / "other.db"), keep_days=0)
    kept_for_ever.add("loadcoach", "started")

    assert removed == 1 and [entry["id"] for entry in log.read()] == [recent]
    assert log.trace_level("pat", "old-task") is None
    assert kept_for_ever.prune() == 0 and len(kept_for_ever.read()) == 1


def test_the_command_line_reads_the_log_and_writes_its_own_user_changes_to_it(tmp_path, capsys):
    users, log = Users(str(tmp_path / "loadcoach.db"), GROUPS), Log(str(tmp_path / "loadcoach.db"))
    user_command(users, "add", ["pat", "plain"], log)
    user_command(users, "group", ["pat", "admin"], log)
    user_command(users, "disable", ["pat"], log)
    user_command(users, "key", ["pat", "laptop"], log)
    people = tmp_path / "people.csv"
    people.write_text("ann,plain\nbob,plain\n")
    user_command(users, "import", [str(people)], log)
    user_command(users, "remove", ["pat"], log)
    log.add("request", "done", "ann", "10.0.0.5", "small", "task-9", "sensitive", status=200, ran=1.5)
    keys_printed = capsys.readouterr().out

    log_command(log, 20, False, user=None, kind=None, trace=None)
    everything = capsys.readouterr().out.splitlines()
    log_command(log, 20, False, user="ann", kind=None, trace=None)
    only_ann = capsys.readouterr().out.splitlines()
    log_command(log, 2, False, user=None, kind="user", trace=None)
    last_two_user_entries = capsys.readouterr().out.splitlines()

    assert [line.split()[3:6] for line in everything[:7]] == [
        ["user", "added", "command-line"], ["user", "changed", "command-line"], ["user", "changed", "command-line"],
        ["user", "key-added", "command-line"], ["user", "added", "command-line"], ["user", "added", "command-line"],
        ["user", "removed", "command-line"],
    ]  # fmt: skip
    assert "name=pat  group=admin" in everything[1] and "enabled=False" in everything[2]
    assert "model=small" in everything[7] and "trace=task-9" in everything[7] and "level=sensitive" in everything[7]
    assert "status=200" in everything[7] and "ran=1.5" in everything[7]
    assert len(only_ann) == 1 and len(last_two_user_entries) == 2
    assert not any(key in "\n".join(everything) for key in keys_printed.split() if key.startswith("lc_"))


# ---- deep capture and the chain ---------------------------------------------------------------

WEATHER_TOOL = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
ASKS_FOR_WEATHER = {
    "choices": [{
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I should look it up.",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}],
        },
    }],
    "usage": {"prompt_tokens": 12, "completion_tokens": 9},
}  # fmt: skip
SAYS_SUNNY = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "It is sunny."}}]}


def test_a_sensitive_piece_of_work_can_be_read_back_from_the_log_alone(fake_llama_server):
    groups = {
        **GROUPS,
        "cleared": Group(can=frozenset({"load"}), max_sensitivity="sensitive"),
        "auditor": Group(can=frozenset({"see-log", "see-capture"})),
    }
    system = {"role": "system", "content": "You are careful."}
    question = {"role": "user", "content": "What is the weather in Paris?"}
    model_asked = {"role": "assistant", "tool_calls": ASKS_FOR_WEATHER["choices"][0]["message"]["tool_calls"]}
    tool_said = {"role": "tool", "tool_call_id": "call-1", "content": "Sunny, 21 C"}

    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        cleo = add_user(caller, "cleo", "cleared", by=admin)
        auditor = add_user(caller, "ada", "auditor", by=admin)
        pat = add_user(caller, "pat", "plain", by=admin)
        marked = {**cleo, "x-loadcoach-sensitivity": "sensitive", "x-loadcoach-trace": "weather-1"}

        first = {"model": "small", "temperature": 0.2, "messages": [system, question], "tools": WEATHER_TOOL}
        caller.post("/v1/chat/completions", json={**first, "reply": ASKS_FOR_WEATHER}, headers=marked)
        second = {**first, "messages": [system, question, model_asked, tool_said]}
        caller.post("/v1/chat/completions", json={**second, "reply": SAYS_SUNNY}, headers=marked)
        caller.post("/v1/chat/completions", json={"model": "small", "messages": [question]}, headers=cleo)  # ordinary

        story = events(caller, auditor, trace="weather-1", text=1)
        stored = caller.get("/loadcoach/events", params={"trace": "weather-1", "text": 1}, headers=auditor).text
        without_text = events(caller, auditor, trace="weather-1")
        ordinary = [entry for entry in events(caller, auditor, user="cleo", text=1) if entry["trace"] is None]
        not_allowed = [
            caller.get("/loadcoach/events", params={"text": 1}, headers=who).status_code for who in (cleo, pat)
        ]

    kinds = [(entry["kind"], entry["what"]) for entry in story]
    assert kinds == [
        ("request", "asked"), ("request", "done"), ("tool", "asked"),
        ("tool", "result"), ("request", "asked"), ("request", "done"),
    ]  # fmt: skip
    first_asked, first_done, tool_asked, tool_result, second_asked, second_done = story

    # What was asked of the model, the first time: everything, since nothing had been kept yet.
    assert first_asked["text"]["body"]["temperature"] == 0.2 and "reply" in first_asked["text"]["body"]
    assert [piece["message"] for piece in first_asked["text"]["messages"]] == [system, question]
    assert first_asked["text"]["tools"]["tools"] == WEATHER_TOOL

    # What the model did: its reasoning, and the tool it asked for with each parameter by name.
    assert first_done["text"] == {
        "reasoning": "I should look it up.", "finish_reason": "tool_calls", "tool_calls": [{"id": "call-1", "tool": "get_weather"}],
    }  # fmt: skip
    assert (first_done["prompt_tokens"], first_done["answer_tokens"]) == (12, 9)
    assert (tool_asked["tool"], tool_asked["call"], tool_asked["of"]) == ("get_weather", "call-1", first_asked["source"])
    assert tool_asked["text"] == {"arguments": {"city": "Paris"}}

    # What came back from the tool, as the harness reported it, and then the final answer.
    assert tool_result["call"] == "call-1" and tool_result["text"]["message"] == tool_said
    assert [piece["message"] for piece in second_asked["text"]["messages"]] == [system, question, model_asked, tool_said]
    assert second_done["text"] == {"content": "It is sunny.", "finish_reason": "stop"}

    # The conversation is kept once, however often it is sent: each message appears a single time.
    assert stored.count("You are careful.") == 2  # once kept, once more where it was filled back in for reading
    assert all("text" not in entry for entry in without_text + ordinary)
    assert not_allowed == [403, 403]  # not even the user whose work it was, without the ability


def test_a_streamed_answer_is_put_back_together_and_an_odd_one_is_kept_as_it_came():
    pieces = [
        {"choices": [{"delta": {"role": "assistant", "content": None}}]},
        {"choices": [{"delta": {"reasoning_content": "Think. "}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_shell", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"command\": "}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"ls -la\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "timings": {"predicted_n": 5}},
    ]
    stream = "".join(f"data: {json.dumps(piece)}\n\n" for piece in pieces).encode() + b"data: [DONE]\n\n"

    answer, calls = answer_text(stream, "text/event-stream")
    broken_arguments = {"choices": [{"message": {"tool_calls": [{"id": "c2", "function": {"name": "f", "arguments": "{oops"}}]}}]}
    _, odd_calls = answer_text(json.dumps(broken_arguments).encode(), "application/json")
    not_chat, no_calls = answer_text(b'{"data": [{"embedding": [0.1]}]}', "application/json")

    assert answer == {
        "content": "Hello", "reasoning": "Think. ", "finish_reason": "tool_calls", "tool_calls": [{"id": "c1", "tool": "run_shell"}],
    }  # fmt: skip
    assert calls == [{"id": "c1", "tool": "run_shell", "arguments": {"command": "ls -la"}}]  # the command, by name
    assert odd_calls[0]["arguments"] == "{oops"  # not JSON, so exactly what the model wrote
    assert not_chat == {"raw": '{"data": [{"embedding": [0.1]}]}'} and no_calls == []


def test_only_what_is_new_in_a_request_is_kept_and_reading_fills_it_back_in():
    system, question = {"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}
    result = {"role": "tool", "tool_call_id": "c1", "content": "42"}
    already = set()

    first, first_results, first_new = request_text({"model": "m", "messages": [system, question], "tools": ["t"]}, already.__contains__)
    already.update(first_new)
    second, second_results, second_new = request_text(
        {"model": "m", "messages": [system, question, result, result], "tools": ["t"]}, already.__contains__
    )

    assert first["body"] == {"model": "m"} and len(first_new) == 3 and first_results == []
    assert ["message" in piece for piece in first["messages"]] == [True, True]
    assert [list(piece) for piece in second["messages"]] == [["seen"]] * 4 and list(second["tools"]) == ["seen"]
    assert len(second_results) == 1 and second_results[0]["message"] == result and len(second_new) == 1

    alone = resolve([{"text": json.loads(json.dumps(second))}])  # a copy, read with nothing to fill it from
    entries = resolve([{"text": first}, {"text": second_results[0]}, {"text": second}, {"what": "no text here"}])
    assert [piece["message"] for piece in entries[2]["text"]["messages"]] == [system, question, result, result]
    assert entries[2]["text"]["tools"]["tools"] == ["t"]
    assert alone[0]["text"]["messages"][0] == {"seen": first["messages"][0]["hash"]}  # left as a reference


def test_the_chain_shows_an_entry_that_was_changed_or_removed(tmp_path):
    def fresh(name):
        log = Log(str(tmp_path / name))
        for number in range(5):
            log.add("request", "done", "pat", text={"content": f"answer {number}"} if number == 2 else None, status=200)
        return log

    intact = fresh("intact.db")
    edited, retexted, removed = fresh("edited.db"), fresh("retexted.db"), fresh("removed.db")
    edited.db.execute("UPDATE events SET detail = '{\"status\": 500}' WHERE id = 2")
    retexted.db.execute("UPDATE events SET text = '{\"content\": \"something else\"}' WHERE id = 3")
    removed.db.execute("DELETE FROM events WHERE id = 3")

    two_writers = Log(str(tmp_path / "shared.db")), Log(str(tmp_path / "shared.db"))  # LoadCoach and the command
    for number in range(6):
        two_writers[number % 2].add("user", "added", "command-line", name=f"user-{number}")

    aged = fresh("aged.db")
    aged.db.execute("UPDATE events SET at = at - 91 * 86400 WHERE id <= 2")
    aged.prune()

    assert intact.verify() == (4, None)  # five entries: the oldest is taken on trust, four follow from it
    assert edited.verify() == (0, 2)
    assert retexted.verify() == (1, 3)
    assert removed.verify() == (1, 4)  # the entry after the gap no longer follows from the one before
    assert two_writers[0].verify() == (5, None)
    assert aged.verify() == (2, None)  # removing the oldest by age does not break what remains


def test_a_log_made_before_the_chain_existed_still_opens(tmp_path):
    path = str(tmp_path / "older.db")
    older = sqlite3.connect(path)
    older.executescript(
        """CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
               what TEXT NOT NULL, user TEXT NOT NULL, caller TEXT NOT NULL, model TEXT, trace TEXT, level TEXT,
               detail TEXT NOT NULL);
           INSERT INTO events (at, kind, what, user, caller, detail) VALUES (1.0, 'loadcoach', 'started', '', '', '{}');"""
    )
    older.close()

    log = Log(path, keep_days=0)
    log.add("loadcoach", "started")
    log.add("loadcoach", "stopped")

    assert [entry["what"] for entry in log.read()] == ["started", "started", "stopped"]
    assert log.verify() == (1, None)  # the old entry has no place in the chain and is passed over


def test_a_sensitive_request_that_cannot_be_recorded_is_not_run(fake_llama_server, tmp_path, capsys):
    database = str(tmp_path / "loadcoach.db")
    with gateway(fake_llama_server, database=database) as caller:
        before = ask(caller, "small").json()
        breaking = sqlite3.connect(database)  # stands in for a full disk: nothing more can be added
        breaking.execute("CREATE TRIGGER full BEFORE INSERT ON events BEGIN SELECT RAISE(FAIL, 'disk is full'); END")
        breaking.commit()

        sensitive = caller.post(
            "/v1/chat/completions", json={"model": "big"}, headers={"x-loadcoach-sensitivity": "sensitive"}
        )
        ordinary = ask(caller, "small")
        still_loaded = [model["model"] for model in caller.get("/loadcoach/status").json()["loaded"]]

    assert sensitive.status_code == 503
    assert "marked sensitive and could not be recorded, so it was not run" in sensitive.json()["error"]["message"]
    assert still_loaded == ["small"]  # big was never loaded: the request did not reach the line
    assert ordinary.status_code == 200 and ordinary.json()["pid"] == before["pid"]  # ordinary work carries on
    assert '"event":"audit_write_failed"' in capsys.readouterr().err


def test_the_command_line_shows_kept_text_and_checks_the_chain(tmp_path, capsys):
    log = Log(str(tmp_path / "loadcoach.db"))
    kept = {"hash": "abc", "message": {"role": "user", "content": "the secret question"}}
    log.add("request", "asked", "cleo", "10.0.0.5", "small", "task-1", "sensitive", text={"body": {}, "messages": [kept]})
    log.add("request", "asked", "cleo", "10.0.0.5", "small", "task-1", "sensitive", text={"body": {}, "messages": [{"seen": "abc"}]})
    log.add("request", "done", "cleo", "10.0.0.5", "small", "task-1", "sensitive", text={"content": "the answer"}, status=200)

    log_command(log, 20, False, user=None, kind=None, trace=None)
    plain = capsys.readouterr().out
    log_command(log, 20, False, text=True, user=None, kind=None, trace="task-1")
    with_text = capsys.readouterr().out
    log_command(log, 20, False, text=True, as_json=True, user=None, kind=None, trace="task-1")
    as_json = json.loads(capsys.readouterr().out)
    verify_command(log)
    intact = capsys.readouterr().out

    log.db.execute("UPDATE events SET text = '{\"content\": \"a different answer\"}' WHERE id = 3")
    with pytest.raises(ValueError) as altered:
        verify_command(log)

    assert "secret question" not in plain and "the answer" not in plain and plain.count("\n") == 3
    assert with_text.count("the secret question") == 2 and "the answer" in with_text  # the second is filled back in
    assert as_json[1]["text"]["messages"][0]["message"]["content"] == "the secret question"
    assert "intact: 2 entries" in intact
    assert "has been altered" in str(altered.value) and "Entry 3" in str(altered.value)


def test_model_defaults_reach_every_model_unless_it_sets_its_own(tmp_path):
    gguf = tmp_path / "m.gguf"
    gguf.touch()
    path = tmp_path / "loadcoach.toml"
    defaults = '[model_defaults]\ncache-type-k = "q8_0"\nflash-attn = "on"\ngpu-layers = "all"\n'
    one = f'[models.plain]\ngpus = 0\n[models.plain.flags]\nmodel = "{gguf}"\nctx-size = 4096\n'
    two = f'[models.own]\ngpus = 0\n[models.own.flags]\nmodel = "{gguf}"\nctx-size = 8192\ncache-type-k = "f16"\n'

    path.write_text(defaults + one + two)
    config = load_config(path)
    kept = Models(":memory:", config.model_defaults, config.models)
    models = {name: kept.get(name) for name in kept.names()}
    assert kept.stored("plain")["flags"] == {"model": str(gguf), "ctx-size": 4096}  # kept as given; defaults join on reading
    path.write_text('[model_defaults]\nport = 1\n' + one)
    with pytest.raises(SystemExit, match="LoadCoach sets those"):  # a default is checked like any other flag
        load_config(path)

    assert models["plain"]["flags"] == {"cache-type-k": "q8_0", "flash-attn": "on", "gpu-layers": "all", "model": str(gguf), "ctx-size": 4096}
    assert models["own"]["flags"]["cache-type-k"] == "f16" and models["own"]["flags"]["flash-attn"] == "on"
    assert command_line("llama-server", 1, models["own"]["flags"], ())[5:] == [
        "--device", "none", "--cache-type-k", "f16", "--flash-attn", "on", "--gpu-layers", "all",
        "--model", str(gguf), "--ctx-size", "8192",
    ]  # fmt: skip


def test_the_database_is_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "loadcoach.db"
    path.touch(mode=0o644)  # as a file made before this was looked after would be

    Users(str(path), GROUPS)
    log = Log(str(path))
    log.add("loadcoach", "started")

    modes = {file.name: file.stat().st_mode & 0o777 for file in tmp_path.iterdir()}
    assert set(modes.values()) == {0o600} and "loadcoach.db-wal" in modes


def test_command_line_turns_settings_into_flags():
    flags = {"model": "m.gguf", "ctx-size": 4096, "split-mode": "layer", "mlock": True, "jinja": False}

    assert command_line("llama-server", 5000, flags, ("CUDA0", "CUDA1")) == [
        "llama-server", "--host", "127.0.0.1", "--port", "5000", "--device", "CUDA0,CUDA1",
        "--model", "m.gguf", "--ctx-size", "4096", "--split-mode", "layer", "--mlock",
    ]  # fmt: skip
    assert command_line("llama-server", 5000, {}, ())[5:] == ["--device", "none"]  # no cards: CPU only


def test_config_refuses_what_would_fail_later(tmp_path, monkeypatch):
    path = tmp_path / "loadcoach.toml"
    gguf = tmp_path / "m.gguf"
    gguf.touch()
    cards = 'gpus = ["A", "B"]\n'
    flags = f'[models.m.flags]\nmodel = "{gguf}"\nctx-size = 4096\n'

    def refused(text, because):
        path.write_text(text)
        with pytest.raises(SystemExit, match=because):
            load_config(path)

    refused('api_keys = ["k"]\n' + cards + flags, "api_keys is gone")
    refused('host = "10.0.0.1" port = 8442\n' + cards + flags, r"not valid TOML.*line 1, column 19")  # a slip says where, in one line
    with pytest.raises(SystemExit, match="cannot be read"):
        load_config(tmp_path / "no-such-file.toml")
    refused(cards + flags + "[groups.g]\ncan = [\"load\"]\n", "kept on the front machine")  # since milestone 76
    refused('upstreem = "typo"\n' + cards + flags, "upstreem")
    refused(cards + f'[models.m.flags]\nmodel = "{gguf}"\n', "ctx-size")
    refused(cards + flags + 'device = "A"\n', "LoadCoach sets those")
    refused(cards + f'[models.m]\nmodel = "{gguf}"\nctx-size = 1\n', "flags go in")  # the old shape
    refused(cards + "[models.m]\ngpus = 3\n" + flags, "gpus must be")
    refused(cards + '[models.m]\ngpus = ["A", "C"]\n' + flags, "gpus must be")
    monkeypatch.setattr("loadcoach.config.detect", lambda _server: [{"id": "A"}, {"id": "B"}])
    path.write_text(flags)
    assert load_config(path).gpus == ("A", "B")  # omitted cards are discovered at startup
    path.write_text("[models.m]\ngpus = 0\n" + flags)
    cpu_model = load_config(path)
    assert cpu_model.gpus == ("A", "B")  # a CPU model does not hide the node's GPU inventory
    assert cpu_model.models["m"]["gpus"] == 0

    path.write_text('host = "0.0.0.0"\nmachine_key_file = "keys/machine.key"\nfront = "http://10.0.0.2:8442"\n' + cards + "[models.m]\ngpus = 1\n" + flags)
    loaded = load_config(path)
    assert loaded.gpus == ("A", "B")
    assert loaded.models == {"m": {"gpus": 1, "flags": {"model": str(gguf), "ctx-size": 4096}}}
    assert (loaded.machine_key_file, loaded.front) == (str(tmp_path / "keys/machine.key"), "http://10.0.0.2:8442")  # counted from the settings file
    assert loaded.database == str(tmp_path / "loadcoach.db")  # beside the configuration

    path.write_text("gpus = []\n[models.m]\ngpus = 0\n" + flags)  # a CPU-only setup explicitly lists no cards
    assert load_config(path).gpus == ()

    # None of these stops LoadCoach any more: models are kept in the database and may come later,
    # and a file that has gone missing is that one model's problem.
    path.write_text(cards + '[models.m.flags]\nmodel = "/no/such.gguf"\nctx-size = 1\n')
    assert "m" in load_config(path).models
    path.write_text(cards)
    assert load_config(path).models == {}


# ---- hosted models ------------------------------------------------------------------------------
# Outside services, their keys and what may leave are the front machine's: see test_hosted_front.py.


@pytest.fixture
def hosted_service():
    """A stand-in for a hosted service such as OpenRouter: the same fake, started by the test itself.

    Yields its address as `hosted_url`. It answers with what it was sent, so a test can see exactly what left.
    """
    port = free_port()
    service = subprocess.Popen([sys.executable, str(FAKE), "--host", "127.0.0.1", "--port", str(port)], stdout=subprocess.DEVNULL)
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield {"hosted_url": f"http://127.0.0.1:{port}/v1"}
    service.kill()
    service.wait()


def test_the_hosted_section_and_service_keys_are_ignored_with_a_note_in_compute_s_settings(tmp_path, capsys):
    (tmp_path / "m.gguf").write_text("")
    local = f'gpus = ["A"]\n[models.small.flags]\nmodel = "{tmp_path}/m.gguf"\nctx-size = 1\n'
    file = tmp_path / "loadcoach.toml"
    file.write_text('keys = "../keys"\n' + local + '[hosted]\nurl = "https://example.test/api/v1"\nkey_env = "K"\n')
    config = load_config(file)
    said = capsys.readouterr().err
    assert "[hosted] is ignored" in said and "[keys] is ignored" in said and "front machine" in said
    assert not hasattr(config, "hosted_models") and "small" in config.models


# ---- stopping and looking at one request --------------------------------------------------------


def test_one_request_can_be_looked_at_and_stopped_while_it_is_being_answered(fake_llama_server, tmp_path, capsys):
    token_file = tmp_path / "loadcoach.cli-token"
    with gateway(fake_llama_server, token_file=str(token_file)) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        cam = add_user(caller, "cam", "plain", by=admin)
        looping = others.submit(caller.post, "/v1/chat/completions", json={"model": "small", "drip": 20}, headers=pat)
        entry = wait_for_status(_signed(caller, admin), lambda s: s["running"])["running"][0]["id"]
        behind = others.submit(caller.post, "/v1/chat/completions", json={"model": "small"}, headers=pat)  # only now, so it is second
        wait_for_status(_signed(caller, admin), lambda s: s["waiting"])

        # What it has written so far can be read as it passes: by its own user, or with see-capture.
        time.sleep(0.2)
        seen = caller.get(f"/loadcoach/peek/{entry}", headers=pat).json()
        assert seen["state"] == "being answered" and seen["said"]["reasoning"].startswith("round 1. round 2. ")
        assert caller.get(f"/loadcoach/peek/{entry}", headers=cam).status_code == 403
        assert caller.post(f"/loadcoach/stop/{entry}", headers=cam).status_code == 403  # nor may cam stop it
        assert caller.post("/loadcoach/stop/999999", headers=admin).status_code == 404

        # The command line shows the id, the text, and stops that one request. The one behind it goes ahead.
        command = OnCompute(caller)
        command.headers["authorization"] = "Bearer " + token_file.read_text()
        server_command(command, "status")
        server_command(command, "peek", str(entry))
        server_command(command, "peek", str(entry))  # looking again, as a page that follows the answer does every second
        began = time.monotonic()
        server_command(command, "stop", str(entry))
        cut_short, went_ahead = looping.result(), behind.result()
        assert time.monotonic() - began < 2  # not the 20 seconds it had left
        shown = capsys.readouterr().out
        assert f"  {entry}" in shown and "REASONING" in shown and "round 3. " in shown and f"Stopped request {entry}." in shown
        assert cut_short.status_code == 200 and "round 1. " in cut_short.text  # what had been sent stays sent
        assert went_ahead.status_code == 200 and "pid" in went_ahead.json()

        ending = [event for event in events(caller, admin, user="pat") if event.get("of") == entry][-1]  # the log is front's
        assert (ending["what"], ending["by"]) == ("stopped", "command-line")

        # One address tells everything on record about one request, to its own user or with see-log.
        over = caller.get(f"/loadcoach/requests/{entry}", headers=admin).json()
        assert [look["user"] for look in over["looked_at"]] == ["command-line"]  # on record once, however often they looked
        assert (over["asked"]["user"], over["asked"]["model"], over["ended"]["what"], over["state"], over["may_stop"]) == ("pat", "small", "stopped", None, False)
        assert caller.get(f"/loadcoach/requests/{entry}", headers=cam).status_code == 403 and caller.get(f"/loadcoach/requests/{entry}", headers=pat).status_code == 200
        assert caller.get("/loadcoach/requests/1", headers=admin).status_code == 404  # entry 1 is something else, not a request
        assert caller.get("/loadcoach/status", headers=admin).json()["cards"] == list(CARDS)
        assert command.get("/loadcoach/status").json()["running"] == []


def test_a_request_can_be_stopped_in_line_and_before_the_model_has_answered_anything(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        thinking = others.submit(caller.post, "/v1/chat/completions", json={"model": "small", "sleep": 20}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        in_line = others.submit(caller.post, "/v1/chat/completions", json={"model": "small"}, headers=pat)
        status = wait_for_status(_signed(caller, admin), lambda s: s["waiting"])

        # Pat takes their own request out of the line. Nothing else is disturbed.
        waiting = status["waiting"][0]["id"]
        assert caller.get(f"/loadcoach/peek/{waiting}", headers=pat).json() == {"id": waiting, "user": "pat", "model": "small", "state": "in line", "said": {}}
        assert caller.post(f"/loadcoach/stop/{waiting}", headers=pat).json() == {"stopped": waiting}
        gone = in_line.result()
        assert gone.status_code == 400 and gone.json()["error"]["message"] == "Stopped by pat."

        # The one the model is still working on, with nothing sent back, stops at once too.
        began = time.monotonic()
        working = status["running"][0]["id"]
        assert caller.get(f"/loadcoach/peek/{working}", headers=admin).json()["state"] == "sent, nothing back yet"
        assert caller.post(f"/loadcoach/stop/{working}", headers=admin).status_code == 200
        stopped = thinking.result()
        assert time.monotonic() - began < 2
        assert stopped.status_code == 400 and stopped.json()["error"] == {"message": "Stopped by jordan.", "type": "loadcoach_error", "code": "stopped"}

        after = caller.get("/loadcoach/status", headers=admin).json()
        assert (after["running"], after["waiting"]) == ([], [])  # and the model is free for the next request
        assert [event["what"] for event in events(caller, admin, kind="request") if "of" in event][-2:] == ["stopped", "stopped"]
        assert caller.post("/v1/chat/completions", json={"model": "small"}, headers=pat).status_code == 200


# ---- models that come, change and go while LoadCoach runs ---------------------------------------


@pytest.mark.parametrize('stage', ['waiting', 'thinking', 'streaming'])
def test_unlogged_requests_can_be_stopped_without_retained_evidence(fake_llama_server, tmp_path, stage):
    groups = {**GROUPS, 'quiet': Group(can=frozenset({'load', 'unlogged'})),
              'viewer': Group(can=frozenset({'load', 'see-all'}))}
    file = str(tmp_path/'quiet.db')
    with gateway(fake_llama_server, database=file, groups=groups, unlogged=True) as caller, ThreadPoolExecutor() as pool:
        admin = add_user(caller, 'admin', 'admin')
        owner = add_user(caller, 'owner', 'quiet', by=admin)
        viewer = add_user(caller, 'viewer', 'viewer', by=admin)
        quiet = {**owner, 'x-loadcoach-log': 'off'}
        assert caller.post('/v1/chat/completions', json={'model': 'small'}, headers=owner).status_code == 200
        def counts():
            with sqlite3.connect(file) as saved, sqlite3.connect(Path(file).with_name('front') / 'roles.db') as front:  # reliability is kept on the front machine
                return [saved.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('events', 'texts', 'traces')] + [
                        front.execute('SELECT COUNT(*) FROM reliability_outcomes').fetchone()[0]]
        before = counts()
        blocker, blocking_handle = None, None
        if stage == 'waiting':
            blocker = pool.submit(caller.post, '/v1/chat/completions', json={'model': 'small', 'sleep': 8}, headers=quiet)
            blocking_handle = wait_for_status(_signed(caller, admin), lambda value: value['running'])['running'][0]['stop_handle']
        payload = {'model': 'small', **({'drip': 8} if stage == 'streaming' else {'sleep': 8})}
        future = pool.submit(caller.post, '/v1/chat/completions', json=payload, headers=quiet)
        part = 'waiting' if stage == 'waiting' else 'running'
        row = wait_for_status(_signed(caller, admin), lambda value: value[part])[part][0]
        handle = row['stop_handle']
        assert row['id'] == 0 and row['unlogged'] and row['may_stop'] and handle != blocking_handle
        own = caller.get('/loadcoach/status', headers=owner)
        assert own.headers['cache-control'] == 'no-store'
        assert own.json()[part][0]['stop_handle'] == handle
        foreign = caller.get('/loadcoach/status', headers=viewer).json()[part][0]
        assert foreign['id'] == 0 and 'stop_handle' not in foreign and not foreign.get('may_stop')
        assert caller.post('/loadcoach/stop-unlogged', json={'handle': handle}, headers=viewer).status_code == 403
        assert caller.get('/loadcoach/peek/0', headers=admin).status_code == 404
        assert caller.get('/loadcoach/requests/0', headers=admin).status_code == 404
        if stage == 'streaming':
            time.sleep(.15)  # let the mock server send a few chunks before cancellation
        began = time.monotonic()
        stopped = caller.post('/loadcoach/stop-unlogged', json={'handle': handle}, headers=admin if stage == 'thinking' else owner)
        assert stopped.status_code == 200 and stopped.json() == {'stopped': True}
        response = future.result(timeout=3)
        assert time.monotonic()-began < 2
        assert response.status_code == (200 if stage == 'streaming' else 400)
        assert 'x-loadcoach-request-id' not in response.headers
        assert caller.post('/loadcoach/stop-unlogged', json={'handle': handle}, headers=owner).status_code == 404
        if blocker:
            assert not blocker.done()  # stopping the waiting request leaves its predecessor alone
            assert caller.post('/loadcoach/stop-unlogged', json={'handle': blocking_handle}, headers=owner).status_code == 200
            assert blocker.result(timeout=3).status_code == 400
        assert counts() == before
        assert caller.get('/loadcoach/status', headers=admin).json()['running'] == []
        assert caller.get('/loadcoach/status', headers=admin).json()['waiting'] == []
        assert caller.post('/v1/chat/completions', json={'model': 'small'}, headers=quiet).status_code == 200
        assert counts() == before


def test_a_model_is_added_changed_and_removed_while_others_keep_answering(fake_llama_server):
    with gateway(fake_llama_server, ONE_CARD_EACH) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        send = lambda model, who=admin, **body: caller.post("/v1/chat/completions", json={"model": model, **body}, headers=who)  # noqa: E731
        Path("new.gguf").touch()

        # Someone is being answered throughout. Nothing below may disturb them.
        busy = others.submit(send, "a", sleep=1.2)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        steady = send("b").json()["pid"]

        # Added: there at the next request, with no restart. Only with the ability.
        assert send("new").status_code == 404
        new = {"gpus": 1, "flags": {"model": "new.gguf", "ctx-size": 1024}}
        assert caller.post("/loadcoach/models/new", json=new, headers=pat).status_code == 403
        assert caller.post("/loadcoach/models/new", json=new, headers=admin).status_code == 201
        assert caller.post("/loadcoach/models/bad", json={"flags": {"model": "x.gguf"}}, headers=admin).status_code == 400
        assert caller.post("/loadcoach/models/new?new=1", json=new, headers=admin).status_code == 409  # add only, never replace
        assert "new" in [model["id"] for model in caller.get("/v1/models", headers=pat).json()["data"]]

        # Changed: a loaded model keeps its settings until it is next idle and asked for, then loads afresh.
        first = send("b").json()
        assert first["pid"] == steady and "--ctx-size 2048" in " ".join(first["arguments"])
        changed = {"gpus": 1, "flags": {"model": "b.gguf", "ctx-size": 4096}}
        assert caller.post("/loadcoach/models/b", json=changed, headers=admin).status_code == 200
        listed = {model["name"]: model for model in caller.get("/loadcoach/models", headers=admin).json()["models"]}
        assert listed["b"]["stale"] and listed["b"]["loaded"] == "ready" and listed["a"]["running"] == 1 and listed["new"]["size"] == 0
        second = send("b").json()
        assert second["pid"] != steady and "--ctx-size 4096" in " ".join(second["arguments"]) and not alive(steady)

        # Removed: gone for new requests at once. The answer under way is finished, and then it is unloaded.
        assert caller.delete("/loadcoach/models/a", headers=admin).json() == {"removed": "a", "still_finishing": 1}
        assert send("a").status_code == 404
        finished = busy.result()
        assert finished.status_code == 200
        loaded = wait_for_status(_signed(caller, admin), lambda s: "a" not in [model["model"] for model in s["loaded"]])
        assert gone(finished.json()["pid"]) and [model["model"] for model in loaded["loaded"]] == ["b"]
        assert caller.delete("/loadcoach/models/a", headers=admin).status_code == 404
        whats = [event["what"] for event in events(caller, admin, kind="model")]
        assert whats.count("added") == 1 and whats.count("changed") == 1 and whats.count("removed") == 1


def test_a_request_in_line_for_a_model_is_still_answered_after_the_model_is_removed(fake_llama_server):
    with gateway(fake_llama_server) as caller, ThreadPoolExecutor() as others:  # small and big each need both cards
        admin = add_user(caller, "jordan", "admin")
        running = others.submit(caller.post, "/v1/chat/completions", json={"model": "small", "sleep": 0.8}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        in_line = others.submit(caller.post, "/v1/chat/completions", json={"model": "big"}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda s: s["waiting"])

        # `big` is not even loaded yet. It is removed while one request waits for it.
        assert caller.delete("/loadcoach/models/big", headers=admin).json()["still_finishing"] == 1
        answered = in_line.result()
        assert running.result().status_code == 200
        assert answered.status_code == 200 and "--model big.gguf" in " ".join(answered.json()["arguments"])
        wait_for_status(_signed(caller, admin), lambda s: not s["loaded"] or s["loaded"][0]["model"] != "big")
        assert gone(answered.json()["pid"])  # with nobody left to want it, it was unloaded


def test_a_model_whose_file_is_missing_is_refused_and_stops_nothing_else(fake_llama_server):
    models = {"here": model("here.gguf", 1), "gone": model("nowhere/gone.gguf", 1)}
    with gateway(fake_llama_server, models) as caller:
        missing = caller.post("/v1/chat/completions", json={"model": "gone"})
        assert missing.status_code == 503 and missing.json()["error"]["message"] == "The file of gone is missing: nowhere/gone.gguf"
        assert caller.post("/v1/chat/completions", json={"model": "here"}).status_code == 200
        listed = {entry["name"]: entry["size"] for entry in caller.get("/loadcoach/models").json()["models"]}
        assert listed == {"here": 0, "gone": None}


def test_models_are_looked_after_from_the_command_line_and_taken_over_from_the_file_once(tmp_path, capsys):
    (tmp_path / "old.gguf").write_bytes(b"x" * 2048)
    (tmp_path / "new.gguf").touch()
    file = tmp_path / "loadcoach.toml"
    in_file = f'gpus = ["A", "B"]\n[model_defaults]\nflash-attn = "on"\n[models.old]\ngpus = 1\n[models.old.flags]\nmodel = "{tmp_path}/old.gguf"\nctx-size = 4096\n'
    file.write_text(in_file)
    config = load_config(file)
    models = Models(config.database, config.model_defaults, config.models)
    log = Log(config.database)
    assert models.taken_over == 1 and models.names() == ["old"]  # what the file held is where the models start from

    model_command(models, config, "add", ["new", str(tmp_path / "new.gguf"), "ctx-size=8192", "gpus=2", "tensor-split=[1,1]", "split-mode=layer"], log)
    model_command(models, config, "set", ["old", "ctx-size=16384", "parallel=2", "gpus="], log)
    assert models.stored("new") == {"gpus": 2, "flags": {"model": str(tmp_path / "new.gguf"), "ctx-size": 8192, "tensor-split": [1, 1], "split-mode": "layer"}}
    assert models.stored("old") == {"flags": {"model": f"{tmp_path}/old.gguf", "ctx-size": 16384, "parallel": 2}}
    assert models.get("old")["flags"]["flash-attn"] == "on"  # the defaults still come from the file

    capsys.readouterr()
    model_command(models, config, "list", [])
    model_command(models, config, "show", ["new"])
    shown = capsys.readouterr().out
    assert "old" in shown and "0.0 GB" in shown and '[models."new".flags]' in shown and "tensor-split = [1, 1]" in shown
    model_command(models, config, "export", [])
    exported = tomllib.loads(capsys.readouterr().out)["models"]
    assert exported == {name: {**models.stored(name), "order": models.place(name)} for name in ("old", "new")}  # order stays separate when read back

    for wrong in (["add", ["new", "x.gguf", "ctx-size=1"]], ["add", ["third", "x.gguf"]], ["set", ["nope", "ctx-size=1"]],
                  ["set", ["old", "port=1"]], ["set", ["old", "gpus=5"]], ["remove", ["nope"]], ["fly", []]):  # fmt: skip
        with pytest.raises(ValueError):
            model_command(models, config, *wrong, log)
    model_command(models, config, "remove", ["old"], log)

    # What a model measured is kept with it, follows its name, and goes when its settings change or when it is forgotten.
    seen = models.measured("new", models.get("new"), [9000, 8000]), models.measured("new", models.get("new"), [9500, 7000])
    assert seen == ([9000, 8000], [9500, 8000]) and models.measure("new")["mib"] == [9500, 8000]  # the highest seen for each card
    capsys.readouterr()
    model_command(models, config, "list", [])
    assert "9500 + 8000" in capsys.readouterr().out
    models.rename("new", "newer"), models.rename("newer", "new")
    model_command(models, config, "forget-measure", ["new"])
    model_command(models, config, "forget-measure", ["new"])
    assert models.measure("new") is None and "Forgot what new measured." in (said := capsys.readouterr().out) and "new has no measure." in said
    models.measured("new", models.get("new"), [9000, 8000])
    model_command(models, config, "set", ["new", "alone=true"])  # a setting changed: the measure says nothing of the model as it is now
    assert models.stored("new")["alone"] is True and models.measure("new") is None and "alone = true" in as_toml("new", models.stored("new"))
    with pytest.raises(ValueError):
        model_command(models, config, "set", ["new", "alone=sometimes"])
    model_command(models, config, "set", ["new", "alone="])
    # Adapters are given whole. What is printed of them can be read back as a configuration file.
    model_command(models, config, "set", ["new", 'adapters={"terse": {"file": "/a/terse.gguf"}, "loud": {"file": "/a/loud.gguf", "scale": 0.5}}'])
    assert tomllib.loads(as_toml("new", models.stored("new")))["models"]["new"] == models.stored("new") and len(models.stored("new")["adapters"]) == 2
    for wrong in ('adapters={"a b": {"file": "/a/x.gguf"}}', 'adapters={"x": {"file": "/a/x:y.gguf"}}', 'adapters={"x": "/a/x.gguf"}', "lora=/a/x.gguf"):
        with pytest.raises(ValueError):
            model_command(models, config, "set", ["new", wrong])
    model_command(models, config, "set", ["new", "adapters="])
    # Kin, which differ only in adapters, share a server, and so they share what it measured.
    as_new = [f"{flag}={json.dumps(value)}" for flag, value in models.stored("new")["flags"].items() if flag != "model"]
    model_command(models, config, "add", ["kin", models.stored("new")["flags"]["model"], *as_new, "gpus=2", 'adapters={"t": {"file": "/a/t.gguf"}}'])
    models.measured("kin", models.get("kin"), [9900, 100])  # `new` measured 9000 and 8000 before: the highest seen of each card counts for both
    assert models.kin("new") == ["kin", "new"] and models.measure("new")["mib"] == [9900, 8000] == models.measure("kin")["mib"]
    assert models.forget_measure("new") and models.measure("kin") is None
    model_command(models, config, "remove", ["kin"])
    models.measured("new", models.get("new"), [9000, 8000])
    (tmp_path / "new.gguf").write_bytes(b"another file under the same name")
    assert models.measure("new") is None  # a measure is of the file it was taken with

    # A second start reads nothing more from the file: the database is where models are kept now.
    file.write_text(in_file.replace("models.old", "models.other"))
    again = load_config(file)
    later = Models(again.database, again.model_defaults, again.models)
    assert later.taken_over == 0 and later.names() == ["new"]
    assert [event["what"] for event in log.read(kind="model")] == ["added", "changed", "removed"]  # the changes made with a log at hand


# ---- signing in ---------------------------------------------------------------------------------

SIGNING_IN = {
    **GROUPS,
    # Whoever sets these people up can do everything: only such a one may make the front machine's own account.
    "admin": read_group("admin", {"can": ["superuser"]}),
    "brief": Group(can=frozenset({"load"}), session_hours=0.0002),  # a sign-in that lasts under a second
    "careful": Group(can=frozenset({"load"}), second_step=True),
    "waker": Group(can=frozenset({"front-wake"})),
}


def people(caller, **groups):
    """Add an admin, the front machine's own key, and one user per `name=group` with the password 'correct horse'."""
    admin = add_user(caller, "jordan", "admin")
    front = add_user(caller, "panel", "front", by=admin)
    keys = {name: add_user(caller, name, group, by=admin) for name, group in groups.items()}
    for name, key in keys.items():
        assert caller.post(f"/loadcoach/users/{name}", json={"password": "correct horse"}, headers=key).status_code == 200
    return admin, front, keys


def sign_in(caller, front, name, password="correct horse", **more):
    return caller.post("/loadcoach/sign-in", json={"name": name, "password": password, **more}, headers=front)


def test_a_password_checked_here_gets_a_session_key_that_ends(fake_llama_server):
    with gateway(fake_llama_server, groups=SIGNING_IN) as caller:
        admin, front, keys = people(caller, pat="plain", bea="brief")

        # Only this machine, or the front machine's key, may pass a password in. And the front's key is no more than that.
        assert caller.post("/loadcoach/sign-in", json={"name": "pat", "password": "correct horse"}).status_code == 403
        assert sign_in(caller, admin, "pat").status_code == 403
        assert caller.get("/loadcoach/me", headers={**front, "x-loadcoach-user": "pat"}).json()["name"] == "panel"

        wrong, nobody = sign_in(caller, front, "pat", "wrong horse"), sign_in(caller, front, "ghost")
        assert wrong.status_code == nobody.status_code == 401 and wrong.json() == nobody.json()  # the two cannot be told apart
        signed = caller.post("/loadcoach/sign-in", json={"name": "pat", "password": "correct horse"}, headers={**front, "x-loadcoach-from": "10.0.0.7"})
        assert signed.status_code == 201 and signed.json()["name"] == "pat" and signed.json()["group"] == "plain"
        session = {"authorization": "Bearer " + signed.json()["key"]}

        me = caller.get("/loadcoach/me", headers=session).json()
        assert me["name"] == "pat" and me["session_ends"] == signed.json()["ends"]
        assert caller.get("/loadcoach/me", headers=keys["pat"]).json()["session_ends"] is None  # an API key does not end

        # What a person chooses for themselves is kept with them, and is only ever about how things are shown.
        assert me["choices"] == {"show_telemetry": True}
        assert caller.post("/loadcoach/me", json={"show_telemetry": False}, headers=session).json() == {"show_telemetry": False}
        assert caller.get("/loadcoach/me", headers=keys["pat"]).json()["choices"] == {"show_telemetry": False}  # theirs, whichever key they come with
        assert caller.post("/loadcoach/me", json={"show_telemetry": "no"}, headers=session).status_code == 400
        assert caller.post("/loadcoach/me", json={"group": "admin"}, headers=session).status_code == 400
        assert caller.post("/v1/chat/completions", json={"model": "small"}, headers=session).status_code == 200

        assert caller.post("/loadcoach/sign-out", headers=keys["pat"]).json() == {"signed_out": False}  # not a sign-in
        assert caller.post("/loadcoach/sign-out", headers=session).json() == {"signed_out": True}
        assert caller.get("/loadcoach/me", headers=session).status_code == 401
        assert caller.get("/loadcoach/me", headers=keys["pat"]).status_code == 200

        short = {"authorization": "Bearer " + sign_in(caller, front, "bea").json()["key"]}
        assert caller.get("/loadcoach/me", headers=short).status_code == 200
        time.sleep(0.9)
        assert caller.get("/loadcoach/me", headers=short).status_code == 401  # its group's hours were up

        # Switching a user off ends their sign-ins at once.
        live = {"authorization": "Bearer " + sign_in(caller, front, "pat").json()["key"]}
        caller.post("/loadcoach/users/pat", json={"enabled": False}, headers=admin)
        assert caller.get("/loadcoach/me", headers=live).status_code == 401 and sign_in(caller, front, "pat").status_code == 401

        seen = caller.log.read(0, 200, "pat")  # sign-ins are on record at the front machine
        assert [event["at_front_from"] for event in seen if event["what"] == "signed-in" and "at_front_from" in event] == ["10.0.0.7"]
        assert [event["why"] for event in seen if event["what"] == "sign-in"] == ["wrong", "wrong"]
        listed = {user["name"]: user for user in caller.get("/loadcoach/users", headers=admin).json()["users"]}
        assert listed["pat"]["sessions"] == 1 and [key["label"] for key in listed["pat"]["keys"]] == ["first"]


def test_this_machine_may_sign_in_with_no_key_and_too_many_wrong_tries_lock_a_name(fake_llama_server):
    settings = Config(models={"small": model("small.gguf")}, gpus=CARDS, llama_server=fake_llama_server, machine_key_file=str(Path.cwd() / "machine.key"))
    # A browser on the front machine itself, where turnstile is.
    with front_of(lambda front: build_app(settings, None, front), Users(":memory:", SIGNING_IN), Path.cwd(), address="127.0.0.1") as caller:
        people(caller, pat="plain", cam="plain")
        here = lambda name, password="correct horse": caller.post("/loadcoach/sign-in", json={"name": name, "password": password})  # noqa: E731
        assert here("pat").status_code == 201

        assert [here("pat", "guess").status_code for _ in range(5)] == [401] * 5
        locked = here("pat")  # even the right password now waits
        assert locked.status_code == 429 and locked.json()["error"]["code"] == "locked" and "5 minutes" in locked.json()["error"]["message"]
        assert here("cam").status_code == 201  # nobody else is affected


def test_compute_believes_only_what_turnstile_signed(fake_llama_server):
    with gateway(fake_llama_server, groups=SIGNING_IN) as caller:
        admin, _, keys = people(caller, pat="plain")
        straight = OnCompute(caller)  # round turnstile, on the network
        signed = lambda key=KEY, **told: statement.make(key, "GET", "/loadcoach/me", b"", statement.caller_of(caller.users.by_name("jordan")), **told)[0]  # noqa: E731
        with_key = straight.get("/loadcoach/me", headers=admin)  # an API key means nothing to compute
        forged = straight.get("/loadcoach/me", headers={statement.HEADER: signed("another key")})
        other_request = straight.get("/loadcoach/status", headers={statement.HEADER: signed()})
        sound = signed()
        first, again = straight.get("/loadcoach/me", headers={statement.HEADER: sound}), straight.get("/loadcoach/me", headers={statement.HEADER: sound})
        through = caller.get("/loadcoach/me", headers=keys["pat"])
        caller.post("/loadcoach/users/pat", json={"enabled": False}, headers=admin)
        revoked = caller.get("/v1/models", headers=keys["pat"])  # turned away at turnstile, with nothing sent on

    assert (with_key.status_code, forged.status_code, other_request.status_code, again.status_code) == (401, 401, 401, 401)
    assert first.status_code == 200 and first.json()["name"] == "jordan" and statement.PROOF in first.headers
    assert first.headers[statement.PROOF] == statement.proof(KEY, json.loads(base64.urlsafe_b64decode(sound.split(".")[0]))["nonce"])
    assert through.json()["name"] == "pat" and statement.PROOF not in through.headers  # the proof is for turnstile, not passed on
    assert revoked.status_code == 401


def test_a_password_somebody_else_set_only_serves_to_choose_ones_own(fake_llama_server, capsys):
    with gateway(fake_llama_server, groups=SIGNING_IN) as caller:
        admin, front, keys = people(caller, pat="plain")
        asked = caller.post("/loadcoach/users/pat", json={"temporary_password": True}, headers=admin)
        temporary = asked.json()["temporary_password"]
        assert caller.post("/loadcoach/users/pat", json={"temporary_password": True}, headers=keys["pat"]).status_code == 403
        listed = lambda: next(user for user in caller.get("/loadcoach/users", headers=admin).json()["users"] if user["name"] == "pat")  # noqa: E731
        assert listed()["password"] == "temporary"

        assert sign_in(caller, front, "pat").status_code == 401  # the old one is gone
        refused = sign_in(caller, front, "pat", temporary)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "password_change"
        assert sign_in(caller, front, "pat", temporary, new_password="short").status_code == 403
        assert sign_in(caller, front, "pat", temporary, new_password=temporary).status_code == 403  # not the same one again
        first = sign_in(caller, front, "pat", temporary, new_password="battery staple")
        assert first.status_code == 201 and listed()["password"] is True
        assert sign_in(caller, front, "pat", temporary).status_code == 401
        assert caller.get("/loadcoach/me", headers=keys["pat"]).status_code == 200  # an API key was never affected

        # Changing one's own password needs the current one, and ends one's other sign-ins but not this one.
        this = {"authorization": "Bearer " + first.json()["key"]}
        other = {"authorization": "Bearer " + sign_in(caller, front, "pat", "battery staple").json()["key"]}
        assert caller.post("/loadcoach/users/pat", json={"password": "another one"}, headers=this).status_code == 403
        change = caller.post("/loadcoach/users/pat", json={"password": "another one", "current_password": "battery staple"}, headers=this)
        assert change.status_code == 200 and "current_password" not in change.json()["changed"]
        assert caller.get("/loadcoach/me", headers=this).status_code == 200 and caller.get("/loadcoach/me", headers=other).status_code == 401

        # A password an admin types in for someone is temporary too.
        caller.post("/loadcoach/users/pat", json={"password": "typed by admin"}, headers=admin)
        assert listed()["password"] == "temporary" and caller.get("/loadcoach/me", headers=this).status_code == 401
        assert "another one" not in json.dumps(events(caller, admin, kind="user")) and "typed by admin" not in json.dumps(events(caller, admin, kind="user"))


def test_a_group_can_ask_for_a_code_from_an_authenticator_app(fake_llama_server):
    with gateway(fake_llama_server, groups=SIGNING_IN) as caller:
        admin, front, _ = people(caller, cleo="careful", pat="plain")
        step = lambda: int(time.time() // 30)  # noqa: E731
        assert sign_in(caller, front, "pat").status_code == 201  # a group that does not ask is untouched

        setup = sign_in(caller, front, "cleo")
        secret = setup.json()["error"]["secret"]
        assert setup.status_code == 403 and setup.json()["error"]["code"] == "second_step_setup"
        assert setup.json()["error"]["uri"] == f"otpauth://totp/LoadCoach:cleo?secret={secret}&issuer=LoadCoach"
        assert sign_in(caller, front, "cleo", code="000000" if code_at(secret, step()) != "000000" else "111111").json()["error"]["code"] == "second_step_setup"
        assert sign_in(caller, front, "cleo").json()["error"]["secret"] == secret  # the same secret until it has been used

        now = step()
        assert sign_in(caller, front, "cleo", code=code_at(secret, now)).status_code == 201  # set up, and signed in
        assert sign_in(caller, front, "cleo").json()["error"]["code"] == "code_required"
        assert sign_in(caller, front, "cleo", code=code_at(secret, now)).json()["error"]["code"] == "code_wrong"  # no code works twice
        assert sign_in(caller, front, "cleo", "wrong horse", code=code_at(secret, now + 1)).status_code == 401  # a code is no use without the password
        assert sign_in(caller, front, "cleo", code=code_at(secret, now + 1)).status_code == 201  # a clock a little ahead is allowed
        listed = lambda: next(user for user in caller.get("/loadcoach/users", headers=admin).json()["users"] if user["name"] == "cleo")  # noqa: E731
        assert listed()["second_step"] is True

        # Being told to choose a password does not use the code up: the same one then finishes the sign-in.
        add_user(caller, "dana", "careful", by=admin)
        temporary = caller.post("/loadcoach/users/dana", json={"temporary_password": True}, headers=admin).json()["temporary_password"]
        hers = sign_in(caller, front, "dana", temporary).json()["error"]["secret"]
        code = code_at(hers, step())
        assert sign_in(caller, front, "dana", temporary, code=code).json()["error"]["code"] == "password_change"
        assert sign_in(caller, front, "dana", temporary, code=code, new_password="dana's own one").status_code == 201

        # A lost phone: an admin has the authenticator forgotten, and it is set up afresh.
        assert caller.post("/loadcoach/users/cleo", json={"second_step": False}, headers=admin).status_code == 200
        again = sign_in(caller, front, "cleo")
        assert listed()["second_step"] is False and again.json()["error"]["code"] == "second_step_setup" and again.json()["error"]["secret"] != secret


def test_a_superuser_can_do_everything_a_person_can_be_allowed_to(fake_llama_server, tmp_path):
    group = read_group("superuser", {"can": ["superuser"]})
    assert group.can == frozenset(ABILITIES) - OWN_KEY and group.max_sensitivity == "restricted"
    assert GROUPS["admin"].can < group.can  # more than an admin who lists every ability by name: the word itself, and whatever comes later
    assert read_group("g", {"can": ["superuser"], "max_sensitivity": "normal"}).max_sensitivity == "normal"  # a limit that is set still holds
    assert "power" not in read_group("g", {"can": ["load"]}).can

    groups = {**GROUPS, "superuser": group}
    with gateway(fake_llama_server, groups=groups) as caller:
        boss = add_user(caller, "sam", "superuser")  # the first account, made while there are none
        admin = add_user(caller, "jordan", "admin", by=boss)
        me = caller.get("/loadcoach/me", headers=boss).json()
        did = [caller.post("/loadcoach/drain", json={"why": "x"}, headers=boss), caller.post("/loadcoach/drain", json={"on": False}, headers=boss),
               caller.get("/loadcoach/flags", headers=boss), caller.get("/loadcoach/users", headers=boss),
               caller.post("/v1/chat/completions", json={"model": "small"}, headers={**boss, "x-loadcoach-sensitivity": "restricted"})]  # fmt: skip
        not_a_persons = caller.get("/loadcoach/plugins/mirror", headers=boss)
    assert {"superuser", "drain", "power", "manage-users", "see-capture"} <= set(me["can"]) and [answer.status_code for answer in did] == [200] * 5
    assert not_a_persons.status_code == 403  # mirror-users is for the front machine's own key, never a person's


# ---- groups in the file, accounts with something of their own, and apps -------------------------

LEVELS_OF = {
    "owner": read_group("owner", {"can": ["superuser"]}),
    "admin": Group(can=frozenset({"load", "lock", "manage-users", "manage-groups"})),
    "strong": Group(can=frozenset({"load", "lock", "power"})),
    "app": Group(can=frozenset({"load", "lock", "hosted"})),
    "plain": Group(can=frozenset({"load"})),
}


def test_groups_are_kept_with_the_users_and_the_file_is_only_the_first_set(tmp_path, capsys):
    file = str(tmp_path / "loadcoach.db")
    users = Users(file, {"reader": Group(can=frozenset({"load"})), "owner": LEVELS_OF["owner"]})
    again = Users(file, {"reader": Group(can=frozenset({"lock"})), "more": Group()})  # another program, or the same one started later
    assert (users.groups.taken_over, again.groups.taken_over) == (2, 0)  # taken over once, into an empty table
    assert list(again.groups) == ["owner", "reader"] and again.groups.get("reader").can == {"load"} and "more" not in again.groups

    users.add("pat", "reader")
    key = users.new_key("pat")
    again.groups.put("reader", {"can": ["load", "lock"], "max_requests": 3})  # changed by the other program
    assert users.by_key(key).can("lock") and users.by_key(key).group.max_requests == 3  # and it counts at the very next look
    again.groups.remove("reader")
    assert not users.by_key(key).group.can and not users.by_key(key).group.may_use("small")  # a group that is gone grants nothing

    for name, wrong in (("reader", {"can": ["fly"]}), ("reader", {"colour": 1}), ("Bad Name", {"can": ["load"]})):
        with pytest.raises(ValueError):
            users.groups.put(name, wrong)
    # A group written out and read back is the same group. "superuser" stays the one word, so it goes on meaning everything.
    for group in (LEVELS_OF["owner"], read_group("g", {"can": ["superuser", "front-wake"], "max_sensitivity": "normal"}), Group(can=frozenset({"load"}), models=("a",), max_lock=5)):
        assert read_group("g", section_of(group)) == group
    assert users.groups.stored("owner")["can"] == ["superuser"]

    # From the command line.
    run = lambda *command: (group_command(users, command[0], list(command[1:])), capsys.readouterr().out)[1]  # noqa: E731
    assert "Added the group reader." in run("add", "reader", "can=load,lock", "max_requests=2", "priority=low")
    assert users.by_key(key).group == Group(can=frozenset({"load", "lock"}), max_requests=2, priority="low")
    assert "max_requests = 2" in run("show", "reader") and '[groups.owner]\ncan = ["superuser"]' in run("export")
    run("set", "reader", "can=load", "max_requests=", "models=small")  # an empty value takes a setting away
    assert users.by_key(key).group == Group(can=frozenset({"load"}), models=("small",), priority="low")
    assert run("set", "reader", "models=*") and users.by_key(key).group.models is None
    assert "reader  1        load" in run("list")
    mistakes = []
    for command in (["add", "reader"], ["set", "ghost", "can=load"], ["set", "reader", "can=fly"], ["set", "reader", "load"], ["remove", "reader"], ["fly"]):
        with pytest.raises(ValueError) as mistake:
            run(*command)
        mistakes.append(str(mistake.value))
    assert ["already a group" in mistakes[0], "no group named" in mistakes[1], "unknown ability" in mistakes[2], "is not KEY=VALUE" in mistakes[3]] == [True] * 4
    assert "1 user is in reader: pat" in mistakes[4] and "weightroom group list" in mistakes[5]
    users.change("pat", group="owner")
    assert "Removed the group reader." in run("remove", "reader") and "reader" not in again.groups


def test_an_account_can_have_more_and_less_than_its_group(fake_llama_server, tmp_path, capsys):
    group = Group(can=frozenset({"load", "lock"}), models=("small", "big"), max_requests=4, max_sensitivity="sensitive")
    own = {"add": ["drain"], "remove": ["lock"], "models": ["small", "other"], "max_requests": 2, "max_sensitivity": "normal"}
    assert adjusted(group, own) == Group(can=frozenset({"load", "drain"}), models=("small",), max_requests=2, max_sensitivity="normal")
    assert adjusted(group, {}) is group
    # A limit can be made tighter, never looser: the group's holds where it is the tighter.
    assert adjusted(group, {"max_requests": 9, "max_sensitivity": "restricted"}) == group
    assert adjusted(Group(), {"max_requests": 3}).max_requests == 3  # no limit in the group: the account's own is the limit
    # Given everything. And with one thing taken away it is no longer everything, so the word goes too.
    everything = adjusted(group, {"add": ["superuser"]})
    assert everything.can == LEVELS_OF["owner"].can and everything.max_sensitivity == "restricted"
    less = adjusted(LEVELS_OF["owner"], {"remove": ["power"]})
    assert less.can == LEVELS_OF["owner"].can - {"power", "superuser"}
    for wrong in ({"add": ["fly"]}, {"colour": 1}, {"remove": "lock"}, {"max_requests": 0}, {"max_sensitivity": "top"}, []):
        with pytest.raises(ValueError):
            check_own(wrong)

    with gateway(fake_llama_server, groups=LEVELS_OF) as caller:
        owner = add_user(caller, "sam", "owner")
        made = caller.post("/loadcoach/users", json={"name": "chat", "group": "app", "own": {"remove": ["lock"], "models": ["small"]}}, headers=owner)
        chat = {"authorization": "Bearer " + made.json()["key"]}
        other = add_user(caller, "notes", "app", by=owner)  # another app in the same group, with nothing of its own
        seen = caller.get("/loadcoach/users/chat", headers=owner).json()
        outcome = [caller.post("/loadcoach/lock", json={"seconds": 5}, headers=chat).status_code, caller.post("/loadcoach/lock", json={"seconds": 5}, headers=other).status_code,
                   [model["id"] for model in caller.get("/v1/models", headers=chat).json()["data"]], caller.post("/v1/chat/completions", json={"model": "big"}, headers=chat).status_code]  # fmt: skip
        caller.post("/v1/chat/completions", json={"model": "small"}, headers=chat)
        usage = caller.get("/loadcoach/users/chat", headers=owner).json()["usage"]
        spans = [[row["days"] for row in caller.get(f"/loadcoach/users/chat?days={days}", headers=owner).json()["usage"]] for days in (3, 0, 5000)]
        why_not = [caller.get(f"/loadcoach/users/{name}", headers=who).json()["why_not"] for name, who in (("chat", owner), ("sam", owner), ("chat", chat))]
        # An account for a person: a password that only serves to choose their own, and no key.
        person = caller.post("/loadcoach/users", json={"name": "ada", "group": "app", "key": False, "temporary_password": True}, headers=owner).json()
        first = caller.get("/loadcoach/users/ada", headers=owner).json()
        mine = caller.get("/loadcoach/users/chat", headers=chat).status_code, caller.get("/loadcoach/users/notes", headers=chat).status_code
        # A priority of its own: its requests go at that priority without asking, whatever its group's is.
        caller.post("/loadcoach/users/chat", json={"own": {**seen["own"], "priority": "high"}}, headers=owner)
        caller.post("/v1/chat/completions", json={"model": "small"}, headers=chat)
        went = caller.get("/loadcoach/events", params={"kind": "request", "what": "done", "user": "chat", "limit": 1}, headers=owner).json()["events"][0]["priority"], caller.get("/loadcoach/me", headers=chat).json()["priority"]
        caller.post("/loadcoach/users/chat", json={"own": seen["own"]}, headers=owner)
        wrong = caller.post("/loadcoach/users/chat", json={"own": {"add": ["fly"]}}, headers=owner)
        caller.post("/loadcoach/users/chat", json={"own": {}}, headers=owner)
        again = caller.post("/loadcoach/lock", json={"seconds": 5}, headers=chat).status_code

    assert made.status_code == 201 and seen["own"] == {"remove": ["lock"], "models": ["small"]} and seen["can"] == ["hosted", "load"]
    assert outcome == [403, 200, ["small"], 404]  # this app may not lock, the other may; and it reaches one model only
    assert [(row["days"], row["requests"]) for row in usage] == [(1, 1), (7, 1), (28, 1)] and mine == (200, 403)  # an account may see itself, and nobody else
    assert spans == [[1, 7, 3], [1, 7, 1], [1, 7, 5000]]  # the last row is as long as is asked for: front keeps the whole log
    # The one asking is told whether a change would be refused. Their own account is closed to everyone but someone who can do everything.
    assert why_not[:2] == [None, None] and "their own group" in why_not[2]
    assert "key" not in person and len(person["temporary_password"]) == 12 and (first["password"], first["keys"]) == ("temporary", [])
    assert wrong.status_code == 400 and again == 200  # with its own settings emptied it is its group again
    assert went == ("high", "high") and adjusted(LEVELS_OF["app"], {"priority": "low"}).priority == "low"
    with pytest.raises(ValueError, match="priority must be"):
        check_own({"priority": "first"})
    # High priority is handed out only by someone who has it: the ability to ask for it, or a group whose own priority it is.
    assert beyond(LEVELS_OF["admin"], adjusted(LEVELS_OF["plain"], {"priority": "high"})) == "high priority" and beyond(LEVELS_OF["owner"], adjusted(LEVELS_OF["plain"], {"priority": "high"})) is None

    # From the command line.
    users = Users(str(tmp_path / "users.db"), LEVELS_OF)
    users.add("chat", "app")
    run = lambda *command: (user_command(users, command[0], list(command[1:])), capsys.readouterr().out)[1]  # noqa: E731
    said = run("adjust", "chat", "remove=lock", "add=app-sign-in,drain", "max_requests=2")
    assert "So it may: app-sign-in, drain, hosted, load" in said and users.own("chat") == {"remove": ["lock"], "add": ["app-sign-in", "drain"], "max_requests": 2}
    run("adjust", "chat", "add=", "remove=")
    assert users.own("chat") == {"max_requests": 2} and "chat is in app." in run("show", "chat") and '{"max_requests": 2}' in run("show", "chat")
    with pytest.raises(ValueError, match="Unknown ability"):
        run("adjust", "chat", "add=fly")


def test_nobody_raises_themselves_or_hands_out_what_they_may_not_do(fake_llama_server):
    with gateway(fake_llama_server, groups=LEVELS_OF) as caller:
        owner = add_user(caller, "sam", "owner")
        admin = add_user(caller, "jordan", "admin", by=owner)
        pat = add_user(caller, "pat", "plain", by=admin)
        change = lambda who, name, **what: caller.post(f"/loadcoach/users/{name}", json=what, headers=who)  # noqa: E731
        group = lambda who, name, new=False, **what: caller.post(f"/loadcoach/groups/{name}" + ("?new" if new else ""), json=what, headers=who)  # noqa: E731

        # 1. Nobody changes their own account. Nobody at all disables or removes it.
        own = [change(admin, "jordan", group="owner"), change(admin, "jordan", own={"add": ["drain"]}), change(admin, "jordan", enabled=False),
               caller.delete("/loadcoach/users/jordan", headers=admin), change(owner, "sam", enabled=False), caller.delete("/loadcoach/users/sam", headers=owner)]  # fmt: skip
        # Someone who can do everything has nothing to raise themselves to: their group and what they have of their own are theirs to change.
        own_owner = [change(owner, "sam", own={"priority": "high"}), change(owner, "sam", group="owner"), caller.get("/loadcoach/users/sam", headers=owner).json()["own"]]
        # 2. Nobody changes the group they are in.
        own_group = [group(admin, "admin", can=["superuser"]), group(owner, "owner", can=["load"]), caller.delete("/loadcoach/groups/admin", headers=admin)]
        # 3. Nobody hands out what they may not do themselves: not to a group, not to an account.
        too_much = [group(admin, "mighty", can=["load", "power"]), group(admin, "plain", can=["load", "drain"]), group(admin, "careful", can=["load"], max_sensitivity="sensitive"),
                    caller.post("/loadcoach/users", json={"name": "x", "group": "strong"}, headers=admin),
                    caller.post("/loadcoach/users", json={"name": "x", "group": "plain", "own": {"add": ["power"]}}, headers=admin),
                    change(admin, "pat", group="strong"), change(admin, "pat", own={"add": ["drain"]}), change(admin, "pat", own={"add": ["superuser"]})]  # fmt: skip
        # And nobody changes an account, or a group, that may do more than they may. A new password for it would be a way to become it.
        out_of_reach = [change(admin, "sam", temporary_password=True), change(admin, "sam", enabled=False), change(admin, "sam", password="a new one of mine"),
                        caller.post("/loadcoach/users/sam/keys", json={"label": "mine now"}, headers=admin), caller.delete("/loadcoach/users/sam", headers=admin),
                        group(admin, "strong", can=["load"]), caller.delete("/loadcoach/groups/strong", headers=admin)]  # fmt: skip

        # What an admin may do: anything up to their own level.
        fine = [group(admin, "readers", new=True, can=["load"], max_requests=2), change(admin, "pat", group="readers"), change(admin, "pat", own={"add": ["lock"], "remove": ["load"]}),
                change(admin, "pat", temporary_password=True), group(admin, "readers", can=["load", "lock"])]  # fmt: skip
        taken = group(admin, "readers", new=True, can=["load"])
        listed = caller.get("/loadcoach/groups", headers=admin).json()
        hidden = caller.get("/loadcoach/groups", headers=pat)
        in_use = caller.delete("/loadcoach/groups/readers", headers=admin)
        change(admin, "pat", group="plain")
        emptied = caller.delete("/loadcoach/groups/readers", headers=admin)
        # Someone who can do everything can hand out anything, the abilities meant for a program's own key included.
        by_owner = [group(owner, "front", can=["sign-in", "mirror-users"]), caller.post("/loadcoach/users", json={"name": "panel", "group": "front"}, headers=owner),
                    change(owner, "jordan", own={"add": ["power"]}), group(owner, "admin", can=["load", "manage-users"])]  # fmt: skip
        kept = [(entry["what"], entry["user"], entry.get("name")) for entry in caller.log.read(0, 100, None, "group")]  # on record at the front machine

    assert [answer.status_code for answer in own_owner[:2]] == [200, 200] and own_owner[2] == {"priority": "high"}
    assert [answer.status_code for answer in own] == [403] * 6 and "on the front machine, with the weightroom command" in own[0].json()["error"]["message"]
    assert [answer.status_code for answer in own_group] == [403, 403, 409]
    assert [answer.status_code for answer in too_much] == [403] * 8
    assert "the ability power, which you may not do yourself" in too_much[0].json()["error"]["message"] and "work at the sensitive level" in too_much[2].json()["error"]["message"]
    assert [answer.status_code for answer in out_of_reach] == [403] * 7 and "sam may do more than you" in out_of_reach[0].json()["error"]["message"]
    assert [answer.status_code for answer in fine] == [201, 200, 200, 200, 200] and taken.status_code == 409
    readers = next(one for one in listed["groups"] if one["name"] == "readers")
    assert (readers["can"], readers["members"]) == (["load", "lock"], ["pat"]) and "manage-groups" in listed["abilities"] and hidden.status_code == 403
    # Every setting of a group is listed, the ones nobody set too, and whether the one asking may change it.
    assert (readers["max_requests"], readers["session_hours"], readers["why_not"]) == (0, 12, None) and listed["own_key"] == ["app-sign-in", "follow-settings", "mirror-users", "sign-in"]
    closed = {one["name"]: one["why_not"] for one in listed["groups"] if one["why_not"]}
    assert sorted(closed) == ["admin", "app", "owner", "strong"] and "the group they are in" in closed["admin"] and "The group strong allows more than you may" in closed["strong"]
    assert in_use.status_code == 409 and "pat" in in_use.json()["error"]["message"] and emptied.status_code == 200
    assert [answer.status_code for answer in by_owner] == [201, 201, 200, 200]
    assert kept == [("taken-over", "", None), ("added", "jordan", "readers"), ("changed", "jordan", "readers"), ("removed", "jordan", "readers"), ("added", "sam", "front"), ("changed", "sam", "admin")]


def test_every_member_of_a_group_can_be_moved_together(fake_llama_server):
    groups = {**GROUPS, "readers": Group(can=frozenset({"load"})), "writers": Group(can=frozenset({"load"}))}
    with gateway(fake_llama_server, groups=groups) as caller:
        admin = add_user(caller, "jordan", "admin")
        add_user(caller, "ada", "readers", by=admin)
        add_user(caller, "pat", "readers", by=admin)

        missing = caller.post("/loadcoach/groups/readers/members", json={"group": "nowhere"}, headers=admin)
        still_there = next(group for group in caller.get("/loadcoach/groups", headers=admin).json()["groups"] if group["name"] == "readers")
        moved = caller.post("/loadcoach/groups/readers/members", json={"group": "writers"}, headers=admin)
        listed = {group["name"]: group["members"] for group in caller.get("/loadcoach/groups", headers=admin).json()["groups"]}
        event = caller.log.read(0, 100, None, "group")[-1]  # on record at the front machine

    assert missing.status_code == 400 and still_there["members"] == ["ada", "pat"]
    assert moved.json() == {"from": "readers", "group": "writers", "moved": ["ada", "pat"]}
    assert listed["readers"] == [] and listed["writers"] == ["ada", "pat"]
    assert (event["what"], event["name"], event["group"], event["members"]) == ("members-moved", "readers", "writers", ["ada", "pat"])


def test_the_latest_requests_are_kept_in_full_and_the_log_is_narrowed_and_paged(fake_llama_server, monkeypatch):
    reply = {"choices": [{"message": {"content": "Four.", "reasoning_content": "Two and two."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}
    groups = {**LEVELS_OF, "reader": Group(can=frozenset({"load", "see-log"}))}  # may read the log, and not the text kept
    with gateway(fake_llama_server, groups=groups, keep_text=2) as caller:
        owner = add_user(caller, "sam", "owner")
        pat, rita = add_user(caller, "pat", "plain", by=owner), add_user(caller, "rita", "reader", by=owner)

        def ask(words, who=pat, **headers):
            sent = {"model": "small", "messages": [{"role": "user", "content": words}], "reply": reply}
            return int(caller.post("/v1/chat/completions", json=sent, headers={**who, **headers}).headers["x-loadcoach-request-id"])

        facts = lambda number, who: caller.get(f"/loadcoach/requests/{number}", headers=who).json()  # noqa: E731
        first, second, third = ask("one"), ask("two"), ask("three")
        mine, gone, hers = facts(third, pat), facts(first, pat), facts(third, rita)
        his, looks = facts(third, owner), [look["user"] for look in facts(third, owner)["looked_at"]]

        # Front's numbers page the log; `source` is each entry's number on compute, which a request is known by.
        events = lambda **narrowed: [(entry["id"], entry["source"]) for entry in caller.get("/loadcoach/events", params=narrowed, headers=owner).json()["events"]]  # noqa: E731
        done = events(kind="request", what="done")
        older, latest = events(kind="request", what="done", before=done[-1][0]), events(model="small", what="asked", limit=2)
        wrong = caller.get("/loadcoach/events?before=soon", headers=owner).status_code

        # Kept whatever the level. Sensitive work is in the log as well, as before; this is beside it.
        secret = facts(ask("four", owner, **{"x-loadcoach-sensitivity": "sensitive"}), owner)
        monkeypatch.setattr("loadcoach.app.KEEP_AT_MOST", 150)
        large = facts(ask("five " * 40), pat)

    assert mine["text"]["asked"]["messages"] == [{"role": "user", "content": "three"}] and mine["text_kept"]
    assert mine["text"]["said"] == {"content": "Four.", "reasoning": "Two and two.", "finish_reason": "stop"}
    assert gone["text"] is None and not gone["text_kept"]  # two are kept, and this was the third from last
    assert hers["text"] is None and hers["text_kept"]  # someone else's is read with see-capture, and she has see-log only
    assert his["text"]["said"]["content"] == "Four." and looks == ["sam"]  # and reading it is on record, once
    assert len(done) == 3 and [one[1] for one in done[1:]] == [second + 1, third + 1] and older == done[:2] and [one[1] for one in latest] == [second, third] and wrong == 400
    assert secret["text"]["asked"]["messages"][0]["content"] == "four" and secret["asked"]["level"] == "sensitive"
    assert large["text"]["asked"] == {"too_big": large["text"]["asked"]["too_big"]} and large["text"]["said"]["content"] == "Four."

    with gateway(fake_llama_server, keep_text=0) as caller:  # none kept
        number = caller.post("/v1/chat/completions", json={"model": "small", "messages": []}).headers["x-loadcoach-request-id"]
        assert caller.get(f"/loadcoach/requests/{number}").json()["text_kept"] is False


def test_a_request_can_leave_nothing_behind_where_that_is_switched_on_and_the_caller_may(fake_llama_server, tmp_path):
    groups = {**LEVELS_OF, "quiet": Group(can=frozenset({"load", "hosted", "unlogged"}), max_sensitivity="sensitive")}
    off, file = {"x-loadcoach-log": "off"}, str(tmp_path / "loadcoach.db")
    counts = lambda: [sqlite3.connect(file).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("events", "texts", "traces")]  # noqa: E731
    with gateway(fake_llama_server, groups=groups, database=file, unlogged=True) as caller:
        owner = add_user(caller, "sam", "owner")
        quinn, pat = add_user(caller, "quinn", "quiet", by=owner), add_user(caller, "pat", "plain", by=owner)
        ask = lambda who, model="small", **more: caller.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "between us"}]}, headers={**who, **more})  # noqa: E731
        usual = ask(quinn)  # an ordinary one first, which also loads the model
        before = counts()
        unseen = ask(quinn, **off)
        # While one runs it is in the line like any other, without a number. That is in memory only.
        slow = threading.Thread(target=lambda: caller.post("/v1/chat/completions", json={"model": "small", "drip": 0.6}, headers={**quinn, **off}))
        slow.start()
        deadline, running = time.monotonic() + 5, []
        while not running and time.monotonic() < deadline:
            running = caller.get("/loadcoach/status", headers=owner).json()["running"]
            time.sleep(0.02)
        slow.join()
        after = counts()
        usage = caller.get("/loadcoach/users/quinn", headers=owner).json()["usage"][0]["requests"]

        # What cannot go with it. None of these was run, and none left an entry.
        cannot = [ask(quinn, **{"x-loadcoach-log": "on"}), ask(quinn, **off, **{"x-loadcoach-trace": "job-1"}), ask(quinn, **off, **{"x-loadcoach-sensitivity": "sensitive"}),
                  ask(quinn, **off, **{"x-loadcoach-offload": "cloud"})]  # fmt: skip
        still = counts()
        # Asking for it without being allowed is turned away, and that is on record.
        denied = ask(pat, **off)
        refused = caller.get("/loadcoach/events?kind=refused&user=pat", headers=owner).json()["events"]

    assert usual.status_code == 200 and "x-loadcoach-request-id" in usual.headers
    assert unseen.status_code == 200 and "x-loadcoach-request-id" not in unseen.headers and "between us" in unseen.json()["body"]  # answered like any other
    assert after == before and before[1] == 1  # not an entry more, and the one text kept is the ordinary request's
    assert [(entry["user"], entry["id"]) for entry in running] == [("quinn", 0)] and usage == 1
    assert [answer.status_code for answer in cannot] == [400] * 4 and still == before
    assert denied.status_code == 403 and len(refused) == 1 and refused[0]["path"] == "/v1/chat/completions"

    with gateway(fake_llama_server, groups=groups) as caller:  # switched off, as it is unless the settings say otherwise
        quinn = add_user(caller, "quinn", "quiet", by=add_user(caller, "sam", "owner"))
        shut = caller.post("/v1/chat/completions", json={"model": "small", "messages": []}, headers={**quinn, **off})
    assert shut.status_code == 403 and "switched off" in shut.json()["error"]["message"]


def settled(caller, ref, who, within=10):
    """A conversation once its answer has come, however long that takes up to `within` seconds."""
    until = time.monotonic() + within
    while time.monotonic() < until:
        talk = caller.get(f"/loadcoach/conversations/{ref}", headers=who).json()
        if not talk["pending"]:
            return talk
        time.sleep(0.02)
    raise AssertionError("the conversation was never answered")






def test_settings_are_changed_from_the_site_within_what_may_be_changed(fake_llama_server, tmp_path, capsys):
    from loadcoach.settings import Changes, said

    file = tmp_path / "loadcoach.toml"
    file.write_text(f'# How many of the latest requests are kept in full.\nkeep_text = 5\n\n# Where llama-server is.\nllama_server = "{fake_llama_server}"\n\ngpus = ["GPU0"]\n')
    config = load_config(file)
    setting = lambda listed, program, name: next(one for one in next(p for p in listed["programs"] if p["program"] == program)["settings"] if one["name"] == name)  # noqa: E731
    with TestClient(build_app(config)) as caller:
        change = lambda program, **asked: caller.post(f"/loadcoach/settings/{program}", json=asked)  # noqa: E731
        first = caller.get("/loadcoach/settings").json()
        changed = change("loadcoach", keep_text=1, unlogged=True).json()
        in_force = config.keep_text, config.unlogged
        refused = [change("loadcoach", llama_server=""), change("loadcoach", keep_text="many"), change("loadcoach", keep_text=-1),
                   change("loadcoach", max_wait=None, keep_text=2.5), change("nothing", keep_text=1), change("loadcoach", gpus=["GPU1"])]  # fmt: skip
        untouched = config.keep_text
        back = change("loadcoach", keep_text=None).json()
        # Another program: LoadCoach keeps what is changed for it and hands it out. The program itself sees to the rest.
        Changes(config.database).seen("spotter", said({"sleep_after": 0, "host": "127.0.0.1"}, {"host": "127.0.0.1"}, {"host": "Where it listens."}))
        theirs = [change("spotter", sleep_after=30).status_code, change("spotter", host="0.0.0.0").status_code, caller.get("/loadcoach/settings/spotter/changes").json()]
        # Saying what a program runs with is for that program's own key, an ability that "everything" leaves out.
        told = caller.post("/loadcoach/settings/spotter/seen", json={"told": {}})
        Changes(config.database).seen("spotter", {"sleep_after": {"value": 0, "says": ""}}, {"sleep_after": "too soon"})
        waiting = setting(caller.get("/loadcoach/settings").json(), "spotter", "sleep_after")
        on_record = [(entry["program"], entry.get("keep_text")) for entry in Log(config.database).read(0, 100, None, "settings")]  # compute alone: its own log, read where it is

    # What LoadCoach runs with, beside what its file says and the file's own comment. What could do harm is shown and not changed.
    kept = setting(first, "loadcoach", "keep_text")
    assert (kept["value"], kept["file"], kept["says"], kept["when"], kept["changed"]) == (5, 5, "How many of the latest requests are kept in full.", "now", None)
    offload = setting(first, "loadcoach", "disable_ram_offload")
    assert offload["label"] == "Disable RAM offload" and offload["choices"] == [["true", "Yes - require VRAM"], ["false", "No - allow host RAM spill"]]
    root = setting(first, "loadcoach", "models_folder")
    assert root["label"] == "Active model cache" and root["when"] == "restart"
    storage = setting(first, "loadcoach", "models_storage_folder")
    assert storage["label"] == "Model storage library" and storage["value"] == "" and storage["when"] == "restart"
    assert setting(first, "loadcoach", "llama_server")["when"] == "restart" and setting(first, "loadcoach", "gpus")["when"] == "file"
    # Taken on at once.
    assert in_force == (1, True) and setting(changed, "loadcoach", "keep_text")["changed"]["value"] == 1
    assert [answer.status_code for answer in refused] == [400, 400, 400, 400, 404, 403] and untouched == 1  # refused whole: nothing of a refused change is kept
    assert "llama_server must name an executable" in refused[0].json()["error"]["message"] and "It is a number, and this is text." in refused[1].json()["error"]["message"]
    assert setting(back, "loadcoach", "keep_text")["value"] == 5 and setting(back, "loadcoach", "keep_text")["changed"] is None  # taken away, the file's value holds again
    assert theirs == [200, 403, {"sleep_after": 30}] and told.status_code == 403 and waiting["refused"] == "too soon" and waiting["changed"]["value"] == 30
    assert on_record[0] == ("loadcoach", 1) and ("spotter", None) in on_record

    # At the next start what was changed lies over the file. A change that cannot be used does not keep LoadCoach from starting.
    again = load_config(file)
    assert (again.keep_text, again.unlogged, again.gpus, again.file["keep_text"]) == (5, True, ("GPU0",), 5)
    Changes(again.database).put("loadcoach", "keep_text", "many", "someone")
    assert load_config(file).keep_text == 5 and "cannot be used" in capsys.readouterr().err
    # On the machine itself: what was changed is listed, and one change is taken away.
    from loadcoach.cli import settings_command

    settings_command(Changes(again.database))
    settings_command(Changes(again.database), "reset", "loadcoach", "keep_text")
    printed = capsys.readouterr().out
    assert 'keep_text = "many"   changed by someone' in printed and "unlogged = true" in printed and "keep_text is as its file says again" in printed
    with pytest.raises(ValueError, match="Nothing was changed"):
        settings_command(Changes(again.database), "reset", "loadcoach", "keep_text")


def test_an_app_signs_someone_in_and_the_session_can_do_no_more_than_the_app(fake_llama_server):
    with gateway(fake_llama_server, groups=LEVELS_OF) as caller:
        owner = add_user(caller, "sam", "owner")
        caller.post("/loadcoach/users/sam", json={"password": "correct horse"}, headers=owner)
        made = caller.post("/loadcoach/users", json={"name": "chat", "group": "app", "own": {"add": ["app-sign-in"], "remove": ["lock"], "models": ["small"]}}, headers=owner)
        chat = {"authorization": "Bearer " + made.json()["key"]}
        notes = add_user(caller, "notes", "app", by=owner)  # an app that was not given the ability to sign people in
        sign_in = lambda who, password="correct horse": caller.post("/loadcoach/sign-in", json={"name": "sam", "password": password}, headers=who)  # noqa: E731

        refused = [sign_in({}).status_code, sign_in(notes).status_code, sign_in(chat, "wrong horse").status_code]
        signed = sign_in(chat)
        session = {"authorization": "Bearer " + signed.json()["key"]}
        me = caller.get("/loadcoach/me", headers=session).json()
        # Sam can do everything. Through this app, only what the app itself may: no lock, no power, one model.
        tried = [caller.post("/loadcoach/lock", json={"seconds": 5}, headers=session).status_code, caller.post("/loadcoach/drain", json={}, headers=session).status_code,
                 caller.post("/v1/chat/completions", json={"model": "small"}, headers=session).status_code, caller.post("/v1/chat/completions", json={"model": "big"}, headers=session).status_code,
                 sign_in(session).status_code]  # fmt: skip
        with_own_key = caller.post("/loadcoach/drain", json={}, headers=owner).status_code  # the same person, not through the app
        caller.post("/loadcoach/drain", json={"on": False}, headers=owner)
        # The app is given the lock back, and the session has it at once. The app is disabled, and the session ends with it.
        caller.post("/loadcoach/users/chat", json={"own": {"add": ["app-sign-in"]}}, headers=owner)
        widened = caller.post("/loadcoach/lock", json={"seconds": 5}, headers=session).status_code
        caller.post("/loadcoach/users/chat", json={"enabled": False}, headers=owner)
        ended = caller.get("/loadcoach/me", headers=session).status_code
        logged = [entry for entry in caller.log.read(0, 100, None, "user") if entry["what"] == "signed-in"]

    assert refused == [403, 403, 401]
    assert signed.status_code == 201 and signed.json()["can"] == ["hosted", "load"] and (me["name"], me["via"], me["models"]) == ("sam", "chat", ["small"])
    assert tried == [403, 403, 200, 404, 403] and with_own_key == 200
    assert (widened, ended) == (200, 401)
    assert [(entry["user"], entry["via"]) for entry in logged] == [("sam", "chat")]


def test_group_settings_for_signing_in_and_an_ability_that_was_retired(tmp_path, capsys):
    group = read_group("g", {"can": ["act-for", "lock"], "session_hours": 0.5, "second_step": True})
    assert group.can == frozenset({"lock"}) and (group.session_hours, group.second_step) == (0.5, True)  # an old file still loads
    assert read_group("g", {}).session_hours == 12 and read_group("g", {}).second_step is False
    for wrong in ({"session_hours": 0}, {"session_hours": "long"}, {"second_step": "yes"}):
        with pytest.raises(ValueError):
            read_group("g", wrong)

    users, log = Users(str(tmp_path / "users.db"), SIGNING_IN), Log(str(tmp_path / "users.db"))
    users.add("pat", "careful")
    user_command(users, "temporary", ["pat"], log)
    temporary = capsys.readouterr().out.strip().splitlines()[-1]
    assert check_password(users.password_hash("pat"), temporary) and users.listing()[0]["password"] == "temporary"
    users.new_key("pat", "session", hours=1)
    user_command(users, "list", [])
    shown = capsys.readouterr().out
    assert "SIGNED IN" in shown and "temporary" in shown and "first" not in shown  # no key was made; one sign-in is counted
    user_command(users, "forget-code", ["pat"], log)
    assert [event["what"] for event in log.read(kind="user")] == ["changed", "changed"]


# ---- what model files say about themselves ------------------------------------------------------


def gguf(path, **told):
    """Write a small file laid out as a .gguf begins: its facts, and no weights."""

    def text(value):
        return struct.pack("<Q", len(value.encode())) + value.encode()

    def fact(name, value):
        if isinstance(value, str):
            return text(name) + struct.pack("<I", 8) + text(value)
        if isinstance(value, list):  # a list of text, as a vocabulary is
            return text(name) + struct.pack("<IIQ", 9, 8, len(value)) + b"".join(text(item) for item in value)
        return text(name) + struct.pack("<IQ", 10, value)  # a whole number

    body = b"".join(fact(name.replace("__", "."), value) for name, value in told.items())
    Path(path).write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(told)) + body)
    return str(path)


def test_a_model_file_is_read_for_what_it_says_about_itself(tmp_path):
    template = "{% if tools %}...{% endif %}{% if enable_thinking %}<think>{% endif %}"
    file = gguf(tmp_path / "m.gguf", general__name="Small One", general__architecture="tiny", general__size_label="1B",
                tiny__context_length=8192, tiny__block_count=12, tokenizer__chat_template=template,
                tokenizer__ggml__tokens=[f"t{n}" for n in range(500)], tokenizer__ggml__merges=["a b", "c d"])  # fmt: skip
    metadata = read_metadata(file)
    assert metadata["general.name"] == "Small One" and metadata["tiny.context_length"] == 8192
    assert metadata["tokenizer.ggml.tokens"] == "500 values" and metadata["tokenizer.ggml.merges"] == ["a b", "c d"]  # a long list is counted
    assert facts(metadata) == {"name": "Small One", "what": "model", "architecture": "tiny", "size_label": "1B", "trained_context": 8192, "layers": 12,
                               "chat_template": True, "takes_tools": True, "takes_images": False, "thinking_switch": True}  # fmt: skip
    (tmp_path / "not.gguf").write_text("this is not a model")
    (tmp_path / "cut.gguf").write_bytes(Path(file).read_bytes()[:60])
    for wrong in ("not.gguf", "cut.gguf"):
        with pytest.raises(ValueError):
            read_metadata(str(tmp_path / wrong))


def test_gguf_vocabulary_blocks_preserve_metadata_across_boundaries(tmp_path):
    tokens = ['x' * (65536 - 12), '漢字' * 40000, '', *['token'] * 10000]
    file = gguf(tmp_path / 'large-vocabulary.gguf', tokenizer__ggml__tokens=tokens,
                general__name='After vocabulary', tiny__context_length=8192)
    metadata = read_metadata(file)
    assert metadata['tokenizer.ggml.tokens'] == f'{len(tokens)} values'
    assert metadata['general.name'] == 'After vocabulary' and metadata['tiny.context_length'] == 8192
    # An incomplete length field must still fail after crossing a block boundary.
    damaged = Path(file).read_bytes()
    next_field = damaged.index(b'general.name') - 8
    Path(file).write_bytes(damaged[:next_field - 8])
    with pytest.raises(ValueError, match='middle of its own description'):
        read_metadata(file)


def test_folders_are_looked_through_for_model_files_not_added_yet(fake_llama_server, tmp_path):
    used = gguf(tmp_path / "used.gguf", general__name="Used", general__architecture="tiny", tiny__context_length=4096)
    (tmp_path / "sub").mkdir()
    new = gguf(tmp_path / "sub" / "new.gguf", general__name="Fresh Download", general__architecture="tiny", tiny__context_length=32768)
    (tmp_path / "broken.gguf").write_text("not a model")
    with gateway(fake_llama_server, {"mine": model(used, 1)}) as caller:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        assert caller.get("/loadcoach/files", headers=pat).status_code == 403
        found = caller.get("/loadcoach/files", headers=admin).json()
        assert [(place["name"], place["folder"]) for place in found["places"]] == [(str(tmp_path), str(tmp_path))]  # none configured: where the models are
        assert found["places"][0]["free"] > 0 and found["places"][0]["files"] == sum(file["size"] for file in found["files"])
        files = {Path(file["path"]).name: file for file in found["files"]}
        assert files["used.gguf"]["used_by"] == ["mine"] and files["new.gguf"]["used_by"] == []
        assert files["used.gguf"]["place"] == str(tmp_path) and files["used.gguf"]["last_used"] is None and files["used.gguf"]["moving"] is None
        assert files["new.gguf"]["facts"]["name"] == "Fresh Download" and files["new.gguf"]["facts"]["trained_context"] == 32768
        assert "not a .gguf file" in files["broken.gguf"]["unreadable"]

        one = caller.get("/loadcoach/file", params={"path": new}, headers=admin).json()
        assert one["metadata"]["general.name"] == "Fresh Download" and one["facts"]["architecture"] == "tiny" and one["size"] > 0
        assert caller.get("/loadcoach/file", params={"path": "/etc/passwd"}, headers=admin).status_code == 404  # not any file at all
        assert caller.get("/loadcoach/file", params={"path": str(tmp_path / "broken.gguf")}, headers=admin).status_code == 422
        assert caller.get("/loadcoach/file", params={"path": new}, headers=pat).status_code == 403


def test_a_file_is_moved_to_another_place_and_its_models_follow_without_anyone_being_cut_off(fake_llama_server, tmp_path):
    fast, slow = tmp_path / "fast", tmp_path / "slow"
    fast.mkdir()
    file = gguf(fast / "m.gguf", general__name="Mover", general__architecture="tiny", tiny__context_length=4096)
    spare = gguf(fast / "spare.gguf", general__name="Spare")
    places = {"fast": str(fast), "slow": str(slow)}  # `slow` does not exist yet: a place that is named is made
    with gateway(fake_llama_server, {"mine": model(file, 1), "twin": model(file, 1)}, places=places) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        move = lambda path, to, who=admin: caller.post("/loadcoach/files/move", json={"path": path, "to": to}, headers=who)  # noqa: E731
        listed = lambda: {Path(entry["path"]).name: entry for entry in caller.get("/loadcoach/files", headers=admin).json()["files"]}  # noqa: E731
        assert slow.is_dir() and listed()["m.gguf"]["place"] == "fast"

        assert move(file, "slow", pat).status_code == 403
        assert move(file, "nowhere").status_code == 400 and move(file, "fast").status_code == 409  # no such place; already there
        assert move("/etc/passwd", "slow").status_code == 404 and move(str(fast / "none.gguf"), "slow").status_code == 404

        # Someone is being answered by the model while its file is moved.
        busy = others.submit(caller.post, "/v1/chat/completions", json={"model": "mine", "sleep": 1.0}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda s: s["running"])
        assert move(file, "slow").status_code == 202
        deadline = time.monotonic() + 5
        while "m.gguf" not in {name for name, entry in listed().items() if entry["place"] == "slow"} and time.monotonic() < deadline:
            time.sleep(0.05)
        there = str(slow / "m.gguf")
        assert listed()["m.gguf"]["path"] == there and not Path(file).exists() and listed()["m.gguf"]["used_by"] == ["mine", "twin"]
        kept = {entry["name"]: entry for entry in caller.get("/loadcoach/models", headers=admin).json()["models"]}
        assert kept["mine"]["flags"]["model"] == there and kept["twin"]["flags"]["model"] == there  # every model that used it follows
        assert busy.result().status_code == 200  # and the answer under way was not disturbed
        after = caller.post("/v1/chat/completions", json={"model": "mine"}, headers=admin).json()
        assert f"--model {there}" in " ".join(after["arguments"])  # the next request loads it from where it is now

        assert move(there, "fast").status_code == 202  # and back again
        while "fast" != listed().get("m.gguf", {}).get("place") and time.monotonic() < deadline + 5:
            time.sleep(0.05)
        whats = [event["what"] for event in events(caller, admin, kind="file")]
        assert whats == ["moving", "moved", "moving", "moved"] and Path(file).exists()
        assert Path(spare).exists()  # a file nobody asked about is where it was


def test_a_file_can_be_copied_from_storage_to_cache_without_removing_the_library_copy(fake_llama_server, tmp_path):
    cache, storage = tmp_path / "cache", tmp_path / "storage"
    cache.mkdir()
    storage.mkdir()
    source = gguf(storage / "m.gguf", general__name="Stored", general__architecture="tiny", tiny__context_length=4096)
    with gateway(fake_llama_server, {}, places={"llm": str(cache), "storage_llm": str(storage)}) as caller:
        admin = add_user(caller, "jordan", "admin")
        copied = caller.post("/loadcoach/files/copy", json={"path": source, "to": "llm"}, headers=admin)
        assert copied.status_code == 202
        target = cache / "m.gguf"
        deadline = time.monotonic() + 5
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Path(source).exists() and target.read_bytes() == Path(source).read_bytes()
        listed = caller.get("/loadcoach/files", headers=admin).json()
        by_path = {entry["path"]: entry for entry in listed["files"]}
        assert by_path[source]["tier"] == "storage" and by_path[str(target)]["tier"] == "cache"


def test_clear_cache_preserves_storage_and_removes_only_unused_cache_files(fake_llama_server, tmp_path):
    cache, storage = tmp_path / "cache", tmp_path / "storage"
    cache.mkdir()
    storage.mkdir()
    used = gguf(cache / "used.gguf", general__name="Used")
    spare = gguf(cache / "spare.gguf", general__name="Spare")
    already = gguf(cache / "already.gguf", general__name="Already")
    (storage / "already.gguf").write_bytes(Path(already).read_bytes())
    places = {"llm": str(cache), "storage_llm": str(storage)}
    with gateway(fake_llama_server, {"mine": model(used, 1)}, places=places) as caller:
        admin = add_user(caller, "jordan", "admin")
        refused = caller.post("/loadcoach/files/cleanup", json={"mode": "clear-cache", "keep": ["/etc/hostname"]}, headers=admin)
        assert refused.status_code == 400 and "inside an active cache place" in refused.json()["error"]["message"]
        started = caller.post("/loadcoach/files/cleanup", json={"mode": "clear-cache", "keep": [spare]}, headers=admin)
        assert started.status_code == 202 and started.json()["state"] == "running"
        deadline = time.monotonic() + 5
        while (result := caller.get("/loadcoach/files", headers=admin).json()["cleanup"])["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result["saved"] == 2 and result["removed"] == 1 and result["freed"] > 0 and result["protected"] == 1 and result["failures"] == []
        assert Path(used).exists() and (storage / "used.gguf").exists()
        assert Path(spare).exists() and (storage / "spare.gguf").exists()
        assert not Path(already).exists() and (storage / "already.gguf").exists()


def test_consolidate_promotes_storage_files_into_cache_and_the_status_expires(fake_llama_server, tmp_path, monkeypatch):
    cache, storage = tmp_path / "cache", tmp_path / "storage"
    cache.mkdir()
    storage.mkdir()
    stored = gguf(storage / "stored.gguf", general__name="Stored")
    with gateway(fake_llama_server, {}, places={"llm": str(cache), "storage_llm": str(storage)}) as caller:
        admin = add_user(caller, "jordan", "admin")
        refused = caller.post("/loadcoach/files/cleanup", json={"mode": "promote", "promote": ["/etc/hostname"]}, headers=admin)
        assert refused.status_code == 400 and "inside a storage place" in refused.json()["error"]["message"]
        assert caller.post("/loadcoach/files/cleanup", json={"mode": "promote", "promote": [stored]}, headers=admin).status_code == 202
        deadline = time.monotonic() + 5
        while (result := caller.get("/loadcoach/files", headers=admin).json()["cleanup"])["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result["promoted"] == 1 and result["failures"] == []
        assert (cache / "stored.gguf").read_bytes() == Path(stored).read_bytes() and Path(stored).exists()
        later = time.time() + 601
        monkeypatch.setattr(time, "time", lambda: later)
        assert caller.get("/loadcoach/files", headers=admin).json()["cleanup"] == {}


def test_node_cleanup_removes_unused_cache_without_requiring_storage(fake_llama_server, tmp_path):
    used = gguf(tmp_path / "used.gguf", general__name="Used")
    spare = gguf(tmp_path / "spare.gguf", general__name="Spare")
    with gateway(fake_llama_server, {"mine": model(used, 1)}, places={"llm": str(tmp_path)}) as caller:
        admin = add_user(caller, "jordan", "admin")
        assert caller.post("/loadcoach/files/cleanup", json={"mode": "unused"}, headers=admin).status_code == 202
        deadline = time.monotonic() + 5
        while (result := caller.get("/loadcoach/files", headers=admin).json()["cleanup"])["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result["removed"] == 1 and Path(used).exists() and not Path(spare).exists()


def test_a_file_is_deleted_only_with_its_name_typed_and_only_if_no_model_uses_it(fake_llama_server, tmp_path):
    used = gguf(tmp_path / "used.gguf", general__name="Used")
    spare = gguf(tmp_path / "spare.gguf", general__name="Spare")
    with gateway(fake_llama_server, {"mine": model(used, 1)}) as caller:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        delete = lambda path, name, who=admin: caller.post("/loadcoach/files/delete", json={"path": path, "name": name}, headers=who)  # noqa: E731

        assert delete(spare, "spare.gguf", pat).status_code == 403
        wrong = delete(spare, "spare")
        assert wrong.status_code == 400 and "give its file name exactly: spare.gguf" in wrong.json()["error"]["message"]
        in_use = delete(used, "used.gguf")
        assert in_use.status_code == 409 and "used by mine. Remove that model first." in in_use.json()["error"]["message"]
        assert delete("/etc/hostname", "hostname").status_code == 404  # never a file outside the places
        assert Path(spare).exists() and Path(used).exists()

        size = Path(spare).stat().st_size
        assert delete(spare, "spare.gguf").json() == {"deleted": spare, "freed": size} and not Path(spare).exists()
        gone = events(caller, admin, kind="file")[-1]
        assert (gone["what"], gone["user"], gone["path"], gone["size"]) == ("deleted", "jordan", spare, size)


def test_carrying_a_file_to_another_drive_copies_it_whole_and_leaves_nothing_half_done(tmp_path, monkeypatch):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir(), there.mkdir()
    source = here / "m.gguf"
    source.write_bytes(os.urandom(50_000_000))
    os.utime(source, (1_700_000_000, 1_700_000_000))

    # Both folders are on one drive in a test. Say they are not, so that the file is really copied.
    monkeypatch.setattr(files, "same_drive", lambda file, folder: False)
    progress = {"done": 0, "of": source.stat().st_size}
    carry(source, there / "m.gguf", progress)

    assert progress["done"] == 50_000_000 and (there / "m.gguf").read_bytes() == source.read_bytes()
    assert (there / "m.gguf").stat().st_mtime == 1_700_000_000 and source.exists()  # the same date; the original is not touched
    assert [file.name for file in there.iterdir()] == ["m.gguf"]  # no half-made file is left beside it

    # A copy that cannot be finished leaves nothing behind.
    def fails(*_, **__):
        raise OSError("the drive gave up")

    with monkeypatch.context() as patch:
        patch.setattr(files.shutil, "copystat", fails)
        with pytest.raises(OSError):
            carry(source, there / "second.gguf", {"done": 0, "of": 1})
    assert [file.name for file in there.iterdir()] == ["m.gguf"] and source.exists()


# ---- Hugging Face: finding model files and bringing them down ------------------------------------


def downloads(caller, who, until=lambda listed: not listed):
    """Poll the list of downloads until `until(list)` is true, and return that list."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        listed = caller.get("/loadcoach/downloads", headers=who).json()["downloads"]
        if until(listed):
            return listed
        time.sleep(0.02)
    raise AssertionError(f"the downloads never matched: {listed}")


def test_model_files_are_found_at_hugging_face_brought_down_and_gone_on_with(fake_llama_server, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("loadcoach.hub.PIECE", 64)  # small pieces, so that small files come in several
    fast, stored, tiny, one, two = tmp_path / "fast", tmp_path / "stored", os.urandom(1000), os.urandom(700), os.urandom(500)
    there = FakeHub({"tiny-q4.gguf": tiny, "parts/big-00001-of-00002.gguf": one, "parts/big-00002-of-00002.gguf": two})
    folder = stored / "acme--tiny-GGUF"  # a repository's files go into a folder named after it, one below the storage place
    with gateway(fake_llama_server, places={"fast": str(fast), "storage_llm": str(stored)}, web=there.client()) as caller:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        look = lambda who=admin, **what: caller.get("/loadcoach/hub", params=what, headers=who)  # noqa: E731
        start = lambda *files, who=admin, **more: caller.post("/loadcoach/downloads", json={"repository": there.repository, "files": list(files), **more}, headers=who)  # noqa: E731
        closed = [look(pat, search="tiny").status_code, start("tiny-q4.gguf", who=pat).status_code, caller.get("/loadcoach/downloads", headers=pat).status_code]

        found, nothing = look(search="TINY").json()["found"], look(search="llama").json()["found"]
        seen = look(repository=there.repository).json()
        wrong = [look(repository="no slash").status_code, look(repository="acme/other").status_code, start("none.gguf").status_code, start("tiny-q4.gguf", place="nowhere").status_code]
        cache = start("tiny-q4.gguf", place="fast")  # the cache is never a download's destination

        # One part of a split model brings both. The place need not be named where there is only one storage place.
        started = start("big-00002-of-00002.gguf", "tiny-q4.gguf")
        downloads(caller, admin)
        kept = {file.name: file.read_bytes() for file in folder.iterdir()}
        after = look(repository=there.repository).json()["files"]
        listed = {Path(entry["path"]).name: entry["place"] for entry in caller.get("/loadcoach/files", headers=admin).json()["files"]}
        again = start("tiny-q4.gguf")

        # One left half done is gone on with from where it stopped, not from the start.
        (folder / "tiny-q4.gguf").rename(folder / "tiny-q4.gguf.part")
        with open(folder / "tiny-q4.gguf.part", "r+b") as part:
            part.truncate(300)
        left = downloads(caller, admin, bool)
        resumed = start("tiny-q4.gguf")  # by its name alone, which is all a `.part` file says
        downloads(caller, admin)
        whats = [event["what"] for event in events(caller, admin, kind="file")]

        caller.headers.update(admin)  # the command line is signed in as itself; here, as jordan
        server_command(caller, "search", "tiny")
        server_command(caller, "download", there.repository)
        server_command(caller, "downloads")
        printed = capsys.readouterr().out

    assert closed == [403, 403, 403] and wrong == [400, 403, 404, 400]
    assert cache.status_code == 400 and "never the cache" in cache.json()["error"]["message"] and not (fast / "acme--tiny-GGUF").exists()
    assert found == [{"repository": "acme/tiny-GGUF", "downloads": 5, "likes": 2, "made": "2026-01-02"}] and nothing == []
    # Smallest first. The two parts are one row. Nothing but model files is listed.
    assert [(row["file"], row["parts"], row["size"], row["here"]) for row in seen["files"]] == [("tiny-q4.gguf", 1, 1000, None), ("parts/big-00001-of-00002.gguf", 2, 1200, None)]
    assert [place["name"] for place in seen["places"]] == ["storage_llm"] and seen["places"][0]["free"] > 0  # storage alone is offered
    assert started.status_code == 202 and started.json()["to"] == "storage_llm" and started.json()["size"] == 2200
    assert kept == {"tiny-q4.gguf": tiny, "big-00001-of-00002.gguf": one, "big-00002-of-00002.gguf": two}  # whole, and no `.part` left
    assert [row["here"] for row in after] == ["storage_llm", "storage_llm"] and listed["tiny-q4.gguf"] == "storage_llm" and again.status_code == 409
    assert [(entry["file"], entry["repository"], entry["state"], entry["done"]) for entry in left] == [("tiny-q4.gguf", "acme/tiny-GGUF", "stopped", 300)]
    assert resumed.status_code == 202 and (folder / "tiny-q4.gguf").read_bytes() == tiny and there.asked_of("cdn.test")[-1]["range"] == "bytes=300-"
    assert whats == ["downloading"] * 3 + ["downloaded"] * 3 + ["downloading", "downloaded"]
    assert "acme/tiny-GGUF" in printed and "big-00001-of-00002.gguf" in printed and "storage_llm (" in printed and "fast (" not in printed


def test_with_no_storage_place_nothing_is_downloaded_into_the_cache(fake_llama_server, tmp_path):
    there, cache = FakeHub({"tiny-q4.gguf": os.urandom(1000)}), tmp_path / "cache"
    cache.mkdir()
    with gateway(fake_llama_server, places={"llm": str(cache)}, web=there.client()) as caller:
        admin = add_user(caller, "jordan", "admin")
        seen = caller.get("/loadcoach/hub", params={"repository": there.repository}, headers=admin).json()
        refused = [caller.post("/loadcoach/downloads", json={"repository": there.repository, "files": ["tiny-q4.gguf"], **chosen}, headers=admin)
                   for chosen in ({}, {"place": "llm"})]
    assert seen["files"] and seen["places"] == []  # the files are listed, with nowhere to put them
    assert [answer.status_code for answer in refused] == [409, 409]
    assert all("never the cache" in answer.json()["error"]["message"] for answer in refused) and not any(cache.iterdir())


def test_a_download_that_goes_wrong_says_why_and_one_is_stopped_and_thrown_away(fake_llama_server, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("loadcoach.hub.PIECE", 64)
    fast, tiny, token = tmp_path / "fast", os.urandom(1000), tmp_path / "hub.token"
    there, target = FakeHub({"tiny-q4.gguf": tiny, "huge.gguf": b"small really"}), fast / "acme--tiny-GGUF" / "tiny-q4.gguf"
    part = target.with_name("tiny-q4.gguf.part")
    with gateway(fake_llama_server, places={"storage_llm": str(fast)}, hub_token_file=str(token), web=there.client()) as caller:
        admin = add_user(caller, "jordan", "admin")
        post = lambda what, **body: caller.post("/loadcoach/downloads" + what, json=body, headers=admin)  # noqa: E731
        start = lambda name="tiny-q4.gguf": post("", repository=there.repository, files=[name])  # noqa: E731
        failed = lambda listed: listed and listed[0]["state"] == "failed"  # noqa: E731

        # A repository that is not open to all: refused with no token, and brought down with one. The token goes to Hugging Face and no further.
        there.closed = True
        start()
        no_token = downloads(caller, admin, failed)[0]["why"]
        token.write_text("good\n")  # read when it is needed: no restart
        start()  # the failed one is tried again
        downloads(caller, admin)
        with_token = target.read_bytes()
        told = [headers.get("authorization") for headers in there.asked_of("huggingface.co")], [headers.get("authorization") for headers in there.asked_of("cdn.test")]
        target.unlink()

        # What came down is checked against the SHA-256 Hugging Face names. A file that differs is thrown away.
        there.lies.add("tiny-q4.gguf")
        start()
        differs = downloads(caller, admin, failed)[0]["why"], part.exists(), target.exists()
        discarded = post("/discard", path=str(target)).status_code, caller.get("/loadcoach/downloads", headers=admin).json()["downloads"], target.parent.exists()
        there.lies.clear()

        # A download is stopped while it comes. What came stays, until it is thrown away. While it comes the machine counts as in use.
        there.slow = 0.02
        start()
        coming = downloads(caller, admin, lambda listed: listed and listed[0]["done"] > 0)[0]
        status = caller.get("/loadcoach/status", headers=admin).json()
        twice = start().status_code, post("/discard", path=str(target)).status_code
        caller.headers.update(admin)
        server_command(caller, "downloads", "stop", str(target))
        stopped = 200
        left = downloads(caller, admin, lambda listed: listed and listed[0]["state"] == "stopped")[0]
        not_coming = post("/stop", path=str(target)).status_code, caller.get("/loadcoach/status", headers=admin).json()["downloads"]
        server_command(caller, "downloads", "resume", str(target))
        downloads(caller, admin, lambda listed: listed and listed[0]["done"] > left["done"])
        server_command(caller, "downloads", "stop", str(target))
        left_again = downloads(caller, admin, lambda listed: listed and listed[0]["state"] == "stopped")[0]
        server_command(caller, "downloads", "discard", str(target))
        gone = left_again["done"], part.exists(), post("/discard", path=str(target)).status_code
        command_output = capsys.readouterr().out

        # A file that would not leave 1 GB free is not begun.
        there.claims["huge.gguf"] = 10**15
        too_big = start("huge.gguf")
        whats = [event["what"] for event in events(caller, admin, kind="file")]

    assert "has no token" in no_token and with_token == tiny
    assert told == ([None, "Bearer good"], [None])  # the machine that holds the file was never shown the token
    assert "SHA-256 differs" in differs[0] and differs[1:] == (False, False) and discarded == (200, [], False)
    assert (coming["state"], coming["of"]) == ("coming", 1000) and coming["speed"] > 0 and (status["downloads"], status["busy"]) == (1, 0)
    assert twice == (409, 409) and stopped == 200 and 0 < left["done"] < 1000 and not_coming == (404, 0)
    assert gone[0] >= left["done"] and gone[1:] == (False, 404)
    assert f"Stopped {target}." in command_output and f"Continuing {target}." in command_output and f"Discarded {target}." in command_output
    assert too_big.status_code == 409 and "1 GB must be left over" in too_big.json()["error"]["message"]
    assert whats == ["downloading", "download-failed", "downloading", "downloaded", "downloading", "download-failed", "download-discarded",
                     "downloading", "download-stopped", "downloading", "download-stopped", "download-discarded"]  # fmt: skip


# ---- cards as room: a measured model shares its card ---------------------------------------------


def sized(name, mib, gpus=1, **more):
    """A model whose server uses `mib` MiB of each card it is on, as the stand-in for nvidia-smi then says."""
    return {**model(f"{name}.gguf", gpus, mib=mib), **more}


def test_a_model_has_a_card_to_itself_until_it_is_measured_and_then_shares_one(fake_llama_server, fake_cards):
    models = {"a": sized("a", 3000), "b": sized("b", 4000), "c": sized("c", 5000)}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0]) as caller:
        a = ask(caller, "a").json()
        b = ask(caller, "b").json()  # an empty card comes first: one each
        first = caller.get("/loadcoach/status").json()
        c = ask(caller, "c").json()  # never measured, so it needs a card to itself. `a`, unused the longest, makes way
        a_went = not alive(a["pid"])
        a_again = ask(caller, "a").json()  # measured now. It fits on either card and goes where there is more room: beside b
        status = caller.get("/loadcoach/status").json()
        nobody_went = alive(b["pid"]) and alive(c["pid"])
        measured = {one["name"]: one["measured"] and one["measured"]["mib"] for one in caller.get("/loadcoach/models").json()["models"]}
        loads = [(entry["model"], entry["cards"], entry["replaced"], entry["mib"]) for entry in events(caller, kind="model") if entry["what"] == "loaded"]
        # A measure belongs to the settings it was taken under. Change one, and the model is measured afresh.
        caller.post("/loadcoach/models/b", json={**models["b"], "flags": {**models["b"]["flags"], "ctx-size": 4096}})
        changed = caller.get("/loadcoach/models").json()["models"][1]["measured"]

    assert (device(a), device(b), device(c), device(a_again)) == ("GPU0", "GPU1", "GPU0", "GPU1") and a_went and nobody_went
    assert scheduling_fields(first["loaded"]) == [{"model": "a", "gpus": ["GPU0"], "state": "ready", "locked": False, "answers": [], "mib": [3000]}, {"model": "b", "gpus": ["GPU1"], "state": "ready", "locked": False, "answers": [], "mib": [4000]}]
    # Room for one more model: the card, less the margin of 1024, less what the models on it measured.
    assert first["memory"] == {"GPU0": {"total": 16000, "others": 0, "free": 11976}, "GPU1": {"total": 16000, "others": 0, "free": 10976}}
    assert status["memory"]["GPU0"]["free"] == 9976 and status["memory"]["GPU1"]["free"] == 7976
    assert measured == {"a": [3000], "b": [4000], "c": [5000]} and changed is None
    assert loads == [("a", ["GPU0"], [], [3000]), ("b", ["GPU1"], [], [4000]), ("c", ["GPU0"], ["a"], [5000]), ("a", ["GPU1"], [], [3000])]


def test_small_models_fill_what_a_model_on_both_cards_leaves_and_one_set_alone_shares_with_nobody(fake_llama_server, fake_cards):
    models = {"big": sized("big", 12000, 2), "small": sized("small", 2000), "x": sized("x", 2000), "solo": sized("solo", 1000, alone=True)}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0]) as caller:
        small = ask(caller, "small").json()
        big = ask(caller, "big").json()  # a model on both cards loads onto empty cards only: small, idle, makes way
        small_went = not alive(small["pid"])
        beside = ask(caller, "small").json()  # and comes back into what big left: 16000 - 1024 - 12000 is room for its 2000
        status = caller.get("/loadcoach/status").json()
        big_stayed = alive(big["pid"])

        solo = ask(caller, "solo").json()  # it shares with nobody. The card where fewer, longer unused models go is the one without small
        after_solo = alive(beside["pid"]), alive(big["pid"])
        x = ask(caller, "x").json()  # never measured: small, unused longer than solo, makes way
        back = ask(caller, "small").json()  # beside x. Not beside solo, though solo measured only 1000
        end = caller.get("/loadcoach/status").json()
        together = alive(solo["pid"]) and alive(x["pid"])

    assert small_went and big_stayed and device(big) == "GPU0,GPU1" and device(beside) == "GPU0"
    assert scheduling_fields(status["loaded"])[0] == {"model": "big", "gpus": ["GPU0", "GPU1"], "state": "ready", "locked": False, "answers": [], "mib": [12000, 12000]}
    assert status["memory"]["GPU0"]["free"] == 976 and status["memory"]["GPU1"]["free"] == 2976
    assert device(solo) == "GPU1" and after_solo == (True, False)
    assert (device(x), device(back)) == ("GPU0", "GPU0") and together
    # One that has a card to itself is shown with no figure, and its card with no room.
    assert {one["model"]: one["mib"] for one in end["loaded"]} == {"solo": None, "x": [2000], "small": [2000]} and end["memory"]["GPU1"]["free"] == 0


def test_a_model_that_grew_is_measured_again_and_a_load_with_too_little_room_is_tried_again_alone(fake_llama_server, fake_cards, monkeypatch):
    monkeypatch.setattr("loadcoach.runner.LOOK_EVERY", 0)  # nvidia-smi is asked every time, so the check need not wait out what it said before
    models = {"p": sized("p", 3000, ["GPU0"]), "q": sized("q", 5000, ["GPU0"])}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0]) as caller:
        measures = lambda: {one["name"]: one["measured"] and one["measured"]["mib"] for one in caller.get("/loadcoach/models").json()["models"]}  # noqa: E731
        ask(caller, "p")
        q = ask(caller, "q").json()  # both are measured now: 3000 and 5000
        # What another program uses of a card is not there for models.
        (fake_cards[1] / "0.json").write_text('{"1": 6000}')
        others = caller.get("/loadcoach/status").json()["memory"]["GPU1"]

        ask(caller, "q", grow=9000)  # q grows to 14000 while it answers. LoadCoach still counts it as 5000
        before = measures()
        p = ask(caller, "p")  # put beside q, where it does not fit after all. Tried again alone: q makes way, and is measured as it goes
        q_went, after = not alive(q["pid"]), measures()
        failed = [entry for entry in events(caller, kind="model") if entry["what"] == "failed"]
        ask(caller, "q")  # 14000 does not fit beside p any more, so p makes way
        p_went = not alive(p.json()["pid"])

        forget = lambda: caller.post("/loadcoach/model-forget-measure", json={"name": "q"}).json()["forgotten"]  # noqa: E731
        forgotten = forget(), forget(), measures()["q"], caller.post("/loadcoach/model-forget-measure", json={"name": "none"}).status_code

    assert others == {"total": 16000, "others": 6000, "free": 8976}
    assert before == {"p": [3000], "q": [5000]} and p.status_code == 200 and q_went
    assert after == {"p": [3000], "q": [14000]}  # p was measured afresh alone, and q's highest figure is the one kept
    assert len(failed) == 1 and failed[0]["again"] == "alone" and "out of memory" in failed[0]["why"]
    assert p_went and forgotten == (True, False, None, 404)


# ---- a model's fingerprint -----------------------------------------------------------------------


def test_a_model_has_a_fingerprint_of_its_file_and_its_settings(fake_llama_server, tmp_path, capsys):
    first_build, second_build = b"a model file, first build", b"the same quant built again, under the same name"
    file, moved = tmp_path / "m.gguf", tmp_path / "elsewhere" / "m.gguf"
    moved.parent.mkdir()
    file.write_bytes(first_build), moved.write_bytes(first_build)
    models = {"m": model(str(file), 1), "twin": model(str(moved), 1), "warmer": model(str(file), 1, temp=0.9)}
    with gateway(fake_llama_server, models) as caller:

        def marks(until=lambda seen: all(mark for mark, _ in seen.values())):
            """Each model's fingerprint and its file's SHA-256, once every file has been read through, which is done apart from any request."""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                seen = {one["name"]: (one["fingerprint"], one["sha256"]) for one in caller.get("/loadcoach/models").json()["models"]}
                if until(seen):
                    return seen
                time.sleep(0.02)
            raise AssertionError(f"the fingerprints never came: {seen}")

        first = marks()
        answer = ask(caller, "m")
        ended = events(caller, kind="request")[-1]
        offered = {one["id"]: one for one in caller.get("/v1/models").json()["data"]}
        build = caller.get("/loadcoach/models").json()["build"]

        file.write_bytes(second_build)  # another file where the first was
        second = marks(lambda seen: seen["m"][0] not in (None, first["m"][0]))
        caller.post("/loadcoach/models/m", json={**models["m"], "flags": {**models["m"]["flags"], "ctx-size": 4096}})
        third = marks()

        # Every fingerprint is written down the first time it is seen, and stays when the model is removed and its file deleted.
        for name in models:
            caller.delete(f"/loadcoach/models/{name}")
        file.unlink(), moved.unlink()
        ever = caller.get("/loadcoach/fingerprints").json()["fingerprints"]
        one = caller.get("/loadcoach/fingerprints", params={"start": first["m"][0][:12]}).json()["fingerprints"]
        admin = add_user(caller, "jordan", "admin")
        guarded = caller.get("/loadcoach/fingerprints", headers=add_user(caller, "pat", "plain", by=admin)).status_code
        caller.headers.update(admin)
        server_command(caller, "fingerprints")
        listed_by_command = capsys.readouterr().out
        server_command(caller, "fingerprints", first["m"][0][:12])
        inspected_by_command = capsys.readouterr().out

    assert first["m"][1] == hashlib.sha256(first_build).hexdigest() and len(first["m"][0]) == 64
    assert first["twin"] == first["m"]  # the same file with the same settings, kept somewhere else: the same model
    assert first["warmer"][0] != first["m"][0] and first["warmer"][1] == first["m"][1]  # one flag differs
    # Every answer says exactly which model gave it, and the llama-server that ran it. So does the log, and the list a caller reads.
    assert (answer.headers["x-loadcoach-fingerprint"], answer.headers["x-loadcoach-build"]) == (first["m"][0], "1 (fake)")
    assert (ended["what"], ended["fingerprint"], ended["build"]) == ("done", first["m"][0], "1 (fake)")
    assert (offered["m"]["fingerprint"], offered["m"]["build"], build) == (first["m"][0], "1 (fake)", "1 (fake)")
    assert second["m"][1] == hashlib.sha256(second_build).hexdigest() and second["m"][0] != first["m"][0]
    assert second["twin"] == first["twin"] and second["warmer"][0] not in (first["warmer"][0], second["m"][0])  # the twin's file is as it was; warmer's is not
    assert third["m"][1] == second["m"][1] and third["m"][0] != second["m"][0]  # the file is the same, a setting is not
    # Five there have been: m and its twin as one, warmer, then m and warmer with the second file, then m with another context. The latest first.
    assert [entry["fingerprint"] for entry in ever] == [third["m"][0], second["warmer"][0], second["m"][0], first["warmer"][0], first["m"][0]] and guarded == 403
    assert len(one) == 1 and one[0]["model"] in ("m", "twin") and one[0]["settings"]["flags"]["ctx-size"] == 2048 and one[0]["first"][:2] == "20"
    assert one[0]["files"]["model"] == {"path": one[0]["settings"]["flags"]["model"], "size": len(first_build), "sha256": first["m"][1], "says": None}  # no .gguf inside, so it says nothing
    assert "FINGERPRINT" in listed_by_command and third["m"][0] in listed_by_command
    assert json.loads(inspected_by_command)["fingerprint"] == first["m"][0]


def test_every_model_that_has_no_measure_is_measured_in_turn_and_the_run_gives_way_to_a_request(fake_llama_server, fake_cards, tmp_path):
    models = {"a": sized("a", 3000), "b": sized("b", 4000), "broken": {**sized("broken", 100), "flags": {**sized("broken", 100)["flags"], "fail": True}},
              "on-the-processor": sized("p", 100, 0), "slow": {**sized("slow", 2000), "flags": {**sized("slow", 2000)["flags"], "takes": 1.0}}}  # fmt: skip
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0]) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=admin)
        listed = lambda: caller.get("/loadcoach/models", headers=admin).json()  # noqa: E731
        run = lambda who=admin, **body: caller.post("/loadcoach/model-measure-all", json=body, headers=who)  # noqa: E731

        def ended():
            deadline = time.monotonic() + 8
            while not (listed()["measuring"] or {}).get("ended") and time.monotonic() < deadline:
                time.sleep(0.05)
            return listed()["measuring"]

        before = listed()["to_measure"], listed()["measuring"], run(pat).status_code, run(on=False).json()

        # Someone is being answered: the run gives way at once, and nothing of it is lost.
        busy = others.submit(caller.post, "/v1/chat/completions", json={"model": "a", "sleep": 0.6}, headers=admin)
        wait_for_status(_signed(caller, admin), lambda status: status["running"])
        gave_way = run().status_code, ended()
        busy.result()

        # Stopped by hand while a model loads. Started again, it goes on with what is left.
        started = run()
        while listed()["measuring"]["now"] != "slow":
            time.sleep(0.02)
        twice, stopped = run().status_code, run(on=False).json()
        by_hand = ended()
        again = run().status_code, ended()
        measured = {one["name"]: one["measured"] and one["measured"]["mib"] for one in listed()["models"]}
        whats = [event["what"] for event in events(caller, admin, kind="model") if "measur" in event["what"]]

    assert before == (["a", "b", "broken", "slow"], None, 403, {"stopped": False})  # not the one on the processor: it uses no card
    # `a` loaded for the request that was under way, so it was measured before the run began. The run itself loaded nothing.
    assert gave_way[0] == 202 and gave_way[1]["ended"].startswith("It gave way") and (gave_way[1]["left"], gave_way[1]["done"]) == (["b", "broken", "slow"], [])
    assert started.json()["measuring"] == ["b", "broken", "slow"] and twice == 409 and stopped == {"stopped": True}
    assert by_hand["ended"] == "Stopped by jordan." and by_hand["done"] == ["b"] and "out of memory" in by_hand["failed"]["broken"] and by_hand["now"] is None
    assert again[0] == 202 and again[1]["ended"] == "Every model was tried." and again[1]["left"] == []
    assert measured == {"a": [3000], "b": [4000], "broken": None, "on-the-processor": None, "slow": [2000]}
    assert whats == ["measuring-all", "measured-all"] * 3














# ---- adapters ------------------------------------------------------------------------------------


def test_models_that_differ_only_in_adapters_are_answered_by_one_server(fake_llama_server, tmp_path):
    file, terse, pirate = tmp_path / "m.gguf", tmp_path / "terse.gguf", tmp_path / "pirate.gguf"
    file.write_bytes(b"the model"), terse.write_bytes(b"says little"), pirate.write_bytes(b"says arr")
    plain = model(str(file), 1)
    models = {"plain": plain, "terse": {**plain, "adapters": {"style": {"file": str(terse)}}}, "pirate": {**plain, "adapters": {"style": {"file": str(pirate), "scale": 0.5}}},
              "apart": {**model(str(file), 1, **{"ctx-size": 4096}), "adapters": {"style": {"file": str(terse)}}}}  # fmt: skip
    with gateway(fake_llama_server, models) as caller:
        deadline = time.monotonic() + 5
        while not all(one["fingerprint"] for one in caller.get("/loadcoach/models").json()["models"]) and time.monotonic() < deadline:
            time.sleep(0.02)  # the files are read through apart from any request
        offered = [one["id"] for one in caller.get("/v1/models").json()["data"]]
        first = ask(caller, "plain")
        second = ask(caller, "terse", lora=[{"id": 9, "scale": 3}])  # which adapters apply is not the caller's to write
        plain_went = not alive(first.json()["pid"])  # the plain one made way for the one with the adapter, which answers for both
        third = ask(caller, "plain")
        fourth = ask(caller, "pirate")  # an adapter the server was not started with: it is loaded afresh, with that one as well as the one it had
        fifth, sixth = ask(caller, "terse"), ask(caller, "plain")
        apart = ask(caller, "apart")  # the same file with another context is no kin: a server of its own
        status = caller.get("/loadcoach/status").json()["loaded"]
        shown = {one["name"]: (one["loaded"], one["stale"]) for one in caller.get("/loadcoach/models").json()["models"]}
        said = [entry.get("adapters") for entry in events(caller, kind="request") if entry["what"] == "done"]
        loads = [(entry["model"], entry["replaced"]) for entry in events(caller, kind="model") if entry["what"] == "loaded"]
        refused = [ask(caller, "terse+none").status_code, caller.post("/loadcoach/models/m", json={**plain, "flags": {**plain["flags"], "lora": str(terse)}}).status_code]

    mark = lambda answer: answer.headers["x-loadcoach-fingerprint"]  # noqa: E731
    pid = lambda answer: answer.json()["pid"]  # noqa: E731
    sent = lambda answer: json.loads(answer.json()["body"]).get("lora")  # noqa: E731
    started = lambda answer: (answer.json()["arguments"] + [None])[answer.json()["arguments"].index("--lora-scaled") + 1] if "--lora-scaled" in answer.json()["arguments"] else None  # noqa: E731
    assert offered == ["apart", "pirate", "plain", "terse"]  # each is a model, asked for by its name, and nothing else is offered
    # A server with no adapter is told nothing. One with adapters is told for each request which apply: the model's own, at its strength.
    assert (started(first), sent(first)) == (None, None) and first.json()["around"]["GGML_CUDA_DISABLE_GRAPHS"] is None
    assert (started(second), sent(second)) == (f"{terse}:1.0", [{"id": 0, "scale": 1.0}]) and second.json()["around"]["GGML_CUDA_DISABLE_GRAPHS"] == "1"
    assert plain_went and pid(third) == pid(second) and sent(third) == [{"id": 0, "scale": 0.0}]  # the plain model, from the server that has the adapter
    assert started(fourth) == f"{terse}:1.0,{pirate}:0.5" and pid(fourth) != pid(second) and sent(fourth) == [{"id": 0, "scale": 0.0}, {"id": 1, "scale": 0.5}]
    assert pid(fifth) == pid(sixth) == pid(fourth) and sent(fifth) == [{"id": 0, "scale": 1.0}, {"id": 1, "scale": 0.0}] and sent(sixth) == [{"id": 0, "scale": 0.0}, {"id": 1, "scale": 0.0}]
    assert pid(apart) != pid(fourth) and loads == [("plain", []), ("terse", ["plain"]), ("pirate", ["terse"]), ("apart", [])]
    assert [(one["model"], one["answers"]) for one in status] == [("pirate", ["plain", "terse"]), ("apart", [])]
    assert shown == {"plain": ("ready", False), "terse": ("ready", False), "pirate": ("ready", False), "apart": ("ready", False)}  # each of the kin is loaded, as far as anyone asking is concerned
    # What answered is always said, and a fingerprint is of the model that was asked for, whichever server its kin share.
    assert [answer.headers.get("x-loadcoach-adapters") for answer in (first, second, third, fourth)] == [None, "style", None, "style"]
    assert said == [None, "style", None, "style", "style", None, "style"]
    assert mark(first) == mark(third) == mark(sixth) and mark(second) == mark(fifth) and len({mark(first), mark(second), mark(fourth), mark(apart)}) == 4
    assert refused == [404, 400]  # a name is a model's name and nothing more; llama-server's own flags for adapters are not written by hand


def test_a_model_that_sees_and_the_same_model_without_a_projector_are_answered_by_one_server(fake_llama_server, tmp_path):
    file, eyes = tmp_path / "m.gguf", tmp_path / "eyes.gguf"
    file.write_bytes(b"the model"), eyes.write_bytes(b"what lets it see")
    plain = model(str(file), 1)
    models = {"plain": plain, "seeing": {**plain, "flags": {**plain["flags"], "mmproj": str(eyes)}}}
    picture = [{"role": "user", "content": [{"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    with gateway(fake_llama_server, models) as caller:
        deadline = time.monotonic() + 5
        while not all(one["fingerprint"] for one in caller.get("/loadcoach/models").json()["models"]) and time.monotonic() < deadline:
            time.sleep(0.02)  # the files are read through apart from any request
        first = ask(caller, "plain", messages=picture)  # a server with no projector: what llama-server makes of a picture is its own affair
        second = ask(caller, "seeing", messages=picture)  # the plain one makes way for the one that sees
        plain_went = not alive(first.json()["pid"])
        third = ask(caller, "plain")  # answered from the server that sees. Words alone are answered the same with or without a projector
        fourth = ask(caller, "plain", messages=picture)  # but the plain model was not set up to see, and does not start to because of where it is answered
        status = caller.get("/loadcoach/status").json()["loaded"]
        refused = [entry for entry in events(caller, kind="request") if entry["what"] == "refused"]

    mark = lambda answer: answer.headers["x-loadcoach-fingerprint"]  # noqa: E731
    started = lambda answer: "--mmproj" in answer.json()["arguments"]  # noqa: E731
    assert (started(first), started(second)) == (False, True) and plain_went and third.json()["pid"] == second.json()["pid"]
    assert [(one["model"], one["answers"]) for one in status] == [("seeing", ["plain"])]
    assert mark(first) == mark(third) != mark(second)  # the plain model is the plain model, whichever server answers for it
    assert fourth.status_code == 400 and "plain has no projector" in fourth.json()["error"]["message"] and len(refused) == 1 and "picture" in refused[0]["why"]
