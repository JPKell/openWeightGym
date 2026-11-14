"""Persisted personal settings, scoped sign-ins and real account desk behavior."""

import asyncio
import json
import time

import httpx
import pytest

from loadcoach.preferences import DEFAULTS
from loadcoach.users import hash_key, key_identity
from test_weightroom import desk, sign_in  # noqa: F401


def api(server, switch, key, method, path, **kwargs):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=switch.front), base_url='http://front:8442') as client:
            return await client.request(method, path, headers={'authorization': 'Bearer ' + key}, **kwargs)
    return server.portal.call(send)


def test_preferences_are_validated_atomic_and_account_scoped(desk):
    server, switch, users = desk
    sign_in(server, 'pat')
    key = server.cookies['weightroom']
    for chosen in ({'timezone':'not/a/zone'}, {'motion':'anything'}, {'text_size':True}, {'clock':24},
                   {'start_page':'https://example.com'}, {'can':['superuser']}, {'notices_read_at':float('inf')}):
        if chosen.get('notices_read_at'):
            with pytest.raises(ValueError):
                users.choose('pat', chosen)
            continue
        answer = api(server, switch, key, 'POST', '/loadcoach/me', json={'show_telemetry':False, **chosen})
        assert answer.status_code == 400
        assert users.choices('pat')['show_telemetry'] is True
    saved = server.post('/account/preferences/workspace', data={
        'timezone':'Asia/Tokyo', 'clock':'12', 'date_format':'iso', 'start_page':'/chat', 'chat_model':''})
    assert saved.status_code == 200 and 'Preferences saved.' in saved.text
    assert users.choices('pat')['timezone'] == 'Asia/Tokyo'
    assert users.choices('jordan') == DEFAULTS
    assert server.post('/account/preferences/appearance', data={'text_size':'large','motion':'reduce'}).status_code == 200
    page = server.get('/status').text
    assert 'data-text-size="large"' in page and 'data-motion="reduce"' in page
    before = users.choices('pat')
    assert server.post('/account/preferences/workspace', headers={'origin':'https://other.test'}, data={'timezone':'UTC'}).status_code == 403
    assert users.choices('pat') == before


def test_session_details_are_private_revocation_is_exact_and_keys_survive(desk):
    server, switch, users = desk
    first = users.new_key('pat', hours=2)
    foreign = users.new_key('jordan', hours=2)
    permanent = users.new_key('pat', 'CLI')
    expired = users.new_key('pat', hours=-1)
    sign_in(server, 'pat', user_agent='unused')
    current = server.cookies['weightroom']
    response = api(server, switch, current, 'GET', '/loadcoach/sessions')
    sessions = response.json()['sessions']
    assert len(sessions) == 2 and sum(row['current'] for row in sessions) == 1
    assert all(raw not in response.text for raw in (first, foreign, permanent, expired, hash_key(first), hash_key(current)))
    assert api(server, switch, current, 'DELETE', '/loadcoach/sessions/' + key_identity(hash_key(foreign))).status_code == 404
    assert api(server, switch, current, 'DELETE', '/loadcoach/sessions/' + key_identity(hash_key(permanent))).status_code == 404
    assert api(server, switch, current, 'DELETE', '/loadcoach/sessions/' + key_identity(hash_key(current))).status_code == 404
    other = next(row for row in sessions if not row['current'])
    assert server.post('/account/sessions/' + other['id'] + '/remove').status_code == 200
    assert users.by_key(first) is None and users.by_key(current) and users.by_key(permanent)
    newer = users.new_key('pat', hours=2)
    assert server.post('/account/sessions/end-others').status_code == 200
    assert users.by_key(newer) is None and users.by_key(permanent) and users.by_key(current)
    assert users.by_key(foreign)


def test_saved_start_page_respects_explicit_deep_links_and_default_chat(desk):
    server, switch, users = desk
    users.choose('pat', {'start_page':'/chat'})
    assert sign_in(server, 'pat').url.path == '/chat'
    assert sign_in(server, 'pat', next='/account/appearance').url.path == '/account/appearance'
    assert sign_in(server, 'pat', next='/status').url.path == '/status'
    key = server.cookies['weightroom']
    assert server.post('/account/preferences/workspace', data={'chat_model':'not-allowed'}).status_code == 400
    users.choose('pat', {'chat_model':'missing-saved-model'})
    assert 'missing-saved-model · currently unavailable' in server.get('/account/workspace').text
    assert 'value="missing-saved-model"' not in server.get('/chat').text


