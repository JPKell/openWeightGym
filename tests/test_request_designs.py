"""Paired request proposals preserve evidence semantics without contacting live services."""

import csv
import io
import json
import math
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ('signal', 'ledger', 'explorer')


def test_proposals_use_repository_components(monkeypatch, tmp_path):
    from test_ui_consistency import test_templates_use_shared_components as audit

    for path in (ROOT / 'weightroom/pages').glob('request*.html'):
        (tmp_path / path.name).symlink_to(path)
    monkeypatch.setitem(audit.__globals__, 'TEMPLATE_ROOTS', (tmp_path,))
    audit()


@pytest.fixture
def preview(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/preview-request-designs.py'))
    output = tmp_path / 'index.html'
    output.write_text(module['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page, module['samples']()
        assert not errors
        assert not network, 'Sample previews must not contact the application or external services.'
        browser.close()


def design(page, name):
    page.locator(f'[data-design="{name}"]').click()
    expect(page.locator('#workspace')).to_have_attribute('data-layout', name)


def open_request(page, record):
    page.locator('button[data-view="directory"]').click()
    page.locator('#query').fill(str(record['id']))
    page.locator('#request-rows [data-open]').click()
    if page.locator('#workspace').get_attribute('data-layout') == 'explorer':
        expect(page.locator('#peek-id')).to_contain_text(str(record['id']))
        page.locator('#open-peek').click()
    expect(page.locator('#detail-view')).to_be_visible()


@pytest.mark.parametrize('name', DESIGNS)
def test_paired_layouts_fit_appearances_widths_and_states(preview, name):
    page, _ = preview
    design(page, name)
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1100})
            for view in ('directory', 'detail'):
                page.locator(f'button[data-view="{view}"]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (name, theme, width, view)
                assert page.locator('h1:visible').count() == 1
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                    return ids.length === new Set(ids).size;
                }''')
                assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                    .filter(e => e.getBoundingClientRect().width)
                    .every(e => e.labels?.length || e.getAttribute('aria-label'))''')
        for state in ('empty', 'stale', 'restricted', 'legacy', 'running'):
            page.locator('#scenario').select_option(state)
            for view in ('directory', 'detail'):
                page.locator(f'button[data-view="{view}"]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (name, state, view)


@pytest.mark.parametrize('name', DESIGNS)
def test_filter_sort_page_and_navigation_preserve_context(preview, name):
    page, records = preview
    design(page, name)
    expect(page.locator('#request-rows tr')).to_have_count(12)
    page.locator('#next').click()
    expect(page.locator('#page-summary')).to_have_text('Showing 13–24 of 72')
    page.locator('#page-size').select_option('72')
    page.locator('[data-sort="total"]').click()
    totals = page.locator('#request-rows [data-field="total"]').all_text_contents()
    measured = [float(value) for value in totals if value != '—' and 'elapsed' not in value]
    assert measured == sorted(measured)
    assert totals[-1] == '—'
    page.locator('#query').fill('does-not-exist')
    expect(page.locator('#empty-rows')).to_be_visible()
    expect(page.locator('#export-directory')).to_be_disabled()
    page.locator('#empty-rows [data-clear]').click()
    page.locator('#requests-preview-node').select_option('atlas')
    page.locator('#model').select_option('qwen3-32b')
    expected = [r for r in records if r['node'] == 'atlas' and r['model'] == 'qwen3-32b']
    expect(page.locator('#request-rows tr')).to_have_count(len(expected))
    page.locator('#request-rows [data-open]').first.focus()
    page.keyboard.press('Enter')
    if name == 'explorer':
        page.locator('#open-peek').click()
    expect(page.locator('#detail-heading h1')).to_be_focused()
    selected_id = page.locator('#detail-heading h1').inner_text()
    page.locator('.rq-outline a[href="#trace-panel"]').click()
    expect(page.locator('#detail-view')).to_be_visible()
    page.reload()
    expect(page.locator('#detail-heading h1')).to_have_text(selected_id)
    expect(page.locator('#workspace')).to_have_attribute('data-layout', name)
    page.locator('#back').click()
    expect(page.locator('#requests-preview-node')).to_have_value('atlas')
    expect(page.locator('#model')).to_have_value('qwen3-32b')
    expect(page.locator('#request-rows [data-open]:focus')).to_have_count(1)


def test_advanced_trace_scope_and_recovery(preview):
    page, records = preview
    page.locator('#advanced-filters summary').click()
    page.locator('#requests-preview-group').select_option('research')
    page.locator('#requests-preview-user').select_option('research-bot')
    page.locator('#caller').select_option('Batch API')
    page.locator('#service').select_option('LoadCoach')
    expected = [r for r in records if r['group'] == 'research' and r['service'] == 'LoadCoach']
    expect(page.locator('#result-count')).to_have_text(f'{len(expected)} matching requests')
    page.locator('#filters [data-clear]').click()
    open_request(page, records[0])
    page.locator('#find-trace').click()
    expect(page.locator('#trace_id')).to_have_value(records[0]['trace_id'])
    expect(page.locator('#request-rows tr')).to_have_count(3)
    page.locator('#request-rows [data-open]').first.click()
    page.locator('#find-ingress').click()
    expect(page.locator('#request-rows tr')).to_have_count(1)
    page.locator('#scenario').select_option('empty')
    expect(page.locator('#empty-rows')).to_be_visible()
    page.locator('#empty-rows [data-clear]').click()
    expect(page.locator('#count')).to_have_text('72')
    page.evaluate("location.hash='design=explorer&view=detail&id=missing'")
    expect(page.locator('#missing-alert')).to_be_visible()
    expect(page.locator('#directory-view')).to_be_visible()


def test_metrics_distinguish_endings_missing_timings_and_active_samples(preview):
    page, records = preview
    endings = [r for r in records if not r['active'] and r['state'] != 'No ending']
    measured = sorted(r['total'] for r in endings if r['total'] is not None)
    errors = sum(r['state'] in ('Failed', 'Timeout') for r in endings)
    expect(page.locator('#error-note')).to_have_text(f'{errors} server errors / {len(endings)} endings')
    expect(page.locator('#p95')).to_have_text(f'{measured[math.ceil(len(measured)*.95)-1]:.3f} s')
    expect(page.locator('#p95-note')).to_have_text(f'{len(measured)} measured / {len(endings)} endings')
    page.locator('#status').select_option('Running')
    expect(page.locator('#p95')).to_have_text('—')
    expect(page.locator('#error-rate')).to_have_text('—')
    page.locator('#filters [data-clear]').click()
    open_request(page, records[0])
    durations = page.locator('#phase-rows [data-phase-value]').all_text_contents()
    assert sum(float(value.split()[0]) for value in durations) == pytest.approx(records[0]['total'])
    page.locator('#span-rows [data-span-open]').first.click()
    expect(page.locator('#span-detail')).to_contain_text('Turnstile / Control node')
    page.locator('#scenario').select_option('legacy')
    expect(page.locator('#detail-total')).to_have_text('—')
    expect(page.locator('#phase-empty')).to_be_visible()
    expect(page.locator('#verdict-note')).to_contain_text('no latency is inferred')


def test_content_permissions_exports_and_sample_controls(preview):
    page, records = preview
    open_request(page, records[5])
    page.locator('[data-evidence="content"]').click()
    expect(page.locator('#answer')).to_contain_text(records[5]['answer'])
    page.locator('[data-rating="1"]').click()
    expect(page.locator('[data-rating="1"]')).to_have_attribute('aria-pressed', 'true')
    page.locator('#scenario').select_option('restricted')
    expect(page.locator('#retention-note')).to_contain_text('requires permission')
    expect(page.locator('#kept-content')).to_be_hidden()
    assert page.locator('#prompt').text_content() == ''
    assert page.locator('#answer').text_content() == ''
    with page.expect_download() as downloaded:
        page.locator('#export-record').click()
    exported = json.loads(Path(downloaded.value.path()).read_text())
    assert exported['id'] == records[5]['id']
    assert not {'prompt', 'answer'} & exported.keys()
    page.locator('#scenario').select_option('normal')
    page.locator('#back').click()
    page.locator('#filters [data-clear]').click()
    page.locator('[data-quick="errors"].nav-link').click()
    with page.expect_download() as downloaded:
        page.locator('#export-directory').click()
    rows = list(csv.DictReader(io.StringIO(Path(downloaded.value.path()).read_text())))
    assert len(rows) == sum(r['state'] in ('Failed', 'Timeout') for r in records)
    assert {r['state'] for r in rows} == {'Failed', 'Timeout'}
    page.locator('#scenario').select_option('running')
    page.locator('button[data-view="detail"]').click()
    expect(page.locator('#detail-total-label')).to_have_text('Elapsed on compute')
    page.locator('#advance').click()
    expect(page.locator('#stream-content')).to_contain_text('Confirmed')
    page.locator('#scenario').select_option('stale')
    expect(page.locator('#source-alert')).to_be_visible()
    expect(page.locator('#stop-panel')).to_be_hidden()
    page.locator('#scenario').select_option('running')
    page.locator('#stop').click()
    expect(page.locator('#detail-state')).to_have_text('Running')
    page.locator('#stop').click()
    expect(page.locator('#detail-state')).to_have_text('Cancelled')
    expect(page.locator('#stop-panel')).to_be_hidden()


def test_copy_metadata_and_browser_history(preview):
    page, records = preview
    page.evaluate('''() => {
        window.copied = [];
        Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => window.copied.push(value)}, configurable: true});
    }''')
    open_request(page, records[0])
    page.locator('#copy-link').click()
    assert f'id={records[0]["id"]}' in page.evaluate('window.copied[0]')
    page.locator('[data-evidence="raw"]').click()
    page.locator('#copy-metadata').click()
    assert json.loads(page.evaluate('window.copied[1]'))['id'] == records[0]['id']
    page.go_back()
    expect(page.locator('#directory-view')).to_be_visible()
    page.go_forward()
    expect(page.locator('#detail-view')).to_be_visible()
