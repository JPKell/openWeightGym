"""Images inventory retains each node copy and its canonical workflow identity."""
from types import SimpleNamespace

import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mirrorwall.ui import routes


def test_all_and_selected_node_inventory_keep_local_settings_and_workflow_identity():
    calls=[]
    def people(handler):
        async def endpoint(request):
            return await handler(request,SimpleNamespace(can=['manage-models'],key='manager'))
        return endpoint
    async def ask(method,path,key,**kwargs):
        calls.append(path)
        if path=='/turnstile/models':
            return httpx.Response(200,json={'models':[
                {'name':'image','engine':'sdcpp','model_id':'canonical','locations':[
                    {'name':'image','engine':'sdcpp','model_id':'local-compute','machine':'compute','gpus':['C0'],'sdcpp':{'family':'sdxl'}},
                    {'name':'image-alias','engine':'sdcpp','model_id':'local-rig','machine':'rig','gpus':['R1'],'sdcpp':{'family':'sdxl'}}]},
                {'name':'language','engine':'llama_cpp'}],
                'nodes':{'compute':{'cards':['C0','C1'],'engine_cards':{'sdcpp':['C0']}},
                         'rig':{'cards':['R0','R1'],'engine_cards':{'sdcpp':['R1']}}}})
        if path=='/turnstile/machines':
            return httpx.Response(200,json={'machines':[{'name':name,'state':'ready'} for name in ('compute','rig')]})
        if path.endswith('/engines'):
            return httpx.Response(200,json={'engines':[]})
        raise AssertionError(path)
    with TestClient(Starlette(routes=routes(people,None,ask,None))) as client:
        all_models=client.get('/mirrorwall-models').json()
        assert [(m['name'],m['node'],m['gpus']) for m in all_models['models']]==[
            ('image','compute',['C0']),('image-alias','rig',['R1'])]
        assert all(m['workflow_model_id']=='canonical' and m['workflow_model']=='image' for m in all_models['models'])
        assert all_models['node_cards']=={'compute':['C0'],'rig':['R1']}
        rig=client.get('/mirrorwall-models?node=rig').json()
        assert [m['name'] for m in rig['models']]==['image-alias'] and rig['cards']==['R1']
        assert '/turnstile/machines/rig/engines' in calls


def test_all_inventory_does_not_depend_on_primary_node_availability():
    def people(handler):
        async def endpoint(request):
            return await handler(request,SimpleNamespace(can=['manage-models'],key='manager'))
        return endpoint
    async def ask(method,path,key,**kwargs):
        if path=='/turnstile/models':
            return httpx.Response(200,json={'models':[{'name':'remote','engine':'sdcpp','machine':'rig','sdcpp':{'family':'sdxl'}}],
                'nodes':{'rig':{'cards':[]}},'unavailable_machines':['compute']})
        if path=='/turnstile/machines':return httpx.Response(200,json={'machines':[{'name':'rig','state':'ready'},{'name':'compute','state':'offline'}]})
        assert path=='/turnstile/machines/rig/engines'
        return httpx.Response(200,json={'engines':[]})
    with TestClient(Starlette(routes=routes(people,None,ask,None))) as client:
        response=client.get('/mirrorwall-models')
        assert response.status_code==200
        assert response.json()['default_node']=='rig'
        assert response.json()['models'][0]['node']=='rig'
