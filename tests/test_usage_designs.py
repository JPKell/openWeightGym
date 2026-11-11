"""Exercise the three Usage dashboard proposals with sample data only: every live field kept, every layout usable."""

import re
import runpy
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
PREVIEW = runpy.run_path(str(ROOT / 'scripts/preview-usage-designs.py'))
DESIGNS = ('briefing', 'analyst', 'spend')
SCENARIOS = ('everyone', 'mine', 'empty')

# What a page must carry to be the Usage page: its IDs, form field names, chart views and totals.
INVENTORY = '''() => ({
  ids: [...document.querySelectorAll('#workspace [id]')].map(el => el.id),
  names: [...document.querySelectorAll('#workspace [name]')].map(el => el.getAttribute('name')),
  views: [...document.querySelectorAll('#workspace [data-usage-view]')].map(el => el.dataset.usageView),
  totals: [...new Set([...document.querySelectorAll('#workspace [data-usage-total]')].map(el => el.dataset.usageTotal))],
  links: [...document.querySelectorAll('#workspace a[href]')].map(el => el.getAttribute('href')),
})'''


def live_inventory(report: dict) -> dict:
    """The same inventory of the live fragment, rendered from the same sample report."""
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters.update(count=PREVIEW['display'].count)
    html = pages.get_template('usage_content.html').render(report=report)
    attribute = lambda name: re.findall(rf'\s{name}="([^"]*)"', html)  # noqa: E731
    return {'ids': set(attribute('id')) - {'usage-report'}, 'names': set(attribute('name')), 'views': set(attribute('data-usage-view')),
            'totals': set(attribute('data-usage-total')), 'links': set(attribute('href'))}


@pytest.fixture(scope='module')
def preview(tmp_path_factory):
    output = tmp_path_factory.mktemp('usage-designs') / 'index.html'
    output.write_text(PREVIEW['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        yield browser, output.as_uri()
        browser.close()


@pytest.fixture
def page(preview):
    browser, address = preview
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors, network = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
    page.goto(address)
    yield page
    assert not errors
    assert not network, 'Preview actions must not contact live APIs.'
    page.close()


def choose(page, design, scenario='everyone'):
    page.locator('#scenario').select_option(scenario)
    page.locator(f'.usage-design-tabs [data-design="{design}"]').click()
    expect(page.locator(f'.usage-design-tabs [data-design="{design}"]')).to_have_attribute('aria-pressed', 'true')


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_every_proposal_keeps_every_field_of_the_live_page(page, scenario):
    live = live_inventory(PREVIEW['SCENARIOS'][scenario][1]())
    for design in DESIGNS:
        choose(page, design, scenario)
        found = page.evaluate(INVENTORY)
        assert len(found['ids']) == len(set(found['ids'])), (design, 'duplicate IDs')
        for key, wanted in live.items():
            assert wanted <= set(found[key]), (design, scenario, key, wanted - set(found[key]))
        # The heading leads straight into the dashboard: nothing but the heading above the filters.
        header = page.locator('#usage-header')
        filters = page.locator('#usage-filters, #usage-filter-drawer').first
        assert filters.bounding_box()['y'] - (header.bounding_box()['y'] + header.bounding_box()['height']) < 40, design


def test_layouts_fit_every_width_and_theme(page):
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1440, 1024, 390, 320):
            page.set_viewport_size({'width': width, 'height': 900})
            for design in DESIGNS:
                for scenario in SCENARIOS:
                    choose(page, design, scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, design, scenario)
                    expect(page.locator('#usage-chart svg')).to_have_count(1)


def test_theme_change_recolours_the_live_chart(page):
    colour = "() => echarts.getInstanceByDom(document.getElementById('usage-chart')).getOption().xAxis[0].axisLabel.color"
    before = page.evaluate(colour)
    page.locator('#theme').click()
    expect(page.locator('html')).to_have_attribute('data-bs-theme', 'dark')
    assert page.evaluate(colour) != before


def test_mock_rates_move_the_cost_figures_and_their_shares(page):
    for design in DESIGNS:
        choose(page, design)
        fallback = page.locator('[data-usage-total="fallback_cost"]').first
        share = page.locator('[data-share="fallback_cost"]').first
        before, said = fallback.inner_text(), share.inner_text()
        page.locator('#usage-input-rate').fill('6')
        expect(fallback).not_to_have_text(before)
        expect(share).not_to_have_text(said)
        segments = page.locator('[data-composition]').first.locator('.usage-segment')
        expect(segments).to_have_count(3)


def test_chart_views_breakdown_and_spend_leaders(page):
    choose(page, 'spend')
    expect(page.locator('#usage-leaders-heading')).to_have_text('Top models')
    expect(page.locator('[data-ranked] li')).to_have_count(6)
    page.locator('[data-usage-view="user"]').click()
    expect(page.locator('#usage-chart-heading')).to_have_text('Tokens by user')
    expect(page.locator('#usage-leaders-heading')).to_have_text('Top users')
    expect(page.locator('#usage-stack-control')).to_be_visible()
    page.locator('#usage-metric').select_option('cost')
    expect(page.locator('[data-ranked] li').first).to_contain_text('$')
    expect(page.locator('[data-outcome-mix] li')).to_have_count(5)
    choose(page, 'briefing')
    page.locator('#usage-group-by').select_option('user')
    expect(page.locator('#usage-range-heading')).to_have_text('Selected range by user')
    expect(page.locator('#usage-breakdown tbody .usage-share')).to_have_count(6)


def test_scenarios_and_preview_only_actions(page):
    choose(page, 'analyst', 'mine')
    expect(page.locator('#usage-user')).to_have_count(0)
    expect(page.locator('#workspace')).to_contain_text('Showing your usage only.')
    expect(page.locator('[data-filter-count]')).to_have_text('None')
    choose(page, 'spend', 'empty')
    expect(page.locator('.usage-empty')).to_have_text('No recorded requests match these filters.')
    expect(page.locator('#usage-breakdown tbody [data-empty-row]')).to_have_text('No usage in this range.')
    expect(page.locator('[data-filter-summary]')).to_have_text('Model: retired-model')
    expect(page.locator('[data-filter-count]')).to_have_text('1 on')
    page.locator('[data-toggle-filters]').click()
    expect(page.locator('#usage-filter-drawer')).to_have_attribute('open', '')
    for field in ('#usage-start', '#usage-end', '#usage-group', '#usage-model'):  # nothing squeezed out of sight in the opened drawer
        assert page.locator(field).evaluate('e => e.scrollWidth <= e.clientWidth + 1 && e.clientWidth >= 96'), field
    expect(page.locator('[data-toggle-filters]')).to_have_attribute('aria-expanded', 'true')
    page.get_by_role('button', name='Apply').click()
    expect(page.locator('#preview-status')).to_contain_text('No request was sent')
    page.get_by_role('link', name='Reset').click()
    expect(page.locator('#preview-status')).to_contain_text('Reset clears every filter')
    assert page.url.endswith('index.html#spend')
