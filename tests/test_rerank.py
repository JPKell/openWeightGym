"""Reranker admission, public alias, model modes and normal batch execution."""
import json

import pytest

from engines.llama_cpp.adapter import LlamaCpp
from engines.llama_cpp.form import settings_from
from engines.llama_cpp.model import effective
from loadcoach.rerank import checked
from test_app import fake_llama_server, in_an_empty_folder, gateway, add_user, model, events  # noqa: F401
from test_batches import until, answers


def test_reranking_form_preserves_mode_and_rejects_embedding_conflicts():
    form = {'file':['rank.gguf'], 'ctx':['8192'], 'reranking':['yes'], 'reranking_controls':['yes'], 'embedding_controls':['false']}
    settings = settings_from(form, json.loads)
    assert settings['flags']['reranking'] is True and settings['flags']['embedding'] is False
    caps = LlamaCpp().capabilities(settings)
    assert caps['rerank'] and caps['load_probes']
    assert all(caps[name] is False for name in ('chat','embeddings','streaming','raw_completion','tools'))
    for kept, expected in [('false', False), ('none', None), ('yes', 'absent')]:
        saved = settings_from({'file':['rank.gguf'], 'ctx':['8192'], 'reranking_controls':[kept]}, json.loads)
        assert saved['flags'].get('reranking', 'absent') == expected
    with pytest.raises(ValueError, match='mutually exclusive'):
        settings_from({**form,'embedding':['yes']}, json.loads)
    with pytest.raises(ValueError, match='mutually exclusive'):
        effective({'flags':{'reranking':True}}, {'embedding':True})


@pytest.mark.parametrize('bad', [ {'documents':[]}, {'documents':[{}]}, {'top_n':True}, {'top_n':0}, {'return_documents':'true'}, {'query':''}, {'unknown':1} ])
def test_bounded_rerank_contract(bad):
    with pytest.raises(ValueError):
        checked({'model':'rank','query':'capital','documents':['Paris'], **bad})


@pytest.mark.parametrize('path', ['/v1/rerank','/rerank'])
def test_rerank_uses_normal_forward_and_canonical_route(fake_llama_server, path):
    with gateway(fake_llama_server, {'rank':model('rank.gguf',0,reranking=True),'chat':model('chat.gguf',0)}) as client:
        who=add_user(client,'owner','admin')
        rows={row['id']:row for row in client.get('/v1/models',headers=who).json()['data']}
        assert rows['rank']['rerank'] and not rows['rank'].get('embedding')
        body={'model':'rank','query':'capital of France','documents':['Paris','bananas'],'top_n':1,'return_documents':True}
        result=client.post(path,json=body,headers=who)
        assert result.status_code==200,result.text
        assert result.json()['path']=='/v1/rerank' and json.loads(result.json()['body'])==body
        assert result.headers.get('x-loadcoach-request-id')
        assert client.post(path,json={**body,'model':'chat'},headers=who).json()['error']['message']=='Choose a reranker model for rerank requests.'
        for other,fields in [('/v1/chat/completions',{'messages':[]}),('/v1/embeddings',{'input':'hi'})]:
            refused=client.post(other,json={'model':'rank',**fields},headers=who)
            assert refused.status_code==400 and 'reranking only' in refused.text
        assert client.post(path,json={**body,'model':'hidden'},headers=who).status_code==404
        done=[row for row in events(client,who,kind='request') if row['what']=='done']
        assert done and done[0]['answer_tokens']==0


def test_rerank_batch_uses_existing_worker(fake_llama_server):
    with gateway(fake_llama_server, {'rank':model('rank.gguf',0,reranking=True)}) as client:
        who=add_user(client,'owner','admin')
        body={'model':'rank','query':'capital','documents':['Paris','fruit']}
        line=json.dumps({'custom_id':'rank','method':'POST','url':'/v1/rerank','body':body}).encode()
        file=client.post('/v1/files',headers=who,data={'purpose':'batch'},files={'file':('rank.jsonl',line)}).json()
        started=client.post('/v1/batches',headers=who,json={'input_file_id':file['id'],'endpoint':'/v1/rerank','completion_window':'24h'})
        assert started.status_code==200,started.text
        done=until(client,who,started.json()['id'],lambda one:one['status']=='completed')
        assert done['request_counts']=={'total':1,'completed':1,'failed':0}, answers(client,who,done['error_file_id']) if done.get('error_file_id') else done
        output=answers(client,who,done['output_file_id'])[0]['response']['body']
        assert output['path']=='/v1/rerank' and json.loads(output['body'])==body