def test_notices_respect_owner_preferences_read_state_and_sensitivity(desk):
    server, switch, users = desk
    sign_in(server, 'pat')
    log = switch.front.state.log
    log.add('user', 'key-added', 'jordan', name='jordan', label='FOREIGN-SECRET-LABEL')
    log.add('user', 'key-added', 'jordan', name='pat', label='PRIVATE-KEY-LABEL')
    log.add('batch', 'completed', 'pat', batch='own-batch')
    log.add('batch', 'completed', 'jordan', batch='foreign-batch')
    users.choose('pat', {'alert_jobs':True})
    feed = server.get('/account/notices')
    assert feed.status_code == 200 and feed.json()['unread'] >= 2
    assert 'API key created' in feed.text and 'Batch completed' in feed.text
    assert all(value not in feed.text for value in ('FOREIGN-SECRET-LABEL','PRIVATE-KEY-LABEL','foreign-batch'))
    assert 'no-store' in feed.headers['cache-control']
    assert server.post('/account/notices/read').status_code == 200
    assert server.get('/account/notices').json()['unread'] == 0
    users.choose('pat', {'alert_security':False,'alert_jobs':False,'alert_spending':False})
    assert server.get('/account/notices').json()['items'] == []


def test_preference_export_and_audio_partial_updates_exclude_credentials(desk):
    server, _, users = desk
    sign_in(server, 'pat')
    key = server.cookies['weightroom']
    assert server.post('/soundroom-api/preferences', json={'format':'flac','autoplay':True}).status_code == 200
    assert server.post('/soundroom-api/preferences', json={'voice':'saved-voice'}).status_code == 200
    prefs = server.get('/soundroom-api/preferences').json()
    assert prefs['format'] == 'flac' and prefs['autoplay'] is True
    assert server.post('/soundroom-api/preferences', json={'autoplay':'yes'}).status_code == 400
    exported = server.get('/account/preferences.json')
    assert exported.status_code == 200 and exported.json()['audio']['format'] == 'flac'
    assert key not in exported.text and hash_key(key) not in exported.text
    assert 'password' not in exported.text and 'notices_read_at' not in exported.text
    assert 'attachment' in exported.headers['content-disposition']


def test_settings_remain_available_offline_and_unavailable_defaults_can_be_retained(desk):
    server, switch, users = desk
    sign_in(server, 'pat')
    users.choose('pat', {'chat_model':'temporarily-offline'})
    switch.off = True
    for path in ('/account','/account/keys','/account/appearance','/account/workspace','/account/privacy','/account/notices'):
        assert server.get(path).status_code == 200, path
    result = server.post('/account/preferences/workspace', data={
        'start_page':'/status','chat_model':'temporarily-offline','timezone':'UTC','clock':'24','date_format':'iso'})
    assert result.status_code == 200 and users.choices('pat')['chat_model'] == 'temporarily-offline'


def test_browser_layout_controls_time_and_saved_accessibility(desk, tmp_path):
    from test_console_browser import browser_context, playwright
    server, _, users = desk
    sign_in(server, 'pat')
    errors = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.on('pageerror', lambda error: errors.append(str(error)))
        for width in (1440, 390):
            page.set_viewport_size({'width':width,'height':1000})
            for path in ('/account','/account/keys','/account/appearance','/account/audio','/account/workspace','/account/privacy','/account/logs'):
                page.goto('http://console.test' + path)
                assert page.locator('main h1').count() == 1
                assert page.locator('.account-rail').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width,path)
                assert page.evaluate('''() => [...document.querySelectorAll('main input, main select')]
                    .filter(el => el.getBoundingClientRect().width && el.type !== 'hidden')
                    .every(el => el.labels?.length || el.getAttribute('aria-label'))''')
        page.goto('http://console.test/account/workspace')
        page.locator('#timezone').select_option('Asia/Tokyo')
        page.locator('#date_format').select_option('iso')
        page.locator('#clock').select_option('12')
        page.get_by_role('button', name='Save workspace defaults').click()
        assert users.choices('pat')['timezone'] == 'Asia/Tokyo'
        page.goto('http://console.test/account')
        assert 'Asia/Tokyo' == page.evaluate('SuiteTime.zone')
        result = page.evaluate('SuiteTime.format("2026-11-14T00:00:00Z")')
        assert '2026-11-14' in result and '9:00' in result
        page.goto('http://console.test/account/appearance')
        page.locator('#text_size').select_option('large')
        page.locator('#motion').select_option('reduce')
        assert page.locator('html').get_attribute('data-motion') == 'reduce'
        page.get_by_role('button', name='Save display preferences').click()
        page.goto('http://console.test/account/keys')
        assert page.locator('html').get_attribute('data-text-size') == 'large'
        assert page.locator('html').get_attribute('data-motion') == 'reduce'
        assert not errors
        browser.close()


