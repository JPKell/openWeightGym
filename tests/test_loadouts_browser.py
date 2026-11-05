"""Real Dry fit forms/scripts with fake CPU inference, never model-generated code."""

import json
from dataclasses import replace

import pytest

from loadcoach.models import Models
from test_app import fake_llama_server  # noqa: F401
from test_console_browser import browser_context
from test_model_setup import native  # noqa: F401
from test_weightroom import desk, gguf, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture
def loadout_desk(native, monkeypatch, request):  # noqa: F811
    from test_weightroom import build_loadcoach
    monkeypatch.setattr('test_weightroom.build_loadcoach', lambda config, *args:
                        build_loadcoach(replace(config, llama_server=native, max_wait=5), *args))
    server, switch, users = request.getfixturevalue('desk')
    compute = switch.wire.app
    life = compute.router.lifespan_context(compute)
    # The existing desk fixture is intentionally read-only. Start compute on the same
    # event loop as its ASGI callers, so scheduler events and database connections stay local.
    server.portal.call(life.__aenter__)
    try:
        yield server, switch, users
    finally:
        server.portal.call(life.__aexit__, None, None, None)


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_dry_fit_saves_cpu_selection_reviews_loads_holds_and_undoes(loadout_desk, tmp_path, width):
    server, _, _ = loadout_desk
    library = Models(tmp_path / 'loadcoach.db')
    settings = {'gpus': 0, 'flags': {'model': gguf(tmp_path / 'cpu.gguf'), 'ctx-size': 4096}}
    library.put('cpu', settings)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/dry-fit')
        page.locator('.fit-cpu').check()
        page.locator('.fit-cpu-context').fill('2048')
        page.locator('#fit-name').fill('Daily <fit>')
        page.get_by_role('button', name='Save current selection', exact=True).click()
        playwright.expect(page.locator('#fit-saved')).to_contain_text('Daily <fit>')
        assert library.get('cpu')['flags']['ctx-size'] == 4096  # saving cannot apply settings
        page.get_by_role('button', name='Select', exact=True).click()
        playwright.expect(page.locator('.fit-cpu')).to_be_checked()
        playwright.expect(page.locator('.fit-cpu-context')).to_have_value('2048')
        playwright.expect(page.locator('#fit-replace')).to_be_checked()
        page.get_by_role('button', name='Save current selection', exact=True).click()
        playwright.expect(page.locator('#fit-saved')).to_contain_text('version 2')
        page.get_by_role('button', name='Preview', exact=True).click()
        playwright.expect(page.locator('#loadout-review')).to_be_visible()
        playwright.expect(page.locator('#loadout-review')).to_contain_text('2048')
        page.screenshot(path=str(tmp_path / f'loadout-review-{width}.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#loadout-acknowledge').check()
        page.get_by_role('button', name='Apply, load and hold 10 minutes', exact=True).click()
        playwright.expect(page.get_by_role('table', name='Your load-out holds')).to_contain_text('ready')
        assert library.get('cpu')['gpus'] == 0 and library.get('cpu')['flags']['ctx-size'] == 2048
        page.get_by_role('button', name='Renew 10 minutes', exact=True).click()
        playwright.expect(page.get_by_role('table', name='Your load-out holds')).to_contain_text('Daily <fit>')
        page.get_by_role('button', name='Release set hold', exact=True).click()
        playwright.expect(page.get_by_role('table', name='Your load-out holds')).to_have_count(0)
        playwright.expect(page.locator('.fit-cpu')).to_be_checked()  # CPU readiness uses loaded state, not nonempty GPU cards
        page.get_by_role('button', name='Preview undo', exact=True).click()
        page.locator('#loadout-acknowledge').check()
        page.get_by_role('button', name='Restore settings', exact=True).click()
        assert library.stored('cpu') == settings
        page.get_by_role('button', name='Delete saved set', exact=True).click()
        page.get_by_label('Yes, delete saved set').check()  # a deletion is confirmed first
        page.get_by_role('button', name='Continue', exact=True).click()
        playwright.expect(page.locator('#fit-saved')).to_contain_text('No saved load-outs.')
        assert not errors
        browser.close()


def test_dry_fit_post_keeps_origin_checks_and_bounds(loadout_desk):
    server, _, _ = loadout_desk
    sign_in(server, 'jordan')
    form = {'do': 'save', 'name': 'Work', 'selection': json.dumps([])}
    assert server.post('/dry-fit', data=form, headers={'Origin': 'https://elsewhere.test'}).status_code == 403
    assert server.post('/dry-fit', data={**form, 'selection': 'x' * (257 * 1024)}).status_code == 413
