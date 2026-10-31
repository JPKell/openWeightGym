"""Native conversation transport, bounded formats, receipts and unavailable operations."""
import copy
import json

import pytest

from loadcoach.probes import Probes
from test_app import add_user,fake_cards,fake_llama_server,gateway,in_an_empty_folder,model
from test_tasks import create
from test_weightroom import gguf,desk,sign_in

SCHEMA = {'type':'json_schema','json_schema':{'name':'label','strict':True,'schema':{
    'type':'object','properties':{'code':{'type':'string'}},'required':['code'],'additionalProperties':False}}}
TOOL = {'type':'function','function':{'name':'lookup_label','parameters':{'type':'object','properties':{},'additionalProperties':False}}}
MESSAGES = [{'role':'system','content':'Keep the stated code.'},
            {'role':'user','content':'The code is amber.'},
            {'role':'assistant','content':'Understood.'},
            {'role':'user','content':'Return the code as JSON.'}]
RECOMMENDED_SAMPLING = {'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
                        'presence_penalty':0.0,'frequency_penalty':0.0}


def models(**flags):
    gguf('small.gguf',general__architecture='tiny',tiny__context_length=8192,tokenizer__chat_template='messages tools')
    return {'small':model('small.gguf',['GPU0'],**{'ctx-size':8192,'mib':1000,'jinja':True,'benchmark-dialogue':True,**flags})}


@pytest.mark.parametrize('field',['evaluation','dialogue'])
@pytest.mark.parametrize('tools',[False,True])
@pytest.mark.parametrize('format',[None,{'type':'json_object'},SCHEMA])
def test_native_conversation_preserves_history_calls_reasoning_and_receipts(fake_llama_server,fake_cards,field,tools,format):
    with gateway(fake_llama_server,models=models(),nvidia_smi=fake_cards[0]) as client:
        owner=add_user(client,'owner','admin');task=create(client,owner)
        spec={'messages':copy.deepcopy(MESSAGES)}
        if tools:
            spec['tools']=[TOOL]
        if format:
            spec['response_format']=format
        response=client.post('/loadcoach/tasks/'+task['id']+'/probe',headers={'x-loadcoach-task':task['token']},
                             json={'context':8192,'inputs':0,'outputs':256,field:spec})
        assert response.status_code==200,response.text
        result=response.json();values,evidence=result['values'],result['evidence']
        assert values['success'] and values['output_tokens']==16 and values['output_budget']==256
        assert values['input_tokens']==len(evidence['prompt'])==evidence['native_usage']['prompt_tokens']
        assert json.loads(evidence['prompt'])=={'messages':MESSAGES,'tools':[TOOL] if tools else []}
        assert evidence['native_dialogue'] and evidence['native_tools']==tools
        assert evidence['response_format']==format and evidence['native_timings']['cache_n']==0
        message=evidence['native_message']
        assert message['reasoning_content']=='private reasoning'
        if tools:
            assert message['tool_calls'][0]['function']['name']=='lookup_label'
        else:
            assert json.loads(message['content'])=={'code':'amber'}
        if field=='evaluation':
            assert result['output']==(message.get('content') or '')
        else:
            assert json.loads(result['output'])==message
        assert client.get('/loadcoach/status',headers=owner).json()['loaded']==[]  # child cleaned up


@pytest.mark.parametrize('flag,stage,unsupported',[('benchmark-dialogue-reject','generation',True),
    ('benchmark-template-reject','template',True),(None,'context',False)])
def test_rejected_or_oversized_conversation_is_retained_as_unavailable(fake_llama_server,fake_cards,flag,stage,unsupported):
    with gateway(fake_llama_server,models=models(**({flag:True} if flag else {})),nvidia_smi=fake_cards[0]) as client:
        owner=add_user(client,'owner','admin');task=create(client,owner)
        context=8192 if flag else 128
        response=client.post('/loadcoach/tasks/'+task['id']+'/probe',headers={'x-loadcoach-task':task['token']},
                             json={'context':context,'outputs':64,'evaluation':{'messages':MESSAGES,'response_format':SCHEMA}})
        assert response.status_code==200,response.text
        result=response.json()
        assert not result['values']['success'] and result['values']['unavailable']
        assert result['values']['unsupported']==unsupported and result['evidence']['unavailable_stage']==stage
        assert result['output']=='' and result['values']['error']
        assert client.get('/loadcoach/status',headers=owner).json()['loaded']==[]


