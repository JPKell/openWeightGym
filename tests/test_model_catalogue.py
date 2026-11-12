"""The live catalogue summarizes node copies accurately and survives polling while browsing."""

import pytest

from loadcoach.models import Models
from weightroom.model_catalogue import catalogue
from test_console_browser import browser_context, playwright
from test_weightroom import desk, gguf, sign_in  # noqa: F401


def copy(node, **values):
    return {'name':'sample', 'machine':node, 'size':1024, 'flags':{'ctx-size':8192}, **values}


def test_inventory_uses_each_copy_state_context_and_local_name():
    copies = [copy('atlas', loaded=None), copy('orion', name='renamed/model', loaded='ready', running=2,
              flags={'ctx-size':32768}), copy('away', loaded='ready', unavailable=True),
              copy('missing', size=None), copy('disabled', enabled=False), copy('starting', loaded='loading')]
    result = catalogue({'models':[{'name':'logical', 'locations':copies}]})
    row = result['rows'][0]
    assert (result['installations'], result['nodes'], result['active'], result['in_use']) == (6,6,1,1)
    assert row['contexts'] == [8192,32768]
    assert [p['state'] for p in row['placements']] == ['installed','in-use','offline','missing','disabled','loading']
    assert row['placements'][1]['href'] == '/models/renamed/model?node=orion'
    assert result['offline'] == ['away']
    assert 'renamed/model' in row['search']


def test_inventory_fallback_uses_selected_node_and_ignores_stale_loaded_state():
    model = copy('unused', loaded='ready')
    result = catalogue({'models':[model], 'unavailable_machines':['rig & two']}, 'rig & two')
    assert result['active'] == 0
    assert result['rows'][0]['placements'][0]['href'] == '/models/sample?node=rig%20%26%20two'
    assert result['rows'][0]['placements'][0]['state'] == 'offline'
    assert catalogue({})['installations'] == 0


def test_disabled_placement_can_still_hold_a_model_in_memory():
    result = catalogue({'models':[{'name':'logical', 'locations':[copy('atlas',loaded='ready',enabled=False)]}]})
    assert result['active'] == 1
    assert result['rows'][0]['placements'][0]['state'] == 'loaded'
    assert result['rows'][0]['placements'][0]['disabled']
    assert 'placement disabled' in result['rows'][0]['placements'][0]['label']


@pytest.mark.parametrize('width', [1440,768,390])
def test_live_catalogue_keeps_filters_details_scroll_and_add_draft_on_refresh(desk, tmp_path, width):
    server, _, _ = desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    for index in range(30):
        file = gguf(tmp_path / f'model-{index:02d}.gguf', general__architecture='qwen',
                    general__name=f'Model {index}', qwen__context_length=8192)
        library.put(f'sample-{index:02d}', {'gpus':0, 'flags':{'model':file,'ctx-size':8192}, 'order':30-index})
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page = browser_context(engine,server)
        page.set_viewport_size({'width':width,'height':1000})
        errors = []
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/models')
        rows = page.locator('#models-table [data-catalogue-row]')
        playwright.expect(rows).to_have_count(30)
        playwright.expect(page.locator('#models-overview .kpi-value').nth(0)).to_have_text('30')
        assert page.locator('#models-table [data-column="placements"] a').count() == 30
        assert 'node=compute' in page.locator('#models-table [data-column="placements"] a').first.get_attribute('href')
        page.locator('#catalogue-sort').select_option('name')
        playwright.expect(rows.first).to_have_attribute('data-name','sample-00')
        page.locator('#catalogue-search').fill('sample-0')
        playwright.expect(page.locator('[data-catalogue-count]')).to_have_text('10 of 30 models')
        rows.first.locator('summary').click()
        playwright.expect(rows.first.locator('details')).to_have_attribute('open','')
        # Add remains the real file/engine/projector/context form, outside the inventory poll.
        page.locator('#file').select_option(index=1)
        page.locator('#name').fill('A draft that must survive')
        region = page.locator('#models .inventory-table-scroll')
        region.evaluate('(el) => el.scrollTop = 100')
        assert region.evaluate('(el) => el.scrollTop > 0')
        page.evaluate('htmx.ajax("GET","/models?part=list",{target:"#models",swap:"outerHTML"})')
        playwright.expect(page.locator('[data-catalogue-count]')).to_have_text('10 of 30 models')
        playwright.expect(page.locator('#catalogue-search')).to_have_value('sample-0')
        playwright.expect(page.locator('#catalogue-sort')).to_have_value('name')
        playwright.expect(page.locator('#name')).to_have_value('A draft that must survive')
        playwright.expect(rows.first.locator('details')).to_have_attribute('open','')
        assert region.evaluate('(el) => el.scrollTop > 0')
        page.locator('#catalogue-status').select_option('active')
        playwright.expect(page.locator('[data-catalogue-empty]')).to_be_visible()
        page.locator('[data-catalogue-clear]').click()
        playwright.expect(page.locator('[data-catalogue-count]')).to_have_text('30 of 30 models')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
