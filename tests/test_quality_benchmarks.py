"""Deterministic scoring, exact prompted probes, frozen plans and retained quality evidence."""

import asyncio
import copy
import json
import re
import time

import pytest

from benchmarks import MODULES
from benchmarks import quality
from benchmarks.store import Benchmarks
from benchmarks.suites import json_schema, extraction, arithmetic, retrieval, code_retrieval, ifeval, abstention, tables, turns, reasoning, hierarchy
from benchmark_clients import gateway
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder
from test_benchmarks import bench_model, start, wait
from test_weightroom import gguf


@pytest.mark.parametrize('module',[json_schema,extraction,arithmetic,retrieval,code_retrieval,abstention,tables,turns,reasoning,hierarchy])
def test_answer_keys_pass_and_wrong_typed_values_do_not(module):
    cases=module.cases(8192)
    assert cases==module.cases(8192)
    assert len({c['id'] for c in cases})==len(cases)
    grade=getattr(module,'grade',quality.grade_fields)
    for case in cases:
        good=json.dumps(case['expected'],ensure_ascii=False)
        metrics,checks=grade(case,good)
        assert metrics['passed'] and metrics['correct']==metrics['checks'],case['id']
        assert all(c['passed'] for c in checks)
        wrong=copy.deepcopy(case['expected'])
        key=next(iter(wrong));wrong[key]=['wrong type and value']
        metrics,_=grade(case,json.dumps(wrong))
        assert not metrics['passed']
        # A fence round the answer: only where the format is the thing asked, in the JSON suite, does it fail.
        fenced=grade(case,'Here it is:\n```json\n'+good+'\n```')[0]
        assert fenced['passed']==(module not in (json_schema,hierarchy)) and not fenced['json_valid']
        if module is json_schema:
            assert fenced['value_correct']==fenced['value_checks']>0  # it knew the answer, and that is kept beside the score
    assert not quality.same(True,1) and not quality.same([False],[0])
    assert not quality.same([1,2],[2,1])


def test_only_the_final_answer_is_graded_whatever_way_a_model_marks_its_reasoning():
    final=quality.final_answer
    assert final(' {"a":1} ')=='{"a":1}'
    assert final('<think>maybe {"a":9}</think>\n{"a":1}')=='{"a":1}' and final('maybe {"a":9}\n</think>{"a":1}')=='{"a":1}'
    assert final('[THINK]hm[/THINK]{"a":1}')=='{"a":1}' and final('<|channel>thought\nhm<channel|>{"a":1}')=='{"a":1}'
    # gpt-oss and its kin: channels. Only the last "final" one is the answer.
    assert final('<|channel|>analysis<|message|>so {"a":9}<|end|><|start|>assistant<|channel|>final<|message|>{"a":1}<|return|>')=='{"a":1}'
    assert final('<|channel|>final <|constrain|>json<|message|>{"a":1}')=='{"a":1}'
    assert final(' to=self<|message|>the final count is 9<|start|>assistant to=user<|message|>{"a":1}')=='{"a":1}' and final(' to=self<|message|>the final count is 9')==''
    # Still reasoning when it stopped: no answer, though its notes hold something that looks like one.
    assert final('<think>maybe {"a":9}')=='' and final('<|channel|>analysis<|message|>so {"a":9}')==''
    assert final('maybe {"a":9}','question<assistant>\n<think>\n')==''  # the template opened the reasoning, so the mark is in the prompt
    assert final('<think>one</think>{"a":1}<think>two')==''


