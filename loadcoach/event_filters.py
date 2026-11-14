"""Allowlisted search fields shared by the event API and AI Logs controls."""

IDENTITY_FIELDS = (
    ('id', 'Event ID'), ('at', 'Recorded time (UTC)'), ('node', 'Node'), ('kind', 'Kind'), ('what', 'What happened'),
    ('user', 'User'), ('caller', 'Caller'), ('model', 'Model'), ('trace', 'Trace ID'),
    ('level', 'Sensitivity'), ('source', 'Source event ID'),
)
DETAIL_FIELDS = (
    ('of', 'Parent request ID'), ('request_id', 'Client request ID'),
    ('error', 'Error'), ('why', 'Why'), ('reason', 'Reason'), ('message', 'Message'),
    ('code', 'Error code'), ('status', 'HTTP status'), ('duration', 'Duration'),
    ('seconds', 'Elapsed seconds'), ('input_tokens', 'Input tokens'), ('output_tokens', 'Output tokens'),
    ('prompt_tokens', 'Prompt tokens'), ('answer_tokens', 'Answer tokens'),
    ('tokens_per_second', 'Tokens per second'), ('context', 'Context'),
    ('engine', 'Engine'), ('tool', 'Tool'), ('service', 'Service'), ('provider', 'Provider'),
    ('hosted', 'Cloud account'), ('route', 'Route'), ('path', 'Path'), ('file', 'File'),
    ('group', 'Recorded group'), ('target', 'Target'), ('cost', 'Cost'),
    ('finish_reason', 'Finish reason'), ('fingerprint', 'Fingerprint'),
    ('reliability_outcome', 'Reliability outcome'), ('details', 'All recorded details'),
)
SEARCH_FIELDS = (('Event identity', IDENTITY_FIELDS), ('Recorded details', DETAIL_FIELDS))
FIELD_NAMES = frozenset(key for fields in (IDENTITY_FIELDS, DETAIL_FIELDS) for key, _ in fields)


def expressions(field, json_value, engine='sqlite'):
    """Build SQL exclusively from known column names or known JSON keys."""
    if field and field not in FIELD_NAMES:
        raise ValueError('Choose a supported event search field.')
    columns = {'id': 'id', 'node': "COALESCE(machine, 'front')", 'kind': 'kind', 'what': 'what',
               'user': 'user', 'caller': 'caller', 'model': 'model', 'trace': 'trace',
               'level': 'level', 'source': 'source', 'details': 'detail'}
    columns['at'] = ("strftime('%Y-%m-%dT%H:%M:%S', at, 'unixepoch')" if engine == 'sqlite' else
                     "to_char(to_timestamp(at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS')")
    if not field:
        return tuple(f'CAST({value} AS TEXT)' for value in columns.values())
    value = columns[field] if field in columns else json_value('detail', field)
    return (f'CAST({value} AS TEXT)',)
