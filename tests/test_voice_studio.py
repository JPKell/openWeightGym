"""The live Transcript studio uses the real client script and deterministic audio/socket events."""
import json
from pathlib import Path

import pytest

from test_voice_browser import mock_audio
from test_weightroom import desk, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]

CONFIG = {'modes': ['direct', 'hermes'], 'default_profile': 'default', 'cloned_profile': 'audio',
    'models': ['small', 'large'], 'voices': [{'id': 'voice_one', 'name': 'Narrator'}],
    'languages': [{'id': 'en', 'name': 'English'}, {'id': 'es', 'name': 'Spanish'}, {'id': 'ja', 'name': 'Japanese'}],
    'speakers': [{'id': 3, 'name': 'Heart'}],
    'profiles': [{'id': 'default', 'model': 'small', 'speaker': 3, 'modes': ['direct', 'hermes']},
                 {'id': 'audio', 'speech': 'audiocpp', 'model': 'small', 'recognition_model': 'asr', 'synthesis_model': 'tts', 'modes': ['direct']}],
    'audio_models': [{'id': 'asr', 'name': 'Recognition', 'kind': 'transcription', 'languages': ['en', 'es', 'ja']},
                     {'id': 'tts', 'name': 'Speech', 'kind': 'speech', 'languages': ['en', 'es'], 'voices': [{'id': 'Ryan', 'name': 'Ryan'}, {'id': 'Vivian', 'name': 'Vivian'}]},
                     {'id': 'clone', 'name': 'Saved-voice model', 'kind': 'clone', 'voices': [], 'languages': []}]}


@pytest.fixture
def studio(desk):
    server, _, users = desk
    users.change('jordan', own={'add': ['front-voice']})
    sign_in(server, 'jordan')
    response = server.get('/voice')
    assert response.status_code == 200
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('http://console.test/**', lambda route: route.fulfill(
            status=200, content_type='text/css', body=(ROOT / 'weightroom/static' / route.request.url.rsplit('/', 1)[-1].split('?')[0]).read_bytes())
            if route.request.url.split('?')[0].endswith('.css') else route.fulfill(status=200, content_type='application/json', body='{}'))
        # Render the real authenticated route, then inject hardware mocks before the production client.
        import re
        markup = re.sub(r'<script\b[^>]*>.*?</script>', '', response.text, flags=re.S)
        page.goto('http://console.test/')
        page.set_content(markup)
        page.add_script_tag(content=(ROOT / 'weightroom/static/themes.js').read_text())
        mock_audio(page, CONFIG)
        page.add_script_tag(content=(ROOT / 'voice/static/voice.js').read_text())
        page.wait_for_function('document.getElementById("status").textContent === "Ready"')
        yield page
        assert not errors
        browser.close()


def event(page, type_, **values):
    page.evaluate('(m) => testSocket.onmessage({data:JSON.stringify(m)})', {'type': type_, **values})


def start(page):
    page.locator('#connect').click()
    page.wait_for_function('window.testSocket')
    event(page, 'session.ready')


def test_live_model_voice_and_language_options_reach_session(studio):
    page = studio
    assert page.locator('#voice-workspace').get_attribute('data-layout') == 'transcript'
    page.locator('#call-profile').select_option('audio')
    page.locator('#call-speaker').select_option('Vivian')
    page.locator('#listen-language').select_option('es')
    page.locator('#translate').check()
    page.locator('#response-language').select_option('en')
    start(page)
    request = page.evaluate('testRequest')
    assert request['profile'] == 'audio' and 'voice' not in request
    assert request['options'] == {'speed': 1, 'model': 'small', 'max_output_tokens': 128,
        'synthesis_model': 'tts', 'recognition_model': 'asr', 'trailing_silence_ms': 700,
        'speech_voice': 'Vivian', 'language': 'es', 'response_language': 'en'}
    playwright.expect(page.locator('#call-speaker')).to_be_disabled()
    playwright.expect(page.locator('#response-language')).to_be_disabled()
    page.locator('#hangup').click()
    playwright.expect(page.locator('#call-speaker')).to_have_value('Vivian')
    playwright.expect(page.locator('#listen-language')).to_have_value('es')
    page.locator('#call-synthesis').select_option('clone')
    playwright.expect(page.locator('#voice-choice')).to_be_visible()
    page.locator('#call-synthesis').select_option('tts')
    playwright.expect(page.locator('#call-speaker')).to_have_value('Vivian')


