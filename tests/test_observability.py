"""Ingress identity, trusted propagation, bounded metadata and observation isolation."""

import asyncio
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from loadcoach import observability as obs, statement
from loadcoach.groups import Group
from loadcoach.users import User
from test_app import fake_llama_server  # noqa: F401
from test_postgresql import postgres  # noqa: F401
from test_weightroom import desk  # noqa: F401


class Sink:
    def __init__(self):
        self.rows = []

    def submit(self, value):
        self.rows.append(value)


def person(name="alice"):
    return User(
        name,
        "fixture",
        Group(can=["superuser", "unlogged"]),
        identity=name + "-identity",
    )


def test_ingress_headers_failures_streams_and_quiet_policy():
    sink = Sink()

    async def answer(request):
        if request.path_params.get("status") == "500":
            raise RuntimeError("secret-canary-body")
        obs.identify(person())
        with obs.span("routing"):
            pass
        return JSONResponse(
            {"ok": True}, status_code=int(request.path_params.get("status", 200))
        )

    async def stream(request):
        obs.identify(person())

        async def pieces():
            yield b"data: hello\n\n"
            yield b"data: [DONE]\n\n"

        return StreamingResponse(pieces(), media_type="text/event-stream")

    app = Starlette(
        routes=[
            Route("/stream", stream),
            Route("/v1/chat/completions", answer),
            Route("/{status}", answer),
        ]
    )
    app.add_middleware(obs.Observe, service="fixture", sink=sink)
    with TestClient(app) as client:
        ids = []
        for code in (200, 400, 401, 403, 404, 429, 500, 502, 503):
            r = client.get("/" + str(code), headers={"x-request-id": "client-chosen"})
            assert r.status_code == code
            assert obs.ID.fullmatch(r.headers["x-request-id"])
            ids.append(r.headers["x-request-id"])
        assert len(set(ids)) == len(ids)
        assert "secret-canary-body" not in json.dumps(sink.rows)
        assert client.get("/stream").text.endswith("data: [DONE]\n\n")
        before = len(sink.rows)
        quiet = client.get("/v1/chat/completions", headers={"x-loadcoach-log": "off"})
        assert quiet.status_code == 200 and "x-request-id" in quiet.headers
        assert len(sink.rows) == before
    assert obs.CURRENT.get() is None


def test_request_bound_signed_context_and_concurrent_users(tmp_path):
    sink = Sink()
    private = tmp_path / "key"
    private.write_text("private-test-key")

    async def upstream(request):
        await asyncio.sleep(0)
        return JSONResponse(obs.wire())

    service = Starlette(routes=[Route("/", upstream)])
    service.add_middleware(statement.Statements, key_file=str(private))
    service.add_middleware(obs.Observe, service="compute", sink=sink)

    async def front(request):
        obs.identify(person(request.query_params["who"]))
        origin = obs.wire()["request_id"]
        signed, _ = statement.make(
            private.read_text(),
            "GET",
            "/",
            b"",
            statement.caller_of(person(request.query_params["who"])),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url="http://compute"
        ) as c:
            result = await c.get("/", headers={statement.HEADER: signed})
            assert result.headers["x-request-id"] == origin
            assert result.json()["request_id"] == origin
            refused = await c.get("/altered", headers={statement.HEADER: signed})
            assert refused.status_code == 401
            assert refused.headers["x-request-id"] != origin
        return JSONResponse({"id": origin})

    door = Starlette(routes=[Route("/", front)])
    door.add_middleware(obs.Observe, service="front", sink=sink)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=door), base_url="http://front"
        ) as c:
            replies = await asyncio.gather(
                *(c.get("/?who=" + name) for name in ("alice", "bob"))
            )
            assert len({r.json()["id"] for r in replies}) == 2

    asyncio.run(run())
    compute = [r for r in sink.rows if r["service"] == "compute" and r["owner_id"]]
    assert {r["owner_id"] for r in compute} == {"alice-identity", "bob-identity"}
    for row in compute:
        assert any(
            p["id"] == row["parent_id"] and p["owner_id"] == row["owner_id"]
            for p in sink.rows
        )


def test_w3c_context_budgets_and_private_field_rejection():
    sink = Sink()

    async def endpoint(request):
        obs.identify(person())
        for _ in range(300):
            with obs.span("child"):
                obs.annotate(
                    prompt="private prompt", authorization="private key", attempt=1
                )
        return JSONResponse(obs.wire())

    app = Starlette(routes=[Route("/", endpoint)])
    app.add_middleware(obs.Observe, service="fixture", sink=sink)
    trace = "1" * 32
    with TestClient(app) as c:
        r = c.get("/", headers={"traceparent": "00-" + trace + "-" + "2" * 16 + "-01"})
        assert r.json()["trace_id"] == trace
    assert 1 < len(sink.rows) <= 256 and sink.rows[-1]["truncated"]
    assert (
        sum(len(json.dumps(r, separators=(",", ":")).encode()) for r in sink.rows)
        <= 65536
    )
    assert "private prompt" not in json.dumps(
        sink.rows
    ) and "private key" not in json.dumps(sink.rows)


