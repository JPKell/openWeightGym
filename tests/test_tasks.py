"""External task capabilities, service boundaries and the one-time evidence migration."""

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import secrets
import sqlite3
import time
import asyncio
import threading
import httpx

import pytest

from loadcoach.batches import Batches
from loadcoach.probes import Probes
from test_app import add_user, fake_cards, fake_llama_server, gateway, in_an_empty_folder, model
from test_weightroom import gguf

ROOT = Path(__file__).resolve().parents[1]


def create(caller, owner):
    body={'id':'batch_'+secrets.token_hex(12),'token':secrets.token_urlsafe(32),'model':'small'}
    deadline=time.monotonic()+10  # fingerprinting the model file can take seconds while other tests run
    while time.monotonic()<deadline:
        response=caller.post('/loadcoach/tasks',headers=owner,json=body)
        if response.status_code==201:
            return body
        assert 'fingerprinted' in response.text,response.text
        time.sleep(.02)
    raise AssertionError(response.text)


@pytest.mark.parametrize('mib', [0, 25, 12000])
def test_probe_identifies_outside_models_and_resumes_after_they_leave(fake_llama_server, fake_cards, monkeypatch, mib):
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=2048)
    models = {'small': model('small.gguf', ['GPU0'], **{'ctx-size': 2048, 'mib': 1000})}
    allocation = fake_cards[1] / f'{os.getpid()}.json'
    monkeypatch.setattr(Probes, 'model_process', lambda self, pid: pid == os.getpid())
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        task = create(caller, owner)
        path = '/loadcoach/tasks/' + task['id'] + '/probe'
        headers = {'x-loadcoach-task': task['token']}
        allocation.write_text(json.dumps({'0': mib, '1': 999}))
        response = caller.post(path, headers=headers, json={'context': 128})
        assert response.status_code == 503, response.text
        reason = response.json()['error']['message']
        assert f'PID {os.getpid()} (GPU 0: {mib} MiB)' in reason
        assert 'GPU 1' not in reason
        assert 'model process' in reason
        # An allocation on a different card is safe; the same queued task can proceed.
        allocation.write_text(json.dumps({'1': 999}))
        response = caller.post(path, headers=headers, json={'context': 128})
        assert response.status_code == 200, response.text
        assert response.json()['values']['success']


def test_outside_process_diagnostics_exclude_the_probe_and_unselected_cards(monkeypatch):
    runner = SimpleNamespace(seen={'pids': {30: {1: 25}, 10: {0: 1000}, 20: {0: 16, 1: 32}}}, servers={})
    monkeypatch.setattr(Probes, 'model_process', lambda self, pid: True)
    probes = Probes(runner, lambda: False, lambda _: True)
    assert probes.outside_processes({0}, own_pid=10) == 'PID 20 (GPU 0: 16 MiB)'
    assert probes.outside_processes({1}, own_pid=30) == 'PID 20 (GPU 1: 32 MiB)'
    assert probes.outside_processes({2}) == ''


@pytest.mark.parametrize('engine,weights,expected', [
    ('ptyxis', '', False), ('Xorg', '/usr/lib/driver.so', False),
    ('llama-server', '', True), ('llama-cli', '', True), ('custom-engine', '', True),
    ('ollama', '', True), ('vllm', '', True),
    ('python3', '/models/model.gguf', True), ('python3', '/models/model.safetensors', True),
    ('python3', '/models/Model.GGUF (deleted)', True), ('python3', '', False),
])
def test_model_process_recognition_uses_engine_or_resident_weights(tmp_path, engine, weights, expected):
    process = tmp_path / '123'; process.mkdir()
    (process / 'exe').symlink_to('/usr/bin/' + engine)
    (process / 'maps').write_text('1000-2000 r--p 00000000 00:00 0 ' + weights + '\n')
    probes = Probes(SimpleNamespace(config=SimpleNamespace(llama_server='/opt/custom-engine')), None, None)
    assert probes.model_process(123, tmp_path) is expected
    assert probes.model_process(999, tmp_path) is False


def test_tracked_model_is_detected_even_without_readable_process_details(monkeypatch):
    runner = SimpleNamespace(seen={'pids': {10: {0: 8000}, 20: {0: 25}}},
                             servers={'model': SimpleNamespace(process=SimpleNamespace(pid=10))})
    monkeypatch.setattr(Probes, 'model_process', lambda self, pid: False)
    probes = Probes(runner, None, None)
    assert probes.outside_processes({0}) == 'PID 10 (GPU 0: 8000 MiB)'
    assert probes.outside_processes({0}, own_pid=10) == ''