def test_an_answer_is_found_in_whatever_dress_and_the_json_suite_alone_minds_the_dress():
    case={'expected':{'customer':'Mira Chen','units':83,'totals':{'blue':3},'labels':['a','b']}}
    right='{"customer":"Mira Chen","units":83,"totals":{"blue":3},"labels":["a","b"]}'
    grade=lambda answer,**how: quality.grade_fields(case,answer,**how)[0]
    assert grade(right)=={'correct':4,'checks':4,'passed':True,'json_valid':True}
    # In a sentence, grouped by record, a number as text, space round a name, a key nobody asked for: all the same answer.
    dressed='Sure. {"R-1":{"customer":" Mira Chen ","units":"83"},"R-2":{"totals":{"blue":"3"},"labels":["a","b"]},"note":"done"} Hope that helps.'
    assert grade(dressed)=={'correct':4,'checks':4,'passed':True,'json_valid':False}
    assert grade('a draft {"customer":"nobody"} and then '+right)['passed']  # the last object is the answer
    # What is wrong stays wrong: another value, another order, true for a number, no JSON at all.
    assert grade(right.replace('83','84'))['correct']==3 and grade(right.replace('["a","b"]','["b","a"]'))['correct']==3
    assert grade(right.replace('83','true'))['correct']==3 and grade('The customer is Mira Chen.')['correct']==0
    assert not grade(right.replace('"totals"','"sums"'))['passed']  # a key that is wanted is not looked for inside another
    # Strict, as the JSON suite grades: bare JSON, exactly the keys, exactly the types.
    assert grade(right,strict=True)['passed'] and not grade(dressed,strict=True)['passed'] and grade(dressed,strict=True)['correct']==0
    assert grade(right.replace('83','"83"'),strict=True)['correct']==3 and not grade(right[:-1]+',"note":1}',strict=True)['passed']


def test_a_model_is_given_room_to_think_and_a_small_context_still_gets_cases():
    assert quality.room(131072)==quality.OUTPUTS==8192 and quality.room(2048)==1024
    assert quality.sizes(131072,[1024,65536])==[1024,65536] and quality.sizes(8192,[512,2048,8192])==[512,2048,4096]
    for module in (json_schema,arithmetic,extraction,retrieval,code_retrieval,ifeval):
        assert module.PROTOCOL['version']==module.DEFINITION['version']==({'arithmetic':3,'retrieval':5,'json_schema':6,'extraction':4,'ifeval':3}.get(module.DEFINITION['id'],2)) and module.PROTOCOL['thinking']=='as served'
        assert all(c['outputs']>=1024 and c['inputs']+c['outputs']<=c['context']<=2048 for c in module.cases(2048))