def test_live_transcript_updates_safely_groups_segments_and_exports(studio, tmp_path):
    page = studio
    start(page)
    event(page, 'transcript.partial', turn=1, text='Hello <img src=x onerror=alert(1)>')
    assert page.locator('#transcript img').count() == 0
    event(page, 'transcript.partial', turn=1, text='Hello there')
    event(page, 'turn.started', turn=1)
    event(page, 'transcript.final', turn=1, text='Hello there.')
    event(page, 'playback.segment', turn=1, segment=1, samples=100, text='Good morning.')
    event(page, 'playback.segment', turn=1, segment=1, samples=100, text='Good morning.')
    event(page, 'playback.segment', turn=1, segment=2, samples=100, text='How can I help?')
    playwright.expect(page.locator('.vc-message')).to_have_count(2)
    playwright.expect(page.locator('.vc-message').last.locator('[data-text]')).to_have_text('Good morning. How can I help?')
    event(page, 'turn.completed', turn=1)
    playwright.expect(page.locator('#status')).to_have_text('Speaking')
    page.evaluate('''() => {testNode.port.onmessage({data:{type:'playback.progress',turn:1,segment:1,samples:100}});testNode.port.onmessage({data:{type:'playback.progress',turn:1,segment:2,samples:100}});}''')
    playwright.expect(page.locator('#status')).to_have_text('Listening')
    page.locator('#export-format').select_option('json')
    with page.expect_download() as download:
        page.locator('#export-transcript').click()
    target = tmp_path / 'transcript.json'
    download.value.save_as(target)
    messages = json.loads(target.read_text())['messages']
    assert [m['text'] for m in messages] == ['Hello there.', 'Good morning. How can I help?']
    assert all(m['language'] == 'en' for m in messages)
    page.locator('#hangup').click()
    page.locator('#new-conversation').click()
    playwright.expect(page.locator('#transcript-empty')).to_be_visible()


def test_live_interrupt_ignores_late_segments_and_mute_retains_playback(studio):
    page = studio
    start(page)
    event(page, 'turn.started', turn=1)
    event(page, 'transcript.final', turn=1, text='Tell me a story.')
    event(page, 'playback.segment', turn=1, segment=1, samples=100, text='Once upon a time.')
    page.locator('#mute').click()
    playwright.expect(page.locator('#mute')).to_have_attribute('aria-pressed', 'true')
    playwright.expect(page.locator('#status')).to_have_text('Speaking')
    page.locator('#interrupt').click()
    event(page, 'playback.segment', turn=1, segment=2, samples=100, text='This must not appear.')
    assert 'This must not appear' not in page.locator('#transcript').inner_text()
    playwright.expect(page.locator('.vc-message').last.locator('[data-message-state]')).to_contain_text('Interrupted')


def test_language_compatibility_hermes_and_live_mobile_layout(studio):
    page = studio
    page.locator('#listen-language').select_option('es')
    playwright.expect(page.locator('#connect')).to_be_disabled()
    playwright.expect(page.locator('#language-help')).to_contain_text('does not support')
    page.locator('#call-profile').select_option('audio')
    playwright.expect(page.locator('#connect')).to_be_enabled()
    page.locator('#translate').check()
    page.locator('#response-language').select_option('ja')
    playwright.expect(page.locator('#connect')).to_be_disabled()
    page.locator('#mode').select_option('hermes')
    playwright.expect(page.locator('#response-language')).to_be_disabled()
    page.locator('#mode').select_option('direct')
    page.locator('#listen-language').select_option('en')
    page.locator('#response-language').select_option('en')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_configuration_failure_retry_does_not_duplicate_controls(studio):
    page = studio
    page.evaluate("testConfiguration.configuration_error='Service unavailable'; document.getElementById('retry-configuration').click()")
    playwright.expect(page.locator('#configuration-notice')).to_be_visible()
    playwright.expect(page.locator('#connect')).to_be_disabled()
    page.evaluate("testConfiguration.configuration_error=null; document.getElementById('retry-configuration').click()")
    playwright.expect(page.locator('#configuration-notice')).to_be_hidden()
    playwright.expect(page.locator('#connect')).to_be_enabled()
    playwright.expect(page.locator('#call-profile')).to_have_count(1)
    page.evaluate("testConfiguration.models=[]; document.getElementById('retry-configuration').click()")
    playwright.expect(page.locator('#connect')).to_be_disabled()
    playwright.expect(page.locator('#language-help')).to_contain_text('No permitted conversation model')


def test_canceled_microphone_permission_does_not_revive_call(studio):
    page = studio
    page.evaluate('''() => {
        navigator.mediaDevices.getUserMedia = () => new Promise(resolve => {window.resolveCapture=resolve;});
    }''')
    page.locator('#connect').click()
    page.wait_for_function('typeof window.resolveCapture === "function"')
    page.locator('#hangup').click()
    page.evaluate('''() => resolveCapture({getTracks:()=>[{stop(){window.captureStopped=true;}}]})''')
    page.wait_for_function('window.captureStopped')
    assert page.evaluate('!window.testSocket')
    playwright.expect(page.locator('#status')).to_have_text('Disconnected')
    playwright.expect(page.locator('#connect')).to_be_enabled()
