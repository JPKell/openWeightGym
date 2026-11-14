"""Calendar, durable dispatch, authority, maintenance and UI with offline/mock hardware."""
import asyncio
import json
import secrets
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.testclient import TestClient

from benchmarks.store import Benchmarks
from db.maintenance import Gate
from loadcoach.schedule_time import is_due, next_at, occurrence_ids, validate
from loadcoach.schedule_grants import ScheduleGrants
from loadcoach.app import build_app
from loadcoach.config import Config
from loadcoach.models import Models
from loadcoach.users import Users
from weightroom.schedules import Schedules, ScheduleWorker
from test_weightroom import GROUPS, desk, sign_in  # noqa: F401
from front_helpers import front_of
from sqlite3 import connect


ZONE = ZoneInfo('America/Vancouver')
NOW = datetime(2026, 10, 13, 9, tzinfo=ZONE).timestamp()


def spec(**changes):
    return {'kind': 'backup', 'model_id': '', 'benchmark': '', 'timezone': 'America/Vancouver',
            'time': '10:00', 'weekdays': list(range(7)), 'missed': 'skip', 'retention': 10, **changes}


def seeded(tmp_path, **changes):
    store = Schedules(tmp_path/'schedules.db')
    store.create('a'*32, 'Daily', 'stable-owner', 'owner', spec(**changes), 'secret-schedule-token', NOW)
    return store


def test_calendar_dst_gap_fold_weekdays_and_invalid_specs():
    spring = datetime(2026, 3, 8, 0, tzinfo=ZONE).timestamp()
    assert datetime.fromtimestamp(next_at(spec(time='02:30'), spring), ZONE).date().isoformat() == '2026-03-09'
    before_gap = datetime(2026, 3, 1, 2, 30, tzinfo=ZONE).timestamp()
    assert datetime.fromtimestamp(next_at(spec(time='02:30', weekdays=[6]), before_gap), ZONE).date().isoformat() == '2026-03-15'
    autumn = datetime(2026, 11, 1, 0, tzinfo=ZONE).timestamp()
    first = next_at(spec(time='01:30'), autumn)
    assert datetime.fromtimestamp(first, UTC).isoformat() == '2026-11-01T08:30:00+00:00'
    assert datetime.fromtimestamp(next_at(spec(time='01:30'), first), ZONE).date().isoformat() == '2026-11-02'
    assert datetime.fromtimestamp(next_at(spec(weekdays=[0]), NOW), ZONE).weekday() == 0
    for changes in ({'weekdays': []}, {'weekdays': [True]}, {'weekdays': [[1]]}, {'missed': 'burst'},
                    {'time': '25:00'}, {'time': None}, {'timezone': []}, {'timezone': 'Unknown/Zone'},
                    {'retention': -1}, {'kind': 'shell'}):
        with pytest.raises(ValueError):
            validate(spec(**changes))
    for invalid in (float('nan'), float('inf'), float('-inf'), 10**1000):
        assert not is_due(spec(), invalid)
    assert is_due(spec(), NOW+3600) and not is_due(spec(), NOW+3601)


def test_claim_restart_dedup_overlap_and_pause(tmp_path):
    store = seeded(tmp_path)
    due = store.rows()[0]['next_at']
    store.claim(due)
    one = store.occurrences()[0]
    assert one['state'] == 'dispatching'
    store.db.close()
    store = Schedules(tmp_path/'schedules.db')
    store.claim(due+1)
    assert len(store.occurrences()) == 1 and store.occurrences()[0]['task_token'] == one['task_token']
    store.claim(due+86400)
    assert store.occurrences()[0]['state'] == 'skipped' and 'previous' in store.occurrences()[0]['message']
    store.enable('a'*32, False, due+86400)
    assert any(row['state'] == 'dispatching' for row in store.occurrences())  # claimed work survives pause
    store.claim(due+3*86400)
    assert len(store.occurrences()) == 2
    store.enable('a'*32, True, due+3*86400)
    assert store.rows()[0]['next_at'] > due+3*86400
    store.db.close()