@pytest.mark.parametrize('module',[extraction,retrieval])
def test_approved_answer_room_and_capped_diagnostics_are_separate(module):
    for limit in (2048,8192,24576,131072):
        cases=module.cases(limit)
        assert all(c['outputs']==min(32768,limit//2) for c in cases)
        assert all(c['inputs']+c['outputs']<=c['context']<=limit for c in cases)
    samples=[{'values':{'checks':10,'correct':8,'passed':False,'success':True}},
             {'values':{'checks':10,'correct':0,'passed':False,'success':True,'generation_capped':True}}]
    result=module.summarize(samples)
    assert result['accuracy']==40 and result['case_accuracy']==0
    assert result['capped_cases']==1 and result['uncapped_accuracy']==80 and result['uncapped_cases']==1
    assert result['qualification_valid'] is False
    assert module.summarize(samples[:1])['qualification_valid'] is True


@pytest.mark.parametrize('module,checks',[(extraction,8),(retrieval,10)])
def test_extraction_and_retrieval_qualification_needs_successful_native_samples(module,checks):
    good={'values':{'success':True,'checks':checks,'correct':checks,'passed':True}}
    failed={'values':{'success':False,'checks':checks,'correct':0,'passed':False}}
    unavailable={'values':{**failed['values'],'unavailable':True}}
    unsupported={'values':{**failed['values'],'unsupported':True}}
    missing_success={'values':{'checks':checks,'correct':0,'passed':False}}
    inconsistent_unavailable={'values':{**good['values'],'unavailable':True}}
    inconsistent_unsupported={'values':{**good['values'],'unsupported':True}}
    assert module.summarize([good])['qualification_valid'] is True
    for bad in (failed,unavailable,unsupported,missing_success,
                inconsistent_unavailable,inconsistent_unsupported):
        result=module.summarize([good,bad])
        assert result['qualification_valid'] is False
        assert result['available_cases']==1+bool(bad['values'].get('success')) and result['cases']==2
        assert result['accuracy']==50+50*(bad['values']['correct']==checks)
        assert result['uncapped_cases']==2
    capped={'values':{**good['values'],'generation_capped':True}}
    assert module.summarize([good,capped])['qualification_valid'] is False


@pytest.mark.parametrize('answer',['{"a":1,"a":1}','{"a":NaN}','{"a":Infinity}','{"a":1e999}','prefix {"a":1}'])
def test_json_rejects_ambiguous_or_nonfinite_answers(answer):
    with pytest.raises(ValueError):
        quality.strict_json(answer)


def test_retrieval_has_multiple_lengths_facts_and_both_code_languages():
    for module in (extraction,retrieval,code_retrieval):
        cases=module.cases(131072)
        assert len({c['inputs'] for c in cases})>=3
        assert all(len(c['expected'])>=6 for c in cases)
        assert all(c['inputs']+c['outputs']<=c['context']<=131072 for c in cases)
        if module in (extraction,retrieval):
            continue  # every one of its cases is another document
        # Answer keys are reused across lengths; padding does not change the question's facts.
        first=[c for c in cases if c['id'].endswith('-1')]
        groups={c['category'] for c in first}
        for category in groups:
            assert len({json.dumps(c['expected'],sort_keys=True) for c in first if c['category']==category})==1
    assert {c['category'] for c in code_retrieval.cases(8192)}=={'python','javascript'}


def score(module,answerer):
    """What a scripted answerer scores on a suite: right fields over all fields, as the headline is."""
    return quality.summarize([{'values':module.grade(case,json.dumps(answerer(case)))[0]} for case in module.cases(131072)])['accuracy']


def nothing(case):
    """A lazy model: null for every key."""
    return dict.fromkeys(case['expected'])


DATE=r'(\d{1,2}) ([A-Z][a-z]+) (\d{4})|([A-Z][a-z]{2}) (\d{1,2})[a-z]{2}, (\d{4})'
SHAPES={'customer':r'[A-Z][a-z]+ (?:%s)\b'%'|'.join(quality.LAST),'venue':r'[A-Z][a-z]+ (?:%s)\b'%'|'.join(quality.PLACE_B),
        'email':r'\S+@\S+\.example','invoice':r'INV-\d+','units':r'\b\d\d\b|\b[a-z]+ty(?:-[a-z]+)?\b','total_cents':r'[\d,]+\.\d\d(?= dollars)','delivery_date':DATE}
SHAPES['contact']=SHAPES['customer']


def first_of_its_shape(case):
    """A lazy model: for each key, the first value in the text that looks right, put in the form that is asked for."""
    text,answer='\n'.join(case['request']['blocks']),{}
    for key in case['expected']:
        found=re.search(SHAPES[key],text)
        value=found and found[0]
        if value and key=='delivery_date':
            day,month,year=(found[1],found[2],found[3]) if found[1] else (found[5],found[4],found[6])
            value=f'{year}-{[m[:3] for m in extraction.MONTHS].index(month[:3])+1:02}-{int(day):02}'
        if value and key=='total_cents':
            value=int(value.replace(',','').replace('.',''))
        if value and key=='units':
            value=int(value) if value.isdigit() else {extraction.count(n):n for n in range(21,100)}.get(value)
        answer[key]=value
    return answer


def test_extraction_asks_for_facts_split_over_prose_and_a_table_that_a_lazy_answer_does_not_find():
    cases=extraction.cases(131072)
    assert extraction.PROTOCOL['dataset']=='extraction-v5'
    assert len(cases)==36 and sum(len(c['expected']) for c in cases)==288
    assert {(c['category'],c['inputs']) for c in cases}=={(kind,size) for kind in extraction.KINDS for size in (1024,2048,8192)}
    documents=lambda some:[(c['request'],c['expected']) for c in some[:12]]
    assert documents(cases)==documents(extraction.cases(8192))  # a document is the same whatever the limit; only the room to answer differs
    used=[]
    for case in cases:
        request,kinds=case['request'],case['kinds']
        assert 1<=list(kinds.values()).count('absent')<=2 and all((kinds[key]=='absent')==(value is None) for key,value in case['expected'].items())
        assert {kinds['delivery_date'],kinds['total_cents']}<={'normalised','absent','combined'}
        assert list(kinds.values()).count('combined')==1 and len(request['blocks'])==5  # one fact by pointing at another order; four orders and the table
        assert quality.clear(case)
        text='\n'.join(request['blocks'])
        # What is expected is in the text only where it is asked for as it is written, and what is absent is not there at all.
        for key,value in case['expected'].items():
            if kinds[key]=='plain' and key!='units':  # two digits turn up inside other numbers
                assert text.count(str(value))==1,(case['id'],key)
            assert key+':' not in text
        assert len(request['question'])+len(text)<2400  # room in the 1,024-token case for the chat template
        # Units are the one thing there are too few of to give each case its own.
        used+=[value for key,value in case['expected'].items() if value is not None and key!='units']
        used+=[value for value in case['distractors'] if type(value) is str]
    assert len(set(used))==len(used)  # no person, place, date or identifier in two cases
    assert not any(str(value) in cases[0]['request']['filler'] for value in used)
    assert score(extraction,nothing)<50 and score(extraction,first_of_its_shape)<50


def test_absent_fields_are_counted_apart_and_only_where_a_case_has_them():
    case={'expected':{'name':'Mira Chen','email':None},'kinds':{'name':'plain','email':'absent'}}
    said,made_up=quality.grade_fields(case,'{"name":"Mira Chen","email":null}'),quality.grade_fields(case,'{"name":"Mira Chen","email":"m@example.test"}')
    assert said[0]=={'correct':2,'checks':2,'passed':True,'json_valid':True,'absent':1,'absent_correct':1}
    assert [check['kind'] for check in said[1]]==['plain','absent'] and made_up[0]['absent_correct']==0 and not made_up[0]['passed']
    assert not quality.grade_fields(case,'{"name":"Mira Chen"}')[0]['passed']  # a key left out is not a null
    assert quality.summarize([{'values':said[0]},{'values':made_up[0]}])['absent_accuracy']==50
    assert 'absent_accuracy' not in quality.summarize([{'values':{'correct':1,'checks':1,'passed':True}}])
    assert not quality.clear({'expected':{'units':83},'distractors':[83]}) and quality.clear({'expected':{'units':83,'email':None},'distractors':[84,'83']})


def test_full_ifeval_is_pinned_and_all_checkers_run_deterministically():
    cases=ifeval.cases(8192)
    assert len(cases)==541
    for case in cases:
        assert case['request']['question']==case['expected']['prompt']
        first=ifeval.grade(case,'A short response.')
        assert first==ifeval.grade(case,'A short response.')
        assert len(first[1])==len(case['expected']['instruction_id_list'])
    one={'expected':{'key':123,'prompt':'Write exactly three bullet points.',
         'instruction_id_list':['detectable_format:number_bullet_lists'],'kwargs':[{'num_bullets':3}]}}
    assert ifeval.grade(one,'* first\n* second\n* third')[0]['passed']
    assert not ifeval.grade(one,'* first\n* second')[0]['passed']


def test_ifeval_rejects_dependency_drift_before_inference(monkeypatch):
    """A frozen checker contract cannot silently run with different dependency versions."""
    monkeypatch.setattr('importlib.metadata.version', lambda name: 'unexpected-version')
    with pytest.raises(ValueError, match='IFEval requires nltk==3.10.3'):
        ifeval.prepare()


def test_quality_summary_preserves_zero_and_distinguishes_unavailable_cases():
    samples=[{'values':{'correct':0,'checks':3,'passed':False,'success':True,'input_tokens':1024}},
             {'values':{'correct':0,'checks':3,'passed':False,'success':False}}]
    summary=quality.summarize(samples)
    assert summary['accuracy']==0 and summary['available_cases']==1 and summary['cases']==2


def test_quality_plan_and_evidence_survive_restart_and_rescoring(tmp_path):
    path=str(tmp_path/'quality.db');s=Benchmarks(path)
    n=s.create('owner','m','extraction')
    s.activate(n,{'fingerprint':'fp','subject':{'values':{}},'environment':{}})
    plan={'cases':extraction.cases(4096),'signature':'fixed'}
    s.plan(n,plan)
    s.plan(n,{'cases':[]})
    sample={'success':True,'passed':True,'correct':8,'checks':8,'input_tokens':512}
    evidence={'case':plan['cases'][0],'checks':[{'passed':True}],'prompt':'kept exact prompt'}
    s.keep(n,'a',sample,'full output','log',evidence)
    s.finish(n,'completed');s.db.close()
    s=Benchmarks(path)
    assert s.plan(n)==plan
    before=s.samples(n)
    s.configure('extraction',True,'case_accuracy * 2')
    assert s.get(n)['score']==200
    assert s.samples(n)==before and before[0]['evidence']==evidence
    s.db.close()


def test_saved_answers_are_scored_again_once_when_the_grader_has_moved_on(tmp_path):
    path=str(tmp_path/'regrade.db');s=Benchmarks(path)
    n=s.create('owner','m','extraction')
    s.activate(n,{'fingerprint':'fp','subject':{'values':{}},'environment':{}})
    case=extraction.cases(8192)[0]
    fenced='it thought a while</think>```json\n'+json.dumps(case['expected'])+'\n```'
    # As the first grader left it: a right answer in a fence, scored nought.
    s.keep(n,case['id'],{'success':True,'passed':False,'correct':0,'checks':8,'input_tokens':512,'generation_capped':False},fenced,'log',{'case':case,'answer':'','checks':[],'prompt':'kept'})
    s.keep(n,'no-evidence',{'success':True,'context_tokens':1},'out','log')
    s.finish(n,'completed')
    old=json.loads(s.db.execute('SELECT subject FROM benchmark_runs WHERE id=?',(n,)).fetchone()[0])
    old['protocol'].update(version=1,grader_version=1)
    with s.db:
        s.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?',(json.dumps(old),n))
    assert s.get(n)['score']==0 and s.get(n)['version']==1 and s.get(n)['latest_version']==extraction.PROTOCOL['version']
    s.db.close()
    s=Benchmarks(path)  # opening the database is when it is done
    run=s.get(n,True)
    scored=next(p for p in run['samples'] if p['id']==case['id'])
    assert run['metrics']['accuracy']==100 and scored['values']['passed'] and scored['values']['input_tokens']==512
    assert scored['output']==fenced and scored['evidence']['prompt']=='kept' and json.loads(scored['evidence']['answer'].strip('`json\n'))==case['expected']
    assert all(check['passed'] for check in scored['evidence']['checks']) and any('scored again' in m['text'] for m in run['messages'])
    assert run['version']==1 and run['latest_version']==extraction.PROTOCOL['version']  # it was still asked the old way: that is not mended by grading
    assert s.regrade()==0  # once
    s.db.close()


@pytest.mark.parametrize('inputs,outputs,expected_budget',[(1024,128,128),(1920,128,96)])
def test_prompted_probe_keeps_facts_and_exact_token_budget(fake_llama_server,fake_cards,inputs,outputs,expected_budget):
    models=bench_model()
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0]) as c:
        owner=add_user(c,'owner','admin')
        c.post('/benchmarks/control',headers=owner,json={'action':'pause'})
        n=start(c,owner,'extraction')
        spec={'question':'What is the named event?','blocks':['Name: Mira.','Event: Cedar Fair.'],'filler':' unrelated notes '}
        r=c.post('/loadcoach/tasks/'+n+'/probe',headers=owner,json={'context':2048,'inputs':inputs,'outputs':outputs,'evaluation':spec})
        assert r.status_code==200,r.text
        result=r.json()
        assert result['values']['success'] and result['values']['input_tokens']==inputs
        assert result['values']['output_budget']==expected_budget
        assert result['values']['stop_type']=='eos'
        text=result['evidence']['prompt']
        assert result['evidence']['thinking']=='as served'  # nothing here switches a model's thinking off
        assert len(text)==inputs  # fake tokenizer uses one Unicode character per token
        assert all(text.count(s)==1 for s in [*spec['blocks'],spec['question']])
        assert text.startswith('<user>') and text.endswith('<assistant>')
        assert 'BENCHMARK_GAP_' not in text
        c.post('/benchmarks/control',headers=owner,json={'action':'clear'})


