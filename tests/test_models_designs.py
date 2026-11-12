"""Models inventory proposals preserve complete installations and accessible browsing."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ('catalogue', 'matrix', 'explorer')


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-models-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1600, 'height':1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        browser.close()
        assert not errors
        assert not network, 'Inventory proposals must never contact live services.'


def test_full_inventory_counts_and_node_exploration(preview):
    page = preview
    expect(page.locator('#total-models')).to_have_text('40')
    expect(page.locator('#total-placements')).to_have_text('135')
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(40)
    page.locator('.inventory-table-scroll').evaluate('(el) => el.scrollTop = el.scrollHeight')
    assert page.locator('.inventory-table-scroll').evaluate('(el) => el.scrollTop > 0')
    page.locator('[data-design="matrix"]').click()
    expect(page.locator('#inventory-head th')).to_have_count(8)
    expect(page.locator('#inventory-head')).to_contain_text('scout')
    page.locator('#inventory-head [data-node="atlas"]').click()
    expect(page.locator('#workspace')).to_have_attribute('data-layout', 'explorer')
    for node, count in [('atlas',30), ('orion',27), ('nova',24), ('cedar',21), ('vega',18), ('lyra',15)]:
        page.locator(f'#node-list [data-node="{node}"]').click()
        expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(count)
    page.locator('#node-list [data-node="scout"]').click()
    expect(page.locator('[data-empty-row]')).to_contain_text('No models on scout yet')


def test_search_filters_and_sort_persist_across_designs(preview):
    page = preview
    page.locator('#model-search').fill('Q4_K_M')
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(30)
    page.locator('#model-search').fill('Qwen3 8B')
    for design in DESIGNS:
        page.locator(f'[data-design="{design}"]').click()
        expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(1)
        expect(page.locator('#model-search')).to_have_value('Qwen3 8B')
    page.locator('[data-design="catalogue"]').click()
    page.locator('#model-search').fill('')
    page.locator('[data-kind="image"]').click()
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(4)
    page.locator('#sort').select_option('size')
    values = [float(size.split()[0]) for size in page.locator('#inventory-rows [data-column="disk"]').all_text_contents()]
    assert values == sorted(values)
    page.locator('#model-search').fill('no such model')
    expect(page.locator('[data-empty-row]')).to_contain_text('No matching models')
    page.locator('[data-clear]').click()
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(40)


def test_statuses_and_offline_inventory_remain_distinct(preview):
    page = preview
    page.locator('#status-filter').select_option('active')
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(5)
    page.locator('#status-filter').select_option('in-use')
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(1)
    page.locator('#status-filter').select_option('offline')
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(18)
    page.locator('#scenario').select_option('offline')
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(40)
    expect(page.locator('#connection-note')).to_be_visible()
    expect(page.locator('#total-active')).to_have_text('—')
    expect(page.locator('#active-note')).to_contain_text('unverified')
    expect(page.locator('#inventory-rows [data-state="loaded"]')).to_have_count(0)
    page.locator('#scenario').select_option('normal')
    page.locator('#status-filter').select_option('all')
    page.locator('[data-scope="scout"]').click()
    expect(page.locator('[data-empty-row]')).to_contain_text('No models on scout yet')


def test_details_name_node_configuration_and_return_focus(preview):
    page = preview
    page.locator('#model-search').fill('Qwen3 8B')
    chip = page.locator('#inventory-rows [data-placement="model-0/atlas"]')
    chip.click()
    expect(page.locator('#model-dialog')).to_be_visible()
    expect(page.locator('#detail-title')).to_have_text('Qwen3 8B')
    expect(page.locator('#detail-note')).to_contain_text('inspecting atlas')
    expect(page.locator('#detail-rows .is-focused')).to_contain_text('In use')
    expect(page.locator('#detail-rows .is-focused')).to_contain_text('4,096 / 32,768')
    expect(page.locator('#detail-facts')).to_contain_text('Fingerprint')
    page.keyboard.press('Escape')
    expect(page.locator('#model-dialog')).not_to_be_visible()
    expect(chip).to_be_focused()


@pytest.mark.parametrize('design', DESIGNS)
def test_layouts_themes_types_and_states_fit_narrow_screens(preview, design):
    page = preview
    page.locator(f'[data-design="{design}"]').click()
    for width in (1600, 768, 390, 320):
        page.set_viewport_size({'width':width, 'height':1100})
        for theme in ('dark','light'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for kind in ('all','image','audio'):
                page.locator(f'[data-kind="{kind}"]').click()
                for scenario in ('normal','empty','offline'):
                    page.locator('#scenario').select_option(scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (design,width,theme,kind,scenario)
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                        return ids.length === new Set(ids).size;
                    }''')
                    assert page.evaluate('document.documentElement.scrollHeight < 3000')
                    if scenario == 'empty':
                        expect(page.locator('[data-empty-row]')).to_contain_text('Your model library starts here')


def test_preview_addition_is_escaped_browsable_and_resets_on_reload(preview):
    page = preview
    page.locator('[data-design="explorer"]').click()
    page.locator('#node-list [data-node="scout"]').click()
    page.locator('#add-model').click()
    expect(page.locator('#add-node')).to_have_value('scout')
    name = '<img src=x onerror=alert(1)> test model'
    page.locator('#add-name').fill(name)
    page.locator('#add-form button[type="submit"]').click()
    expect(page.locator('#inventory-rows tr[data-id]')).to_have_count(1)
    expect(page.locator('#inventory-rows [data-name]')).to_have_text(name)
    expect(page.locator('#inventory-rows img')).to_have_count(0)
    expect(page.locator('#total-placements')).to_have_text('136')
    page.locator('#inventory-rows [data-model]').click()
    expect(page.locator('#detail-title')).to_have_text(name)
    expect(page.locator('#detail-facts')).to_contain_text('Not measured')
    page.reload()
    expect(page.locator('#total-placements')).to_have_text('135')
