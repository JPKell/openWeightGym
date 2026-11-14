"""Native decision throughput, measured separately from text generation."""

from statistics import median

DEFINITION = {'id': 'classifier_speed', 'name': 'Decisions/sec', 'classifier': True,
    'summary': 'Native typed-decision throughput and p50/p95 latency on a fixed request mix.',
    'method': 'Three fresh native probes for every combination of 1/8/32 questions and 2/8/50 choices. '
              'Measure complete request latency, input-only token usage and startup separately. '
              'The headline is median decisions/sec across the fixed mix; no generated tokens are counted.',
    'formula': 'decisions_per_second', 'score_unit': 'decisions/s',
    'kpis': [('decisions_per_second', 'Decisions', 'decisions/s'), ('p50_seconds', 'p50 latency', 's'),
             ('p95_seconds', 'p95 latency', 's'), ('load_seconds', 'Load time', 's'), ('requests', 'Requests', 'count')]}
PROTOCOL = {'version': 1, 'questions': [1, 8, 32], 'options': [2, 8, 50], 'repetitions': 3,
            'transport': '/v1/systemone', 'timing': 'complete native HTTP request; startup excluded'}


async def measure(worker, run, ceiling):
    """Execute this frozen Classifier benchmark protocol."""
    for questions in PROTOCOL['questions']:
        for options in PROTOCOL['options']:
            for repeat in range(PROTOCOL['repetitions']):
                body = {'state': 'Route this support ticket to team_0. The customer was charged twice.',
                    'model': run['model'], 'questions': {f'route_{i}': {'type': 'choice',
                        'instructions': 'Which team should handle this customer request?',
                        'criteria': {f'team_{n}': 'Billing' if n == 0 else None for n in range(options)}}
                        for i in range(questions)}}
                await worker.classify(run['id'], f'decisions-{questions}-{options}-{repeat}', body)


def percentile(values, p):
    """Interpolate a bounded latency percentile."""
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    low = int(index)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low)


def summarize(samples):
    """Derive authoritative aggregate metrics from saved samples."""
    values = [s['values'] for s in samples if s['values'].get('success')]
    if not values:
        return {}
    return {'decisions_per_second': median(v['decisions_per_second'] for v in values),
            'p50_seconds': median(v['request_seconds'] for v in values),
            'p95_seconds': percentile([v['request_seconds'] for v in values], .95),
            'load_seconds': median(v['load_seconds'] for v in values), 'requests': len(values)}
