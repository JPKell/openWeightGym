"""Plugin logs proposals preserve metadata and make filtering, inspection and export usable."""

import csv
import io
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-plugin-log-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Previews must not contact live services.'
        browser.close()


def test_filters_sort_pagination_and_cross_layout_state(preview):
    page = preview
    expect(page.locator('#total-count')).to_have_text('72')
    expect(page.locator('#log-rows tr')).to_have_count(10)
    page.locator('#next').click()
    expect(page.locator('#page-summary')).to_contain_text('11–20 of 72')
    page.locator('#outcome-filter').select_option('failed')
    expect(page.locator('#total-count')).to_have_text('7')
    expect(page.locator('#failed-count')).to_have_text('7')
    expect(page.locator('#completed-count')).to_have_text('0')
    page.locator('[data-design="ledger"]').click()
    expect(page.locator('#outcome-filter')).to_have_value('failed')
    page.locator('[data-instance-choice]').filter(has=page.locator('[data-instance-name]', has_text='research')).click()
    expect(page.locator('#instance-filter')).to_have_value('research')
    page.locator('#clear').click()
    page.locator('[data-sort="user"]').click()
    expect(page.locator('#log-rows [data-user]').first).to_have_text('alex')
    expect(page.locator('[data-sort-column="user"]')).to_have_attribute('aria-sort', 'ascending')
    page.locator('#search').fill('no-match-for-this')
    expect(page.locator('#log-rows')).to_contain_text('No requests match')
    expect(page.locator('#export')).to_be_disabled()
    page.locator('#clear').click()
    page.locator('#period').select_option('24')
    expect(page.locator('#total-count')).to_have_text('9')
    page.locator('#user-filter').select_option('pat')
    expect(page.locator('#total-count')).to_have_text('3')


def test_inspect_follow_session_and_missing_identifiers(preview):
    page = preview
    page.locator('#log-rows [data-open]').first.click()
    expect(page.locator('[data-fact="request"]')).to_have_text('req-2084-8b32-4d91-bf63')
    expect(page.locator('[data-fact="at"]')).to_contain_text('14:27:00')
    page.locator('#follow-session').click()
    expect(page.locator('#trail-title')).to_have_text('sess-0001-c38e-a4f6')
    expect(page.locator('#trail-records li')).to_have_count(3)
    first = page.locator('#trail-records [data-request]').first.inner_text()
    page.locator('#trail-order').select_option('desc')
    assert page.locator('#trail-records [data-request]').first.inner_text() != first
    page.locator('#trail-records [data-open]').first.click()
    expect(page.locator('#detail-dialog')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('#detail-dialog')).not_to_be_visible()
    page.locator('#search').fill('2013')
    expect(page.locator('#trail-title')).to_have_text('Request without a session ID')
    page.locator('#trail-records [data-open]').click()
    expect(page.locator('[data-fact="session"]')).to_have_text('Not recorded')
    expect(page.locator('[data-fact="request"]')).to_have_text('Not recorded')
    expect(page.locator('#follow-session')).to_be_disabled()


def test_export_includes_filtered_records_across_all_pages(preview):
    page = preview
    page.locator('#instance-filter').select_option('jeeves')
    expect(page.locator('#total-count')).to_have_text('24')
    with page.expect_download() as downloaded:
        page.locator('#export').click()
    rows = list(csv.DictReader(io.StringIO(Path(downloaded.value.path()).read_text())))
    assert len(rows) == 24 and all(row['instance'] == 'jeeves' for row in rows)
    assert rows[0]['request'] == 'req-2084-8b32-4d91-bf63'
    assert rows[0]['at'] == '2026-11-13T21:27:00.000Z'
    assert not {'prompt', 'answer', 'tokens', 'latency'} & rows[0].keys()


def test_layouts_themes_empty_error_and_phone(preview):
    page = preview
    for width in (1600, 1024, 768, 390):
        page.set_viewport_size({'width':width,'height':1000})
        for theme in ('light','dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for layout in ('desk','trail','ledger'):
                page.locator(f'[data-design="{layout}"]').click()
                for scenario in ('normal','empty','error'):
                    page.locator('#scenario').select_option(scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width,theme,layout,scenario)
                    if scenario == 'error':
                        expect(page.locator('#load-error')).to_be_visible()
                        expect(page.locator('#total-count')).to_have_text('—')
                        expect(page.locator('#log-body')).not_to_be_visible()
                    else:
                        expect(page.locator('#load-error')).not_to_be_visible()
                page.locator('#retry').click()
                expect(page.locator('#total-count')).to_have_text('72')
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(el=>el.id);
                    return ids.length === new Set(ids).size;
                }''')
                assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                    .filter(el=>el.getBoundingClientRect().width && !el.closest('[hidden]'))
                    .every(el=>el.labels?.length || el.getAttribute('aria-label'))''')
                if width == 390:
                    page.locator('#trail-records [data-open]' if layout=='trail' else '#log-rows [data-open]').first.click()
                    expect(page.locator('#detail-dialog')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.keyboard.press('Escape')
