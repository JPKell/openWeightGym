"""Request-column ranges, catalog choices and authenticated client attribution."""

import asyncio
import json
from datetime import datetime, UTC

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from loadcoach.client_origin import ClientOrigin, HEADER, forward
from loadcoach.groups import Group
from loadcoach.log import Log, public_number
from loadcoach.request_filters import parse
from loadcoach.users import Users
from turnstile.accesses import ClientAccesses
from test_weightroom import desk, sign_in  # noqa: F401
from front_helpers import door
from loadcoach import statement


@pytest.mark.parametrize('params', [
    {'request_min': '2', 'request_max': '1'}, {'request_min': '1.2'},
    {'request_max': str(1 << 52)}, {'duration_min': 'nan'}, {'duration_max': 'inf'},
    {'duration_min': '-1'}, {'duration_min': '5', 'duration_max': '4'},
    {'date_min': 'bad'}, {'date_min': '2026-10-20', 'date_max': '2026-10-19'},
    {'ip': '198.51.100.1, 127.0.0.1'}, {'status': 'invented'}, {'timezone': 'invalid/zone'},
])
def test_ranges_reject_invalid_values(params):
    with pytest.raises(ValueError):
        parse(params)


def test_request_ranges_use_public_ids_and_completed_durations(tmp_path):
    log = Log(str(tmp_path / 'events.db'))
    one = log.add('request', 'asked', 'alice', '198.51.100.1', 'first')
    two = log.add('request', 'asked', 'bob', '198.51.100.2', 'second')
    log.add('request', 'done', 'alice', '198.51.100.1', 'first', of=one)
    with log.db:
        log.db.execute('UPDATE events SET at=100 WHERE id=?', (one,))
        log.db.execute('UPDATE events SET at=103 WHERE kind=? AND what=?', ('request', 'done'))
        log.db.execute('UPDATE events SET at=105 WHERE id=?', (two,))
    number = public_number(None, one)
    filters = parse({'request_min': str(number), 'request_max': str(number),
                     'duration_min': '3', 'duration_max': '3', 'model': 'first',
                     'user': 'alice', 'ip': '198.51.100.1', 'status': 'done',
                     'date_min': '1970-01-01T00:01:40Z', 'date_max': '1970-01-01T00:01:40Z'})
    found = log.requests(None, 1, filters=filters)
    assert found['total'] == 1 and found['requests'][0]['id'] == number
    assert log.requests('bob', 25, filters=filters)['total'] == 0
    assert log.requests(None, 25, filters={'status': 'unfinished'})['total'] == 1
    assert log.requests(None, 25, filters={'duration_min': 0})['total'] == 1
    assert log.requests(None, 25, filters={'model': "' OR 1=1 --"})['total'] == 0


def test_public_range_handles_secondary_machine_and_timezone(tmp_path):
    log = Log(str(tmp_path / 'events.db'))
    number = log.add('request', 'asked', 'alice', '2001:db8::1', 'remote')
    with log.db:
        log.db.execute('UPDATE events SET source=7,machine=? WHERE id=?', ('retired-node', number))
    public = public_number('retired-node', 7)
    assert log.requests(None, 25, filters={'request_min': public, 'request_max': public})['total'] == 1
    assert parse({'date_min': '2026-10-20T10:00', 'timezone': 'America/Vancouver'})['date_min'] == datetime(2026, 10, 20, 17, tzinfo=UTC).timestamp()


def test_access_registry_retains_owners_failures_and_no_credentials(tmp_path):
    users = Users(str(tmp_path / 'users.db'), {'plain': Group()})
    users.add('alice', 'plain')
    key = users.new_key('alice')
    identity = users.identity('alice')
    users.record_access('2001:0db8::1', key, users.by_key(key))
    users.record_access('203.0.113.8', 'invalid-secret', None)
    with users.db:
        users.db.execute('UPDATE keys SET expires=0')
    users.record_access('2001:db8::1', key, users.by_key(key))
    rows = users.client_accesses()['rows']
    known = next(row for row in rows if row['owner_id'] == identity)
    assert (known['ip'], known['user'], known['accepted'], known['failed']) == ('2001:db8::1', 'alice', 1, 1)
    assert users.client_accesses(identity)['ips'] == ['2001:db8::1']
    assert 'invalid-secret' not in json.dumps(rows) and key not in json.dumps(rows)
    users.db.close()


