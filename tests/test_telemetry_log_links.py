"""Log links resolve exact historical hosts without borrowing another machine's readings."""

import json
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from loadcoach.log import Log
from test_console_browser import browser_context, playwright
from test_telemetry_workspace import HOST, fleet, payload, telemetry_server  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401
from turnstile.machines import Machines

RECORDED = '3821eba8bbf77a6ddab346d79d8f8558a67eea5dd36e0d232fb7e0eaaa6c53ee'
REAL_HISTORY = Machines.history


@pytest.mark.parametrize('until', ['2026-11-13T22:04:02Z', '2026-11-13T15:04:02-07:00'])
def test_historical_range_preserves_the_timezone_in_the_link(telemetry_server, until):
    server, (_, calls), _ = telemetry_server
    expected = datetime.fromisoformat(until).timestamp()
    response = server.get('/telemetry/' + HOST, params={'until': until, 'hours': .5})
    assert response.status_code == 200
    data = payload(response)
    assert data['end'] == expected and data['begin'] == expected - 1800
    assert calls[-1][1:3] == (expected, 1800)


def test_log_link_opens_its_exact_recorded_host_with_no_invented_samples(telemetry_server, tmp_path, monkeypatch):
    server, (_, calls), _ = telemetry_server
    log = Log(str(tmp_path / 'data/accounts.db'), keep_days=0)
    number = log.add('service', 'failed', 'pat', machine_fingerprint=RECORDED)
    recorded_at = int(log.db.execute('SELECT at FROM events WHERE id=?', (number,)).fetchone()[0])
    def empty_history(self, fingerprint, end, seconds, points):
        calls.append((fingerprint, end, seconds, points))
        return {'begin': end - seconds, 'end': end, 'step': seconds / points, 'series': {}}
    monkeypatch.setattr(Machines, 'history', empty_history)
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/log/' + str(number))
        link = page.get_by_role('link', name='The machine in the half hour before', exact=True)
        address = urlsplit(link.get_attribute('href'))
        query = parse_qs(address.query)
        assert address.path == '/telemetry/' + RECORDED
        assert datetime.fromisoformat(query['until'][0]).timestamp() == recorded_at
        assert datetime.fromisoformat(query['until'][0]).tzinfo is not None
        link.click()
        playwright.expect(page.locator('#host-title')).to_contain_text('Recorded host ' + RECORDED[:12])
        playwright.expect(page.get_by_text('No telemetry samples were retained for this physical host in the selected time range.')).to_be_visible()
        assert page.locator('#history-range').evaluate('(form) => form.checkValidity()')
        assert calls[-1][0:3] == (RECORDED, recorded_at, 1800)
        assert not page.locator('#host-view button', has_text='Wake').count()
        for width in (1440, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        assert not errors
        browser.close()
    # The log reference does not register a new active host or grant it to other users.
    overview = server.get('/telemetry')
    assert 'Recorded host ' + RECORDED[:12] not in overview.text
    server.post('/sign-out'); sign_in(server, 'wanda')
    assert server.get('/telemetry/' + RECORDED).status_code == 404
    sign_in(server, 'jordan')
    assert server.get('/telemetry/' + RECORDED).status_code == 200


def test_retained_samples_survive_missing_live_registration(telemetry_server, tmp_path, monkeypatch):
    server, _, _ = telemetry_server
    log = Log(str(tmp_path / 'data/accounts.db'), keep_days=0)
    until = datetime.fromisoformat('2026-11-13T22:04:02Z').timestamp()
    with log.db:
        log.db.execute('INSERT INTO machine_telemetry(fingerprint,at,numbers) VALUES(?,?,?)',
                       (RECORDED, until - 30, json.dumps({'processor load': 23})))
    monkeypatch.setattr(Machines, 'history', REAL_HISTORY)
    response = server.get('/telemetry/' + RECORDED, params={'until': '2026-11-13T22:04:02Z', 'hours': .5})
    assert response.status_code == 200 and 'Only retained history is shown' in response.text
    data = payload(response)
    processor = next(group for group in data['groups'] if group['id'] == 'processor')
    assert 23 in processor['charts'][0]['told']['values']
    assert data['end'] == until and data['begin'] == until - 1800
    assert 'No telemetry samples were retained' not in response.text


def test_retained_log_host_lookup_is_exact_and_owner_scoped(tmp_path):
    log = Log(str(tmp_path / 'events.db'), keep_days=0)
    log.add('service', 'started', 'pat', machine_fingerprint=RECORDED)
    assert log.has_host_reference(RECORDED, 'pat')
    assert log.has_host_reference(RECORDED, None)
    assert not log.has_host_reference(RECORDED, 'cleo')
    assert not log.has_host_reference(RECORDED[:-1], 'pat')
    assert not log.has_host_reference('x' + RECORDED, 'pat')
