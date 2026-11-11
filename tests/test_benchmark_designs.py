"""Exercise the sample Runs proposals without contacting compute or starting inference."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-benchmark-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Design previews must never contact live services.'
        browser.close()


def target(page, model, node):
    return page.locator(f'[data-target][data-key="{model}/{node}"]')


def test_layouts_keep_all_nodes_and_sections_in_every_state(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            previous = page.locator('html').evaluate('(el) => getComputedStyle(el).backgroundColor')
            page.locator('#theme').click()
            assert page.locator('html').evaluate('(el) => getComputedStyle(el).backgroundColor') != previous
        for width in (1440, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1100})
            for design in ('matrix', 'lanes', 'studio'):
                page.locator(f'button[data-design="{design}"]').click()
                expect(page.locator(f'[data-design="{design}"]')).to_have_attribute('aria-pressed', 'true')
                for state in ('normal', 'offline', 'empty', 'readonly'):
                    page.locator('#scenario').select_option(state)
                    expect(page.locator('#nodes [data-node]')).to_have_count(5)
                    for node in ('front', 'atlas', 'orion', 'cedar', 'nova'):
                        expect(page.locator(f'#nodes [data-node="{node}"]')).to_be_visible()
                    for section in ('running-panel', 'composer', 'queue-panel', 'history-panel'):
                        expect(page.locator(f'#{section}')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, design, state)
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                        return ids.length === new Set(ids).size;
                    }''')


def test_batch_review_preserves_hidden_selection_and_excludes_active_pairs(preview):
    page = preview
    expect(target(page, 'qwen8', 'atlas')).to_be_disabled()
    expect(target(page, 'qwen8', 'cedar')).to_be_disabled()
    expect(target(page, 'qwen8', 'nova')).to_be_disabled()
    expect(target(page, 'embed', 'front')).to_have_count(0)
    page.locator('#select-gaps').click()
    expect(page.locator('#selection-count')).to_have_text('3 runs · 2 models · 2 nodes')
    page.locator('#search-models').fill('qwen')
    expect(page.locator('#selection-note')).to_contain_text('2 selected targets outside this search')
    page.locator('[data-design="lanes"]').click()
    expect(target(page, 'qwen8', 'orion')).to_be_checked()
    page.locator('[data-design="studio"]').click()
    page.locator('#review').click()
    expect(page.locator('#review-rows tr')).to_have_count(3)
    expect(page.locator('#review-summary')).to_contain_text('2 models · 2 nodes')
    page.locator('#queue-batch').click()
    expect(page.locator('#queue tr')).to_have_count(6)
    expect(page.locator('#feedback')).to_contain_text('3 sample runs queued')
    expect(page.locator('#review')).to_be_disabled()
    expect(target(page, 'qwen8', 'orion')).to_be_disabled()
    page.locator('#pause-all').click()
    expect(page.locator('#queue [data-state]')).to_have_text(['Paused'] * 6)
    expect(page.locator('#running .live-run')).to_have_count(2)
    page.locator('#pause-all').click()
    page.locator('[data-remove="Q-107"]').click()
    expect(target(page, 'qwen8', 'orion')).to_be_enabled()


def test_capability_rules_all_node_selection_details_and_recovery(preview):
    page = preview
    page.locator('#benchmark').select_option('load')
    expect(target(page, 'sdxl', 'atlas')).to_be_enabled()
    expect(target(page, 'kokoro', 'cedar')).to_be_enabled()
    page.locator('input[data-select-node="front"]').check()
    expect(page.locator('#selection-count')).to_have_text('3 runs · 3 models · 1 node')
    page.locator('#scenario').select_option('offline')
    expect(page.locator('#connection-note')).to_be_visible()
    expect(page.locator('#review')).to_be_disabled()
    expect(page.locator('#pause-all')).to_be_disabled()
    expect(page.locator('#running .live-run')).to_have_count(2)
    page.locator('#scenario').select_option('readonly')
    expect(page.locator('#permission-note')).to_be_visible()
    assert page.locator('[data-target]:enabled').count() == 0
    page.locator('#scenario').select_option('normal')
    page.locator('#history-state').select_option('failed')
    expect(page.locator('#history tr')).to_have_count(1)
    page.locator('[data-detail="R-099"]').click()
    expect(page.locator('#run-detail')).to_contain_text('insufficient system RAM')
    page.keyboard.press('Escape')
    page.locator('[data-rerun="R-099"]').click()
    expect(page.locator('#benchmark')).to_have_value('context')
    expect(target(page, 'qwen8', 'cedar')).to_be_checked()
    page.locator('#history-search').fill('no such model')
    expect(page.locator('#history [data-empty-row]')).to_be_visible()
    page.locator('#reset').click()
    expect(page.locator('#queue tr')).to_have_count(3)
    expect(page.locator('#history tr')).to_have_count(6)
