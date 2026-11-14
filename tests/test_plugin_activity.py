"""Live Plugin logs: complete-set filtering, ownership and compact Activity desk interactions."""

import csv
import io
import json
import re
import time

import pytest

from test_weightroom import desk, sign_in  # noqa: F401


@pytest.fixture
def activity_desk(desk):
    server, _, users = desk
    registry = server.app.state.plugins
    users.change('pat', own={'add': ['front-plugins']})
    now = time.time()
    for name, count in (('pat', 32), ('jordan', 4)):
        principal = {'name': name, 'identity': users.identity(name)}
        for index in range(count):
            registry.record(principal, 'hermes:research' if index % 2 else 'hermes:jeeves', 'hermes',
                            'research' if index % 2 else 'jeeves', 'default',
                            ('completed', 'failed', 'interrupted')[index % 3],
                            f'{name}-session-{index // 4}', f'{name}-request-{index:02d}')
            with registry.db:
                registry.db.execute('UPDATE uses SET at = ? WHERE id = (SELECT MAX(id) FROM uses)',
                                    (now - (10 * 86400 if index == 31 else index * 3600),))
    sign_in(server, 'pat')
    return server, users, registry


def record_ids(response):
    return [int(value) for value in re.findall(r'data-record-id="(\d+)"', response.text)]


def selected_record(response):
    return json.loads(re.search(r'<script type="application/json" data-selected-json>(.*?)</script>', response.text, re.S)[1])


def test_authorized_filters_counts_sort_and_pages_cover_all_retained_rows(activity_desk):
    server, _, _ = activity_desk
    response = server.get('/plugins/activity?limit=10')
    assert response.status_code == 200 and len(record_ids(response)) == 10
    assert '32 results' in response.text
    assert 'jordan-request' not in response.text and '<option value="jordan">' not in response.text
    assert 'OBSERVE / PLUGINS' not in response.text and 'In the current filter' not in response.text
    following = server.get('/plugins/activity?limit=10&page=2')
    assert set(record_ids(response)).isdisjoint(record_ids(following))
    filtered = server.get('/plugins/activity?limit=10&outcome=failed')
    assert len(record_ids(filtered)) == 10 and '11 results' in filtered.text
    assert selected_record(filtered)['outcome'] == 'failed'
    last = server.get('/plugins/activity?limit=10&outcome=failed&page=999999')
    assert len(record_ids(last)) == 1 and 'Page 2 of 2' in last.text
    recent = server.get('/plugins/activity?period=24')
    assert '24 results' in recent.text
    week = server.get('/plugins/activity?period=168')
    assert '31 results' in week.text
    session = server.get('/plugins/activity?session=pat-session-7')
    assert '4 results' in session.text
    ascending = server.get('/plugins/activity?limit=10&sort=at&direction=asc')
    assert selected_record(ascending)['request'] == 'pat-request-31'
    searched = server.get('/plugins/activity?q=pat-request-27')
    assert record_ids(searched) == [28]
    assert not record_ids(server.get('/plugins/activity?q=%'))
    assert not record_ids(server.get('/plugins/activity?q=%27%20OR%201=1--'))
    assert server.get('/plugins/activity?sort=at%3BDROP+TABLE+uses&direction=oops').status_code == 200


def test_exports_selection_and_choices_keep_the_same_owner_boundary(activity_desk):
    server, users, registry = activity_desk
    export = server.get('/plugins/activity?limit=10&page=2&instance=jeeves&export=csv')
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 16
    assert all(row['user'] == 'pat' and row['instance'] == 'jeeves' for row in rows)
    assert export.headers['cache-control'] == 'no-store'
    assert 'owner' not in rows[0]
    assert server.get('/plugins/activity?export=json&record=33').status_code == 404
    assert 'jordan-request' not in server.get('/plugins/activity?record=33').text
    assert server.get('/plugins/activity?export=json&record=99999999999999999999999999').status_code == 404
    record = server.get('/plugins/activity?export=json&record=28')
    assert record.status_code == 200 and record.json()['request'] == 'pat-request-27'
    assert 'owner' not in record.json()
    assert server.get('/plugins/activity?outcome=failed&record=28&export=json').status_code == 404
    registry.record({'name': 'pat', 'identity': users.identity('pat')}, '=MODEL()', 'hermes', 'jeeves',
                    '@profile', 'completed', None, 'quoted,"request\nline')
    escaped = list(csv.DictReader(io.StringIO(server.get('/plugins/activity?export=csv').text)))[0]
    assert escaped['model'] == "'=MODEL()" and escaped['profile'] == "'@profile"
    assert escaped['request'] == 'quoted,"request\nline' and escaped['session'] == ''
    sign_in(server, 'jordan')
    assert len(list(csv.DictReader(io.StringIO(server.get('/plugins/activity?export=csv').text)))) == 37
    users.change('pat', own={'add': []})
    sign_in(server, 'pat')
    for query in ('', '?record=1', '?export=csv', '?record=1&export=json'):
        assert server.get('/plugins/activity' + query).status_code == 403


