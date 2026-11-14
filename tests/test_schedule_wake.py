"""Due schedule wake/retry through real control node grants and signed, simulated compute nodes."""
import asyncio
import secrets
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from benchmarks.store import Benchmarks
from db.maintenance import Gate
from front_helpers import machine_key
from loadcoach import statement
from loadcoach.users import Users
from test_weightroom import GROUPS
from turnstile.app import Settings, build_app
from weightroom.schedules import Schedules, ScheduleWorker


@pytest.fixture
def sleeping_nodes(tmp_path):
    key = machine_key(tmp_path)
    rig_key = tmp_path/'rig.key'
    rig_key.write_text(secrets.token_hex(32))
    online = {'compute': True, 'rig': True}
    calls, wakes, backups = [], [], {}
    wake_failure = []
    before_unavailable = []

    async def answer(request):
        node, path = request.url.hostname, request.url.path
        if path == '/loadcoach/status':
            return JSONResponse({})
        if path == '/v1/models':
            return JSONResponse({'data': [dict(id='small', owned_by='loadcoach', model_id=node+'-id',
                                               content_sha256='a'*64, fingerprint='fp', engine_id='llama_cpp')]})
        if path.startswith('/loadcoach/probe-subject/'):
            return JSONResponse({'front_probe_dispatch': 1, 'fingerprint': 'fp',
                                 'subject': {'model_id': node+'-id', 'settings': {'flags': {}}, 'values': {}}})
        if path == '/loadcoach/probe-environment':
            return JSONResponse({'engine_hash': 'engine', 'machine_hash': node})
        if path == '/loadcoach/backups' and request.method == 'POST':
            body = await request.json()
            backups[body['id']] = dict(body, verification={'state': 'verified'})
            return JSONResponse(backups[body['id']])
        if path.startswith('/loadcoach/backups/') and path.rsplit('/',1)[-1] in backups:
            return JSONResponse(backups[path.rsplit('/',1)[-1]])
        return JSONResponse({}, status_code=404)

    compute = Starlette(routes=[Route('/{path:path}', answer, methods=['GET','POST'])])
    transports = {name: httpx.ASGITransport(app=statement.Statements(compute, file))
                  for name,file in [('compute',key),('rig',str(rig_key))]}

    async def wire(request):
        calls.append((request.url.host, request.method, request.url.path))
        if not online[request.url.host]:
            if request.url.path == '/loadcoach/status':
                for callback in before_unavailable:
                    callback()
            raise httpx.ConnectError('Node is asleep', request=request)
        return await transports[request.url.host].handle_async_request(request)

    users = Users(tmp_path/'front.db', GROUPS)
    users.add('owner','admin')
    users.change('owner', own={'add':['front-wake']})
    settings = Settings(compute='compute', data=str(tmp_path), machine_key_file=key,
                        wake_mac='00:11:22:33:44:55', machines={'rig': dict(compute='http://rig:8442',
                        machine_key_file=str(rig_key), wake_mac='00:11:22:33:44:66', wake_address='10.0.0.255')})
    def wake_node(*args):
        if wake_failure:
            raise OSError('Wake packet could not be sent')
        wakes.append(args)
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(wire)), wake_node, users=users)
    with TestClient(app, base_url='http://front:8442') as client:
        headers = {'authorization': 'Bearer '+users.new_key('owner')}
        yield SimpleNamespace(client=client, app=app, users=users, headers=headers, online=online,
                              calls=calls, wakes=wakes, backups=backups, settings=settings, wake_failure=wake_failure,
                              before_unavailable=before_unavailable)


