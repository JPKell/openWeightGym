"""Keep the app-builder API brief (docs/guide/api/app-builder.md) exactly in step with the code.

The brief is meant to be handed whole to a developer or AI agent, so it may not silently drift:
its endpoint index must match the declared and proxy-dispatched client routes, each field list
must equal the validator's allowlist, and its examples must pass the real validators or gateway.
When one of these tests fails, update the brief (or the reasoned exclusion below) in the same commit.
"""

import ast
import json
from pathlib import Path
import re

from test_app import fake_llama_server, gateway, add_user, model  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / 'docs/guide/api/app-builder.md'

# Declared routes deliberately left out of the brief, each with the reason. A stale entry fails.
NOT_FOR_APPS = {
    ('POST', '/owg/classifier-tools/classify'): 'delegated ToolLocker tool dispatch, not a client API',
    ('POST', '/owg/extraction-tools/{id}'): 'delegated ToolLocker tool dispatch, not a client API',
    ('GET', '/owg/docs/abilities'): 'administrator-only generated reference',
    ('GET', '/owg/docs/endpoints'): 'administrator-only generated reference',
    ('GET', '/owg/docs/settings'): 'administrator-only generated reference',
    ('GET', '/owg/docs/source'): 'administrator-only source reference',
}


def text():
    return GUIDE.read_text(encoding='utf-8')


def normal(path):
    """Compare paths independently of each handler's parameter names and converters."""
    return re.sub(r'\{[^}]+\}', '{id}', path)


def function(relative, name):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    return next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def literal_set(node, contains=None):
    """The first string set literal under `node`, optionally one that holds `contains`."""
    for one in ast.walk(node):
        if isinstance(one, ast.Set) and all(isinstance(e, ast.Constant) for e in one.elts):
            values = {e.value for e in one.elts}
            if contains is None or contains in values:
                return values
    raise AssertionError('no literal set found')


def constant(relative, name):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} is not a constant in {relative}')


def code_fields():
    """Each field list in the brief, read from its validator's allowlist."""
    soundroom = 'soundroom/requests.py'
    return {
        'Image': literal_set(function('mirrorwall/requests.py', 'checked_request'), 'prompt'),
        'Video': literal_set(function('mirrorwall/video.py', 'checked_request'), 'prompt'),
        'Speech': set(constant(soundroom, 'SPEECH_FIELDS')),
        'Transcription': set(constant(soundroom, 'TRANSCRIPTION_FIELDS')),
        'Music and sound': set(constant(soundroom, 'GENERATION_FIELDS')),
        'Script': set(constant(soundroom, 'SCRIPT_FIELDS')),
        'Rerank': literal_set(function('loadcoach/rerank.py', 'checked'), 'query'),
        'Classifier': literal_set(function('loadcoach/classifier.py', 'validate'), 'questions'),
        'Batch': literal_set(function('turnstile/batches.py', 'batch_start'), 'input_file_id'),
        'Extraction': literal_set(function('weightroom/extraction.py', 'specification'), 'asset_id'),
        'Batch endpoints': set(constant('loadcoach/batches.py', 'ENDPOINTS')),
    }


def documented_fields():
    found = {}
    for label, kind, names in re.findall(r'^\*\*(.+?) (fields|endpoints):\*\* (`.*)$', text(), re.M):
        label = label if kind == 'fields' else label + ' endpoints'
        assert label not in found, f'{label} is listed twice'
        found[label] = set(re.findall(r'`([^`]+)`', names))
    return found


def code_routes():
    """Client-facing routes: declared /v1 and /owg routes, plus paths the gateways dispatch by hand."""
    from weightroom import system_docs
    declared = {(row['method'], normal(row['path']))
                for group in system_docs.api_reference(system_docs.source_files('api')) for row in group['rows']
                if row['path'].startswith(('/v1/', '/owg/'))}
    # Dispatched inside Turnstile's pass_on or LoadCoach's /v1/{path:path} proxy, so absent from declarations.
    turnstile = (ROOT / 'turnstile/app.py').read_text(encoding='utf-8')
    loadcoach = (ROOT / 'loadcoach/app.py').read_text(encoding='utf-8')
    assert 'path == "/v1/workflows"' in turnstile and "'/v1/audio/translations'" in turnstile
    assert "request.url.path == '/v1/rerank'" in loadcoach
    dispatched = {('GET', '/v1/workflows'), ('POST', '/v1/audio/translations')}
    dispatched |= {('POST', path) for path in constant('loadcoach/batches.py', 'ENDPOINTS')}
    dispatched |= {('POST', path) for path in constant('loadcoach/classifier.py', 'NATIVE_PATHS')}
    return declared, declared | dispatched