@pytest.mark.parametrize('policy', ['skip', 'latest'])
def test_missed_occurrences_are_recorded_without_burst(tmp_path, policy):
    store = seeded(tmp_path, missed=policy)
    due = store.rows()[0]['next_at']
    store.claim(due+3*86400+901)
    rows = store.occurrences()
    assert len(rows) == 4
    assert sum(row['state'] == 'dispatching' for row in rows) == (1 if policy == 'latest' else 0)
    assert len({row['id'] for row in rows}) == 4
    store.db.close()


def test_claim_compare_and_set_across_connections(tmp_path):
    first = seeded(tmp_path)
    second = Schedules(tmp_path/'schedules.db')
    due = first.rows()[0]['next_at']
    first.claim(due)
    second.claim(due)
    assert len(first.occurrences()) == 1
    assert (tmp_path/'schedules.db').stat().st_mode & 0o077 == 0
    first.db.close(); second.db.close()


def test_schedule_edits_preserve_identity_and_delete_preserves_history(tmp_path):
    store = seeded(tmp_path)
    row = store.rows()[0]
    store.enable(row['id'], False, NOW)
    edited = spec(time='11:00', weekdays=[1, 3], missed='latest', retention=4)
    store.update(row['id'], 'Changed', edited, 'new-scoped-token', now=NOW)
    current = store.rows()[0]
    assert current['id'] == row['id'] and current['owner_id'] == row['owner_id']
    assert not current['enabled'] and current['spec'] == edited
    assert current['next_at'] == next_at(edited, NOW)
    store.enable(row['id'], True, NOW)
    due = store.rows()[0]['next_at']
    store.claim(due)
    with pytest.raises(ValueError, match='claimed occurrence'):
        store.update(row['id'], 'Unsafe change', spec(), 'another-token', now=due)
    assert store.rows()[0]['name'] == 'Changed'
    store.delete(row['id'], due+1)
    assert not store.rows() and store.rows(include_deleted=True)[0]['name'] == 'Changed'
    assert store.rows(include_deleted=True)[0]['grant_token'] == ''
    assert store.occurrences()[0]['state'] == 'cancelled'
    store.claim(due+86400)
    assert len(store.occurrences()) == 1
    with pytest.raises(ValueError, match='No such schedule'):
        store.enable(row['id'], True)
    store.db.close()


def test_existing_schedule_schema_migrates_without_losing_saved_work(tmp_path):
    from db import database, migrate
    from db.schemas import SCHEDULES
    target = database(tmp_path/'schedules.db')
    connection = target.connect()
    migrate(target, connection, 'weightroom.schedules', SCHEDULES[:1])
    with connection:
        connection.execute('INSERT INTO recurring_schedules VALUES(?,?,?,?,?,?,?,?,?)',
            ('a'*32, 'Existing', 'stable-owner', 'owner', '', json.dumps(spec()), 'saved-token', 1, next_at(spec(), NOW)))
    connection.close()
    store = Schedules(tmp_path/'schedules.db')
    assert store.rows()[0]['name'] == 'Existing' and store.rows()[0]['grant_token'] == 'saved-token'
    store.delete('a'*32)
    store.db.close()
    reopened = Schedules(tmp_path/'schedules.db')
    assert not reopened.rows() and reopened.rows(include_deleted=True)[0]['name'] == 'Existing'
    reopened.db.close()


def test_delayed_scheduled_backup_retention_uses_creation_time(tmp_path):
    from loadcoach.backups import BACKUP_ID, rotate
    from loadcoach.backup_crypto import ARCHIVE
    _, late = occurrence_ids('a'*32, NOW-20*86400)
    assert BACKUP_ID.fullmatch(late) and len(late.split('-')[1]) == 24
    older='20261013T120000Z-abcdef'
    for identifier, created in ((older,'2026-10-13T12:00:00+00:00'), (late,'2026-10-13T13:00:00+00:00')):
        folder=tmp_path/identifier; folder.mkdir()
        (folder/'manifest.json').write_text(json.dumps({'created_at':created}))
        (folder/ARCHIVE).write_bytes(b'fixture ciphertext')
    assert rotate(tmp_path,1) == [older]
    assert (tmp_path/late).is_dir()


