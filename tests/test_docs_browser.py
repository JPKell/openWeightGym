"""The live search-first library, native article routes and separate source browser fit every screen."""

import re

import pytest

from test_console_browser import browser_context
from test_weightroom import desk, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect


def test_docs_search_navigation_history_contents_and_source_browser(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda problem: errors.append(str(problem)))
        page.goto('http://console.test/status')
        page.locator('.category-links .nav-link', has_text='Docs').click()
        expect(page).to_have_url('http://console.test/docs')
        expect(page.get_by_role('heading', name='A clear path to what you need.', exact=True)).to_be_visible()
        expect(page.locator('.category-links .nav-link.active')).to_have_text('Docs')
        menu = page.get_by_role('navigation', name='Docs pages')
        expect(menu.get_by_role('link', name='Documentation', exact=True)).to_have_attribute('aria-current', 'page')
        expect(menu.get_by_role('link', name='Code references', exact=True)).to_be_visible()
        page.locator('#docs-query').fill('recipients.txt')
        expect(page.locator('#docs-results-list a[href="/docs/data/backups"]')).to_be_visible()
        expect(page.locator('.result-excerpt mark').first).to_have_text('recipients.txt')
        expect(page).to_have_url(re.compile(r'/docs\?q=recipients.txt'))
        page.locator('#docs-scope').select_option('web-app')
        expect(page.locator('#docs-search-empty')).to_be_visible()
        page.locator('#docs-search-empty [data-docs-clear]').click()
        expect(page.locator('.docs-hero')).to_be_visible()
        page.locator('#docs-query').fill('shift+enter')
        page.locator('#docs-results-list a[href="/docs/web-app/chat"]').click()
        expect(page.locator('#docs-article .page-title')).to_have_text('Chat')
        page.get_by_role('navigation', name='Article contents').get_by_role('link', name='Options and keys').click()
        expect(page).to_have_url(re.compile('#chat-options$'))
        page.go_back()
        expect(page).not_to_have_url(re.compile('#chat-options$'))
        page.go_back()
        expect(page.locator('#docs-results-list a[href="/docs/web-app/chat"]')).to_be_visible()
        page.goto('http://console.test/docs#system-map')
        expect(page).to_have_url('http://console.test/docs/system#system-map')
        page.goto('http://console.test/docs/modules/loadcoach#source')
        expect(page).to_have_url('http://console.test/docs/code/loadcoach#source')
        page.goto('http://console.test/docs/modules/loadcoach')
        expect(page.locator('#docs-article nav[aria-label="Source modules"]')).to_have_count(0)
        page.locator('.docs-code-reference a').click()
        expect(page).to_have_url('http://console.test/docs/code/loadcoach')
        modules = page.get_by_role('navigation', name='Source modules')
        expect(modules.locator('details[open]')).to_have_count(1)
        modules.locator('a[href^="?module=loadcoach/users.py"]').click()
        expect(modules.locator('a.active')).to_have_attribute('aria-current', 'page')
        definition = page.locator('.docs-definition').filter(has=page.get_by_text('async def sign_in', exact=True))
        definition.locator('summary').click()
        expect(definition).to_contain_text('Raises SignInRefused')
        page.screenshot(path=str(tmp_path / 'docs-code-desktop.png'), full_page=True)
        assert not errors
        browser.close()


@pytest.mark.parametrize('width', [1440, 1024, 390, 320])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_docs_library_reading_and_mobile_navigation(desk, tmp_path, width, theme):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        page.emulate_media(color_scheme=theme)
        errors = []
        page.on('pageerror', lambda problem: errors.append(str(problem)))
        for address in ('/docs', '/docs/system', '/docs/api/openai', '/docs/operations/installation', '/docs/web-app/chat', '/docs/web-app/image-extraction', '/docs/data/backups', '/docs/code/loadcoach'):
            response = page.goto('http://console.test' + address)
            assert response.status == 200
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (address, width, theme)
            assert page.locator('main h1:visible').count() == 1
            ids = page.locator('[id]').evaluate_all('(nodes) => nodes.map(node => node.id)')
            assert len(ids) == len(set(ids)), address
        page.goto('http://console.test/docs')
        if width <= 390:
            page.locator('#mobile-menu-toggle').click()
            page.locator('#mobile-navigation').get_by_role('link', name='Code references', exact=True).click()
            expect(page.locator('#docs-article .page-title')).to_have_text('Code references')
        page.goto('http://console.test/docs')
        page.screenshot(path=str(tmp_path / f'docs-library-{width}-{theme}.png'), full_page=True)
        page.locator('#docs-query').fill('not-a-real-document-needle')
        expect(page.locator('#docs-search-empty')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('#docs-query')).to_have_value('')
        expect(page.locator('.docs-hero')).to_be_visible()
        assert not errors
        browser.close()


def test_docs_search_and_reading_work_without_javascript(desk):
    server, _, _ = desk
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.context.set_extra_http_headers({})
        # Disable execution after context creation; request routing still uses the isolated test app.
        session = page.context.new_cdp_session(page)
        session.send('Emulation.setScriptExecutionDisabled', {'value': True})
        page.goto('http://console.test/docs')
        expect(page.locator('.library-category')).to_have_count(1)
        page.locator('#docs-query').fill('Shift+Enter')
        page.locator('#docs-search button[type=submit]').click()
        page.locator('#docs-results-list a[href="/docs/web-app/chat"]').click()
        expect(page.locator('#docs-article')).to_contain_text('Shift+Enter adds a line')
        expect(page.locator('a[href="/docs/code"]')).to_have_count(0)
        browser.close()


def test_docs_live_search_recovers_from_an_unavailable_response(desk):
    server, _, _ = desk
    sign_in(server, 'pat')
    offline = {'value': True}
    def capture(route, path):
        if offline['value'] and path.startswith('/docs?') and 'part=search' in path:
            route.fulfill(status=503, content_type='text/plain', body='Unavailable')
            return True
        return False
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        page.goto('http://console.test/docs')
        page.locator('#docs-query').fill('Shift+Enter')
        expect(page.locator('#docs-result-count')).to_contain_text('Press Search to retry')
        expect(page.locator('#docs-search')).not_to_have_attribute('aria-busy', 'true')
        offline['value'] = False
        page.locator('#docs-search button[type=submit]').click()
        expect(page.locator('#docs-results-list a[href="/docs/web-app/chat"]')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('#docs-search')).not_to_have_attribute('aria-busy', 'true')
        expect(page.locator('.docs-hero')).to_be_visible()
        browser.close()