@pytest.mark.parametrize('mib', [25, 12000])
def test_desktop_gpu_allocations_allow_loading_and_measurement(fake_llama_server, fake_cards, monkeypatch, mib):
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=2048)
    models = {'small': model('small.gguf', ['GPU0'], **{'ctx-size': 2048, 'mib': 1000, 'benchmark-delay': .8})}
    allocation = fake_cards[1] / f'{os.getpid()}.json'
    allocation.write_text(json.dumps({'0': mib}))
    checks = []
    original = Probes.outside_processes
    def observe(self, indexes, own_pid=None):
        checks.append(own_pid)
        return original(self, indexes, own_pid)
    monkeypatch.setattr(Probes, 'outside_processes', observe)
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin'); task = create(caller, owner)
        response = caller.post('/loadcoach/tasks/' + task['id'] + '/probe',
                               headers={'x-loadcoach-task': task['token']},
                               json={'context': 128, 'inputs': 32, 'outputs': 16, 'text': 'Benchmark text.', 'warmup_outputs': 0})
        assert response.status_code == 200, response.text
        assert response.json()['values']['success']
        assert response.json()['values']['peak_gpu_mib'] == 1000  # excludes desktop allocation
        assert allocation.exists()  # never terminate or otherwise change the desktop process
    assert checks[0] is None and any(pid is not None for pid in checks)  # preflight and watcher


@pytest.mark.parametrize('is_model', [False, True])
def test_measurement_watcher_distinguishes_new_desktop_and_model_allocations(fake_llama_server, fake_cards, monkeypatch, is_model):
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=2048)
    models = {'small': model('small.gguf', ['GPU0'], **{'ctx-size': 2048, 'mib': 1000, 'benchmark-delay': .8})}
    allocation = fake_cards[1] / f'{os.getpid()}.json'
    original = Probes.completion
    async def completion(self, *args, **kwargs):
        allocation.write_text(json.dumps({'0': 25}))
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(Probes, 'completion', completion)
    monkeypatch.setattr(Probes, 'model_process', lambda self, pid: is_model and pid == os.getpid())
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin'); task = create(caller, owner)
        response = caller.post('/loadcoach/tasks/' + task['id'] + '/probe',
                               headers={'x-loadcoach-task': task['token']},
                               json={'context': 128, 'inputs': 32, 'outputs': 16, 'text': 'Benchmark text.', 'warmup_outputs': 0})
        assert response.status_code == (502 if is_model else 200), response.text
        if is_model:
            assert f'PID {os.getpid()} (GPU 0: 25 MiB)' in response.text
            assert 'model process appeared' in response.text
            assert caller.get('/loadcoach/status', headers=owner).json()['lock'] is None
        assert allocation.exists()


def test_new_gpu_model_process_stops_only_probe_child_and_retry_uses_same_task(fake_llama_server, fake_cards, monkeypatch):
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=2048)
    models = {'small': model('small.gguf', ['GPU0'], **{'ctx-size': 2048, 'mib': 1000})}
    allocation = fake_cards[1] / f'{os.getpid()}.json'
    original = Probes.completion
    inject = [True]
    async def interrupted(self, server, *args, **kwargs):
        if not inject[0]:
            return await original(self, server, *args, **kwargs)
        allocation.write_text(json.dumps({'0': 25}))
        async def until_stopped():
            while server.process.returncode is None:
                await asyncio.sleep(.01)
        await asyncio.wait_for(until_stopped(), 4)
        raise httpx.TransportError('Probe child stopped after outside model appeared.')
    monkeypatch.setattr(Probes, 'completion', interrupted)
    monkeypatch.setattr(Probes, 'model_process', lambda self, pid: pid == os.getpid())
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin'); task = create(caller, owner)
        path = '/loadcoach/tasks/' + task['id'] + '/probe'
        headers = {'x-loadcoach-task': task['token']}
        body = {'context': 128, 'inputs': 32, 'outputs': 16, 'text': 'Benchmark text.'}
        response = caller.post(path, headers=headers, json=body)
        assert response.status_code == 502, response.text
        assert 'sample is not comparable' in response.text
        assert allocation.exists()  # the outside process was not touched
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert status['lock'] is None and not status['loaded']
        inject[0] = False
        allocation.unlink()
        response = caller.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json()['values']['success']


