"""Front owns node lifecycle independently of installation and reachability."""

import asyncio
import json

import pytest
from playwright.sync_api import sync_playwright, expect
from starlette.testclient import TestClient

from front_helpers import door, serve_browser_request
from loadcoach.log import Log
from test_two_machines import metadata_front  # noqa: F401
from turnstile.machines import Machines
from weightroom.app import Settings, build_app


def test_disabled_keeps_telemetry_and_retirement_preserves_history_across_restart(tmp_path):
    log = Log(str(tmp_path / "history.db"))
    calls = []

    async def ask(name, path):
        calls.append(name)
        if path == "/v1/models":
            return {"data": [{"id": "small", "owned_by": "loadcoach"}]}
        return {"machine_fingerprint": "a" * 64, "hostname": "shared", "memory": {},
                "loaded": [], "running": [], "waiting": []}

    async def telemetry(name, path):
        return {"at": 1000, "machine_fingerprint": "a" * 64, "cards": [],
                "processor": {"load": 42}, "memory": {"used": 2000, "total": 10000}, "disks": []}

    machines = Machines(["compute", "rig"], ask, now=lambda: 1000, store=log.db, ask_telemetry=telemetry)
    asyncio.run(machines.poll())
    history = machines.history("a" * 64, 1000, 60, 2)
    machines.change_state("compute", "disabled", "jordan")
    calls.clear()
    asyncio.run(machines.poll())
    assert "compute" in calls and machines.available("compute") is True
    assert machines.active_names == ["rig"]
    assert machines.hosts()[0]["disabled_nodes"] == ["compute"]
    assert machines.models()[0]["machines"] == ["rig"]
    machines.change_state("compute", "retired", "jordan")
    calls.clear()
    asyncio.run(machines.poll())
    assert calls == ["rig", "rig"]
    assert machines.hosts()[0]["machines"] == ["rig"]  # another instance still uses this host
    machines.change_state("rig", "retired", "jordan")
    assert machines.hosts() == [] and machines.report() == [] and machines.models() == []
    assert machines.history("a" * 64, 1000, 60, 2) == history
    restarted = Machines(["compute", "rig"], ask, now=lambda: 1000, store=log.db)
    assert restarted.hosts() == [] and restarted.active_names == []
    assert [row["lifecycle"] for row in restarted.report(True)] == ["retired", "retired"]
    restarted.change_state("rig", "active", "jordan")
    assert restarted.hosts()[0]["machines"] == ["rig"] and restarted.enabled("rig")


def test_an_unconfigured_historical_registration_can_be_retired():
    log = Log(":memory:")
    log.db.execute("INSERT INTO machine_hosts VALUES(?,?,?,?)", ("b" * 64, "old-node", '["removed"]', 1000))
    machines = Machines([], None, now=lambda: 1000, store=log.db)
    assert machines.report(True)[0]["configured"] is False
    assert machines.hosts()[0]["machines"] == ["removed"]
    with pytest.raises(ValueError, match="configure"):
        machines.change_state("removed", "active", "jordan")
    machines.change_state("removed", "retired", "jordan")
    assert machines.hosts() == []
    assert log.db.execute("SELECT COUNT(*) FROM machine_hosts").fetchone()[0] == 1


def test_retirement_during_a_poll_cannot_restore_telemetry():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def ask(name, path):
            started.set()
            await release.wait()
            return {"machine_fingerprint": "a" * 64, "hostname": "retiring"}

        machines = Machines(["compute"], ask)
        polling = asyncio.create_task(machines.poll())
        await started.wait()
        machines.change_state("compute", "retired", "jordan")
        release.set()
        await polling
        assert machines.seen == {} and machines.hosts() == []

    asyncio.run(run())


def test_api_authorization_cached_routing_and_offline_retirement(metadata_front):
    front, wire, users, key, own = metadata_front
    headers = {"authorization": "Bearer " + key}
    base = "/turnstile/machines/compute/lifecycle"
    assert front.post(base, json={"state": "disabled"}).status_code == 401
    assert front.post(base, headers={"authorization": "Bearer " + own}, json={"state": "retired"}).status_code == 403
    assert front.get("/turnstile/machines?include_retired=1", headers={"authorization": "Bearer " + own}).status_code == 403
    assert front.post(base, headers=headers, json={"state": "missing"}).status_code == 400
    assert front.post("/turnstile/machines/missing/lifecycle", headers=headers, json={"state": "retired"}).status_code == 404
    # Prime both discovery and placement caches before changing lifecycle.
    assert front.get("/v1/models", headers=headers).json()["data"][0]["machines"] == ["compute", "rig"]
    payload = {"model": "small", "messages": [{"role": "user", "content": "hello"}]}
    assert front.post("/v1/chat/completions", headers=headers, json=payload).status_code == 200
    assert front.post(base, headers=headers, json={"state": "disabled"}).json()["lifecycle"] == "disabled"
    assert front.get("/v1/models", headers=headers).json()["data"][0]["machines"] == ["rig"]
    assert front.post("/v1/chat/completions", headers=headers, json=payload).json()["machine"] == "rig"
    # Maintenance observation is still allowed; new installations and direct writes are refused.
    assert front.get("/turnstile/machines/compute/control/status", headers=headers).status_code == 200
    assert front.post("/turnstile/machines/compute/model-api/model-setup", headers=headers, json={}).status_code == 409
    refused_install = front.post("/turnstile/models/install", headers=headers,
                                 json={"name": "new-model", "machine": "compute", "settings": {}})
    assert refused_install.status_code == 409 and "disabled" in refused_install.text
    users.change("pat", own={"add": ["front-wake"]})
    assert front.post("/turnstile/machines/compute/wake", headers=headers).status_code == 409
    wire.down = True
    front.portal.call(front.app.state.machines.poll)
    before = list(wire.calls)
    assert front.post("/turnstile/machines/rig/lifecycle", headers=headers, json={"state": "retired"}).status_code == 200
    assert wire.calls == before  # retirement never waits for the uninstalled worker
    assert [row["name"] for row in front.get("/turnstile/machines", headers=headers).json()["machines"]] == ["compute"]
    assert front.get("/loadcoach/me", headers=headers).json()["models"] == []
    assert front.get("/v1/models", headers=headers).json()["data"] == []
    refused = front.post("/v1/chat/completions", headers=headers, json=payload)
    assert refused.status_code == 409 and "disabled" in refused.text
    assert wire.calls == before
    events = front.app.state.log.db.execute("SELECT what,user,detail FROM events WHERE kind='machine' ORDER BY id").fetchall()
    assert [(what, who, json.loads(detail)["machine"]) for what, who, detail in events] == [
        ("disabled", "pat", "compute"), ("retired", "pat", "rig")]
    assert front.post("/turnstile/machines/rig/lifecycle", headers=headers, json={"state": "active"}).status_code == 200


