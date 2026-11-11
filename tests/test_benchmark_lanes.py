"""Live benchmark setup preserves node scope, drafts, evidence and batch outcomes."""

import json

import pytest
from html.parser import HTMLParser
from starlette.responses import JSONResponse
from starlette.routing import request_response

from loadcoach.models import Models
from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.benchmark_views import run_lanes, history_page


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def with_attribute(self, name):
        return [attrs for _, attrs in self.tags if name in attrs]


@pytest.fixture
def fleet(desk, tmp_path, monkeypatch):
    server, switch, users = desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    for name in ('local-model', 'private-model'):
        library.put(name, {'flags': {'model': str(tmp_path / (name + '.gguf')), 'ctx-size': 8192}})
    state = {'detail_error': False, 'empty': False}
    route = next(route for route in server.app.state.benchmarks.routes if route.path == '/benchmarks' and 'GET' in route.methods)
    original = route.endpoint

    async def listing(request):
        answer = await original(request)
        if answer.status_code != 200:
            return answer
        data = json.loads(answer.body)
        data['catalogue']['available_machines'] = ['compute', 'rig', 'empty']
        data['catalogue']['unavailable_machines'] = ['offline']
        for model in data['catalogue']['data']:
            model['locations'] = [{**model, 'machine': machine} for machine in ('compute', 'rig')]
        if state['empty']:
            data['catalogue']['data'] = []
        return JSONResponse(data)

    monkeypatch.setattr(route, 'app', request_response(listing))
    inventory_route = next(route for route in switch.front.routes if route.path == '/turnstile/machines')
    original_inventory = inventory_route.endpoint

    async def inventory(request):
        response = await original_inventory(request)
        if response.status_code != 200:
            return response
        return JSONResponse({'machines': [{'name': name, 'accepting_work': name != 'disabled', 'state': 'ready'}
                                          for name in ('compute', 'rig', 'empty', 'offline', 'disabled')],
                             'models': [{'id': 'monitor-only-private-model'}]})

    monkeypatch.setattr(inventory_route, 'app', request_response(inventory))
    detail_route = next(route for route in server.app.state.benchmarks.routes if route.path == '/benchmarks/{number}')
    original_detail = detail_route.endpoint

    async def detail(request):
        return JSONResponse({'error': {'message': 'Temporarily unavailable'}}, status_code=503) if state['detail_error'] else await original_detail(request)

    monkeypatch.setattr(detail_route, 'app', request_response(detail))
    sign_in(server, 'jordan')
    return server, library, users, state


def test_inventory_keeps_empty_offline_disabled_and_front_nodes_without_leaking_models(fleet):
    server, _, users, state = fleet
    users.change('pat', own={'models': ['local-model']})
    sign_in(server, 'pat')
    response = server.get('/benchmarks/runs?benchmark=extraction')
    soup = Tags(response.text)
    expected = {'front', 'compute', 'rig', 'empty', 'offline', 'disabled'}
    assert {node['data-benchmark-node'] for node in soup.with_attribute('data-benchmark-node')} == expected
    assert {node['data-machine-lane'] for node in soup.with_attribute('data-machine-lane')} == expected
    assert 'private-model' not in response.text and 'monitor-only' not in response.text
    assert {json.loads(box['value'])['machine'] for box in [attrs for tag, attrs in soup.tags if tag == 'input' and attrs.get('name') == 'run-target']} == {'compute', 'rig'}
    assert all('disabled' in box for box in [attrs for tag, attrs in soup.tags if tag == 'input' and attrs.get('name') == 'run-target'])
    state['empty'] = True
    empty = server.get('/benchmarks/runs').text
    assert 'No compatible models available here.' in empty
    assert len(Tags(empty).with_attribute('data-machine-lane')) == 6


