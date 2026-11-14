"""Session-authenticated, front-local stored Responses ledger and record pages."""

from datetime import datetime, UTC
from types import SimpleNamespace
from urllib.parse import parse_qs

from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from turnstile.extractions import PRIVATE
from weightroom.extraction import Problem
from weightroom.pagination import controls, pagination


def reader(person):
    """Use current session identity and effective account permissions for store access."""
    return SimpleNamespace(name=person.name, identity=person.me.get('identity') or person.name,
                           group=SimpleNamespace(can=frozenset(person.can), max_sensitivity=person.me.get('max_sensitivity', 'normal')))


def routes(for_people, page, store):
    """Register console-only listing, record, export and confirmed-delete routes."""
    async def listing(request, person):
        """Apply native GET filters and SQL pagination within the reader's current scope."""
        user = reader(person)
        filters = {key: request.query_params.get(key, '')[:1000] for key in ('user', 'group', 'model', 'status', 'q', 'since', 'until')}
        filters['scope'] = request.query_params.get('scope', 'own')
        displayed = dict(filters)
        groups, user_groups = [], {}
        try:
            if 'see-log' in user.group.can:
                memberships = store.db.execute('SELECT name,grp FROM users ORDER BY name').fetchall()
                user_groups = dict(memberships)
                groups = sorted(set(user_groups.values()))
                if filters['group']:
                    filters['owners'] = [name for name, group in memberships if group == filters['group']]
            for key in ('since', 'until'):
                if filters[key]:
                    try:
                        filters[key] = datetime.fromisoformat(filters[key]).replace(tzinfo=UTC).timestamp()
                    except ValueError:
                        raise Problem('Choose a valid UTC date and time.') from None
            limit, current = controls(request.query_params)
            records, total = store.listing(user, filters, limit=limit, offset=(current-1)*limit)
            return page(request, 'responses.html', person, records=records, filters=displayed,
                        groups=groups, user_groups=user_groups,
                        paging=pagination('/responses', request.query_params, total, limit, current),
                        target='page-content', said=request.query_params.get('said', '')[:100],
                        input_tokens=sum(row['response']['usage']['input_tokens'] for row in records),
                        output_tokens=sum(row['response']['usage']['output_tokens'] for row in records))
        except Problem as error:
            return page(request, 'refused.html', person, status=error.status, why=str(error))

    async def detail(request, person):
        """Read or delete a stored object with the same authorization as the API."""
        user, identifier = reader(person), request.path_params['identifier']
        try:
            if request.method == 'POST':
                fields = parse_qs((await request.body()).decode())
                if fields.get('do') != ['delete']:
                    raise Problem('Choose Delete to remove this response.')
                store.remove(user, identifier)
                return RedirectResponse('/responses?said=Response+deleted.', status_code=303)
            record = store.one(user, identifier)
            if request.query_params.get('format') == 'json':
                return JSONResponse(record['response'], headers={**PRIVATE,
                    'content-disposition': 'attachment; filename="response.json"'})
            previous = None
            if record['previous']:
                try:
                    previous = store.one(user, record['previous'])
                except Problem:
                    pass
            return page(request, 'response.html', person, record=record, previous=previous,
                        following=store.next(user, identifier),
                        can_delete=record['owner'] == (user.identity or user.name) or 'manage-users' in user.group.can)
        except Problem as error:
            return page(request, 'refused.html', person, status=error.status, why=str(error))

    return [Route('/responses', for_people(listing)),
            Route('/responses/{identifier}', for_people(detail), methods=['GET', 'POST'])]
