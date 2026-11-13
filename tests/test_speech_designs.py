"""Speech proposals: drafts, speaker contracts, honest previews and responsive controls."""

import json
import re
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-speech-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1500, 'height': 1100}, reduced_motion='reduce')
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith(('https:', 'http:')) else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not requests
        browser.close()


def export_job(page, tmp_path):
    with page.expect_download() as downloaded:
        page.locator('[data-job-action]').first.click()
    target = tmp_path / downloaded.value.suggested_filename
    downloaded.value.save_as(target)
    return json.loads(target.read_text())


def test_dialogue_assignments_and_model_drafts_survive_layouts(preview, tmp_path):
    page = preview
    page.locator('#script-example').select_option('dialogue')
    page.locator('[data-speaker="Maya"]').select_option('rowan')
    page.locator('[data-speaker="Alex"]').select_option('ellis')
    text = page.locator('#sr-prompt').input_value()
    for layout in ('desk', 'listen', 'script'):
        page.locator(f'[data-design="{layout}"]').click()
        playwright.expect(page.locator('#sr-prompt')).to_have_value(text)
        playwright.expect(page.locator('[data-speaker="Maya"]')).to_be_visible()
        playwright.expect(page.locator('[data-speaker="Maya"]')).to_have_value('rowan')
    page.locator('#sr-model').select_option('qwen-design')
    page.locator('[data-speaker="Maya"]').fill('Calm and measured')
    page.locator('[data-speaker="Alex"]').fill('Bright and expressive')
    page.locator('#sr-model').select_option('qwen-clone')
    playwright.expect(page.locator('[data-speaker="Maya"]')).to_have_value('rowan')
    page.locator('#sr-generate').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('No model was called')
    request = export_job(page, tmp_path)
    assert [line['voice_id'] for line in request['script']] == ['rowan', 'ellis', 'rowan', 'ellis']
    assert request['model'] == 'qwen-clone'
    assert request['script'][0]['text'] == 'What if we started with something simple?'
    page.locator('#sr-model').select_option('qwen-design')
    playwright.expect(page.locator('[data-speaker="Maya"]')).to_have_value('Calm and measured')


def test_profile_specific_requests_and_numeric_settings(preview, tmp_path):
    page = preview
    page.locator('#sr-model').select_option('qwen-speech')
    playwright.expect(page.locator('#sr-preset-wrap')).to_be_visible()
    playwright.expect(page.locator('#sr-voice-wrap')).to_be_hidden()
    page.locator('#sr-preset').fill('example-preset')
    page.locator('.sp-advanced summary').click()
    page.locator('#sr-seed').fill('412')
    page.locator('#sr-gap').fill('0.75')
    page.locator('#sr-prompt').press('Control+Enter')
    playwright.expect(page.locator('#action-status')).to_contain_text('No model was called')
    request = export_job(page, tmp_path)
    assert request['seed'] == 412 and request['gap'] == 0.75
    assert all(line['voice'] == 'example-preset' and 'voice_id' not in line for line in request['script'])
    page.locator('#sr-model').select_option('qwen-design')
    page.locator('#sr-generate').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('Describe the voice')
    page.locator('#sr-design').fill('A warm, slow narrator')
    page.locator('#sr-generate').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('No model was called')
    request = export_job(page, tmp_path)
    assert all(line['instructions'] == 'A warm, slow narrator' and 'voice' not in line for line in request['script'])


def test_narrator_cast_and_voice_cards_use_the_same_request_voice(preview, tmp_path):
    page = preview
    page.locator('[data-design="script"]').click()
    page.locator('[data-speaker="Narrator"]').select_option('ellis')
    playwright.expect(page.locator('[data-voice="ellis"]')).to_have_attribute('aria-pressed', 'true')
    page.locator('#sr-generate').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('No model was called')
    assert all(line['voice_id'] == 'ellis' for line in export_job(page, tmp_path)['script'])
    page.locator('[data-voice="rowan"]').click()
    playwright.expect(page.locator('[data-speaker="Narrator"]')).to_have_value('rowan')


def test_import_and_reuse_are_local_and_keep_settings(preview):
    page = preview
    script = 'Maya: A new line.\nAlex: Another line.'
    page.locator('#script-import').set_input_files({'name': 'scene.txt', 'mimeType': 'text/plain', 'buffer': script.encode()})
    playwright.expect(page.locator('#sr-prompt')).to_have_value(script)
    playwright.expect(page.locator('#action-status')).to_contain_text('No file was uploaded')
    page.locator('#script-import').set_input_files({'name': 'huge.txt', 'mimeType': 'text/plain', 'buffer': b'x' * 20001})
    playwright.expect(page.locator('#action-status')).to_contain_text('exceeds 20,000')
    playwright.expect(page.locator('#sr-prompt')).to_have_value(script)
    page.locator('#sr-model').select_option('qwen-speech')
    page.locator('#sr-preset').fill('keep-this')
    page.locator('[data-open-take]').nth(2).click()
    page.locator('#sr-reuse').click()
    playwright.expect(page.locator('#sr-prompt')).to_have_value(re.compile('Welcome to the next chapter'))
    playwright.expect(page.locator('#sr-preset')).to_have_value('keep-this')


