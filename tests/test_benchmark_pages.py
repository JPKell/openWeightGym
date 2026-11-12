"""Benchmark design pages use the caller's catalogue and never enqueue preview selections."""

import pytest
import json
from dataclasses import replace
from starlette.responses import JSONResponse
from starlette.routing import request_response

from engines import engine_for
from weightroom.navigation import navigation
from loadcoach.groups import read_group
from loadcoach.models import Models
from test_weightroom import desk, sign_in
from test_benchmark_lanes import fleet  # noqa: F401


@pytest.fixture
def typed_models(desk, tmp_path, monkeypatch):
    browser, _, _ = desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    store = browser.app.state.benchmarks.state.store
    entries = {}
    for kind, engine in (('llm', 'llama_cpp'), ('image', 'sdcpp'), ('audio', 'audiocpp')):
        adapter = engine_for(engine)
        installation = adapter.installation
        monkeypatch.setattr(adapter, 'installation', lambda config, original=installation: replace(original(config), enabled=True))
        name = kind + '-model'
        path = str(tmp_path / (name + '.gguf'))
        settings = {'engine': engine, 'gpus': 0, 'flags': {'model': path, 'ctx-size': 2048 if kind == 'llm' else 0}}
        if kind == 'image':
            settings['sdcpp'] = {'family': 'flux2-dev', 'components': {}}
        elif kind == 'audio':
            settings['audiocpp'] = {'family': 'kokoro_tts', 'kind': 'speech', 'path': path}
        library.put(name, settings)
        number = store.create('jordan', name, 'load', library.identity(name), location='compute')
        store.activate(number, {'fingerprint': kind, 'subject': {'settings': settings}, 'environment': {}})
        store.keep(number, 'load-1', {'success': True, 'context_tokens': settings['flags']['ctx-size'], 'load_seconds': 2}, '', '')
        store.finish(number, 'completed'); store.synced(number)
        entries[kind] = (name, number)
    return entries


def test_results_are_llm_only_and_node_comparisons_keep_types_separate(desk, typed_models):
    browser, _, users = desk
    sign_in(browser, 'jordan')
    for kind, (name, number) in typed_models.items():
        result = browser.get('/benchmarks/results', params={'kind': kind, 'benchmark': 'load'})
        assert result.status_code == 200
        assert 'id="benchmark-resource-table"' not in result.text
        assert number not in result.text
        table = result.text.split('id="benchmark-results"', 1)[1].split('</table>', 1)[0]
        assert 'llm-model' in table and 'image-model' not in table and 'audio-model' not in table
        assert 'aria-label="Model type"' not in result.text
        assert 'name="kind"' not in result.text and 'name="family"' not in result.text
        options = result.text.split('<select id="benchmark"', 1)[1].split('</select>', 1)[0]
        assert all(f'value="{resource}"' not in options for resource in ('context', 'speed', 'load'))
        nodes = browser.get('/benchmarks/node-measurements', params={'kind': kind, 'benchmark': 'load'})
        grid = nodes.text.split('id="benchmark-resource-table"', 1)[1].split('</table>', 1)[0]
        assert name in grid and number in grid and 'data-label="compute"' in grid
        assert all(other not in grid for other, _ in typed_models.values() if other != name)
        assert f'kind={kind}#node-measurements' in nodes.text
    users.change('pat', own={'models': ['llm-model']})
    sign_in(browser, 'pat')
    for section in ('results', 'node-measurements'):
        page = browser.get('/benchmarks/' + section + '?kind=audio')
        assert page.status_code == 200 and 'audio-model' not in page.text
        assert typed_models['audio'][1] not in page.text


