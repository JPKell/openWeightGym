"""The live Results briefing uses authorised, saved evidence and complete metric tables."""

from pathlib import Path

import pytest

from benchmarks import BENCHMARKS
from loadcoach.models import Models
from weightroom.benchmark_views import metric_sections, results_workspace
from test_weightroom import desk, sign_in  # noqa: F401


def test_rankings_keep_precision_direction_protocols_and_authorised_baselines():
    definition = dict(id='custom_case', name='Custom test', version=2, green='low',
                      score_unit='points', kpis=[('accuracy', 'Correct fields', '%')])
    models = [{'id': name} for name in ('one', 'two', 'old', 'unmeasured')]
    def run(score, version=2, current=True):
        return dict(score=score, version=version, current_fingerprint=current)
    latest = {'one': {'custom_case': run(1.2345)}, 'two': {'custom_case': run(1.2346, current=False)},
              'old': {'custom_case': run(0, version=1)}, 'secret': {'custom_case': run(-1)}}
    view = results_workspace(models, models, [definition], definition, latest, {'baseline': 'two'})
    assert [row['model'] for row in view['leaders']] == ['one', 'two']
    assert view['leaders'][0]['delta'] == pytest.approx(-.0001)
    assert view['scored'] == 3 and view['total'] == 4 and view['review'] == 2
    filtered = results_workspace(models, models, [definition], definition, latest,
                                 {'q': 'one', 'evidence': 'current', 'baseline': 'secret'})
    assert filtered['baseline'] == '' and filtered['models'] == [{'id': 'one'}]
    assert filtered['scored'] == view['scored'] and filtered['model_count'] == 4
    gaps = results_workspace(models, models, [definition], definition, latest, {'evidence': 'gaps'})
    assert gaps['models'] == [{'id': 'unmeasured'}]
    latest['two']['custom_case']['score'] = 1.2345
    tied = results_workspace(models, models, [definition], definition, latest, {})
    assert [row['rank'] for row in tied['leaders']] == [1, 1]


def test_metric_sections_retain_custom_and_dense_registry_fields():
    for benchmark in BENCHMARKS:
        assert [metric for section in metric_sections(benchmark) for metric in section['metrics']] == benchmark['kpis']
    custom = dict(id='custom_case', kpis=[('a', 'A', '%'), ('b', 'B', 'count')])
    assert metric_sections(custom) == [{'name': 'Overall & coverage', 'metrics': custom['kpis']}]


@pytest.fixture
def scored_library(desk, tmp_path):
    server, _, _ = desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    store = server.app.state.benchmarks.state.store
    definition = next(b for b in store.definitions() if b['id'] == 'json_schema')
    ids = {}
    for index, name in enumerate(('alpha-model', 'beta-model', 'unmeasured-model')):
        library.put(name, {'flags': {'model': str(tmp_path / (name + '.gguf')), 'ctx-size': 32768}})
        if name == 'unmeasured-model':
            continue
        number = store.create('jordan', name, 'json_schema', library.identity(name))
        store.activate(number, {'fingerprint': 'recorded-settings',
            'subject': {'protocol': {'version': definition['version']}}, 'environment': {}})
        for mode in ('free', 'forced'):
            for case in range(3):
                correct = 5 if index == 0 or mode == 'forced' else 4
                store.keep(number, f'{mode}-{case}', dict(success=True, decoding=mode, pair=str(case),
                    input_tokens=1024 * (case+1), correct=correct, checks=5, value_checks=3,
                    value_correct=3 if index == 0 or mode == 'forced' else 2,
                    json_valid=True, schema_valid=True, passed=correct == 5, output_tokens=250,
                    request_seconds=6.25 + index, generation_capped=False), '{}', '')
        store.finish(number, 'completed'); store.synced(number)
        ids[name] = number
    sign_in(server, 'jordan')
    return server, ids


