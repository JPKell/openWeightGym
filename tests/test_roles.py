"""Checks of roles: a kind of work named where a model would be, and the model picked for it."""

import json

import pytest

from loadcoach.groups import Group
from loadcoach.roles import check_role, ordered, pick
from test_app import GROUPS, add_user, events, fake_llama_server, gateway, in_an_empty_folder, model, settled  # noqa: F401  (two of them set a check up)


def one(name, warm=False, load=None, speed=None, score=1):
    return {"name": name, "warm": warm, "load": load, "speed": speed, 'score': score, 'resident': warm,
            'loaded_factor': 1.25 if warm else 1, 'fast_loaded_factor': 2 if warm else 1}


def test_the_rule_that_picks_and_the_order_of_a_list():
    # A list is ordered by the owner's weight, and by score times weight once there are scores. Weight 0 leaves a model out.
    listed = {"models": {"small": 1, "big": 2, "old": 0, "mid": 1}}
    assert ordered(listed) == ["big", "small", "mid"] and ordered(listed, {"mid": 5, "big": 1}) == ["mid", "big", "small"]

    cold = [one("big"), one("mid"), one("small")]
    third_warm = [one("big", load=40, speed=20, score=100), one("mid", load=20, speed=30, score=90), one("small", True, speed=90, score=50)]
    second_warm = [one("big", load=40, speed=20, score=100), one("mid", True, speed=30, score=90), one("small", True, speed=90, score=50)]
    picked = lambda strength, among: pick(strength, among)[0]  # noqa: E731
    # Plain: sufficient weighted quality is required; residency cannot rescue the weak candidate.
    assert (picked(None, cold), picked(None, third_warm), picked(None, second_warm)) == ("big", "big", "mid")
    # best: the top, whatever is warm. available: the highest that is warm, and the top if none is.
    assert (picked("best", second_warm), picked("available", third_warm), picked("available", second_warm), picked("available", cold)) == ("big", "small", "mid", "big")
    # fast: the one done soonest. A warm quick one beats loading a better one. Among cold ones, the time to load counts.
    assert (picked("fast", second_warm), picked("fast", [one("big", load=5, speed=100), one("mid", load=60, speed=100)])) == ("small", "big")
    assert picked("fast", [one("slow", True, speed=2), one("quick", load=10, speed=100)]) == "quick"  # 200 s of writing against 10 s of loading and 4 of writing
    assert pick(None, []) is None and "warm" in pick("available", second_warm)[1] and "speed preference" in pick("fast", second_warm)[1]

    for wrong in (("chat:best", {"models": {}}), ("small", {"models": {}}), ("chat", {"models": {"nope": 1}}), ("chat", {"models": {"small": -1}}), ("chat", {"order": []})):
        with pytest.raises(ValueError):
            check_role(*wrong, {"small", "big"})


@pytest.mark.parametrize("weight", [float("inf"), float("-inf"), float("nan"), -1, True, "2", [], {}])
def test_invalid_role_weights_are_rejected(weight):
    with pytest.raises(ValueError, match="finite number"):
        check_role("chat", {"models": {"small": weight}}, {"small"})


