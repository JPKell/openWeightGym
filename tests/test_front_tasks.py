"""Task records kept on the front, through real signed front/node ASGI boundaries."""

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from front_helpers import OnCompute
from test_app import add_user, fake_cards, fake_llama_server, gateway, in_an_empty_folder, model
from test_tasks import create
from test_weightroom import gguf


def retire(path):
    """Mark a node store as retired after the DB1 cutover, as `db.consolidation_retire` does, keeping its tables."""
    from dataclasses import replace
    from db import Database, migrate
    from db.schemas import COMPUTE_RETIREMENT
    database = Database.sqlite(path)
    connection = database.connect()
    try:
        migrate(database, connection, 'loadcoach.retirement', (replace(COMPUTE_RETIREMENT[0], sqlite=lambda *_: None),))
    finally:
        connection.close()


def task_gateway(fake_llama_server, fake_cards, tmp_path, stateless=False):
    """A front and one node. `stateless`: the node's store is retired, so it keeps no records of its own."""
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=2048)
    if stateless and not (tmp_path / 'compute.db').exists():
        retire(tmp_path / 'compute.db')
    return gateway(fake_llama_server, models={'small': model('small.gguf', ['GPU0'], **{'ctx-size': 2048, 'mib': 1000})},
                   nvidia_smi=fake_cards[0], database=str(tmp_path / 'compute.db'))


def legacy_task(caller, name):
    """A task the node created and keeps itself, as nodes did before the front kept every task record."""
    import httpx
    from front_helpers import KEY
    from loadcoach import statement
    body = {'id': 'batch_' + secrets.token_hex(12), 'token': secrets.token_urlsafe(32), 'model': 'small'}
    encoded = json.dumps(body).encode()
    async def made():
        user = caller.front.state.users.by_name(name)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=caller.compute), base_url='http://compute') as node:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                signed, _nonce = statement.make(KEY, 'POST', '/loadcoach/tasks', encoded, statement.caller_of(user))
                response = await node.post('/loadcoach/tasks', content=encoded, headers={statement.HEADER: signed, 'content-type': 'application/json'})
                if response.status_code == 201:
                    return body
                assert 'fingerprinted' in response.text, response.text
                await asyncio.sleep(.02)
            raise AssertionError(response.text)
    return caller.portal.call(made)


def test_retired_node_api_and_cli_cannot_read_or_reset_console_settings(fake_llama_server, fake_cards, tmp_path, capsys):
    import httpx
    from front_helpers import KEY
    from loadcoach import statement
    from loadcoach.settings import Changes, FRONT_PROGRAMS
    from loadcoach.cli import settings_command
    with task_gateway(fake_llama_server, fake_cards, tmp_path, stateless=True) as caller:
        add_user(caller, 'owner', 'admin')
        caller.users.change('owner', own={'add': ['follow-settings']})
        changes = Changes(tmp_path / 'compute.db')
        changes.seen('weightroom', {'timezone': {'value': 'UTC'}})
        changes.put('weightroom', 'timezone', 'UTC', 'old-owner')
        async def rejected():
            user = caller.front.state.users.by_name('owner')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=caller.compute), base_url='http://compute') as web:
                for method, path, body in [
                    ('GET', '/loadcoach/settings', None),
                    ('GET', '/loadcoach/settings/weightroom/changes', None),
                    ('POST', '/loadcoach/settings/weightroom', {'timezone': 'America/Vancouver'}),
                    ('POST', '/loadcoach/settings/weightroom/seen', {'told': {}}),
                ]:
                    encoded = json.dumps(body).encode() if body is not None else b''
                    signed, nonce = statement.make(KEY, method, path, encoded, statement.caller_of(user))
                    response = await web.request(method, path, content=encoded, headers={statement.HEADER: signed})
                    if path == '/loadcoach/settings':
                        assert response.status_code == 200
                        assert all(one['program'] not in FRONT_PROGRAMS for one in response.json()['programs'])
                    else:
                        assert response.status_code == 410, (path, response.text)
        caller.portal.call(rejected)
        settings_command(changes, excluded=FRONT_PROGRAMS)
        output = capsys.readouterr().out
        assert 'UTC' not in output and 'managed on front' in output
        import pytest
        with pytest.raises(ValueError, match='The front keeps'):
            settings_command(changes, 'reset', 'weightroom', 'timezone', excluded=FRONT_PROGRAMS)
        assert changes.of('weightroom') == {'timezone': 'UTC'}
        changes.db.close()


