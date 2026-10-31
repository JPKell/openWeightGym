"""Free-score compatibility, matched-pair accounting and unsupported forced formats."""
import asyncio
import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from benchmarks import quality
from benchmarks.store import Benchmarks
from benchmarks.suites import json_schema
from loadcoach.probes import Probes
from benchmark_clients import gateway
from test_app import add_user,fake_cards,fake_llama_server,in_an_empty_folder
from test_benchmarks import start,wait
from test_conversation_probe import models
from test_weightroom import desk,sign_in

FREE_V2_SHA256='7f0a5659a66629c28059226e40b2b99a34cc32b56f5bac839909305fecd05de5'


def test_ledger_oracle_requires_scoped_committed_revisions_and_ordered_duplicates():
    cases=json_schema.free_cases(8192)
    assert cases[0]['expected']=={'name':'B-71-types','age':37,'active':False,'middle':None}
    assert cases[1]['expected']=={'labels':['blue','red','blue','green','amber'],'priority':'low','count':5575}
    assert cases[3]['expected']=={'title':'B-71-escaping "ready"','path':'C:\\archive\\B-71-escaping-R8.txt',
        'message':','.join('B-71-escaping-R'+str(i) for i in (1,2,3,5,6,7,8))+'\n5575'}
    assert cases[4]['expected']=={'id':'B-71-empty','readings':['B-71-empty-R8'],'operator':None,'errors':0}
    assert cases[5]['expected']['active'] is True
    # A valid-schema answer that took the uncommitted 99, included the voided R4,
    # or deduplicated labels must lose semantic checks; format success cannot hide it.
    for index,field,wrong in [(0,'age',129),(1,'labels',['blue','red','green','amber']),
                              (1,'count',6275),(3,'path','C:\\archive\\B-71-escaping-R2.txt')]:
        case=cases[index];value=copy.deepcopy(case['expected']);value[field]=wrong
        metrics,_=json_schema.grade(case,json.dumps(value))
        assert metrics['schema_valid'] and not metrics['passed']
    for case in cases:
        Draft202012Validator.check_schema(case['schema'])
        assert Draft202012Validator(case['schema']).is_valid(case['expected'])


def test_v6_free_questions_have_108_independent_semantic_checks_and_unique_sources():
    cases=json_schema.free_cases(65536)
    assert cases==json_schema.free_cases(65536)
    assert len(cases)==27
    batches=set();source_ids=set();semantic=0
    for case in cases:
        question=case['request']['question']
        batch=re.search(r'For batch (\S+) only',question).group(1)
        rows,_=json.JSONDecoder().raw_decode(question.split('Source rows:\n',1)[1])
        identifiers={row['id'] for row in rows}
        assert batch not in batches and not identifiers & source_ids
        assert all(row['id'].startswith(batch+'-R') for row in rows)
        batches.add(batch);source_ids.update(identifiers)
        metrics,_=json_schema.grade(case,json.dumps(case['expected'],ensure_ascii=False))
        assert metrics['passed'] and metrics['value_correct']==metrics['value_checks']
        semantic+=metrics['value_checks']
        if case['category'] in ('revision_audit','cost_audit','group_audit','rank_audit'):
            selected={}
            for row in rows:
                if row['batch']==batch and row['committed'] and row['revision']>selected.get(row['id'],{}).get('revision',0):
                    selected[row['id']]=row
            kept=[selected[key] for key in sorted(selected) if not selected[key]['void']]
            expected=case['expected']
            if case['category']=='revision_audit':
                assert expected['surviving_ids']==[row['id'] for row in kept]
                assert expected['survivor_count']==len(kept)
                assert expected['revised_id']==next(row['id'] for row in kept if row['revision']==2)
                assert expected['voided_id']==next(row['id'] for row in selected.values() if row['void'])
                assert expected['sum_units']==sum(row['units'] for row in kept)
            elif case['category']=='cost_audit':
                assert expected['total_cents']==sum(row['units']*row['price_cents'] for row in kept)
                assert expected['high_cents']==sum(row['units']*row['price_cents'] for row in kept if row['priority']=='high')
                assert expected['blue_cents']==sum(row['units']*row['price_cents'] for row in kept if row['label']=='blue')
                assert expected['largest_id']==sorted(kept,key=lambda row:(-row['units']*row['price_cents'],row['id']))[0]['id']
                assert expected['smallest_id']==sorted(kept,key=lambda row:(row['units']*row['price_cents'],row['id']))[0]['id']
            elif case['category']=='group_audit':
                counts={label:sum(row['label']==label for row in kept) for label in ('red','blue','green','amber')}
                assert all(expected[label+'_count']==count for label,count in counts.items())
                assert expected['most_common_label']==sorted(counts,key=lambda label:(-counts[label],label))[0]
            else:
                ranked=sorted(kept,key=lambda row:(-row['units'],row['id']))
                assert expected['top_unit_id']==ranked[0]['id'] and expected['second_unit_id']==ranked[1]['id']
                assert expected['bottom_price_id']==sorted(kept,key=lambda row:(row['price_cents'],row['id']))[0]['id']
                assert expected['high_count']==sum(row['priority']=='high' for row in kept)
                assert expected['units_at_least_6_count']==sum(row['units']>=6 for row in kept)
    assert semantic==108
    assert sum(json_schema.grade(case,json.dumps(case['expected']))[0]['checks'] for case in cases)==162

    def first_row_shortcut(value,first):
        if value is None:return None
        if type(value) is bool:return first['priority']=='high'
        if type(value) is int:return first['units']
        if isinstance(value,str):return first['id']
        if isinstance(value,list):return [first['id']]
        return {key:first_row_shortcut(item,first) for key,item in value.items()}
    empty=first_row=0
    for case in cases:
        rows,_=json.JSONDecoder().raw_decode(case['request']['question'].split('Source rows:\n',1)[1])
        empty+=json_schema.grade(case,'{}')[0]['value_correct']
        answer=first_row_shortcut(case['expected'],rows[0])
        first_row+=json_schema.grade(case,json.dumps(answer))[0]['value_correct']
    assert empty/semantic<.5 and first_row/semantic<.5


