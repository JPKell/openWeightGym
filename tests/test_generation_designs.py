"""Generation proposals remain offline, family-aware and usable across layouts."""

import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
LAYOUTS = ('canvas', 'lab', 'desk')


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-generation-designs.py'))['render']())
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        browser.close()
        assert not errors
        assert not requests, 'Design proposals must not contact services or download remote artwork.'


def test_controls_follow_backend_families_and_keep_per_model_drafts(preview):
    page = preview
    page.locator('#prompt').fill('A carefully composed product photograph')
    page.locator('#steps').fill('33')
    page.locator('#reference-section > summary').click()
    page.locator('#sample-reference').click()
    page.locator('#sample-reference').click()
    expect(page.locator('#reference-list > div')).to_have_count(2)
    page.locator('[data-up]').nth(1).click()
    expect(page.locator('[data-name]').first).to_have_text('1. Sample 2')
    for layout in LAYOUTS:
        page.locator(f'[data-design={layout}]').click()
        expect(page.locator('#steps')).to_have_value('33')
    page.locator('#model').select_option('sdxl')
    expect(page.locator('#negative_prompt')).to_be_visible()
    expect(page.locator('#cfg_scale')).to_be_visible()
    expect(page.locator('#guidance')).to_be_disabled()
    page.locator('#negative_prompt').fill('blur, artifacts')
    page.locator('#sample-reference').click()
    expect(page.locator('#sample-reference')).to_be_disabled()
    expect(page.locator('#strength')).to_be_visible()
    page.locator('#model').select_option('qwen-image-2.1')
    expect(page.locator('#reference-section')).to_be_hidden()
    expect(page.locator('#image-text')).to_be_visible()
    expect(page.locator('#width')).to_have_attribute('step', '32')
    expect(page.locator('#cfg_scale')).to_have_value('6')
    page.locator('#image-text').fill('FIELD NOTES')
    page.locator('#insert-text').click()
    expect(page.locator('#prompt')).to_have_value('A tiger cub resting beneath a broad banana leaf in a tropical rainforest. Rain beads on the leaf, soft overcast light, intimate wildlife portrait, rich green tones. Include the exact text "FIELD NOTES" with clear, readable lettering.')
    page.locator('#model').select_option('flux2-dev')
    expect(page.locator('#steps')).to_have_value('33')
    expect(page.locator('#prompt')).to_have_value('A carefully composed product photograph')
    expect(page.locator('#reference-list > div')).to_have_count(2)
    page.locator('#model').select_option('sdxl')
    expect(page.locator('#negative_prompt')).to_have_value('blur, artifacts')
    expect(page.locator('#reference-list > div')).to_have_count(1)


@pytest.mark.parametrize('layout', LAYOUTS)
def test_simulated_results_reuse_resolved_settings_and_preserve_selection(preview, tmp_path, layout):
    page = preview
    page.locator(f'[data-design={layout}]').click()
    page.locator('#model').select_option('sdxl')
    page.locator('#seed').fill('12345')
    page.locator('#negative_prompt').fill('blurry')
    page.locator('#prompt').fill('An editorial landscape')
    page.locator('#upscale').select_option('2')
    page.locator('#generate').click()
    expect(page.locator('[data-state]')).to_have_text('Complete', timeout=10000)
    expect(page.locator('#results > button')).to_have_count(4)
    expect(page.locator('#result-title')).to_have_text('Rainforest study')
    page.locator('[data-result="run-1-0"]').click()
    expect(page.locator('#result-detail')).to_contain_text('seed 12345')
    page.locator('#favorite').click()
    page.locator('#library-filter').select_option('favorites')
    expect(page.locator('#results > button')).to_have_count(1)
    page.locator('.ig-result-details > summary').click()
    with page.expect_download() as downloading:
        page.locator('#export-settings').click()
    target = tmp_path / 'settings.json'
    downloading.value.save_as(target)
    exported = json.loads(target.read_text())
    assert exported['simulation']
    assert exported['settings']['seed'] == 12345
    assert exported['settings']['negative_prompt'] == 'blurry'
    assert 'guidance' not in exported['settings']
    page.locator('#model').select_option('flux2-klein')
    page.locator('#reuse').click()
    expect(page.locator('#model')).to_have_value('sdxl')
    expect(page.locator('#seed')).to_have_value('12345')
    expect(page.locator('#prompt')).to_have_value('An editorial landscape')
    expect(page.locator('#upscale')).to_have_value('2')


