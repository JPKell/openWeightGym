"""Live Log explorer: real journal routing, intact output, and browser interactions."""

import json
import shlex
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_weightroom import desk_with_machine, sign_in  # noqa: F401
from weightroom.system_log_views import workspace

expect = playwright.expect


def test_journal_presentation_preserves_unknown_text_tracebacks_and_stable_identity():
    lines = [
        '2026-11-13T14:00:00-0700 atlas loadcoach[42]: INFO Ready',
        '2026-11-13T14:00:01-07:00 atlas loadcoach[42]: ERROR Worker lost',
        'Traceback (most recent call last):', '  await worker.receive()', 'ConnectionError: closed',
        '<script>window.injected=true</script>', 'plain output', 'plain output',
        '2026-11-13T99:00:00 atlas loadcoach[42]: WARNING Bad timestamp',
    ]
    view = workspace(lines, 'configured-node', 'loadcoach', 'America/Vancouver')
    assert '\n'.join(entry['raw'] for entry in view['entries']) == '\n'.join(lines)
    assert view['count'] == 6 and view['errors'] == 1 and view['warnings'] == 1
    assert view['entries'][0]['message'] == 'INFO Ready'
    assert view['entries'][1]['raw'].endswith('ConnectionError: closed')
    assert view['entries'][-1]['epoch'] is None
    assert view['entries'][-2]['id'] != view['entries'][-3]['id']
    assert all(entry['node'] == 'configured-node' for entry in view['entries'])
    again = workspace(['earlier output', *lines], 'configured-node', 'loadcoach', 'America/Vancouver')
    assert view['entries'][1]['id'] == again['entries'][2]['id']
    assert workspace(['1970-01-01T00:00:00Z kernel: INFO zero'], 'front', 'kernel', 'UTC')['entries'][0]['epoch'] == 0
    assert workspace([], 'front', 'kernel', 'UTC')['duration'] == '—'
    naive = workspace(['2026-01-01T00:00:00 INFO Ready'], 'front', 'kernel', 'Asia/Tokyo')
    assert naive['entries'][0]['at'].endswith('+09:00')


@pytest.fixture
def journal(desk_with_machine, tmp_path):
    server, compute, machine, users, _ = desk_with_machine
    start = datetime.fromisoformat('2026-11-13T14:00:00+00:00')
    lines = []
    for index in range(67):
        level = 'ERROR' if index % 10 == 0 else 'WARNING' if index % 7 == 0 else 'INFO'
        text = f'request {8040 + index} · {"worker disconnected" if level == "ERROR" else "worker ready"}'
        lines.append(f'{(start + timedelta(seconds=index * 5)).isoformat()} atlas loadcoach[42]: {level} {text}')
    lines += ['Traceback (most recent call last):', '  await worker.receive()', 'ConnectionError: lost',
              '<img src=x onerror="window.injected=true">', 'unclassified output']
    (tmp_path / 'bin/journalctl').write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.quote('\n'.join(lines)) + '\n')
    model = tmp_path / 'sample.gguf'
    model.touch()
    Models(str(tmp_path / 'loadcoach.db')).put('sample', {'flags': {'model': str(model)}})
    sign_in(server, 'jordan')
    return server, compute, machine, users, lines


def test_live_explorer_keeps_filters_raw_fallback_and_safe_json(journal):
    server, _, _, _, lines = journal
    response = server.get('/output?source=loadcoach&lines=1000')
    assert response.status_code == 200
    assert 'LOGS / DIAGNOSTICS' not in response.text and 'Sample snapshot' not in response.text
    assert 'data-filter="printed"' in response.text and 'data-auto-submit' in response.text
    assert 'hx-target="#page-content"' in response.text and 'hx-post="/ask"' in response.text
    assert '&lt;img src=x onerror=' in response.text
    content = response.text.split('<script type="application/json" id="journal-data">')[1].split('</script>')[0]
    entries = json.loads(content)['entries']
    assert '\n'.join(row['raw'] for row in entries) == '\n'.join(lines)
    assert entries[-3]['raw'].endswith('ConnectionError: lost')


