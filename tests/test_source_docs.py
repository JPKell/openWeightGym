"""Source documentation stays private, inert, current and complete."""

import ast
from pathlib import Path

import pytest

from weightroom import system_docs
from test_weightroom import desk, sign_in  # noqa: F401


def test_production_definitions_have_docstrings():
    """Every maintained production class and function is usable in the code reference."""
    missing = []
    roots = {'benchmarks', 'db', 'engines', 'loadcoach', 'mirrorwall', 'plugins', 'scripts', 'soundroom', 'spotter',
             'toollocker', 'turnstile', 'voice', 'weightroom'}
    for root in sorted(roots):
        for path in sorted((system_docs.ROOT / root).rglob('*.py')):
            if {'vendor', '__pycache__'}.intersection(path.parts):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and not ast.get_docstring(node):
                    missing.append(f'{path.relative_to(system_docs.ROOT)}:{node.lineno}:{node.name}')
    assert not missing, '\n'.join(missing)


def test_toollocker_setup_guide_is_available_without_compute_and_stays_private(desk):
    """An administrator can reach setup from Tools while a fresh console has no worker."""
    browser, switch, _ = desk
    sign_in(browser, 'jordan')
    switch.off = True
    assert 'href="/docs/modules/toollocker"' in browser.get('/tools').text
    response = browser.get('/docs/modules/toollocker')
    assert response.status_code == 200
    for anchor in ('requirements', 'install-toollocker-on-the-control-node', 'verify-the-connection', 'add-a-python-tool', 'troubleshooting'):
        assert f'href="#{anchor}"' in response.text and f'id="{anchor}"' in response.text
    for instruction in ('keep_console_settings_on_front', 'toollocker_url', 'install-suite.sh --services toollocker',
                        'restart toollocker.service', 'example__greet'):
        assert instruction in response.text
    sign_in(browser, 'pat')
    guide = browser.get('/docs/web-app/tools').text
    assert 'id="tools"' in guide and 'http://testserver:8443/mcp' in guide
    assert 'href="/docs/modules/toollocker"' not in guide
    assert browser.get('/docs/modules/toollocker').status_code == 403


def test_reference_parses_without_importing_and_invalidates_on_edit(tmp_path, monkeypatch):
    """Documentation never executes a service and reflects edited source revisions."""
    monkeypatch.setattr(system_docs, 'ROOT', tmp_path)
    source = tmp_path / 'loadcoach' / 'app.py'
    source.parent.mkdir()
    source.write_text('raise RuntimeError("must not execute")\ndef example(value: int = 3):\n    """First description."""\n    return value\n')
    first = system_docs.source_reference('loadcoach', 'loadcoach/app.py')['module']
    assert first['definitions'][0]['doc'] == 'First description.'
    assert first['definitions'][0]['signature'] == '(value: int=3)'
    source.write_text(source.read_text().replace('First description.', 'Updated description.'))
    second = system_docs.source_reference('loadcoach', 'loadcoach/app.py')['module']
    assert second['definitions'][0]['doc'] == 'Updated description.'
    with pytest.raises(ValueError):
        system_docs.source_reference('loadcoach', '../../.env')


def test_source_allowlist_excludes_vendors_and_symlinks(tmp_path, monkeypatch):
    """A source selection cannot expose vendor copies or files outside the source root."""
    monkeypatch.setattr(system_docs, 'ROOT', tmp_path)
    folder = tmp_path / 'benchmarks'
    folder.mkdir()
    (folder / 'good.py').write_text('')
    (folder / 'vendor').mkdir()
    (folder / 'vendor' / 'copy.py').write_text('')
    (folder / 'linked.py').symlink_to(Path(__file__))
    assert system_docs.source_files('benchmarks') == ('benchmarks/good.py',)


