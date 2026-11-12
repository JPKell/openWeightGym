"""Live fleet matrix: complete topology, exact location evidence and bounded browsing."""

from pathlib import Path

import pytest
from starlette.responses import JSONResponse
from starlette.routing import request_response

from test_benchmark_lanes import fleet, Tags  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.benchmark_views import measurement_matrix


def test_measurements_keep_all_nodes_across_types_without_private_model_metadata(fleet):
    server, _, users, state = fleet
    users.change('pat', own={'models': ['local-model']})
    sign_in(server, 'pat')
    expected = ['front', 'compute', 'rig', 'empty', 'offline', 'disabled']
    for kind in ('llm', 'embedding', 'image', 'audio'):
        response = server.get('/benchmarks/node-measurements', params={'kind': kind, 'benchmark': 'load'})
        assert response.status_code == 200
        nodes = Tags(response.text).with_attribute('data-matrix-node')
        assert [node['data-node'] for node in nodes] == expected
        assert 'private-model' not in response.text and 'monitor-only' not in response.text
        assert 'Unreachable' in response.text and 'Disabled' in response.text
    state['empty'] = True
    response = server.get('/benchmarks/node-measurements')
    assert len(Tags(response.text).with_attribute('data-matrix-node')) == 6
    assert 'No local models are available to your account.' in response.text


def test_matrix_evidence_is_node_specific_and_preserves_missing_zero_failed_and_stale():
    nodes = ['one', 'two', 'offline', 'empty', 'disabled']
    catalogue = {'available_machines': ['one', 'two', 'empty'], 'unavailable_machines': ['offline']}
    inventory = {'machines': [{'name': node, 'accepting_work': node != 'disabled'} for node in nodes]}
    models = [{'id': 'same', 'locations': [{'machine': node, 'enabled': node != 'disabled'} for node in ['one', 'two', 'disabled']]}]
    bench = {'id': 'load', 'score_unit': '1/s', 'green': 'high', 'enabled': True}
    def run(node, seconds, score, **extra):
        return dict(id=node, model='same', benchmark='load', location=node, state='completed',
                    current_fingerprint=True, metrics={'load_seconds': seconds}, score=score, **extra)
    results = {('same', node, 'load'): run(node, seconds, score) for node, seconds, score in [('one', 2, .5), ('two', 8, .125)]}
    matrix = measurement_matrix(catalogue, inventory, models, [bench], bench, [], results)
    cells = {cell['node']: cell for cell in matrix['rows'][0]['cells']}
    assert cells['one']['value'] == 2 and cells['two']['value'] == 8
    assert cells['one']['best_value'] and cells['one']['best_score']
    assert cells['empty']['state'] == 'absent' and cells['offline']['state'] == 'unknown'
    assert cells['disabled']['state'] == 'missing'
    results[('same', 'two', 'load')].update(current_fingerprint=False, score=0)
    failed = dict(id='failure', model='same', benchmark='load', location='disabled', state='failed')
    matrix = measurement_matrix(catalogue, inventory, models, [bench], bench, [failed], results)
    cells = {cell['node']: cell for cell in matrix['rows'][0]['cells']}
    assert cells['two']['score'] == 0 and cells['two']['score_text'] == '0'
    assert cells['two']['state'] == 'stale' and not cells['two']['best_score']
    assert cells['disabled']['state'] == 'failed' and cells['disabled']['value'] is None
    # A successful saved run survives a newer failed attempt on the same node.
    failed.update(location='one')
    matrix = measurement_matrix(catalogue, inventory, models, [bench], bench, [failed], results)
    assert next(cell for cell in matrix['rows'][0]['cells'] if cell['node'] == 'one')['result']['id'] == 'one'


def test_legacy_results_never_migrate_to_another_node():
    bench = {'id': 'context', 'score_unit': 'tokens', 'enabled': True}
    saved = dict(id='legacy', model='same', benchmark='context', state='completed', score=4096,
                 metrics={'max_context_tokens': 4096}, current_fingerprint=True)
    explicit = [{'id': 'same', 'locations': [{'machine': 'rig'}]}]
    matrix = measurement_matrix({}, {}, explicit, [bench], bench, [saved], {})
    assert all(cell['value'] is None for cell in matrix['rows'][0]['cells'])
    implicit = measurement_matrix({}, {}, [{'id': 'same'}], [bench], bench, [saved], {})
    assert next(cell for cell in implicit['rows'][0]['cells'] if cell['node'] == 'compute')['value'] == 4096
    named = {**saved, 'location': 'rig'}
    other = measurement_matrix({}, {}, [{'id': 'same'}], [bench], bench, [named], {('same', 'rig', 'context'): named})
    assert next(cell for cell in other['rows'][0]['cells'] if cell['node'] == 'compute')['value'] is None


def test_inventory_outage_keeps_last_known_topology_and_evidence(fleet, desk, monkeypatch):
    server, _, _, _ = fleet
    server.get('/benchmarks/node-measurements')
    _, switch, _ = desk
    route = next(route for route in switch.front.routes if route.path == '/turnstile/machines')
    async def unavailable(request):
        return JSONResponse({'error': {'message': 'Unavailable'}}, status_code=503)
    monkeypatch.setattr(route, 'app', request_response(unavailable))
    response = server.get('/benchmarks/node-measurements')
    assert response.status_code == 200 and 'Node inventory could not be refreshed' in response.text
    assert len(Tags(response.text).with_attribute('data-matrix-node')) == 6