def test_default_model_is_selected_only_for_new_chat_and_allowed_models(desk, tmp_path):
    from loadcoach.models import Models
    server, switch, users = desk
    models = Models(str(tmp_path / 'loadcoach.db'))
    for name in ('first-model', 'preferred-model', 'private-model'):
        models.put(name, {'gpus':0, 'flags':{'model':name + '.gguf'}})
    users.change('pat', own={'models':['first-model', 'preferred-model']})
    sign_in(server, 'pat')
    key = server.cookies['weightroom']
    assert api(server, switch, key, 'POST', '/loadcoach/me', json={'chat_model':'private-model'}).status_code == 200
    assert 'value="private-model"' not in server.get('/chat').text
    assert api(server, switch, key, 'POST', '/loadcoach/me', json={'chat_model':'preferred-model'}).status_code == 200
    assert 'value="preferred-model" data-plugin-model="false" selected' in server.get('/chat').text
    explicit = server.get('/chat?model=first-model').text
    assert 'value="first-model" data-plugin-model="false" selected' in explicit


def test_spending_notice_uses_only_owned_advisory_policies(desk):
    from test_allowances import ending
    from turnstile.allowances import Allowances
    server, switch, users = desk
    sign_in(server, 'pat')
    principal = users.by_key(server.cookies['weightroom'])
    log = switch.front.state.log
    policy = Allowances(users, log)
    policy.save(users.by_name('jordan'), 'user', principal.identity, 10)
    policy.save(users.by_name('jordan'), 'user', users.identity('jordan'), 1)
    ending(log, principal, 8.5)
    ending(log, users.by_name('jordan'), 100)
    feed = server.get('/account/notices').json()
    warnings = [row for row in feed['items'] if row['category'] == 'Spending']
    assert len(warnings) == 1 and '85%' in warnings[0]['title']
    assert warnings[0]['unread'] is True and 'jordan' not in warnings[0]['title']
    server.post('/account/notices/read')
    assert not any(item['unread'] for item in server.get('/account/notices').json()['items'])


def test_menu_navigation_preserves_nonsensitive_drafts_and_audio_saves(desk):
    from test_console_browser import browser_context, playwright
    server, _, users = desk
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/account/workspace')
        page.locator('#timezone').select_option('Asia/Tokyo')
        page.goto('http://console.test/account/appearance')
        assert page.locator('a[href="/account/workspace"] [data-account-draft]').first.is_visible()
        page.goto('http://console.test/account/workspace')
        assert page.locator('#timezone').input_value() == 'Asia/Tokyo'
        assert users.choices('pat')['timezone'] == 'device'
        page.get_by_role('button', name='Discard changes').click()
        assert page.locator('#timezone').input_value() == 'device'
        page.goto('http://console.test/account/audio')
        page.locator('#ap-fields:not([disabled])').wait_for()
        page.locator('#ap-format').select_option('flac')
        page.locator('#ap-autoplay').check()
        page.goto('http://console.test/account/workspace')
        page.goto('http://console.test/account/audio')
        page.locator('#ap-fields:not([disabled])').wait_for()
        assert page.locator('#ap-format').input_value() == 'flac'
        assert page.locator('#ap-autoplay').is_checked()
        page.get_by_role('button', name='Save audio preferences').click()
        page.get_by_text('Saved.', exact=True).wait_for()
        assert server.get('/soundroom-api/preferences').json()['autoplay'] is True
        assert page.evaluate('Object.keys(sessionStorage).filter(key => key.startsWith("weightroom-account-draft-v1:")).length') == 0
        browser.close()


