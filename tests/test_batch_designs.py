"""Exercise the offline Batches proposals, including ownership and partial results."""

import csv
import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-batch-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Sample proposals must never contact live services.'
        browser.close()


def test_layouts_themes_states_and_dialogs_fit_and_have_accessible_fields(preview):
    page = preview
    for width in (1600, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        for theme in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for layout in ('ledger', 'board', 'inspector'):
                page.locator(f'[data-design="{layout}"]').click()
                expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
                for scenario in ('normal', 'empty', 'attention'):
                    page.locator('#scenario').select_option(scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, layout, scenario)
                    expect(page.locator('#empty')).to_be_visible() if scenario == 'empty' else expect(page.locator('#empty')).to_be_hidden()
                page.locator('#scenario').select_option('normal')
                page.locator('#workspace [data-open]').first.focus()
                page.keyboard.press('Enter')
                expect(page.locator('#batch-inspector')).to_be_visible()
                assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                    .filter(el => el.getBoundingClientRect().width && !el.closest('[hidden]'))
                    .every(el => el.labels?.length || el.getAttribute('aria-label'))''')
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                    return ids.length === new Set(ids).size;
                }''')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if layout != 'inspector':
                    page.keyboard.press('Escape')
                page.locator('#workspace [data-guide]').first.click()
                expect(page.locator('#guide-dialog')).to_be_visible()
                assert page.locator('#guide-dialog').evaluate('(el) => el.getBoundingClientRect().right <= innerWidth')
                page.keyboard.press('Escape')
    before = page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor')
    page.locator('#theme').click()
    assert page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor') != before


def test_search_filters_sort_pagination_and_export_are_consistent(preview, tmp_path):
    page = preview
    page.locator('#status-filter').select_option('errors')
    page.locator('#sort').select_option('errors')
    expect(page.locator('#visible-count')).to_have_text('6 batches')
    expect(page.locator('#ledger-rows [data-name]').first).to_have_text('Invoice field extraction')
    page.locator('#owner-filter').select_option('jordan')
    expect(page.locator('#visible-count')).to_have_text('3 batches')
    page.locator('[data-design="board"]').click()
    expect(page.locator('[data-lane="active"] .bt-entry')).to_have_count(1)
    expect(page.locator('[data-lane="completed"] .bt-entry')).to_have_count(1)
    expect(page.locator('[data-lane="cancelled"] .bt-entry')).to_have_count(1)
    page.locator('[data-design="inspector"]').click()
    expect(page.locator('#batch-list .bt-entry')).to_have_count(3)
    page.locator('#search').fill('no matching batch')
    expect(page.locator('#empty-title')).to_have_text('No matching batches')
    page.locator('#empty-clear').click()
    page.locator('[data-design="ledger"]').click()
    page.locator('#owner-filter').select_option('jordan')
    page.locator('#page-size').select_option('6')
    expect(page.locator('#page-summary')).to_have_text('1–6 of 8')
    page.locator('#next').click()
    expect(page.locator('#ledger-rows tr')).to_have_count(2)
    expect(page.locator('#page-summary')).to_have_text('7–8 of 8')
    with page.expect_download() as downloaded:
        page.locator('#export').click()
    destination = tmp_path / 'filtered.csv'
    downloaded.value.save_as(destination)
    with destination.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 8 and {row['user'] for row in rows} == {'jordan'}
    assert rows[0]['completed'] == '846' and rows[0]['failed'] == '12'
    page.locator('#search').fill('batch_7b4e0004')
    expect(page.locator('#visible-count')).to_have_text('1 batch')
    expect(page.locator('#page-summary')).to_have_text('1–1 of 1')


def test_counts_ownership_cancellation_and_partial_downloads(preview, tmp_path):
    page = preview
    first = page.locator('#ledger-rows tr').first
    expect(first.locator('[data-progress-label]')).to_have_text('858 / 1,200 processed')
    expect(first.locator('[data-progress]')).to_have_attribute('aria-valuetext', '858 of 1200 processed: 846 successful, 12 errors')
    other = page.locator('#ledger-rows tr').filter(has=page.locator('[data-name]', has_text='Research archive'))
    expect(other.locator('[data-download]:visible')).to_have_count(0)
    expect(other.locator('[data-file-note]')).to_have_text('Files visible to owner')
    cancel = first.locator('[data-cancel-form] button')
    cancel.click()
    expect(cancel).to_have_text('Are you sure?')
    expect(first.locator('[data-state]')).to_have_text('Running')
    page.keyboard.press('Escape')
    expect(cancel).to_have_text('Cancel')
    cancel.click()
    cancel.click()
    expect(first.locator('[data-state]')).to_have_text('Cancelling')
    first.locator('[data-open]').click()
    expect(page.locator('#detail-name')).to_have_text('Support ticket classification')
    page.keyboard.press('Escape')
    page.locator('#advance').click()
    expect(first.locator('[data-state]')).to_have_text('Cancelled')
    expect(first.locator('[data-progress-label]')).to_have_text('858 / 1,200 processed')
    with page.expect_download() as downloaded:
        first.locator('[data-download="answers"]').click()
    destination = tmp_path / 'answers.jsonl'
    downloaded.value.save_as(destination)
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert len(rows) == 846 and all(row['sample'] for row in rows)
    with page.expect_download() as downloaded:
        first.locator('[data-download="errors"]').click()
    downloaded.value.save_as(destination)
    assert len(destination.read_text().splitlines()) == 12
    page.locator('[data-design="inspector"]').click()
    expect(page.locator('#detail-name')).to_have_text('Support ticket classification')
    expect(page.locator('[data-remaining]')).to_have_text('342')
    expect(page.locator('#detail-outcome')).to_contain_text('unprocessed requests will not run')
    page.locator('#stop-all-form button').click()
    page.locator('#stop-all-form button').click()
    expect(page.locator('#stop-all-form button')).to_be_disabled()
    page.locator('#advance').click()
    page.locator('#status-filter').select_option('in_progress')
    expect(page.locator('#empty')).to_be_visible()
    page.locator('#clear').click()
    page.locator('#status-filter').select_option('paused')
    expect(page.locator('#visible-count')).to_have_text('1 batch')


def test_record_text_is_escaped_and_hash_links_select_design(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/preview-batch-designs.py'))
    original = module['sample_data']
    def injected():
        data = original()
        data['batches'][0]['metadata']['name'] = '<img src=x onerror="window.unsafe=true">'
        return data
    module['render'].__globals__['sample_data'] = injected
    output = tmp_path / 'injected.html'
    output.write_text(module['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(output.as_uri() + '#inspector')
        expect(page.locator('[data-design="inspector"]')).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('#detail-name')).to_contain_text('<img src=x')
        assert page.evaluate('window.unsafe') is None
        assert page.locator('#detail-name img').count() == 0
        page.locator('summary', has_text='Batch API record').click()
        expect(page.locator('#batch-record')).to_contain_text('<img src=x')
        browser.close()
