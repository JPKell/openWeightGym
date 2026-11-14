"""Browser page and bounded session-authenticated proxy for control-node-owned extraction APIs."""

import re

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from turnstile.extractions import bounded_body, PRIVATE
from weightroom.extraction import MAX_BODY, Problem


def routes(for_people, page, ask):
    """Register the session-authenticated workspace and bounded API proxy."""
    async def workspace(request, person):
        """Render the extraction desk using the shared console shell."""
        return page(request, 'extraction.html', person)

    async def api(request, person):
        """Proxy only allowlisted extraction operations under this session."""
        origin = request.headers.get('origin')
        if request.method != 'GET' and origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse({'error': {'message': 'This request must come from the console page.'}}, 403, headers=PRIVATE)
        path = request.path_params['path']
        first, _, suffix = path.partition('/')
        roots = {'models': '/owg/extraction-models', 'assets': '/owg/extraction-assets',
                 'jobs': '/owg/extractions', 'presets': '/owg/extraction-presets'}
        valid = not suffix
        if first == 'assets' and re.fullmatch(r'exa_[0-9a-f]{32}', suffix):
            valid = True
        if first == 'jobs' and re.fullmatch(r'ext_[0-9a-f]{32}(?:/(?:cancel|review|export))?', suffix):
            valid = True
        if first == 'presets' and re.fullmatch(r'exp_[0-9a-f]{32}', suffix):
            valid = True
        if first not in roots or not valid:
            return JSONResponse({'error': {'message': 'Unknown extraction operation.'}}, 404, headers=PRIVATE)
        target = roots[first] + ('/' + suffix if suffix else '')
        try:
            body = await bounded_body(request, MAX_BODY)
        except Problem as error:
            return JSONResponse({'error': {'message': str(error)}}, error.status, headers=PRIVATE)
        answer = await ask(request.method, target, person.key, content=body,
            headers={key: value for key, value in request.headers.items() if key in ('content-type', 'idempotency-key')},
            params=list(request.query_params.multi_items()), timeout=30)
        return Response(answer.content, answer.status_code,
                        headers={**PRIVATE, **{key: value for key, value in answer.headers.items() if key in ('content-type', 'content-disposition')}})

    return [Route('/chat/extraction', for_people(workspace)),
            Route('/chat/extraction/api/{path:path}', for_people(api), methods=['GET', 'POST', 'PATCH', 'DELETE'])]
