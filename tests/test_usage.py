"""Usage totals stay consistent across filters, timezones, missing counts and access scopes."""

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from loadcoach.log import Log
from loadcoach.outside import Services
from loadcoach.usage import report
from loadcoach.users import Users
from front_helpers import turnstile_to
from test_app import add_user, fake_llama_server, gateway, in_an_empty_folder
from test_weightroom import GROUPS, desk, sign_in


def recorded(log, monkeypatch, at, user, model="small", what="done", **detail):
    """Write real chained records at a controlled instant, without editing the event chain."""
    with monkeypatch.context() as clock:
        clock.setattr("loadcoach.log.time.time", lambda: datetime.fromisoformat(at).timestamp())
        return log.add("request", what, user, model=model, **detail)


def test_usage_retained_endings_filters_groups_and_missing_counts(tmp_path, monkeypatch):
    database = str(tmp_path / "usage.db")
    users = Users(database, GROUPS)
    users.add("pat", "plain")
    users.add("jordan", "admin")
    log = Log(database)
    recorded(log, monkeypatch, "2026-10-01T08:00:00+00:00", "pat", prompt_tokens=100, answer_tokens=10, group="plain")
    recorded(log, monkeypatch, "2026-10-01T09:00:00+00:00", "pat", what="peeked", prompt_tokens=9999)
    recorded(log, monkeypatch, "2026-10-01T10:00:00+00:00", "pat", what="asked")
    recorded(log, monkeypatch, "2026-10-03T08:00:00+00:00", "pat", model="remote", prompt_tokens=200, answer_tokens=20, service="provider")
    recorded(log, monkeypatch, "2026-10-03T09:00:00+00:00", "jordan", what="failed", answer_tokens=5, group="admin")
    users.change("pat", group="admin")
    params = {"start": "2026-10-01", "end": "2026-10-03", "timezone": "America/Vancouver", "group_by": "group"}
    all_usage = report(log.db, params, groups=users.memberships())
    expected = {"requests": 3, "input_tokens": 300, "output_tokens": 35, "total_tokens": 335,
                "missing_usage": 1, "inferred_group": 1, "input_cost": .0003,
                "output_cost": .000105, "total_cost": .000405}
    assert {key: all_usage["totals"][key] for key in expected} == expected
    assert all_usage["daily"][1]["total_tokens"] == 0
    assert {row["name"]: row["total_tokens"] for row in all_usage["breakdown"]} == {"plain": 110, "admin": 225}
    assert report(log.db, {**params, "group": "plain"})["totals"]["requests"] == 1
    assert report(log.db, {**params, "source": "hosted", "model": "remote", "user": "pat"})["totals"]["total_tokens"] == 220
    own = report(log.db, params, "pat")
    assert own["choices"]["user"] == ["pat"] and own["totals"]["requests"] == 2
    assert report(log.db, {**params, "model": "' OR 1=1 --"})["totals"]["requests"] == 0
    assert log.verify()[1] is None


@pytest.mark.parametrize("day,hours", [("2025-03-09", 23), ("2025-11-02", 25)])
def test_calendar_days_cover_dst_and_exclude_next_midnight(tmp_path, monkeypatch, day, hours):
    from datetime import timedelta
    database = str(tmp_path / "usage.db")
    Users(database, GROUPS)
    log = Log(database)
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("America/Vancouver"))
    end = start + timedelta(days=1)
    assert (end.timestamp() - start.timestamp()) / 3600 == hours
    for moment in [start - timedelta(seconds=1), start, end - timedelta(seconds=1), end]:
        recorded(log, monkeypatch, moment.isoformat(), "removed-user", prompt_tokens=1, answer_tokens=0)
    result = report(log.db, {"start": day, "end": day, "timezone": "America/Vancouver"})
    assert result["totals"]["requests"] == 2
    assert result["choices"]["group"] == ["(unknown)"]
    hourly = report(log.db, {"start": day, "end": day, "timezone": "America/Vancouver", "bucket": "hour"})
    assert len(hourly["hourly"]) == hours and sum(row["requests"] for row in hourly["hourly"]) == 2
    assert hourly["hourly"][0]["requests"] == hourly["hourly"][-1]["requests"] == 1
    assert len({row["label"] for row in hourly["hourly"]}) == hours