@pytest.mark.parametrize('cancel', [False, True])
def test_status_lists_only_the_task_with_an_active_probe(fake_llama_server, fake_cards, tmp_path, monkeypatch, cancel):
    entered, release = threading.Event(), threading.Event()
    async def sample(self, task, options,kept=None):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return {'values': {'success': True}, 'output': '', 'log': ''}
    monkeypatch.setattr(Probes, 'sample', sample)
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=2048)
    models = {'small': model('small.gguf', ['GPU0'], **{'ctx-size': 2048, 'mib': 1000})}
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0], database=str(tmp_path / 'tasks.db')) as caller:
        owner = add_user(caller, 'owner', 'admin')
        other = add_user(caller, 'other', 'plain', by=owner)
        first, queued = create(caller, owner), create(caller, owner)
        assert caller.get('/loadcoach/status', headers=owner).json()['active_batches'] == []
        results = []
        thread = threading.Thread(target=lambda: results.append(caller.post('/loadcoach/tasks/' + first['id'] + '/probe',
            headers={'x-loadcoach-task': first['token']}, json={'context': 128})))
        thread.start()
        try:
            assert entered.wait(3)
            active = caller.get('/loadcoach/status', headers=owner).json()['active_batches']
            assert [job['id'] for job in active] == [first['id']]
            assert active[0]['model'] == 'small' and active[0]['user'] == 'owner'
            assert caller.get('/loadcoach/status', headers=other).json()['active_batches'] == []
            if cancel:
                response = caller.post('/v1/batches/' + first['id'] + '/cancel', headers=owner)
                assert response.status_code == 200 and response.json()['status'] == 'cancelled'
                thread.join(3)
                assert not thread.is_alive(), 'Cancelling a batch must interrupt its active benchmark probe'
                assert caller.get('/v1/batches/' + queued['id'], headers=owner).json()['status'] == 'in_progress'
        finally:
            release.set(); thread.join(3)
        assert results[0].status_code == (409 if cancel else 200)
        assert caller.get('/loadcoach/status', headers=owner).json()['active_batches'] == []


def test_task_tokens_are_scoped_and_permissions_are_rechecked(fake_llama_server,fake_cards,tmp_path):
    database=str(tmp_path/'tasks.db')
    gguf('small.gguf',general__architecture='tiny',tiny__context_length=2048)
    models={'small':model('small.gguf',['GPU0'],**{'ctx-size':2048,'mib':1000})}
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as caller:
        owner=add_user(caller,'owner','admin')
        plain=add_user(caller,'plain','plain',by=owner)
        manager=add_user(caller,'manager','admin',by=owner)
        first=create(caller,owner);second=create(caller,owner)
        scoped={'x-loadcoach-task':first['token']}
        path='/loadcoach/tasks/'+first['id']
        assert caller.get(path,headers=scoped).json()['allowed']
        assert caller.post('/loadcoach/tasks',headers=owner,json=first).json()['id']==first['id']
        assert caller.get('/loadcoach/tasks/'+second['id'],headers=scoped).status_code==404  # the front never says whether another task exists
        assert caller.get('/v1/models',headers={'authorization':'Bearer '+first['token']}).status_code==401
        assert caller.post('/loadcoach/tasks',headers=plain,json=first).status_code==403
        assert caller.post(path+'/probe',headers=scoped,json={'context':128,'inputs':120,'outputs':16}).status_code==400
        assert caller.get('/loadcoach/benchmarks',headers=owner).status_code==404
        assert caller.post('/loadcoach/users/owner',headers=manager,json={'own':{'remove':['benchmark']}}).status_code==200
        assert not caller.get(path,headers=scoped).json()['allowed']
        assert caller.post(path+'/probe',headers=scoped,json={'context':128}).status_code==403
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as caller:
        assert caller.get(path,headers=scoped).json()['id']==first['id']
        assert caller.post(path+'/finish',headers=scoped,json={'state':'cancelled'}).status_code==200
        assert caller.get('/v1/batches/'+first['id'],headers=owner).json()['status']=='cancelled'


def test_benchmark_application_has_no_loadcoach_imports():
    for path in (ROOT/'benchmarks').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.ImportFrom):
                assert not (node.module or '').startswith('loadcoach'),path
            elif isinstance(node,ast.Import):
                assert all(not name.name.startswith('loadcoach') for name in node.names),path


