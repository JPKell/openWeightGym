"""Expanded Part 8 coverage, independent mutations and preserved P4 evidence."""
import json
from collections import Counter

import pytest

from benchmarks import tooling
from benchmarks.suites import tool_use
from test_tool_benchmarks import native, oracle


def score(case, turns):
    return tool_use.grade(case, json.dumps(turns))[0]


def test_coverage_counts_envelopes_and_real_seed_results():
    cases=tool_use.cases(32768)
    assert Counter(c['category'] for c in cases)==tool_use.SIZES
    assert cases==tool_use.cases(32768)
    assert len({c['id'] for c in cases})==60
    for case in cases:
        assert case['inputs']==0 and case['context']==32768 and 3<=len(case['tools'])<=8
        assert all(step['name'] in {t['function']['name'] for t in case['tools']} for step in case['steps'])
        if len(case['messages'])>2:
            call=case['messages'][2]['tool_calls'][0]['function']
            expected=tooling.World(case).call(call['name'],json.loads(call['arguments']))
            assert json.loads(case['messages'][3]['content'])==expected and 'error' not in expected
        assert score(case,oracle(case))['completed']
    assert len({len(c['tools']) for c in cases})==6


def test_order_is_free_only_for_independent_calls():
    cases=tool_use.cases(32768)
    for case in cases:
        if case['category']=='two_calls':
            turns=oracle(case);turns[:2]=reversed(turns[:2])
            assert score(case,turns)['completed']
        elif case['requires_lookup']:
            turns=oracle(case)
            # A guessed ID may happen to be right but was not observed from the lookup.
            turns[0]['tool_calls']+=turns.pop(1)['tool_calls']
            metrics,checks=tool_use.grade(case,json.dumps(turns))
            assert not metrics['completed']
            assert any(c['field']=='lookup_result_observed_before_use' and not c['passed'] for c in checks)


@pytest.mark.parametrize('mutation',['missing','extra','wrong_name','wrong_value','extra_argument','boolean','duplicate_json','bad_json','duplicate_id','seed_id','bad_envelope','trailing'])
def test_correct_final_answer_cannot_hide_bad_calls(mutation):
    case=next(c for c in tool_use.cases(32768) if c['category']=='follow_up' and c['steps']) if mutation=='seed_id' else tool_use.cases(32768)[1]
    turns=oracle(case)
    function=turns[0]['tool_calls'][0]['function']
    if mutation=='missing':
        turns.pop(0)
    elif mutation=='extra':
        turns.insert(1,native(case['steps'][0],99))
    elif mutation=='wrong_name':
        function['name']='convert_currency'
    elif mutation=='wrong_value':
        function['arguments']='{"metres":-1}'
    elif mutation=='extra_argument':
        params=json.loads(function['arguments']);params['extra']=1;function['arguments']=json.dumps(params)
    elif mutation=='boolean':
        function['arguments']='{"metres":true}'
    elif mutation=='duplicate_json':
        function['arguments']='{"metres":1,"metres":1}'
    elif mutation=='bad_json':
        function['arguments']='not JSON'
    elif mutation=='duplicate_id':
        turns[0]['tool_calls']*=2
    elif mutation=='seed_id':
        turns[0]['tool_calls'][0]['id']='seed'
    elif mutation=='bad_envelope':
        turns[0]['tool_calls']={}
    else:
        turns.append({'role':'assistant','content':json.dumps(case['expected'])})
    assert not score(case,turns)['completed']


def test_no_call_controls_and_shortcut_upper_bounds():
    rows=[];never=first=0
    for case in tool_use.cases(32768):
        answer=oracle(case)[-1]
        never+=score(case,[answer])['completed']
        name=case['tools'][0]['function']['name']
        matching=next((s for s in case['steps'] if s['name']==name),dict(name=name,arguments={}))
        first+=score(case,[native(matching,100),answer])['completed']
        if not case['steps']:
            rows.append({'values':{**score(case,[native(dict(name=name,arguments={}),100),answer]),'success':True}})
    assert never==11 and first<30  # generous scripts receive the correct final keys
    summary=tool_use.summarize(rows)
    assert summary['unneeded_call_rate']==100 and summary['calls_when_unneeded']==11
    assert summary['task_completion']==0 and summary['exact_call_count_accuracy']==0
    good=[{'values':{**score(c,oracle(c)),'success':True}} for c in tool_use.cases(32768) if not c['steps']]
    summary=tool_use.summarize(good)
    assert summary['task_completion']==100 and summary['unneeded_call_rate']==0
    assert 'selection_accuracy' not in summary and 'argument_accuracy' not in summary
    assert 'task_completion' not in tool_use.summarize([{'values':{'unsupported':True}}])


def test_native_names_schemas_values_and_extras_are_separate_checks():
    case=tool_use.cases(32768)[1];turns=oracle(case)
    params=json.loads(turns[0]['tool_calls'][0]['function']['arguments']);params['extra']=123
    turns[0]['tool_calls'][0]['function']['arguments']=json.dumps(params)
    metrics=score(case,turns)
    assert metrics['selection_correct']==metrics['required_value_correct']==1
    assert metrics['valid_argument_correct']==metrics['no_extra_argument_correct']==0
    assert not metrics['completed']


def test_v1_scores_and_completion_survive_v2_grader():
    for case in tooling.cases(8192):
        turns=oracle(case)
        for raw in (json.dumps(turns),json.dumps([turns[0],native(case['steps'][0],99),*turns[1:]]),'invalid'):
            assert tool_use.grade(case,raw)==tooling.grade(case,raw)


@pytest.mark.parametrize('calls',[{},[{'id':'broken'}]])
def test_malformed_no_call_decision_counts_as_an_unneeded_attempt(calls):
    case=next(c for c in tool_use.cases(32768) if not c['steps'])
    metrics=score(case,[{'role':'assistant','content':json.dumps(case['expected']),'tool_calls':calls}])
    assert not metrics['completed'] and metrics['called_when_unneeded']
    assert not metrics['exact_call_count'] and metrics['native_call_attempts']==1
    assert metrics['tool_calls']==0  # malformed envelopes never reach a handler
    summary=tool_use.summarize([{'values':{**metrics,'success':True}}])
    assert summary['unneeded_call_rate']==100 and summary['native_call_attempts']==1
