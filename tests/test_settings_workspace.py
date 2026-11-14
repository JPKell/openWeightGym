"""One settings workspace and catalogue-filtered routes preserve native service forms."""
import pytest
from test_weightroom import desk, sign_in
from weightroom.navigation import navigation
from weightroom.settings_views import PAGES, filtered


@pytest.mark.parametrize('section', list(PAGES))
def test_group_pages_share_the_workspace_and_require_settings_permission(desk, section):
    browser, _, _ = desk
    title, path = PAGES[section]
    sign_in(browser, 'pat')
    assert browser.get(path).status_code == 403
    sign_in(browser, 'jordan')
    response = browser.get(path)
    assert response.status_code == 200 and 'data-settings-workspace' in response.text
    assert title in response.text and 'aria-label="Settings sections"' in response.text
    nav = navigation(path, ['manage-settings'], True)
    assert nav['active'] == path and nav['section'] == 'Settings'


def test_existing_settings_surfaces_share_the_same_workspace(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    for path in ('/compute-nodes', '/system/voice', '/system/audio', '/tools', '/allowances', '/settings/image'):
        response = browser.get(path)
        assert response.status_code == 200
        assert 'data-settings-workspace' in response.text, path


def test_catalogue_page_filter_preserves_owner_and_native_form_contract():
    programs = [{'program':'turnstile', 'settings':[
        {'name':'wait','value':180,'when':'now'}, {'name':'keep_days','value':90,'when':'now'},
        {'name':'host','value':'127.0.0.1','when':'file'}]}]
    assert [r['name'] for p in filtered(programs, 'requests') for r in p['settings']] == ['wait']
    assert programs[0]['settings'][1]['name'] == 'keep_days'


def test_grouped_settings_save_and_errors_return_to_the_same_page(desk):
    from loadcoach.settings import said
    from turnstile.app import Settings
    browser, switch, _ = desk
    switch.front.state.console_settings.changes.seen('turnstile', said(vars(Settings()), {}, {}, 'turnstile'))
    sign_in(browser, 'jordan')
    page = browser.get('/settings/requests')
    assert 'name="return_to" value="/settings/requests"' in page.text
    assert 'name="value-wait"' in page.text
    saved = browser.post('/settings/turnstile', data={'return_to':'/settings/requests', 'value-wait':'181'})
    assert saved.status_code == 200 and 'Requests and queue' in saved.text and 'Saved.' in saved.text
    invalid = browser.post('/settings/turnstile', data={'return_to':'/settings/requests', 'value-wait':'0'})
    assert invalid.status_code == 400 and 'data-settings-workspace' in invalid.text
    assert 'Requests and queue' in invalid.text
    assert browser.post('/settings/turnstile', data={'return_to':'/settings/requests','reset':'wait'}).status_code == 200


def test_general_moves_monitoring_and_retention_to_their_groups(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    general = browser.get('/settings').text
    assert 'name="value-observation_keep_days"' not in general
    assert 'name="value-third_party_services"' not in general
    assert 'name="value-observation_keep_days"' in browser.get('/settings/retention').text
    assert 'third-party-services-present' in browser.get('/settings/alerts').text
    assert 'Read-only machine values' not in general


def test_unknown_settings_page_uses_the_shared_error_frame(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    response = browser.get('/settings/not-a-section')
    assert response.status_code == 404
    assert 'That settings page does not exist.' in response.text
    assert 'id="page-content"' in response.text
