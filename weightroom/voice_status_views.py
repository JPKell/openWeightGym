"""Read-only Voice status presentation and documented latency references."""

TARGETS = {'end_to_audio_seconds': (2500, 3000), 'clear_ack_seconds': (None, 350)}
NAMES = {'browser': 'Browser', 'desktop': 'Desktop', 'telephone': 'Phone', 'hermes': 'Hermes'}


def normalized(status):
    status = dict(status) if isinstance(status, dict) else {}
    for name, default in {'entries': {}, 'telephone': {'numbers': []}, 'readiness': {'profiles': {}},
                          'version': {}, 'recordings': {}, 'latency': {}}.items():
        status[name] = {**default, **(status.get(name) or {})}
    return status


def workspace(status, problem=''):
    entries = []
    for key, name in NAMES.items():
        entry = dict(status.get('entries', {}).get(key, {}))
        reason = entry.get('reason') or ('Ready' if entry.get('can_start') else
            'Gateway is not answering.' if problem else 'The gateway has not reported this entry’s readiness.')
        target = '/system/voice/telephone' if key == 'telephone' else '/system/voice'
        if key != 'telephone' and any(word in reason.lower() for word in ('model', 'speech', 'compute')):
            target = '/system/components'
        if any(word in reason.lower() for word in ('account', 'permission')):
            target = '/users'
        entries.append({**entry, 'key': key, 'name': name, 'can_start': bool(entry.get('can_start')), 'reason': reason, 'fix': target})
    timings = []
    for row in status.get('latency', {}).get('summaries', []):
        cells = []
        for value, target in zip((row.get('p50_ms'), row.get('p95_ms')), TARGETS.get(row.get('metric'), (None, None))):
            cells.append({'value': value, 'target': target, 'within': value <= target if target is not None and value is not None else None})
        timings.append({**row, 'cells': cells})
    return {'entries': entries, 'timings': timings}
