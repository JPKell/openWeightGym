"""Daily/weekday wall-clock schedules and stable occurrence IDs, using only the stdlib."""
import hashlib
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate(spec):
    """Validate and normalize a daily or weekday schedule specification."""
    optional = {'machine', 'model_ids', 'benchmarks', 'nodes'}
    if not isinstance(spec, dict) or set(spec) - optional != {'kind', 'model_id', 'benchmark', 'timezone', 'time', 'weekdays', 'missed', 'retention'}:
        raise ValueError('Choose a backup or benchmark, timezone, time, weekdays and missed-run policy.')
    if spec['kind'] not in ('backup', 'benchmark', 'outstanding') or spec['missed'] not in ('skip', 'latest'):
        raise ValueError('Choose backup or benchmark and skip or latest missed runs.')
    if not isinstance(spec['timezone'], str) or not isinstance(spec['time'], str):
        raise ValueError('Choose a timezone name and time in HH:MM format.')
    try:
        ZoneInfo(spec['timezone'])
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('Choose a known timezone name, such as America/Vancouver.') from None
    clock = datetime.strptime(spec['time'], '%H:%M')
    if spec['time'] != clock.strftime('%H:%M'):
        raise ValueError('Time must be HH:MM.')
    days = spec['weekdays']
    if not isinstance(days, list) or not days or any(type(day) is not int or not 0 <= day <= 6 for day in days) or len(days) != len(set(days)):
        raise ValueError('Select at least one distinct weekday (Monday=0 through Sunday=6).')
    if not isinstance(spec['model_id'], str) or not isinstance(spec['benchmark'], str):
        raise ValueError('Model and benchmark must be strings.')
    if type(spec['retention']) is not int or not 0 <= spec['retention'] <= 100:
        raise ValueError('Backup retention must be from zero through 100.')
    if spec['kind'] == 'benchmark' and (not spec['model_id'] or not spec['benchmark']):
        raise ValueError('Select one local model and benchmark.')
    if spec['kind'] == 'backup' and (spec['model_id'] or spec['benchmark']):
        raise ValueError('A backup has no model or benchmark.')
    if not isinstance(spec.get('machine', ''), str) or len(spec.get('machine', '')) > 100:
        raise ValueError('Choose a configured preferred node.')
    for field in ('model_ids', 'benchmarks', 'nodes'):
        values = spec.get(field, [])
        if not isinstance(values, list) or len(values) > 100 or len(set(values)) != len(values) or any(not isinstance(value, str) or not value or len(value)>200 for value in values):
            raise ValueError('Select up to 100 distinct models and benchmarks.')
    if spec['kind'] == 'outstanding' and (spec['model_id'] or spec['benchmark']):
        raise ValueError('Missing/outdated work uses model and benchmark scopes.')
    return {**spec, 'weekdays': sorted(days)}


def next_at(spec, after):
    """First future local occurrence. Skip spring gaps; choose the first autumn fold."""
    zone = ZoneInfo(spec['timezone'])
    date = datetime.fromtimestamp(after, zone).date()
    hour, minute = map(int, spec['time'].split(':'))
    for offset in range(15):  # a weekly slot may be skipped by the next spring gap
        day = date + timedelta(days=offset)
        if day.weekday() not in spec['weekdays']:
            continue
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        stamp = local.timestamp()
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if stamp > after:
            return stamp
    raise ValueError('No next occurrence could be calculated.')


def occurrence_ids(schedule, due):
    """Derive stable batch and backup IDs from a schedule and due timestamp."""
    digest = hashlib.sha256(f'{schedule}:{due:.0f}'.encode()).hexdigest()
    backup = datetime.fromtimestamp(due, UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + digest[:24]
    return 'batch_' + digest[:24], backup


def is_due(spec, due):
    """Return whether the timestamp is an actual occurrence of this schedule."""
    if type(due) not in (int, float):
        return False
    try:
        return math.isfinite(due) and next_at(spec, due-1) == due
    except (ValueError, OverflowError, OSError):
        return False
