"""Independent transcription and generation pages share audio inputs, not page state."""

import base64
import json

import pytest

from test_console_browser import browser_context, playwright
from test_soundroom import wav_bytes
from test_soundroom_browser import audio_model, asset, capture_audio, state
from test_weightroom import desk, sign_in  # noqa: F401


def transcription_state(**values):
    return state(models=[audio_model('asr', 'transcription'), audio_model('preferred', 'transcription'),
                         audio_model('speakers', 'diarization')], preferences={'transcription_model': 'preferred'}, **values)


def test_transcribe_requires_identity_and_navigation_distinguishes_chat_modes(desk):
    server, _, users = desk
    assert server.get('/chat/transcribe', follow_redirects=False).status_code == 303
    users.change('jordan', own={'add': ['front-voice']})
    sign_in(server, 'jordan')
    from weightroom.navigation import navigation
    nav = navigation('/chat/transcribe', ['front-voice'], False)
    assert nav['active'] == '/chat/transcribe' and nav['section'] == 'Interact'
    assert [('Text chat', '/chat'), ('Voice generation', '/chat/speech'), ('Transcribe', '/chat/transcribe'), ('Voice chat', '/voice')] == [
        row for row in nav['links'] if row[1] in ('/chat', '/chat/speech', '/chat/transcribe', '/voice')]
    assert 'Voice chat' in server.get('/voice').text
    speech = server.get('/chat/speech').text
    assert 'Voice generation' in speech and 'Spoken chat' not in speech
    assert 'sc-open' not in speech and 'tr-model' not in speech
    transcript = server.get('/chat/transcribe').text
    assert 'tr-model' in transcript and 'sr-generate' not in transcript
    assert 'speech.js?' not in transcript and 'music.js?' not in transcript


def test_transcription_upload_options_copy_and_all_exports(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    payload = transcription_state()
    uploaded, transcribed, jobs = [], [], []
    recording = {**asset(), 'kind': 'recording', 'filename': 'meeting.wav', 'metadata': {}}
    result = {'text': 'A safe <b>transcript</b>.', 'segments': [
        {'text': 'A safe <b>transcript</b>.', 'start': .25, 'end': 1.5, 'speaker': 'A'}]}
    fallback = capture_audio(payload, jobs)

    def capture(route, path):
        if path == '/soundroom-api/assets':
            uploaded.append(json.loads(route.request.post_data))
            payload['assets'].append(recording)
            route.fulfill(status=201, content_type='application/json', body=json.dumps(recording))
            return True
        if path == '/soundroom-api/transcribe':
            transcribed.append(json.loads(route.request.post_data))
            route.fulfill(content_type='application/json', body=json.dumps(result))
            return True
        return fallback(route, path)

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/chat/transcribe')
        playwright.expect(page.locator('#tr-model')).to_have_value('preferred')
        playwright.expect(page.locator('#tr-go')).to_be_disabled()
        content = wav_bytes(.2)
        page.locator('#tr-file').set_input_files({'name': 'meeting.wav', 'mimeType': 'audio/wav', 'buffer': content})
        playwright.expect(page.locator('#tr-go')).to_be_enabled()
        assert uploaded == [{'b64_audio': base64.b64encode(content).decode(), 'filename': 'meeting.wav', 'kind': 'recording'}]
        page.locator('#tr-diarize').select_option('speakers')
        page.locator('#tr-language').fill('en')
        page.locator('#tr-go').click()
        playwright.expect(page.locator('#tr-result')).to_contain_text(result['text'])
        assert not page.locator('#tr-result b').count()
        assert transcribed == [{'asset': recording['id'], 'model': 'preferred', 'diarization_model': 'speakers', 'language': 'en'}]
        for format_ in ('txt', 'srt', 'vtt', 'json'):
            with page.expect_download() as download:
                page.locator(f'[data-save="{format_}"]').click()
            target = tmp_path / download.value.suggested_filename
            download.value.save_as(target)
            text = target.read_text()
            assert target.name == 'meeting.' + format_
            if format_ == 'json':
                assert json.loads(text) == result
            else:
                assert 'A: A safe <b>transcript</b>.' in text
                if format_ == 'srt': assert '00:00:00,250 --> 00:00:01,500' in text
                if format_ == 'vtt': assert text.startswith('WEBVTT\n') and '00:00:00.250' in text
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.copied = text;}}})")
        page.locator('[data-copy]').click()
        playwright.expect(page.locator('#sr-message')).to_be_hidden()
        assert page.evaluate('window.copied') == 'A: A safe <b>transcript</b>.\n'
        assert not errors and not jobs
        browser.close()


