"""Alert desk presentation over the existing durable episode store."""
import json
from urllib.parse import parse_qsl, urlencode, urlsplit

SCOPES = (('open', 'Open episodes'), ('history', 'Closed history'), ('all', 'All episodes'),
          ('unseen', 'Unacknowledged open'), ('review', 'Needs manual review'), ('unknown', 'Recovery unknown'))
PAGE_SIZE = 25


def link(filters, **more):
    return '/alerts?' + urlencode({key: value for key, value in (filters | more).items() if value != '' and value is not None})


def return_url(value):
    """Retain only this desk's local, bookmarkable context after an action."""
    try:
        parsed = urlsplit(str(value or ''))
    except ValueError:
        return '/alerts'
    if parsed.scheme or parsed.netloc or parsed.path != '/alerts':
        return '/alerts'
    return link({key: value for key, value in parse_qsl(parsed.query)
                 if key in ('scope', 'source', 'q', 'page', 'selected')})


def timeline(row):
    events = [('First observed', row['first_at']), ('Last observed', row['last_at'])]
    if row['acknowledged_at']:
        events.append(('Acknowledged by ' + row['acknowledged_by'], row['acknowledged_at']))
    if row['resolved_at']:
        events.append(('Incident reviewed' if row['state'] == 'reviewed' else 'Recovery confirmed', row['resolved_at']))
    events.append(('Checked · condition unavailable' if row['state'] == 'unknown' else 'Source checked', row['checked_at']))
    return sorted(events, key=lambda event: event[1])


def workspace(store, asked):
    scope = asked.get('scope', 'open')
    if scope not in dict(SCOPES):
        raise ValueError('Choose a supported episode scope.')
    try:
        number = min(max(int(asked.get('page', 1)), 1), 1_000_000)
    except (TypeError, ValueError):
        raise ValueError('Page must be a whole number.') from None
    filters = dict(scope=scope, source=asked.get('source', ''), q=asked.get('q', '').strip())
    total = store.count(filters)
    number = min(number, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
    offset = (number - 1) * PAGE_SIZE
    rows = list(store.matching(filters, PAGE_SIZE, offset))
    selected = next((row for row in rows if row['id'] == asked.get('selected')), rows[0] if rows else None)
    context = filters | {'page': number}
    for row in rows:
        row['href'] = link(context, selected=row['id'])
    sources = store.sources()
    return dict(filters=filters, scopes=SCOPES, rows=rows, selected=selected,
                timeline=timeline(selected) if selected else [], summary=store.summary(), total=total,
                start=offset + 1 if total else 0, end=offset + len(rows), page=number,
                previous=link(context, page=number - 1) if number > 1 else None,
                next=link(context, page=number + 1) if offset + len(rows) < total else None,
                refresh_url=link(context, selected=selected['id'] if selected else ''),
                export_url=link(filters, format='json'), source_names=sorted(set(store.source_names()) | ({filters['source']} if filters['source'] else set())),
                sources=sources, available=sum(bool(source['available']) for source in sources),
                checked_at=max((source['checked_at'] for source in sources), default=None),
                key=urlencode(context))


def export_json(store, view):
    """Stream every matching episode; pagination does not truncate the export."""
    yield '{"filters":' + json.dumps(view['filters']) + ',"sources":' + json.dumps(view['sources']) + ',"episodes":['
    separator = ''
    for row in store.matching(view['filters']):
        yield separator + json.dumps(row, ensure_ascii=True)
        separator = ','
    yield ']}'
