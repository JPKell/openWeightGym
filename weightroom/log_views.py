"""Presentation and metadata exports for the authorized AI event log."""

import csv
import io
import json
from urllib.parse import urlencode

from weightroom.display import moment
from loadcoach.event_filters import SEARCH_FIELDS

FILTERS = ('q', 'q_field', 'kind', 'what', 'user', 'group', 'model', 'trace', 'start', 'end', 'node')
CORE = {'id', 'at', 'kind', 'what', 'user', 'caller', 'machine', 'model', 'trace', 'level', 'text'}


def metadata(event):
    """Keep captured request/response bodies on their existing protected record pages."""
    return {key: value for key, value in event.items() if key != 'text'}


def event_view(event, query):
    """Add links and labels without rewriting recorded evidence."""
    event = metadata(event)
    what = event.get('what', '')
    failed, denied = what == 'failed', what in ('denied', 'refused')
    complete = what in ('done', 'created', 'loaded', 'signed-in')
    outcome = 'Failed' if failed else 'Denied' if denied else 'Completed' if complete else 'Activity'
    summary = next((event[key] for key in ('error', 'why', 'reason', 'message') if event.get(key)), '')
    if not isinstance(summary, str):
        summary = json.dumps(summary, ensure_ascii=False)
    request_id = event.get('of') or event.get('source') or event['id']
    return dict(event, raw=event, outcome=outcome,
                badge='red' if failed else 'orange' if denied else 'green' if complete else 'secondary',
                title=f"{event.get('kind', '')} / {what}",
                summary=summary or (f"Request #{request_id}" if event.get('kind') == 'request' else f"Event #{event['id']}"),
                href='/log?' + urlencode({**query, 'view': 'traces', 'selected': event['id']}),
                record_url=f'/requests/{request_id}' if event.get('kind') == 'request' else f"/log/{event['id']}",
                details={key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else '—' if value is None else str(value)
                         for key, value in event.items() if key not in CORE},
                request_id=request_id if event.get('kind') == 'request' or event.get('of') else None)


def workspace(events, query, selected=None, context=None, context_error=False):
    """Group only exact trace IDs; untraced events remain separate records."""
    views = [event_view(event, query) for event in events]
    selected = event_view(selected, query) if selected else None
    groups = {}
    for event in views:
        key = ('trace', event['trace']) if event.get('trace') else ('event', event['id'])
        groups.setdefault(key, []).append(event)
    choices = []
    for (kind, key), group in groups.items():
        representative = next((event for event in group if event['outcome'] == 'Failed'),
                              next((event for event in group if event['outcome'] == 'Denied'), group[0]))
        active = bool(selected and (selected.get('trace') == key if kind == 'trace' else selected['id'] == key))
        choices.append(dict(event=representative, label=str(key) if kind == 'trace' else f'Event #{key}',
                            count=len(group), active=active))
    chain = [event_view(event, query) for event in (context or ([selected['raw']] if selected else []))]
    chain.sort(key=lambda event: ((moment(event.get('at')).timestamp() if moment(event.get('at')) else 0), event['id']))
    ids = {event['id'] for event in events}
    quick_views = [dict(label=label, href='/log?' + urlencode({**{key: value for key, value in query.items() if key not in ('kind', 'what', 'before')}, **filters}))
                   for label, filters in [('All activity', {}), ('Failures', {'what': 'failed'}), ('Denied', {'what': 'denied'}), ('Requests', {'kind': 'request'}), ('Model lifecycle', {'kind': 'model'})]]
    return dict(events=views, choices=choices, selected=selected, timeline=chain, quick_views=quick_views,
                context_error=context_error, context_capped=len(chain) >= 1000,
                context_matches=sum(event['id'] in ids for event in chain),
                trace_count=sum(kind == 'trace' for kind, _ in groups),
                untraced_count=sum(kind == 'event' for kind, _ in groups),
                failed=sum(event['outcome'] == 'Failed' for event in views),
                denied=sum(event['outcome'] == 'Denied' for event in views))


def export_csv(events):
    """Export precisely the fetched, authorized result window and neutralize formulas."""
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    columns = ('id', 'at', 'machine', 'kind', 'what', 'user', 'model', 'trace', 'details')
    writer.writerow(columns)
    for event in events:
        row = {**event, 'details': json.dumps({key: value for key, value in metadata(event).items() if key not in CORE}, ensure_ascii=False)}
        writer.writerow("'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value
                        for value in (row.get(key, '') for key in columns))
    return output.getvalue()
