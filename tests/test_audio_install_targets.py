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