def documented_routes():
    rows = re.findall(r'^\| `(GET|POST|PATCH|PUT|DELETE) (/\S+)` \|', text(), re.M)
    assert len(rows) == len(set(rows)), 'an endpoint is listed twice'
    return {(method, normal(path)) for method, path in rows}


def examples():
    return [json.loads(block) for block in re.findall(r'```json\n(.*?)```', text(), re.S)]


def test_endpoint_index_matches_every_client_route():
    declared, routes = code_routes()
    stale = set(NOT_FOR_APPS) - declared
    assert not stale, f'remove exclusions for routes that no longer exist: {sorted(stale)}'
    wanted, listed = routes - set(NOT_FOR_APPS), documented_routes()
    assert not wanted - listed, ('add these routes to the brief\'s endpoint index, or to NOT_FOR_APPS with a reason: '
                                 f'{sorted(wanted - listed)}')
    assert not listed - wanted, f'the brief lists routes the code no longer has: {sorted(listed - wanted)}'


def test_field_lists_equal_validator_allowlists():
    code, written = code_fields(), documented_fields()
    assert written.keys() == code.keys(), f'field lists without a code source: {written.keys() ^ code.keys()}'
    for label, fields in code.items():
        assert written[label] == fields, (f'{label}: add {sorted(fields - written[label])}, '
                                          f'remove {sorted(written[label] - fields)}')


def test_examples_are_json_and_pass_the_real_validators():
    from loadcoach.classifier import validate
    from loadcoach.rerank import checked
    from mirrorwall.requests import checked_request
    from weightroom.extraction import specification
    bodies = examples()
    assert len(bodies) >= 10
    by = lambda key: [body for body in bodies if key in body]  # noqa: E731
    checked(by('documents')[0])
    validate(by('questions')[0])
    for body in by('prompt'):
        if 'size' in body and 'seconds' not in body:
            checked_request(body)
    specification(by('asset_id')[0])
    assert set(by('input_file_id')[0]) <= code_fields()['Batch']
    assert by('input_file_id')[0]['endpoint'] in code_fields()['Batch endpoints']


def test_model_examples_run_through_the_gateway(fake_llama_server, tmp_path, monkeypatch):
    bodies = examples()
    by = lambda key: next(body for body in bodies if key in body and 'model' in body)  # noqa: E731
    monkeypatch.chdir(tmp_path)
    models = {'chat': model('chat.gguf', **{'docs-examples': True}), 'embed': model('e.gguf', embedding=True),
              'rank': model('r.gguf', reranking=True)}
    with gateway(fake_llama_server, models=models) as caller:
        headers = {**add_user(caller, 'app-builder', 'admin'), 'x-suite-application': 'my-app'}
        listed = caller.get('http://front:8442/v1/models', headers=headers).json()
        assert {'object', 'data', 'available_machines', 'unavailable_machines'} <= listed.keys()
        assert all({'id', 'capabilities', 'machines', 'locations'} <= one.keys() for one in listed['data'])
        embedder = next(one for one in listed['data'] if one['id'] == 'embed')
        assert embedder['embedding'] is True and embedder['context_tokens'] > 0
        assert not {'embedding', 'rerank'} & next(one for one in listed['data'] if one['id'] == 'chat').keys()
        assert next(one for one in listed['data'] if one['id'] == 'rank')['rerank'] is True
        missing = caller.get('http://front:8442/v1/models/nope', headers=headers)
        assert missing.status_code == 404 and missing.json()['error']['code'] == 'model_not_found'
        for path, key, name in (('/v1/chat/completions', 'messages', 'chat'), ('/v1/responses', 'store', 'chat'),
                                ('/v1/embeddings', 'encoding_format', 'embed'), ('/v1/rerank', 'documents', 'rank')):
            response = caller.post('http://front:8442' + path, json={**by(key), 'model': name}, headers=headers)
            assert response.status_code == 200, (path, response.text)
            assert response.headers['x-request-id'] and response.headers['x-loadcoach-request-id']
        assert caller.get('http://front:8442/v1/workflows', headers=headers).json() == {'object': 'list', 'data': []}
        refused = caller.get('http://front:8442/v1/models', headers={'authorization': 'Bearer wrong'})
        assert refused.status_code == 401 and refused.json()['error']['message'] == 'Missing or wrong API key.'
