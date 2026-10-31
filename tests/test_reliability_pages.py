"""Authorized reliability tables, chart rendering and feedback in the existing console."""

import hashlib
from pathlib import Path
import sqlite3

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
        playwright.expect(page.locator('#reliability-chart svg')).to_be_visible()
        assert page.locator('#reliability-table tbody tr').count() == 2
        page.locator('#reliability-issues').check()
        page.get_by_role('button', name='Apply report filters').click()
        playwright.expect(page).to_have_url('http://console.test/reliability?model=&role=&issues=1')
        assert page.locator('#reliability-table tbody tr').count() == 1
        playwright.expect(page.locator('#reliability-chart svg')).to_be_visible()
        chart = page.evaluate("echarts.getInstanceByDom(document.getElementById('reliability-chart')).getOption()")
        assert [series['data'] for series in chart['series']] == [[1], [1], [1], [1]]
        page.locator('#reliability-issues').uncheck()
        page.locator('#reliability-role').fill('missing')
        page.get_by_role('button', name='Apply report filters').click()
        playwright.expect(page).to_have_url('http://console.test/reliability?model=&role=missing')
        playwright.expect(page.locator('#reliability-table')).to_contain_text('No reliability outcomes')
        page.goto('http://console.test/reliability')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
        playwright.expect(page.locator('#reliability-chart svg')).to_be_visible()
        output = Path('data/reliability-previews'); output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output/'reliability-mobile.png'), full_page=True)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.screenshot(path=str(output/'reliability-desktop.png'), full_page=True)
        page.locator('#reliability-table tbody tr').filter(has=page.get_by_text('chat', exact=True)).get_by_role('button', name='Reset reliability').click()
        playwright.expect(page.locator('.said-box')).to_have_text('Reliability reset.')
        assert page.url.split('?', 1)[0] == 'http://console.test/reliability'
        assert not errors
        browser.close()
