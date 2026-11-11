"""Browser contracts for the three sample-only Compute nodes proposals."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-node-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Sample previews must not contact live services.'
        browser.close()


def choose(page, name):
    directory = '#fleet-rows' if page.locator('#workspace').get_attribute('data-layout') == 'fleet' else '#node-list'
    page.locator(f'{directory} [data-node-choice="{name}"]').click()


def test_all_layouts_themes_widths_and_sections_are_accessible(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in ('navigator', 'focus', 'fleet'):
                page.locator(f'[data-design="{layout}"]').click()
                for section in ('hardware', 'runtime', 'storage', 'access'):
                    if layout != 'navigator':
                        page.locator(f'button[data-section="{section}"]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout, section)
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                        return ids.length === new Set(ids).size;
                    }''')
                    assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                        .filter(el => el.getBoundingClientRect().width)
                        .every(el => el.labels?.length || el.getAttribute('aria-label'))''')


def test_global_pairing_is_collapsed_and_survives_node_and_design_switches(preview):
    page = preview
    expect(page.locator('#pairing-panel')).to_be_hidden()
    expect(page.locator('#add-node')).to_have_attribute('aria-expanded', 'false')
    page.locator('#add-node').click()
    expect(page.locator('#pairing-panel')).to_be_visible()
    expect(page.locator('#copy-command')).to_be_focused()
    expect(page.locator('#pairing-command')).to_contain_text('--code DEMO-ONLY')
    for layout in ('navigator', 'focus', 'fleet'):
        page.locator(f'[data-design="{layout}"]').click()
        choose(page, 'nova')
        expect(page.locator('#pairing-panel')).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 1440, 'height': 1000})
    page.keyboard.press('Escape')
    expect(page.locator('#pairing-panel')).to_be_hidden()
    expect(page.locator('#add-node')).to_be_focused()


def test_drafts_follow_nodes_and_saves_are_scoped(preview):
    page = preview
    page.locator('#ram-headroom').fill('8192')
    page.locator('#llama_cpp-path').fill('/custom/llama-server')
    for layout in ('focus', 'fleet', 'navigator'):
        page.locator(f'[data-design="{layout}"]').click()
        choose(page, 'nova')
        choose(page, 'atlas')
        expect(page.locator('#ram-headroom')).to_have_value('8192')
        expect(page.locator('#llama_cpp-path')).to_have_value('/custom/llama-server')
    choose(page, 'nova')
    page.locator('#ram-headroom').fill('2048')
    page.locator('#save').click()
    expect(page.locator('#feedback')).to_contain_text('Saved nova in this browser')
    choose(page, 'atlas')
    expect(page.locator('#save-state')).to_contain_text('Unsaved changes')
    page.reload()
    expect(page.locator('#ram-headroom')).to_have_value('4096')
    choose(page, 'nova')
    expect(page.locator('#ram-headroom')).to_have_value('2048')


def test_node_filters_empty_state_and_hardware_capabilities(preview):
    page = preview
    page.locator('#node-search').fill('no-such-node')
    expect(page.locator('#no-nodes')).to_be_visible()
    page.locator('#node-search').fill('')
    page.locator('#node-filter').select_option('available')
    expect(page.locator('#node-list [data-node-choice]')).to_have_count(3)
    page.locator('#node-filter').select_option('attention')
    expect(page.locator('#node-list [data-node-choice]')).to_have_count(2)
    choose(page, 'orion')
    expect(page.locator('#node-notice')).to_contain_text('asleep')
    expect(page.locator('[data-gpu-unsupported]')).to_be_visible()
    expect(page.locator('[data-gpu-engine="strata"]')).to_be_hidden()
    page.locator('#node-filter').select_option('all')
    choose(page, 'front-cpu')
    expect(page.locator('#gpu-rows')).to_contain_text('No GPUs detected')
    choose(page, 'atlas')
    page.locator('[data-gpu-row="A"] [data-gpu-disabled]').check()
    expect(page.locator('[data-gpu-row="A"] [data-gpu-engine="llama_cpp"]')).to_be_disabled()
    page.locator('#discard').click()
    expect(page.locator('[data-gpu-row="A"] [data-gpu-engine="llama_cpp"]')).to_be_enabled()


def test_lifecycle_actions_require_confirmation_and_preserve_drafts(preview):
    page = preview
    page.locator('#ram-headroom').fill('8192')
    page.locator('#disable-node').click()
    expect(page.locator('#action-impact')).to_contain_text('Telemetry remains visible')
    page.keyboard.press('Escape')
    expect(page.locator('#lifecycle-state')).to_have_text('active')
    page.locator('#disable-node').click()
    page.locator('#confirm-action').click()
    expect(page.locator('#lifecycle-state')).to_have_text('disabled')
    expect(page.locator('#ram-headroom')).to_have_value('8192')
    page.locator('#retire-node').click()
    page.keyboard.press('Escape')
    expect(page.locator('#lifecycle-state')).to_have_text('disabled')
    page.locator('#retire-node').click()
    page.locator('#confirm-action').click()
    expect(page.locator('#ram-headroom')).to_be_disabled()
    expect(page.locator('#save')).to_be_disabled()
    page.locator('#enable-node').click()
    page.locator('#confirm-action').click()
    expect(page.locator('#ram-headroom')).to_be_enabled()
    expect(page.locator('#ram-headroom')).to_have_value('8192')
    expect(page.locator('#save-state')).to_contain_text('Unsaved changes')


def test_validation_and_storage_failure_keep_draft(preview):
    page = preview
    page.locator('#rotation-days').fill('3651')
    expect(page.locator('#save')).to_be_disabled()
    expect(page.locator('#save-state')).to_contain_text('Check')
    page.locator('#rotation-days').fill('60')
    expect(page.locator('#save')).to_be_enabled()
    page.evaluate("() => { Storage.prototype.setItem = () => { throw new Error('blocked'); }; }")
    page.locator('#save').click()
    expect(page.locator('#feedback')).to_contain_text('could not save')
    expect(page.locator('#save-state')).to_contain_text('Unsaved changes')
    page.locator('#discard').click()
    expect(page.locator('#rotation-days')).to_have_value('30')
