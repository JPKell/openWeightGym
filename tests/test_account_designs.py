"""Browser checks for portable My Account design proposals; no app server needed."""

import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ('account', 'keys', 'appearance', 'audio', 'workspace', 'privacy', 'logs')


@pytest.fixture
def preview(tmp_path):
    source = runpy.run_path(str(ROOT / 'scripts/preview-account-designs.py'))['render']()
    output = tmp_path / 'index.html'
    output.write_text(source)
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Previews must not contact live services.'
        assert page.evaluate('localStorage.length') == 0, 'Preview preferences must remain tab-local.'
        browser.close()


def section(page, name):
    page.locator(f'.account-rail [data-section={name}]').click()
    expect(page.locator(f'#panel-{name}')).to_be_visible()


def test_layouts_themes_mobile_labels_and_unique_ids(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1536, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in ('desk', 'overview', 'notebook'):
                page.locator(f'[data-design={layout}]').click()
                expect(page.locator('[data-design][aria-pressed=true]')).to_have_count(1)
                for name in SECTIONS:
                    section(page, name)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout, name)
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                        return ids.length === new Set(ids).size;
                    }''')
                    assert page.evaluate('''() => [...document.querySelectorAll('input, select')]
                        .filter(el => el.getBoundingClientRect().width)
                        .every(el => el.labels?.length || el.getAttribute('aria-label'))''')


def test_drafts_save_discard_and_survive_layout_switches(preview):
    page = preview
    section(page, 'audio')
    page.locator('#ap-format').click()
    expect(page.locator('#ap-format')).to_be_focused()
    page.keyboard.press('Escape')
    page.locator('#ap-format').select_option('flac')
    expect(page.locator('[data-draft-for=audio]')).to_be_visible()
    for layout in ('overview', 'notebook', 'desk'):
        page.locator(f'[data-design={layout}]').click()
        section(page, 'audio')
        expect(page.locator('#ap-format')).to_have_value('flac')
    page.locator('#audio-form [type=submit]').click()
    page.locator('#ap-format').select_option('opus')
    page.locator('#audio-form [data-discard]').click()
    expect(page.locator('#ap-format')).to_have_value('flac')
    expect(page.locator('[data-draft-for=audio]')).to_be_hidden()
    page.locator('[data-design=overview]').click()
    expect(page.locator('[data-overview=audio]')).to_contain_text('FLAC')
    page.reload()
    section(page, 'audio')
    expect(page.locator('#ap-format')).to_have_value('wav')


def test_all_palettes_theme_discard_and_accessibility_preview(preview):
    page = preview
    section(page, 'appearance')
    expect(page.locator('[name=palette]')).to_have_count(17)
    page.locator('[name=palette][value=forest]').check()
    page.locator('#appearance-mode').select_option('dark')
    expect(page.locator('html')).to_have_attribute('data-site-theme', 'forest')
    expect(page.locator('html')).to_have_attribute('data-bs-theme', 'dark')
    page.locator('#text-size').select_option('large')
    page.locator('#motion').select_option('reduce')
    expect(page.locator('#workspace')).to_have_attribute('data-text-size', 'large')
    page.locator('#appearance-form [data-discard]').click()
    expect(page.locator('html')).to_have_attribute('data-site-theme', 'slate')
    expect(page.locator('html')).to_have_attribute('data-bs-theme', 'light')
    expect(page.locator('#workspace')).to_have_attribute('data-text-size', 'standard')


def test_key_secret_scoping_escaping_and_confirmed_removal(preview):
    page = preview
    section(page, 'keys')
    label = '<img src=x onerror=alert(1)>'
    page.locator('#label').fill(label)
    page.locator('#key-form [type=submit]').click()
    expect(page.locator('[data-key-label]').last).to_have_text(label)
    expect(page.locator('#secret-panel')).to_be_visible()
    expect(page.locator('#sample-secret')).to_contain_text('DEMO-ONLY-NOT-A-CREDENTIAL')
    section(page, 'audio')
    expect(page.locator('#sample-secret')).to_be_empty()
    section(page, 'keys')
    for _ in range(3):
        page.locator('[data-remove-key]').first.click()
        expect(page.locator('#danger-confirmation [value=confirm]')).to_be_disabled()
        page.keyboard.press('Escape')
        page.locator('[data-remove-key]').first.click()
        page.locator('#danger-confirmation-check').check()
        page.locator('#danger-confirmation [value=confirm]').click()
    expect(page.locator('#keys-empty')).to_be_visible()


def test_password_validation_session_confirmation_and_sensitive_drafts(preview):
    page = preview
    page.locator('#current_password').fill('sample-only-current')
    page.locator('#new_password').fill('sample-only-new')
    page.locator('#new_again').fill('sample-only-other')
    page.locator('#password-form [type=submit]').click()
    expect(page.locator('#password-error')).to_contain_text('do not match')
    section(page, 'audio')
    section(page, 'account')
    expect(page.locator('#current_password')).to_have_value('')
    page.locator('[data-revoke-session]').click()
    page.keyboard.press('Escape')
    expect(page.locator('#other-session')).to_be_visible()
    page.locator('[data-revoke-session]').click()
    page.locator('#danger-confirmation-check').check()
    page.locator('#danger-confirmation [value=confirm]').click()
    expect(page.locator('#session-empty')).to_be_visible()
    expect(page.locator('[data-session-count]')).to_have_text('1')


def test_activity_empty_state_timezone_and_private_export(preview):
    page = preview
    section(page, 'logs')
    page.locator('#activity-kind').select_option('security')
    expect(page.locator('[data-event-kind]:visible')).to_have_count(2)
    page.locator('#activity-search').fill('no-such-event')
    expect(page.locator('#activity-empty')).to_be_visible()
    section(page, 'workspace')
    page.locator('#timezone').select_option('America/Vancouver')
    page.locator('#date-format').select_option('iso')
    expect(page.locator('#timestamp-preview')).to_contain_text('2026-11-14')
    expect(page.locator('#timestamp-preview')).to_contain_text('America/Vancouver')
    page.locator('#timezone').select_option('UTC')
    expect(page.locator('#timestamp-preview')).to_contain_text('2026-11-15, 00:30')
    section(page, 'privacy')
    with page.expect_download() as info:
        page.locator('#export-preferences').click()
    payload = json.loads(Path(info.value.path()).read_text())
    assert payload['sample'] is True
    assert set(payload['preferences']) == {'appearance-form', 'audio-form', 'workspace-form', 'privacy-form'}
    assert 'password' not in json.dumps(payload)


def test_notebook_keyboard_disclosures_and_overview_return(preview):
    page = preview
    page.locator('[data-design=notebook]').click()
    expect(page.locator('[data-panel]:visible')).to_have_count(7)
    summary = page.locator('#panel-workspace > summary')
    summary.focus()
    page.keyboard.press('Enter')
    expect(page.locator('#panel-workspace')).to_have_attribute('open', '')
    page.keyboard.press('Enter')
    expect(page.locator('#panel-workspace')).not_to_have_attribute('open', '')
    page.locator('[data-design=overview]').click()
    expect(page.locator('#overview-home')).to_be_visible()
    page.locator('#overview-home [data-section=keys]').click()
    expect(page.locator('#account-main')).to_be_focused()
    expect(page.locator('#panel-keys')).to_be_visible()
    page.locator('.overview-back').click()
    expect(page.locator('#overview-home')).to_be_visible()
