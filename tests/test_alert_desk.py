"""The live Alert desk reads stored evidence and retains native, authorized actions."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from weightroom.alerts import Alerts
from weightroom.alert_views import workspace, return_url, export_json
from test_weightroom import desk, sign_in  # noqa: F401


def seed(store, count=3):
    store.observe('desk fixtures', [dict(key=f'desk:{index}', active=True, title=f'GPU event {index}',
                  explanation=f'atlas GPU {index} · 90°C', manual=index == 0) for index in range(count)], now=1000)
    return store.rows(open_only=True)


def test_complete_filters_counts_paging_literal_search_and_export(tmp_path):
    store = Alerts(tmp_path / 'alerts.db')
    rows = seed(store, 31)
    store.acknowledge(rows[1]['id'], 'jordan')
    store.observe('missing source', [dict(key='unknown', active=True, title='Earlier heat', explanation='last reading 89°C')], now=900)
    store.observe('missing source', None, now=1100)
    view = workspace(store, {'source': 'desk fixtures', 'page': '2'})
    assert len(view['rows']) == 6 and view['total'] == 31
    assert view['start'] == 26 and view['end'] == 31
    assert view['summary'] == dict(open=32, unseen=31, review=1, unknown=1)
    exported = json.loads(''.join(export_json(store, view)))
    assert len(exported['episodes']) == 31  # every match, including the other page
    assert all(row['source'] == 'desk fixtures' for row in exported['episodes'])
    assert workspace(store, {'scope': 'review'})['total'] == 1
    assert workspace(store, {'scope': 'unknown'})['total'] == 1
    assert workspace(store, {'scope': 'unseen'})['total'] == 31
    for text in ('%', '_', "' OR 1=1 --"):
        assert workspace(store, {'q': text})['total'] == 0
    assert workspace(store, {'q': 'ATLAS'})['total'] == 31
    store.db.close()


def test_review_closes_only_memory_episode_and_retains_timeline(tmp_path):
    store = Alerts(tmp_path / 'alerts.db')
    seed(store)
    view = workspace(store, {'scope': 'review'})
    identifier = view['selected']['id']
    store.acknowledge(identifier, 'jordan')
    assert workspace(store, {'scope': 'review'})['total'] == 1
    store.review(identifier, 'jordan')
    view = workspace(store, {'scope': 'history', 'selected': identifier})
    assert view['total'] == 1 and view['selected']['state'] == 'reviewed'
    assert 'not proof' in view['selected']['recovery']
    assert [title for title, _ in view['timeline']].count('Incident reviewed') == 1
    store.db.close()


@pytest.mark.parametrize('url', ['https://evil.test/alerts', '//evil.test/alerts', '/alerts/anything', 'https://[', '/other'])
def test_action_return_stays_on_alerts(url):
    assert return_url(url) == '/alerts'


def test_live_filters_export_permissions_and_return_context(desk):
    server, _, _ = desk
    rows = seed(server.app.state.alerts, 31)
    sign_in(server, 'pat')
    assert server.get('/alerts?format=json').status_code == 403
    sign_in(server, 'jordan')
    response = server.get('/alerts?source=desk+fixtures&page=2')
    assert response.status_code == 200 and '26–31 of 31' in response.text
    assert len(server.get('/alerts?source=desk+fixtures&format=json').json()['episodes']) == 31
    assert server.get('/alerts?scope=unsupported').status_code == 400
    destination = '/alerts?scope=all&source=desk+fixtures&q=atlas&page=2&selected=' + rows[0]['id']
    posted = server.post('/alerts/' + rows[0]['id'], data={'do': 'acknowledge', 'return_to': destination}, follow_redirects=False)
    query = parse_qs(urlsplit(posted.headers['location']).query)
    assert query['source'] == ['desk fixtures'] and query['page'] == ['2'] and query['q'] == ['atlas']
    assert query['selected'] == [rows[0]['id']]
    escaped = server.get('/alerts?q=%3Cscript%3Ealert(1)%3C%2Fscript%3E')
    assert '<script>alert(1)</script>' not in escaped.text


@pytest.mark.parametrize('width', [1440, 390])
def test_live_selection_acknowledgement_review_themes_and_native_fallback(desk, tmp_path, width):
    from test_console_browser import browser_context, playwright
    server, _, _ = desk
    rows = seed(server.app.state.alerts, 8)
    manual = next(row for row in rows if row['manual'])
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto('http://console.test/alerts?source=desk+fixtures')
        page.locator(f'[data-open="{manual["id"]}"]').focus()
        page.keyboard.press('Enter')
        playwright.expect(page.locator('#inspector [data-title]')).to_have_text(manual['title'])
        stamp = page.locator('#inspector [data-first-at] time')
        playwright.expect(stamp).to_have_text(stamp.evaluate('el => SuiteTime.format(el.dateTime)'))
        if width == 390:
            playwright.expect(page.locator('#inspector')).to_be_focused()
        page.locator('#inspector [value="acknowledge"]').click()
        playwright.expect(page.locator('#inspector')).to_contain_text('Acknowledged by jordan')
        assert next(row for row in server.app.state.alerts.rows(open_only=True) if row['id'] == manual['id'])['acknowledged_at']
        page.locator('#inspector [value="review"]').click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Incident reviewed')
        page.locator('#scope').select_option('history')
        page.get_by_role('button', name='Filter', exact=True).click()
        playwright.expect(page.locator('#inspector [data-state]')).to_have_text('reviewed')
        playwright.expect(page.locator('#inspector')).to_contain_text('not proof of memory recovery')
        playwright.expect(page.locator('#inspector [value="review"]')).to_have_count(0)
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            playwright.expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'alert-desk-{width}.png'), full_page=True)
        server.app.state.alerts.observe('desk fixtures', None, now=1100)
        page.goto('http://console.test/alerts?source=desk+fixtures&scope=unknown')
        playwright.expect(page.locator('#inspector [data-unknown]')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://console.test/alerts?source=desk+fixtures&q=missing-fixture')
        playwright.expect(page.locator('#inspector')).to_contain_text('No episode selected')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
        # Server-rendered queue links and actions remain usable without JavaScript.
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(java_script_enabled=False, viewport={'width': width, 'height': 1000})
        def serve(route):
            response = server.get(route.request.url.replace('http://console.test', ''))
            route.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get('content-type', 'text/html'))
        context.route('http://console.test/**', serve)
        native = context.new_page()
        native.goto('http://console.test/alerts?source=desk+fixtures')
        native.locator('#episode-list [data-open]').last.click()
        assert parse_qs(urlsplit(native.url).query)['source'] == ['desk fixtures']
        assert native.locator('#inspector [data-title]').inner_text()
        assert native.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
