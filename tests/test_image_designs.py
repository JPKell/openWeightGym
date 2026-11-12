"""Integrated image proposals exercise independent workflows without contacting live services."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ('studio', 'board', 'matrix')


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-image-designs.py'))['render']())
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
        assert not requests, 'Image proposals must never contact live services.'


def choose(page, name):
    container = '#matrix-rows' if page.locator('#workspace').get_attribute('data-layout') == 'matrix' else '#workflow-list'
    page.locator(container).get_by_role('button', name=name, exact=container == '#matrix-rows').click()


@pytest.mark.parametrize('design', DESIGNS)
def test_workflows_are_independent_versioned_and_retained_after_reload(preview, design):
    page = preview
    page.locator(f'[data-design={design}]').click()
    expect(page.locator('#draft-state')).to_have_text('Saved')
    page.locator('#new-workflow').click()
    page.locator('#workflow-name').fill('Architecture study')
    page.locator('#wf-steps').fill('18')
    page.locator('#wf-shared').check()
    page.locator('#save-workflow').click()
    expect(page.locator('#version-badge')).to_have_text('Version 1')
    expect(page.locator('#workflow-count')).to_contain_text('5 workflows · 1 model setup')
    page.locator('#wf-steps').fill('20')
    page.locator('#save-workflow').click()
    expect(page.locator('#version-badge')).to_have_text('Version 2')
    choose(page, 'Quick exploration')
    expect(page.locator('#wf-steps')).to_have_value('4')
    choose(page, 'Architecture study')
    expect(page.locator('#wf-steps')).to_have_value('20')
    page.locator('summary').filter(has_text='Version history').click()
    page.locator('#history').select_option('1')
    page.locator('#restore').click()
    expect(page.locator('#wf-steps')).to_have_value('18')
    page.locator('#save-workflow').click()
    expect(page.locator('#version-badge')).to_have_text('Version 3')
    page.reload()
    choose(page, 'Architecture study')
    expect(page.locator('#wf-steps')).to_have_value('18')
    expect(page.locator('#wf-shared')).to_be_checked()
    expect(page.locator('#history option')).to_have_count(3)


def test_drafts_duplicate_and_family_specific_controls(preview):
    page = preview
    page.locator('#wf-steps').fill('19')
    for design in DESIGNS:
        page.locator(f'[data-design={design}]').click()
        expect(page.locator('#wf-steps')).to_have_value('19')
    page.locator('[data-model=sdxl]').click()
    expect(page.locator('#wf-negative_prompt')).to_be_visible()
    expect(page.locator('#wf-cfg_scale')).to_be_visible()
    expect(page.locator('#wf-guidance')).not_to_be_visible()
    page.locator('#wf-negative_prompt').fill('film grain, blur')
    page.locator('[data-model=dev]').click()
    expect(page.locator('#wf-guidance')).to_be_visible()
    expect(page.locator('#wf-strength')).not_to_be_visible()
    page.locator('[data-model=flux]').click()
    expect(page.locator('#wf-steps')).to_have_value('19')
    page.locator('#duplicate').click()
    expect(page.locator('#workflow-name')).to_have_value('Quick exploration copy')
    page.locator('#save-workflow').click()
    choose(page, 'Quick exploration')
    expect(page.locator('#wf-steps')).to_have_value('19')  # Unsaved source draft is preserved.
    page.locator('#discard').click()
    expect(page.locator('#wf-steps')).to_have_value('4')
    page.locator('[data-model=sdxl]').click()
    expect(page.locator('#wf-negative_prompt')).to_have_value('film grain, blur')


def test_model_creation_editing_limits_and_empty_state(preview):
    page = preview
    page.locator('[data-model=empty]').click()
    expect(page.locator('#workflows-empty')).to_be_visible()
    page.locator('[data-create]').click()
    page.locator('#workflow-name').fill('First sketch')
    page.locator('#save-workflow').click()
    expect(page.locator('#workflow-count')).to_contain_text('1 workflow')
    page.locator('#add-model').click()
    page.locator('#setup-name').fill('New image model')
    page.locator('#setup-gpu').select_option('GPU 0 + GPU 1')
    page.get_by_role('button', name='Save model setup', exact=True).click()
    expect(page.locator('#model-error')).to_contain_text('multi-GPU')
    page.locator('#setup-split').select_option('components')
    page.get_by_role('button', name='Save model setup', exact=True).click()
    expect(page.locator('#model-dialog')).not_to_be_visible()
    expect(page.locator('#model-name')).to_have_text('New image model')
    page.locator('#workflow-name').fill('New model workflow')
    page.locator('#wf-steps').fill('30')
    page.locator('#save-workflow').click()
    page.locator('#edit-model').click()
    page.locator('#setup-max_steps').fill('20')
    page.get_by_role('button', name='Save model setup', exact=True).click()
    expect(page.locator('#model-error')).to_contain_text('New model workflow')
    page.locator('#setup-max_steps').fill('40')
    page.locator('#setup-name').fill('Renamed image model')
    page.get_by_role('button', name='Save model setup', exact=True).click()
    expect(page.locator('#model-name')).to_have_text('Renamed image model')
    expect(page.locator('#workflow-name')).to_have_value('New model workflow')
    page.reload()
    page.get_by_role('button', name='Renamed image model · 1 workflows', exact=True).click()
    expect(page.locator('#wf-steps')).to_have_value('30')


def test_themes_narrow_layouts_search_and_keyboard(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1600, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for design in DESIGNS:
                page.locator(f'[data-design={design}]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#workflow-search').fill('no such workflow')
                expect(page.locator('#search-empty')).to_be_visible()
                page.locator('#clear-search').click()
                page.locator('#sharing-filter').select_option('shared')
                expect(page.locator('#workflow-list [data-workflow]')).to_have_count(2)
                page.locator('#sharing-filter').select_option('all')
                page.locator('#edit-model').click()
                assert page.locator('#model-dialog').evaluate('el => el.scrollWidth <= el.clientWidth')
                page.keyboard.press('Escape')
                expect(page.locator('#edit-model')).to_be_focused()
    page.locator('[data-design=studio]').click()
    button = page.locator('#workflow-list [data-workflow]').nth(1)
    button.focus()
    page.keyboard.press('Enter')
    expect(page.locator('#workflow-name')).to_have_value('Product stills')
    page.locator('#model-search').fill('no such model')
    expect(page.locator('#models-empty')).to_be_visible()


def test_duplicate_names_and_pixel_budget_are_reported(preview):
    page = preview
    page.locator('#new-workflow').click()
    page.locator('#workflow-name').fill('Quick exploration')
    page.locator('#save-workflow').click()
    expect(page.locator('#status')).to_contain_text('already exists')
    page.locator('#workflow-name').fill('Too large')
    page.locator('#edit-model').click()
    page.locator('#setup-max_pixels').fill('2097152')
    page.get_by_role('button', name='Save model setup', exact=True).click()
    page.locator('#wf-width').fill('2048')
    page.locator('#wf-height').fill('2048')
    page.locator('#save-workflow').click()
    expect(page.locator('#status')).to_contain_text('pixel budget')
    expect(page.locator('#workflow-count')).to_contain_text('4 workflows')
