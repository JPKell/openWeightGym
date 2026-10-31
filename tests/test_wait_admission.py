"""Wait estimates and refusals on isolated scheduler state and the ordinary openWeightGym API."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from loadcoach.config import Config, checked
from loadcoach.diagnostics import Timeline
from loadcoach.log import Log
from loadcoach.models import Models
from loadcoach.roles import pick
from loadcoach.runner import Lock, Refusal, Runner, Server, Ticket
from loadcoach.waiting import completion, duration, estimate, workload
from test_app import (add_user, fake_llama_server, gateway, in_an_empty_folder,  # noqa: F401
                      model, wait_for_status, _signed)
from test_postgresql import postgres  # noqa: F401


RATES = {'speed': {'one': 10, 'two': 20}, 'prompt_speed': {'one': 100, 'two': 200},
         'load': {'one': 5, 'two': 8}}
WORK = {'prompt_tokens': 100, 'output_tokens': 50}


def scheduler(placement=0, parallel=1, limit=0):
    models = Models(':memory:')
    for name in ('one', 'two'):
        models.put(name, model('/' + name + '.gguf', placement, parallel=parallel))
    runner = Runner(Config(gpus=('GPU0', 'GPU1'), max_estimated_wait=limit), None, models=models)
    runner.paces = lambda: RATES
    runner.seen = {'cards': {i: {'total': 16000, 'used': 0} for i in (0, 1)}, 'pids': {}}
    return runner


def loaded(runner, name='one', cards=()):
    server = Server(name, cards, runner.models.get(name), ready=True, mib=[1000] * len(cards))
    runner.servers[name] = server
    return server


def ticket(name='one', priority='normal', **more):
    return Ticket(name, 'user', 'here', priority, **{'work': WORK, **more})


def test_waits_serialize_inferred_work_without_discounting_unobserved_progress():
    runner = scheduler()
    server = loaded(runner)
    ahead = ticket(server=server, since=time.monotonic() - 100)
    runner.running.append(ahead)
    value = estimate(runner, ticket(), RATES)
    assert value['seconds'] == 6 and value['ahead_requests'] == 1 and value['load_seconds'] == 0
    own = completion(value, WORK, 10, 100)
    assert own['seconds'] == 12 and own['work_seconds'] == 6
    # A low-priority job still waiting cannot get ahead of this normal-priority request.
    runner.running.clear()
    runner.waiting.append(ticket('two', 'low'))
    assert estimate(runner, ticket(), RATES)['seconds'] == 0
    runner.waiting[0].priority = 'high'
    assert estimate(runner, ticket(), RATES)['seconds'] == 11  # three seconds work + eight load


def test_parallel_room_and_disjoint_cards_do_not_become_false_queue_work():
    runner = scheduler(parallel=2)
    server = loaded(runner)
    runner.running.append(ticket(server=server, work=None))
    assert estimate(runner, ticket(), {})['seconds'] == 0
    runner = scheduler(['GPU0'])
    first = loaded(runner, cards=('GPU0',))
    runner.models.put('two', model('/two.gguf', ['GPU1']))
    loaded(runner, 'two', ('GPU1',))
    runner.running.append(ticket(server=first))
    assert estimate(runner, ticket('two'), {})['seconds'] == 0


def test_unknown_rates_unbounded_work_and_protected_fit_are_never_zero_waits():
    runner = scheduler()
    assert estimate(runner, ticket(), {})['seconds'] is None  # unknown cold load
    server = loaded(runner)
    runner.running.append(ticket(server=server, work=None))
    assert estimate(runner, ticket(), RATES)['seconds'] is None
    runner.running.clear()
    runner.lock = Lock('secret', 'other', 'here', 600, False, False, until=time.monotonic()+600)
    assert estimate(runner, ticket(), RATES)['seconds'] is None
    assert estimate(runner, ticket(token='secret'), RATES)['seconds'] == 0
    runner = scheduler(['GPU0'])
    server = loaded(runner, 'two', ('GPU0',))
    runner.holds['two', 'voice'] = time.monotonic() + 600
    assert estimate(runner, ticket(), RATES)['seconds'] is None
    assert runner.pinned(server) and runner.holds


@pytest.mark.parametrize('limit, rates, refused', [(5, RATES, True), (6, RATES, False), (0, {}, False), (10, {}, False)])  # unknown rates: admitted under a limit
def test_admission_refuses_before_enqueue_and_disabled_preserves_queueing(limit, rates, refused):
    async def run():
        runner = scheduler(limit=limit)
        runner.paces = lambda: rates
        server = loaded(runner)
        runner.running.append(ticket(server=server))
        asking = ticket()
        if refused:
            with pytest.raises(Refusal) as problem:
                await runner.acquire(asking)
            assert problem.value.status == 503 and problem.value.retry_after
            assert asking not in runner.waiting and asking.server is None
        else:
            work = asyncio.create_task(runner.acquire(asking))
            await asyncio.sleep(0)
            assert asking in runner.waiting
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            assert asking not in runner.waiting
    asyncio.run(run())


@pytest.mark.parametrize('value', [-1, True, '5', float('nan'), float('inf'), 10**1000])
def test_invalid_global_wait_limit_is_rejected(value):
    with pytest.raises(SystemExit, match='max_estimated_wait'):
        checked(Config(max_estimated_wait=value), 'settings')


def test_workload_keeps_only_counts_and_handles_images_and_multiple_candidates():
    asked = {'messages': [{'role': 'user', 'content': 'PRIVATE QUESTION'}],
             'tools': [{'name': 'PRIVATE TOOL'}], 'max_tokens': 50, 'n': 2, 'best_of': 3}
    work = workload(asked)
    assert work['output_tokens'] == 150 and 'PRIVATE' not in json.dumps(work)
    assert duration(work, 10, 100) > 15
    assert workload({'messages': [{'content': [{'type': 'image_url', 'image_url': 'PRIVATE_IMAGE'}]}], 'max_tokens': 5})['prompt_tokens'] is None
    assert workload({'messages': [], 'max_tokens': -1})['output_tokens'] is None
    assert workload({'messages': [], 'max_tokens': 10, 'n': True})['output_tokens'] is None
    assert workload({'input': 'some text'})['output_tokens'] == 0


def test_shared_role_completion_estimates_include_actual_work_and_keep_reuse_weight():
    def candidate(name, wait, speed, resident):
        return {'name': name, 'score': 1, 'warm': resident, 'resident': resident, 'load': 5,
                'speed': speed, 'fast_loaded_factor': 2 if resident else 1,
                'estimate': completion({'seconds': wait}, WORK, speed, 100)}
    cold = candidate('cold', 2, 20, False)  # 5.5 seconds
    warm = candidate('warm', 0, 10, True)   # six seconds, weighted as three
    assert pick('fast', [cold, warm])[0] == 'warm'
    warm['estimate'] = completion({'seconds': 20}, WORK, 10, 100)
    assert pick('fast', [cold, warm])[0] == 'cold'
    cold['estimate']['seconds'] = None
    assert pick('fast', [cold, warm])[0] == 'warm'  # known beats a provisional fallback
    assert 'timing unavailable' in pick('fast', [cold])[1]


def test_native_prompt_rates_are_structured_and_portable_to_postgresql(postgres):  # noqa: F811
    timing = Timeline({}, None, False)
    timing.document({'choices': [{'message': {'content': '"prompt_per_second": 900000'}}]})
    assert 'prompt_tokens_per_second' not in timing.native
    timing.document({'timings': {'prompt_per_second': 100}})
    for source in (':memory:', postgres):
        log = Log(source)
        log.add('request', 'done', 'user', model='one', tokens_per_second=10,
                prompt_tokens_per_second=timing.native['prompt_tokens_per_second'])
        assert log.paces()['prompt_speed']['one'] == 100


def test_suite_returns_admission_headers_and_admits_an_unknown_wait_under_a_limit(fake_llama_server):
    with gateway(fake_llama_server, max_estimated_wait=5) as caller:
        owner = add_user(caller, 'owner', 'admin')
        # Never measured, so its wait cannot be estimated: admitted, and measured by answering, with the limit on.
        answer = caller.post('/v1/chat/completions', headers=owner,
                             json={'model': 'small', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 50})
        assert answer.status_code == 200 and answer.headers['x-loadcoach-estimated-wait'] == 'unknown'
        unbounded = caller.post('/v1/chat/completions', headers=owner, json={'model': 'small', 'messages': []})  # no max_tokens: unknown work
        assert unbounded.status_code == 200
    with gateway(fake_llama_server) as caller:
        answer = caller.post('/v1/chat/completions', json={'model': 'small'})
        assert answer.status_code == 200 and answer.headers['x-loadcoach-estimated-wait'] == 'unknown'
        again = caller.post('/v1/chat/completions', json={'model': 'small'})
        assert again.status_code == 200 and float(again.headers['x-loadcoach-estimated-wait']) == 0


def test_numeric_refusal_happens_at_arrival_and_settings_can_disable_it(fake_llama_server):
    with gateway(fake_llama_server, max_estimated_wait=1) as caller:
        owner = add_user(caller, 'owner', 'admin')
        caller.log.add('model', 'loaded', model='small', seconds=5)
        request = {'model': 'small', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 50}
        refused = caller.post('/v1/chat/completions', json=request, headers=owner)
        assert refused.status_code == 503 and refused.headers['x-loadcoach-estimated-wait'] == '5.0'
        assert refused.headers['retry-after'] == '5'
        assert not caller.get('/loadcoach/status', headers=owner).json()['loaded']
        assert caller.post('/loadcoach/settings/loadcoach', json={'max_estimated_wait': 0}, headers=owner).status_code == 200
        admitted = caller.post('/v1/chat/completions', json=request, headers=owner)
        assert admitted.status_code == 200 and admitted.headers['x-loadcoach-estimated-wait'] == '5.0'
        facts = caller.get('/loadcoach/requests/' + refused.headers['x-loadcoach-request-id'], headers=owner).json()
        assert facts['ended']['admission_estimate']['seconds'] == 5


def test_probe_cleanup_finishes_before_an_enabled_limit_estimates():
    from loadcoach.probes import Probes
    async def run():
        runner = scheduler(limit=10)
        lock = Lock('private', 'benchmarker', 'probe', 86400, False, True, until=time.monotonic()+86400)
        runner.lock = lock
        probes = Probes(runner, lambda: False, lambda _: True)
        cleaned = []
        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.01)
                cleaned.append(True)
        task = asyncio.create_task(work())
        probes.working['batch'] = task
        await asyncio.sleep(0)
        await probes.yield_to_foreground()
        assert cleaned and task.cancelled() and runner.holder() is None
        assert estimate(runner, ticket(), RATES)['seconds'] == 5
        runner.admitting = 1
        with pytest.raises(Refusal, match='Compute is busy'):
            await probes.probe(None, {}, {})
    asyncio.run(run())


def test_fast_role_uses_the_actual_request_budget_through_suite(fake_llama_server):
    with gateway(fake_llama_server, {name: model(name + '.gguf', 1) for name in ('one', 'two')}) as caller:
        owner = add_user(caller, 'owner', 'admin')
        for name, speed, prompt_speed in (('one', 1000, 10), ('two', 20, 1000)):
            caller.log.add('model', 'loaded', model=name, seconds=5)
            caller.log.add('request', 'done', model=name, tokens_per_second=speed, prompt_tokens_per_second=prompt_speed)
        assert caller.post('/loadcoach/roles/chat', json={'models': {'one': 1, 'two': 1}}, headers=owner).status_code == 201
        request = {'model': 'chat:fast', 'messages': [{'role': 'user', 'content': 'hello' * 60}]}
        short = caller.post('/v1/chat/completions', json={**request, 'max_tokens': 1}, headers=owner)
        long = caller.post('/v1/chat/completions', json={**request, 'max_tokens': 1000}, headers=owner)
        assert short.status_code == long.status_code == 200
        assert short.headers['x-loadcoach-picked'] == 'two' and long.headers['x-loadcoach-picked'] == 'one'
        assert 'queue, load, prompt and output budgets' in long.headers['x-loadcoach-because']


def test_running_work_is_counted_before_a_second_request_is_enqueued(fake_llama_server):
    with gateway(fake_llama_server, max_estimated_wait=2) as caller, ThreadPoolExecutor() as others:
        owner = add_user(caller, 'owner', 'admin')
        caller.log.add('model', 'loaded', model='small', seconds=.1)
        caller.log.add('request', 'done', model='small', tokens_per_second=10, prompt_tokens_per_second=100)
        body = {'model': 'small', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 50}
        first = others.submit(caller.post, '/v1/chat/completions', headers=owner, json={**body, 'sleep': .6})
        wait_for_status(_signed(caller, owner), lambda status: status['running'])
        second = caller.post('/v1/chat/completions', headers=owner, json=body)
        assert second.status_code == 503 and float(second.headers['x-loadcoach-estimated-wait']) > 5
        assert not caller.get('/loadcoach/status', headers=owner).json()['waiting']
        assert first.result(timeout=5).status_code == 200
        third = caller.post('/v1/chat/completions', headers=owner, json=body)
        assert third.status_code == 200 and float(third.headers['x-loadcoach-estimated-wait']) == 0
