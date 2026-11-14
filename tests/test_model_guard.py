"""Local inference constraints are enforced before routing and after GPU admission."""
import time

import httpx
import pytest

from loadcoach.runner import Runner
from test_app import add_user, fake_llama_server, gateway, in_an_empty_folder, model


def guard(caller,owner,name='small'):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        data=caller.get('/v1/models/'+name,headers=owner).json()
        if data.get('fingerprint') and data.get('build'):
            assert data['local_model_guard'] is True
            return {**owner,**{'x-loadcoach-require-'+label:data[key] for label,key in
                (('model-id','model_id'),('fingerprint','fingerprint'),('build','build'))}}
        time.sleep(.02)
    raise AssertionError('Native model identity was not ready')


def test_local_guard_rejects_wrong_incomplete_hosted_and_offload_before_sending(fake_llama_server,monkeypatch):
    native=[];send=httpx.AsyncClient.send
    async def observe(self,request,*args,**kwargs):
        if request.url.host=='127.0.0.1' and request.url.path=='/v1/chat/completions':native.append(request)
        return await send(self,request,*args,**kwargs)
    monkeypatch.setattr(httpx.AsyncClient,'send',observe)
    with gateway(fake_llama_server) as caller:
        owner=add_user(caller,'owner','admin');headers=guard(caller,owner)
        body={'model':'small','messages':[{'role':'user','content':'hello'}]}
        assert caller.post('/v1/chat/completions',headers=headers,json=body).status_code==200
        before=len(native);assert before==1
        for field in ('model-id','fingerprint','build'):
            wrong={**headers,'x-loadcoach-require-'+field:'wrong'}
            refused=caller.post('/v1/chat/completions',headers=wrong,json=body)
            assert refused.status_code==409
            number=refused.headers['x-loadcoach-request-id']
            facts=caller.get('/loadcoach/requests/'+number,headers=owner).json()
            assert facts['ended']['status']==409 and 'does not match' in facts['ended']['why']
        partial={**owner,'x-loadcoach-require-model-id':'one'}
        assert caller.post('/v1/chat/completions',headers=partial,json=body).status_code==400
        assert caller.post('/v1/chat/completions',headers={**headers,'x-loadcoach-offload':'cloud'},json=body).status_code==409
        assert caller.post('/v1/chat/completions',headers=headers,json={**body,'model':'cloud'}).status_code==404  # outside models are front's: see test_hosted_front.py
        assert len(native)==before
        assert caller.get('/loadcoach/status',headers=owner).json()['running']==[]


def test_name_guard_accepts_local_name_and_rejects_a_different_name(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        owner=add_user(caller,'owner','admin')
        headers={**owner,'x-loadcoach-require-model-name':'small'}
        body={'model':'small','messages':[]}
        assert caller.post('/v1/chat/completions',headers=headers,json=body).status_code==200
        refused=caller.post('/v1/chat/completions',headers={**headers,'x-loadcoach-require-model-name':'other'},json=body)
        assert refused.status_code==409 and refused.headers['x-loadcoach-request-id'].isdigit()


def test_the_guard_pins_the_configured_model_not_the_server_a_fit_reshaped(fake_llama_server,monkeypatch):
    """A context fit at load time changes the server as started, not the model the caller pinned: it is answered."""
    from loadcoach.models import Models
    marks=Runner.marks;native=[];send=httpx.AsyncClient.send
    def reshaped(self,*args,**kwargs):
        return {**marks(self,*args,**kwargs),'fingerprint':'as-started-after-a-context-fit'}
    async def observe(self,request,*args,**kwargs):
        if request.url.host=='127.0.0.1' and request.url.path=='/v1/chat/completions':native.append(request)
        return await send(self,request,*args,**kwargs)
    monkeypatch.setattr(Runner,'marks',reshaped);monkeypatch.setattr(httpx.AsyncClient,'send',observe)
    with gateway(fake_llama_server) as caller:
        owner=add_user(caller,'owner','admin');headers=guard(caller,owner)
        body={'model':'small','messages':[{'role':'user','content':'hello'}]}
        reply=caller.post('/v1/chat/completions',headers=headers,json=body)
        assert reply.status_code==200 and reply.headers['x-loadcoach-fingerprint']=='as-started-after-a-context-fit'  # the evidence says what ran
        assert reply.headers['x-loadcoach-configured-fingerprint']==headers['x-loadcoach-require-fingerprint']  # and says what was pinned, for a client to check
        assert len(native)==1
        # A change to the configuration itself is still refused, and nothing is sent.
        fingerprint=Models.fingerprint
        monkeypatch.setattr(Models,'fingerprint',lambda self,*args,**kwargs:'reconfigured')
        reply=caller.post('/v1/chat/completions',headers=headers,json=body)
        monkeypatch.setattr(Models,'fingerprint',fingerprint)
        assert reply.status_code==409 and len(native)==1
        assert caller.get('/loadcoach/status',headers=owner).json()['running']==[]
