"""Independent archive oracles, adversarial answerers and retained-version boundaries."""
import asyncio
import copy
import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from benchmarks import quality
from benchmarks.store import Benchmarks
from benchmarks.suites import extraction, retrieval
from loadcoach.probes import Probes
from loadcoach.tasks import Tasks
from starlette.requests import Request


@pytest.fixture(scope='module')
def cases():
    return retrieval.cases(131072)


@pytest.mark.parametrize('module,limit,expected',[
    (extraction,32768,'723fa779ccc3586fe6071af6722e33b073f49bd8c6dedeef9ae2dea25261c224'),
    (extraction,131072,'398a38469045a1949b9c6c923d9d6a6db9416ff5e451606d1d1d2cbcdb0858f2'),
    (retrieval,32768,'2bc22e6a1537e5a60d43074d318f97225a1664cfea58e85c4835abb8834157a4'),
    (retrieval,131072,'38b3bf551d10f12d19ce4a60f32c07703198ff9623e317df368728770cb3bf26'),
])
def test_sampler_revision_preserves_every_original_case_and_input_size(module,limit,expected):
    original=copy.deepcopy(module.cases(limit))
    for case in original:
        assert case['request'].pop('temperature')==1.0
        assert case['request'].pop('sampling')==module.SAMPLING
    encoded=json.dumps(original,sort_keys=True,separators=(',',':')).encode()
    assert hashlib.sha256(encoded).hexdigest()==expected


@pytest.mark.parametrize('module,previous',[(extraction,3),(retrieval,4)])
def test_sampler_revision_freezes_agreeing_metadata_and_refuses_old_plan(module,previous,tmp_path):
    name=module.DEFINITION['id']
    store=Benchmarks(tmp_path/(name+'.db'))
    try:
        number=store.create('owner','model',name)
        store.activate(number,{'fingerprint':'fixture','environment':{},
                               'subject':{'settings':{'flags':{'ctx-size':131072}}}})
        protocol=store.execution(number)['subject']['protocol']
        case=module.cases(131072)[0]
        assert protocol['temperature']==protocol['sampling']['temperature']==case['request']['temperature']==1.0
        assert protocol['generation_seed']==42
        assert protocol['sampling']=={'temperature':1.0,**case['request']['sampling']}
        assert Probes.evaluation_sampling(case['request'])==module.SAMPLING
        legacy={**module.PROTOCOL,'version':previous}
        for key in ('temperature','sampling','generation_seed'):
            legacy.pop(key,None)
        store.plan(number,{'protocol':legacy,'signature':quality.signature(legacy),'cases':[case]})
        with pytest.raises(ValueError,match='protocol changed'):
            asyncio.run(module.measure(SimpleNamespace(store=store),store.get(number,True),131072))
        assert store.samples(number,True)==[]
    finally:
        store.db.close()


def oracle(request):
    """Read only displayed text; independently resolve scope, date precedence and joins."""
    scope = re.search(r'PRJ-\d+/\d{4}', request['question'])[0]
    records = []
    for block in request['blocks']:
        matches = list(re.finditer(r'PRJ-\d+(?:-X)?/\d{4}', block))
        for index, match in enumerate(matches):
            if match[0] != scope:
                continue
            end = matches[index+1].start() if index+1 < len(matches) else len(block)
            text = block[match.end():end]
            issued = re.search(r'issued (\d{4}-\d{2}-\d{2})', text)[1]
            records.append((issued, text))
    cancelled = {amendment for _, text in records
                 for amendment in re.findall(r'(A\d) is withdrawn', text)}
    ledger, transfers = {}, {}
    for _, text in sorted(records):
        amendment = re.search(r'amendment (A\d)', text)
        if amendment and amendment[1] in cancelled:
            continue
        for field, pattern in (
            ('director', r'Director ([A-Z][a-z]+ [A-Z][a-z]+)'),
            ('opening_date', r'Opens (\d{4}-\d{2}-\d{2})'),
            ('closing_date', r'closes (\d{4}-\d{2}-\d{2})'),
            ('closing_event', r'closes \S+ with ([A-Z][a-z]+ [A-Z][a-z]+)'),
            ('final_attendance', r'[Aa]ttendance (\d+)'),
            ('final_room', r'[Rr]oom ([A-Z][a-z]+ [A-Z][a-z]+)'),
            ('receipts', r'Receipts (\d+) cents'),
            ('exhibits', r'Inspected exhibits (\d+)'),
            ('earlier', r'Earlier responsibility holder: ([A-Z][a-z]+ [A-Z][a-z]+)'),
        ):
            found = re.search(pattern, text)
            if found:
                ledger[field] = found[1]
        transfer = re.search(r'([A-Z][a-z]+ [A-Z][a-z]+) handed responsibility to ([A-Z][a-z]+ [A-Z][a-z]+)', text)
        if transfer:
            transfers[transfer[1]] = transfer[2]
    opened = date.fromisoformat(ledger['opening_date']).toordinal()
    closed = date.fromisoformat(ledger['closing_date']).toordinal()
    quotient, remainder = divmod(int(ledger['receipts']), int(ledger['exhibits']))
    assert remainder == 0
    assert not any(re.search(r'(?i)sponsor|access code', text) for _, text in records)
    return dict(director=ledger['director'], opening_date=ledger['opening_date'],
                final_attendance=int(ledger['final_attendance']), final_room=ledger['final_room'],
                elapsed_days=closed-opened, cents_per_exhibit=quotient,
                successor=transfers[ledger['earlier']], closing_event=ledger['closing_event'],
                sponsor=None, access_code=None)