@pytest.mark.parametrize('legacy', [False, True])
def test_scores_exclude_media_and_run_lanes_offer_only_compatible_models(desk, typed_models, monkeypatch, legacy):
    browser, _, _ = desk
    if legacy:
        route = next(route for route in browser.app.state.benchmarks.routes if route.path == '/benchmarks' and 'GET' in route.methods)
        original = route.endpoint
        async def without_engine_id(request):
            response = await original(request)
            if response.status_code == 200:
                value = json.loads(response.body)
                for model in value['catalogue']['data']:
                    model.pop('engine_id', None)
                return JSONResponse(value)
            return response
        monkeypatch.setattr(route, 'app', request_response(without_engine_id))
    sign_in(browser, 'jordan')
    scores = browser.get('/benchmarks/results').text.split('id="benchmark-score-table"', 1)[1].split('</table>', 1)[0]
    assert 'llm-model' in scores and 'image-model' not in scores and 'audio-model' not in scores
    for benchmark in ('load', 'context', 'extraction'):
        page = browser.get('/benchmarks/runs', params={'benchmark': benchmark})
        assert page.status_code == 200
        import re
        from html import unescape
        choices = [json.loads(unescape(value))['model'] for value in re.findall(r'name="run-target"[^>]*value="([^"]+)"', page.text)]
        assert 'llm-model' in choices
        assert ('image-model' in choices) == (benchmark == 'load')
        assert ('audio-model' in choices) == (benchmark == 'load')
    # Previously saved measurements remain available without returning these models to the picker.
    for kind in ('image', 'audio'):
        assert browser.get('/benchmarks/run/' + typed_models[kind][1]).status_code == 200


def test_categories_keep_detail_pages_and_voice_in_their_section():
    nav = navigation("/file", ["manage-models", "front-voice"], True)
    assert nav["section"] == "Models" and nav["active"] == "/files"
    chat = next(group for group in nav["groups"] if group["name"] == "Chat")
    assert ("Voice", "/voice") in chat["links"] and ("Voice desktop app", "/voice/desktop") in chat["links"]
    assert navigation("/voice/desktop", ["front-voice"], False)["active"] == "/voice/desktop"
    assert navigation("/chat/abc", [], False)["active"] == "/chat"
    assert navigation("/benchmarks/manage", ["manage-settings"], False)["section"] == "Benchmark"
    assert navigation("/benchmarks", [], False)["links"][:3] == [("Runs", "/benchmarks/runs"), ("Results", "/benchmarks/results"), ("Node measurements", "/benchmarks/node-measurements")]
    assert navigation("/benchmarks", [], False)["active"] == "/benchmarks/runs"
    assert navigation("/batches", [], False)["section"] == "Logs"
    assert "Models" not in [group["name"] for group in navigation("/", [], False)["groups"]]


def test_benchmark_page_fetches_the_authorized_catalogue_once(desk, monkeypatch):
    browser, switch, _ = desk
    sign_in(browser, 'pat')
    paths = []
    original = switch.handle_async_request

    async def counted(request):
        paths.append(request.url.path)
        return await original(request)

    monkeypatch.setattr(switch, 'handle_async_request', counted)
    assert browser.get('/benchmarks').status_code == 200
    assert paths.count('/v1/models') == 1


def test_benchmark_pages_filter_models_before_rendering_or_comparing(desk, tmp_path):
    browser, _, users = desk
    models = Models(str(tmp_path / "loadcoach.db"))
    models.put("visible-model", {"flags": {"model": str(tmp_path / "visible.gguf")}})
    models.put("private-model", {"flags": {"model": str(tmp_path / "private.gguf")}})
    users.change("pat", own={"models": ["visible-model"]})
    sign_in(browser, "pat")
    for address in ["/benchmarks", "/benchmarks/results", "/benchmarks/node-measurements", "/benchmarks/runs", "/benchmarks/compare?selection=1&model=private-model&model=visible-model&bench=context&bench=load"]:
        page = browser.get(address)
        assert page.status_code == 200
        assert "visible-model" in page.text and "private-model" not in page.text
        assert "Setup in progress" not in page.text
    results = browser.get("/benchmarks/results?benchmark=speed&model=visible-model").text
    assert "First token" not in results and "Correct fields" in results and "run-details" not in results
    assert 'id="benchmark-resource-heatmap"' not in results and 'id="benchmark-resource-table"' not in results
    nodes = browser.get('/benchmarks/node-measurements?benchmark=speed').text
    assert 'data-measurement-matrix' in nodes and 'id="benchmark-resource-table"' in nodes
    assert 'id="benchmark-resource-heatmap"' not in nodes
    old_link = browser.get("/benchmarks?benchmark=speed&model=visible-model", follow_redirects=False)
    assert old_link.headers['location'] == '/benchmarks/results?benchmark=speed&model=visible-model'
    assert "Not run" in results and "score-missing" in results
    runs = browser.get("/benchmarks/runs").text
    # Shared cards include their headers in polling; remembered state restores after OOB swaps.
    for key in ('benchmark-running', 'benchmark-start', 'benchmark-queue', 'benchmark-history'):
        assert f'data-card-key="{key}"' in runs
    assert "Running now" in runs and "No benchmark is running." in runs
    again = browser.get("/benchmarks/runs?part=live").text
    assert "<html" not in again and again.count('hx-swap-oob="outerHTML"') == 4
    assert all(f'id="{piece}"' in runs and f'id="{piece}"' in again for piece in ("benchmark-active", "benchmark-queue", "benchmark-history"))
    assert 'aria-label="Select visible-model at compute" disabled' in runs
    assert browser.get("/benchmarks/manage").status_code == 403
    assert browser.get("/benchmarks/unknown").status_code == 404
    refused = browser.post("/benchmarks/runs", data={"run-model": "visible-model", "benchmark": "context"}, headers={"referer": "http://testserver/benchmarks"})
    assert refused.status_code == 400 and '<h1 class="page-title wrap">Runs' in refused.text and 'role="alert"' in refused.text and "Not here" not in refused.text


