"""Escaped, consistent display of optional values, counts and recorded timestamps."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import os
import math
import re

from markupsafe import Markup, escape


def value(item):
    """Use a dash for absent or non-finite readings; preserve zero and ordinary text."""
    return '—' if item is None or isinstance(item, float) and not math.isfinite(item) else item


def node_label(name):
    """Present the control node's stable machine ID without changing stored identities."""
    return 'Control node' if name == 'front' else name


def configured_timezone(name=''):
    """Resolve the configured display zone consistently with the host fallback."""
    selected = name or server_zone()
    ZoneInfo(selected)
    return selected


def count(item):
    """Display a count as a whole number with separators, or a dash when unavailable."""
    if value(item) == '—':
        return '—'
    try:
        return f'{int(item):,}'
    except (ValueError, TypeError, OverflowError):
        return '—'


def moment(item):
    """Normalize recorded epochs/ISO dates; legacy naive dates use the source server's zone."""
    try:
        date = (datetime.fromtimestamp(item, UTC) if isinstance(item, (int, float)) else
                item if isinstance(item, datetime) else datetime.fromisoformat(item))
        return date.astimezone(UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def timestamp(item):
    """An accessible UTC fallback, with a machine-readable instant for browser localization."""
    date = moment(item)
    if date is None:
        return escape(value(item) if item is not None else '—')
    iso = date.isoformat(timespec='seconds').replace('+00:00', 'Z')
    return Markup('<time datetime="{}" data-local-time title="{}">{}</time>').format(
        iso, iso, date.strftime('%d %b %Y, %H:%M UTC'))


def log_line(line):
    """Format a leading log timestamp while escaping every byte of the recorded message."""
    found = re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?', line)
    return timestamp(found[0]) + escape(line[found.end():]) if found else escape(line)


def server_zone():
    """Read the host's IANA zone so legacy local filters retain their original meaning."""
    candidate = os.environ.get('TZ', '').removeprefix(':')
    if not candidate:
        path = str(Path('/etc/localtime').resolve())
        candidate = path.split('/zoneinfo/', 1)[-1] if '/zoneinfo/' in path else 'UTC'
    try:
        ZoneInfo(candidate)
        return candidate
    except (ValueError, KeyError):
        return 'UTC'


def input_utc(item, zone):
    """Expose a legacy datetime-local field's instant without changing its wire contract."""
    if not item:
        return ''
    try:
        date = datetime.fromisoformat(item)
        if date.tzinfo is None:
            date = date.replace(tzinfo=ZoneInfo(zone))
        return date.astimezone(UTC).isoformat(timespec='seconds')
    except (TypeError, ValueError):
        return ''
