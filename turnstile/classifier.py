"""Signed Classifier tool delegation into ordinary control node model routing."""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.classifier import Invalid, validate, MAX_BODY


def routes(delegate, routing):
    """Register the signed classifier tool operation."""
    async def classify(request):
        """Recheck caller identity and submit typed questions through normal routing."""
        from weightroom.classifier_views import bounded_json
        from weightroom.extraction import Problem
        try:
            await bounded_json(request)
            raw = await request.body()
            user, authorization, proof = delegate(request, raw)
            body = json.loads(raw)
            if body.keys() - {'arguments', 'sensitivity'}:
                raise Invalid('Unknown Classifier tool fields.')
            sent = validate(body.get('arguments'))
            headers = [(k, v) for k, v in request.scope['headers'] if k not in (b'authorization', b'content-length', b'x-loadcoach-sensitivity')]
            headers += [(b'authorization', authorization.encode()), (b'x-loadcoach-sensitivity', str(body.get('sensitivity', 'normal')).encode())]
            native = Request({**request.scope, 'path': '/v1/systemone', 'raw_path': b'/v1/systemone', 'headers': headers}, request.receive)
            native._body = json.dumps(sent).encode()
            answer = await routing.answer(native, user, '/v1/systemone', native._body)
            content = b''.join([chunk async for chunk in answer.body_iterator]) if hasattr(answer, 'body_iterator') else answer.body
            return Response(content, answer.status_code, media_type='application/json', headers=proof)
        except (Invalid, Problem) as problem:
            return JSONResponse({'error': {'message': str(problem)}}, problem.status)
    return [Route('/owg/classifier-tools/classify', classify, methods=['POST'])]