def test_optional_storage_is_idempotent_scoped_indexed_and_separate_from_audit(
    tmp_path,
):
    from loadcoach.log import Log
    from loadcoach.observation_store import ingest, search, metrics, prune
    import time

    log = Log(str(tmp_path / "log.db"))
    record = {
        "request_id": "a" * 32,
        "trace_id": "b" * 32,
        "id": "c" * 16,
        "service": "loadcoach",
        "operation": "http",
        "owner_id": "alice-identity",
        "at": time.time(),
        "seconds": 0.03,
        "status": 200,
        "outcome": "success",
        "finished": True,
        "ingress": False,
        "prompt": "secret-canary",
        "authorization": "secret-key",
    }
    with log.db:
        assert (
            ingest(
                log.db,
                [{**record, "finished": False, "outcome": "underway"}],
                "compute",
            )
            == 1
        )
        assert ingest(log.db, [record], "compute") == 1
        assert ingest(log.db, [record], "compute") == 0
        assert (
            ingest(log.db, [{**record, "id": "d" * 16, "seconds": 0.07}], "remote_cpu")
            == 1
        )
    found = search(log.db, "alice-identity", {"request_id": "a" * 32})
    assert found["total"] == 2 and not found["truncated"]
    assert search(log.db, "bob-identity", {"request_id": "a" * 32})["total"] == 0
    assert "secret-canary" not in json.dumps(found) and "secret-key" not in json.dumps(
        found
    )
    group = metrics(log.db, None)["groups"][0]
    assert (
        group["count"] == 2 and group["p95"] == 0.1
    )  # merged histogram, not mean node percentile
    audit = log.add("test", "audit", "alice")
    prune(log.db, now=time.time() + 8 * 86400)
    assert search(log.db, None, {"since": 0})["total"] == 0
    assert (
        log.about(audit)
        and metrics(log.db, None, now=time.time() + 8 * 86400, seconds=9 * 86400)[
            "groups"
        ]
    )
    log.db.close()


def test_background_writer_bounds_failure_and_no_log_retention(tmp_path, monkeypatch):
    from loadcoach.log import Log
    from loadcoach.observation_store import Store, MAX_BYTES, MAX_RECORDS
    import time

    log = Log(str(tmp_path / "log.db"))
    sink = Store(log.db.database)
    root = obs.Root()
    context = obs.Context(root, "fixture", "http", sink)
    token = obs.CURRENT.set(context)
    try:
        obs.identify(person())
        obs.finish(context)
        assert sink.flush()
        assert (
            log.db.execute(
                "SELECT COUNT(*) FROM observation_spans WHERE finished=1"
            ).fetchone()[0]
            == 1
        )
        before = log.db.execute("SELECT COUNT(*) FROM observation_spans").fetchone()[0]
        root.quiet = True
        obs.finish(context)
        assert sink.flush()
        assert (
            log.db.execute("SELECT COUNT(*) FROM observation_spans").fetchone()[0]
            == before
        )
    finally:
        obs.CURRENT.reset(token)
        sink.close()
    blocked = Store(log.db.database)
    monkeypatch.setattr(blocked, "run", lambda: time.sleep(0.2))
    for _ in range(MAX_RECORDS + 100):
        blocked.submit(
            {
                "outcome": "error",
                "trace_id": "a" * 32,
                "id": "b" * 16,
                "padding": "x" * 2048,
            }
        )
    health = blocked.health()
    assert (
        health["pending"] <= MAX_RECORDS
        and health["bytes"] <= MAX_BYTES
        and health["dropped"]["queue_full"] > 0
    )
    began = time.monotonic()
    blocked.close()
    assert time.monotonic() - began < 2.1
    log.db.close()


def test_observation_api_search_export_and_receipt_enforce_owner(tmp_path):
    from loadcoach.log import Log
    from loadcoach.users import Users
    from loadcoach.observation_store import ingest
    from turnstile.observations import routes
    import time

    users = Users(str(tmp_path / "users.db"), {"plain": Group()})
    users.add("alice", "plain")
    users.add("bob", "plain")
    key = users.new_key("alice")
    log = Log(str(tmp_path / "log.db"))
    with log.db:
        ingest(
            log.db,
            [
                {
                    "request_id": "a" * 32,
                    "trace_id": "b" * 32,
                    "id": "c" * 16,
                    "service": "loadcoach",
                    "operation": "http",
                    "owner_id": users.identity("bob"),
                    "at": time.time(),
                    "seconds": 1,
                    "outcome": "error",
                    "error_code": "model_failed",
                }
            ],
            "compute",
        )
    app = Starlette(routes=routes(log, users.by_key))
    with TestClient(app) as client:
        auth = {"authorization": "Bearer " + key}
        response = client.get(
            "/loadcoach/observations?request_id=" + "a" * 32, headers=auth
        )
        assert (
            response.json()["total"] == 0
            and response.headers["cache-control"] == "no-store"
        )
        assert (
            client.get("/loadcoach/observations?format=jsonl", headers=auth).text == ""
        )
        assert (
            client.get(
                "/loadcoach/observations?format=receipt&request_id=" + "a" * 32,
                headers=auth,
            ).json()["attempts"]
            == []
        )
        assert (
            client.get("/loadcoach/observations?since=nan", headers=auth).status_code
            == 400
        )
        assert client.get("/loadcoach/observations").status_code == 401
    users.db.close()
    log.db.close()


