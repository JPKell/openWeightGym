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
        page.locator('#role-tuning > summary').click()
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
        page.locator('#role-evidence > summary').click()
        page.locator('#role-evidence details > summary').first.click()
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
        assert pills.get_by_role('link').evaluate_all('(links) => links.map(link => link.dataset.roleChoice)') == ['code', 'chat']
        pills.get_by_role('link', name='code', exact=True).click()
        playwright.expect(page.locator('#role-heading')).to_contain_text('code')
        assert page.url.endswith('/roles?role=code')
        playwright.expect(page.locator('#bench-chat-code_retrieval')).to_have_count(0)
        page.locator('#role-tuning > summary').click()
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
        if page.locator('#role-tuning').get_attribute('open') is None:
            page.locator('#role-tuning > summary').click()
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


@pytest.fixture
def studio(desk, tmp_path):
    server, _, _ = desk
    models = Models(str(tmp_path / 'loadcoach.db'))
    for name in ('strong', 'quick'):
        models.put(name, {'gpus': 1, 'flags': {'model': gguf(tmp_path / f'{name}.gguf'), 'ctx-size': 2048}})
    roles = Roles(str(tmp_path / 'data/roles.db'))
    for name in ('code', 'chat'):
        roles.put(name, {'models': {'strong': 3, 'quick': 1}})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.context.add_init_script('window.EventSource = class { close() {} };')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/roles?role=code')
        yield page, roles
        assert not errors
        browser.close()


def test_studio_drafts_survive_switch_refresh_and_selected_role_save(studio):
    page, roles = studio
    weight = page.locator('[data-model-name="strong"] [data-weight]')
    weight.fill('7.5')
    playwright.expect(page.locator('[data-role-preview-stale]')).to_be_visible()
    playwright.expect(page.locator('[data-role-pick]:visible [data-role-pick-label]')).to_have_text('LAST SAVED PICK')
    assert roles.get('code')['models']['strong'] == 3
    page.locator('#role-request-mode').select_option(':fast')
    playwright.expect(page.locator('[data-role-pick]:visible code')).to_have_text('code:fast')
    page.get_by_role('navigation', name='Roles', exact=True).get_by_role('link', name='chat', exact=True).click()
    weight.fill('4')
    page.get_by_role('button', name='Save', exact=True).click()
    playwright.expect(page.locator('.said-box')).to_have_text('Saved.')
    assert roles.get('chat')['models']['strong'] == 4
    assert roles.get('code')['models']['strong'] == 3
    page.get_by_role('navigation', name='Roles', exact=True).get_by_role('link', name='code', exact=True).click()
    playwright.expect(weight).to_have_value('7.5')
    playwright.expect(page.locator('[data-role-pick]:visible code')).to_have_text('code:fast')
    page.get_by_role('link', name='Refresh model state').click()
    playwright.expect(weight).to_have_value('7.5')
    page.get_by_role('button', name='Save', exact=True).click()
    playwright.expect(page.locator('[data-role-save-state]')).to_have_text('No unsaved changes')
    playwright.expect(page.locator('[data-role-preview-stale]')).to_be_hidden()
    assert roles.get('code')['models']['strong'] == 7.5


def test_studio_remove_add_pause_and_discard_use_server_form_contract(studio):
    page, roles = studio
    page.get_by_role('button', name='Remove strong from role', exact=True).click()
    playwright.expect(page.locator('[data-model-name="strong"]')).to_be_hidden()
    page.locator('#add-role-model').select_option('strong')
    page.get_by_role('button', name='+ Add model', exact=True).click()
    playwright.expect(page.locator('[data-model-name="strong"] [data-weight]')).to_have_value('1')
    page.get_by_role('button', name='Discard changes').click()
    playwright.expect(page.locator('[data-model-name="strong"] [data-weight]')).to_have_value('3')
    page.get_by_role('button', name='Remove quick from role', exact=True).click()
    page.get_by_role('button', name='Save', exact=True).click()
    playwright.expect(page.locator('.said-box')).to_have_text('Saved.')
    assert roles.get('code')['models'] == {'strong': 3}
    page.locator('#add-role-model').select_option('quick')
    page.get_by_role('button', name='+ Add model', exact=True).click()
    page.locator('[data-model-name="quick"] [data-weight]').fill('0')
    page.get_by_role('button', name='Save', exact=True).click()
    playwright.expect(page.locator('.said-box')).to_have_text('Saved.')
    assert roles.get('code')['models'] == {'strong': 3, 'quick': 0}
    playwright.expect(page.locator('[data-model-name="quick"] [data-candidate-state]')).to_have_text('Paused · weight is 0')


def test_studio_rejected_save_keeps_draft_and_discard_restores_saved_values(studio):
    page, roles = studio
    page.locator('#role-tuning > summary').click()
    page.locator('#loaded-code').fill('40')
    page.locator('[data-model-name="strong"] [data-weight]').fill('9')
    # Bypass browser constraint validation to exercise a real server rejection and replacement.
    page.locator('#context-code').fill('12.5')
    page.evaluate("document.querySelector('[data-role-editor]').noValidate = true")
    page.get_by_role('button', name='Save', exact=True).click()
    playwright.expect(page.locator('.said-box.alert-danger')).to_be_visible()
    playwright.expect(page.locator('#loaded-code')).to_have_value('40')
    playwright.expect(page.locator('#context-code')).to_have_value('12.5')
    playwright.expect(page.locator('[data-model-name="strong"] [data-weight]')).to_have_value('9')
    assert roles.get('code')['models']['strong'] == 3
    page.get_by_role('button', name='Discard changes').click()
    playwright.expect(page.locator('#loaded-code')).to_have_value('25')
    playwright.expect(page.locator('#context-code')).to_have_value('0')
    playwright.expect(page.locator('[data-role-save-state]')).to_have_text('No unsaved changes')


def test_studio_populated_layout_uses_shared_sizes_in_all_states(studio, tmp_path):
    from test_ui_consistency import MEASURE
    page, _ = studio
    for theme in ('light', 'dark'):
        page.emulate_media(color_scheme=theme)
        for width in (1440, 900, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for open_details in (False, True):
                page.locator('#role-tuning').evaluate('(el, open) => {el.open = open;}', open_details)
                page.locator('#role-evidence').evaluate('(el, open) => {el.open = open;}', open_details)
                if not open_details and width in (1440, 390):
                    page.screenshot(path=str(tmp_path / f'roles-{theme}-{width}.png'), full_page=True)
                result = page.evaluate(MEASURE)
                assert not result['overflow'], (theme, width)
                assert not result['namelessFields'], result['namelessFields']
                assert not result['invalidControls'], result['invalidControls']
                assert not result['mismatches'], result['mismatches']
                assert page.evaluate("() => { const ids = [...document.querySelectorAll('[id]')].map(el => el.id); return ids.length === new Set(ids).size; }")


def test_studio_create_role_and_keyboard_copy(studio):
    page, roles = studio
    page.get_by_role('link', name='+ Create role', exact=True).click()
    page.locator('#new-role').fill('research')
    page.locator('#first-model').select_option('quick')
    page.get_by_role('button', name='Create role', exact=True).click()
    playwright.expect(page.locator('#role-heading')).to_have_text('research')
    assert roles.get('research')['models'] == {'quick': 1}
    page.evaluate("() => { Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.copiedRole = text;}}}); }")
    page.get_by_role('button', name='Copy name', exact=True).focus()
    page.keyboard.press('Enter')
    playwright.expect(page.locator('[data-role-copy-status]')).to_contain_text('Copied research')
    assert page.evaluate('window.copiedRole') == 'research'
