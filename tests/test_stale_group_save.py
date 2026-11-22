"""A group page left open after the group was removed cannot bring it back (QA-046)."""

from test_weightroom import desk, sign_in  # noqa: F401  (the first sets a check up)


def test_saving_a_removed_group_from_a_stale_page_is_refused_and_does_not_recreate_it(desk):
    browser, _, users = desk
    sign_in(browser, 'jordan')
    browser.post('/groups', data={'name': 'qa-stale'})
    assert browser.get('/groups/qa-stale').status_code == 200  # the page another tab keeps open
    removed = browser.post('/groups/qa-stale', data={'do': 'remove'})
    assert 'Removed qa-stale.' in removed.text and browser.get('/groups/qa-stale').status_code == 404
    for do in ('save', 'limits', 'abilities'):
        stale = browser.post('/groups/qa-stale', data={'do': do, 'priority': 'high', 'max_sensitivity': 'normal'})
        assert stale.status_code == 404 and 'it was removed, so nothing was saved' in stale.text.replace('&#39;', "'") if do == 'save' else stale.status_code in (400, 404)
        assert users.groups.get('qa-stale') is None


def test_the_api_replaces_only_a_group_that_exists_when_asked_to(desk):
    browser, _, users = desk
    sign_in(browser, 'jordan')
    browser.post('/groups', data={'name': 'qa-kept'})
    users.groups.remove('qa-kept')
    browser.post('/groups', data={'name': 'qa-kept'})
    saved = browser.post('/groups/qa-kept', data={'do': 'save', 'priority': 'low', 'max_sensitivity': 'normal'})
    assert 'Saved.' in saved.text and users.groups.stored('qa-kept')['priority'] == 'low'
