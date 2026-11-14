"""Typed native probes keep distributions, keys and authoritative benchmark scores."""

import json

import pytest

from benchmarks.eligibility import compatible
from benchmarks.suites import classifier_accuracy, classifier_speed
from benchmark_clients import gateway
from test_app import fake_llama_server, in_an_empty_folder, model, add_user  # noqa: F401
from test_benchmarks import wait
from test_classifiers import decision_file


def test_classifier_kind_boundary_and_probability_metrics():
    assert compatible({'capabilities': {'classifier': 'kev'}}, classifier_accuracy.DEFINITION)
    assert not compatible({'capabilities': {'classifier': 'kev'}}, {'id': 'speed'})
    assert not compatible({'capabilities': {'chat': True}}, classifier_accuracy.DEFINITION)
    case = {'expected': {'q': {'label': True, 'source': 'fixture'}}}
    first, checks = classifier_accuracy.grade(case, {'answers': {'q': {'noul': .8}}})
    assert checks[0]['passed'] and first['observations'][0]['brier'] == pytest.approx(.08)
    second, _ = classifier_accuracy.grade(case, {'answers': {'q': {'noul': .2}}})
    summary = classifier_accuracy.summarize([{'values': {'success': True, **values}} for values in (first, second)])
    assert summary['accuracy'] == 50 and summary['brier'] == pytest.approx(.68)
    assert summary['ece'] == pytest.approx(.3)
    assert sum(b['count'] for b in summary['reliability_bins']) == 2


@pytest.mark.parametrize('kind', ['classifier_speed', 'classifier_accuracy'])
def test_classifier_benchmarks_use_native_transport_without_answer_key(fake_llama_server, monkeypatch, kind):
    path = decision_file('decision.gguf', 'kev')
    monkeypatch.setattr(classifier_speed, 'PROTOCOL', {**classifier_speed.PROTOCOL, 'questions': [1, 8], 'options': [2], 'repetitions': 1})
    monkeypatch.setattr(classifier_accuracy, 'records', lambda: [{'state': 'Example', 'questions': {'q': {'type': 'noul',
        'instructions': 'Is this an example?', 'label': True, 'src': 'fixture'}}}])
    with gateway(fake_llama_server, models={'decision': model(path, 0)}) as caller:
        owner = add_user(caller, 'owner', 'admin')
        answer = caller.post('/benchmarks', headers=owner, json={'benchmark': kind, 'models': ['decision']})
        assert answer.status_code == 202, answer.text
        run = wait(caller, owner, answer.json()['runs'][0], lambda r: r['state'] in ('completed', 'failed'), seconds=40)
        assert run['state'] == 'completed', run['reason']
        assert run['score'] > 0
        for sample in run['samples']:
            assert sample['values']['output_tokens'] == 0
            evidence = sample['evidence']
            assert evidence['endpoint'] == '/v1/systemone' and evidence['answers']
            assert all('label' not in q and 'src' not in q for q in evidence['request']['questions'].values())
        assert caller.post('/benchmarks', headers=owner, json={'benchmark': 'speed', 'models': ['decision']}).status_code == 400
