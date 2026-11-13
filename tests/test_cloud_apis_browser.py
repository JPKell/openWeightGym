"""The live account desk over isolated apps: scoped saves, drafts and two-click removal."""

import pytest

from loadcoach.outside import Services
from test_weightroom import desk, sign_in  # noqa: F401
from test_outside import service  # noqa: F401
from test_console_browser import browser_context, playwright

expect = playwright.expect


def add_account(server, name, url='https://provider.example/v1'):
    response = server.post('/cloud-apis', data={'do':'add','name':name,'url':url,'ceiling':'25'})
    assert response.status_code == 200


@pytest.mark.parametrize('width', [1440, 768, 390])
def test_ten_accounts_shared_endpoints_filters_themes_and_empty_state(desk, width):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width,'height':1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/cloud-apis')
        expect(page.locator('#add-account')).to_be_visible()
        for i in range(10):
            add_account(server, f'account-{i}')
        page.goto('http://console.test/cloud-apis')
        expect(page.locator('[data-cloud-choice]')).to_have_count(10)
        expect(page.locator('.ca-provider-group')).to_have_count(1)
        for theme in ('light','dark'):
            page.evaluate('''theme => {
                const mode = document.createElement('select'); mode.name = 'mode';
                mode.innerHTML = `<option value="${theme}">${theme}</option>`;
                const form = document.createElement('form'); form.dataset.themeForm = '';
                form.innerHTML = '<div data-theme-options></div>';
                form.append(mode); document.body.append(form);
                mode.dispatchEvent(new Event('change',{bubbles:true})); form.remove();
            }''',theme)
            expect(page.locator('html')).to_have_attribute('data-bs-theme',theme)
            for group in ('connection','access','models','limits'):
                page.locator(f'.ca-settings-nav [data-editor-section="{group}"]').click()
                expect(page.locator(f'#panel-{group}')).to_be_visible()
                expect(page.locator(f'.ca-settings-nav [data-editor-section="{group}"]')).to_have_attribute('aria-current','page')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#search').fill('account-9')
        expect(page.locator('#result-count')).to_have_text('1 of 10 accounts')
        page.locator('#search').fill('no-such-account')
        expect(page.locator('[data-no-accounts]')).to_be_visible()
        page.locator('#search').fill('')
        page.locator('#status-filter').select_option('ready')
        expect(page.locator('#result-count')).to_have_text('0 of 10 accounts')
        assert page.evaluate('''() => {
            const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
            return ids.length === new Set(ids).size;
        }''')
        assert not errors
        browser.close()


def test_drafts_survive_account_switches_and_saves_of_other_sections(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    add_account(server,'work'); add_account(server,'personal')
    kept = Services(str(tmp_path / 'data/accounts.db'),str(tmp_path))
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine,server)
        errors = []; page.on('pageerror',lambda error: errors.append(str(error)))
        page.goto('http://console.test/cloud-apis?account=work')
        page.locator('#connection-form [name=url]').fill('https://draft.example/v1')
        page.locator('[data-cloud-choice="personal"]').click()
        expect(page.locator('#account-id')).to_have_value('personal')
        page.locator('[data-cloud-choice="work"]').click()
        expect(page.locator('#connection-form [name=url]')).to_have_value('https://draft.example/v1')
        expect(page.locator('[data-draft="connection"]')).to_have_text('Unsaved changes')
        page.locator('.ca-settings-nav [data-editor-section="models"]').click()
        page.locator('#model-rows [name=ours]').first.fill('work-fast')
        page.locator('#model-rows [name=theirs]').first.fill('provider/fast')
        page.locator('#add-model-row').click()
        page.locator('#model-rows [name=ours]').nth(1).fill('work-extra')
        page.locator('#model-rows [name=theirs]').nth(1).fill('provider/extra')
        page.locator('.ca-settings-nav [data-editor-section="limits"]').click()
        page.locator('#limits-form [name=ceiling]').fill('42')
        page.locator('#limits-form button').click()
        expect(page.locator('.said-box')).to_contain_text('Saved')
        expect(page.locator('[data-draft="limits"]')).to_be_empty()
        assert kept.get('work')['ceiling'] == 42
        assert kept.get('work')['url'] == 'https://provider.example/v1'
        page.locator('.ca-settings-nav [data-editor-section="connection"]').click()
        expect(page.locator('#connection-form [name=url]')).to_have_value('https://draft.example/v1')
        page.locator('.ca-settings-nav [data-editor-section="models"]').click()
        expect(page.locator('#model-rows [name=ours]').nth(1)).to_have_value('work-extra')
        page.locator('[data-cloud-choice="personal"]').click()
        expect(page.locator('#account-id')).to_have_value('personal')
        page.locator('[data-cloud-choice="work"]').click()
        expect(page.locator('#account-id')).to_have_value('work')
        expect(page.locator('#panel-models')).to_be_visible()
        expect(page.locator('#model-rows [name=ours]').first).to_have_value('work-fast')
        expect(page.locator('#model-rows [name=ours]').nth(1)).to_have_value('work-extra')
        page.locator('#models-form button[name=do]').click()
        expect(page.locator('.said-box')).to_contain_text('Saved')
        assert set(kept.get('work')['models']) == {'work-fast','work-extra'}
        assert not kept.get('personal')['models']
        assert not errors
        browser.close()


