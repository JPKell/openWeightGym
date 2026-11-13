"""The selected run briefing reads real stored evidence and preserves authorization."""

import csv

import pytest

from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in  # noqa: F401


@pytest.fixture
def recorded_runs(desk, tmp_path):
    server, _, _ = desk
    models = Models(str(tmp_path / 'loadcoach.db'))
    models.put('briefing-model', {'flags': {'model': str(tmp_path / 'briefing.gguf'), 'ctx-size': 32768}})
    store = server.app.state.benchmarks.state.store
    plan = {'cases': [{'id': f'extract-{i}', 'expected': {'total': i}, 'context': 4096} for i in range(1, 7)]}

    def create(baseline=False, state='completed', empty=False):
        run = store.create('jordan', 'briefing-model', 'extraction', models.identity('briefing-model'), location='atlas')
        store.activate(run, {'fingerprint': 'briefing-recorded-fp',
            'subject': {'settings': models.get('briefing-model'), 'values': {}},
            'environment': {'engine': 'llama.cpp', 'engine_hash': 'abcdef123456', 'machine': {'host': 'atlas.local'}}})
        store.plan(run, plan)
        if not empty:
            for i in range(1, 7):
                unsupported, unavailable = i == 3 and not baseline, i == 4 and not baseline
                passed = i != 1 if baseline else i not in (2, 3, 4, 6)
                values = dict(context_tokens=4096, input_tokens=512 if i <= 3 else 2048,
                    target_input_tokens=512 if i <= 3 else 2048, category='invoices',
                    output_tokens=16, success=not (unsupported or unavailable),
                    correct=4 if passed else 3, checks=4, passed=passed, load_seconds=.284,
                    request_seconds=.512, ttft_seconds=.028, generation_tokens_per_second=61.5,
                    peak_gpu_mib=None, device='cpu')
                if unsupported:
                    values.update(unsupported=True, correct=0, checks=0, error='Unsupported native mode')
                elif unavailable:
                    values.update(correct=0, error='Connection closed before a response')
                elif i == 5 and not baseline:
                    values.pop('passed')
                evidence = {'case': plan['cases'][i - 1], 'answer': '{"total": 99}' if not passed else '{"total": 1}',
                    'prompt': 'Exact <script>window.unsafe = true</script> prompt', 'prompt_tokens_sha256': 'sample-token-digest',
                    'checks': [{'field': 'total', 'passed': passed, 'expected': i, 'actual': i if passed else 99}],
                    'resource': {'device': 'cpu', 'peak_ram_mib': 0, 'rss_mib': 0}}
                store.keep(run, f'extract-{i}', values, 'complete raw output', 'retained server log', evidence)
        store.finish(run, state); store.synced(run)
        return run

    baseline, current = create(baseline=True), create()
    failed, empty = create(state='failed'), create(empty=True)
    sign_in(server, 'jordan')
    return server, current, baseline, failed, empty