def test_scoped_grants_owner_identity_permissions_and_revocation(tmp_path):
    users = Users(tmp_path/'core.db', GROUPS)
    users.add('owner', 'admin')
    grants = ScheduleGrants(users.db, users)
    owner = users.by_name('owner')
    grants.put('a'*32, 'x'*32, spec(), owner)
    users.rename('owner', 'renamed')
    assert grants.authorized('a'*32, 'x'*32)[0].name == 'renamed'
    with pytest.raises(PermissionError):
        grants.authorized('a'*32, 'wrong')
    grants.enable('a'*32, False)
    with pytest.raises(PermissionError):
        grants.authorized('a'*32, 'x'*32)
    grants.enable('a'*32, True)
    users.change('renamed', enabled=False)
    with pytest.raises(PermissionError):
        grants.authorized('a'*32, 'x'*32)
    users.remove('renamed'); users.add('renamed', 'admin')
    with pytest.raises(PermissionError):
        grants.authorized('a'*32, 'x'*32)
    users.db.close()


def test_grant_edit_rechecks_owner_rotates_scope_and_keeps_paused_state(tmp_path):
    users = Users(tmp_path/'core.db', GROUPS)
    users.add('owner', 'admin'); users.add('other', 'admin')
    grants = ScheduleGrants(users.db, users)
    grants.put('a'*32, 'x'*32, spec(), users.by_name('owner'))
    with pytest.raises(PermissionError, match='Only the schedule owner'):
        grants.replace('a'*32, 'y'*32, spec(time='11:00'), users.by_name('other'))
    assert grants.authorized('a'*32, 'x'*32)[1]['time'] == '10:00'
    grants.replace('a'*32, 'y'*32, spec(time='11:00'), users.by_name('owner'), enabled=False)
    for token in ('x'*32, 'y'*32):
        with pytest.raises(PermissionError):
            grants.authorized('a'*32, token)
    grants.enable('a'*32, True)
    assert grants.authorized('a'*32, 'y'*32)[1]['time'] == '11:00'
    with pytest.raises(PermissionError, match='predates'):
        grants.authorized('a'*32, 'y'*32, NOW)
    users.db.close()


def test_worker_lost_reply_reuses_ids_completes_backup_and_obeys_barrier(tmp_path):
    store = seeded(tmp_path)
    due = store.rows()[0]['next_at']
    calls, backups = [], []
    async def wire(request):
        calls.append((request.url.path, json.loads(request.content)))
        if len(calls) == 1:
            raise httpx.ConnectError('Lost reply after compute committed')
        return httpx.Response(200, json={'verification': {'state': 'verified'}})
    async def front_backup(identifier, retention):
        backups.append((identifier, retention))
    async def check():
        gate = Gate()
        bench = Benchmarks(tmp_path/'benchmarks.db')
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire), base_url='http://compute') as client:
            worker = ScheduleWorker(store, client, bench, front_backup, gate, asyncio.Event())
            await worker.tick(due)
            assert store.occurrences()[0]['state'] == 'dispatching'
            await worker.tick(due+1)
            assert store.occurrences()[0]['state'] == 'completed'
            assert calls[0] == calls[1] and len(backups) == 1 and gate.active == 0
            gate.path = tmp_path/'marker.json'; gate.path.write_text('{"token":"maintenance"}')
            await worker.tick(due+86400)
            assert len(calls) == 2 and len(store.occurrences()) == 1
        bench.db.close()
    asyncio.run(check())
    store.db.close()


