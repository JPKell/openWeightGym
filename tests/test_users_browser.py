"""Settings › Users in a browser: the account directory, drafts that follow each account, and one save."""

import pytest

from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in  # noqa: F401

expect = playwright.expect


@pytest.fixture
def console(desk):  # noqa: F811
    browser, _, users = desk
    sign_in(browser, 'jordan')
    with playwright.sync_playwright() as engine:
        chromium, page = browser_context(engine, browser)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        yield page, users
        assert not errors
        chromium.close()


def choose(page, name):
    page.locator(f'[data-user-choice="{name}"]').click()
    expect(page.locator('#selected-user')).to_have_text(name)


def test_directory_fits_every_width_and_filters_accounts(console):
    page, _ = console
    page.goto('http://console.test/users/cleo')
    for width in (1440, 1024, 768, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        assert page.evaluate('''() => [...document.querySelectorAll('input,select,textarea')]
            .filter(el => el.type !== 'hidden' && el.getBoundingClientRect().width)
            .every(el => el.labels?.length || el.getAttribute('aria-label'))'''), width
        assert page.evaluate('''() => { const ids = [...document.querySelectorAll('[id]')].map(el => el.id); return ids.length === new Set(ids).size; }''')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('#user-search').fill('car')
    expect(page.locator('[data-user-choice]:visible')).to_have_count(1)
    expect(page.locator('[data-user-count]')).to_have_text('1')
    page.locator('#user-search').fill('nobody-at-all')
    expect(page.locator('[data-user-empty]')).to_be_visible()
    page.locator('#user-search').fill('')
    page.locator('#user-filter').select_option('program')
    expect(page.locator('[data-user-choice]:visible')).to_have_count(1)
    expect(page.locator('[data-user-choice="panel"]')).to_be_visible()
    choose(page, 'panel')  # the filter outlives the swap
    expect(page.locator('#user-filter')).to_have_value('program')
    expect(page.locator('[data-user-choice]:visible')).to_have_count(1)
    # The 30 days load after the page.
    expect(page.locator('#account-trend svg')).to_be_visible()


def test_drafts_follow_accounts_and_one_save_keeps_them(console):
    page, users = console
    page.goto('http://console.test/users/cleo')
    page.locator('#max_requests').fill('2')
    page.locator('#can-lock').check()
    expect(page.locator('[data-user-draft-status]')).to_contain_text('Unsaved changes: 2 fields')
    expect(page.locator('[data-user-choice="cleo"] [data-draft-mark]')).to_be_visible()
    choose(page, 'pat')
    expect(page.locator('#max_requests')).to_have_value('')
    expect(page.locator('[data-user-draft-status]')).to_contain_text('1 other account with unsaved changes')
    choose(page, 'cleo')
    expect(page.locator('#max_requests')).to_have_value('2')
    expect(page.locator('#can-lock')).to_be_checked()
    page.locator('[name=do][value=save]').click()
    expect(page.locator('.said-box')).to_contain_text('Saved.')
    expect(page.locator('[data-user-draft-status]')).to_have_text('No unsaved changes')
    expect(page.locator('[data-user-choice="cleo"] [data-draft-mark]')).to_be_hidden()
    assert users.own('cleo') == {'add': ['lock'], 'max_requests': 2}
    # What differs from the group is marked, and can be shown alone.
    page.locator('#changed-only').check()
    expect(page.locator('[data-ability-category] .col-md-6:visible')).to_have_count(1)
    # Discarding puts back what was saved.
    page.locator('#max_requests').fill('5')
    page.locator('[data-discard-draft]').click()
    expect(page.locator('#max_requests')).to_have_value('2')
    expect(page.locator('[data-user-draft-status]')).to_have_text('No unsaved changes')


def test_adding_an_account_opens_in_place_and_shows_its_secret_once(console):
    page, users = console
    page.goto('http://console.test/users')
    expect(page.locator('#add-account')).to_be_hidden()
    page.locator('[data-toggle-add]').click()
    expect(page.locator('#add-name')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#add-account')).to_be_hidden()
    page.locator('[data-toggle-add]').click()
    page.locator('#add-account [value=program]').check()
    expect(page.locator('#add-key')).to_be_checked()
    expect(page.locator('#add-password')).not_to_be_checked()
    page.locator('#add-name').fill('grafana')
    page.locator('#add-group').select_option('plain')
    page.locator('#add-account button.btn-primary').click()
    expect(page.locator('#selected-user')).to_have_text('grafana')
    expect(page.locator('.account-secrets pre.secret')).to_have_count(1)
    expect(page.locator('.account-secrets')).to_contain_text('API key.')
    assert users.listing('grafana')[0]['keys'] and not users.listing('grafana')[0]['password']
