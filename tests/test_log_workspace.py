"""Real AI Logs trace navigation, retained search, exports, and authorization."""

import csv
import io
import json
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

from loadcoach.log import Log
from test_console_browser import browser_context, playwright
from test_ui_consistency import MEASURE
from test_weightroom import desk, sign_in, versioned_script  # noqa: F401


@pytest.fixture
def recorded(desk, tmp_path):
    browser, _, _ = desk
    log = Log(str(tmp_path / 'data' / 'accounts.db'))
    asked = log.add('request', 'asked', 'pat', model='qwen3', trace='trace-a', input_tokens=512)
    tool = log.add('tool', 'failed', 'pat', model='qwen3', trace='trace-a', of=asked,
                   error='<img src=x onerror="window.injected=true"> 100%_literal', code='tool_timeout')
    done = log.add('request', 'done', 'pat', model='qwen3', trace='trace-a', of=asked, duration=.123)
    lone = log.add('model', 'loaded', 'pat', model='other', duration=0)
    foreign = log.add('request', 'failed', 'jordan', model='private-model', trace='trace-a',
                      error='foreign evidence')
    log.keep_asked(asked, json.dumps({'messages': [{'content': 'private captured words'}]}))
    return browser, log, dict(asked=asked, tool=tool, done=done, lone=lone, foreign=foreign)


def test_authorized_workspace_filters_search_literal_text_and_exports(recorded):
    browser, log, ids = recorded
    sign_in(browser, 'pat')
    page = browser.get('/log?complete=1&what=failed')
    assert page.status_code == 200
    assert 'Observe / Recorded activity' not in page.text
    assert 'Trace timeline' in page.text and 'Event inspector' in page.text
    assert 'foreign evidence' not in page.text and 'private-model' not in page.text
    assert page.text.count('data-event-id=') == 3  # Exact trace context includes asked/done outside filters.
    assert '&lt;img src=x' in page.text and '<img src=x' not in page.text
    assert 'private captured words' not in page.text
    found = browser.get('/log?q=100%25_literal&export=csv')
    rows = list(csv.DictReader(io.StringIO(found.text)))
    assert [int(row['id']) for row in rows] == [ids['tool']]
    assert browser.get('/log?q=private%20captured%20words&export=csv').text.count('\n') == 1
    event = browser.get(f"/log?selected={ids['tool']}&export=json")
    assert event.status_code == 200 and event.json()['id'] == ids['tool']
    assert 'text' not in event.json() and event.headers['cache-control'] == 'no-store'
    assert browser.get(f"/log?selected={ids['foreign']}&export=json").status_code == 403
    assert browser.get('/log?selected=not-a-number&complete=1').status_code == 400
    assert browser.get('/log?start=2026-11-13T12:00&end=2026-11-13T10:00&complete=1').status_code == 400
    sign_in(browser, 'jordan')
    assert 'foreign evidence' in browser.get('/log?complete=1&trace=trace-a').text
    log.add('model', 'failed', 'jordan', model='=1+1', error='formula')
    exported = list(csv.DictReader(io.StringIO(browser.get('/log?q=formula&export=csv').text)))
    assert exported[0]['model'] == "'=1+1"


def test_trace_navigation_refresh_and_mobile_preserve_selected_evidence(recorded, tmp_path):
    server, log, ids = recorded
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/log?what=failed')
        playwright.expect(page.locator('#detail-id')).to_have_text(f"#{ids['tool']}")
        playwright.expect(page.locator('#timeline-events li')).to_have_count(3)
        page.locator(f'[data-event-id="{ids["asked"]}"] a').click()
        playwright.expect(page.locator('#detail-id')).to_have_text(f"#{ids['asked']}")
        assert parse_qs(urlsplit(page.url).query)['selected'] == [str(ids['asked'])]
        page.locator('#log-raw summary').click()
        # An explicit fragment refresh follows the same route as the five-second poll.
        page.get_by_role('link', name='Refresh now', exact=True).click()
        playwright.expect(page.locator('#detail-id')).to_have_text(f"#{ids['asked']}")
        assert page.locator('#log-raw').evaluate('(el)=>el.open')
        page.get_by_role('link', name='Reset', exact=True).click()
        page.locator(f'#trace-choices a[href*="selected={ids["lone"]}"]').click()
        playwright.expect(page.locator('#timeline-note')).to_contain_text('No trace ID was recorded')
        playwright.expect(page.locator('#timeline-events li')).to_have_count(1)
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            measured = page.evaluate(MEASURE)
            for key in ('mismatches', 'invalidControls', 'rawValues', 'namelessFields'):
                assert not measured[key], (width, key, measured[key])
        page.locator('[data-log-more]').click()
        page.locator('#log-trace').fill('trace-a')
        page.get_by_role('button', name='Apply filters', exact=True).click()
        playwright.expect(page.locator('#trace-count')).to_have_text('1 trace · 0 untraced events')
        page.locator('#log-q').fill('nothing matches this')
        page.get_by_role('button', name='Apply filters', exact=True).click()
        playwright.expect(page.locator('#trace-choices')).to_contain_text('Nothing on record that fits.')
        playwright.expect(page.locator('#inspector-content')).to_be_hidden()
        assert not errors
        browser.close()


