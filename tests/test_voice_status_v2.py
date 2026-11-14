"""Observed listener facts, private call controls and bounded privacy-safe activity."""
import asyncio
from contextlib import asynccontextmanager
import json
import socket

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from voice.gateway import build_app
from voice.gateway_status import Window, PhoneActivity, bound
from voice.telephone import signature
from test_voice_telephone import phone


def test_exact_sliding_phone_counts_and_ring_bounds(monkeypatch):
    now = [100000.0]
    monkeypatch.setattr('voice.gateway_status.time.time', lambda: now[0])
    window = Window(); window.record('accepted'); window.record('rejected')
    now[0] += 3599
    assert window.snapshot()['hour'] == {'accepted': 1, 'rejected': 1}
    now[0] += 1
    assert window.snapshot()['hour'] == {'accepted': 0, 'rejected': 0}
    assert window.snapshot()['day'] == {'accepted': 1, 'rejected': 1}
    now[0] += 82800
    assert window.snapshot()['day'] == {'accepted': 0, 'rejected': 0}
    activity = PhoneActivity()
    for index in range(70):
        activity.record('number', {'at': now[0], 'outcome': 'rejected', 'reason': 'not_listed'})
    snapshot = activity.snapshot('number')
    assert len(snapshot['attempts']) == 50 and snapshot['day']['rejected'] == 70


def test_v2_status_reports_real_listener_facts_and_no_personal_numbers(tmp_path):
    key = tmp_path / 'gateway.key'; key.write_text('gateway-secret')
    token = tmp_path / 'token.key'; token.write_text('twilio-secret')
    listeners = []
    for _ in range(3):
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(2); listeners.append(listener)
    private, desktop, public = [listener.getsockname()[1] for listener in listeners]
    calls = []
    def upstream(request):
        calls.append(request)
        assert request.method == 'GET' and request.headers.get('x-turnstile-no-wake') == '1'
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200, json={'can': ['manage-settings']})
        if request.url.path == '/voice/ready':
            return httpx.Response(200, json={'profiles': {'default': {'can_start': True, 'recognition': True, 'synthesis': True, 'model': True, 'model_name': 'small', 'modes': ['direct']}}})
        if request.url.path == '/loadcoach/telephone':
            return httpx.Response(200, json={'number_facts': {'+15555550123': {'account_ready': True, 'models': None}}})
        assert request.url.path == '/loadcoach/status'
        return httpx.Response(503, json={})
    config = phone(); config['numbers'] = config['numbers'][:1]
    settings = dict(key_file=str(key), turnstile='http://turnstile:8442', voice='http://compute:8445',
        telephone_token_file=str(token), telephone=config, port=private, desktop_port=desktop, public_port=public)
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), listeners=listeners)
    try:
        with TestClient(app, base_url='http://private:' + str(private)) as client:
            auth = {'authorization': 'Bearer operator'}
            status = client.get('/admin/status', headers=auth)
            payload = status.json()
            assert set(payload['entries']) == {'browser', 'desktop', 'telephone', 'hermes'}
            assert payload['entries']['telephone']['can_start']
            assert payload['entries']['browser']['listening'] and payload['entries']['desktop']['listening']
            assert not payload['entries']['hermes']['configured']
            assert payload['version']['commit'] and payload['version']['started_at'] and payload['version']['uptime_seconds'] >= 0
            assert not payload['calls'] and payload['active'] == 0
            assert '+15555550123' not in status.text
            assert 'twilio-secret' not in status.text and 'gateway-secret' not in status.text
            for path in ('/admin/status', '/admin/telephone', '/admin/calls/abc/end', '/loadcoach/phone-numbers', '/loadcoach/telephone/admit'):
                assert client.get(f'http://public:{public}' + path, headers=auth).status_code == 404
                assert client.post(f'http://public:{public}' + path, headers=auth).status_code == 404
            assert client.post('/browser/sessions', json={}).status_code == 403
            payload = client.get('/admin/status', headers=auth).json()
            assert payload['entries']['browser']['last_rejection']['reason'] == 'unauthorized'
            listeners[2].close()
            payload = client.get('/admin/status', headers=auth).json()
            assert not payload['entries']['telephone']['can_start'] and not payload['telephone']['public_bound']
            assert 'not bound' in payload['entries']['telephone']['reason']
    finally:
        for listener in listeners:
            listener.close()


