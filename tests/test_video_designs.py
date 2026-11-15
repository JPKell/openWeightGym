"""Offline proposals share controls, keep drafts and cover responsive failure states."""
import runpy
from pathlib import Path

import pytest

playwright=pytest.importorskip('playwright.sync_api')
expect=playwright.expect
ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output=tmp_path/'video.html'
    output.write_text(runpy.run_path(str(ROOT/'scripts/preview-video-designs.py'))['render']())
    with playwright.sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1600,'height':1000})
        errors=[];requests=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:requests.append(r.url) if r.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        browser.close()
        assert not errors
        assert not requests


def test_six_layouts_and_themes_keep_controls_and_drafts(preview):
    page=preview
    page.locator('#video-prompt').fill('My retained unsent clip')
    page.locator('#video-model').select_option('ltx')
    expect(page.locator('#video-soundtrack-row')).to_be_visible()
    page.locator('#video-model').select_option('wan-small')
    expect(page.locator('#video-prompt')).to_have_value('My retained unsent clip')
    for width in (1600,390):
        page.set_viewport_size({'width':width,'height':1000})
        for surface in ('generation','models'):
            page.locator('[data-surface='+surface+']').click()
            for layout in ('canvas','desk','review'):
                page.locator('[data-video-design='+layout+']').click()
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                page.locator('#video-theme').click()


def test_simulation_selection_reuse_stop_and_failure_states(preview):
    page=preview
    page.locator('#video-generate').click()
    expect(page.locator('#video-jobs')).to_contain_text('Completed simulation')
    expect(page.locator('#video-gallery > button')).to_have_count(2)
    page.locator('[data-id=sample]').click()
    page.locator('#video-sort').select_option('oldest')
    expect(page.locator('[data-id=sample]')).to_have_attribute('aria-selected','true')
    page.locator('#video-reuse').click()
    expect(page.locator('#video-prompt')).to_have_value('Rain beneath a banana leaf')
    page.locator('#video-generate').click();page.locator('#video-stop').click()
    expect(page.locator('#video-jobs')).to_contain_text('Cancelled')
    for state in ('empty','forbidden','runtime','quota','error','timeout','cancelled'):
        page.locator('#video-demo-state').select_option(state)
        expect(page.locator('#video-message')).to_be_visible()
        expect(page.locator('#video-gallery > button')).to_have_count(2)