def test_front_creation_probe_receipt_finish_are_local_atomic_and_idempotent(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        intent = create(caller, owner)
        path = '/loadcoach/tasks/' + intent['id']
        scoped = {'x-loadcoach-task': intent['token']}
        assert caller.post('/loadcoach/tasks', headers=owner, json=intent).status_code == 200
        status = caller.get(path, headers=scoped)
        assert status.status_code == 200, status.text
        assert status.headers.get('x-suite-data-owner') == 'front'
        assert status.json()['allowed'] and status.json()['machine'] == 'compute'
        assert caller.get('/v1/models', headers={'authorization': 'Bearer ' + intent['token']}).status_code == 401
        options = {'context': 128, 'inputs': 32, 'outputs': 16, 'text': 'Benchmark text.'}
        probe_headers = {**scoped, 'x-loadcoach-probe-id': 'a' * 64}
        result = caller.post(path + '/probe', headers=probe_headers, json=options)
        assert result.status_code == 200, result.text
        repeated = caller.post(path + '/probe', headers=probe_headers, json=options)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == result.json()
        changed = caller.post(path + '/probe', headers=probe_headers, json={**options, 'outputs': 17})
        assert changed.status_code == 409
        for _ in range(2):
            ended = caller.post(path + '/finish', headers=scoped, json={'state': 'completed', 'result': {'ok': True}})
            assert ended.status_code == 200, ended.text
        assert caller.post(path + '/finish', headers=scoped, json={'state': 'completed', 'result': {'different': True}}).status_code == 409
        assert caller.get('/v1/batches/' + intent['id'], headers=owner).json()['status'] == 'completed'
        with sqlite3.connect(tmp_path / 'compute.db') as compute:
            assert compute.execute('SELECT COUNT(*) FROM benchmark_tasks').fetchone() == (0,)
            assert compute.execute('SELECT COUNT(*) FROM batches').fetchone() == (0,)
        front = caller.front.state.task_records
        async def counts():
            return [front.batches.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('benchmark_tasks', 'batches', 'batch_lines', 'files')]
        assert caller.portal.call(counts) == [1, 1, 1, 2]


def test_dispatch_is_private_and_wrong_scope_never_touches_compute(fake_llama_server, fake_cards, tmp_path):
    import httpx
    from loadcoach import statement
    from front_helpers import KEY
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        assert caller.post('/loadcoach/front-probes', headers=owner, json={}).status_code == 404
        assert OnCompute(caller).post('/loadcoach/front-probes', json={}).status_code == 403
        async def signed_refusals():
            user = caller.front.state.users.by_name('owner')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=caller.compute), base_url='http://compute') as compute:
                ordinary = statement.make(KEY, 'POST', '/loadcoach/front-probes', b'{}', statement.caller_of(user))[0]
                assert (await compute.post('/loadcoach/front-probes', content=b'{}', headers={statement.HEADER: ordinary})).status_code == 403
                signed = statement.make(KEY, 'POST', '/loadcoach/front-probes', b'{}', statement.caller_of(user), purpose='front-probe')[0]
                assert (await compute.post('/loadcoach/front-probes', content=b'{"changed":true}', headers={statement.HEADER: signed})).status_code == 401
        caller.portal.call(signed_refusals)
        intent = create(caller, owner)
        path = '/loadcoach/tasks/' + intent['id']
        assert caller.get(path, headers={'x-loadcoach-task': 'wrong-token'}).status_code == 404
        assert caller.post(path + '/probe', headers={'x-loadcoach-task': intent['token']}, json={'context': 128, 'inputs': 120, 'outputs': 16}).status_code == 400