def test_usage_api_and_page_enforce_scope_and_validate_filters(desk, tmp_path, monkeypatch):
    browser, switch, users = desk
    log = Log(str(tmp_path / "loadcoach.db"))
    for name, tokens in [("pat", 10), ("jordan", 999)]:
        recorded(log, monkeypatch, "2026-10-01T12:00:00+00:00", name, model="private-model" if name == "jordan" else "own-model",
                 prompt_tokens=tokens, answer_tokens=1, group="plain" if name == "pat" else "admin")
    # WeightRoom polls its control node on another event loop. Give this client independent
    # account/log connections to the same files, as separate real processes have.
    with TestClient(turnstile_to(tmp_path, users, {8442: switch}), base_url="http://front:8442") as api:
        key = users.new_key("pat")
        headers = {"Authorization": "Bearer " + key}
        url = "/loadcoach/usage?start=2026-10-01&end=2026-10-02"
        assert api.get(url).status_code == 401
        result = api.get(url, headers=headers).json()
        assert result["totals"]["input_tokens"] == 10
        assert result["choices"]["model"] == ["own-model"] and not result["everyone"]
        assert api.get(url + "&user=jordan", headers=headers).status_code == 403
        for query in ["start=bad", "start=2026-10-05&end=2026-10-01", "timezone=invalid", "group_by=detail", "start=0001-01-01&end=2026-01-01", "input_rate=-1", "output_rate=NaN", "input_rate=Infinity", "output_rate=no", "input_rate=1000001", "bucket=minute", "start=2026-10-01&end=2026-10-02&bucket=hour"]:
            assert api.get("/loadcoach/usage?" + query, headers=headers).status_code == 400
    sign_in(browser, "pat")
    page = browser.get("/usage?start=2026-10-01&end=2026-10-02&complete=1")
    assert page.status_code == 200 and "Showing your usage only" in page.text
    defaults = json.loads(re.search(r'<script type="application/json" id="usage-data">(.*?)</script>', page.text, re.S)[1])
    assert defaults["mock_pricing"]["input_rate"] == 2 and defaults["mock_pricing"]["output_rate"] == 10
    assert "private-model" not in page.text and 'name="user"' not in page.text
    assert browser.get("/usage?user=jordan&complete=1").status_code == 403
    assert browser.get("/usage?start=bad&complete=1").status_code == 400
    browser.post("/sign-out")
    sign_in(browser, "jordan")
    page = browser.get("/usage?start=2026-10-01&end=2026-10-02&group_by=user&input_rate=2.5&output_rate=5&complete=1")
    result = json.loads(re.search(r'<script type="application/json" id="usage-data">(.*?)</script>', page.text, re.S)[1])
    assert result["mock_pricing"]["input_rate"] == 2.5
    assert result["totals"]["total_cost"] == pytest.approx((1009 * 2.5 + 2 * 5) / 1_000_000)
    assert 'name="output_rate"' in page.text and "Recorded cost" in page.text
    assert result["totals"]["input_tokens"] == 1009
    assert 'href="/usage"' in page.text and "Per day" in page.text and "Cumulative" in page.text
    assert "Selected range by user" in page.text and "Daily totals" in page.text


