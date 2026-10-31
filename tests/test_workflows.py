"""Immutable private prompts, resumable fixed steps and retained accepted work."""
import asyncio
import json
import time

import pytest

from test_collections import knowledge,create,add,ready,EMBED,CHAT
from test_app import fake_llama_server,in_an_empty_folder
from test_postgresql import postgres
from weightroom.workflow_store import Workflows,builtin
from weightroom.knowledge import cited_answer,Problem


def workflow(caller,collection,**extra):
    reply=caller.post('/loadcoach/workflows',json=dict(title='Cedar draft',question='What is the Cedar budget?',minimum=.65,
        collection=collection.rsplit('/',1)[1],research_model='chat',draft_model='chat',**extra))
    assert reply.status_code==201,reply.text
    return '/loadcoach/workflows/'+reply.json()['id']


def finished(caller,path):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        result=caller.get(path).json()
        if not result['active'] and result['state'] not in ('researching','drafting'):return result
        time.sleep(.01)
    raise AssertionError('Workflow did not settle')


def test_fixed_research_acceptance_draft_and_immutable_prompt_versions(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        def prompt(body):
            reply=caller.post('/loadcoach/workflows/prompts',json={'name':'Brief','kind':'research','body':body})
            assert reply.status_code==201,reply.text
            return reply.json()['id']
        first=prompt('Write three concise facts.');path=workflow(caller,collection,research_prompt=first)
        second=prompt('Write a detailed analysis.')
        assert first!=second
        saved=caller.get(path).json()
        assert saved['config']['prompts']['research']['body']=='Write three concise facts.'
        listing=caller.get('/loadcoach/workflows').json()
        assert {p['version'] for p in listing['prompts']}=={1,2}
        assert caller.post(path,json={'action':'start'}).status_code==200
        research=finished(caller,path);assert research['state']=='review_research',research
        assert research['research']['answer']=='The Cedar budget is 47.' and research['sources']
        assert all('text' not in s and 'vector' not in s for s in research['sources'])
        assert caller.post(path,json={'action':'start'}).status_code==400  # acceptance is explicit
        assert caller.post(path,json={'action':'accept','step':'research'}).status_code==200
        assert caller.post(path,json={'action':'start'}).status_code==200
        draft=finished(caller,path);assert draft['state']=='review_draft',draft
        sent=state['calls'][-1][2]
        assert json.loads(sent['messages'][1]['content'])['accepted_research']==research['research']['answer']
        assert sent['max_tokens']==2048 and 'tools' not in sent
        assert caller.post(path,json={'action':'accept','step':'draft'}).status_code==200
        final=caller.get(path).json();assert final['state']=='completed'
        assert len(final['attempts'])==2 and all(a['receipts'] for a in final['attempts'])
        assert all(a['usage']['cost'] is None for a in final['attempts'])  # unknown is never a fabricated zero
        assert caller.delete(path).status_code==200 and caller.get(path).status_code==404


def test_failed_draft_keeps_accepted_research_and_retries_only_that_step(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection)
        caller.post(path,json={'action':'start'});research=finished(caller,path)['research']
        caller.post(path,json={'action':'accept','step':'research'})
        state['malformed']={'answer':'uncited','source_ids':[],'abstain':False}
        caller.post(path,json={'action':'start'});failed=finished(caller,path)
        assert failed['state']=='failed_draft' and failed['research']==research
        state['malformed']=None
        caller.post(path,json={'action':'start'});done=finished(caller,path)
        assert done['state']=='review_draft' and done['research']==research
        assert [a['step'] for a in done['attempts']]==['research','draft','draft']
        assert [a['ordinal'] for a in done['attempts']]==[1,1,2]


def test_empty_evidence_never_calls_research_or_draft(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        path=workflow(caller,create(caller))
        caller.post(path,json={'action':'start'});run=finished(caller,path)
        assert run['state']=='no_evidence' and not state['calls']
        assert run['attempts'][0]['usage']['cost'] is None


def test_deleted_source_blocks_draft_but_keeps_accepted_research(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection)
        caller.post(path,json={'action':'start'});research=finished(caller,path)['research']
        caller.post(path,json={'action':'accept','step':'research'})
        caller.delete(doc);before=len(state['calls'])
        caller.post(path,json={'action':'start'});run=finished(caller,path)
        assert run['state']=='failed_draft' and run['research']==research and len(state['calls'])==before
        caller.delete(collection)
        assert caller.get(path).json()['research']==research  # retained private history survives source deletion


def test_cancellation_restart_and_fresh_authorization(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection);state['block']=True
        caller.post(path,json={'action':'start'})
        assert caller.post(path,json={'action':'cancel'}).status_code==200
        assert finished(caller,path)['state']=='paused_research'
        state['block']=False
        caller.post(path,json={'action':'start'});research=finished(caller,path)['research']
        caller.post(path,json={'action':'accept','step':'research'})
    with knowledge(tmp_path) as (caller,state,service):
        assert not state['calls'] and caller.get(path).json()['state']=='ready_draft'
        assert caller.get(path,headers={'authorization':'Bearer admin'}).status_code==404
        assert caller.get('/loadcoach/workflows',headers={'authorization':'Bearer admin'}).json()['prompts']==[]
        assert caller.post(path,headers={'authorization':'Bearer revoked'},json={'action':'start'}).status_code==401
        caller.post(path,json={'action':'start'});run=finished(caller,path)
        assert run['state']=='review_draft' and run['research']==research


@pytest.mark.parametrize('field,value',[('collection',[]),('minimum',True),('minimum',float('inf')),('research_model','embed'),('research_prompt','other-user-id')])
def test_workflow_inputs_cannot_bypass_source_or_model_permissions(tmp_path,field,value):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller)
        body=dict(title='A',question='Cedar?',collection=collection.rsplit('/',1)[1],research_model='chat',draft_model='chat')
        body[field]=value
        if field=='minimum' and value==float('inf'):
            response=caller.post('/loadcoach/workflows',content=json.dumps(body),headers={'content-type':'application/json'})
        else:response=caller.post('/loadcoach/workflows',json=body)
        assert response.status_code in (400,403,404) and not state['calls']


def test_usage_preserves_known_values_and_rejects_stale_receipt_snapshots(tmp_path):
    store=Workflows(str(tmp_path/'chat.db'))
    number=store.create('owner','Title','Question',.65,dict(id='collection',revision=1,level='normal'),{})
    attempt=store.begin('owner',number)
    store.receipt('owner',number,attempt['id'],{'request':'12','stage':'research'})
    usage=lambda cost,prompt:dict(requests=[dict(request='12',cost=cost,prompt_tokens=prompt,output_tokens=3)],cost=cost)
    store.usage('owner',number,attempt['id'],usage(.4,20))
    store.usage('owner',number,attempt['id'],usage(None,None))
    saved=store.attempts('owner',number)[0]['usage'];assert saved['cost']==.4 and saved['requests'][0]['prompt_tokens']==20
    store.receipt('owner',number,attempt['id'],{'request':'13'})
    store.usage('owner',number,attempt['id'],usage(99,999))
    assert store.attempts('owner',number)[0]['usage']==saved
    store.db.close()


def test_postgresql_prompt_and_workflow_transfer(postgres,tmp_path):
    from db import Database
    from db.transfer import copy_store
    store=Workflows(postgres)
    prompt=store.version('owner','Brief','research','Source-bound report.')
    number=store.create('owner','Title','Question',.65,dict(id='collection',revision=1,level='normal'),{'prompt':store.prompt('owner',prompt,'research')})
    attempt=store.begin('owner',number)
    store.finish('owner',number,attempt,'review_research',{'answer':'Accepted fact.','source_ids':['chunk'],'abstain':False})
    store.accept('owner',number,'research');store.begin('owner',number)
    store.db.close()
    target=Database.sqlite(tmp_path/'copy.db');assert copy_store(postgres,target)['state']=='verified'
    restored=Workflows(target);restored.interrupted()
    run=restored.one('owner',number)
    assert run['state']=='paused_draft' and run['research']['answer']=='Accepted fact.'
    assert restored.prompt('owner',prompt,'research')['body']=='Source-bound report.' and restored.listing('other')==[]
    restored.db.close()


@pytest.mark.parametrize('raw',[
    '{"answer":"one","answer":"two","source_ids":["c"],"abstain":false}',
    '{"answer":"one","source_ids":["c"],"abstain":NaN}',
])
def test_shared_citation_validator_rejects_ambiguous_json(raw):
    with pytest.raises(Problem):cited_answer({'choices':[{'message':{'role':'assistant','content':raw}}]},[{'id':'c'}])


def test_native_fixed_workflow_records_normal_requests_and_stays_source_bound(fake_llama_server,tmp_path):
    from chat_clients import gateway
    from test_app import add_user,model
    models={'embed':model(str(tmp_path/'embed.gguf'),embedding=True),
            'chat':model(str(tmp_path/'chat.gguf'),**{'ctx-size':8192,'benchmark-workflow':True})}
    with gateway(fake_llama_server,models=models) as caller:
        caller.headers.update(add_user(caller,'owner','admin'))
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if all(m.get('fingerprint') for m in caller.get('/v1/models').json()['data']):break
            time.sleep(.01)
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection)
        caller.post(path,json={'action':'start'});research=finished(caller,path)
        assert research['state']=='review_research',research
        assert research['research']['answer']=='The Cedar budget is 47.'
        caller.post(path,json={'action':'accept','step':'research'})
        caller.post(path,json={'action':'start'});draft=finished(caller,path)
        assert draft['state']=='review_draft' and draft['draft']['answer']=='Draft: The Cedar budget is 47.',draft
        assert [len(a['receipts']) for a in draft['attempts']]==[2,1]
        caller.post(path,json={'action':'usage'})
        for attempt in caller.get(path).json()['attempts']:
            for receipt in attempt['receipts']:
                record=caller.get('/loadcoach/requests/'+receipt['request']).json()
                assert record['asked']['trace']==receipt['trace']
                assert record['asked']['path'] in ('embeddings','chat/completions')
            assert attempt['usage']['cost'] is None