@pytest.mark.parametrize('name',['json_schema','extraction','arithmetic','retrieval','code_retrieval','ifeval','abstention','tables','turns','reasoning','hierarchy'])
def test_each_quality_worker_finishes_batch_and_keeps_case_evidence(fake_llama_server,fake_cards,monkeypatch,name):
    module=MODULES[name]
    original=module.cases
    monkeypatch.setattr(module,'cases',lambda limit:original(limit)[:2])
    models=bench_model()
    if name in ('abstention','tables','turns','reasoning','hierarchy'):
        gguf('small.gguf',general__architecture='tiny',tiny__context_length=32768,general__parameter_count=123456)
        models['small']['flags']['ctx-size']=32768
    if name in ('turns','reasoning','hierarchy'):
        models['small']['flags']['benchmark-dialogue']=True
        if name=='reasoning':models['small']['flags']['benchmark-reasoning-count']='8'
        if name=='hierarchy':gguf('small.gguf',general__architecture='tiny',tiny__context_length=32768,general__parameter_count=123456,tokenizer__chat_template='messages tools')
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0]) as c:
        owner=add_user(c,'owner','admin')
        n=start(c,owner,name)
        run=wait(c,owner,n,lambda r:r['state'] in ('completed','failed'),seconds=30)
        assert run['state']=='completed',run
        assert len(run['samples'])==2 and len(run['plan']['cases'])==2
        assert run['metrics']['cases']==2 and run['score'] is not None
        if name in ('json_schema','extraction','retrieval','tables','turns','reasoning'):
            protocol=run['subject']['protocol']
            assert protocol['temperature']==protocol['sampling']['temperature']==1.0
            assert protocol['generation_seed']==42
            assert all(s['evidence']['request_sampling']==protocol['sampling'] for s in run['samples']
                       if s['values']['success'])
        for s in run['samples']:
            assert 'case' in s['evidence'] and 'checks' in s['evidence']
            assert s['values']['checks']>0
            if name in ('abstention','tables','turns','reasoning','hierarchy'):
                assert s['values']['success'] and s['values']['input_tokens']>0
            if name in ('turns','reasoning','hierarchy'):
                assert s['evidence']['native_dialogue'] and s['evidence']['native_usage']['completion_tokens']==16
                if name=='reasoning':assert s['values']['reasoning_tokens']==8
        assert c.get('/v1/batches/'+n,headers=owner).json()['status']=='completed'


