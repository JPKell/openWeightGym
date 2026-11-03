"""Cache preflight, verified installation and per-node/global model configuration."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import loadcoach_through, machine_key
from loadcoach import statement
from loadcoach.files import SPARE
from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.app import Settings, build_app
from turnstile.model_configuration import generation, with_generation
from turnstile.model_install import execute, plan
from test_two_machines import Two
from test_weightroom import desk, gguf, sign_in


def node(name, files=(), free=10 * SPARE):
    return {'name': name, 'answering': True, 'files': list(files), 'places': [
        {'name': 'llm', 'folder': '/' + name + '/cache', 'tier': 'cache', 'free': free, 'filesystem': '1'},
        {'name': 'storage_llm', 'folder': '/' + name + '/storage', 'tier': 'storage', 'free': free, 'filesystem': '2'}]}


def test_bundle_space_preflight_counts_every_file_and_preserves_storage():
    files = [{'path': '/front/storage/' + name, 'size': SPARE, 'place': 'storage_llm', 'tier': 'storage'}
             for name in ('weights.gguf', 'projector.gguf')]
    settings = {'flags': {'model': files[0]['path'], 'mmproj': files[1]['path'], 'ctx-size': 8192}, 'gpus': 'any'}
    with pytest.raises(ValueError, match='No files or models were changed'):
        plan(settings, [node('front', files), node('cpu', free=2 * SPARE)], 'cpu', 'front')
    prepared = plan(settings, [node('front', files), node('cpu')], 'cpu', 'front')
    assert len(prepared['steps']) == 4
    assert prepared['settings']['flags']['model'] == '/cpu/cache/weights.gguf'
    assert prepared['settings']['flags']['mmproj'] == '/cpu/cache/projector.gguf'
    assert settings['flags']['model'] == '/front/storage/weights.gguf'


def test_bundle_cannot_merge_different_sources_into_one_destination_file():
    first = {'path':'/first/cache/same.gguf','size':100,'place':'llm','tier':'cache'}
    second = {**first,'path':'/second/cache/same.gguf'}
    settings = {'flags':{'model':first['path'],'mmproj':second['path'],'ctx-size':2048},'gpus':0}
    with pytest.raises(ValueError,match='would collide'):
        plan(settings,[node('first',[first]),node('second',[second]),node('cpu')],'cpu')


def test_audio_discovery_includes_package_data_but_preserves_installation_receipts(tmp_path):
    from loadcoach.files import model_files_in
    folder=tmp_path/'audio'; package=folder/'package';package.mkdir(parents=True)
    for name in ('model.gguf','tokenizer.json','.suite-package-fixture.json'):
        (package/name).write_text('fixture')
    assert {file.name for file in model_files_in(str(folder))} == {'model.gguf','tokenizer.json'}


def test_hash_conflict_prevents_every_copy_even_with_equal_sizes():
    source = {'path': '/front/storage/a.gguf', 'size': 100, 'place': 'storage_llm', 'tier': 'storage'}
    cached = {**source, 'path': '/cpu/cache/a.gguf', 'place': 'llm', 'tier': 'cache'}
    settings = {'flags': {'model': source['path'], 'ctx-size': 2048}, 'gpus': 0}
    prepared = plan(settings, [node('front', [source]), node('cpu', [cached])], 'cpu', 'front')
    calls = []
    async def digest(machine, path):
        return 'different' if machine == 'cpu' else 'source'
    async def copy(*args):
        calls.append(args)
    with pytest.raises(ValueError, match='SHA-256'):
        asyncio.run(execute(prepared, digest, copy, copy, {}))
    assert calls == []


def test_generation_merge_keeps_destination_loading_flags_and_context():
    source = {'flags': {'model': '/a.gguf', 'temp': .2, 'reasoning': 'off', 'ctx-size': 32000, 'parallel': 4}, 'gpus': ['CUDA0']}
    target = {'flags': {'model': '/b.gguf', 'temp': .8, 'top-k': 40, 'ctx-size': 4096, 'parallel': 1,
                        'threads': 8, 'cache-type-k': 'q8_0'}, 'gpus': 0}
    merged = with_generation(target, generation(source))
    assert merged['gpus'] == 0 and merged['flags']['ctx-size'] == 4096 and merged['flags']['parallel'] == 1
    assert merged['flags']['threads'] == 8 and merged['flags']['cache-type-k'] == 'q8_0'
    assert merged['flags']['temp'] == .2 and merged['flags']['reasoning'] == 'off' and 'top-k' not in merged['flags']


@pytest.mark.parametrize('enough', [False, True])
def test_install_promotes_storage_only_after_space_check_and_creates_model_last(tmp_path, monkeypatch, enough):
    cache, storage = tmp_path / 'cache', tmp_path / 'storage'
    cache.mkdir(); storage.mkdir()
    file = Path(gguf(storage / 'tiny.gguf', general__architecture='tiny'))
    original = file.read_bytes()
    users = Users(str(tmp_path / 'accounts.db'), {'manager': Group(can=frozenset({'manage-models'}))})
    users.add('manager', 'manager')
    headers = {'authorization': 'Bearer ' + users.new_key('manager')}
    if not enough:
        monkeypatch.setattr('loadcoach.app.room', lambda folder: {'total': 10 * SPARE, 'free': SPARE, 'filesystem':'same'})
    with loadcoach_through(tmp_path, users, places={'llm': str(cache), 'storage_llm': str(storage)},
                           llama_server='/bin/true', gpus=('CUDA0',)) as front:
        response = front.post('/turnstile/models/install', headers=headers, json={'name':'tiny', 'machine':'compute',
                              'source_machine':'compute', 'settings':{'flags':{'model':str(file), 'ctx-size':2048}, 'gpus':0}})
        if not enough:
            assert response.status_code == 409 and 'cache space' in response.text
            assert not list(cache.iterdir())
            assert not front.get('/loadcoach/models', headers=headers).json()['models']
        else:
            assert response.status_code == 202, response.text
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = front.get('/turnstile/artifacts/distributions', headers=headers).json()['distributions'][-1]
                if job['state'] in ('complete', 'failed'):
                    break
                time.sleep(.05)
            assert job['state'] == 'complete', job
            model = front.get('/loadcoach/models', headers=headers).json()['models'][0]
            assert model['flags']['model'] == str(cache / file.name)
            assert (cache / file.name).read_bytes() == original
        assert file.read_bytes() == original


def test_global_save_synchronizes_signed_nodes_but_loading_changes_remain_local(tmp_path):
    users = Users(str(tmp_path / 'accounts.db'), {'manager': Group(can=frozenset({'manage-models'}))})
    users.add('manager', 'manager')
    headers = {'authorization':'Bearer ' + users.new_key('manager')}
    primary_key = machine_key(tmp_path)
    rig_key = str(tmp_path / 'rig.key'); statement.replace_key(rig_key, 'r' * 40)
    states = {'compute':{'flags':{'model':'/gpu/a.gguf','ctx-size':8192,'parallel':4,'temp':.7},'gpus':['CUDA0']},
              'rig':{'flags':{'model':'/cpu/a.gguf','ctx-size':2048,'parallel':1,'temp':.7,'threads':8},'gpus':0}}
    offline = []
    def worker(name, key):
        async def answer(request):
            if request.url.path == '/loadcoach/models':
                if name in offline:
                    return JSONResponse({'error':{'message':'offline'}},503)
                return JSONResponse({'models':[{'name':'a','model_id':name,'sha256':'a'*64,**states[name]}], 'cards':['CUDA0'], 'defaults':{}})
            if request.url.path == '/loadcoach/models/a':
                states[name] = json.loads(await request.body())
                return JSONResponse({'name':'a',**states[name]})
            return JSONResponse({'error':{'message':'unused fixture endpoint'}},404)
        return statement.Statements(Starlette(routes=[Route('/{path:path}', answer, methods=['GET','POST'])]),key)
    wire = Two(worker('compute', primary_key), worker('rig', rig_key))
    settings = Settings(compute='compute', data=str(tmp_path), machine_key_file=primary_key,
                        machines={'rig':{'compute':'http://compute:9442','machine_key_file':rig_key}})
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users), base_url='http://front:8442') as front:
        path = '/turnstile/machines/compute/model-api/models/a'
        changed = deepcopy(states['compute']); changed['flags'].update(temp=.2, reasoning='off', **{'ctx-size':16000})
        response = front.post(path, headers=headers, json=changed)
        assert response.status_code == 200, response.text
        assert states['rig']['gpus'] == 0 and states['rig']['flags']['ctx-size'] == 2048 and states['rig']['flags']['parallel'] == 1
        assert states['rig']['flags']['temp'] == .2 and states['rig']['flags']['reasoning'] == 'off'
        offline.append('rig')
        local = deepcopy(states['compute']); local['flags']['ctx-size'] = 24000
        assert front.post(path, headers=headers, json=local).status_code == 200
        global_change = deepcopy(local); global_change['flags']['temp'] = .9
        refused = front.post(path, headers=headers, json=global_change)
        assert refused.status_code == 409 and states['compute']['flags']['temp'] == .2


def test_catalogue_pills_and_node_settings_render_for_every_model_type(desk, tmp_path):
    from loadcoach.models import Models
    browser, _, _ = desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    for name, engine in [('language','llama_cpp'), ('picture','sdcpp'), ('song','audiocpp')]:
        settings = {'engine':engine,'flags':{'model':gguf(tmp_path / (name+'.gguf')),'ctx-size':2048 if engine == 'llama_cpp' else 0},'gpus':0}
        if engine == 'sdcpp': settings['sdcpp'] = {'family':'sdxl','split_mode':'single'}
        if engine == 'audiocpp': settings['audiocpp'] = {'family':'ace_step','kind':'music','path':settings['flags']['model']}
        library.put(name,settings)
    sign_in(browser,'jordan')
    for kind, included, excluded in [('llm','language','picture'),('image','picture','song'),('audio','song','language')]:
        page = browser.get('/models?kind='+kind)
        assert page.status_code == 200
        table = page.text.split('<table id="models-table"')[1].split('</table>', 1)[0]
        assert included in table and excluded not in table
        assert page.text.index('Add a model') < page.text.index('<table id="models-table"')
        detail = browser.get('/models/'+included)
        assert detail.status_code == 200, detail.text
        assert 'Add model to node' in detail.text
        if kind != 'llm':
            assert 'Generation defaults' in detail.text and 'Load-time measurements only.' in detail.text


@pytest.mark.parametrize('engine', ['sdcpp', 'audiocpp'])
def test_multimedia_benchmarks_allow_loading_and_refuse_other_work(tmp_path, engine):
    from engines import engine_for
    from test_benchmarks import queue_fixture
    app, submitted = queue_fixture(tmp_path, capabilities=engine_for(engine).capabilities({}))
    for definition in app.state.store.definitions():
        app.state.store.configure(definition['id'], True, definition['formula'])
    with TestClient(app) as client:
        headers = {'authorization':'Bearer test'}
        for benchmark in ('context', 'speed', 'reasoning'):
            refused = client.post('/benchmarks',headers=headers,json={'benchmark':benchmark,'models':['small']})
            assert refused.status_code == 400, refused.text
            assert not submitted
        loaded = client.post('/benchmarks',headers=headers,json={'benchmark':'load','models':['small']})
        assert loaded.status_code == 202, loaded.text
        outstanding = client.post('/benchmarks/outstanding',headers=headers,json={'model':'small'})
        assert outstanding.status_code == 202, outstanding.text
        assert {task['metadata']['benchmark'] for task in submitted} == {'load'}


def test_image_loading_waits_for_native_weights_without_generation():
    from mirrorwall.runtime import create_app
    class Native:
        starts = 0
        async def start(self):
            self.starts += 1
        async def stop(self):
            pass
    native = Native()
    with TestClient(create_app({'token':'private'},native)) as client:
        headers = {'authorization':'Bearer private'}
        assert client.get('/health',headers=headers).status_code == 200 and native.starts == 0
        assert client.post('/load').status_code == 401 and native.starts == 0
        result = client.post('/load',headers=headers)
        assert result.json()['loaded'] and not result.json()['context_available'] and native.starts == 1


@pytest.mark.parametrize('engine', ['sdcpp','audiocpp'])
def test_multimedia_probe_uses_runner_load_and_cleanup_only(engine):
    from types import SimpleNamespace
    from engines.llama_cpp.probes import Probes
    settings = {'engine':engine,'flags':{'model':'/fixture.gguf','ctx-size':0},'gpus':0,engine:{}}
    calls=[]; servers={}
    async def start(server):
        calls.append('start');server.process=SimpleNamespace(pid=1234,returncode=None);server.url='http://fixture'
    async def stop(server):
        calls.append('stop')
    async def post(url,**kwargs):
        calls.append(url)
        return httpx.Response(200,json={'loaded':True},request=httpx.Request('POST',url))
    def resident(server):
        server.rss_peak_mib=server.rss_mib=100
    runner=SimpleNamespace(models=SimpleNamespace(get=lambda name:settings,fingerprint=lambda settings,name:'mark'),
        fit=lambda *args:(settings,(),[]),servers=servers,start=start,stop=stop,sample_resident=resident,
        assert_cpu_memory=lambda *args,**kwargs:None,client=SimpleNamespace(post=post))
    probes=Probes(runner,lambda:False,lambda task:True)
    task={'id':'task','model':'model','fingerprint':'mark','subject':{'settings':settings}}
    result=asyncio.run(probes.load_sample(task,{'context':0,'inputs':0,'outputs':0}))
    assert result['values']['success'] and result['values']['context_available'] is False
    assert calls == (['start','http://fixture/load','stop'] if engine=='sdcpp' else ['start','stop'])
    assert servers == {}
    with pytest.raises(ValueError,match='load-only'):
        asyncio.run(probes.load_sample(task,{'context':128,'inputs':64,'outputs':1}))


@pytest.mark.parametrize('tamper', [False, True])
def test_cross_node_install_streams_into_the_verified_atomic_receiver(tmp_path, tamper):
    import hashlib
    from starlette.responses import Response
    from spotter.app import Settings as SpotterSettings, build_app as build_spotter
    source, destination = tmp_path/'source', tmp_path/'destination'
    source.mkdir(); destination.mkdir()
    content = b'fixture model bytes'
    (source/'weights.gguf').write_bytes(content)
    users = Users(str(tmp_path/'accounts.db'), {'manager':Group(can=frozenset({'manage-models'}))})
    users.add('manager','manager')
    headers = {'authorization':'Bearer '+users.new_key('manager')}
    primary_key=machine_key(tmp_path); rig_key=str(tmp_path/'rig.key');statement.replace_key(rig_key,'r'*40)
    created=[]
    def worker(name, folder, key):
        async def answer(request):
            if request.url.path == '/loadcoach/files':
                return JSONResponse({'places':[{'name':'llm','folder':str(folder),'free':10*SPARE,'tier':'cache','filesystem':'one'}],
                    'files':[{'path':str(file),'size':file.stat().st_size,'place':'llm','tier':'cache'} for file in folder.glob('*.gguf')]})
            if request.url.path == '/loadcoach/models':
                rows = [{'name':'source','model_id':'source-id','flags':{'model':str(source/'weights.gguf'),'ctx-size':8192,'temp':.9},'gpus':['CUDA0']}] if name == 'compute' else []
                return JSONResponse({'models':rows,'cards':['CUDA0'],'defaults':{}})
            if request.url.path == '/loadcoach/engines':
                return JSONResponse({'engines':[{'id':'llama_cpp','enabled':True,'available':True}]})
            if request.url.path == '/loadcoach/files/hash':
                file=Path(request.query_params['path'])
                if file.parent != folder or not file.is_file():return JSONResponse({'error':{'message':'missing'}},404)
                return JSONResponse({'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
            if request.url.path == '/loadcoach/files/content':
                assert request.query_params['path']==str(source/'weights.gguf')
                return Response(b'corrupt model bytes' if tamper else content,media_type='application/octet-stream')
            if request.url.path == '/loadcoach/models/copied':
                settings=json.loads(await request.body())
                assert Path(settings['flags']['model']).read_bytes()==content
                created.append(settings)
                return JSONResponse(settings,201)
            return JSONResponse({'error':{'message':'unused fixture endpoint'}},404)
        return statement.Statements(Starlette(routes=[Route('/{path:path}',answer,methods=['GET','POST'])]),key)
    receiver = build_spotter(SpotterSettings(services={},database=str(tmp_path/'spotter.db'), history=str(tmp_path/'history.db'),
        machine_key_file=rig_key,places={'llm':str(destination)}))
    class Wire(httpx.AsyncBaseTransport):
        wires = {8442:httpx.ASGITransport(app=worker('compute',source,primary_key)),
                 9442:httpx.ASGITransport(app=worker('rig',destination,rig_key)),9444:httpx.ASGITransport(app=receiver)}
        async def handle_async_request(self,request):
            return await self.wires[request.url.port].handle_async_request(request)
    settings=Settings(compute='compute',data=str(tmp_path),machine_key_file=primary_key,ports=(8442,8443,8444),
        machines={'rig':{'compute':'http://compute:9442','spotter':'http://compute:9444','machine_key_file':rig_key}})
    with TestClient(build_app(settings,httpx.AsyncClient(transport=Wire()),users=users),base_url='http://front:8442') as front:
        result=front.post('/turnstile/models/install',headers=headers,json={'name':'copied','machine':'rig','source_machine':'compute','source_model_id':'source-id',
            'settings':{'flags':{'model':str(source/'weights.gguf'),'ctx-size':2048,'temp':.1},'gpus':0,'order':10}})
        assert result.status_code==202,result.text
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=front.get('/turnstile/artifacts/distributions',headers=headers).json()['distributions'][-1]
            if job['state'] in ('failed','complete'):break
            time.sleep(.05)
        assert job['state']==('failed' if tamper else 'complete'),job
        assert bool(created) is not tamper
        if created:
            assert created[0]['flags']['temp'] == .9 and created[0]['flags']['ctx-size'] == 2048 and created[0]['gpus'] == 0
        assert (destination/'weights.gguf').exists() is not tamper
        assert not list(destination.glob('*.part'))
        assert (source/'weights.gguf').read_bytes()==content
