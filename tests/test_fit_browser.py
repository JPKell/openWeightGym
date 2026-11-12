"""Authenticated live split browser at catalogue scale; no native model is loaded."""

from dataclasses import replace

import pytest

from loadcoach.models import Models, at_context
from test_console_browser import browser_context, playwright
from test_weightroom import desk, gguf, sign_in  # noqa: F401


@pytest.fixture
def cpu_fit_desk(tmp_path, monkeypatch, request):
    from test_weightroom import build_loadcoach
    monkeypatch.setattr('test_weightroom.build_loadcoach', lambda config, *args: build_loadcoach(replace(config, gpus=()), *args))
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {'total': 8192, 'available': 8192,
                         'service_available': 8192, 'service_limit': 8192})
    server, wire, _ = request.getfixturevalue('desk')
    library = Models(tmp_path / 'loadcoach.db', {'flash-attn': 'on'})
    file = gguf(tmp_path / 'sample.gguf')
    for index in range(30):
        name = f'model-{index:02}'
        library.put(name, {'gpus': 0, 'min_context': 1024, 'flags': {'model': file, 'ctx-size': 8192}})
        for context, rss in [(1024, 128), (8192, 1024)]:
            library.resident_measure(name, at_context(library.get(name), context), rss)
    sign_in(server, 'jordan')
    return server, wire, library


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_live_thirty_model_catalogue_exact_context_locks_and_drafts(cpu_fit_desk, tmp_path, width):
    server, _, library = cpu_fit_desk
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/dry-fit')
        playwright.expect(page.locator('[data-catalogue-select]')).to_have_count(30)
        playwright.expect(page.locator('[data-card]')).to_have_count(1)
        playwright.expect(page.locator('[data-card]')).to_contain_text('System RAM')
        assert page.locator('#catalogue-list').evaluate('el => el.scrollHeight > el.clientHeight')
        for index in range(8):
            page.get_by_label(f'Select model-{index:02}', exact=True).check()
        playwright.expect(page.locator('#fit-picks tr')).to_have_count(8)
        playwright.expect(page.locator('#verdict')).to_have_attribute('data-state', 'over')
        first = page.locator('#fit-picks tr').first
        first.locator('[data-lock]').check()
        page.locator('#fit-auto').click()
        playwright.expect(page.locator('#verdict')).to_have_attribute('data-state', 'fits')
        playwright.expect(first.locator('[data-context]')).to_have_value('8192')
        second = page.locator('#fit-picks tr').nth(1)
        second.locator('[data-context]').fill('2001')
        page.locator('#catalogue-search').fill('model-29')
        playwright.expect(page.locator('[data-catalogue-select]')).to_have_count(1)
        page.get_by_label('Select model-29', exact=True).check()
        playwright.expect(second.locator('[data-context]')).to_have_value('2001')
        page.locator('#catalogue-search').fill('')
        page.locator('#search').fill('model-29')
        playwright.expect(page.locator('#fit-picks tr:visible')).to_have_count(1)
        playwright.expect(page.locator('#selection-count')).to_contain_text('9 selected')
        page.reload()
        playwright.expect(page.locator('#fit-picks tr')).to_have_count(9)
        playwright.expect(page.locator('#fit-picks tr').nth(1).locator('[data-context]')).to_have_value('2001')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.fit-row-scroll').evaluate('el => el.clientHeight <= 550 && el.scrollHeight > el.clientHeight')
        # Invalid edits remain blocked across dynamic replacements and can be corrected exactly.
        page.locator('#fit-picks tr').nth(1).locator('[data-context]').fill('99999')
        page.locator('#catalogue-kind').select_option('audio')
        page.locator('#catalogue-kind').select_option('all')
        page.get_by_label('Select model-10', exact=True).check()
        playwright.expect(page.locator('#fit-save button[value="save"]')).to_be_disabled()
        page.locator('#fit-picks tr').nth(1).locator('[data-context]').fill('2001')
        playwright.expect(page.locator('#fit-save button[value="save"]')).to_be_enabled()
        page.locator('#fit-name').fill('Exact set')
        page.get_by_role('button', name='Save current selection', exact=True).click()
        playwright.expect(page.locator('#fit-saved')).to_contain_text('Exact set')
        assert library.get('model-01')['flags']['ctx-size'] == 8192
        page.get_by_role('button', name='API example', exact=True).click()
        playwright.expect(page.locator('#fit-api-code')).to_contain_text('/turnstile/machines/compute/control/loadouts')
        assert not errors
        page.screenshot(path=str(tmp_path / f'fit-browser-{width}.png'), full_page=True)
        browser.close()


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_context_limit_override_is_per_model_and_restores_exact_saved_values(cpu_fit_desk, width):
    server, _, library = cpu_fit_desk
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/dry-fit')
        for name in ('model-00', 'model-01'):
            page.get_by_label('Select ' + name, exact=True).check()
        first = page.locator('#fit-picks [data-model="model-00"]')
        second = page.locator('#fit-picks [data-model="model-01"]')
        context = first.locator('[data-context]')
        override = first.get_by_role('switch', name='Allow context outside limits for model-00')
        save = page.get_by_role('button', name='Save current selection', exact=True)
        playwright.expect(override).not_to_be_checked()
        playwright.expect(context).to_have_attribute('min', '1024')
        playwright.expect(context).to_have_attribute('max', '8192')
        before = first.locator('[data-memory]').inner_text()
        context.fill('9001')
        playwright.expect(save).to_be_disabled()
        override.check()
        playwright.expect(context).to_have_value('9001')
        playwright.expect(save).to_be_enabled()
        assert first.locator('[data-memory]').inner_text() != before
        playwright.expect(first.locator('[data-slider]')).to_have_value('9001')
        first.locator('[data-slider]').press('End')
        playwright.expect(context).to_have_value('16384')
        first.locator('[data-slider]').press('Home')
        playwright.expect(context).to_have_value('1')
        context.fill('9001')
        playwright.expect(second.locator('[data-context-override]')).not_to_be_checked()
        second.locator('[data-context]').fill('9001')
        playwright.expect(save).to_be_disabled()
        second.locator('[data-context]').fill('8192')
        for value in ('0', '-1', '1.5', '2147483648'):
            context.fill(value)
            playwright.expect(save).to_be_disabled()
        context.fill('512')
        playwright.expect(save).to_be_enabled()
        override.uncheck()
        playwright.expect(context).to_have_value('512')
        playwright.expect(save).to_be_disabled()
        page.reload()
        playwright.expect(context).to_have_value('512')
        playwright.expect(override).not_to_be_checked()
        playwright.expect(save).to_be_disabled()
        override.check()
        playwright.expect(save).to_be_enabled()
        context.fill('9001')
        first.locator('[data-lock]').check()
        page.locator('#fit-auto').click()
        playwright.expect(context).to_have_value('9001')
        page.reload()
        playwright.expect(override).to_be_checked()
        playwright.expect(context).to_have_value('9001')
        playwright.expect(first.locator('[data-lock]')).to_be_checked()
        page.locator('#fit-name').fill('Custom contexts')
        save.click()
        playwright.expect(page.locator('#fit-saved')).to_contain_text('Custom contexts')
        page.locator('#clear').click()
        page.reload()
        playwright.expect(page.locator('#fit-picks tr')).to_have_count(0)
        page.locator('[data-loadout-name="Custom contexts"] [data-loadout-edit]').click()
        playwright.expect(context).to_have_value('9001')
        playwright.expect(override).to_be_checked()
        playwright.expect(second.locator('[data-context-override]')).not_to_be_checked()
        assert library.get('model-00')['flags']['ctx-size'] == 8192
        assert library.get('model-00')['min_context'] == 1024
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        browser.close()


def test_named_set_member_limit_does_not_limit_the_catalogue(cpu_fit_desk):
    server, _, _ = cpu_fit_desk
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        page.goto('http://console.test/dry-fit')
        for index in range(17):
            page.get_by_label(f'Select model-{index:02}', exact=True).check()
        playwright.expect(page.locator('#fit-selection-message')).to_contain_text('up to 16 models')
        playwright.expect(page.locator('#fit-save button[value="save"]')).to_be_disabled()
        playwright.expect(page.locator('[data-catalogue-select]')).to_have_count(30)
        browser.close()