def test_fixed_keys_unique_entities_omissions_and_context_caps(cases):
    assert cases == retrieval.cases(131072)
    assert len(cases) == 20 and sum(len(case['expected']) for case in cases) == 200
    assert Counter(case['category'] for case in cases) == dict.fromkeys(retrieval.STORIES, 5)
    assert {case['inputs'] for case in cases} == set(retrieval.PROTOCOL['input_sizes'])
    entities = [entity for case in cases for entity in case['entities']]
    assert len(entities) == len(set(entities))
    for case in cases:
        assert oracle(case['request']) == case['expected']
        assert quality.clear(case)
        assert Counter(case['kinds'].values()) == dict(plain=3, corrected=2, combined=3, absent=2)
        metrics, checks = retrieval.grade(case, json.dumps(case['expected']))
        assert metrics['correct'] == metrics['checks'] == 10 and metrics['passed']
        assert metrics['absent'] == metrics['absent_correct'] == 2
        assert metrics['invented_fields'] == 0
        assert all(check['positions'] for check in checks if check['kind'] != 'absent')
        for key in case['expected']:
            wrong = {**case['expected'], key: True}
            assert retrieval.grade(case, json.dumps(wrong))[0]['correct'] == 9
        fixed = case['request']['question'] + ''.join(case['request']['blocks'])
        assert len(fixed) < 2800  # compact evidence; never truncate it to hit a native budget
    # Capping can add an intermediate size without changing the canonical records.
    smaller = retrieval.cases(16384)
    assert {case['inputs'] for case in smaller} == {1024, 4096, 8192}
    by_id = {case['id']:case for case in cases}
    for small in smaller:
        large = by_id[small['id']]
        assert small['request'] == large['request'] and small['expected'] == large['expected']
    tiny = retrieval.cases(2048)
    assert len(tiny) == 4 and all(case['inputs'] == case['outputs'] == 1024 for case in tiny)
    capped = retrieval.cases(6000)
    assert {case['inputs'] for case in capped} == {1024, 3000}
    assert len({entity for case in capped for entity in case['entities']}) == 8*8
    assert retrieval.PROTOCOL['version'] == retrieval.DEFINITION['version'] == 5
    assert retrieval.PROTOCOL['dataset'] == 'retrieval-v3'
    assert retrieval.PROTOCOL['grader_version'] == quality.PROTOCOL['grader_version'] == 3


def test_archive_variety_near_misses_and_long_appendices(cases):
    for case in cases:
        filler, source = case['request']['filler'], case['source']
        entries = filler.split('--- archive ')[1:]
        assert len(entries) == 300 and len(set(entries)) == 300
        assert f'{source["project"]}/{source["year"]-1}' in filler
        assert f'{source["predecessor"]} served as' in filler
        assert 'Access code ARCH-' in filler and 'sponsor Trust-' in filler
        assert len(entries[60].split()) >= 15000
        # Padding carries wrong-role and wrong-year cues, never the missing target facts.
        assert f'{source["project"]}/{source["year"]}:' not in filler
        assert not any(str(value) in filler for key, value in case['expected'].items()
                       if key in ('director', 'final_room', 'successor', 'closing_event'))