def test_source_docs_require_admin_and_reject_unknown_files(desk):
    """The rendered reference preserves existing admin authorization and source limits."""
    browser, _, _ = desk
    sign_in(browser, 'pat')
    assert browser.get('/docs/loadcoach?module=loadcoach/users.py').status_code == 403
    assert browser.get('/docs/modules/loadcoach?module=loadcoach/users.py').status_code == 403
    sign_in(browser, 'jordan')
    moved = browser.get('/docs/accounts?module=loadcoach/users.py', follow_redirects=False)  # a link from before the docs had sections
    assert moved.status_code == 308 and moved.headers['location'] == '/docs/system/accounts?module=loadcoach/users.py'
    response = browser.get('/docs/system/accounts?module=loadcoach/users.py')
    assert response.status_code == 200
    assert 'Users.sign_in' in response.text and 'deliberately slow, salted hash' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert browser.get('/docs/system/accounts?module=../../.env').status_code == 404
    assert browser.get('/docs/system/nope').status_code == 404 and browser.get('/docs/nope/models').status_code == 404


def test_docstring_html_is_escaped(desk, monkeypatch):
    """Docstrings remain plain text even if source comments contain active HTML."""
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    monkeypatch.setattr('weightroom.app.source_reference', lambda *args: {
        'files': ('example.py',), 'module': {'path': 'example.py', 'doc': '<script>bad()</script>',
            'definitions': [{'name': 'example', 'line': 1, 'kind': 'def', 'signature': '()', 'doc': '<img onerror="bad()">'}]}})
    monkeypatch.setattr('weightroom.app.api_reference', lambda *args: [{'path': 'example.py', 'rows': [{'method': 'GET', 'path': '/<script>bad()</script>',
            'summary': '<img onerror="bad()">', 'headers': ['<script>bad()</script>']}]}])
    text = browser.get('/docs/system/accounts?module=example.py').text
    assert '&lt;script&gt;bad()&lt;/script&gt;' in text
    assert '<img onerror="bad()">' not in text
    api = browser.get('/docs/api/custom').text
    assert '&lt;script&gt;bad()&lt;/script&gt;' in api
    assert '<script>bad()</script>' not in api and '<img onerror="bad()">' not in api


def test_browser_reference_reads_jsdoc_without_execution(tmp_path, monkeypatch):
    """Named script helpers appear from JSDoc while bundled libraries stay excluded."""
    monkeypatch.setattr(system_docs, 'ROOT', tmp_path)
    folder = tmp_path / 'voice' / 'static'
    folder.mkdir(parents=True)
    (folder / 'example.js').write_text('throw Error("never execute");\n/** Read a value. */\nconst read = value => value;\n')
    (folder / 'vendor.min.js').write_text('')
    assert system_docs.source_files('voice') == ('voice/static/example.js',)
    module = system_docs.source_reference('voice', 'voice/static/example.js')['module']
    assert module['definitions'] == [{'name': 'read', 'line': 3, 'kind': 'function', 'signature': '(value)', 'doc': 'Read a value.'}]


def test_identity_portability_reference_and_backup_guidance_are_admin_only(desk):
    """Offline credential tools have source guidance, never a raw-credential web route."""
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    response = browser.get('/docs/system/accounts?module=loadcoach/identity_bundle.py')
    assert response.status_code == 200
    assert 'restore' in response.text and 'AES-256-GCM' in response.text
    response = browser.get('/backups')
    assert response.status_code == 200
    assert 'suite-identity' in response.text
    sign_in(browser, 'pat')
    assert browser.get('/backups').status_code == 403
    assert browser.get('/docs/system/accounts?module=loadcoach/identity_bundle.py').status_code == 403