def test_removal_requires_two_clicks_and_resets_on_escape_and_section_change(desk, tmp_path):
    server, _, _ = desk
    sign_in(server,'jordan')
    add_account(server,'work'); add_account(server,'personal')
    kept = Services(str(tmp_path / 'data/accounts.db'),str(tmp_path))
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine,server)
        page.goto('http://console.test/cloud-apis?account=work&section=limits')
        button = page.locator('#remove-form button')
        expect(page.locator('#remove-form input[type=checkbox]')).to_have_count(0)
        button.click(); expect(button).to_have_text('Are you sure?')
        assert kept.get('work') is not None
        page.keyboard.press('Escape'); expect(button).to_have_text('Remove account')
        button.click()
        page.locator('.ca-settings-nav [data-editor-section="connection"]').click()
        page.locator('.ca-settings-nav [data-editor-section="limits"]').click()
        expect(button).to_have_text('Remove account')
        button.click(); assert kept.get('work') is not None
        button.click()
        expect(page.locator('[data-cloud-choice="work"]')).to_have_count(0)
        expect(page.locator('#account-id')).to_have_value('personal')
        assert kept.get('work') is None and kept.get('personal') is not None
        expect(page.locator('#danger-confirmation')).not_to_be_visible()
        browser.close()


def test_discovery_preserves_model_drafts_and_add_form_keeps_failed_values(desk, service, tmp_path):
    server, _, _ = desk
    sign_in(server,'jordan'); add_account(server,'rented',service)
    server.post('/cloud-apis',data={'name':'rented','do':'key','api_key':'good-key'})
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine,server)
        errors = []; page.on('pageerror',lambda error: errors.append(str(error)))
        page.goto('http://console.test/cloud-apis?account=rented&section=models')
        page.locator('#model-rows [name=ours]').first.fill('my-draft')
        page.locator('#model-rows [name=theirs]').first.fill('vendor/small')
        page.locator('#discover').click()
        expect(page.locator('#offers-panel')).to_be_visible()
        expect(page.locator('#model-rows [name=ours]').first).to_have_value('my-draft')
        page.get_by_label('vendor/big',exact=True).check()
        page.locator('#offers-form button').click()
        expect(page.locator('#model-rows [name=theirs]')).to_have_count(2)
        expect(page.locator('#model-rows [name=ours]').first).to_have_value('my-draft')
        expect(page.locator('#model-rows [name=theirs]').nth(1)).to_have_value('vendor/big')
        page.locator('[data-add]').first.click()
        page.locator('#new-name').fill('rented')
        page.locator('#new-url').fill(service)
        page.locator('#new-ceiling').fill('12')
        page.locator('#add-form button[name=do]').click()
        expect(page.locator('.said-box.alert-danger')).to_be_visible()
        expect(page.locator('#new-name')).to_have_value('rented')
        expect(page.locator('#new-ceiling')).to_have_value('12')
        page.locator('#new-name').fill('rented-other')
        page.locator('#add-form button[name=do]').click()
        expect(page.locator('#account-id')).to_have_value('rented-other')
        expect(page.locator('#panel-access')).to_be_visible()
        expect(page.locator('#add-account')).not_to_be_visible()
        assert not errors
        browser.close()
