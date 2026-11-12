"""Private image ownership, temporary retention and bounded stream contracts."""
import asyncio
import base64
import io
import json

from PIL import Image
import pytest
import httpx

from loadcoach.batches import Batches
from mirrorwall.store import Store
from mirrorwall import stream
from mirrorwall import civitai
from test_weightroom import desk, sign_in


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB', (32, 24), 'blue').save(output, format='PNG')
    return output.getvalue()


def test_private_assets_and_thumbnails_use_stable_owner_ids_and_no_log_is_memory_only(tmp_path):
    batches = Batches(str(tmp_path / 'accounts.db'))
    store = Store(batches.db, tmp_path / 'data')
    one = store.save('owner-id', image_bytes())
    assert store.content('other-owner', one['id']) is None
    assert store.content('other-owner', one['id'], True) is None
    assert store.content('owner-id', one['id']) == image_bytes()
    assert store.path(one['id']).stat().st_mode & 0o777 == 0o600
    quiet = store.save('owner-id', image_bytes(), temporary=True)
    assert store.content('owner-id', quiet['id']) == image_bytes()
    assert not store.path(quiet['id']).exists()
    assert not store.db.execute('SELECT 1 FROM mirrorwall_assets WHERE id=?', (quiet['id'],)).fetchone()
    assert not store.delete('other-owner', one['id'])
    assert store.delete('owner-id', one['id'])
    assert store.content('owner-id', one['id']) is None


def test_workflow_versions_cannot_be_claimed_by_another_owner(tmp_path):
    batches = Batches(str(tmp_path / 'accounts.db'))
    store = Store(batches.db, tmp_path)
    one = store.save_preset('one', 'Product', 'klein', 'model-1', {'steps':6})
    with pytest.raises(ValueError, match='yours'):
        store.save_preset('two', 'Stolen', 'klein', 'model-1', {}, one['id'])
    two = store.save_preset('one', 'Product', 'klein', 'model-1', {'steps':8}, one['id'])
    assert two['version'] == 2 and two['settings']['steps'] == 8
    assert store.presets('two') == []


def test_image_batches_and_their_files_do_not_follow_a_recreated_username(tmp_path):
    batches=Batches(str(tmp_path/'accounts.db'));store=Store(batches.db,tmp_path)
    source=batches.put_file('same-name','images.jsonl','batch',b'private input')
    store.bind_upload(source['id'],'old-owner')
    assert store.visible('old-owner',source)
    assert not store.visible('new-owner',source)
    batch=batches.start('same-name','/v1/images/generations',source['id'],[{'custom_id':'one','body':{'model':'flux','prompt':'A boat'}}])
    assert not store.visible('old-owner',batch)  # an interrupted ownership bind fails closed
    store.bind_job(batch['id'],'old-owner','model-id','flux',{})
    batches.keep(batch['id'],0,200,'',{'data':[{'asset_id':'private-render'}]})
    ended=batches.finish(batch['id']);result=batches.file(ended['output_file_id'])
    assert store.visible('old-owner',result)
    assert not store.visible('new-owner',result)


def test_civitai_filters_previews_and_installs_only_hash_verified_model_data(tmp_path):
    import hashlib
    content=b'fixture image weights';sha=hashlib.sha256(content).hexdigest()
    requests=[]
    def provider(request):
        requests.append(request)
        if request.url.path=='/api/v1/model-versions/123':
            return httpx.Response(200,json={'files':[
                {'name':'style.safetensors','sizeKB':len(content)/1024,'hashes':{'SHA256':sha},'downloadUrl':'https://civitai.com/api/download/models/123'},
                {'name':'../escape.pth','sizeKB':1,'hashes':{'SHA256':sha},'downloadUrl':'https://localhost/private'},
                {'name':'unsafe.py','sizeKB':1,'hashes':{'SHA256':sha},'downloadUrl':'https://civitai.com/api/download/models/123'}]})
        return httpx.Response(200,content=content)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            files=await civitai.files(client,'civitai/123','private-token')
            assert len(files)==1
            progress={};target=tmp_path/'style.safetensors'
            await civitai.fetch(client,'civitai/123',files[0],target,'private-token',progress)
            assert target.read_bytes()==content and not target.with_suffix('.safetensors.part').exists()
            assert json.loads(target.with_name(target.name+'.source.json').read_text())['sha256']==sha
    asyncio.run(run())
    assert all('private-token' not in str(request.url) for request in requests)
    assert all(request.headers['authorization']=='Bearer private-token' for request in requests)
    preview={'url':'https://image.civitai.com/preview.png','nsfw':'X'}
    assert civitai.preview(preview) is None
    assert civitai.preview(preview,True)==preview['url']
    assert civitai.preview({'url':'http://127.0.0.1/private','nsfw':False},True) is None


