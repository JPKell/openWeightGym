"""Front ToolLocker ownership with compute unavailable and existing signed MCP clients."""

import json
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from db import migrate
from db.consolidation_import import copy_missing, settings as import_settings
from db.schemas import TOOL_SCHEMAS
from front_helpers import front_of
from loadcoach.groups import Group
from loadcoach.log import Log
from loadcoach.settings import Changes
from loadcoach.users import Users
from toollocker.door import Config, build_app, load_config
from turnstile.app import Settings, build_app as build_front, load_settings


def test_front_config_inherits_named_accounts_and_key_without_loadcoach(tmp_path):
    (tmp_path / 'weightroom.toml').write_text('''data = "."
[turnstile]
machine_key_file = "front.key"
[databases.stores.accounts]
engine = "sqlite"
path = "authority.db"
[databases.bindings]
"weightroom.accounts" = "accounts"
''')
    config = tmp_path / 'toollocker.toml'
    config.write_text('front = "weightroom.toml"\nport = 9443\n')
    result = load_config(config)
    assert result.database.path == tmp_path / 'authority.db'
    assert result.machine_key_file == str(tmp_path / 'front.key')
    assert not (tmp_path / 'loadcoach.db').exists()
    config.write_text('front = "weightroom.toml"\nloadcoach = "other.toml"\n')
    with pytest.raises(SystemExit, match='choose front or loadcoach'):
        load_config(config)


def test_front_tools_survive_compute_loss_with_permissions_settings_and_chain(tmp_path):
    source = tmp_path / 'accounts.db'
    users = Users(source, {'admin': Group(max_sensitivity='restricted', can=frozenset({'manage-settings'})),
                           'plain': Group(tools=('fake__echo',))})
    users.add('owner', 'admin')
    users.add('plain', 'plain')
    owner = {'authorization': 'Bearer ' + users.new_key('owner')}
    plain = {'authorization': 'Bearer ' + users.new_key('plain')}
    config = Config(servers={'fake': {'command': [sys.executable, str(Path(__file__).with_name('fake_tool_server.py'))]}},
                    database=source, machine_key_file=str(tmp_path / 'machine.key'))
    with front_of(lambda _: build_app(config), users, tmp_path, port=8443,
                  toollocker_url='http://front-tools:9443', front_settings=True) as client:

        async def offline(*args, **kwargs):
            return None

        client.front.state.front_settings.ask = offline
        response = client.post('/mcp', headers=owner, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response.status_code == 200
        assert any(t['name'] == 'fake__echo' for t in response.json()['result']['tools'])
        call = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'fake__echo', 'arguments': {'text': 'kept'}}}
        result = client.post('/mcp', headers={**owner, 'x-loadcoach-sensitivity': 'sensitive', 'x-loadcoach-trace': 'front-test'}, json=call)
        assert result.json()['result']['content'][0]['text'] == 'kept'
        assert client.post('/mcp', headers={'authorization': 'Bearer wrong'}, json=call).status_code == 401
        hidden = {**call, 'params': {'name': 'fake__pid'}}
        assert 'error' in client.post('/mcp', headers=plain, json=hidden).json()
        tools = client.get('/loadcoach/tools', headers=owner)
        assert tools.status_code == 200 and len(tools.json()['tools']) == 5
        # Front's report includes ToolLocker even when compute cannot answer.
        listed = client.get('/loadcoach/settings', headers=owner)
        assert listed.status_code == 200 and listed.json()['compute_unavailable']
        assert any(p['program'] == 'toollocker' for p in listed.json()['programs'])
        assert client.post('/loadcoach/settings/toollocker', headers=owner, json={'call_timeout': 41}).status_code == 200
        assert Changes(source).of('toollocker')['call_timeout'] == 41
        assert client.get('/loadcoach/tools', headers=plain).status_code == 403
        users.remove('owner')
        assert client.post('/mcp', headers=owner, json=call).status_code == 401
    log = Log(source)
    assert log.verify()[1] is None
    assert log.trace_level('owner', 'front-test') == 'sensitive'
    assert any(e.get('text', {}).get('arguments') == {'text': 'kept'} for e in log.read(kind='tool', with_text=True))
    log.db.close()