def test_library_filters_favourites_and_honest_exports(preview, tmp_path):
    page = preview
    page.locator('#sr-favourites').click()
    assert page.locator('.sp-take').count() == 1
    page.locator('#sr-favourite').click()
    playwright.expect(page.locator('#library-empty')).to_be_visible()
    page.locator('#sr-favourites').click()
    page.locator('#sr-filter').fill('conversation')
    assert page.locator('.sp-take').count() == 1
    page.locator('[data-open-take]').click()
    playwright.expect(page.locator('#sr-title')).to_have_text('A conversation, naturally')
    with page.expect_download() as downloaded:
        page.locator('#export-take').click()
    target = tmp_path / downloaded.value.suggested_filename
    downloaded.value.save_as(target)
    sample = json.loads(target.read_text())
    assert sample['sample'] and not sample['audio_generated']
    assert sample['text'].startswith('Maya:')
    page.locator('#sr-filter').fill('no matches')
    playwright.expect(page.locator('#library-empty')).to_contain_text('No matching recordings')


def test_cancel_and_temporary_requests_do_not_leave_completed_history(preview):
    page = preview
    page.locator('#sr-generate').click()
    page.locator('[data-job-action]').click()
    playwright.expect(page.locator('[data-job-state]')).to_have_text('Cancelled')
    page.wait_for_timeout(1500)
    playwright.expect(page.locator('[data-job-state]')).to_have_text('Cancelled')
    page.locator('.sp-advanced summary').click()
    page.locator('#sr-temporary').check()
    page.locator('#sr-generate').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('removed from history')
    assert page.locator('.sp-job').count() == 1


def test_browser_audition_uses_displayed_text_and_handles_missing_voices(preview):
    page = preview
    page.evaluate("""() => {
      window.auditions = [];
      Object.defineProperty(window, 'speechSynthesis', {value: {
        getVoices: () => [], cancel: () => {}, speak: u => window.auditions.push(u.text)
      }, configurable: true});
    }""")
    page.locator('#play-take').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('No browser speech voice')
    page.evaluate("speechSynthesis.getVoices = () => [{name:'Device voice'}]")
    page.locator('#sr-prompt').fill('This is the unsaved script.')
    page.locator('#audition-script').click()
    assert page.evaluate('window.auditions') == ['This is the unsaved script.']
    playwright.expect(page.locator('#action-status')).to_contain_text('selected model and saved voice are not used')
    page.locator('#audition-script').click()
    page.locator('#play-take').click()
    assert page.evaluate('window.auditions.at(-1)').startswith('Some ideas start')


@pytest.mark.parametrize('layout', ['desk', 'script', 'listen'])
@pytest.mark.parametrize('width', [390, 820, 1500])
def test_designs_fit_both_appearances_and_all_states(preview, layout, width):
    page = preview
    page.set_viewport_size({'width': width, 'height': 1000})
    page.locator(f'[data-design="{layout}"]').click()
    for appearance in range(2):
        for state in ('normal', 'empty', 'offline', 'no-models', 'no-voices'):
            page.locator('#scenario').select_option(state)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (layout, width, state)
            playwright.expect(page.locator('#sr-generate')).to_be_disabled() if state in ('offline', 'no-models', 'no-voices') else playwright.expect(page.locator('#sr-generate')).to_be_enabled()
            assert not page.evaluate("""() => [...document.querySelectorAll('input:not([type=hidden]),select,textarea')]
              .filter(e => e.getBoundingClientRect().width && !e.closest('[hidden]'))
              .filter(e => !e.labels?.length && !e.getAttribute('aria-label')).map(e => e.id)""")
        page.locator('#theme').click()
    ids = page.locator('[id]').evaluate_all('(els) => els.map(e => e.id)')
    assert len(ids) == len(set(ids))
    assert page.locator('h1').count() == 1
    assert page.locator('svg use').evaluate_all("(els) => els.every(e => document.getElementById(e.getAttribute('href').slice(1)))")


def test_voice_mode_keeps_generation_drafts(preview):
    page = preview
    page.locator('#sr-prompt').fill('Keep this work.')
    page.locator('#sr-model').select_option('qwen-design')
    page.locator('[data-view="voices"]').click()
    playwright.expect(page.locator('#voice-search')).to_be_focused()
    playwright.expect(page.locator('#sr-prompt')).to_have_value('Keep this work.')
