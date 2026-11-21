"""Keep the app-builder API brief (docs/guide/api/app-builder.md) exactly in step with the code.

The brief is meant to be handed whole to a developer or AI agent, so it may not silently drift:
its endpoint index must match the declared and proxy-dispatched client routes, each field list
must equal the validator's allowlist, every header and query parameter the code reads must be
named, and its examples must pass the real validators or gateway.
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


def literal_set(node, contains=None, kind=ast.Set):
    """The first string set (or `kind`) literal under `node`, optionally one that holds `contains`."""
    for one in ast.walk(node):
        if isinstance(one, kind) and one.elts and all(isinstance(e, ast.Constant) for e in one.elts):
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


def subscripts(node, name):
    """String keys read as `name["key"]` under `node`: the fields of a parsed form."""
    return {one.slice.value for one in ast.walk(node) if isinstance(one, ast.Subscript) and isinstance(one.value, ast.Name)
            and one.value.id == name and isinstance(one.slice, ast.Constant)}


def code_fields():
    """Each field list in the brief, read from its validator's allowlist."""
    soundroom = 'soundroom/requests.py'
    responses, images = 'turnstile/responses.py', 'mirrorwall/requests.py'
    inputs = function(responses, 'append_input')
    advanced = function(images, 'checked_advanced')
    classifier, shim = function('loadcoach/classifier.py', 'validate'), function('loadcoach/classifier.py', 'shim_request')
    return {
        'Strata chat': literal_set(function('engines/strata/runtime.py', 'checked_request'), 'min_p'),
        'Agent chat': literal_set(function('plugins/_runtime.py', 'chat'), 'reasoning_effort', ast.Tuple),
        'Responses': literal_set(function(responses, 'chat_request'), 'previous_response_id'),
        'Responses message item': literal_set(inputs, 'role'),
        'Responses image part': literal_set(inputs, 'image_url'),
        'Responses function_call item': literal_set(inputs, 'arguments'),
        'Responses function_call_output item': literal_set(inputs, 'output'),
        'Responses mcp_approval_response item': literal_set(function('turnstile/response_tools.py', 'prepare'), 'approval_request_id'),
        'Responses text format': literal_set(function(responses, 'chat_request'), 'schema'),
        'Responses function tool': literal_set(function(responses, 'function_tools'), 'parameters'),
        'Responses MCP tool': literal_set(function('turnstile/tool_loop.py', 'configuration'), 'server_label'),
        'JSON Schema keywords': literal_set(function('weightroom/extraction.py', 'schema_checked'), 'pattern'),
        'Classifier question': literal_set(classifier, 'criteria'),
        'Classifier noul criteria': literal_set(classifier, 'true'),
        'Classifier chat': literal_set(shim, 'top_logprobs'),
        'Classifier schema': literal_set(shim, 'additionalProperties'),
        'Classifier property': literal_set(shim, 'x-levels'),
        'Image advanced': literal_set(advanced, 'clip_skip'),
        'Image slg': literal_set(advanced, 'layer_start'),
        'Image hires': literal_set(advanced, 'denoising_strength'),
        'Image control': literal_set(function(images, 'checked_control'), 'edges'),
        'Image upscale_with': literal_set(function(images, 'checked_upscale_with'), 'tile'),
        'Workflow call': set(constant('mirrorwall/graph_run.py', 'CALLER_FIELDS')),
        'Script line': set(constant(soundroom, 'LINE_FIELDS')),
        'Video poster': literal_set(function('mirrorwall/video_api.py', 'poster'), 'b64_json'),
        'File upload form': subscripts(function('turnstile/batches.py', 'file_upload'), 'form'),
        'Extraction preset': literal_set(function('turnstile/extractions.py', 'presets'), 'spec'),
        'Extraction review': literal_set(function('turnstile/extractions.py', 'review'), 'reviewed'),
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
    for label, kind, names in re.findall(r'^\*\*(.+?) (fields|endpoints|keywords):\*\* (`.*)$', text(), re.M):
        label = label if kind == 'fields' else f'{label} {kind}'
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


def section(anchor):
    """The text of one H2 section of the brief, by its anchor."""
    found = re.search(r'^## .*\{#' + anchor + r'\}\n(.*?)(?=^## )', text(), re.M | re.S)
    assert found, f'the brief has no #{anchor} section'
    return found.group(1)


ROUTE_ROW = r'^\| `(GET|POST|PATCH|PUT|DELETE) (/\S+)` \|'


def documented_routes():
    rows = re.findall(ROUTE_ROW, section('app-endpoints'), re.M)
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
        assert caller.get('http://front:8442/v1/models/nope', headers=headers).status_code == 404
        unknown = caller.post('http://front:8442/v1/chat/completions', json={**by('messages'), 'model': 'nope'}, headers=headers)
        assert unknown.status_code == 404 and unknown.json()['error']['code'] == 'model_not_found'
        for path, key, name in (('/v1/chat/completions', 'messages', 'chat'), ('/v1/responses', 'store', 'chat'),
                                ('/v1/embeddings', 'encoding_format', 'embed'), ('/v1/rerank', 'documents', 'rank')):
            response = caller.post('http://front:8442' + path, json={**by(key), 'model': name}, headers=headers)
            assert response.status_code == 200, (path, response.text)
            assert response.headers['x-request-id'] and response.headers['x-loadcoach-request-id']
            if path == '/v1/chat/completions':  # while it is loaded: each model here takes both cards
                assert response.headers['x-loadcoach-context-tokens'] == '2048'
                chat = next(one for one in caller.get('http://front:8442/v1/models', headers=headers).json()['data'] if one['id'] == 'chat')
                assert chat['context'] == {'guaranteed': 2048, 'max': 2048} and 'loaded_context' not in chat
                assert chat['locations'][0]['loaded_context'] == 2048
        assert caller.get('http://front:8442/v1/workflows', headers=headers).json() == {'object': 'list', 'data': []}
        refused = caller.get('http://front:8442/v1/models', headers={'authorization': 'Bearer wrong'})
        assert refused.status_code == 401 and refused.json()['error']['message'] == 'Missing or wrong API key.'


def test_thinking_section_matches_the_code():
    """Each claim of the Thinking section rests on one source fact; this fails when one changes."""
    written = text()
    assert '"chat_template_kwargs": {"enable_thinking": false}' in written and '"reasoning_effort"' in written
    # Responses builds its chat request from an allowlist without `reasoning`, so the brief says it is refused.
    assert 'reasoning' not in literal_set(function('turnstile/responses.py', 'chat_request'), 'model')
    assert '`reasoning` output item' in written and 'reasoning_tokens' in written
    responses = (ROOT / 'turnstile/responses.py').read_text(encoding='utf-8')
    assert "message.get('reasoning_content')" in responses and "'reasoning_tokens'" in responses
    # Strata accepts only disabled thinking; llama.cpp passes both fields through and reports template capabilities.
    assert 'sent.pop("chat_template_kwargs") != {"enable_thinking": False}' in (ROOT / 'engines/strata/adapter.py').read_text(encoding='utf-8')
    assert '"chat_template_caps"' in (ROOT / 'turnstile/machines.py').read_text(encoding='utf-8')  # carried per location
    assert 'reasoning_effort_levels' in (ROOT / 'engines/llama_cpp/adapter.py').read_text(encoding='utf-8')
    for flag in ('reasoning', 'reasoning-effort', 'reasoning-budget', 'reasoning-format', 'chat-template-kwargs'):
        assert f'`{flag}`' in written and f'"{flag}"' in (ROOT / 'turnstile/model_configuration.py').read_text(encoding='utf-8')


def test_thinking_example_is_accepted_by_the_gateway(fake_llama_server, tmp_path, monkeypatch):
    body = next(body for body in examples() if 'chat_template_kwargs' in body)
    monkeypatch.chdir(tmp_path)
    with gateway(fake_llama_server, models={'chat': model('chat.gguf')}) as caller:
        headers = add_user(caller, 'thinker', 'admin')
        answer = caller.post('http://front:8442/v1/chat/completions', json={**body, 'model': 'chat'}, headers=headers)
        assert answer.status_code == 200, answer.text
        assert json.loads(answer.json()['body'])['chat_template_kwargs'] == {'enable_thinking': False}  # reached the engine unchanged
        refused = caller.post('http://front:8442/v1/responses', json={'model': 'chat', 'input': 'hi', 'reasoning': {'effort': 'low'}}, headers=headers)
        assert refused.status_code == 400 and refused.json()['error']['code'] == 'unsupported_parameter'


PACKAGES = ('turnstile', 'loadcoach', 'mirrorwall', 'soundroom', 'weightroom', 'toollocker', 'voice', 'plugins', 'engines')

# Ordinary headers the code reads from requests that an app does not send to the API, each with the reason.
NOT_APP_HEADERS = {
    'accept': 'the console chooses HTML or a fragment; API answers are always JSON or the file',
    'cookie': 'console sign-in; the API takes a bearer key',
    'host': 'console and media-preview address checks',
    'hx-current-url': 'console HTMX navigation',
    'hx-request': 'console HTMX navigation',
    'if-none-match': 'the voice gateway caches its own prompt recordings',
    'origin': 'browser cross-origin checks on console and voice pages',
    'referer': 'console return links',
}


def trees():
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob('*.py')):
            yield path, ast.parse(path.read_text(encoding='utf-8'))


