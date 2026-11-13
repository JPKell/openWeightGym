"""Mock-world oracles, native conversations, scoped probes and durable P4 replay."""

import asyncio
import copy
import hashlib
import json

import pytest

from benchmarks import tooling
from benchmarks.store import Benchmarks
from benchmarks.suites import tool_use, tool_recovery
from benchmarks.worker import BenchmarkWorker, Retry
from benchmark_clients import gateway
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder, model
from test_benchmarks import start, wait
from test_weightroom import gguf, desk, sign_in


def native(step, number):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': f'call-{number}', 'type': 'function',
            'function': {'name': step['name'], 'arguments': json.dumps(step['arguments'])}}]}


def oracle(case):
    return [native(step, i) for i, step in enumerate(case['steps'])] + [
        {'role': 'assistant', 'content': json.dumps(case['expected'])}]


def test_expanded_tool_sampler_preserves_v2_cases_and_recovery_defaults():
    fields = ('id','category','question','messages','tools','steps','expected','world',
              'ordered','requires_lookup','initial_record_ids')
    cases = tool_use.cases(131072)
    original = [{key: case[key] for key in fields} for case in cases]
    digest = lambda value: hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
                                               ensure_ascii=False).encode()).hexdigest()
    assert digest(original) == '436c0d3227f18d5cc2e5e242615580fdf3251dc465fac3206ce04282e4181f12'
    assert tool_use.PROTOCOL['version'] == 3 and tool_use.PROTOCOL['dataset'] == 'tool-use-v2'
    assert tool_use.PROTOCOL['grader_version'] == 2 and len(cases) == 60
    sampling = {'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
                'presence_penalty':0.0,'frequency_penalty':0.0}
    assert tool_use.PROTOCOL['outputs'] == 32768
    assert tool_use.PROTOCOL['temperature'] == tool_use.PROTOCOL['sampling']['temperature'] == 1.0
    assert tool_use.PROTOCOL['generation_seed'] == 42
    assert {key:value for key,value in tool_use.PROTOCOL['sampling'].items() if key != 'temperature'} == sampling
    assert all(case['temperature'] == 1.0 and case['sampling'] == sampling and case['outputs'] == 32768
               and case['coverage_version'] == 2 for case in cases)
    assert all(case['outputs'] == 4096 for case in tool_use.cases(8192))
    legacy = copy.deepcopy(cases[0])
    legacy.pop('temperature');legacy.pop('sampling');legacy['outputs'] = 8192
    assert tool_use.grade(legacy,json.dumps(oracle(legacy))) == tool_use.grade(cases[0],json.dumps(oracle(cases[0])))
    recovery = tool_recovery.cases(131072)
    assert digest(recovery) == '847e39c5d4bd5629b0298bc945dd81f60659cb1d3239f67abc1bc4c6d82b62e3'
    assert tool_recovery.PROTOCOL['version'] == 1 and tool_recovery.PROTOCOL['temperature'] == 0
    assert all('temperature' not in case and 'sampling' not in case for case in recovery)


@pytest.mark.parametrize('module', [tool_use, tool_recovery])
def test_fixture_oracles_pass_reproducibly_and_wrong_decisions_fail(module):
    cases = module.cases(8192)
    assert len(cases) == module.PROTOCOL.get('cases', 12) and len({c['id'] for c in cases}) == len(cases)
    assert cases == module.cases(8192)
    for case in cases:
        output = json.dumps(oracle(case))
        metrics, checks = module.grade(case, output)
        assert metrics['passed'] and metrics['completed'] and all(c['passed'] for c in checks), case['id']
        assert metrics['unnecessary_retries'] == 0
        if module is tool_recovery:
            assert metrics['recovered'] and metrics['injected_error_seen']
        if not case['steps']:
            continue
        wrong = oracle(case)
        wrong[0]['tool_calls'][0]['function']['name'] = 'shell'
        bad = module.grade(case, json.dumps(wrong))[0]
        assert not bad['passed'] and bad['selection_correct'] < metrics['selection_correct']
        assert not module.grade(case, json.dumps([oracle(case)[-1]]))[0]['completed']  # guessing is not using a tool
        for argument in ('{"metres":true}', '{"metres":13,"extra":1}', '{"metres":13,"metres":13}', 'not JSON'):
            wrong = oracle(case)
            wrong[0]['tool_calls'][0]['function']['arguments'] = argument
            assert not module.grade(case, json.dumps(wrong))[0]['passed']


