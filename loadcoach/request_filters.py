"""Validated request-column filters shared by the console and retained-log reader."""

import math
from datetime import datetime
from ipaddress import ip_address
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loadcoach.usage import ENDINGS
from loadcoach.groups import LEVELS

FIELDS = ('request_min', 'request_max', 'date_min', 'date_max', 'duration_min', 'duration_max',
          'model', 'user', 'ip', 'status', 'node', 'level', 'q')
STATUSES = (*ENDINGS, 'unfinished')


def parse(params) -> dict:
    """Parse inclusive finite ranges and exact selections; reject inverted or malformed bounds."""
    result = {}
    try:
        zone = ZoneInfo(params.get('timezone') or 'UTC')
        for name in FIELDS:
            value = params.get(name, '').strip()
            if not value:
                continue
            if name.startswith('request_'):
                number = int(value)
                if not 0 <= number < 1 << 52:
                    raise ValueError('Request numbers must be whole numbers in the public ID range.')
                result[name] = number
            elif name.startswith('duration_'):
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    raise ValueError('Durations must be finite, nonnegative seconds.')
                result[name] = number
            elif name.startswith('date_'):
                moment = datetime.fromisoformat(value)
                result[name] = (moment.replace(tzinfo=zone) if moment.tzinfo is None else moment).timestamp()
            elif name == 'ip':
                result[name] = str(ip_address(value))
            elif name == 'status':
                if value not in STATUSES:
                    raise ValueError('Choose a valid request status.')
                result[name] = value
            elif name == 'level':
                if value not in (*LEVELS, 'unrecorded'):
                    raise ValueError('Choose a valid request security level.')
                result[name] = value
            else:
                if len(value) > 256:
                    raise ValueError('Filter selections must be at most 256 characters.')
                result[name] = value
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as problem:
        raise ValueError('Choose valid request, date, duration, IP, status and security level filters.') from problem
    for prefix in ('request', 'date', 'duration'):
        low, high = result.get(prefix + '_min'), result.get(prefix + '_max')
        if low is not None and high is not None and low > high:
            raise ValueError('Each minimum must be no greater than its maximum.')
    return result