def test_alert_freshness_loss_recovery_and_sustained_thresholds():
    from weightroom.alerts import observation_conditions

    report = {
        "local": {"process": "p", "dropped": {}},
        "sources": [],
        "groups": [
            {
                "service": "turnstile",
                "operation": "ingress",
                "metric": "seconds",
                "count": 100,
                "outcomes": {"error": 20},
                "p95": 5,
            }
        ],
    }
    conditions, previous = observation_conditions(
        report, now=100, error_percent=10, hold_seconds=60
    )
    assert (
        next(c for c in conditions if c["key"].endswith("error-rate"))["active"] is None
    )
    conditions, previous = observation_conditions(
        report, previous, now=161, error_percent=10, hold_seconds=60
    )
    assert (
        next(c for c in conditions if c["key"].endswith("error-rate"))["active"] is True
    )
    report["local"]["dropped"] = {"writer_error": 1}
    conditions, previous = observation_conditions(report, previous, now=162)
    assert next(c for c in conditions if c["key"].endswith(":loss"))["active"] is True
    conditions, previous = observation_conditions(report, previous, now=223)
    assert next(c for c in conditions if c["key"].endswith(":loss"))["active"] is False
    assert observation_conditions(None, previous)[0] is None


def test_durable_job_resume_uses_stable_trace_and_fresh_attempt_ids():
    sink = Sink()
    parent = obs.Context(obs.Root(), "fixture", "http", sink)
    token = obs.CURRENT.set(parent)
    try:
        obs.identify(person())
        with obs.job("workflow", "saved-job", person()) as first:
            one = obs.wire()
        with obs.job("workflow", "saved-job", person()) as second:
            two = obs.wire()
        assert (
            one["trace_id"] == two["trace_id"]
            and one["request_id"] != two["request_id"]
        )
        assert obs.CURRENT.get() is parent
    finally:
        obs.CURRENT.reset(token)


def test_postgresql_observation_forwarding_metrics_and_privacy(postgres):
    from loadcoach.log import Log
    from loadcoach.observation_store import receive, search, metrics, Store, acknowledge
    import time

    log = Log(postgres)
    record = {
        "request_id": "a" * 32,
        "trace_id": "b" * 32,
        "id": "c" * 16,
        "service": "loadcoach",
        "operation": "http",
        "owner_id": "owner",
        "at": time.time(),
        "seconds": 0.5,
        "outcome": "success",
        "finished": True,
        "ingress": False,
    }
    assert receive(postgres, [record], "compute") == 1
    assert receive(postgres, [record], "compute") == 0
    assert search(log.db, "owner", {"request_id": "a" * 32})["total"] == 1
    assert metrics(log.db, "owner")["groups"][0]["count"] == 1
    sink = Store(postgres)
    sink.submit({**record, "id": "d" * 16})
    assert sink.flush()
    assert search(log.db, "owner", {"request_id": "a" * 32})["total"] == 2
    sink.close()
    assert acknowledge(
        postgres, [{"id": record["trace_id"] + record["id"], "finished": True}]
    )
    log.db.close()