def test_worker_benchmark_reuses_existing_queue_and_observes_outcome(tmp_path):
    store = seeded(tmp_path, kind='benchmark', model_id='model_a', benchmark='load', retention=0)
    due = store.rows()[0]['next_at']
    async def check():
        bench = Benchmarks(tmp_path/'benchmarks.db')
        wake = asyncio.Event()
        async def wire(request):
            return httpx.Response(201, json={'user':'owner', 'model':'renamed-model', 'model_id':'model_a',
                'fingerprint':'fp', 'environment':{}, 'subject':{'settings':{'flags':{'ctx-size':2048}},'values':{}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire), base_url='http://compute') as client:
            worker = ScheduleWorker(store, client, bench, None, Gate(), wake)
            await worker.tick(due)
            number = store.occurrences()[0]['id']
            assert store.occurrences()[0]['state'] == 'running' and wake.is_set()
            assert bench.get(number)['state'] == 'queued' and bench.get(number)['model_id'] == 'model_a'
            bench.finish(number, 'completed', 'Mock completion')
            await worker.tick(due+86400)
            assert len(store.occurrences()) == 2 and store.occurrences()[0]['state'] == 'running'
            assert store.occurrences()[1]['state'] == 'completed'
        bench.db.close()
    asyncio.run(check())
    store.db.close()


def test_revoked_authorization_records_failure_and_pauses_future_slots(tmp_path):
    store=seeded(tmp_path)
    due=store.rows()[0]['next_at']
    async def check():
        bench=Benchmarks(tmp_path/'benchmarks.db')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(403,json={})),base_url='http://compute') as client:
            await ScheduleWorker(store,client,bench,None,Gate(),asyncio.Event()).tick(due)
        assert not store.rows()[0]['enabled']
        assert store.occurrences()[0]['state'] == 'failed' and 'HTTP 403' in store.occurrences()[0]['message']
        store.claim(due+86400)
        assert len(store.occurrences()) == 1
        bench.db.close()
    asyncio.run(check())
    store.db.close()


def test_inflight_schedule_snapshot_keeps_durable_intent_across_maintenance(tmp_path):
    store = seeded(tmp_path)
    due = store.rows()[0]['next_at']
    async def check():
        gate = Gate(); gate.path = tmp_path/'marker.json'
        bench = Benchmarks(tmp_path/'benchmarks.db')
        async def wire(request):
            assert gate.active == 1
            gate.path.write_text('{"token":"maintenance"}')
            return httpx.Response(200, json={})
        async def backup(*args):
            raise AssertionError('Maintenance must defer this writer')
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire),base_url='http://compute') as client:
            await ScheduleWorker(store,client,bench,backup,gate,asyncio.Event()).tick(due)
        assert store.occurrences()[0]['state'] == 'dispatching' and gate.active == 0
        bench.db.close()
    asyncio.run(check())
    store.db.close()


def test_schedule_ui_authorization_pause_resume_csrf_and_backup(desk, tmp_path, backup_keys):
    backup_keys.configure(tmp_path / 'backups')
    backup_keys.configure(tmp_path / 'data/backups')
    browser, wire, users = desk
    core = connect(str(tmp_path / 'loadcoach.db'))  # the grants are compute's; the accounts are the control node's
    sign_in(browser, 'pat')
    assert browser.get('/schedules').status_code == 403
    assert browser.post('/schedules', data={}).status_code == 403
    sign_in(browser, 'jordan')
    data={'name':'Nightly backup','kind':'backup','timezone':'America/Vancouver','time':'23:59',
          'weekdays':['0','1','2','3','4','5','6'],'missed':'skip','retention':'10'}
    assert browser.post('/schedules',data=data,headers={'origin':'https://elsewhere.invalid'}).status_code == 403
    response = browser.post('/schedules', data=data)
    assert response.status_code == 200 and 'Nightly backup' in response.text
    store = browser.app.state.schedules
    row = store.rows()[0]
    assert row['grant_token'] not in response.text and browser.cookies['weightroom'] not in response.text
    assert browser.post('/schedules/'+row['id'],data={'do':'pause'}).status_code == 200
    assert not store.rows()[0]['enabled']
    assert browser.post('/schedules/'+row['id'],data={'do':'resume'}).status_code == 200
    assert store.rows()[0]['enabled']
    # Controlled due time in the recent past, valid for the same calendar definition.
    due = next_at(row['spec'], time.time()-86400)
    core.execute('UPDATE schedule_grants SET valid_from=? WHERE id=?',(due,row['id'])); core.commit()
    store.db.execute('UPDATE recurring_schedules SET next_at=? WHERE id=?',(due,row['id'])); store.db.commit()
    browser.portal.call(browser.app.state.schedule_worker.tick, due)
    assert store.occurrences()[0]['state'] == 'completed'
    assert 'Paired recovery set' in browser.get('/schedules').text
    assert 'weightroom.schedules' in browser.get('/backups').text