def test_incomplete_native_token_receipt_is_refused(fake_llama_server,fake_cards):
    with gateway(fake_llama_server,models=models(**{'benchmark-dialogue-malformed':True}),nvidia_smi=fake_cards[0]) as client:
        owner=add_user(client,'owner','admin');task=create(client,owner)
        response=client.post('/loadcoach/tasks/'+task['id']+'/probe',headers={'x-loadcoach-task':task['token']},
                             json={'context':8192,'outputs':64,'evaluation':{'messages':MESSAGES}})
        assert response.status_code==400 and 'uncached token counts' in response.text
        assert client.get('/loadcoach/status',headers=owner).json()['loaded']==[]


@pytest.mark.parametrize('bad',[
    {'messages':MESSAGES,'question':'both'}, {'messages':MESSAGES,'blocks':['padding']},
    {'messages':[{'role':'developer','content':'x'}]}, {'messages':[{'role':'user','content':None}]},
    {'messages':[{'role':'user','content':[], 'tool_calls':[]}]},
    {'messages':[{'role':'user','content':'x','tool_call_id':'foreign'}]},
    {'messages':MESSAGES*14}, {'messages':MESSAGES,'tools':[TOOL]*17},
    {'messages':MESSAGES,'response_format':{'type':'json_object','url':'https://elsewhere'}},
    {'messages':MESSAGES,'response_format':{'type':'json_schema','json_schema':{'name':'bad','schema':{'$ref':'file:///private'}}}},
    {'messages':MESSAGES,'response_format':{'type':'json_schema','json_schema':{'name':'bad','schema':{'$ref':'#'}}}},
    {'messages':MESSAGES,'response_format':{'type':'json_schema','json_schema':{'name':'bad','strict':1,'schema':{}}}},
    {'messages':MESSAGES,'response_format':{'type':'json_schema','json_schema':{'name':'bad','schema':{'type':'not-a-type'}}}},
])
def test_conversation_validator_rejects_media_padding_overrides_and_unbounded_schemas(bad):
    with pytest.raises(ValueError):
        Probes.validate_dialogue(bad)