def test_trace_search_browser_metadata_exports_and_mobile_layout(desk):
    import time
    from loadcoach.log import public_number
    from loadcoach.observation_store import ingest
    from test_weightroom import sign_in
    from test_console_browser import browser_context
    from playwright.sync_api import sync_playwright, expect

    server, switch, users = desk
    log = switch.front.state.log
    ingress, trace = "a" * 32, "b" * 32
    number = log.add(
        "request",
        "asked",
        "pat",
        "198.51.100.4",
        "fixture-model",
        request_id=ingress,
        trace_id=trace,
    )
    log.add("request", "failed", "pat", of=number)
    public = public_number(None, number)
    base = {
        "request_id": ingress,
        "trace_id": trace,
        "id": "c" * 16,
        "parent_id": "",
        "service": "turnstile",
        "operation": "http",
        "owner_id": users.identity("pat"),
        "at": time.time(),
        "seconds": 0.1,
        "status": 502,
        "outcome": "error",
        "error_code": "model_failed",
        "finished": True,
        "ingress": True,
        "request_number": public,
    }
    with log.db:
        ingest(
            log.db,
            [
                base,
                {
                    **base,
                    "id": "d" * 16,
                    "parent_id": "c" * 16,
                    "operation": "dispatch",
                    "ingress": False,
                },
            ],
            "front",
        )
    sign_in(server, "jordan")
    with sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on("pageerror", lambda value: errors.append(str(value)))
        page.goto("http://console.test/requests?request_id=" + ingress)
        expect(page.locator("#observation-spans tbody tr")).to_have_count(2)
        expect(page.locator("#observation-spans")).to_contain_text("model_failed")
        expect(page.locator("#observation-spans")).to_contain_text("dispatch")
        assert (
            ingress
            in server.get("/requests?request_id=" + ingress + "&export=receipt").text
        )
        assert (
            server.get("/requests?request_id=" + ingress + "&export=jsonl")
            .headers["content-type"]
            .startswith("application/x-ndjson")
        )
        page.screenshot(path="/tmp/suite-observability-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path="/tmp/suite-observability-mobile.png", full_page=True)
        assert not errors
        browser.close()


def test_optional_disk_failure_is_bounded_and_does_not_fail_response():
    from loadcoach.observation_store import Store
    import time

    class Unavailable:
        engine = "sqlite"

        def connect(self, **kwargs):
            raise OSError("secret-canary-disk-full")

    sink = Store(Unavailable())

    async def answer(request):
        obs.identify(person())
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", answer)])
    app.add_middleware(obs.Observe, service="fixture", sink=sink)
    with TestClient(app) as client:
        for _ in range(100):
            assert client.get("/").status_code == 200
        assert sink.flush()
        health = sink.health()
        assert health["pending"] == 0 and health["active"] == 0
        assert health["dropped"]["writer_error"] > 0
    began = time.monotonic()
    sink.close()
    assert time.monotonic() - began < 2.1


def test_configured_application_identity_cannot_be_replaced_by_client_label():
    sink = Sink()
    from dataclasses import replace

    user = replace(person(), via="configured-app")
    current = obs.Context(
        obs.Root(application="asserted-label"), "fixture", "http", sink
    )
    token = obs.CURRENT.set(current)
    try:
        obs.identify(user)
        obs.finish(current)
    finally:
        obs.CURRENT.reset(token)
    assert sink.rows[-1]["application_id"] == "configured-app"
    assert sink.rows[-1]["application"] == "asserted-label"


def test_fallback_and_real_tool_process_have_explicit_attempts(
    tmp_path, fake_llama_server
):
    from test_app import gateway, model, add_user
    from test_toollocker import door, ask, said
    import sqlite3

    trace = "9" * 32
    context = {"traceparent": "00-" + trace + "-" + "8" * 16 + "-01"}
    models = {
        "broken": model(str(tmp_path / "broken.gguf"), 1, fail=True),
        "good": model(str(tmp_path / "good.gguf"), 1),
    }
    with gateway(
        fake_llama_server, models, database=str(tmp_path / "native.db")
    ) as client:
        auth = add_user(client, "jordan", "admin")
        assert (
            client.post(
                "/loadcoach/roles/work",
                headers=auth,
                json={"models": {"broken": 3, "good": 1}},
            ).status_code
            == 201
        )
        reply = client.post(
            "/v1/chat/completions",
            headers={**auth, **context},
            json={
                "model": "work:best",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert (
            reply.status_code == 200 and reply.headers["x-loadcoach-picked"] == "good"
        )
        ingress = reply.headers["x-request-id"]
        assert client.front.state.observations.flush()
    with door(tmp_path) as client:
        result = ask(
            client,
            "tools/call",
            {"name": "fake__echo", "arguments": {"text": "secret-tool-canary"}},
            {**auth, **context},
        )
        assert said(result) == "secret-tool-canary"
        assert client.compute.state.observations.flush()
    rows = []
    for name in ("front/accounts.db", "accounts.db", "native.db", "users.db"):
        with sqlite3.connect(tmp_path / name) as connection:
            rows += [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT data FROM observation_spans WHERE trace_id=?", (trace,)
                )
            ]
    attempts = sorted(
        [r for r in rows if r["operation"] == "model_attempt"],
        key=lambda r: r["attempt"],
    )
    assert [r["attempt"] for r in attempts] == [1, 2]
    assert [r["outcome"] for r in attempts] == ["error", "success"]
    assert len({r["request_number"] for r in attempts}) == 2
    assert all(r["request_id"] == ingress and r["parent_id"] for r in attempts)
    assert any(r["operation"] == "tool_execution" and r["tool_executed"] for r in rows)
    assert "secret-tool-canary" not in json.dumps(rows)


def test_disconnect_after_completed_handoff_is_not_cancellation():
    sink = Sink()

    async def app(scope, receive, send):
        obs.identify(person())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        await asyncio.sleep(0.02)
        await receive()

    async def run():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        await obs.Observe(app, "fixture", sink)(
            {"type": "http", "path": "/", "headers": [], "method": "GET"}, receive, send
        )

    asyncio.run(run())
    assert sink.rows[-1]["outcome"] == "success"
    assert sink.rows[-1]["seconds"] < 0.02


def test_operator_recording_switch_preserves_ids_and_required_audit(
    tmp_path, monkeypatch
):
    from loadcoach.log import Log

    monkeypatch.setenv("SUITE_OBSERVATION_RECORDING", "off")
    log = Log(str(tmp_path / "log.db"))

    async def answer(request):
        obs.identify(person())
        log.add("security", "checked", "alice", must=True)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", answer)])
    assert obs.instrument(app, "fixture", log) is None
    with TestClient(app) as client:
        reply = client.get("/")
        assert reply.status_code == 200 and obs.ID.fullmatch(
            reply.headers["x-request-id"]
        )
    assert log.db.execute("SELECT COUNT(*) FROM observation_spans").fetchone()[0] == 0
    assert log.read(kind="security")[0]["what"] == "checked"
    log.db.close()


def test_batched_metric_rollups_preserve_counts_replays_and_denominators(tmp_path):
    from loadcoach.log import Log
    from loadcoach.observation_store import ingest, metrics
    import time

    now = time.time()
    base = {
        "request_id": "a" * 32,
        "trace_id": "b" * 32,
        "service": "turnstile",
        "operation": "dispatch",
        "owner_id": "alice",
        "at": now,
        "seconds": 0.01,
        "outcome": "success",
        "finished": True,
    }
    rows = [{**base, "id": format(index + 1, "016x")} for index in range(100)]
    log = Log(str(tmp_path / "log.db"))
    with log.db:
        assert ingest(log.db, rows, "front") == 100
        assert ingest(log.db, rows, "front") == 0
        assert ingest(log.db, [{**r, "finished": False} for r in rows], "front") == 0
    report = metrics(log.db, "alice", now=now)
    seconds = next(g for g in report["groups"] if g["metric"] == "seconds")
    dispatches = next(
        g for g in report["groups"] if g["metric"] == "inference_dispatches"
    )
    assert seconds["count"] == dispatches["total"] == 100
    assert abs(seconds["total"] - 1) < 1e-9
    assert seconds["outcomes"] == {"success": 100}
    assert seconds["p95"] == 0.01 and seconds["cancelled_percent"] == 0
    assert metrics(log.db, "bob", now=now)["groups"] == []
    log.db.close()


def test_skewed_clocks_and_missing_endings_require_process_evidence(tmp_path):
    from loadcoach.log import Log
    from loadcoach.observation_store import ingest, search, tree
    import time

    now = time.time()
    parent = {
        "request_id": "a" * 32,
        "trace_id": "b" * 32,
        "id": "c" * 16,
        "service": "loadcoach",
        "operation": "http",
        "owner_id": "alice",
        "process": "old-process",
        "at": now - 86400,
        "seconds": 0,
        "outcome": "underway",
        "finished": False,
    }
    child = {
        **parent,
        "id": "d" * 16,
        "parent_id": parent["id"],
        "at": now - 2 * 86400,
        "seconds": 0.25,
        "outcome": "success",
        "finished": True,
    }
    log = Log(str(tmp_path / "log.db"))
    with log.db:
        ingest(log.db, [child, parent], "compute")
        log.db.execute(
            "INSERT INTO observation_sources VALUES (?,?,?)",
            ("compute", now, json.dumps({"process": "old-process", "recording": True})),
        )
    filters = {"request_id": parent["request_id"]}
    rows = tree(search(log.db, "alice", filters)["spans"])
    assert [r["id"] for r in rows] == [parent["id"], child["id"]]
    assert rows[0]["outcome"] == "underway" and rows[1]["seconds"] == 0.25
    with log.db:
        log.db.execute(
            "INSERT INTO observation_sources VALUES (?,?,?)",
            (
                "compute:loadcoach",
                now,
                json.dumps({"process": "replacement", "service": "loadcoach"}),
            ),
        )
    rows = tree(search(log.db, "alice", filters)["spans"])
    assert rows[0]["outcome"] == "unknown" and "No ending" in rows[0]["uncertainty"]
    assert tree([rows[1]])[0]["parent_unavailable"]
    log.db.close()


def test_retention_failure_is_not_reported_as_lost_committed_spans(
    tmp_path, monkeypatch
):
    from loadcoach.log import Log
    from loadcoach.observation_store import Store
    import time

    observed = []

    def unavailable(connection, **_):
        observed.append(obs.CURRENT.get())
        raise OSError("secret-retention-canary")

    monkeypatch.setattr("loadcoach.observation_store.prune", unavailable)
    log = Log(str(tmp_path / "log.db"))
    store = Store(log.db.database)
    store.last_prune = time.monotonic() - 3601  # Retention is due even on a newly booted host.
    context = obs.Context(obs.Root(resolved=True), "fixture", "http")
    token = obs.CURRENT.set(context)
    try:
        store.submit(
            {
                "request_id": "a" * 32,
                "trace_id": "b" * 32,
                "id": "c" * 16,
                "service": "fixture",
                "operation": "http",
                "owner_id": "alice",
                "at": time.time(),
                "seconds": 0.01,
                "outcome": "success",
            }
        )
        assert store.flush()
    finally:
        obs.CURRENT.reset(token)
        store.close()
    assert observed == [None]
    assert store.health()["written"] == 1 and store.health()["dropped"] == {}
    assert log.db.execute("SELECT COUNT(*) FROM observation_spans").fetchone()[0] == 1
    log.db.close()


def test_acknowledgements_commit_once_and_preserve_late_completions(tmp_path):
    from loadcoach.log import Log
    from loadcoach.observation_store import acknowledge, ingest
    from types import SimpleNamespace
    import time

    log = Log(str(tmp_path / "log.db"))
    base = {
        "request_id": "a" * 32,
        "trace_id": "b" * 32,
        "service": "loadcoach",
        "operation": "http",
        "owner_id": "alice",
        "at": time.time(),
        "seconds": 0.01,
        "outcome": "success",
        "finished": True,
    }
    rows = [{**base, "id": format(i + 1, "016x")} for i in range(4)]
    log.db.execute("BEGIN IMMEDIATE")
    ingest(log.db, rows, "compute", forwarded=True)
    log.db.commit()
    trace = []

    def connect(**kwargs):
        connection = log.db.database.connect(isolation_level=None, **kwargs)
        connection.raw.set_trace_callback(trace.append)
        return connection

    source = SimpleNamespace(engine="sqlite", connect=connect)
    stale = [{"id": row["trace_id"] + row["id"], "finished": False} for row in rows]
    assert acknowledge(source, stale)
    assert (
        log.db.execute(
            "SELECT COUNT(*) FROM observation_spans WHERE sent=0"
        ).fetchone()[0]
        == 4
    )
    trace.clear()
    current = [{**row, "finished": True} for row in stale]
    assert acknowledge(source, current)
    assert sum(statement == "COMMIT" for statement in trace) == 1
    assert (
        log.db.execute(
            "SELECT COUNT(*) FROM observation_spans WHERE sent=1"
        ).fetchone()[0]
        == 4
    )
    log.db.execute("UPDATE observation_spans SET sent=0")
    log.db.execute(
        "CREATE TRIGGER reject_ack BEFORE UPDATE ON observation_spans WHEN NEW.id='"
        + current[-1]["id"]
        + "' BEGIN SELECT RAISE(ABORT,'secret-ack-canary'); END"
    )
    assert not acknowledge(source, current)
    assert (
        log.db.execute(
            "SELECT COUNT(*) FROM observation_spans WHERE sent=0"
        ).fetchone()[0]
        == 4
    )
    log.db.close()


def test_client_parent_headers_cannot_establish_cross_owner_relationships():
    from loadcoach.observation_store import tree

    parent = {
        "trace_id": "a" * 32,
        "id": "b" * 16,
        "owner_id": "bob",
        "at": 1,
        "ingress": True,
    }
    child = {
        "trace_id": "a" * 32,
        "id": "c" * 16,
        "parent_id": parent["id"],
        "owner_id": "alice",
        "at": 2,
        "ingress": True,
    }
    rows = tree([parent, child])
    assert rows[1]["depth"] == 0 and rows[1]["parent_unavailable"]
    child.update(owner_id="bob", ingress=False)
    rows = tree([parent, child])
    assert rows[1]["depth"] == 1 and not rows[1]["parent_unavailable"]


def test_repeated_ingress_releases_task_context_and_bounded_writer_memory(tmp_path):
    from loadcoach.log import Log
    from loadcoach.observation_store import Store
    import tracemalloc
    import gc

    log = Log(str(tmp_path / "log.db"))
    store = Store(log.db.database)

    async def app(scope, receive, send):
        obs.identify(person())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run():
        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            pass

        middleware = obs.Observe(app, "fixture", store)
        sizes = []
        for phase in range(3):
            for _ in range(500):
                await middleware(
                    {"type": "http", "method": "GET", "path": "/", "headers": []},
                    receive,
                    send,
                )
            assert await asyncio.to_thread(store.flush)
            assert obs.CURRENT.get() is None and store.health()["active"] == 0
            gc.collect()
            sizes.append(tracemalloc.get_traced_memory()[0])
        assert sizes[-1] - sizes[0] < 512 * 1024
        assert store.health()["high_water_bytes"] <= 8 * 1024 * 1024

    tracemalloc.start()
    try:
        asyncio.run(run())
    finally:
        tracemalloc.stop()
        store.close()
        log.db.close()


def test_optional_wal_spool_does_not_weaken_required_audit_connection(tmp_path):
    from loadcoach.log import Log
    from loadcoach.observation_store import optional_spool, Store
    import time

    log = Log(str(tmp_path / "log.db"))
    required = log.db.execute("PRAGMA synchronous").fetchone()[0]
    connection = log.db.database.connect()
    assert optional_spool(connection)
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert log.db.execute("PRAGMA synchronous").fetchone()[0] == required == 2
    connection.close()
    store = Store(log.db.database, "compute", forwarded=True)
    store.submit(
        {
            "request_id": "a" * 32,
            "trace_id": "b" * 32,
            "id": "c" * 16,
            "service": "loadcoach",
            "operation": "http",
            "owner_id": "alice",
            "at": time.time(),
            "seconds": 0.01,
            "outcome": "success",
        }
    )
    assert store.flush()
    assert log.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    log.add("security", "required", "alice", must=True)
    assert (
        log.verify()[1] is None and log.read(kind="security")[0]["what"] == "required"
    )
    store.close()
    log.db.close()


def test_optional_write_failure_rolls_back_spans_and_metrics_on_autocommit_sources(
    tmp_path,
):
    from loadcoach.log import Log
    from loadcoach.observation_store import Store
    from types import SimpleNamespace
    import time

    log = Log(str(tmp_path / "log.db"))
    log.db.execute(
        "CREATE TRIGGER reject_metric BEFORE INSERT ON observation_metrics BEGIN SELECT RAISE(ABORT,'secret-metric-canary'); END"
    )
    source = SimpleNamespace(
        engine="sqlite",
        connect=lambda **kwargs: log.db.database.connect(
            isolation_level=None, **kwargs
        ),
    )
    store = Store(source)
    store.submit(
        {
            "request_id": "a" * 32,
            "trace_id": "b" * 32,
            "id": "c" * 16,
            "service": "fixture",
            "operation": "http",
            "owner_id": "alice",
            "at": time.time(),
            "seconds": 0.01,
            "outcome": "success",
        }
    )
    assert store.flush()
    assert store.health()["dropped"]["writer_error"] == 1
    assert log.db.execute("SELECT COUNT(*) FROM observation_spans").fetchone()[0] == 0
    assert log.db.execute("SELECT COUNT(*) FROM observation_metrics").fetchone()[0] == 0
    log.add("security", "still-required", "alice", must=True)
    assert log.verify()[1] is None
    store.close()
    log.db.close()


def test_stream_failure_does_not_leak_exception_secrets_to_outer_journals():
    import traceback

    sink = Sink()

    async def app(scope, receive, send):
        obs.identify(person())
        obs.annotate(
            outcome="success"
        )  # The backend completed before response delivery broke.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {"type": "http.response.body", "body": b"partial", "more_body": True}
        )
        raise ValueError("secret-stream-credential-canary")

    async def run():
        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            pass

        with pytest.raises(RuntimeError, match="request_id="):
            try:
                await obs.Observe(app, "fixture", sink)(
                    {"type": "http", "path": "/", "method": "GET", "headers": []},
                    receive,
                    send,
                )
            except Exception:
                assert "secret-stream-credential-canary" not in traceback.format_exc()
                raise

    asyncio.run(run())
    assert (
        sink.rows[-1]["outcome"] == "error"
        and sink.rows[-1]["error_code"] == "ValueError"
    )


def test_known_admission_refusals_and_unknown_latency_are_not_false_failures_or_recovery():
    from weightroom.alerts import observation_conditions

    sink = Sink()

    async def declined(request):
        obs.identify(person())
        obs.annotate(outcome="refused", error_code="")
        return JSONResponse({"error": "admission"}, status_code=503)

    app = Starlette(routes=[Route("/", declined)])
    app.add_middleware(obs.Observe, service="fixture", sink=sink)
    with TestClient(app) as client:
        assert client.get("/").status_code == 503
    assert (
        sink.rows[-1]["outcome"] == "refused"
        and sink.rows[-1]["error_code"] == "http_503"
    )
    report = {
        "local": {"process": "p", "dropped": {}},
        "sources": [],
        "groups": [
            {
                "service": "turnstile",
                "operation": "ingress",
                "metric": "seconds",
                "count": 100,
                "outcomes": {"success": 100},
                "p95": None,
            }
        ],
    }
    conditions, previous = observation_conditions(report, now=100, p95_seconds=1)
    conditions, _ = observation_conditions(report, previous, now=200, p95_seconds=1)
    assert (
        next(c for c in conditions if c["key"].endswith(":latency"))["active"] is None
    )


def test_durable_execution_links_back_to_ingress_and_inherits_no_log_policy():
    sink = Sink()
    parent = obs.Context(obs.Root(), "weightroom", "http", sink)
    token = obs.CURRENT.set(parent)
    try:
        obs.identify(person())
        with obs.job("workflow", "durable-one", person()):
            execution = obs.wire()
        link = next(row for row in sink.rows if row["operation"] == "job_link")
        assert link["request_id"] == parent.root.request_id
        assert link["job_trace_id"] == execution["trace_id"]
        assert execution["request_id"] != parent.root.request_id
        parent.root.quiet = True
        before = len(sink.rows)
        with obs.job("workflow", "durable-two", person()):
            assert obs.wire()["quiet"] is True
        assert len(sink.rows) == before
    finally:
        obs.CURRENT.reset(token)


def test_span_policy_samples_successful_traces_keeps_errors_and_bounds_the_table(tmp_path):
    """Front's policy limits stored spans; metrics still count every finished span."""
    from loadcoach.log import Log
    from loadcoach.observation_store import ingest, metrics, prune, set_policy, policy
    import time

    now = time.time()
    log = Log(str(tmp_path / "log.db"))
    with pytest.raises(ValueError):
        set_policy(log.db, 0, 100, 0)
    with log.db:
        set_policy(log.db, 3, 0, 0)
    assert policy(log.db) == {"keep_days": 3, "success_percent": 0, "max_spans": 0, "quiet_routes": list(obs.POLLS)}
    base = {"request_id": "a" * 32, "service": "turnstile", "operation": "dispatch", "owner_id": "alice",
            "at": now, "seconds": 0.01, "finished": True}
    rows = [{**base, "trace_id": format(index, "032x"), "id": format(index + 1, "016x"),
             "outcome": "error" if index % 10 == 0 else "success"} for index in range(100)]
    with log.db:
        ingest(log.db, rows, "front")
    assert log.db.execute("SELECT COUNT(*), MIN(outcome), MAX(outcome) FROM observation_spans").fetchone() == (10, "error", "error")
    assert sum(g["count"] for g in metrics(log.db, "alice", now=now)["groups"] if g["metric"] == "seconds") == 100
    with log.db:
        set_policy(log.db, 3, 100, 4)
        for row in rows:  # one batch at a time, as the writers send them; the cap counts by arrival
            ingest(log.db, [{**row, "id": format(int(row["id"], 16) + 1000, "016x")}], "front")
            time.sleep(0.001)
    prune(log.db, now=now + 1)
    assert log.db.execute("SELECT COUNT(*) FROM observation_spans").fetchone()[0] == 4
    prune(log.db, now=now + 4 * 86400)
    assert log.db.execute("SELECT COUNT(*) FROM observation_spans").fetchone()[0] == 0


def test_a_forwarding_node_lets_go_of_acknowledged_spans_after_an_hour(tmp_path):
    from loadcoach.log import Log
    from loadcoach.observation_store import ingest, prune
    import time

    now = time.time()
    log = Log(str(tmp_path / "log.db"), chained=False)
    record = {"request_id": "a" * 32, "trace_id": "b" * 32, "service": "loadcoach", "operation": "http",
              "owner_id": "alice", "at": now, "seconds": 0.01, "outcome": "success", "finished": True}
    with log.db:
        ingest(log.db, [{**record, "id": "c" * 16}, {**record, "id": "d" * 16}], "compute", forwarded=True)
        log.db.execute("UPDATE observation_spans SET sent=1 WHERE id LIKE ?", ("%" + "c" * 16,))
    prune(log.db, now=now + 3601, forwarded=True)
    assert [row[0][-16:] for row in log.db.execute("SELECT id FROM observation_spans")] == ["d" * 16]  # not yet sent: kept


def test_routine_polls_are_swept_but_polls_inside_real_work_and_failures_are_kept(tmp_path):
    from types import SimpleNamespace
    from loadcoach.log import Log
    from loadcoach.observation_store import ingest, metrics, prune, SETTLE
    import time

    catch_all = SimpleNamespace(path="/loadcoach/{path}")
    assert obs.route_of({"route": catch_all, "path": "/loadcoach/status"}) == "/loadcoach/status"
    assert obs.route_of({"route": catch_all, "path": "/loadcoach/peek/42"}) == "/loadcoach/{path}"  # no IDs kept
    other_ports = SimpleNamespace(path="/{path:path}")  # turnstile's Spotter and Voice ports
    assert obs.route_of({"route": other_ports, "path": "/spotter/now"}) == "/spotter/now"
    assert obs.route_of({"route": other_ports, "path": "/spotter/history"}) == "/{path:path}"

    now = time.time()
    log = Log(str(tmp_path / "log.db"))

    def span(trace, number, operation="http", outcome="success", ingress=False, route="", service="turnstile"):
        return {"request_id": "a" * 32, "trace_id": trace * 32, "id": format(number, "016x"), "service": service,
                "operation": operation, "owner_id": "alice", "at": now, "seconds": 0.01, "outcome": outcome,
                "finished": True, "ingress": ingress, "route": route}

    with log.db:
        # A poll through turnstile: each service starts its own ingress span, both on poll routes.
        ingest(log.db, [span("1", 1, "authentication"), span("1", 2, ingress=True, route="/loadcoach/status"),
                        span("1", 3, ingress=True, route="/loadcoach/status", service="loadcoach")], "front")
        # A chat request whose routing looks up facts: LoadCoach's facts span is an ingress too, turnstile's is not a poll.
        ingest(log.db, [span("2", 4, ingress=True, route="/loadcoach/facts", service="loadcoach"),
                        span("2", 5, ingress=True, route="/v1/chat/completions")], "front")
        # A poll that failed somewhere: kept.
        ingest(log.db, [span("3", 6, "transport_headers", outcome="error"), span("3", 7, ingress=True, route="/v1/models")], "front")
    prune(log.db, now=now + 1)  # too recent to judge: nothing is swept yet
    assert log.db.execute("SELECT COUNT(DISTINCT trace_id) FROM observation_spans").fetchone()[0] == 3
    prune(log.db, now=now + SETTLE + 1)
    assert {row[0] for row in log.db.execute("SELECT trace_id FROM observation_spans")} == {"2" * 32, "3" * 32}
    with log.db:  # compute's span of the swept poll arrives late: not stored, still counted
        ingest(log.db, [span("1", 8, "queue", service="loadcoach")], "front")
    assert log.db.execute("SELECT COUNT(*) FROM observation_spans WHERE trace_id=?", ("1" * 32,)).fetchone()[0] == 0
    assert sum(g["count"] for g in metrics(log.db, "alice", now=now)["groups"] if g["metric"] == "seconds") == 8
    prune(log.db, now=now + 2 * 86400)
    assert log.db.execute("SELECT COUNT(*) FROM observation_quiet").fetchone()[0] == 0
