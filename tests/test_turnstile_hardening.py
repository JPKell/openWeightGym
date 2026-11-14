"""Control node reports cannot block inference, and failed compute bodies have honest statuses."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from starlette.testclient import TestClient

from front_helpers import machine_key
from loadcoach import statement
from loadcoach.groups import Group
from loadcoach.log import Log
from loadcoach.users import Users
from test_app import add_user, fake_llama_server, gateway  # noqa: F401
from test_two_machines import Two, worker
from turnstile.app import Settings, build_app
from turnstile.reporting import ReportTimeout, reader, run
import turnstile.events as front_events


def test_departing_caller_cancels_compute_before_response_headers(tmp_path):
    """A canceled model hold must leave compute's queue rather than block later voice calls."""
    users = Users(str(tmp_path / 'accounts.db'), {'voice': Group(can=frozenset({'hold', 'load'}))})
    users.add('voice', 'voice')
    key = users.new_key('voice')

    async def exercise():
        entered, departed, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def upstream(request):
            if request.url.path != '/loadcoach/hold':
                return httpx.Response(200, json={})
            entered.set()
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.set()
            return httpx.Response(200, json={})
        app = build_app(Settings(compute='compute', data=str(tmp_path)),
                        httpx.AsyncClient(transport=httpx.MockTransport(upstream)), users=users)
        body = json.dumps({'model': 'small', 'seconds': 30}).encode()
        sent, consumed = [], False
        async def receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await departed.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
            'scheme': 'http', 'path': '/loadcoach/hold', 'raw_path': b'/loadcoach/hold', 'query_string': b'',
            'root_path': '', 'server': ('front', 8442), 'client': ('127.0.0.1', 1),
            'headers': [(b'host', b'front:8442'), (b'authorization', ('Bearer ' + key).encode())]}
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                departed.set()
                await asyncio.wait_for(task, 1)
                assert cancelled.is_set()
                assert any(message.get('status') == 499 for message in sent)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_report_reader_is_an_independent_read_only_connection(tmp_path):
    log = Log(str(tmp_path / 'events.db'))
    log.add('request', 'asked', 'pat', model='small')
    with reader(log, time.monotonic() + 2) as view:
        assert view.db.raw is not log.db.raw
        assert view.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        with pytest.raises(Exception):
            view.db.execute("INSERT INTO events (kind) VALUES ('changed')")
    assert log.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_slow_sql_is_interrupted_at_its_deadline(tmp_path):
    log = Log(str(tmp_path / 'events.db'))
    query = ('WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM numbers WHERE n<100000000) '
             'SELECT sum(n) FROM numbers')
    with pytest.raises(ReportTimeout):
        asyncio.run(run(log, lambda view: view.db.execute(query).fetchone(), limit=0.05))
    log.add('request', 'asked', 'pat', model='small')  # the writer remains usable after interruption


def test_home_reports_are_cached_but_usage_page_stays_exact(fake_llama_server):
    with gateway(fake_llama_server) as front:
        headers = add_user(front, 'pat', 'admin')
        other = add_user(front, 'other', 'admin', by=headers)
        home = {'summary': 'mine', 'period': '30', '_home': '1'}
        first = front.get('/loadcoach/usage', params=home, headers=headers)
        second = front.get('/loadcoach/usage', params=home, headers=headers)
        exact = front.get('/loadcoach/usage', params={'summary': 'mine', 'period': '30'}, headers=headers)
        assert (first.status_code, first.headers['x-turnstile-report-cache']) == (200, 'miss')
        assert (second.status_code, second.headers['x-turnstile-report-cache']) == (200, 'hit')
        assert first.json() == second.json() == exact.json()
        assert 'x-turnstile-report-cache' not in exact.headers
        assert front.get('/loadcoach/usage', params=home, headers=other).headers['x-turnstile-report-cache'] == 'miss'


def test_slow_usage_report_does_not_delay_a_model_request(fake_llama_server, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = front_events.usage_report

    def slow(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(front_events, 'usage_report', slow)
    with gateway(fake_llama_server) as front:
        headers = add_user(front, 'pat', 'admin')
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(front.get, '/loadcoach/usage', headers=headers)
            assert entered.wait(2)
            began = time.monotonic()
            answered = front.post('/v1/chat/completions', json={'model': 'small'}, headers=headers)
            elapsed = time.monotonic() - began
            release.set()
            assert answered.status_code == 200
            assert elapsed < 1.5  # the report was still blocked in its own thread
            assert waiting.result(timeout=3).status_code == 200


def test_model_timing_report_does_not_block_other_front_requests(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = Log.paces

    def slow(view):
        entered.set()
        assert release.wait(3)
        return original(view)

    monkeypatch.setattr(Log, 'paces', slow)
    primary_key = machine_key(tmp_path)
    users = Users(str(tmp_path / 'accounts.db'), {'plain': Group()})
    users.add('pat', 'plain')
    app = build_app(Settings(compute='compute', data=str(tmp_path), machine_key_file=primary_key), users=users)
    header, nonce = statement.make(statement.read_key(primary_key), 'GET', '/turnstile/log-facts', b'', machine='compute')

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://front:8442') as client:
            timing = asyncio.create_task(client.get('/turnstile/log-facts', headers={statement.HEADER: header}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                other = await asyncio.wait_for(client.get('/loadcoach/me'), timeout=1)
                assert other.status_code == 401
                assert not timing.done()
            finally:
                release.set()
            answer = await timing
            assert answer.status_code == 200
            assert answer.headers[statement.PROOF] == statement.proof(statement.read_key(primary_key), nonce)
            assert set(answer.json()) == {'speed', 'prompt_speed', 'load', 'last_used'}

    asyncio.run(exercise())
    app.state.log.db.close()
    users.db.close()


def test_node_body_drop_returns_retryable_503_before_output(tmp_path):
    users = Users(str(tmp_path / 'accounts.db'), {'plain': Group(can=frozenset({'load'}))})
    users.add('pat', 'plain')
    headers = {'authorization': 'Bearer ' + users.new_key('pat')}
    primary_key = machine_key(tmp_path)
    wire = Two(worker(primary_key, 'compute', False), worker(primary_key, 'rig', False))
    original = wire.handle_async_request

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.RemoteProtocolError('compute disconnected before its body')
            yield b''  # pragma: no cover

    async def broken(request):
        if request.url.path == '/v1/chat/completions':
            encoded = request.headers[statement.HEADER].partition('.')[0]
            import base64
            nonce = json.loads(base64.urlsafe_b64decode(encoded))['nonce']
            return httpx.Response(upstream_status, headers={statement.PROOF: statement.proof(statement.read_key(primary_key), nonce),
                                               'content-type': 'application/json'}, stream=Broken(), request=request)
        return await original(request)

    wire.handle_async_request = broken
    settings = Settings(compute='compute', data=str(tmp_path), machine_key_file=primary_key)
    with TestClient(build_app(settings, httpx.AsyncClient(transport=wire), users=users), base_url='http://front:8442') as front:
        for upstream_status in (200, 500):
            for streaming in (False, True):
                answer = front.post('/v1/chat/completions', headers=headers,
                                    json={'model': 'small', 'messages': [], 'stream': streaming})
                # Every node is a peer: a broken answer is retryable on another node, so it is a 503 with Retry-After.
                assert answer.status_code == 503 and answer.headers['retry-after']
                assert 'complete answer' in answer.json()['error']['message'] or 'sent an answer' in answer.json()['error']['message']
