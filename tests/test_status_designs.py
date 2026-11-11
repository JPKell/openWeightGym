"""Status design options keep actions isolated while handling variable fleets."""
import runpy
from pathlib import Path
import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-operations-designs.py'))['render']
    output = tmp_path / 'status.html'
    output.write_text(render(status=True))
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page(viewport={'width':1600,'height':1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Previews must not contact live services.'
        browser.close()


def test_layouts_keep_requests_above_capacity_and_real_sidebar(preview):
    page = preview
    playwright.expect(page.locator('#site-sidebar')).to_be_visible()
    assert page.locator('[data-kpi="running"]').inner_text() == '5+'
    assert page.locator('[data-kpi="queued"]').inner_text() == '3+'
    for design in ('focus','ledger','lanes'):
        page.locator('button[data-design="' + design + '"]').click()
        requests = page.locator('#requests-section').bounding_box()
        capacity = page.locator('#capacity-section').bounding_box()
        assert requests['y'] + requests['height'] <= capacity['y']
        assert page.locator('#node-navigator').is_visible() == (design == 'focus')
        assert page.locator('#request-lanes').is_visible() == (design == 'lanes')
        assert 'Request history' not in page.locator('#sd-workspace').inner_text()
        for width in (1600, 1000, 390):
            page.set_viewport_size({'width':width,'height':1100})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (design,width)
        page.set_viewport_size({'width':1600,'height':1100})
    assert page.evaluate('''() => { const ids = [...document.querySelectorAll('[id]')].map(e => e.id); return ids.length === new Set(ids).size; }''')


def test_scoped_controls_keep_running_work_and_confirm(preview):
    page = preview
    page.locator('#control-scope').select_option('0')
    page.locator('#pause').click()
    page.keyboard.press('Escape')
    playwright.expect(page.locator('#pause')).to_have_text('Pause intake')
    page.locator('#pause').click(); page.locator('#confirm-action').click()
    playwright.expect(page.locator('#pause')).to_have_text('Resume intake')
    assert page.locator('[data-kpi="running"]').inner_text() == '5+'
    page.locator('#clear').click(); page.locator('#confirm-action').click()
    assert page.locator('[data-kpi="queued"]').inner_text() == '2+'
    assert page.locator('[data-kpi="running"]').inner_text() == '5+'
    page.locator('#unload').click(); page.locator('#confirm-action').click()
    playwright.expect(page.locator('#focus-panel')).to_contain_text('Unload queued')
    assert page.locator('#focus-panel [data-models] tr').count() == 2
    page.locator('#scenario').select_option('idle')
    page.locator('#control-scope').select_option('0')
    page.locator('#unload').click(); page.locator('#confirm-action').click()
    playwright.expect(page.locator('#focus-panel')).to_contain_text('No models loaded.')


def test_variable_fleets_unknowns_and_filtering(preview):
    page = preview
    for count in ('0','1','12','48'):
        page.locator('#fleet-size').select_option(count)
        assert page.locator('#node-list button').count() == int(count)
    before = page.locator('.sd-kpis').inner_text()
    page.locator('#search').fill('worker-48')
    assert page.locator('#node-list button').count() == 1
    assert page.locator('.sd-kpis').inner_text() == before
    page.locator('#scenario').select_option('offline')
    assert page.locator('[data-kpi="running"]').inner_text() == '—'
    playwright.expect(page.locator('#pause')).to_be_disabled()
    playwright.expect(page.locator('#clear')).to_be_disabled()
    playwright.expect(page.locator('#unload')).to_be_disabled()
    playwright.expect(page.locator('#focus-panel')).to_contain_text('Loaded model readings unavailable.')
