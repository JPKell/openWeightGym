"""Checks of the event log kept on the control node: compute sends, control node chains, and the move."""

import json
import sqlite3

from typer.testing import CliRunner

from loadcoach.log import Log, public_number
from test_app import add_user, fake_llama_server, gateway, in_an_empty_folder  # noqa: F401  (two of them set a check up)


def test_compute_sends_its_entries_front_chains_them_and_a_request_keeps_compute_s_number(fake_llama_server):
    with gateway(fake_llama_server, keep_text=5) as caller:
        jordan = add_user(caller, "jordan", "admin")
        answer = caller.post("/v1/chat/completions", json={"model": "small"}, headers=jordan)
        number = int(answer.headers["x-loadcoach-request-id"])
        facts = caller.get(f"/loadcoach/requests/{number}", headers=jordan).json()
        listed = caller.get("/loadcoach/requests", headers=jordan).json()["requests"]
        shown = caller.get("/loadcoach/events", params={"kind": "request"}, headers=jordan).json()["events"]
        front = caller.log  # turnstile's log, which is the one kept

    assert facts["asked"]["source"] == number and facts["ended"]["what"] == "done" and facts["text"]["asked"]["model"] == "small"
    assert listed[0]["id"] == number  # the request is known by compute's number
    assert [entry["of"] for entry in shown if entry["what"] == "done"] == [number]
    # One chain, on the control node: the user added on the control node and the request sent from compute are in it together.
    assert front.verify()[1] is None and front.verify()[0] > 2
    assert front.db.execute("SELECT COUNT(*) FROM events WHERE machine = 'compute' AND source = ?", (number,)).fetchone()[0] == 1


def test_compute_keeps_what_front_does_not_have_and_sends_it_once_front_is_back(tmp_path):
    compute, front = Log(str(tmp_path / "compute.db"), chained=False), Log(str(tmp_path / "front.db"), keep_days=0)
    first = compute.add("request", "asked", "pat", model="small")
    compute.add("request", "done", "pat", model="small", of=first)
    entries, texts = compute.unsent()  # control node is away: nothing is marked as sent
    assert [one["id"] for one in compute.unsent()[0]] == [first, first + 1] and texts == []
    assert compute.db.execute("SELECT COUNT(*) FROM events WHERE hash IS NOT NULL").fetchone()[0] == 0  # no chain on compute

    compute.sent(front.take("compute", entries, texts, 0))
    front.take("compute", entries, texts, 0)  # sent twice: taken once
    assert compute.unsent()[0] == [] and front.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert [entry["what"] for entry in front.about(first, request=True)] == ["asked", "done"] and front.verify() == (1, None)


def test_two_compute_logs_with_the_same_local_request_number_keep_separate_answers_and_texts(tmp_path):
    front = Log(str(tmp_path / "front.db"), keep_days=0, keep_text=5)
    front.machine_names = ("compute", "rig")
    for machine in ("compute", "rig"):
        worker = Log(str(tmp_path / f"{machine}.db"), chained=False, keep_text=5)
        number = worker.add("request", "asked", "pat", model=machine)
        worker.add("request", "done" if machine == "compute" else "failed", "pat", model=machine, of=number)
        worker.keep_asked(number, json.dumps({"machine": machine}))
        front.take(machine, *worker.unsent(), 5)
    first, second = sorted(front.requests("pat", 10)["requests"], key=lambda one: one["model"])
    assert first["id"] == number and second["id"] == public_number("rig", number)
    assert first["outcome"] == "done" and second["outcome"] == "failed"
    assert [entry["model"] for entry in front.about(first["id"], request=True)] == ["compute", "compute"]
    assert [entry["model"] for entry in front.about(second["id"], request=True)] == ["rig", "rig"]
    assert front.about(second["id"], request=True)[1]["of"] == second["id"]
    assert front.text_of(first["id"])["asked"]["machine"] == "compute"
    assert front.text_of(second["id"])["asked"]["machine"] == "rig"
    assert front.verify()[1] is None