def test_new_requests_snapshot_group_in_usage(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        owner = add_user(caller, "owner", "admin")
        answer = caller.post("/v1/chat/completions", headers=owner, json={"model": "small", "messages": []})
        assert answer.status_code == 200
        ending = caller.get("/loadcoach/events?kind=request&what=done", headers=owner).json()["events"][-1]
        assert ending["group"] == "admin"
        usage = caller.get("/loadcoach/usage", headers=owner).json()
        assert usage["totals"]["requests"] == 1 and usage["totals"]["inferred_group"] == 0


def test_mock_costs_use_selected_rates_without_rounding_requests(tmp_path, monkeypatch):
    log = Log(str(tmp_path / "usage.db"))
    for day, inputs, outputs in [("01", 1, 2), ("02", 3, 4)]:
        recorded(log, monkeypatch, f"2026-10-{day}T12:00:00+00:00", "pat",
                 prompt_tokens=inputs, answer_tokens=outputs, cost=123)
    params = {"start": "2026-10-01", "end": "2026-10-03", "input_rate": "0.125", "output_rate": "2.75"}
    result = report(log.db, params)
    assert result["totals"]["input_cost"] == .0000005
    assert result["totals"]["output_cost"] == .0000165
    assert result["totals"]["total_cost"] == .000017
    for rows in (result["daily"], result["breakdown"]):
        assert sum(row["total_cost"] for row in rows) == pytest.approx(.000017)
    assert result["daily"][-1]["total_cost"] == 0
    assert report(log.db, {**params, "user": "absent"})["totals"]["total_cost"] == 0
    assert report(log.db, {**params, "input_rate": "0", "output_rate": "0"})["totals"]["total_cost"] == 0
    assert report(log.db, params)["totals"]["total_cost"] == .000017
    assert log.verify()[1] is None


def test_recorded_model_and_mock_cost_precedence(tmp_path, monkeypatch):
    log = Log(str(tmp_path / "usage.db"))
    params = {"start": "2026-10-01", "end": "2026-10-02"}
    models = {"paid": {"service": "provider", "in": 2, "out": 8},
              "free": {"service": "provider", "in": 0, "out": 0},
              "partial": {"service": "provider", "in": 2}}
    def add(model, **detail):
        recorded(log, monkeypatch, "2026-10-01T12:00:00+00:00", "pat", model=model,
                 service="provider", **{"prompt_tokens": 1_000_000, "answer_tokens": 500_000, **detail})
    add("paid", cost=1.25)  # provider charge wins over today's $6 model estimate
    add("paid", cost=0)  # known free cost must not become mock or model-priced
    add("paid")  # current model estimate $6
    add("free")  # current explicitly free model
    add("partial")  # missing nonzero output price -> $2.50 mock fallback
    add("removed", cost=.75)  # recorded cost survives model removal
    add("paid", cost=2, prompt_tokens=None, answer_tokens=None)  # known total despite missing counts
    add("paid", prompt_tokens=None)  # incomplete usage -> $1.50 mock fallback
    add("unknown", cost=-5)  # invalid price -> $2.50 mock fallback
    result = report(log.db, params, models=models)
    totals = result["totals"]
    assert totals["recorded_cost"] == 4
    assert totals["model_cost"] == 6
    assert totals["fallback_cost"] == 6.5
    assert totals["effective_cost"] == 16.5
    assert [totals[f"{source}_requests"] for source in ("recorded", "model", "fallback")] == [4, 2, 3]
    for rows in (result["daily"], result["breakdown"]):
        assert sum(row["effective_cost"] for row in rows) == 16.5
    assert result["daily"][1]["effective_cost"] == 0
    cheaper = report(log.db, {**params, "input_rate": "0", "output_rate": "0"}, models=models)["totals"]
    assert cheaper["effective_cost"] == 10 and cheaper["recorded_cost"] == 4
    models["paid"]["service"] = "replacement-provider"
    assert report(log.db, params, models=models)["totals"]["model_cost"] == 0
    assert report(log.db, params, "someone-else", models=models)["totals"]["effective_cost"] == 0
    assert log.verify()[1] is None


def test_usage_api_uses_registered_model_prices_without_disclosing_catalog(desk, tmp_path, monkeypatch):
    browser, switch, users = desk
    services = Services(str(tmp_path / "data" / "accounts.db"), str(tmp_path / "keys"))  # the services are the control node's
    services.put("rented", {"url": "http://unused/v1", "models": {
        "own-model": {"theirs": "remote", "in": 2, "out": 6},
        "secret-model": {"theirs": "secret", "in": 999}}})
    log = Log(str(tmp_path / "loadcoach.db"))
    recorded(log, monkeypatch, "2026-10-01T12:00:00+00:00", "pat", model="own-model",
             prompt_tokens=1000, answer_tokens=500, service="rented")
    recorded(log, monkeypatch, "2026-10-01T12:00:00+00:00", "jordan", model="secret-model", cost=100)
    sign_in(browser, "pat")
    page = browser.get("/usage?start=2026-10-01&end=2026-10-02&complete=1")
    result = json.loads(re.search(r'<script type="application/json" id="usage-data">(.*?)</script>', page.text, re.S)[1])
    assert result["totals"]["model_cost"] == .005
    assert result["totals"]["effective_cost"] == .005
    assert result["totals"]["recorded_cost"] == 0
    assert "secret-model" not in page.text


def test_chart_breakdowns_and_user_model_stacks_share_filters_and_scope(tmp_path, monkeypatch):
    log = Log(str(tmp_path / "usage.db"))
    for user, group, model, inputs, outputs, cost in [
        ("pat", "plain", "first", 100, 10, .1),
        ("pat", "plain", "second", 200, 20, .2),
        ("jordan", "admin", "first", 300, 30, .3),
        ("pat", "old-group", "first", 400, 40, .4),
    ]:
        recorded(log, monkeypatch, "2026-10-01T12:00:00+00:00", user, model=model,
                 group=group, prompt_tokens=inputs, answer_tokens=outputs, cost=cost)
    params = {"start": "2026-10-01", "end": "2026-10-02"}
    result = report(log.db, params)
    for dimension, rows in result["breakdowns"].items():
        assert sum(row["total_tokens"] for row in rows) == 1100
        assert sum(row["effective_cost"] for row in rows) == pytest.approx(1)
    assert result["breakdowns"]["model"] == result["breakdown"]
    for user in result["breakdowns"]["user"]:
        stack = [row for row in result["user_models"] if row["user"] == user["name"]]
        assert sum(row["total_tokens"] for row in stack) == user["total_tokens"]
        assert sum(row["effective_cost"] for row in stack) == pytest.approx(user["effective_cost"])
    scoped = report(log.db, {**params, "group": "plain"}, "pat")
    assert [row["name"] for row in scoped["breakdowns"]["user"]] == ["pat"]
    assert [row["name"] for row in scoped["breakdowns"]["group"]] == ["plain"]
    assert sum(row["total_tokens"] for row in scoped["user_models"]) == 330
    filtered = report(log.db, {**params, "model": "second"})
    assert len(filtered["user_models"]) == 1 and filtered["user_models"][0]["model"] == "second"
    empty = report(log.db, {**params, "model": "absent"})
    assert empty["user_models"] == [] and all(rows == [] for rows in empty["breakdowns"].values())


def test_summary_api_scope_covers_totals_requests_and_not_captured_text(desk, tmp_path):
    browser, switch, users = desk
    log = Log(str(tmp_path / 'loadcoach.db'))
    ids = {}
    for name, group, tokens in [('pat', 'plain', 10), ('jordan', 'admin', 99), ('cleo', 'plain', 20)]:
        ids[name] = log.add('request', 'asked', name, model=name + '-model', group=group)
        log.add('request', 'done', name, model=name + '-model', group=group,
                of=ids[name], prompt_tokens=tokens, answer_tokens=1, text='private response')
    with TestClient(turnstile_to(tmp_path, users, {8442: switch}), base_url='http://front:8442') as api:
        headers = {'authorization': 'Bearer ' + users.new_key('pat')}
        for endpoint in ('usage', 'requests'):
            assert api.get('/loadcoach/' + endpoint + '?summary=total', headers=headers).status_code == 403
        users.change('pat', own={'add': ['see-summaries']})
        total = api.get('/loadcoach/usage?summary=total&period=all', headers=headers)
        assert total.status_code == 200 and total.json()['totals']['input_tokens'] == 129
        group = api.get('/loadcoach/usage?summary=group&group=plain&period=all', headers=headers)
        assert group.json()['totals']['input_tokens'] == 30
        rows = api.get('/loadcoach/requests?summary=group&group=plain', headers=headers)
        assert {row['user'] for row in rows.json()['requests']} == {'pat', 'cleo'}
        assert 'private response' not in rows.text
        assert api.get(f'/loadcoach/requests/{ids["jordan"]}', headers=headers).status_code == 403
        # Membership changes must not rewrite groups saved with a request.
        users.change('cleo', group='admin')
        rows = api.get('/loadcoach/requests?summary=group&group=plain', headers=headers).json()['requests']
        assert {row['user'] for row in rows} == {'pat', 'cleo'}
        users.change('pat', own={})
        for endpoint in ('usage', 'requests'):
            assert api.get('/loadcoach/' + endpoint + '?summary=total', headers=headers).status_code == 403
    log.db.close()


def test_hourly_summaries_match_the_endings_and_survive_event_pruning(tmp_path, monkeypatch):
    """usage_hours gives the same report as reading every ending, by sensitivity level too, and outlives the events."""
    import loadcoach.usage as usage
    from db.schemas import _usage_hours
    database = str(tmp_path / "usage.db")
    users = Users(database, GROUPS)
    users.add("pat", "plain")
    log = Log(database)
    recorded(log, monkeypatch, "2026-10-01T08:10:00+00:00", "pat", prompt_tokens=100, answer_tokens=10, group="plain")
    recorded(log, monkeypatch, "2026-10-01T08:50:00+00:00", "pat", prompt_tokens=50, answer_tokens=5)  # inferred group
    recorded(log, monkeypatch, "2026-10-02T09:00:00+00:00", "pat", model="remote", prompt_tokens=200, answer_tokens=20,
             service="provider", group="plain")
    recorded(log, monkeypatch, "2026-10-02T09:30:00+00:00", "pat", model="remote", prompt_tokens=10, answer_tokens=1,
             service="provider", cost=0.5, group="plain")
    recorded(log, monkeypatch, "2026-10-03T08:00:00+00:00", "pat", what="failed", answer_tokens=5, group="plain")
    with monkeypatch.context() as clock:
        clock.setattr("loadcoach.log.time.time", lambda: datetime.fromisoformat("2026-10-03T10:00:00+00:00").timestamp())
        log.add("request", "done", "pat", model="small", level="sensitive", prompt_tokens=7, answer_tokens=3, group="plain")
    prices = {"remote": {"service": "provider", "in": 2.0, "out": 4.0}}
    params = {"start": "2026-10-01", "end": "2026-10-03", "timezone": "America/Vancouver", "group_by": "level"}
    assert log.db.execute("SELECT COUNT(*) FROM usage_hours").fetchone()[0] == 5  # two endings share one hour
    fast = report(log.db, params, groups=users.memberships(), models=prices)
    with monkeypatch.context() as slow:
        slow.setattr(usage, "whole_hours", lambda *_: False)
        exact = report(log.db, params, groups=users.memberships(), models=prices)
    assert fast == exact
    assert {row["name"]: row["requests"] for row in fast["breakdown"]} == {"normal": 5, "sensitive": 1}
    assert fast["totals"]["recorded_cost"] == 0.5 and fast["totals"]["model_requests"] == 1
    assert fast["totals"]["inferred_group"] == 1 and fast["totals"]["missing_usage"] == 1
    one_day = report(log.db, {**params, "start": "2026-10-01", "end": "2026-10-01", "bucket": "hour"}, groups=users.memberships())
    assert sum(hour["requests"] for hour in one_day["hourly"]) == 2
    # The migration rebuilds the same rows from the endings.
    before = log.db.execute("SELECT * FROM usage_hours ORDER BY hour, model, level").fetchall()
    log.db.execute("DELETE FROM usage_hours")
    _usage_hours(log.db.database, log.db)
    assert log.db.execute("SELECT * FROM usage_hours ORDER BY hour, model, level").fetchall() == before
    log.db.execute("DELETE FROM events")  # old events pruned: the summary still answers
    assert report(log.db, params, groups=users.memberships(), models=prices)["totals"] == fast["totals"]
