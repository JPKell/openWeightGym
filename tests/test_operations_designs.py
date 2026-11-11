"""Browser checks for the standalone, sample-only operations design proposals."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-operations-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        assert page.evaluate('''() => {
            const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
            return ids.length === new Set(ids).size;
        }''')
        yield page
        assert not errors
        assert not network, 'The preview must never contact real services.'
        browser.close()


def test_all_layouts_scale_and_fit_both_themes(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1100})
            for design in ('ledger', 'grid', 'focus'):
                page.locator(f'button[data-design="{design}"]').click()
                for view in ('status', 'telemetry'):
                    page.locator(f'button[data-page="{view}"]').click()
                    for size in ('1', '4', '12', '48'):
                        page.locator('#fleet-size').select_option(size)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, design, view, size)
                        assert page.locator('#fleet-panel [data-open]').count() <= 12
                        playwright.expect(page.locator('#detail-title')).not_to_have_text('Select a node')
                    page.locator('#next').click()
                    playwright.expect(page.locator('#page-count')).to_contain_text('13–24')


def test_empty_search_and_zero_workers_keep_front_visible(preview):
    page = preview
    page.locator('#search').fill('no-matching-machine')
    playwright.expect(page.locator('#empty-title')).to_have_text('No matching nodes or hosts')
    playwright.expect(page.locator('#inspector')).to_be_hidden()
    page.locator('#clear-filters').click()
    page.locator('#fleet-size').select_option('0')
    playwright.expect(page.locator('#empty-title')).to_have_text('No compute nodes yet')
    page.locator('button[data-page="telemetry"]').click()
    playwright.expect(page.locator('#fleet-rows tr')).to_have_count(1)
    playwright.expect(page.locator('#detail-title')).to_have_text('front')
    playwright.expect(page.locator('#switch-detail')).to_be_disabled()


def test_shared_host_capacity_and_cpu_only_service(preview):
    page = preview
    page.get_by_role('button', name='Inspect atlas-cpu', exact=True).click()
    playwright.expect(page.locator('#inspector [data-value="gpu"]')).to_have_text('No GPU')
    page.locator('#switch-detail').click()
    playwright.expect(page.locator('#detail-title')).to_have_text('atlas')
    playwright.expect(page.locator('#fleet-rows tr')).to_have_count(4)
    playwright.expect(page.locator('#services button')).to_have_count(2)
    # Atlas 64 + Orion 128 + front 16; offline Nova is excluded, atlas is counted once.
    playwright.expect(page.locator('[data-stat="waiting"]')).to_have_text('208 GiB')
    playwright.expect(page.locator('[data-stat="running"]')).to_have_text('3')


def test_drain_is_scoped_and_running_work_continues(preview):
    page = preview
    page.get_by_role('button', name='Inspect atlas-gpu', exact=True).click()
    before = sorted(page.locator('#requests tr').all_text_contents())
    page.locator('#node-action').click()
    playwright.expect(page.locator('#detail-state')).to_have_text('Draining')
    assert sorted(page.locator('#requests tr').all_text_contents()) == before
    cpu_row = page.locator('#fleet-rows tr').filter(has=page.get_by_role('button', name='Inspect atlas-cpu', exact=True))
    playwright.expect(cpu_row.locator('[data-state]')).to_have_text('Available')
    page.locator('#node-action').click()
    playwright.expect(page.locator('#detail-state')).to_have_text('Available')
    page.locator('#models [data-lock]').click()
    playwright.expect(page.locator('#models [data-lock]')).to_have_text('Unlock')
    page.get_by_role('button', name='Inspect nova-gpu', exact=True).click()
    playwright.expect(page.locator('#node-action')).to_be_disabled()


def test_missing_readings_stay_unknown_and_recovery_is_explicit(preview):
    page = preview
    for scenario in ('stale', 'outage'):
        page.locator('#scenario').select_option(scenario)
        playwright.expect(page.locator('[data-stat="running"]')).to_have_text('—')
        assert set(page.locator('#fleet-rows [data-value="cpu"]').all_text_contents()) == {'—'}
        playwright.expect(page.locator('#node-action')).to_be_disabled()
    playwright.expect(page.locator('#monitor-warning')).to_be_visible()
    page.wait_for_function("echarts.getInstanceByDom(document.querySelector('#detail-chart')).getOption().series[0].data.at(-1)[1] === null")
    assert page.locator('#detail-chart').evaluate('el => echarts.getInstanceByDom(el).getOption().series[0].connectNulls') is False
    page.locator('#refresh').click()
    playwright.expect(page.locator('#ops-message')).to_contain_text('refresh failed')
    page.locator('#retry').click()
    playwright.expect(page.locator('#monitor-warning')).to_be_hidden()
    playwright.expect(page.locator('[data-stat="running"]')).to_have_text('5')


def test_comparison_limit_range_and_keyboard_selection(preview):
    page = preview
    page.locator('#fleet-size').select_option('12')
    page.locator('button[data-page="telemetry"]').click()
    for name in ('atlas', 'orion', 'front'):
        page.get_by_label(f'Compare {name}', exact=True).check()
    # A fourth checkbox is reverted while the original comparison is retained.
    page.get_by_label('Compare nova', exact=True).click()
    playwright.expect(page.get_by_label('Compare nova', exact=True)).not_to_be_checked()
    playwright.expect(page.locator('#ops-message')).to_contain_text('up to 3')
    page.locator('#range').select_option('672')
    page.locator('#metric').select_option('ram')
    playwright.expect(page.locator('#compare-description')).to_contain_text('RAM used · 4 weeks')
    page.locator('button[data-design="focus"]').click()
    page.get_by_role('button', name='Inspect atlas', exact=True).focus()
    page.keyboard.press('Enter')
    playwright.expect(page.locator('#detail-title')).to_have_text('atlas')
    playwright.expect(page.locator('#compare-selected')).to_have_text('Remove comparison')
    page.locator('#clear-compare').click()
    playwright.expect(page.locator('#compare-panel')).to_be_hidden()