def test_search_inspector_export_pagination_keyboard_and_themes(journal, tmp_path):
    server, _, _, _, _ = journal
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/output?source=loadcoach')
        expect(page.locator('#printed [data-entry]')).to_have_count(20)
        page.screenshot(path=str(tmp_path / 'system-log-explorer.png'), full_page=True)
        assert page.locator('#printed [data-entry]').first.bounding_box()['y'] < 600
        assert not page.evaluate('window.injected')
        page.locator('#next').click()
        expect(page.locator('#page-summary')).to_contain_text('21–40')
        page.locator('#printed [data-entry]').first.focus()
        page.keyboard.press('ArrowDown')
        expect(page.locator('#printed [data-entry]').nth(1)).to_have_attribute('aria-pressed', 'true')
        page.locator('#search').fill('ConnectionError')
        expect(page.locator('#printed [data-entry]')).to_have_count(1)
        expect(page.locator('#detail-raw')).to_contain_text('Traceback')
        page.locator('#level').select_option('warning')
        expect(page.locator('#empty-state')).to_be_visible()
        page.locator('#empty-reset').click()
        page.locator('#level').select_option('error')
        with page.expect_download() as download:
            page.locator('#export').click()
        path = tmp_path / 'errors.txt'
        download.value.save_as(path)
        text = path.read_text()
        assert 'Traceback' in text and 'ERROR' in text and ' INFO request 8041 ' not in text
        page.locator('#search').fill('.*[<script>')
        expect(page.locator('#empty-state')).to_be_visible()
        page.locator('#clear-search').click()
        page.locator('#level').select_option('unclassified')
        expect(page.locator('#printed [data-entry]')).to_have_count(2)
        assert not page.locator('#printed img').count()
        for theme in ('light', 'dark'):
            page.evaluate('(theme) => {document.documentElement.dataset.bsTheme=theme}', theme)
            for width in (1440, 1024, 768, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
        assert not errors
        browser.close()


def test_refresh_time_windows_chart_and_local_model_keep_real_routes(journal):
    server, _, _, _, _ = journal
    fetched, asked = [], []
    def capture(route, path):
        if path.startswith('/output?'):
            fetched.append(parse_qs(urlsplit(path).query))
        if path == '/ask':
            asked.append(parse_qs(route.request.post_data))
            route.fulfill(status=200, content_type='text/html', body='<p>Local answer</p>')
            return True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/output?source=loadcoach')
        page.locator('#search').fill('worker disconnected')
        page.locator('#ask-entry').click()
        raw = page.locator('[data-log-selection]').input_value()
        page.locator('#words').fill('Why did it stop?')
        page.locator('#ask-model').select_option('sample')
        page.locator('#refresh').click()
        expect(page.locator('#system-log-explorer')).to_have_attribute('data-ready', 'true')
        expect(page.locator('#search')).to_have_value('worker disconnected')
        expect(page.locator('#words')).to_have_value('Why did it stop?')
        expect(page.locator('[data-log-selection]')).to_have_value(raw)
        page.get_by_role('button', name='Ask', exact=True).click()
        expect(page.locator('#answer')).to_contain_text('Local answer')
        assert asked[-1]['selection'] == [raw] and asked[-1]['model'] == ['sample']
        assert 'unclassified output' in asked[-1]['log'][0]
        expect(page.locator('#detail-content')).to_be_visible()
        page.locator('#around-entry').click()
        page.wait_for_function('new URL(location.href).searchParams.get("after") === "60"')
        assert fetched[-1]['before'] == ['60'] and fetched[-1]['after'] == ['60']
        assert 'at' in fetched[-1] and fetched[-1]['source'] == ['loadcoach']
        expect(page.locator('#search')).to_have_value('worker disconnected')
        page.locator('#clear-window').click()
        page.wait_for_function('!new URL(location.href).searchParams.get("at")')
        page.locator('#volume-chart [data-bucket]:not([disabled])').first.click()
        page.wait_for_function('new URL(location.href).searchParams.get("before") === "0"')
        assert fetched[-1]['before'] == ['0'] and 'at' in fetched[-1]
        assert not errors
        browser.close()


def test_offline_snapshot_keeps_export_and_unclassified_output(journal):
    server, compute, machine, _, _ = journal
    url = '/output?source=loadcoach&lines=500'
    server.get(url)
    compute.off = machine.off = True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test' + url)
        expect(page.locator('#journal-offline')).to_contain_text('Showing logs last fetched')
        expect(page.locator('#ask-entry')).to_be_hidden()
        expect(page.locator('#around-entry')).to_be_disabled()
        expect(page.locator('#export')).to_be_enabled()
        page.locator('#level').select_option('unclassified')
        expect(page.locator('#printed [data-entry]')).to_have_count(2)
        page.locator('#source').select_option('kernel')
        expect(page.locator('#empty-state')).to_be_visible()
        expect(page.locator('#journal-offline')).to_contain_text('No logs have been fetched for these filters yet')
        expect(page.locator('#export')).to_be_disabled()
        assert not page.locator('#asking').count()
        browser.close()
