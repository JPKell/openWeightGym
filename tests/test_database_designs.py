"""Interactive DB proposals stay local and make scope/destructive-action semantics visible."""

import json
import runpy
import zipfile
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-database-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1600, 'height': 1100}, accept_downloads=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(output.as_uri())
        yield page
        assert not errors
        browser.close()


def test_layouts_expand_nested_tables_and_fit_both_themes(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1600, 390):
            page.set_viewport_size({'width': width, 'height': 1100})
            for design in ('ledger', 'nodes', 'desk'):
                page.locator(f'[data-design="{design}"]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('[data-database="front/accounts"] [data-expand]').click()
                playwright.expect(page.get_by_role('table', name='Tables in owg_accounts on Front', exact=True)).to_be_visible()
                page.locator('[data-database="front/accounts"] [data-expand]').click()
                page.get_by_label('Search databases', exact=True).fill('not-a-database')
                playwright.expect(page.locator('[data-empty-row]')).to_be_visible()
                page.get_by_label('Search databases', exact=True).fill('')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_mixed_engines_are_valid_but_unreachable_or_mismatched_sources_block_complete_export(preview):
    page = preview
    playwright.expect(page.locator('[data-database]')).to_have_count(15)
    playwright.expect(page.locator('#coverage')).to_contain_text('4 / 4')
    playwright.expect(page.locator('#export-all')).to_be_enabled()
    page.locator('#scenario').select_option('offline')
    playwright.expect(page.locator('[data-database]')).to_have_count(15)
    playwright.expect(page.locator('#coverage')).to_contain_text('3 / 4')
    playwright.expect(page.locator('#export-all')).to_be_disabled()
    playwright.expect(page.locator('[data-database="compute-04/loadcoach"] [data-dump]')).to_be_disabled()
    playwright.expect(page.locator('[data-database="front/accounts"] [data-dump]')).to_be_enabled()
    page.locator('#scenario').select_option('mismatch')
    playwright.expect(page.locator('#coverage-message')).to_contain_text('Other PostgreSQL and SQLite stores are valid')
    playwright.expect(page.locator('[data-database="compute/loadcoach"] [data-dump]')).to_be_disabled()
    playwright.expect(page.locator('[data-database="front-cpu/loadcoach"] [data-dump]')).to_be_enabled()
    page.locator('#scenario').select_option('empty')
    playwright.expect(page.locator('[data-empty-row]')).to_be_visible()
    playwright.expect(page.locator('#export-all')).to_be_disabled()


def test_dump_all_ignores_filters_and_downloads_one_file_per_database(preview, tmp_path):
    page = preview
    page.locator('#node-filter').select_option('front-cpu')
    page.locator('#engine-filter').select_option('SQLite')
    playwright.expect(page.locator('[data-database]')).to_have_count(2)
    page.locator('#export-all').click()
    playwright.expect(page.locator('#export-list li')).to_have_count(15)
    with page.expect_download() as result:
        page.locator('#download-export').click()
    path = tmp_path / 'sample.zip'
    result.value.save_as(path)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['sample'] is True
        assert len(manifest['databases']) == 15
        assert len(archive.namelist()) == 16
        assert {item['engine'] for item in manifest['databases']} == {'PostgreSQL', 'SQLite'}
        assert len({item['node'] for item in manifest['databases']}) == 5


def test_selected_scope_survives_filters_and_single_table_sql_stays_scoped(preview, tmp_path):
    page = preview
    page.locator('[data-database="front/accounts"] [data-select]').check()
    page.locator('#node-filter').select_option('remote-cpu')
    playwright.expect(page.locator('#selection')).to_contain_text('1 selected across all filters')
    page.locator('#export-selected').click()
    playwright.expect(page.locator('#export-list li')).to_have_count(1)
    playwright.expect(page.locator('#export-list')).to_contain_text('Front / owg_accounts')
    page.keyboard.press('Escape')
    page.locator('[data-database="remote-cpu/spotter"] [data-expand]').click()
    page.locator('#detail-remote-cpu-spotter [data-table-dump]').click()
    playwright.expect(page.locator('#export-consistency')).to_contain_text('may depend on other tables')
    with page.expect_download() as result:
        page.locator('#download-export').click()
    path = tmp_path / 'sample.sql'
    result.value.save_as(path)
    sql = path.read_text()
    assert 'DESIGN PREVIEW ONLY' in sql and '"readings"' in sql
    assert '"models"' not in sql


def test_purge_cutoff_invalidation_protected_tables_and_separate_disk_reclaim(preview):
    page = preview
    page.locator('[data-database="front/accounts"] [data-maintain]').click()
    page.locator('#preview-purge').click()
    page.locator('#confirm-purge').check()
    playwright.expect(page.locator('#apply-purge')).to_be_disabled()
    page.keyboard.press('Escape')
    row = page.locator('[data-database="front-cpu/spotter"]')
    initial_size = row.locator('[data-size]').inner_text()
    row.locator('[data-maintain]').click()
    page.locator('#preview-purge').click()
    playwright.expect(page.locator('#purge-impact')).to_contain_text('90,000 sample records')
    page.locator('#confirm-purge').check()
    page.locator('#cutoff').fill('2026-11-01')
    playwright.expect(page.locator('#apply-purge')).to_be_disabled()
    playwright.expect(page.locator('#purge-preview')).to_be_hidden()
    page.locator('#preview-purge').click()
    playwright.expect(page.locator('#purge-impact')).to_contain_text('135,000 sample records')
    page.locator('#confirm-purge').check()
    page.locator('#apply-purge').click()
    assert row.locator('[data-size]').inner_text() == initial_size
    playwright.expect(page.locator('#cleanup-result')).to_contain_text('unchanged until disk reclamation')
    page.locator('#confirm-reclaim').check()
    page.locator('#apply-reclaim').click()
    assert row.locator('[data-size]').inner_text() != initial_size
    playwright.expect(page.locator('#cleanup-result')).to_contain_text('illustrative measurement')
    page.locator('#preview-purge').click()
    playwright.expect(page.locator('#purge-impact')).to_contain_text('0 sample records')