def test_history_limit_and_search_apply_before_the_result_window(recorded):
    browser, log, ids = recorded
    for i in range(25):
        log.add('model', 'loaded', 'pat', model=f'noise-{i}')
    sign_in(browser, 'pat')
    page = browser.get('/log?q=tool_timeout&limit=10&complete=1')
    assert page.status_code == 200 and f'#{ids["tool"]}' in page.text
    page = browser.get(f'/log?before={ids["tool"]}&complete=1')
    assert 'Historical results' in page.text
    root = page.text.split('id="log"', 1)[1].split('>', 1)[0]
    assert 'hx-get=' not in root
    assert versioned_script('log.html', 'log') in page.text


def test_trace_context_failure_is_explicit_and_filters_remain_readable(recorded, monkeypatch):
    from turnstile.reporting import ReportTimeout

    browser, _, ids = recorded
    original = Log.read

    def unavailable(self, *args, **kwargs):
        if kwargs.get('trace') or len(args) > 4 and args[4]:
            raise ReportTimeout
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Log, 'read', unavailable)
    sign_in(browser, 'pat')
    page = browser.get('/log?what=failed&complete=1')
    assert page.status_code == 200
    assert 'Trace context could not be loaded' in page.text
    assert page.text.count('data-event-id=') == 1
    assert f'#{ids["tool"]}' in page.text


def test_table_uses_timestamp_order_across_pages_and_exports(recorded):
    class Document(HTMLParser):
        def __init__(self, text):
            super().__init__()
            self.elements = []
            self.feed(text)

        def handle_starttag(self, tag, attrs):
            self.elements.append((tag, dict(attrs)))

        def events(self):
            return [int(attrs['data-event-id']) for _, attrs in self.elements if 'data-log-table-row' in attrs]

    browser, log, _ = recorded
    ids = [log.write((at, 'ordering', 'recorded', 'pat', 'web', 'sample', None, None, '{}', None))
           for at in (400, 100, 300, 300, 200)]
    sign_in(browser, 'pat')
    response = browser.get('/log?view=table&kind=ordering&limit=2&complete=1')
    assert response.status_code == 200
    document = Document(response.text)
    assert not any(attrs.get('id') in ('trace-list', 'trace-timeline', 'inspector') for _, attrs in document.elements)
    assert document.events() == [ids[0], ids[3]]
    older = next(attrs['href'] for tag, attrs in document.elements if tag == 'a' and 'before=' in attrs.get('href', ''))
    older_response = browser.get(older + '&complete=1')
    assert Document(older_response.text).events() == [ids[2], ids[4]]
    assert 'automatic refresh paused' in older_response.text
    exported = list(csv.DictReader(io.StringIO(browser.get('/log?view=table&kind=ordering&export=csv').text)))
    assert [int(row['id']) for row in exported] == [ids[0], ids[3], ids[2], ids[4], ids[1]]
    dated = list(csv.DictReader(io.StringIO(browser.get('/log?kind=ordering&q=1970-01-01&q_field=at&export=csv').text)))
    assert [int(row['id']) for row in dated] == [int(row['id']) for row in exported]