def test_malformed_targets_never_reach_submission_or_fall_back_to_models(fleet, monkeypatch):
    server, _, _, _ = fleet
    route = next(route for route in server.app.state.benchmarks.routes if route.path == '/benchmarks' and 'POST' in route.methods)
    calls = []

    async def submit(request):
        calls.append(await request.json())
        return JSONResponse({'runs': []}, status_code=202)

    monkeypatch.setattr(route, 'app', request_response(submit))
    for invalid in ('invalid JSON', '[]', 'null', '{"model":"local-model","machine":4}'):
        response = server.post('/benchmarks/runs', data={'benchmark': 'load', 'run-target': [json.dumps({'model': 'local-model', 'machine': 'compute'}), invalid], 'run-model': 'local-model'})
        assert response.status_code == 400 and 'No runs were submitted.' in response.text
    assert calls == []


def test_page_reports_partial_deduplicated_and_pending_batches(fleet, monkeypatch):
    server, _, _, _ = fleet
    route = next(route for route in server.app.state.benchmarks.routes if route.path == '/benchmarks' and 'POST' in route.methods)
    result = {'runs': ['accepted'], 'errors': [{'model': 'local-model', 'machine': 'rig', 'message': 'Node refused'}]}

    async def submit(request):
        return JSONResponse(result, status_code=207)

    monkeypatch.setattr(route, 'app', request_response(submit))
    form = {'benchmark': 'load', 'run-target': json.dumps({'model': 'local-model', 'machine': 'compute'})}
    response = server.post('/benchmarks/runs', data=form)
    assert response.status_code == 200 and 'Queued 1 run.' in response.text and 'local-model at rig: Node refused' in response.text
    result.clear(); result.update({'runs': [], 'active': [{'run': 'accepted'}], 'pending': [{'run': 'pending'}]})
    response = server.post('/benchmarks/runs', data=form)
    assert 'Queued 0 runs.' in response.text and '1 already queued or running' in response.text and '1 task acknowledgements pending' in response.text


def test_live_detail_outage_keeps_running_summary_queue_and_setup(fleet):
    server, library, _, state = fleet
    store = server.app.state.benchmarks.state.store
    number = store.create('jordan', 'local-model', 'load', library.identity('local-model'), location='rig')
    store.activate(number, {'fingerprint': 'recorded', 'subject': {}, 'environment': {}})
    store.state(number, 'running')
    state['detail_error'] = True
    response = server.get('/benchmarks/runs')
    assert response.status_code == 200
    soup = Tags(response.text)
    active = response.text.split('id="benchmark-active"', 1)[1].split('id="benchmark-queue"', 1)[0]
    assert number in active and 'Live details could not be refreshed' in active
    assert not soup.with_attribute('data-queued-run')
    assert 'id="benchmark-choice"' in response.text and 'id="benchmark-history"' in response.text



def test_history_filters_and_lane_evidence_are_node_specific():
    catalogue = {'available_machines': ['one', 'two'], 'data': []}
    model = {'id': 'same', 'locations': [{'machine': node} for node in ('one', 'two')]}
    results = {('same', node, 'load'): {'id': node, 'score': score, 'current_fingerprint': True} for node, score in [('one', 1), ('two', 2)]}
    lanes = run_lanes(catalogue, {}, [model], {'id': 'load', 'enabled': True}, [], results, {}, True)
    assert [(lane['name'], lane['choices'][0]['result']['score']) for lane in lanes if lane['choices']] == [('one', 1), ('two', 2)]
    runs = [{'model': 'same', 'id': node, 'state': state, 'location': node, 'benchmark': 'load'}
            for node, state in [('one', 'completed'), ('two', 'failed')]]
    page = history_page(runs, {'history_node': 'two', 'outcome': 'failed', 'q': 'same'})
    assert page['total'] == 1 and page['all_total'] == 2 and page['items'][0]['id'] == 'two'


