"""Owner isolation, bounded parsing, stale-job safety and the ordinary inference contract."""
import asyncio
import base64
import copy
import io
import json
import time
from contextlib import contextmanager

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from starlette.testclient import TestClient

from weightroom.conversations import build_app
from weightroom.knowledge import ABSTAIN, Problem, chunks, extract, upload, vector
from weightroom.knowledge_store import Collections
from test_postgresql import postgres
from test_app import fake_llama_server, in_an_empty_folder


EMBED = dict(id='embed',owned_by='loadcoach',embedding=True,model_id='embed-id',fingerprint='embed-fp',build='build',context_tokens=2048,local_model_guard=True)
CHAT = dict(id='chat',owned_by='loadcoach',model_id='chat-id',fingerprint='chat-fp',build='build',local_model_guard=True)


@contextmanager
def knowledge(tmp_path):
    state = dict(models=[copy.deepcopy(EMBED),copy.deepcopy(CHAT)], calls=[], maximum='restricted', malformed=None)
    async def compute(request):
        auth=request.headers.get('authorization','');name=auth.removeprefix('Bearer ')
        if not name or name=='revoked':
            return httpx.Response(401)
        if request.url.path=='/loadcoach/me':
            return httpx.Response(200,json=dict(identity=name,name=name,can=['superuser'] if name=='admin' else [],max_sensitivity=state['maximum'],
                chat={'models':{'chat':{'context':8192}}}))
        if request.url.path=='/v1/models':
            return httpx.Response(200,json={'data':state['models']})
        if request.url.path.startswith('/loadcoach/requests/'):
            return httpx.Response(200,json=state.get('records',{}).get(request.url.path.rsplit('/',1)[1],{}))
        body=json.loads(request.content);state['calls'].append((request.url.path,dict(request.headers),body))
        model=next(m for m in state['models'] if m['id']==body['model'])
        headers={'x-loadcoach-model-id':model['model_id'],'x-loadcoach-fingerprint':model['fingerprint'],
                 'x-loadcoach-build':model['build'],'x-loadcoach-request-id':str(len(state['calls']))}
        if state.get('wrong_receipt'):
            headers['x-loadcoach-fingerprint']='wrong'
        if request.url.path=='/v1/embeddings':
            while state.get('block'):
                await asyncio.sleep(.01)
            if state.get('revoke_on_embed'):
                state['maximum']='normal'
            value=[0,1,0] if 'unknown' in body['input'] else [1,0,0]
            return httpx.Response(200,headers=headers,json={'data':[{'index':0,'embedding':state.get('vector',value)}]})
        if state.get('during_generation'):
            state['during_generation']()
        sources=json.loads(body['messages'][1]['content'])['sources']
        answer=state['malformed'] or {'answer':'The Cedar budget is 47.','source_ids':[sources[0]['id']],'abstain':False}
        return httpx.Response(200,headers=headers,json={'choices':[{'message':{'role':'assistant','content':json.dumps(answer)}}]})
    client=httpx.AsyncClient(transport=httpx.MockTransport(compute),base_url='http://compute')
    app=build_app(str(tmp_path/'chat.db'),client)
    with TestClient(app,headers={'authorization':'Bearer alice'}) as caller:
        yield caller,state,app.state.knowledge
    asyncio.run(client.aclose())


def create(caller,**extra):
    reply=caller.post('/loadcoach/collections',json={'title':'Cedar sources','model':'embed',**extra})
    assert reply.status_code==201,reply.text
    return '/loadcoach/collections/'+reply.json()['id']


def add(caller,path,name='notes.txt',data=b'The Cedar budget is 47.\nOnly the approved budget applies.'):
    reply=caller.post(path+'/documents',json={'name':name,'data':base64.b64encode(data).decode()})
    assert reply.status_code==201,reply.text
    return path+'/documents/'+reply.json()['id']


def ready(caller,path,document):
    reply=caller.post(document+'/index');assert reply.status_code==202,reply.text
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        result=caller.get(path).json()['documents']
        result=next(d for d in result if document.endswith(d['id']))
        if result['state']!='indexing':
            assert result['state']=='ready',result
            return result
        time.sleep(.01)
    raise AssertionError('Indexing did not finish')


