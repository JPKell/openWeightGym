"""Authorized reliability tables, chart rendering and feedback in the existing console."""

import hashlib
import csv
import io
import json
import re
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest

from loadcoach.log import Log
from loadcoach.models import Models
from loadcoach.reliability import Reliability
from loadcoach.roles import Roles
from test_console_browser import browser_context
from test_weightroom import desk, sign_in  # noqa: F401


def seed(tmp_path, users):
    database = str(tmp_path/'loadcoach.db')
    models = Models(database, {'flash-attn': 'on'})
    path = tmp_path/'small.gguf'; path.write_bytes(b'fake-model')
    models.put('small', {'gpus': 0, 'flags': {'model': str(path), 'ctx-size': 2048}})
    stat = path.stat()
    models.keep_hash(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime)
    fp = models.fingerprint(models.get('small'), 'small')
    front = str(tmp_path/'data'/'roles.db')  # roles and reliability are kept on the front machine, beside the accounts
    Roles(front).put('chat', {'models': {'small': 1}})
    log = Log(database)
    kept = Reliability(sqlite3.connect(front))
    key = (models.identity('small'), fp, 'chat')
    numbers = {}
    for name, result in [('pat', 'success'), ('jordan', 'empty'), ('jordan', 'timeout'), ('jordan', 'error')]:
        number = log.add('request', 'asked', name, model='small', role='chat', fingerprint=fp)
        log.add('request', 'done' if result == 'success' else 'failed', name, model='small', of=number, reliability_outcome=result)
        kept.record(number, name, 'small', key, 0, result, owner_id=users.identity(name))
        numbers[name] = number
    return kept, key, numbers


def test_report_feedback_and_reset_follow_permissions_and_keep_history(desk, tmp_path):
    browser, _, users = desk
    kept, key, numbers = seed(tmp_path, users)
    sign_in(browser, 'pat')
    report = browser.get('/reliability')
    assert report.status_code == 200 and 'Reliability report' in report.text
    assert 'your requests' in report.text and 'id="reliability-data"' in report.text
    assert 'Reset reliability' not in report.text and 'id="reliability-table"' in report.text
    assert 'action="/reliability"' in report.text and 'href="/log?kind=reliability"' in report.text
    log = browser.get('/log').text
    assert 'href="/reliability"' in log
    assert 'id="reliability-table"' not in log and '/static/reliability.js' not in log
    assert 'Helpful' in browser.get(f'/requests/{numbers["pat"]}').text
    assert 'Helpful' in browser.get(f'/requests/{numbers["pat"]}?part=live').text
    assert 'Feedback saved.' in browser.post(f'/requests/{numbers["pat"]}/feedback', data={'value': '-1'}).text
    assert kept.summary(key)['negative'] == 1
    refused = browser.post('/reliability', data={'model': 'small', 'role': 'chat', 'fingerprint': key[1], 'action': 'reset'})
    assert refused.status_code == 400 and 'Your group may not' in refused.text
    assert refused.url.path == '/reliability'
    assert kept.summary(key)['excluded']  # front renders the permission refusal, never applies it
    sign_in(browser, 'jordan')
    report = browser.get('/reliability').text
    assert 'all users' in report and 'Reset reliability' in report
    assert '/reliability?model=small&amp;role=chat' in browser.get('/roles').text
    assert 'Helpful' in browser.get(f'/requests/{numbers["pat"]}').text  # rate-answers: anyone's answer
    rated = browser.post(f'/requests/{numbers["pat"]}/feedback', data={'value': '-1', 'explanation': 'ignored the question'})
    assert 'Feedback saved.' in rated.text and 'ignored the question' in rated.text
    assert kept.summary(key)['negative'] == 1
    quality = browser.get('/reliability').text
    assert 'Answer quality' in quality and 'jordan: ignored the question' in quality
    assert browser.post('/reliability', data={'model': 'small', 'role': 'chat', 'fingerprint': key[1], 'action': 'reset'}, headers={'origin': 'https://elsewhere.invalid'}).status_code == 403
    assert kept.summary(key)['excluded']
    reset = browser.post('/reliability', data={'model': 'small', 'role': 'chat', 'fingerprint': key[1], 'action': 'reset', 'filter_model': 'small', 'filter_role': 'chat', 'filter_issues': '1'})
    assert 'Reliability reset.' in reset.text and reset.url.path == '/reliability'
    assert reset.url.params['model'] == 'small' and reset.url.params['role'] == 'chat' and reset.url.params['issues'] == '1'
    assert kept.summary(key)['factor'] == 1 and not kept.summary(key)['excluded']
    assert kept.db.execute('SELECT COUNT(*) FROM reliability_outcomes').fetchone()[0] == 4
    assert '/static/reliability.js' in browser.get('/reliability?role=chat').text
    assert 'No reliability outcomes' in browser.get('/reliability?role=missing').text
    assert 'Reliability report' not in browser.get('/log?part=list').text
    # Previously rendered action forms still return to the new report page.
    legacy = browser.post('/log/reliability', data={'model': 'small', 'role': 'chat', 'fingerprint': key[1], 'action': 'reset'})
    assert legacy.url.path == '/reliability' and 'Reliability reset.' in legacy.text


