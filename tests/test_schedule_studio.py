"""Live Schedule Studio contracts over isolated front/compute apps."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from loadcoach.schedule_time import next_at
from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright
from test_ui_consistency import MEASURE

expect = playwright.expect


@pytest.fixture
def studio(desk):
    server, wire, users = desk
    sign_in(server, 'jordan')
    data = dict(kind='backup', timezone='America/Vancouver', time='23:59',
                weekdays=['0','1','2','3','4','5','6'], missed='skip', retention='10')
    for name in ('Alpha backup', 'Beta backup'):
        assert server.post('/schedules', data=dict(data, name=name)).status_code == 200
    return server, wire, users


def test_studio_keeps_drafts_and_filters_across_swaps_and_saves_selected_only(studio):
    server, _, _ = studio
    rows = server.app.state.schedules.rows()
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/schedules')
        page.locator('#schedule-name').fill('Alpha draft')
        page.locator('#repeat').select_option('weekdays')
        page.locator('#schedule-time').fill('07:15')
        expect(page.locator('#cron-value')).to_have_text('15 7 * * 1-5')
        expect(page.locator('#next-times li')).to_have_count(3)
        page.locator('#schedule-search').fill('backup')
        page.get_by_role('link', name='Edit Beta backup', exact=True).click()
        expect(page.locator('#schedule-name')).to_have_value('Beta backup')
        expect(page.locator('#schedule-search')).to_have_value('backup')
        page.locator('#schedule-name').fill('Beta saved')
        page.get_by_role('button', name='Save schedule', exact=True).click()
        expect(page.locator('#schedule-draft-status')).to_have_text('Saved')
        assert server.app.state.schedules.rows()[0]['name'] == 'Alpha backup'
        page.get_by_role('link', name='Edit Alpha backup', exact=True).click()
        expect(page.locator('#schedule-name')).to_have_value('Alpha draft')
        expect(page.locator('#schedule-time')).to_have_value('07:15')
        expect(page.locator('#cron-value')).to_have_text('15 7 * * 1-5')
        page.locator('[data-discard-schedule]').click()
        expect(page.locator('#schedule-name')).to_have_value('Alpha backup')
        expect(page.locator('#schedule-draft-status')).to_have_text('Saved')
        page.get_by_role('button', name='Pause Alpha backup', exact=True).click()
        expect(page.locator('#schedule-inspector [data-state]').first).to_have_text('Paused')
        expect(page.locator('#next-times')).to_contain_text('Resume to start')
        page.locator('#schedule-status-filter').select_option('paused')
        expect(page.locator('[data-schedule-row]:visible')).to_have_count(1)
        page.locator('#schedule-search').fill('nothing matches')
        expect(page.locator('#schedule-no-matches')).to_be_visible()
        assert not errors
        assert rows[0]['grant_token'] not in page.content()
        browser.close()


def test_timing_preview_is_authorized_offline_and_uses_dispatch_calendar(studio):
    server, wire, _ = studio
    before = server.app.state.schedules.rows()
    wire.off = True
    spec = dict(time='02:30', timezone='Europe/London', weekdays=[0,2,6])
    response = server.get('/schedules', params=[('part','timing'),('time',spec['time']),('timezone',spec['timezone']),
                                               *(('weekdays', day) for day in spec['weekdays'])])
    assert response.status_code == 200 and '30 2 * * 1,3,0' in response.text
    # The exact display comes from the same next_at implementation as dispatch.
    at = next_at(spec, datetime.now().timestamp())
    assert datetime.fromtimestamp(at, ZoneInfo(spec['timezone'])).strftime('%a %d %b, %H:%M %Z') in response.text
    assert server.get('/schedules?part=timing&time=02:30&timezone=Not/AZone&weekdays=0').status_code == 200
    assert 'known timezone' in server.get('/schedules?part=timing&time=02:30&timezone=Not/AZone&weekdays=0').text
    assert 'at least one' in server.get('/schedules?part=timing&time=02:30&timezone=UTC').text
    assert server.app.state.schedules.rows() == before
    wire.off = False
    sign_in(server, 'pat')
    assert server.get('/schedules?part=timing&time=02:30&timezone=UTC&weekdays=0').status_code == 403


def test_failed_save_keeps_draft_and_other_owner_is_read_only(studio):
    server, _, _ = studio
    identifier = server.app.state.schedules.rows()[0]['id']
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/schedules?edit='+identifier)
        page.locator('#schedule-name').fill('Keep this draft')
        page.locator('#schedule-zone').fill('Not/AZone')
        page.get_by_role('button', name='Save schedule', exact=True).click()
        expect(page.locator('.said-box.alert-danger')).to_be_visible()
        expect(page.locator('#schedule-name')).to_have_value('Keep this draft')
        expect(page.locator('#schedule-zone')).to_have_value('Not/AZone')
        expect(page.locator('#schedule-draft-status')).to_have_text('Unsaved changes')
        assert server.app.state.schedules.rows()[0]['name'] == 'Alpha backup'
        page.locator('[data-discard-schedule]').click()
        expect(page.locator('#schedule-name')).to_have_value('Alpha backup')
        browser.close()
    sign_in(server, 'sam')
    assert server.get('/schedules?edit='+identifier).status_code == 403
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/schedules?view='+identifier)
        expect(page.locator('#schedule-name')).to_have_value('Alpha backup')
        expect(page.locator('#schedule-name')).to_be_disabled()
        expect(page.locator('#schedule-form button[type=submit]')).to_be_disabled()
        expect(page.locator('[data-schedule-delete]')).to_have_count(0)
        browser.close()


def test_multiselect_drafts_new_schedule_and_confirmed_delete(studio):
    server, _, users = studio
    store = server.app.state.schedules
    spec = dict(store.rows()[0]['spec'], kind='outstanding', machine='compute',
                model_ids=['missing-a','missing-b'], benchmarks=['removed-a','removed-b'])
    store.create('c'*32, 'Scope work', users.identity('jordan'), 'jordan', spec, 'test-only-unused-grant')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/schedules?edit='+'c'*32)
        page.get_by_text('Eligible models & benchmarks', exact=True).click()
        page.locator('#schedule-models').select_option(['missing-b'])
        page.locator('#schedule-benchmarks').select_option(['removed-b'])
        expect(page.locator('#schedule-model')).to_be_disabled()
        expect(page.locator('#schedule-node')).to_be_enabled()
        page.get_by_role('link', name='Edit Alpha backup', exact=True).click()
        page.get_by_role('link', name='Edit Scope work', exact=True).click()
        expect(page.locator('#schedule-models')).to_have_values(['missing-b'])
        expect(page.locator('#schedule-benchmarks')).to_have_values(['removed-b'])
        page.get_by_role('link', name='＋ New schedule', exact=True).click()
        expect(page.locator('#schedule-name')).to_have_value('')
        page.locator('#schedule-name').fill('Created in studio')
        page.get_by_role('button', name='Create schedule', exact=True).click()
        expect(page.locator('#editor-title')).to_have_text('Created in studio')
        expect(page.locator('#schedule-draft-status')).to_have_text('Saved')
        assert any(row['name'] == 'Created in studio' for row in store.rows())
        page.get_by_role('button', name='Delete Created in studio', exact=True).click()
        delete = page.locator('[data-schedule-delete] button')
        expect(delete).to_have_text('Are you sure?')
        expect(page.locator('dialog[open]')).to_have_count(0)
        assert any(row['name'] == 'Created in studio' for row in store.rows())
        page.keyboard.press('Escape')
        expect(delete).to_have_text('Delete schedule')
        delete.click()
        expect(delete).to_have_text('Are you sure?')
        delete.click()
        expect(page.locator('[data-schedule-row]')).to_have_count(3)
        assert all(row['name'] != 'Created in studio' for row in store.rows())
        page.get_by_role('link', name='Edit Scope work', exact=True).click()
        expect(page.locator('#schedule-models')).to_have_values(['missing-b'])
        page.locator('[data-discard-schedule]').click()
        expect(page.locator('#schedule-models')).to_have_values(['missing-a','missing-b'])
        browser.close()


@pytest.mark.parametrize('width', [1440,390])
def test_studio_sizes_accessibility_empty_and_populated_states(studio, width):
    server, _, _ = studio
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size(dict(width=width, height=1000))
        def check_card_spacing():
            if width <= 800:
                return
            spacing = page.locator('.sc-body').evaluate('''body => ({
                actual: body.querySelector('#history-panel').getBoundingClientRect().top -
                        body.querySelector('#schedule-list').getBoundingClientRect().bottom,
                intended: parseFloat(getComputedStyle(body).rowGap)
            })''')
            assert abs(spacing['actual'] - spacing['intended']) < 1, spacing
        for theme in ('light','dark'):
            page.emulate_media(color_scheme=theme)
            for path in ('/schedules', '/schedules?new=1'):
                page.goto('http://console.test'+path)
                result = page.evaluate(MEASURE)
                assert not result['overflow'], result
                assert not result['namelessFields'], result
                assert not result['invalidControls'], result
                assert not result['mismatches'], result
                assert all(item['size'] == '14.4px' for item in result['sections'])
                check_card_spacing()
        with server.app.state.schedules.db:
            server.app.state.schedules.db.execute('UPDATE recurring_schedules SET deleted=1')
        page.goto('http://console.test/schedules')
        expect(page.locator('#schedule-list')).to_contain_text('No schedules yet.')
        assert not page.evaluate(MEASURE)['overflow']
        check_card_spacing()
        browser.close()
