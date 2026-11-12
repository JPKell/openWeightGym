"""Real-browser interactions over isolated ASGI fixtures, without GPU execution."""
from pathlib import Path
import json
import pytest
from loadcoach.models import Models
from front_helpers import serve_browser_request
from test_weightroom import desk, sign_in  # noqa: F401
from test_benchmark_pages import typed_models  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')


def test_results_type_pills_and_node_page_preserve_selected_models(desk, typed_models):
    from test_console_browser import browser_context
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/benchmarks/results?benchmark=load')
        playwright.expect(page.locator('#benchmark-score-table tbody')).to_contain_text('llm-model')
        assert 'image-model' not in page.locator('#benchmark-score-table tbody').inner_text()
        assert 'audio-model' not in page.locator('#benchmark-score-table tbody').inner_text()
        for kind, label in (('llm', 'LLM'), ('image', 'Image'), ('audio', 'Audio')):
            page.locator('nav[aria-label="Model type"]').get_by_role('link', name=label, exact=True).click()
            page.wait_for_url('http://console.test/benchmarks/results?kind=' + kind)
            if kind == 'llm':
                page.select_option('#benchmark', 'load')
            name, number = typed_models[kind]
            playwright.expect(page.locator('#benchmark-results tbody')).to_contain_text(name)
            assert page.locator('#benchmark-resource-table').count() == 0
            playwright.expect(page.locator(f'#benchmark-results a[href="/benchmarks/run/{number}"]')).to_have_count(1)
            if kind != 'llm':
                assert page.locator('#benchmark option').count() == 1
                # A real GET submission must retain the selected model type.
                page.select_option('#benchmark', 'load')
                playwright.expect(page).to_have_url('http://console.test/benchmarks/results?kind=' + kind + '&benchmark=load')
                playwright.expect(page.locator('#benchmark-results tbody')).to_contain_text(name)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.get_by_role('link', name='Node measurements', exact=True).click()
        playwright.expect(page.locator('#benchmark-resource-table')).to_be_visible()
        page.locator('nav[aria-label="Model type"]').get_by_role('link', name='Audio', exact=True).click()
        playwright.expect(page.locator('#benchmark-resource-table tbody')).to_contain_text('audio-model')
        assert page.locator('#benchmark-resource-table [data-measurement-row]').count() == 1
        page.get_by_role('link', name='Load time', exact=True).click()
        playwright.expect(page).to_have_url('http://console.test/benchmarks/node-measurements?benchmark=load&kind=audio#node-measurements')
        page.get_by_role('link', name='Runs', exact=True).click()
        page.wait_for_url('http://console.test/benchmarks/runs')
        page.select_option('#benchmark', 'load')
        playwright.expect(page.locator('#benchmark-models')).to_contain_text('llm-model')
        playwright.expect(page.locator('#benchmark-models')).to_contain_text('image-model')
        playwright.expect(page.locator('#benchmark-models')).to_contain_text('audio-model')
        assert set(page.locator('#outstanding-model option').all_text_contents()) == {'llm-model', 'image-model', 'audio-model'}
        assert not errors
        browser.close()