def test_live_results_remove_requested_copy_and_keep_saved_metrics(scored_library):
    server, ids = scored_library
    response = server.get('/benchmarks/results')
    assert response.status_code == 200
    html = response.text
    for removed in ('All LLMs across all quality benchmarks.', 'These controls filter the measurements below.',
                    'Every metric from the benchmark, with units and grouped headings.', 'Export CSV', 'Compare all models'):
        assert removed not in html
    assert 'class="card-heading">Measurements' not in html
    assert html.index('id="heatmap-panel"') < html.index('id="results-filters"')
    table = html.split('id="benchmark-results"', 1)[1].split('</table>', 1)[0]
    for label in ('Free valid JSON', 'Forced correct checks', 'Executed matched pairs',
                  'Matched forced minus free values', 'Free output tokens / request-second'):
        assert label in table
    assert 'data-value="40.0"' in table  # Actual 750 tokens / 18.75 request seconds.
    assert '/benchmarks/run/' + ids['alpha-model'] in table
    filtered = server.get('/benchmarks/results', params={'q': 'alpha', 'baseline': 'beta-model'})
    focused = filtered.text.split('id="benchmark-results"', 1)[1].split('</table>', 1)[0]
    heatmap = filtered.text.split('id="benchmark-score-table"', 1)[1].split('</table>', 1)[0]
    assert 'beta-model' not in focused and 'unmeasured-model' not in focused
    assert all(name in heatmap for name in ('alpha-model', 'beta-model', 'unmeasured-model'))
    assert 'Baseline: beta-model' in filtered.text


def test_live_filters_sorting_chart_and_dense_columns_in_browser(scored_library):
    engine = pytest.importorskip('playwright.sync_api')
    from test_console_browser import browser_context
    server, ids = scored_library
    with engine.sync_playwright() as playwright:
        browser, page = browser_context(playwright, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/results')
        engine.expect(page.locator('#benchmark')).to_have_value('json_schema')
        engine.expect(page.locator('#benchmark-results th[data-metric]')).to_have_count(25)
        assert page.locator('#benchmark-top .page-actions a, #benchmark-top .page-actions button').count() == 0
        page.evaluate('window.resultsChart = document.querySelector("#benchmark-heatmap")')
        original = page.locator('#benchmark-heatmap').evaluate('el=>echarts.getInstanceByDom(el).getOption().series[0].data.map(p=>p.value)')
        page.locator('#baseline').select_option('beta-model')
        engine.expect(page.locator('.leader-row.baseline-row')).to_contain_text('beta-model')
        page.locator('#search-models').fill('alpha')
        engine.expect(page.locator('#benchmark-results tbody tr')).to_have_count(1)
        assert page.evaluate('window.resultsChart === document.querySelector("#benchmark-heatmap")')
        assert page.locator('#benchmark-heatmap').evaluate('el=>echarts.getInstanceByDom(el).getOption().series[0].data.map(p=>p.value)') == original
        page.locator('#reset').click()
        engine.expect(page.locator('#benchmark-results tbody tr')).to_have_count(3)
        page.locator('[data-scroll-metric="free_output_tokens_per_second"]').click()
        page.wait_for_function('document.querySelector("#benchmark-results").parentElement.scrollLeft > 1000')
        # Sorting the second header row must use that metric's body column, not a group heading.
        header = page.locator('#benchmark-results [data-metric="free_output_tokens_per_second"]')
        header.focus(); page.keyboard.press('Enter')
        first = page.locator('#benchmark-results tbody tr').first
        engine.expect(first).to_have_attribute('data-model', 'alpha-model')
        assert header.get_attribute('data-mark') == '▼'
        chart_heading = page.locator('#benchmark-score-table th[data-bench="extraction"] a')
        chart_heading.focus(); page.keyboard.press('Enter')
        engine.expect(page.locator('#benchmark-results')).to_have_attribute('data-benchmark', 'extraction')
        assert page.evaluate('window.resultsChart === document.querySelector("#benchmark-heatmap")')
        chart_heading.evaluate('el=>el.blur()')
        page.locator('#benchmark').select_option('hierarchy')
        engine.expect(page.locator('#benchmark-results [data-metric="poisoned_history_accuracy"]')).to_have_count(1)
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            for width in (1600, 1440, 768, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1100})
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
                chart = page.locator('#heatmap-panel').bounding_box()
                filters = page.locator('#results-filters').bounding_box()
                assert filters['y'] >= chart['y'] + chart['height'], (theme, width, chart, filters)
                if width >= 1440:
                    assert filters['height'] < 90
        page.set_viewport_size({'width': 1440, 'height': 1100})
        page.locator('#benchmark').select_option('json_schema')
        engine.expect(page.locator('#benchmark-results th[data-metric]')).to_have_count(25)
        page.locator('#benchmark-results').evaluate('el=>el.parentElement.scrollLeft=0')
        page.evaluate('window.scrollTo(0, 0)')
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        images = Path('data/benchmark-previews'); images.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(images / 'results-briefing-live.png'), full_page=True)
        page.locator('#benchmark-results a[href="/benchmarks/run/' + ids['alpha-model'] + '"]').click()
        page.wait_for_url('http://console.test/benchmarks/run/' + ids['alpha-model'])
        assert not errors
        browser.close()