def first_shapes(case):
    """Copy the first value of each shape, without resolving scope, joins or revisions."""
    text = '\n'.join(case['request']['blocks']) + case['request']['filler']
    name = re.search(r'\b(?:'+'|'.join(quality.FIRST)+r') (?:'+'|'.join(quality.LAST)+r')\b', text)[0]
    place = re.search(r'\b(?:'+'|'.join(quality.PLACE_A)+r') (?:'+'|'.join(quality.PLACE_B)+r')\b', text)[0]
    return dict(director=name, successor=name, final_room=place, closing_event=place,
                opening_date=re.search(r'\d{4}-\d{2}-\d{2}', text)[0],
                final_attendance=int(re.search(r'(?i)attendance (\d+)', text)[1]),
                elapsed_days=int(re.search(r'\d+', text)[0]),
                cents_per_exhibit=int(re.search(r'(\d+) cents', text)[1]),
                sponsor=re.search(r'sponsor (\S+);', text)[1],
                access_code=re.search(r'Access code (\S+);', text)[1])


def test_shortcuts_cannot_pass_and_amendment_traps_change_answers(cases):
    scores = []
    for answerer in (lambda case:dict.fromkeys(case['expected']), first_shapes):
        samples = [{'values':{'success':True, **retrieval.grade(case,json.dumps(answerer(case)))[0]}}
                   for case in cases]
        score = retrieval.summarize(samples)
        assert score['accuracy'] < 50 and score['passed_cases'] == 0
        scores.append(score['accuracy'])
    assert scores[0] == 20
    assert sum(case['source']['withdrawn'] for case in cases) == 10
    for case in cases:
        source = case['source']
        for wrong in (source['attendance'][0], source['attendance'][4]):
            assert wrong != case['expected']['final_attendance']
        if source['withdrawn']:
            assert source['attendance'][3] != case['expected']['final_attendance']


@pytest.mark.parametrize('value,invented,credit', [(None,0,True), ('unknown',1,False), ('null',1,False),
                                                (False,1,False), (0,1,False), ({},1,False)])
def test_invention_counts_non_null_values_without_rewarding_missing_answers(cases,value,invented,credit):
    case = cases[0]
    answer = {**case['expected'], 'sponsor':value}
    metrics, _ = retrieval.grade(case,json.dumps(answer))
    assert metrics['invented_fields'] == invented
    assert metrics['absent_correct'] == 1+credit
    missing = {key:value for key,value in case['expected'].items() if key != 'sponsor'}
    metrics, _ = retrieval.grade(case,json.dumps(missing))
    assert metrics['invented_fields'] == 0 and metrics['correct'] == 9 and metrics['absent_correct'] == 1
    assert retrieval.grade(case,'')[0]['correct'] == 0


def test_position_splits_are_micro_averages_and_visible_in_the_detail_page(cases):
    samples = []
    for index, case in enumerate(cases):
        answer = case['expected'] if index % 2 else {**case['expected'],'sponsor':'invented','director':'wrong'}
        metrics, checks = retrieval.grade(case,json.dumps(answer))
        samples.append(dict(id=case['id'], values=dict(success=True,target_input_tokens=case['inputs'],
                            category=case['category'],**metrics), evidence=dict(checks=checks,case=case)))
    summary = retrieval.summarize(samples)
    assert summary['accuracy'] == 90 and summary['invention'] == 25
    assert summary['invented_fields'] == 10 and summary['absent_fields'] == 40
    for place in retrieval.REGIONS:
        checks = [check for sample in samples for check in sample['evidence']['checks'] if place in check['positions']]
        assert summary[f'{place}_accuracy'] == 100*sum(check['passed'] for check in checks)/len(checks)
        assert all(check['kind'] != 'absent' for check in checks)
    detail = dict(subject={'protocol':retrieval.PROTOCOL}, breakdown=retrieval.breakdown(samples),
                  environment={}, version=3, latest_version=3, created='', samples_count=20,
                  score=90,formula='accuracy',samples=[],messages=[],plan=None)
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1]/'weightroom/pages'),
                      autoescape=select_autoescape())
    env.filters['clock'] = str
    page = env.get_template('benchmark_detail.html').render(detail=detail)
    assert 'Invented / absent' in page and '<th>Start</th>' in page and '<th>Middle</th>' in page and '<th>End</th>' in page
    assert '1 / 2 (50.0%)' in page and 'Combined fields may count in several positions' in page
    detail['subject']['protocol'] = {'dataset':'retrieval-v1'}
    assert 'Invented / absent' not in env.get_template('benchmark_detail.html').render(detail=detail)