def test_runs_is_the_landing_page_and_polling_preserves_the_start_form(desk, tmp_path):
    browser, _, _ = desk
    Models(str(tmp_path / 'loadcoach.db')).put('active-model', {'flags': {'model': str(tmp_path / 'active.gguf')}})
    sign_in(browser, 'jordan')
    landing = browser.get('/benchmarks', follow_redirects=False)
    assert landing.status_code == 303 and landing.headers['location'] == '/benchmarks/runs'
    assert browser.get('/benchmarks/home').status_code == 404
    idle = browser.get('/benchmarks').text
    assert '<h1 class="page-title wrap">Runs</h1>' in idle and '<div id="benchmark-choice">' in idle
    assert 'data-benchmark-rows="10"' in idle and 'class="card-heading">Start a run</h2>' in idle
    store = browser.app.state.benchmarks.state.store
    number = store.create('jordan', 'active-model', 'load')
    store.activate(number, {'fingerprint': 'fp', 'subject': {'settings': {'flags': {'ctx-size': 2048}}}, 'environment': {}})
    store.state(number, 'running')
    store.keep(number, 'load-2048-0', {'context_tokens': 2048, 'success': True, 'load_seconds': 3}, '', '')
    store.message(number, 'Newest line <script>unsafe()</script>')
    assert '<div id="benchmark-choice" hidden>' not in browser.get('/benchmarks').text
    for address in ('/benchmarks', '/benchmarks/runs'):
        text = browser.get(address).text
        assert '1 / 3 samples' in text and 'Time elapsed' in text and 'Estimated time remaining' in text
        assert 'Newest line &lt;script&gt;unsafe()&lt;/script&gt;' in text
        assert 'tabindex="0" aria-label="Latest benchmark log"' in text
    refresh = browser.get('/benchmarks?part=live').text
    assert refresh.count('hx-swap-oob="outerHTML"') == 4 and 'Running now' in refresh and 'active-model' in refresh
    assert 'start-benchmark' not in refresh
    store.finish(number, 'completed')
    assert 'No benchmark is running.' in browser.get('/benchmarks?part=live').text
    assert 'id="benchmark-queue-scroll"' in browser.get('/benchmarks/runs').text


def test_admin_can_review_definitions_and_compare_several_benchmarks(desk, tmp_path):
    browser, _, _ = desk
    models = Models(str(tmp_path / "loadcoach.db"))
    for name in ("model-one", "model-two"):
        models.put(name, {"flags": {"model": str(tmp_path / f"{name}.gguf")}})
    sign_in(browser, "jordan")
    manage = browser.get("/benchmarks/manage")
    assert manage.status_code == 200
    assert "IFEval" in manage.text and 'value="max_context_tokens"' in manage.text
    assert 'value="1 / load_seconds"' in browser.get("/benchmarks/manage?benchmark=load").text
    assert "Version 1</span>" in manage.text
    assert "Formula changes rescore saved measurements without rerunning models." in manage.text
    assert manage.text.count('class="nav-link py-1') == len(browser.app.state.benchmarks.state.store.definitions())
    assert "max_context_tokens * parameter_count" in manage.text
    compare = browser.get("/benchmarks/compare?selection=1&model=model-one&model=model-two&bench=speed&bench=load").text
    table = compare.split('class="table comparison-table')[1]
    assert "model-one" in table and "model-two" in table and "First token" in table and "Load time" in table
    assert "Maximum context" not in table
    assert "Choose at least one model" in browser.get("/benchmarks/compare?selection=1&model=model-one").text
    assert "benchmark" in read_group("admin", {"can": ["superuser"]}).can
    assert "benchmark" not in read_group("reader", {"can": ["load"]}).can
    assert "benchmark" in browser.get("/groups/admin").text


