"""Frozen held-out Kev questions, native probability grading and reliability bins."""

import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

import httpx

SOURCE = json.loads(Path(__file__).with_name('classifier_source.json').read_text())
DEFINITION = {'id': 'classifier_accuracy', 'name': 'Classifier accuracy', 'classifier': True, 'quality': True,
    'summary': 'Held-out typed decisions: accuracy, macro-F1, Brier score and calibration error.',
    'method': 'Run every record of the frozen kev-suites decision-v1 test partition. Answer keys remain on the control node '
              'and are never sent to the model. Use argmax for choice/score correctness and a 0.5 threshold for yes/no. '
              'Macro-F1 covers observed source/question/class labels. Brier is the mean sum of squared class errors; '
              'ECE uses ten equal-width maximum-probability bins. Probabilities remain uncalibrated for user data. '
              'Upstream source datasets retain their own terms; this repository distributes only the frozen manifest.',
    'formula': 'accuracy', 'score_unit': '%',
    'kpis': [('accuracy', 'Accuracy', '%'), ('macro_f1', 'Macro-F1', '%'), ('brier', 'Brier score', 'fraction'),
             ('ece', 'Calibration error', 'fraction'), ('questions', 'Questions', 'count'), ('cases', 'Cases', 'count')]}
PROTOCOL = {'version': 1, 'grader_version': 1, 'dataset': SOURCE, 'transport': '/v1/systemone',
            'headline': 'all labelled questions; native distributions', 'calibration_bins': 10}


def records():
    """Fetch the pinned test partition on demand and verify it before using labels."""
    folder = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'openWeightGym' / 'benchmarks'
    file = folder / (SOURCE['sha256'] + '.jsonl')
    if not file.is_file():
        url = f"https://huggingface.co/datasets/{SOURCE['dataset']}/resolve/{SOURCE['revision']}/{SOURCE['path']}"
        response = httpx.get(url, follow_redirects=True, timeout=60)
        response.raise_for_status()
        raw = response.content
        if hashlib.sha256(raw).hexdigest() != SOURCE['sha256']:
            raise ValueError('Classifier dataset checksum mismatch.')
        folder.mkdir(parents=True, exist_ok=True)
        temporary = file.with_suffix('.part')
        temporary.write_bytes(raw)
        temporary.replace(file)
    raw = file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE['sha256']:
        raise ValueError('Classifier dataset checksum mismatch.')
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


def cases(rows):
    """Strip labels and provenance from the native request, retaining both as evidence."""
    return [{'id': f'kev-test-{i:04}', 'request': {'state': row['state'], 'questions': {
                key: {k: v for k, v in question.items() if k in ('type', 'instructions', 'criteria')}
                for key, question in row['questions'].items()}},
             'expected': {key: {'label': q['label'], 'source': q.get('src', row.get('_meta', {}).get('source', ''))}
                          for key, q in row['questions'].items()}, 'provenance': row.get('_meta', {})}
            for i, row in enumerate(rows)]


async def measure(worker, run, ceiling):
    """Execute this frozen Classifier benchmark protocol."""
    plan = worker.store.plan(run['id'])
    if plan is None:
        plan = worker.store.plan(run['id'], {'protocol': PROTOCOL, 'cases': cases(await asyncio.to_thread(records))})
    if plan['protocol'] != PROTOCOL:
        raise ValueError('Classifier accuracy protocol changed; submit a new run.')
    for case in plan['cases']:
        await worker.classify(run['id'], case['id'], {'model': run['model'], **case['request']}, grade, case)


def grade(case, response):
    """Score native distributions against labels retained on the control node."""
    if isinstance(response, str):
        response = json.loads(response)
    observations, checks = [], []
    for key, expected in case['expected'].items():
        answer = response['answers'][key]
        if 'noul' in answer:
            probs = {'false': 1 - answer['noul'], 'true': answer['noul']}
            truth = 'true' if expected['label'] else 'false'
        else:
            probs = answer['probabilities']
            truth = str(expected['label'])
        if not probs or any(type(p) not in (int, float) or not 0 <= p <= 1 for p in probs.values()) or abs(sum(probs.values()) - 1) > .001:
            raise ValueError('Classifier returned an invalid probability distribution.')
        predicted = max(probs, key=probs.get)
        correct = predicted == truth
        observation = {'source': expected['source'], 'question': key, 'truth': truth, 'predicted': predicted,
            'correct': correct, 'confidence': probs[predicted],
            'brier': sum((p - (label == truth)) ** 2 for label, p in probs.items())}
        observations.append(observation)
        checks.append({'name': key, 'passed': correct, 'expected': truth, 'actual': predicted})
    return {'observations': observations, 'questions': len(observations), 'checks': len(observations),
            'passed': all(o['correct'] for o in observations), 'correct': sum(o['correct'] for o in observations)}, checks


def summarize(samples):
    """Derive authoritative aggregate metrics from saved samples."""
    values = [s['values'] for s in samples if s['values'].get('success')]
    observations = [o for value in values for o in value.get('observations', [])]
    if not observations:
        return {}
    true, predicted, matched = Counter(), Counter(), Counter()
    bins = [{'lower': i / 10, 'upper': (i + 1) / 10, 'count': 0, 'correct': 0, 'confidence_sum': 0.0} for i in range(10)]
    for o in observations:
        prefix = (o['source'], o['question'])
        true[(*prefix, o['truth'])] += 1
        predicted[(*prefix, o['predicted'])] += 1
        if o['correct']:
            matched[(*prefix, o['truth'])] += 1
        bucket = bins[min(9, int(o['confidence'] * 10))]
        bucket['count'] += 1
        bucket['correct'] += int(o['correct'])
        bucket['confidence_sum'] += o['confidence']
    labels = true.keys() | predicted.keys()
    count = len(observations)
    return {'accuracy': 100 * sum(o['correct'] for o in observations) / count,
        'macro_f1': 100 * sum(2 * matched[k] / (true[k] + predicted[k]) for k in labels) / len(labels),
        'brier': sum(o['brier'] for o in observations) / count,
        'ece': sum(abs(b['correct'] - b['confidence_sum']) for b in bins) / count,
        'questions': count, 'cases': len(values), 'reliability_bins': bins}