def code_headers():
    """Every `x-…` header name written in the services, and the other header names read from requests."""
    custom, standard = set(), set()
    for _, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                value = node.value.decode('latin-1') if isinstance(node.value, bytes) else node.value
                if re.fullmatch(r'[xX]-[A-Za-z0-9-]*[A-Za-z0-9]', value):
                    custom.add(value.lower())
            read = node.func if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ('get', 'getlist') else None
            target = read.value if read else node.value if isinstance(node, ast.Subscript) else None
            key = (node.args[0] if read and node.args else None) if read else node.slice if isinstance(node, ast.Subscript) else None
            if (isinstance(target, ast.Attribute) and target.attr == 'headers' and isinstance(target.value, ast.Name)
                    and target.value.id in ('request', 'websocket') and isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and not key.value.lower().startswith('x-')):
                standard.add(key.value.lower())
    # The identity guards are read as 'x-loadcoach-require-' + suffix, so their full names are not literals.
    assert "'x-loadcoach-require-'+header" in (ROOT / 'loadcoach/app.py').read_text(encoding='utf-8')
    custom |= {'x-loadcoach-require-' + suffix for suffix in ('model-id', 'fingerprint', 'build')}
    return custom, standard


def named(page, header):
    return re.search(r'(?<![a-z0-9-])' + re.escape(header) + r'(?![a-z0-9-])', page) is not None