def test_context_details_show_exact_capacity_and_actual_token_counts(desk, tmp_path):
    browser, _, _ = desk
    Models(str(tmp_path / "loadcoach.db")).put("context-model", {"flags": {"model": str(tmp_path / "context.gguf")}})
    store = browser.app.state.benchmarks.state.store
    run = store.create("jordan", "context-model", "context")
    store.activate(run, {"fingerprint": "context-fp", "subject": {"values": {}},
                         "environment": {"engine": "test engine", "engine_hash": "abc123", "machine": {"host": "test"}}})
    store.keep(run, "context-131072", {"context_tokens": 131072, "input_tokens": 131040, "output_tokens": 16,
                                      "success": True, "request_seconds": 227.3}, "answer", "server log")
    store.finish(run, "completed")
    store.synced(run)
    sign_in(browser, "jordan")
    response = browser.get("/benchmarks/results", params={"benchmark": "context", "model": "context-model", "run": run})
    assert response.status_code == 200
    for text in ("Context tokens", "Input tokens", "Output tokens", "Request (s)", "<td>131,072</td>",
                 "<td>131,040</td>", "<td>16</td>", "<td>227.3</td>", "First-token time starts after loading"):
        assert text in response.text


def test_cpu_sample_memory_is_separate_from_historical_gpu_memory(desk, tmp_path):
    browser, _, _ = desk
    Models(str(tmp_path / "loadcoach.db")).put("memory-model", {"flags": {"model": str(tmp_path / "memory.gguf")}})
    store = browser.app.state.benchmarks.state.store
    environment = {"engine": "test", "machine": {"host": "test"}}

    def recorded(values, evidence=None):
        run = store.create("jordan", "memory-model", "load")
        store.activate(run, {"fingerprint": "memory-fp", "subject": {"values": {}}, "environment": environment})
        store.keep(run, "load-2048-0", {"context_tokens": 2048, "success": True, "load_seconds": 2, **values}, "", "", evidence)
        store.finish(run, "completed")
        store.synced(run)
        return run

    gpu = recorded({"peak_gpu_mib": 512})
    cpu = recorded({"device": "cpu", "peak_gpu_mib": None, "peak_ram_mib": 768, "rss_mib": 640},
                   {"resource": {"device": "cpu", "peak_ram_mib": 768, "rss_mib": 640}})
    cpu_receipt = recorded({"peak_gpu_mib": None}, {"resource": {"device": "cpu", "peak_ram_mib": 700, "rss_mib": 600}})
    sign_in(browser, "jordan")
    gpu_page = browser.get(f"/benchmarks/run/{gpu}").text
    assert "<th>GPU MiB</th>" in gpu_page and "<th>CPU peak RAM (MiB)</th>" not in gpu_page
    assert "<td>512.0</td>" in gpu_page
    cpu_page = browser.get(f"/benchmarks/run/{cpu}").text
    assert "<th>GPU MiB</th><th>CPU peak RAM (MiB)</th>" in cpu_page
    assert "<td>—</td><td>768.0</td>" in cpu_page
    assert "final resident sample" in cpu_page and '"rss_mib": 640' in cpu_page
    receipt_page = browser.get(f"/benchmarks/run/{cpu_receipt}").text
    assert "<th>CPU peak RAM (MiB)</th>" in receipt_page and "<td>—</td><td>700.0</td>" in receipt_page

    manage = browser.get("/benchmarks/manage?benchmark=load").text
    assert "Peak measured resident RAM of the CPU probe process and its children" in manage
    assert "GPU generation probes require full layer and KV offload" in manage
    assert "Configured CPU embedders support Load and embedding resource probes" in manage
    assert "Model layers must fit on GPUs for these probes" not in manage


