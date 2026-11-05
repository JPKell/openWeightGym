"""The docs open on the system reference, each section opens its topics in the menu, and every page fits a phone."""

import pytest

from test_console_browser import browser_context
from test_weightroom import desk, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')


def test_docs_sections_open_their_topics_and_fit_a_phone(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda problem: errors.append(str(problem)))
        page.goto('http://console.test/status')
        page.locator('.category-links .nav-link', has_text='Docs').click()
        page.wait_for_url('http://console.test/docs')  # Docs opens on the system reference
        assert page.get_by_role('heading', name='System reference', exact=True).is_visible()
        assert page.locator('.category-links .nav-link.active').inner_text().strip() == 'Docs'
        menu = page.get_by_role('navigation', name='Docs pages')
        assert [text.strip() for text in menu.locator(':scope > a.section-link').all_inner_texts()] == [
            'System reference', 'API reference', 'Web app', 'Modules', 'Benchmarks', 'Data and backups', 'Deployment and operations']
        assert menu.locator('a[aria-current="page"]').inner_text().strip() == 'System reference'
        assert menu.locator('.section-children').count() == 1 and menu.get_by_role('link', name='Models', exact=True).is_visible()
        assert page.locator('.docs-map-node').count() == 12
        assert page.get_by_text('Twilio telephone', exact=True).is_visible()
        page.screenshot(path=str(tmp_path / 'docs-desktop.png'), full_page=True)

        # A section's link opens its overview, and its topics appear beneath it in the menu.
        menu.get_by_role('link', name='API reference', exact=True).click()
        page.wait_for_url('http://console.test/docs/api')
        assert page.get_by_role('heading', name='Connection and scope', exact=True).is_visible()
        assert not menu.get_by_role('link', name='Models', exact=True).count()
        menu.get_by_role('link', name='OpenAI compatibility', exact=True).click()
        page.wait_for_url('http://console.test/docs/api/openai')
        assert page.get_by_role('heading', name='Implemented OpenAI endpoints', exact=True).is_visible()
        assert menu.locator('a[aria-current="page"]').inner_text().strip() == 'OpenAI compatibility'
        assert 'open' in menu.get_by_role('link', name='API reference', exact=True).get_attribute('class')
        menu.get_by_role('link', name='Custom endpoints', exact=True).click()
        page.locator('#api-custom details').filter(has=page.get_by_text('turnstile/app.py', exact=True)).locator('summary').click()
        assert page.get_by_text('/turnstile/models/install', exact=True).is_visible()

        # A component on the map leads to its module, with the code reference.
        page.goto('http://console.test/docs')
        page.locator('.docs-map-node[href="/docs/modules/loadcoach"]').click()
        assert page.get_by_role('heading', name='LoadCoach', exact=True).is_visible()
        assert menu.locator('a[aria-current="page"]').inner_text().strip() == 'LoadCoach'
        modules = page.get_by_role('navigation', name='Source modules')
        assert modules.locator('details[open]').count() == 1  # one folder open at a time, not every module at once
        modules.locator('a[href^="?module=loadcoach/users.py"]').click()
        assert modules.locator('a.active').get_attribute('aria-current') == 'page'
        page.screenshot(path=str(tmp_path / 'docs-source-desktop.png'), full_page=True)
        definition = page.locator('details').filter(has=page.get_by_text('async def sign_in', exact=True))
        definition.locator('summary').click()
        assert 'Raises SignInRefused' in definition.inner_text()

        for address in ('/docs/modules/loadcoach', '/docs/api/openai', '/docs/operations/installation', '/docs/web-app/chat'):
            page.goto('http://console.test' + address)
            for width in (390, 320):
                page.set_viewport_size({'width': width, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), (address, width)
            page.set_viewport_size({'width': 1440, 'height': 1000})
        page.set_viewport_size({'width': 390, 'height': 844})
        page.goto('http://console.test/docs')
        assert page.get_by_role('heading', name='System reference', exact=True).is_visible()
        assert page.locator('.docs-map-scroll').evaluate('(node) => node.scrollWidth > node.clientWidth')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(tmp_path / 'docs-mobile.png'), full_page=True)
        assert not errors
        browser.close()
