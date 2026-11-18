"""Image generation → Compare: one run's images in the shared comparison grid, filtered by the settings it varied."""

import pytest

from test_console_browser import browser_context, playwright
from test_image_studio import studio  # noqa: F401
from test_sampler_comparison import BASE, comparison  # noqa: F401
from test_weightroom import desk  # noqa: F401

expect = playwright.expect


@pytest.mark.parametrize('width', [1440, 390])
def test_a_sweep_opens_in_the_compare_view_with_shared_zoom_size_selection_and_filter(comparison, width):
    server, front, held, run = comparison
    single = server.post('/mirrorwall-api/jobs', json={'request': BASE})
    assert single.status_code == 201, single.text
    run(single.json()['id'])
    sweep = server.post('/mirrorwall-api/jobs', json={'request': BASE, 'sweep': {'seed': [1, 2, 3], 'steps': [10, 20]}})
    assert sweep.status_code == 201, sweep.text
    run(sweep.json()['id'])
    engine = playwright.sync_playwright().start()
    browser, page = browser_context(engine, server)
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto('http://console.test/chat/images')
        page.locator('[data-mw-view="queue"]').click()
        expect(page.locator('#mw-jobs .mw-job')).to_have_count(2)
        # Only the run of more than one image offers Compare.
        expect(page.locator('#mw-jobs .mw-job').nth(0).locator('[data-compare]')).to_be_visible()
        expect(page.locator('#mw-jobs .mw-job').nth(1).locator('[data-compare]')).to_be_hidden()
        page.locator('#mw-jobs .mw-job').nth(0).locator('[data-compare]').click()
        expect(page.locator('#mw-compare-view')).to_be_visible()
        expect(page.locator('.ig-settings-panel')).to_be_hidden()
        expect(page.locator('#mw-cmp-run')).to_have_value(sweep.json()['id'])
        expect(page.locator('#mw-cmp-run option')).to_have_count(2)  # the single image is not a run to compare
        expect(page.locator('#mw-cmp-grid > .cg-tile[data-state=done]')).to_have_count(6)
        expect(page.locator('#mw-cmp-status')).to_have_text('6 of 6 done.')
        expect(page.locator('.cg-tile').first.locator('[data-caption]')).to_have_text('seed 1 · steps 10')
        expect(page.locator('.cg-tile').first.locator('[data-detail]')).to_have_text('CFG 5')
        # Filter: one group per varied setting, with this run's values.
        page.locator('#mw-cmp-filter-toggle').click()
        expect(page.locator('#mw-cmp-filter-seeds .form-check-label')).to_have_text(['seed 1', 'seed 2', 'seed 3'])
        expect(page.locator('#mw-cmp-filter-stepss .form-check-label')).to_have_text(['steps 10', 'steps 20'])
        page.locator('#mw-cmp-filter-seeds input[value="3"]').uncheck()
        expect(page.locator('.cg-tile:visible')).to_have_count(4)
        page.locator('[data-cg-filter-none="steps"]').click()
        expect(page.locator('.cg-tile:visible')).to_have_count(0)
        page.locator('#mw-cmp-filter-reset').click()
        expect(page.locator('#mw-cmp-filter-count')).to_have_text('Showing 6 of 6 images')
        # One zoom and focus point for every image, at full resolution; arrow keys move it, not the studio's image.
        page.locator('#mw-cmp-zoom').fill('3')
        expect(page.locator('#mw-cmp-zoom-value')).to_have_text('3×')
        assert page.locator('.cg-tile img').first.get_attribute('src').endswith('/content')
        frame = page.locator('.cg-tile').nth(1).locator('.cg-frame')
        frame.focus(); page.keyboard.press('ArrowRight')
        assert page.evaluate("parseFloat(getComputedStyle(document.getElementById('mw-cmp-grid')).getPropertyValue('--cg-x'))") == pytest.approx(55)
        expect(page.locator('#mw-compare-view')).to_be_visible()
        origins = page.locator('.cg-tile img').evaluate_all('images => images.map(image => getComputedStyle(image).transformOrigin.split(" ").map((value, axis) => (parseFloat(value) / (axis ? image.clientHeight : image.clientWidth)).toFixed(2)).join())')
        assert len(set(origins)) == 1
        # Image size, then select two and show only those.
        page.locator('#mw-cmp-size').fill('300')
        expect(page.locator('#mw-cmp-size-value')).to_have_text('300 px')
        page.locator('.cg-tile').nth(0).locator('[data-pick]').check()
        page.locator('.cg-tile').nth(5).locator('[data-pick]').check()
        page.locator('#mw-cmp-selected-only').check()
        expect(page.locator('.cg-tile:visible')).to_have_count(2)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # The Compare pill opens the newest sweep on its own, and the size is remembered.
        page.reload()
        page.locator('[data-mw-view="compare"]').click()
        expect(page.locator('#mw-cmp-run')).to_have_value(sweep.json()['id'])
        expect(page.locator('#mw-cmp-grid > .cg-tile')).to_have_count(6)
        expect(page.locator('#mw-cmp-size-value')).to_have_text('300 px')
        page.locator('[data-mw-view="generate"]').click()
        expect(page.locator('#mw-compare-view')).to_be_hidden()
        assert not errors
    finally:
        browser.close(); engine.stop()
