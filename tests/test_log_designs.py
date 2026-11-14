"""Browser contracts for AI Logs proposals: scoped evidence and local-only interactions."""

import csv
import io
import json
import runpy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/preview-log-designs.py'))
    output = tmp_path / 'index.html'
    output.write_text(module['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page, module['sample_data'](), tmp_path
        assert not errors
        assert not network, 'The proposals must never contact live services.'
        browser.close()


def test_all_layouts_themes_states_and_phone_have_usable_controls(preview):
    page, data, _ = preview
    expect(page.locator('#metric-events')).to_have_text(str(len(data['events'])))
    expect(page.locator('#detail-title')).to_have_text('request / failed')
    expect(page.locator('#event-rows time').first).to_have_text('10:41:42')
    for width in (1600, 1024, 768, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        for theme in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for design in ('explorer', 'incidents', 'traces'):
                page.locator(f'[data-design="{design}"]').click()
                expect(page.locator(f'[data-design="{design}"]')).to_have_attribute('aria-pressed', 'true')
                for scenario in ('normal', 'empty', 'failure'):
                    page.locator('#scenario').select_option(scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, design, scenario)
                    if scenario == 'failure':
                        expect(page.locator('#metric-events')).to_have_text('—')
                        expect(page.locator('#export')).to_be_disabled()
                    elif scenario == 'empty':
                        expect(page.locator('#metric-events')).to_have_text('0')
                        expect(page.locator('#inspector-content')).to_be_hidden()
                page.locator('#scenario').select_option('normal')
    page.locator('#more-filters').click()
    assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
        .filter(el => el.getBoundingClientRect().width && !el.closest('[hidden]'))
        .every(el => el.labels?.length || el.getAttribute('aria-label'))''')
    assert page.evaluate('''() => {
        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
        return ids.length === new Set(ids).size;
    }''')


def test_filters_scope_counts_and_can_be_cleared_without_losing_layout(preview):
    page, data, _ = preview
    page.locator('[data-quick="failed"]').click()
    failed = [event for event in data['events'] if event['what'] == 'failed']
    expect(page.locator('#metric-events')).to_have_text(str(len(failed)))
    expect(page.locator('#metric-denied')).to_have_text('0')
    page.locator('#log-node').select_option('orion')
    expect(page.locator('#metric-events')).to_have_text(str(sum(event['machine'] == 'orion' for event in failed)))
    page.locator('#more-filters').click()
    page.locator('#log-group').select_option('research')
    expect(page.locator('#metric-events')).to_have_text('2')
    page.locator('#log-user').select_option('sam')
    page.locator('[data-design="traces"]').click()
    expect(page.locator('#log-node')).to_have_value('orion')
    expect(page.locator('#trace-count')).to_have_text('1 trace · 0 untraced events')
    page.locator('[data-clear]').first.click()
    expect(page.locator('#workspace')).to_have_attribute('data-layout', 'traces')
    expect(page.locator('#metric-events')).to_have_text(str(len(data['events'])))
    assert all(not item for item in page.locator('#log-user option').evaluate_all('(els)=>els.map(el=>el.hidden)'))
    page.locator('#search').fill('there is no such event')
    expect(page.locator('#metric-events')).to_have_text('0')
    expect(page.locator('#inspector-content')).to_be_hidden()


def test_exact_trace_context_untraced_events_and_keyboard_inspection(preview):
    page, _, _ = preview
    page.locator('#search').fill('out_of_memory')
    expect(page.locator('#metric-events')).to_have_text('5')
    first = page.locator('#event-rows [data-open]').first
    first.focus()
    page.keyboard.press('Enter')
    expect(page.locator('#detail-summary')).to_contain_text('CUDA out of memory')
    expect(page.locator('#related-events li')).to_have_count(3)
    page.locator('#inspect-trace').click()
    expect(page.locator('#timeline-title')).to_have_text('chat-7310')
    expect(page.locator('#timeline-events li')).to_have_count(3)
    expect(page.locator('#timeline-note')).to_contain_text('1 match your filters')
    expect(page.locator('#timeline-events [data-field="title"]')).to_have_text(['request / asked', 'routing / selected', 'request / failed'])
    page.locator('#timeline-events [data-open]').first.click()
    expect(page.locator('#detail-title')).to_have_text('request / asked')
    expect(page.locator('#timeline-note')).to_contain_text('1 match your filters')
    page.locator('[data-clear]').first.click()
    page.locator('#search').fill('signed-in')
    expect(page.locator('#timeline-title')).to_contain_text('Untraced event')
    expect(page.locator('#timeline-events li')).to_have_count(1)
    expect(page.locator('#related-note')).to_contain_text('No trace ID was recorded')
    expect(page.locator('#inspect-trace')).to_be_hidden()


def test_incident_drilldowns_chart_time_validation_and_retry(preview):
    page, data, _ = preview
    page.locator('[data-design="incidents"]').click()
    expect(page.locator('#issue-list [data-issue]')).to_have_count(3)
    page.locator('#issue-list [data-issue]').filter(has_text='GPU memory exhausted').click()
    expect(page.locator('#metric-events')).to_have_text('5')
    expect(page.locator('#active-filters')).to_contain_text('Error code: out_of_memory')
    page.locator('[data-clear]').first.click()
    page.locator('#node-breakdown button').filter(has_text='orion').click()
    expect(page.locator('#metric-events')).to_have_text('3')
    page.locator('[data-clear]').first.click()
    page.locator('#activity-chart button').last.click()
    expect(page.locator('#time-range')).to_have_value('custom')
    expect(page.locator('#log-start')).to_have_value('2026-11-13T10:37')
    expected = [event for event in data['events'] if '10:37' <= event['at'][11:16] <= '10:41']
    expect(page.locator('#metric-events')).to_have_text(str(len(expected)))
    page.locator('#log-start').fill('2026-11-13T11:00')
    expect(page.locator('#filter-error')).to_be_visible()
    expect(page.locator('#export')).to_be_disabled()
    page.locator('[data-clear]').first.click()
    page.locator('#log-node').select_option('atlas')
    page.locator('#scenario').select_option('failure')
    page.locator('#retry').click()
    expect(page.locator('#log-node')).to_have_value('atlas')
    expect(page.locator('#metric-events')).to_have_text(str(sum(event['machine'] == 'atlas' for event in data['events'])))


def test_pagination_numeric_sort_filtered_export_and_reloadable_view(preview):
    page, data, tmp_path = preview
    page.locator('#next').click()
    expect(page.locator('#page-summary')).to_contain_text('13–24')
    page.locator('[data-sort="at"]').click()
    dates = page.locator('#event-rows time').evaluate_all('(els)=>els.map(el=>Date.parse(el.dateTime))')
    assert dates == sorted(dates)
    page.locator('#log-model').select_option('qwen3-32b')
    expected = [event for event in data['events'] if event['model'] == 'qwen3-32b']
    with page.expect_download() as downloaded:
        page.locator('#export').click()
    export = tmp_path / 'export.csv'
    downloaded.value.save_as(export)
    rows = list(csv.DictReader(io.StringIO(export.read_text())))
    assert len(rows) == len(expected) > 12
    assert {int(row['id']) for row in rows} == {event['id'] for event in expected}
    assert all(row['model'] == 'qwen3-32b' for row in rows)
    page.locator('#next').click()
    page.locator('#event-rows [data-open]').first.click()
    selected_id = page.locator('#detail-id').inner_text()
    page.locator('[data-design="traces"]').click()
    url = page.url
    params = parse_qs(urlsplit(url).fragment)
    assert params['model'] == ['qwen3-32b']
    page.reload()
    expect(page.locator('#workspace')).to_have_attribute('data-layout', 'traces')
    expect(page.locator('#detail-id')).to_have_text(selected_id)
    expect(page.locator('#page-summary')).to_contain_text('13–')
    expect(page.locator('#log-model')).to_have_value('qwen3-32b')
    with page.expect_download() as downloaded:
        page.locator('#download-event').click()
    downloaded.value.save_as(tmp_path / 'event.json')
    event = json.loads((tmp_path / 'event.json').read_text())
    assert event['id'] == int(selected_id[1:])


def test_untrusted_event_fields_remain_text_and_csv_formulas_are_neutralized(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/preview-log-designs.py'))
    data = module['sample_data']()
    data['events'][0].update(model='=1+1', error='<img src=x onerror="window.injected=true">')
    module['render'].__globals__['sample_data'] = lambda: data
    output = tmp_path / 'hostile.html'
    output.write_text(module['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(output.as_uri())
        page.locator('#log-model').select_option('=1+1')
        expect(page.locator('#detail-summary')).to_contain_text('<img src=x')
        assert page.evaluate('window.injected') is None
        assert page.locator('#inspector img').count() == 0
        with page.expect_download() as downloaded:
            page.locator('#export').click()
        downloaded.value.save_as(tmp_path / 'safe.csv')
        row = next(csv.DictReader(io.StringIO((tmp_path / 'safe.csv').read_text())))
        assert row['model'] == "'=1+1"
        browser.close()
