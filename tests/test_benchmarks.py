"""Native measurements, durable batch recovery, preemption and immutable scoring evidence."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from starlette.testclient import TestClient

from benchmarks.app import Settings, build_app, evidence_matches
from benchmarks.store import Benchmarks
from benchmarks.scoring import calculate, formula_tree
from benchmarks.suites.context import next_context
from benchmarks.suites import extraction
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder, model
from test_weightroom import gguf
from benchmark_clients import gateway


def wait(caller, who, number, predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = caller.get('/benchmarks/' + number, headers=who).json()
        if predicate(result):
            if result['state'] in ('completed','failed','cancelled'):
                batch = caller.get('/v1/batches/' + number, headers=who).json()
                if batch.get('status') in ('in_progress','cancelling'):
                    time.sleep(.03)
                    continue
            return result
        time.sleep(.03)
    raise AssertionError(result)


def start(caller, who, kind):
    deadline = time.monotonic() + 3
    while True:
        response = caller.post('/benchmarks', headers=who, json={'benchmark': kind, 'models': ['small']})
        if response.status_code == 202:
            return response.json()['runs'][0]
        if 'fingerprinted' not in response.text or time.monotonic() > deadline:
            raise AssertionError(response.text)
        time.sleep(.05)


def test_scores_change_without_mutating_measurements(tmp_path):
    store = Benchmarks(str(tmp_path / 'scores.db'))
    run = store.create('owner', 'small', 'context')
    store.activate(run, {'fingerprint':'abc', 'subject':{'values': {'parameter_count':10}}, 'environment':{}})
    store.keep(run, 'probe', {'success': True, 'context_tokens': 4096}, 'answer', 'log')
    store.finish(run, 'completed')
    original = store.samples(run)
    assert store.get(run)['score'] == 4096
    store.configure('context', True, 'max_context_tokens * parameter_count')
    assert store.get(run)['score'] == 40960 and store.samples(run) == original
    assert calculate('missing / 0', {}) is None
    for expression in ['__import__("os")', 'parameter_count.__class__', '2 ** 100000', '[1][0]']:
        with pytest.raises(ValueError):
            formula_tree(expression, {'parameter_count'})
    assert calculate('1 / 0', {}) is None
    assert calculate('2 + 3 * (10 - 4) / 2', {}) == 11


def test_run_listing_batches_reads_and_reuses_summaries_for_role_evidence(tmp_path, monkeypatch):
    store = Benchmarks(str(tmp_path / 'batched.db'))
    for index in range(30):
        number = store.create('owner', 'small', 'context', model_id='stable-model')
        store.activate(number, {'fingerprint': 'same', 'subject': {}, 'environment': {}})
        store.keep(number, 'probe', {'success': True, 'context_tokens': 1024 + index}, 'private output', 'private log')
        store.finish(number, 'completed')
    expected = [store.get(number) for number, in store.db.execute('SELECT id FROM benchmark_runs ORDER BY created DESC')]
    queries = []
    store.db.raw.set_trace_callback(queries.append)
    runs = store.listing()
    store.db.raw.set_trace_callback(None)
    assert runs == expected
    assert len([query for query in queries if query.upper().startswith('SELECT')]) <= 6
    assert not any('benchmark_evidence' in query or 'output,log' in query for query in queries)
    snapshot = store.role_snapshot()

    def no_reread():
        raise AssertionError('Already-read summaries must not be rebuilt to publish role evidence')

    monkeypatch.setattr(store, 'listing', no_reread)
    assert store.role_snapshot(runs=runs) == snapshot
    store.db.close()


@pytest.mark.parametrize('publishing', [False, True])
def test_benchmark_history_reads_do_not_block_other_requests(tmp_path, monkeypatch, publishing):
    entered, release = threading.Event(), threading.Event()
    method = 'role_snapshot' if publishing else 'listing'
    original = getattr(Benchmarks, method)

    def slow(view, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(view, *args, **kwargs)

    monkeypatch.setattr(Benchmarks, method, slow)
    app, _ = queue_fixture(tmp_path, can=('benchmark', 'manage-models') if publishing else ('benchmark',))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://benchmarks') as client:
            waiting = asyncio.create_task(client.get('/benchmarks'))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                other = await asyncio.wait_for(client.get('/benchmarks/no-such-run'), timeout=1)
                assert other.status_code == 404
                assert not waiting.done()
            finally:
                release.set()
            answer = await waiting
            assert answer.status_code == 200 and answer.json()['catalogue']['data']

    asyncio.run(exercise())
    app.state.store.db.close()


def test_benchmark_catalogue_refusal_is_not_treated_as_a_compute_outage(tmp_path):
    def answer(request):
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200, json={'name': 'owner', 'can': ['benchmark']})
        return httpx.Response(403, json={'error': {'message': 'Catalogue access refused.'}})

    compute = httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url='http://compute')
    with TestClient(build_app(Settings(compute='http://compute', database=str(tmp_path / 'results.db')), compute)) as client:
        refused = client.get('/benchmarks')
        assert refused.status_code == 403 and 'Catalogue access refused.' in refused.text


def test_a_benchmark_run_freezes_its_compute_location(tmp_path):
    store = Benchmarks(str(tmp_path / 'locations.db'))
    run = store.create('owner', 'embed', 'embedding_speed', 'model_cpu', location='front_cpu')
    store.activate(run, {'fingerprint': 'cpu-fingerprint', 'subject': {}, 'environment': {'machine_hash': 'host-a'},
                         'model_id': 'model_cpu', 'machine': 'front_cpu'})
    assert store.get(run)['location'] == 'front_cpu'
    assert store.execution(run)['location'] == 'front_cpu'
    with pytest.raises(ValueError):
        store.create('owner', 'embed', 'embedding_speed', 'model_cpu', location='remote_cpu',
                     number=run, token=store.token(run))


def test_context_search_starts_at_ceiling_and_refines_after_capacity_failure():
    for ceiling, fits in [(8192,8192),(5000,5000),(128,128),(16384,12000),(512,300),(8192,0)]:
        samples=[]
        assert next_context(samples,ceiling)==ceiling
        while (probe:=next_context(samples,ceiling)) is not None:
            assert probe not in [sample['values']['context_tokens'] for sample in samples]
            samples.append({'values': {'context_tokens':probe,'success':probe<=fits}})
            assert len(samples)<20
        successes=[sample['values']['context_tokens'] for sample in samples if sample['values']['success']]
        assert (max(successes) if successes else 0) <= fits
        if fits==ceiling:
            assert max(successes)==ceiling and len(samples)==1
        elif fits >= 128:
            assert fits-max(successes) <= min(1024,max(128,ceiling//4))


def bench_model(delay=0):
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=2048, general__parameter_count=123456)
    return {'small': model('small.gguf', ['GPU0'], **{'ctx-size':2048,'mib':1000,'benchmark-delay':delay})}


def test_current_benchmark_evidence_requires_model_protocol_engine_and_machine_identity():
    run = {'fingerprint': 'model-a', 'version': 2, 'environment': {'engine_hash': 'engine-a', 'machine_hash': 'machine-a'}}
    definition = {'version': 2}
    environment = {'engine_hash': 'engine-a', 'machine_hash': 'machine-a'}
    assert evidence_matches(run, definition, 'model-a', environment)
    assert not evidence_matches(run, {**definition, 'version': 3}, 'model-a', environment)
    assert not evidence_matches(run, definition, 'model-b', environment)
    assert not evidence_matches(run, definition, 'model-a', {**environment, 'engine_hash': 'engine-b'})
    assert not evidence_matches(run, definition, 'model-a', {**environment, 'machine_hash': 'machine-b'})
    assert not evidence_matches({**run, 'current_protocol': False}, definition, 'model-a', environment)
    assert not evidence_matches({**run, 'current_server': False}, definition, 'model-a', environment)
    assert not evidence_matches(run, definition, 'model-a', {})
    assert not evidence_matches({**run, 'environment': {}}, definition, 'model-a', {})


def queue_fixture(tmp_path, *, disabled=(), offline=(), rejected=(), rejected_status=503, transport_fail=(),
                  can=('benchmark', 'stop'), capabilities=None):
    """Two copies of the same logical model with independently addressable evidence."""
    locations = [
        {'machine': name, 'id': 'small', 'model_id': name + '-id', 'fingerprint': 'same-weights',
         'embedding': False, 'enabled': name not in disabled, **({'capabilities':capabilities} if capabilities is not None else {})}
        for name in ('compute', 'rig')]
    catalogue = {'data': [{**locations[0], 'id': 'small', 'owned_by': 'loadcoach', 'locations': locations}]}
    submitted = []

    def answer(request):
        path = request.url.path
        if path == '/loadcoach/me':
            return httpx.Response(200, json={'name': 'owner', 'can': list(can)})
        if path == '/v1/models':
            return httpx.Response(200, json=catalogue)
        if path in ('/loadcoach/probe-environment', '/turnstile/machines/rig/probe-environment'):
            machine = 'rig' if '/rig/' in path else 'compute'
            return httpx.Response(503 if machine in offline else 200, json={
                'engine_hash': 'engine-' + machine, 'machine_hash': 'host-' + machine})
        if path == '/loadcoach/tasks':
            payload = json.loads(request.content)
            submitted.append(payload)
            kind = payload['metadata']['benchmark']
            if kind in transport_fail:
                raise httpx.ConnectError('connection dropped before task acknowledgement')
            if kind in rejected:
                return httpx.Response(rejected_status, json={'error': {'message': 'location unavailable'}})
            machine = payload.get('machine', 'compute')
            return httpx.Response(200, json={'fingerprint': 'same-weights', 'model_id': machine + '-id',
                'machine': machine, 'subject': {}, 'environment': {
                    'engine_hash': 'engine-' + machine, 'machine_hash': 'host-' + machine}})
        return httpx.Response(404, json={'error': {'message': path}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url='http://compute')
    app = build_app(Settings(compute='http://compute', database=str(tmp_path / 'queue.db')), client)
    async def idle_worker():
        await asyncio.Event().wait()
    app.state.worker.run = idle_worker
    app.state.store.paused(True)
    for definition in app.state.store.definitions():
        if definition['id'] not in ('load', 'context'):
            app.state.store.configure(definition['id'], False, definition['formula'])
    return app, submitted


def test_outstanding_uses_per_location_ids_and_skips_disabled_copies(tmp_path):
    app, submitted = queue_fixture(tmp_path, disabled=('rig',))
    with TestClient(app) as caller:
        headers = {'authorization': 'Bearer test'}
        assert caller.post('/benchmarks', headers=headers, json={'benchmark': 'load', 'targets': [
            {'model': 'small', 'machine': 'rig'}]}).status_code == 400
        response = caller.post('/benchmarks/outstanding', headers=headers, json={'model': 'small'})
        assert response.status_code == 202 and response.json()['queued'] == 2
        assert {task['machine'] for task in submitted} == {'compute'}
        again = caller.post('/benchmarks/outstanding', headers=headers, json={'model': 'small'})
        assert again.json()['queued'] == 0 and len(again.json()['active']) == 2


def test_outstanding_respects_each_location_evidence_and_environment(tmp_path):
    app, submitted = queue_fixture(tmp_path)
    store = app.state.store
    run = store.create('owner', 'small', 'load', 'compute-id', location='compute')
    store.activate(run, {'fingerprint': 'same-weights', 'model_id': 'compute-id', 'machine': 'compute',
        'subject': {}, 'environment': {'engine_hash': 'engine-compute', 'machine_hash': 'host-compute'}})
    store.finish(run, 'completed')
    with TestClient(app) as caller:
        response = caller.post('/benchmarks/outstanding', headers={'authorization': 'Bearer test'}, json={'model': 'small'})
        result = response.json()
        assert response.status_code == 202 and result['queued'] == 3
        assert result['fresh'] == ['load at compute']
        assert len([task for task in submitted if task['metadata']['benchmark'] == 'load']) == 1
        assert next(task for task in submitted if task['metadata']['benchmark'] == 'load')['machine'] == 'rig'


def test_outstanding_reports_offline_and_partial_queue_failures(tmp_path):
    app, submitted = queue_fixture(tmp_path, offline=('rig',), rejected=('context',))
    with TestClient(app) as caller:
        response = caller.post('/benchmarks/outstanding', headers={'authorization': 'Bearer test'}, json={'model': 'small'})
        result = response.json()
        assert response.status_code == 207 and result['queued'] == 1
        assert set(result['unavailable']) == {'load at rig', 'context at rig'}
        assert result['errors'] == [{'benchmark': 'context at compute', 'status': 503,
                                     'message': 'location unavailable'}]
        assert {task['machine'] for task in submitted} == {'compute'}
        assert all(run['location'] != 'rig' for run in app.state.store.listing())


def test_outstanding_keeps_supported_work_when_one_suite_is_rejected(tmp_path):
    app, submitted = queue_fixture(tmp_path, rejected=('context',), rejected_status=400)
    with TestClient(app) as caller:
        result = caller.post('/benchmarks/outstanding', headers={'authorization': 'Bearer test'},
                             json={'model': 'small'})
        assert result.status_code == 207 and result.json()['queued'] == 2
        assert {error['benchmark'] for error in result.json()['errors']} == {
            'context at compute', 'context at rig'}
        assert all(error['status'] == 400 for error in result.json()['errors'])
        assert {task['metadata']['benchmark'] for task in submitted} == {'context', 'load'}


def test_outstanding_transport_failure_closes_intent_and_keeps_successful_work(tmp_path):
    app, submitted = queue_fixture(tmp_path, transport_fail=('context',))
    with TestClient(app) as caller:
        result = caller.post('/benchmarks/outstanding', headers={'authorization': 'Bearer test'},
                             json={'model': 'small'})
        assert result.status_code == 207 and result.json()['queued'] == 2
        assert {error['benchmark'] for error in result.json()['errors']} == {
            'context at compute', 'context at rig'}
        assert all(error['status'] == 503 for error in result.json()['errors'])
        runs = app.state.store.listing()
        assert len(runs) == 4 and sorted(run['state'] for run in runs) == ['failed', 'failed', 'queued', 'queued']
        assert all('acceptance is unconfirmed' in run['reason'] for run in runs if run['state'] == 'failed')
        assert {run['id'] for run in runs} == {task['id'] for task in submitted}
        again = caller.post('/benchmarks/outstanding', headers={'authorization': 'Bearer test'},
                            json={'model': 'small'})
        assert again.json()['queued'] == 0 and set(again.json()['active']) == {'load at compute', 'load at rig'}
        assert not any(run['state'] == 'creating' for run in app.state.store.listing())


def test_direct_queue_transport_failure_wakes_reconciliation(tmp_path):
    app, submitted = queue_fixture(tmp_path, transport_fail=('load',))
    with TestClient(app) as caller:
        response = caller.post('/benchmarks', headers={'authorization': 'Bearer test'},
                               json={'benchmark': 'load', 'targets': [{'model': 'small', 'machine': 'compute'}]})
        assert response.status_code == 503 and len(submitted) == 1
        runs = app.state.store.listing()
        assert len(runs) == 1 and runs[0]['state'] == 'failed'
        assert 'acceptance is unconfirmed' in runs[0]['reason']
        assert app.state.worker.wake.is_set()


def test_outstanding_rejects_unpermitted_user_without_creating_tasks(tmp_path):
    app, submitted = queue_fixture(tmp_path, can=())
    with TestClient(app) as caller:
        response = caller.post('/benchmarks/outstanding', headers={'authorization': 'Bearer test'}, json={'model': 'small'})
        assert response.status_code == 403 and submitted == []


def test_outstanding_requeues_stale_engine_evidence_for_only_its_location(tmp_path):
    app, submitted = queue_fixture(tmp_path)
    store = app.state.store
    for machine in ('compute', 'rig'):
        run = store.create('owner', 'small', 'load', machine + '-id', location=machine)
        store.activate(run, {'fingerprint': 'same-weights', 'model_id': machine + '-id', 'machine': machine,
            'subject': {}, 'environment': {'engine_hash': 'older' if machine == 'rig' else 'engine-compute',
                                            'machine_hash': 'host-' + machine}})
        store.finish(run, 'completed')
    with TestClient(app) as caller:
        response = caller.post('/benchmarks/outstanding', headers={'authorization': 'Bearer test'}, json={'model': 'small'})
        assert response.status_code == 202
        assert 'load at compute' in response.json()['fresh']
        assert any(task['metadata']['benchmark'] == 'load' and task['machine'] == 'rig' for task in submitted)
        assert not any(task['metadata']['benchmark'] == 'load' and task['machine'] == 'compute' for task in submitted)


def test_queue_outstanding_adds_each_applicable_benchmark_once(fake_llama_server, fake_cards):
    with gateway(fake_llama_server, models=bench_model(), nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        caller.post('/benchmarks/control', headers=owner, json={'action': 'pause'})
        definitions = caller.get('/benchmarks', headers=owner).json()['definitions']
        applicable = [one for one in definitions if one['enabled'] and not one.get('embedding')]
        queued = caller.post('/benchmarks/outstanding', headers=owner, json={'model': 'small'})
        assert queued.status_code == 202 and queued.json()['queued'] == len(applicable)
        again = caller.post('/benchmarks/outstanding', headers=owner, json={'model': 'small'})
        assert again.status_code == 202 and again.json()['queued'] == 0
        assert len(again.json()['active']) == len(applicable) and again.json()['fresh'] == []
        runs = caller.get('/benchmarks', headers=owner).json()['runs']
        assert len(runs) == len(applicable) and {run['benchmark'] for run in runs} == {one['id'] for one in applicable}


@pytest.mark.parametrize('kind', ['context','speed','load'])
def test_real_worker_protocol_completes_one_batch_and_keeps_samples(fake_llama_server, fake_cards, kind):
    models=bench_model()
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,kind)
        run=wait(caller,owner,number,lambda run:run['state'] in ('completed','failed'))
        assert run['state']=='completed',run
        assert run['score'] is not None and run['samples']
        assert all(point['values']['peak_gpu_mib']==1000 for point in run['samples'])
        batch=caller.get('/v1/batches/'+number,headers=owner).json()
        assert batch['status']=='completed' and batch['request_counts']['completed']==1
        assert caller.get('/loadcoach/status',headers=owner).json()['lock'] is None
        if kind=='context':
            assert run['metrics']['max_context_tokens']==2048
            assert len(run['samples'])==1 and run['metrics']['probes']==1
            assert run['subject']['protocol']['context_search']=='ceiling-first'
        if kind=='speed':
            assert run['metrics']['generation_tokens_per_second']==50
            assert run['metrics']['input_tokens']==512


@pytest.mark.parametrize('wait_limit', [0, 60])
def test_ordinary_request_preempts_then_run_resumes_and_survives_restart(fake_llama_server, fake_cards, tmp_path, wait_limit):
    models=bench_model(.3)
    database=str(tmp_path/'resume.db')
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database,max_estimated_wait=wait_limit) as caller:
        owner=add_user(caller,'owner','admin')
        caller.log.add('model', 'loaded', model='small', seconds=1)
        number=start(caller,owner,'speed')
        wait(caller,owner,number,lambda run:run['samples_count']>=1)
        response=caller.post('/v1/chat/completions',headers=owner,json={'model':'small','messages':[]})
        assert response.status_code==200
        run=wait(caller,owner,number,lambda run:any('Yielded' in line['text'] for line in run['messages']))
        saved=run['samples'][0]
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as caller:
        run=wait(caller,owner,number,lambda run:run['state'] in ('completed','failed'))
        assert run['state']=='completed',run
        assert run['samples'][0]==saved and len(run['samples'])==3


def test_pause_cancel_and_model_permissions(fake_llama_server, fake_cards):
    with gateway(fake_llama_server,models=bench_model(.4),nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        plain=add_user(caller,'plain','plain',by=owner)
        assert caller.post('/benchmarks',headers=plain,json={'benchmark':'context','models':['small']}).status_code==403
        number=start(caller,owner,'speed')
        wait(caller,owner,number,lambda run:run['state']=='running')
        assert caller.post('/benchmarks/control',headers=owner,json={'run':number,'action':'pause'}).status_code==200
        run=wait(caller,owner,number,lambda run:run['state']=='paused')
        time.sleep(.5)
        assert caller.get('/loadcoach/status',headers=owner).json()['lock'] is None
        assert caller.post('/v1/batches/'+number+'/cancel',headers=owner).status_code==200
        assert wait(caller,owner,number,lambda run:run['state']=='cancelled')['state']=='cancelled'
        assert caller.post('/loadcoach/users/plain',headers=owner,json={'own':{'models':[]}}).status_code==200
        assert caller.get('/benchmarks',headers=plain).json()['runs']==[]
        assert caller.get('/benchmarks/'+number,headers=plain).status_code==404


def test_freshness_is_advisory_for_engine_but_distinct_for_model(fake_llama_server, fake_cards, tmp_path):
    import json,sqlite3
    database=str(tmp_path/'freshness.db')
    with gateway(fake_llama_server,models=bench_model(),nvidia_smi=fake_cards[0],database=database) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'context')
        run=wait(caller,owner,number,lambda run:run['state']=='completed')
        samples=run['samples']
        environment=dict(run['environment'],engine_hash='an-older-engine')
        with sqlite3.connect(str(tmp_path/'freshness.benchmarks.db')) as db:
            db.execute('UPDATE benchmark_runs SET environment=? WHERE id=?',(json.dumps(environment),number))
        stale=caller.get('/benchmarks',headers=owner).json()['runs'][0]
        assert stale['current_fingerprint'] and stale['rerun']==['engine'] and stale['score']==2048
        assert caller.post('/benchmarks/definitions',headers=owner,json={'benchmark':'context','enabled':False,'formula':'max_context_tokens * parameter_count'}).status_code==200
        rescored=caller.get('/benchmarks/'+number,headers=owner).json()
        assert rescored['score']==2048*123456 and rescored['samples']==samples
        assert caller.post('/benchmarks',headers=owner,json={'benchmark':'context','models':['small']}).status_code==400
        assert caller.post('/loadcoach/models/small',headers=owner,json={'flags':{'model':'small.gguf','ctx-size':1024},'gpus':['GPU0']}).status_code==200
        old=caller.get('/benchmarks',headers=owner).json()['runs'][0]
        assert not old['current_fingerprint'] and old['score']==2048*123456
        # A run made under an older version of its benchmark is kept and marked. One made under the current version is not.
        assert (old['version'],old['latest_version'])==(1,1) and not any('version' in why for why in old['rerun'])
        with sqlite3.connect(str(tmp_path/'freshness.benchmarks.db')) as db:
            subject=json.loads(db.execute('SELECT subject FROM benchmark_runs WHERE id=?',(number,)).fetchone()[0])
            subject['protocol']['version']=0
            db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?',(json.dumps(subject),number))
        behind=caller.get('/benchmarks',headers=owner).json()
        assert 'benchmark version 0, now 1' in behind['runs'][0]['rerun'] and behind['runs'][0]['score']==2048*123456
        assert {b['id']:b['version'] for b in behind['definitions']}['extraction']==extraction.PROTOCOL['version']


def test_a_quality_benchmark_is_not_run_on_a_model_that_only_makes_embeddings(fake_llama_server, fake_cards):
    models=bench_model()
    gguf('vectors.gguf', general__architecture='tiny', tiny__context_length=2048)
    models['vectors']=model('vectors.gguf', ['GPU0'], **{'ctx-size':2048,'mib':1000,'embedding':True})
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        caller.post('/benchmarks/control',headers=owner,json={'action':'pause'})
        deadline=time.monotonic()+3
        while 'fingerprinted' in caller.post('/benchmarks',headers=owner,json={'benchmark':'extraction','models':['vectors']}).text and time.monotonic()<deadline:
            time.sleep(.05)
        alone=caller.post('/benchmarks',headers=owner,json={'benchmark':'extraction','models':['vectors']})
        assert alone.status_code==400 and 'embedding' in alone.text
        both=caller.post('/benchmarks',headers=owner,json={'benchmark':'extraction','models':['vectors','small']})
        assert both.status_code==202 and both.json()['skipped']==['vectors'] and len(both.json()['runs'])==1
        assert caller.post('/benchmarks',headers=owner,json={'benchmark':'load','models':['vectors']}).status_code==202  # how fast it loads is still a fair question
        caller.post('/benchmarks/control',headers=owner,json={'action':'clear'})


def test_shared_pause_survives_restart_and_clear_keeps_evidence(fake_llama_server, fake_cards, tmp_path):
    database=str(tmp_path/'paused.db')
    models=bench_model(.3)
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as caller:
        owner=add_user(caller,'owner','admin')
        assert caller.post('/benchmarks/control',headers=owner,json={'action':'pause'}).status_code==200
        numbers=[start(caller,owner,kind) for kind in ('speed','context')]
        time.sleep(.2)
        assert all(caller.get('/benchmarks/'+number,headers=owner).json()['state']=='queued' for number in numbers)
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as caller:
        assert caller.get('/benchmarks',headers=owner).json()['paused']
        assert caller.post('/benchmarks/control',headers=owner,json={'action':'resume'}).status_code==200
        wait(caller,owner,numbers[0],lambda run:run['samples_count']>=1)
        assert caller.post('/benchmarks/control',headers=owner,json={'action':'clear'}).status_code==200
        for number in numbers:
            assert caller.get('/v1/batches/'+number,headers=owner).json()['status']=='cancelled'
        assert caller.get('/benchmarks/'+numbers[0],headers=owner).json()['samples_count']>=1


def test_weightroom_restart_resumes_samples_without_restarting_compute(fake_llama_server,fake_cards,tmp_path):
    """A real browser submission survives its host restarting, without a benchmark listener."""
    import httpx
    from starlette.testclient import TestClient
    from weightroom.app import Settings, build_app
    from test_app import gateway as compute_gateway

    calls = []

    class Wire(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert not request.url.path.startswith('/benchmarks')
            calls.append(request.url.path)
            response = await asyncio.to_thread(compute.request, request.method, request.url.raw_path.decode(),
                headers=dict(request.headers),content=await request.aread())
            return httpx.Response(response.status_code,headers=dict(response.headers),content=response.content)

    data=tmp_path/'front'
    with compute_gateway(fake_llama_server,models=bench_model(.3),nvidia_smi=fake_cards[0]) as compute:
        owner=add_user(compute,'owner','admin')
        key=owner['authorization'].removeprefix('Bearer ')

        def front():
            app=build_app(Settings(compute='http://compute',key=key,data=str(data)),
                httpx.AsyncClient(base_url='http://compute',transport=Wire(),timeout=30))
            browser=TestClient(app)
            browser.cookies.set('weightroom',key)
            return browser

        def saved(browser, predicate):
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                run=browser.app.state.benchmarks.state.store.get(number,True)
                if predicate(run):
                    return run
                time.sleep(.03)
            raise AssertionError(run)

        with front() as browser:
            deadline=time.monotonic()+3
            while True:
                page=browser.post('/benchmarks/runs',data={'benchmark':'speed','run-model':'small'})
                assert page.status_code==200
                runs=browser.app.state.benchmarks.state.store.listing()
                if runs:
                    break
                assert time.monotonic()<deadline, page.text
                time.sleep(.05)
            number=runs[0]['id']
            sample=saved(browser,lambda run:run['samples_count']>=1)['samples'][0]
        assert compute.get('/v1/models',headers=owner).status_code==200
        with front() as browser:
            run=saved(browser,lambda run:run['state'] in ('completed','failed'))
            assert run['state']=='completed',run
            assert run['samples'][0]==sample and len(run['samples'])==3
            detail=browser.get('/benchmarks?benchmark=speed&run='+number+'&model=small')
            assert detail.status_code==200 and 'small' in detail.text
            # Wait until the owning batch closes; the worker then sleeps without task polling.
            deadline=time.monotonic()+5
            while browser.app.state.benchmarks.state.store.pending():
                assert time.monotonic()<deadline
                time.sleep(.05)
            task_calls=lambda: sum(path.startswith('/loadcoach/tasks/') for path in calls)
            before=task_calls()
            time.sleep(1.1)
            assert task_calls()==before
            # Waking an idle worker and queue controls still work through browser forms.
            browser.post('/benchmarks/control',data={'action':'pause'})
            browser.post('/benchmarks/runs',data={'benchmark':'context','run-model':'small'})
            assert browser.app.state.benchmarks.state.store.paused()
            browser.post('/benchmarks/control',data={'action':'clear'})
        assert compute.get('/v1/batches/'+number,headers=owner).json()['status']=='completed'
        assert key.encode() not in (data/'benchmarks.db').read_bytes()
        assert (data/'benchmarks.db').stat().st_mode & 0o777 == 0o600


def test_heatmap_direction_migrates_old_definitions_and_survives_restart(tmp_path):
    import sqlite3
    from benchmarks.store import Benchmarks
    path=str(tmp_path/'old.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE benchmark_definitions(id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,formula TEXT NOT NULL)')
        db.execute("INSERT INTO benchmark_definitions VALUES('context',0,'max_context_tokens * 2')")
    store=Benchmarks(path)
    context=next(d for d in store.definitions() if d['id']=='context')
    assert context['green']=='high' and not context['enabled'] and context['formula']=='max_context_tokens * 2'
    store.configure('context',False,'max_context_tokens * 2','low')
    store.db.close()
    store=Benchmarks(path)
    store.configure('context',True,'max_context_tokens')  # old clients preserve direction
    assert next(d for d in store.definitions() if d['id']=='context')['green']=='low'
    store.db.close()


def test_cuda_allocation_disconnect_becomes_failed_fit_and_search_steps_down(fake_llama_server,fake_cards):
    models=bench_model()
    models['small']['flags']['benchmark-crash-above']=1024
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0]) as c:
        owner=add_user(c,'owner','admin');n=start(c,owner,'context')
        run=wait(c,owner,n,lambda r:r['state'] in ('failed','completed'))
        assert run['state']=='completed',run['reason']
        assert run['metrics']['max_context_tokens']==1024
        bad=[s for s in run['samples'] if not s['values']['success']]
        assert bad and all('resource allocation failed' in s['values']['error'] and 'resource allocation failed' in s['log'] for s in bad)
        assert len({s['id'] for s in run['samples']})==len(run['samples'])
        assert not any('retrying after native failure' in m['text'] for m in run['messages'])
        assert c.get('/loadcoach/status',headers=owner).json()['lock'] is None


def test_repeated_unknown_native_crash_fails_after_three_attempts(fake_llama_server,fake_cards):
    models=bench_model()
    models['small']['flags'].update({'benchmark-crash-above':0,'benchmark-crash-message':'CUDA error: an illegal memory access was encountered'})
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0]) as c:
        owner=add_user(c,'owner','admin');n=start(c,owner,'context')
        run=wait(c,owner,n,lambda r:r['state'] in ('failed','completed'))
        assert run['state']=='failed' and '3 failed native attempts' in run['reason']
        assert 'illegal memory access' in run['reason']
        assert run['samples_count']==0  # unrelated CUDA faults must not become fake capacity evidence
        assert c.app.state.benchmarks.state.store.probe_attempts(n,'context-2048')[0]==3
        assert c.get('/loadcoach/status',headers=owner).json()['lock'] is None


def test_retry_limit_survives_restart_and_does_not_consume_busy_or_preempted_attempts(tmp_path):
    import httpx
    from benchmarks.worker import BenchmarkWorker,Retry
    path=str(tmp_path/'retries.db');store=Benchmarks(path)
    n=store.create('owner','small','context')
    for _ in range(3):store.probe_failure(n,'context-2048','native broke')
    store.db.close();store=Benchmarks(path)
    calls=[]
    def wire(request):
        calls.append(request)
        return httpx.Response(503,json={'error':{'message':'Compute is busy'}})
    async def check():
        async with httpx.AsyncClient(base_url='http://compute',transport=httpx.MockTransport(wire)) as client:
            worker=BenchmarkWorker(store,client)
            with pytest.raises(ValueError,match='already reached'):
                await worker.isolated(n,'context-2048',{})
            assert not calls
            for _ in range(4):
                with pytest.raises(Retry):await worker.isolated(n,'other-sample',{})
            assert store.probe_attempts(n,'other-sample')[0]==0
    asyncio.run(check());store.db.close()


def test_probe_status_tracks_native_readiness_and_original_settings(fake_llama_server, fake_cards, monkeypatch):
    """Probe overrides are not stale settings, and active inference is not labelled loading."""
    import threading
    from loadcoach.probes import Probes
    entered, release = threading.Event(), threading.Event()
    completion = Probes.completion
    async def blocked(self, server, *args, **kwargs):
        assert not server.ready  # no scheduler reuse or ordinary measurement writes
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return await completion(self, server, *args, **kwargs)
    monkeypatch.setattr(Probes, 'completion', blocked)
    models = bench_model()
    models['small']['flags'].update({'parallel': 2, 'takes': .4, 'ctx-size': 4096})
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        number = start(caller, owner, 'speed')
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                loaded = caller.get('/loadcoach/status', headers=owner).json()['loaded']
                if loaded:
                    break
                time.sleep(.01)
            assert loaded[0]['state'] == 'loading'
            assert entered.wait(5)
            loaded = caller.get('/loadcoach/status', headers=owner).json()['loaded']
            assert loaded[0]['state'] == 'probing'
            model_state = caller.get('/loadcoach/models', headers=owner).json()['models'][0]
            assert model_state['loaded'] == 'probing' and not model_state['stale']
            assert model_state['context_loaded'] != models['small']['flags']['ctx-size']
            changed = caller.post('/loadcoach/models/small', headers=owner,
                json={'flags': {'model': 'small.gguf', 'ctx-size': 8192}, 'gpus': ['GPU0']})
            assert changed.status_code == 200
            assert caller.get('/loadcoach/models', headers=owner).json()['models'][0]['stale']
        finally:
            release.set()
        run = wait(caller, owner, number, lambda run: run['state'] in ('completed', 'failed'))
        assert run['state'] == 'failed' and not run['samples']
        assert not caller.get('/loadcoach/status', headers=owner).json()['loaded']


def test_gpu_formula_uses_successful_summed_vram_and_preserves_samples(tmp_path):
    store = Benchmarks(str(tmp_path / 'gpu.db'))
    number = store.create('owner', 'model', 'load')
    store.activate(number, {'fingerprint': 'fp', 'subject': {}, 'environment': {}})
    store.configure('load', True, 'gpu_vram_mib / 1024')
    store.keep(number, 'one', {'success': True, 'context_tokens': 2048, 'load_seconds': 2, 'peak_gpu_mib': 2048}, '', '')
    store.keep(number, 'two', {'success': True, 'context_tokens': 2048, 'load_seconds': 2, 'peak_gpu_mib': 4096}, '', '')
    store.keep(number, 'failed', {'success': False, 'peak_gpu_mib': 99999}, '', '')
    store.finish(number, 'completed')
    saved = store.samples(number)
    assert store.get(number)['score'] == 4
    store.configure('load', True, 'gpu_vram_mib * parameter_count')
    assert store.get(number)['score'] is None and store.samples(number) == saved