def test_errors_are_real_world_responses_and_writes_are_idempotent():
    for case in tool_recovery.cases(8192):
        world = tooling.World(case)
        results = [world.call(step['name'], step['arguments']) for step in case['steps']]
        assert world.injected and any('error' in r for r in results) and 'error' not in results[-1]
        replay = tooling.World(case)
        assert results == [replay.call(step['name'], step['arguments']) for step in case['steps']]
        if case['category'] == 'lost_receipt':
            assert len(world.notes) == 1 and results[-1]['note_id'] == case['expected']['note_id']
            changed = {**case['steps'][-1]['arguments'], 'text': 'different payload'}
            assert world.call('create_note', changed)['error'] == 'idempotency_conflict'
        before = copy.deepcopy(world.notes)
        assert world.call('exec', {'command': 'rm anything'})['error'] == 'unknown_tool'
        assert world.call(case['steps'][0]['name'], {'unexpected': 1})['error'] == 'invalid_arguments'
        assert world.notes == before


def test_retries_completion_and_unsupported_are_separate_metrics():
    case = tooling.cases(8192)[0]
    turns = oracle(case)
    turns.insert(1, native(case['steps'][0], 99))
    metrics, _ = tooling.grade(case, json.dumps(turns))
    assert metrics['completed'] and not metrics['passed'] and metrics['unnecessary_retries'] == 1
    rows = [{'values': {**metrics, 'success': True, 'input_tokens': 100, 'output_tokens': 20}},
            {'values': {'unsupported': True, 'success': False}}]
    summary = tooling.summarize(rows)
    assert summary['task_completion'] == 100 and summary['selection_accuracy'] == 50
    assert summary['unsupported_cases'] == 1 and summary['available_cases'] == 1
    assert summary['input_tokens'] == 100 and summary['output_tokens'] == 20
    assert 'task_completion' not in tooling.summarize(rows[1:])
    malformed = oracle(case)
    malformed[0]['tool_calls'][0]['id'] = ''
    assert not tooling.grade(case, json.dumps(malformed))[0]['completed']
    # Duplicate IDs and native reasoning text must never be repaired into a passing trace.
    malformed[0]['tool_calls'] *= 2
    assert not tooling.grade(case, json.dumps(malformed))[0]['passed']
    assert tooling.answer('[{"reasoning_content":"<think>data</think>"}]') == '[{"reasoning_content":"<think>data</think>"}]'


def tools_model(delay=0, supported=True):
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=32768,
         tokenizer__chat_template='messages tools' if supported else 'messages')
    return {'small': model('small.gguf', ['GPU0'], **{'ctx-size': 32768, 'mib': 1000,
                         'benchmark-tools': True, 'benchmark-delay': delay, 'jinja': True})}


