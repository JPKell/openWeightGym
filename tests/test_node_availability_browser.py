"""Offline node controls stay visible and disabled without blocking personal settings."""

import pytest
from playwright.sync_api import sync_playwright, expect

from front_helpers import serve_browser_request
from test_weightroom import desk, sign_in  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_offline_node_controls_in_browser(desk, width):
    server, switch, users = desk
    users.change("jordan", own={"add": ["front-wake"]})
    sign_in(server, "jordan")
    assert server.get("/compute-nodes").status_code == 200
    switch.off = True
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": 1000})
        context.add_init_script("window.EventSource = class { close() {} };")
        context.route("http://offline.test/**", lambda route: serve_browser_request(server, "http://offline.test", route))
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://offline.test/compute-nodes")
        expect(page.locator("#node-availability")).to_contain_text("Node unavailable: compute")
        buttons = page.locator('form[action="/compute-nodes/engines"] button')
        assert buttons.count() > 0
        for button in buttons.all():
            expect(button).to_be_disabled()
        expect(page.locator('form[action="/wake"] button')).to_be_enabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.goto("http://offline.test/account/appearance")
        for button in page.locator('form[method="post"] button.btn-primary').all():
            expect(button).to_be_enabled()
        assert not errors
        browser.close()