def test_image_stream_survives_chunk_boundaries_and_does_not_retain_pixels_in_diagnostics():
    value = {'type':'image_generation.completed','b64_json':'pixel-data','settings':{'steps':6}}
    encoded = stream.encode(value)
    async def chunks():
        for index in range(0, len(encoded), 7): yield encoded[index:index+7]
    async def collect():
        return [one async for one in stream.events(chunks())]
    assert asyncio.run(collect()) == [value]
    capture = stream.Capture('text/event-stream')
    for index in range(0, len(encoded), 7): capture.feed(encoded[index:index+7])
    assert capture.outcome() == 'success'
    assert b'pixel-data' not in capture.snapshot()
    assert stream.completed(capture.snapshot())


def test_adult_previews_require_global_permission_and_each_users_opt_in(desk,monkeypatch):
    from functools import partial
    from types import SimpleNamespace
    browser,switch,users=desk
    captured=[]
    async def search(http,words,token='',adult=False):
        captured.append(adult)
        return []
    monkeypatch.setattr(civitai,'search',search)
    changes=switch.front.state.console_settings.changes
    changes.seen('turnstile',{'image_adult_previews':{'value':False}})
    def ask(method,path,**kwargs):
        # The front app also serves WeightRoom's background polls: use its existing
        # event loop rather than sharing its clients and stores across TestClient loops.
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=switch.front),base_url='http://front:8442') as client:
                return await client.request(method,path,**kwargs)
        return browser.portal.call(request)
    api=SimpleNamespace(get=partial(ask,'GET'),post=partial(ask,'POST'))
    sign_in(browser,'jordan')
    jordan={'authorization':'Bearer '+browser.cookies['weightroom']}
    setting='/loadcoach/settings/turnstile'
    preferences='/mirrorwall/preferences'
    searches=('/loadcoach/hub?source=civitai&search=style&adult=true&adult=true',
              '/turnstile/artifacts/hub?source=civitai&search=style&adult=true',
              '/loadcoach/%68ub?source=civitai&search=style&adult=true')
    assert api.get(preferences,headers=jordan).json()=={'adult_previews':False,'adult_previews_allowed':False}
    assert 'id="mw-adult"' not in browser.get('/civitai').text
    assert api.post(preferences,headers=jordan,json={'adult_previews':True}).status_code==403
    for path in searches:assert api.get(path,headers=jordan).status_code==200
    assert captured==[False]*len(searches)
    assert api.post(setting,headers=jordan,json={'image_adult_previews':'true'}).status_code==400
    assert api.post(setting,headers=jordan,json={'image_adult_previews':True}).status_code==200
    assert 'id="mw-adult"' in browser.get('/civitai').text
    # Global permission alone does not opt anyone in, even through a forged query.
    assert api.get(searches[0],headers=jordan).status_code==200 and captured[-1] is False
    assert browser.post('/mirrorwall-api/preferences',json={'adult_previews':True}).json()['adult_previews'] is True
    for path in searches:assert api.get(path,headers=jordan).status_code==200
    assert captured[-len(searches):]==[True]*len(searches)
    sign_in(browser,'sam')
    sam={'authorization':'Bearer '+browser.cookies['weightroom']}
    assert browser.get('/mirrorwall-api/preferences').json()['adult_previews'] is False
    assert api.get(searches[1],headers=sam).status_code==200 and captured[-1] is False
    assert api.get(preferences,headers=jordan).json()['adult_previews'] is True
    # Disabling the global policy takes effect immediately without deleting opt-ins.
    assert api.post(setting,headers=jordan,json={'image_adult_previews':False}).status_code==200
    assert 'id="mw-adult"' not in browser.get('/civitai').text
    assert api.get(preferences,headers=jordan).json()['adult_previews'] is False
    assert api.get(searches[0],headers=jordan).status_code==200 and captured[-1] is False
    assert api.post(setting,headers=jordan,json={'image_adult_previews':True}).status_code==200
    assert api.get(preferences,headers=jordan).json()['adult_previews'] is True
    reset=api.post(setting,headers=jordan,json={'image_adult_previews':None})
    assert reset.status_code==200,reset.text
    assert api.get(preferences,headers=jordan).json()['adult_previews_allowed'] is False
    sign_in(browser,'pat')
    plain={'authorization':'Bearer '+browser.cookies['weightroom']}
    assert api.post(setting,headers=plain,json={'image_adult_previews':True}).status_code==403


