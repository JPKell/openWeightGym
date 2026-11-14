"""Results proposals retain all quality evidence and honest task-specific comparisons."""

import json
import runpy
from pathlib import Path

import pytest

from benchmarks import BENCHMARKS

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-benchmark-results-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not requests, 'Design previews must work offline without contacting model services.'
        browser.close()


def cell(page, model, benchmark):
    return page.locator(f'#benchmark-score-table [data-result-model="{model}"][data-result-bench="{benchmark}"]')


def test_registry_contract_and_complete_llm_heatmap(preview):
    page = preview
    data = json.loads(page.locator('#results-data').text_content())
    actual = {b['id']: b for b in BENCHMARKS if b.get('quality') and not b.get('embedding')}
    assert {b['id'] for b in data['benchmarks']} == actual.keys()
    for b in data['benchmarks']:
        for field in ('name', 'kpis', 'formula', 'version', 'method'):
            # JSON serializes the registry's KPI tuples as lists.
            assert b[field] == json.loads(json.dumps(actual[b['id']][field]))
    assert len(data['models']) == 12 and all(m['kind'] == 'llm' for m in data['models'])
    expect(page.locator('#benchmark-score-table tbody tr')).to_have_count(12)
    expect(page.locator('#benchmark-score-table .score-cell')).to_have_count(12 * len(actual))
    expect(page.locator('#kpi-coverage')).to_have_text('89%')
    expect(cell(page, 'Qwen3-4B', 'tool_recovery')).to_have_text('0.0')
    assert 'score-missing' not in cell(page, 'Qwen3-4B', 'tool_recovery').get_attribute('class')
    expect(cell(page, 'Gemma-3-12B', 'tool_recovery')).to_contain_text('Unsupported')
    expect(cell(page, 'Qwen3-4B', 'retrieval')).to_contain_text('Partial')


def chart(page):
    return page.locator('#benchmark-heatmap').evaluate("""el => {
        const option = echarts.getInstanceByDom(el).getOption();
        return {models: option.yAxis[0].data, benchmarks: option.xAxis[0].data,
                points: option.series[0].data.map(p => [p.sourceRow, p.value[0], p.score]),
                height: el.clientHeight};
    }""")


def click_chart_cell(page, model, benchmark):
    point = page.locator('#benchmark-heatmap').evaluate("""(el, wanted) => {
        const chart = echarts.getInstanceByDom(el), option = chart.getOption();
        const y = option.yAxis[0].data.indexOf(wanted.model);
        const x = option.xAxis[0].data.indexOf(wanted.benchmark);
        const pixel = chart.convertToPixel({seriesIndex: 0}, [x, y]);
        const rect = el.getBoundingClientRect();
        return {x: rect.x + pixel[0], y: rect.y + pixel[1]};
    }""", {'model': model, 'benchmark': benchmark})
    page.mouse.click(point['x'], point['y'])


def test_existing_chart_stays_complete_when_measurements_are_filtered(preview):
    page = preview
    page.locator('[data-design="briefing"]').click()
    initial = chart(page)
    assert initial['height'] == 12 * 26 + 85
    assert len(initial['models']) == 12 and len(initial['benchmarks']) == 13
    assert len(initial['points']) == 156
    page.locator('#search-models').fill('qwen')
    expect(page.locator('[data-measurement="json_schema"] tbody tr:visible')).to_have_count(6)
    page.locator('#evidence-filter').select_option('review')
    page.locator('#benchmark').select_option('reasoning')
    page.locator('#baseline').select_option('Llama-3.3-70B-Instruct')
    for layout in ('briefing', 'atlas', 'explorer'):
        page.locator(f'[data-design="{layout}"]').click()
        assert chart(page) == initial
        heatmap = page.locator('#heatmap-panel').bounding_box()
        filters = page.locator('.results-filters').bounding_box()
        assert filters['y'] >= heatmap['y'] + heatmap['height']
    page.locator('#search-models').fill('does not exist')
    assert chart(page) == initial
    expect(page.locator('#ranking-empty')).to_be_visible()
    expect(page.locator('#heatmap-empty')).not_to_be_visible()


