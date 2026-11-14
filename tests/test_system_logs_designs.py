"""System logs proposals preserve diagnostic controls and work offline in every layout."""

import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-system-logs-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, timezone_id='Asia/Tokyo')
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url))
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not any(url.startswith(('http:', 'https:')) for url in requests)
        browser.close()


def test_all_layouts_fit_in_both_themes_and_empty_offline_failure_states(preview):
    page = preview
    for appearance in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != appearance:
            page.locator('#theme').click()
        for width in (1440, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for design in ('explorer', 'investigation', 'console'):
                page.locator(f'[data-design="{design}"]').click()
                expect(page.locator(f'[data-design="{design}"]')).to_have_attribute('aria-pressed', 'true')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (appearance, width, design)
                expect(page.locator('#detail-content')).to_be_visible()
                for state in ('empty', 'failure', 'offline', 'normal'):
                    page.locator('#scenario').select_option(state)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (appearance, width, design, state)
                    if state in ('empty', 'failure'):
                        expect(page.locator('#detail-content')).to_be_hidden()
    # Field labels and IDs remain unique, including the reused analysis form.
    ids = page.locator('[id]').evaluate_all('(elements) => elements.map(e => e.id)')
    assert len(ids) == len(set(ids))
    for name in ('node', 'source', 'at', 'before', 'after', 'lines'):
        expect(page.locator(f'#journal-filters [name="{name}"]')).to_have_count(1)


def test_search_includes_original_tracebacks_and_preserves_drafts_across_designs(preview):
    page = preview
    page.locator('#search').fill('ConnectionError 8041')
    expect(page.locator('#printed [data-entry]')).not_to_have_count(0)
    expect(page.locator('#detail-raw')).to_contain_text('Traceback')
    for design in ('investigation', 'console', 'explorer'):
        page.locator(f'[data-design="{design}"]').click()
        expect(page.locator('#search')).to_have_value('ConnectionError 8041')
        expect(page.locator('#detail-message')).to_contain_text('connection closed')
    page.locator('#level').select_option('warning')
    expect(page.locator('#empty-state')).to_be_visible()
    page.locator('#empty-reset').click()
    expect(page.locator('#visible-count')).to_have_text('100 matching / 100 fetched')
    page.locator('#search').fill('8041')
    expect(page.locator('#printed mark').first).to_have_text('8041')
    page.locator('#search').fill('.*[<script>')
    expect(page.locator('#empty-state')).to_be_visible()
    page.locator('#clear-search').click()
    page.locator('#level').select_option('unclassified')
    expect(page.locator('#detail-raw')).to_contain_text('llama_model_loader:')


def test_pagination_order_keyboard_selection_and_filtered_export(preview, tmp_path):
    page = preview
    page.locator('#next').click()
    expect(page.locator('#page-summary')).to_have_text('21–40 of 100 entries')
    page.locator('#previous').click()
    first = page.locator('#printed [data-entry]').first
    first.focus()
    first.press('ArrowDown')
    expect(page.locator('#printed [data-entry]').nth(1)).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('#detail-id')).to_have_text('#atlas-1')
    page.locator('#level').select_option('error')
    page.locator('#sort').select_option('oldest')
    expect(page.locator('#printed [data-entry]')).to_have_count(13)
    with page.expect_download() as download:
        page.locator('#export').click()
    path = tmp_path / 'filtered.txt'
    download.value.save_as(path)
    content = path.read_text()
    assert 'ERROR' in content and 'Traceback' in content and 'INFO' not in content
    assert content.count('ERROR') == 13
    rows = page.locator('#printed [data-entry] time').evaluate_all('(els) => els.map(e => e.dateTime)')
    assert rows == sorted(rows)
    assert content.index(rows[0]) < content.index(rows[-1])


def test_time_windows_use_display_zone_and_chart_context_are_functional(preview):
    page = preview
    # The browser is in Tokyo; its zone must not affect the Vancouver wall-time filter.
    page.locator('#at').fill('2026-11-13T14:32:18')
    page.locator('#journal-filters').get_by_role('button', name='Apply time window').click()
    expect(page.locator('#total-count')).to_have_text('3')
    expect(page.locator('#printed [data-entry] time').first).to_have_text('14:32:18')
    expect(page.locator('#detail-at')).to_contain_text('2026-11-13 14:32:18')
    page.locator('#clear-window').click()
    page.locator('#volume-chart [data-bucket]:not([disabled])').nth(10).click()
    assert 0 < int(page.locator('#total-count').inner_text()) < 100
    expect(page.locator('#at')).not_to_have_value('')
    page.locator('#clear-window').click()
    page.locator('#search').fill('8041')
    page.locator('#around-entry').click()
    expect(page.locator('#before')).to_have_value('60')
    expect(page.locator('#after')).to_have_value('60')
    expect(page.locator('#search')).to_have_value('')
    assert int(page.locator('#total-count').inner_text()) < 10
    page.locator('#entry-context [data-context]').first.click()
    expect(page.locator('#entry-context [aria-current="true"]')).to_have_count(1)
    page.locator('#before').fill('-1')
    assert not page.locator('#before').evaluate('(el) => el.checkValidity()')