def test_changed_models_and_sensitivity_block_resume_without_losing_history(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller,level='sensitive');doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection)
        caller.post(path,json={'action':'start'});research=finished(caller,path)['research']
        caller.post(path,json={'action':'accept','step':'research'})
        state['models'][1]['fingerprint']='changed'
        before=len(state['calls']);caller.post(path,json={'action':'start'})
        run=finished(caller,path)
        assert run['state']=='failed_draft' and run['research']==research and len(state['calls'])==before
        state['maximum']='normal'
        assert caller.get(path).status_code==403
        assert caller.get('/loadcoach/workflows').json()['workflows']==[]


def test_queued_workflow_cancel_and_maintenance_release_the_background_count(tmp_path):
    from db.maintenance import Gate
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection);gate=Gate(tmp_path/'settings.toml')
        caller.portal.call(lambda:setattr(service,'maintenance',gate))
        state['block']=True
        assert caller.post(path,json={'action':'start'}).status_code==200
        assert gate.active==1
        gate.path.write_text(json.dumps({'token':'test-maintenance'}))
        other=workflow(caller,collection)
        assert caller.post(other,json={'action':'start'}).status_code==503
        assert caller.post(path,json={'action':'cancel'}).status_code==200
        assert finished(caller,path)['state']=='paused_research' and gate.active==0
        gate.path.unlink()


