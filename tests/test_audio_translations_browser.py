"""Translation controls follow the chosen model and retain exact untimed Whisper text."""

import json

import pytest

from test_console_browser import browser_context, playwright
from test_soundroom_browser import asset, audio_model, capture_audio, state
from test_weightroom import desk, sign_in  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_translate_choice_only_for_capable_model_and_no_invented_subtitle_times(desk, width, theme):
    server, _, _ = desk
    sign_in(server, 'jordan')
    models = [audio_model('whisper', 'transcription'), audio_model('other', 'transcription')]
    models[0].update(translates=True, engine_id='whispercpp')
    recording = asset(); recording['kind'] = 'recording'
    normal = capture_audio(state(models=models, assets=[recording]), [])
    calls = []
    def capture(route, path):
        if path == '/soundroom-api/transcribe':
            calls.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({'text': 'Good morning. <safe>', 'task': 'translate'}))
            return True
        return normal(route, path)
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        page.set_viewport_size({'width': width, 'height': 1100})
        page.goto('http://console.test/chat/transcribe')
        page.evaluate('(theme)=>document.documentElement.dataset.bsTheme=theme', theme)
        playwright.expect(page.locator('#tr-task-control')).to_be_visible()
        page.locator('#tr-list button').first.click()
        page.locator('#tr-language').fill('es')
        page.locator('#tr-task').select_option('translate')
        playwright.expect(page.locator('#tr-language')).to_be_disabled()
        page.locator('#tr-go').click()
        playwright.expect(page.locator('#tr-result')).to_have_text('Good morning. <safe>')
        assert calls[0]['translate'] is True and 'language' not in calls[0]
        playwright.expect(page.locator('[data-save="srt"]')).to_be_disabled()
        playwright.expect(page.locator('[data-save="vtt"]')).to_be_disabled()
        page.locator('#tr-model').select_option('other')
        playwright.expect(page.locator('#tr-task-control')).to_be_hidden()
        playwright.expect(page.locator('#tr-language')).to_be_enabled()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        browser.close()