def test_quality_details_render_size_summary_answers_and_pinned_plan(desk,tmp_path):
    browser,_,_=desk
    models=Models(str(tmp_path/'loadcoach.db'))
    models.put('quality-model',{'flags':{'model':str(tmp_path/'quality.gguf')}})
    store=browser.app.state.benchmarks.state.store
    n=store.create('jordan','quality-model','extraction')
    store.activate(n,{'fingerprint':'quality-fp','subject':{'values':{}},
                     'environment':{'engine':'test engine','engine_hash':'abc123','machine':{'host':'test'}}})
    store.plan(n,{'cases':[{'id':'extract-512-1'}]})
    store.keep(n,'extract-512-1',{'context_tokens':1024,'target_input_tokens':512,'category':'records',
               'input_tokens':512,'correct':8,'checks':8,'success':True,'passed':True},'raw answer','server log',
               {'case':{'expected':{'event':'Cedar Workshop'}},'answer':'graded answer',
                'prompt':'Exact <script>unsafe()</script> input','checks':[{'field':'event','passed':True}]})
    store.finish(n,'completed');store.synced(n)
    sign_in(browser,'jordan')
    page=browser.get('/benchmarks/results',params={'benchmark':'extraction','model':'quality-model','run':n})
    assert page.status_code==200
    for label in ['Results by input size and category','8 / 8','1 / 1','Checks and answer key',
                  'Cedar Workshop','graded answer','Exact rendered input','raw answer']:
        assert label in page.text,label
    assert '<script>unsafe()</script>' not in page.text
    assert '&lt;script&gt;unsafe()&lt;/script&gt;' in page.text
    # The run says which version of its benchmark it was made under, and warns once the benchmark has moved on.
    version=store.module('extraction').PROTOCOL['version']
    assert f'Benchmark version {version}' in page.text and 'Made under version' not in page.text
    import json
    subject=json.loads(store.db.execute('SELECT subject FROM benchmark_runs WHERE id=?',(n,)).fetchone()[0])
    subject['protocol']['version']=1
    with store.db:
        store.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?',(json.dumps(subject),n))
    behind=browser.get('/benchmarks/results',params={'benchmark':'extraction','model':'quality-model','run':n}).text
    assert f'Made under version 1 of this benchmark, which is now at version {version}.' in behind


