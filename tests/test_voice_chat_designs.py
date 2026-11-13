"""Voice browser proposals: model/voice routing, multilingual transcripts and call lifecycle."""
import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]
LAYOUTS = ('desk', 'transcript', 'interpreter')


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-voice-chat-designs.py'))['render']
    target = tmp_path / 'index.html'
    target.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1600, 'height': 1100}, reduced_motion='reduce')
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith(('http:', 'https:')) else None)
        page.goto(target.as_uri())
        yield page
        assert errors == []
        assert requests == []
        browser.close()


def test_model_specific_voices_and_compatibility(preview):
    page = preview
    page.locator('#call-speaker').select_option('vivian')
    page.locator('#call-synthesis').select_option('kokoro')
    assert page.locator('#call-speaker option').all_text_contents() == ['Heart', 'Adam']
    playwright.expect(page.locator('#compatibility-notice')).to_contain_text('Spanish')
    playwright.expect(page.locator('#connect')).to_be_disabled()
    page.locator('#translate').uncheck()
    playwright.expect(page.locator('#connect')).to_be_enabled()
    page.locator('#call-speaker').select_option('am_adam')
    page.locator('#call-synthesis').select_option('qwen-tts')
    playwright.expect(page.locator('#call-speaker')).to_have_value('vivian')
    page.locator('#call-synthesis').select_option('kokoro')
    playwright.expect(page.locator('#call-speaker')).to_have_value('am_adam')
    page.locator('#listen-language').select_option('ja')
    playwright.expect(page.locator('#compatibility-notice')).to_contain_text('Japanese')
    page.locator('#call-synthesis').select_option('qwen-tts')
    page.locator('#call-recognition').select_option('parakeet')
    playwright.expect(page.locator('#compatibility-notice')).to_contain_text('Choose Qwen3 ASR')


def test_layouts_preserve_one_form_and_all_drafts(preview):
    page = preview
    page.locator('.vc-preferences summary').click()
    page.locator('#call-instructions').fill('Help me practise gently.')
    page.locator('#call-model').select_option('llama')
    page.locator('#call-speaker').select_option('serena')
    page.locator('#response-language').select_option('fr')
    for layout in LAYOUTS:
        page.locator(f'[data-design="{layout}"]').click()
        playwright.expect(page.locator('#call-settings')).to_have_count(1)
        playwright.expect(page.locator('#transcript')).to_have_count(1)
        playwright.expect(page.locator('#call-instructions')).to_have_value('Help me practise gently.')
        playwright.expect(page.locator('#call-model')).to_have_value('llama')
        playwright.expect(page.locator('#call-speaker')).to_have_value('serena')
        playwright.expect(page.locator('#response-language')).to_have_value('fr')
        assert page.locator('.vc-message').count() == 2
    # Past messages are immutable after changing the languages for the next call.
    playwright.expect(page.locator('.vc-message[data-role="assistant"] [data-language]')).to_have_text('Spanish')


def test_streaming_end_and_interrupt_discard_late_work(preview):
    page = preview
    page.locator('#connect').click()
    playwright.expect(page.locator('#call-model')).to_be_disabled()
    playwright.expect(page.locator('#translate')).to_be_disabled()
    playwright.expect(page.locator('.vc-message')).to_have_count(3)
    page.wait_for_function('document.querySelector("#call-panel").dataset.state === "speaking"')
    page.wait_for_function('document.querySelectorAll(".vc-message")[3].querySelector("[data-text]").textContent.length > 5')
    page.locator('#interrupt').click()
    playwright.expect(page.locator('.vc-message').last.locator('[data-message-state]')).to_have_text('Interrupted')
    interrupted = page.locator('#transcript').inner_text()
    page.wait_for_timeout(900)
    assert page.locator('#transcript').inner_text() == interrupted
    page.locator('#next-turn').click()
    page.wait_for_function('document.querySelector("#transcript [data-pending] [data-text]").textContent.length > 0')
    page.locator('#hangup').click()
    ended = page.locator('#transcript').inner_text()
    page.wait_for_timeout(900)
    assert page.locator('#transcript').inner_text() == ended
    playwright.expect(page.locator('#call-model')).to_be_enabled()
    playwright.expect(page.locator('#connect')).to_be_enabled()
    playwright.expect(page.locator('#transcript [data-pending]')).to_have_count(0)