def authorize(nodes, kind='backup', machine='rig'):
    due = datetime.now(ZoneInfo('UTC')).replace(second=0, microsecond=0).timestamp()
    value = dict(kind=kind, model_id='rig-id' if kind == 'benchmark' else '',
                 benchmark='load' if kind == 'benchmark' else '', time=datetime.fromtimestamp(due, ZoneInfo('UTC')).strftime('%H:%M'),
                 timezone='UTC', weekdays=list(range(7)), retention=10, missed='skip')
    if kind != 'backup':
        value['machine'] = machine
    if kind == 'outstanding':
        value.update(model_ids=['rig-id'], benchmarks=['load'])
    identifier, token = secrets.token_hex(16), secrets.token_urlsafe(32)
    response = nodes.client.post('/loadcoach/schedules', headers=nodes.headers,
                                 json=dict(id=identifier, token=token, spec=value))
    assert response.status_code == 201, response.text
    with nodes.app.state.task_records.batches.db:
        nodes.app.state.task_records.batches.db.execute('UPDATE schedule_grants SET valid_from=? WHERE id=?', (due-1,identifier))
    return identifier, token, value, due


@pytest.mark.parametrize('kind', ['backup','benchmark','outstanding'])
def test_due_jobs_wake_only_their_node_then_submit_once(sleeping_nodes, tmp_path, kind):
    nodes = sleeping_nodes
    identifier, token, value, due = authorize(nodes, kind)
    target = 'compute' if kind == 'backup' else 'rig'
    nodes.online[target] = False
    store = Schedules(tmp_path/'schedules.db')
    store.create(identifier, 'Scheduled work', nodes.users.identity('owner'), 'owner', value, token, due-60)
    bench = Benchmarks(tmp_path/'benchmarks.db')
    front_backups = []

    async def backup(number, retention):
        front_backups.append((number,retention))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=nodes.app), base_url='http://front:8442') as client:
            gate = Gate()
            worker = ScheduleWorker(store, client, bench, backup, gate, asyncio.Event())
            # A future slot and a maintenance barrier do not wake hardware.
            await worker.tick(due-1)
            assert not nodes.wakes
            gate.path = tmp_path/'maintenance.json'
            gate.path.write_text('{"token":"maintenance"}')
            await worker.tick(due)
            assert not nodes.wakes and not store.occurrences()
            gate.path.unlink()
            await worker.tick(due)
            assert store.occurrences()[0]['state'] == 'dispatching', store.occurrences()[0]['message']
            assert 'Asked '+target+' to wake' in store.occurrences()[0]['message']
            assert len(nodes.wakes) == 1
            assert nodes.wakes[0][0] == ('00:11:22:33:44:55' if target == 'compute' else '00:11:22:33:44:66')
            assert not front_backups and not nodes.backups and not bench.listing()
            occurrence = store.occurrences()[0]
            await worker.tick(due+1)
            assert len(nodes.wakes) == 1  # concurrent/retried due work shares the wake cooldown
            nodes.online[target] = True
            await worker.tick(due+2)
            result = store.occurrences()[0]
            assert result['id'] == occurrence['id'] and result['task_token'] == occurrence['task_token']
            assert result['state'] == ('completed' if kind == 'backup' else 'running')
            if kind == 'backup':
                assert len(front_backups) == len(nodes.backups) == 1
                assert len([call for call in nodes.calls if call[1:] == ('POST','/loadcoach/backups')]) == 1
            else:
                assert bench.get(result['id'])['location'] == 'rig'
                assert bench.state_of(result['id']) == 'queued'
                # Returning an accepted task after a lost reply never wakes its sleeping node again.
                nodes.online[target] = False
                response = await client.post('/loadcoach/schedules/'+identifier+'/dispatch', headers={'x-suite-schedule': token},
                                             json={'due_at':due,'task_token':occurrence['task_token']})
                assert response.status_code == 200, response.text
                assert response.json()['id'] == occurrence['id']
                assert len(nodes.wakes) == 1
            await worker.tick(due+3)
            assert len(store.occurrences()) == 1 and len(nodes.wakes) == 1
    try:
        asyncio.run(run())
    finally:
        store.db.close(); bench.db.close()