def test_empty_and_unmatched_records_remain_usable(desk):
    server, _, users = desk
    sign_in(server, 'jordan')
    empty = server.get('/plugins/activity')
    assert empty.status_code == 200 and 'No plugin requests recorded yet.' in empty.text
    assert 'data-has-detail="false"' in empty.text
    assert 'aria-disabled="true"' in empty.text
    server.app.state.plugins.record({'name': 'jordan', 'identity': users.identity('jordan')},
                                    'hermes:local', 'hermes', 'local', 'default', 'interrupted')
    unmatched = server.get('/plugins/activity?q=unknown')
    assert 'No requests match these filters.' in unmatched.text
    no_ids = server.get('/plugins/activity')
    assert no_ids.text.count('Not recorded') == 2
    assert 'Filter session</a>' not in no_ids.text


def test_lifecycle_outcomes_are_filterable_without_inventing_request_totals(activity_desk):
    server, users, registry = activity_desk
    for value in ('started', 'refused', 'unavailable'):
        registry.record({'name': 'pat', 'identity': users.identity('pat')},
                        'hermes:local', 'hermes', 'local', 'default', value, None, 'same-request')
        response = server.get('/plugins/activity', params={'outcome': value})
        assert response.status_code == 200 and len(record_ids(response)) == 1
        assert selected_record(response)['outcome'] == value
    summary = registry.activity_summary({'name': 'pat', 'identity': users.identity('pat')})
    assert summary == {'total':35, 'completed':11, 'failed':11, 'interrupted':10}
    assert '>Records</span>' in server.get('/plugins/activity').text


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_live_desk_filters_selection_mobile_and_history(activity_desk, tmp_path, width):
    from test_console_browser import browser_context, playwright
    server, _, _ = activity_desk
    expect = playwright.expect
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/plugins/activity?limit=10')
        expect(page.locator('#total-count')).to_have_text('32')
        expect(page.locator('#plugin-log-rows tr')).to_have_count(10)
        page.locator('#plugin-outcome').select_option('failed')
        expect(page.locator('#total-count')).to_have_text('11')
        expect(page.locator('#failed-count')).to_have_text('11')
        expect(page.locator('#completed-count')).to_have_text('0')
        expect(page.locator('nav[aria-label="Results pages"]')).to_contain_text('Page 1 of 2')
        page.get_by_role('link', name='Next', exact=True).click()
        expect(page.locator('#plugin-log-rows tr')).to_have_count(1)
        page.locator('#plugin-log-rows [data-open]').click()
        expect(page.locator('[data-fact="request"]')).to_have_text('pat-request-31')
        if width < 1200:
            expect(page.locator('#plugin-detail-dialog')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#plugin-detail-dialog')).not_to_be_visible()
        else:
            expect(page.locator('#plugin-detail')).to_be_visible()
        page.get_by_role('link', name='Clear', exact=True).click()
        expect(page.locator('#total-count')).to_have_text('32')
        page.wait_for_function("!document.querySelector('.htmx-settling')")
        page.locator('#plugin-search').fill('pat-request-27')
        expect(page.locator('#total-count')).to_have_text('1')
        page.locator('#plugin-log-rows [data-open]').click()
        expect(page.locator('[data-fact="request"]')).to_have_text('pat-request-27')
        if width < 1200:
            page.get_by_role('button', name='Close', exact=True).click()
        page.go_back()
        expect(page.locator('#plugin-search')).to_have_value('pat-request-27')
        page.get_by_role('link', name='Clear', exact=True).click()
        expect(page.locator('#total-count')).to_have_text('32')
        page.wait_for_function("!document.querySelector('.htmx-settling')")
        page.get_by_role('link', name='Sort by When', exact=True).click()
        expect(page.locator('#plugin-log-rows [data-open]').first).to_have_attribute('aria-label', 'Inspect record 32, failed, pat')
        page.locator('#plugin-limit').select_option('10')
        expect(page.locator('#plugin-log-rows tr')).to_have_count(10)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.evaluate('''() => {
            const ids = [...document.querySelectorAll('[id]')].map(el=>el.id);
            return ids.length===new Set(ids).size;
        }''')
        page.emulate_media(color_scheme='dark')
        expect(page.locator('html')).to_have_attribute('data-bs-theme', 'dark')
        dark = page.locator('.pl-inspector').evaluate('(el)=>getComputedStyle(el).backgroundColor')
        page.emulate_media(color_scheme='light')
        expect(page.locator('html')).to_have_attribute('data-bs-theme', 'light')
        assert page.locator('.pl-inspector').evaluate('(el)=>getComputedStyle(el).backgroundColor') != dark
        page.screenshot(path=str(tmp_path / f'plugin-activity-{width}.png'), full_page=True)
        assert not errors
        browser.close()


def test_without_javascript_filters_and_record_links_work(activity_desk):
    from test_console_browser import browser_context, playwright
    server, _, _ = activity_desk
    expect = playwright.expect
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(java_script_enabled=False, viewport={'width':390,'height':1000})
        def serve(route):
            from urllib.parse import urlsplit
            url = urlsplit(route.request.url)
            response = server.get(url.path + ('?' + url.query if url.query else ''))
            route.fulfill(status=response.status_code, content_type=response.headers.get('content-type'), body=response.content)
        context.route('http://console.test/**', serve)
        page = context.new_page()
        page.goto('http://console.test/plugins/activity')
        page.locator('#plugin-outcome').select_option('failed')
        page.get_by_role('button', name='Apply', exact=True).click()
        expect(page.locator('#total-count')).to_have_text('11')
        page.locator('#plugin-log-rows [data-open]').first.click()
        expect(page.locator('#plugin-detail')).to_be_visible()
        expect(page.locator('[data-fact="request"]')).to_have_text('pat-request-01')
        browser.close()