def test_signed_origin_is_bound_to_request_and_isolated_across_clients(tmp_path):
    users = Users(str(tmp_path / 'users.db'), {'plain': Group()})
    keys = {}
    for name in ('alice', 'bob'):
        users.add(name, 'plain')
        keys[name] = users.new_key(name)
    log = Log(str(tmp_path / 'events.db'))
    secret = 'private control node application key'
    key_file = tmp_path / 'front.key'; key_file.write_text(secret)

    async def observed(request: Request):
        await asyncio.sleep(0)
        return JSONResponse({'ip': request.client.host})

    upstream = Starlette(routes=[Route('/observe', observed)])
    upstream.add_middleware(ClientAccesses, users=users, log=log)
    upstream.add_middleware(ClientOrigin, key_file=str(key_file))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream, client=('127.0.0.1', 1)), base_url='http://front') as wire:
            async def carry(request):
                await forward(request, secret)
            wire.event_hooks['request'].append(carry)

            async def send(request):
                result = await wire.get('/observe', headers={'authorization': 'Bearer ' + keys[request.path_params['name']]})
                return JSONResponse(result.json())

            console = Starlette(routes=[Route('/{name}', send)])
            console.add_middleware(ClientOrigin)
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=console, client=('198.51.100.4', 1)), base_url='http://console') as alice,
                        httpx.AsyncClient(transport=httpx.ASGITransport(app=console, client=('2001:db8::5', 1)), base_url='http://console') as bob):
                replies = await asyncio.gather(alice.get('/alice'), bob.get('/bob'))
                assert [r.json()['ip'] for r in replies] == ['198.51.100.4', '2001:db8::5']

    asyncio.run(exercise())
    with TestClient(upstream, client=('203.0.113.9', 1)) as direct:
        result = direct.get('/observe', headers={'authorization': 'Bearer bad-key', HEADER: 'forged', 'x-forwarded-for': '1.2.3.4'})
        assert result.json()['ip'] == '203.0.113.9'
        signed, _ = statement.make(secret, 'GET', '/observe', ('Bearer '+keys['alice']).encode(),
                                   machine='weightroom', purpose='client-origin', origin='198.51.100.4')
        assert direct.get('/observe', headers={'authorization': 'Bearer '+keys['bob'], HEADER: signed}).json()['ip'] == '203.0.113.9'
        assert direct.get('/observe', headers={'authorization': 'Bearer '+keys['alice'], HEADER: signed}).json()['ip'] == '198.51.100.4'
        assert direct.get('/observe', headers={'authorization': 'Bearer '+keys['alice'], HEADER: signed}).json()['ip'] == '203.0.113.9'
    rows = users.client_accesses()['rows']
    assert {(r['ip'], r['user']) for r in rows} == {('198.51.100.4', 'alice'), ('2001:db8::5', 'bob'),
                                                 ('203.0.113.9', ''), ('203.0.113.9', 'alice'), ('203.0.113.9', 'bob')}
    rejected = log.read(kind='key')[0]
    assert rejected['caller'] == '203.0.113.9' and 'bad-key' not in json.dumps(rejected)