def test_autoplay_only_plays_new_owned_audio_when_enabled(desk):
    from test_console_browser import browser_context, playwright
    from test_soundroom_browser import state, asset, capture_audio
    server, _, _ = desk
    sign_in(server, 'pat')
    payload = state(assets=[asset()], preferences={'autoplay':True})
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(payload, []))
        page.add_init_script('window.played=[]; HTMLMediaElement.prototype.play=function(){played.push(this.src);return Promise.resolve();}')
        page.goto('http://console.test/chat/music')
        page.locator('#sr-model option').first.wait_for(state='attached')
        assert page.evaluate('played.length') == 0
        fresh = asset(2); fresh['created'] = time.time() + 1
        payload['assets'].insert(0, fresh)
        page.evaluate('soundroomRefresh()')
        assert page.evaluate('played.length') == 1
        page.evaluate('soundroomRefresh()')
        assert page.evaluate('played.length') == 1
        payload['preferences']['autoplay'] = False
        newer = asset(3); newer['created'] = time.time() + 1
        payload['assets'].insert(0, newer)
        page.evaluate('soundroomRefresh()')
        assert page.evaluate('played.length') == 1
        browser.close()


def test_workflow_notices_use_stable_ownership_and_real_review_states(desk):
    from test_workflow_pages import seed
    server, _, users = desk
    number = seed(server, users)
    users.choose('jordan', {'alert_jobs':True})
    sign_in(server, 'jordan')
    feed = server.get('/account/notices').json()
    row = next(item for item in feed['items'] if item['id'] == 'workflow-' + number)
    assert row['title'] == 'Cedar draft · Draft failed'
    assert row['href'] == '/workflows/' + number
    users.choose('pat', {'alert_jobs':True})
    sign_in(server, 'pat')
    assert all(item['id'] != 'workflow-' + number for item in server.get('/account/notices').json()['items'])


def test_activity_filters_are_owner_scoped_and_ignore_captured_text(desk):
    server, switch, users = desk
    sign_in(server, 'pat')
    key = server.cookies['weightroom']
    log = switch.front.state.log
    log.add('request', 'done', 'pat', model='own-model', status=201, text={'prompt':'private-needle'})
    log.add('request', 'done', 'jordan', model='foreign-model', status=503)
    found = api(server, switch, key, 'GET', '/loadcoach/account-events?q=own-model&kind=request').json()
    assert found['total'] == 1 and found['events'][0]['model'] == 'own-model'
    assert api(server, switch, key, 'GET', '/loadcoach/account-events?q=foreign-model').json()['total'] == 0
    assert api(server, switch, key, 'GET', '/loadcoach/account-events?q=private-needle').json()['total'] == 0
    assert api(server, switch, key, 'GET', '/loadcoach/account-events?q=%25').json()['total'] == 0
    assert api(server, switch, key, 'GET', '/loadcoach/account-events?kind=other').status_code == 400
    page = server.get('/account/logs?q=own-model&kind=request').text
    assert '1 matching record' in page and 'foreign-model' not in page


def test_preferred_time_zone_keeps_existing_filter_wire_values():
    from pathlib import Path
    from test_console_browser import playwright
    script = (Path(__file__).resolve().parents[1] / 'weightroom/static/console.js').read_text()
    markup = '''<html data-personal-preferences='{"timezone":"Asia/Tokyo"}'><body>
      <form id="filter"><label for="at">Around</label><input id="at" name="at" type="datetime-local"
      data-input-zone="UTC" data-instant="2026-10-30T12:00:00Z"><button>Show</button></form>'''
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(timezone_id='America/Vancouver')
        page = context.new_page()
        page.set_content(markup + '<script>' + script + '</script></body></html>')
        playwright.expect(page.locator('#at')).to_have_value('2026-10-30T21:00')
        page.evaluate('document.getElementById("filter").addEventListener("submit",event=>{event.preventDefault();window.sent=new FormData(event.target).get("at");})')
        page.get_by_role('button', name='Show').click()
        assert page.evaluate('window.sent') == '2026-10-30T12:00'
        playwright.expect(page.locator('#at')).to_have_value('2026-10-30T21:00')
        browser.close()
