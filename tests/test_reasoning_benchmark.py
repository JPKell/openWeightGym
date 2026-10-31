"""Independent finite oracles read only the exact puzzle data sent to the model."""
import asyncio
import hashlib
import itertools
import json
import time
from collections import Counter

import pytest

from benchmarks import quality
from benchmarks.store import Benchmarks
from benchmarks.suites import reasoning


def printed(case):
    content=case['request']['messages'][0]['content']
    puzzle=json.loads(content.split('Puzzle data:\n',1)[1].split('\nReturn only',1)[0])
    assert puzzle==case['source']
    return puzzle


def oracle(case):
    p=printed(case);category=case['category']
    if category=='ordering':
        found=[]
        for row in itertools.permutations(p['people']):
            good=True
            for kind,a,b,n in p['clues']:
                left,right=row.index(a),row.index(b)
                if kind=='between':end=row.index(n);good &= min(right,end)<left<max(right,end)
                else:good &= {'before':left<right,'gap':right-left==n,'adjacent':abs(right-left)==1,'not_adjacent':abs(right-left)!=1,'not_end':left not in (0,len(row)-1)}[kind]
            if good:found.append(row)
        assert len(found)==1
        return {key:found[0][n-1] for key,n in p['queries'].items()}
    if category=='scheduling':
        found=[]
        for row in itertools.product(p['hours'],repeat=len(p['meetings'])):
            booked=set();good=True
            for hour,meeting in zip(row,p['meetings']):
                resources=[('room',meeting['room'])]+[('person',person) for person in meeting['people']]
                good &= all(hour in p['availability'][person] for person in meeting['people'])
                for resource in resources:
                    entry=(hour,resource)
                    if entry in booked:good=False
                    booked.add(entry)
            if good and all(row[a]<row[b] for a,b in p['clues']):found.append(row)
        assert len(found)==1
        return {key:found[0][n] for key,n in p['queries'].items()}
    if category=='constraints':
        names=list(p['attributes']);found=[]
        for rows in itertools.product(*(itertools.permutations(p['attributes'][n]) for n in names)):
            people=[{name:rows[k][i] for k,name in enumerate(names)} for i in range(4)];good=True
            for clue in p['clues']:
                if clue[0] in ('has','not_has'):good &= (people[clue[1]][clue[2]]==clue[3])==(clue[0]=='has')
                else:
                    owner=lambda attribute,value:next(i for i,x in enumerate(people) if x[attribute]==value)
                    good &= (owner(clue[1],clue[2])==owner(clue[3],clue[4]))==(clue[0]=='same')
            if good:found.append(people)
        assert len(found)==1
        return {key:found[0][n][attribute] for key,(attribute,n) in p['queries'].items()}
    state=p['initial'].copy()
    for target,condition,operand,threshold,yes,no in p['moves']:
        test={'even':lambda:state[operand]%2==0,'greater':lambda:state[operand]>threshold,'compare':lambda:state[operand]>state[threshold]}[condition]()
        op,source,number=yes if test else no
        original=state[source]
        state[target]={'add':original+number,'subtract':original-number,'multiply':original*number,
                       'floor_div':original//number,'mod':original%number}[op]
        assert abs(state[target])<=10**6
    return {name:state[name] for name in p['queries']}


def test_forty_eight_printed_puzzles_have_unique_independently_solved_keys():
    cases=reasoning.cases(65536)
    assert cases==reasoning.cases(65536)
    assert len(cases)==48 and sum(len(c['expected']) for c in cases)==144
    assert Counter(c['category'] for c in cases)==dict.fromkeys(reasoning.CATEGORIES,12)
    assert len({c['id'] for c in cases})==48
    frozen=[{'id':c['id'],'source':c['source'],'expected':c['expected'],'messages':c['request']['messages']} for c in cases]
    assert hashlib.sha256(json.dumps(frozen,sort_keys=True,separators=(',',':')).encode()).hexdigest()==\
        '675e4275f5a7093dd48af4c2a60222df4b74beabe62f164b1882496946703ad2'
    for case,small in zip(cases,reasoning.cases(2048)):
        assert oracle(case)==case['expected'],case['id']
        assert case['request']==small['request'] and small['outputs']==1024 and case['outputs']==32768
        assert set(case['request'])=={'messages','temperature','sampling'}
        assert case['request']['temperature']==1.0 and case['request']['sampling']==reasoning.SAMPLING
        hard=int(case['id'].rsplit('-',1)[1])>reasoning.STANDARD
        source=case['source']
        if case['category']=='ordering':assert len(source['people'])>=(7 if hard else 5)
        if case['category']=='scheduling':
            assert len(source['meetings'])==(6 if hard else 4)
            assert all(len(source['availability'][p])>=3 for p in source['availability'])
        if case['category']=='constraints':assert len(source['attributes'])==(3 if hard else 2)
        if case['category']=='steps':assert len(source['moves'])==(10 if hard else 5)
    hard=[c['source'] for c in cases if int(c['id'].rsplit('-',1)[1])>reasoning.STANDARD]
    kinds={c[0] for p in hard for c in p.get('clues',[]) if isinstance(c[0],str)}
    assert {'between','not_adjacent','not_has','differ'}<=kinds
    operations={o[0] for p in hard if 'moves' in p for m in p['moves'] for o in m[4:]}
    assert {'floor_div','mod'}<=operations and any(m[1]=='compare' for p in hard if 'moves' in p for m in p['moves'])


def test_answer_room_is_large_but_bounded_by_half_the_context():
    assert [reasoning.cases(limit)[0]['outputs'] for limit in (8192,32768,190000)]==[4096,16384,32768]
    assert reasoning.PROTOCOL['outputs']==32768 and reasoning.PROTOCOL['version']==3 and reasoning.PROTOCOL['dataset']=='reasoning-v2'
    assert reasoning.PROTOCOL['temperature']==1.0 and reasoning.PROTOCOL['generation_seed']==42
    assert reasoning.PROTOCOL['sampling']=={'temperature':1.0,**reasoning.SAMPLING}


def test_reasoning_submission_freezes_matching_sampler_and_generation_seed(tmp_path):
    store=Benchmarks(str(tmp_path/'benchmarks.db'))
    try:
        number=store.create('owner','gpt-oss-20b-q4','reasoning')
        store.activate(number,{'subject':{'settings':{'flags':{'ctx-size':65536}}},
                               'fingerprint':'model-fingerprint','environment':{}})
        protocol=store.execution(number)['subject']['protocol']
        case=reasoning.cases(65536)[0]
        assert protocol['temperature']==protocol['sampling']['temperature']==case['request']['temperature']==1.0
        assert protocol['seed']==reasoning.SEED and protocol['generation_seed']==42
        assert protocol['sampling']=={'temperature':1.0,**case['request']['sampling']}
    finally:
        store.db.close()


def test_generation_stays_fast_and_every_input_fits_a_small_model():
    start=time.perf_counter();cases=reasoning.cases(8192)
    assert time.perf_counter()-start<30
    assert max(len(c['request']['messages'][0]['content']) for c in cases)<4000*3  # characters; well under 4,000 tokens


def test_exhaustive_oracle_refuses_impossible_and_ambiguous_puzzles():
    for case in reasoning.cases(32768):
        kind=case['category'];p=json.loads(json.dumps(case['source']))
        if kind=='steps':continue
        p['clues']=[]
        if kind=='scheduling':p['availability']={name:p['hours'] for name in p['availability']}
        with pytest.raises(ValueError,match='exactly one'):reasoning.solve(kind,p)
        if kind=='ordering':p['clues']=[['before',p['people'][0],p['people'][0],0]]
        elif kind=='scheduling':p['availability'][next(iter(p['availability']))]=[]
        else:p['clues']=[['has',0,'colour',p['attributes']['colour'][0]],['has',0,'colour',p['attributes']['colour'][1]]]
        with pytest.raises(ValueError,match='exactly one'):reasoning.solve(kind,p)


def test_state_values_are_bounded_and_floor_division_follows_python():
    state={'a':-7,'b':3}
    assert reasoning.transition(state,['a','even','b',0,['floor_div','a',2],['add','b',1]])['a']==4  # b=3 is odd: no branch
    assert reasoning.transition(state,['a','compare','b','b',['floor_div','a',2],['mod','a',5]])['a']==3  # -7 > 3 is false: -7 % 5
    with pytest.raises(ValueError,match='bound'):reasoning.transition({'a':10**6},['a','even','a',0,['multiply','a',2],['multiply','a',2]])


def test_reasoning_shortcuts_and_whole_puzzle_scoring():
    samples=[];initial=[];printed_order=[];perfect=[]
    for case in reasoning.cases(32768):
        puzzle=case['source'];person=puzzle.get('people',[''])[0]
        wrong=dict.fromkeys(case['expected'],person)
        samples.append({'values':reasoning.grade(case,json.dumps(wrong))[0]})
        initial.append({'values':reasoning.grade(case,json.dumps(puzzle.get('initial',{})))[0]})
        # The lazy answerer that reads the puzzle in the order it is printed and takes the first thing it sees.
        lazy={key:puzzle['people'][n-1] for key,n in puzzle['queries'].items()} if case['category']=='ordering' else \
            {key:min(puzzle['hours']) for key in case['expected']} if case['category']=='scheduling' else \
            {key:puzzle['attributes'][a][n] for key,(a,n) in puzzle['queries'].items()} if case['category']=='constraints' else puzzle['initial']
        printed_order.append({'values':reasoning.grade(case,json.dumps(lazy))[0]})
        good=reasoning.grade(case,json.dumps(case['expected']))[0]
        perfect.append({'values':dict(good,category=case['category'],success=True)})
    assert reasoning.summarize(samples)['accuracy']<=50
    assert reasoning.summarize(initial)['accuracy']<=50
    assert reasoning.summarize(printed_order)['accuracy']<=50
    result=reasoning.summarize(perfect)
    assert result['accuracy']==result['case_accuracy']==100
    assert result['passed_cases']==48 and result['reasoning_token_cases']==0 and 'reasoning_tokens' not in result
    assert len(reasoning.breakdown(perfect))==4
    case=reasoning.cases(32768)[0];answer=dict(case['expected']);answer['position_1' if 'position_1' in answer else next(iter(answer))]='wrong'
    values=reasoning.grade(case,json.dumps(answer))[0]
    assert values['correct']==2 and not values['passed']
    report=reasoning.summarize([{'values':dict(values,reasoning_tokens=0)}, {'values':{'unsupported':True}}])
    assert report['reasoning_token_cases']==1 and report['reasoning_tokens']==0 and report['unsupported_cases']==1
    assert reasoning.summarize([])=={}


def test_a_solver_that_ignores_the_new_clue_kinds_fails_the_hard_ordering_puzzles():
    """The between and not_adjacent clues are needed: the first row that fits only the version 1 kinds is mostly wrong."""
    scores=[]
    for case in reasoning.cases(32768):
        if case['category']!='ordering' or int(case['id'].rsplit('-',1)[1])<=reasoning.STANDARD:continue
        old=[c for c in case['source']['clues'] if c[0] not in ('between','not_adjacent')]
        row=next(r for r in itertools.permutations(case['source']['people']) if all(reasoning.ordering_ok(r,c) for c in old))
        answer={key:row[n-1] for key,n in case['source']['queries'].items()}
        scores.append(reasoning.grade(case,json.dumps(answer))[0]['correct']/3)
    assert sum(scores)/len(scores)<=0.5


def test_cut_off_answers_are_told_apart_from_wrong_ones():
    right={'success':True,'checks':3,'correct':3,'passed':True,'category':'steps'}
    cut={'success':True,'checks':3,'correct':0,'passed':False,'generation_capped':True,'category':'steps'}
    wrong=dict(right,correct=0,passed=False)
    result=reasoning.summarize([{'values':right},{'values':cut},{'values':wrong}])
    assert result['accuracy']==pytest.approx(100*3/9) and result['capped_cases']==1 and result['capped_checks']==3
    assert result['uncapped_accuracy']==pytest.approx(50)
    assert 'uncapped_accuracy' not in reasoning.summarize([{'values':cut}]) and reasoning.summarize([{'values':cut}])['capped_checks']==3


def test_an_unfinished_version_1_plan_refuses_new_cases_and_version_1_answers_regrade_the_same():
    old=dict(reasoning.PROTOCOL,version=1,dataset='reasoning-v1',cases=40,checks=120)
    assert quality.signature(old)!=quality.signature(reasoning.PROTOCOL)
    class Store:
        def plan(self,number,value=None):return {'protocol':old,'signature':quality.signature(old),'cases':[]}
    class Worker:store=Store()
    with pytest.raises(ValueError,match='submit a new run'):
        asyncio.run(quality.measure(Worker(),{'id':'n','subject':{}},65536,reasoning.PROTOCOL,reasoning.cases,reasoning.grade))
    v1={'expected':{'third':'Mira Chen','first':'Oren Vale','last':'Ilse Pike'}}  # a version 1 case carries no new field
    metrics,checks=reasoning.grade(v1,'{"third":"Mira Chen","first":"x","last":"Ilse Pike"}')
    assert metrics['correct']==2 and [c['passed'] for c in checks]==[True,False,True]
    assert reasoning.PROTOCOL['grader_version']==3