def test_size_breakdown_does_not_hide_long_input_failures():
    rows=[{'values':{'target_input_tokens':s,'category':lang,'checks':2,'correct':correct,
                     'passed':correct==2,'success':True,'input_tokens':s}}
          for s,lang,correct in [(1024,'python',2),(8192,'python',0),(8192,'javascript',1)]]
    groups=quality.breakdown(rows)
    assert len(groups)==3
    assert {(g['input_tokens'],g['category']):g['accuracy'] for g in groups}=={
        (1024,'python'):100,(8192,'python'):0,(8192,'javascript'):50}


def test_changed_implementation_rejects_queued_and_resumed_plans(tmp_path):
    from types import SimpleNamespace
    s=Benchmarks(str(tmp_path/'protocol.db'));n=s.create('owner','m','extraction')
    s.activate(n,{'fingerprint':'fp','subject':{'values':{},'settings':{'flags':{'ctx-size':2048}}},'environment':{}})
    run=s.execution(n);worker=SimpleNamespace(store=s)
    protocol={**extraction.PROTOCOL,'implementation_sha256':'changed'}
    with pytest.raises(ValueError,match='changed after submission'):
        asyncio.run(quality.measure(worker,run,2048,protocol,extraction.cases,quality.grade_fields))
    s.plan(n,{'signature':'old','cases':[]})
    with pytest.raises(ValueError,match='protocol changed'):
        asyncio.run(quality.measure(worker,run,2048,extraction.PROTOCOL,extraction.cases,quality.grade_fields))
    s.db.close()


