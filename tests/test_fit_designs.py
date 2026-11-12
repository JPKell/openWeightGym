"""Exercise Dry fit proposals with sample data only, including RAM and reviewed sets."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-fit-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Preview actions must not contact live APIs.'
        browser.close()


def model(page, name):
    return page.locator(f'#models [data-model="{name}"]')


def pick(page, name):
    inline = page.locator('#workspace').get_attribute('data-design') == 'matrix'
    if not inline:
        page.locator('#catalogue-open').click()
    page.locator(f'[data-catalogue-select="{name}"]').check()
    if not inline:
        page.locator('#catalogue-done').click()


def test_layouts_all_nodes_states_themes_and_narrow_screens(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1440, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in ('workbench', 'matrix', 'studio'):
                page.locator(f'button[data-design="{layout}"]').click()
                expect(page.locator(f'button[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
                for node in ('atlas', 'orion', 'cedar'):
                    page.locator('#node').select_option(node)
                    for state in ('ready', 'unknown', 'offline', 'empty'):
                        page.locator('#scenario').select_option(state)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout, node, state)
                        assert page.evaluate('''() => {
                            const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                            return ids.length === new Set(ids).size;
                        }''')
                        if state in ('unknown', 'offline'):
                            expect(page.locator('#verdict')).to_have_attribute('data-state', 'unknown')
                            expect(page.locator('#auto-fit')).to_be_disabled()
                        if node == 'cedar':
                            expect(page.locator('[data-resource]')).to_have_count(1)
                            expect(page.locator('[data-resource]')).to_contain_text('System RAM')
                            assert 'integrated' not in page.locator('#workspace').inner_text().lower()
                        if state == 'empty':
                            expect(page.locator('#save')).to_be_disabled()
                            expect(page.locator('#models-empty')).to_be_visible()


def test_mixed_selection_filters_overcommit_autofit_and_locks(preview):
    page = preview
    expect(page.locator('#verdict')).to_have_attribute('data-state', 'fits')
    model(page, 'gemma4').locator('[data-lock]').check()
    pick(page, 'sdxl')
    expect(page.locator('#verdict')).to_have_attribute('data-state', 'over')
    expect(page.locator('#live-budget-state')).to_have_text('Over budget')
    page.locator('[data-kind-filter="image"]').click()
    expect(page.locator('#models [data-model]')).to_have_count(1)
    expect(page.locator('#selection-count')).to_contain_text('4 selected')
    expect(page.locator('#selection-count')).to_contain_text('1 audio · 1 image')
    expect(page.locator('#models [data-context]')).to_have_count(0)
    page.locator('#auto-fit').click()
    expect(page.locator('#verdict')).to_have_attribute('data-state', 'fits')
    expect(page.locator('#live-budget-state')).to_have_text('Fits estimate')
    page.locator('[data-kind-filter="all"]').click()
    expect(model(page, 'gemma4').locator('[data-context]')).to_have_value('16384')
    assert 2048 <= int(model(page, 'qwen8').locator('[data-context]').input_value()) < 32768
    # Locked contexts cannot be lowered behind the user's back.
    model(page, 'qwen8').locator('[data-lock]').check()
    model(page, 'qwen8').locator('[data-context]').fill('65536')
    expect(page.locator('#verdict')).to_have_attribute('data-state', 'over')
    expect(page.locator('#auto-fit')).to_be_disabled()


def test_context_limit_switches_preserve_custom_saved_values(preview):
    page = preview
    first = model(page, 'qwen8')
    context = first.locator('[data-context]')
    switch = first.locator('[data-context-override]')
    context.fill('99999')
    expect(page.locator('#save')).to_be_disabled()
    switch.check()
    expect(context).to_have_value('99999')
    expect(page.locator('#save')).to_be_enabled()
    expect(model(page, 'gemma4').locator('[data-context-override]')).not_to_be_checked()
    expect(model(page, 'whisper').locator('[data-context-override]')).to_have_count(0)
    context.fill('512')
    switch.uncheck()
    expect(context).to_have_value('512')
    expect(page.locator('#save')).to_be_disabled()
    first.locator('[data-select]').click()
    expect(page.locator('#save')).to_be_enabled()
    pick(page, 'qwen8')
    expect(context).to_have_value('512')
    expect(page.locator('#save')).to_be_disabled()
    switch.check()
    context.fill('99999')
    page.locator('#set-name').fill('Custom context')
    page.locator('#save').click()
    page.reload()
    page.locator('#restore').click()
    expect(context).to_have_value('99999')
    expect(switch).to_be_checked()
    expect(page.locator('#save')).to_be_enabled()


def test_cpu_context_hybrid_reservations_and_unknown_profiles(preview):
    page = preview
    page.locator('#clear').click()
    pick(page, 'oss')
    expect(page.locator('#memory-notes')).to_contain_text('Hybrid profile selected')
    expect(page.locator('[data-resource="ram"] [data-resource-used]')).to_have_text('16.0 GiB')
    expect(page.locator('[data-resource="gpu0"] [data-resource-used]')).to_have_text('4.4 GiB')
    page.locator('#clear').click()
    pick(page, 'flux')
    expect(page.locator('#verdict')).to_have_attribute('data-state', 'unknown')
    page.locator('#review-open').click()
    expect(page.locator('#simulate-load')).to_be_disabled()
    page.locator('#review [data-close]').click()
    page.locator('#node').select_option('cedar')
    before = page.locator('[data-resource="ram"] [data-resource-used]').inner_text()
    model(page, 'qwen4').locator('[data-context]').fill('32001')
    assert page.locator('[data-resource="ram"] [data-resource-used]').inner_text() != before
    page.locator('#node').select_option('atlas')
    expect(model(page, 'flux').locator('[data-select]')).to_be_checked()
    page.locator('#node').select_option('cedar')
    expect(model(page, 'qwen4').locator('[data-context]')).to_have_value('32001')


def test_saved_mixed_set_exact_context_versions_persistence_and_api(preview):
    page = preview
    pick(page, 'sdxl')
    page.locator('#auto-fit').click()
    model(page, 'qwen8').locator('[data-context]').fill('6001')
    # Names are handled as text, including shell/HTML punctuation in the API sample.
    name = 'Daily "mix" $(touch no) <img>'
    page.locator('#set-name').fill(name)
    page.locator('#save').click()
    expect(page.locator('#fit-message')).to_contain_text('version 1')
    page.reload()
    page.locator('#restore').click()
    expect(model(page, 'qwen8').locator('[data-context]')).to_have_value('6001')
    expect(model(page, 'sdxl').locator('[data-select]')).to_be_checked()
    expect(model(page, 'whisper').locator('[data-select]')).to_be_checked()
    expect(page.locator('#set-name')).to_have_value(name)
    assert page.locator('#workspace img').count() == 0
    page.locator('#review-open').click()
    expect(page.locator('#review-models tr')).to_have_count(4)
    expect(page.locator('#simulate-load')).to_be_enabled()
    page.locator('#simulate-load').click()
    expect(page.locator('#fit-message')).to_contain_text('No real API was called')
    page.locator('#replace').uncheck()
    page.locator('#save').click()
    expect(page.locator('#fit-message')).to_contain_text('Tick Replace')
    page.locator('#replace').check()
    page.locator('#save').click()
    expect(page.locator('#fit-message')).to_contain_text('version 2')
    page.locator('#api-open').click()
    expect(page.locator('#api-note')).to_contain_text('Sample set only')
    expect(page.locator('#api-note')).to_contain_text('API below is implemented')
    expect(page.locator('#api-code')).to_contain_text('/turnstile/machines/atlas/control/loadouts')
    expect(page.locator('#api-code')).to_contain_text('"action":"preview"')
    expect(page.locator('#api-code')).to_contain_text('"action":"request"')
    expect(page.locator('#api-code')).to_contain_text("<<'OWG_JSON'")
    expect(page.locator('#api-code')).to_contain_text('TOKEN_FROM_REVIEW')
    assert '\n+  -H' not in page.locator('#api-code').inner_text()
    page.locator('#api [data-close]').click()
    page.locator('#node').select_option('cedar')
    expect(page.locator('#saved-sets')).to_contain_text('No saved sets yet')
    page.locator('#set-name').fill(name)
    page.locator('#save').click()
    expect(page.locator('#fit-message')).to_contain_text('version 1')
    page.locator('#node').select_option('atlas')
    expect(page.locator('#saved-sets')).to_contain_text('v2')


def test_studio_catalogue_review_stale_selection_and_invalid_context(preview):
    page = preview
    page.locator('button[data-design="studio"]').click()
    page.locator('#clear').click()
    expect(page.locator('#models [data-model]')).to_have_count(30)
    expect(page.locator('#models [data-select]:checked')).to_have_count(0)
    pick(page, 'qwen8')
    expect(page.locator('#models [data-select]:checked')).to_have_count(1)
    page.locator('#save').click()
    page.locator('#review-open').click()
    expect(page.locator('#simulate-load')).to_be_enabled()
    page.locator('#review [data-close]').click()
    model(page, 'qwen8').locator('[data-context]').fill('32001')
    page.locator('#review-open').click()
    expect(page.locator('#simulate-load')).to_be_disabled()
    expect(page.locator('#review-status')).to_contain_text('Save this exact selection')
    page.locator('#review [data-close]').click()
    model(page, 'qwen8').locator('[data-context]').fill('999999')
    expect(page.locator('#save')).to_be_disabled()
    expect(page.locator('#review-open')).to_be_disabled()
    model(page, 'qwen8').locator('[data-context]').fill('5001')
    expect(page.locator('#save')).to_be_enabled()
    page.locator('#node').select_option('cedar')
    pick(page, 'sd15')
    expect(page.locator('#selection-count')).to_contain_text('1 image')


def test_autofit_reports_impossible_minimum_without_changing_contexts(preview):
    page = preview
    # Both big language models are left fixed at maximum; only the small hybrid can move.
    for name in ('qwen8', 'gemma4'):
        model(page, name).locator('[data-context]').fill('65536' if name == 'qwen8' else '32768')
        model(page, name).locator('[data-lock]').check()
    pick(page, 'oss')
    before = model(page, 'oss').locator('[data-context]').input_value()
    page.locator('#auto-fit').click()
    expect(page.locator('#fit-message')).to_contain_text('Cannot auto fit')
    expect(model(page, 'oss').locator('[data-context]')).to_have_value(before)
    expect(page.locator('#verdict')).to_have_attribute('data-state', 'over')


def test_thirty_model_picker_search_sort_and_keyboard_selection(preview):
    page = preview
    expect(page.locator('#models [data-model]')).to_have_count(3)
    model(page, 'qwen8').locator('[data-context]').fill('6001')
    page.locator('#catalogue-open').click()
    expect(page.locator('[data-catalogue-select]')).to_have_count(30)
    expect(page.locator('#catalogue-count')).to_contain_text('30 of 30')
    assert page.locator('#catalogue-list').evaluate('el => el.scrollHeight > el.clientHeight')
    page.locator('#catalogue-search').fill('SDXL')
    expect(page.locator('[data-catalogue-select]')).to_have_count(2)
    page.locator('#catalogue-kind').select_option('audio')
    expect(page.locator('[data-catalogue-select]')).to_have_count(0)
    expect(page.locator('#catalogue-list')).to_contain_text('No matching models')
    page.locator('#catalogue-search').fill('')
    page.locator('#catalogue-kind').select_option('all')
    page.locator('#catalogue-sort').select_option('memory')
    expect(page.locator('[data-catalogue-select]').first).to_have_attribute('data-catalogue-select', 'kokoro')
    page.locator('#catalogue-sort').select_option('name')
    last = page.locator('[data-catalogue-select]').last
    last_id = last.get_attribute('id')
    last.focus()
    last.press('Space')
    expect(page.locator('#' + last_id)).to_be_checked()
    expect(page.locator('#' + last_id)).to_be_focused()
    assert page.locator('#catalogue-list').evaluate('el => el.scrollTop > 0')
    page.locator('#catalogue-done').click()
    expect(page.locator('#models [data-model]')).to_have_count(4)
    expect(model(page, 'qwen8').locator('[data-context]')).to_have_value('6001')
    for row in page.locator('#models [data-model]').all():
        assert row.bounding_box()['height'] <= 80


def test_large_selection_and_full_sheet_keep_scrolling_inside_lists(preview):
    page = preview
    page.locator('button[data-design="matrix"]').click()
    for checkbox in page.locator('[data-catalogue-select]').all():
        checkbox.check()
    expect(page.locator('#models [data-model]')).to_have_count(30)
    expect(page.locator('#selection-count')).to_contain_text('30 selected')
    assert page.locator('.fit-row-scroll').evaluate('el => el.clientHeight <= 430 && el.scrollHeight > el.clientHeight')
    assert page.locator('#catalogue-list').evaluate('el => el.clientHeight <= 430 && el.scrollHeight > el.clientHeight')
    page.locator('.fit-row-scroll').evaluate('el => el.scrollTop = el.scrollHeight')
    expect(page.locator('#models [data-model]').last).to_be_in_viewport()
    page.locator('button[data-design="studio"]').click()
    page.locator('#clear').click()
    expect(page.locator('#models [data-model]')).to_have_count(30)
    expect(page.locator('#models [data-select]:checked')).to_have_count(0)
    heights = page.locator('#models [data-model]').evaluate_all('rows => rows.map(row => row.getBoundingClientRect().height)')
    assert max(heights) <= 46
    # Absolute accessibility labels must stay inside their scroll container.
    assert page.evaluate('document.documentElement.scrollHeight <= document.body.getBoundingClientRect().height + 2')
    page.locator('.fit-row-scroll').evaluate('el => el.scrollTop = el.scrollHeight')
    page.locator('#models [data-model]').last.locator('[data-select]').check()
    expect(page.locator('#selection-count')).to_contain_text('1 selected')