def test_failed_transcription_keeps_the_saved_transcript_and_can_retry(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    recording = {**asset(transcripts=[{'text': 'Existing transcript.', 'segments': []}]), 'kind': 'recording'}
    payload = transcription_state(assets=[recording])
    fallback = capture_audio(payload, [])

    def capture(route, path):
        if path == '/soundroom-api/transcribe':
            route.fulfill(status=503, content_type='application/json', body='{"error":{"message":"Transcription model unavailable"}}')
            return True
        return fallback(route, path)

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        page.goto('http://console.test/chat/transcribe')
        page.locator('#tr-list button').click()
        playwright.expect(page.locator('#tr-result')).to_contain_text('Existing transcript.')
        page.locator('#tr-go').click()
        playwright.expect(page.locator('#sr-message')).to_contain_text('Transcription model unavailable')
        playwright.expect(page.locator('#tr-result')).to_contain_text('Existing transcript.')
        playwright.expect(page.locator('#tr-go')).to_be_enabled()
        browser.close()


def test_voice_management_keeps_shared_upload_and_transcription_helpers(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    payload = transcription_state()
    recording = {**asset(), 'kind': 'reference'}
    saved = []
    fallback = capture_audio(payload, [])

    def capture(route, path):
        if path == '/soundroom-api/assets':
            assert json.loads(route.request.post_data)['kind'] == 'reference'
            route.fulfill(status=201, content_type='application/json', body=json.dumps(recording))
            return True
        if path == '/soundroom-api/transcribe':
            route.fulfill(content_type='application/json', body='{"text":"The voice sample."}')
            return True
        if path == '/soundroom-api/voices':
            saved.append(json.loads(route.request.post_data))
            route.fulfill(status=201, content_type='application/json', body='{"id":"saved"}')
            return True
        return fallback(route, path)

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/chat/speech')
        assert page.locator('[data-tab]').all_text_contents() == ['Generate', 'Voices']
        page.locator('[data-tab="voices"]').click()
        playwright.expect(page.locator('#vo-asr option')).to_have_count(2)
        page.locator('#vo-file').set_input_files({'name': 'voice.wav', 'mimeType': 'audio/wav', 'buffer': wav_bytes()})
        playwright.expect(page.locator('#vo-recording')).to_contain_text('voice.wav')
        page.locator('#vo-fill').click()
        playwright.expect(page.locator('#vo-transcript')).to_have_value('The voice sample.')
        page.locator('#vo-name').fill('Narrator')
        page.locator('#vo-consent').check()
        page.locator('#vo-save').click()
        playwright.expect(page.locator('#vo-name')).to_have_value('')
        assert saved == [{'name': 'Narrator', 'asset': recording['id'], 'transcript': 'The voice sample.', 'consent': True}]
        assert not errors
        browser.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('appearance', ['light', 'dark'])
def test_transcribe_page_fits_and_explains_unavailable_models(desk, width, appearance):
    from test_ui_consistency import MEASURE

    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(), []))
        page.set_viewport_size({'width': width, 'height': 1000})
        page.emulate_media(color_scheme=appearance)
        page.goto('http://console.test/chat/transcribe')
        playwright.expect(page.locator('#tr-model')).to_be_disabled()
        playwright.expect(page.locator('#tr-model option')).to_have_text('No compatible models available')
        playwright.expect(page.locator('h1')).to_have_text('Transcribe')
        measured = page.evaluate(MEASURE)
        assert not measured['overflow']
        assert not measured['namelessFields']
        assert not measured['invalidControls']
        assert not measured['mismatches']
        browser.close()