def test_the_log_moves_to_front_once_and_compute_s_copy_stays(tmp_path):
    from weightroom.app import cli as weightroom_cli

    compute = Log(str(tmp_path / "compute.db"), keep_text=5)
    compute.add("backup", "created", "pat", machine="compute")  # a detail with the name of a column control node keeps: it stays a detail
    first = compute.add("request", "asked", "pat", model="small")
    compute.add("request", "done", "pat", model="small", of=first)
    compute.keep_asked(first, json.dumps({"model": "small"}))
    (tmp_path / "front").mkdir()
    settings = tmp_path / "front.toml"
    settings.write_text('compute = "http://c:1"\ndata = "front"\n')
    front = Log(str(tmp_path / "front" / "accounts.db"), keep_days=0)
    front.take("compute", compute.unsent()[0][:2], [], 0)  # a move cut short after two entries
    move = lambda: CliRunner().invoke(weightroom_cli, ["--config", str(settings), "log-from", str(tmp_path / "compute.db")])  # noqa: E731
    moved, again = move(), move()

    assert moved.exit_code == 0 and "Took 3 entries and 1 requests kept in full" in moved.output, moved.output
    assert front.db.execute("SELECT COUNT(*) FROM events WHERE machine = 'compute'").fetchone()[0] == 3  # it went on where it stopped
    assert front.read(kind="backup")[0]["machine"] == "compute"
    assert [entry["what"] for entry in front.about(first, request=True)] == ["asked", "done"] and front.text_of(first)["asked"] == {"model": "small"}
    assert front.verify()[1] is None and "first move only" in str(again.exception)
    assert sqlite3.connect(tmp_path / "compute.db").execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3  # the way back


def test_front_prunes_from_the_start_of_its_chain_so_what_is_left_still_verifies(tmp_path):
    import time

    front = Log(str(tmp_path / "front.db"), keep_days=90)
    front.write((time.time() - 100 * 86400, "user", "added", "jordan", "", None, None, None, "{}", None))
    young = front.add("user", "signed-in", "jordan")
    front.write((time.time() - 200 * 86400, "request", "asked", "pat", "", None, None, None, "{}", None), machine="compute", source=7)  # moved here later, though older
    gone = front.prune()

    assert gone == 1 and [entry["id"] for entry in front.read(limit=10)][0] == young  # only the start: the old moved entry stays, after a young one
    assert front.verify()[1] is None and Log(str(tmp_path / "all.db"), keep_days=0).prune() == 0


def test_front_s_own_request_and_a_compute_request_with_the_same_number_keep_their_own_records(tmp_path):
    front = Log(str(tmp_path / "front.db"), keep_days=0, keep_text=5)
    hosted = front.add("request", "asked", "pat", model="cloud", hosted="vendor")  # answered on the control node, numbered by the control node's entry
    front.add("request", "done", "pat", model="cloud", of=hosted)
    front.keep_asked(hosted, json.dumps({"from": "front"}))
    worker = Log(str(tmp_path / "compute.db"), chained=False, keep_text=5)
    while (number := worker.add("request", "asked", "pat", model="small")) < hosted:  # compute's counter catches up with the control node's
        worker.add("request", "done", "pat", model="small", of=number)
    worker.add("request", "done", "pat", model="small", of=number)
    worker.keep_asked(number, json.dumps({"from": "compute"}))
    front.take("compute", *worker.unsent(), 5)
    assert number == hosted
    listed = {one["model"]: one["id"] for one in front.requests("pat", 500)["requests"] if one["model"] == "cloud" or one["id"] == number}
    assert listed == {"cloud": public_number(None, hosted), "small": number}
    assert [(entry["model"], entry["what"]) for entry in front.about(listed["cloud"], request=True)] == [("cloud", "asked"), ("cloud", "done")]
    assert front.about(listed["cloud"], request=True)[1]["of"] == listed["cloud"]
    assert [entry["model"] for entry in front.about(number, request=True)] == ["small", "small"]
    assert front.text_of(listed["cloud"])["asked"] == {"from": "front"} and front.text_of(number)["asked"] == {"from": "compute"}
