"""Installations expose verification, retain their destination and reject duplicate work."""
import asyncio
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route

from front_helpers import loadcoach_through
from loadcoach.files import PIECE, sha256_of
from loadcoach.groups import Group
from loadcoach.users import Users
from test_model_install import node
from test_postgresql import postgres  # noqa: F401
from test_weightroom import desk, gguf, sign_in
from turnstile.model_install import execute, plan
from turnstile.installation_store import Installations


def test_hash_reports_bytes_without_changing_digest_or_cancellation(tmp_path):
    path=tmp_path/'weights.gguf'
    content=b'v'*(PIECE+17)
    path.write_bytes(content)
    progress={}
    assert sha256_of(str(path),progress=progress)==hashlib.sha256(content).hexdigest()
    assert progress=={'done':len(content),'of':len(content)}
    stop=threading.Event();stop.set()
    with pytest.raises(InterruptedError):sha256_of(str(path),stop,progress)
    assert progress['done']==0


def test_verification_is_visible_before_reads_and_destination_remains_fixed():
    file={'path':'/front/storage/model.gguf','place':'storage_llm','tier':'storage','size':100}
    prepared=plan({'flags':{'model':file['path'],'ctx-size':2048},'gpus':0},[node('front',[file]),node('compute')],'compute','front')
    job={'machine':'compute','state':'queued'}
    available={('front',file['path']):'digest'}
    seen=[]
    async def digest(machine,path):
        assert job['state']=='verifying' and job['file']=='model.gguf'
        assert job['machine']=='compute' and job['working_machine']==machine
        seen.append((machine,path))
        await asyncio.sleep(0)
        if (machine,path) not in available:raise ValueError('Missing')
        return available[(machine,path)]
    async def promote(step):
        assert job['state']=='promoting' and job['working_machine']=='front' and job['machine']=='compute'
        available[('front',step['path'])]='digest'
    async def transfer(step,digest,progress):
        assert progress['state']=='copying' and progress['machine']=='compute'
        available[('compute',step['path'])]=digest
    asyncio.run(execute(prepared,digest,promote,transfer,job))
    assert seen[0]==('front',file['path'])
    assert ('compute','/compute/cache/model.gguf') in seen


def test_retry_during_installation_does_not_create_another_job(tmp_path,monkeypatch):
    cache=tmp_path/'cache';cache.mkdir()
    file=gguf(cache/'tiny.gguf',general__architecture='tiny')
    users=Users(str(tmp_path/'accounts.db'),{'manager':Group(can=frozenset({'manage-models'}))})
    users.add('manager','manager');headers={'authorization':'Bearer '+users.new_key('manager')}
    release=asyncio.Event()
    async def paused(prepared,*args,**kwargs):
        await release.wait()
        return prepared['settings']
    monkeypatch.setattr('turnstile.app.install_files',paused)
    body={'name':'pending','machine':'compute','settings':{'flags':{'model':file,'ctx-size':2048},'gpus':0}}
    with loadcoach_through(tmp_path,users,places={'llm':str(cache)},llama_server='/bin/true',data=str(tmp_path)) as front:
        first=front.post('/turnstile/models/install',headers=headers,json=body)
        assert first.status_code==202,first.text
        for _ in range(2):
            repeat=front.post('/turnstile/models/install',headers=headers,json=body)
            assert repeat.status_code==409 and 'already being installed' in repeat.text
        jobs=front.get('/turnstile/artifacts/distributions',headers=headers).json()['distributions']
        assert len(jobs)==1
        front.portal.call(release.set)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            jobs=front.get('/turnstile/artifacts/distributions',headers=headers).json()['distributions']
            if jobs[0]['state'] in ('complete','failed'):break
            time.sleep(.05)
        assert jobs[0]['state']=='complete',jobs


def test_models_page_shows_active_installations_without_a_bookmarked_job(desk):
    server,wire,_=desk
    sign_in(server,'jordan')
    job={'id':'progress','kind':'model-install','name':'Flux2Dev','machine':'compute','destination':'compute',
         'state':'verifying','working_machine':'front','file':'flux2-dev.gguf','done':8*2**30,'of':20*2**30,
         'progress_available':True}
    async def distributions(request):return JSONResponse({'distributions':[job]})
    wire.front.router.routes.insert(0,Route('/turnstile/artifacts/distributions',distributions))
    response=server.get('/models?part=list&kind=image')
    assert response.status_code==200
    assert 'Flux2Dev' in response.text and 'Verifying SHA-256' in response.text and 'flux2-dev.gguf' in response.text
    assert 'on front' in response.text
    job['state']='complete'
    assert 'Flux2Dev' not in server.get('/models?part=list&kind=image').text
    assert 'Installed' in server.get('/models?part=list&kind=image&install=progress').text