def test_requests_ui_uses_suite_catalogs_and_access_registry(desk, tmp_path, monkeypatch):
    browser, switch, users = desk
    monkeypatch.setattr('turnstile.machines.Machines.models', lambda self: [{'names':['unused-catalog-model']}])
    users.record_access('198.51.100.42', '', users.by_name('pat'))
    log = switch.front.state.log
    one = log.add('request', 'asked', 'pat', '198.51.100.42', 'historical-only-model')
    log.add('request', 'done', 'pat', '198.51.100.42', 'historical-only-model', of=one)
    sign_in(browser, 'jordan')
    page = browser.get('/requests?limit=10&complete=1')  # the list is loaded after the page
    assert page.status_code == 200
    assert 'name="request_min"' in page.text and 'name="date_max"' in page.text and 'name="duration_max"' in page.text
    assert '<option value="cleo"' in page.text  # configured user with no request history
    assert '<option value="unused-catalog-model"' in page.text
    assert '<option value="198.51.100.42"' in page.text
    assert '<option value="historical-only-model"' not in page.text
    assert 'Client access' in page.text
    filtered = browser.get('/requests?ip=198.51.100.42&status=done&user=pat&complete=1')
    assert '1 results' in filtered.text and 'value="198.51.100.42" selected' in filtered.text
    assert 'name="ip"' in browser.get('/requests?completed=1&ip=198.51.100.42&complete=1').text
    invalid = browser.get('/requests?duration_min=9&duration_max=2&complete=1')
    assert 'Each minimum' in invalid.text and 'name="duration_min"' in invalid.text
    sign_in(browser, 'pat')
    assert '<option value="cleo"' not in browser.get('/requests?complete=1').text
    with TestClient(switch.front) as api:
        key = browser.cookies['weightroom']
        assert api.get('/loadcoach/requests?duration_min=nan', headers={'authorization': 'Bearer '+key}).status_code == 400
        assert api.get('/loadcoach/requests?user=jordan', headers={'authorization': 'Bearer '+key}).json()['total'] == 0


def test_internal_dispatch_is_not_misidentified_as_an_external_key_failure(tmp_path):
    from starlette.responses import Response
    users = Users(str(tmp_path/'users.db'), {'plain': Group()})
    log = Log(str(tmp_path/'events.db'))
    app = Starlette(routes=[Route('/', lambda request: Response(status_code=401))])
    app.add_middleware(ClientAccesses, users=users, log=log, internal_prefix='internal:')
    with TestClient(app, client=('127.0.0.1', 1)) as client:
        assert client.get('/', headers={'authorization':'Bearer internal:deleted-user'}).status_code == 401
        assert users.client_accesses()['rows'] == []
        assert client.get('/').status_code == 401
        assert users.client_accesses()['rows'][0]['failed'] == 1


def test_column_filters_work_in_browser_and_remain_inside_mobile_table(desk):
    import os
    playwright = pytest.importorskip('playwright.sync_api')
    from test_console_browser import browser_context
    browser, switch, users = desk
    users.record_access('198.51.100.42', '', users.by_name('pat'))
    log = switch.front.state.log
    entries = []
    for name, outcome in (('pat', 'done'), ('pat', 'failed'), ('jordan', 'done')):
        number = log.add('request', 'asked', name, '198.51.100.42', 'old-label')
        log.add('request', outcome, name, '198.51.100.42', 'old-label', of=number)
        entries.append(public_number(None, number))
    sign_in(browser, 'jordan')
    with playwright.sync_playwright() as engine:
        chromium, page = browser_context(engine, browser)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/requests')
        page.get_by_label('Filter user', exact=True).select_option('pat')
        playwright.expect(page.locator('#recent-requests tbody tr')).to_have_count(2)
        page.get_by_text('Date, duration & request ranges', exact=True).click()
        page.get_by_label('Minimum request number').fill(str(entries[0]))
        page.get_by_label('Maximum request number').fill(str(entries[0]))
        page.get_by_role('button', name='Apply filters', exact=True).click()
        playwright.expect(page.locator('#recent-requests tbody tr')).to_have_count(1)
        playwright.expect(page.get_by_label('Filter user', exact=True)).to_have_value('pat')
        playwright.expect(page.get_by_label('Minimum request number')).to_have_value(str(entries[0]))
        assert 'request_min=' in page.url
        page.get_by_label('Filter status', exact=True).select_option('failed')
        playwright.expect(page.locator('#recent-requests')).to_contain_text('No requests on record.')
        page.get_by_role('link', name='Clear filters', exact=True).click()
        playwright.expect(page.locator('#recent-requests tbody tr')).to_have_count(3)
        if os.getenv('SUITE_REQUEST_FILTER_SCREENSHOTS'):
            page.screenshot(path='/tmp/suite-request-filters-desktop.png', full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.get_by_label('Filter security level', exact=True).is_visible()
        assert page.locator('.rq-filter-rail').is_visible()
        if os.getenv('SUITE_REQUEST_FILTER_SCREENSHOTS'):
            page.screenshot(path='/tmp/suite-request-filters-mobile.png', full_page=True)
        assert not errors
        chromium.close()
