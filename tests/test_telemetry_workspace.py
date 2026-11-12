"""Live Telemetry uses physical-host data, retained history and permission-scoped controls."""

import copy
import json
import re
import time
from pathlib import Path

import pytest

from test_weightroom import desk, desk_with_machine, sign_in  # noqa: F401
from weightroom.app import chart
from weightroom.telemetry_workspace import component_pills, fleet_summary, history_groups, hosts_view

ROOT = Path(__file__).resolve().parents[1]
HOST = 'a' * 64


@pytest.fixture
def fleet(monkeypatch):
    from turnstile.machines import Machines
    gpu = lambda slot: dict(card=slot, model=f'GPU card {slot}', memory_used=(slot + 1) * 2**30,
                            memory_total=8 * 2**30, load=0 if slot == 0 else 90, temperature=40 + slot * 20,
                            power=20 + slot * 40, power_limit=200, fan=None if slot == 2 else 0)
    snapshot = dict(cards=[gpu(0), gpu(2)], processor={'load': 35, 'temperature': 65}, up_for=90061,
                    services=[{'name': name, 'unit': name + '.service', 'state': state, 'installed': installed}
                              for name, state, installed in [('loadcoach', 'active', True), ('voice', 'inactive', True),
                                                             ('spotter', 'failed', True), ('missing', 'inactive', False)]],
                    memory={'used': 12 * 2**30, 'total': 32 * 2**30},
                    disks=[{'path': '/models', 'free': 60 * 2**30, 'total': 100 * 2**30}])
    base = dict(fingerprint=HOST, hostname='atlas', machines=['compute', 'front_cpu'], state='ready',
                age=1, telemetry=snapshot, loaded=[], models_unknown=False, disabled_nodes=[], wake_nodes=[])
    rows = [base, {**copy.deepcopy(base), 'fingerprint': 'b' * 64, 'hostname': 'offline', 'machines': ['remote'],
                   'state': 'not answering', 'age': 600},
            {**copy.deepcopy(base), 'fingerprint': 'c' * 64, 'hostname': 'cpu-only', 'machines': ['front'],
             'telemetry': {**snapshot, 'cards': [], 'processor': {'load': 10, 'temperature': None}}}]
    calls = []

    def history(self, fingerprint, end, seconds, points):
        calls.append((fingerprint, end, seconds, points))
        values = {'processor load': [0, 20, None, 60, 100], 'processor temperature': [40, 50, None, 70, 95],
                  'memory used': [n * 2**30 if n is not None else None for n in [1, 2, None, 10, 12]],
                  'free on /models': [n * 2**30 for n in [100, 90, 80, 70, 60]]}
        for slot in (0, 2):
            values.update({f'GPU {slot} load': [0, None, 50, None, 100],
                           f'GPU {slot} memory used': [n * 2**30 for n in [1, 2, 3, 5, 7]],
                           f'GPU {slot} temperature': [40, 50, None, 70, 90],
                           f'GPU {slot} power': [5, 20, None, 100, 180]})
        return {'begin': end - seconds, 'end': end, 'step': seconds / 5, 'series': values}

    monkeypatch.setattr(Machines, 'hosts', lambda self: copy.deepcopy(rows))
    monkeypatch.setattr(Machines, 'history', history)
    return rows, calls


@pytest.fixture
def telemetry_server(fleet, desk):
    server, _, users = desk
    sign_in(server, 'pat')
    return server, fleet, users


def payload(response):
    return json.loads(re.search(r'<script id="telemetry-history" type="application/json">(.*?)</script>', response.text, re.S)[1])


def test_retired_machine_page_and_chart_asset_are_gone(telemetry_server):
    server, _, _ = telemetry_server
    for path in ('/machine', '/machine?part=power', '/static/charts.js'):
        assert server.get(path).status_code == 404, path
    assert server.get('/telemetry').status_code == 200


def test_chart_samples_keep_gaps_units_and_temperature_thresholds():
    temperature = chart('GPU 0 temperature', [40, None, 50, 70])['told']
    assert temperature == {'values': [40, None, 50, 70], 'unit': '°', 'top': None, 'heat': [45, 85]}
    assert chart('GPU 0 load', [None, None])['told'] is None
    assert chart('processor temperature', [50])['told']['heat'] == [45, 90]
    assert chart('memory used', [2**30], top=2**31)['told'] == {
        'values': [1.0], 'unit': ' GB', 'top': 2.0, 'heat': [0, 2.0]}
    assert chart('free on /', [2**30], top=2**32)['told']['heat'] == [4.0, 0]
    assert chart('GPU 1 power', [50])['told']['heat'] is None
    assert chart('GPU 1 power', [50], top=180)['told']['heat'] == [0, 180]