def test_a_role_is_asked_for_like_a_model_and_every_answer_says_what_was_picked_and_why(fake_llama_server):
    models = {name: model(f"{name}.gguf", 1) for name in ("strong", "middle", "quick")}
    groups = {**GROUPS, "narrow": Group(can=frozenset({"load"}), models=("quick",)), "elsewhere": Group(can=frozenset({"load"}), models=("another",))}
    with gateway(fake_llama_server, models, groups=groups) as caller:
        jordan = add_user(caller, "jordan", "admin")
        pat, nan, ray = (add_user(caller, name, group, by=jordan) for name, group in (("pat", "plain"), ("nan", "narrow"), ("ray", "elsewhere")))
        put = lambda name, who=jordan, **listed: caller.post(f"/loadcoach/roles/{name}", json={"models": listed}, headers=who)  # noqa: E731
        chat = lambda name, who=pat, **more: caller.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "hello"}], **more}, headers=who)  # noqa: E731
        said = lambda answer: (answer.headers.get("x-loadcoach-picked"), answer.headers.get("x-loadcoach-because"))  # noqa: E731

        wrong = [put("chat", pat, strong=1).status_code, put("strong", strong=1).status_code, put("chat", nope=1).status_code, put("a:b", strong=1).status_code]
        added = put("chat", quick=1, strong=3, middle=2)  # the weights order it: strong, middle, quick
        offered = [entry["id"] for entry in caller.get("/v1/models", headers=pat).json()["data"]], caller.get("/loadcoach/me", headers=pat).json()["roles"]

        first = chat("chat")  # nothing is loaded: the top of the list
        again = chat("chat")  # the top is warm now
        caller.post("/v1/chat/completions", json={"model": "quick"}, headers=pat)  # quick is loaded by name. The cards hold strong and quick
        still = chat("chat")  # quick is warm but third in the list, which is not near the top: strong
        available = chat("chat:available")  # the highest that is warm
        best = chat("chat:best")
        asked = events(caller, jordan, kind="request")[-2]

        # A model is passed over if the person may not use it. Someone who may use none of a role's models does not have the role.
        narrow = chat("chat", nan), chat("chat:best", nan)
        none = chat("chat", ray).status_code, caller.get("/v1/models", headers=ray).json()["data"]
        unknown = chat("chat:strongest").status_code, chat("talk").status_code

        dry = caller.get("/loadcoach/pick", params={"model": "chat:available"}, headers=pat).json()
        not_theirs = caller.get("/loadcoach/pick", params={"model": "chat"}, headers=ray).status_code
        listed = caller.get("/loadcoach/roles", headers=jordan).json()
        caller.post("/loadcoach/model-rename", json={"name": "middle", "to": "medium"}, headers=jordan)
        followed = caller.get("/loadcoach/roles", headers=jordan).json()["roles"][0]["order"]
        removed = caller.delete("/loadcoach/roles/chat", headers=jordan).json(), chat("chat").status_code

    assert wrong == [403, 400, 400, 400] and added.status_code == 201
    assert offered == (["middle", "quick", "strong", "chat"], ["chat"])  # a role is offered beside the models, and chat pages can list it
    assert said(first) == ("strong", "highest preference after loaded-model bonuses") and said(again) == ("strong", "ready and loaded locally; 25% loaded-model bonus")
    assert said(still)[0] == "strong" and said(available) == ("strong", "the highest in the list that is warm") and said(best) == ("strong", "the top of the list")
    # Turnstile picks, on the front machine. Compute, and the model server behind it, are sent the plain model name.
    assert json.loads(first.json()["body"])["model"] == "strong" and first.headers["x-loadcoach-role"] == "chat" and best.headers["x-loadcoach-role"] == "chat:best"
    assert (asked["what"], asked["model"], asked["role"], asked["picked"], asked["because"]) == ("asked", "strong", "chat:best", "strong", "the top of the list")
    assert said(narrow[0])[0] == said(narrow[1])[0] == "quick" and none == (404, []) and unknown == (404, 404)
    assert (dry["role"], dry["strength"], dry["picked"]) == ("chat", "available", "strong") and [(entry["name"], entry["weight"], entry["warm"]) for entry in dry["models"]] == [("strong", 3, True), ("middle", 2, False), ("quick", 1, True)]
    assert not_theirs == 404 and listed["roles"] == [{"name": "chat", "models": {"quick": 1, "strong": 3, "middle": 2}, "order": ["strong", "middle", "quick"]}] and listed["strengths"] == ["best", "available", "fast"]
    assert followed == ["strong", "medium", "quick"] and removed == ({"removed": "chat"}, 404)


