"""Scoring studio uses real settings saves and retains unrelated and rejected drafts."""

from pathlib import Path

import pytest

from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in  # noqa: F401

expect = playwright.expect


def definition(server, name):
    return next(b for b in server.app.state.benchmarks.state.store.definitions() if b['id'] == name)


def choose(page, name):
    page.locator(f'[data-benchmark="{name}"]').click()
    expect(page.locator('[name=benchmark]')).to_have_value(name)


def test_real_saves_retain_other_drafts_and_recover_rejected_changes(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/manage?benchmark=json_schema')
        page.locator('#formula').fill('accuracy * 2')
        page.locator('#green').select_option('low')
        page.locator('#server').select_option('fresh')
        page.locator('#enabled').uncheck()
        choose(page, 'speed')
        expect(page.locator('[data-benchmark=json_schema] [data-draft]')).to_be_visible()
        page.locator('#formula').fill('generation_tokens_per_second * 2')
        choose(page, 'json_schema')
        expect(page.locator('#formula')).to_have_value('accuracy * 2')
        expect(page.locator('#server')).to_have_value('fresh')
        expect(page.locator('#green')).to_have_value('low')
        expect(page.locator('#enabled')).not_to_be_checked()
        page.locator('#save').click()
        expect(page.locator('.said-box')).to_contain_text('Saved.')
        expect(page.locator('#save-state')).to_have_text('All changes saved')
        saved = definition(server, 'json_schema')
        assert saved['formula'] == 'accuracy * 2' and saved['green'] == 'low' and saved['server'] == 'fresh'
        assert not saved['enabled']
        expect(page.locator('[data-benchmark=speed] [data-draft]')).to_be_visible()
        expect(page.locator('[data-benchmark=json_schema] [data-available]')).to_have_attribute('data-enabled', 'false')
        page.locator('#formula').fill('accuracy + unknown_value')
        page.locator('#save').click()
        expect(page.locator('.said-box.alert-danger')).to_contain_text('unknown_value')
        expect(page.locator('#formula')).to_have_value('accuracy + unknown_value')
        choose(page, 'speed')
        expect(page.locator('#formula')).to_have_value('generation_tokens_per_second * 2')
        choose(page, 'json_schema')
        expect(page.locator('#formula')).to_have_value('accuracy + unknown_value')
        page.locator('#discard').click()
        expect(page.locator('#formula')).to_have_value('accuracy * 2')
        expect(page.locator('#save')).to_be_disabled()
        assert definition(server, 'speed')['formula'] == 'generation_tokens_per_second'
        assert not errors
        browser.close()


def test_search_keyboard_insertion_and_transport_failure_preserve_drafts(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    failing = {'value': False}

    def capture(route, path):
        if path == '/benchmarks/manage' and route.request.method == 'POST' and failing['value']:
            route.abort('failed')
            return True
        return False

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/manage?benchmark=speed')
        page.locator('#benchmark-search').fill('no matching test')
        expect(page.locator('#no-matches')).to_be_visible()
        page.locator('#benchmark-search').fill('load')
        expect(page.locator('[data-benchmark]:visible')).to_have_count(4)
        choose(page, 'load')
        expect(page.locator('#benchmark-search')).to_have_value('load')
        page.locator('#benchmark-search').fill('')
        page.locator('#formula').fill('')
        page.locator('[data-insert=parameter_count]').focus()
        page.keyboard.press('Enter')
        expect(page.locator('#formula')).to_have_value('parameter_count')
        page.locator('#value-search').fill('not a measurement')
        expect(page.locator('#value-count')).to_contain_text('No matching values')
        choose(page, 'speed')
        choose(page, 'load')
        expect(page.locator('#value-search')).to_have_value('not a measurement')
        expect(page.locator('#formula')).to_have_value('parameter_count')
        failing['value'] = True
        page.locator('#save').click()
        expect(page.locator('#save')).to_be_enabled()
        expect(page.locator('#formula')).to_have_value('parameter_count')
        assert definition(server, 'load')['formula'] == '1 / load_seconds'
        failing['value'] = False
        page.locator('#save').click()
        expect(page.locator('.said-box')).to_contain_text('Saved.')
        expect(page.locator('#save-state')).to_have_text('All changes saved')
        assert definition(server, 'load')['formula'] == 'parameter_count'
        assert not errors
        browser.close()


def test_native_forms_and_deep_links_keep_all_benchmarks(desk):
    from front_helpers import serve_browser_request

    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(java_script_enabled=False)
        context.route('http://native.test/**', lambda route: serve_browser_request(server, 'http://native.test', route))
        page = context.new_page()
        page.goto('http://native.test/benchmarks/manage?benchmark=ifeval')
        expect(page.locator('#formula')).to_have_value('prompt_strict_accuracy')
        assert page.locator('[data-benchmark]').count() == len(server.app.state.benchmarks.state.store.definitions())
        page.locator('#formula').fill('prompt_strict_accuracy * 2')
        page.locator('#save').click()
        expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert definition(server, 'ifeval')['formula'] == 'prompt_strict_accuracy * 2'
        choose(page, 'classifier_accuracy')
        expect(page.locator('#server')).to_be_visible()
        choose(page, 'classifier_speed')
        expect(page.locator('#server')).to_have_count(0)
        browser.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_responsive_layout_and_replacements(desk, theme):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.emulate_media(color_scheme=theme)
        page.goto('http://console.test/benchmarks/manage?benchmark=json_schema')
        for width in (1680, 1440, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1100})
            choose(page, 'json_schema')
            expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
            expect(page.locator('#formula')).to_be_visible()
            assert page.locator('#benchmark-recorded-values tbody tr').count() == len(definition(server, 'json_schema')['kpis'])
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), (theme, width)
            assert page.evaluate('''() => {
                const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                return ids.length === new Set(ids).size;
            }''')
            if width in (1680, 390):
                output = Path('data/benchmark-previews')
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / f'manage-{theme}-{width}.png'), full_page=True)
        assert not errors
        browser.close()


def test_empty_catalogue_and_permissions(desk, monkeypatch):
    server, _, _ = desk
    sign_in(server, 'pat')
    assert server.get('/benchmarks/manage').status_code == 403
    sign_in(server, 'jordan')
    store = server.app.state.benchmarks.state.store
    monkeypatch.setattr(store, 'definitions', lambda: [])
    result = server.get('/benchmarks/manage')
    assert result.status_code == 200 and 'No benchmark definitions' in result.text
    assert 'data-settings-form' not in result.text
