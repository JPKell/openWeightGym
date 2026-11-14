"""The live Operations ledger preserves recorded security, authorization and diagnostic evidence."""

import csv
import io
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from loadcoach.log import Log, public_number
from loadcoach.request_filters import parse
from test_weightroom import desk, sign_in  # noqa: F401


def record(log, level='normal', user='pat', outcome='done', total=2.5):
    entry = log.add('request', 'asked', user, '198.51.100.4', 'ledger-model', level=level,
                    trace='ledger-trace', path='chat/completions')
    if outcome:
        log.add('request', outcome, user, '198.51.100.4', 'ledger-model', of=entry, level=level,
                status=500 if outcome == 'failed' else 200, prompt_tokens=1200, answer_tokens=64,
                diagnostics={'version': 1, 'total_seconds': total, 'first_output_seconds': .8,
                             'phases': [{'stage': 'queue', 'seconds': .25, 'offset': 0},
                                        {'stage': 'generation', 'seconds': total-.25, 'offset': .25}]})
    log.keep_asked(entry, json.dumps({'messages': [{'role': 'user', 'content': 'PRIVATE_LEDGER_PROMPT'}]}))
    log.keep_said(entry, {'content': 'PRIVATE_LEDGER_ANSWER'})
    return public_number(None, entry)


@pytest.mark.parametrize('level', ('normal', 'sensitive', 'restricted', 'unrecorded'))
def test_security_filter_accepts_only_recorded_tiers(level):
    assert parse({'level': level})['level'] == level
    with pytest.raises(ValueError):
        parse({'level': 'public OR 1=1'})


def test_level_filter_search_pagination_and_metadata_are_scoped(tmp_path):
    log = Log(str(tmp_path/'events.db'))
    ids = [record(log, level=level) for level in ('normal', 'sensitive', 'restricted', None, 'restricted')]
    record(log, level='restricted', user='jordan')
    found = log.requests('pat', 1, offset=1, filters=parse({'level': 'restricted'}))
    assert found['total'] == 2 and found['page'] == 2
    assert found['requests'][0]['id'] == ids[2]
    row = found['requests'][0]
    assert row['level'] == 'restricted' and row['compute_total'] == 2.5 and row['queue_seconds'] == .25
    assert row['first_output_seconds'] == .8 and row['prompt_tokens'] == 1200
    assert 'PRIVATE_LEDGER' not in json.dumps(found)
    assert log.requests('pat', 25, filters={'level': 'unrecorded'})['requests'][0]['id'] == ids[3]
    assert log.requests('pat', 25, filters={'q': str(ids[0])})['requests'][0]['id'] == ids[0]
    assert log.requests('pat', 25, filters={'q': 'ledger-trace'})['total'] == 5
    assert log.requests('pat', 25, filters={'q': '%'})['total'] == 0
    assert log.requests('pat', 25, filters={'q': "' OR 1=1 --"})['total'] == 0
    log.db.close()


def test_page_metrics_keep_benchmark_active_and_cancelled_outcomes_separate():
    from weightroom.request_views import listing, detail
    stats = listing([{'outcome':'done', 'compute_total':2.5},
                     {'outcome':'failed', 'compute_total':8.5, 'server_error':True},
                     {'outcome':'stopped', 'compute_total':1},
                     {'outcome':None, 'compute_total':999},
                     {'outcome':'done', 'compute_total':1000, 'benchmark':True}])
    assert stats['shown'] == 5 and stats['sample'] == 4 and stats['endings'] == 3
    assert stats['unfinished'] == 1 and stats['measured'] == 3 and stats['p95'] == 8.5
    assert stats['errors'] == 1 and stats['error_percent'] == 33.3
    cancelled = detail({'id':1, 'asked':{}, 'state':None,
                        'ended':{'what':'stopped', 'why':'Cancelled by the caller.'}})
    assert not cancelled['error'] and cancelled['note'] == 'Cancelled by the caller.'