def test_private_ingestion_retrieval_answers_and_no_evidence(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller,level='sensitive');document=add(caller,path)
        assert not state['calls']  # uploads never wake inference
        result=ready(caller,path,document);assert result['chunks']==1
        retrieved=caller.post(path+'/query',json={'question':'What is the Cedar budget?'}).json()
        assert len(retrieved['sources'])==1 and not retrieved['generated'] and retrieved['answer']==''
        answer=caller.post(path+'/query',json={'question':'What is the Cedar budget?','model':'chat'})
        assert answer.status_code==200,answer.text
        assert answer.json()['answer']=='The Cedar budget is 47.' and answer.json()['cited']==[retrieved['sources'][0]['id']]
        before=len(state['calls'])
        absent=caller.post(path+'/query',json={'question':'unknown topic','model':'chat'}).json()
        assert absent['abstain'] and absent['answer']==ABSTAIN and absent['sources']==[]
        assert len(state['calls'])==before+1 and state['calls'][-1][0]=='/v1/embeddings'
        for _,headers,body in state['calls']:
            assert headers['authorization']=='Bearer alice'
            assert headers['x-loadcoach-sensitivity']=='sensitive' and headers['x-turnstile-no-wake']=='1'
            assert headers['x-loadcoach-trace'].startswith('collection-')
            assert 'offload' not in body and 'tools' not in body
        assert caller.get(document).content.startswith(b'The Cedar')
        assert caller.get(document).headers['content-disposition']=='attachment'
        before=len(state['calls']);assert caller.delete(document).status_code==200
        assert caller.post(path+'/query',json={'question':'Cedar','model':'chat'}).json()['answer']==ABSTAIN
        assert len(state['calls'])==before
        assert caller.get(document).status_code==404
        assert caller.portal.call(lambda:service.store.chunks('alice',path.rsplit('/',1)[1]))==[]


def test_other_users_admin_and_new_identity_cannot_see_sources(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller);doc=add(caller,path);ready(caller,path,doc)
        for auth in ('bob','admin','alice-recreated'):
            headers={'authorization':'Bearer '+auth}
            assert caller.get('/loadcoach/collections',headers=headers).json()['collections']==[]
            for method,suffix,body in [('GET','',None),('POST','/query',{'question':'Cedar'}),('GET',doc[len(path):],None),('DELETE','',None)]:
                assert caller.request(method,path+suffix,headers=headers,json=body).status_code==404
        state['maximum']='normal'
        restricted=create(caller,level='normal')
        state['maximum']='restricted';sensitive=create(caller,level='restricted');add(caller,sensitive)
        state['maximum']='normal'
        assert caller.get(sensitive).status_code==403
        assert sensitive.rsplit('/',1)[1] not in {c['id'] for c in caller.get('/loadcoach/collections').json()['collections']}
        assert caller.get(restricted).status_code==200
        caller.portal.call(service.forget,'alice')
        assert caller.get('/loadcoach/collections').json()['collections']==[]


def test_changed_embedding_space_requires_explicit_rebuild(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller);doc=add(caller,path);ready(caller,path,doc)
        # A rename preserves identity and space. A changed fingerprint does not.
        state['models'][0]['id']='renamed'
        assert caller.post(path+'/query',json={'question':'Cedar'}).status_code==200
        state['models'][0]['fingerprint']='new-fp'
        before=len(state['calls'])
        assert caller.post(path+'/query',json={'question':'Cedar'}).status_code==409
        assert len(state['calls'])==before
        assert caller.post(path,json={'model':'renamed'}).status_code==200
        found=caller.get(path).json();assert found['dimensions']==0 and found['documents'][0]['state']=='pending'
        ready(caller,path,doc)
        assert caller.post(path+'/query',json={'question':'Cedar'}).status_code==200
        state['models']=state['models'][1:]
        assert caller.post(path+'/query',json={'question':'Cedar'}).status_code==409


@pytest.mark.parametrize('bad',[[],[0,0],[True,1],[float('inf'),1],[[1,2]],['one'],[1]*4097])
def test_bad_vectors_are_not_indexed_or_searched(bad):
    with pytest.raises(Problem):vector(bad)


@pytest.mark.parametrize('answer',[
    {'answer':'invented','source_ids':['missing'],'abstain':False},
    {'answer':'uncited','source_ids':[],'abstain':False},
    {'answer':'bad','source_ids':[],'abstain':'yes'},
    ['not an object'],
])
def test_invalid_cited_generation_is_not_accepted(tmp_path,answer):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller);doc=add(caller,path);ready(caller,path,doc)
        state['malformed']=answer
        reply=caller.post(path+'/query',json={'question':'Cedar','model':'chat'})
        assert reply.status_code==502 and 'No answer was accepted' in reply.text


def pdf(blank=False,encrypted=False):
    writer=PdfWriter();page=writer.add_blank_page(width=300,height=200)
    if not blank:
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 20 100 Td (The Cedar budget is 47.) Tj ET')
        page[NameObject('/Contents')]=content
    if encrypted:writer.encrypt('secret')
    result=io.BytesIO();writer.write(result);return result.getvalue()


