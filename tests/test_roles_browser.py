"""Exercise benchmark preferences through the actual Roles form in Chromium."""

import pytest

from loadcoach.models import Models
from loadcoach.roles import Roles
from test_app import gguf
from test_console_browser import browser_context
from test_weightroom import desk, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')


def test_roles_preferences_save_preview_and_fit_phone_viewport(desk, tmp_path):
    server, _, _ = desk
    models = Models(str(tmp_path / 'loadcoach.db'))
    models.put('small', {'gpus': 1, 'flags': {'model': gguf(tmp_path/'small.gguf'), 'ctx-size': 2048}})
    roles = Roles(str(tmp_path/'data'/'roles.db'))  # kept on the front machine
    roles.put('chat', {'models': {'small': 1}})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        # The finite mock transport cannot serve the telemetry SSE stream.
        page.context.add_init_script('window.EventSource = class { close() {} };')
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/roles')
        page.locator('#bench-chat-context').fill('2')
        page.locator('#loaded-chat').fill('30')
        page.locator('#fast-loaded-chat').fill('150')
        page.locator('#missing-chat').select_option('exclude')
        page.locator('#stale-chat').select_option('exclude')
        page.locator('#age-chat').fill('14')
        page.locator('#context-chat').fill('1024')
        page.locator('[name="requires-stays"]').check()
        page.get_by_role('button', name='Save', exact=True).click()
        notice = page.locator('.said-box')
        playwright.expect(notice).to_have_text('Saved.')
        header = page.locator('header.sticky-top').bounding_box()
        notice_box = notice.bounding_box()
        assert header and notice_box and notice_box['y'] >= header['y'] + header['height']
        assert roles.get('chat')['benchmarks'] == {'context': 2}
        assert roles.get('chat')['loaded_bonus'] == 30 and roles.get('chat')['fast_loaded_bonus'] == 150
        playwright.expect(page.locator('#loaded-chat')).to_have_value('30')
        playwright.expect(page.locator('#fast-loaded-chat')).to_have_value('150')
        assert roles.get('chat')['requires']['stays'] and roles.get('chat')['requires']['tokens'] == 1024
        playwright.expect(page.locator('#bench-chat-context')).to_have_value('2')
        playwright.expect(page.get_by_text('no compatible result for the current model fingerprint and benchmark version')).to_be_visible()
        page.screenshot(path=str(tmp_path/'roles-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path/'roles-mobile.png'), full_page=True)
        assert not errors
        browser.close()


def test_benchmark_shares_presets_and_saves_are_scoped_to_one_role(desk, tmp_path):
    server, _, _ = desk
    models = Models(str(tmp_path/'loadcoach.db'))
    models.put('small', {'gpus':1,'flags':{'model':gguf(tmp_path/'small.gguf'),'ctx-size':2048}})
    roles = Roles(str(tmp_path/'data'/'roles.db'))  # kept on the front machine
    for name in ('code', 'chat'):
        roles.put(name, {'models':{'small':1}})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        # The finite mock transport cannot serve the telemetry SSE stream.
        page.context.add_init_script('window.EventSource = class { close() {} };')
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/roles')
        # The explanation starts folded away; the pills choose which role's card is shown.
        playwright.expect(page.get_by_text('The top of the list.')).to_be_hidden()
        pills = page.get_by_role('navigation', name='Roles')
        playwright.expect(pills.get_by_role('link')).to_have_text(['code', 'chat'])
        pills.get_by_role('link', name='code', exact=True).click()
        playwright.expect(page.locator('#role-heading')).to_contain_text('code')
        assert page.url.endswith('/roles?role=code')
        playwright.expect(page.locator('#bench-chat-code_retrieval')).to_have_count(0)
        code = page.locator('form').filter(has=page.locator('#bench-code-code_retrieval'))
        code.get_by_role('button', name='Use coding weights', exact=True).click()
        page.locator('#loaded-code').fill('40')
        page.locator('#fast-loaded-code').fill('200')
        playwright.expect(page.locator('#bench-code-code_retrieval')).to_have_value('4')
        playwright.expect(code.locator('[data-benchmark-share="code_retrieval"]')).to_have_text('40.0% share')
        assert roles.get('code')['models'] == {'small':1} and 'benchmarks' not in roles.get('code')
        # Manual edits immediately show their relative influence, without changing saved routing.
        code.get_by_role('button', name='Ignore all benchmarks', exact=True).click()
        page.locator('#bench-code-code_retrieval').fill('3')
        page.locator('#bench-code-ifeval').fill('1')
        playwright.expect(code.locator('[data-benchmark-share="code_retrieval"]')).to_have_text('75.0% share')
        playwright.expect(code.locator('[data-benchmark-share="ifeval"]')).to_have_text('25.0% share')
        page.evaluate("document.dispatchEvent(new Event('htmx:afterSwap'))")
        playwright.expect(code.locator('[data-benchmark-status]')).to_contain_text('Unsaved changes')
        code.get_by_role('button', name='Save', exact=True).click()
        assert roles.get('code')['benchmarks'] == {'code_retrieval':3,'ifeval':1}
        assert roles.get('code')['loaded_bonus'] == 40 and roles.get('code')['fast_loaded_bonus'] == 200
        assert 'loaded_bonus' not in roles.get('chat')
        assert 'benchmarks' not in roles.get('chat')
        playwright.expect(page.locator('#role-heading')).to_contain_text('code')  # saving stays on its role
        pills.get_by_role('link', name='chat', exact=True).click()
        playwright.expect(page.locator('#bench-chat-code_retrieval')).to_have_value('0')
        chat = page.locator('form').filter(has=page.locator('#bench-chat-code_retrieval'))
        chat.get_by_role('button', name='Use chat weights', exact=True).click()
        playwright.expect(chat.locator('[data-benchmark-share="ifeval"]')).to_have_text('50.0% share')
        chat.get_by_role('button', name='Save', exact=True).click()
        assert roles.get('chat')['benchmarks'] == {'ifeval':4,'retrieval':2,'speed':2}
        assert roles.get('chat')['loaded_bonus'] == 25 and roles.get('chat')['fast_loaded_bonus'] == 100
        assert roles.get('code')['loaded_bonus'] == 40 and roles.get('code')['fast_loaded_bonus'] == 200
        assert roles.get('code')['benchmarks'] == {'code_retrieval':3,'ifeval':1}
        page.screenshot(path=str(tmp_path/'role-weight-controls.png'), full_page=True)
        page.set_viewport_size({'width':390, 'height':844})
        page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path/'role-weight-controls-mobile.png'), full_page=True)
        assert not errors
        browser.close()