def test_saved_request_survives_crash_without_retaining_credentials(tmp_path):
    source=str(tmp_path/'jobs.db')
    owner=SimpleNamespace(identity='owner-id',name='owner',can=lambda ability:False)
    other=SimpleNamespace(identity='other-id',name='other',can=lambda ability:False)
    job={'id':'durable','kind':'model-install','name':'image','destination':'compute','machine':'compute','state':'verifying'}
    asked={'name':'image','settings':{'engine':'sdcpp','gpus':1},'machines':['compute','rig'],
           'source_machine':'front','api_key':'must-not-be-saved'}
    first=Installations(source);first.add(job,owner,asked);first.db.close()
    second=Installations(source)
    recovered=second.recover()['durable']
    assert recovered['state']=='failed' and recovered['interrupted'] and 'restart' in recovered['why']
    assert second.retry_request('durable',owner)=={'name':'image','settings':asked['settings'],'machine':'compute','source_machine':'front'}
    with pytest.raises(PermissionError):second.retry_request('durable',other)
    raw=second.db.execute('SELECT request,progress FROM model_installations').fetchone()
    assert 'must-not-be-saved' not in ''.join(raw)
    second.db.close()


def test_interrupted_request_can_retry_after_service_restart(tmp_path,monkeypatch):
    cache=tmp_path/'cache';cache.mkdir();file=gguf(cache/'tiny.gguf',general__architecture='tiny')
    users=Users(str(tmp_path/'accounts.db'),{
        'manager':Group(can=frozenset({'manage-models'})),'reader':Group()})
    for name,group in [('owner','manager'),('other','manager'),('reader','reader')]:users.add(name,group)
    headers={'authorization':'Bearer '+users.new_key('owner')}
    release=asyncio.Event()
    async def paused(*args,**kwargs):await release.wait()
    monkeypatch.setattr('turnstile.app.install_files',paused)
    config={'places':{'llm':str(cache)},'llama_server':'/bin/true','data':str(tmp_path)}
    with loadcoach_through(tmp_path,users,**config) as front:
        response=front.post('/turnstile/models/install',headers=headers,json={
            'name':'recover-me','machine':'compute','settings':{'flags':{'model':file,'ctx-size':2048},'gpus':0}})
        assert response.status_code==202,response.text
        original=response.json()['id']
    monkeypatch.setattr('turnstile.app.install_files',execute)
    with loadcoach_through(tmp_path,users,**config) as front:
        job=next(j for j in front.get('/turnstile/artifacts/distributions',headers=headers).json()['distributions'] if j['id']==original)
        assert job['state']=='failed' and job['interrupted'] and job['can_retry']
        retry='/turnstile/models/install/'+original+'/retry'
        assert front.post(retry).status_code==401
        for name,status in [('reader',403),('other',403)]:
            assert front.post(retry,headers={'authorization':'Bearer '+users.new_key(name)}).status_code==status
        response=front.post(retry,headers=headers,json={'machine':'not-the-saved-destination'})
        assert response.status_code==202,response.text
        assert response.json()['destination']=='compute'
        retried=response.json()['id']
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            jobs=front.get('/turnstile/artifacts/distributions',headers=headers).json()['distributions']
            job=next(j for j in jobs if j['id']==retried)
            if job['state'] in ('complete','failed'):break
            time.sleep(.05)
        assert job['state']=='complete',job
        old=next(j for j in jobs if j['id']==original)
        assert old['retry_id']==retried and not old['can_retry']



def test_postgresql_installation_request_recovery(postgres):
    owner=SimpleNamespace(identity='owner-id',name='owner',can=lambda ability:False)
    store=Installations(postgres)
    job={'id':'postgres-job','kind':'model-install','name':'Flux2Dev','destination':'compute','machine':'compute','state':'copying'}
    request={'name':'Flux2Dev','settings':{'engine':'sdcpp','gpus':1},'source_machine':'front'}
    store.add(job,owner,request);store.db.close()
    recovered=Installations(postgres)
    assert recovered.recover()['postgres-job']['interrupted']
    assert recovered.retry_request('postgres-job',owner)=={**request,'machine':'compute'}
    recovered.db.close()
