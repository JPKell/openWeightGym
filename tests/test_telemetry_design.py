"""Review the selected Telemetry workspace without contacting a live host."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-operations-designs.py'))['render']
    output = tmp_path / 'telemetry.html'
    output.write_text(render(telemetry=True))
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page(viewport={'width': 1600, 'height': 1050})
        errors, failures, requests = [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('requestfailed', lambda request: failures.append(request.url))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        assert page.evaluate('''() => {
            const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
            return ids.length === new Set(ids).size;
        }''')
        yield page
        assert not errors
        assert not failures
        assert not requests, 'Sample controls must never contact live services.'
        browser.close()


def test_initial_grid_represents_every_gpu_once(preview):
    page = preview
    playwright.expect(page.locator('#host-view')).to_be_hidden()
    playwright.expect(page.locator('#fleet-view')).to_be_visible()
    playwright.expect(page.locator('#host-grid [data-host]')).to_have_count(4)
    playwright.expect(page.locator('#host-grid [data-device]')).to_have_count(8)
    playwright.expect(page.locator('#host-grid [data-host="host-0"] [data-device]')).to_have_count(2)
    playwright.expect(page.locator('#fleet-totals')).to_contain_text('176 GiB')
    # An offline host still contributes inventory, with unknown readings.
    offline = page.locator('#host-grid [data-host="host-2"]')
    assert offline.locator('[data-gpu-load]').all_text_contents() == ['—', '—']
    assert page.locator('#graph-grid .tw-plot').count() == 0


def test_uptime_and_installed_service_pills_follow_the_selected_host(preview):
    page = preview
    page.get_by_role('button', name='Inspect atlas GPU 0', exact=True).click()
    playwright.expect(page.locator('#host-uptime-value')).to_have_text('2d 3h 12m')
    playwright.expect(page.locator('#host-services [data-service="loadcoach"]')).to_have_text('loadcoach · Running')
    playwright.expect(page.locator('#host-services [data-service="voice"]')).to_have_text('voice · Stopped')
    page.locator('#all-hosts').click()
    page.get_by_role('button', name='Inspect nova GPU 0', exact=True).click()
    playwright.expect(page.locator('#host-uptime-value')).to_have_text('Unknown')
    playwright.expect(page.locator('#host-services [data-service="loadcoach"]')).to_have_text('loadcoach · Unknown')


def test_gpu_drilldown_tabs_and_comparison_keep_devices_distinct(preview):
    page = preview
    page.get_by_role('button', name='Inspect orion GPU 1', exact=True).click()
    playwright.expect(page.locator('#host-title')).to_have_text('orion')
    playwright.expect(page.get_by_role('tab', name='GPU 1', exact=True)).to_have_attribute('aria-selected', 'true')
    playwright.expect(page.locator('#graph-grid .tw-plot')).to_have_count(1)
    playwright.expect(page.locator('#metric-choices button')).to_have_count(5)
    playwright.expect(page.locator('#graph-grid [data-current]').first).to_have_text('12.4 GiB')
    assert page.locator('#graph-grid .tw-plot').first.evaluate('e => echarts.getInstanceByDom(e).getOption().series[0].name') == 'GPU 1'
    page.get_by_role('tab', name='Compare GPUs', exact=True).click()
    assert page.locator('#graph-grid .tw-plot').first.evaluate('e => echarts.getInstanceByDom(e).getOption().series.map(s => s.name)') == ['GPU 0', 'GPU 1', 'GPU 2', 'GPU 3']
    page.get_by_role('button', name='Fan', exact=True).click()
    playwright.expect(page.locator('#graph-grid [data-extents]').last).to_contain_text('GPU 3 unavailable')
    page.get_by_role('tab', name='Processor & memory', exact=True).click()
    playwright.expect(page.locator('#metric-choices button')).to_have_count(3)
    page.get_by_role('tab', name='Storage', exact=True).click()
    playwright.expect(page.locator('#metric-choices button')).to_have_count(2)
    page.locator('#back-to-grid').click()
    playwright.expect(page.locator('#all-hosts')).to_have_attribute('aria-pressed', 'true')
    playwright.expect(page.locator('#fleet-view')).to_be_visible()
    assert page.evaluate("[...document.querySelectorAll('.tw-plot')].every(el => !echarts.getInstanceByDom(el))")


def test_keyboard_tabs_and_non_contiguous_gpu_slots(preview):
    page = preview
    page.locator('#fleet-size').select_option('12')
    page.get_by_role('button', name='Inspect lyra GPU 2', exact=True).click()
    playwright.expect(page.locator('#graph-title')).to_contain_text('GPU 2')
    page.get_by_role('tab', name='GPU 2', exact=True).focus()
    page.keyboard.press('ArrowLeft')
    playwright.expect(page.get_by_role('tab', name='GPU 0', exact=True)).to_be_focused()
    page.keyboard.press('Home')
    playwright.expect(page.get_by_role('tab', name='Processor & memory', exact=True)).to_be_focused()
    page.keyboard.press('End')
    playwright.expect(page.get_by_role('tab', name='Storage', exact=True)).to_be_focused()


def test_range_zoom_expansion_and_escape(preview):
    page = preview
    page.get_by_role('button', name='Inspect atlas GPU 0', exact=True).click()
    page.locator('#range').select_option('672')
    plot = page.locator('#graph-grid .tw-plot').first
    assert plot.evaluate('e => { const o = echarts.getInstanceByDom(e).getOption(); return o.xAxis[0].max - o.xAxis[0].min; }') == 672 * 3600000
    plot.evaluate("e => echarts.getInstanceByDom(e).dispatchAction({type:'dataZoom',start:25,end:75})")
    page.get_by_role('button', name='Expand Memory used', exact=True).click()
    playwright.expect(page.locator('#graph-dialog')).to_be_visible()
    assert page.locator('#expanded-chart').evaluate('e => echarts.getInstanceByDom(e).getOption().dataZoom[0].start') == 25
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#graph-dialog')).not_to_be_visible()
    playwright.expect(page.get_by_role('button', name='Expand Memory used', exact=True)).to_be_focused()
    page.wait_for_function("!echarts.getInstanceByDom(document.querySelector('#expanded-chart'))")
    page.locator('#reset-zoom').click()
    assert plot.evaluate('e => echarts.getInstanceByDom(e).getOption().dataZoom[0].start') == 0


def test_unknown_sensors_offline_and_real_zero_are_distinct(preview):
    page = preview
    page.get_by_role('button', name='Inspect atlas GPU 0', exact=True).click()
    page.get_by_role('button', name='Load', exact=True).click()
    playwright.expect(page.locator('#graph-grid [data-current]')).to_have_text('0%')
    page.locator('#back-to-grid').click()
    page.get_by_role('button', name='Inspect orion GPU 3', exact=True).click()
    page.get_by_role('button', name='Fan', exact=True).click()
    playwright.expect(page.locator('#graph-grid [data-current]').last).to_have_text('—')
    playwright.expect(page.locator('#graph-grid [data-extents]').last).to_have_text('Sensor unavailable')
    page.locator('#back-to-grid').click()
    page.get_by_role('button', name='Inspect nova GPU 1', exact=True).click()
    playwright.expect(page.locator('#stale-notice')).to_be_visible()
    assert set(page.locator('#graph-grid [data-current]').all_text_contents()) == {'—'}
    assert page.locator('#graph-grid .tw-plot').first.evaluate('e => {const s = echarts.getInstanceByDom(e).getOption().series[0]; return s.connectNulls === false && s.data.at(-1)[1] === null && s.data.some(p => p[1] !== null);}')
    page.locator('#scenario').select_option('stale')
    playwright.expect(page.locator('#fleet-totals')).to_contain_text('0 / 4')
    assert set(page.locator('#host-grid [data-gpu-load]').all_text_contents()) == {'—'}


def test_empty_and_cpu_only_hosts(preview):
    page = preview
    playwright.expect(page.locator('#host-grid [data-host]')).to_have_count(4)
    playwright.expect(page.locator('#host-search, #host-filter, #host-sort')).to_have_count(0)
    page.get_by_role('button', name='Open Control node telemetry', exact=True).click()
    playwright.expect(page.locator('#cpu-only')).to_be_visible()
    playwright.expect(page.get_by_role('tab')).to_have_count(2)
    page.get_by_role('button', name='Processor temperature', exact=True).click()
    playwright.expect(page.locator('#graph-grid [data-extents]')).to_have_text('Sensor unavailable')
    page.locator('#fleet-size').select_option('0')
    playwright.expect(page.locator('#empty-title')).to_have_text('No hosts registered')


def test_dense_fleets_and_mobile_fit_both_themes(preview):
    page = preview
    page.locator('#scenario').select_option('dense')
    for theme in ('dark', 'light'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1600, 900, 390):
            page.set_viewport_size({'width': width, 'height': 1050})
            for size in ('1', '4', '12', '48'):
                page.locator('#fleet-size').select_option(size)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, size)
                assert page.locator('#host-grid [data-device]').evaluate_all('rows => rows.length === new Set(rows.map(r => r.dataset.device)).size')
                assert page.locator('#host-grid [data-host="host-0"] [data-device]').count() == 8
            page.get_by_role('button', name='Inspect atlas GPU 7', exact=True).click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, 'detail')
            playwright.expect(page.locator('#host-view [data-device]')).to_have_count(8)
            page.get_by_role('button', name='Load', exact=True).click()
            page.get_by_role('button', name='Expand Load', exact=True).click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.keyboard.press('Escape')
            page.locator('#all-hosts').click()


def test_real_shell_kpis_and_workspace_stay_aligned(preview):
    page = preview
    playwright.expect(page.locator('#site-sidebar')).to_be_visible()
    playwright.expect(page.locator('#site-sidebar [aria-current="page"]')).to_have_text('Telemetry')
    kpis = page.locator('#fleet-totals').bounding_box()
    hosts = page.locator('.tw-sidebar').bounding_box()
    workspace = page.locator('#main-view').bounding_box()
    rail = page.locator('#site-sidebar').bounding_box()
    assert kpis['x'] >= rail['x'] + rail['width']
    assert abs(kpis['x'] - hosts['x']) < 1
    assert abs(kpis['x'] + kpis['width'] - workspace['x'] - workspace['width']) < 1
    assert abs(hosts['y'] - workspace['y']) < 1
    assert kpis['y'] + kpis['height'] < hosts['y']
    before = page.locator('#fleet-totals').inner_text()
    page.get_by_role('button', name='Inspect orion GPU 1', exact=True).click()
    assert page.locator('#fleet-totals').inner_text() == before
    assert page.locator('#fleet-totals').bounding_box() == kpis
    playwright.expect(page.locator('#host-list [data-host-open]')).to_have_count(4)
    page.locator('#sidebar-toggle').click()
    playwright.expect(page.locator('#site-sidebar')).to_be_hidden()
    assert page.locator('#main-view').bounding_box()['width'] > workspace['width']
    page.locator('#sidebar-toggle').click()
    playwright.expect(page.locator('#site-sidebar')).to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 900})
    page.locator('#mobile-menu-toggle').click()
    playwright.expect(page.locator('#mobile-navigation')).to_be_visible()
    page.locator('#mobile-navigation [aria-current="page"]').click()
    playwright.expect(page.locator('#mobile-navigation')).not_to_be_visible()
    playwright.expect(page.locator('#fleet-view')).to_be_visible()