def test_end_call_uses_normal_cleanup_and_fresh_admin_permissions(tmp_path):
    key = tmp_path / 'gateway.key'; key.write_text('app-key')
    paths = []
    def upstream(request):
        paths.append((request.method, request.url.path))
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200, json={'name': 'caller', 'can': ['manage-settings'] if request.headers['authorization'] == 'Bearer operator' else ['front-voice']})
        if request.url.path == '/turnstile/ready':
            return httpx.Response(200, json={'state': 'ready'})
        if request.url.path == '/voice/sessions':
            return httpx.Response(200, json={'model': 'small', 'token': 'compute-token'})
        return httpx.Response(200, json={})
    class Compute:
        async def recv(self):
            return json.dumps({'type': 'session.ready'})
        async def send(self, value):
            pass
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield ''
    @asynccontextmanager
    async def connector(uri, **options):
        yield Compute()
    app = build_app(dict(key_file=str(key), turnstile='http://front:8442', voice='http://compute:8445',
        desktop_port=8447, prompts=str(tmp_path / 'no-prompts')), httpx.AsyncClient(transport=httpx.MockTransport(upstream)), connector)
    with TestClient(app, base_url='http://private:8446') as client:
        with client.websocket_connect('ws://private:8447/desktop/stream', headers={'authorization': 'Bearer caller'}) as ws:
            assert ws.receive_json()['state'] == 'checking_key'
            assert ws.receive_json()['type'] == 'session.ready'
            status = client.get('/admin/status', headers={'authorization': 'Bearer operator'}).json()
            assert len(status['calls']) == 1
            call = status['calls'][0]
            assert call['account'] == 'caller' and call['source'] == 'desktop' and call['model'] == 'small'
            assert not {'key', 'socket', 'task', 'text', 'audio'} & call.keys()
            endpoint = '/admin/calls/' + call['id'] + '/end'
            assert client.post(endpoint).status_code == 401
            assert client.post(endpoint, headers={'authorization': 'Bearer caller'}).status_code == 403
            assert client.post(endpoint, headers={'authorization': 'Bearer operator'}).status_code == 200
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert not app.state.live_calls
        assert any(method == 'DELETE' and path.startswith('/voice/sessions/') for method, path in paths)
        assert client.post(endpoint, headers={'authorization': 'Bearer operator'}).status_code == 404


@pytest.mark.parametrize('state,speech_ready,reason', [
    ({'loaded': []}, True, 'not loaded'),
    ({'loaded': [{'model': 'small', 'state': 'ready'}], 'draining': True}, True, 'draining'),
    ({'loaded': [{'model': 'small', 'state': 'ready'}], 'lock': {'user': 'owner'}}, True, 'locked'),
    ({'loaded': [{'model': 'small', 'state': 'ready'}]}, False, 'Speech recognition')])
def test_compute_readiness_names_each_unmet_step_without_warming(tmp_path, state, speech_ready, reason):
    from voice.service import build_app as service
    from test_voice import FakeSpeech
    from front_helpers import machine_key, Signed, caller
    key = tmp_path / 'voice.key'; key.write_text('service-key')
    asked = []
    class Speech(FakeSpeech):
        ready_since = 1 if speech_ready else None
        async def warm(self):
            raise AssertionError('Status must never warm speech')
    def upstream(request):
        asked.append(request)
        assert request.method == 'GET' and request.url.path == '/loadcoach/status'
        return httpx.Response(200, json=state)
    app = service(dict(key_file=str(key), machine_key_file=machine_key(tmp_path), compute='http://turnstile',
        profiles={'default': {'model': 'small'}}), Speech(), httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    with TestClient(app) as client:
        response = client.get('/voice/ready', auth=Signed(caller('operator', ['manage-settings'])))
        assert not response.json()['profiles']['default']['can_start']
        assert reason in response.json()['profiles']['default']['reason']
        assert len(asked) == 1
