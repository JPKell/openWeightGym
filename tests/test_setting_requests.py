"""Control-node request settings: live readers, interruptible polling and pool restart."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from loadcoach.groups import Group
from loadcoach.settings import said
from loadcoach.users import Users
from turnstile.app import Settings, build_app, load_settings
from turnstile.machines import Machines
from front_helpers import machine_key


@pytest.fixture
def control(tmp_path):
    users = Users(tmp_path / 'accounts.db', {'admin': Group(can=frozenset({'manage-settings', 'load', 'front-wake'}))})
    users.add('owner', 'admin')
    headers = {'authorization': 'Bearer ' + users.new_key('owner')}

    class Full(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.PoolTimeout('Unit-test pool is occupied.')

    settings = Settings(compute='compute', data=str(tmp_path), machine_key_file=machine_key(tmp_path),
                        wake_mac='00:11:22:33:44:55', wait=0.5)
    wakes = []
    app = build_app(settings, httpx.AsyncClient(transport=Full()), lambda *packet: wakes.append(packet), users=users)
    app.state.console_settings.changes.seen('turnstile', said(vars(settings), {}, {}, 'turnstile'))
    with TestClient(app, base_url='http://control:8442', headers=headers) as api:
        yield api, app, settings, wakes


def test_wait_retry_and_poll_settings_change_in_the_running_service(control):
    api, app, settings, wakes = control
    assert api.get('/loadcoach/status?node=compute').headers['retry-after'] == '5'
    assert api.post('/loadcoach/settings/turnstile', json={'busy_retry_seconds': 9, 'wait': 0.25, 'machines_poll_seconds': 2}).status_code == 200
    assert api.get('/loadcoach/status?node=compute').headers['retry-after'] == '9'
    assert api.get('/turnstile/machines').json()['poll_seconds'] == 2
    assert app.state.machines.every == 2
    wake = api.post('/turnstile/machines/compute/wake')
    assert wake.status_code == 202 and wake.json()['timeout_seconds'] == 0.25 and len(wakes) == 1
    report = next(p for p in api.get('/loadcoach/settings').json()['programs'] if p['program'] == 'turnstile')
    assert {s['name']: s['value'] for s in report['settings']}['busy_retry_seconds'] == 9
    assert api.post('/loadcoach/settings/turnstile', json={'busy_retry_seconds': None, 'wait': None, 'machines_poll_seconds': None}).status_code == 200
    assert api.get('/loadcoach/status?node=compute').headers['retry-after'] == '5'
    assert app.state.machines.every == settings.machines_poll_seconds


@pytest.mark.parametrize('name,value', [('wait', 0), ('wait', -1), ('busy_retry_seconds', 0),
                                     ('machines_poll_seconds', 1), ('machines_poll_seconds', 301),
                                     ('max_in_flight', 0), ('max_in_flight', 1.5)])
def test_request_setting_ranges_are_validated_before_save(control, name, value):
    api, app, _, _ = control
    assert api.post('/loadcoach/settings/turnstile', json={name: value}).status_code == 400
    assert name not in app.state.console_settings.changes.of('turnstile')


def test_pool_capacity_waits_for_startup_while_pending_override_is_retained(control, tmp_path):
    api, app, settings, _ = control
    assert api.post('/loadcoach/settings/turnstile', json={'max_in_flight': 37}).status_code == 200
    program = next(p for p in api.get('/loadcoach/settings').json()['programs'] if p['program'] == 'turnstile')
    row = next(s for s in program['settings'] if s['name'] == 'max_in_flight')
    assert row['when'] == 'restart' and row['value'] == settings.max_in_flight and row['changed']['value'] == 37
    path = tmp_path / 'weightroom.toml'
    path.write_text('[turnstile]\nmax_in_flight = 200\n')
    (tmp_path / 'settings.json').write_text(json.dumps({'turnstile': app.state.console_settings.changes.of('turnstile')}))
    assert load_settings(path).max_in_flight == 37
    assert settings.max_in_flight == 200


def test_shorter_poll_interval_interrupts_the_previous_long_delay(monkeypatch):
    import turnstile.machines as module
    clock, interval, polls, sleeps = [0.0], [10.0], [], []
    machines = Machines([], None, every=lambda: interval[0])

    async def poll():
        polls.append(clock[0])
        if len(polls) == 2:
            raise asyncio.CancelledError

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        interval[0] = 2.0

    machines.poll = poll
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(machines.run())
    assert polls == [0.0, 2.0] and sleeps == [1, 1]


def test_restart_value_reports_the_started_value_even_with_a_different_file_fallback(tmp_path):
    from turnstile.settings import ConsoleSettings
    console = ConsoleSettings(tmp_path / 'settings.db', None, None, defaults={'turnstile': {'max_in_flight': 200}})
    try:
        console.changes.seen('turnstile', {'max_in_flight': {'value': 37}})
        console.changes.put('turnstile', 'max_in_flight', 50, 'operator')
        assert console.live('turnstile', 'max_in_flight', 37) == 37
        assert console.reported()[0]['settings'][0]['value'] == 37
    finally:
        console.changes.db.close()
