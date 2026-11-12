"""Individual-file proposals: responsive information and safe browser-local operations."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-file-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url))
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not any(url.startswith(('http:', 'https:')) for url in requests)
        browser.close()


def test_layouts_fit_and_keep_information_in_both_appearances(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1440, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for design in ('dossier', 'inspector', 'placement'):
                page.locator(f'[data-design="{design}"]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, design)
                playwright.expect(page.locator(f'[data-design="{design}"]')).to_have_attribute('aria-pressed', 'true')
                playwright.expect(page.get_by_role('heading', name='Inside the file')).to_be_visible()
                playwright.expect(page.get_by_role('table', name='File destinations')).to_be_visible()
    # All expanded details fit in one desktop screen, apart from the review footer.
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('[data-design="dossier"]').click()
    assert page.locator('.file-layout').bounding_box()['height'] < 720


def test_search_sort_and_drafts_survive_layout_switches(preview):
    page = preview
    page.get_by_label('Search metadata').fill('no-such-entry')
    playwright.expect(page.locator('#no-metadata')).to_be_visible()
    page.get_by_label('Search metadata').fill('head_count')
    playwright.expect(page.locator('#metadata-count')).to_have_text('2 / 22 entries')
    page.get_by_label('Metadata group').select_option('general.')
    playwright.expect(page.locator('#no-metadata')).to_be_visible()
    page.get_by_label('Metadata group').select_option('all')
    page.locator('#place').select_option('small')
    for design in ('inspector', 'placement', 'dossier'):
        page.locator(f'[data-design="{design}"]').click()
        playwright.expect(page.get_by_label('Search metadata')).to_have_value('head_count')
        playwright.expect(page.locator('#place')).to_have_value('small')
        playwright.expect(page.locator('#transfer')).to_be_disabled()
    page.get_by_role('button', name='Name ↕', exact=True).click()
    playwright.expect(page.locator('#metadata th').first).to_have_attribute('aria-sort', 'ascending')


def test_transfer_completion_updates_nodes_and_move_path(preview):
    page = preview
    playwright.expect(page.get_by_label('Distribute to Atlas', exact=True)).to_be_disabled()
    playwright.expect(page.get_by_label('Distribute to Orion', exact=True)).to_be_disabled()
    page.locator('#distribute').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('Distributed to Nova')
    playwright.expect(page.get_by_label('Distribute to Nova', exact=True)).to_be_disabled()
    page.locator('#operation').select_option('move')
    page.locator('#transfer').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('model paths updated')
    playwright.expect(page.locator('#file-path')).to_contain_text('/archive/')
    playwright.expect(page.locator('#transfer')).to_be_disabled()
    page.locator('#reset').click()
    playwright.expect(page.locator('#file-heading .eyebrow')).to_contain_text('ACTIVE CACHE')
    playwright.expect(page.locator('#file-path')).to_contain_text('/cache/')


def test_companion_unreadable_failure_and_deletion_states(preview):
    page = preview
    page.locator('#scenario').select_option('failure')
    page.locator('#distribute').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('source file is retained')
    page.locator('#scenario').select_option('unreadable')
    playwright.expect(page.locator('#metadata-unavailable')).to_be_visible()
    playwright.expect(page.locator('#metadata-available')).to_be_hidden()
    for scenario in ('adapter', 'projector'):
        page.locator('#scenario').select_option(scenario)
        page.locator('[data-design="placement"]').click()
        playwright.expect(page.locator('#part-card')).to_be_visible()
        page.locator('#new').fill('my-new-model')
        page.get_by_role('button', name='Make the model', exact=True).click()
        playwright.expect(page.locator('#action-status')).to_contain_text(f'with this {scenario}')
    page.locator('#scenario').select_option('unused')
    page.locator('.file-delete summary').click()
    page.locator('#delete-name').fill('wrong-name')
    playwright.expect(page.locator('#delete')).to_be_disabled()
    page.locator('#delete-name').fill('Qwen3-32B-Q6_K.gguf')
    page.locator('#delete').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('Sample file deleted')
    playwright.expect(page.locator('#transfer')).to_be_disabled()
    page.locator('#reset').click()
    playwright.expect(page.locator('#delete-name')).to_be_enabled()
    playwright.expect(page.locator('#transfer')).to_be_enabled()
