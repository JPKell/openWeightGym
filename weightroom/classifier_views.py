"""Session-authenticated Classifier workspace using normal front inference and downloads."""

import json
from urllib.parse import quote

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.classifier import MAX_BODY, Invalid, validate, UNCALIBRATED

STARTER_MODELS = (
    ('ggml-org/Julia-1-GGUF', 'Julia-1-Q8_0.gguf'),
    ('ggml-org/Laya-GGUF', 'Laya-Q8_0.gguf'),
    ('ggml-org/Kev-0.8B-GGUF', 'Kev-0.8B-Q8_0.gguf'),
    ('ggml-org/Kev-4B-GGUF', 'Kev-4B-Q4_K_M.gguf'),
    ('ggml-org/lev-GGUF', 'lev-Q4_K_M.gguf'),
    ('ggml-org/Kev-9B-GGUF', 'Kev-9B-Q4_K_M.gguf'),
)
PRIVATE = {'cache-control': 'no-store'}


async def bounded_json(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise Invalid('Classifier request exceeds the 64 KiB body limit.')
    try:
        body = json.loads(raw)
    except ValueError:
        raise Invalid('Send a JSON object.') from None
    if not isinstance(body, dict):
        raise Invalid('Send a JSON object.')
    request._body = bytes(raw)
    return body


def routes(for_people, page, ask, platform):
    """Expose only the model catalogue, classification and starter-set operations."""
    async def workspace(request, person):
        return page(request, 'classifier.html', person, uncalibrated=UNCALIBRATED)

    async def api(request, person):
        origin = request.headers.get('origin')
        if request.method == 'POST' and origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse({'error': {'message': 'Submit this request from the console.'}}, 403, headers=PRIVATE)
        action = request.path_params['action']
        try:
            if action == 'models' and request.method == 'GET':
                answer = await ask('GET', '/v1/models', person.key)
                if not answer.is_success:
                    return Response(answer.content, answer.status_code, media_type='application/json', headers=PRIVATE)
                models = [m for m in answer.json()['data'] if m.get('capabilities', {}).get('classifier')]
                nodes = await ask('GET', '/turnstile/machines', person.key)
                return JSONResponse({'models': models, 'nodes': nodes.json().get('machines', []) if nodes.is_success else [],
                                     'downloads': platform().downloads, 'manage': 'manage-models' in person.can}, headers=PRIVATE)
            if action == 'run' and request.method == 'POST':
                body = validate(await bounded_json(request))
                answer = await ask('POST', '/v1/systemone', person.key, json=body, timeout=None)
                return Response(answer.content, answer.status_code, media_type='application/json', headers=PRIVATE)
            if action == 'starter' and request.method == 'POST':
                if 'manage-models' not in person.can:
                    raise Invalid('Model downloads require manage-models.', 403)
                if not platform().downloads:
                    raise Invalid('Model downloads are disabled in Platform services.', 403)
                body = await bounded_json(request)
                node = body.get('node')
                if not isinstance(node, str) or not node:
                    raise Invalid('Choose one GPU compute node for the starter set.')
                nodes = await ask('GET', '/turnstile/machines', person.key)
                if not nodes.is_success or not any(n['name'] == node and n.get('accepting_work', True) for n in nodes.json().get('machines', [])):
                    raise Invalid('Choose an available compute node.')
                queued, errors = [], []
                for repository, file in STARTER_MODELS:
                    answer = await ask('POST', '/turnstile/artifacts/downloads', person.key,
                                       json={'repository': repository, 'files': [file], 'place': 'storage_classifier'}, timeout=60)
                    if answer.status_code == 202:
                        queued.append({'repository': repository, 'file': file, **answer.json()})
                    else:
                        errors.append({'repository': repository, 'message': answer.json().get('error', {}).get('message', 'Download refused.')})
                return JSONResponse({'queued': queued, 'errors': errors}, 202 if queued else 400, headers=PRIVATE)
            if action == 'install-starters' and request.method == 'POST':
                if 'manage-models' not in person.can:
                    raise Invalid('Model installation requires manage-models.', 403)
                body = await bounded_json(request)
                node = body.get('node')
                inventory = await ask('GET', '/turnstile/models/files', person.key)
                if not inventory.is_success:
                    return Response(inventory.content, inventory.status_code, media_type='application/json', headers=PRIVATE)
                files = [{**f, 'source_node': n['name']} for n in inventory.json().get('nodes', []) for f in n.get('files', [])]
                installed, pending = [], []
                for repository, filename in STARTER_MODELS:
                    file = next((f for f in files if f['path'].endswith('/' + repository.replace('/', '--') + '/' + filename)
                                 and f.get('facts', {}).get('what') == 'classifier'), None)
                    if not file:
                        pending.append(filename)
                        continue
                    budget = min(file['facts'].get('trained_context') or 8192, 8192)
                    answer = await ask('POST', '/turnstile/models/install', person.key, json={
                        'name': filename.removesuffix('.gguf').lower(), 'machines': [node], 'source_machine': file['source_node'],
                        'settings': {'engine': 'llama_cpp', 'gpus': 'any', 'flags': {'model': file['path'],
                                     'ctx-size': budget}}, 'prepare_files': True}, timeout=60)
                    installed.append({'file': filename, 'status': answer.status_code, 'result': answer.json()})
                return JSONResponse({'installations': installed, 'pending': pending}, headers=PRIVATE)
        except Invalid as problem:
            return JSONResponse({'error': {'message': str(problem)}}, problem.status, headers=PRIVATE)
        return JSONResponse({'error': {'message': 'Unknown Classifier operation.'}}, 404, headers=PRIVATE)

    return [Route('/chat/classifier', for_people(workspace)),
            Route('/chat/classifier/api/{action}', for_people(api), methods=['GET', 'POST'])]
