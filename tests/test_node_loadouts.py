"""Explicit node routing must not mutate primary sets, lose caller headers or wake hosts."""

import json
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient
from front_helpers import door
from test_console_browser import browser_context, playwright
from weightroom.app import Settings, build_app

from test_two_machines import metadata_front  # noqa: F401


def test_node_loadout_route_isolates_names_and_preserves_signed_caller(metadata_front):
    front, wire, _, key, _ = metadata_front
    headers = {'authorization': 'Bearer ' + key, 'x-loadcoach-lock': 'review-lock', 'x-loadcoach-priority': 'normal'}
    for node in ('compute', 'rig'):
        result = front.post(f'/turnstile/machines/{node}/control/loadouts', headers=headers,
                            json={'action': 'save', 'name': 'Daily', 'models': [node]})
        assert result.status_code == 200, result.text
        assert result.json()['machine'] == node and result.json()['caller'] == 'pat'
        assert result.json()['lock'] == 'review-lock' and result.json()['priority'] == 'normal'
    for node in ('compute', 'rig'):
        response = front.get(f'/turnstile/machines/{node}/control/loadouts', headers=headers)
        assert response.json()['loadouts'] == [{'action': 'save', 'name': 'Daily', 'models': [node]}]
        assert response.headers['cache-control'] == 'no-store'
    assert front.post('/turnstile/machines/missing/control/loadouts', headers=headers, json={}).status_code == 404
    assert front.get('/turnstile/machines/rig/control/loadouts').status_code == 401
    before = len(wire.calls)
    assert front.post('/turnstile/machines/rig/control/loadouts', headers=headers, content='x' * 65537).status_code == 413
    assert len(wire.calls) == before


def test_unavailable_node_never_falls_back_or_wakes_for_a_set(metadata_front):
    front, wire, _, key, _ = metadata_front
    wire.down = True
    before = len(wire.calls)
    response = front.post('/turnstile/machines/rig/control/loadouts', headers={'authorization': 'Bearer ' + key},
                          json={'action': 'request', 'name': 'Daily', 'token': 'reviewed'})
    assert response.status_code == 503
    assert all(port == 9442 and path == '/loadcoach/loadouts' for port, path in wire.calls[before:])


def test_console_node_drafts_survive_navigation_and_unavailability(metadata_front, tmp_path):
    front, wire, _, key, own = metadata_front
    app = build_app(Settings(compute='http://compute', key=own, data=str(tmp_path / 'web')), door(front.app, 8442))
    with TestClient(app) as server:
        server.cookies.set('weightroom', key, domain='testserver.local', path='/')
        with playwright.sync_playwright() as driver:
            browser, page = browser_context(driver, server)
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            answer = page.goto('http://console.test/dry-fit?machine=rig')
            assert answer.status == 200, page.locator('body').inner_text()
            assert page.locator('#fit-data').count(), page.locator('body').inner_text()
            assert page.locator('[data-catalogue-select]').count(), (page.locator('#fit-data').inner_text(), errors)
            page.get_by_label('Select small', exact=True).check()
            page.locator('[data-context]').fill('2001')
            page.locator('[data-fit-node="compute"]').click()
            page.get_by_label('Select small', exact=True).check()
            page.locator('[data-context]').fill('3001')
            page.locator('[data-fit-node="rig"]').click()
            playwright.expect(page.locator('[data-context]')).to_have_value('2001')
            wire.down = True
            page.reload()
            playwright.expect(page.locator('#verdict-title')).to_have_text('Node unavailable')
            assert page.locator('[data-fit-node="compute"]').count() == 1
            wire.down = False
            page.reload()
            playwright.expect(page.locator('[data-context]')).to_have_value('2001')
            assert not errors
            browser.close()
        # A posted node is retained in the destination and the redirect; primary is untouched.
        response = server.post('/dry-fit', data={'machine': 'rig', 'do': 'save', 'name': 'Node set',
                               'selection': json.dumps([{'model': 'small', 'cards': [], 'context': 2001}])}, follow_redirects=False)
        assert response.status_code == 303
        assert parse_qs(urlsplit(response.headers['location']).query)['machine'] == ['rig']
        headers = {'authorization': 'Bearer ' + key}
        assert front.get('/turnstile/machines/compute/control/loadouts', headers=headers).json()['loadouts'] == []
        assert front.get('/turnstile/machines/rig/control/loadouts', headers=headers).json()['loadouts'][0]['name'] == 'Node set'