def test_search_field_scopes_literal_matches_and_rejects_unknown_fields(recorded):
    from loadcoach.event_filters import DETAIL_FIELDS

    browser, log, ids = recorded
    first = log.add('model', 'loaded', 'pat', model='needle', error='unrelated', duration=0)
    second = log.add('model', 'failed', 'pat', model='other', error='needle')
    sign_in(browser, 'pat')
    for field, expected in [('model', [first]), ('error', [second]), ('duration', [first, ids['lone'], ids['done']])]:
        query = '0' if field == 'duration' else 'needle'
        response = browser.get('/log', params={'q': query, 'q_field': field, 'export': 'csv'})
        assert response.status_code == 200
        assert [int(row['id']) for row in csv.DictReader(io.StringIO(response.text))] == expected
    literal = browser.get('/log?q=100%25_literal&q_field=error&export=csv')
    assert [int(row['id']) for row in csv.DictReader(io.StringIO(literal.text))] == [ids['tool']]
    assert browser.get('/log?q=jordan&q_field=user&export=csv').text.count('\n') == 1
    for field in ('text', "error') OR 1=1 --"):
        assert browser.get('/log', params={'q_field': field, 'export': 'csv'}).status_code == 400
    assert browser.get('/log?q=private%20captured%20words&q_field=details&export=csv').text.count('\n') == 1
    # Every advertised detail field narrows to that JSON key, rather than matching other fields.
    detail = {key: 'field-probe' for key, _ in DETAIL_FIELDS if key != 'details'}
    probe = log.add('model', 'loaded', 'pat', model='other', **detail)
    log.add('model', 'loaded', 'pat', model='field-probe')
    for field, _ in DETAIL_FIELDS:
        assert [row['id'] for row in log.read(user='pat', q='field-probe', q_field=field)] == [probe], field


def test_table_switch_double_click_keyboard_and_field_filters_survive_refresh(recorded):
    server, _, ids = recorded
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/log?what=failed')
        page.get_by_role('link', name='Table', exact=True).click()
        playwright.expect(page.locator('#log-table')).to_be_visible()
        playwright.expect(page.locator('#trace-list')).to_have_count(0)
        playwright.expect(page.locator('#trace-timeline')).to_have_count(0)
        playwright.expect(page.locator('#inspector')).to_have_count(0)
        playwright.expect(page.locator('#log-view-choice')).to_have_value('table')
        page.locator(f'[data-log-table-row][data-event-id="{ids["tool"]}"] td').last.dblclick()
        playwright.expect(page.locator('#detail-id')).to_have_text(f"#{ids['tool']}")
        playwright.expect(page.locator('#detail-title')).to_be_focused()
        playwright.expect(page.locator(f'[data-event-id="{ids["tool"]}"] a')).to_have_attribute('aria-current', 'true')
        page.get_by_role('link', name='Table', exact=True).click()
        row = page.locator('[data-log-table-row]').first
        row.focus(); page.keyboard.press('Enter')
        playwright.expect(page.locator('#detail-id')).to_have_text(f"#{ids['tool']}")
        page.get_by_role('link', name='Table', exact=True).click()
        page.locator('#log-q-field').select_option('code')
        page.locator('#log-q').fill('tool_timeout')
        page.get_by_role('button', name='Apply filters', exact=True).click()
        playwright.expect(page.locator('#log-table tbody tr')).to_have_count(1)
        playwright.expect(page.locator('#log-q-field')).to_have_value('code')
        page.get_by_role('link', name='Refresh now', exact=True).click()
        playwright.expect(page.locator('#log-table tbody tr')).to_have_count(1)
        assert parse_qs(urlsplit(page.url).query)['q_field'] == ['code']
        page.reload()
        playwright.expect(page.locator('#log-q-field')).to_have_value('code')
        playwright.expect(page.locator('#log-table')).to_be_visible()
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            measured = page.evaluate(MEASURE)
            assert not measured['invalidControls'] and not measured['namelessFields'], measured
        page.locator('#log-table-scroll').evaluate('(el)=>el.scrollLeft=160')
        page.get_by_role('link', name='Refresh now', exact=True).click()
        page.wait_for_function('document.getElementById("log-table-scroll").scrollLeft >= 150')
        page.locator('#log-q').fill('nothing matches')
        page.get_by_role('button', name='Apply filters', exact=True).click()
        playwright.expect(page.locator('#log-table')).to_contain_text('No events match these filters')
        assert not errors
        browser.close()
