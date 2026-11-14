"""Checks of outside services: registered on the site, their key in a file here, their models asked for like any other."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from loadcoach.groups import Group
from loadcoach.outside import cost_of
from test_app import GROUPS, add_user, events, fake_llama_server, gateway, hosted_service, in_an_empty_folder, settled  # noqa: F401  (three of them set a check up)

THEIRS = {"data": [{"id": "vendor/small"}, {"id": "vendor/big"}]}  # what the stand-in service says it has
OUTWARD = {**GROUPS, "outward": Group(can=frozenset({"load", "hosted"}))}


@pytest.fixture
def service():
    """A stand-in for an outside service that speaks OpenAI's format. Gives its address.

    It lists its models only for the key `good-key`. Asked to answer, it says which model and
    which key it was asked with, and counts 1000 tokens in and 500 out. It says what the
    answer cost only if the request has a `cost`, as some services do and some do not.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            known = self.headers.get("authorization") == "Bearer good-key"
            self.reply(200 if known else 401, THEIRS if known else {"error": {"message": "no"}})

        def do_POST(self):
            asked = json.loads(self.rfile.read(int(self.headers["content-length"])))
            usage = {"prompt_tokens": 1000, "completion_tokens": 500} | ({"cost": asked["cost"]} if "cost" in asked else {})
            # Two ways an outside service answers 200 and gives nothing: an error inside the answer, and an answer that only says it is still at work.
            wrong = {"error": {"message": "Provider returned error", "code": 429}} if asked["model"] == "vendor/broken" else None
            if not asked.get("stream"):
                return self.reply(200, wrong or {"model": asked["model"], "key": self.headers.get("authorization"), "path": self.path, "choices": [{"message": {"content": "hi"}}], "usage": usage})
            pieces = [wrong] if wrong else [] if asked["model"] == "vendor/silent" else [{"choices": [{"delta": {"reasoning": "thinking it over"}}]}, {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}]
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": OPENROUTER PROCESSING\n\n" + b"".join(b"data: " + json.dumps(piece).encode() + b"\n\n" for piece in pieces) + b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_an_outside_service_is_registered_its_models_chosen_and_its_cost_kept_under_a_ceiling(fake_llama_server, service, tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    with gateway(fake_llama_server, groups=OUTWARD, keys=str(keys)) as caller:
        jordan = add_user(caller, "jordan", "admin")
        olga, pat = add_user(caller, "olga", "outward", by=jordan), add_user(caller, "pat", "plain", by=jordan)
        put = lambda name, who=jordan, new=False, **settings: caller.post(f"/loadcoach/services/{name}" + ("?new" if new else ""), json=settings, headers=who)  # noqa: E731
        ask = lambda model, who=olga, **more: caller.post("/v1/chat/completions", json={"model": model, **more}, headers=who)  # noqa: E731
        offered = lambda who: [one["id"] for one in caller.get("/v1/models", headers=who).json()["data"]]  # noqa: E731
        ended = lambda: events(caller, jordan, kind="request")[-1]  # noqa: E731

        not_theirs = [put("rented", pat, url=service).status_code, caller.get("/loadcoach/services", headers=olga).status_code]  # using outside models is not managing them
        wrong = [put("Rented One", url=service).status_code, put("rented").status_code, put("rented", url=service, ceiling=-1).status_code,
                 put("rented", url=service, models={"small": {"theirs": "vendor/small"}}).status_code, put("rented", url=service, models={"x": {"theirs": "vendor/small", "in": -2}}).status_code]  # fmt: skip

        # A service is registered with its address. Its key is not sent: it is a file here, named after the service.
        added = put("rented", url=service, new=True)
        keyless = caller.get("/loadcoach/services/rented/models", headers=jordan).status_code
        (keys / "rented.key").write_text("good-key\n")  # read when it is needed: no restart
        theirs = caller.get("/loadcoach/services/rented/models", headers=jordan).json()["models"]
        chosen = put("rented", url=service, ceiling=0.02, models={"big-out": {"theirs": "vendor/big", "in": 2.0, "out": 6.0}, "small-out": {"theirs": "vendor/small"}})
        clashes = [put("rented", new=True, url=service).status_code, put("other", url=service, models={"big-out": {"theirs": "x"}}).status_code]

        # Its models are asked for like any other, by those who may send requests out. The service gets its own key and its own name for the model.
        names = offered(olga), offered(pat)
        answer = ask("big-out")
        priced = ended()  # the service did not say what it cost: worked out from the price, 1000 tokens in at 2 and 500 out at 6 a million
        ask("small-out")
        unpriced = ended()  # no price, and the service did not say: nothing is known
        ask("big-out", cost=0.02)
        reported = ended()  # the service says what it cost, and is believed over the price
        listed = caller.get("/loadcoach/services", headers=jordan).json()["services"][0]

        # Everyone together has spent what the service may cost in a day. Its models are refused, and stand in for nothing.
        over = ask("big-out"), ask("small", **{}).status_code
        stood_in = caller.post("/v1/chat/completions", json={"model": "big"}, headers={**olga, "x-loadcoach-offload": "big-out"})  # `big` is not loaded, so it would have gone out
        put("rented", url=service, models=chosen.json()["models"])  # no ceiling any more
        (keys / "rented.key").unlink()
        keyless_ask = ask("small-out")
        removed = caller.delete("/loadcoach/services/rented", headers=jordan).json(), offered(olga), ask("big-out").status_code, caller.delete("/loadcoach/services/rented", headers=jordan).status_code
        whats = [(entry["what"], entry["name"]) for entry in events(caller, jordan, kind="service")]

    assert not_theirs == [403, 403] and wrong == [400, 400, 400, 400, 400] and clashes == [409, 400]
    assert added.status_code == 201 and added.json()["key"] is False and added.json()["key_file"] == str(keys / "rented.key") and keyless == 502
    assert theirs == ["vendor/big", "vendor/small"] and chosen.status_code == 200 and chosen.json()["key"] is True
    assert names == (["big", "small", "big-out", "small-out"], ["big", "small"])
    assert (answer.json()["model"], answer.json()["key"], answer.json()["path"]) == ("vendor/big", "Bearer good-key", "/v1/chat/completions")
    assert (answer.headers["x-loadcoach-hosted"], answer.headers["x-loadcoach-service"]) == ("big-out", "rented")
    assert (priced["cost"], priced["service"], priced["prompt_tokens"]) == (0.005, "rented", 1000) and "cost" not in unpriced and reported["cost"] == 0.02
    assert (listed["name"], listed["spent"], listed["ceiling"], sorted(listed["models"])) == ("rented", 0.025, 0.02, ["big-out", "small-out"])
    assert over[0].status_code == 429 and "has cost 0.03 in the last 24 hours. Its ceiling is 0.02." in over[0].json()["error"]["message"] and over[1] == 200
    assert stood_in.status_code == 200 and "x-loadcoach-hosted" not in stood_in.headers  # it stayed here and was answered by `big` itself
    assert keyless_ask.status_code == 503 and "has no key on the control node" in keyless_ask.json()["error"]["message"]
    assert removed == ({"removed": "rented"}, ["big", "small"], 404, 404)
    assert whats == [("added", "rented"), ("changed", "rented"), ("changed", "rented"), ("removed", "rented")]


def test_what_an_answer_costs_is_worked_out_from_the_models_price_only_when_it_has_one():
    assert cost_of({"prompt_tokens": 10, "answer_tokens": 10}, {"theirs": "x"}) is None and cost_of({"answer_tokens": 1_000_000}, {"out": 3}) == 3


def test_provider_key_replacement_is_private_authorized_and_applies_without_restart(fake_llama_server, service, tmp_path, monkeypatch):
    from chat_clients import gateway as chat_gateway

    keys = tmp_path / "keys"
    with chat_gateway(fake_llama_server, groups=OUTWARD, keys=str(keys)) as caller:
        admin = add_user(caller, "jordan", "admin")
        user = add_user(caller, "olga", "outward", by=admin)
        caller.post("/loadcoach/services/rented", headers=admin,
                    json={"url": service, "models": {"cloud-model": {"theirs": "vendor/small"}}}).raise_for_status()
        endpoint = "/loadcoach/services/rented/key"
        assert caller.post(endpoint, headers=user, json={"key": "not-authorized"}).status_code == 403
        assert not keys.exists()
        assert caller.post("/loadcoach/services/missing/key", headers=admin, json={"key": "not-installed"}).status_code == 404
        saved = caller.post(endpoint, headers=admin, json={"key": "good-key"})
        assert saved.status_code == 200 and saved.json() == {"name": "rented", "key": True}
        assert saved.headers["cache-control"] == "no-store" and "good-key" not in saved.text
        credential = keys / "rented.key"
        assert credential.read_text() == "good-key\n" and credential.stat().st_mode & 0o777 == 0o600
        assert caller.get("/loadcoach/services/rented/models", headers=admin).json()["models"] == ["vendor/big", "vendor/small"]
        for invalid in (None, "", "two words", "bad\r\nheader", "ü", "x" * 8193):
            assert caller.post(endpoint, headers=admin, json={"key": invalid}).status_code == 400
            assert credential.read_text() == "good-key\n"
        with monkeypatch.context() as patch:
            def fail_replace(*args):
                raise PermissionError("fixture")
            patch.setattr("loadcoach.outside.os.replace", fail_replace)
            refused = caller.post(endpoint, headers=admin, json={"key": "unsaved-secret"})
            assert refused.status_code == 500 and "unsaved-secret" not in refused.text
            assert credential.read_text() == "good-key\n" and not list(keys.glob(".cloud-key-*"))
        rotated = caller.post(endpoint, headers=admin, json={"key": "rotated-secret"})
        listing = caller.get("/loadcoach/services", headers=admin)
        assert rotated.status_code == 200 and listing.json()["services"][0]["key"] is True
        audit = events(caller, admin, kind="service")
        assert [entry["what"] for entry in audit] == ["added", "key-changed", "key-changed"]
        for secret in ("good-key", "rotated-secret", "unsaved-secret"):
            assert secret not in rotated.text + listing.text + json.dumps(audit)
        answer = caller.post("/v1/chat/completions", headers=user, json={"model": "cloud-model", "messages": []})
        assert answer.status_code == 200 and answer.json()["key"] == "Bearer rotated-secret"


def test_an_answer_that_says_200_and_gives_nothing_is_not_left_unexplained(fake_llama_server, service, tmp_path):
    from chat_clients import gateway as chat_gateway

    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "rented.key").write_text("good-key")
    with chat_gateway(fake_llama_server, groups=OUTWARD, keys=str(keys)) as caller:
        jordan = add_user(caller, "jordan", "admin")
        caller.post("/loadcoach/services/rented", json={"url": service, "models": {f"{name}-out": {"theirs": f"vendor/{name}"} for name in ("broken", "silent", "big")}}, headers=jordan)
        ask = lambda model, **more: caller.post("/v1/chat/completions", json={"model": model, "messages": [], **more}, headers=jordan)  # noqa: E731
        ended = lambda: events(caller, jordan, kind="request")[-1]  # noqa: E731
        kept = lambda answer: caller.get(f"/loadcoach/requests/{answer.headers['x-loadcoach-request-id']}", headers=jordan).json()["text"]["said"]  # noqa: E731

        # The service says 200 and, inside the answer, that the model's provider failed. That is passed on as it came, and written down as what it is.
        plain = ask("broken-out")
        ends = [(ended()["what"], ended()["status"], ended()["why"])]
        streamed = ask("broken-out", stream=True)
        ends.append((ended()["what"], ended()["status"], ended()["why"]))
        # An answer in which nothing is understood: its beginning is kept as it came, so that there is something to look at.
        silent = ask("silent-out", stream=True)
        ends.append((ended()["what"], ended()["status"], ended().get("why")))
        kept_of = kept(streamed), kept(silent), kept(ask("big-out", stream=True))
        reliability = {row['model']: row for row in caller.get('/loadcoach/reliability', headers=jordan).json()['rows']}
        assert reliability['broken-out']['counts']['error'] == 2
        assert reliability['silent-out']['counts']['empty'] == 1
        assert reliability['big-out']['counts']['success'] == 1
        assert len({row['fingerprint'] for row in reliability.values()}) == 3

        # In a conversation neither is left as a blank answer: it says why, in the service's words where it gave any.
        said = lambda model: settled(caller, caller.post("/loadcoach/conversations", json={"content": "test", "model": model}, headers=jordan).json()["id"], jordan)["messages"][1]  # noqa: E731
        talk = said("broken-out"), said("silent-out"), said("big-out")

    assert plain.status_code == streamed.status_code == silent.status_code == 200 and plain.json()["error"]["code"] == 429
    assert ends == [("failed", 200, "Provider returned error (code 429)"), ("failed", 200, "Provider returned error (code 429)"), ("done", 200, None)]
    assert kept_of[0] == {"error": "Provider returned error (code 429)"} and kept_of[1]["raw"].startswith(": OPENROUTER PROCESSING")
    assert kept_of[2] == {"content": "hi", "reasoning": "thinking it over", "finish_reason": "stop"}  # OpenRouter's own word for a model's thinking is read too
    assert (talk[0]["content"], talk[0]["error"]) == ("", "Provider returned error (code 429)") and "answered with nothing" in talk[1]["error"]
    assert (talk[2]["content"], talk[2]["reasoning"]) == ("hi", "thinking it over") and "error" not in talk[2]