@pytest.mark.parametrize('module', [tool_use, tool_recovery])
def test_native_worker_finishes_batch_and_publishes_role_evidence(fake_llama_server, fake_cards, monkeypatch, module):
    sent = []
    request = BenchmarkWorker.request

    async def observe(self, number, method='GET', suffix='', **kwargs):
        if suffix == '/probe':
            body = kwargs['json']
            spec = body['evaluation' if module is tool_use else 'dialogue']
            if module is tool_use:
                assert set(spec) == {'messages', 'tools', 'temperature', 'sampling'}
                assert {'temperature':spec['temperature'], **spec['sampling']} == module.PROTOCOL['sampling']
                assert 0 < body['outputs'] <= module.PROTOCOL['outputs']
            else:
                assert set(spec) == {'messages', 'tools'}
            assert 'expected' not in body and 'world' not in body and 'steps' not in body
            sent.append(copy.deepcopy(body))
        return await request(self, number, method, suffix, **kwargs)

    monkeypatch.setattr(BenchmarkWorker, 'request', observe)
    with gateway(fake_llama_server, models=tools_model(), nvidia_smi=fake_cards[0]) as c:
        owner = add_user(c, 'owner', 'admin')
        assert c.post('/benchmarks/publish', headers=owner).status_code == 200
        n = start(c, owner, module.DEFINITION['id'])
        run = wait(c, owner, n, lambda r: r['state'] in ('completed', 'failed'), seconds=180)
        assert run['state'] == 'completed', run['reason']
        assert len(run['samples']) == module.PROTOCOL.get('cases', 12) and run['score'] == 100
        assert run['metrics']['selection_accuracy'] == run['metrics']['argument_accuracy'] == 100
        assert run['metrics']['input_tokens'] > 0 and run['metrics']['output_tokens'] > 0
        assert run['metrics']['unnecessary_retries'] == 0
        if module is tool_recovery:
            assert run['metrics']['recovery_accuracy'] == 100
            assert any('"error"' in message.get('content', '') for body in sent for message in body['dialogue']['messages'] if message['role'] == 'tool')
        for point in run['samples']:
            assert point['values']['passed'] and point['evidence']['tool_exchanges']
            assert point['values']['input_tokens'] == sum(e['probe']['values']['input_tokens'] for e in point['evidence']['tool_exchanges'])
            if module is tool_use:
                assert point['evidence']['request_sampling'] == module.PROTOCOL['sampling']
                assert all(exchange['probe']['evidence']['request_sampling'] == module.PROTOCOL['sampling']
                           for exchange in point['evidence']['tool_exchanges'])
        assert c.get('/v1/batches/'+n, headers=owner).json()['status'] == 'completed'
        snapshot = c.app.state.benchmarks.state.store.role_snapshot(number=n)
        assert snapshot['results'][0]['score'] == 100
        assert snapshot['results'][0]['benchmark'] == module.DEFINITION['id']
        assert c.get('/loadcoach/status', headers=owner).json()['lock'] is None
        # This is the same snapshot contract accepted by P1, not a second ranking path.
        role = c.post('/loadcoach/roles/agent', headers=owner, json={'models': {'small': 1},
                    'benchmarks': {module.DEFINITION['id']: 1}, 'missing': 'exclude'})
        assert role.status_code == 201, role.text
        picked = c.get('/loadcoach/pick', headers=owner, params={'model': 'agent:best'}).json()
        assert picked['picked'] == 'small', picked
    assert sent