@pytest.mark.parametrize('name,count',[('arithmetic',40),('abstention',20),('tables',40),('turns',48),('reasoning',48),('hierarchy',56)])
def test_quality_preemption_resume_and_answer_keys_stay_local(fake_llama_server,fake_cards,tmp_path,monkeypatch,name,count):
    from benchmarks.worker import BenchmarkWorker
    requests=[]
    request=BenchmarkWorker.request
    async def observe(self,number,method='GET',suffix='',**kw):
        if suffix=='/probe':
            body=kw['json'];requests.append(body)
            allowed=({'messages','tools'} if name=='hierarchy' else {'messages'}) if name in ('turns','reasoning','hierarchy') else {'question','blocks','filler'}
            if name in ('tables','turns','reasoning'):
                allowed |= {'temperature','sampling'}
            assert set(body['evaluation']) <= allowed
            assert 'expected' not in body and 'schema' not in body
        return await request(self,number,method,suffix,**kw)
    monkeypatch.setattr(BenchmarkWorker,'request',observe)
    database=str(tmp_path/'resume.db');models=bench_model(.2)
    if name in ('abstention','tables','turns','reasoning','hierarchy'):
        gguf('small.gguf',general__architecture='tiny',tiny__context_length=32768,general__parameter_count=123456)
        models['small']['flags']['ctx-size']=32768
    if name in ('turns','reasoning','hierarchy'):
        models['small']['flags']['benchmark-dialogue']=True
        if name=='reasoning':models['small']['flags']['benchmark-reasoning-count']='8'
        if name=='hierarchy':gguf('small.gguf',general__architecture='tiny',tiny__context_length=32768,general__parameter_count=123456,tokenizer__chat_template='messages tools')
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as c:
        owner=add_user(c,'owner','admin');n=start(c,owner,name)
        first=wait(c,owner,n,lambda r:r['samples_count']>=1)['samples'][0]
        response=c.post('/v1/chat/completions',headers=owner,json={'model':'small','messages':[{'role':'user','content':'hi'}]})
        assert response.status_code==200
        c.post('/benchmarks/control',headers=owner,json={'run':n,'action':'pause'})
        paused=wait(c,owner,n,lambda r:r['state']=='paused')
        assert paused['samples'][0]==first
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as c:
        kept=c.get('/benchmarks/'+n,headers=owner).json()
        assert kept['state']=='paused' and kept['plan']==paused['plan'] and kept['samples'][0]==first
        c.post('/benchmarks/control',headers=owner,json={'run':n,'action':'resume'})
        done=wait(c,owner,n,lambda r:r['state'] in ('completed','failed'),seconds=45)
        assert done['state']=='completed',done['reason']
        assert len(done['samples'])==count and done['samples'][0]==first
        if name in ('abstention','tables','turns','reasoning','hierarchy'):
            assert all(s['values']['success'] for s in done['samples'])
        assert len({p['id'] for p in done['samples']})==count
    assert requests