def test_native_padding_keeps_every_fixed_record_and_reports_positions(cases):
    # A CPU-only character tokenizer exercises the actual native prompt assembly code.
    calls = []
    async def server(request):
        body = json.loads(request.content)
        calls.append((request.url.path,body))
        if request.url.path == '/apply-template':
            result = {'prompt':'<user>'+body['messages'][0]['content']+'<assistant>'}
        elif request.url.path == '/tokenize':
            result = {'tokens':list(map(ord,body['content']))}
        else:
            result = {'content':''.join(map(chr,body['tokens']))}
        return httpx.Response(200,json=result)
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            probe = SimpleNamespace(runner=SimpleNamespace(client=client))
            for case in (cases[0], cases[8], cases[-1]):
                target = max(8192,case['inputs'])
                tokens, evidence = await Probes.evaluation_prompt(probe,SimpleNamespace(url='http://tokenizer'),
                                   {'evaluation':case['request'],'inputs':target})
                text = evidence['prompt']
                assert len(tokens) == len(text) == target
                for block in case['request']['blocks']:
                    assert text.count(block) == 1
                assert text.count(case['request']['question']) == 1
                assert len(evidence['fixed_fragment_token_offsets']) == 7
                assert evidence['fixed_fragment_token_offsets'] == sorted(evidence['fixed_fragment_token_offsets'])
                assert not any(key in body for _,body in calls for key in ('expected','source','positions','distractors'))
            with pytest.raises(ValueError,match='exceeds the available context'):
                await Probes.evaluation_prompt(probe,SimpleNamespace(url='http://tokenizer'),
                      {'evaluation':cases[0]['request'],'inputs':10})
    asyncio.run(check())


def test_only_evaluation_probe_bodies_can_exceed_64_kib(cases):
    async def read(body, allow=False):
        raw = json.dumps(body).encode()
        async def receive():
            return {'type':'http.request','body':raw,'more_body':False}
        return await Tasks.body(None,Request({'type':'http'},receive),allow_large_evaluation=allow)
    body = {'context':2048,'inputs':1024,'outputs':1024,'evaluation':cases[0]['request']}
    assert 65536 < len(json.dumps(body).encode()) < 1048576
    assert asyncio.run(read(body,True)) == body
    with pytest.raises(ValueError,match='64 KiB'):
        asyncio.run(read(body))
    with pytest.raises(ValueError,match='64 KiB'):
        asyncio.run(read({'text':'x'*65536},True))
    with pytest.raises(ValueError,match='1 MiB'):
        asyncio.run(read({'evaluation':{'question':'x'*1048576}},True))


def test_legacy_results_and_unfinished_plans_remain_separate(tmp_path):
    store = Benchmarks(tmp_path/'retrieval.db')
    expected = dict(director='Nia Solano',date='2029-06-11',room='West Gallery',budget_cents=48000,
                    speaker='Tomas Reed',exhibits=37,attendance=146,closing_event='Lantern Walk')
    case = dict(id='retrieve-1024-1',category='events',expected=expected,request={'question':'old question'})
    legacy = {**retrieval.PROTOCOL,'version':2,'dataset':'retrieval-v1','variants':3}
    legacy.pop('seed')
    number = store.create('owner','model','retrieval')
    store.activate(number,{'fingerprint':'fixture','environment':{},'subject':{'settings':{'flags':{'ctx-size':8192}}}})
    subject = store.execution(number)['subject']
    subject['protocol'] = legacy
    with store.db:
        store.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?',(json.dumps(subject),number))
    store.plan(number,{'protocol':legacy,'signature':quality.signature(legacy),'cases':[case]})
    values, checks = retrieval.grade(case,json.dumps(expected))
    assert values == quality.grade_fields(case,json.dumps(expected))[0]
    store.keep(number,case['id'],dict(success=True,**values),json.dumps(expected),'',{'case':case,'checks':checks})
    store.finish(number,'completed')
    before = copy.deepcopy(store.samples(number,True))
    store.db.close()
    store = Benchmarks(tmp_path/'retrieval.db')
    assert store.regrade() == 0 and store.samples(number,True) == before
    run = store.get(number,True)
    assert run['version'] == 2 and run['latest_version'] == 5 and run['score'] == 100
    assert 'invention' not in run['metrics'] and 'start_accuracy' not in run['metrics']
    store.finish(number,'paused')
    worker = SimpleNamespace(store=store)
    with pytest.raises(ValueError,match='protocol changed'):
        asyncio.run(retrieval.measure(worker,store.get(number,True),8192))
    assert store.samples(number,True) == before
    store.db.close()