def test_briefing_filters_and_evidence_are_readable_on_desktop_and_mobile(recorded_runs, tmp_path):
    server, current, _, _, _ = recorded_runs
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/run/' + current)
        playwright.expect(page.get_by_role('heading', name='briefing-model', exact=True)).to_be_visible()
        playwright.expect(page.locator('#visible-count')).to_have_text('6 of 6 samples')
        page.locator('[data-inspect="fail"]').click()
        playwright.expect(page.locator('#visible-count')).to_have_text('2 of 6 samples')
        page.locator('a[href="#sample-2"]').click()
        page.get_by_role('button', name='Checks and answer key', exact=True).click()
        playwright.expect(page.locator('#sample-2 [data-actual]')).to_have_text('99')
        playwright.expect(page.locator('#sample-2 [data-result]')).to_have_text('Fail')
        page.get_by_role('button', name='Exact rendered input', exact=True).click()
        playwright.expect(page.locator('#sample-2')).to_contain_text('Exact <script>window.unsafe = true</script> prompt')
        assert page.evaluate('window.unsafe') is None
        page.get_by_role('button', name='Next filtered sample').click()
        playwright.expect(page.locator('[data-selected-label]')).to_contain_text('extract-6')
        page.locator('#sample-search').fill('no matching case')
        playwright.expect(page.locator('[data-evidence-empty]')).to_be_visible()
        page.locator('#clear-filters').click()
        page.locator('[data-inspect="unscored"]').click()
        playwright.expect(page.locator('#visible-count')).to_have_text('2 of 6 samples')
        playwright.expect(page.locator('#sample-3')).to_contain_text('Unsupported native mode')
        page.locator('#clear-filters').click()
        for appearance in ('light', 'dark'):
            page.emulate_media(color_scheme=appearance)
            for width in (1440, 768, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (appearance, width)
                playwright.expect(page.locator('#summary')).to_be_visible()
                playwright.expect(page.locator('#run-passport')).to_be_visible()
                assert page.locator('[data-budget-chart] circle').count() == 2
                assert page.locator('#benchmark-regressions').evaluate('''card => {
                    const box = card.getBoundingClientRect();
                    return [...card.querySelectorAll('select, button')].every(control => {
                        const rect = control.getBoundingClientRect();
                        return rect.left >= box.left && rect.right <= box.right;
                    });
                }'''), (appearance, width)
                if width in (1440, 390):
                    page.screenshot(path=str(tmp_path / f'briefing-{appearance}-{width}.png'), full_page=True)
        assert not errors
        browser.close()


def test_real_baseline_comparison_and_exports_keep_scoring_and_precision(recorded_runs, tmp_path):
    server, current, baseline, _, _ = recorded_runs
    saved = server.app.state.benchmarks.state.store.get(current, detail=True)
    exported = server.get(f'/benchmarks/run/{current}?export=json')
    assert exported.status_code == 200 and 'attachment' in exported.headers['content-disposition']
    assert exported.json()['score'] == saved['score']
    assert exported.json()['samples'][0]['evidence']['prompt'].startswith('Exact <script>')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/run/' + current)
        page.locator('#case-baseline').select_option(baseline)
        page.get_by_role('button', name='Compare cases', exact=True).click()
        playwright.expect(page.get_by_role('table', name='Case regression comparison')).to_contain_text('new failure')
        page.locator('#outcome').select_option('regression')
        playwright.expect(page.locator('#visible-count')).to_have_text('3 of 6 samples')
        page.locator('[data-columns="memory"]').click()
        playwright.expect(page.locator('#sample-table thead')).to_contain_text('CPU peak RAM')
        with page.expect_download() as downloaded:
            page.locator('#export-samples').click()
        path = tmp_path / 'filtered.csv'
        downloaded.value.save_as(path)
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        assert {row['id'] for row in rows} == {'extract-2', 'extract-5', 'extract-6'}
        assert {row['load_seconds'] for row in rows} == {'0.284'}
        assert {row['peak_ram_mib'] for row in rows} == {'0'}
        assert not errors
        browser.close()


def test_partial_empty_and_direct_sample_links_keep_evidence_and_no_final_score(recorded_runs):
    server, current, _, failed, empty = recorded_runs
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto(f'http://console.test/benchmarks/run/{current}#sample-6')
        playwright.expect(page.locator('#sample-6')).to_be_visible()
        playwright.expect(page.locator('[data-selected-label]')).to_contain_text('extract-6')
        page.goto('http://console.test/benchmarks/run/' + failed)
        playwright.expect(page.locator('#run-score')).to_have_text('—')
        playwright.expect(page.locator('#summary')).to_contain_text('Partial evidence')
        page.goto('http://console.test/benchmarks/run/' + empty)
        playwright.expect(page.locator('#sample-table')).to_contain_text('No samples recorded')
        playwright.expect(page.locator('[data-evidence-empty]')).to_be_visible()
        playwright.expect(page.locator('#export-samples')).to_be_disabled()
        browser.close()
    server.post('/sign-out')
    assert server.get(f'/benchmarks/run/{current}?export=json', follow_redirects=False).status_code in (303, 401, 403)


def test_run_export_follows_model_authorization(recorded_runs, desk):
    server, current, _, _, _ = recorded_runs
    _, _, users = desk
    users.change('pat', own={'models': ['another-model']})
    sign_in(server, 'pat')
    assert server.get(f'/benchmarks/run/{current}?export=json').status_code == 404


def test_native_sample_disclosures_work_without_the_enhancement(recorded_runs):
    server, current, _, _, _ = recorded_runs
    def block_enhancement(route, path):
        if path.startswith('/static/benchmark-run.js'):
            route.fulfill(content_type='application/javascript', body='')
            return True
        return False
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=block_enhancement)
        page.goto('http://console.test/benchmarks/run/' + current)
        playwright.expect(page.locator('#sample-table [data-sample-row]')).to_have_count(6)
        page.locator('a[href="#sample-6"]').click()
        page.locator('#sample-6 .sample-section > summary').filter(has_text='Exact rendered input').click()
        playwright.expect(page.locator('#sample-6')).to_contain_text('sample-token-digest')
        browser.close()
