"""Presentation of immutable fingerprint snapshots; never consult current model settings."""

from urllib.parse import parse_qsl, urlencode, urlsplit


# What a return link keeps of the archive's view: its filters, and the rows, page and sort the browser chose (QA-028).
VIEW = ('start', 'model', 'context', 'view', 'rows', 'page', 'sort')


def directory_url(value: str) -> str:
    """Keep the record's return link within this archive, without nested return links."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return '/fingerprints'
    if parsed.scheme or parsed.netloc or parsed.path != '/fingerprints':
        return '/fingerprints'
    query = {key: item for key, item in parse_qsl(parsed.query)
             if key in VIEW}
    query['view'] = 'directory'
    return '/fingerprints?' + urlencode(query)


def fingerprint_context(records: list[dict], params) -> dict:
    """Filter archived records, retaining legacy unique-prefix detail links."""
    start = params.get('start', '').strip()
    model = params.get('model', '')
    context = params.get('context', '')
    query = start.casefold()
    found = [one for one in records if
             (not query or one['fingerprint'].casefold().startswith(query) or
              query in (one.get('model') or '').casefold() or
              any(query in file.get('path', '').casefold() for file in one.get('files', {}).values())) and
             (not model or one.get('model') == model) and
             (not context or str(one.get('settings', {}).get('flags', {}).get('ctx-size', '')) == context)]
    # Each node keeps a fingerprint once, so a record is a fingerprint on a node; the same snapshot on several nodes is
    # several records, and a link names the node to open one of them (QA-027).
    node = params.get('node', '')
    exact = [record for record in found if not node or record.get('node', '') == node]
    one = exact[0] if len(exact) == 1 and start and params.get('view') != 'directory' else None
    peek, peek_node = params.get('peek', ''), params.get('peek_node', '')
    selected = next((record for record in records if record['fingerprint'] == peek and (not peek_node or record.get('node', '') == peek_node)),
                    None) if peek else (found[0] if found else None)
    current = '/fingerprints?' + urlencode({key: value for key, value in
        [('start', start), ('model', model), ('context', context), ('view', 'directory'),
         *((key, params.get(key, '')) for key in ('rows', 'page', 'sort'))] if value})
    back = directory_url(params.get('return', '/fingerprints'))

    def record_url(record):
        """Build a fingerprint selection link retaining its safe return destination."""
        return '/fingerprints?' + urlencode({'start': record['fingerprint'], **({'node': record['node']} if record.get('node') else {}),
                                             'return': back if peek else current})

    files = {file.get('sha256') or file.get('path') for record in records for file in record.get('files', {}).values()}
    contexts = {str(record.get('settings', {}).get('flags', {}).get('ctx-size', '')) for record in records}
    return dict(found=found, one=one, selected=selected, start=start, model_filter=model,
                context_filter=context, model_names=sorted({record.get('model') or '' for record in records} - {''}),
                contexts=sorted(contexts - {''}, key=lambda value: int(value) if value.isdigit() else 0),
                archive_count=len(records), archive_models=len({record.get('model') for record in records}),
                archive_files=len(files), latest_seen=max((record['first'] for record in records), default=None),
                back=back, record_url=record_url, peek_requested=bool(peek), directory=current)
