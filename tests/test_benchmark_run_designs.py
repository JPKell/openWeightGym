"""User journeys through the individual-run proposals, without inference or network access."""

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
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-benchmark-run-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not requests, 'Run design previews must not contact live services.'
        browser.close()


def test_readable_layouts_and_run_states_in_both_appearances(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1440, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in ('briefing', 'ledger', 'explorer'):
                page.locator(f'[data-design="{layout}"]').click()
                expect(page.locator('#workspace')).to_have_attribute('data-layout', layout)
                expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
                for state in ('normal', 'stale', 'failed', 'empty'):
                    page.locator('#scenario').select_option(state)
                    expect(page.locator('#summary')).to_be_visible()
                    expect(page.locator('#run-passport')).to_be_visible()
                    expect(page.locator('#evidence-panel')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout, state)
                    assert page.evaluate('''() => {
                      const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                      return ids.length === new Set(ids).size;
                    }''')
                    if state in ('failed', 'empty'):
                        expect(page.locator('#score')).to_have_text('—')
                    if state == 'empty':
                        expect(page.locator('#sample-rows [data-empty-row]')).to_be_visible()
                        expect(page.locator('#evidence-empty')).to_be_visible()
                        expect(page.locator('#next-sample')).to_be_disabled()


def test_filter_compare_and_inspect_a_new_failure_without_losing_context(preview):
    page = preview
    page.locator('#show-regressions').click()
    expect(page.locator('#visible-count')).to_have_text('1 of 48 samples')
    expect(page.locator('#selected-id')).to_contain_text('S043')
    page.locator('[data-evidence="checks"]').click()
    expect(page.locator('#check-rows tr')).to_have_count(4)
    failed = page.locator('#check-rows tr').filter(has_text='due_date')
    expect(failed).to_contain_text('null')
    expect(failed).to_contain_text('2026-12-01')
    expect(failed).to_contain_text('Fail')
    for layout in ('ledger', 'explorer', 'briefing'):
        page.locator(f'[data-design="{layout}"]').click()
        expect(page.locator('#outcome')).to_have_value('regression')
        expect(page.locator('#selected-id')).to_contain_text('S043')
        expect(page.locator('#evidence-checks')).to_be_visible()
    page.locator('#baseline').select_option('incompatible')
    page.locator('#compare').click()
    expect(page.locator('#comparison-note')).to_contain_text('Comparison unavailable')
    expect(page.locator('#comparison-counts')).to_be_empty()
    expect(page.locator('#show-regressions')).to_be_disabled()
    expect(page.locator('#outcome')).to_have_value('all')
    page.locator('#sample-search').fill('not a sample')
    expect(page.locator('#sample-rows [data-empty-row]')).to_be_visible()
    expect(page.locator('#evidence-empty')).to_be_visible()
    page.locator('#clear-filters').click()
    page.locator('#show-unavailable').click()
    expect(page.locator('#visible-count')).to_have_text('4 of 48 samples')
    expect(page.locator('#sample-error')).to_contain_text('Input exceeds')
    page.locator('#next-sample').click()
    page.locator('#next-sample').click()
    expect(page.locator('#selected-outcome')).to_have_text('Unavailable')
    expect(page.locator('#sample-error')).to_contain_text('Connection closed')
    page.locator('[data-evidence="measurements"]').click()
    expect(page.locator('#evidence-measurements')).to_contain_text('"request_seconds": null')


def test_metric_groups_sorting_and_exports_keep_original_precision(preview, tmp_path):
    page = preview
    page.locator('[data-columns="timing"]').click()
    expect(page.locator('#sample-table th[data-column="load_seconds"]')).to_be_visible()
    expect(page.locator('#sample-table th[data-column="peak_gpu_mib"]')).to_be_hidden()
    expect(page.locator('[data-sample-row="S002"] [data-cell="load_seconds"]')).to_have_text('0.284')
    page.locator('[data-sort="load_seconds"]').click()
    expect(page.locator('#sample-rows tr').first).to_have_attribute('data-sample-row', 'S002')
    page.locator('#budget').select_option('2048')
    with page.expect_download() as result:
        page.locator('#export-samples').click()
    csv_path = tmp_path / 'samples.csv'
    result.value.save_as(csv_path)
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 12
    assert {row['context_tokens'] for row in rows} == {'2048'}
    assert next(row for row in rows if row['id'] == 'S002')['load_seconds'] == '0.284'
    with page.expect_download() as result:
        page.locator('#export-run').click()
    json_path = tmp_path / 'run.json'
    result.value.save_as(json_path)
    document = json.loads(json_path.read_text())
    assert len(document['samples']) == 48, 'Run export keeps evidence outside the current filter.'
    assert document['samples'][42]['evidence']['case']['expected']['due_date'] is None
    assert document['environment']['machine']['host'] == 'atlas.local'


def test_generation_and_embedding_profiles_keep_their_own_measurements(preview):
    page = preview
    for width in (1440, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        for layout in ('briefing', 'ledger', 'explorer'):
            page.locator(f'[data-design="{layout}"]').click()
            for profile in ('speed', 'embedding'):
                page.locator('#profile').select_option(profile)
                expect(page.locator('#comparison-panel')).to_be_hidden()
                expect(page.locator('#breakdown')).to_be_hidden()
                expect(page.locator('[data-evidence="checks"]')).to_be_hidden()
                if profile == 'embedding':
                    expect(page.locator('#run-heading .page-title')).to_have_text('Qwen3 Embedding 8B')
                    expect(page.locator('#evidence-receipt')).to_be_visible()
                    expect(page.locator('#evidence-receipt')).to_contain_text('1024')
                    expect(page.locator('[data-evidence="answer"]')).to_be_hidden()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, layout, profile)
    page.locator('#profile').select_option('quality')
    expect(page.locator('#comparison-panel')).to_be_visible()
    expect(page.locator('#score')).to_have_text('92.9%')
