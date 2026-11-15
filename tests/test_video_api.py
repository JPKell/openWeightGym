"""Video jobs reuse real batch storage, stable identities and authenticated control routes."""
import asyncio
from functools import partial
from types import SimpleNamespace

import httpx
import pytest

from test_video_requests import profile
from test_weightroom import desk,sign_in


@pytest.fixture
def api(desk,monkeypatch):
    browser,wire,users=desk
    front=wire.front.state.mirrorwall
    selected=profile()
    async def catalogue(user,kind='image'):
        return [{'id':'wan','model_id':'stable-video','capabilities':{'video_generation':True},
                 'video_workflow':{**selected['sdcpp'],'exclusive':True}}] if kind=='video' else []
    async def model_ids(user):
        return {'wan':'stable-video'}
    monkeypatch.setattr(front,'catalogue',catalogue)
    monkeypatch.setattr(front.batches,'model_ids',model_ids)
    monkeypatch.setattr(front.batches,'work',lambda *args:None)
    sign_in(browser,'jordan')
    token=browser.cookies['weightroom']
    def request(method,path,**kwargs):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wire.front),base_url='http://front:8442') as client:
                return await client.request(method,path,headers={'authorization':'Bearer '+token,**kwargs.pop('headers',{})},**kwargs)
        return browser.portal.call(call)
    return SimpleNamespace(get=partial(request,'GET'),post=partial(request,'POST'),delete=partial(request,'DELETE'),
                           front=front,users=users,browser=browser,wire=wire)


def test_create_retrieve_content_range_and_delete_keep_owner(api):
    response=api.post('/v1/videos',json={'model':'wan','prompt':'Rain','seconds':'2.1'})
    assert response.status_code==202,response.text
    row=response.json();identifier=row['id']
    assert row['object']=='video' and row['frames']==33 and row['status']=='queued'
    batch=api.front.batches.store.get(identifier)
    assert batch['endpoint']=='/v1/videos/generations'
    owner=api.front.store.owner(identifier)
    assert owner==api.users.by_name('jordan').identity
    assert api.get('/v1/videos').json()['data'][0]['id']==identifier
    assert api.get('/v1/videos/'+identifier+'/content').status_code==409
    assert api.delete('/v1/videos/'+identifier).status_code==409
    asset=api.front.store.save(owner,b'\x1aE\xdf\xa3webm-fixture',kind='video',job=identifier,
                              metadata={'width':832,'height':480,'seconds':33/16,'job':identifier,'batch_line':0})
    api.front.batches.store.keep(identifier,0,200,'',{'data':[{'asset_id':asset['id']}]})
    api.front.batches.store.finish(identifier)
    content=api.get('/v1/videos/'+identifier+'/content',headers={'range':'bytes=0-3'})
    assert content.status_code==206 and content.content==b'\x1aE\xdf\xa3'
    assert content.headers['content-type']=='video/webm'
    assert api.get('/v1/videos/'+identifier+'/content',headers={'range':'bytes=999-'}).status_code==416
    assert api.delete('/v1/videos/'+identifier).json()['deleted']
    assert api.get('/v1/videos/'+identifier).status_code==404


def test_unsupported_inputs_foreign_assets_and_platform_switch_fail_before_queueing(api):
    for extra in ({'init_image':'https://example.com/private.png'},{'init_image':{'asset_id':'image_'+'a'*32}},
                  {'ref_audios':['audio']},{'native_arguments':'--unsafe'}):
        response=api.post('/v1/videos',json={'model':'wan','prompt':'Rain',**extra})
        assert response.status_code==400,response.text
    assert api.front.store.jobs(api.users.by_name('jordan').identity)==[]
    changes=api.wire.front.state.console_settings.changes
    changes.seen('weightroom',{'allow_video_generation':{'value':False}})
    # The same live console-policy writer used by the settings API controls the service gate.
    setting=api.post('/loadcoach/settings/weightroom',json={'allow_video_generation':False})
    assert setting.status_code==200,setting.text
    assert api.post('/v1/videos',json={'model':'wan','prompt':'Rain'}).status_code==403


