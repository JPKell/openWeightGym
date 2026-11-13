"""Music design proposals exercise real playback and browser-local generation workflows."""

import io
import runpy
import wave
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def markup():
    return runpy.run_path(str(ROOT / 'scripts/preview-music-designs.py'))['render']()


@pytest.fixture
def preview(markup, tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(markup)
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width':1600, 'height':1100}, reduced_motion='reduce')
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith(('https:', 'http:')) else None)
        page.goto(output.as_uri())
        yield page
        assert not errors, errors
        assert not requests, requests
        browser.close()


def test_layout_switching_keeps_one_form_player_and_draft(preview):
    page = preview
    page.locator('#sr-prompt').fill('A fragile piano melody under a warm synth pad.')
    page.locator('#sr-lyrics').fill('[verse]\nKeep this line')
    page.locator('#sr-duration').fill('45')
    for design in ('studio', 'writer', 'lab'):
        page.locator(f'[data-design="{design}"]').click()
        playwright.expect(page.locator('#sr-form')).to_have_count(1)
        playwright.expect(page.locator('#sr-audio')).to_have_count(1)
        playwright.expect(page.locator('#sr-prompt')).to_have_value('A fragile piano melody under a warm synth pad.')
        playwright.expect(page.locator('#sr-lyrics')).to_have_value('[verse]\nKeep this line')
        playwright.expect(page.locator('#sr-duration')).to_have_value('45')
        assert page.url.endswith('#' + design)


def test_models_keep_drafts_and_enforce_lyrics_and_modes(preview):
    page = preview
    page.locator('#sr-prompt').fill('My ACE draft')
    page.locator('#sr-instrumental').check()
    page.locator('#sr-model').select_option('HeartMuLa Songs')
    playwright.expect(page.locator('#sr-instrumental')).to_be_disabled()
    playwright.expect(page.locator('#sr-instrumental')).not_to_be_checked()
    playwright.expect(page.locator('#sr-lyrics')).to_be_enabled()
    page.locator('#sr-prompt').fill('My Heart draft')
    page.locator('#sr-lyrics').fill('')
    page.locator('#sr-generate').click()
    assert page.locator('.music-job').count() == 0
    page.locator('#sr-model').select_option('Stable Audio')
    playwright.expect(page.locator('#sr-lyrics-wrap')).not_to_be_visible()
    playwright.expect(page.locator('#music-no-lyrics')).to_be_visible()
    assert [option.get_attribute('value') for option in page.locator('#sr-mode option').all()] == ['song', 'cover', 'repaint']
    page.locator('#sr-model').select_option('ACE-Step Studio')
    playwright.expect(page.locator('#sr-prompt')).to_have_value('My ACE draft')
    playwright.expect(page.locator('#sr-instrumental')).to_be_checked()
    page.locator('#sr-model').select_option('HeartMuLa Songs')
    playwright.expect(page.locator('#sr-prompt')).to_have_value('My Heart draft')


def test_playback_waveform_regions_reuse_and_download(preview, tmp_path):
    page = preview
    page.wait_for_function('document.querySelector("#sr-audio").duration === 16')
    page.locator('#sr-audio').evaluate('audio => audio.play()')
    page.wait_for_function('document.querySelector("#sr-audio").currentTime > .1')
    page.locator('#sr-audio').evaluate('audio => audio.pause()')
    page.locator('#sr-region-start').fill('3')
    page.locator('#sr-region-start').press('Tab')
    page.locator('#sr-region-end').fill('7')
    page.locator('#sr-region-end').press('Tab')
    page.locator('#sr-edit-range').click()
    playwright.expect(page.locator('#sr-mode')).to_have_value('repaint')
    playwright.expect(page.locator('#sr-start')).to_have_value('3.00')
    playwright.expect(page.locator('#sr-end')).to_have_value('7.00')
    playwright.expect(page.locator('#sr-duration')).to_be_disabled()
    page.locator('#sr-variation').click()
    playwright.expect(page.locator('#sr-seed')).to_have_value('-1')
    with page.expect_download() as download:
        page.locator('#sr-download').click()
    target = tmp_path / download.value.suggested_filename
    download.value.save_as(target)
    with wave.open(str(target)) as audio:
        assert audio.getnframes() / audio.getframerate() == 16
        assert audio.getnchannels() == 1
        assert any(audio.readframes(16000))