def test_front_tool_route_targets_front_and_never_wakes_compute(tmp_path):
    key = tmp_path / 'machine.key'
    key.write_text('the front signing key')
    users = Users(tmp_path / 'accounts.db', {'plain': Group()})
    users.add('caller', 'plain')
    headers = {'authorization': 'Bearer ' + users.new_key('caller')}
    targets, wakes = [], []

    def failed(request):
        targets.append(str(request.url))
        raise httpx.ConnectError('offline', request=request)

    front = build_front(Settings(compute='compute', data=str(tmp_path), ports=(8443,), machine_key_file=str(key),
                                 wake_mac='aa:bb:cc:dd:ee:ff', toollocker_url='http://127.0.0.1:9443', front_settings=True),
                        users=users, client=httpx.AsyncClient(transport=httpx.MockTransport(failed)),
                        waker=lambda *args: wakes.append(args))
    with TestClient(front, base_url='http://front:8443') as client:
        response = client.post('/mcp', headers=headers, json={'id': 1, 'method': 'ping'})
        assert response.status_code == 503 and response.headers['retry-after']
        assert 'Front ToolLocker' in response.text
    assert targets == ['http://127.0.0.1:9443/mcp'] and not wakes


def test_toollocker_import_is_exact_idempotent_and_conflicts_are_atomic(tmp_path):
    before, after = Changes(tmp_path / 'compute.db'), Changes(tmp_path / 'front.db')
    before.put('toollocker', 'call_timeout', 42, 'original')
    before.seen('toollocker', {'call_timeout': {'value': 42}})
    for changes in (before, after):
        migrate(changes.db.database, changes.db, 'toollocker.tools', TOOL_SCHEMAS)
    before.db.execute('INSERT INTO tool_schemas VALUES (?,?)', ('files__read', json.dumps({'name': 'read'})))
    before.db.commit()
    assert import_settings(before.db, after.db, programs={'toollocker'}, apply=True)['tables']['settings_changed']['missing'] == 1
    assert import_settings(before.db, after.db, programs={'toollocker'}, apply=True)['tables']['settings_changed']['missing'] == 0
    assert before.db.execute('SELECT * FROM settings_changed').fetchall() == after.db.execute('SELECT * FROM settings_changed').fetchall()
    selection = [('tool_schemas', '', (), {})]
    assert copy_missing(before.db, after.db, selection, apply=True)['tables']['tool_schemas']['missing'] == 1
    assert copy_missing(before.db, after.db, selection, apply=True)['tables']['tool_schemas']['missing'] == 0
    after.db.execute('UPDATE tool_schemas SET schema=?', ('{}',))
    after.db.commit()
    with pytest.raises(ValueError, match='conflicting'):
        copy_missing(before.db, after.db, selection, apply=True)
    assert after.db.execute('SELECT schema FROM tool_schemas').fetchone()[0] == '{}'
    before.db.close()
    after.db.close()


def test_stale_compute_gateway_cannot_restart_after_relocation(tmp_path):
    (tmp_path / 'loadcoach.toml').write_text('front_toollocker = true\ndata = "."\n')
    config = tmp_path / 'toollocker.toml'
    config.write_text('port = 8443\n')
    with pytest.raises(SystemExit, match='ToolLocker moved to front'):
        load_config(config)


def test_weightroom_keeps_gateway_recovery_ownership_after_splitting_shared_settings(tmp_path):
    from weightroom.app import load_settings as load_front
    (tmp_path / 'front.key').write_text('private test key')
    path = tmp_path / 'weightroom.toml'
    path.write_text('compute = "http://compute:8442"\nkey_file = "front.key"\n[turnstile]\ntoollocker_url = "http://127.0.0.1:9443"\n')
    config = load_front(path)
    assert config.front_toollocker
    assert 'turnstile' not in config.file
    path.write_text('compute = "http://compute:8442"\nkey_file = "front.key"\n')
    assert not load_front(path).front_toollocker
