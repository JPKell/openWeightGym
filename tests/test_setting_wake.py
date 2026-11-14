"""Control-node Wake overrides work without contacting or changing node files."""
import pytest
from test_setting_requests import control


def test_wake_overrides_apply_to_the_next_packet_and_reset_without_restart(control):
    api, app, settings, wakes = control
    base = '/turnstile/machines/compute/settings'
    report = api.get(base)
    assert report.status_code == 200 and report.json()['unavailable']
    program = next(p for p in report.json()['programs'] if p['program'] == 'machines')
    assert {s['when'] for s in program['settings']} == {'now'}
    patch = {'wake_mac': '01:23:45:67:89:ab', 'wake_address': '192.0.2.255'}
    assert api.post(base + '/machines', json=patch).status_code == 200
    assert api.post('/turnstile/machines/compute/wake').status_code == 202
    assert wakes[-1] == (patch['wake_mac'], patch['wake_address'])
    assert settings.machines['compute']['wake_mac'] == '00:11:22:33:44:55'
    assert app.state.console_settings.changes.of('machines:compute') == patch
    assert api.post(base + '/machines', json={'wake_mac': '', 'wake_address': None}).status_code == 200
    assert api.post('/turnstile/machines/compute/wake').status_code == 409
    assert api.post(base + '/machines', json={'wake_mac': None}).status_code == 200
    assert api.post('/turnstile/machines/compute/wake').status_code == 202
    assert wakes[-1] == ('00:11:22:33:44:55', '255.255.255.255')


@pytest.mark.parametrize('patch', [{'wake_mac': 'shell;command'}, {'wake_address': 'x;reboot'},
                                  {'wake_mac': 1}, {'compute': 'http://changed'},
                                  {'machine_key_file': '/new'}, {'wake_address': '::1'}])
def test_wake_validation_is_atomic_and_cannot_change_listeners_or_keys(control, patch):
    api, app, _, _ = control
    assert api.post('/turnstile/machines/compute/settings/machines', json=patch).status_code == 400
    assert not app.state.console_settings.changes.of('machines:compute')
    assert api.post('/turnstile/machines/missing/settings/machines', json=patch).status_code == 404


def test_wake_settings_require_the_existing_settings_permission(control):
    api, app, _, _ = control
    assert api.post('/turnstile/machines/compute/settings/machines', json={'wake_mac': ''},
                    headers={'authorization': 'Bearer invalid'}).status_code == 401
