"""Live Telemetry uses physical-host data, retained history and permission-scoped controls."""

import copy
import json
import re
import time
from pathlib import Path

import pytest

from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.app import chart
from weightroom.telemetry_workspace import fleet_summary, history_groups, hosts_view

ROOT = Path(__file__).resolve().parents[1]
HOST = 'a' * 64


@pytest.fixture
def fleet(monkeypatch):
    from turnstile.machines import Machines
    gpu = lambda slot: dict(card=slot, model=f'GPU card {slot}', memory_used=(slot + 1) * 2**30,
                            memory_total=8 * 2**30, load=0 if slot == 0 else 90, temperature=40 + slot * 20,
                            power=20 + slot * 40, power_limit=200, fan=None if slot == 2 else 0)
    snapshot = dict(cards=[gpu(0), gpu(2)], processor={'load': 35, 'temperature': 65},
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
            values.update({f'GPU {slot} load': [0, 25, None, 50, 100],
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


def test_inventory_freshness_zero_and_unknown_are_distinct(fleet):
    rows, _ = fleet
    views = hosts_view(rows, 10)
    online = next(host for host in views if host['hostname'] == 'atlas')
    offline = next(host for host in views if host['hostname'] == 'offline')
    assert [gpu['card'] for gpu in online['gpus']] == [0, 2]
    assert online['gpus'][0]['load'] == 0
    assert offline['cpu'] is None and offline['gpus'][0]['memory_used'] is None
    assert offline['gpus'][0]['memory_total'] == 8 * 2**30
    summary = fleet_summary(views)
    assert summary == dict(total=3, reporting=2, gpus=4, vram=32 * 2**30, partial=False, attention=1)
    assert rows[1]['telemetry']['cards'][0]['memory_used'] == 2**30  # presentation never mutates cached telemetry
    stale = hosts_view([{**rows[0], 'age': 31}], 10)[0]
    assert not stale['fresh'] and stale['label'] == 'Stale' and stale['cpu'] is None


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
        assert config['series']['data'][2][1] is None
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


def test_polling_preserves_filters_and_unsent_time_range(telemetry_server):
    from test_console_browser import browser_context, playwright
    server, _, _ = telemetry_server
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/telemetry/' + HOST)
        page.locator('#host-search').fill('atlas')
        page.get_by_role('button', name='Refresh', exact=True).click()
        playwright.expect(page.locator('#host-search')).to_have_value('atlas')
        playwright.expect(page.locator('#host-list [data-host]:visible')).to_have_count(1)
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