def test_schedule_ui_edit_delete_validation_and_history(desk, tmp_path):
    browser, wire, users = desk
    core = wire.front.state.task_records.batches.db  # the control node keeps the grants
    sign_in(browser, 'jordan')
    data = {'name': 'Nightly backup', 'kind': 'backup', 'timezone': 'America/Vancouver', 'time': '23:59',
            'weekdays': ['0', '3'], 'missed': 'skip', 'retention': '10'}
    assert browser.post('/schedules', data=data).status_code == 200
    store = browser.app.state.schedules
    row = store.rows()[0]
    url = '/schedules/'+row['id']
    editor = browser.get('/schedules?edit='+row['id'])
    assert editor.status_code == 200 and 'Save schedule' in editor.text
    assert 'value="Nightly backup"' in editor.text and 'value="23:59"' in editor.text
    assert 'value="0" checked' in editor.text and 'value="3" checked' in editor.text
    assert 'value="1" checked' not in editor.text
    changes = {**data, 'name': 'Weekly backup', 'time': '07:05', 'weekdays': ['1', '4'], 'missed': 'latest', 'retention': '4'}
    assert browser.post(url+'/edit', data=changes, headers={'origin': 'https://elsewhere.invalid'}).status_code == 403
    assert store.rows()[0]['name'] == row['name']
    assert browser.post(url, data={'do': 'pause'}).status_code == 200
    saved = browser.post(url+'/edit', data=changes)
    assert saved.status_code == 200 and 'Schedule saved.' in saved.text
    edited = store.rows()[0]
    assert edited['id'] == row['id'] and edited['spec']['weekdays'] == [1, 4]
    assert not edited['enabled'] and edited['grant_token'] != row['grant_token']
    assert edited['grant_token'] not in saved.text
    assert core.execute('SELECT enabled FROM schedule_grants WHERE id=?', (row['id'],)).fetchone()[0] == 0
    refused = browser.post(url+'/edit', data={**changes, 'weekdays': []})
    assert refused.status_code == 400 and 'Select at least one' in refused.text
    assert store.rows()[0]['spec'] == edited['spec']
    # Existing history keeps its name after removal from the schedule list.
    assert browser.post(url, data={'do': 'resume'}).status_code == 200
    due = store.rows()[0]['next_at']
    store.claim(due)
    blocked = browser.post(url+'/edit', data={**changes, 'time': '08:00'})
    assert blocked.status_code == 400 and 'claimed occurrence' in blocked.text
    store.finish(store.occurrences()[0]['id'], 'completed', 'Retained result.', due+1)
    assert browser.post(url, data={'do': 'delete'}, headers={'origin': 'https://elsewhere.invalid'}).status_code == 403
    assert store.rows()
    removed = browser.post(url, data={'do': 'delete'})
    assert removed.status_code == 200 and 'Schedule deleted.' in removed.text
    assert not store.rows() and 'Weekly backup' in removed.text and 'Retained result.' in removed.text
    assert core.execute('SELECT enabled FROM schedule_grants WHERE id=?', (row['id'],)).fetchone()[0] == 0
    assert browser.get('/schedules?edit='+row['id']).status_code == 404
    assert browser.post(url+'/edit', data=changes).status_code == 404
    assert browser.post(url, data={'do': 'resume'}).status_code == 404
    assert core.execute('SELECT enabled FROM schedule_grants WHERE id=?', (row['id'],)).fetchone()[0] == 0


def test_schedule_ui_edit_permissions_and_edits_while_nodes_are_off(desk):
    browser, wire, _ = desk
    sign_in(browser, 'jordan')
    data = {'name': 'Backup', 'kind': 'backup', 'timezone': 'America/Vancouver', 'time': '23:59',
            'weekdays': ['0'], 'missed': 'skip', 'retention': '10'}
    browser.post('/schedules', data=data)
    row = browser.app.state.schedules.rows()[0]
    url = '/schedules/'+row['id']
    sign_in(browser, 'pat')
    assert browser.post(url+'/edit', data=data).status_code == 403
    assert browser.post(url, data={'do': 'delete'}).status_code == 403
    sign_in(browser, 'sam')
    assert browser.get('/schedules?edit='+row['id']).status_code == 403
    assert browser.post(url+'/edit', data=data).status_code == 403
    sign_in(browser, 'jordan')
    wire.off = True  # the control node keeps the grant, so a backup schedule can be edited with every node off
    response = browser.post(url+'/edit', data={**data, 'name': 'Changed'})
    assert response.status_code == 200, response.text
    assert browser.app.state.schedules.rows()[0]['name'] == 'Changed'


