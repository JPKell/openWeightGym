"""Documentation gaps must be explicit and shrink as guides and references arrive."""

import json
from pathlib import Path

from weightroom import docs_coverage


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