def test_language_route_and_complete_translated_turn(preview, tmp_path):
    page = preview
    page.locator('#swap-languages').click()
    playwright.expect(page.locator('#listen-language')).to_have_value('es')
    playwright.expect(page.locator('#response-language')).to_have_value('en')
    page.locator('#translate').uncheck()
    playwright.expect(page.locator('#response-language')).to_have_value('es')
    playwright.expect(page.locator('#response-language')).to_be_disabled()
    page.locator('#translate').check()
    playwright.expect(page.locator('#response-language')).to_have_value('en')
    page.locator('#new-conversation').click()
    page.locator('#connect').click()
    playwright.expect(page.locator('#next-turn')).to_be_enabled(timeout=10000)
    playwright.expect(page.locator('.vc-message[data-role="user"] [data-language]')).to_have_text('Spanish')
    playwright.expect(page.locator('.vc-message[data-role="assistant"] [data-language]')).to_have_text('English')
    playwright.expect(page.locator('.vc-message .vc-translation')).to_have_count(2)
    page.locator('#show-translations').uncheck()
    assert page.locator('.vc-translation:visible').count() == 0
    page.locator('#hangup').click()
    page.locator('#export-format').select_option('json')
    with page.expect_download() as download:
        page.locator('#export-transcript').click()
    target = tmp_path / 'transcript.json'
    download.value.save_as(target)
    result = json.loads(target.read_text())
    assert result['sample'] is True
    assert [m['language'] for m in result['messages']] == ['es', 'en']
    assert all(m['status'] == 'Complete' and m['translation'] for m in result['messages'])
    assert result['messages'][0]['models']['voice'] == 'ryan'


def test_mute_cancels_recognition_without_sending_turn(preview):
    page = preview
    page.locator('#new-conversation').click()
    page.locator('#connect').click()
    page.locator('#mute').click()
    playwright.expect(page.locator('#call-panel')).to_have_attribute('data-state', 'muted')
    playwright.expect(page.locator('.vc-message').last.locator('[data-message-state]')).to_have_text('Not sent · Microphone muted')
    page.wait_for_timeout(900)
    assert page.locator('.vc-message[data-role="assistant"]').count() == 0
    playwright.expect(page.locator('#next-turn')).to_be_disabled()
    page.locator('#mute').click()
    playwright.expect(page.locator('#next-turn')).to_be_enabled()


def test_mobile_call_brings_transcript_before_settings(preview):
    page = preview
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#connect').click()
    positions = page.evaluate('''() => Object.fromEntries(
        ['call-panel','transcript-panel','model-panel'].map(id =>
        [id, document.getElementById(id).getBoundingClientRect().top]))''')
    assert positions['call-panel'] < positions['transcript-panel'] < positions['model-panel']
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_failure_recovery_and_empty_workspace(preview):
    page = preview
    original = page.locator('#transcript').inner_text()
    for scenario in ('offline', 'no-models', 'denied'):
        page.locator('#scenario').select_option(scenario)
        playwright.expect(page.locator('#call-notice')).to_be_visible()
        playwright.expect(page.locator('#connect')).to_be_disabled()
        assert page.locator('#transcript').inner_text() == original
        page.locator('#recover').click()
        playwright.expect(page.locator('#connect')).to_be_enabled()
    page.locator('#scenario').select_option('empty')
    playwright.expect(page.locator('#transcript-empty')).to_be_visible()
    playwright.expect(page.locator('#export-transcript')).to_be_disabled()


@pytest.mark.parametrize('layout', LAYOUTS)
def test_responsive_themes_empty_and_error_states(preview, layout):
    page = preview
    page.locator(f'[data-design="{layout}"]').click()
    for width in (1600, 1024, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        for mode in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != mode:
                page.locator('#theme').click()
            for scenario in ('ready', 'empty', 'offline'):
                page.locator('#scenario').select_option(scenario)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (layout, width, mode, scenario)
        # Native fields remain named and keyboard reachable in every arrangement.
        assert page.locator('#call-recognition').get_attribute('id') == 'call-recognition'
        assert page.get_by_label('Voice within model', exact=True).is_visible()