def test_every_header_is_documented():
    custom, standard = code_headers()
    page = text().lower()
    missing = sorted(header for header in custom if not named(page, header))
    assert not missing, f'name these headers in the brief\'s Headers section (or say they are internal): {missing}'
    stale = sorted(set(re.findall(r'(?<![a-z0-9-])x-[a-z0-9-]*[a-z0-9]', section('app-headers').lower())) - custom)
    assert not stale, f'the brief names headers the code no longer has: {stale}'
    assert not set(NOT_APP_HEADERS) - standard, f'stale NOT_APP_HEADERS: {sorted(set(NOT_APP_HEADERS) - standard)}'
    unnamed = sorted(header for header in standard - set(NOT_APP_HEADERS) if not named(page, header))
    assert not unnamed, f'document these request headers in the brief, or add them to NOT_APP_HEADERS with a reason: {unnamed}'


def handler(row, files):
    """The function that serves one declared route."""
    owner = next(path for path in files if row['id'].startswith('endpoint-' + re.sub(r'[^a-z0-9]+', '-', path.lower()) + '-'))
    *container, name = row['handler'].split('.')
    candidates = [ROOT / owner] if not container else sorted((ROOT / owner.split('/')[0]).rglob('*.py'))
    for path in candidates:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if container and not (isinstance(node, ast.ClassDef) and node.name == container[-1]):
                continue
            for one in ast.walk(node):
                if isinstance(one, (ast.FunctionDef, ast.AsyncFunctionDef)) and one.name == name:
                    return one
    raise AssertionError(f'no handler {row["handler"]} for {row["method"]} {row["path"]}')


def query_reads(node):
    """Query parameter names read as `….query_params.get('name')` or `….query_params['name']`."""
    names = set()
    for one in ast.walk(node):
        call = one.func if isinstance(one, ast.Call) and isinstance(one.func, ast.Attribute) and one.func.attr in ('get', 'getlist') else None
        target, key = (call.value, one.args[0] if one.args else None) if call else (one.value, one.slice) if isinstance(one, ast.Subscript) else (None, None)
        if isinstance(target, ast.Attribute) and target.attr == 'query_params' and isinstance(key, ast.Constant):
            names.add(key.value)
    return names


def test_every_query_parameter_is_documented():
    from weightroom import system_docs
    files = system_docs.source_files('api')
    rows = [row for group in system_docs.api_reference(files) for row in group['rows'] if row['path'].startswith(('/v1/', '/owg/'))]
    by_handler = {}
    for row in rows:
        if (row['method'], normal(row['path'])) not in NOT_FOR_APPS:
            by_handler.setdefault(row['handler'], set()).add((row['method'], normal(row['path'])))
    read = {name: query_reads(handler(next(r for r in rows if r['handler'] == name), files)) for name in by_handler}
    written = section('app-query')
    table = {(method, normal(path)): set(re.findall(r'`([^`]+)`', cells.split(' | ')[0]))
             for method, path, cells in re.findall(ROUTE_ROW[:-3] + r' \| (.*)$', written, re.M)}
    assert set(table) <= documented_routes(), f'query rows for routes not in the endpoint index: {sorted(set(table) - documented_routes())}'
    for name, names in read.items():
        if names:
            rows_for = by_handler[name] & set(table)
            assert rows_for, f'add a query parameter row for one of {sorted(by_handler[name])}, which read {sorted(names)}'
            shown = set().union(*(table[route] for route in rows_for))
            assert names == shown, f'{sorted(rows_for)}: add {sorted(names - shown)}, remove {sorted(shown - names)}'
    # The documentation reads check their arguments against one table instead of reading each by name.
    allowed = ast.literal_eval(next(node.value for node in ast.walk(function('weightroom/docs_views.py', 'endpoint'))
                                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'allowed' for t in node.targets)))
    for operation, names in allowed.items():
        route = ('GET', '/owg/docs/' + operation)
        assert route in NOT_FOR_APPS or table.get(route) == names, f'{route}: document {sorted(names)}'
    # Any endpoint: the key fallback and the node choice, read before dispatch.
    assert 'request.query_params.get("key", "")' in (ROOT / 'turnstile/accounts.py').read_text(encoding='utf-8')
    assert 'if name == "node"' in (ROOT / 'turnstile/app.py').read_text(encoding='utf-8')
    assert '`key`' in written and '`node`' in written
