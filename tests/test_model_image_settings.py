"""Cross-feature acceptance: policy authority, complete relocation and durable one-pair work."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from benchmarks.eligibility import compatible, evidence_matches, resolve_location
from benchmarks.store import Benchmarks
from db.maintenance import Gate
from loadcoach.encoder_migration import manifest, prepare, rewrite, finish, restore_files
from loadcoach.models import Models
from test_schedules import seeded
from test_weightroom import desk, sign_in, gguf
from weightroom.app import fit_line
from weightroom.schedules import ScheduleWorker


def test_image_policy_uses_existing_authority_and_does_not_opt_users_in(desk):
    server, wire, _ = desk
    sign_in(server,'pat')
    assert server.get('/settings/image').status_code==403
    assert server.post('/settings/image',data={'adult_previews':'yes'}).status_code==403
    wire.front.state.console_settings.changes.seen('turnstile',{'image_adult_previews':{'value':False}})
    sign_in(server,'jordan')
    assert server.get('/settings/image').status_code==200
    assert server.post('/settings/image',data={'adult_previews':'yes'}).status_code==200
    policy=wire.front.state.mirrorwall.adult_previews_allowed
    assert policy() is True
    assert wire.front.state.mirrorwall.store.preferences('another-owner').get('adult_previews',False) is False
    assert server.post('/settings/image',data={}).status_code==200
    assert policy() is False
    assert 'name="value-image_adult_previews"' not in server.get('/settings').text


def test_encoder_relocation_preserves_ids_shared_consumers_and_recovers_after_removal(tmp_path):
    root=tmp_path/'models';old=root/'image/text_encoders/package/weights.gguf';old.parent.mkdir(parents=True);old.write_bytes(b'weights')
    sidecar=old.with_name('tokenizer.json');sidecar.write_bytes(b'{}')
    weights=root/'image/diffusion.gguf';weights.write_bytes(b'diffusion')
    library=Models(tmp_path/'models.db')
    settings={'engine':'sdcpp','flags':{'model':str(weights),'ctx-size':0},'sdcpp':{'components':{'text_encoder':str(old),'vae':str(weights)}}}
    for name in ('one','two'):library.put(name,settings)
    node={'name':'compute','answering':True,'places':[{'name':'image','folder':str(root/'image')}],
          'files':[{'path':str(p),'place':'image','size':p.stat().st_size,'facts':{}} for p in (old,sidecar,weights)]}
    models={'compute':[{'name':name,'model_id':library.identity(name),'settings':settings} for name in ('one','two')]}
    with pytest.raises(ValueError,match='Every configured'):
        manifest([node,{'answering':False}],models)
    plan=manifest([node],models)['nodes'][0]
    record=tmp_path/'manifest.json';prepare(plan,record)
    identity=library.identity('one');revision=library.file_revision('one')
    rewrite(plan,library,record)
    assert library.identity('one')==identity and library.file_revision('one')>revision
    new=library.stored('one')['sdcpp']['components']['text_encoder']
    assert Path(new).read_bytes()==b'weights' and Path(new).with_name('tokenizer.json').exists()
    assert library.stored('two')['sdcpp']['components']['text_encoder']==new
    assert library.stored('one')['sdcpp']['components']['vae']==str(weights)
    with pytest.raises(ValueError,match='Every affected consumer'):finish(plan,record)
    plan['verified_consumers']=['one','two'];finish(plan,record);assert not old.exists()
    restore_files(plan,record);rewrite(plan,library,record,rollback=True)
    assert old.read_bytes()==b'weights' and library.stored('one')==settings
    # Recovery refuses a different destination instead of replacing it.
    Path(new).write_bytes(b'bad')
    with pytest.raises(ValueError):prepare(plan,record)


def test_fixed_fit_profiles_do_not_interpolate_or_turn_unknown_into_zero():
    model={'engine':'sdcpp','gpus':['A','B'],'size':1,'image_profile':{'qualified':True},
           'measurements':[{'context':0,'mib':[5000,4000]}]}
    assert fit_line(model,['A','B'])['points']==[[0,[5000,4000]]]
    model['image_profile']['qualified']=False
    assert fit_line(model,['A','B'])['points']==[]
    assert not compatible({'capabilities':{'load_probes':True}}, {'id':'extraction','quality':True})
    assert compatible({'capabilities':{'load_probes':True}}, {'id':'load'})
    with pytest.raises(ValueError,match='unknown'):
        resolve_location({'id':'one','locations':[{'machine':'other'}]},'preferred',['preferred'])


def test_atomic_queue_reservation_excludes_manual_and_scheduled_duplicate(tmp_path):
    one=Benchmarks(tmp_path/'bench.db');two=Benchmarks(tmp_path/'bench.db')
    number=one.create('owner','old-name','load','stable-id',location='compute',reserve=True)
    assert two.create('owner','new-name','load','stable-id',location='compute',reserve=True) is None
    assert two.create('owner','new-name','load','recreated-id',location='compute',reserve=True)
    one.finish(number,'failed')
    assert two.create('owner','new-name','load','stable-id',location='compute',reserve=True)


def test_outstanding_occurrence_freezes_one_pair_across_lost_reply_and_restart(tmp_path):
    store=seeded(tmp_path,kind='outstanding',machine='preferred',model_ids=[],benchmarks=['load'])
    due=store.rows()[0]['next_at'];bench=Benchmarks(tmp_path/'bench.db');calls=[]
    environment={'engine_hash':'engine','machine_hash':'machine'}
    model={'id':'first','owned_by':'loadcoach','model_id':'model-a','fingerprint':'fp',
           'locations':[{'id':'first','machine':'other','model_id':'model-a','fingerprint':'fp','engine_id':'llama_cpp'}]}
    async def wire(request):
        if request.url.path.endswith('/inspect'):
            return httpx.Response(200,json={'models':[model], 'unavailable':[], 'environments':{'other:llama_cpp':environment}})
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:raise httpx.ConnectError('Reply lost after acceptance')
        return httpx.Response(201,json={'user':'owner','model':'first','model_id':'model-a','machine':'other',
            'fingerprint':'fp','environment':environment,'subject':{'settings':{'flags':{'ctx-size':2048}}}})
    async def check():
        async with httpx.AsyncClient(base_url='http://front',transport=httpx.MockTransport(wire)) as client:
            worker=ScheduleWorker(store,client,bench,None,Gate(),asyncio.Event());await worker.tick(due)
            assert store.occurrences()[0]['state']=='dispatching'
            frozen=store.occurrences()[0]['spec']['target'];assert frozen['machine']=='other' and frozen['fallback_reason']
            # Rebuild the worker as after a restart. No new pair is selected.
            worker=ScheduleWorker(store,client,bench,None,Gate(),asyncio.Event());await worker.tick(due+1)
            assert calls[0]==calls[1] and len(bench.listing())==1
            assert store.occurrences()[0]['state']=='running'
    asyncio.run(check())


def test_outstanding_freshness_rename_recreation_environment_and_failure_fairness(tmp_path):
    store=seeded(tmp_path,kind='outstanding',machine='compute',model_ids=[],benchmarks=['load'])
    due=store.rows()[0]['next_at'];store.claim(due);occurrence=store.occurrences()[0]
    bench=Benchmarks(tmp_path/'fresh.db');environment={'engine_hash':'engine','machine_hash':'machine'}
    model={'id':'renamed','owned_by':'loadcoach','model_id':'stable','fingerprint':'fp',
           'locations':[{'id':'renamed','machine':'compute','model_id':'stable','fingerprint':'fp','engine_id':'llama_cpp'}]}
    number=bench.create('owner','old-name','load','stable',location='compute')
    bench.activate(number,{'fingerprint':'fp','environment':environment,'subject':{}});bench.finish(number,'completed')
    async def check():
        async def wire(request):return httpx.Response(200,json={'models':[model], 'unavailable':[], 'environments':{'compute:llama_cpp':environment}})
        async with httpx.AsyncClient(base_url='http://front',transport=httpx.MockTransport(wire)) as client:
            worker=ScheduleWorker(store,client,bench,None,Gate(),asyncio.Event())
            assert await worker.choose_outstanding(occurrence,due)==(None,'Nothing outstanding.')
            unrelated=bench.create('owner','outside-scope','load','unrelated',location='compute')
            bench.finish(unrelated,'failed')
            assert await worker.choose_outstanding(occurrence,__import__('time').time())==(None,'Nothing outstanding.')
            environment['engine_hash']='changed'
            target,_=await worker.choose_outstanding(occurrence,due);assert target['model_id']=='stable'
            environment['engine_hash']='engine';model['locations'][0]['model_id']='recreated';model['model_id']='recreated'
            target,_=await worker.choose_outstanding(occurrence,due);assert target['model_id']=='recreated'
            failed=bench.create('owner','renamed','load','recreated',location='compute');bench.finish(failed,'failed')
            now=__import__('time').time()
            target,message=await worker.choose_outstanding(occurrence,now);assert target is None and 'cooldown' in message
            target,_=await worker.choose_outstanding(occurrence,now+3601);assert target['model_id']=='recreated'
    asyncio.run(check())


def test_occurrence_target_compare_and_set_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    store=seeded(tmp_path,kind='outstanding');store.claim(store.rows()[0]['next_at'])
    occurrence=store.occurrences()[0]
    from weightroom.schedules import Schedules
    other=Schedules(tmp_path/'schedules.db');barrier=threading.Barrier(2)
    def freeze(pair):
        db,target=pair;barrier.wait();return db.freeze_target(occurrence,{'model_id':target})
    with ThreadPoolExecutor(max_workers=2) as executor:
        targets=list(executor.map(freeze,[(store,'one'),(other,'two')]))
    assert targets[0]==targets[1]==store.occurrences()[0]['spec']['target']
