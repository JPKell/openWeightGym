"""Presentation for recurring schedules; timing always uses the dispatcher's calendar rules."""
from datetime import datetime
from zoneinfo import ZoneInfo

from loadcoach.schedule_time import next_at, validate


def timing(spec, enabled=True, now=None):
    """Validate a timing-only draft and return its next three local occurrences."""
    now = datetime.now().timestamp() if now is None else now
    try:
        spec = validate(dict(kind='backup', model_id='', benchmark='', retention=0, missed='skip',
                             time=spec['time'], timezone=spec['timezone'], weekdays=spec['weekdays']))
        runs, after = [], now
        for _ in range(3):
            after = next_at(spec, after)
            runs.append(datetime.fromtimestamp(after, ZoneInfo(spec['timezone'])).strftime('%a %d %b, %H:%M %Z'))
        return dict(spec=spec, next_runs=runs if enabled else [], enabled=enabled, timing_error='')
    except (ValueError, KeyError, TypeError) as error:
        return dict(spec=spec, next_runs=[], enabled=enabled, timing_error=str(error))


def decorate(row, now):
    """Add display labels to an already-sanitized schedule row."""
    spec = row['spec']
    local = datetime.fromtimestamp(row['next_at'], ZoneInfo(spec['timezone']))
    today = datetime.fromtimestamp(now, ZoneInfo(spec['timezone'])).date()
    days = (local.date() - today).days
    row['next_time'] = local.strftime('%H:%M') if row['enabled'] else '—'
    row['next_day'] = ('Today' if days == 0 else 'Tomorrow' if days == 1 else local.strftime('%a %d %b')) if row['enabled'] else 'Paused'
    row['attention'] = bool(row['last'] and row['last']['state'] in ('failed', 'deferred', 'rejected'))
    row['work'] = (f"{row['model_name'] or spec['model_id']} · {spec['benchmark']}" if spec['kind'] == 'benchmark' else
                   f"One eligible pair · {spec.get('machine') or 'default'}" if spec['kind'] == 'outstanding' else
                   f"Recovery sets · retain {spec['retention'] or 'all'} per machine")
    return row