def test_all_metrics_are_populated_grouped_and_displayed_by_default(preview):
    page = preview
    data = json.loads(page.locator('#results-data').text_content())
    definition = next(b for b in data['benchmarks'] if b['id'] == 'json_schema')
    assert len(definition['kpis']) == 25
    assert [m for section in definition['metric_sections'] for m in section['metrics']] == definition['kpis']
    for model in data['models'][:-1]:
        metrics = data['latest'][model['id']]['json_schema']['metrics']
        assert all(isinstance(metrics[key], (int, float)) for key, _, _ in definition['kpis'])
        assert metrics['matched_pairs'] <= metrics['forced_available_cases']
        assert metrics['recorded_samples'] == metrics['cases'] + metrics['forced_cases']
        assert metrics['forced_value_delta'] == pytest.approx(metrics['paired_forced_value_accuracy'] - metrics['paired_free_value_accuracy'])
    expect(page.locator('#benchmark')).to_have_value('json_schema')
    expect(page.locator('[data-design="briefing"]')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('[data-measurement="json_schema"] [data-metric]:visible')).to_have_count(25)
    assert page.locator('#all-metrics').count() == 0
    page.locator('[data-scroll-metric="free_output_tokens_per_second"]').click()
    page.wait_for_function("document.querySelector('[data-measurement=\"json_schema\"] .table-responsive').scrollLeft > 1000")
    page.locator('[data-design="atlas"]').click()
    expect(page.locator('[data-ledger="json_schema"] tr[data-metric]:visible')).to_have_count(25)
    page.locator('#benchmark').select_option('tool_use')
    expect(page.locator('[data-ledger="tool_use"] tr[data-metric]:visible')).to_have_count(20)
    expect(page.locator('[data-ledger="tool_use"]')).to_contain_text('Calls without extra arguments')
    page.locator('[data-design="explorer"]').click()
    expect(page.locator('#profile-scores tr')).to_have_count(20)
    page.locator('#benchmark').select_option('hierarchy')
    expect(page.locator('#profile-scores')).to_contain_text('Poisoned History checks correct')


def test_chart_sorting_clicks_and_protocol_comparisons(preview):
    page = preview
    click_chart_cell(page, 'Qwen3-4B', 'Tool recovery')
    expect(page.locator('#evidence-dialog')).to_be_visible()
    expect(page.locator('#evidence-score')).to_have_text('0.0%')
    expect(page.locator('#evidence-delta')).to_have_text('-66.7 pp')
    page.keyboard.press('Escape')
    click_chart_cell(page, 'Gemma-3-27B', 'Multi-step reasoning')
    expect(page.locator('#evidence-status')).to_have_text('Older protocol')
    expect(page.locator('#evidence-delta')).to_have_text('—')
    page.locator('#close-evidence').click()
    # The chart's retained keyboard links sort the real chart and focus the measurements.
    heading = page.locator('#benchmark-score-table th[data-bench="reasoning"] a')
    heading.focus()
    page.keyboard.press('Enter')
    expect(page.locator('#benchmark')).to_have_value('reasoning')
    assert chart(page)['models'][0] == 'DeepSeek-R1-Distill-Qwen-14B'
    assert 'Gemma-3-27B' not in page.locator('#ranking').inner_text()
    # Repeated column sorting must keep each score attached to its original model.
    heading = page.locator('#benchmark-score-table th[data-bench="extraction"] a')
    heading.focus()
    page.keyboard.press('Enter')
    assert chart(page)['models'][0] == 'Qwen3-32B'
    page.locator('#benchmark-score-table').evaluate('el=>el.querySelector("a").blur()')
    page.locator('#heatmap-panel').scroll_into_view_if_needed()
    click_chart_cell(page, 'Qwen3-30B-A3B', 'Information extraction')
    expect(page.locator('#evidence-model')).to_have_text('Qwen3-30B-A3B')
    expect(page.locator('#evidence-status')).to_have_text('Settings changed')


def test_layouts_themes_and_dataset_states_stay_within_viewport(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1600, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in ('briefing', 'atlas', 'explorer'):
                page.locator(f'[data-design="{layout}"]').click()
                expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout)
                heatmap = page.locator('#heatmap-panel').bounding_box()
                filters = page.locator('.results-filters').bounding_box()
                assert filters['y'] >= heatmap['y'] + heatmap['height']
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
                    return ids.length === new Set(ids).size;
                }''')
    for scenario in ('empty', 'unmeasured', 'normal'):
        page.locator('#scenario').select_option(scenario)
        result = chart(page)
        if scenario == 'empty':
            assert not result['models'] and not result['points']
            expect(page.locator('#heatmap-empty')).to_be_visible()
        elif scenario == 'unmeasured':
            assert len(result['models']) == 12 and len(result['points']) == 156
            assert all(point[2] == 'Not measured' for point in result['points'])
        else:
            assert len(result['models']) == 12 and len(result['points']) == 156
    page.locator('#theme').click()
    assert page.locator('#benchmark-heatmap').evaluate("""el => {
        const option = echarts.getInstanceByDom(el).getOption();
        return option.yAxis[0].axisLabel.color === getComputedStyle(document.documentElement).getPropertyValue('--bs-body-color').trim();
    }""")


def test_keyboard_measurement_sorting_and_selected_model_metrics(preview):
    page = preview
    page.locator('[data-design="briefing"]').click()
    score = page.locator('[data-measurement="json_schema"] .metric-columns th').nth(1)
    score.focus()
    page.keyboard.press('Enter')
    page.keyboard.press('Enter')
    values = page.locator('[data-measurement="json_schema"] tbody tr').evaluate_all('(rows) => rows.map(r => r.cells[1].dataset.value).filter(v => v !== "").map(Number)')
    assert values == sorted(values)
    page.locator('#search-models').fill('qwen')
    assert page.locator('#export').count() == 0
    page.locator('[data-design="explorer"]').click()
    page.locator('#model-choices [data-focus-model="Qwen3-4B"]').click()
    expect(page.locator('#profile-title')).to_have_text('Qwen3-4B')
    expect(page.locator('#profile-scores tr')).to_have_count(25)