def test_sweep_validation_pause_resume_stop_and_empty_recovery(preview):
    page = preview
    page.locator('[data-design=lab]').click()
    page.locator('#sweep-enabled').check()
    page.locator('#sweep-axis').select_option('steps')
    page.locator('#sweep-values').fill('28, invalid')
    page.locator('#generate').click()
    expect(page.locator('#feedback-text')).to_contain_text('whole numbers')
    expect(page.locator('#jobs > div')).to_have_count(0)
    page.locator('#sweep-values').fill('28, 32, 40')
    page.locator('#generate').click()
    page.locator('[data-pause]').click()
    expect(page.locator('[data-state]')).to_have_text('Paused')
    page.locator('[data-pause]').click()
    expect(page.locator('#results > button')).to_have_count(4, timeout=10000)
    page.locator('[data-stop]').click()
    expect(page.locator('[data-state]')).to_have_text('Stopped')
    expect(page.locator('#results > button')).to_have_count(4)
    page.locator('#clear-library').click()
    expect(page.locator('#canvas-empty')).to_be_visible()
    expect(page.locator('#download')).to_be_disabled()
    expect(page.locator('#prompt')).not_to_be_empty()
    page.locator('#sweep-enabled').uncheck()
    page.locator('#generate').click()
    expect(page.locator('#canvas-empty')).to_be_hidden(timeout=10000)


def test_workflow_excludes_prompt_and_refs_and_layout_has_one_form(preview):
    page = preview
    page.locator('#prompt').fill('A private draft')
    page.locator('#steps').fill('41')
    page.locator('#save-preset').click()
    page.locator('#preset-name').fill('Detailed composition')
    page.locator('#preset-form button[type=submit], #preset-form button:not([type])').click()
    page.locator('#prompt').fill('A completely different idea')
    page.locator('#steps').fill('12')
    page.locator('#preset').select_option(label='Detailed composition')
    expect(page.locator('#steps')).to_have_value('41')
    expect(page.locator('#prompt')).to_have_value('A completely different idea')
    for layout in LAYOUTS:
        page.locator(f'[data-design={layout}]').click()
        expect(page.locator('#generation-form')).to_have_count(1)
        expect(page.locator('#steps')).to_have_value('41')
    page.locator('#research').click()
    expect(page.get_by_role('dialog')).to_be_visible()
    expect(page.locator('#guide-dialog')).to_contain_text('Qwen Image 2.1')
    page.keyboard.press('Escape')
    expect(page.locator('#research')).to_be_focused()


def test_all_layouts_themes_narrow_states_and_keyboard(preview):
    page = preview
    light_surface = page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--theme-surface")')
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        if theme == 'dark':
            assert page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--theme-surface")') != light_surface
        for width in (1600, 900, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in LAYOUTS:
                page.locator(f'[data-design={layout}]').click()
                for family in ('flux2-dev', 'sdxl', 'qwen-image', 'qwen-image-2.1'):
                    page.locator('#model').select_option(family)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#toolkit').click()
                expect(page.locator('#tools-dialog')).to_contain_text('Proposed')
                assert page.locator('#tools-dialog').evaluate('el => el.scrollWidth <= el.clientWidth')
                page.keyboard.press('Escape')
                for view in ('library', 'queue', 'generate'):
                    page.locator(f'button[data-view={view}]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#library-filter').select_option('favorites')
    expect(page.locator('#library-empty')).to_be_visible()
    page.locator('#library-filter').select_option('all')
    page.locator('body').click(position={'x': 3, 'y': 3})
    page.keyboard.press('ArrowRight')
    expect(page.locator('#result-title')).to_have_text('Color study')


def test_profiles_and_sampler_choices_reuse_backend_contract(preview):
    from mirrorwall.model import FAMILIES, SAMPLERS, SCHEDULERS, defaults
    page = preview
    assert set(page.locator('#model option').evaluate_all('els => els.map(el => el.value)')) == set(FAMILIES)
    assert page.locator('#sampler option').evaluate_all('els => els.map(el => el.value)') == list(SAMPLERS)
    assert page.locator('#scheduler option').evaluate_all('els => els.map(el => el.value)') == list(SCHEDULERS)
    for family in FAMILIES:
        page.locator('#model').select_option(family)
        expected = defaults({'sdcpp': {'family': family}})
        for key in ('steps', 'cfg_scale', 'guidance', 'sampler', 'scheduler'):
            assert page.locator('#' + key).input_value() == str(expected[key]).removesuffix('.0')
