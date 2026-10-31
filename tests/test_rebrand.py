"""Canonical service names preserve settings, existing sessions and browser preferences."""
import json
import sqlite3
from pathlib import Path

from loadcoach.settings import Changes
from test_weightroom import desk, sign_in  # noqa: F401


def test_legacy_settings_migrate_without_losing_values_authors_or_timestamps(tmp_path):
    path = str(tmp_path / 'settings.db')
    changes = Changes(path)
    # Seed the pre-rebrand shape directly, as it exists on deployed installations.
    with changes.db:
        changes.db.execute("INSERT INTO settings_changed VALUES ('frontdesk','timezone','\"America/Vancouver\"','owner',123)")
        changes.db.execute("INSERT INTO settings_changed VALUES ('tooldoor','call_timeout','45','owner',124)")
        changes.db.execute("INSERT INTO settings_seen VALUES ('frontdesk',125,'{}','{}')")
        changes.db.execute("INSERT INTO settings_seen VALUES ('tooldoor',126,'{}','{}')")
    changes.db.close()
    renamed = Changes(path)
    assert renamed.of('weightroom') == renamed.of('frontdesk') == {'timezone': 'America/Vancouver'}
    assert renamed.of('toollocker') == renamed.of('tooldoor') == {'call_timeout': 45}
    assert renamed.db.execute('SELECT program,by,at FROM settings_changed ORDER BY program').fetchall() == [('toollocker', 'owner', 124), ('weightroom', 'owner', 123)]
    assert {p['program'] for p in renamed.everything()} == {'weightroom', 'toollocker'}
    renamed.put('frontdesk', 'timezone', 'UTC', 'owner')
    assert renamed.of('weightroom') == {'timezone': 'UTC'}
    renamed.db.close()


def test_old_signin_cookie_still_works_and_signout_clears_both_names(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    key = browser.cookies['weightroom']
    browser.cookies.clear()
    browser.cookies.set('frontdesk', key)
    assert browser.get('/account').status_code == 200
    assert 'My account' in browser.get('/account').text
    signed_out = browser.post('/sign-out', follow_redirects=False)
    assert 'frontdesk=' in '\n'.join(signed_out.headers.get_list('set-cookie'))
    assert 'weightroom=' in '\n'.join(signed_out.headers.get_list('set-cookie'))
    assert browser.get('/account', follow_redirects=False).headers['location'] == '/sign-in?next=%2Faccount'


def test_legacy_browser_preferences_and_icons(desk):
    import pytest
    engine_api = pytest.importorskip('playwright.sync_api')
    server, _, users = desk
    sign_in(server, 'jordan')
    with engine_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--disable-gpu"])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        identity = users.identity('jordan')
        context.add_init_script("""(() => {
          const identity = """ + json.dumps(identity) + """;
          localStorage.setItem('frontdesk-theme-v1:' + identity, JSON.stringify({palette:'ocean',mode:'dark'}));
          localStorage.setItem('frontdesk-cards-v1:' + identity, JSON.stringify({'benchmark-start':false}));
          localStorage.setItem('frontdesk-sidebar','closed');
        })()""")
        def serve(route):
            if route.request.url.split('?', 1)[0] == 'http://branding.test/strip/stream':
                from front_helpers import serve_browser_request
                return serve_browser_request(server, 'http://branding.test', route)
            response = server.get(route.request.url.replace('http://branding.test', ''))
            route.fulfill(status=response.status_code, content_type=response.headers.get('content-type','text/html'), body=response.content)
        context.route('http://branding.test/**', serve)
        page = context.new_page(); errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('http://branding.test/benchmarks/runs')
        engine_api.expect(page.locator('html')).to_have_attribute('data-site-theme','ocean')
        engine_api.expect(page.locator('html')).to_have_attribute('data-bs-theme','dark')
        engine_api.expect(page.locator('html')).to_have_attribute('data-sidebar','closed')
        engine_api.expect(page.locator('[data-card-key="benchmark-start"]')).not_to_have_attribute('open','')
        assert 'openWeightGym' in page.title()
        assert page.locator('.navbar-brand use').get_attribute('href') == '/static/icons.svg?v=3#barbell'
        assert page.locator('link[rel="icon"]').get_attribute('href') == '/static/favicon.svg'
        page.goto('http://branding.test/account/appearance')
        page.locator('[name=palette][value=rose]').check()
        page.get_by_role('button',name='Save theme',exact=True).click()
        page.reload()
        engine_api.expect(page.locator('html')).to_have_attribute('data-site-theme','rose')
        images=Path('data/rebrand-previews');images.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(images/'weightroom.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
