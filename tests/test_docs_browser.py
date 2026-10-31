"""The admin map is navigable in Chromium at desktop and phone widths."""

import pytest

from test_console_browser import browser_context
from test_weightroom import desk, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')


def test_docs_map_links_and_phone_layout(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda problem: errors.append(str(problem)))
        page.goto('http://console.test/docs')
        assert page.get_by_role('heading', name='System docs').is_visible()
        assert page.locator('.category-links .nav-link.active').inner_text().strip() == 'Docs'
        assert page.locator('.docs-map-node').count() == 12
        assert page.locator('.docs-map-zone').count() == 2
        assert page.get_by_text('Twilio telephone', exact=True).is_visible()
        assert page.get_by_text('Signed model request', exact=True).is_visible()
        page.screenshot(path=str(tmp_path / 'docs-desktop.png'), full_page=True)
        page.locator('.docs-map-node[href="/docs/loadcoach"]').click()
        assert page.get_by_role('heading', name='LoadCoach', exact=True).is_visible()
        assert 'GPU residency' in page.locator('.docs-topic-summary').inner_text()
        modules = page.get_by_role('navigation', name='Source modules')
        assert modules.locator('details[open]').count() == 1  # one folder open at a time, not every module at once
        modules.locator('a[href="?module=loadcoach/users.py"]').click()
        assert modules.locator('a.active').get_attribute('aria-current') == 'page'
        page.screenshot(path=str(tmp_path / 'docs-source-desktop.png'), full_page=True)
        definition = page.locator('details').filter(has=page.get_by_text('async def sign_in', exact=True))
        definition.locator('summary').click()
        assert 'Raises SignInRefused' in definition.inner_text()
        for width in (390, 320):
            page.set_viewport_size({'width': width, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(tmp_path / 'docs-source-mobile.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.goto('http://console.test/docs')
        assert page.get_by_role('heading', name='System docs').is_visible()
        assert page.locator('.category-links .nav-link.active').is_visible()
        assert page.locator('.docs-map-scroll').evaluate('(node) => node.scrollWidth > node.clientWidth')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(tmp_path / 'docs-mobile.png'), full_page=True)
        page.locator('.docs-directory-link[href="/docs/api"]').click()
        assert page.get_by_role('heading', name='API reference', exact=True).is_visible()
        assert page.get_by_role('heading', name='Implemented OpenAI endpoints', exact=True).is_visible()
        page.locator('#api-custom details').filter(has=page.get_by_text('turnstile/app.py', exact=True)).locator('summary').click()
        assert page.get_by_text('/turnstile/models/install', exact=True).is_visible()
        for width in (390, 320):
            page.set_viewport_size({'width': width, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(tmp_path / 'docs-api-mobile.png'))
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('#api-compatible').scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / 'docs-api-desktop.png'))
        assert not errors
        browser.close()