def test_native_connection_loss_retries_incomplete_quality_case(fake_llama_server,fake_cards,monkeypatch):
    import httpx
    from loadcoach.probes import Probes
    original=Probes.completion
    calls=[]
    async def disconnected_once(self,*args,**kwargs):
        calls.append(True)
        if len(calls)==1:
            raise httpx.RemoteProtocolError('Native child stopped during service restart')
        return await original(self,*args,**kwargs)
    monkeypatch.setattr(Probes,'completion',disconnected_once)
    original_cases=arithmetic.cases
    monkeypatch.setattr(arithmetic,'cases',lambda limit:original_cases(limit)[:2])
    with gateway(fake_llama_server,models=bench_model(),nvidia_smi=fake_cards[0]) as c:
        owner=add_user(c,'owner','admin');n=start(c,owner,'arithmetic')
        done=wait(c,owner,n,lambda r:r['state'] in ('completed','failed'))
        assert done['state']=='completed',done['reason']
        assert len(done['samples'])==2 and len(calls)==3
        assert any('Model connection interrupted' in m['text'] for m in done['messages'])


@pytest.mark.parametrize('content', [' {"a":1} ', '{"literal":"<think>"}', None, '', ['not text']])
def test_native_final_content_is_not_reparsed_as_raw_reasoning(content):
    evidence = {'native_dialogue': True, 'prompt': 'assistant\n<think>\n',
                'native_message': {'role': 'assistant', 'content': content,
                                   'reasoning_content': '{"a":9}'}}
    assert quality.evaluation_answer('{"a":9}', evidence) == (content.strip() if isinstance(content, str) else '')
    assert quality.evaluation_answer('{"a":9}', {'prompt': evidence['prompt']}) == ''
    assert quality.evaluation_answer('<think>notes</think>{"a":1}', {}) == '{"a":1}'
    assert quality.evaluation_answer('{"a":9}', {'native_dialogue': True}) == ''