def test_browser_lanes_review_polling_filters_and_narrow_layouts(fleet, monkeypatch, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    from test_console_browser import browser_context
    server, _, _, _ = fleet
    route = next(route for route in server.app.state.benchmarks.routes if route.path == '/benchmarks' and 'POST' in route.methods)
    submitted = []

    async def submit(request):
        submitted.append(await request.json())
        return JSONResponse({'runs': ['sample-receipt']}, status_code=202)

    monkeypatch.setattr(route, 'app', request_response(submit))
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/runs?benchmark=load')
        for theme in ('light', 'dark'):
            page.evaluate('(theme) => { document.documentElement.dataset.bsTheme = theme; }', theme)
            for width in (1440, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
                for node in ('front', 'compute', 'rig', 'empty', 'offline', 'disabled'):
                    playwright.expect(page.locator(f'[data-machine-lane="{node}"]')).to_be_visible()
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                    return ids.length === new Set(ids).size;
                }''')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('[data-model-search]').fill('local')
        page.locator('[data-select-node="compute"]').check()
        playwright.expect(page.get_by_role('checkbox', name='Select local-model at compute', exact=True)).to_be_checked()
        playwright.expect(page.locator('[data-selected-count]')).to_have_text('1 run selected')
        page.select_option('#benchmark', 'extraction')
        playwright.expect(page.locator('#start-benchmark [name=benchmark]')).to_have_value('extraction')
        page.select_option('#benchmark', 'load')
        playwright.expect(page.locator('#start-benchmark [name=benchmark]')).to_have_value('load')
        playwright.expect(page.get_by_role('checkbox', name='Select local-model at compute', exact=True)).to_be_checked()
        page.locator('[data-queue-selected]').click()
        playwright.expect(page.locator('#benchmark-review')).to_be_visible()
        playwright.expect(page.locator('[data-review-rows] tr')).to_have_count(1)
        assert submitted == []
        page.keyboard.press('Escape')
        page.evaluate("window.priorHistory = document.querySelector('#benchmark-history')")
        page.wait_for_function("document.querySelector('#benchmark-history') !== window.priorHistory", timeout=7000)
        playwright.expect(page.get_by_role('checkbox', name='Select local-model at compute', exact=True)).to_be_checked()
        page.locator('#history-search').fill('not yet submitted')
        page.evaluate("window.priorHistory = document.querySelector('#benchmark-history')")
        page.wait_for_timeout(2300)
        assert page.evaluate("document.querySelector('#benchmark-history') === window.priorHistory")
        playwright.expect(page.locator('#history-search')).to_have_value('not yet submitted')
        page.locator('#benchmark-history button[type=submit]').click()
        playwright.expect(page.locator('#history-search')).to_have_value('not yet submitted')
        page.locator('[data-queue-selected]').click()
        page.locator('[data-confirm-batch]').click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Queued 1 run.')
        assert submitted == [{'benchmark': 'load', 'targets': [{'model': 'local-model', 'machine': 'compute'}]}]
        page.screenshot(path=str(tmp_path / 'machine-lanes.png'), full_page=True)
        assert not errors
        browser.close()


@pytest.mark.parametrize('model_count', (15, 30))
def test_run_studio_bounds_large_catalogues_and_reviews_hidden_targets(fleet, monkeypatch, tmp_path, model_count):
    playwright = pytest.importorskip('playwright.sync_api')
    from test_console_browser import browser_context
    server, library, _, _ = fleet
    for name in ('local-model', 'private-model'):
        library.remove(name)
    names = [f'studio-model-{index:02d}-a-long-name-for-a-quantized-language-model' for index in range(model_count)]
    for name in names:
        library.put(name, {'flags': {'model': str(tmp_path / (name + '.gguf')), 'ctx-size': 8192}})
    store = server.app.state.benchmarks.state.store
    for index in range(13):
        number = store.create('jordan', names[2], 'load', library.identity(names[2]), location='compute' if index == 0 else 'rig')
        store.activate(number, {'fingerprint': 'recorded', 'subject': {}, 'environment': {}})
        if index == 0:
            store.state(number, 'running')
    route = next(route for route in server.app.state.benchmarks.routes if route.path == '/benchmarks' and 'POST' in route.methods)
    submitted = []

    async def submit(request):
        submitted.append(await request.json())
        return JSONResponse({'runs': ['reviewed-batch']}, status_code=202)

    monkeypatch.setattr(route, 'app', request_response(submit))
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/runs?benchmark=load')
        playwright.expect(page.locator('[data-model-row]')).to_have_count(model_count)
        playwright.expect(page.locator('input[name="run-target"]')).to_have_count(model_count * 2)
        backgrounds = []
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            playwright.expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
            backgrounds.append(page.locator('html').evaluate('(el) => getComputedStyle(el).backgroundColor'))
            for width in (1440, 1024, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (model_count, theme, width)
                assert page.locator('#benchmark-models').evaluate('(el) => el.clientHeight <= 482 && el.scrollHeight > el.clientHeight')
                assert page.locator('#benchmark-queue-scroll').evaluate('(el) => el.clientHeight <= 422 && el.scrollHeight > el.clientHeight')
                assert page.locator('#benchmark-queue-scroll').evaluate('(el) => el.scrollWidth <= el.clientWidth')
                for node in ('front', 'compute', 'rig', 'empty', 'offline', 'disabled'):
                    playwright.expect(page.locator(f'[data-benchmark-node="{node}"]')).to_be_visible()
                    playwright.expect(page.locator(f'[data-select-node="{node}"]')).to_be_visible()
                if width == 1440:
                    setup = page.locator('#benchmark-choice').bounding_box()
                    monitor = page.locator('.benchmark-monitor').bounding_box()
                    assert setup['x'] + setup['width'] <= monitor['x'] + 1
                    assert abs(setup['y'] - monitor['y']) <= 1
                    assert page.locator('[data-model-row]').first.evaluate('(el) => el.getBoundingClientRect().height') <= 100
                page.screenshot(path=str(tmp_path / f'studio-{model_count}-{theme}-{width}.png'), full_page=True)
        assert backgrounds[0] != backgrounds[1]
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('#benchmark-models').evaluate('(el) => { el.scrollTop = 200; }')
        page.wait_for_function('''() => {
            const list = document.querySelector('#benchmark-models');
            return Math.abs(list.querySelector('thead').getBoundingClientRect().top - list.getBoundingClientRect().top) <= 1;
        }''')
        page.get_by_role('checkbox', name=f'Select {names[0]} at rig', exact=True).check()
        first = page.get_by_role('checkbox', name=f'Select eligible nodes for {names[0]}', exact=True)
        assert first.evaluate('(el) => el.indeterminate')
        first.check()
        playwright.expect(page.locator('[data-selected-count]')).to_have_text('2 runs selected')
        page.locator('[data-model-search]').fill('no matching model')
        playwright.expect(page.locator('[data-model-empty]')).to_be_visible()
        playwright.expect(page.locator('[data-select-models]')).to_be_disabled()
        playwright.expect(page.locator('[data-selection-note]')).to_contain_text('2 selected outside this search')
        page.locator('[data-model-search]').fill(names[-1])
        playwright.expect(page.locator('[data-model-count]')).to_have_text(f'1 of {model_count} models')
        playwright.expect(page.locator('[data-machine-lane="compute"] [data-node-count]')).to_have_text('1')
        page.locator('[data-select-models]').check()
        page.locator('[data-select-node="compute"]').uncheck()
        playwright.expect(page.locator('[data-selected-count]')).to_have_text('3 runs selected')
        page.locator('[data-queue-selected]').click()
        playwright.expect(page.locator('[data-review-rows] tr')).to_have_count(3)
        assert submitted == []
        page.locator('[data-confirm-batch]').click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Queued 1 run.')
        assert submitted == [{'benchmark': 'load', 'targets': [
            {'model': names[0], 'machine': 'compute'}, {'model': names[0], 'machine': 'rig'},
            {'model': names[-1], 'machine': 'rig'}]}]
        assert not errors
        browser.close()
