"""Image jobs finish together while distinct resident runtimes can run concurrently."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from loadcoach.batches import Batches
from loadcoach.groups import Group
from loadcoach.log import Log
from loadcoach.users import Users
from mirrorwall.front import Front
from mirrorwall.store import Store
from turnstile.batches import FrontBatches, Pins


@asynccontextmanager
async def executor(tmp_path, loaded=()):
    source = str(tmp_path/'accounts.db')
    users = Users(source, {'members': Group(can=frozenset({'unlogged'}))})
    for name in ('alice', 'bob'):
        users.add(name, 'members')
    store, log = Batches(source), Log(source)
    catalogue = [{'id': name, 'model_id': 'id-'+name, 'owned_by': 'loadcoach',
                  'locations': [{'machine': 'node', 'id': name+'-local', 'enabled': True}]}
                 for name in ('blue', 'red')]
    status = {'loaded': [{'machine': 'node', 'model': name+'-local', 'state': 'ready'} for name in loaded]}
    events, gates, attempts, visibility = asyncio.Queue(), {}, [], {}

    async def transport(request):
        if request.url.path == '/loadcoach/status':
            assert request.headers['x-turnstile-no-wake'] == '1'
        owner = request.headers['authorization'].removeprefix('Bearer internal-')
        visible = [row for row in catalogue if owner not in visibility or row['id'] in visibility[owner]]
        return httpx.Response(200, json={'data': visible} if request.url.path == '/v1/models' else status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url='http://front') as door:
        front = FrontBatches(store, log, None, door, lambda name: 'internal-'+name)
        images = Front(Store(store.db, tmp_path/'images'), front, users)
        front.image_work = images
        store.models = Pins({row['id']: row['model_id'] for row in catalogue})

        async def execute(number, line, body, user, headers, **kwargs):
            key = (number, line['line'])
            attempts.append(key)
            event = gates.setdefault(key, asyncio.Event())
            await events.put(key)
            await event.wait()
            return httpx.Response(200, json={'data': []})

        images.execute_line = execute

        async def available(user):
            return catalogue

        async def unlogged(user):
            return True

        images.catalogue = available
        front.can_unlogged = unlogged

        def create(model='blue', count=2, owner='alice'):
            upload = store.put_file(owner, 'images.jsonl', 'batch', b'')
            batch = store.start(owner, '/v1/images/generations', upload['id'],
                [{'custom_id': str(i), 'body': {'model': model, 'prompt': 'A landscape', 'width': 256, 'height': 256}}
                 for i in range(count)])
            images.store.bind_job(batch['id'], users.by_name(owner).identity, 'id-'+model, model, {})
            return batch['id']

        async def started():
            return await asyncio.wait_for(events.get(), 3)

        async def settled():
            await asyncio.sleep(.02)

        try:
            yield SimpleNamespace(front=front, images=images, users=users, store=store, catalogue=catalogue,
                                  status=status, gates=gates, attempts=attempts, visibility=visibility, create=create,
                                  started=started, settled=settled)
        finally:
            tasks = list(set(front.carrying) | set(front.working.values()))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (users.db, log.db, store.db):
                connection.close()


def test_same_model_finishes_each_job_fifo_across_owners_without_spending_queued_retries(tmp_path):
    async def run():
        async with executor(tmp_path, ['blue']) as h:
            jobs = [h.create(owner=owner) for owner in ('alice', 'bob', 'alice')]
            for job in jobs:
                h.front.work(job, h.users.by_name)
            original = h.front.working[jobs[0]]
            h.front.work(jobs[0], h.users.by_name)
            assert h.front.working[jobs[0]] is original
            assert await h.started() == (jobs[0], 0)
            await h.settled()
            assert set(h.front.image_waiting) == set(jobs[1:])
            for job in jobs[1:]:
                assert h.store.db.execute('SELECT sum(tries) FROM batch_lines WHERE batch=?', (job,)).fetchone()[0] == 0
            for index, job in enumerate(jobs):
                if index:
                    assert await h.started() == (job, 0)
                h.gates[job, 0].set()
                assert await h.started() == (job, 1)
                h.gates[job, 1].set()
            await asyncio.gather(*h.front.working.values())
            assert h.attempts == [(job, line) for job in jobs for line in range(2)]
            assert all(h.store.get(job)['status'] == 'completed' for job in jobs)
            assert not h.front.image_waiting and not h.front.image_active
    asyncio.run(run())


@pytest.mark.parametrize('residency', ['distinct', 'cold', 'loading', 'shared', 'disabled', 'unavailable', 'renamed', 'scoped'])
def test_parallel_jobs_require_distinct_ready_resident_models(tmp_path, residency):
    async def run():
        async with executor(tmp_path, ['blue', 'red']) as h:
            parallel = residency in ('distinct', 'renamed', 'scoped')
            if residency == 'cold':
                h.status['loaded'].pop()
            elif residency == 'loading':
                h.status['loaded'][1]['state'] = 'loading'
            elif residency == 'shared':
                h.status['loaded'] = [{**h.status['loaded'][0], 'answers': ['red-local']}]
            elif residency == 'disabled':
                h.catalogue[1]['locations'][0]['enabled'] = False
            elif residency == 'unavailable':
                h.status['loaded'] = []
            elif residency == 'renamed':
                h.catalogue[1]['id'] = 'red-renamed'
            elif residency == 'scoped':
                h.visibility.update(alice={'blue'}, bob={'red'})
            first, second = h.create('blue', 1), h.create('red', 1, owner='bob')
            h.front.work(first, h.users.by_name)
            assert await h.started() == (first, 0)
            h.front.work(second, h.users.by_name)
            await h.settled()
            if parallel:
                assert await h.started() == (second, 0)
                assert set(h.front.image_active) == {first, second}
            else:
                assert h.attempts == [(first, 0)]
            h.gates[first, 0].set()
            if not parallel:
                assert await h.started() == (second, 0)
            h.gates[second, 0].set()
            await asyncio.gather(*h.front.working.values())
    asyncio.run(run())


def test_failures_and_capacity_retries_do_not_rotate_to_another_job(tmp_path):
    async def run():
        async with executor(tmp_path, ['blue']) as h:
            first, second = h.create(), h.create(count=1)
            attempts = []

            async def execute(number, line, *args):
                attempts.append((number, line['line']))
                await asyncio.sleep(0)
                if number == first and line['line'] == 0:
                    return httpx.Response(400, json={'error': {'message': 'Invalid image settings.'}})
                if len(attempts) == 2:
                    return httpx.Response(503, headers={'retry-after': '0'})
                if len(attempts) == 3:
                    raise httpx.ReadError('Connection unavailable')
                return httpx.Response(200, json={'data': []})

            async def retry():
                await asyncio.sleep(0)

            h.images.execute_line = execute
            h.front.retry_delay = retry
            for job in (first, second):
                h.front.work(job, h.users.by_name)
            await asyncio.gather(*h.front.working.values())
            assert attempts == [(first, 0), (first, 1), (first, 1), (first, 1), (second, 0)]
            assert h.store.get(first)['request_counts'] == {'total': 2, 'completed': 1, 'failed': 1}
            assert h.store.db.execute('SELECT tries FROM batch_lines WHERE batch=? AND line=1', (first,)).fetchone()[0] == 1
    asyncio.run(run())


def test_pause_cancel_queued_and_resume_release_the_job_turn(tmp_path):
    async def run():
        async with executor(tmp_path, ['blue']) as h:
            first, cancelled, next_job = h.create(), h.create(), h.create(count=1)
            for job in (first, cancelled, next_job):
                h.front.work(job, h.users.by_name)
            assert await h.started() == (first, 0)
            await h.settled()
            h.store.cancelling(cancelled)
            h.front.working[cancelled].cancel()
            await h.front.working[cancelled]
            assert h.store.get(cancelled)['status'] == 'cancelled'
            with h.store.db:
                h.store.db.execute("UPDATE batches SET status='paused' WHERE id=?", (first,))
            h.gates[first, 0].set()
            assert await h.started() == (next_job, 0)
            await h.front.working[first]
            assert h.store.get(first)['request_counts']['completed'] == 1
            with h.store.db:
                h.store.db.execute("UPDATE batches SET status='in_progress' WHERE id=?", (first,))
            h.front.work(first, h.users.by_name)
            await h.settled()
            assert first in h.front.image_waiting
            h.gates[next_job, 0].set()
            assert await h.started() == (first, 1)
            h.gates[first, 1].set()
            await asyncio.gather(*h.front.working.values())
            assert h.attempts == [(first, 0), (next_job, 0), (first, 1)]
    asyncio.run(run())


def test_shutdown_resumes_oldest_unfinished_job_before_other_jobs(tmp_path):
    async def run():
        async with executor(tmp_path, ['blue']) as h:
            first, second = h.create(), h.create(count=1)
            for job in (first, second):
                h.front.work(job, h.users.by_name)
            assert await h.started() == (first, 0)
            h.gates[first, 0].set()
            assert await h.started() == (first, 1)
            tasks = list(h.front.working.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert not h.front.image_active and not h.front.image_waiting
            assert h.store.get(first)['status'] == h.store.get(second)['status'] == 'in_progress'
            h.front.resume(h.users.by_name)
            assert await h.started() == (first, 1)
            h.gates[first, 1].set()
            assert await h.started() == (second, 0)
            h.gates[second, 0].set()
            await asyncio.gather(*h.front.working.values())
            assert h.attempts == [(first, 0), (first, 1), (first, 1), (second, 0)]
    asyncio.run(run())


def test_temporary_jobs_share_ordering_and_queued_progress_without_sql_history(tmp_path):
    async def run():
        async with executor(tmp_path, ['blue']) as h:
            first = h.create(count=1)
            h.front.work(first, h.users.by_name)
            assert await h.started() == (first, 0)
            body = json.dumps({'temporary': True, 'request': {'model': 'blue', 'prompt': 'A landscape',
                                                          'width': 256, 'height': 256, 'n': 2}}).encode()

            async def receive():
                return {'type': 'http.request', 'body': body}

            request = Request({'type': 'http', 'method': 'POST', 'path': '/mirrorwall/jobs', 'headers': []}, receive)
            response = await h.images.start(request, h.users.by_name('alice'))
            assert response.status_code == 201, response.body
            quiet = json.loads(response.body)['id']
            await h.settled()
            assert quiet in h.front.image_waiting and h.attempts == [(first, 0)]
            state = json.loads((await h.images.state(request, h.users.by_name('alice'))).body)
            assert next(row for row in state['jobs'] if row['id'] == quiet)['progress']['stage'] == 'queued'
            assert h.store.db.execute('SELECT count(*) FROM batches').fetchone()[0] == 1
            h.gates[first, 0].set()
            assert await h.started() == (quiet, 0)
            h.gates[quiet, 0].set()
            assert await h.started() == (quiet, 1)
            h.gates[quiet, 1].set()
            await h.images.transient[quiet]['task']
            assert h.images.transient[quiet]['status'] == 'completed'
            assert h.store.db.execute('SELECT count(*) FROM batches').fetchone()[0] == 1
    asyncio.run(run())
