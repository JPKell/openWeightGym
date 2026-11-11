"""Browser contracts for the three sample-only Users proposals (the overview and an account's page)."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
LAYOUTS = ('directory', 'ledger', 'lanes')
SECTIONS = ('activity', 'access', 'keys', 'permissions', 'limits')


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-user-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Sample previews must not contact live services.'
        browser.close()


def design(page, layout):
    page.locator(f'[data-design="{layout}"]').click()
    expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')


def choose(page, name):
    layout = page.locator('#workspace').get_attribute('data-layout')
    holder = {'directory': '#user-list', 'ledger': '#user-rows', 'lanes': '#user-lanes'}[layout]
    if layout == 'lanes' and page.locator('#workspace').get_attribute('data-view') == 'profile':
        page.locator('#back-to-overview').click()
    page.locator(f'{holder} [data-user-choice="{name}"]').click()
    expect(page.locator('#selected-name')).to_have_text(name)


def section(page, key):
    if page.locator('#workspace').get_attribute('data-layout') != 'directory':
        page.locator(f'button[data-section="{key}"]').click()


def test_all_layouts_themes_widths_and_sections_are_accessible(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in LAYOUTS:
                design(page, layout)
                assert page.locator('[data-design][aria-pressed="true"]').count() == 1
                for view in (('overview', 'profile') if layout == 'lanes' else ('profile',)):
                    if view == 'profile' and layout == 'lanes':
                        choose(page, 'lena.k')
                    for key in (SECTIONS if view == 'profile' else ('activity',)):
                        if view == 'profile':
                            section(page, key)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout, view, key)
                        assert page.evaluate('''() => {
                            const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                            return ids.length === new Set(ids).size;
                        }''')
                        assert page.evaluate('''() => [...document.querySelectorAll('input,select,textarea')]
                            .filter(el => el.getBoundingClientRect().width)
                            .every(el => el.labels?.length || el.getAttribute('aria-label'))''')


def test_each_layout_lists_every_account_and_lanes_open_a_profile(preview):
    page = preview
    expect(page.locator('#user-list [data-user-choice]')).to_have_count(11)
    design(page, 'ledger')
    expect(page.locator('#user-rows [data-user-row]')).to_have_count(11)
    expect(page.locator('.user-inspector')).to_be_visible()
    design(page, 'lanes')
    expect(page.locator('#user-lanes [data-group-lane]')).to_have_count(5)
    expect(page.locator('.user-inspector')).to_be_hidden()
    page.locator('#user-lanes [data-user-choice="lena.k"]').click()
    expect(page.locator('#user-lanes')).to_be_hidden()
    expect(page.locator('#selected-name')).to_be_focused()
    section(page, 'permissions')
    expect(page.locator('[data-ability-row="hold"] [data-ability-group]')).to_have_text('Allowed')
    expect(page.locator('[data-ability-row="hold"] [data-ability-marks]')).to_contain_text('taken away')
    page.locator('#back-to-overview').click()
    expect(page.locator('#user-lanes [data-user-choice="lena.k"]')).to_be_focused()


def test_filters_attention_and_empty_state(preview):
    page = preview
    expect(page.locator('#kpi-attention')).to_have_text('3')
    page.locator('#show-attention').click()
    expect(page.locator('#user-list [data-user-choice]')).to_have_count(3)
    page.locator('#user-filter').select_option('program')
    expect(page.locator('#user-list [data-user-choice]')).to_have_count(4)
    page.locator('#user-filter').select_option('custom')
    expect(page.locator('#user-list [data-user-choice]')).to_have_count(5)
    page.locator('#user-filter').select_option('all')
    page.locator('#user-search').fill('ha-staging')
    expect(page.locator('#user-list [data-user-choice]')).to_have_count(1)
    page.locator('#user-search').fill('no-such-account')
    expect(page.locator('#no-users')).to_be_visible()
    page.locator('#user-search').fill('')
    choose(page, 'dev.chen')
    expect(page.locator('#attention-notice')).to_contain_text('temporary password')


def test_permission_drafts_follow_accounts_and_save_as_its_own(preview):
    page = preview
    expect(page.locator('[data-ability-row="see-log"] [data-ability-marks]')).to_contain_text('given')
    page.locator('#can-hold').uncheck()
    expect(page.locator('[data-ability-row="hold"] [data-ability-marks]')).to_contain_text('taken away')
    expect(page.locator('#save-state')).to_contain_text('Unsaved: permissions')
    expect(page.locator('#user-list [data-user-choice="maya.r"] [data-draft-mark]')).to_be_visible()
    for layout in ('ledger', 'lanes', 'directory'):
        design(page, layout)
        choose(page, 'priya')
        choose(page, 'maya.r')
        section(page, 'permissions')
        expect(page.locator('#can-hold')).not_to_be_checked()
    page.locator('#changed-only').check()
    expect(page.locator('[data-ability-row]:visible')).to_have_count(2)
    page.locator('#save').click()
    expect(page.locator('#feedback')).to_contain_text('Saved maya.r')
    page.reload()
    expect(page.locator('#can-hold')).not_to_be_checked()
    expect(page.locator('#save-state')).to_have_text('No unsaved changes')


def test_group_moves_keep_own_changes_and_limits_cannot_widen(preview):
    page = preview
    page.locator('#user-group').select_option('researchers')
    expect(page.locator('#can-see-log')).to_be_checked()
    expect(page.locator('#can-high')).not_to_be_checked()
    expect(page.locator('#group-summary')).to_contain_text('researchers')
    page.locator('#max-requests').fill('9')
    expect(page.locator('#save')).to_be_disabled()
    expect(page.locator('#save-state')).to_contain_text('cannot be above')
    page.locator('#max-requests').fill('2')
    page.locator('#fewer-models').check()
    expect(page.locator('#model-choices [data-model]:checked')).to_have_count(4)
    expect(page.locator('#save')).to_be_enabled()
    page.locator('#discard').click()
    expect(page.locator('#user-group')).to_have_value('staff')


def test_superuser_covers_other_abilities_but_not_program_keys(preview):
    page = preview
    choose(page, 'jpk')
    expect(page.locator('#self-notice')).to_be_visible()
    expect(page.locator('#toggle-enabled')).to_be_hidden()
    expect(page.locator('#can-load')).to_be_checked()
    expect(page.locator('#can-load')).to_be_disabled()
    expect(page.locator('[data-ability-row="load"] [data-ability-marks]')).to_contain_text('in superuser')
    expect(page.locator('#can-sign-in')).not_to_be_checked()
    expect(page.locator('#make-key')).to_be_disabled()


def test_secrets_are_shown_once_for_new_accounts_keys_and_passwords(preview):
    page = preview
    page.locator('#add-account').click()
    expect(page.locator('#add-name')).to_be_focused()
    page.locator('#add-form [value=program]').check()
    page.locator('#add-name').fill('Bad Name')
    page.locator('#add-submit').click()
    expect(page.locator('#add-error')).to_contain_text('lowercase')
    page.locator('#add-name').fill('grafana')
    page.locator('#add-submit').click()
    expect(page.locator('#add-error')).to_contain_text('Choose a group')
    page.locator('#add-group').select_option('apps')
    page.locator('#add-key-label').fill('dashboards')
    page.locator('#add-submit').click()
    expect(page.locator('#selected-name')).to_have_text('grafana')
    expect(page.locator('#secret-panel')).to_be_visible()
    expect(page.locator('[data-secret-row]')).to_have_count(1)
    expect(page.locator('[data-secret-value]')).to_contain_text('DEMO-ONLY-key-')
    page.locator('#dismiss-secrets').click()
    expect(page.locator('#secret-panel')).to_be_hidden()
    page.locator('#new-key-label').fill('backup')
    page.locator('#new-key-label').press('Enter')
    expect(page.locator('#key-rows [data-key-row]')).to_have_count(2)
    expect(page.locator('#secret-panel')).to_be_visible()
    choose(page, 'maya.r')
    expect(page.locator('#secret-panel')).to_be_hidden()
    page.locator('#make-password').click()
    expect(page.locator('[data-secret-what]')).to_have_text('Temporary password')
    expect(page.locator('#fact-password')).to_contain_text('Temporary')


def test_destructive_actions_require_confirmation(preview):
    page = preview
    page.locator('#toggle-enabled').click()
    page.keyboard.press('Escape')
    expect(page.locator('.account-heading [data-account-status]')).to_have_text('Active')
    page.locator('#toggle-enabled').click()
    page.locator('#confirm-action').click()
    expect(page.locator('.account-heading [data-account-status]')).to_have_text('Disabled')
    page.locator('#toggle-enabled').click()
    expect(page.locator('.account-heading [data-account-status]')).to_have_text('Active')
    choose(page, 'home-assistant')
    page.locator('[data-key-remove]').first.click()
    expect(page.locator('#confirm-action')).to_be_disabled()
    page.locator('#action-check').check()
    page.locator('#confirm-action').click()
    expect(page.locator('#key-rows [data-key-row]')).to_have_count(1)
    page.locator('#remove-account').click()
    page.locator('#action-check').check()
    page.locator('#confirm-action').click()
    expect(page.locator('#user-list [data-user-choice="home-assistant"]')).to_have_count(0)


def test_rename_validation_and_storage_failure_keep_draft(preview):
    page = preview
    page.locator('#rename').fill('priya')
    page.locator('#rename-button').click()
    expect(page.locator('#feedback')).to_contain_text('already an account')
    page.locator('#can-hold').uncheck()
    page.locator('#rename').fill('maya')
    page.locator('#rename').press('Enter')
    expect(page.locator('#selected-name')).to_have_text('maya')
    expect(page.locator('#can-hold')).not_to_be_checked()
    page.evaluate("() => { Storage.prototype.setItem = () => { throw new Error('blocked'); }; }")
    page.locator('#save').click()
    expect(page.locator('#feedback')).to_contain_text('could not save')
    expect(page.locator('#save-state')).to_contain_text('Unsaved')