@pytest.mark.parametrize('case', ['wrong-token','future','not-due','paused','disabled-owner','no-wake-permission','disabled-node','no-mac','no-wake-header','wake-failure'])
def test_wake_respects_due_grant_owner_node_and_request_policy(sleeping_nodes, case):
    nodes = sleeping_nodes
    identifier, token, _, due = authorize(nodes,'benchmark' if case == 'no-mac' else 'backup')
    headers, payload = {'x-suite-schedule': token}, {'due_at':due,'task_token':secrets.token_urlsafe(32)}
    nodes.online['compute'] = False
    expected = 503
    if case == 'wrong-token': headers['x-suite-schedule'] = 'wrong'; expected = 403
    elif case == 'future': payload['due_at'] += 86400; expected = 400
    elif case == 'not-due': payload['due_at'] -= 1; expected = 400
    elif case == 'paused':
        nodes.app.state.task_records.grants.enable(identifier, False); expected = 403
    elif case == 'disabled-owner': nodes.users.change('owner', enabled=False); expected = 403
    elif case == 'no-wake-permission': nodes.users.change('owner', own={'remove':['front-wake']}); expected = 403
    elif case == 'disabled-node': nodes.app.state.machines.change_state('compute','disabled','owner'); expected = 409
    elif case == 'no-mac':
        nodes.settings.machines['rig']['wake_mac'] = ''
        nodes.online['rig'] = False
    elif case == 'no-wake-header': headers['x-turnstile-no-wake'] = '1'
    elif case == 'wake-failure': nodes.wake_failure.append(True)
    response = nodes.client.post('/loadcoach/schedules/'+identifier+'/dispatch', headers=headers, json=payload)
    assert response.status_code == expected, response.text
    assert not nodes.wakes and not nodes.backups


def test_passive_inspection_does_not_wake_but_due_inspection_waits(sleeping_nodes):
    nodes = sleeping_nodes
    identifier, token, _, due = authorize(nodes,'outstanding')
    nodes.online['rig'] = False
    url, headers = '/loadcoach/schedules/'+identifier+'/inspect', {'x-suite-schedule':token}
    assert nodes.client.post(url, headers=headers, json={}).status_code == 200
    assert not nodes.wakes
    waiting = nodes.client.post(url, headers=headers, json={'due_at':due})
    assert waiting.status_code == 503 and 'waiting for the node' in waiting.text
    assert len(nodes.wakes) == 1


def test_revoked_outstanding_permission_fails_and_pauses_without_wake(sleeping_nodes, tmp_path):
    nodes = sleeping_nodes
    identifier, token, value, due = authorize(nodes,'outstanding')
    store = Schedules(tmp_path/'schedules.db')
    store.create(identifier,'Due work',nodes.users.identity('owner'),'owner',value,token,due-60)
    bench = Benchmarks(tmp_path/'benchmarks.db')
    nodes.online['rig'] = False
    nodes.users.change('owner',own={'remove':['benchmark']})
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=nodes.app),base_url='http://front:8442') as client:
            await ScheduleWorker(store,client,bench,None,Gate(),asyncio.Event()).tick(due)
    try:
        asyncio.run(run())
        assert not nodes.wakes and not store.rows()[0]['enabled']
        assert store.occurrences()[0]['state'] == 'failed'
        assert 'HTTP 403' in store.occurrences()[0]['message']
    finally:
        store.db.close(); bench.db.close()


@pytest.mark.parametrize('change', ['node-disabled','grant-paused','token-rotated'])
def test_changes_during_readiness_probe_cancel_wake(sleeping_nodes, change):
    nodes = sleeping_nodes
    identifier, token, value, due = authorize(nodes)
    nodes.online['compute'] = False
    grants = nodes.app.state.task_records.grants
    if change == 'node-disabled':
        nodes.before_unavailable.append(lambda: nodes.app.state.machines.change_state('compute','disabled','owner'))
    elif change == 'grant-paused':
        nodes.before_unavailable.append(lambda: grants.enable(identifier,False))
    else:
        nodes.before_unavailable.append(lambda: grants.replace(identifier,secrets.token_urlsafe(32),value,nodes.users.by_name('owner')))
    response = nodes.client.post('/loadcoach/schedules/'+identifier+'/dispatch', headers={'x-suite-schedule':token},
                                 json={'due_at':due,'task_token':secrets.token_urlsafe(32)})
    assert response.status_code == (409 if change == 'node-disabled' else 403), response.text
    assert not nodes.wakes