def test_effective_prompt_and_parameters_are_frozen_before_start(tmp_path,monkeypatch):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection);config=caller.get(path).json()['config']
        monkeypatch.setattr('weightroom.workflows.ANSWER_SYSTEM','Different future source rules.')
        caller.post(path,json={'action':'start'});run=finished(caller,path)
        assert run['state']=='review_research'
        sent=state['calls'][-1][2]
        assert sent['messages'][0]['content']==config['systems']['research']['content']
        assert sent['temperature']==config['temperature'] and sent['max_tokens']==config['outputs']['research']


def test_model_rename_preserves_pinned_identity_and_setup_label(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection);state['models'][1]['id']='renamed'
        caller.post(path,json={'action':'start'});run=finished(caller,path)
        assert run['state']=='review_research'
        assert run['config']['models']['research']['id']=='chat' and state['calls'][-1][2]['model']=='renamed'


def test_usage_uses_matching_trace_records_and_retains_observed_costs(tmp_path):
    with knowledge(tmp_path) as (caller,state,service):
        collection=create(caller);doc=add(caller,collection);ready(caller,collection,doc)
        path=workflow(caller,collection);caller.post(path,json={'action':'start'})
        run=finished(caller,path);receipts=run['attempts'][0]['receipts']
        state['records']={r['request']:{'asked':{'trace':r['trace']},'ended':{'cost':0.,'prompt_tokens':20,'answer_tokens':2}} for r in receipts}
        state['records'][receipts[-1]['request']]['asked']['trace']='unrelated-request'
        caller.post(path,json={'action':'usage'})
        assert caller.get(path).json()['attempts'][0]['usage']['cost'] is None
        state['records'][receipts[-1]['request']]['asked']['trace']=receipts[-1]['trace']
        state['records'][receipts[-1]['request']]['ended']['cost']=.25
        caller.post(path,json={'action':'usage'})
        known=caller.get(path).json()['attempts'][0]['usage']
        assert known['cost']==.25 and known['prompt_tokens']==40 and known['output_tokens']==4
        state['records']={};caller.post(path,json={'action':'usage'})
        assert caller.get(path).json()['attempts'][0]['usage']==known


def test_unobserved_generation_receipt_does_not_turn_partial_cost_into_a_total(tmp_path):
    store=Workflows(str(tmp_path/'chat.db'))
    number=store.create('owner','Title','Question',.65,dict(id='collection',revision=1,level='normal'),{})
    attempt=store.begin('owner',number)
    store.receipt('owner',number,attempt['id'],{'request':'12','stage':'retrieval'})
    store.usage('owner',number,attempt['id'],{'requests':[dict(request='12',cost=0,prompt_tokens=20,output_tokens=0)],'cost':0})
    usage=store.attempts('owner',number)[0]['usage']
    assert usage['cost'] is None and usage['prompt_tokens'] is None
    assert usage['requests'][0]['cost']==0  # the observed component is retained without claiming a complete total
    store.db.close()