@pytest.mark.parametrize('kind',['text','pdf','blank','encrypted','corrupt'])
def test_actual_bounded_parser_and_chunk_locations(kind):
    data=b'alpha\nbeta' if kind=='text' else pdf(blank=kind=='blank',encrypted=kind=='encrypted') if kind!='corrupt' else b'%PDF-bad'
    _,mime,original,_=upload({'name':'doc.txt' if kind=='text' else 'doc.pdf','data':base64.b64encode(data).decode()})
    if kind in ('blank','encrypted','corrupt'):
        with pytest.raises(Problem):asyncio.run(extract({'mime':mime,'original':original}))
    else:
        pages=asyncio.run(extract({'mime':mime,'original':original}))
        parts=chunks(pages,2048)
        assert parts[0]['page']==(0 if kind=='text' else 1) and parts[0]['line']==1
        assert 'alpha' in parts[0]['text'] if kind=='text' else 'Cedar' in parts[0]['text']


def test_stale_jobs_never_resurrect_deleted_cancelled_or_rebuilt_sources(tmp_path):
    store=Collections(str(tmp_path/'chat.db'));owner='stable-id'
    number=store.create(owner,'Sources','normal',EMBED)
    doc=store.add(owner,number,'notes','text/plain','YWJj','digest')
    parts=[dict(text='abc',page=0,line=1,vector=[1,0])]
    token=store.begin(owner,number,doc)
    store.stop(owner,number,doc,'cancelled')
    assert not store.finish(owner,number,doc,token,1,parts) and store.one(owner,number)['dimensions']==0
    token=store.begin(owner,number,doc);store.reset(owner,number,EMBED)
    assert not store.finish(owner,number,doc,token,1,parts)
    token=store.begin(owner,number,doc);store.remove(owner,number,doc)
    assert not store.finish(owner,number,doc,token,2,parts)
    assert store.chunks(owner,number)==[]
    store.db.close()



def test_index_cancellation_deletion_and_shutdown_do_not_replay_credentials(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller);doc=add(caller,path);state['block']=True
        assert caller.post(doc+'/index').status_code==202
        assert caller.post(doc+'/index').status_code==409
        assert caller.post(doc+'/cancel').status_code==200
        assert caller.get(path).json()['documents'][0]['state']=='failed'
        assert caller.post(doc+'/index').status_code==202
        assert caller.delete(doc).status_code==200
        assert caller.get(path).json()['documents']==[]
        doc=add(caller,path);assert caller.post(doc+'/index').status_code==202
    with knowledge(tmp_path) as (caller,state,service):
        assert not state['calls']
        found=caller.get(path).json()['documents'][0]
        assert found['state']=='failed' and 'retry' in found['status']
        ready(caller,path,doc)
        assert caller.post(doc+'/cancel').status_code==409
        assert caller.get(path).json()['documents'][0]['chunks']==1


@pytest.mark.parametrize('mode',['deleted_source','revoked_level','wrong_model'])
def test_generation_results_are_rechecked_before_returning(mode,tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller,level='sensitive');doc=add(caller,path);ready(caller,path,doc)
        if mode=='deleted_source':
            state['during_generation']=lambda:service.store.remove('alice',path.rsplit('/',1)[1],doc.rsplit('/',1)[1])
        elif mode=='revoked_level':
            state['during_generation']=lambda:state.update(maximum='normal')
        else:
            state['wrong_receipt']=True
        reply=caller.post(path+'/query',json={'question':'Cedar','model':'chat'})
        assert reply.status_code==(403 if mode=='revoked_level' else 409)
        assert 'The Cedar budget is 47.' not in reply.text


def test_indexing_rechecks_sensitivity_and_dimensions(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller,level='sensitive');doc=add(caller,path)
        state['revoke_on_embed']=True
        assert caller.post(doc+'/index').status_code==202
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if caller.portal.call(lambda:not service.jobs):break
            time.sleep(.01)
        state['maximum']='restricted'
        found=caller.get(path).json()['documents'][0]
        assert found['state']=='failed' and found['chunks']==0
        state['revoke_on_embed']=False;ready(caller,path,doc)
        state['vector']=[1,0]
        assert caller.post(path+'/query',json={'question':'Cedar'}).status_code==409


def test_utf8_chunks_are_bounded_overlap_and_preserve_locations():
    pages=[{'page':3,'text':('Cedar αβγ\n'*200)}]
    parts=chunks(pages,1024)
    assert len(parts)>1 and all(len(c['text'].encode())<=480 and c['page']==3 for c in parts)
    assert parts[1]['line']>1 and not any('�' in p['text'] for p in parts)
    assert parts[-1]['text'].endswith('αβγ\n')


