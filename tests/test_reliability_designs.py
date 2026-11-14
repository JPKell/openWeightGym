"""Portable Reliability proposals: scope, evidence, recovery and responsive behavior."""

import csv
import io
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-reliability-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not requests, 'Sample previews must never contact live services.'
        browser.close()


def test_layouts_themes_states_and_details_fit_desktop_tablet_phone(preview):
    page = preview
    for width in (1600, 768, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        for theme in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for layout in ('briefing', 'ledger', 'desk'):
                page.locator(f'[data-design="{layout}"]').click()
                expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
                for state in ('normal', 'healthy', 'empty', 'offline'):
                    page.locator('#scenario').select_option(state)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, layout, state)
                    assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                        .filter(el => el.getBoundingClientRect().width && !el.closest('[hidden]'))
                        .every(el => el.labels?.length || el.getAttribute('aria-label'))''')
                page.locator('#scenario').select_option('normal')
                if layout != 'desk':
                    page.locator('#candidate-rows [data-inspect]').first.click()
                    expect(page.locator('#evidence-dialog')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.keyboard.press('Escape')
                else:
                    positions = page.evaluate('''() => ['inventory','inspector-slot','outcomes'].map(id =>
                        document.getElementById(id).getBoundingClientRect().top)''')
                    if width <= 800:
                        assert positions == sorted(positions), positions
    assert page.evaluate('''() => {const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
        return ids.length === new Set(ids).size;}''')
    dark = page.locator('body').evaluate('(e) => getComputedStyle(e).backgroundColor')
    page.locator('#theme').click()
    assert page.locator('body').evaluate('(e) => getComputedStyle(e).backgroundColor') != dark
    assert page.locator('[data-segment="timeout"]').first.evaluate('(e) => getComputedStyle(e).backgroundColor') != 'rgba(0, 0, 0, 0)'
    page.goto(page.url.split('#')[0] + '#ledger')
    expect(page.locator('#workspace')).to_have_attribute('data-layout', 'ledger')
    page.locator('#scenario').select_option('healthy')
    page.locator('#reset-demo').click()
    expect(page.locator('#kpi-excluded')).to_have_text('2')


def test_filters_and_sort_keep_counts_chart_and_lifetime_quality_consistent(preview):
    page = preview
    expect(page.locator('#candidate-rows tr')).to_have_count(8)
    expect(page.locator('#kpi-success')).to_have_text('87.2%')
    expect(page.locator('#kpi-faults')).to_have_text('42')
    expect(page.locator('#kpi-excluded')).to_have_text('2')
    expect(page.locator('#kpi-regressions')).to_have_text('2')
    page.locator('#issues').check()
    expect(page.locator('#candidate-rows tr')).to_have_count(6)
    expect(page.locator('#outcome-list > div')).to_have_count(6)
    expect(page.locator('#quality-rows tr')).to_have_count(6)
    page.locator('#clear').click()
    page.locator('#model').select_option('qwen3-32b')
    expect(page.locator('#candidate-rows tr')).to_have_count(2)
    expect(page.locator('#quality-rows tr')).to_have_count(1)
    page.locator('#role').select_option('tools')
    expect(page.locator('#candidate-rows tr')).to_have_count(1)
    expect(page.locator('#quality-rows tr')).to_have_count(1)
    expect(page.locator('#kpi-faults')).to_have_text('14')
    page.locator('#fingerprint').select_option('all')
    page.locator('#role').select_option('')
    expect(page.locator('#candidate-rows tr')).to_have_count(3)
    expect(page.locator('#quality-rows tr')).to_have_count(2)
    page.locator('#clear').click()
    page.locator('#sort').select_option('rate')
    expect(page.locator('#candidate-rows tr').first).to_contain_text('qwen3-32b')
    page.locator('#search').fill('unmatched')
    expect(page.locator('#candidate-rows')).to_contain_text('No candidates match')
    expect(page.locator('#kpi-success')).to_have_text('—')
    page.locator('#clear').click()
    page.locator('#role').select_option('direct')
    expect(page.locator('#candidate-rows tr')).to_have_count(1)
    expect(page.locator('#candidate-rows')).to_contain_text('Direct model request')


def test_recovery_reset_confirmation_and_lifetime_evidence_persist_across_designs(preview):
    page = preview
    page.locator('[data-design="desk"]').click()
    page.locator('#desk-list [data-candidate="candidate-1"]').click()
    quality_before = page.locator('#quality-rows').inner_text()
    page.locator('#recover').click()
    expect(page.locator('#detail-content [data-state]')).to_have_text('recovery required')
    page.keyboard.press('Escape')
    expect(page.locator('#recover')).to_have_text('Run recovery')
    page.locator('#recover').click()
    page.locator('#recover').click()
    expect(page.locator('#detail-content [data-state]')).to_have_text('eligible')
    expect(page.locator('#detail-error')).to_have_text('8')
    expect(page.locator('#kpi-excluded')).to_have_text('1')
    page.locator('[data-design="ledger"]').click()
    page.locator('#candidate-rows [data-inspect][data-candidate="candidate-1"]').click()
    expect(page.locator('#detail-content [data-state]')).to_have_text('eligible')
    page.locator('#reset-candidate').click()
    expect(page.locator('#detail-error')).to_have_text('8')
    page.locator('#reset-candidate').click()
    expect(page.locator('#detail-rate')).to_have_text('—')
    expect(page.locator('#detail-feedback')).to_have_text('0 positive / 0 negative')
    expect(page.locator('#detail-factor')).to_have_text('×1.000')
    assert page.locator('#quality-rows').inner_text() == quality_before
    page.keyboard.press('Escape')
    page.locator('#fingerprint').select_option('all')
    expect(page.locator('#candidate-rows tr')).to_have_count(10)
    expect(page.locator('#candidate-rows')).to_contain_text('historical')


def test_reader_historical_cooldown_and_offline_controls(preview):
    page = preview
    page.locator('[data-design="desk"]').click()
    page.locator('#desk-list [data-candidate="candidate-3"]').click()
    expect(page.locator('#recover')).to_be_hidden()
    expect(page.locator('#manage-note')).to_contain_text('cooldown')
    page.locator('#access').select_option('reader')
    expect(page.locator('#manage-actions')).to_be_hidden()
    expect(page.locator('#scope-label')).to_have_text('your requests')
    expect(page.locator('#detail-content [data-state]')).to_have_text('cooldown')
    expect(page.locator('#kpi-excluded')).to_have_text('2')
    expect(page.locator('#quality-rows')).to_contain_text('Reading explanations needs')
    page.locator('#access').select_option('operator')
    page.locator('#fingerprint').select_option('historical')
    expect(page.locator('#manage-actions')).to_be_hidden()
    expect(page.locator('#manage-note')).to_contain_text('Historical fingerprint')
    page.locator('#clear').click()
    page.locator('#scenario').select_option('offline')
    expect(page.locator('#offline-note')).to_be_visible()
    expect(page.locator('#manage-actions')).to_be_hidden()
    expect(page.locator('#kpi-excluded')).to_have_text('—')
    expect(page.locator('#detail-factor')).to_have_text('Unavailable')
    expect(page.locator('#candidate-rows tr')).to_have_count(8)


def test_filtered_csv_and_keyboard_record_inspection(preview):
    page = preview
    page.locator('#model').select_option('llama-3.3-70b')
    with page.expect_download() as download_info:
        page.locator('#export').click()
    exported = list(csv.DictReader(io.StringIO(Path(download_info.value.path()).read_text())))
    assert len(exported) == 1
    assert exported[0]['error'] == '8'
    assert exported[0]['state'] == 'recovery required'
    button = page.locator('#candidate-rows [data-inspect]')
    button.focus()
    page.keyboard.press('Enter')
    expect(page.locator('#evidence-dialog')).to_be_visible()
    page.locator('#detail-request').click()
    expect(page.locator('#event-dialog')).to_contain_text('Sample request #8407')
    page.locator('#close-event').click()
    page.keyboard.press('Escape')
    expect(button).to_be_focused()
    page.locator('#audit').click()
    expect(page.locator('#event-dialog')).to_contain_text('/log?kind=reliability')