def test_unsupported_template_gets_no_score_or_inference(fake_llama_server, fake_cards, monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError('An unsupported model must not start a probe.')
    monkeypatch.setattr(BenchmarkWorker, 'isolated', unexpected)
    with gateway(fake_llama_server, models=tools_model(supported=False), nvidia_smi=fake_cards[0]) as c:
        owner = add_user(c, 'owner', 'admin')
        n = start(c, owner, 'tool_use')
        run = wait(c, owner, n, lambda r: r['state'] in ('completed', 'failed'))
        assert run['state'] == 'completed' and run['score'] is None
        assert run['metrics']['unsupported_cases'] == 60 and run['metrics']['available_cases'] == 0
        assert not c.app.state.benchmarks.state.store.role_snapshot(number=n)['results']


@pytest.mark.parametrize('module', [tool_use, tool_recovery])
def test_tool_case_replays_after_interruption_and_keeps_completed_samples(tmp_path, module):
    """Simulate a disconnect during the second case; reopen the real queue DB and resume."""
    case_list = module.cases(8192)[:2]
    path = str(tmp_path/'tools.db')
    store = Benchmarks(path)
    n = store.create('owner', 'small', module.DEFINITION['id'])
    store.activate(n, {'fingerprint': 'fp', 'subject': {'metadata': {'takes_tools': True},
        'settings': {'flags': {'ctx-size': 8192}}, 'values': {}}, 'environment': {}})
    seen, fail = [], [True]

    class Worker:
        async def check(self, number):
            return self.store.execution(number)

        async def isolated(self, number, sample, body):
            seen.append((sample, copy.deepcopy(body.get('evaluation', body.get('dialogue'))['messages'])))
            if sample == case_list[1]['id']+':turn-2' and fail[0]:
                fail[0] = False
                raise Retry('disconnect')
            case = next(c for c in case_list if sample.startswith(c['id']+':'))
            turn = int(sample.rsplit('-', 1)[1])-1
            return {'values': {'success': True, 'input_tokens': 10, 'output_tokens': 2},
                    'output': json.dumps(oracle(case)[turn]), 'log': '', 'evidence': {'prompt': 'native', 'native_message': oracle(case)[turn]}}

    worker = Worker()
    worker.store = store
    with pytest.raises(Retry):
        asyncio.run(tooling.measure(worker, store.execution(n), 8192, module.PROTOCOL, lambda limit: case_list, module.grade))
    first = store.samples(n)[0]
    assert len(store.samples(n)) == 1
    plan = store.plan(n)
    store.db.close()
    worker.store = Benchmarks(path)
    asyncio.run(tooling.measure(worker, worker.store.execution(n), 8192, module.PROTOCOL, lambda _: [], module.grade))
    assert worker.store.plan(n) == plan and worker.store.samples(n)[0] == first
    assert len(worker.store.samples(n)) == 2
    restarted = [messages for sample, messages in seen if sample == case_list[1]['id']+':turn-1']
    assert len(restarted) == 2 and restarted[0] == restarted[1] and len(restarted[0]) == 2
    worker.store.db.close()


def test_real_batch_preemption_pause_and_restart_resume_only_unfinished_case(fake_llama_server, fake_cards, tmp_path, monkeypatch):
    make_cases = tool_recovery.cases
    monkeypatch.setattr(tool_recovery, 'cases', lambda limit: make_cases(limit)[:2])
    database = str(tmp_path/'resumed.db')
    models = tools_model(delay=.25)
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0], database=database) as c:
        owner = add_user(c, 'owner', 'admin')
        n = start(c, owner, 'tool_recovery')
        first = wait(c, owner, n, lambda r: r['samples_count'] >= 1)['samples'][0]
        assert c.post('/v1/chat/completions', headers=owner,
                      json={'model': 'small', 'messages': [{'role': 'user', 'content': 'foreground request'}]}).status_code == 200
        c.post('/benchmarks/control', headers=owner, json={'run': n, 'action': 'pause'})
        paused = wait(c, owner, n, lambda r: r['state'] == 'paused')
        assert paused['samples'][0] == first
    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0], database=database) as c:
        kept = c.get('/benchmarks/'+n, headers=owner).json()
        assert kept['plan'] == paused['plan'] and kept['state'] == 'paused' and kept['samples'][0] == first
        c.post('/benchmarks/control', headers=owner, json={'run': n, 'action': 'resume'})
        done = wait(c, owner, n, lambda r: r['state'] in ('completed', 'failed'))
        assert done['state'] == 'completed' and done['score'] == 100, done['reason']
        assert len(done['samples']) == 2 and done['samples'][0] == first


@pytest.mark.parametrize('module', [tool_use, tool_recovery])
def test_saved_native_decisions_regrade_without_losing_exchanges_or_tokens(tmp_path, module):
    store = Benchmarks(str(tmp_path/'regrade.db'))
    n = store.create('owner', 'small', module.DEFINITION['id'])
    store.activate(n, {'fingerprint': 'fp', 'subject': {}, 'environment': {}})
    case = tooling.cases(8192)[0] if module is tool_use else module.cases(8192)[0]
    raw = json.dumps(oracle(case))
    store.keep(n, case['id'], {'success': True, 'input_tokens': 99, 'completed': False}, raw, 'native log',
               {'case': case, 'answer': raw, 'checks': [], 'tool_exchanges': [{'prompt': 'unchanged'}]})
    store.finish(n, 'completed')
    subject = json.loads(store.db.execute('SELECT subject FROM benchmark_runs WHERE id=?', (n,)).fetchone()[0])
    subject['protocol']['grader_version'] = 0
    with store.db:
        store.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?', (json.dumps(subject), n))
    assert store.regrade() == 1 and store.regrade() == 0
    assert store.get(n)['score'] == 100
    saved = store.samples(n)[0]
    assert saved['output'] == raw and saved['values']['input_tokens'] == 99
    assert saved['evidence']['tool_exchanges'] == [{'prompt': 'unchanged'}]
    store.db.close()


