"""Manage proposals retain the live controls, benchmark registry and per-benchmark drafts."""

import json
from pathlib import Path
import runpy

import pytest

from benchmarks import BENCHMARKS

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-benchmark-manage-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1680, 'height': 1100})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not requests, 'Manage previews must not contact model or settings services.'
        browser.close()


def choose(page, benchmark):
    page.locator(f'[data-benchmark="{benchmark}"]').click()


def test_complete_registry_controls_and_recorded_values(preview):
    page = preview
    definitions = json.loads(page.locator('#definitions').text_content())
    assert len(definitions) == len(BENCHMARKS)
    for bench, actual in zip(definitions, BENCHMARKS):
        for key in ('id', 'name', 'kpis', 'method', 'formula', 'summary', 'version'):
            assert bench[key] == json.loads(json.dumps(actual[key]))
        choose(page, bench['id'])
        expect(page.locator('[name=benchmark]')).to_have_value(bench['id'])
        expect(page.locator('#formula')).to_have_value(bench['formula'])
        expect(page.locator('#enabled')).to_be_checked()
        expect(page.locator('#green')).to_have_value('high')
        expect(page.locator('#server')).to_have_count(1 if bench.get('quality') else 0)
        assert page.locator('#benchmark-recorded-values tbody th code').all_text_contents() == [key for key, _, _ in bench['kpis']]
        assert page.locator('[data-insert]').count() == len(bench['kpis']) + 5
    page.locator('[data-card-key=benchmark-external] summary').click()
    expect(page.get_by_role('link', name='Upstream source and dataset', exact=False)).to_be_visible()
    assert page.locator('.freshness-rules dt').count() == 4


def test_drafts_save_discard_validation_and_error_recovery(preview):
    page = preview
    page.locator('#formula').fill('accuracy * 2')
    page.locator('#green').select_option('low')
    page.locator('#server').select_option('fresh')
    page.locator('#enabled').uncheck()
    choose(page, 'speed')
    expect(page.locator('[data-benchmark=json_schema] [data-draft]')).to_be_visible()
    choose(page, 'json_schema')
    expect(page.locator('#formula')).to_have_value('accuracy * 2')
    expect(page.locator('#server')).to_have_value('fresh')
    expect(page.locator('#green')).to_have_value('low')
    expect(page.locator('#enabled')).not_to_be_checked()
    for layout in ('ledger', 'desk', 'studio'):
        page.locator(f'[data-design={layout}]').click()
        expect(page.locator('#formula')).to_have_value('accuracy * 2')
    page.locator('#scenario').select_option('error')
    page.locator('#save').click()
    expect(page.locator('#feedback')).to_contain_text('Could not save')
    expect(page.locator('#formula')).to_have_value('accuracy * 2')
    page.locator('#scenario').select_option('normal')
    page.locator('#save').click()
    expect(page.locator('#enabled-count')).to_have_text(str(len(BENCHMARKS) - 1))
    expect(page.locator('#save-state')).to_have_text('All changes saved')
    page.locator('#formula').fill('accuracy + unknown_value')
    expect(page.locator('#formula')).to_have_attribute('aria-invalid', 'true')
    page.locator('#save').click()
    expect(page.locator('#save-state')).to_have_text('Unsaved changes')
    page.locator('#discard').click()
    expect(page.locator('#formula')).to_have_value('accuracy * 2')
    page.locator('#scenario').select_option('readonly')
    expect(page.locator('#formula')).to_be_disabled()
    expect(page.locator('#save')).to_be_disabled()
    page.locator('#scenario').select_option('normal')
    expect(page.locator('#save-state')).to_have_text('All changes saved')
    expect(page.locator('#save')).to_be_disabled()
    choose(page, 'speed')
    choose(page, 'json_schema')
    expect(page.locator('#formula')).to_have_value('accuracy * 2')


def test_search_insertion_empty_and_reset(preview):
    page = preview
    page.locator('#benchmark-search').fill('no matching benchmark')
    expect(page.locator('#no-matches')).to_be_visible()
    page.locator('#benchmark-search').fill('Embedder')
    expect(page.locator('[data-benchmark]:visible')).to_have_count(3)
    choose(page, 'embedding_speed')
    page.locator('#formula').fill('')
    page.locator('[data-insert=parameter_count]').focus()
    page.keyboard.press('Enter')
    expect(page.locator('#formula')).to_have_value('parameter_count')
    page.locator('#value-search').fill('not a recorded value')
    expect(page.locator('#value-count')).to_contain_text('No matching values')
    page.locator('#value-search').fill('')
    page.locator('#scenario').select_option('empty')
    expect(page.locator('#catalogue-empty')).to_be_visible()
    expect(page.locator('#manage-grid')).not_to_be_visible()
    page.locator('#restore-catalogue').click()
    expect(page.locator('#formula')).to_have_value('parameter_count')
    page.locator('#reset').click()
    expect(page.locator('#formula')).to_have_value('accuracy')
    expect(page.locator('#save')).to_be_disabled()


def test_all_designs_themes_and_narrow_layouts(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1680, 1280, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1100})
            for layout in ('studio', 'ledger', 'desk'):
                page.locator(f'[data-design={layout}]').click()
                expect(page.locator('#formula')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout)
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                    return ids.length === new Set(ids).size;
                }''')
