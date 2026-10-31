"""Live progress from saved work, without inventing completion inside a running probe."""

import math

from .suites import context, load, speed, embedding_speed


def progress(run: dict, now: float) -> dict:
    """Count planned samples and estimate remaining probe time; elapsed is wall-clock time.

    Context search has no fixed denominator. Its ETA is a rough upper search budget based
    on the remaining interval and mean completed probe duration, not a completion promise.
    Pauses and queue waits are included in elapsed time but not the probe-duration estimate.
    """
    samples = run['samples']
    done, total, remaining = len(samples), None, None
    subject = run.get('subject', {})
    settings = subject.get('settings', {})
    configured = settings.get('flags', {}).get('ctx-size', 0)
    ceiling = int(subject.get('values', {}).get('trained_context') or configured)
    plan = run.get('plan')
    adaptive = run['benchmark'] in ('context', 'embedding_context')
    if plan and 'cases' in plan:
        total = len(plan['cases'])
    elif run['benchmark'] in ('load', 'embedding_load') and configured:
        minimum = settings.get('min_context', min(2048, configured))
        total = len({minimum, (minimum + configured) // 2, configured}) * load.PROTOCOL['repetitions']
    elif run['benchmark'] == 'speed' and samples:
        # The first result knows the effective context, including a prior capacity result.
        effective = samples[0]['values'].get('context_tokens', min(configured, ceiling))
        total = sum(size + speed.PROTOCOL['speed_outputs'] < effective
                    for size in speed.PROTOCOL['speed_inputs']) * speed.PROTOCOL['repetitions']
    elif run['benchmark'] == 'embedding_speed' and samples:
        effective = samples[0]['values'].get('context_tokens', min(configured, ceiling))
        total = len(embedding_speed.input_sizes(effective)) * embedding_speed.PROTOCOL['repetitions']
    if total is not None:
        total = max(total, done)
        remaining = total - done
    elif adaptive and ceiling >= 128:
        if context.next_context(samples, ceiling) is None:
            remaining = 0
        else:
            step = min(1024, max(128, ceiling // 4))
            low = max((s['values']['context_tokens'] for s in samples if s['values']['success']), default=0)
            high = min((s['values']['context_tokens'] for s in samples if not s['values']['success']), default=ceiling)
            remaining = math.ceil(math.log2(max(1, (high - low) / step))) + 2
    elapsed = max(0, (run.get('ended') or now) - run['started']) if run.get('started') else 0
    durations = [sum(s['values'].get(key, 0) or 0 for key in ('load_seconds', 'request_seconds')) for s in samples]
    durations = [value for value in durations if math.isfinite(value) and value > 0]
    eta = sum(durations) / len(durations) * remaining if durations and remaining is not None else None
    # Subtract time already spent on the current probe, including a retried probe only once.
    probing = [m['at'] for m in run.get('messages', [])
               if ': requesting an isolated probe' in m['text'] or ': evaluating at ' in m['text']]
    if eta is not None and probing and probing[-1] > max((s['at'] for s in samples), default=0):
        eta -= max(0, now - probing[-1])
    overdue = eta is not None and eta < 0
    if run['state'] == 'completed':
        eta, overdue, remaining = 0, False, 0
    elif run['state'] != 'running':
        eta, overdue = None, False
    percent = 100 if run['state'] == 'completed' else (min(99, done / total * 100) if total else None)
    return {'completed': done, 'total': total, 'percent': percent, 'adaptive': adaptive,
            'remaining_samples': remaining, 'elapsed_seconds': elapsed,
            'eta_seconds': max(0, eta) if eta is not None else None, 'eta_overdue': overdue}
