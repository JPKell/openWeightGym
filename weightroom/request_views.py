"""Presentation for the Requests ledger; measurements retain their recorded scope and basis."""

import math

from loadcoach.diagnostics import number


def listing(rows):
    """Summarize only this visible page, without presenting a sample as a fleet aggregate."""
    sample = [row for row in rows if not row.get('benchmark')]
    ended = [row for row in sample if row.get('outcome')]
    values = sorted(value for row in ended if (value := number(row.get('compute_total'))) is not None)
    errors = sum(bool(row.get('server_error')) for row in ended)
    return dict(shown=len(rows), sample=len(sample), endings=len(ended), errors=errors,
                unfinished=sum(not row.get('outcome') for row in sample), measured=len(values),
                error_percent=round(errors * 100 / len(ended), 1) if ended else None,
                p95=values[math.ceil(len(values) * .95) - 1] if values else None)


def metadata(facts):
    """Export the same authorized diagnostic metadata, excluding retained text and tool arguments."""
    asked, ended = facts.get('asked') or {}, facts.get('ended') or {}
    return dict(id=facts['id'], state=facts.get('state'), live_available=facts.get('live_available', False),
                asked={key: asked[key] for key in ('at', 'user', 'caller', 'model', 'level', 'path', 'machine',
                       'trace', 'request_id', 'role', 'picked', 'because', 'hosted', 'instead_of') if key in asked},
                ended={key: ended[key] for key in ('at', 'what', 'status', 'waited', 'ran', 'fingerprint', 'build',
                       'prompt_tokens', 'answer_tokens', 'tokens_per_second', 'cost', 'why') if key in ended},
                diagnostics=facts.get('diagnostics') or {})


def detail(facts):
    """Highlight the outcome and measured timing without inventing missing or overlapping stages."""
    asked, ended = facts.get('asked') or {}, facts.get('ended') or {}
    diagnostic = facts.get('diagnostics') or {}
    metrics = {metric['label']: metric for metric in diagnostic.get('metrics', [])}
    total = metrics.get('Total request', {})
    phases = diagnostic.get('phases', [])
    queue = [phase for phase in phases if phase.get('stage') == 'queue']
    queue_seconds = sum(phase['seconds'] for phase in queue) if queue else None
    error = ((number(ended.get('status')) or 0) >= 500 or ended.get('what') == 'failed'
             or ended.get('reliability_outcome') in ('error', 'timeout', 'restart'))
    active = bool(facts.get('state')) and facts.get('live_available', True)
    if not ended:
        title = 'Request is ' + facts['state'] + '.' if active else 'No ending is recorded.'
        note = 'Measurements are partial while this request is under way.' if active else 'Live progress is unknown. Retained metadata remains available.'
    elif error:
        title = 'The request ended with an error.'
        note = ended.get('why') or f"Recorded outcome: {ended.get('what')}; HTTP status {ended.get('status', 'unavailable')}."
    elif ended.get('what') == 'done':
        title = 'The request completed successfully.'
        note = f"Answered by {asked.get('model') or 'the selected model'}. Inspect the recorded stages and trace for timing evidence."
    else:
        title = f"The request ended: {ended.get('what', 'unknown')}."
        note = ended.get('why') or 'The recorded outcome is separate from a server failure.'
    return dict(title=title, note=note, error=error, active=active,
                total=total.get('seconds'), total_basis=total.get('basis', 'Unavailable'),
                queue=queue_seconds, first_output=metrics.get('First generated output', {}).get('seconds'),
                rate=number(ended.get('tokens_per_second')), metadata=metadata(facts))
