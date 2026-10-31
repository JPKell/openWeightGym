"""Checks of batches: requests handed in to be answered later, which outlive LoadCoach stopping."""

import json
import time

import pytest

from loadcoach.batches import TRIES, Batches, lines_of, uploaded
from test_app import add_user, events, fake_cards, fake_llama_server, gateway, in_an_empty_folder, settled, sized  # noqa: F401  (three of them set a check up)

CHAT = "/v1/chat/completions"


def lines(*requests):
    """A file for a batch: one request to a line, each with a name of its own."""
    return "\n".join(json.dumps({"custom_id": f"q{number}", "method": "POST", "url": CHAT, "body": body}) for number, body in enumerate(requests)).encode()


def hand_in(caller, who, content, **more):
    """Upload a file and start a batch from it. Gives the batch as it is answered."""
    file = caller.post("/v1/files", data={"purpose": "batch"}, files={"file": ("in.jsonl", content)}, headers=who).json()
    return caller.post("/v1/batches", json={"input_file_id": file["id"], "endpoint": CHAT, "completion_window": "24h", **more}, headers=who).json()


def until(caller, who, number, wanted, seconds=8):
    """Ask how far a batch has got until `wanted(batch)` is true, and return it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        batch = caller.get(f"/v1/batches/{number}", headers=who).json()
        if wanted(batch):
            return batch
        time.sleep(0.03)
    raise AssertionError(f"the batch never got there: {batch}")


def answers(caller, who, file):
    return [json.loads(line) for line in caller.get(f"/v1/files/{file}/content", headers=who).text.splitlines()]


def test_status_shows_only_executing_batches_and_respects_request_visibility(fake_llama_server, monkeypatch):
    monkeypatch.setattr('loadcoach.batches.AGAIN_AFTER', .05)
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, 'jordan', 'admin')
        pat = add_user(caller, 'pat', 'plain', by=admin)
        cam = add_user(caller, 'cam', 'plain', by=admin)
        caller.post('/loadcoach/drain', json={'why': 'test queue'}, headers=admin)
        body = {'model': 'small', 'sleep': 1.5, 'messages': []}
        first = hand_in(caller, pat, lines(body))
        queued = hand_in(caller, cam, lines(body))
        assert caller.get('/loadcoach/status', headers=admin).json()['active_batches'] == []
        caller.post('/loadcoach/drain', json={'on': False}, headers=admin)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = caller.get('/loadcoach/status', headers=admin).json()
            if status['active_batches']:
                break
            time.sleep(.02)
        assert [job['id'] for job in status['active_batches']] == [first['id']]
        job = status['active_batches'][0]
        assert job['model'] == 'small' and job['user'] == 'pat' and job['request_ids']
        assert all(entry.get('batch_id') == first['id'] for entry in status['running'])
        assert [job['id'] for job in caller.get('/loadcoach/status', headers=pat).json()['active_batches']] == [first['id']]
        assert caller.get('/loadcoach/status', headers=cam).json()['active_batches'] == []
        caller.post(f"/v1/batches/{first['id']}/cancel", headers=admin)
        until(caller, cam, queued['id'], lambda batch: batch['status'] == 'completed')
        assert caller.get('/loadcoach/status', headers=admin).json()['active_batches'] == []


def test_a_batch_is_handed_in_answered_behind_everyone_and_its_answers_fetched(fake_llama_server):
    said = [{"role": "user", "content": "hello"}]
    with gateway(fake_llama_server) as caller:
        jordan = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=jordan)
        upload = lambda who=pat, **form: caller.post("/v1/files", headers=who, **form)  # noqa: E731
        wrong_uploads = [upload(data={"purpose": "fine-tune"}, files={"file": ("x", b"{}")}).status_code, upload(json={"purpose": "batch"}).status_code, upload(data={"purpose": "batch"}).status_code]
        file = upload(data={"purpose": "batch"}, files={"file": ("mine.jsonl", lines({"model": "small", "messages": said, "stream": True}, {"model": "nope", "messages": said}, {"model": "small", "messages": said}))}).json()
        start = lambda who=pat, **what: caller.post("/v1/batches", json={"input_file_id": file["id"], "endpoint": CHAT, "completion_window": "24h", **what}, headers=who)  # noqa: E731
        bad_line = upload(data={"purpose": "batch"}, files={"file": ("bad.jsonl", lines({"model": "small"}) + b"\nnot json")}).json()
        wrong_starts = [start(jordan).status_code, start(endpoint="/v1/images").status_code, start(input_file_id=bad_line["id"]).status_code, start(input_file_id="file-none").status_code]

        begun = start(metadata={"for": "a benchmark"}).json()
        waiting = caller.get("/loadcoach/status", headers=jordan).json()["batches"]
        done = until(caller, pat, begun["id"], lambda batch: batch["status"] == "completed")
        good, bad = answers(caller, pat, done["output_file_id"]), answers(caller, pat, done["error_file_id"])
        asked = [(entry["user"], entry["trace"], entry["priority"]) for entry in events(caller, jordan, kind="request") if entry["what"] == "done"]
        listed = [one["id"] for one in caller.get("/v1/batches", headers=pat).json()["data"]], caller.get("/v1/batches", headers=jordan).json()["data"]
        for_the_site = [(one["id"], one["user"]) for one in caller.get("/loadcoach/batches", headers=jordan).json()["batches"]], len(caller.get("/loadcoach/batches", headers=pat).json()["batches"])
        not_theirs = [caller.get(f"/v1/batches/{begun['id']}", headers=jordan).status_code, caller.get(f"/v1/files/{done['output_file_id']}/content", headers=jordan).status_code]
        whats = [entry["what"] for entry in events(caller, jordan, kind="batch")]
        removed = caller.delete(f"/v1/files/{file['id']}", headers=pat).json(), caller.get(f"/v1/files/{file['id']}", headers=pat).status_code
        left = caller.get("/loadcoach/status", headers=jordan).json()["batches"]

    assert wrong_uploads == [400, 400, 400] and (file["object"], file["purpose"], file["filename"]) == ("file", "batch", "mine.jsonl") and file["bytes"] > 100
    assert wrong_starts == [404, 400, 400, 404]  # someone else's file, an address batches are not for, a line that is no request, no such file
    assert (begun["object"], begun["status"], begun["request_counts"], begun["metadata"]) == ("batch", "in_progress", {"total": 3, "completed": 0, "failed": 0}, {"for": "a benchmark"})
    assert waiting == 1 and left == 0  # a batch that is not done counts, so that compute is not put to sleep under it
    assert done["request_counts"] == {"total": 3, "completed": 2, "failed": 1} and done["completed_at"] and "user" not in done
    # The answers, each under the name its request was given, exactly as the model gave them. A request is never streamed: there is nobody to stream to.
    assert [(one["custom_id"], one["response"]["status_code"], one["error"]) for one in good] == [("q0", 200, None), ("q2", 200, None)]
    assert json.loads(good[0]["response"]["body"]["body"])["stream"] is False and good[0]["response"]["request_id"].isdigit()
    assert [(one["custom_id"], one["response"]["status_code"]) for one in bad] == [("q1", 404)] and "does not exist" in bad[0]["error"]["message"]
    # Each request went through the line as pat's own, behind everyone, as part of the batch.
    assert asked == [("pat", begun["id"], "low"), ("pat", begun["id"], "low")]
    assert listed == ([begun["id"]], []) and for_the_site == ([(begun["id"], "pat")], 1) and not_theirs == [404, 404]
    assert whats == ["started", "completed"] and removed == ({"id": file["id"], "object": "file", "deleted": True}, 404)


def test_a_batch_goes_on_where_it_was_when_loadcoach_comes_back(fake_llama_server, tmp_path):
    database = str(tmp_path / "loadcoach.db")
    slow = {"model": "small", "sleep": 0.4, "messages": [{"role": "user", "content": "hello"}]}
    with gateway(fake_llama_server, database=database) as caller:
        pat = add_user(caller, "pat", "plain", by=add_user(caller, "jordan", "admin"))
        begun = hand_in(caller, pat, lines(slow, slow, slow))
        until(caller, pat, begun["id"], lambda batch: batch["request_counts"]["completed"] == 1)
        time.sleep(0.1)  # the second request is being answered when LoadCoach stops
    kept = Batches(str(tmp_path / "front" / "accounts.db"))  # the batches are front's, in its accounts store
    between = kept.get(begun["id"])
    tries = [count for (count,) in kept.db.execute("SELECT tries FROM batch_lines WHERE batch = ? ORDER BY line", (begun["id"],))]

    with gateway(fake_llama_server, database=database) as caller:  # nobody asks for anything: the batch goes on by itself, in pat's name, with no key of pat's
        done = until(caller, pat, begun["id"], lambda batch: batch["status"] == "completed")
        good = answers(caller, pat, done["output_file_id"])
    servers = [json.loads(line)["response"]["body"]["pid"] for line in kept.content(done["output_file_id"]).decode().splitlines()]

    assert (between["status"], between["request_counts"]) == ("in_progress", {"total": 3, "completed": 1, "failed": 0}) and tries == [1, 1, 0]
    assert done["request_counts"] == {"total": 3, "completed": 3, "failed": 0} and [one["custom_id"] for one in good] == ["q0", "q1", "q2"]
    assert servers[0] != servers[1] == servers[2]  # the first answer is the one from before. Only what had no answer was asked again

    # A request that was begun and cut off every time is not begun for ever: it gets an answer that says so.
    kept.db.execute("UPDATE batch_lines SET result = NULL, tries = ? WHERE batch = ? AND line = 1", (TRIES, begun["id"]))
    assert kept.next_line(begun["id"]) is None and "cut off each time" in json.loads(kept.db.execute("SELECT result FROM batch_lines WHERE batch = ? AND line = 1", (begun["id"],)).fetchone()[0])["body"]["error"]["message"]


def test_a_batch_waits_while_requests_are_not_taken_and_can_be_cancelled(fake_llama_server, monkeypatch):
    monkeypatch.setattr("loadcoach.batches.AGAIN_AFTER", 0.05)
    quick, slow = {"model": "small", "messages": []}, {"model": "small", "sleep": 1.0, "messages": []}
    with gateway(fake_llama_server) as caller:
        jordan = add_user(caller, "jordan", "admin")
        pat = add_user(caller, "pat", "plain", by=jordan)
        # Requests are not being taken. That is not an answer: the batch waits, and goes on when they are taken again.
        caller.post("/loadcoach/drain", json={"why": "new drives"}, headers=jordan)
        held = hand_in(caller, pat, lines(quick, quick))
        time.sleep(0.3)
        meanwhile = caller.get(f"/v1/batches/{held['id']}", headers=pat).json()["request_counts"]
        caller.post("/loadcoach/drain", json={"on": False}, headers=jordan)
        went_on = until(caller, pat, held["id"], lambda batch: batch["status"] == "completed")["request_counts"]

        # Cancelled while its second request is being answered: that one is given up, the first answer is kept, the third is never asked.
        long = hand_in(caller, pat, lines(quick, slow, quick))
        until(caller, pat, long["id"], lambda batch: batch["request_counts"]["completed"] == 1)
        not_pats = caller.post(f"/v1/batches/{long['id']}/cancel", headers=add_user(caller, "cam", "plain", by=jordan)).status_code
        cancelled = until(caller, pat, caller.post(f"/v1/batches/{long['id']}/cancel", headers=pat).json()["id"], lambda batch: batch["status"] == "cancelled")
        kept = answers(caller, pat, cancelled["output_file_id"])
        asked = sum(entry["what"] == "asked" and entry["trace"] == long["id"] for entry in events(caller, jordan, kind="request"))

    assert meanwhile == {"total": 2, "completed": 0, "failed": 0} and went_on == {"total": 2, "completed": 2, "failed": 0}
    assert not_pats == 404 and cancelled["request_counts"] == {"total": 3, "completed": 1, "failed": 0} and cancelled["cancelled_at"]
    assert [one["custom_id"] for one in kept] == ["q0"] and asked == 2


def test_a_batch_file_is_read_line_by_line_and_old_batches_lose_their_files(tmp_path):
    with pytest.raises(ValueError, match="Line 2"):
        lines_of(lines({"model": "m"}) + b"\n" + json.dumps({"custom_id": "q0", "method": "POST", "url": CHAT, "body": {"model": "m"}}).encode(), CHAT)  # a name used twice
    with pytest.raises(ValueError, match="Line 1"):
        lines_of(json.dumps({"custom_id": "a", "method": "POST", "url": "/v1/embeddings", "body": {"model": "m"}}).encode(), CHAT)  # for another address than the batch
    with pytest.raises(ValueError, match="from 1 to"):
        lines_of(b"\n\n", CHAT)
    # A form as a browser or OpenAI's own library sends it: fields between lines that mark where each begins.
    form = uploaded("multipart/form-data; boundary=xyz", b'--xyz\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n--xyz\r\nContent-Disposition: form-data; name="file"; filename="a.jsonl"\r\n\r\n{"a": "\xc3\xa9"}\n{"b": 2}\r\n--xyz--\r\n')
    assert form == {"purpose": ("", b"batch"), "file": ("a.jsonl", b'{"a": "\xc3\xa9"}\n{"b": 2}')}

    kept = Batches(str(tmp_path / "loadcoach.db"))
    file = kept.put_file("pat", "in.jsonl", "batch", lines({"model": "m"}))
    batch = kept.start("pat", CHAT, file["id"], lines_of(kept.content(file["id"]), CHAT))
    kept.keep(batch["id"], kept.next_line(batch["id"])["line"], 200, "7", {"choices": []})
    ended = kept.finish(batch["id"])
    kept.prune(7)
    assert kept.content(ended["output_file_id"]) and kept.open() == 0  # ended a moment ago: everything is there
    kept.db.execute("UPDATE batches SET ended = ended - 8 * 86400")
    kept.prune(0)
    assert kept.content(ended["output_file_id"])  # 0 keeps everything for ever
    kept.prune(7)
    after = kept.get(batch["id"])
    assert kept.content(ended["output_file_id"]) is None and kept.content(file["id"]) is None  # the files are gone
    assert (after["status"], after["request_counts"]) == ("completed", {"total": 1, "completed": 1, "failed": 0})  # the batch is still listed, with its counts


def test_a_measuring_run_goes_on_with_the_models_that_are_left_when_loadcoach_comes_back(fake_llama_server, fake_cards, tmp_path):
    database = str(tmp_path / "loadcoach.db")
    models = {"a": sized("a", 3000), "slow": {**sized("slow", 2000), "flags": {**sized("slow", 2000)["flags"], "takes": 1.0}}, "z": sized("z", 1000)}  # measured in the order models are listed: by name
    measuring = lambda caller, who: caller.get("/loadcoach/models", headers=who).json()["measuring"]  # noqa: E731

    def wait_for(caller, who, wanted):
        deadline = time.monotonic() + 10
        while not wanted(measuring(caller, who) or {}) and time.monotonic() < deadline:
            time.sleep(0.03)
        return measuring(caller, who)

    with gateway(fake_llama_server, models, database=database, nvidia_smi=fake_cards[0]) as caller:
        jordan = add_user(caller, "jordan", "admin")
        assert caller.post("/loadcoach/model-measure-all", json={}, headers=jordan).status_code == 202
        wait_for(caller, jordan, lambda run: run.get("now") == "slow")  # `a` is measured, `slow` is loading, `z` is still to come
    with gateway(fake_llama_server, models, database=database, nvidia_smi=fake_cards[0]) as caller:  # nobody presses anything
        again = wait_for(caller, jordan, lambda run: run.get("ended"))
        measured = {one["name"]: bool(one["measured"]) for one in caller.get("/loadcoach/models", headers=jordan).json()["models"]}
        whats = [(entry["what"], entry.get("again", False)) for entry in events(caller, jordan, kind="model") if "measur" in entry["what"]]
    with gateway(fake_llama_server, models, database=database, nvidia_smi=fake_cards[0]) as caller:
        time.sleep(0.2)
        afterwards = measuring(caller, jordan)  # a run that ended is not begun again

    assert (again["by"], again["ended"], again["left"]) == ("jordan", "Every model was tried.", []) and sorted(again["done"]) == ["slow", "z"]
    assert measured == {"a": True, "slow": True, "z": True} and afterwards is None
    assert whats == [("measuring-all", False), ("measured-all", False), ("measuring-all", True), ("measured-all", False)]


def test_a_batch_may_name_a_role_which_front_picks_for_as_each_request_comes_up(fake_llama_server):
    said = [{"role": "user", "content": "hello"}]
    with gateway(fake_llama_server) as caller:
        jordan = add_user(caller, "jordan", "admin")
        caller.post("/loadcoach/roles/chat", json={"models": {"big": 1, "small": 2}}, headers=jordan)
        caller.post("/loadcoach/roles/none", json={"models": {"big": 1}, "requires": {"pictures": True}}, headers=jordan)  # big cannot see
        begun = hand_in(caller, jordan, lines({"model": "chat", "messages": said}, {"model": "none", "messages": said}))
        done = until(caller, jordan, begun["id"], lambda batch: batch["status"] == "completed")
        good, bad = answers(caller, jordan, done["output_file_id"]), answers(caller, jordan, done["error_file_id"])
        asked = [(entry["model"], entry.get("role")) for entry in events(caller, jordan, kind="request") if entry["what"] == "asked"]

    # The role is picked for on the front machine; compute is sent the model, and keeps which role it was asked as.
    assert [(one["custom_id"], json.loads(one["response"]["body"]["body"])["model"]) for one in good] == [("q0", "small")]
    assert asked == [("small", "chat")]
    assert bad[0]["custom_id"] == "q1" and bad[0]["response"]["status_code"] == 503 and "No model of the role none" in bad[0]["error"]["message"]