@pytest.mark.parametrize('zone,hour', [('America/Vancouver', '05:00'), ('Asia/Tokyo', '21:00')])
def test_live_telemetry_chart_uses_browser_zone_and_utc_tooltips(telemetry_server, zone, hour):
    from datetime import datetime
    from front_helpers import serve_browser_request
    from test_console_browser import playwright
    server, _, _ = telemetry_server
    instant = datetime.fromisoformat('2026-10-30T12:00:00+00:00').timestamp() * 1000
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        context = browser.new_context(timezone_id=zone, locale='en-GB')
        context.route('http://console.test/**', lambda route: serve_browser_request(server, 'http://console.test', route))
        page = context.new_page()
        page.goto('http://console.test/telemetry/' + HOST)
        tooltip = page.evaluate('''at => {
            const chart = echarts.getInstanceByDom(document.querySelector('#graph-grid .tw-plot'));
            return chart.getOption().tooltip[0].formatter([{value:[at,5], seriesName:'Processor load'}]);
        }''', instant)
        assert hour in tooltip and '12:00' in tooltip and 'UTC' in tooltip and '5%' in tooltip
        assert page.locator('#chart-context').inner_text() == zone
        browser.close()


@pytest.fixture
def component_reports(monkeypatch):
    from loadcoach import components

    async def inventory(config=None, running_here=(), **kwargs):
        front = 'turnstile' in running_here
        services = ([{'id': 'turnstile', 'installed': True, 'state': 'running'}] if front else
                    [{'id': identity, 'installed': True, 'state': state} for identity, state in
                     [('loadcoach', 'running'), ('voice', 'stopped'), ('spotter', 'failed'), ('docker', 'running')]])
        return {'hostname': 'atlas', 'receipt': {}, 'services': services,
                'engines': [] if front else [
                    {'id': 'llama_cpp', 'installed': True, 'available': True, 'enabled': True},
                    {'id': 'strata', 'installed': True, 'available': False, 'enabled': False},
                    {'id': 'audiocpp', 'installed': False, 'available': False, 'enabled': False}]}

    monkeypatch.setattr(components, 'here', inventory)


@pytest.fixture
def control_server(fleet, desk_with_machine, monkeypatch, component_reports):
    monkeypatch.setattr('loadcoach.machine_identity.fingerprint', lambda: HOST)
    rows, _ = fleet
    rows[0]['loaded'] = [{'model': 'test-model', 'machine': 'compute', 'state': 'ready'}]
    server, compute, machine, users, asked = desk_with_machine
    users.change('jordan', own={'add': ['front-wake']})
    sign_in(server, 'jordan')
    return server, compute, machine, asked


def test_component_pills_use_only_this_hosts_installed_components():
    host = {'machines': ['compute', 'cpu']}
    services = [{'id': 'loadcoach', 'installed': True, 'state': 'running'},
                {'id': 'docker', 'installed': True, 'state': 'running'},
                {'id': 'voice', 'installed': False, 'state': 'stopped'}]
    report = {'nodes': [
        {'name': 'front', 'answering': True, 'services': [{'id': 'turnstile', 'installed': True, 'state': 'running'}]},
        {'name': 'compute', 'answering': True, 'with_front': True, 'services': services, 'engines': [
            {'id': 'llama_cpp', 'installed': True, 'available': True, 'enabled': True},
            {'id': 'strata', 'installed': False, 'available': False, 'enabled': False}]},
        {'name': 'cpu', 'answering': True, 'services': services},
        {'name': 'other-host', 'answering': True, 'services': [{'id': 'voice', 'installed': True, 'state': 'running'}]}]}
    pills = component_pills(host, report)
    assert [(row['id'], row['label']) for row in pills] == [
        ('loadcoach', 'Running'), ('llama_cpp', 'Ready'), ('turnstile', 'Running')]
    report['nodes'][2]['services'] = [{'id': 'loadcoach', 'installed': True, 'state': 'stopped'}]
    names = [row['name'] for row in component_pills(host, report) if row['id'] == 'loadcoach']
    assert names == ['loadcoach at compute', 'loadcoach at cpu']
    assert component_pills({'machines': ['offline']}, {'nodes': [{'name': 'offline', 'answering': False}]}) is None


