"""Desktop design studies keep call controls usable without touching live services."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-voice-designs.py'))['render']())
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url)
                if request.url.startswith(('http:', 'https:', 'ws:', 'wss:')) else None)
        page.goto(output.as_uri())
        yield page
        browser.close()
        assert not errors
        assert not requests, 'Desktop proposals must stay offline.'


def test_settings_and_transcript_survive_design_switches(preview):
    page = preview
    page.locator('#vd-scenario').select_option('ready')
    page.locator('#vd-mode').select_option('hermes')
    page.locator('#vd-input').select_option('USB microphone')
    page.locator('#vd-echo').uncheck()
    page.locator('#vd-save').click()
    for layout in ('focus', 'control', 'desk'):
        page.locator(f'[data-design={layout}]').click()
        if page.locator('#vd-settings').is_hidden():
            page.locator('#vd-settings-toggle').click()
        expect(page.locator('#vd-mode')).to_have_value('hermes')
        expect(page.locator('#vd-input')).to_have_value('USB microphone')
        expect(page.locator('#vd-echo')).not_to_be_checked()
        expect(page.locator('.vd-message')).to_have_count(4)
    page.locator('#vd-start').click()
    expect(page.locator('#vd-call-status')).to_have_text('Listening')
    expect(page.locator('#vd-mode')).to_be_disabled()
    page.locator('#vd-mute').click()
    expect(page.locator('#vd-mute')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('#vd-finish')).to_be_disabled()
    page.locator('#vd-mute').click()
    page.locator('#vd-finish').click()
    expect(page.locator('#vd-call-status')).to_have_text('Speaking')
    expect(page.locator('.vd-message')).to_have_count(6)
    page.locator('#vd-interrupt').click()
    expect(page.locator('#vd-call-status')).to_have_text('Listening')
    page.locator('#vd-hangup').click()
    expect(page.locator('#vd-start')).to_be_enabled()
    expect(page.locator('#vd-mode')).to_be_enabled()
    expect(page.locator('.vd-message')).to_have_count(6)


def test_setup_failure_recovery_empty_and_canceled_start(preview):
    page = preview
    page.locator('#vd-scenario').select_option('no-key')
    expect(page.locator('#vd-empty')).to_be_visible()
    page.locator('#vd-start').click()
    expect(page.locator('#vd-error')).to_contain_text('Add a sample API key')
    page.locator('#vd-key').fill('sample-key-only')
    page.locator('#vd-save').click()
    page.locator('#vd-start').click()
    page.locator('#vd-hangup').click()
    expect(page.locator('#vd-feedback')).to_contain_text('Startup canceled')
    page.wait_for_timeout(750)
    expect(page.locator('#vd-call-status')).to_have_text('Ready when you are')
    page.locator('#vd-scenario').select_option('offline')
    page.locator('#vd-start').click()
    expect(page.locator('#vd-error')).to_contain_text('still unavailable')
    expect(page.locator('.vd-message')).to_have_count(4)
    page.locator('#vd-scenario').select_option('no-mic')
    expect(page.locator('#vd-start')).to_be_disabled()
    page.locator('#vd-refresh').click()
    expect(page.locator('#vd-start')).to_be_enabled()
    expect(page.locator('#vd-error')).to_be_hidden()
    page.locator('#vd-scenario').select_option('empty')
    page.locator('#vd-start').click()
    expect(page.locator('#vd-call-status')).to_have_text('Listening')
    page.locator('#vd-finish').click()
    expect(page.locator('#vd-empty')).to_be_hidden()
    expect(page.locator('.vd-message')).to_have_count(2)


@pytest.mark.parametrize('layout', ('desk', 'focus', 'control'))
def test_layouts_keep_controls_in_view_and_fit_narrow_empty_errors(preview, layout):
    page = preview
    page.locator(f'[data-design={layout}]').click()
    button = page.locator('#vd-hangup').bounding_box()
    assert button['y'] + button['height'] <= 900, 'Hang up must be visible on a desktop.'
    for width in (1440, 1100, 760, 390):
        page.set_viewport_size({'width': width, 'height': 900})
        for scenario in ('listening', 'empty', 'no-key', 'offline'):
            page.locator('#vd-scenario').select_option(scenario)
            for _ in range(2):
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                expect(page.get_by_role('heading', level=1)).to_be_visible()
                page.locator('#vd-theme').click()
    page.locator('#vd-scenario').select_option('listening')
    page.locator('#vd-transcript-toggle').click()
    expect(page.locator('#vd-transcript-body')).to_be_hidden()
    page.locator('#vd-transcript-toggle').click()
    expect(page.locator('#vd-transcript-body')).to_be_visible()


def test_shortcuts_avoid_fields_and_keys_are_never_persisted(preview):
    page = preview
    page.locator('#vd-call-status').click()
    page.keyboard.press('Alt+m')
    expect(page.locator('#vd-mute')).to_have_attribute('aria-pressed', 'true')
    page.locator('#vd-hangup').click()
    page.locator('#vd-connection-settings').evaluate('(el) => el.open = true')
    page.locator('#vd-key').fill('sample-do-not-save')
    page.keyboard.press('Alt+m')
    expect(page.locator('#vd-mute')).to_have_attribute('aria-pressed', 'false')
    page.locator('#vd-save').click()
    assert 'sample-do-not-save' not in page.evaluate('JSON.stringify({...localStorage})')
    page.reload()
    page.locator('#vd-connection-settings').evaluate('(el) => el.open = true')
    expect(page.locator('#vd-key')).to_have_value('')
