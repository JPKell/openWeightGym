"""Status verdicts, target comparisons, polling, selection and operator call controls."""
from copy import deepcopy
import pytest
from starlette.testclient import TestClient
from test_voice_admin import voice_panel
from test_weightroom import sign_in
from voice.latency import Recent


def payload():
    timings = Recent()
    timings.update('safe-session', 1, 'desktop', 'direct', {'end_to_audio_seconds': 3.2,
        'basis': 'input_end_received', 'profile': 'default', 'model': '<script>private()</script>', 'revision': 'r1'})
    return {'version': {'commit': 'f' * 40, 'uptime_seconds': 10}, 'entries': {
        'browser': {'configured': True, 'listening': True, 'can_start': True, 'reason': 'Ready'},
        'desktop': {'configured': True, 'listening': True, 'can_start': True, 'reason': 'Ready'},
        'telephone': {'configured': False, 'listening': True, 'can_start': False, 'reason': 'Telephone is off.'},
        'hermes': {'configured': False, 'listening': False, 'can_start': False, 'reason': 'Not configured.'}},
        'calls': [], 'readiness': {'profiles': {'default': {'recognition': True, 'synthesis': True, 'model': True, 'can_start': True, 'reason': None}}},
        'telephone': {'numbers': []}, 'latency': timings.snapshot(), 'recordings': {'source': 'Cached', 'revision': 'r1'}}


@pytest.mark.parametrize('state', ['gateway_down', 'compute_asleep', 'phone_off', 'phone_ready', 'active', 'no_timings'])
def test_status_feature_states_render_without_false_ready_or_private_text(tmp_path, state):
    status = payload()
    if state == 'gateway_down':
        status = 503
    elif state == 'compute_asleep':
        status['entries']['browser'].update(can_start=False, reason='Compute is asleep.')
        status['readiness']['profiles']['default'].update(can_start=False, reason='Compute is asleep.')
    elif state == 'phone_ready':
        status['entries']['telephone'].update(configured=True, can_start=True, reason='Ready')
    elif state == 'active':
        status['calls'] = [{'id': 'call123', 'source': 'desktop', 'number': None, 'account': 'pat', 'mode': 'direct',
            'profile': 'default', 'model': 'small', 'duration_seconds': 20, 'turns': 3, 'state': 'speaking'}]
    elif state == 'no_timings':
        status['latency'] = {}
    app, _, _, calls, users, switch = voice_panel(tmp_path, voice_status_data=status)
    with TestClient(app) as client:
        sign_in(client, 'jordan')
        response = client.get('/system/voice/status')
        assert response.status_code == 200 and 'hx-trigger="every 5s"' in response.text
        assert '<script>private()</script>' not in response.text
        assert 'Voice entry readiness' in response.text and 'Calls can start' in response.text
        if state == 'gateway_down':
            assert 'Gateway is not answering.' in response.text
        elif state == 'compute_asleep':
            assert 'Compute is asleep.' in response.text
        elif state == 'active':
            assert '/system/voice/calls/call123/end' in response.text
        elif state == 'no_timings':
            assert 'No timings reported.' in response.text
        assert not any('/settings' in call.url.path for call in calls)
        fragment = client.get('/system/voice/status?part=content')
        assert 'id="voice-status-content"' in fragment.text and '<!doctype' not in fragment.text.lower()


def test_status_browser_polling_retains_selection_disclosures_scroll_and_ends_call(tmp_path):
    from test_console_browser import browser_context, playwright
    status = payload()
    status['calls'] = [{'id': 'call123', 'source': 'desktop', 'number': None, 'account': 'pat', 'mode': 'direct',
        'profile': 'default', 'model': 'small', 'duration_seconds': 20, 'turns': 3, 'state': 'speaking'}]
    app, _, _, calls, _, _ = voice_panel(tmp_path, voice_status_data=status)
    with TestClient(app) as server, playwright.sync_playwright() as engine:
        sign_in(server, 'jordan')
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.clock.install()
        page.goto('http://console.test/system/voice/status')
        assert page.locator('#voice-latency script').count() == 0
        assert 'Over target ≤2500' in page.locator('#voice-latency').inner_text()
        page.locator('#voice-latency-limits > summary').click()
        page.evaluate('window.scrollTo(0, 600)')
        before = page.evaluate('scrollY')
        count = len([call for call in calls if call.url.path == '/admin/status'])
        page.evaluate("""() => { const el=document.querySelector('#voice-status-content .kpi-label'); const range=document.createRange(); range.selectNodeContents(el); getSelection().removeAllRanges(); getSelection().addRange(range); }""")
        page.clock.run_for(5500)
        assert len([call for call in calls if call.url.path == '/admin/status']) == count
        page.evaluate('getSelection().removeAllRanges()')
        status['version']['uptime_seconds'] = 15
        page.clock.run_for(5500)
        playwright.expect(page.locator('#voice-status-content')).to_contain_text('15 s')
        assert page.locator('#voice-latency-limits').get_attribute('open') is not None
        assert abs(page.evaluate('scrollY')-before) < 5
        button = page.locator('form[action="/system/voice/calls/call123/end"] button')
        button.click()
        assert not any(call.url.path.endswith('/call123/end') for call in calls)
        button.click()
        playwright.expect(page.locator('.said')).to_contain_text('Call ended.')
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            for width in (1440, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
