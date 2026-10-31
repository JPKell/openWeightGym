"""Evidence separation, deterministic keys and anti-shortcut baselines for abstention."""
import json
import re
from collections import Counter

from benchmarks import quality
from benchmarks.suites import abstention


def scored(answerer):
    return abstention.summarize([{'values': abstention.grade(case, json.dumps(answerer(case)))[0]}
                                for case in abstention.cases(131072)])


def first_typed(case):
    patterns = {'person': r'\b(?:' + '|'.join(quality.FIRST) + r') (?:' + '|'.join(quality.LAST) + r')\b',
                'place': r'\b(?:' + '|'.join(quality.PLACE_A) + r') (?:' + '|'.join(quality.PLACE_B) + r')\b',
                'date': r'\b\d{4}-\d{2}-\d{2}\b', 'integer': r'(?<![\d-])\b\d{3,4}\b(?![\d-])'}
    answer = {}
    for key, kind in case['types'].items():
        found = re.search(patterns[kind], case['source']['passage'])
        value = found[0] if found else None
        answer[key] = int(value) if value and kind == 'integer' else value
    return answer


def test_frozen_passages_keys_and_four_distinct_absences():
    cases = abstention.cases(131072)
    assert cases == abstention.cases(131072)
    assert len(cases) == 20 and sum(len(c['expected']) for c in cases) == 120
    assert Counter(kind for c in cases for kind in c['absence_kinds'].values()) == dict.fromkeys(abstention.ABSENCES, 15)
    seen = set()
    for case, small in zip(cases, abstention.cases(2048)):
        assert case['request'] == small['request'] and case['expected'] == small['expected']
        assert case['inputs'] == 0 and small['outputs'] == 1024
        assert list(case['expected'].values()).count(None) == 3 and quality.clear(case)
        text = case['source']['passage']
        assert 1200 <= len(text) <= 7800 and text in case['request']['question']
        source = case['source']
        for key, value in case['expected'].items():
            field = source['fields'][int(key[1:])-1]
            label = abstention.FIELDS[field][2] + ':'
            current = [line for line in text.splitlines() if line.startswith('Project ' + source['project'] + ': ' + label)]
            archive = [line for line in text.splitlines() if line.startswith('Project ' + source['archive'] + ': ' + label)]
            if value is not None:
                assert value not in seen
                seen.add(value)
                assert str(value) in source['support'][key] and source['support'][key] in current
            else:
                kind = case['absence_kinds'][key]
                assert bool(current) == (kind == 'false_premise')
                assert bool(archive) == (kind == 'other_subject')
                if kind == 'false_premise':
                    assert 'exactly one entry' in current[0]
                    assert re.search(key + r': .*second ', case['request']['question'])
                if kind == 'not_given':
                    assert 'has an approved ' + abstention.FIELDS[field][0] + ', but its entries are not recorded' in text


def test_lazy_answerers_cannot_win_by_null_or_first_typed_value():
    nulls = scored(lambda c: dict.fromkeys(c['expected']))
    first = scored(first_typed)
    assert nulls['accuracy'] == 50 and nulls['present_accuracy'] == 0 and nulls['absent_accuracy'] == 100
    assert first['accuracy'] < 50 and first['present_accuracy'] == 0
    assert all(nulls[k + '_accuracy'] == 100 for k in abstention.ABSENCES)


def test_kpis_distinguish_missing_keys_nulls_and_invented_values():
    perfect = scored(lambda c: c['expected'])
    assert perfect['accuracy'] == perfect['present_accuracy'] == perfect['absent_accuracy'] == 100
    assert perfect['invention_rate'] == 0
    missing = scored(lambda c: {})
    assert missing['accuracy'] == missing['absent_accuracy'] == missing['invention_rate'] == 0
    invented = scored(lambda c: {k: v if v is not None else 'not in text' for k,v in c['expected'].items()})
    assert invented['accuracy'] == 50 and invented['present_accuracy'] == invented['invention_rate'] == 100
    assert invented['absent_accuracy'] == 0 and all(invented[k + '_accuracy'] == 0 for k in abstention.ABSENCES)
    assert abstention.summarize([]) == {}


def test_null_string_and_boolean_are_values_not_correct_absent_answers():
    case = abstention.cases(131072)[0]
    for replacement in ('null', False, 0):
        answer = {k: replacement if v is None else v for k,v in case['expected'].items()}
        metrics, checks = abstention.grade(case, json.dumps(answer))
        assert metrics['correct'] == 3 and metrics['invented'] == 3
        assert sum(c['passed'] for c in checks) == 3
