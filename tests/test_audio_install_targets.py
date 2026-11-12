"""Audio creation validates and forwards explicit destination nodes."""
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from soundroom.ui import routes


@pytest.mark.parametrize('targets',[[],[''],['compute',None],'compute'])
def test_audio_creation_refuses_invalid_targets_before_installing(targets):
    called=[]
    def people(handler):
        async def endpoint(request):
            return await handler(request,SimpleNamespace(can=['manage-models']))
        return endpoint
    async def model_api(*args,**kwargs):
        called.append(kwargs)
        return httpx.Response(202,json={'jobs':[]})
    app=Starlette(routes=routes(people,None,None,model_api))
    with TestClient(app) as client:
        response=client.post('/soundroom-models',json={'new':True,'name':'audio','settings':{'engine':'audiocpp'},'machines':targets})
        assert response.status_code==400,response.text
        assert not called


def test_audio_creation_forwards_all_selected_nodes_and_preserves_source():
    called=[]
    def people(handler):
        async def endpoint(request):
            return await handler(request,SimpleNamespace(can=['manage-models']))
        return endpoint
    async def model_api(*args,**kwargs):
        called.append(kwargs)
        return httpx.Response(202,json={'jobs':[{'id':'first'},{'id':'second'}]})
    app=Starlette(routes=routes(people,None,None,model_api))
    with TestClient(app) as client:
        response=client.post('/soundroom-models?node=preview',json={'new':True,'name':'audio','settings':{'engine':'audiocpp'},
            'machines':['compute','rig','compute'],'source_machine':'front'})
        assert response.status_code==202
        assert called[0]['machines']==['compute','rig']
        assert called[0]['source_machine']=='front' and called[0]['prepare_files'] is True
        assert [job['id'] for job in response.json()['jobs']]==['first','second']


@pytest.mark.parametrize('allowed,status',[(False,200),(True,200),(True,503)])
def test_audio_progress_checks_authority_and_limits_results_to_requested_installations(allowed,status):
    called=[]
    def people(handler):
        async def endpoint(request):
            return await handler(request,SimpleNamespace(can=['manage-models'] if allowed else [],key='operator'))
        return endpoint
    async def ask(method,path,key):
        called.append((method,path,key))
        return httpx.Response(status,json={'distributions':[
            {'id':'wanted','kind':'model-install','name':'voice','machine':'rig','state':'copying','done':10,'of':20,'path':'private'},
            {'id':'other','kind':'model-install','name':'unrelated','state':'complete'},
            {'id':'file-copy','kind':'arrange','state':'complete'}]} if status==200 else {'error':{'message':'Progress offline'}})
    app=Starlette(routes=routes(people,None,ask,None))
    with TestClient(app) as client:
        response=client.get('/soundroom-installations?install=wanted&install=file-copy')
        assert response.status_code==(status if allowed else 403)
        if not allowed:
            assert not called
        elif status==200:
            assert called==[('GET','/turnstile/artifacts/distributions','operator')]
            assert response.json()=={'installations':[{'id':'wanted','name':'voice','machine':'rig','state':'copying','done':10,'of':20}]}
            assert response.headers['cache-control']=='no-store'
        else:
            assert response.json()['error']['message']=='Progress offline'