def test_host_controls_and_models_are_in_the_inventory_column(control_server):
    server, _, _, _ = control_server
    response = server.get('/telemetry/' + HOST + '?hours=6&gpu=2')
    assert response.status_code == 200
    column = response.text.split('<aside class="tw-inventory"', 1)[1].split('</aside>', 1)[0]
    assert column.index('test-model') < column.index('>Device capacity</h2>')
    assert 'Wake compute' in column
    assert '>Power</h2>' not in column and '/machine/power/' not in column
    assert '>Sleep</button>' not in column and 'id="force"' not in column
    assert column.rsplit('</form>', 1)[1].strip() == '</div>'
    assert 'Restart loadcoach' not in response.text and 'Service controls' not in response.text
    assert '/machine/services/' not in response.text
    assert 'Services and power' not in response.text and 'Compute service and power controls' not in response.text
    assert f'name="return_to" value="/telemetry/{HOST}"' in column
    sign_in(server, 'pat')
    restricted = server.get('/telemetry/' + HOST).text
    assert 'test-model' in restricted and 'Restart loadcoach' not in restricted and '>Sleep</button>' not in restricted
    assert 'Component status unavailable' in restricted and 'id="host-uptime"' in restricted


def test_host_actions_return_to_telemetry_and_refuse_another_physical_host(control_server):
    server, _, _, asked = control_server
    destination = '/telemetry/' + HOST + '?hours=6&gpu=2'
    done = server.post('/machine/services/loadcoach/restart', data={'return_to': destination}, follow_redirects=False)
    assert done.status_code == 303 and done.headers['location'].startswith(destination + '&said=')
    assert '--user restart loadcoach.service' in asked.read_text()
    before = asked.read_text().count('--user restart loadcoach.service')
    refused = server.post('/machine/services/loadcoach/restart',
                          data={'return_to': '/telemetry/' + 'b' * 64}, follow_redirects=False)
    assert refused.status_code == 400 and 'could not verify this physical host' in refused.text
    assert asked.read_text().count('--user restart loadcoach.service') == before
    sign_in(server, 'pat')
    denied = server.post('/machine/services/loadcoach/restart', data={'return_to': destination})
    assert denied.status_code == 400 and 'may not' in denied.text and 'id="telemetry-workspace"' in denied.text
    assert asked.read_text().count('--user restart loadcoach.service') == before


