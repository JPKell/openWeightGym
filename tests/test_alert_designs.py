"""Browser contracts for the portable Alerts proposals; all actions use local fixtures."""

import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-alert-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100}, timezone_id='America/Vancouver')
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Design proposals must never contact live services.'
        browser.close()


def test_acknowledgement_persists_across_layouts_without_clearing_condition(preview):
    page = preview
    page.locator('#episode-list [data-open="gpu-atlas"]').click()
    page.locator('#inspector [value="acknowledge"]').click()
    expect(page.locator('#open-count')).to_have_text('6')
    expect(page.locator('#unseen-count')).to_have_text('3')
    expect(page.locator('#inspector [data-acknowledgement]')).to_contain_text('Acknowledged by jordan')
    for layout in ('board', 'ledger', 'desk'):
        page.locator(f'[data-design="{layout}"]').click()
        expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('#inspector [data-title]')).to_have_text('GPU temperature above threshold')
        expect(page.locator('#inspector [data-state]')).to_have_text('Active')
        expect(page.locator('#inspector [value="acknowledge"]')).not_to_be_visible()
    page.locator('#refresh').click()
    expect(page.locator('#inspector [data-acknowledgement]')).to_be_visible()
    page.locator('#reset').click()
    expect(page.locator('#unseen-count')).to_have_text('4')


def test_memory_review_is_separate_from_acknowledgement(preview):
    page = preview
    page.locator('#scope').select_option('review')
    page.locator('#inspector [value="acknowledge"]').click()
    expect(page.locator('#review-count')).to_have_text('1')
    expect(page.locator('#inspector [value="review"]')).to_be_visible()
    page.locator('#inspector [value="review"]').click()
    expect(page.locator('#open-count')).to_have_text('5')
    expect(page.locator('#review-count')).to_have_text('0')
    expect(page.locator('#result-count')).to_contain_text('0 matching')
    page.locator('#scope').select_option('history')
    page.locator('#episode-list [data-open="memory-atlas"]').click()
    expect(page.locator('#inspector [data-state]')).to_have_text('reviewed')
    expect(page.locator('#inspector [data-recovery]')).to_contain_text('not proof of memory recovery')
    expect(page.locator('#inspector [data-demo-actions]')).not_to_be_visible()


def test_filters_pagination_scoped_export_and_escaped_text(preview):
    page = preview
    page.locator('#scope').select_option('all')
    page.locator('#next').click()
    expect(page.locator('#page-summary')).to_have_text('7–12 of 12')
    page.locator('#previous').click()
    expect(page.locator('#page-summary')).to_have_text('1–6 of 12')
    page.locator('#source').select_option('GPU temperature')
    page.locator('#search').fill('orion')
    expect(page.locator('#result-count')).to_contain_text('1 matching episode')
    with page.expect_download() as download:
        page.locator('#export').click()
    payload = json.loads(Path(download.value.path()).read_text())
    assert payload['sample'] is True
    assert [row['id'] for row in payload['episodes']] == ['gpu-orion']
    assert payload['source'] == 'GPU temperature' and payload['search'] == 'orion'
    page.locator('#search').fill('<img src=x onerror=alert(1)>')
    expect(page.locator('#result-count')).to_contain_text('0 matching')
    assert page.locator('img[src=x]').count() == 0
    page.locator('[data-clear]').first.click()
    expect(page.locator('#result-count')).to_contain_text('6 matching')


def test_unavailable_sources_keep_episodes_open_and_empty_history_remains_clear(preview):
    page = preview
    page.locator('#scenario').select_option('unavailable')
    expect(page.locator('#open-count')).to_have_text('6')
    expect(page.locator('#unknown-count')).to_have_text('5')
    expect(page.locator('#source-count')).to_have_text('0 / 7 available')
    expect(page.locator('#inspector [data-unknown]')).to_be_visible()
    page.locator('#scenario').select_option('healthy')
    expect(page.locator('#open-count')).to_have_text('0')
    expect(page.locator('#empty-slot')).to_contain_text('Check source availability')
    page.locator('#scope').select_option('history')
    expect(page.locator('#result-count')).to_contain_text('6 matching')
    page.locator('#scenario').select_option('empty')
    expect(page.locator('#source-count')).to_have_text('No readings yet')
    expect(page.locator('#inspector')).to_contain_text('No episode selected')


def test_layouts_themes_states_and_keyboard_selection_fit(preview):
    page = preview
    for width in (1600, 1024, 768, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        for theme in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for layout in ('desk', 'board', 'ledger'):
                page.locator(f'[data-design="{layout}"]').click()
                for scenario in ('normal', 'healthy', 'unavailable', 'empty'):
                    page.locator('#scenario').select_option(scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, layout, scenario)
                page.locator('#scenario').select_option('normal')
                container = {'desk': '#episode-list', 'board': '#episode-board', 'ledger': '#episode-ledger'}[layout]
                button = page.locator(container + ' [data-open="memory-atlas"]')
                button.focus()
                page.keyboard.press('Enter')
                expect(page.locator('#inspector [data-title]')).to_have_text('Model worker killed by memory pressure')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.evaluate('''() => {
        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
        return ids.length === new Set(ids).size;
    }''')
    assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
        .filter(el => el.getBoundingClientRect().width && !el.closest('[hidden]'))
        .every(el => el.labels?.length || el.getAttribute('aria-label'))''')
