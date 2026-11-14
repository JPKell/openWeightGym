"""Live documentation search stays current, scoped, native and separate from code references."""

import re

from test_weightroom import desk, sign_in  # noqa: F401
from weightroom import docs_views, system_docs
from weightroom.navigation import navigation


def test_docs_navigation_keeps_home_guides_and_code_separate():
    admin = navigation('/docs/code/loadcoach', ['manage-settings'], False)
    assert admin['section'] == 'Docs' and admin['active'] == '/docs/code/loadcoach'
    assert admin['links'][0] == ('Documentation', '/docs')
    assert ('Code references', '/docs/code') in admin['links']
    assert admin['children']['/docs/code'][0] == ('Models', '/docs/code/models')
    assert navigation('/docs', ['manage-settings'], False)['children'] == {}
    public = navigation('/docs', [], False)
    assert public['links'] == [('Documentation', '/docs'), ('Web app', '/docs/web-app')]


def test_docs_home_and_search_are_permission_scoped_even_after_admin_search(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    home = server.get('/docs')
    assert home.status_code == 200 and 'A clear path to what you need.' in home.text
    assert 'id="system-map"' not in home.text
    assert 'Code references' in home.text and '/docs/operations/installation' in home.text
    admin = server.get('/docs?q=recipients.txt')
    assert admin.status_code == 200 and 'class="search-result" href="/docs/data/backups"' in admin.text
    assert admin.headers['cache-control'] == 'no-store'
    sign_in(server, 'pat')
    for address in ('/docs', '/docs?q=recipients.txt', '/docs?q=recipients.txt&part=search', '/docs?scope=code'):
        response = server.get(address)
        assert response.status_code == 200
        assert 'Code references' not in response.text
        assert '/docs/data/backups' not in response.text
        assert '/docs/operations/installation' not in response.text
        assert response.headers['cache-control'] == 'no-store'
    assert server.get('/docs?q=Shift%2BEnter').status_code == 200
    assert 'class="search-result" href="/docs/web-app/chat"' in server.get('/docs?q=Shift%2BEnter').text
    for address in ('/docs/code', '/docs/code/loadcoach', '/docs/api/source', '/docs/api', '/docs/system'):
        assert server.get(address).status_code == 403


def test_code_references_are_separate_and_legacy_source_urls_keep_modules(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    guide = server.get('/docs/modules/loadcoach')
    assert guide.status_code == 200 and 'aria-label="Source modules"' not in guide.text
    assert 'href="/docs/code/loadcoach"' in guide.text
    code = server.get('/docs/code/loadcoach?module=loadcoach/users.py')
    assert code.status_code == 200 and 'Users.sign_in' in code.text
    assert 'aria-label="Source modules"' in code.text
    for old, new in (('/docs/modules/loadcoach', '/docs/code/loadcoach'), ('/docs/system/accounts', '/docs/code/accounts'), ('/docs/api/source', '/docs/code/api')):
        response = server.get(old + '?module=loadcoach/users.py', follow_redirects=False)
        assert response.status_code == 308
        assert response.headers['location'] == new + '?module=loadcoach/users.py'
    assert server.get('/docs/code/accounts?module=../../.env').status_code == 404
    assert server.get('/docs/system').status_code == 200
    assert 'id="system-map"' in server.get('/docs/system').text


def test_search_scopes_empty_escaping_and_fragment_shape(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    found = server.get('/docs?q=Shift%2BEnter&scope=web-app&part=search')
    assert found.status_code == 200 and 'id="docs-search-results"' in found.text
    assert '<html' not in found.text and 'id="site-sidebar"' not in found.text
    assert 'class="search-result" href="/docs/web-app/chat"' in found.text
    empty = server.get('/docs?q=Shift%2BEnter&scope=data')
    assert 'id="docs-search-empty"' in empty.text
    unsafe = server.get('/docs', params={'q': '<img src=x onerror=alert(1)>'})
    assert unsafe.status_code == 200 and '<img src=x onerror=alert(1)>' not in unsafe.text
    assert '&lt;img src=x onerror=alert(1)&gt;' in unsafe.text
    code = server.get('/docs?q=loadcoach&scope=code&part=search')
    categories = re.findall(r'class="result-category">([^<]+)', code.text)
    assert categories and set(categories) == {'Code references'}


def test_search_invalidates_on_document_edit_and_exposes_retry_for_read_failure(desk, monkeypatch, tmp_path):
    server, _, _ = desk
    sign_in(server, 'pat')
    original_written = system_docs.written
    token = tmp_path / 'revision.txt'
    token.write_text('one')
    monkeypatch.setattr(docs_views, 'revision', lambda sections: token.read_text())
    assert 'id="docs-search-empty"' in server.get('/docs?q=revisionneedle').text
    def changed(topic):
        if topic['slug'] == 'chat':
            return {'contents': [], 'overview': 'revisionneedle', 'sections': []}
        return original_written(topic)
    monkeypatch.setattr(system_docs, 'written', changed)
    token.write_text('two')
    assert 'class="search-result" href="/docs/web-app/chat"' in server.get('/docs?q=revisionneedle').text
    def unavailable(topic):
        raise OSError('unavailable')
    monkeypatch.setattr(system_docs, 'written', unavailable)
    token.write_text('three')
    failure = server.get('/docs?q=revisionneedle&part=search')
    assert failure.status_code == 503 and 'temporarily unavailable' in failure.text
    assert failure.headers['cache-control'] == 'no-store'
