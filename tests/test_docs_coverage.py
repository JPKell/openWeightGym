"""Documentation gaps must be explicit and shrink as guides and references arrive."""

import json
from pathlib import Path

from weightroom import docs_coverage
from test_weightroom import desk  # noqa: F401


def test_documentation_coverage():
    found = set(docs_coverage.report()['gaps'])
    path = Path(__file__).with_name('docs_coverage_allowlist.json')
    allowed = set(json.loads(path.read_text())) if path.exists() else set()
    assert not found - allowed, 'Undocumented additions:\n' + '\n'.join(sorted(found - allowed))
    assert not allowed - found, 'Remove resolved gaps:\n' + '\n'.join(sorted(allowed - found))


def test_new_navigation_page_requires_a_guide(monkeypatch):
    from weightroom.navigation import NAV_SECTIONS
    monkeypatch.setitem(NAV_SECTIONS, 'Coverage probe', (('', ('/missing-guide-probe',)),))
    assert 'page:/missing-guide-probe' in docs_coverage.report()['gaps']


def test_generated_references_are_defaults_only_and_have_real_citation_anchors(desk):
    from test_weightroom import sign_in
    from weightroom import docs_reference
    server, _, _ = desk
    sign_in(server, 'jordan')
    for address, entries in (('/docs/system/abilities', docs_reference.abilities()),
                             ('/docs/operations/settings-reference', docs_reference.settings())):
        response = server.get(address)
        assert response.status_code == 200
        assert all('id="' + entry['id'] + '"' in response.text for entry in entries)
    assert all(entry['source'].startswith('settings/defaults/') for entry in docs_reference.settings())
    sign_in(server, 'pat')
    assert server.get('/docs/system/abilities').status_code == 403
    assert server.get('/docs/operations/settings-reference').status_code == 403
    assert 'Find the guide for a page' in server.get('/docs').text
