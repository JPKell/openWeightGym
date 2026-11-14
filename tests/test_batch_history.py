"""Batch history links durable outcomes, lifecycle events and legacy references."""

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from loadcoach.batches import Batches, TRIES
from loadcoach.log import Log
from loadcoach.users import Users
from test_console_browser import browser_context, playwright
from test_weightroom import GROUPS, desk, sign_in  # noqa: F401
from turnstile.batches import FrontBatches


def make_batch(store, owner='pat', size=2):
    file = store.put_file(owner, 'input.jsonl', 'batch', b'{}\n')
    return store.start(owner, '/v1/chat/completions', file['id'], [
        {'custom_id': f'item-{i}', 'body': {'model': 'small', 'messages': [{'role': 'user', 'content': 'PRIVATE PROMPT'}]}}
        for i in range(size)], {'name': 'History check'})


def test_legacy_batch_and_task_trace_queries_are_exact_scoped_and_keep_evidence(tmp_path):
    log = Log(str(tmp_path / 'front.db'), keep_days=0)
    number = 'batch_legacy'
    started = log.add('batch', 'started', 'pat', batch=number, requests=2)
    task = log.add('task', 'created', 'pat', task=number)
    result = log.add('batch_request', 'failed', 'pat', trace=number, custom_id='item-1', status=400)
    log.add('batch', 'completed', 'pat', batch=number + '_other')
    log.add('batch', 'completed', 'jordan', batch=number)
    log.add('model', 'loaded', 'pat', batch=number)  # Arbitrary detail text is not a batch association.
    before = log.verify()
    found = log.read(user='pat', trace=number)
    assert [entry['id'] for entry in found] == [started, task, result]
    assert found[0]['trace'] is None and found[1]['trace'] is None
    assert [entry['id'] for entry in log.read(user='pat', trace=number, kind='batch')] == [started]
    assert len(log.read(user='jordan', trace=number)) == 1
    assert log.verify() == before and before[1] is None


@pytest.fixture
def worker(tmp_path):
    source = str(tmp_path / 'front.db')
    users = Users(source, GROUPS)
    users.add('pat', 'plain'); users.add('jordan', 'admin')
    log, store = Log(source, keep_days=0), Batches(source)
    return users, log, store


def test_worker_records_deduplicated_waits_and_each_saved_outcome(worker, monkeypatch):
    users, log, store = worker
    batch = make_batch(store)
    monkeypatch.setattr('turnstile.batches.rules.AGAIN_AFTER', .001)
    attempts = 0
    async def dispatch(request):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise httpx.ConnectError('unreachable')
        if attempts <= 4:
            return httpx.Response(503, json={}, headers={'retry-after': '1'})
        if attempts == 5:
            return httpx.Response(200, json={'answer': 'PRIVATE OUTPUT'}, headers={'x-loadcoach-request-id': '123'})
        return httpx.Response(404, json={'error': {'message': 'The recorded model does not exist.', 'code': 'model_not_found'}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch), base_url='http://front') as client:
            front = FrontBatches(store, log, None, client, lambda name: name)
            front.work(batch['id'], users.by_name)
            await asyncio.gather(*front.carrying)
    asyncio.run(run())
    rows = log.read(user='pat', trace=batch['id'])
    assert [row['what'] for row in rows if row['kind'] == 'batch'] == ['running', 'deferred', 'deferred', 'completed']
    outcomes = [row for row in rows if row['kind'] == 'batch_request']
    assert [(row['custom_id'], row['what'], row['status']) for row in outcomes] == [('item-0', 'done', 200), ('item-1', 'failed', 404)]
    assert outcomes[0]['inference_request_id'] == '123' and outcomes[0]['duration'] >= 0
    assert outcomes[1]['error'] == 'The recorded model does not exist.'
    assert 'PRIVATE PROMPT' not in json.dumps(rows) and 'PRIVATE OUTPUT' not in json.dumps(rows)
    assert rows[-1]['completed'] == 1 and rows[-1]['failed'] == 1
    assert log.verify()[1] is None


