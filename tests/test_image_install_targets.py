"""Image creation forwards explicit destinations and plans the complete model bundle."""
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from mirrorwall.ui import routes
from turnstile.model_install import plan
from loadcoach.files import SPARE


@pytest.mark.parametrize('targets',[[],[''],['compute',None],'compute'])
def test_image_creation_refuses_invalid_targets_before_installing(targets):
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
        response=client.post('/mirrorwall-models',json={'new':True,'name':'image','settings':{'engine':'sdcpp'},'machines':targets})
        assert response.status_code==400,response.text
        assert not called


def test_image_creation_forwards_all_selected_nodes_and_preserves_source():
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
        response=client.post('/mirrorwall-models?node=preview',json={'new':True,'name':'image','settings':{'engine':'sdcpp'},
            'machines':['compute','rig','compute'],'source_machine':'front'})
        assert response.status_code==202
        assert called[0]['machines']==['compute','rig']
        assert called[0]['source_machine']=='front' and called[0]['prepare_files'] is True
        assert [job['id'] for job in response.json()['jobs']]==['first','second']


def test_each_image_target_receives_diffusion_encoders_vae_adapters_and_upscaler():
    files=['diffusion/base.gguf','text_encoders/qwen.gguf','vae/ae.safetensors','loras/style.safetensors','upscalers/4x.pth']
    nodes=[{'name':name,'answering':True,'places':[
        {'name':'image','folder':'/'+name+'/image','tier':'cache','free':10*SPARE},
        {'name':'storage_image','folder':'/'+name+'/storage','tier':'storage','free':10*SPARE}],
        'files':[{'path':'/front/storage/'+file,'place':'storage_image','tier':'storage','size':100} for file in files] if name=='front' else []}
        for name in ('front','compute','rig')]
    settings={'engine':'sdcpp','flags':{'model':'/front/storage/'+files[0],'ctx-size':0},'gpus':0,
        'sdcpp':{'family':'flux2-klein','components':{'text_encoder':'/front/storage/'+files[1],'vae':'/front/storage/'+files[2]},
            'loras':{'style':{'file':'/front/storage/'+files[3],'scale':.5}},'upscaler':'/front/storage/'+files[4]}}
    for target in ('compute','rig'):
        prepared=plan(settings,nodes,target,'front')
        assert {step['relative'] for step in prepared['steps'] if step['node']==target}==set(files)
        assert prepared['settings']['flags']['model']=='/'+target+'/image/'+files[0]
        assert prepared['settings']['sdcpp']['components']['text_encoder']=='/'+target+'/image/'+files[1]
        assert prepared['settings']['sdcpp']['loras']['style']['file']=='/'+target+'/image/'+files[3]
        assert prepared['settings']['sdcpp']['upscaler']=='/'+target+'/image/'+files[4]
