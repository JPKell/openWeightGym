"""Embedding probes never generate text; retain native counts and share the existing queue."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from benchmarks import MODULES
from benchmarks.progress import progress
from benchmarks.suites import embedding_context, embedding_speed, embedding_load
from loadcoach.probes import Probes
from loadcoach.runner import Refusal, Runner, Server, ServiceRamCapacity
from loadcoach.config import Config
from loadcoach.models import Models, at_context
import loadcoach.probes as probe_module
from benchmark_clients import gateway
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder, model
from test_benchmarks import bench_model, wait
from test_weightroom import gguf, desk, sign_in


KINDS = ('embedding_context','embedding_speed','embedding_load')


def models(limit=1024, **flags):
    result = bench_model()
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=limit)
    result['vectors'] = model('vectors.gguf',['GPU0'], **{'ctx-size':limit,'mib':1000,'embedding':True,**flags})
    return result


@pytest.mark.parametrize('kind', ('load', *KINDS))
def test_cpu_embedding_probes_keep_native_and_ram_evidence_without_gpu_telemetry(fake_llama_server,kind):
    settings=models(limit=512)
    settings['vectors'] = model('vectors.gguf', 0, **{'ctx-size':512,'gpu-layers':0,
                                                    'embedding':True,'pooling':'last'})
    with gateway(fake_llama_server,models=settings) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,kind)
        run=wait(caller,owner,number,lambda row:row['state'] in ('completed','failed'),seconds=40)
        assert run['state']=='completed',run['reason']
        assert run['score'] > 0 and run['samples']
        for sample in run['samples']:
            values=sample['values']
            assert values['success'] and values['device']=='cpu' and values['cards']==[]
            assert values['peak_gpu_mib'] is None
            assert values['peak_ram_mib'] >= values['rss_mib'] > 0
            assert sample['evidence']['resource'] == {'device':'cpu',
                'peak_ram_mib':values['peak_ram_mib'],'rss_mib':values['rss_mib']}
            if kind in ('embedding_context','embedding_speed'):
                assert values['input_tokens'] > 0 and values['output_tokens']==0
                assert values['embedding_dimensions']==3
                assert sample['evidence']['endpoint']=='/v1/embeddings'
                assert sample['evidence']['usage']['prompt_tokens']==values['input_tokens']
            else:
                assert 'embedding_dimensions' not in values
        status=caller.get('/loadcoach/status',headers=owner).json()
        assert status['lock'] is None and status['loaded']==[]


def test_cpu_generation_subject_remains_unsupported(fake_llama_server):
    settings={'text':model('text.gguf',0,**{'ctx-size':512,'gpu-layers':0})}
    gguf('text.gguf',general__architecture='tiny',tiny__context_length=512)
    with gateway(fake_llama_server,models=settings) as caller:
        owner=add_user(caller,'owner','admin')
        response=caller.post('/benchmarks',headers=owner,json={'benchmark':'load','models':['text']})
        assert response.status_code == 400
        assert 'CPU probes require an embedding model' in response.text


def test_cpu_probe_refuses_insufficient_host_ram_before_start(fake_llama_server):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    async def check():
        async with httpx.AsyncClient() as client:
            runner=Runner(Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server),client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            runner.host_memory=lambda:{'free':0}
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            with pytest.raises(Refusal) as refusal:
                await probes.sample(task,{'context':512,'inputs':0,'outputs':0,'warmup_outputs':0,'embedding':True})
            assert refusal.value.status==503 and not runner.servers
    asyncio.run(check())


def test_cpu_probe_records_configured_admission_bound_without_claiming_measurement(fake_llama_server):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    async def check():
        async with httpx.AsyncClient() as client:
            runner=Runner(Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server,
                                 host_ram_limit_mib=128),client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            result=await probes.sample(task,{'context':512,'inputs':0,'outputs':0,'warmup_outputs':0,'embedding':True})
            values=result['values']
            assert not values['success'] and values['capacity_basis']=='fixed_admission_bound'
            assert values['rss_mib'] is None and values['peak_ram_mib'] is None
            assert result['evidence']['admission']['native_attempted'] is False
            assert result['log']=='' and 'no native load was attempted' in values['error']
            assert not runner.servers
    asyncio.run(check())


def test_cpu_context_admission_bound_has_explicit_unmeasured_samples(fake_llama_server):
    settings=models(limit=512)
    settings['vectors']=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    with gateway(fake_llama_server,models=settings,host_ram_limit_mib=128) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_context')
        run=wait(caller,owner,number,lambda row:row['state'] in ('completed','failed'),seconds=10)
        assert run['state']=='failed' and 'No tested input produced a complete embedding' in run['reason']
        assert run['samples_count']>0
        assert all(not row['values']['success'] and row['values']['capacity_basis']=='fixed_admission_bound'
                   and row['values']['rss_mib'] is None and row['evidence']['admission']['native_attempted'] is False
                   for row in run['samples'])
        assert not any('retrying' in message['text'] for message in run['messages'])
        status=caller.get('/loadcoach/status',headers=owner).json()
        assert status['lock'] is None and status['loaded']==[]


def test_cpu_service_charge_limit_does_not_bound_process_rss_estimate(
        fake_llama_server,monkeypatch,tmp_path):
    settings=models(limit=512)
    settings['vectors']=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,
                                                'embedding':True,'parallel':1})
    database=str(tmp_path/'service-bound.db')
    library=Models(database,first=settings)
    library.resident_measure('vectors',at_context(settings['vectors'],512),700)
    library.db.close()
    monkeypatch.setattr('loadcoach.runner.memory_snapshot',lambda:{'total':8192,'available':7000,
                        'service_limit':512,'service_used':300,'service_available':212})
    with gateway(fake_llama_server,models=settings,database=database) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_context')
        run=wait(caller,owner,number,lambda row:row['state'] in ('completed','failed'),seconds=20)
        assert run['state']=='completed',run['reason']
        assert run['metrics']['max_context_tokens']==480
        assert all(row['values']['success'] and row['evidence']['endpoint']=='/v1/embeddings'
                   for row in run['samples'])
        assert not any('Yielded; incomplete sample discarded' in message['text'] for message in run['messages'])
        assert caller.get('/loadcoach/status',headers=owner).json()['lock'] is None


def test_cpu_zero_service_room_stays_retryable_even_with_host_rss_room(fake_llama_server,monkeypatch):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    monkeypatch.setattr('loadcoach.runner.memory_snapshot',lambda:{'total':8192,'available':7000,
                        'service_limit':1500,'service_used':1500,'service_available':0,'service_scope':'ancestor'})
    async def check():
        async with httpx.AsyncClient() as client:
            runner=Runner(Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server),client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            with pytest.raises(Refusal) as refusal:
                await probes.sample(task,{'context':512,'inputs':0,'outputs':0,'warmup_outputs':0,'embedding':True})
            assert refusal.value.status==503 and not runner.servers
    asyncio.run(check())


def test_cpu_current_service_room_below_required_stays_retryable(fake_llama_server):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    async def check():
        async with httpx.AsyncClient() as client:
            runner=Runner(Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server),client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            runner.host_memory=lambda:{'free':100,'service_limit':4096,'service_scope':'self',
                                       'service_used':2000,'total':8192}
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            with pytest.raises(Refusal) as refusal:
                await probes.sample(task,{'context':512,'inputs':0,'outputs':0,'warmup_outputs':0,'embedding':True})
            assert refusal.value.status==503 and not runner.servers
    asyncio.run(check())


def test_cpu_context_search_records_measured_budget_failure_and_steps_down(fake_llama_server,monkeypatch):
    settings=models(limit=512)
    settings['vectors']=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    def measured(pid):
        arguments=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        context=int(arguments[arguments.index(b'--ctx-size')+1])
        return 700 if context>256 else 64
    monkeypatch.setattr(probe_module,'resident_mib',measured)
    with gateway(fake_llama_server,models=settings,host_ram_limit_mib=512) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_context')
        run=wait(caller,owner,number,lambda row:row['state'] in ('completed','failed'),seconds=40)
        assert run['state']=='completed',run['reason']
        assert run['metrics']['max_context_tokens']==224
        failed=[row for row in run['samples'] if not row['values']['success']]
        assert {row['values']['context_tokens'] for row in failed}=={384,512}
        assert all(row['values']['capacity_basis']=='measured_configured_ram'
                   and row['values']['peak_ram_mib']==700
                   and row['values']['peak_gpu_mib'] is None
                   and row['evidence']['resource']['rss_mib']==700 for row in failed)
        assert any(row['values']['success'] and row['values']['embedding_dimensions']==3
                   and row['evidence']['usage']['prompt_tokens']==224 for row in run['samples'])
        assert caller.get('/loadcoach/status',headers=owner).json()['lock'] is None


def test_cpu_own_service_breach_is_measured_capacity_and_context_searches_down(fake_llama_server,monkeypatch):
    settings=models(limit=512)
    settings['vectors']=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    def measured(pid):
        arguments=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        context=int(arguments[arguments.index(b'--ctx-size')+1])
        return 1100 if context>256 else 64
    monkeypatch.setattr(probe_module,'resident_mib',measured)
    original=Runner.assert_cpu_memory
    def own_service(self,server,*,isolated_probe=False):
        if isolated_probe and server.settings['flags']['ctx-size']>256:
            raise ServiceRamCapacity(server.model,1500,1500)
        return original(self,server,isolated_probe=isolated_probe)
    monkeypatch.setattr(Runner,'assert_cpu_memory',own_service)
    with gateway(fake_llama_server,models=settings) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_context')
        run=wait(caller,owner,number,lambda row:row['state'] in ('completed','failed'),seconds=40)
        assert run['state']=='completed',run['reason']
        assert run['metrics']['max_context_tokens']==224
        failed=[row for row in run['samples'] if not row['values']['success']]
        assert {row['values']['context_tokens'] for row in failed}=={384,512}
        assert all(row['values']['capacity_basis']=='measured_service_ram'
                   and row['values']['rss_mib']==row['values']['peak_ram_mib']==1100
                   and row['values']['service_used_mib']==row['values']['service_limit_mib']==1500
                   and row['evidence']['resource']=={'device':'cpu','rss_mib':1100,'peak_ram_mib':1100}
                   and row['evidence']['service']=={'scope':'self','used_mib':1500,
                                                      'limit_mib':1500,'native_attempted':True}
                   for row in failed)
        assert any(row['values']['success'] and row['evidence']['endpoint']=='/v1/embeddings'
                   for row in run['samples'])
        assert not any('Yielded; incomplete sample discarded' in message['text'] for message in run['messages'])
        assert caller.get('/loadcoach/status',headers=owner).json()['lock'] is None


def test_cpu_speed_measured_budget_failure_is_terminal(fake_llama_server,monkeypatch):
    settings=models(limit=512)
    settings['vectors']=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    monkeypatch.setattr(probe_module,'resident_mib',lambda _pid:700)
    with gateway(fake_llama_server,models=settings,host_ram_limit_mib=512) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_speed')
        run=wait(caller,owner,number,lambda row:row['state'] in ('completed','failed'),seconds=20)
        assert run['state']=='failed' and 'configured host RAM limit' in run['reason']
        assert run['samples_count']==0  # no successful throughput is implied
        assert caller.get('/loadcoach/status',headers=owner).json()['lock'] is None


def test_cpu_probe_keeps_service_headroom_pressure_retryable(fake_llama_server,monkeypatch):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    monkeypatch.setattr('loadcoach.runner.memory_snapshot',lambda:{'available':4096,'service_available':0,
                        'service_limit':1500,'service_used':1500,'service_scope':'ancestor'})
    async def check():
        async with httpx.AsyncClient() as client:
            runner=Runner(Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server),client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            runner.host_memory=lambda:{'free':1000}
            started=[]
            original_start=runner.start
            async def start_and_track(server):
                started.append(server)
                await original_start(server)
            runner.start=start_and_track
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            with pytest.raises(Refusal) as refusal:
                await probes.sample(task,{'context':512,'inputs':0,'outputs':0,'warmup_outputs':0,'embedding':True})
            assert refusal.value.status==503 and 'headroom' in str(refusal.value)
            assert started and started[0].process.returncode is not None and not runner.servers
    asyncio.run(check())


def test_cpu_mid_request_service_pressure_keeps_watcher_503(fake_llama_server,monkeypatch):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True,
                                      'benchmark-delay':.8})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    pressure=[False]
    monkeypatch.setattr('loadcoach.runner.memory_snapshot',lambda:{'available':4096,
                        'service_available':0 if pressure[0] else 1000,
                        'service_limit':1500,'service_used':1500 if pressure[0] else 500,
                        'service_scope':'ancestor'})
    async def check():
        async with httpx.AsyncClient() as client:
            runner=Runner(Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server),client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            runner.host_memory=lambda:{'free':1000}
            started=[]
            original_start=runner.start
            async def start_and_track(server):
                started.append(server)
                await original_start(server)
            runner.start=start_and_track
            original_embedding=probes.embedding
            async def embedding(server,prompt):
                pressure[0]=True  # only after readiness and tokenizer completed
                return await original_embedding(server,prompt)
            probes.embedding=embedding
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            options={'context':512,'inputs':128,'outputs':0,'warmup_outputs':0,
                     'embedding':True,'text':'A complete native embedding input'}
            with pytest.raises(Refusal) as refusal:
                await probes.sample(task,options)
            assert pressure[0] and refusal.value.status==503
            assert 'headroom' in str(refusal.value)
            assert started and started[0].process.returncode is not None and not runner.servers
    asyncio.run(check())


@pytest.mark.parametrize('high_after,inputs',[(0,0),(4,128)])
def test_cpu_probe_reaps_child_when_measured_ram_exceeds_admitted_estimate(
        fake_llama_server,monkeypatch,high_after,inputs):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True,
                                      'benchmark-delay':.8})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    reads=[0]
    def observed(_pid):
        reads[0]+=1
        return 512 if reads[0]>high_after else 32
    monkeypatch.setattr(probe_module,'resident_mib',observed)
    async def check():
        async with httpx.AsyncClient() as client:
            config=Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server,
                          host_ram_limit_mib=128)
            runner=Runner(config,client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            native_started=[False]
            original_embedding=probes.embedding
            async def embedding(server,prompt):
                native_started[0]=True
                return await original_embedding(server,prompt)
            probes.embedding=embedding
            runner.ram_required=lambda *_:1  # estimated room passes, actual RSS must still govern
            runner.host_memory=lambda:{'free':1000}
            started=[]
            original_start=runner.start
            async def start_and_track(server):
                started.append(server)
                await original_start(server)
            runner.start=start_and_track
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            options={'context':512,'inputs':inputs,'outputs':0,'warmup_outputs':0,
                     'embedding':True,'text':'A complete native embedding input'}
            result=await probes.sample(task,options)
            values=result['values']
            assert not values['success'] and values['capacity_basis']=='measured_configured_ram'
            assert 'configured host RAM limit' in values['error']
            assert values['peak_ram_mib'] >= values['rss_mib'] == 512
            assert result['evidence']['resource']['rss_mib']==512
            assert started and started[0].process.returncode is not None
            assert not runner.servers
            if inputs:
                assert native_started[0]
    asyncio.run(check())


@pytest.mark.parametrize('cancel_sample',[False,True])
def test_cpu_violation_waits_for_watcher_stop_before_final_cleanup(fake_llama_server,monkeypatch,cancel_sample):
    gguf('vectors.gguf',general__architecture='tiny',tiny__context_length=512)
    settings=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,'embedding':True})
    library=Models(':memory:',first={'vectors':settings})
    file=Path('vectors.gguf')
    library.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    monkeypatch.setattr(probe_module,'resident_mib',lambda _pid:512)
    async def check():
        async with httpx.AsyncClient() as client:
            runner=Runner(Config(models={'vectors':settings},gpus=(),llama_server=fake_llama_server,
                                 host_ram_limit_mib=128),client,models=library)
            probes=Probes(runner,lambda:False,lambda _:True)
            mark,subject=probes.subject('vectors')
            runner.ram_required=lambda *_:1
            runner.host_memory=lambda:{'free':1000}
            child=SimpleNamespace(pid=1,returncode=None)
            entered,released=asyncio.Event(),asyncio.Event()
            cancelled,stops=[False],[]
            async def start(server):
                server.process=child
                await asyncio.sleep(.05)  # the RAM watcher begins stopping before readiness returns
            async def stop(server):
                stops.append(server)
                if len(stops)==1:
                    entered.set()
                    try:
                        await released.wait()
                    except asyncio.CancelledError:
                        cancelled[0]=True
                        raise
                child.returncode=0
            runner.start,runner.stop=start,stop
            task={'id':'trial','model':'vectors','model_id':subject['model_id'],'subject':subject,'fingerprint':mark}
            options={'context':512,'inputs':0,'outputs':0,'warmup_outputs':0,'embedding':True}
            work=asyncio.create_task(probes.sample(task,options))
            await asyncio.wait_for(entered.wait(),1)
            if cancel_sample:
                work.cancel()
            await asyncio.sleep(.1)
            released.set()
            if cancel_sample:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(work,1)
            else:
                result=await asyncio.wait_for(work,1)
                assert not result['values']['success']
                assert result['values']['capacity_basis']=='measured_configured_ram'
            assert not cancelled[0]
            assert child.returncode==0 and len(stops)==2 and not runner.servers
    asyncio.run(check())


def test_cpu_embedding_cancel_releases_process_and_lock(fake_llama_server):
    settings=models(limit=512,**{'benchmark-delay':1})
    settings['vectors']=model('vectors.gguf',0,**{'ctx-size':512,'gpu-layers':0,
                                                'embedding':True,'benchmark-delay':1})
    with gateway(fake_llama_server,models=settings) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_speed')
        wait(caller,owner,number,lambda row:caller.get('/loadcoach/status',headers=owner).json()['lock'] is not None)
        response=caller.post('/v1/batches/'+number+'/cancel',headers=owner)
        assert response.status_code==200
        assert wait(caller,owner,number,lambda row:row['state']=='cancelled')['state']=='cancelled'
        status=caller.get('/loadcoach/status',headers=owner).json()
        assert status['lock'] is None and status['loaded']==[]


def start(caller, owner, kind):
    response = caller.post('/benchmarks',headers=owner,json={'benchmark':kind,'models':['vectors']})
    assert response.status_code == 202,response.text
    return response.json()['runs'][0]


def test_embedding_types_are_marked_filtered_and_enforced_before_queueing(fake_llama_server,fake_cards):
    with gateway(fake_llama_server,models=models(),nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        caller.post('/benchmarks/control',headers=owner,json={'action':'pause'})
        catalogue = caller.get('/v1/models',headers=owner).json()['data']
        assert next(row for row in catalogue if row['id']=='vectors')['embedding'] is True
        for kind in KINDS:
            refused=caller.post('/benchmarks',headers=owner,json={'benchmark':kind,'models':['small']})
            assert refused.status_code == 400
        for kind in ('context','speed','extraction'):
            refused=caller.post('/benchmarks',headers=owner,json={'benchmark':kind,'models':['vectors']})
            assert refused.status_code == 400
        response=caller.post('/benchmarks',headers=owner,json={'benchmark':'embedding_speed','models':['small','vectors']})
        assert response.status_code == 202 and response.json()['skipped'] == ['small']
        assert len(response.json()['runs']) == 1
        caller.post('/benchmarks/control',headers=owner,json={'action':'clear'})


@pytest.mark.parametrize('kind',KINDS)
def test_actual_worker_finishes_embedding_measurements_with_retained_evidence(fake_llama_server,fake_cards,kind):
    with gateway(fake_llama_server,models=models(),nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,kind)
        run=wait(caller,owner,number,lambda r:r['state'] in ('completed','failed'),seconds=30)
        assert run['state']=='completed',run['reason']
        assert run['score'] > 0 and run['samples']
        assert run['subject']['protocol']['embedding'] is True
        for sample in run['samples']:
            values=sample['values']
            assert values['success'] and values['peak_gpu_mib']==1000
            assert 'generation_tokens_per_second' not in values and 'ttft_seconds' not in values
            if kind != 'embedding_load':
                assert values['output_tokens']==0 and values['embedding_dimensions']==3
                assert values['embedding_tokens_per_second'] == pytest.approx(values['input_tokens']/values['request_seconds'])
                assert sample['evidence']['endpoint']=='/v1/embeddings'
                assert sample['evidence']['usage']['prompt_tokens']==values['input_tokens']
                assert sample['log'].count('fake embedding input=') == (2 if kind=='embedding_speed' else 1)
            else:
                assert 'fake embedding input=' not in sample['log']
        if kind=='embedding_context':
            assert run['metrics']['max_context_tokens']==992 and len(run['samples'])==1
            assert run['samples'][0]['values']['context_tokens']==1024
        else:
            assert len(run['samples'])==(9 if kind=='embedding_speed' else 3) and run['metrics']['repetitions']==3
        assert caller.get('/v1/batches/'+number,headers=owner).json()['status']=='completed'
        assert caller.get('/loadcoach/status',headers=owner).json()['lock'] is None


def test_physical_batch_capacity_is_a_result_and_limits_embedding_speed(fake_llama_server,fake_cards):
    with gateway(fake_llama_server,models=models(**{'benchmark-embedding-limit':700}),nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        context=start(caller,owner,'embedding_context')
        fit=wait(caller,owner,context,lambda r:r['state'] in ('completed','failed'))
        assert fit['state']=='completed' and fit['metrics']['max_context_tokens']==480
        assert any(not sample['values']['success'] and 'physical batch size' in sample['values']['error'] for sample in fit['samples'])
        speed=start(caller,owner,'embedding_speed')
        run=wait(caller,owner,speed,lambda r:r['state'] in ('completed','failed'))
        assert run['state']=='completed' and len(run['samples'])==6
        assert {sample['values']['input_tokens'] for sample in run['samples']}=={128,480}
        assert all(sample['values']['context_tokens']==512 for sample in run['samples'])
        assert run['metrics']['input_tokens']==480


def test_embedding_context_and_speed_reserve_native_boundary_token_space(fake_llama_server,fake_cards):
    with gateway(fake_llama_server,models=models(limit=512,**{'benchmark-embedding-exclusive-context':True}),
                 nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_context')
        run=wait(caller,owner,number,lambda r:r['state'] in ('completed','failed'))
        assert run['state']=='completed',run['reason']
        assert run['subject']['protocol']['reserve_tokens']==32
        assert run['metrics']['max_context_tokens']==480
        assert run['samples'][0]['values']['context_tokens']==512
        assert run['samples'][0]['values']['input_tokens']==480
        number=start(caller,owner,'embedding_speed')
        speed=wait(caller,owner,number,lambda r:r['state'] in ('completed','failed'))
        assert speed['state']=='completed',speed['reason']
        assert speed['subject']['protocol']['reserve_tokens']==32
        assert len(speed['samples'])==6
        assert {sample['values']['input_tokens'] for sample in speed['samples']}=={128,480}
        assert all(sample['values']['context_tokens']==512 for sample in speed['samples'])


def test_embedding_context_search_resumes_saved_process_sizes():
    class Store:
        saved=[]
        def samples(self, number):
            assert number=='run'
            return self.saved
    class Worker:
        store=Store()
        interrupt=True
        async def probe(self, number, sample, context, inputs, outputs, **options):
            assert inputs==context-32 and outputs==0
            assert options['capacity_is_result'] is True
            assert sample not in [row['id'] for row in self.store.saved]
            self.store.saved.append({'id':sample,'values':{
                'context_tokens':context,'input_tokens':inputs,'success':inputs<=700}})
            if self.interrupt:
                self.interrupt=False
                raise RuntimeError('simulated worker stop after saved sample')
    worker=Worker()
    with pytest.raises(RuntimeError):
        asyncio.run(embedding_context.measure(worker,{'id':'run'},1024))
    assert [row['values']['context_tokens'] for row in worker.store.saved]==[1024]
    asyncio.run(embedding_context.measure(worker,{'id':'run'},1024))
    assert [row['values']['context_tokens'] for row in worker.store.saved]==[1024,512,768]
    assert embedding_context.summarize(worker.store.saved)['max_context_tokens']==480


@pytest.mark.parametrize(('cards','tokens','read_timeout'),[
    ((),8192,1700),((),8193,3300),(('0',),8193,600)])
def test_native_embedding_read_timeout_respects_cpu_and_gpu_probe_budget(cards,tokens,read_timeout):
    seen=[]
    async def response(request):
        seen.append(request.extensions['timeout'])
        return httpx.Response(200,json={'usage':{'prompt_tokens':tokens},
            'data':[{'index':0,'embedding':[.2,.8]}]})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            probe=SimpleNamespace(runner=SimpleNamespace(client=client))
            values,_,_=await Probes.embedding(probe,SimpleNamespace(url='http://native',cards=cards),[1]*tokens)
            assert values['input_tokens']==tokens and values['embedding_dimensions']==2
    asyncio.run(check())
    assert len(seen)==1 and seen[0]=={'connect':5,'read':read_timeout,'write':600,'pool':600}


@pytest.mark.parametrize('change', ['short','missing_usage','multiple','nested','zero','bool','nan','cache','truncated','not_object','bad_timings'])
def test_incomplete_or_invalid_native_embeddings_are_not_successful(change):
    result={'data':[{'index':0,'embedding':[.2,.8]}],'usage':{'prompt_tokens':3}}
    if change=='short': result['usage']['prompt_tokens']=2
    if change=='missing_usage': result.pop('usage')
    if change=='multiple': result['data']*=2
    if change=='nested': result['data'][0]['embedding']=[[.2,.8]]
    if change=='zero': result['data'][0]['embedding']=[0.,0.]
    if change=='bool': result['data'][0]['embedding']=[True,.2]
    if change=='nan': result['data'][0]['embedding']=[float('nan')]
    if change=='cache': result['timings']={'cache_n':1}
    if change=='bad_timings': result['timings']=None
    if change=='truncated': result['truncated']=True
    if change=='not_object': result=[]
    async def response(request):
        return httpx.Response(200,content=json.dumps(result),headers={'content-type':'application/json'})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            probe=SimpleNamespace(runner=SimpleNamespace(client=client))
            with pytest.raises(ValueError):
                await Probes.embedding(probe,SimpleNamespace(url='http://native',cards=('0',)),[1,2,3])
    asyncio.run(check())


def test_embedder_page_cards_results_and_model_page_keep_families_separate(desk,tmp_path):
    browser,_,users=desk
    library=Models(str(tmp_path/'loadcoach.db'))
    gguf(str(tmp_path/'embed.gguf'),general__architecture='tiny',tiny__context_length=512)
    library.put('vector-only',{'flags':{'model':str(tmp_path/'embed.gguf'),'ctx-size':512,'embedding':True}})
    library.put('text-only',{'flags':{'model':str(tmp_path/'text.gguf'),'ctx-size':2048}})
    library.put('private-vector',{'flags':{'model':str(tmp_path/'embed.gguf'),'ctx-size':512,'embedding':True}})
    users.change('pat',own={'models':['vector-only','text-only']})
    sign_in(browser,'pat')
    page=browser.get('/benchmarks/embedders').text
    assert '<h1 class="page-title wrap">Embedders</h1>' in page
    assert 'aria-label="Select vector-only at compute"' in page and 'text-only' not in page and 'private-vector' not in page
    assert 'data-card-key="benchmark-resources"' in page
    assert 'id="benchmark-resource-heatmap"' in page and 'id="benchmark-resource-table"' in page
    assert '<th scope="row" class="fw-normal ps-3">' not in page
    assert all(f'?benchmark={kind}#node-measurements' in page for kind in KINDS)
    assert 'aria-label="Embedder benchmarks"' not in page
    assert 'name="benchmark" value="embedding_context"' in page
    assert '<h1 class="page-title wrap">Runs</h1>' in browser.get('/benchmarks').text
    assert 'vector-only' not in browser.get('/benchmarks/results').text
    for kind, unit in zip(KINDS, ('tokens', 'tokens/s', '1/s')):
        nodes = browser.get('/benchmarks/node-measurements', params={'kind':'embedding', 'benchmark':kind}).text
        grid = nodes.split('id="benchmark-resource-table"', 1)[1].split('</table>', 1)[0]
        assert 'vector-only' in grid and 'text-only' not in grid and 'private-vector' not in grid
        assert f'data-unit="{unit}"' in grid
        assert all(f'?benchmark={candidate}&amp;kind=embedding#node-measurements' in nodes for candidate in KINDS)
        assert 'aria-current="page">Embedders</a>' in nodes
    for query in ('benchmark=embedding_speed', 'family=embedding&benchmark=embedding_speed', 'kind=embedding&benchmark=speed'):
        nodes = browser.get('/benchmarks/node-measurements?' + query).text
        assert 'aria-current="page">Embedders</a>' in nodes
        assert 'kind=llm&amp;benchmark=speed' in nodes
        assert 'kind=embedding&amp;benchmark=embedding_speed' in nodes
    language = browser.get('/benchmarks/node-measurements?kind=llm&benchmark=embedding_speed').text
    assert 'text-only' in language and 'vector-only' not in language
    assert '?benchmark=speed&amp;kind=llm#node-measurements' in language
    compare=browser.get('/benchmarks/compare?family=embedding&selection=1&model=vector-only&model=text-only&bench=embedding_speed').text
    assert 'name="family" value="embedding"' in compare and 'text-only' not in compare
    assert '<th scope="row">Input throughput</th>' in compare
    assert 'First token' not in compare
    live=browser.get('/benchmarks/embedders?part=live').text
    assert 'id="benchmark-embedder-active" hx-swap-oob="innerHTML"' in live and 'start-benchmark' not in live
    sign_in(browser,'jordan')
    store=browser.app.state.benchmarks.state.store
    number=store.create('jordan','vector-only','embedding_speed')
    store.activate(number,{'fingerprint':'fixture','subject':{'settings':{'flags':{'embedding':True}}},'environment':{}})
    store.keep(number,'embedding-speed-512-0',{'success':True,'context_tokens':512,'input_tokens':512,'output_tokens':0,
               'request_seconds':.1,'embedding_tokens_per_second':5120,'embeddings_per_second':10,'embedding_dimensions':3},
               '{}','',{'endpoint':'/v1/embeddings','usage':{'prompt_tokens':512}})
    store.finish(number,'completed');store.synced(number)
    node_result = browser.get('/benchmarks/node-measurements?kind=embedding&benchmark=embedding_speed').text
    assert '/benchmarks/run/' + number in node_result and 'data-score="5120' in node_result
    detail=browser.get('/benchmarks/run/'+number)
    assert detail.status_code==200
    assert 'Input tokens/s' in detail.text and 'Dimensions' in detail.text and 'Embedding receipt' in detail.text
    assert 'Write tokens/s' not in detail.text and 'First token (s)' not in detail.text and 'Checks and answer key' not in detail.text
    card=browser.get('/models/vector-only?part=benchmark').text
    assert 'embedding_speed' in card and 'benchmark=extraction' not in card


def test_embedding_speed_summary_and_progress_use_their_own_workload():
    samples=[{'values':{'success':True,'context_tokens':768,'input_tokens':size,
                       'embedding_tokens_per_second':rate,'request_seconds':size/rate}}
             for size,rate in [(128,100),(512,200),(512,300),(512,400),(768,50)]]
    summary=embedding_speed.summarize(samples)
    assert summary['embedding_tokens_per_second']==300 and summary['input_tokens']==512
    run={'benchmark':'embedding_speed','samples':samples[:1],'subject':{'settings':{'flags':{'ctx-size':768}}},
         'state':'running','started':1,'messages':[]}
    assert progress(run,2)['total']==9
    run['benchmark']='embedding_context';run['samples']=[]
    assert progress(run,2)['adaptive'] and progress(run,2)['total'] is None
    assert all(MODULES[kind].DEFINITION['embedding'] for kind in KINDS)
    assert [MODULES[kind].DEFINITION['version'] for kind in KINDS]==[2,2,1]


@pytest.mark.parametrize('kind',[*KINDS,'load'])
def test_resource_definitions_expose_actual_cpu_ram_without_inventing_gpu_usage(kind):
    module=MODULES[kind]
    assert ('peak_ram_mib','Peak resident RAM','MiB') in module.DEFINITION['kpis']
    samples=[{'values':{'success':True,'device':'cpu','context_tokens':2048,'input_tokens':512,
                       'load_seconds':1.0,'embedding_tokens_per_second':100,
                       'peak_ram_mib':ram,'peak_gpu_mib':None}} for ram in (128,256)]
    summary=module.summarize(samples)
    assert summary['peak_ram_mib']>0
    assert summary.get('peak_gpu_mib') is None


def test_embedding_speed_yields_to_foreground_and_resumes_saved_samples(fake_llama_server,fake_cards,tmp_path):
    settings=models(**{'benchmark-delay':.2})
    database=str(tmp_path/'resume.db')
    with gateway(fake_llama_server,models=settings,nvidia_smi=fake_cards[0],database=database) as caller:
        owner=add_user(caller,'owner','admin')
        number=start(caller,owner,'embedding_speed')
        first=wait(caller,owner,number,lambda run:run['samples_count']>=1)['samples'][0]
        response=caller.post('/v1/chat/completions',headers=owner,json={'model':'small','messages':[{'role':'user','content':'hi'}]})
        assert response.status_code==200
        wait(caller,owner,number,lambda run:any('Yielded' in message['text'] for message in run['messages']))
        caller.post('/benchmarks/control',headers=owner,json={'run':number,'action':'pause'})
    with gateway(fake_llama_server,models=settings,nvidia_smi=fake_cards[0],database=database) as caller:
        caller.post('/benchmarks/control',headers=owner,json={'run':number,'action':'resume'})
        run=wait(caller,owner,number,lambda run:run['state'] in ('completed','failed'),seconds=30)
        assert run['state']=='completed' and len(run['samples'])==9
        assert run['samples'][0]==first
        assert len({sample['id'] for sample in run['samples']})==9


def test_task_probe_rejects_embedding_mode_bypasses(fake_llama_server,fake_cards):
    with gateway(fake_llama_server,models=models(),nvidia_smi=fake_cards[0]) as caller:
        owner=add_user(caller,'owner','admin')
        caller.post('/benchmarks/control',headers=owner,json={'action':'pause'})
        number=start(caller,owner,'embedding_speed')
        base={'context':512,'inputs':128,'outputs':0,'embedding':True,'text':'fixed passage'}
        for changes in ({'embedding':False},{'embedding':'true'},{'warmup_embeddings':1},
                        {'outputs':16},{'warmup_outputs':16},{'evaluation':{'question':'no'}},
                        {'warmup_embeddings':True,'inputs':0}):
            response=caller.post('/loadcoach/tasks/'+number+'/probe',headers=owner,json={**base,**changes})
            assert response.status_code==400,response.text
        assert caller.get('/loadcoach/status',headers=owner).json()['loaded']==[]
        caller.post('/benchmarks/control',headers=owner,json={'action':'clear'})


def test_cancellation_during_final_memory_measurement_still_terminates_the_process():
    async def check():
        entered=asyncio.Event()
        async def measure(server):
            entered.set()
            await asyncio.Event().wait()
        class Process:
            returncode=None
            terminated=False
            waited=False
            def terminate(self):
                self.terminated=True
                self.returncode=0
            async def wait(self):
                self.waited=True
                return self.returncode
        process=Process()
        runner=object.__new__(Runner)
        runner.measure,runner.seen_at,runner.retiring=measure,123,set()
        server=Server('test',(),ready=True,process=process,stop_seconds=.02)
        task=asyncio.create_task(runner.stop(server))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,1)
        assert process.terminated and process.waited and runner.seen_at==0
        assert not runner.retiring and server.stopping.done()
    asyncio.run(check())