def sample(case,answer=None,success=True,**extra):
    metrics,_=json_schema.grade(case,json.dumps(case['expected']) if answer is None else answer)
    return dict(id=case['id'],values={**metrics,'category':case['category'],'success':success,**extra})


def test_legacy_requests_are_byte_stable_and_new_forced_samples_use_their_own_schema():
    cases=json_schema.cases(8192);free,forced=cases[:27],cases[27:]
    assert len(cases)==54 and len({c['id'] for c in cases})==54
    canonical=[{k:c[k] for k in ('id','request','schema','expected')} for c in json_schema.legacy_free_cases(8192)]
    assert hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()==FREE_V2_SHA256
    assert json_schema.PROTOCOL['version']==6 and json_schema.PROTOCOL['dataset']=='json-schema-paired-v5'
    assert json_schema.PROTOCOL['free_dataset']=='json-schema-ledger-v3'
    assert json_schema.PROTOCOL['cases']==54 and json_schema.PROTOCOL['pairs']==27
    assert json_schema.PROTOCOL['temperature']==1.0 and json_schema.PROTOCOL['generation_seed']==42
    assert json_schema.PROTOCOL['sampling']=={'temperature':1.0,**json_schema.SAMPLING}
    assert json_schema.PROTOCOL['outputs']==32768
    assert all(c['outputs']==4096 for c in cases)
    assert all(c['outputs']==32768 for c in json_schema.cases(65536))
    frozen=[{'id':c['id'],'question':c['request'].get('question',c['request'].get('messages',[{}])[0].get('content')),
             'schema':c['schema'],'expected':c['expected']} for c in cases]
    assert hashlib.sha256(json.dumps(frozen,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()==\
        'c59590b13fd3ea04a821640ffcf6df20b931907784b125461f0792bdc1377471'
    for before,after in zip(free,forced):
        assert before['decoding']=='free' and after['decoding']=='forced'
        assert before['pair']==after['pair']==before['id'] and after['id']==before['id']+'-forced'
        assert before['expected']==after['expected'] and before['schema']==after['schema']
        assert after['request']['messages']==[{'role':'user','content':before['request']['question']}]
        assert before['request']['temperature']==after['request']['temperature']==1.0
        assert before['request']['sampling']==after['request']['sampling']==json_schema.SAMPLING
        assert after['request']['response_format']['json_schema']['schema']==before['schema']
        assert after['request']['response_format']['json_schema']['strict'] is True
        Probes.validate_dialogue(after['request'])
    assert json_schema.summarize([])=={}


def test_paired_json_submission_freezes_matching_sampler_and_generation_seed(tmp_path):
    store=Benchmarks(str(tmp_path/'benchmarks.db'))
    try:
        number=store.create('owner','gpt-oss-20b-q4','json_schema')
        store.activate(number,{'subject':{'settings':{'flags':{'ctx-size':65536}}},
                               'fingerprint':'model-fingerprint','environment':{}})
        protocol=store.execution(number)['subject']['protocol']
        assert protocol['seed']==42 and protocol['generation_seed']==42
        for case in json_schema.cases(65536):
            assert protocol['temperature']==protocol['sampling']['temperature']==case['request']['temperature']==1.0
            assert protocol['sampling']=={'temperature':1.0,**case['request']['sampling']}
    finally:
        store.db.close()


def test_forced_answers_never_change_the_original_free_headline_or_case_counts():
    cases=json_schema.cases(8192)
    free=[sample(c) for c in cases[:27]]
    forced=[sample(c,answer='{}') for c in cases[27:]]
    original=json_schema.qualities(free)
    result=json_schema.summarize(free+forced)
    for key in ('accuracy','case_accuracy','cases','passed_cases','json_accuracy','schema_accuracy','value_accuracy'):
        assert result[key]==original[key]
    assert result['accuracy']==100 and result['cases']==27 and result['recorded_samples']==54
    assert result['forced_value_accuracy']==0 and result['forced_schema_accuracy']==0
    assert result['matched_pairs']==27 and result['forced_value_delta']==-100
    perfect=json_schema.summarize(free+[sample(c) for c in cases[27:]])
    assert perfect['forced_accuracy']==perfect['forced_value_accuracy']==100 and perfect['forced_value_delta']==0


def test_pair_delta_uses_the_same_executed_cases_and_rates_use_total_request_time():
    cases=json_schema.cases(8192)
    free=[sample(cases[0],output_tokens=10,request_seconds=1),
          sample(cases[1],answer='',success=False,output_tokens=0,request_seconds=50)]
    forced=[sample(cases[27],answer='{}',output_tokens=15,request_seconds=3),
            sample(cases[28],output_tokens=20,request_seconds=2)]
    result=json_schema.summarize(free+forced)
    assert result['matched_pairs']==1
    assert result['paired_free_value_accuracy']==100 and result['paired_forced_value_accuracy']==0
    assert result['forced_value_delta']==-100 and result['forced_value_accuracy']==pytest.approx(300/7)
    assert result['free_output_tokens_per_second']==10 and result['forced_output_tokens_per_second']==7
    bad=[sample(cases[2],output_tokens=99,request_seconds=float('inf')),
         sample(cases[3],output_tokens=99,request_seconds=True)]
    assert 'free_output_tokens_per_second' not in json_schema.summarize(bad)


def test_unsupported_and_context_unavailable_forced_samples_have_no_forced_correctness():
    cases=json_schema.cases(8192)
    good=sample(cases[0])
    rejected=sample(cases[27],answer='',success=False,unsupported=True,unavailable=True)
    result=json_schema.summarize([good,rejected])
    assert result['accuracy']==100 and result['unsupported_cases']==1 and result['forced_available_cases']==0
    assert result['matched_pairs']==0 and 'forced_accuracy' not in result and 'forced_value_delta' not in result
    group=json_schema.breakdown([rejected])[0]
    assert 'accuracy' not in group and group['unsupported_cases']==1 and group['cases']==1
    context=sample(cases[27],answer='',success=False,unavailable=True,unsupported=False)
    result=json_schema.summarize([good,context])
    assert result['forced_unavailable_cases']==1 and result['unsupported_cases']==0 and 'forced_accuracy' not in result


def test_saved_v2_free_results_keep_their_score_and_do_not_gain_forced_measurements(tmp_path):
    store=Benchmarks(str(tmp_path/'v2.db'));number=store.create('owner','m','json_schema')
    store.activate(number,{'fingerprint':'old','subject':{'values':{}},'environment':{}})
    for case in json_schema.legacy_free_cases(8192):
        row=sample(case);values=row['values'];values.pop('decoding');values.pop('pair')
        store.keep(number,case['id'],values,json.dumps(case['expected']),'',{'case':case,'answer':json.dumps(case['expected'])})
    store.finish(number,'completed')
    old=json.loads(store.db.execute('SELECT subject FROM benchmark_runs WHERE id=?',(number,)).fetchone()[0])
    old['protocol']={**quality.PROTOCOL,'version':2,'cases':15,'dataset':'json-schema-v1',
                     'jsonschema_version':'4.26.0','implementation_sha256':'0'*64}
    with store.db:
        store.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?',(json.dumps(old),number))
    store.db.close();store=Benchmarks(str(tmp_path/'v2.db'))
    kept=store.get(number,True)
    assert (kept['score'],kept['version'],kept['latest_version'])==(100,2,6)
    assert len(kept['samples'])==15 and kept['metrics']['forced_cases']==0
    assert 'forced_accuracy' not in kept['metrics'] and store.regrade()==0
    store.db.close()


def test_an_old_incomplete_plan_cannot_mix_in_new_forced_samples(tmp_path):
    store=Benchmarks(str(tmp_path/'old-plan.db'));number=store.create('owner','m','json_schema')
    store.activate(number,{'fingerprint':'old','subject':{'settings':{'flags':{'ctx-size':8192}}},'environment':{}})
    subject=json.loads(store.db.execute('SELECT subject FROM benchmark_runs WHERE id=?',(number,)).fetchone()[0])
    subject['protocol']={**quality.PROTOCOL,'version':2,'cases':15,'dataset':'json-schema-v1',
                         'jsonschema_version':'4.26.0','implementation_sha256':'0'*64}
    with store.db:
        store.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?',(json.dumps(subject),number))
    oldplan={'cases':json_schema.free_cases(8192),'protocol':subject['protocol'],'signature':'old-signature'}
    store.plan(number,oldplan)
    with pytest.raises(ValueError,match='changed after submission'):
        asyncio.run(json_schema.measure(SimpleNamespace(store=store),store.execution(number),8192))
    assert store.plan(number)==oldplan
    store.db.close()


@pytest.mark.parametrize('rejected',[False,True])
def test_durable_worker_finishes_free_and_forced_or_records_unsupported(fake_llama_server,fake_cards,rejected):
    flags={'benchmark-dialogue-reject':True} if rejected else {}
    with gateway(fake_llama_server,models=models(**flags),nvidia_smi=fake_cards[0]) as client:
        owner=add_user(client,'owner','admin');number=start(client,owner,'json_schema')
        run=wait(client,owner,number,lambda r:r['state'] in ('completed','failed'),seconds=45)
        assert run['state']=='completed',run['reason']
        assert len(run['samples'])==len(run['plan']['cases'])==54
        assert run['metrics']['cases']==27 and run['metrics']['forced_cases']==27
        forced=[s for s in run['samples'] if s['values'].get('decoding')=='forced']
        assert len(forced)==27 and all(s['evidence']['native_dialogue'] for s in forced)
        assert all(s['evidence']['response_format']['type']=='json_schema' for s in forced)
        free=[s for s in run['samples'] if s['values'].get('decoding')=='free']
        assert all(s['values']['success'] for s in free)
        profile=run['subject']['protocol']['sampling']
        assert all(s['evidence']['request_sampling']==profile for s in free)
        assert run['metrics']['accuracy']==json_schema.qualities(free)['accuracy']
        if rejected:
            assert run['metrics']['unsupported_cases']==27 and run['metrics']['forced_available_cases']==0
            assert all(s['values']['unsupported'] and not s['values']['success'] for s in forced)
            assert 'forced_accuracy' not in run['metrics'] and run['metrics']['matched_pairs']==0
        else:
            assert run['metrics']['forced_available_cases']==27 and run['metrics']['matched_pairs']==27
            assert all(s['values']['success'] for s in forced)
            assert all(s['evidence']['request_sampling']==profile for s in forced)
        assert client.get('/loadcoach/status',headers=owner).json()['loaded']==[]


def test_paired_worker_resumes_after_forced_samples_begin(fake_llama_server,fake_cards,tmp_path):
    configured=models(**{'benchmark-delay':.05});database=str(tmp_path/'resume-pairs.db')
    with gateway(fake_llama_server,models=configured,nvidia_smi=fake_cards[0],database=database) as client:
        owner=add_user(client,'owner','admin');number=start(client,owner,'json_schema')
        active=wait(client,owner,number,lambda r:any(s['values'].get('decoding')=='forced' for s in r['samples']))
        first=next(s for s in active['samples'] if s['values'].get('decoding')=='forced')
        free_score=active['metrics']['accuracy']
        client.post('/benchmarks/control',headers=owner,json={'run':number,'action':'pause'})
        paused=wait(client,owner,number,lambda r:r['state']=='paused')
    with gateway(fake_llama_server,models=configured,nvidia_smi=fake_cards[0],database=database) as client:
        assert client.get('/benchmarks/'+number,headers=owner).json()['plan']==paused['plan']
        client.post('/benchmarks/control',headers=owner,json={'run':number,'action':'resume'})
        done=wait(client,owner,number,lambda r:r['state'] in ('completed','failed'),seconds=45)
        assert done['state']=='completed',done['reason']
        assert len(done['samples'])==len({s['id'] for s in done['samples']})==54
        assert next(s for s in done['samples'] if s['id']==first['id'])==first
        assert done['metrics']['accuracy']==free_score and done['metrics']['matched_pairs']==27


def test_paired_result_kpis_and_unavailable_breakdown_render_on_desktop_and_mobile(desk,tmp_path):
    from loadcoach.models import Models
    from playwright.sync_api import sync_playwright
    server,_,_=desk
    library=Models(str(tmp_path/'loadcoach.db'))
    library.put('paired-model',{'flags':{'model':str(tmp_path/'paired.gguf')}})
    store=server.app.state.benchmarks.state.store
    number=store.create('jordan','paired-model','json_schema',library.identity('paired-model'))
    store.activate(number,{'fingerprint':'fixture','subject':{},'environment':{}})
    cases=json_schema.cases(8192)
    selected=[cases[0],cases[27],cases[28]]
    store.plan(number,{'cases':selected})
    for case in selected:
        rejected=case['id']==cases[28]['id']
        answer='' if rejected else '{}' if case['decoding']=='forced' else json.dumps(case['expected'])
        values,checks=json_schema.grade(case,answer)
        store.keep(number,case['id'],{**values,'category':case['category'],'success':not rejected,'unsupported':rejected,
                   **(dict(output_tokens=10,request_seconds=1) if not rejected else {})},answer,'',dict(case=case,answer=answer,checks=checks,prompt='rendered'))
    store.finish(number,'completed');store.synced(number)
    sign_in(server,'jordan')
    assert store.get(number)['score']==100 and store.get(number)['metrics']['forced_value_delta']==-100
    with sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000})
        page=context.new_page();errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def serve(route):
            from front_helpers import serve_browser_request
            serve_browser_request(server,'http://paired.test',route)
        context.route('http://paired.test/**',serve)
        page.goto('http://paired.test/benchmarks/manage?benchmark=json_schema')
        assert 'matched executed pairs' in page.locator('#benchmark-definitions').inner_text()
        page.goto('http://paired.test/benchmarks/results?benchmark=json_schema')
        assert 'Free Correct fields' in page.locator('#benchmark-results thead').inner_text()
        assert 'Matched forced minus free values' in page.locator('#benchmark-results thead').inner_text()
        page.goto('http://paired.test/benchmarks/run/'+number)
        assert 'types · free' in page.locator('main').inner_text() and 'types · forced' in page.locator('main').inner_text()
        assert 'array · forced' in page.locator('main').inner_text() and 'Unsupported' in page.locator('main').inner_text()
        rejected_row=page.locator('tbody tr').filter(has_text='array-1-forced')
        assert rejected_row.locator('td').nth(4).inner_text()=='—'
        folder=Path(__file__).resolve().parents[1]/'data/m103-verification';folder.mkdir(parents=True,exist_ok=True)
        for width in (1440,390,320):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(folder/f'paired-{width}.png'),full_page=True)
        assert not errors
        browser.close()
