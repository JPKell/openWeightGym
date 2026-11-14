"""Presentation of authorized reliability evidence; routing scores remain authoritative."""

import csv
import io
import json
from urllib.parse import urlencode

FILTERS = ('model', 'role', 'issues', 'q', 'fingerprints', 'sort')


def faults(row):
    return sum(row['counts'].get(kind, 0) for kind in ('error', 'timeout', 'empty'))


def workspace(report, quality, asked):
    """Filter only fetched, authorized rows and keep lifetime ratings independent of roles."""
    offline = report.get('compute_unavailable', False)
    fingerprints = asked.get('fingerprints', 'current')
    if fingerprints not in ('current', 'all', 'historical'):
        fingerprints = 'current'
    sort = asked.get('sort', 'attention')
    if sort not in ('attention', 'rate', 'faults', 'model'):
        sort = 'attention'
    filters = {key: asked.get(key, '').strip() for key in FILTERS}
    filters.update(fingerprints=fingerprints, sort=sort)
    query = filters['q'].casefold()

    def fingerprint_matches(row):
        # Compute cannot identify current fingerprints during an outage.
        return offline or fingerprints == 'all' or row['current'] == (fingerprints == 'current')

    rows = [dict(row, faults=faults(row), scored=sum(row['counts'].values())) for row in report['rows']
            if fingerprint_matches(row) and (not filters['model'] or row['model'] == filters['model'])
            and (not filters['role'] or row['role'] == filters['role'])
            and (filters['issues'] != '1' or faults(row))
            and (not query or query in f"{row['model']} {row['role'] or 'direct'} {row['fingerprint']}".casefold())]
    def priority(row):
        return (100 if row['state'] != 'eligible' else 0) + (50 if row['regression'] else 0) + row['faults']
    rows.sort(key=lambda row: (row['model'].casefold(), row['role'], row['fingerprint']) if sort == 'model' else
              (row['success_rate'] if row['success_rate'] is not None else 101, row['model']) if sort == 'rate' else
              (-row['faults'], row['model']) if sort == 'faults' else (-priority(row), row['model']))
    scored = sum(row['scored'] for row in rows)
    counts = {kind: sum(row['counts'].get(kind, 0) for row in rows) for kind in ('success', 'error', 'timeout', 'empty')}
    summary = dict(counts=counts, scored=scored, success_rate=100 * counts['success'] / scored if scored else None,
                   faults=sum(counts[kind] for kind in ('error', 'timeout', 'empty')),
                   excluded=None if offline else sum(row['current'] and row['state'] != 'eligible' for row in rows),
                   regressions=None if offline else sum(row['current'] and row['regression'] for row in rows))
    ratings = [row for row in quality if fingerprint_matches(row) and (not filters['model'] or row['model'] == filters['model'])]
    for index, row in enumerate(rows):
        row['detail_id'] = f'reliability-candidate-{index}'
    return dict(rows=rows, quality=ratings, summary=summary, filters=filters,
                attention=[row for row in rows if (row['current'] or offline) and (row['state'] != 'eligible' or row['regression'])],
                export_url='/reliability?' + urlencode({**filters, 'export': 'csv'}))


def export_csv(rows, offline=False):
    """Export precisely the displayed evidence, neutralizing spreadsheet formulas."""
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    columns = ('model', 'fingerprint', 'current', 'role', 'success', 'error', 'timeout', 'empty',
               'success_rate', 'positive', 'negative', 'factor', 'state', 'regression', 'reason',
               'ignored', 'last_request', 'last_at')
    writer.writerow(columns)
    for row in rows:
        values = {**row, **row['counts'], 'ignored': json.dumps(row.get('ignored', {}), ensure_ascii=False)}
        if offline:
            values.update(current='unknown', factor='unavailable', state='unavailable')
        writer.writerow("'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value
                        for value in (values.get(key) for key in columns))
    return output.getvalue()