def test_delete_keeps_submitted_benchmark_occurrence_and_history(tmp_path):
    store = seeded(tmp_path, kind='benchmark', model_id='stable-model', benchmark='tool_use')
    due = store.rows()[0]['next_at']
    store.claim(due)
    occurrence = store.occurrences()[0]
    with store.db:
        store.db.execute("UPDATE schedule_occurrences SET state='running' WHERE id=?", (occurrence['id'],))
    store.delete('a'*32, due+1)
    assert store.occurrences()[0]['state'] == 'running' and not store.rows()
    store.finish(occurrence['id'], 'completed', 'Benchmark completed.', due+2)
    assert store.occurrences()[0]['state'] == 'completed'
    store.db.close()


def through(tmp_path, monkeypatch, **config):
    """LoadCoach behind turnstile, as on the two machines. Compute asks control node for the accounts at every turn here, not every ten seconds."""
    monkeypatch.setattr('loadcoach.front.FRESH_FOR', 0)
    settings = Config(nvidia_smi='missing', machine_key_file=str(tmp_path/'machine.key'), front='http://front:8442', **config)
    return front_of(lambda front: build_app(settings, None, front), Users(tmp_path/'accounts.db', GROUPS), tmp_path)


def test_schedule_dispatch_is_scoped_idempotent_and_preserves_model_access(tmp_path, monkeypatch, backup_keys):
    backup_keys.configure(tmp_path / 'backups')
    caller = through(tmp_path, monkeypatch, database=str(tmp_path/'core.db')).__enter__()
    users, core = caller.users, caller.front.state.task_records.batches.db  # the control node keeps the grants
    users.add('owner','admin'); users.add('pat','plain')
    owner = {'authorization':'Bearer '+users.new_key('owner')}
    identifier, token = 'b'*32, secrets.token_urlsafe(32)
    value=spec()
    assert caller.post('/loadcoach/schedules',headers={'authorization':'Bearer '+users.new_key('pat')},json={'id':identifier,'token':token,'spec':value}).status_code == 403
    assert caller.post('/loadcoach/schedules',headers=owner,json={'id':identifier,'token':token,'spec':value}).status_code == 201
    due=next_at(value,time.time()-86400)
    path='/loadcoach/schedules/'+identifier+'/dispatch'
    assert caller.post(path,headers=owner,json={'due_at':due}).status_code == 403  # API key is not the scoped grant
    headers={'x-suite-schedule':token}
    assert caller.post(path,headers=headers,json={'due_at':due}).status_code == 403
    core.execute('UPDATE schedule_grants SET valid_from=? WHERE id=?',(due,identifier)); core.commit()
    first=caller.post(path,headers=headers,json={'due_at':due})
    assert first.status_code == 201
    assert caller.post(path,headers=headers,json={'due_at':due}).json()['id'] == first.json()['id']
    assert caller.post(path,headers=headers,json={'due_at':due+1}).status_code == 400
    assert caller.get('/loadcoach/me',headers={'authorization':'Bearer '+token}).status_code == 401
    users.change('owner',enabled=False)
    assert caller.post(path,headers=headers,json={'due_at':due}).status_code == 403
    caller.__exit__(None, None, None)