def test_front_task_survives_revocation_and_legacy_ids_stay_on_their_node(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        manager = add_user(caller, 'manager', 'admin', by=owner)
        legacy = legacy_task(caller, 'owner')
        intent = create(caller, owner)
        scoped = {'x-loadcoach-task': intent['token']}
        path = '/loadcoach/tasks/' + intent['id']
        assert caller.get(path, headers=scoped).json()['allowed']
        legacy_status = caller.get('/loadcoach/tasks/' + legacy['id'], headers={'x-loadcoach-task': legacy['token']})
        assert legacy_status.json()['id'] == legacy['id'] and 'x-suite-data-owner' not in legacy_status.headers
        assert caller.post('/loadcoach/users/owner', headers=manager, json={'own': {'remove': ['benchmark']}}).status_code == 200
        assert caller.get(path, headers=scoped).json()['allowed'] is False
        assert caller.post(path + '/probe', headers=scoped, json={'context': 128}).status_code == 403
        assert caller.post(path + '/finish', headers=scoped, json={'state': 'cancelled'}).status_code == 200
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        assert caller.get(path, headers=scoped).json()['status'] == 'cancelled'
        assert caller.post(path + '/finish', headers=scoped, json={'state': 'cancelled'}).status_code == 200


def test_front_task_tracks_model_rename_then_refuses_reused_name(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        intent = create(caller, owner)
        path = '/loadcoach/tasks/' + intent['id']
        scoped = {'x-loadcoach-task': intent['token']}
        before = caller.get(path, headers=scoped).json()
        assert caller.post('/loadcoach/model-rename', headers=owner, json={'name': 'small', 'to': 'renamed'}).status_code == 200
        after = caller.get(path, headers=scoped).json()
        assert after['model'] == 'renamed' and after['allowed']
        assert after['model_id'] == before['model_id']
        assert caller.delete('/loadcoach/models/renamed', headers=owner).status_code == 200
        removed = caller.get(path, headers=scoped)
        assert removed.status_code == 200, removed.text
        assert removed.json()['allowed'] is False
        assert caller.post(path + '/probe', headers=scoped, json={'context': 128}).status_code == 403


def test_front_settings_followers_and_changes_use_front_store(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        plain = add_user(caller, 'plain', 'plain', by=owner)
        caller.front.state.console_settings.enabled = True
        def follower():
            caller.users.groups.put('settings-follower', {'can': ['follow-settings']})
            caller.users.add('follower', 'settings-follower')
            return {'authorization': 'Bearer ' + caller.users.new_key('follower')}
        follow = caller.portal.call(follower)
        settings = {'timezone': {'value': 'UTC'}, 'alert_gpu_temperature': {'value': 85}}
        assert caller.post('/loadcoach/settings/weightroom/seen', headers=follow, json={'told': settings}).status_code == 200
        assert caller.post('/loadcoach/settings/weightroom', headers=plain, json={'timezone': 'America/Vancouver'}).status_code == 403
        assert caller.post('/loadcoach/settings/weightroom', headers=owner, json={'timezone': 'America/Vancouver', 'alert_gpu_temperature': 200}).status_code == 400
        assert caller.get('/loadcoach/settings/weightroom/changes', headers=owner).json() == {}
        assert caller.post('/loadcoach/settings/weightroom', headers=owner, json={'timezone': 'America/Vancouver'}).status_code == 200
        assert caller.get('/loadcoach/settings/weightroom/changes', headers=owner).json() == {'timezone': 'America/Vancouver'}
        assert caller.post('/loadcoach/settings/weightroom', headers=owner, json={'database': 'other.db'}).status_code == 403
        with sqlite3.connect(tmp_path / 'compute.db') as compute:
            assert compute.execute("SELECT COUNT(*) FROM settings_changed WHERE program='weightroom'").fetchone() == (0,)


def test_front_scheduled_benchmark_uses_front_grant_and_task(fake_llama_server, fake_cards, tmp_path, monkeypatch):
    from test_schedules import spec, NOW
    from loadcoach.schedule_time import next_at, occurrence_ids
    import turnstile.tasks
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        identity = caller.get('/v1/models', headers=owner).json()['data'][0]['model_id']
        value = spec(kind='benchmark', model_id=identity, benchmark='context')
        identifier, token = 'a' * 32, secrets.token_urlsafe(32)
        monkeypatch.setattr(turnstile.tasks.time, 'time', lambda: NOW)
        response = caller.post('/loadcoach/schedules', headers=owner, json={'id': identifier, 'token': token, 'spec': value})
        assert response.status_code == 201, response.text
        due = next_at(value, NOW)
        monkeypatch.setattr(turnstile.tasks.time, 'time', lambda: due + 1)
        headers = {'x-suite-schedule': token}
        payload = {'due_at': due, 'task_token': secrets.token_urlsafe(32)}
        for _ in range(2):
            dispatched = caller.post('/loadcoach/schedules/' + identifier + '/dispatch', headers=headers, json=payload)
            assert dispatched.status_code in (200, 201), dispatched.text
            assert dispatched.json()['id'] == occurrence_ids(identifier, due)[0]
        with sqlite3.connect(tmp_path / 'compute.db') as compute:
            assert compute.execute('SELECT COUNT(*) FROM benchmark_tasks').fetchone() == (0,)
            assert compute.execute('SELECT COUNT(*) FROM schedule_grants').fetchone() == (0,)


def test_fresh_stateless_compute_runs_signed_probes_without_legacy_tables(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path, stateless=True) as caller:
        owner = add_user(caller, 'owner', 'admin')
        intent = create(caller, owner)
        path = '/loadcoach/tasks/' + intent['id']
        scoped = {'x-loadcoach-task': intent['token']}
        result = caller.post(path + '/probe', headers=scoped, json={'context': 128})
        assert result.status_code == 200, result.text
        assert caller.post(path + '/finish', headers=scoped, json={'state': 'cancelled'}).status_code == 200
        assert caller.get('/loadcoach/status', headers=owner).status_code == 200
        assert OnCompute(caller).post('/loadcoach/schedules/' + 'a' * 32 + '/dispatch', headers={'x-suite-schedule': 'old-grant-token'}, json={}).status_code == 410
        with sqlite3.connect(tmp_path / 'compute.db') as compute:
            tables = {name for name, in compute.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not tables.intersection({'users','groups','keys','roles','role_evidence','reliability_outcomes','benchmark_tasks','batches','batch_lines','files','benchmark_runs','services'})
            assert {'events','forwarded','file_hashes','schedule_grants','models'} <= tables


def test_front_probe_interrupt_finishes_cleanup_before_retry(fake_llama_server, fake_cards, tmp_path, monkeypatch):
    from loadcoach.probes import Probes
    entered = threading.Event()
    original = Probes.sample
    async def slow(self, task, options, kept=None):
        entered.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(Probes, 'sample', slow)
    with task_gateway(fake_llama_server, fake_cards, tmp_path, stateless=True) as caller, ThreadPoolExecutor() as pool:
        owner = add_user(caller, 'owner', 'admin')
        intent = create(caller, owner)
        path = '/loadcoach/tasks/' + intent['id']
        scoped = {'x-loadcoach-task': intent['token'], 'x-loadcoach-probe-id': 'a' * 64}
        pending = pool.submit(caller.post, path + '/probe', headers=scoped, json={'context': 128})
        assert entered.wait(3)
        assert caller.post(path + '/probe', headers=scoped, json={'context': 128}).status_code == 409
        assert caller.post(path + '/interrupt', headers=scoped, json={}).status_code == 200
        assert pending.result(timeout=3).status_code == 409
        monkeypatch.setattr(Probes, 'sample', original)
        retry = caller.post(path + '/probe', headers=scoped, json={'context': 128})
        assert retry.status_code == 200, retry.text


def test_front_evidence_is_frozen_and_recovers_publication_after_finish_commit(fake_llama_server, fake_cards, tmp_path, monkeypatch):
    from loadcoach.role_evidence import RoleEvidence
    with task_gateway(fake_llama_server, fake_cards, tmp_path, stateless=True) as caller:
        owner = add_user(caller, 'owner', 'admin')
        manager = add_user(caller, 'manager', 'admin', by=owner)
        intent = {'id': 'batch_' + secrets.token_hex(12), 'token': secrets.token_urlsafe(32), 'model': 'small',
                  'metadata': {'benchmark': 'context', 'benchmark_version': 1}}
        for _ in range(100):
            created = caller.post('/loadcoach/tasks', headers=owner, json=intent)
            if created.status_code == 201:
                break
            assert 'fingerprinted' in created.text, created.text
            time.sleep(.02)
        assert created.status_code == 201, created.text
        task = created.json()
        definition = {'id': 'context', 'name': 'Context', 'version': 1, 'formula': 'max_context_tokens', 'direction': 'high'}
        result = {'model_id': task['model_id'], 'fingerprint': task['fingerprint'], 'benchmark': 'context', 'run': task['id'],
                  'measured_at': time.time(), 'score': 128, 'formula': 'max_context_tokens', 'version': 1,
                  'engine_hash': task['environment']['engine_hash'], 'machine_hash': task['environment']['machine_hash']}
        snapshot = {'definitions': [definition], 'results': [result]}
        path = '/loadcoach/tasks/' + task['id']
        scoped = {'x-loadcoach-task': intent['token']}
        invalid = {'definitions': [definition], 'results': [{**result, 'version': 2}]}
        assert caller.post(path + '/finish', headers=scoped, json={'state': 'completed', 'role_evidence': invalid}).status_code == 400
        assert caller.get(path, headers=scoped).json()['status'] == 'in_progress'
        original = RoleEvidence.accept
        def unavailable(*args, **kwargs):
            raise RuntimeError('injected publication interruption')
        monkeypatch.setattr(RoleEvidence, 'accept', unavailable)
        finish = {'state': 'completed', 'result': {'ok': True}, 'role_evidence': snapshot}
        interrupted = caller.post(path + '/finish', headers=scoped, json=finish)
        assert interrupted.status_code == 500 and interrupted.headers['x-request-id']
        assert 'injected' not in interrupted.text
        assert caller.get(path, headers=scoped).json()['status'] == 'completed'
        monkeypatch.setattr(RoleEvidence, 'accept', original)
        assert caller.post(path + '/finish', headers=scoped, json=finish).status_code == 200
        async def saved():
            front = caller.front.state.task_records
            return (front.batches.db.execute('SELECT published FROM benchmark_task_evidence').fetchone(),
                    caller.front.state.routing.evidence.db.execute('SELECT COUNT(*) FROM role_evidence').fetchone())
        assert caller.portal.call(saved) == ((1,), (1,))
        second = {**intent, 'id': 'batch_' + secrets.token_hex(12), 'token': secrets.token_urlsafe(32)}
        created2 = caller.post('/loadcoach/tasks', headers=owner, json=second)
        assert created2.status_code == 201, created2.text
        second_snapshot = json.loads(json.dumps(snapshot))
        second_snapshot['results'][0].update(run=second['id'], measured_at=time.time())
        second_finish = {'state': 'completed', 'result': {'ok': True}, 'role_evidence': second_snapshot}
        second_path = '/loadcoach/tasks/' + second['id']
        second_scoped = {'x-loadcoach-task': second['token']}
        monkeypatch.setattr(RoleEvidence, 'accept', unavailable)
        interrupted = caller.post(second_path + '/finish', headers=second_scoped, json=second_finish)
        assert interrupted.status_code == 500 and interrupted.headers['x-request-id']
        monkeypatch.setattr(RoleEvidence, 'accept', original)
        assert caller.post('/loadcoach/users/owner', headers=manager, json={'own': {'remove': ['benchmark']}}).status_code == 200
        assert caller.post(second_path + '/finish', headers=second_scoped, json=second_finish).status_code == 200
        assert caller.post('/loadcoach/users/owner', headers=manager, json={'own': {}}).status_code == 200
        caller.portal.call(caller.front.state.task_records.publish_pending)
        async def retained_run():
            return json.loads(caller.front.state.routing.evidence.db.execute('SELECT evidence FROM role_evidence').fetchone()[0])['run']
        assert caller.portal.call(retained_run) == task['id']


def test_two_machine_selection_places_on_the_named_copy_and_refuses_wrong_weights_outage_and_disallowed_choice(tmp_path):
    import httpx
    from starlette.requests import Request
    from loadcoach.batches import Batches
    from loadcoach.log import Log
    from loadcoach.role_evidence import RoleEvidence
    from loadcoach.users import Users
    from loadcoach.groups import read_group
    from turnstile.tasks import TaskRecords
    source = str(tmp_path / 'front.db')
    users = Users(source, {'admin': read_group('admin', {'can': ['superuser']})})
    users.add('owner', 'admin')
    log, batches = Log(source), Batches(source)
    hashes = {'compute': 'a' * 64, 'rig': 'a' * 64}
    online = {'compute': True, 'rig': True}
    calls = []
    async def ask(user, method, path, *args, machine='compute'):
        calls.append((machine, path))
        if not online[machine]:
            return None
        if path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'small', 'owned_by': 'loadcoach', 'model_id': machine + '-id',
                                                     'content_sha256': hashes[machine], 'fingerprint': 'fp'}]})
        if path.startswith('/loadcoach/probe-subject/'):
            return httpx.Response(200, json={'fingerprint': 'fp', 'subject': {'model_id': machine + '-id', 'settings': {'flags': {}}}, 'front_probe_dispatch': 1})
        if path == '/loadcoach/probe-environment':
            return httpx.Response(200, json={'engine_hash': 'engine', 'machine_hash': machine})
        return httpx.Response(404, json={})
    async def dispatch(*args):
        if args[1].endswith('/interrupt'):
            return httpx.Response(200, json={'ok': True})
        raise AssertionError('An unavailable pinned machine must never dispatch elsewhere')
    allowed = [True]  # Settings → General, 'Allow callers to choose the node'
    tasks = TaskRecords(batches, users, log, RoleEvidence(log.db), ask, dispatch, ['compute', 'rig'],
                        destination_allowed=lambda: allowed[0])
    intent = {'id': 'batch_' + secrets.token_hex(12), 'token': secrets.token_urlsafe(32), 'model': 'small', 'model_id': 'rig-id'}
    def request(method, path, body=None):
        async def receive():
            return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
        return Request({'type': 'http', 'method': method, 'path': path, 'query_string': b'',
                        'headers': [(b'x-loadcoach-task', intent['token'].encode())]}, receive)
    async def run():
        hashes['rig'] = 'b' * 64
        rejected = await tasks.create(request('POST', '/loadcoach/tasks', intent), users.by_name('owner'))
        assert rejected.status_code == 400 and b'weights' in rejected.body
        hashes['rig'] = hashes['compute']
        allowed[0] = False
        pinned = await tasks.create(request('POST', '/loadcoach/tasks', {**intent, 'machine': 'rig'}), users.by_name('owner'))
        assert pinned.status_code == 400 and 'Settings → General' in json.loads(pinned.body)['error']['message']
        assert tasks.get(intent['id']) is None
        # Without a node named, the copy the model ID points at is used, whatever the setting.
        made = await tasks.create(request('POST', '/loadcoach/tasks', intent), users.by_name('owner'))
        assert made.status_code == 201, made.body
        assert tasks.get(intent['id'])['machine'] == 'rig'
        allowed[0] = True
        online['rig'] = False
        calls.clear()
        response = await tasks.handle(request('GET', '/loadcoach/tasks/' + intent['id']), None)
        assert response.status_code == 503
        assert calls and {machine for machine, _ in calls} == {'rig'}
        online['rig'] = True
        assert (await tasks.finish(request('POST', '', {'state': 'cancelled'}), tasks.get(intent['id']))).status_code == 200
        online['rig'] = False
        calls.clear()
        history = await tasks.handle(request('GET', '/loadcoach/tasks/' + intent['id']), None)
        assert history.status_code == 200 and calls == []
    asyncio.run(run())
    batches.db.close(); log.db.close(); users.db.close()