def test_removed_registration_can_be_selected_and_retired_from_settings(metadata_front, tmp_path):
    front, wire, users, key, own = metadata_front
    machines, log = front.app.state.machines, front.app.state.log
    fingerprint = "c" * 64
    machines.known_hosts[fingerprint] = {"hostname": "uninstalled", "machines": ["removed"], "last_seen": 1000}
    log.db.execute("INSERT INTO machine_hosts VALUES(?,?,?,?)", (fingerprint, "uninstalled", '["removed"]', 1000))
    log.add("service", "uninstalled", "pat", machine="removed")
    wire.down = True
    front.portal.call(machines.poll)
    web = build_app(Settings(compute="http://front:8442", key=own, data=str(tmp_path / "web")), door(front.app, 8442))
    with TestClient(web) as server:
        server.cookies.set("weightroom", key)
        assert "uninstalled" in server.get("/telemetry").text
        selected = server.get("/compute-nodes?machine=removed")
        assert selected.status_code == 200 and "retained registration" in selected.text
        assert 'name="machine" value="removed"' in selected.text
        assert 'value="retired"' in selected.text
        saved = server.post("/compute-nodes/lifecycle", data={"machine": "removed", "state": "retired"})
        assert saved.status_code == 200 and "is retired. Reachability:" in saved.text
        assert "Historical logs are preserved" in saved.text
        assert "uninstalled" not in server.get("/telemetry").text
        historical = server.get("/telemetry/" + fingerprint)
        assert historical.status_code == 200 and 'Only retained history is shown' in historical.text
        assert 'Wake removed' not in historical.text
        assert log.db.execute("SELECT COUNT(*) FROM events WHERE kind='service' AND what='uninstalled'").fetchone()[0] == 1
        assert log.db.execute("SELECT COUNT(*) FROM machine_hosts WHERE fingerprint=?", (fingerprint,)).fetchone()[0] == 1


@pytest.mark.parametrize("width", [1440, 390])
def test_offline_node_pills_and_lifecycle_controls_work_in_browser(metadata_front, tmp_path, width):
    front, wire, users, key, own = metadata_front
    wire.down = True
    front.portal.call(front.app.state.machines.poll)
    web = build_app(Settings(compute="http://front:8442", key=own, data=str(tmp_path / "web")), door(front.app, 8442))
    with TestClient(web) as server, sync_playwright() as engine:
        server.cookies.set("weightroom", key)
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": 1000})
        context.add_init_script("window.EventSource = class { close() {} };")
        context.route("http://nodes.test/**", lambda route: serve_browser_request(server, "http://nodes.test", route))
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://nodes.test/compute-nodes")
        pills = page.get_by_role("navigation", name="Compute nodes", exact=True)
        expect(pills.get_by_role("link")).to_have_count(2)
        pills.get_by_role("link", name="rig · not answering", exact=True).click()
        expect(pills.get_by_role("link", name="rig · not answering", exact=True)).to_have_attribute("aria-current", "page")
        # Confirmation preserves the clicked button's state field on a normal form submission.
        page.get_by_role("button", name="Disable node", exact=True).click()
        page.get_by_role("button", name="Are you sure? Disable node", exact=True).click()
        expect(page.locator("#node-lifecycle")).to_contain_text("rig is disabled")
        expect(pills.get_by_role("link", name="rig · disabled", exact=True)).to_be_visible()
        page.goto("http://nodes.test/strip")
        expect(page.locator('.host-meter[title*="disabled: rig"]')).to_have_count(1)
        page.goto("http://nodes.test/telemetry")
        expect(page.get_by_text("Disabled for new work: rig. Telemetry remains visible.", exact=True)).to_be_visible()
        page.goto("http://nodes.test/compute-nodes?machine=rig")
        page.get_by_role("button", name="Retire node", exact=True).click()
        page.get_by_role("button", name="Are you sure? Retire node", exact=True).click()
        expect(page.locator("#node-lifecycle")).to_contain_text("rig is retired")
        assert page.locator('form[action="/compute-nodes/engines"]').count() == 0
        expect(page.get_by_role("button", name="Enable node", exact=True)).to_be_enabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.goto("http://nodes.test/strip")
        expect(page.locator('.host-meter').filter(has_text="rig")).to_have_count(0)
        page.goto("http://nodes.test/telemetry")
        expect(page.locator('.telemetry-card').filter(has_text="rig")).to_have_count(0)
        page.goto("http://nodes.test/compute-nodes?machine=rig")
        page.get_by_role("button", name="Enable node", exact=True).click()
        expect(page.locator("#node-lifecycle")).to_contain_text("rig is active")
        assert not errors
        browser.close()