def test_postgresql_collection_sources_and_engine_transfer(postgres,tmp_path):
    from db import Database
    from db.transfer import copy_store
    store=Collections(postgres)
    number=store.create('owner','Source','normal',EMBED)
    doc=store.add('owner',number,'notes','text/plain','YWJj','digest')
    token=store.begin('owner',number,doc)
    assert store.finish('owner',number,doc,token,1,[dict(page=1,line=1,text='abc',vector=[1.,0.])])
    before=store.chunks('owner',number);assert len(before)==1 and store.listing('other')==[]
    store.db.close()
    target=Database.sqlite(tmp_path/'restored-chat.db')
    assert copy_store(postgres,target)['state']=='verified'
    restored=Collections(target)
    assert restored.chunks('owner',number)==before
    restored.remove('owner',number)
    assert restored.chunks('owner',number)==[] and restored.documents('owner',number)==[]
    restored.db.close()


def test_old_compute_without_guard_advertisement_is_not_used(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        state['models'][0].pop('local_model_guard')
        reply=caller.post('/loadcoach/collections',json={'title':'Sources','model':'embed'})
        assert reply.status_code==409 and not state['calls']


def test_native_embedding_and_collection_retrieval_use_normal_authorized_requests(fake_llama_server,tmp_path):
    from chat_clients import gateway
    from test_app import add_user,model
    models={'embed':model(str(tmp_path/'embed.gguf'),embedding=True), 'chat':model(str(tmp_path/'chat.gguf'))}
    with gateway(fake_llama_server,models=models) as caller:
        owner=add_user(caller,'owner','admin');caller.headers.update(owner)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            catalog=caller.get('/v1/models/embed').json()
            if catalog.get('fingerprint') and catalog.get('build'):break
            time.sleep(.02)
        assert catalog['embedding'] and catalog['context_tokens']==2048 and catalog['local_model_guard']
        path=create(caller);doc=add(caller,path)
        ready(caller,path,doc)
        reply=caller.post(path+'/query',json={'question':'Cedar budget'})
        assert reply.status_code==200,reply.text
        assert reply.json()['sources'][0]['text'].startswith('The Cedar budget') and reply.json()['requests']
        record=caller.get('/loadcoach/requests/'+reply.json()['requests'][0]).json()
        assert record['asked']['model']=='embed' and record['asked']['path']=='embeddings'



def test_background_ingestion_participates_in_existing_maintenance_drain(tmp_path):
    from db.maintenance import Gate
    with knowledge(tmp_path) as (caller,state,service):
        gate=Gate(tmp_path/'settings.toml')
        caller.portal.call(lambda:setattr(service,'maintenance',gate))
        path=create(caller);doc=add(caller,path);state['block']=True
        assert caller.post(doc+'/index').status_code==202
        assert gate.active==1
        gate.path.write_text(json.dumps({'token':'test-maintenance'}))
        other=add(caller,path,name='other.txt')
        assert caller.post(other+'/index').status_code==503
        assert caller.post(doc+'/cancel').status_code==200 and gate.active==0
        gate.path.unlink();state['block']=False
        ready(caller,path,doc)
        assert gate.active==0


def test_known_full_collection_refuses_before_embedding_more_sources(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=create(caller);number=path.rsplit('/',1)[1]
        def fill():
            for name in ('one','two'):
                doc=service.store.add('alice',number,name,'text/plain','YWJj','digest')
                token=service.store.begin('alice',number,doc)
                assert service.store.finish('alice',number,doc,token,1,[dict(page=0,line=1,text='abc',vector=[1.,0.,0.])]*250)
        caller.portal.call(fill)
        doc=add(caller,path);assert caller.post(doc+'/index').status_code==202
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            found=next(d for d in caller.get(path).json()['documents'] if doc.endswith(d['id']))
            if found['state']!='indexing':break
            time.sleep(.01)
        assert found['state']=='failed' and '500 indexed chunks' in found['status']
        assert not state['calls']
        assert sum(d['chunks'] for d in caller.get(path).json()['documents'])==500


def test_native_index_cancellation_releases_normal_admission(fake_llama_server,tmp_path):
    from chat_clients import gateway
    from test_app import add_user,model
    with gateway(fake_llama_server,models={'embed':model(str(tmp_path/'embed.gguf'),embedding=True,**{'benchmark-delay':1})}) as caller:
        caller.headers.update(add_user(caller,'owner','admin'))
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if caller.get('/v1/models/embed').json().get('fingerprint'):break
            time.sleep(.01)
        path=create(caller);doc=add(caller,path)
        assert caller.post(doc+'/index').status_code==202
        deadline=time.monotonic()+5;active=False
        while time.monotonic()<deadline:
            active=bool(caller.get('/loadcoach/status').json()['running'])
            if active:break
            time.sleep(.01)
        assert active
        assert caller.post(doc+'/cancel').status_code==200
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if not caller.get('/loadcoach/status').json()['running']:break
            time.sleep(.01)
        assert not caller.get('/loadcoach/status').json()['running']
        assert caller.get(path).json()['documents'][0]['state']=='failed'