def test_no_log_video_never_creates_sql_jobs_or_files(api,monkeypatch):
    async def execute(*args,**kwargs):
        return httpx.Response(200,json={'data':[{'asset_id':'temporary'}]})
    monkeypatch.setattr(api.front,'execute_line',execute)
    before={table:api.front.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('mirrorwall_assets','mirrorwall_jobs','batches','files')}
    response=api.post('/v1/videos',json={'model':'wan','prompt':'Private rain','temporary':True})
    assert response.status_code==202,response.text
    row=response.json();assert row['temporary']
    api.browser.portal.call(asyncio.sleep,.01)
    after={table:api.front.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before}
    assert before==after
    assert api.front.store.owner(row['id']) is None
    listed=api.get('/v1/videos')
    assert listed.headers['x-suite-image-temporary']=='1'


def test_video_history_cursor_reaches_jobs_older_than_one_hundred_other_media_jobs(api):
    owner=api.users.by_name('jordan').identity
    for index in range(105):
        kind='video' if index in (0,1,104) else 'image'
        api.front.store.bind_job('history-'+str(index),owner,'stable-video','wan',
                                 {'kind':kind,'request':{'model':'wan','prompt':'History'}})
    first=api.get('/v1/videos?limit=2').json()
    assert [one['id'] for one in first['data']]==['history-104','history-1'] and first['has_more']
    second=api.get('/v1/videos?limit=2&after=history-1').json()
    assert [one['id'] for one in second['data']]==['history-0'] and not second['has_more']
    assert api.get('/v1/videos?order=asc&limit=1').json()['data'][0]['id']=='history-0'


def test_live_retention_setting_expires_completed_content(api,monkeypatch):
    response=api.post('/v1/videos',json={'model':'wan','prompt':'Rain'})
    identifier=response.json()['id'];owner=api.users.by_name('jordan').identity
    asset=api.front.store.save(owner,b'\x1aE\xdf\xa3fixture',kind='video',job=identifier,
                              metadata={'width':832,'height':480,'seconds':1,'job':identifier})
    api.front.batches.store.keep(identifier,0,200,'',{'data':[{'asset_id':asset['id']}]})
    api.front.batches.store.finish(identifier)
    with api.front.store.db:api.front.store.db.execute('UPDATE mirrorwall_assets SET created=0 WHERE id=?',(asset['id'],))
    read=api.front.limits.read
    monkeypatch.setattr(api.front.limits,'read',lambda key:1 if key=='video_retention_days' else read(key))
    rows=api.get('/v1/videos').json()['data']
    assert rows[0]['status']=='failed' and rows[0]['error']['code']=='expired'
    assert api.front.store.content(owner,asset['id']) is None


def test_native_video_batch_line_resolves_owned_inputs_and_uses_video_transport(api,monkeypatch):
    import json,base64
    from mirrorwall.stream import encode
    from test_video_requests import image
    owner=api.users.by_name('jordan')
    reference=api.front.store.save(owner.identity,base64.b64decode(image()))
    identifier=api.post('/v1/videos',json={'model':'wan','prompt':'Rain'}).json()['id']
    sent=[]
    def respond(request):
        sent.append((request.url.path,json.loads(request.content)))
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=encode({
            'type':'video_generation.completed','asset_id':'fixture-output','frame_count':33,'fps':16}))
    async def run():
        async with httpx.AsyncClient(base_url='http://front',transport=httpx.MockTransport(respond)) as door:
            monkeypatch.setattr(api.front.batches,'door',door)
            return await api.front.execute_line(identifier,{'line':0},
                {'model':'wan','prompt':'Rain','seconds':2.1,'input_reference':{'asset_id':reference['id']}},owner,{})
    answer=api.browser.portal.call(run)
    assert answer.status_code==200
    assert sent[0][0]=='/v1/videos/generations' and sent[0][1]['stream'] is True
    assert sent[0][1]['init_image']==image() and 'video_asset_ids' not in sent[0][1]
    with pytest.raises(ValueError,match='unfinished'):
        api.front.store.delete(owner.identity,reference['id'])
