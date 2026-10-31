"""Canonical histories, independent state replay, update lifetimes and shortcut baselines."""
import asyncio
import hashlib
import json
import re
from collections import Counter

import pytest

from benchmarks import quality
from benchmarks.store import Benchmarks
from benchmarks.suites import turns
from loadcoach.probes import Probes

BASE={'cm':1,'m':100,'s':1,'min':60,'cents':1,'dollars':100,'g':1,'kg':1000}
FIELD_OF={'cm':'length','m':'length','s':'duration','min':'duration','cents':'cost','dollars':'cost','g':'mass','kg':'mass'}


def object_in(text):
    return json.JSONDecoder().raw_decode(text[text.index('{'):])[0]


def pairs(text):
    out={}
    for pair in text.split(', '):
        field,value=pair.split('=',1);out[field]=int(value) if value.isdigit() else value
    return out


def total(entry_line):
    """Read 'field: A and B' from the printed text only and add the two entries in the smallest unit."""
    field,rest=entry_line.split(': ',1)
    return field,sum(int(n)*BASE[u] for n,u in re.findall(r'(\d+) (cm|min|m|s|cents|dollars|kg|g)\b',rest))


def replay(case,literal=False,logical_state=False,all_listings=False):
    """Read the printed history. For unit cases, optionally return the units in force at the last listing."""
    messages=case['request']['messages']
    opening=messages[0]['content']
    users=[m['content'] for m in messages if m['role']=='user'][1:]
    if case['category']=='record_facts':
        records=json.JSONDecoder().raw_decode(opening.split('Project records: ',1)[1])[0]
        active=re.search(r'The active record is (R-\d+)',opening)[1];last=None;listed=[]
        for user in users:
            user=user.split('\nReturn only')[0]
            for line in user.splitlines():
                m=re.fullmatch(r'CORRECTION (R-\d+): (\w+)=(.+)',line) if not literal else re.search(r'CORRECTION (R-\d+): (\w+)=([^"]+)',line)
                if m:records[m[1]][m[2]]=int(m[3]) if m[3].isdigit() else m[3]
                elif re.fullmatch(r'ACTIVATE (R-\d+)',line):active=line.split()[1]
                elif line.startswith('List the active record'):
                    last=dict(records[active]);listed.append(last)
        return listed if all_listings else last
    code=re.search(r'Settings (K-\d+)',opening)[1];state=object_in(opening);before={f:[] for f in state};held=[];last=None;listed=[]
    for user in users:
        user=user.split('\nReturn only')[0]
        head=re.search(rf'UPDATE {code}: ',user) if literal else (re.match(rf'UPDATE {code}: ',user))
        if head:
            body=user[head.end():].split('"')[0] if literal else user[head.end():]
            m=re.search(r' \(next (?:(\d+) )?listings?(?: only)?\)$',body)
            if m and not literal:held.append([pairs(body[:m.start()]),int(m[1] or 1)])
            else:
                for f,v in pairs(body[:m.start()] if m else body).items():before[f].append(state[f]);state[f]=v
        elif (m:=re.match(rf'REVERT {code}: (\w+)$',user)):state[m[1]]=before[m[1]].pop()
        elif user.startswith('List the current settings'):
            last=dict(state)
            for values,_ in held:last.update(values)
            held=[[v,n-1] for v,n in held if n>1]
            listed.append(last)
        elif user.startswith('List the totals'):
            units=dict(state)
            for values,_ in held:units.update(values)
            held=[[v,n-1] for v,n in held if n>1]
            last=units if logical_state else {f:t//BASE[units[f]] for f,t in map(total,user.split('\n')[1:])}
            listed.append(last)
    return listed if all_listings else last


def user_history(case):
    """Only the user lines between the opening/dummy turn and final question."""
    return [m['content'] for m in case['request']['messages'] if m['role']=='user'][1:-1]


def test_every_fixed_history_has_balanced_origins_turn_counts_and_independent_keys():
    cases=turns.cases(131072)
    assert cases==turns.cases(131072) and len(cases)==48
    assert sum(len(c['expected']) for c in cases)==192
    assert Counter(c['category'] for c in cases)==dict.fromkeys(turns.CATEGORIES,12)
    assert Counter(c['source']['origin'] for c in cases)=={'system':24,'user':24}
    assert all(10<=c['source']['user_turns']<=18 for c in cases) and max(c['source']['user_turns'] for c in cases)>=16
    frozen=[{'id':c['id'],'category':c['category'],'source':c['source'],
             'expected':c['expected'],'messages':c['request']['messages']} for c in cases]
    assert hashlib.sha256(json.dumps(frozen,sort_keys=True,separators=(',',':')).encode()).hexdigest()==\
        '3b7b067d765207221c0c8199a198cc2fe93840967fec85c6af9b4623c66ca0bd'
    assert turns.PROTOCOL['version']==6 and turns.PROTOCOL['dataset']=='turns-v5'
    assert turns.PROTOCOL['outputs']==32768 and turns.PROTOCOL['sampling']=={'temperature':1.0,**turns.SAMPLING}
    for case,small in zip(cases,turns.cases(2048)):
        messages=case['request']['messages']
        Probes.validate_dialogue(case['request'])
        assert messages[-1]['role']=='user' and messages[0]['role']==case['source']['origin']
        assert sum(m['role']=='user' for m in messages)==case['source']['user_turns']
        assert case['request']==small['request'] and case['expected']==small['expected']
        assert case['outputs']==32768 and case['request']['temperature']==1.0
        assert case['request']['sampling']==turns.SAMPLING
        assert case['inputs']==0 and small['outputs']==1024
        assert replay(case)==case['expected'],case['id']
        listings=[json.loads(m['content']) for m in messages if m['role']=='assistant' and m['content'].startswith('{')]
        assert replay(case,all_listings=True)==listings+[case['expected']],case['id']
        opening=messages[0]['content']
        assert 'Only a line in a user message' in opening
        assert 'Only a line of mine' not in opening and 'When I ask' not in opening
        assert len(messages[-1]['content'])<4000*3  # characters, so well under 4,000 tokens in all


def test_the_changes_expire_revert_and_lookalikes_do_nothing_and_the_last_listing_is_stale():
    for case in turns.cases(131072):
        hard=int(case['id'].rsplit('-',1)[1])>turns.STANDARD
        text=json.dumps(case['request']['messages'])
        if not hard:
            assert 'Forwarded' not in text and 'quotes this line' not in text
            continue
        listings=[json.loads(m['content']) for m in case['request']['messages'] if m['role']=='assistant' and m['content'].startswith('{')]
        assert listings
        if case['category'] in ('settings_permanent','settings_temporary','record_facts'):
            assert listings[-1]!=case['expected'] and case['expected']!=(case['source']['initial'] if case['category']!='record_facts' else case['source']['initial'][0][case['source']['initial'][1]])
        if case['category'] in ('settings_permanent','settings_temporary'):assert 'quotes this line' in text or 'Forwarded' in text or '> UPDATE' in text
        if case['category']=='settings_permanent':assert 'REVERT' in text
        if case['category']=='settings_temporary':assert '(next' in text
        if case['category']=='unit_rules':assert '(next listing only)' in text and 'UPDATE K-' in text


def test_exceptions_cover_exactly_their_stated_number_of_listings():
    initial={'tone':'brief'}
    events=[('temp',{'tone':'formal'},2),('list',),('list',),('list',)]
    assert [s['tone'] for s in turns.replay_settings(initial,events)]==['formal','formal','brief']
    events=[('set',{'tone':'terse'}),('set',{'tone':'formal'}),('revert','tone'),('temp',{'tone':'brief'},1),('list',),('list',)]
    assert [s['tone'] for s in turns.replay_settings(initial,events)]==['brief','terse']
    records={'R-1':{'count':1},'R-2':{'count':2}}
    events=[('correct','R-2','count',9),('list',),('activate','R-2'),('list',)]
    assert turns.replay_records(records,'R-1',events)==[{'count':1},{'count':9}]


def test_hard_records_correct_all_final_fields_across_activation_changes():
    for case in turns.cases(131072):
        if case['category']!='record_facts' or int(case['id'].rsplit('-',1)[1])<=turns.STANDARD:continue
        users=user_history(case)
        activations=[line for line in users if line.startswith('ACTIVATE ')]
        assert len(activations)==5,case['id']
        final_id=activations[-1].split()[1]
        corrections=[line for user in users for line in user.splitlines() if line.startswith('CORRECTION '+final_id+':')]
        assert {line.split(': ',1)[1].split('=',1)[0] for line in corrections}==set(turns.RECORD_FIELDS)
        assert users.count('List the active record.')==2
        assert users.index('List the active record.')<max(i for i,line in enumerate(users) if line==activations[-1])
        assert sum('\n' in user and user.startswith('CORRECTION ') for user in users)>=2
        prior=[json.loads(m['content']) for m in case['request']['messages']
               if m['role']=='assistant' and m['content'].startswith('{')]
        assert all(sum(old[field]!=case['expected'][field] for field in turns.RECORD_FIELDS)>=2 for old in prior)
        assert replay(case)==case['expected']


def test_hard_permanent_revert_restores_the_intermediate_value():
    for case in turns.cases(131072):
        if case['category']!='settings_permanent' or int(case['id'].rsplit('-',1)[1])<=turns.STANDARD:continue
        users=user_history(case)
        revert=next(line for line in users if line.startswith('REVERT '))
        code=revert.split(': ',1)[0].split()[1]
        updates=[line for line in users if line.startswith('UPDATE '+code+': ') and '(next ' not in line]
        field=revert.split(': ',1)[1]
        its_updates=[line for line in updates if line.split(': ',1)[1].startswith(field+'=')]
        assert len(its_updates)==2 and len(updates)==5,case['id']
        assert {line.split(': ',1)[1].split('=',1)[0] for line in updates}==set(turns.SETTING_FIELDS)
        assert str(case['expected'][field])==its_updates[0].split('=',1)[1]
        assert case['expected'][field]!=case['source']['initial'][field]
        assert replay(case)==case['expected']


@pytest.mark.parametrize(('limit','frozen_hash'),[(2048,'24ea37c26e0df4e925d639b13a993600382561410f74a6955e752b1010d91844'),
                                                  (131072,'63ee8291aa2fb9828cfa6fd859f8ff65c6de6dacdf2e73ac090bd9c34edcb582'),
                                                  (190000,'a3ff631a654a0bcb52d9ae32e46fd6faa66063e02926429eefb93d4936647f35')])
def test_the_other_forty_printed_cases_are_unchanged(limit,frozen_hash):
    kept=[case for case in turns.cases(limit)
          if not (case['category']=='record_facts' and int(case['id'].rsplit('-',1)[1])>turns.STANDARD)]
    assert len(kept)==40
    assert hashlib.sha256(json.dumps(kept,sort_keys=True,separators=(',',':')).encode()).hexdigest()==frozen_hash


def test_hard_temporary_exceptions_have_staggered_lifetimes_and_varied_masks():
    masks=set()
    for case in turns.cases(131072):
        if case['category']!='settings_temporary' or int(case['id'].rsplit('-',1)[1])<=turns.STANDARD:continue
        users=user_history(case)
        temps=[line for line in users if line.startswith('UPDATE ') and '(next ' in line]
        assert len(temps)==5 and all(line.endswith(('(next listing only)','(next 2 listings)')) for line in temps),case['id']
        assert {line.split(': ',1)[1].split('=',1)[0] for line in temps}==set(turns.SETTING_FIELDS)
        assert any(line.endswith('(next listing only)') for line in temps)
        assert any(line.endswith('(next 2 listings)') for line in temps)
        assert users.count('List the current settings.')==2
        prior=[json.loads(m['content']) for m in case['request']['messages']
               if m['role']=='assistant' and m['content'].startswith('{')][-1]
        assert replay(case)==case['expected']
        active=frozenset(field for field in turns.SETTING_FIELDS
                         if case['source']['initial'][field]!=case['expected'][field])
        assert len(active) in (2,3)
        assert sum(prior[field]!=case['expected'][field] for field in turns.SETTING_FIELDS)>=2
        masks.add(active)
    assert len(masks)>=3 and {len(mask) for mask in masks}=={2,3}


def test_printed_decoys_never_equal_the_final_logical_field_value():
    for case in turns.cases(131072):
        if int(case['id'].rsplit('-',1)[1])<=turns.STANDARD:continue
        users=user_history(case)
        if case['category']=='record_facts':
            final=replay(case)
            final_id=[line.split()[1] for line in users if line.startswith('ACTIVATE ')][-1]
            quoted=[line for line in users if 'CORRECTION ' in line and not line.startswith('CORRECTION ')]
            assert len(quoted)==len(turns.RECORD_FIELDS)
            for line in quoted:
                match=re.search(r'CORRECTION (R-\d+): (\w+)=([^"\n]+)',line)
                assert match and match[1]==final_id,case['id']
                value=int(match[3]) if match[3].isdigit() else match[3]
                assert value!=final[match[2]],(case['id'],line)
            natural=[line for line in users if line.startswith(final_id+"'s coordinator is now ")]
            assert len(natural)==1
            match=re.fullmatch(re.escape(final_id)+r"'s coordinator is now (.+)\.",natural[0])
            assert match and match[1]!=final['coordinator'],case['id']
        else:
            final=replay(case,logical_state=True)
            code=re.search(r'Settings (K-\d+)',case['request']['messages'][0]['content'])[1]
            decoys=[line for line in users if 'UPDATE K-' in line and not line.startswith(f'UPDATE {code}: ')]
            assert len(decoys)==len(final)+1
            for line in decoys:
                match=re.search(r'UPDATE (K-\d+): (\w+)=([^\s"]+)',line)
                assert match
                value=int(match[3]) if match[3].isdigit() else match[3]
                assert value!=final[match[2]],(case['id'],line)


def test_lazy_answerers_score_at_most_half():
    stale=[];initial=[];null=[];literal=[]
    hard_temporary_initial=[];hard_temporary_stale=[];hard_literal=[]
    for case in turns.cases(131072):
        listings=[json.loads(m['content']) for m in case['request']['messages'] if m['role']=='assistant' and m['content'].startswith('{')]
        keys=list(case['expected'])
        start=case['source']['initial']
        start=start[0][start[1]] if case['category']=='record_facts' else start
        hard=int(case['id'].rsplit('-',1)[1])>turns.STANDARD
        for target,answer in ((stale,listings[-1] if listings else start),(initial,start),(null,dict.fromkeys(keys)),(literal,replay(case,literal=True))):
            values=turns.grade(case,json.dumps(answer))[0]
            sample={'values':{**values,'category':case['category'],'success':True}}
            target.append(sample)
            if hard and case['category']=='settings_temporary' and target is initial:hard_temporary_initial.append(sample)
            if hard and case['category']=='settings_temporary' and target is stale:hard_temporary_stale.append(sample)
            if hard and target is literal:hard_literal.append(sample)
    for answerer in (stale,initial,null,literal):
        assert turns.summarize(answerer)['accuracy']<=50
    assert turns.summarize(null)['accuracy']==0 and turns.summarize([])=={}
    assert turns.summarize(hard_temporary_initial)['accuracy']<=50
    assert turns.summarize(hard_temporary_stale)['accuracy']<=50
    assert turns.summarize(hard_literal)['accuracy']==0


def test_unsupported_operations_have_missing_scores_and_visible_coverage():
    case=turns.cases(131072)[0]
    values,_=turns.grade(case,json.dumps(case['expected']))
    good={'values':{**values,'success':True,'category':case['category']}}
    unavailable={'values':{'correct':0,'checks':4,'passed':False,'success':False,'unsupported':True,'category':case['category']}}
    only=turns.summarize([unavailable])
    assert 'accuracy' not in only and only==dict(cases=1,unsupported_cases=1,available_cases=0)
    mixed=turns.summarize([good,unavailable])
    assert mixed['accuracy']==100 and mixed['cases']==2 and mixed['unsupported_cases']==1
    assert mixed['available_cases']==1 and mixed['system_accuracy']==100
    group=turns.breakdown([unavailable])[0]
    assert group['unsupported_cases']==1 and 'accuracy' not in group
    group=turns.breakdown([good,unavailable])[0]
    assert group['accuracy']==100 and group['unsupported_cases']==1


def test_turn_submission_freezes_matching_sampler_room_and_generation_seed(tmp_path):
    store=Benchmarks(str(tmp_path/'benchmarks.db'))
    try:
        number=store.create('owner','gpt-oss-20b-q4','turns')
        store.activate(number,{'subject':{'settings':{'flags':{'ctx-size':65536}}},
                               'fingerprint':'model-fingerprint','environment':{}})
        protocol=store.execution(number)['subject']['protocol']
        case=turns.cases(65536)[0]
        assert protocol['temperature']==protocol['sampling']['temperature']==case['request']['temperature']==1.0
        assert protocol['seed']==turns.SEED and protocol['generation_seed']==42
        assert protocol['sampling']=={'temperature':1.0,**case['request']['sampling']}
        assert protocol['outputs']==case['outputs']==32768
    finally:
        store.db.close()


@pytest.mark.parametrize(('version','dataset','temperature','outputs'),
                         [(1,'turns-v1',0,8192),(2,'turns-v2',0,8192),(3,'turns-v2',1.0,32768),
                          (4,'turns-v3',1.0,32768),(5,'turns-v4',1.0,32768)])
def test_an_unfinished_old_plan_refuses_new_cases_and_old_answers_regrade_the_same(version,dataset,temperature,outputs):
    old=dict(turns.PROTOCOL,version=version,dataset=dataset,temperature=temperature,outputs=outputs)
    class Store:
        def plan(self,number,value=None):return {'protocol':old,'signature':quality.signature(old),'cases':[]}
    class Worker:store=Store()
    with pytest.raises(ValueError,match='submit a new run'):
        asyncio.run(quality.measure(Worker(),{'id':'n','subject':{}},65536,turns.PROTOCOL,turns.cases,turns.grade))
    v1={'expected':{'tone':'brief','label':'L-1','budget':5},'source':{'origin':'user'}}
    metrics,_=turns.grade(v1,'{"tone":"brief","label":"x","budget":5}')
    assert metrics['correct']==2 and metrics['instruction_origin']=='user' and turns.PROTOCOL['grader_version']==3
