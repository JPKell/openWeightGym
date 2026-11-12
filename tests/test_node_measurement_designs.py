"""Sample measurement proposals preserve node coverage and per-location evidence."""

import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
NODES = ['atlas', 'orion', 'nova', 'cedar', 'front-cpu', 'vega', 'lyra', 'scout']


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-node-measurement-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Portable proposals must not contact live services.'
        browser.close()


def test_all_nodes_survive_layouts_types_states_and_narrow_screens(preview):
    page = preview
    for width in (1600, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1100})
        for design in ('ledger', 'matrix', 'lens'):
            page.locator(f'[data-design="{design}"]').click()
            expect(page.locator(f'[data-design="{design}"]')).to_have_attribute('aria-pressed', 'true')
            for kind in ('llm', 'embedding', 'image', 'audio'):
                page.locator(f'[data-kind="{kind}"]').click()
                for state in ('normal', 'unmeasured', 'empty', 'offline'):
                    page.locator('#scenario').select_option(state)
                    assert page.locator('#node-list [data-node]').evaluate_all('(els) => els.map(el => el.dataset.node)') == NODES
                    assert all(page.locator(f'#node-list [data-node="{node}"]').is_visible() for node in NODES)
                    expect(page.locator('#measurement-table-panel')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, design, kind, state)
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                        return ids.length === new Set(ids).size;
                    }''')
    page.locator('#scenario').select_option('normal')
    page.locator('[data-design="matrix"]').click()
    page.locator('[data-kind="llm"]').click()
    page.locator('#theme').click()
    expect(page.locator('html')).to_have_attribute('data-bs-theme', 'dark')
    dark = page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor')
    for design in ('ledger', 'matrix', 'lens'):
        page.locator(f'[data-design="{design}"]').click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#theme').click()
    assert page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor') != dark


def test_full_inventory_search_sorting_and_model_node_drilldown(preview):
    page = preview
    expect(page.locator('#measurement-rows [data-model-row]')).to_have_count(30)
    page.locator('.measurement-table-scroll').evaluate('(el) => el.scrollTop = el.scrollHeight')
    assert page.locator('.measurement-table-scroll').evaluate('(el) => el.scrollTop > 0')
    page.locator('#sort').select_option('best')
    rows = page.locator('#measurement-rows tr')
    values = rows.locator('td[data-metric="context"][data-state="current"] [data-value]').all_text_contents()
    numbers = [int(value.replace(',', '')) for value in values]
    assert numbers == sorted(numbers, reverse=True)
    page.locator('#model-search').fill('Qwen3 8B')
    expect(page.locator('#measurement-rows [data-model-row]')).to_have_count(1)
    page.locator('[data-subject]').click()
    expect(page.locator('#workspace')).to_have_attribute('data-layout', 'lens')
    expect(page.locator('#lens-model-name')).to_contain_text('Qwen3 8B')
    expect(page.locator('#measurement-rows [data-node-row]')).to_have_count(8)
    expect(page.locator('[data-node-row="scout"]')).to_contain_text('Not on node')
    page.locator('#node-list [data-node="scout"]').click()
    expect(page.locator('#measurement-rows [data-empty-row]')).to_contain_text('No LLM models on scout')
    page.locator('#node-list [data-node="vega"]').click()
    expect(page.locator('#context-note')).to_contain_text('Unreachable · saved evidence retained')
    expect(page.locator('#measurement-rows [data-model-row]')).to_have_count(1)
    page.locator('#model-search').fill('nothing matches this')
    expect(page.locator('[data-empty-row]')).to_contain_text('No matching models')


def test_measurements_scores_failed_and_changed_evidence_stay_distinct(preview):
    page = preview
    page.locator('[data-design="matrix"]').click()
    expect(page.locator('#measurement-head th')).to_have_count(9)
    # Even a node with no models retains a real, labelled column.
    expect(page.locator('#measurement-head')).to_contain_text('scout')
    page.locator('[data-metric="load"]').first.click()
    page.locator('#model-search').fill('Qwen3 8B')
    value = page.locator('#measurement-rows td').first.locator('[data-value]')
    seconds = float(value.inner_text())
    page.locator('#display').select_option('scores')
    assert abs(float(value.inner_text()) - 1 / seconds) < .0001
    page.locator('#measurement-rows [data-evidence]').first.click()
    expect(page.locator('#evidence-body')).to_contain_text('1 ÷ load_seconds')
    expect(page.locator('#evidence-body')).to_contain_text('llm-00-0-load')
    expect(page.locator('#evidence-body')).to_contain_text('Their configurations can differ')
    page.keyboard.press('Escape')
    expect(page.locator('#evidence-dialog')).not_to_be_visible()
    page.locator('#model-search').fill('')
    page.locator('#evidence-filter').select_option('failed')
    page.locator('#measurement-rows td[data-state="failed"] [data-evidence]').first.click()
    expect(page.locator('#evidence-body')).to_contain_text('No score')
    expect(page.locator('#evidence-body')).to_contain_text('never treated as zero')
    page.locator('#close-evidence').click()
    page.locator('#evidence-filter').select_option('stale')
    page.locator('#measurement-rows td[data-state="stale"] [data-evidence]').first.click()
    expect(page.locator('#evidence-body')).to_contain_text('Settings changed')
    expect(page.locator('#evidence-body')).to_contain_text('recorded configuration')
    page.locator('#close-evidence').click()
    page.locator('#evidence-filter').select_option('all')
    page.locator('#scenario').select_option('unmeasured')
    expect(page.locator('#measurement-rows [data-evidence]')).to_have_count(0)
    expect(page.locator('#measurement-rows')).to_contain_text('Not run')
    expect(page.locator('#measurement-rows')).to_contain_text('Not on node')


def test_type_specific_protocols_and_offline_evidence(preview):
    page = preview
    page.locator('[data-kind="embedding"]').click()
    page.locator('[data-metric="speed"]').first.click()
    expect(page.locator('#metric-note')).to_contain_text('512 input tokens')
    page.locator('#measurement-rows td[data-metric="speed"][data-state="current"] [data-evidence]').first.click()
    expect(page.locator('#evidence-body')).to_contain_text('embedding_tokens_per_second')
    expect(page.locator('#evidence-body')).to_contain_text('512 input tokens')
    page.locator('#close-evidence').click()
    for kind in ('image', 'audio'):
        page.locator(f'[data-kind="{kind}"]').click()
        expect(page.locator('#measurement-head th')).to_have_count(2)
        expect(page.locator('button[data-metric="context"]')).not_to_be_visible()
        expect(page.locator('button[data-metric="speed"]')).not_to_be_visible()
    page.locator('[data-design="matrix"]').click()
    page.locator('#model-search').fill('Kokoro')
    page.locator('#scenario').select_option('offline')
    expect(page.locator('#connection-note')).to_be_visible()
    assert page.locator('#measurement-rows [data-evidence]').count() > 0


def test_sample_capacity_and_individual_run_identity(preview):
    data = json.loads(preview.locator('#measurement-data').text_content())
    counts = [sum(node['id'] in model['placements'] for model in data['models'] if model['kind'] == 'llm') for node in data['nodes']]
    assert counts == [30, 27, 24, 21, 15, 18, 20, 0]
    runs = [test['run'] for record in data['results'].values() for test in record['measurements'].values() if test['run']]
    assert len(runs) == len(set(runs))
    assert all(not test['run'] and not test['at'] for record in data['results'].values()
               for test in record['measurements'].values() if test['state'] == 'missing')