def test_a_model_page_has_a_card_with_its_result_on_each_benchmark(desk, tmp_path):
    browser, _, _ = desk
    models = Models(str(tmp_path / "loadcoach.db"))
    file = tmp_path / "scored.gguf"
    file.touch()
    models.put("scored-model", {"flags": {"model": str(file), "ctx-size": 2048}})
    models.keep_hash(str(file), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", 0, file.stat().st_mtime)  # read through, so it has a fingerprint
    sign_in(browser, "jordan")
    store = browser.app.state.benchmarks.state.store
    environment = {"engine": "test engine", "engine_hash": "abc123", "machine": {"host": "test"}}
    now = browser.get("/models/scored-model").text.split('href="/fingerprints?start=')[1].split('"')[0]
    for mark in ("an-older-fingerprint", now):  # only the run for the fingerprint it has now is shown
        number = store.create("jordan", "scored-model", "load")
        store.activate(number, {"fingerprint": mark, "subject": {"values": {}}, "environment": environment})
        store.keep(number, "load-1", {"context_tokens": 2048, "success": True, "load_seconds": 4.0 if mark == "an-older-fingerprint" else 2.0}, "", "")
        store.finish(number, "completed")
        store.synced(number)
    # Settings stay above saved results; the results card has a pill for every benchmark.
    whole = browser.get("/models/scored-model").text
    card = whole.split('<span class="card-heading">Benchmark results</span>')[1].split("</details>")[0]
    assert whole.index('class="card-heading">Settings</span>') < whole.index('class="card-heading">Benchmark results</span>') and card.count('class="nav-link') == len([bench for bench in store.definitions() if not bench.get('embedding')])
    assert '<a class="nav-link py-1 active" href="/models/scored-model?benchmark=load#benchmark" aria-current="page" hx-get="/models/scored-model?part=benchmark&amp;benchmark=load">' in card
    # Its figures, two to a row: the score, then what was measured. They lead to the run they came from.
    assert "Score <small>1/s</small></th>" in card and '<td class="w-25">0.5<' in card and "Load time <small>s</small></th>" in card and ">2.0</td>" in card
    assert f"/benchmarks/run/{number}" in card
    # A pill asks for the piece alone, with its benchmark chosen. One the model has not run says so.
    piece = browser.get("/models/scored-model?part=benchmark&benchmark=speed").text
    assert "<html" not in piece and '<div id="benchmark" hx-target="this" hx-swap="outerHTML">' in piece
    assert 'nav-link py-1 active" href="/models/scored-model?benchmark=speed#benchmark"' in piece and "No completed saved run" in piece and "<table" not in piece

    # A result made under an older version of its benchmark is shown with a star that says so.
    import json
    subject = json.loads(store.db.execute("SELECT subject FROM benchmark_runs WHERE id=?", (number,)).fetchone()[0])
    subject["protocol"]["version"] = 0
    with store.db:
        store.db.execute("UPDATE benchmark_runs SET subject=? WHERE id=?", (json.dumps(subject), number))
    assert "benchmark version 0, now 1" in browser.get("/models/scored-model?part=benchmark&benchmark=load").text and "benchmark version 0, now 1" in browser.get("/benchmarks/node-measurements?benchmark=load").text


def test_context_edits_keep_scores_details_comparison_and_model_results(desk, tmp_path):
    """A changed context preserves recorded evidence without queuing or relabelling a run."""
    import hashlib
    browser, _, _ = desk
    models = Models(str(tmp_path / 'loadcoach.db'))
    file = tmp_path / 'context.gguf'
    file.write_bytes(b'weights')
    models.put('context-model', {'flags': {'model': str(file), 'ctx-size': 2048}})
    models.keep_hash(str(file), hashlib.sha256(b'weights').hexdigest(), file.stat().st_size, file.stat().st_mtime)
    sign_in(browser, 'jordan')
    def fingerprint():
        return browser.get('/models/context-model').text.split('href="/fingerprints?start=')[1].split('"')[0]
    mark = fingerprint()
    store = browser.app.state.benchmarks.state.store
    def recorded(fingerprint, context, seconds, state='completed'):
        ref = store.create('jordan', 'context-model', 'load')
        store.activate(ref, {'fingerprint': fingerprint, 'subject': {'settings': {'flags': {'ctx-size': context}}},
                            'environment': {'engine': 'test', 'engine_hash': 'test', 'machine': {'host': 'test'}}})
        store.keep(ref, 'load-1', {'success': True, 'context_tokens': context, 'load_seconds': seconds}, '', '')
        store.finish(ref, state); store.synced(ref)
        return ref
    original = recorded(mark, 2048, 2)
    assert 'Settings changed.' not in browser.get('/benchmarks/node-measurements?benchmark=load&model=context-model').text
    # Change only context: no run is added and raw evidence is unchanged.
    models.put('context-model', {'flags': {'model': str(file), 'ctx-size': 8192}})
    current = fingerprint()
    assert current != mark
    before = store.db.execute('SELECT * FROM benchmark_samples').fetchall()
    for path in ('/benchmarks/node-measurements?benchmark=load&model=context-model', '/models/context-model?part=benchmark&benchmark=load'):
        page = browser.get(path)
        assert page.status_code == 200 and 'Settings changed.' in page.text and ('configured context 2048' in page.text or 'configured context: 2048 tokens' in page.text)
        assert original in page.text and '>0.5' in page.text
    compared = browser.get('/benchmarks/compare?selection=1&model=context-model&bench=load').text
    assert original in compared and 'Settings changed.' in compared
    assert store.db.execute('SELECT * FROM benchmark_samples').fetchall() == before
    assert store.db.execute('SELECT count(*) FROM benchmark_runs').fetchone()[0] == 1
    # A current completed measurement takes precedence, even over a newer historical run.
    matching = recorded(current, 8192, 4)
    recorded(mark, 2048, 8)
    page = browser.get('/benchmarks/node-measurements?benchmark=load&model=context-model').text
    assert f'/benchmarks/run/{matching}' in page and 'Settings changed.' not in page
    recorded(current, 8192, 10, 'failed')
    assert f'/benchmarks/run/{matching}' in browser.get('/benchmarks/node-measurements?benchmark=load&model=context-model').text


def test_green_direction_saves_without_changing_matrix_scores(desk,tmp_path,monkeypatch):
    import re
    browser,_,_=desk
    models=Models(str(tmp_path/'loadcoach.db'))
    for name in ('low-model','high-model','unmeasured'):
        models.put(name,{'flags':{'model':str(tmp_path/(name+'.gguf'))}})
    store=browser.app.state.benchmarks.state.store
    runs=[{'id':name,'model':name,'model_id':models.identity(name),'benchmark':'context','fingerprint':None,'environment':{},'version':1,'latest_version':1,
           'state':'completed','score':score,'metrics':{},'ended':1,'user':'jordan'}
          for name,score in [('low-model',-5),('high-model',0)]]
    monkeypatch.setattr(store,'listing',lambda:[dict(r) for r in runs])
    sign_in(browser,'jordan')
    def matrix(direction):
        page=browser.get('/benchmarks/node-measurements');assert page.status_code==200
        assert 'Not run' in page.text and f'data-score-higher="{direction}"' in page.text
        return {float(score) for score in re.findall(r'<td[^>]+data-score="([^"]+)"',page.text)}
    assert matrix('true')=={-5,0}
    form={'benchmark':'context','formula':'max_context_tokens','enabled':'on','green':'low'}
    saved=browser.post('/benchmarks/manage',data=form)
    assert saved.status_code==200 and 'value="low" selected' in saved.text
    assert matrix('false')=={-5,0}
    assert [r['score'] for r in runs]==[-5,0]
    refused=browser.post('/benchmarks/manage',data={**form,'green':'invalid'})
    assert refused.status_code==400 and 'role="alert"' in refused.text and 'value="max_context_tokens"' in refused.text
    assert next(d for d in store.definitions() if d['id']=='context')['green']=='low'
    # Tied scores and real zeroes remain recorded evidence.
    runs[0]['score']=0
    assert matrix('false')=={0}


def test_removed_history_is_visible_to_managers_without_name_reuse_or_private_leaks(desk, tmp_path):
    browser, _, users = desk
    models = Models(str(tmp_path / 'loadcoach.db'))
    for name in ('base-1-3b-q4_k_m', 'private-1-3b-q4_k_m'):
        models.put(name, {'flags': {'model': str(tmp_path / (name + '.gguf'))}})
    store = browser.app.state.benchmarks.state.store
    ids = []
    for name in ('base-1-3b-q4_k_m', 'private-1-3b-q4_k_m'):
        # The first run predates stable IDs; bind its retired identity before
        # reading the shared listing, never to the later model reusing its name.
        identity = None if name.startswith('base-') else models.identity(name)
        number = store.create('jordan', name, 'load', identity)
        store.activate(number, {'fingerprint': 'saved-' + name, 'subject': {}, 'environment': {}})
        store.keep(number, 'load', {'success': True, 'context_tokens': 2048, 'load_seconds': 2}, '', '')
        store.finish(number, 'completed'); store.synced(number)
        ids.append(number)
    old_id = models.identity('base-1-3b-q4_k_m')
    models.remove('base-1-3b-q4_k_m')
    models.put('base-1-3b-q4_k_m', {'flags': {'model': str(tmp_path / 'other.gguf')}})
    assert models.identity('base-1-3b-q4_k_m') != old_id
    sign_in(browser, 'jordan')
    removed = browser.get('/benchmarks/compare?history_filter=retired').text
    assert ids[0] in removed and ids[1] not in removed
    assert 'Removed model' in browser.get('/benchmarks/run/' + ids[0]).text
    assert ids[0] not in browser.get('/benchmarks/results').text
    users.change('pat', own={'models': ['base-1-3b-q4_k_m']})
    sign_in(browser, 'pat')
    assert browser.get('/benchmarks/run/' + ids[0]).status_code == 404
    assert browser.get('/benchmarks/run/' + ids[1]).status_code == 404
    assert ids[0] not in browser.get('/benchmarks/compare?history_filter=all').text


def test_rank_direction_ties_version_exclusion_and_missing_coverage():
    from weightroom.benchmark_views import comparison_ranks
    definitions = [{'id': 'a', 'version': 2, 'green': 'high'}, {'id': 'b', 'version': 1, 'green': 'low'}, {'id': 'c', 'version': 1, 'green': 'high'}]
    def run(score, version): return {'score': score, 'version': version}
    latest = {'one': {'a': run(20, 2), 'b': run(5, 1)}, 'two': {'a': run(20, 2)}, 'three': {'a': run(10, 2), 'b': run(2, 1)}}
    columns = [{'results': latest['one']}, {'results': latest['two']}, {'results': latest['three']}, {'results': {'a': run(50, 1)}}]
    comparison_ranks(columns, definitions, latest, ['a', 'b', 'c'])
    assert [c['ranks'] for c in columns] == [{'a': 1, 'b': 2}, {'a': 1}, {'a': 3, 'b': 1}, {}]
    assert [c['average_rank'] for c in columns] == [1.5, 1, 2, None]
    assert [c['rank_missing'] for c in columns] == [False, True, False, True]
    comparison_ranks(columns[:3], definitions, latest, ['c'])
    assert all(c['average_rank'] is None and not c['rank_missing'] for c in columns[:3])


def test_quant_matching_preserves_base_version_size_and_history_pagination():
    from benchmarks.identity import variant
    from weightroom.benchmark_views import history_page
    assert variant('Qwen3.8-27B-Q4_K_M')['family'] == variant('Qwen3.8-27B-Q6_K')['family']
    assert variant('Qwen3.6-27B-Q4_K_M')['family'] != variant('Qwen3.8-27B-Q4_K_M')['family']
    assert variant('Qwen3.8-9B-Q4_K_M')['family'] != variant('Qwen3.8-27B-Q4_K_M')['family']
    assert variant('Qwen3.8-27B-Q4_K_M')['quant'] == 'q4_k_m'
    ud4 = variant('qwen3.8-27b-q4', {'settings': {'flags': {'model': '/weights/Qwen3.8-27B-UD-Q4_K_XL.gguf'}}})
    ud6 = variant('qwen3.8-27b-q6', {'settings': {'flags': {'model': '/weights/Qwen3.8-27B-UD-Q6_K_M.gguf'}}})
    assert ud4['family'] == ud6['family'] == 'qwen3.8-27b'
    assert ud4['quant'] == 'q4_k_xl' and ud6['quant'] == 'q6_k_m'
    assert variant('alias', {'settings': {'flags': {'model': '/weights/Qwen3.8-27B-Q4_K_M-00001-of-00003.gguf'}}}) == {'family': 'qwen3.8-27b', 'quant': 'q4_k_m'}

    runs = [{'id': str(i), 'state': 'completed'} for i in range(31)] + [{'state': 'queued'}]
    page = history_page(runs, {'page': '2', 'per_page': '25'})
    assert page['total'] == 31 and len(page['items']) == 6 and page['page'] == 2
    assert history_page(runs, {'page': '-5', 'per_page': 'oops'})['page'] == 1
    assert history_page(runs, {'page': '999', 'per_page': '10'})['page'] == 4


def test_results_show_one_quality_result_per_model_across_nodes(fleet):
    browser, library, _, _ = fleet
    store = browser.app.state.benchmarks.state.store
    saved = []
    for node, fingerprint in [('compute', 'saved-compute-settings'), ('rig', 'saved-rig-settings')]:
        ref = store.create('jordan', 'local-model', 'extraction', library.identity('local-model'), location=node)
        store.activate(ref, {'fingerprint': fingerprint, 'subject': {}, 'environment': {}})
        store.keep(ref, 'extract-one', {'success': True, 'correct': 1, 'checks': 1, 'passed': True}, '', '')
        store.finish(ref, 'completed'); store.synced(ref)
        saved.append(ref)
    for requested in ('extraction', 'context', 'speed', 'load', 'embedding_load'):
        html = browser.get('/benchmarks/results', params={'benchmark': requested, 'kind': 'llm'}).text
        table = html.split('id="benchmark-results"', 1)[1].split('</table>', 1)[0]
        assert table.count('local-model') == 1 and table.count('private-model') == 1
        if requested == 'extraction':
            assert saved[1] in table and saved[0] not in table
        options = html.split('<select id="benchmark"', 1)[1].split('</select>', 1)[0]
        assert all(f'value="{key}"' not in options for key in ('context', 'speed', 'load'))
        assert 'value="extraction"' in options
    legacy = browser.get('/benchmarks/results?kind=image&family=embedding&benchmark=embedding_load').text
    assert 'id="benchmark-results"' in legacy and 'local-model' in legacy
    assert 'name="kind"' not in legacy and 'name="family"' not in legacy