def test_cards_survive_navigation_oob_refresh_accounts_and_sample_anchors(desk, tmp_path):
    server, _, users = desk
    models = Models(str(tmp_path / 'loadcoach.db'))
    for name in ('one-1-3b-q4', 'two-1-3b-q6'):
        models.put(name, {'flags': {'model': str(tmp_path / (name + '.gguf'))}})
    store = server.app.state.benchmarks.state.store
    run = store.create('jordan', 'one-1-3b-q4', 'extraction', models.identity('one-1-3b-q4'))
    store.activate(run, {'fingerprint': 'historical', 'subject': {}, 'environment': {}})
    store.keep(run, 'extract-one', {'success': True, 'context_tokens': 2048, 'correct': 1, 'checks': 1,
               'passed': True, 'input_tokens': 512, 'target_input_tokens': 512, 'category': 'records'}, 'raw output', 'server log',
               {'prompt': 'exact input', 'answer': 'graded answer', 'checks': [], 'case': {'expected': 'answer key'}})
    store.finish(run, 'completed'); store.synced(run)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def serve(route):
            serve_browser_request(server, 'http://benchmark.test', route)
        context.route('http://benchmark.test/**', serve)
        page.goto('http://benchmark.test/benchmarks/runs')
        for section in ('benchmark-start', 'benchmark-active', 'benchmark-queue', 'benchmark-history'):
            playwright.expect(page.locator('#' + section)).to_be_visible()
            assert page.locator('#' + section).evaluate('(el) => el.tagName') == 'SECTION'
        assert page.locator('#outstanding-model').count() == 1
        page.locator('summary').filter(has_text='Queue every outstanding benchmark').click()
        playwright.expect(page.get_by_role('button', name='Queue outstanding benchmarks')).to_be_enabled()
        page.evaluate("window.priorHistory = document.querySelector('#benchmark-history')")
        page.wait_for_function("document.querySelector('#benchmark-history') !== window.priorHistory", timeout=7000)
        assert page.locator('#benchmark-active').count() == 1
        assert page.locator('#benchmark-queue').count() == 1
        playwright.expect(page.locator('#benchmark-history')).to_be_visible()
        page.reload()
        playwright.expect(page.locator('#benchmark-start')).to_be_visible()
        page.select_option('#benchmark', 'abstention')
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('abstention')
        page.locator('#benchmark-models input[name="run-target"]:enabled').first.check()
        playwright.expect(page.locator('[data-selected-count]')).to_have_text('1 run selected')
        page.select_option('#benchmark', 'abstention')
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('abstention')
        page.select_option('#benchmark', 'hierarchy')
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('hierarchy')
        page.select_option('#benchmark', 'reasoning')
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('reasoning')
        page.select_option('#benchmark', 'turns')
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('turns')
        page.select_option('#benchmark', 'load')
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('load')
        page.locator('#benchmark-history a[href="/benchmarks/run/' + run + '"]').click()
        playwright.expect(page).to_have_url('http://benchmark.test/benchmarks/run/' + run)
        page.locator('a[href="#sample-1"]').click()
        playwright.expect(page.locator('#sample-1')).to_have_attribute('open', '')
        headings = page.locator('#sample-1 .sample-section > summary').all_text_contents()
        assert headings == ['Exact rendered input', 'Graded final answer', 'Checks and answer key', 'Server log', 'Measurements', 'Raw output']
        assert page.locator('#sample-1 .sample-section[open]').count() == 0
        page.locator('#sample-1 .sample-section > summary').first.click()
        page.reload()
        playwright.expect(page.locator('#sample-1 .sample-section').first).to_have_attribute('open', '')
        top = page.locator('#sample-1').bounding_box()['y']
        header = page.get_by_role('banner').bounding_box()['height']
        assert top >= header
        images = Path('data/benchmark-previews'); images.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(images / 'sample.png'))
        page.goto('http://benchmark.test/benchmarks/results?benchmark=abstention')
        assert 'Present facts answered correctly' in page.locator('#benchmark-results thead').inner_text()
        assert 'Absent answers given invented values' in page.locator('#benchmark-results thead').inner_text()
        page.set_viewport_size({'width': 320, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://benchmark.test/benchmarks/results?benchmark=tables')
        assert 'CSV checks correct' in page.locator('#benchmark-results thead').inner_text()
        assert 'Dirty checks correct' in page.locator('#benchmark-results thead').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://benchmark.test/benchmarks/results?benchmark=hierarchy')
        assert 'System rules correct' in page.locator('#benchmark-results thead').inner_text()
        assert 'Tool Result checks correct' in page.locator('#benchmark-results thead').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://benchmark.test/benchmarks/results?benchmark=reasoning')
        assert 'Scheduling facts correct' in page.locator('#benchmark-results thead').inner_text()
        assert 'Cases reporting reasoning tokens' in page.locator('#benchmark-results thead').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://benchmark.test/benchmarks/results?benchmark=turns')
        assert 'System-origin checks correct' in page.locator('#benchmark-results thead').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.goto('http://benchmark.test/benchmarks/results')
        assert page.locator('#benchmark-score-table > summary').count() == 0
        assert page.locator('#benchmark-heatmap svg').count() == 1
        assert page.locator('#benchmark-resource-table').count() == 0
        page.screenshot(path=str(images / 'results.png'))
        page.get_by_role('link', name='Node measurements', exact=True).click()
        playwright.expect(page.locator('[data-measurement-matrix]')).to_be_visible()
        assert page.locator('#benchmark-resource-heatmap').count() == 0
        assert page.locator('#benchmark-resource-table [data-measurement-row]').count() == 2
        page.screenshot(path=str(images / 'node-measurements.png'))
        # Auto comparison refreshes the chooser too, retaining one active filter.
        page.goto('http://benchmark.test/benchmarks/compare')
        page.locator('[name=model]').first.check()
        playwright.expect(page.locator('.comparison-table')).to_be_visible()
        page.locator('[data-card-key="benchmark-recorded"] > summary').click()
        page.locator('label.nav-link').filter(has_text='Other quants').click()
        playwright.expect(page.locator('[name=history_filter]:checked')).to_have_value('quants')
        assert page.locator('[name=history_filter]:checked').count() == 1
        sign_in(server, 'pat')
        page.goto('http://benchmark.test/benchmarks/runs')
        playwright.expect(page.locator('[data-card-key="benchmark-start"]')).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(images / 'mobile.png'))
        page.evaluate("() => { Storage.prototype.setItem = () => { throw new Error('disabled'); }; }")
        page.locator('summary').filter(has_text='Queue every outstanding benchmark').click()
        assert not errors
        browser.close()


def test_embedder_cards_picker_and_mobile_results_are_separate(desk,tmp_path):
    server,_,_=desk
    models=Models(str(tmp_path/'loadcoach.db'))
    models.put('text-model',{'flags':{'model':str(tmp_path/'text.gguf'),'ctx-size':2048}})
    models.put('vector-model',{'flags':{'model':str(tmp_path/'vector.gguf'),'ctx-size':512,'embedding':True}})
    store=server.app.state.benchmarks.state.store
    number=store.create('jordan','vector-model','embedding_speed',models.identity('vector-model'))
    store.activate(number,{'fingerprint':'fixture','subject':{'settings':{'flags':{'embedding':True}}},'environment':{}})
    store.keep(number,'speed-512',{'success':True,'context_tokens':512,'input_tokens':512,
               'embedding_tokens_per_second':5120,'embeddings_per_second':10,'embedding_dimensions':384,
               'request_seconds':.1,'load_seconds':1,'peak_gpu_mib':100},'{}','',{'usage':{'prompt_tokens':512}})
    store.finish(number,'completed');store.synced(number)
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000})
        page=context.new_page()
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def serve(route):
            serve_browser_request(server, 'http://embedding.test', route)
        context.route('http://embedding.test/**',serve)
        page.goto('http://embedding.test/benchmarks/embedders')
        assert page.locator('[aria-label="Embedder benchmarks"]').count()==0
        assert page.locator('[data-card-key="benchmark-resources"] .nav-pills .nav-link').count()==3
        assert page.locator('[name=run-target]').count()==1
        assert json.loads(page.locator('[name=run-target]').input_value()) == {'model':'vector-model','machine':'compute'}
        enabled_only = page.evaluate('''() => {
          const wrapper = document.createElement('div');
          wrapper.innerHTML = '<input type="checkbox" data-select-node="compute">' +
            '<input type="checkbox" name="run-target" data-node="compute" disabled>' +
            '<input type="checkbox" name="run-target" data-node="compute">';
          document.body.appendChild(wrapper);
          wrapper.querySelector('[data-select-node]').click();
          const result = [...wrapper.querySelectorAll('[name="run-target"]')].map(box => box.checked);
          wrapper.remove();
          return result;
        }''')
        assert enabled_only == [False, True]
        assert page.locator('#node-measurements').count()==1
        assert page.locator('#benchmark-resource-heatmap svg').count()==1
        assert page.locator('#benchmark-resource-table tbody tr').count()==1
        assert page.locator('#benchmark-resource-table .score-cell').count()==1
        page.select_option('#benchmark','embedding_load')
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('embedding_load')
        page.wait_for_timeout(2400)  # real OOB polling must leave the card and selected benchmark alone
        playwright.expect(page.locator('#start-benchmark input')).to_have_value('embedding_load')
        images=Path('data/benchmark-previews');images.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(images/'embedders-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(images/'embedders-mobile.png'))
        page.goto('http://embedding.test/benchmarks/node-measurements?benchmark=speed')
        types = page.locator('nav[aria-label="Model type"]')
        playwright.expect(types.get_by_role('link')).to_have_text(['LLM', 'Embedders', 'Image', 'Audio'])
        types.get_by_role('link', name='Embedders', exact=True).click()
        page.wait_for_url('http://embedding.test/benchmarks/node-measurements?kind=embedding&benchmark=embedding_speed')
        playwright.expect(page.locator('#benchmark-resource-table tbody')).to_contain_text('vector-model')
        assert 'text-model' not in page.locator('#benchmark-resource-table tbody').inner_text()
        playwright.expect(page.locator('#benchmark-resource-table a[data-score="5120.0"]')).to_have_count(1)
        page.locator('nav[aria-label="Model type"]').get_by_role('link', name='LLM', exact=True).click()
        page.wait_for_url('http://embedding.test/benchmarks/node-measurements?kind=llm&benchmark=speed')
        playwright.expect(page.locator('#benchmark-resource-table tbody')).to_contain_text('text-model')
        assert 'vector-model' not in page.locator('#benchmark-resource-table tbody').inner_text()
        page.locator('nav[aria-label="Model type"]').get_by_role('link', name='Embedders', exact=True).click()
        page.wait_for_url('http://embedding.test/benchmarks/node-measurements?kind=embedding&benchmark=embedding_speed')
        for metric in ('context', 'load', 'speed'):
            label = {'context':'Embedder context', 'load':'Embedder load time', 'speed':'Embedder speed'}[metric]
            page.locator('nav[aria-label="Resource benchmark"]').get_by_role('link', name=label, exact=True).click()
            page.wait_for_url('http://embedding.test/benchmarks/node-measurements?benchmark=embedding_' + metric + '&kind=embedding#node-measurements')
            playwright.expect(page.locator('#benchmark-resource-table tbody')).to_contain_text('vector-model')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.goto('http://embedding.test/benchmarks/run/'+number)
        assert 'Input tokens/s' in page.locator('thead').inner_text()
        assert 'First token' not in page.locator('thead').inner_text()
        page.locator('a[href="#sample-1"]').click()
        page.locator('#sample-1 .sample-section > summary').first.click()
        assert 'Embedding receipt' in page.locator('#sample-1').inner_text()
        assert not errors
        browser.close()