def test_library_search_favourites_sort_tags_and_removal(preview):
    page = preview
    page.locator('#sr-favourites').check()
    playwright.expect(page.locator('.music-take')).to_have_count(2)
    page.locator('#sr-filter').fill('nothing matches')
    playwright.expect(page.locator('.music-empty-library')).to_be_visible()
    page.locator('#clear-take-filters').click()
    page.locator('#take-sort').select_option('name')
    assert page.locator('[data-take-title]').first.inner_text() == 'After the last light'
    page.locator('#sr-tags').fill('final, warm, piano')
    page.locator('#sr-save-tags').click()
    page.locator('#sr-filter').fill('warm')
    playwright.expect(page.locator('.music-take')).to_have_count(1)
    page.locator('#sr-delete').click()
    page.get_by_role('button', name='Remove demo take', exact=True).click()
    page.locator('#clear-take-filters').click()
    playwright.expect(page.locator('.music-take')).to_have_count(5)


def test_generation_cancel_retry_and_empty_states(preview):
    page = preview
    page.locator('#sr-generate').click()
    page.get_by_role('button', name='Cancel', exact=True).click()
    playwright.expect(page.locator('#sr-jobs')).to_contain_text('Cancelled')
    playwright.expect(page.locator('.music-take')).to_have_count(6)
    page.locator('#scenario').select_option('error')
    playwright.expect(page.locator('#sr-jobs')).to_contain_text('out-of-memory')
    page.get_by_role('button', name='Retry', exact=True).click()
    playwright.expect(page.locator('#music-status')).to_contain_text('2 demo takes ready', timeout=10000)
    playwright.expect(page.locator('.music-take')).to_have_count(8)
    page.locator('#scenario').select_option('empty')
    playwright.expect(page.locator('#sr-empty')).to_be_visible()
    playwright.expect(page.locator('.music-empty-library')).to_be_visible()
    page.locator('#sr-generate').click()
    playwright.expect(page.locator('.music-take')).to_have_count(2, timeout=10000)
    page.locator('#scenario').select_option('no-model')
    playwright.expect(page.locator('#sr-generate')).to_be_disabled()
    playwright.expect(page.locator('#sr-availability')).to_contain_text('Add a model')


def test_source_upload_stays_local_and_repaint_validates_range(preview):
    page = preview
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\0\0' * 32000)
    page.locator('#sr-mode').select_option('repaint')
    page.locator('#sr-source').set_input_files({'name':'my-source.wav', 'mimeType':'audio/wav', 'buffer':audio.getvalue()})
    playwright.expect(page.locator('#sr-source-name')).to_contain_text('my-source.wav · 0:02')
    page.locator('#sr-start').fill('1')
    page.locator('#sr-end').fill('3')
    page.locator('#sr-generate').click()
    playwright.expect(page.locator('#sr-message')).to_contain_text('within the source')
    assert page.locator('.music-job').count() == 0


@pytest.mark.parametrize('width', [1600, 1024, 390])
def test_all_designs_fit_themes_scenarios_and_accessible_controls(preview, width):
    page = preview
    page.set_viewport_size({'width':width, 'height':1000})
    from test_ui_consistency import MEASURE
    for design in ('studio', 'writer', 'lab'):
        page.locator(f'[data-design="{design}"]').click()
        for scenario in ('normal', 'empty', 'no-model', 'error'):
            page.locator('#scenario').select_option(scenario)
            measurements = page.evaluate(MEASURE)
            assert not measurements['overflow'], (design, scenario, width)
            assert not measurements['namelessFields'], measurements['namelessFields']
            assert not measurements['invalidControls'], measurements['invalidControls']
        wave_before = page.locator('#sr-wave').evaluate('canvas => canvas.toDataURL()')
        background = page.evaluate('getComputedStyle(document.body).backgroundColor')
        page.locator('#theme').click()
        assert page.evaluate('getComputedStyle(document.body).backgroundColor') != background
        assert page.locator('#sr-wave').evaluate('canvas => canvas.toDataURL()') != wave_before
        page.locator('#palette').select_option('clay')
        playwright.expect(page.locator('html')).to_have_attribute('data-site-theme', 'clay')
        page.locator('#palette').select_option('sage')
