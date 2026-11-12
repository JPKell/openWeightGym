"""Browser contracts for the sample-only Schedules layouts and timing editor."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-schedule-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Sample proposals must never contact live services.'
        browser.close()


def test_layouts_states_themes_and_editor_fit_phone(preview):
    page = preview
    for width in (1600, 768, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        for theme in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for layout in ('studio', 'planner', 'ledger'):
                page.locator(f'[data-design="{layout}"]').click()
                expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
                for scenario in ('normal', 'empty', 'attention'):
                    page.locator('#scenario').select_option(scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, layout, scenario)
                page.locator('#scenario').select_option('normal')
                page.locator('[data-new]').first.click()
                assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                    .filter(el => el.getBoundingClientRect().width && !el.closest('[hidden]'))
                    .every(el => el.labels?.length || el.getAttribute('aria-label'))''')
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                    return ids.length === new Set(ids).size;
                }''')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if layout != 'studio':
                    page.keyboard.press('Escape')
    light = page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor')
    page.locator('#theme').click()
    assert page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor') != light


def test_drafts_persist_across_layouts_and_create_valid_weekday_timing(preview):
    page = preview
    page.locator('#schedule-name').fill('Recovery draft')
    page.locator('#repeat').select_option('weekdays')
    page.locator('#schedule-time').fill('07:15')
    expect(page.locator('#cron-value')).to_have_text('15 7 * * 1-5')
    expect(page.locator('#next-times li').first).to_contain_text('Thu 12 Nov, 07:15')
    page.locator('[data-design="ledger"]').click()
    page.locator('#ledger-rows [data-open][data-schedule="nightly"]').click()
    expect(page.locator('#schedule-name')).to_have_value('Recovery draft')
    expect(page.locator('#draft-state')).to_have_text('Unsaved changes')
    page.locator('#save-schedule').click()
    expect(page.locator('#feedback')).to_contain_text('Saved Recovery draft')
    page.keyboard.press('Escape')
    page.locator('[data-design="planner"]').click()
    # Calendar uses scheduled start times, in the displayed timezone.
    thursday = page.locator('[data-calendar-day]').nth(3)
    expect(thursday.locator('[data-open][data-schedule="nightly"]')).to_contain_text('07:15')
    page.locator('[data-new]').first.click()
    page.locator('#schedule-name').fill('UTC morning')
    page.locator('#schedule-zone').fill('UTC')
    page.locator('#schedule-time').fill('09:05')
    page.locator('#save-schedule').click()
    expect(page.locator('#feedback')).to_contain_text('Saved UTC morning')
    expect(page.locator('#cron-value')).to_have_text('5 9 * * *')
    expect(page.locator('#next-times li')).to_have_count(3)


def test_scopes_validation_pause_filters_and_deletion_preserve_history(preview):
    page = preview
    page.locator('#schedule-kind').select_option('outstanding')
    expect(page.locator('#schedule-node')).to_be_enabled()
    expect(page.locator('#schedule-model')).to_be_disabled()
    page.locator('#schedule-zone').fill('Not/AZone')
    page.locator('#save-schedule').click()
    expect(page.locator('#form-error')).to_contain_text('valid IANA timezone')
    page.locator('#discard').click()
    for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'):
        page.get_by_label(day, exact=True).uncheck()
    page.locator('#save-schedule').click()
    expect(page.locator('#form-error')).to_contain_text('at least one weekday')
    page.locator('#discard').click()
    page.locator('#schedule-list [data-toggle]').first.click()
    expect(page.locator('#editor-state')).to_have_text('Paused')
    expect(page.locator('#next-times')).to_contain_text('Resume to start')
    page.locator('#status-filter').select_option('paused')
    expect(page.locator('#schedule-list .sc-schedule-row')).to_have_count(2)
    page.locator('#status-filter').select_option('all')
    page.locator('#delete-schedule').click()
    expect(page.locator('#delete-schedule')).to_have_text('Are you sure?')
    expect(page.locator('#schedule-list .sc-schedule-row')).to_have_count(6)
    expect(page.locator('#danger-confirmation')).to_have_count(0)
    page.locator('#schedule-list [data-open][data-schedule="quality"]').click()
    expect(page.locator('#delete-schedule')).to_have_text('Delete schedule')
    page.locator('#schedule-list [data-open][data-schedule="nightly"]').click()
    page.locator('#delete-schedule').click()
    page.locator('#delete-schedule').click()
    expect(page.locator('#schedule-list .sc-schedule-row')).to_have_count(5)
    expect(page.locator('#history-list')).to_contain_text('Nightly recovery')
    # Escape cancels a later first click without opening a popup.
    page.locator('#delete-schedule').click()
    page.keyboard.press('Escape')
    expect(page.locator('#delete-schedule')).to_have_text('Delete schedule')
    expect(page.locator('#schedule-list .sc-schedule-row')).to_have_count(5)
    page.locator('#search').fill('no matching schedule')
    expect(page.locator('#schedule-list')).to_contain_text('No schedules match')