def tool_page(browser, tmp_path):
    from loadcoach.models import Models
    models = Models(str(tmp_path/'loadcoach.db'))
    models.put('agent-model', {'flags': {'model': str(tmp_path/'agent.gguf')}})
    store = browser.app.state.benchmarks.state.store
    n = store.create('jordan', 'agent-model', 'tool_recovery', models.identity('agent-model'))
    store.activate(n, {'fingerprint': 'fp', 'subject': {}, 'environment': {}})
    case = tool_recovery.cases(8192)[0]
    metrics, checks = tooling.grade(case, json.dumps(oracle(case)))
    store.plan(n, {'cases': [case]})
    store.keep(n, case['id'], {**metrics, 'success': True, 'category': case['category'], 'input_tokens': 333},
               json.dumps(oracle(case)), '', {'case': case, 'checks': checks, 'answer': json.dumps(oracle(case)),
               'prompt': '<script>unsafe()</script>', 'conversation': [{'role': 'tool', 'content': 'temporarily_unavailable'}],
               'tool_exchanges': [{'probe': {'values': {'input_tokens': 333}, 'evidence': {'prompt': 'exact native turn'}}}]})
    store.keep(n, 'unsupported', {'success': False, 'unsupported': True, 'category': 'unsupported',
               'error': 'Template does not support tools.'}, '', '', {'case': case, 'checks': []})
    store.finish(n, 'completed'); store.synced(n)
    sign_in(browser, 'jordan')
    return n


def test_tool_pages_show_actual_errors_category_metrics_and_unsupported(desk, tmp_path):
    browser, _, _ = desk
    n = tool_page(browser, tmp_path)
    page = browser.get('/benchmarks/run/'+n)
    assert page.status_code == 200
    for text in ('Tool results by category', 'Tool conversation and actual responses', 'temporarily_unavailable',
                 'Unsupported', 'unsupported', 'Template does not support tools.', 'exact native turn'):
        assert text in page.text
    assert '<script>unsafe()</script>' not in page.text and '&lt;script&gt;unsafe()&lt;/script&gt;' in page.text
    manage = browser.get('/benchmarks/manage?benchmark=tool_recovery')
    assert 'Tasks completed' in manage.text and 'Recovery after injected errors' in manage.text
    assert 'Correct argument objects' in manage.text


def test_tool_evidence_in_desktop_and_mobile_chromium(desk, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    server, _, _ = desk
    n = tool_page(server, tmp_path)
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def serve(route):
            from front_helpers import serve_browser_request
            serve_browser_request(server,'http://tools.test',route)

        context.route('http://tools.test/**', serve)
        for viewport in ({'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}):
            page.set_viewport_size(viewport)
            page.goto('http://tools.test/benchmarks/run/'+n)
            page.locator('a[href="#sample-1"]').click()
            page.get_by_role('button', name='Tool conversation and actual responses', exact=True).click()
            playwright.expect(page.locator('#sample-1')).to_contain_text('temporarily_unavailable')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.goto('http://tools.test/benchmarks/manage?benchmark=tool_use')
            playwright.expect(page.locator('[name=formula]')).to_have_value('task_completion')
            assert 'Arguments valid against function schema' in page.content()
            assert 'No-call cases given calls' in page.content()
        assert not errors
        browser.close()


@pytest.mark.parametrize('spec', [None, {}, {'messages': [], 'tools': []},
    {'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'file:///private'}}]}], 'tools': []},
    {'messages': [{'role': 'tool', 'content': 'hi'}], 'tools': []},
    {'messages': [{'role': 'user', 'content': 'hi'}], 'tools': [], 'url': 'http://other'}])
def test_tool_probe_validator_rejects_media_and_overrides(spec):
    from loadcoach.probes import Probes
    with pytest.raises(ValueError):
        Probes.validate_dialogue(spec)
