"""Presentation and bookmarkable filters for the authorized batch inventory."""

import csv
import io
from urllib.parse import urlencode


def workspace(batches, person, params):
    """Filter only the batches already authorized by the control node's API."""
    filters = {key: params.get(key, '') for key in ('q', 'status', 'owner', 'sort')}
    filters['sort'] = filters['sort'] if filters['sort'] in ('oldest', 'errors', 'progress') else 'newest'
    owners = sorted({batch['user'] for batch in batches})
    query = filters['q'].strip().casefold()
    def title(batch):
        return str((batch.get('metadata') or {}).get('name') or batch['id'])
    def processed(batch):
        counts = batch['request_counts']
        return counts['completed'] + counts['failed']
    selected = [batch for batch in batches if
                (not query or query in ' '.join((title(batch), batch['id'], batch['endpoint'], batch['user'])).casefold()) and
                (not filters['owner'] or batch['user'] == filters['owner']) and
                (not filters['status'] or batch['status'] == filters['status'] or
                 filters['status'] == 'errors' and batch['request_counts']['failed'] > 0)]
    order = {'newest': lambda b: -b['created_at'], 'oldest': lambda b: b['created_at'],
             'errors': lambda b: (-b['request_counts']['failed'], -b['created_at']),
             'progress': lambda b: (-processed(b) / (b['request_counts']['total'] or 1), -b['created_at'])}
    selected.sort(key=order[filters['sort']])
    query_params = {key: value for key, value in filters.items() if value}
    overview = dict(active=sum(b['status'] in ('in_progress', 'paused', 'cancelling') for b in batches),
                    processed=sum(processed(b) for b in batches), total=sum(b['request_counts']['total'] for b in batches),
                    errors=sum(b['request_counts']['failed'] > 0 for b in batches),
                    ready=sum(b['user'] == person.name and bool(b.get('output_file_id')) for b in batches))
    return dict(batches=selected, batch_filters=filters, batch_owners=owners, batch_overview=overview,
                batch_count=len(batches), batch_export='/batches?' + urlencode({**query_params, 'export': 'csv'}),
                batch_can_stop=any(b['status'] == 'in_progress' and (b['user'] == person.name or 'stop' in person.can) for b in batches))


def export_csv(batches):
    """Export all matching authorized rows, independent of the current page."""
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(('id', 'name', 'user', 'status', 'endpoint', 'total', 'completed', 'failed', 'created_at'))
    for batch in batches:
        values = [batch['id'], (batch.get('metadata') or {}).get('name', ''), batch['user'], batch['status'],
                  batch['endpoint'], *(batch['request_counts'][key] for key in ('total', 'completed', 'failed')), batch['created_at']]
        writer.writerow("'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value for value in values)
    return output.getvalue()