def test_resuming_an_exhausted_request_records_the_saved_failure_once(worker):
    users, log, store = worker
    batch = make_batch(store, size=1)
    for _ in range(TRIES):
        store.next_line(batch['id'])
    async def run():
        front = FrontBatches(store, log, None, None, lambda name: name)
        front.resume(users.by_name)
        await asyncio.gather(*front.carrying)
    asyncio.run(run())
    rows = log.read(trace=batch['id'])
    assert [row['what'] for row in rows] == ['resumed', 'running', 'failed', 'completed']
    assert rows[2]['code'] == 'cut_off' and rows[2]['custom_id'] == 'item-0'
    assert store.get(batch['id'])['request_counts']['failed'] == 1
    asyncio.run(run())
    assert len(log.read(trace=batch['id'])) == len(rows)


def test_administrator_cancellation_stays_in_the_batch_owners_history(worker):
    users, log, store = worker
    batch = make_batch(store)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/batches/' + batch['id'],
                       'headers': [], 'path_params': {'number': batch['id']}})
    front = FrontBatches(store, log, None, None, lambda name: name)
    response = asyncio.run(front.batch_one(request, users.by_name('jordan')))
    assert response.status_code == 200
    rows = log.read(user='pat', trace=batch['id'])
    assert [row['what'] for row in rows] == ['cancelling', 'cancelled']
    assert all(row['actor'] == 'jordan' for row in rows)
    assert not log.read(user='other', trace=batch['id'])


@pytest.mark.parametrize('problem, event', [(asyncio.CancelledError, 'interrupted'), (ValueError, 'failed')])
def test_worker_interruption_or_crash_keeps_progress_and_records_the_cause(worker, problem, event):
    users, log, store = worker
    batch = make_batch(store, size=1)
    async def dispatch(request):
        raise problem('test interruption')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch), base_url='http://front') as client:
            front = FrontBatches(store, log, None, client, lambda name: name)
            front.work(batch['id'], users.by_name)
            errors = await asyncio.gather(*front.carrying, return_exceptions=True)
            assert isinstance(errors[0], problem)
    asyncio.run(run())
    assert store.get(batch['id'])['status'] == 'in_progress'
    assert [row['what'] for row in log.read(trace=batch['id'])] == ['running', event]
    assert store.get(batch['id'])['request_counts']['completed'] == 0


def test_batch_history_link_shows_legacy_timeline_and_retained_summary(desk, tmp_path):
    server, _, _ = desk
    store = Batches(str(tmp_path / 'data/accounts.db'))
    log = Log(str(tmp_path / 'data/accounts.db'), keep_days=0)
    batch = make_batch(store)
    store.keep(batch['id'], 0, 200, '123', {'answer': 'PRIVATE OUTPUT'})
    store.keep(batch['id'], 1, 400, '', {'error': {'message': 'Invalid request'}})
    store.finish(batch['id'])
    started = log.add('batch', 'started', 'pat', batch=batch['id'], requests=2)
    log.add('task', 'created', 'pat', task=batch['id'])
    log.add('batch', 'completed', 'pat', batch=batch['id'], completed=1, failed=1, total=2)
    log.add('batch', 'completed', 'jordan', batch=batch['id'], error='PRIVATE OTHER OWNER')
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/batches')
        page.locator(f'tr[data-batch="{batch["id"]}"]').get_by_role('link', name='View history', exact=True).click()
        playwright.expect(page.locator('#trace-count')).to_have_text('1 trace · 0 untraced events')
        playwright.expect(page.locator('#timeline-events [data-event-id]')).to_have_count(3)
        assert 'PRIVATE OTHER OWNER' not in page.content() and 'PRIVATE OUTPUT' not in page.content()
        page.goto(f'http://console.test/log?trace={batch["id"]}&selected={started}')
        playwright.expect(page.locator('#timeline-note')).to_contain_text('linked by recorded batch ID')
        assert server.get(f'/log?selected={started}&export=json').json()['trace'] is None
        # Once events are no longer retained, show the saved record without inventing a timeline.
        with log.db:
            log.db.execute('DELETE FROM events')
        page.goto('http://console.test/log?trace=' + batch['id'])
        playwright.expect(page.locator('#batch-history-record')).to_contain_text('No event history is retained')
        playwright.expect(page.locator('#batch-history-record')).to_contain_text('1 successful')
        playwright.expect(page.locator('#batch-history-record a', has_text='Answers')).to_be_visible()
        for width in (1440, 390, 320):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        assert not errors
        browser.close()
    sign_in(server, 'jordan')
    assert 'Answers</a>' not in server.get('/log?complete=1&trace=' + batch['id']).text
    server.post('/sign-out'); sign_in(server, 'cleo')
    assert 'batch-history-record' not in server.get('/log?complete=1&trace=' + batch['id']).text