def test_a_role_passes_over_what_cannot_answer_and_tries_the_next_if_a_pick_will_not_load(fake_llama_server, tmp_path):
    eyes = tmp_path / "eyes.gguf"
    eyes.touch()
    models = {"broken": model("broken.gguf", 1, fail=True), "blind": model("blind.gguf", 1), "seeing": model("seeing.gguf", 1, mmproj=str(eyes)), "short": model("short.gguf", 1, **{"ctx-size": 16})}
    picture = [{"role": "user", "content": [{"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    with gateway(fake_llama_server, models) as caller:
        jordan = add_user(caller, "jordan", "admin")
        caller.post("/loadcoach/roles/look", json={"models": {"blind": 3, "seeing": 2}}, headers=jordan)
        caller.post("/loadcoach/roles/work", json={"models": {"broken": 3, "blind": 2}}, headers=jordan)
        caller.post("/loadcoach/roles/brief", json={"models": {"short": 2, "blind": 1}}, headers=jordan)
        caller.post("/loadcoach/roles/dead", json={"models": {"broken": 1}}, headers=jordan)
        chat = lambda name, messages: caller.post("/v1/chat/completions", json={"model": name, "messages": messages}, headers=jordan)  # noqa: E731
        words = [{"role": "user", "content": "hello there, how are you today, and what is the weather like where you are"}]

        sees = chat("look", words), chat("look", picture)  # with words, the top of the list. With a picture, only a model that sees
        warm = chat("look", words)  # both are loaded now, and both are near the top: the higher of them
        longer = chat("brief", words)  # more than the first model's context holds
        works = chat("work:best", words)  # the top of the list does not load: the next one answers, and the caller is not told to try again
        fails = chat("dead", words)  # no next one
        dry = caller.get("/loadcoach/pick", params={"model": "look", "pictures": "1"}, headers=jordan).json()
        tried = [(entry["what"], entry["model"]) for entry in events(caller, jordan, kind="request") if entry.get("role") == "work:best"]

    picked = lambda answer: answer.headers.get("x-loadcoach-picked")  # noqa: E731
    assert (picked(sees[0]), picked(sees[1]), picked(warm)) == ("blind", "seeing", "blind") and picked(longer) == "blind"
    assert works.status_code == 200 and picked(works) == "blind" and fails.status_code == 502
    assert (dry["picked"], dry["models"][0]["why_not"]) == ("seeing", "it cannot see pictures")
    assert tried == [("asked", "broken"), ("asked", "blind")] and "exited with code 3" in fails.json()["error"]["message"]  # with nothing else to try, llama-server's own reason is told


def test_a_conversation_started_under_a_role_keeps_the_model_it_got(fake_llama_server):
    models = {name: model(f"{name}.gguf", 1) for name in ("strong", "quick")}
    from chat_clients import gateway as chat_gateway
    with chat_gateway(fake_llama_server, models) as caller:
        jordan = add_user(caller, "jordan", "admin")
        caller.post("/loadcoach/roles/chat", json={"models": {"strong": 2, "quick": 1}}, headers=jordan)
        ref = caller.post("/loadcoach/conversations", json={"content": "hello", "model": "chat:best"}, headers=jordan).json()["id"]
        first = settled(caller, ref, jordan)["messages"][1]
        # The page offers the model of the last answer for the next turn. That is the model that was picked, so the conversation stays with it.
        caller.post(f"/loadcoach/conversations/{ref}/messages", json={"content": "and again", "model": first["model"]}, headers=jordan)
        second = settled(caller, ref, jordan)["messages"][3]
        unknown = caller.post("/loadcoach/conversations", json={"content": "hello", "model": "talk:best"}, headers=jordan).status_code

    assert (first["model"], first["asked_as"], first["because"]) == ("strong", "chat:best", "the top of the list") and first["content"].startswith("You said: hello")
    assert second["model"] == "strong" and "asked_as" not in second and unknown == 404


def test_roles_and_recent_reliability_move_to_front_once_and_evidence_is_published_from_front(tmp_path):
    import sqlite3
    import time

    from typer.testing import CliRunner

    from loadcoach.reliability import Reliability
    from loadcoach.roles import Roles
    from weightroom.app import cli as weightroom_cli

    compute = Roles(str(tmp_path / "compute.db"))
    compute.put("chat", {"models": {"small": 2, "big": 1}})
    kept = Reliability(compute.db)
    kept.record(1, "pat", "small", ("id", "fp", "chat"), 0, "error", now=time.time() - 30 * 86400)  # older than reliability looks back
    kept.record(2, "pat", "small", ("id", "fp", "chat"), 0, "empty")
    (tmp_path / "front").mkdir()
    settings = tmp_path / "front.toml"
    settings.write_text('compute = "http://c:1"\ndata = "front"\n')
    move = lambda: CliRunner().invoke(weightroom_cli, ["--config", str(settings), "roles-from", str(tmp_path / "compute.db")])  # noqa: E731
    moved, again = move(), move()
    front = sqlite3.connect(tmp_path / "front" / "roles.db")

    assert moved.exit_code == 0 and "Took 1 roles and 1 reliability outcomes" in moved.output, moved.output
    assert Roles(str(tmp_path / "front" / "roles.db")).get("chat") == {"models": {"small": 2, "big": 1}}
    assert front.execute("SELECT streak FROM reliability_state").fetchall() == [(2,)]  # the state as it was, with both faults in it
    assert front.execute("SELECT COUNT(*) FROM role_benchmarks").fetchone()[0] > 0  # the benchmark definitions, published from front's own store
    assert "first move only" in str(again.exception)  # never twice
    assert compute.get("chat") and kept.db.execute("SELECT COUNT(*) FROM reliability_outcomes").fetchone()[0] == 2  # compute's copy is the way back