def test_native_worker_and_regrade_preserve_parsed_final_answer(tmp_path):
    from benchmarks.worker import BenchmarkWorker
    path = str(tmp_path/'native-regrade.db')
    store = Benchmarks(path)
    number = store.create('owner', 'm', 'turns')
    store.activate(number, {'fingerprint': 'fp', 'subject': {'values': {}}, 'environment': {}})
    case = turns.cases(8192)[0]
    answer = json.dumps(case['expected'])
    evidence = {'native_dialogue': True, 'prompt': 'assistant\n<think>\n',
                'native_message': {'role': 'assistant', 'content': answer, 'reasoning_content': 'notes'}}
    worker = BenchmarkWorker(store, None)
    async def check(_): return store.get(number)
    async def isolated(*_): return {'output': answer, 'log': 'kept', 'values': {'success': True}, 'evidence': evidence}
    worker.check, worker.isolated = check, isolated
    asyncio.run(worker.evaluate(number, case, turns.grade))
    point = store.samples(number, True)[0]
    assert point['values']['passed'] and point['evidence']['answer'] == answer
    store.finish(number, 'completed')
    # Reproduce grader 2's discarded native answer; opening repairs retained data once.
    subject = json.loads(store.db.execute('SELECT subject FROM benchmark_runs WHERE id=?', (number,)).fetchone()[0])
    subject['protocol']['grader_version'] = 2
    old_evidence = {**point['evidence'], 'answer': '', 'checks': []}
    with store.db:
        store.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?', (json.dumps(subject), number))
        store.db.execute('UPDATE benchmark_samples SET measurements=? WHERE run=?',
                         (json.dumps({**point['values'], 'correct': 0, 'passed': False}), number))
        store.db.execute('UPDATE benchmark_evidence SET evidence=? WHERE run=?', (json.dumps(old_evidence), number))
    store.db.close()
    store = Benchmarks(path)
    repaired = store.samples(number, True)[0]
    assert repaired['values']['passed'] and repaired['evidence']['answer'] == answer
    assert repaired['output'] == answer and repaired['log'] == 'kept'
    assert repaired['evidence']['native_message'] == evidence['native_message']
    assert store.get(number)['version'] == turns.PROTOCOL['version']
    assert store.regrade() == 0
    store.db.close()


def run_two_cases(fake_llama_server, fake_cards, monkeypatch, server):
    """Run a quality benchmark of two cases; say how many model servers were started and what the samples report."""
    from loadcoach.runner import Runner
    started=[];original=tables.cases
    monkeypatch.setattr(tables,'cases',lambda limit:original(limit)[:2])
    real=Runner.start
    async def counted(self,srv):
        started.append(srv.model);return await real(self,srv)
    monkeypatch.setattr(Runner,'start',counted)
    models=bench_model()
    gguf('small.gguf',general__architecture='tiny',tiny__context_length=32768,general__parameter_count=123456)
    models['small']['flags']['ctx-size']=32768
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0]) as c:
        owner=add_user(c,'owner','admin')
        if server:
            assert c.post('/benchmarks/definitions',headers=owner,json={'benchmark':'tables','enabled':True,'formula':'accuracy','server':server}).status_code==200
        n=start(c,owner,'tables')
        run=wait(c,owner,n,lambda r:r['state'] in ('completed','failed'),seconds=60)
        assert run['state']=='completed',run
        protocol=run['subject']['protocol']
        assert protocol['temperature']==protocol['sampling']['temperature']==1.0
        assert protocol['generation_seed']==42
        assert all(sample['evidence']['request_sampling']==protocol['sampling'] for sample in run['samples'])
        reused=[s['values'].get('server_reused',False) for s in run['samples']]
        time.sleep(.5)
        return len(started),reused,c.get('/loadcoach/status',headers=owner).json()


def test_a_quality_benchmark_keeps_one_model_server_for_its_cases_by_default(fake_llama_server,fake_cards,monkeypatch):
    count,reused,status=run_two_cases(fake_llama_server,fake_cards,monkeypatch,None)
    assert count==1 and sorted(reused)==[False,True]
    assert status['running']==[]  # the server and the GPU lock are let go when the run ends


def test_a_quality_benchmark_can_be_set_to_load_a_fresh_server_for_every_case(fake_llama_server,fake_cards,monkeypatch):
    count,reused,_=run_two_cases(fake_llama_server,fake_cards,monkeypatch,'fresh')
    assert count==2 and reused==[False,False]


def test_only_a_quality_benchmark_may_reuse_a_server(tmp_path):
    store=Benchmarks(str(tmp_path/'b.db'))
    by={b['id']:b['server'] for b in store.definitions()}
    assert by['tables']=='reuse' and by['context']=='fresh' and by['load']=='fresh'
    with pytest.raises(ValueError,match='quality'):store.configure('context',True,'max_context_tokens',None,'reuse')
    with pytest.raises(ValueError,match='quality'):store.configure('tables',True,'accuracy',None,'sometimes')
    store.configure('tables',True,'accuracy',None,'fresh')
    assert {b['id']:b['server'] for b in store.definitions()}['tables']=='fresh'
    store.configure('tables',True,'accuracy')  # leaving it out keeps the choice
    assert {b['id']:b['server'] for b in store.definitions()}['tables']=='fresh'