def test_interrupted_front_creation_rolls_back_file_batch_line_and_token(fake_llama_server, fake_cards, tmp_path, monkeypatch):
    from db.connection import Connection
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        # Wait for the ordinary model-file fingerprint before injecting the write fault.
        seed = create(caller, owner)
        front = caller.front.state.task_records
        original = Connection.execute
        def broken(self, sql, args=()):
            if self is front.batches.db and sql.startswith('INSERT INTO benchmark_tasks'):
                raise RuntimeError('injected before task ownership commit')
            return original(self, sql, args)
        intent = {**seed, 'id': 'batch_' + secrets.token_hex(12), 'token': secrets.token_urlsafe(32)}
        with monkeypatch.context() as patch:
            patch.setattr(Connection, 'execute', broken)
            interrupted = caller.post('/loadcoach/tasks', headers=owner, json=intent)
            assert interrupted.status_code == 500 and interrupted.headers['x-request-id']
            assert 'injected' not in interrupted.text
        async def counts():
            return [front.batches.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('benchmark_tasks','batches','batch_lines','files')]
        assert caller.portal.call(counts) == [1, 1, 1, 1]
        assert caller.post('/loadcoach/tasks', headers=owner, json=intent).status_code == 201


def test_terminal_legacy_import_deduplicates_lists_and_recreated_username_cannot_inherit(fake_llama_server, fake_cards, tmp_path):
    from db.consolidation import readonly
    from db.consolidation_import import tasks as import_tasks
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        manager = add_user(caller, 'manager', 'admin', by=owner)
        intent = legacy_task(caller, 'owner')  # kept on the node, from before the front kept every task
        path = '/loadcoach/tasks/' + intent['id']
        scoped = {'x-loadcoach-task': intent['token']}
        assert caller.post(path + '/finish', headers=scoped, json={'state': 'completed', 'result': {'ok': True}}).status_code == 200
        snapshot = tmp_path / 'snapshot.db'
        old = sqlite3.connect((tmp_path / 'compute.db').as_uri() + '?mode=ro', uri=True)
        new = sqlite3.connect(snapshot)
        old.backup(new); old.close(); new.close()
        async def move():
            with readonly(snapshot) as source:
                return import_tasks(source, caller.front.state.task_records.batches.db, apply=True)
        assert caller.portal.call(move)['tables']['benchmark_tasks']['missing'] == 1
        listed = caller.get('/v1/batches', headers=owner).json()['data']
        assert [one['id'] for one in listed].count(intent['id']) == 1
        batch = caller.get('/v1/batches/' + intent['id'], headers=owner).json()
        file = batch['input_file_id']
        assert caller.get('/v1/files/' + file + '/content', headers=owner).status_code == 200
        assert caller.get(path, headers=owner).status_code == 200
        assert caller.delete('/loadcoach/users/owner', headers=manager).status_code == 200
        replacement = add_user(caller, 'owner', 'admin', by=manager)
        assert caller.get(path, headers=replacement).status_code == 404
        assert caller.get('/v1/batches/' + intent['id'], headers=replacement).status_code == 404
        assert caller.get('/v1/files/' + file + '/content', headers=replacement).status_code == 404
        assert caller.get('/v1/batches', headers=replacement).json()['data'] == []
        assert caller.get('/v1/files', headers=replacement).json()['data'] == []