def test_browser_pages_assets_and_mutations_require_current_identity_and_origin(desk):
    browser, switch, users = desk
    assert browser.get('/chat/images', follow_redirects=False).status_code == 303
    sign_in(browser, 'jordan')
    for path, title in [('/chat/images','Image generation'), ('/models/images','Image models'), ('/models/images/workflows','Image workflows')]:
        answer = browser.get(path)
        assert answer.status_code == 200 and title in answer.text
    assert browser.get('/mirrorwall-static/images.js').status_code == 200
    created = browser.post('/mirrorwall-api/assets', json={'b64_json':base64.b64encode(image_bytes()).decode(),'filename':'ref.png'})
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    assert browser.get('/mirrorwall-api/assets/'+identifier+'/thumbnail').status_code == 200
    assert browser.delete('/mirrorwall-api/assets/'+identifier, headers={'origin':'https://elsewhere.example'}).status_code == 403
    sign_in(browser, 'pat')
    assert browser.get('/mirrorwall-api/assets/'+identifier+'/content').status_code == 404
    assert browser.get('/models/images').status_code == 403
    assert browser.delete('/mirrorwall-api/assets/'+identifier).status_code == 404
    sign_in(browser, 'jordan')
    assert browser.delete('/mirrorwall-api/assets/'+identifier).json()['deleted']


def test_sweeps_expand_to_bounded_sequential_image_lines():
    from mirrorwall.front import Front
    front=Front(None,None,None)
    rows=front.lines({'request':{'model':'flux','prompt':'A boat','n':2},'sweep':{'seed':[40,50],'steps':[6,12]},'references':['private-ref']})
    assert len(rows)==8
    assert [(row['body']['seed'],row['body']['steps']) for row in rows]==[(40,6),(41,6),(40,12),(41,12),(50,6),(51,6),(50,12),(51,12)]
    assert all(row['body']['n']==1 and row['body']['reference_asset_ids']==['private-ref'] for row in rows)
    with pytest.raises(ValueError,match='128'):
        front.lines({'request':{'model':'flux','prompt':'A boat','n':8},'sweep':{'seed':list(range(32))}})


def test_private_asset_pagination_cannot_cross_owners(desk):
    browser,switch,users=desk
    sign_in(browser,'jordan')
    one=browser.post('/mirrorwall-api/assets',json={'b64_json':base64.b64encode(image_bytes()).decode()}).json()
    assert browser.get('/mirrorwall-api/assets?kind=reference').json()['assets'][0]['id']==one['id']
    assert browser.get('/mirrorwall-api/assets?kind=reference&before=0').json()['assets']==[]
    assert browser.get('/mirrorwall-api/assets?before=nan').status_code==400
    sign_in(browser,'pat')
    assert browser.get('/mirrorwall-api/assets?kind=reference').json()['assets']==[]


def test_image_models_remain_available_without_entering_language_chat(desk,tmp_path,monkeypatch):
    from engines.contracts import Installation
    from engines import engine_for
    from loadcoach.models import Models
    browser,switch,users=desk
    monkeypatch.setattr(engine_for('sdcpp'),'installation',lambda config:Installation('sdcpp',True,'sd-server'))
    Models(str(tmp_path/'loadcoach.db')).put('image-only',{'engine':'sdcpp','flags':{'model':str(tmp_path/'image.gguf'),'ctx-size':0},'gpus':1,'sdcpp':{'family':'flux2-klein','components':{'text_encoder':str(tmp_path/'te.gguf'),'vae':str(tmp_path/'vae.gguf')}}})
    sign_in(browser,'jordan')
    page=browser.get('/chat')
    assert page.status_code==200
    assert '<option value="image-only">' not in page.text
    from starlette.testclient import TestClient
    me=TestClient(switch.front).get('/loadcoach/me',headers={'authorization':'Bearer '+browser.cookies['weightroom']}).json()
    assert 'image-only' in me['models']
    assert me['chat']['models']['image-only']['image_generation']