def test_issues_only_filters_the_chart_and_table_together(desk, tmp_path):
    browser, _, users = desk
    kept, key, _ = seed(tmp_path, users)
    kept.record(999, 'jordan', 'small', (key[0], key[1], 'clean'), 0, 'success', owner_id=users.identity('jordan'))
    sign_in(browser, 'jordan')
    full = browser.get('/reliability').text
    assert '>clean<' in full and '"role": "clean"' in full
    filtered = browser.get('/reliability?issues=1').text
    assert 'id="reliability-issues" name="issues" value="1" type="checkbox" class="form-check-input" checked' in filtered
    assert '>clean<' not in filtered and '"role": "clean"' not in filtered
    assert '>chat<' in filtered and '"role": "chat"' in filtered


def test_reliability_chart_table_filters_and_mobile_viewport(desk, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    server, _, users = desk
    kept, key, _ = seed(tmp_path, users)
    kept.record(999, 'jordan', 'small', (key[0], key[1], 'clean'), 0, 'success', owner_id=users.identity('jordan'))
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/log')
        page.get_by_role('link', name='Reliability', exact=True).click()
        playwright.expect(page).to_have_url('http://console.test/reliability')
        playwright.expect(page.locator('#reliability-chart .rl-outcome-row')).to_have_count(2)
        assert page.locator('#reliability-table tbody tr').count() == 2
        page.locator('#reliability-issues').check()
        page.get_by_role('button', name='Apply report filters').click()
        page.wait_for_url('**/reliability?**')
        assert parse_qs(urlsplit(page.url).query)['issues'] == ['1']
        assert page.locator('#reliability-table tbody tr').count() == 1
        playwright.expect(page.locator('#reliability-chart .rl-outcome-row')).to_have_count(1)
        chart = page.locator('#reliability-data').evaluate('e => JSON.parse(e.textContent)')
        assert chart[0]['counts'] == dict(success=1, error=1, timeout=1, empty=1)
        page.locator('#reliability-issues').uncheck()
        page.locator('#reliability-role').fill('missing')
        page.get_by_role('button', name='Apply report filters').click()
        page.wait_for_url('**/reliability?**role=missing**')
        playwright.expect(page.locator('#reliability-table')).to_contain_text('No reliability outcomes')
        page.goto('http://console.test/reliability')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
        playwright.expect(page.locator('#reliability-chart .rl-outcome-row')).to_have_count(2)
        output = Path('data/reliability-previews'); output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output/'reliability-mobile.png'), full_page=True)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.screenshot(path=str(output/'reliability-desktop.png'), full_page=True)
        page.locator('#reliability-table tbody tr').filter(has=page.get_by_text('chat', exact=True)).locator('[data-reliability-detail] > summary').click()
        page.locator('#reliability-dialog button[value="reset"]').click()
        page.locator('#reliability-dialog button[value="reset"]').click()
        playwright.expect(page.locator('.said-box')).to_have_text('Reliability reset.')
        assert page.url.split('?', 1)[0] == 'http://console.test/reliability'
        assert not errors
        browser.close()


def report_rows(response):
    return json.loads(re.search(r'<script id="reliability-data" type="application/json">(.*?)</script>', response.text, re.S)[1])


def test_briefing_history_filters_export_and_lifetime_quality(desk, tmp_path):
    browser, _, users = desk
    kept, key, numbers = seed(tmp_path, users)
    kept.record(999, 'jordan', 'small', (key[0], 'older-fingerprint', 'chat'), 0, 'error', owner_id=users.identity('jordan'))
    sign_in(browser, 'jordan')
    browser.post(f'/requests/{numbers["pat"]}/feedback', data={'value': '1', 'explanation': 'Useful answer'})
    assert len(report_rows(browser.get('/reliability'))) == 1
    history = report_rows(browser.get('/reliability?fingerprints=all&sort=rate'))
    assert len(history) == 2 and history[0]['fingerprint'] == 'older-fingerprint'
    assert not history[0]['may_manage']
    query = '/reliability?fingerprints=all&q=older-fingerprint&export=csv'
    exported = browser.get(query)
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert len(rows) == 1 and rows[0]['fingerprint'] == 'older-fingerprint'
    assert rows[0]['error'] == '1'
    assert exported.headers['cache-control'] == 'no-store'
    assert 'attachment' in exported.headers['content-disposition']
    filtered = browser.get('/reliability?role=missing')
    assert report_rows(filtered) == []
    assert 'Useful answer' in filtered.text  # lifetime quality spans all roles
    reset = browser.post('/reliability', data=dict(model='small', role='chat', fingerprint=key[1], action='reset',
                         filter_q='small', filter_fingerprints='all', filter_sort='faults'))
    assert dict(reset.url.params) == dict(q='small', fingerprints='all', sort='faults', said='Reliability reset.')
    assert 'Useful answer' in reset.text