def test_offline_snapshots_are_exact_filter_scoped_and_read_failures_retry(preview):
    page = preview
    page.locator('#scenario').select_option('offline')
    expect(page.locator('#offline-notice')).to_be_visible()
    expect(page.locator('#refresh')).to_be_disabled()
    page.locator('#source').select_option('kernel')
    expect(page.locator('#empty-title')).to_have_text('No saved snapshot for these filters')
    expect(page.locator('#detail-content')).to_be_hidden()
    page.locator('#empty-reset').click()
    expect(page.locator('#source')).to_have_value('loadcoach')
    expect(page.locator('#total-count')).to_have_text('100')
    page.locator('#search').fill('8041')
    expect(page.locator('#printed [data-entry]')).not_to_have_count(0)
    page.locator('#output-node').select_option('orion')
    expect(page.locator('#total-count')).to_have_text('0')
    page.locator('#scenario').select_option('failure')
    expect(page.locator('#failure-notice')).to_be_visible()
    page.locator('#retry').click()
    expect(page.locator('#failure-notice')).to_be_hidden()
    expect(page.locator('#printed [data-entry]')).not_to_have_count(0)


def test_local_analysis_refresh_and_untrusted_log_text(preview):
    page = preview
    # Treat raw output as text, including search highlights and multiline stack traces.
    page.locator('#search').fill('8041')
    selected = page.locator('#detail-id').inner_text()
    page.locator('#ask-entry').click()
    page.locator('#words').fill('Why did this worker disconnect?')
    page.locator('#ask-model').select_option('llama-3.3-70b')
    for design in ('console', 'investigation'):
        page.locator(f'[data-design="{design}"]').click()
        expect(page.locator('#words')).to_have_value('Why did this worker disconnect?')
        expect(page.locator('#ask-model')).to_have_value('llama-3.3-70b')
    page.locator('#analysis-panel').get_by_role('button', name='Ask', exact=True).click()
    expect(page.locator('#answer')).to_contain_text('1 sample entry')
    expect(page.locator('#answer')).to_contain_text('no model was called')
    page.locator('#refresh').click()
    expect(page.locator('#detail-id')).to_have_text(selected)
    expect(page.locator('#search')).to_have_value('8041')
    page.locator('#reset').click()
    expect(page.locator('#words')).to_have_value('')
    expect(page.locator('#search')).to_have_value('')
    page.locator('#source').select_option('kernel')
    expect(page.locator('#journal-command')).to_contain_text('journalctl -k')
    expect(page.locator('#printed [data-entry]')).not_to_have_count(0)


def test_mobile_filters_expand_and_reopen_when_returning_to_desktop(preview):
    page = preview
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.locator('#filters-panel')).not_to_have_attribute('open', '')
    page.locator('#filters-panel > summary').click()
    expect(page.locator('#source')).to_be_visible()
    page.locator('#source').select_option('kernel')
    expect(page.locator('#detail-source')).to_have_text('kernel')
    page.locator('#at').fill('2026-11-13T14:30')
    assert page.locator('#at').bounding_box()['width'] > 300
    page.locator('#filters-panel > summary').click()
    page.set_viewport_size({'width': 1440, 'height': 1000})
    expect(page.locator('#source')).to_be_visible()
    expect(page.locator('#source')).to_have_value('kernel')


def test_embedded_sample_data_is_safe_to_render(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/preview-system-logs-designs.py'))
    data = module['sample_data']()
    assert len(data['logs']) == 504
    assert {row['level'] for row in data['logs']} == {'info', 'warning', 'error', 'debug', 'unclassified'}
    assert all(row['raw'] and row['node'] and row['source'] for row in data['logs'])
    # JSON is inlined through Jinja's tojson, so script-shaped output stays data.
    original = module['render'].__globals__['sample_data']
    data['logs'][0]['message'] = '</script><script>window.injected=true</script>'
    data['logs'][0]['raw'] += data['logs'][0]['message']
    module['render'].__globals__['sample_data'] = lambda: data
    try:
        markup = module['render']()
    finally:
        module['render'].__globals__['sample_data'] = original
    assert '</script><script>window.injected=true</script>' not in markup
    assert '\\u003c/script\\u003e' in markup
    assert json.loads(markup.split('<script type="application/json" id="system-logs-data">')[1].split('</script>')[0])['logs'][0]['message'] == data['logs'][0]['message']