def test_api_inventory_reads_routes_without_execution_and_tracks_edits(tmp_path, monkeypatch):
    """Literal declarations, methods and fixed comprehensions stay current without imports or eval."""
    monkeypatch.setattr(system_docs, 'ROOT', tmp_path)
    source = tmp_path / 'turnstile/app.py'
    source.parent.mkdir()
    source.write_text('''raise RuntimeError("must not execute")
def sample(request):
    """Read an <escaped> sample."""
    return request
routes = [Route("/turnstile/sample", sample),
    *[Route(path, signed_in(caller, log, handler), methods=methods)
      for path, handler, methods in (("/v1/files", sample, ["GET", "POST"]),
                                     ("/turnstile/listed", sample, ["POST"]))],
    *[Route("/turnstile/actions/" + action, sample, methods=["POST"])
      for action in ("clear", "copy")],
    Route("/turnstile/unsafe", sample, methods=run_side_effect())]
''')
    groups = system_docs.api_reference(('turnstile/app.py',))
    rows = groups[0]['rows']
    assert {(row['method'], row['path']) for row in rows} == {
        ('GET', '/turnstile/sample'), ('POST', '/turnstile/listed'),
        ('POST', '/turnstile/actions/clear'), ('POST', '/turnstile/actions/copy')}
    assert all(row['summary'] == 'Read an <escaped> sample.' for row in rows)
    source.write_text(source.read_text().replace('/turnstile/sample', '/turnstile/updated'))
    assert any(row['path'] == '/turnstile/updated' for row in system_docs.api_reference(('turnstile/app.py',))[0]['rows'])


def test_api_page_lists_compatibility_gaps_custom_routes_and_scoped_headers(desk):
    """The private page is discoverable and differentiates inference headers from management APIs."""
    browser, _, _ = desk
    sign_in(browser, 'pat')
    assert browser.get('/docs/api').status_code == 403
    sign_in(browser, 'jordan')
    assert 'href="/docs/api"' in browser.get('/docs').text
    pages = [browser.get('/docs/api' + part) for part in ('', '/openai', '/custom', '/headers')]
    assert all(page.status_code == 200 and page.headers['cache-control'] == 'no-store' for page in pages)
    response = type('Pages', (), {'text': ''.join(page.text for page in pages)})
    for text in ('Implemented OpenAI endpoints', 'Not implemented by openWeightGym', '/v1/chat/completions',
                 '/v1/responses', 'backend-dependent passthrough', 'purpose=batch',
                 '/v1/audio/generations', '/v1/audio/diarizations', '/turnstile/models/install',
                 '/turnstile/artifacts/files/cleanup-nodes', '/loadcoach/services/{name}/key',
                 '/owg/extractions', '/owg/extraction-assets', 'Supports stream:true',
                 'X-LoadCoach-Require-Model-ID', 'X-LoadCoach-Task', 'X-ToolLocker-Sets', 'X-Twilio-Signature',
                 'X-Suite-Application', 'X-Suite-Session-Mode', 'Idempotency-Key'):
        assert text in response.text, text
    assert browser.get('/docs/api/source?module=../../settings/voice.toml').status_code == 404
    assert browser.get('/docs/api/source?module=turnstile/app.py').status_code == 200
    assert system_docs.api_headers('loadcoach/app.py', '/loadcoach/settings', 'GET') == ()
    assert 'X-LoadCoach-Lock' in system_docs.api_headers('loadcoach/app.py', '/loadcoach/facts', 'GET')
    assert 'X-LoadCoach-Task' in system_docs.api_headers('loadcoach/tasks.py', '/loadcoach/tasks/{number}/probe', 'POST')


def test_api_inventory_includes_every_explicit_custom_route():
    """New literal service routes cannot silently fall out of the API inventory."""
    groups = {group['path']: group['rows'] for group in system_docs.source_reference('api')['api']}
    for relative in system_docs.SOURCES['api']:
        source = ast.parse((system_docs.ROOT / relative).read_text())
        for node in ast.walk(source):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ('route', 'Route', 'WebSocketRoute') and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                path = node.args[0].value
                if path.startswith(('/loadcoach/', '/turnstile/', '/mirrorwall/', '/soundroom/', '/spotter/', '/voice/', '/browser/', '/desktop/', '/twilio/', '/mcp', '/adapter/', '/owg/')):
                    assert any(row['path'] == path for row in groups.get(relative, [])), (relative, path)