def test_host_power_and_spotter_failure_stay_on_the_selected_host(control_server):
    server, compute, machine, asked = control_server
    destination = '/telemetry/' + HOST
    wrong_host = server.post('/machine/power/off', data={'return_to': '/telemetry/' + 'b' * 64}, follow_redirects=False)
    assert wrong_host.status_code == 400 and 'could not verify this physical host' in wrong_host.text
    assert 'poweroff' not in asked.read_text()
    compute.off = True
    assert '/machine/power/' not in server.get(destination).text
    result = server.post('/machine/power/sleep', data={'return_to': destination, 'force': 'on'}, follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'].startswith(destination + '?said=')
    machine.off = True
    unavailable = server.get(destination)
    assert unavailable.status_code == 200 and 'Spotter is not answering' not in unavailable.text
    assert '/machine/power/' not in unavailable.text and 'Wake compute' in unavailable.text
    assert 'Restart loadcoach' not in unavailable.text and 'test-model' in unavailable.text


def test_host_inventory_and_wake_fit_narrow_layouts(control_server):
    from test_console_browser import browser_context, playwright
    server, _, _, _ = control_server
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/telemetry/' + HOST)
        column = page.locator('.tw-inventory')
        playwright.expect(column.get_by_role('button', name='Wake compute', exact=True)).to_be_visible()
        assert column.locator('form[action^="/machine/power/"]').count() == 0
        assert column.get_by_role('heading', name='Power', exact=True).count() == 0
        assert column.locator('p').filter(has_text='logged in').count() == 0
        playwright.expect(column.locator('[data-component=llama_cpp]')).to_have_text('llama.cpp · Ready')
        assert page.get_by_role('button', name='Restart loadcoach', exact=True).count() == 0
        assert column.locator('#host-uptime').count() == 0
        positions = [page.locator(selector).bounding_box() for selector in ('#host-title', '#host-subtitle', '#host-uptime')]
        assert max(box['y'] for box in positions) < min(box['y'] + box['height'] for box in positions)
        assert column.locator('.host-models').bounding_box()['y'] < column.get_by_role('heading', name='Device capacity').bounding_box()['y']
        for width in (1440, 1024, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            memory = page.locator('#host-totals .tw-total').nth(2)
            playwright.expect(memory.locator('span')).to_have_text(re.compile(r'^Memory used of .+ GB$'))
            assert memory.evaluate("""element => {
                const label = element.querySelector('span');
                const value = element.querySelector('strong');
                const text = document.createRange();
                text.selectNodeContents(label);
                const left = label.getBoundingClientRect(), right = value.getBoundingClientRect();
                return text.getClientRects().length === 1 && left.right <= right.left &&
                    left.top < right.bottom && right.top < left.bottom;
            }"""), width
        browser.close()


def test_inventory_freshness_zero_and_unknown_are_distinct(fleet):
    rows, _ = fleet
    views = hosts_view(rows, 10)
    online = next(host for host in views if host['hostname'] == 'atlas')
    offline = next(host for host in views if host['hostname'] == 'offline')
    assert [gpu['card'] for gpu in online['gpus']] == [0, 2]
    assert online['gpus'][0]['load'] == 0
    assert online['uptime'] == 90061
    assert offline['uptime'] is None
    assert offline['cpu'] is None and offline['gpus'][0]['memory_used'] is None
    assert offline['gpus'][0]['memory_total'] == 8 * 2**30
    summary = fleet_summary(views)
    assert summary == dict(total=3, reporting=2, gpus=4, vram=32 * 2**30, partial=False, attention=1)
    assert rows[1]['telemetry']['cards'][0]['memory_used'] == 2**30  # presentation never mutates cached telemetry
    stale = hosts_view([{**rows[0], 'age': 31}], 10)[0]
    assert not stale['fresh'] and stale['label'] == 'Stale' and stale['cpu'] is None


@pytest.mark.parametrize('uptime', [None, -1, float('nan'), float('inf'), '90061'])
def test_unknown_uptime_stays_unknown(fleet, uptime):
    rows, _ = fleet
    rows[0]['telemetry']['up_for'] = uptime
    host = hosts_view(rows, 10)[0]
    assert host['uptime'] is None


def test_telemetry_renders_components_inventory_and_header_uptime(telemetry_server, component_reports):
    server, _, _ = telemetry_server
    sign_in(server, 'jordan')
    detail = server.get('/telemetry/' + HOST).text
    header = detail.split('id="host-view"', 1)[1].split('id="host-totals"', 1)[0]
    assert 'id="host-uptime-value">1d 1h 1m</span>' in header
    assert 'loadcoach · Running' in detail and 'voice · Stopped' in detail and 'spotter · Failed' in detail
    assert 'llama.cpp · Ready' in detail and 'Strata · Turned off' in detail
    assert 'data-component="docker"' not in detail and 'data-component="audiocpp"' not in detail
    assert 'data-service="missing"' not in detail and 'Restart loadcoach' not in detail
    offline = server.get('/telemetry/' + 'b' * 64).text
    assert 'id="host-uptime-value">Unknown</span>' in offline and 'Component status unavailable' in offline


def test_history_only_devices_and_heat_thresholds(fleet):
    rows, _ = fleet
    host = hosts_view([rows[0]], 10)[0]
    groups = history_groups(host, {'series': {'GPU 7 load': [0, None, 100], 'GPU 0 temperature': [40, 80],
                                                    'free on /models': [90 * 2**30, None, 20 * 2**30]}}, chart)
    assert [gpu['card'] for gpu in host['gpus']] == [0, 2, 7]
    gpu7 = next(group for group in groups if group['id'] == 'gpu-7')['charts'][1]
    assert gpu7['told']['values'] == [0, None, 100] and gpu7['told']['heat'] == [0, 100]
    temp = next(group for group in groups if group['id'] == 'gpu-0')['charts'][2]
    assert temp['told']['heat'] == [45, 85]
    storage = next(group for group in groups if group['id'] == 'storage')['charts'][0]
    assert storage['told']['values'] == [10, None, 80] and storage['told']['heat'] == [0, 100]


def test_routes_load_only_selected_history_and_preserve_permissions(telemetry_server):
    server, (rows, calls), _ = telemetry_server
    overview = server.get('/telemetry')
    assert overview.status_code == 200 and not calls
    assert 'id="host-grid"' in overview.text and 'id="site-sidebar"' in overview.text
    assert overview.text.count('data-device=') == 4
    detail = server.get('/telemetry/' + HOST + '?gpu=2&hours=6')
    assert detail.status_code == 200
    data = payload(detail)
    assert data['initial_group'] == 'gpu-2' and calls[-1][0] == HOST and calls[-1][2] == 21600
    assert 'Wake compute' not in detail.text
    assert 'id="history-range"' in detail.text and 'data-hold-refresh' in detail.text
    fragment = server.get('/telemetry/' + HOST + '?part=workspace&hours=6', headers={'HX-Request': 'true'})
    assert '<html' not in fragment.text and 'id="telemetry-workspace"' in fragment.text
    assert server.get('/telemetry/' + 'f' * 64).status_code == 404
    sign_in(server, 'wanda')
    assert 'Wake compute' in server.get('/telemetry/' + HOST).text
    # Reads only: no route invoked a power action.
    assert rows[0]['state'] == 'ready'


@pytest.mark.parametrize('hours,expected', [('nan', 3600), ('inf', 3600), ('10000', 672 * 3600), ('-10', 60)])
def test_history_ranges_are_finite_and_bounded(telemetry_server, hours, expected):
    server, (_, calls), _ = telemetry_server
    response = server.get('/telemetry/' + HOST + '?hours=' + hours)
    assert response.status_code == 200 and calls[-1][2] == expected


def test_live_browser_tabs_coloring_refresh_and_navigation(telemetry_server, tmp_path):
    from test_console_browser import browser_context, playwright
    server, _, _ = telemetry_server
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/telemetry')
        playwright.expect(page.locator('#host-grid [data-device]')).to_have_count(4)
        page.screenshot(path=str(tmp_path / 'telemetry-overview.png'), full_page=True, animations='disabled')
        before = page.locator('#fleet-totals').inner_text()
        page.get_by_role('link', name='Inspect atlas GPU 2', exact=True).click()
        playwright.expect(page.locator('#host-title')).to_have_text('atlas')
        playwright.expect(page.get_by_role('tab', name='GPU 2', exact=True)).to_have_attribute('aria-selected', 'true')
        assert page.locator('#fleet-totals').inner_text() == before
        page.get_by_role('button', name='Load', exact=True).click()
        plot = page.locator('#graph-grid .tw-plot')
        config = plot.evaluate('el => {const o=echarts.getInstanceByDom(el).getOption(); return {heat:o.visualMap[0],series:o.series[0]};}')
        assert config['heat']['min'] == 0 and config['heat']['max'] == 100
        assert config['heat']['inRange']['color'] == ['hsl(120,60%,42%)', 'hsl(90,60%,42%)', 'hsl(60,60%,42%)', 'hsl(30,60%,42%)', 'hsl(0,60%,42%)']
        assert config['series']['data'][1][1] is None
        # Sparse history must draw one continuous polyline, not three isolated dots.
        commands = plot.evaluate('''el => echarts.getInstanceByDom(el).getZr().storage.getDisplayList()
            .filter(shape => shape.type === 'ec-polyline')
            .map(shape => Array.from(shape.getUpdatedPathProxy().data).filter((_, i) => i % 3 === 0))''')
        assert commands == [[1, 2, 2]]  # move to first reading, line to each subsequent reading
        page.screenshot(path=str(tmp_path / 'telemetry-heat.png'), full_page=True, animations='disabled')
        plot.evaluate("el => echarts.getInstanceByDom(el).dispatchAction({type:'dataZoom', start:25, end:75})")
        page.get_by_role('button', name='Refresh', exact=True).click()
        playwright.expect(page.get_by_role('button', name='Load', exact=True)).to_have_attribute('aria-pressed', 'true')
        page.wait_for_function("echarts.getInstanceByDom(document.querySelector('#graph-grid .tw-plot')).getOption().dataZoom[0].start === 25")
        page.get_by_role('button', name='Expand Load', exact=True).click()
        playwright.expect(page.locator('#graph-dialog')).to_be_visible()
        assert page.locator('#expanded-chart').evaluate('e => echarts.getInstanceByDom(e).getOption().visualMap[0].max') == 100
        page.keyboard.press('Escape')
        page.get_by_role('tab', name='Compare GPUs', exact=True).click()
        assert plot.evaluate('e => echarts.getInstanceByDom(e).getOption().series.map(s => s.name)') == ['GPU 0', 'GPU 2']
        page.locator('#history-preset').select_option('24')
        playwright.expect(page.locator('#hours')).to_have_value('24.0')
        page.get_by_role('tab', name='GPU 2', exact=True).click()
        page.get_by_role('button', name='Fan', exact=True).click()
        playwright.expect(page.locator('[data-no-history]')).to_be_visible()
        page.locator('#host-list a').filter(has_text='cpu-only').click()
        playwright.expect(page.locator('#host-title')).to_have_text('cpu-only')
        page.get_by_role('tab', name='Processor & memory', exact=True).click()
        page.get_by_role('button', name='Processor temperature', exact=True).click()
        playwright.expect(page.locator('[data-current]')).to_have_text('—')
        page.locator('#host-list a').filter(has_text='offline').click()
        playwright.expect(page.locator('#host-totals')).to_contain_text('—')
        for width in (1600, 1440, 900, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        assert not errors
        browser.close()


def test_polling_preserves_unsent_time_range(telemetry_server):
    from test_console_browser import browser_context, playwright
    server, _, _ = telemetry_server
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/telemetry/' + HOST)
        page.get_by_role('button', name='Refresh', exact=True).click()
        playwright.expect(page.locator('#host-search, #host-filter, #host-sort')).to_have_count(0)
        playwright.expect(page.locator('#host-list [data-host]:visible')).to_have_count(3)
        page.locator('#hours').fill('12')
        page.locator('#host-title').click()
        page.evaluate("document.querySelector('#telemetry-workspace').dataset.retained = 'yes'; htmx.trigger('#telemetry-workspace','htmx:poll:trigger')")
        # Exercise the same before-request contract as a timer, with no artificial sleeps.
        assert page.evaluate('''() => {
          const root=document.querySelector('#telemetry-workspace');
          const event=new CustomEvent('htmx:beforeRequest',{cancelable:true,bubbles:true,detail:{target:root,requestConfig:{elt:root,verb:'get'}}});
          root.dispatchEvent(event); return event.defaultPrevented;
        }''')
        playwright.expect(page.locator('#hours')).to_have_value('12')
        browser.close()


def test_live_refresh_failure_preserves_snapshot_and_recovers(telemetry_server):
    from test_console_browser import browser_context, playwright
    server, _, _ = telemetry_server
    failure = [False]
    def capture(route, path):
        if failure[0] and path.startswith('/telemetry/') and 'part=workspace' in path:
            route.fulfill(status=503, content_type='text/plain', body='Temporarily unavailable')
            return True
        return False
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        page.goto('http://console.test/telemetry/' + HOST)
        before = page.locator('#host-totals').inner_text()
        failure[0] = True
        page.get_by_role('button', name='Refresh', exact=True).click()
        playwright.expect(page.locator('#telemetry-refresh-error')).to_be_visible()
        assert page.locator('#host-totals').inner_text() == before
        failure[0] = False
        page.get_by_role('button', name='Refresh', exact=True).click()
        playwright.expect(page.locator('#telemetry-refresh-error')).to_be_hidden()
        assert page.locator('#host-totals').inner_text() == before
        browser.close()


def test_empty_unknown_and_large_fleets(telemetry_server):
    from test_console_browser import browser_context, playwright
    server, (rows, _), _ = telemetry_server
    original = copy.deepcopy(rows[0])
    rows.clear()
    assert 'No hosts registered' in server.get('/telemetry').text
    rows.append({**copy.deepcopy(original), 'fingerprint': None, 'hostname': '<unknown>', 'state': 'unknown', 'age': None, 'telemetry': None})
    unknown = hosts_view(rows, 10)[0]
    response = server.get(unknown['href'])
    assert response.status_code == 200 and '&lt;unknown&gt;' in response.text
    assert 'Device inventory unavailable' in response.text and 'Some inventory unavailable' in response.text
    rows[:] = [{**copy.deepcopy(original), 'fingerprint': f'{index:064x}', 'hostname': f'worker-{index:02}'} for index in range(48)]
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/telemetry')
        playwright.expect(page.locator('#host-grid [data-device]')).to_have_count(96)
        assert page.locator('#host-grid [data-device]').evaluate_all('rows => new Set(rows.map(row => row.dataset.device)).size') == 96
        page.set_viewport_size({'width':390,'height':1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