def test_validator_keeps_32_message_compatibility_and_bounds_schema_size_depth_and_nonfinite_values():
    Probes.validate_dialogue({'messages':[{'role':'user','content':'x'}]*32,'tools':[]})
    Probes.validate_dialogue({'messages':[{'role':'assistant','content':None,'tool_calls':[{
        'id':'a','type':'function','function':{'name':'lookup_label','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'a','content':'done'}]})
    for schema in ({'enum':['x'*20000]}, {'enum':[float('nan')]}):
        with pytest.raises(ValueError):
            Probes.validate_dialogue({'messages':MESSAGES,'response_format':{'type':'json_schema','json_schema':{'name':'large','schema':schema}}})
    deep={'type':'string'}
    for _ in range(30):
        deep={'type':'array','items':deep}
    with pytest.raises(ValueError,match='nesting'):
        Probes.validate_response_format({'type':'json_schema','json_schema':{'name':'deep','schema':deep}})
    with pytest.raises(ValueError,match='512 KiB'):
        Probes.validate_dialogue({'messages':[{'role':'user','content':'x'*600000}]})


def test_schema_and_adapter_are_sent_to_native_endpoint_without_tool_options():
    import asyncio
    from types import SimpleNamespace
    import httpx
    sent=[]
    async def native(request):
        body=json.loads(request.content);sent.append((request.url.path,body))
        if request.url.path=='/apply-template':
            return httpx.Response(200,json={'prompt':'rendered'})
        if request.url.path=='/tokenize':
            return httpx.Response(200,json={'tokens':list(range(8))})
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':8,'completion_tokens':2},'timings':{'cache_n':0}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
            probe=Probes(SimpleNamespace(client=client),None,None)
            server=SimpleNamespace(url='http://native',settings={'adapters':{'a':{'scale':.5}}})
            return await probe.dialogue(server,dict(context=256,outputs=64,evaluation={'messages':MESSAGES,'response_format':SCHEMA,
                                                                                       'temperature':0.2,'sampling':RECOMMENDED_SAMPLING}))
    measured,_,evidence=asyncio.run(run())
    assert measured['input_tokens']==8
    assert evidence['request_sampling']=={'temperature':0.2,**RECOMMENDED_SAMPLING}
    payload=next(body for path,body in sent if path=='/v1/chat/completions')
    assert payload['messages']==MESSAGES and payload['response_format']==SCHEMA
    assert payload['lora']==[{'id':0,'scale':.5}] and payload['max_tokens']==64
    assert payload['temperature']==0.2 and payload['seed']==42
    assert all(payload[key]==value for key,value in RECOMMENDED_SAMPLING.items())
    assert 'tools' not in payload and 'tool_choice' not in payload and 'parallel_tool_calls' not in payload


def test_optional_evaluation_temperature_is_finite_and_bounded():
    assert Probes.evaluation_temperature({}) == 0
    assert Probes.evaluation_temperature({'temperature':0.2}) == 0.2
    Probes.validate_dialogue({'messages':MESSAGES,'temperature':0.2})
    for value in (True,-0.1,1.1,float('nan'),float('inf'),'0.2'):
        with pytest.raises(ValueError,match='temperature'):
            Probes.evaluation_temperature({'temperature':value})
        with pytest.raises(ValueError,match='temperature'):
            Probes.validate_dialogue({'messages':MESSAGES,'temperature':value})


def test_optional_evaluation_sampling_is_allowlisted_and_bounded():
    assert Probes.evaluation_sampling({}) == {}
    assert Probes.evaluation_sampling({'sampling':RECOMMENDED_SAMPLING}) == RECOMMENDED_SAMPLING
    Probes.validate_dialogue({'messages':MESSAGES,'sampling':RECOMMENDED_SAMPLING})
    for invalid in ({'unknown':1}, {'top_k':True}, {'top_k':-1}, {'top_k':1001},
                    {'top_p':-0.1}, {'min_p':float('nan')}, {'repeat_penalty':3},
                    {'presence_penalty':float('inf')}, {'frequency_penalty':'0'}):
        with pytest.raises(ValueError,match='sampling|top_k|top_p|min_p|repeat_penalty|presence_penalty|frequency_penalty'):
            Probes.evaluation_sampling({'sampling':invalid})
        with pytest.raises(ValueError):
            Probes.validate_dialogue({'messages':MESSAGES,'sampling':invalid})


def test_task_probe_rejects_unbounded_prompt_sampling_before_native_load(fake_llama_server,fake_cards):
    with gateway(fake_llama_server,models=models(),nvidia_smi=fake_cards[0]) as client:
        owner=add_user(client,'owner','admin');task=create(client,owner)
        response=client.post('/loadcoach/tasks/'+task['id']+'/probe',headers={'x-loadcoach-task':task['token']},
                             json={'context':8192,'outputs':64,'evaluation':{'question':'Return JSON.',
                                   'sampling':{'top_k':1001}}})
        assert response.status_code==400 and 'top_k' in response.text
        assert client.get('/loadcoach/status',headers=owner).json()['loaded']==[]


def test_prompt_evaluation_passes_optional_temperature_to_native_completion():
    import asyncio
    from types import SimpleNamespace
    import httpx
    sent=[]
    async def native(request):
        sent.append(json.loads(request.content))
        piece={'content':'{}','tokens':[1],'timings':{'prompt_n':3,'predicted_n':1,
               'prompt_per_second':10,'predicted_per_second':20,'cache_n':0},
               'stop':True,'stop_type':'eos'}
        return httpx.Response(200,content=('data: '+json.dumps(piece)+'\n\n').encode())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
            probe=Probes(SimpleNamespace(client=client),None,None)
            return await probe.completion(SimpleNamespace(url='http://native',settings={}),[1,2,3],16,
                                          ignore_eos=False,temperature=0.2,sampling=RECOMMENDED_SAMPLING)
    measured,answer=asyncio.run(run())
    assert answer=='{}' and measured['output_tokens']==1
    assert sent[0]['temperature']==0.2 and sent[0]['seed']==42 and sent[0]['ignore_eos'] is False
    assert all(sent[0][key]==value for key,value in RECOMMENDED_SAMPLING.items())


def test_front_worker_freezes_and_resumes_general_conversation_evidence(fake_llama_server,fake_cards,tmp_path,monkeypatch):
    from benchmark_clients import gateway as front_gateway
    from benchmarks.suites import json_schema
    from benchmarks.worker import BenchmarkWorker
    from test_benchmarks import start,wait
    cases=[]
    for n in range(4):
        request={'messages':copy.deepcopy(MESSAGES)}
        if n%2:
            request['response_format']=SCHEMA
        cases.append(dict(id=f'conversation-{n}',category='conversation',inputs=0,outputs=256,context=8192,
                          request=request,schema=SCHEMA['json_schema']['schema'],expected={'code':'amber'}))
    monkeypatch.setattr(json_schema,'cases',lambda limit:copy.deepcopy(cases))
    sent=[];original=BenchmarkWorker.request
    async def observe(self,number,method='GET',suffix='',**kwargs):
        if suffix=='/probe':
            sent.append(copy.deepcopy(kwargs['json']))
            assert set(kwargs['json']['evaluation']) <= {'messages','response_format'}
            assert 'expected' not in kwargs['json'] and 'schema' not in kwargs['json']
        return await original(self,number,method,suffix,**kwargs)
    monkeypatch.setattr(BenchmarkWorker,'request',observe)
    configured=models(**{'benchmark-delay':.2});database=str(tmp_path/'conversations.db')
    with front_gateway(fake_llama_server,models=configured,nvidia_smi=fake_cards[0],database=database) as client:
        owner=add_user(client,'owner','admin');number=start(client,owner,'json_schema')
        first=wait(client,owner,number,lambda r:r['samples_count']>=1)['samples'][0]
        client.post('/benchmarks/control',headers=owner,json={'run':number,'action':'pause'})
        paused=wait(client,owner,number,lambda r:r['state']=='paused')
    with front_gateway(fake_llama_server,models=configured,nvidia_smi=fake_cards[0],database=database) as client:
        saved=client.get('/benchmarks/'+number,headers=owner).json()
        assert saved['plan']==paused['plan'] and saved['samples'][0]==first
        client.post('/benchmarks/control',headers=owner,json={'run':number,'action':'resume'})
        done=wait(client,owner,number,lambda r:r['state'] in ('completed','failed'))
        assert done['state']=='completed',done['reason']
        assert done['score']==100 and len(done['samples'])==4 and done['samples'][0]==first
        for sample in done['samples']:
            assert json.loads(sample['evidence']['answer'])=={'code':'amber'}
            assert sample['evidence']['native_message']['reasoning_content']=='private reasoning'
            assert sample['evidence']['native_timings']['cache_n']==0
    assert sent


def test_native_receipts_and_rejections_are_visible_without_rendering_model_markup(desk,tmp_path):
    from loadcoach.models import Models
    server,_,users=desk
    library=Models(str(tmp_path/'loadcoach.db'))
    library.put('native-model',{'flags':{'model':str(tmp_path/'model.gguf')}})
    store=server.app.state.benchmarks.state.store
    number=store.create('jordan','native-model','turns',library.identity('native-model'))
    store.activate(number,{'fingerprint':'frozen','subject':{},'environment':{}})
    store.keep(number,'native',{'success':False,'unsupported':True,'checks':1,'correct':0,'passed':False},'','',
        dict(prompt='rendered conversation',answer='',case={'expected':{'code':'amber'}},checks=[],native_dialogue=True,
             native_message={'role':'assistant','content':None,'reasoning_content':'<script>never_run()</script>'},
             response_format=SCHEMA,native_usage={'prompt_tokens':20},native_timings={'cache_n':0},
             unavailable_stage='generation',native_error='<script>schema unsupported</script>'))
    store.plan(number,{'cases':[{'id':'native'}]})
    store.finish(number,'completed');store.synced(number)
    sign_in(server,'jordan')
    page=server.get('/benchmarks/run/'+number)
    assert page.status_code==200
    assert 'Native conversation response' in page.text and 'Native usage and timings' in page.text
    assert 'Requested response format' in page.text and 'Unavailable at generation' in page.text
    assert 'Score: —' in page.text and '<th>Unsupported</th>' in page.text
    assert '<script>never_run()' not in page.text and '<script>schema unsupported' not in page.text
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    with sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000})
        page=context.new_page();errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def serve(route):
            if route.request.url.split('?', 1)[0] == 'http://native.test/strip/stream':
                from front_helpers import serve_browser_request
                return serve_browser_request(server, 'http://native.test', route)
            request=route.request
            response=server.request(request.method,request.url.replace('http://native.test',''),
                content=request.post_data_buffer,headers={'HX-Request':request.headers.get('hx-request','false')})
            route.fulfill(status=response.status_code,content_type=response.headers.get('content-type','text/html'),body=response.content)
        context.route('http://native.test/**',serve)
        page.goto('http://native.test/benchmarks/run/'+number)
        page.locator('#sample-1 > summary').click()
        page.locator('#sample-1 summary').filter(has_text='Native conversation response').click()
        assert 'private' not in page.locator('#sample-1').inner_text()
        assert 'never_run()' in page.locator('#sample-1').inner_text()  # escaped data, never script
        folder=Path('data/m101-verification');folder.mkdir(parents=True,exist_ok=True)
        for width in (1440,390,320):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(folder/f'native-{width}.png'),full_page=True)
        assert not errors
        browser.close()
    users.change('pat',own={'models':[]})
    sign_in(server,'pat')
    assert server.get('/benchmarks/run/'+number).status_code==404


@pytest.mark.parametrize('fault',['cache_missing','cache_boolean','cache_used','prompt_boolean','count_mismatch','choice_shape','media','reason_nonfinite'])
def test_general_native_receipt_must_explicitly_prove_uncached_complete_text(fault):
    import asyncio
    from types import SimpleNamespace
    import httpx
    result={'choices':[{'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':8,'completion_tokens':2},'timings':{'cache_n':0}}
    if fault=='cache_missing':
        result['timings']={}
    elif fault=='cache_boolean':
        result['timings']['cache_n']=False
    elif fault=='cache_used':
        result['timings']['cache_n']=1
    elif fault=='prompt_boolean':
        result['usage']['prompt_tokens']=True
    elif fault=='count_mismatch':
        result['usage']['prompt_tokens']=7
    elif fault=='choice_shape':
        result['choices']=[None]
    elif fault=='media':
        result['choices'][0]['message']['content']=[]
    else:
        result['timings']['predicted_ms']=float('inf')
    async def native(request):
        if request.url.path=='/apply-template':
            return httpx.Response(200,json={'prompt':'rendered'})
        if request.url.path=='/tokenize':
            return httpx.Response(200,json={'tokens':list(range(8))})
        return httpx.Response(200,content=json.dumps(result),headers={'content-type':'application/json'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
            probe=Probes(SimpleNamespace(client=client),None,None)
            return await probe.dialogue(SimpleNamespace(url='http://native',settings={}),
                dict(context=256,outputs=64,evaluation={'messages':MESSAGES}))
    with pytest.raises(ValueError):
        asyncio.run(run())


@pytest.mark.parametrize('value',[None,0,1,-1,True,3,'1'])
def test_native_reasoning_counts_are_reported_only_when_valid(value):
    import asyncio
    from types import SimpleNamespace
    import httpx
    async def native(request):
        if request.url.path=='/apply-template':return httpx.Response(200,json={'prompt':'rendered'})
        if request.url.path=='/tokenize':return httpx.Response(200,json={'tokens':list(range(8))})
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':8,'completion_tokens':2,'completion_tokens_details':{'reasoning_tokens':value}},'timings':{'cache_n':0}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
            probe=Probes(SimpleNamespace(client=client),None,None)
            server=SimpleNamespace(url='http://native',settings={})
            return await probe.dialogue(server,dict(context=256,outputs=64,evaluation={'messages':MESSAGES}))
    measured,_,evidence=asyncio.run(run())
    if type(value) is int and 0<=value<=2:assert measured['reasoning_tokens']==value
    else:assert 'reasoning_tokens' not in measured
    assert evidence['native_usage']['completion_tokens_details']['reasoning_tokens']==value


@pytest.mark.parametrize('field',['evaluation','dialogue'])
def test_model_output_the_native_server_cannot_parse_is_a_failed_sample_not_a_broken_probe(fake_llama_server,fake_cards,field):
    with gateway(fake_llama_server,models=models(**{'benchmark-dialogue-unparseable':True}),nvidia_smi=fake_cards[0]) as client:
        owner=add_user(client,'owner','admin');task=create(client,owner)
        response=client.post('/loadcoach/tasks/'+task['id']+'/probe',headers={'x-loadcoach-task':task['token']},
                             json={'context':8192,'outputs':64,field:{'messages':MESSAGES,**({'tools':[]} if field=='dialogue' else {})}})
        assert response.status_code==200,response.text
        result=response.json()['values']
        assert not result['success'] and not result['unsupported'] and 'could not parse' in result['error']