def test_security_api_ui_and_exports_preserve_permissions(desk):
    server, switch, users = desk
    log = switch.front.state.log
    log.keep_text = 5
    own = record(log, level='sensitive')
    other = record(log, level='restricted', user='jordan')
    sign_in(server, 'pat')
    api = TestClient(switch.front)
    auth = {'authorization': 'Bearer '+server.cookies['weightroom']}
    found = api.get('/loadcoach/requests?level=sensitive', headers=auth).json()
    assert found['total'] == 1 and found['requests'][0]['id'] == own
    assert found['choices']['level'] == ['normal', 'sensitive', 'restricted']
    assert api.get('/loadcoach/requests?level=restricted', headers=auth).json()['total'] == 0
    assert api.get('/loadcoach/requests?level=invalid', headers=auth).status_code == 400
    api.close()
    html = server.get('/requests?complete=1&level=sensitive').text
    assert 'value="sensitive" selected' in html and 'data-security-level="sensitive"' in html
    assert 'Secure' in html and 'PRIVATE_LEDGER' not in html
    assert 'data-hold-refresh' in html and 'Find requests' in html
    page = server.get(f'/requests/{own}')
    assert page.status_code == 200 and 'data-security-level="sensitive"' in page.text
    assert 'PRIVATE_LEDGER_PROMPT' in page.text
    export = server.get(f'/requests/{own}?export=metadata')
    assert export.status_code == 200 and export.json()['asked']['level'] == 'sensitive'
    assert 'PRIVATE_LEDGER' not in export.text
    assert server.get(f'/requests/{other}?export=metadata').status_code == 403
    rows = list(csv.DictReader(io.StringIO(server.get('/requests?level=sensitive&export=csv').text)))
    assert len(rows) == 1 and rows[0]['level'] == 'sensitive'
    sign_in(server, 'jordan')
    users.change('jordan', own={'remove': ['see-capture']})
    restricted = server.get(f'/requests/{own}')
    assert restricted.status_code == 200 and 'Security level' in restricted.text
    assert 'PRIVATE_LEDGER_PROMPT' not in restricted.text and 'PRIVATE_LEDGER_ANSWER' not in restricted.text
    assert 'PRIVATE_LEDGER' not in server.get(f'/requests/{own}?export=metadata').text


def test_missing_security_is_not_inferred_and_live_fragment_keeps_security(desk):
    server, switch, _ = desk
    public = record(switch.front.state.log)
    missing = record(switch.front.state.log, level=None, outcome=None)
    sign_in(server, 'pat')
    normal = server.get(f'/requests/{public}')
    assert 'data-security-level="normal"' in normal.text and 'Public' in normal.text
    html = server.get(f'/requests/{missing}?part=live').text
    assert 'data-security-level="unrecorded"' in html and 'Not recorded' in html
    assert 'No ending is recorded.' in html and 'Stop this request</button>' not in html
    assert 'data-security-level="normal"' not in html


def test_ledger_and_detail_browser_navigation_and_layout(desk, tmp_path):
    from test_console_browser import browser_context, playwright
    server, switch, _ = desk
    for level in ('normal', 'sensitive', 'restricted'):
        record(switch.front.state.log, level=level)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/requests')
        page.get_by_label('Filter security level', exact=True).select_option('restricted')
        playwright.expect(page.locator('#request-ledger-table tbody tr')).to_have_count(1)
        playwright.expect(page.locator('#request-ledger-table [data-security-level]')).to_have_text('Restricted')
        page.locator('#request-ledger-table a').click()
        playwright.expect(page.locator('.rq-detail-meta [data-security-level]')).to_have_text('Restricted')
        for tab in ('content', 'activity', 'raw', 'diagnostics'):
            page.locator(f'[data-request-tab="{tab}"]').click()
            playwright.expect(page.locator(f'[data-request-panel="{tab}"]')).to_be_visible()
        for width in (1440, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            page.screenshot(path=str(tmp_path/f'ledger-detail-{width}.png'), full_page=True)
        page.locator('[data-request-back]').click()
        playwright.expect(page.get_by_label('Filter security level', exact=True)).to_have_value('restricted')
        for width in (1440, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        assert not errors
        browser.close()


def test_polling_preserves_evidence_tab_and_recorded_security(desk):
    from test_console_browser import browser_context, playwright
    server, switch, _ = desk
    entry = record(switch.front.state.log, level='restricted')
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto(f'http://console.test/requests/{entry}')
        page.locator('[data-request-tab="raw"]').click()
        page.locator('#request').evaluate("e => e.dataset.beforePoll = 'yes'")
        with page.expect_response(lambda response: f'/requests/{entry}?part=live' in response.url):
            page.evaluate(f"htmx.ajax('GET','/requests/{entry}?part=live',{{target:'#request',swap:'outerHTML'}})")
        playwright.expect(page.locator('#request')).not_to_have_attribute('data-before-poll', 'yes')
        playwright.expect(page.locator('[data-request-panel="raw"]')).to_be_visible()
        playwright.expect(page.locator('[data-request-tab="raw"]')).to_have_attribute('aria-pressed', 'true')
        playwright.expect(page.locator('.rq-detail-meta [data-security-level]')).to_have_text('Restricted')
        browser.close()