def test_schedule_edit_api_rejects_invalid_scope_without_changing_existing_grant(tmp_path, monkeypatch):
    caller = through(tmp_path, monkeypatch, database=str(tmp_path/'core.db')).__enter__()
    users, core = caller.users, caller.front.state.task_records.batches.db  # the control node keeps the grants
    users.add('owner', 'admin'); users.add('other', 'admin'); users.add('pat', 'plain')
    headers = {'authorization': 'Bearer '+users.new_key('owner')}
    identifier, token = 'b'*32, 'x'*40
    assert caller.post('/loadcoach/schedules', headers=headers, json={'id': identifier, 'token': token, 'spec': spec()}).status_code == 201
    path = '/loadcoach/schedules/'+identifier+'/edit'
    payload = {'token': 'y'*40, 'spec': spec(time='11:00'), 'enabled': True}
    for name in ('other', 'pat'):
        denied = caller.post(path, headers={'authorization': 'Bearer '+users.new_key(name)}, json=payload)
        assert denied.status_code == 403
    invalid = caller.post(path, headers=headers, json={**payload, 'spec': spec(kind='benchmark', model_id='missing', benchmark='tool_use')})
    assert invalid.status_code in (400, 403)
    original = core.execute('SELECT token_hash,spec FROM schedule_grants WHERE id=?', (identifier,)).fetchone()
    assert json.loads(original[1]) == spec()
    assert caller.post(path, headers=headers, json={**payload, 'enabled': 'yes'}).status_code == 400
    assert core.execute('SELECT token_hash,spec FROM schedule_grants WHERE id=?', (identifier,)).fetchone() == original
    edited = caller.post(path, headers=headers, json={**payload, 'enabled': False})
    assert edited.status_code == 200
    current = core.execute('SELECT token_hash,spec,enabled FROM schedule_grants WHERE id=?', (identifier,)).fetchone()
    assert current[0] != original[0] and json.loads(current[1])['time'] == '11:00' and current[2] == 0
    caller.__exit__(None, None, None)


def test_scheduled_benchmark_delegation_uses_stable_model_and_current_permissions(tmp_path, monkeypatch):
    path=tmp_path/'core.db'
    models=Models(path)
    models.put('tiny',{'gpus':'any','flags':{'model':str(tmp_path/'fixture.gguf'),'ctx-size':2048}})
    identity=models.identity('tiny')
    async def environment(self, engine='llama_cpp'):
        assert engine == 'llama_cpp'
        return {'engine_hash':'fixture','machine_hash':'fixture'}
    def subject(self, name):
        return 'fixture-fingerprint', {'model_id':identity,'settings':models.get(name),'values':{'configured_context':2048}}
    monkeypatch.setattr('loadcoach.probes.Probes.identify',environment)
    monkeypatch.setattr('loadcoach.probes.Probes.subject',subject)
    caller = through(tmp_path, monkeypatch, database=str(path)).__enter__()
    users, core = caller.users, caller.front.state.task_records.batches.db  # the control node keeps the grants
    users.add('owner','admin')
    headers={'authorization':'Bearer '+users.new_key('owner')}
    identifier,token='c'*32,'x'*40
    value=spec(kind='benchmark',model_id=identity,benchmark='load',retention=0)
    assert caller.post('/loadcoach/schedules',headers=headers,json={'id':identifier,'token':token,'spec':value}).status_code == 201
    models.rename('tiny','renamed')
    due=next_at(value,time.time()-86400)
    core.execute('UPDATE schedule_grants SET valid_from=? WHERE id=?',(due,identifier)); core.commit()
    dispatch={'x-suite-schedule':token}
    body={'due_at':due,'task_token':'y'*40}
    target='/loadcoach/schedules/'+identifier+'/dispatch'
    first=caller.post(target,headers=dispatch,json=body)
    assert first.status_code == 201 and first.json()['model'] == 'renamed'
    repeated=caller.post(target,headers=dispatch,json=body)
    assert repeated.status_code == 200 and repeated.json()['id'] == first.json()['id']
    # An accepted occurrence is only replayed (no node is asked again); a different task token gains nothing.
    other=caller.post(target,headers=dispatch,json=dict(body,task_token='z'*40))
    assert other.status_code == 200 and other.json()['id'] == first.json()['id'] and 'z'*40 not in other.text
    users.change('owner',own={'models':[]})
    # The accepted occurrence is still only replayed, but its task is no longer allowed to run.
    assert caller.post(target,headers=dispatch,json=body).status_code == 200
    assert not caller.get('/loadcoach/tasks/'+first.json()['id'],headers={'x-loadcoach-task':'y'*40}).json()['allowed']
    users.remove('owner'); users.add('owner','admin')
    task_status=caller.get('/loadcoach/tasks/'+first.json()['id'],headers={'x-loadcoach-task':'y'*40})
    assert task_status.status_code == 200 and not task_status.json()['allowed']
    models.db.close(); caller.__exit__(None, None, None)