@pytest.mark.parametrize('count', (15, 30))
def test_browser_fleet_matrix_scale_filters_units_and_navigation(fleet, monkeypatch, tmp_path, count):
    playwright = pytest.importorskip('playwright.sync_api')
    from test_console_browser import browser_context
    server, library, _, _ = fleet
    library.remove('local-model'); library.remove('private-model')
    for index in range(count):
        name = f'matrix-model-{index:02d}-with-a-long-quantized-model-name'
        library.put(name, {'flags': {'model': str(tmp_path / f'{index}.gguf'), 'ctx-size': 8192}})
    store = server.app.state.benchmarks.state.store
    # Real retained runs; fingerprints are deliberately historical to test their display.
    name = 'matrix-model-00-with-a-long-quantized-model-name'
    for node, seconds in [('compute', 2), ('rig', 8)]:
        ref = store.create('jordan', name, 'load', library.identity(name), location=node)
        store.activate(ref, {'fingerprint': 'saved', 'subject': {}, 'environment': {}})
        store.keep(ref, 'load', {'success': True, 'context_tokens': 8192, 'load_seconds': seconds}, '', '')
        store.finish(ref, 'completed'); store.synced(ref)
    failed = store.create('jordan', 'matrix-model-01-with-a-long-quantized-model-name', 'load',
                          library.identity('matrix-model-01-with-a-long-quantized-model-name'), location='compute')
    store.finish(failed, 'failed')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/node-measurements?benchmark=load')
        playwright.expect(page.locator('[data-measurement-row]')).to_have_count(count)
        playwright.expect(page.locator('#benchmark-top .eyebrow')).to_have_count(0)
        playwright.expect(page.get_by_role('button', name='Export view')).to_have_count(0)
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            for width in (1440, 1024, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (count, theme, width)
                assert page.locator('.measurement-table-scroll').evaluate('(el) => el.clientHeight <= 602 && el.scrollHeight > el.clientHeight')
                for node in ('front', 'compute', 'rig', 'empty', 'offline', 'disabled'):
                    playwright.expect(page.locator(f'.measurement-node[data-node="{node}"]')).to_be_visible()
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('[data-matrix-search]').fill('matrix-model-00')
        playwright.expect(page.locator('[data-measurement-row]:visible')).to_have_count(1)
        cells = page.locator('[data-measurement-row]:visible td')
        playwright.expect(cells.nth(1).locator('[data-matrix-values]').first).to_have_text('2')
        playwright.expect(cells.nth(2).locator('[data-matrix-values]').first).to_have_text('8')
        page.locator('[data-matrix-display]').select_option('scores')
        playwright.expect(cells.nth(1).locator('[data-matrix-scores]').first).to_have_text('0.5')
        playwright.expect(cells.nth(2).locator('[data-matrix-scores]').first).to_have_text('0.125')
        page.reload()
        playwright.expect(page.locator('[data-matrix-search]')).to_have_value('matrix-model-00')
        playwright.expect(page.locator('[data-matrix-display]')).to_have_value('scores')
        page.get_by_role('link', name='Context', exact=True).click()
        playwright.expect(page.locator('[data-matrix-search]')).to_have_value('matrix-model-00')
        playwright.expect(page.locator('[data-matrix-display]')).to_have_value('scores')
        page.get_by_role('link', name='Load time', exact=True).click()
        playwright.expect(page.locator('[data-measurement-row]:visible')).to_have_count(1)
        page.locator('[data-matrix-search]').fill('no matching model')
        playwright.expect(page.locator('[data-matrix-empty]')).to_be_visible()
        page.locator('[data-matrix-search]').fill('')
        page.locator('[data-matrix-filter]').select_option('failed')
        playwright.expect(page.locator('[data-measurement-row]:visible')).to_have_count(1)
        page.locator('[data-matrix-filter]').select_option('stale')
        playwright.expect(page.locator('[data-measurement-row]:visible')).to_have_count(1)
        page.locator('[data-matrix-filter]').select_option('all')
        page.locator('[data-matrix-sort]').select_option('node-2')
        playwright.expect(page.locator('#measurement-column-2')).to_have_attribute('aria-sort', 'descending')
        page.locator('.measurement-node[data-node="disabled"]').click()
        playwright.expect(page.locator('#measurement-column-5 button')).to_be_focused()
        # Headers stay fixed while browsing the full model inventory.
        page.locator('.measurement-table-scroll').evaluate('(el) => { el.scrollTop = 200; el.scrollLeft = 250; }')
        assert page.locator('thead').evaluate('(el) => getComputedStyle(el).position') == 'sticky'
        assert page.locator('tbody th').first.evaluate('(el) => getComputedStyle(el).position') == 'sticky'
        images = Path('data/benchmark-previews'); images.mkdir(parents=True, exist_ok=True)
        page.goto('http://console.test/benchmarks/node-measurements?benchmark=load')
        page.emulate_media(color_scheme='light')
        page.screenshot(path=str(images / f'fleet-matrix-{count}.png'), full_page=True)
        assert not errors
        browser.close()
