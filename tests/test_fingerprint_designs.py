"""Paired fingerprint proposals preserve archive evidence and work without live services."""

import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ('registry', 'ledger', 'explorer')


@pytest.fixture
def preview(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/preview-fingerprint-designs.py'))
    output = tmp_path / 'index.html'
    output.write_text(module['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page, module['samples']()
        assert not errors
        assert not network, 'Portable previews must not contact live services.'
        browser.close()


def design(page, name):
    page.locator(f'[data-design="{name}"]').click()
    expect(page.locator(f'[data-design="{name}"]')).to_have_attribute('aria-pressed', 'true')


def test_six_screens_fit_all_widths_and_appearances(preview):
    page, _ = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--theme-bg').trim()") == ('#101722' if theme == 'dark' else '#f3f6fb')
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for name in DESIGNS:
                design(page, name)
                for view in ('directory', 'detail'):
                    page.locator(f'button[data-view="{view}"]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, name, view)
                    assert page.locator('h1:visible').count() == 1
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                        return ids.length === new Set(ids).size;
                    }''')
                    assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                        .filter(e => e.getBoundingClientRect().width)
                        .every(e => e.labels?.length || e.getAttribute('aria-label'))''')


@pytest.mark.parametrize('name', DESIGNS)
def test_directory_search_sort_pagination_and_recovery(preview, name):
    page, records = preview
    design(page, name)
    expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(12)
    page.locator('#next').click()
    expect(page.locator('#page-summary')).to_have_text('Showing 13–24 of 24')
    page.locator('#page-size').select_option('24')
    expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(24)
    page.locator('[data-sort="context"]').click()
    expect(page.locator('#fingerprint-rows [data-record]').first.locator('[data-field="context"]')).to_have_text('8,192')
    page.locator('#model-filter').select_option('qwen3-32b')
    expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(3)
    page.locator('#context-filter').select_option('32768')
    expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(1)
    page.locator('#start').fill('does-not-exist')
    expect(page.locator('#no-records')).to_be_visible()
    expect(page.locator('#export-directory')).to_be_disabled()
    page.locator('#no-records [data-clear]').click()
    expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(24)
    page.locator('#start').fill(records[2]['fingerprint'][:16])
    expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(1)
    page.reload()
    expect(page.locator('#start')).to_have_value(records[2]['fingerprint'][:16])
    expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(1)


@pytest.mark.parametrize('name', DESIGNS)
def test_detail_contains_complete_snapshot_and_restores_directory(preview, name):
    page, records = preview
    record = records[2]  # multimodal record includes a separate projector
    design(page, name)
    page.locator('#start').fill(record['fingerprint'][:12])
    page.locator('#fingerprint-rows [data-open]').click()
    if name == 'explorer':
        if page.locator('#open-peek').is_visible():
            expect(page.locator('#peek-model')).to_have_text(record['model'])
            page.locator('#open-peek').click()
    expect(page.locator('#record-model')).to_have_text(record['model'])
    expect(page.locator('#record-model')).to_be_focused()
    expect(page.locator('#file-records [data-file-hash]')).to_have_count(2)
    for file in record['files'].values():
        expect(page.locator('#file-records')).to_contain_text(file['sha256'])
        expect(page.locator('#file-records')).to_contain_text(file['path'])
    expect(page.locator('#setting-rows')).to_contain_text('false')
    page.locator('#setting-search').fill('seed')
    expect(page.locator('#setting-rows tr:visible')).to_have_count(1)
    expect(page.locator('#setting-rows tr:visible td')).to_have_text('0')
    page.locator('#setting-search').fill('absent-setting')
    expect(page.locator('#no-settings')).to_be_visible()
    page.reload()
    expect(page.locator('#record-hash')).to_have_text(record['fingerprint'])
    expect(page.locator('#workspace')).to_have_attribute('data-layout', name)
    page.locator('#back').click()
    expect(page.locator('#start')).to_have_value(record['fingerprint'][:12])
    expect(page.locator('#fingerprint-rows [data-open]')).to_be_focused()


def test_json_export_copy_and_missing_deep_link(preview):
    page, records = preview
    page.locator('#model-filter').select_option('qwen3-32b')
    with page.expect_download() as downloaded:
        page.locator('#export-directory').click()
    assert len(json.loads(Path(downloaded.value.path()).read_text())) == 3
    page.locator('button[data-view="detail"]').click()
    with page.expect_download() as downloaded:
        page.locator('#export-record').click()
    assert json.loads(Path(downloaded.value.path()).read_text()) == records[0]
    page.evaluate('''() => { window.copied = [];
        Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => window.copied.push(value)}, configurable: true});
    }''')
    page.locator('#copy-hash').click()
    expect(page.locator('#feedback')).to_have_text('Fingerprint copied.')
    assert page.evaluate('window.copied[0]') == records[0]['fingerprint']
    page.locator('#copy-link').click()
    assert f'id={records[0]["fingerprint"]}' in page.evaluate('window.copied[1]')
    page.locator('#file-records [data-copy-file]').first.click()
    assert page.evaluate('window.copied[2]') == records[0]['files']['model']['sha256']
    page.evaluate("location.hash = 'design=ledger&view=detail&id=missing'")
    expect(page.locator('#missing-record')).to_be_visible()
    expect(page.locator('#directory-view')).to_be_visible()


def test_keyboard_navigation_and_record_switching(preview):
    page, records = preview
    page.locator('#fingerprint-rows [data-open]').first.focus()
    page.keyboard.press('Enter')
    expect(page.locator('#record-model')).to_be_focused()
    page.locator('#next-record').click()
    expect(page.locator('#record-hash')).to_have_text(records[1]['fingerprint'])
    page.locator('#previous-record').click()
    expect(page.locator('#record-hash')).to_have_text(records[0]['fingerprint'])
    design(page, 'explorer')
    page.locator('[data-anchor="record-settings"]').click()
    expect(page.locator('#record-settings')).to_be_focused()


def test_explorer_opens_records_directly_on_a_phone(preview):
    page, records = preview
    page.set_viewport_size({'width': 390, 'height': 844})
    design(page, 'explorer')
    page.locator('#fingerprint-rows [data-open]').first.click()
    expect(page.locator('#detail-view')).to_be_visible()
    expect(page.locator('#record-hash')).to_have_text(records[0]['fingerprint'])
