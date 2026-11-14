"""Present journal text without losing unrecognized lines or traceback continuations."""

import hashlib
import re
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

STAMP = re.compile(r'^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?')
ORIGIN = re.compile(r'^(?:\S+\s+)?[\w@./-]+(?:\[\d+\])?:\s*')
CONTINUATION = re.compile(r'^(?:\s|Traceback\b|Caused by:|\w*(?:Error|Exception):)')


def level(text):
    """Journal short-iso has no priority field; these labels are inferred from text."""
    for severity, pattern in (
        ('error', r'\b(?:EMERG|ALERT|CRIT|CRITICAL|FATAL|ERROR|ERR|Traceback)\b|\w*(?:Error|Exception):'),
        ('warning', r'\b(?:WARN|WARNING)\b'),
        ('debug', r'\b(?:DEBUG|TRACE)\b'),
        ('info', r'\b(?:INFO|NOTICE)\b'),
    ):
        if re.search(pattern, text, re.I):
            return severity
    return 'unclassified'


def workspace(lines, node, source, timezone):
    entries = []
    for line in lines:
        stamp = STAMP.match(line)
        if not stamp and entries and CONTINUATION.match(line):
            entries[-1]['raw'] += '\n' + line
            continue
        instant = None
        if stamp:
            try:
                instant = datetime.fromisoformat(stamp[0])
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=ZoneInfo(timezone))
            except ValueError:
                pass
        message = line[stamp.end():].lstrip() if stamp else line
        # The configured node/source stay authoritative even if output names another host.
        message = ORIGIN.sub('', message, count=1)
        entries.append(dict(raw=line, message=message or line, node=node, source=source,
                            at=instant.isoformat() if instant else None,
                            epoch=instant.timestamp() if instant else None))
    occurrences = Counter()
    for index, entry in enumerate(entries):
        digest = hashlib.blake2s(entry['raw'].encode(), digest_size=8).hexdigest()
        occurrences[digest] += 1
        entry.update(id=f'{digest}-{occurrences[digest]}', index=index, level=level(entry['raw']))
    instants = [entry['epoch'] for entry in entries if entry['epoch'] is not None]
    duration = max(instants) - min(instants) if instants else None
    return dict(entries=entries, count=len(entries),
                errors=sum(entry['level'] == 'error' for entry in entries),
                warnings=sum(entry['level'] == 'warning' for entry in entries),
                duration='—' if duration is None else f'{duration:g} s' if duration < 60 else f'{duration // 60:g} min')