def test_compute_outage_keeps_saved_evidence_and_hides_management(desk, tmp_path):
    browser, switch, users = desk
    seed(tmp_path, users)
    sign_in(browser, 'jordan')
    browser.get('/reliability')
    switch.off = True
    browser.portal.call(switch.front.state.machines.poll)
    report = browser.get('/reliability')
    assert report.status_code == 200 and len(report_rows(report)) == 1
    assert 'All saved fingerprints' in report.text and 'saved fingerprint' in report.text
    assert 'Reset reliability' not in report.text and 'Run recovery' not in report.text
    exported = list(csv.DictReader(io.StringIO(browser.get('/reliability?export=csv').text)))
    assert exported[0]['current'] == 'unknown' and exported[0]['state'] == 'unavailable'


def test_native_evidence_disclosure_without_javascript(desk, tmp_path):
    from front_helpers import serve_browser_request
    playwright = pytest.importorskip('playwright.sync_api')
    server, _, users = desk
    seed(tmp_path, users)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(java_script_enabled=False, viewport={'width': 390, 'height': 844})
        context.route('http://console.test/**', lambda route: serve_browser_request(server, 'http://console.test', route))
        page = context.new_page()
        page.goto('http://console.test/reliability')
        playwright.expect(page.get_by_role('heading', level=1)).to_have_text('Reliability')
        playwright.expect(page.locator('#reliability-chart .rl-outcome-row')).to_have_count(1)
        page.locator('[data-reliability-detail] > summary').click()
        playwright.expect(page.get_by_role('button', name='Reset reliability', exact=True)).to_be_visible()
        playwright.expect(page.get_by_role('link', name='Latest request', exact=False)).to_be_visible()
        page.locator('#reliability-search').fill('no matching candidate')
        page.get_by_role('button', name='Apply report filters').click()
        playwright.expect(page.locator('#reliability-table')).to_contain_text('No reliability outcomes')
        page.goto('http://console.test/reliability?model=small')
        playwright.expect(page.locator('#reliability-issue-table tbody tr')).to_have_count(3)
        page.locator('#reliability-issue-outcome').select_option('empty')
        page.get_by_role('button', name='Apply issue filters').click()
        playwright.expect(page.locator('#reliability-issue-table tbody tr')).to_have_count(1)
        playwright.expect(page.locator('#reliability-issue-table')).to_contain_text('Empty response')
        browser.close()


def test_issue_request_ids_follow_log_permissions_and_reset(desk, tmp_path):
    browser, _, users = desk
    kept, key, _ = seed(tmp_path, users)
    log = Log(str(tmp_path / 'loadcoach.db'))
    number = log.add('request', 'asked', 'pat', model='small')
    log.add('request', 'failed', 'pat', of=number, model='small', reliability_outcome='error')
    kept.record(number, 'pat', 'small', key, 0, 'error', owner_id=users.identity('pat'))
    sign_in(browser, 'pat')
    assert 'issue_requests' not in report_rows(browser.get('/reliability'))[0]
    own = report_rows(browser.get('/reliability?model=small'))[0]['issue_requests']
    assert [row['request'] for row in own] == [number]
    assert own[0]['outcome'] == 'error'
    sign_in(browser, 'jordan')
    all_issues = report_rows(browser.get('/reliability?model=small'))[0]['issue_requests']
    assert len(all_issues) == 4
    assert {row['outcome'] for row in all_issues} == {'error', 'timeout', 'empty'}
    selection = report_rows(browser.get('/reliability?model=small'))[0]['issue_urls']['empty']
    selected = {key: value[0] for key, value in parse_qs(urlsplit(selection).query).items()}
    reset = browser.post('/reliability', data=dict(model='small', role='chat', fingerprint=key[1], action='reset',
                         filter_model='small', filter_issue_outcome='empty', filter_issue_candidate=selected['issue_candidate']))
    assert reset.url.params['issue_outcome'] == 'empty' and reset.url.params['issue_candidate'] == selected['issue_candidate']
    assert report_rows(browser.get('/reliability?model=small'))[0]['issue_requests'] == []


def test_issue_count_links_filter_requests_and_open_the_record(desk, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    server, _, users = desk
    _, _, numbers = seed(tmp_path, users)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/reliability')
        page.get_by_role('link', name='View error requests for small, chat', exact=True).click()
        playwright.expect(page.locator('#reliability-issue-table tbody tr')).to_have_count(1)
        playwright.expect(page.locator('#reliability-issue-outcome')).to_have_value('error')
        page.locator('#reliability-issue-table').get_by_role('link', name=f'#{numbers["jordan"]}', exact=True).click()
        page.wait_for_url(f'**/requests/{numbers["jordan"]}')
        page.goto('http://console.test/reliability?model=small')
        playwright.expect(page.locator('#reliability-issue-table tbody tr')).to_have_count(3)
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            for width in (1440, 768, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
                if theme == 'light' and width in (1440, 390):
                    output = Path('data/reliability-previews'); output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output / f'issues-{width}.png'), full_page=True)
        assert not errors
        browser.close()
