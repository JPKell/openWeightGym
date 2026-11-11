"""Exercise the sample-only Files proposals without a service or real model files."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-files-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(output.as_uri())
        yield page
        assert not errors
        browser.close()


def test_designs_fit_and_support_filtering_in_both_themes(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        assert page.locator('#cleanup').evaluate("e => getComputedStyle(e).backgroundColor") != 'rgba(0, 0, 0, 0)'
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1100})
            for design in ('ledger', 'matrix', 'desk'):
                page.locator(f'[data-design="{design}"]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_role('button', name='Browse Storage files', exact=True).click()
                page.get_by_label('Search files', exact=True).fill('no-such-model')
                playwright.expect(page.locator('[data-empty-row]')).to_be_visible()
                page.get_by_label('Search files', exact=True).fill('qwen')
                playwright.expect(page.locator('[data-select]')).to_have_count(1)
                page.get_by_label('Search files', exact=True).fill('')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_bulk_move_preserves_storage_and_in_use_copies(preview):
    page = preview
    page.get_by_role('button', name='Browse Cache files', exact=True).click()
    page.get_by_label('Select qwen3-32b-q6_k.gguf', exact=True).check()
    page.get_by_label('To', exact=True).select_option('nova')
    page.get_by_label('Action', exact=True).select_option('move')
    playwright.expect(page.locator('#transfer')).to_be_disabled()
    playwright.expect(page.locator('#transfer-note')).to_contain_text('in use')
    page.get_by_role('button', name='Clear selection', exact=True).click()
    page.get_by_label('Filter files by usage', exact=True).select_option('unused')
    page.get_by_label('Select all visible files', exact=True).check()
    playwright.expect(page.locator('#selection')).to_contain_text('3 selected')
    page.locator('#transfer').click()
    playwright.expect(page.locator('#status')).to_contain_text('3 files moved')
    playwright.expect(page.locator('[data-empty-row]')).to_be_visible()
    page.get_by_label('From', exact=True).select_option('storage')
    playwright.expect(page.locator('[data-select]')).to_have_count(8)
    playwright.expect(page.locator('#operation option[value="move"]')).to_be_disabled()
    page.get_by_label('From', exact=True).select_option('cache')
    playwright.expect(page.locator('[data-select]')).to_have_count(3)


def test_shared_directory_blocks_cache_cleanup_and_global_cleanup_keeps_library(preview):
    page = preview
    page.locator('#shared').check()
    playwright.expect(page.get_by_role('button', name='Clean unused Storage + cache copies', exact=True)).to_be_disabled()
    playwright.expect(page.locator('#shared-note')).to_be_visible()
    page.locator('#cleanup').click()
    playwright.expect(page.locator('#status')).to_contain_text('All 8 storage originals')
    playwright.expect(page.locator('#cleanup')).to_be_disabled()
    page.get_by_role('button', name='Browse Storage + cache files', exact=True).click()
    playwright.expect(page.locator('[data-select]')).to_have_count(8)
    page.get_by_label('From', exact=True).select_option('atlas')
    playwright.expect(page.locator('[data-select]')).to_have_count(3)
    page.get_by_label('From', exact=True).select_option('nova')
    playwright.expect(page.locator('[data-select]')).to_have_count(2)


def test_matrix_bulk_copy_updates_presence_and_same_destination_is_blocked(preview):
    page = preview
    page.locator('[data-design="matrix"]').click()
    page.get_by_label('Select all visible files', exact=True).check()
    page.get_by_label('To', exact=True).select_option('storage')
    playwright.expect(page.locator('#transfer')).to_be_disabled()
    page.get_by_label('To', exact=True).select_option('atlas')
    page.locator('#transfer').click()
    playwright.expect(page.locator('#status')).to_contain_text('3 new copies')
    assert page.locator('[data-presence="atlas"]').all_text_contents() == ['●'] * 8
    page.get_by_label('Search files', exact=True).fill('qwen')
    page.get_by_label('Select all visible files', exact=True).check()
    playwright.expect(page.locator('#selection')).to_contain_text('1 selected')
    page.get_by_label('Search files', exact=True).fill('')
    playwright.expect(page.locator('#transfer')).to_be_disabled()