def test_task_tracks_renames_without_following_a_reused_name(fake_llama_server,fake_cards,tmp_path):
    gguf('small.gguf',general__architecture='tiny',tiny__context_length=2048)
    models={'small':model('small.gguf',['GPU0'],**{'ctx-size':2048,'mib':1000})}
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=str(tmp_path/'tasks.db')) as caller:
        owner=add_user(caller,'owner','admin')
        intent=create(caller,owner)
        path='/loadcoach/tasks/'+intent['id'];scoped={'x-loadcoach-task':intent['token']}
        before=caller.get(path,headers=scoped).json()
        assert caller.post('/loadcoach/model-rename',headers=owner,json={'name':'small','to':'renamed'}).status_code==200
        after=caller.get(path,headers=scoped).json()
        assert after['model']=='renamed' and after['allowed']
        assert after['model_id']==before['model_id'] and after['fingerprint']==before['fingerprint']
        retry={**intent,'model_id':before['model_id']}
        assert caller.post('/loadcoach/tasks',headers=owner,json=retry).json()['id']==intent['id']
        assert caller.delete('/loadcoach/models/renamed',headers=owner).status_code==200
        assert caller.post('/loadcoach/models/small',headers=owner,json=models['small']).status_code==201
        removed=caller.get(path,headers=scoped).json()
        assert removed['model'] is None and not removed['allowed']
        assert caller.post('/loadcoach/tasks',headers=owner,json=retry).status_code==200  # a repeat only replays the receipt
        assert caller.post(path+'/probe',headers=scoped,json={'context':128}).status_code==403
    for path in (ROOT/'loadcoach').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.ImportFrom):
                assert not (node.module or '').startswith('benchmarks'),path


def test_migration_keeps_samples_batches_controls_and_private_backup(tmp_path):
    spec=importlib.util.spec_from_file_location('split_benchmarks',ROOT/'scripts/split-benchmarks.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source,target,backup=(tmp_path/name for name in ('loadcoach.db','benchmarks.db','before.db'))
    batches=Batches(str(source));file=batches.put_file('owner','sample.jsonl','batch',b'{}')
    batch=batches.start('owner','/loadcoach/benchmark-probe',file['id'],[{'custom_id':'measure','body':{'model':'small'}}])
    db=batches.db
    db.executescript('''
        CREATE TABLE benchmark_definitions(id TEXT PRIMARY KEY,enabled INTEGER,formula TEXT);
        INSERT INTO benchmark_definitions VALUES('context',1,'max_context_tokens * parameter_count');
        CREATE TABLE benchmark_state(id INTEGER PRIMARY KEY,paused INTEGER);
        INSERT INTO benchmark_state VALUES(1,1);
        CREATE TABLE benchmark_runs(id TEXT PRIMARY KEY,model TEXT,benchmark TEXT,fingerprint TEXT,subject TEXT,environment TEXT,
            state TEXT,created REAL,started REAL,ended REAL,reason TEXT,messages TEXT);
        CREATE TABLE benchmark_samples(run TEXT,sample TEXT,at REAL,measurements TEXT,output TEXT,log TEXT,PRIMARY KEY(run,sample));
    ''')
    subject=json.dumps({'values':{'parameter_count':10},'settings':{'flags':{}}})
    sample=(batch['id'],'context-2048',1.0,json.dumps({'success':True,'context_tokens':2048}),'answer','original log')
    with db:
        db.execute('INSERT INTO benchmark_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(batch['id'],'small','context','fingerprint',subject,'{}','running',1,2,None,'','[]'))
        db.execute('INSERT INTO benchmark_samples VALUES(?,?,?,?,?,?)',sample)
    result=module.migrate(source,target,backup)
    assert result['runs']==result['samples']==1
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target) as moved:
        assert moved.execute('SELECT * FROM benchmark_samples').fetchone()==sample
        assert moved.execute('SELECT state,user FROM benchmark_runs').fetchone()==('queued','owner')
        token=moved.execute('SELECT token FROM benchmark_runs').fetchone()[0]
        assert moved.execute('SELECT paused FROM benchmark_state').fetchone()==(1,)
    assert db.execute('SELECT token_hash FROM external_tasks').fetchone()[0]==hashlib.sha256(token.encode()).hexdigest()
    assert batches.get(batch['id'])['endpoint']=='/loadcoach/task'
    assert db.execute('SELECT * FROM benchmark_samples').fetchone()==sample
    with pytest.raises(ValueError,match='already exists'):
        module.migrate(source,target,tmp_path/'another-backup.db')
