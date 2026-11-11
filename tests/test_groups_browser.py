"""Settings › Groups in a browser: the group directory, drafts that follow each group, and one save."""

import pytest

from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in  # noqa: F401

expect = playwright.expect


@pytest.fixture
def console(desk):  # noqa: F811
    browser, _, users = desk
    sign_in(browser, 'jordan')
    browser.post('/groups', data={'name': 'readers'})
    browser.post('/groups', data={'name': 'writers'})
    with playwright.sync_playwright() as engine:
        chromium, page = browser_context(engine, browser)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        yield page, users
        assert not errors
        chromium.close()


def choose(page, name):
    page.locator(f'[data-group-choice="{name}"]').click()
    expect(page.locator('#selected-group')).to_have_text(name)


def test_directory_fits_every_width_and_filters_groups(console):
    page, _ = console
    page.goto('http://console.test/groups/readers')
    for width in (1440, 1024, 768, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        assert page.evaluate('''() => [...document.querySelectorAll('input,select,textarea')]
            .filter(el => el.type !== 'hidden' && el.getBoundingClientRect().width)
            .every(el => el.labels?.length || el.getAttribute('aria-label'))'''), width
        assert page.evaluate('''() => { const ids = [...document.querySelectorAll('[id]')].map(el => el.id); return ids.length === new Set(ids).size; }''')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('#group-search').fill('jordan')  # a member's name finds their group
    expect(page.locator('[data-group-choice]:visible')).to_have_count(1)
    expect(page.locator('[data-group-choice="admin"]')).to_be_visible()
    page.locator('#group-search').fill('nobody-at-all')
    expect(page.locator('[data-group-empty]')).to_be_visible()
    page.locator('#group-search').fill('')
    page.locator('#group-filter').select_option('read-only')
    expect(page.locator('[data-group-choice="admin"]')).to_be_visible()
    expect(page.locator('[data-group-choice="readers"]')).to_be_hidden()
    choose(page, 'admin')  # the filter outlives the swap
    expect(page.locator('#group-filter')).to_have_value('read-only')
    expect(page.locator('#max_requests')).to_be_disabled()
    expect(page.locator('#account-trend svg')).to_be_visible()


def test_drafts_follow_groups_and_one_save_keeps_them(console):
    page, users = console
    page.goto('http://console.test/groups/readers')
    page.locator('#max_requests').fill('3')
    page.locator('#can-lock').check()
    page.locator('#every_tool').uncheck()
    page.locator('#tools').fill('nas__*')
    expect(page.locator('[data-group-draft-status]')).to_contain_text('Unsaved changes: 4 fields')
    expect(page.locator('[data-group-choice="readers"] [data-draft-mark]')).to_be_visible()
    choose(page, 'writers')
    expect(page.locator('#max_requests')).to_have_value('0')
    expect(page.locator('[data-group-draft-status]')).to_contain_text('1 other group with unsaved changes')
    choose(page, 'readers')
    expect(page.locator('#max_requests')).to_have_value('3')
    page.locator('[name=do][value=save]').click()
    expect(page.locator('.said-box')).to_contain_text('Saved.')
    expect(page.locator('[data-group-draft-status]')).to_have_text('No unsaved changes')
    stored = users.groups.stored('readers')
    assert stored['max_requests'] == 3 and stored['can'] == ['lock'] and stored['tools'] == ['nas__*']
    # Discarding puts back what was saved.
    page.locator('#max_requests').fill('9')
    page.locator('[data-discard-draft]').click()
    expect(page.locator('#max_requests')).to_have_value('3')


def test_adding_a_group_opens_in_place(console):
    page, users = console
    page.goto('http://console.test/groups')
    expect(page.locator('#add-group')).to_be_hidden()
    page.locator('[data-toggle-add]').click()
    expect(page.locator('#add-group-name')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#add-group')).to_be_hidden()
    page.locator('[data-toggle-add]').click()
    page.locator('#add-group-name').fill('editors')
    page.locator('#add-group button.btn-primary').click()
    expect(page.locator('#selected-group')).to_have_text('editors')
    assert 'editors' in users.groups
