"""Turnstile adapters use openWeightGym permissions while compute is unreachable, without waking it."""

import json
import time

import httpx
import pytest
from starlette.testclient import TestClient

from loadcoach.groups import Group
from loadcoach.users import Users
from plugin_helpers import setup, serving
from turnstile.app import Settings, build_app


def test_compute_offline_adapter_routing_auth_sessions_and_minimal_logs(tmp_path):
    registry, _, adapter, _ = setup(tmp_path)
    # Who may use what is the control node's: the accounts are kept beside turnstile. Compute's list of models is the last it gave.
    (tmp_path / 'data').mkdir(exist_ok=True)
    users = Users(str(tmp_path / 'data' / 'accounts.db'), {'plugins': Group(can=frozenset({'front-plugins'})), 'none': Group()})
    users.add('alice', 'plugins')
    key = users.new_key('alice')
    user = {'name': 'alice', 'identity': users.identity('alice')}
    registry.auth.replace({'generated_at': time.time(), 'keys': [], 'models': [{'id': 'small', 'owned_by': 'local'}], 'unlogged': False})
    compute_calls, wakes = [], []
    async def offline(request):
        compute_calls.append(request.url.path)
        raise httpx.ConnectError('offline')
    with serving(adapter) as url:
        registry.save_instance('hermes', 'local', url, registry.instance('hermes', 'local')['settings'], enabled=True)
        registry.inventory('hermes', 'local', [{'id': 'gandalf'}])
        registry.model('hermes', 'local', 'gandalf', 'hermes:gandalf', True)
        turnstile = build_app(Settings(compute='compute', data=str(tmp_path / 'data'), plugins_dir=str(registry.root), wake_mac='00:11:22:33:44:55'),
                          httpx.AsyncClient(transport=httpx.MockTransport(offline)), lambda *args: wakes.append(args), users=Users(str(tmp_path / 'data' / 'accounts.db'), {}))
        with TestClient(turnstile, base_url='http://front:8442') as client:
            headers = {'authorization': 'Bearer ' + key, 'x-suite-user-id': 'attacker', 'x-suite-instance': 'wrong', 'x-suite-session-mode': 'persistent'}
            body = {'model': 'hermes:gandalf', 'messages': [{'role': 'user', 'content': 'private prompt'}], 'stream': True}
            response = client.post('/v1/chat/completions', headers=headers, json=body)
            assert response.status_code == 200 and 'Final answer' in response.text
            assert response.headers['x-suite-adaptor'] == 'hermes' and response.headers['x-suite-instance'] == 'local'
            assert compute_calls == [] and wakes == []
            sid = response.headers['x-suite-session-id']
            receipt = client.get('/turnstile/plugins/runs/hermes/local/' + response.headers['x-suite-request-id'], headers=headers)
            assert receipt.status_code == 200 and receipt.json()['status'] == 'completed'
            assert adapter.state.runtime.db.execute('SELECT owner FROM sessions WHERE id=?', (sid,)).fetchone()[0] == user['identity']
            catalog = client.get('/v1/models', headers=headers)
            assert catalog.status_code == 200 and {row['id'] for row in catalog.json()['data']} == {'small', 'hermes:gandalf'}
            assert not catalog.json()['data'][0]['available'] and wakes == []
            assert catalog.json()['unavailable_machines'] == ['compute']
            assert client.post('/v1/chat/completions', headers=headers, json={**body, 'model': 'hermes:unknown'}).status_code == 404
            # Raw user keys are never forwarded or stored. The turnstile log stores only use metadata.
            uses = registry.activity(user)
            assert [row['outcome'] for row in uses] == ['completed', 'started']
            log = json.dumps(uses)
            assert 'private prompt' not in log and 'Final answer' not in log and 'tokens' not in log and key not in log
            assert client.post('/v1/chat/completions', headers={**headers, 'x-loadcoach-log': 'off'}, json=body).status_code == 403
            users.change('alice', group='none')  # no longer may use the plugins: at once, since turnstile reads the accounts each time
            assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 404
            users.change('alice', group='plugins', enabled=False)
            assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 401
            assert wakes == []
    registry.db.close()


@pytest.mark.parametrize('missing', [set(), {8442}, {9442}, {8442, 9442}])
def test_plugin_catalogue_preserves_missing_worker_evidence(tmp_path, missing):
    """Plugin merging must not turn a partial native inventory into proof of removal."""
    from front_helpers import machine_key
    from loadcoach import statement
    from test_two_machines import Two, worker

    registry, _, _, _ = setup(tmp_path)
    users = Users(str(tmp_path / 'data' / 'accounts.db'), {
        'plain': Group(can=frozenset({'load', 'front-plugins'})),
        'system': Group(can=frozenset({'follow-settings'}))})
    users.add('alice', 'plain')
    users.add('weightroom', 'system')
    (tmp_path / 'weightroom.key').write_text(users.new_key('weightroom'))
    headers = {'authorization': 'Bearer ' + users.new_key('alice')}
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / 'rig.key')
    statement.replace_key(rig_key, 'r' * 40)
    wire = Two(worker(primary_key, 'compute', False, model='speech', content='a' * 64),
               worker(rig_key, 'rig', False, model='embed', content='b' * 64))
    wire.missing_catalogues = missing
    settings = Settings(compute='compute', data=str(tmp_path / 'data'),
                        machine_key_file=primary_key, plugins_dir=str(registry.root),
                        auth_key_file=str(tmp_path / 'weightroom.key'),
                        machines={'rig': {'compute': 'http://compute:9442', 'machine_key_file': rig_key}},
                        machines_poll_seconds=.01)
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users),
                    base_url='http://front:8442') as front:
        for _ in range(100):
            catalogue = front.get('/v1/models', headers=headers).json()
            if 9442 in missing or any(row['id'] == 'embed' for row in catalogue['data']):
                break
            time.sleep(.01)
        unavailable = catalogue['unavailable_machines']
        assert bool(unavailable) == bool(missing)
        if len(missing) < 2:
            assert unavailable == sorted('compute' if port == 8442 else 'rig' for port in missing)
            assert {row['id'] for row in catalogue['data']} == {
                model for port, model in ((8442, 'speech'), (9442, 'embed')) if port not in missing}
        if not missing:
            wire.missing_catalogues = {8442}
            retained = front.get('/v1/models', headers=headers).json()
            assert retained['unavailable_machines'] == ['compute']
            assert retained['available_machines'] == ['rig']
            assert {row['id'] for row in retained['data']} == {'speech', 'embed'}
    registry.db.close()
