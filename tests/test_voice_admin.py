"""Voice management uses existing identities and the exact durable playback cache."""

import base64
import json
from copy import deepcopy
from pathlib import Path
import tomllib

import httpx
import pytest
from starlette.testclient import TestClient

from front_helpers import Signed, caller, door, machine_key, Later, turnstile_to
from loadcoach.settings import Changes, said
from test_voice_prompt_recordings import bundle
from test_weightroom import GROUPS, Switch, sign_in
from loadcoach.app import build_app as build_loadcoach
from loadcoach.config import Config
from loadcoach.users import Users
from voice import prompt_recordings as prompts
from voice.gateway import build_app as gateway
from voice.service import build_app as service
from weightroom.app import Settings, build_app


def test_admin_synthesis_and_forced_regeneration_use_edited_text_and_no_model_hold(tmp_path):
    key = tmp_path / 'voice.key'; key.write_text('service-key')
    synthesis = dict(model='/models/kokoro-multi-lang-v1_0/model.onnx', voices='/models/kokoro-multi-lang-v1_0/voices.bin')
    settings = dict(key_file=str(key), compute='http://turnstile', machine_key_file=machine_key(tmp_path),
                    profiles={'default': dict(model='small', speaker=3)}, synthesis=synthesis,
                    greetings={'ready': 'Welcome. Ask me anything.'})
    class Speech:
        ready_since = None
        calls = []
        async def warm(self):
            self.ready_since = 1
        async def say(self, text, speaker):
            self.calls.append((text, speaker))
            return b'\x10\x27' * 240
        def close(self):
            pass
    speech = Speech()
    def never(request):
        raise AssertionError('Administrative synthesis must not request a model or hold')
    admin, ordinary = Signed(caller('admin', ['manage-settings'])), Signed(caller('ordinary', ['front-voice']))
    with TestClient(service(settings, speech, httpx.AsyncClient(transport=httpx.MockTransport(never)))) as client:
        assert client.post('/voice/preview', json={'speaker': 3, 'text': 'Hello'}, auth=ordinary).status_code == 403
        for invalid in [[], {'speaker': -1, 'text': 'x'}, {'speaker': 1000, 'text': 'x'}, {'speaker': True, 'text': 'x'},
                        {'speaker': 3, 'text': ''}, {'speaker': 3, 'text': 'x' * 301}]:
            assert client.post('/voice/preview', json=invalid, auth=admin).status_code == 400
        assert not speech.calls
        preview = client.post('/voice/preview', json={'speaker': 20, 'text': 'Hello'}, auth=admin)
        assert preview.status_code == 200 and preview.content.startswith(b'RIFF')
        assert speech.calls == [('Hello', 20)] and settings['profiles']['default']['speaker'] == 3
        response = client.post('/voice/prompts', auth=admin)
        assert response.status_code == 200 and response.json()['texts']['ready'] == 'Welcome. Ask me anything.'
        assert ('Welcome. Ask me anything.', 3) in speech.calls
        count = len(speech.calls)
        assert client.get('/voice/prompts', auth=admin, headers={'if-none-match': response.headers['etag']}).status_code == 304
        assert len(speech.calls) == count
        assert client.post('/voice/prompts', auth=ordinary).status_code == 403
        assert client.post('/voice/prompts', auth=admin).status_code == 200
        assert len(speech.calls) == count + len(prompts.TEXTS)
        settings['greetings']['ready'] = 'Changed again.'
        changed = client.get('/voice/prompts', auth=admin)
        assert changed.headers['etag'] != response.headers['etag']
        assert changed.json()['texts']['ready'] == 'Changed again.'


def test_private_gateway_admin_refreshes_actual_front_cache_and_respects_revocation(tmp_path):
    key = tmp_path / 'gateway.key'; key.write_text('gateway-key')
    cache = prompts.PromptCache(tmp_path / 'recordings.json'); cache.install(bundle())
    requests, revoked = [], []
    def respond(request):
        requests.append(request)
        assert request.headers['x-turnstile-no-wake'] == '1'
        if request.url.path == '/loadcoach/me':
            token = request.headers['authorization']
            return httpx.Response(200, json={'can': ['manage-settings'] if token == 'Bearer admin' and not revoked else []})
        if request.method == 'POST' and request.url.path == '/voice/prompts':
            result = bundle(3); result['texts'] = {**prompts.TEXTS, 'ready': 'Welcome.'}
            return httpx.Response(200, json=result)
        if request.url.path == '/voice/prompts':
            return httpx.Response(304)
        if request.url.path == '/voice/ready':
            raise httpx.ConnectError('compute is off')
        if request.url.path == '/loadcoach/status':
            return httpx.Response(503, json={})
        if request.url.path == '/voice/preview':
            assert request.method == 'POST' and request.headers['authorization'] == 'Bearer admin'
            return httpx.Response(200, content=prompts.wav(b'\x10\x27' * 240), headers={'content-type': 'audio/wav'})
        raise AssertionError(request.url)
    settings = dict(key_file=str(key), turnstile='http://front:8442', voice='http://compute:8445',
                    voice_turnstile='http://front:8445', desktop_port=8447, public_port=8081)
    app = gateway(settings, httpx.AsyncClient(transport=httpx.MockTransport(respond)), prompt_cache=cache)
    with TestClient(app, base_url='http://private:8446') as client:
        assert client.get('/admin/status').status_code == 401
        assert client.get('/admin/status', headers={'authorization': 'Bearer ordinary'}).status_code == 403
        headers = {'authorization': 'Bearer admin'}
        status = client.get('/admin/status', headers=headers).json()
        assert status['recordings']['speaker'] == 26
        assert status['latency']['count'] == 0
        app.state.latency.update('private-session',1,'desktop','direct',dict(basis='input_end_received',end_to_audio_seconds=.25))
        assert client.get('/admin/status',headers=headers).json()['latency']['recent'][0]['values']['end_to_audio_seconds'] == .25
        assert any(row['name'] == 'Compute voice' and row['state'] == 'Unavailable' for row in status['services'])
        assert client.get('/admin/recordings?name=ready', headers=headers).content.startswith(b'RIFF')
        assert client.get('/admin/recordings?name=../../secret', headers=headers).status_code == 404
        result = client.post('/admin/regenerate', headers=headers)
        assert result.status_code == 200 and cache.bundle['speaker'] == 3
        assert prompts.PromptCache(cache.path).bundle['speaker'] == 3
        assert client.get('/admin/status', headers=headers).json()['recordings']['texts']['ready'] == 'Welcome.'
        assert client.post('/admin/preview', headers=headers, json={'speaker': 3, 'text': 'Hi'}).content.startswith(b'RIFF')
        revoked.append(True)
        assert client.get('/admin/recordings?name=ready', headers=headers).status_code == 403
        assert client.post('/admin/regenerate', headers=headers).status_code == 403
        for port in (8447, 8081):
            assert client.get(f'http://private:{port}/admin/status', headers=headers).status_code == 404
            assert client.post(f'http://private:{port}/admin/regenerate', headers=headers).status_code == 404


def voice_panel(tmp_path, **more):
    users = Users(str(tmp_path / 'accounts.db'), GROUPS)
    for name, group in [('jordan', 'admin'), ('pat', 'plain'), ('panel', 'front')]:
        users.add(name, group)
        users.change(name, password='correct horse')
    front = Later()
    switch = Switch(build_loadcoach(Config(database=str(tmp_path / 'loadcoach.db'), nvidia_smi='no-such-program',
        machine_key_file=machine_key(tmp_path), front='http://front:8442'), front=door(front, 8442)))
    switch.front = front.app = turnstile_to(tmp_path, users, {8442: switch})
    original = tomllib.loads(Path('settings/defaults/voice.toml').read_text())
    original['synthesis'] = dict(model='/models/kokoro-multi-lang-v1_0/model.onnx', voices='/models/kokoro-multi-lang-v1_0/voices.bin')
    changes = Changes(str(tmp_path / 'loadcoach.db'))
    changes.seen('voice', said(original, original, {}))
    changes.seen('voice-gateway', said(tomllib.loads(Path('settings/defaults/voice-gateway.toml').read_text()), {}, {}))
    calls = []
    from voice.latency import Recent
    timings = Recent()
    timings.update('safe-session',1,'desktop','direct',dict(basis='input_end_received',state='completed',
        model='<script>private()</script>',profile='default',revision='r1',end_to_audio_seconds=.25,final_to_audio_seconds=.15))
    def respond(request):
        calls.append(request)
        if request.url.path == '/admin/status':
            return httpx.Response(200, json=dict(services=[dict(name='Voice gateway', state='Available', address='front')],
                active=0, profile='default', modes=['direct'], readiness={'profiles': {}}, latency=timings.snapshot(),
                recordings=dict(source='Cached on front', speaker=3, texts=prompts.TEXTS)))
        if request.url.path == '/admin/regenerate':
            return httpx.Response(200, json={'saved': True})
        if request.url.path == '/browser/sessions':
            return httpx.Response(200, json={'session_id': 'ab12', 'cookie': 'gateway-cookie'})
        if request.url.path == '/browser/config':
            return httpx.Response(200, json={'modes': ['direct'], 'voices': []})
        return httpx.Response(200, content=prompts.wav(b'\x10\x27' * 240), headers={'content-type': 'audio/wav'})
    app = build_app(Settings(compute='http://compute', key=users.new_key('panel'), data=str(tmp_path / 'voice-panel'), **more),
                    door(switch.front, 8442), voice_client=httpx.AsyncClient(base_url='http://gateway', transport=httpx.MockTransport(respond)))
    return app, changes, original, calls, users, switch


def test_voice_pages_permissions_configuration_download_and_previews(tmp_path):
    app, changes, original, calls, users, switch = voice_panel(tmp_path)
    with TestClient(app) as client:
        sign_in(client, 'pat')
        assert client.get('/voice').status_code == 403
        assert client.get('/system/voice').status_code == 403
        assert client.post('/system/voice/regenerate').status_code == 403
        assert not calls
        users.change('pat', own={'add': ['front-voice']})
        chat = client.get('/voice')
        assert chat.status_code == 200 and 'Start conversation' in chat.text and 'data-base="/voice"' in chat.text
        assert 'ts.net' not in chat.text and client.get('/voice/static/voice.js').status_code == 200
        assert client.get('/voice/browser/config').json()['modes'] == ['direct']
        started = client.post('/voice/browser/sessions', json={'conversation_mode': 'direct'})
        assert started.json() == {'session_id': 'ab12'} and 'gateway-cookie' not in started.text + started.headers.get('set-cookie', '')
        reserved = next(call for call in calls if call.url.path == '/browser/sessions')
        assert reserved.headers['authorization'].startswith('Bearer ') and json.loads(reserved.content) == {'conversation_mode': 'direct'}
        assert client.post('/voice/browser/sessions', json={}, headers={'origin': 'https://elsewhere.test'}).status_code == 403
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):  # a call nobody reserved is refused before the gateway is asked
            with client.websocket_connect('/voice/browser/sessions/unknown/stream'):
                pass
        calls.clear()
        download = client.get('/voice/desktop')
        assert download.status_code == 200 and 'Download Voice app' in download.text
        assert 'href="/static/LoadCoach-Voice.zip"' in download.text
        assert "the transcript is sent to Hermes" in download.text and "Hermes voice ability" not in download.text
        sign_in(client, 'jordan')
        page = client.get('/system/voice')
        assert page.status_code == 200 and 'Voice settings' in page.text
        assert 'Preview a voice' in page.text and 'af_heart' in page.text
        assert page.text.count('src="/system/voice/recordings?name=') == len(prompts.TEXTS)
        assert 'name="value-greetings.ready"' in page.text
        for method, path in [('get', '/system/voice/recordings?name=ready'), ('post', '/system/voice/preview')]:
            result = getattr(client, method)(path, **({'data': {'speaker': '3', 'text': 'Hello'}} if method == 'post' else {}))
            assert result.status_code == 200 and result.content.startswith(b'RIFF')
        assert client.post('/system/voice/regenerate').url.path == '/system/voice'
        saved = client.post('/settings/voice', data={'return_to': 'voice', 'value-greetings.ready': 'Hello again.'})
        assert saved.url.path == '/system/voice' and changes.of('voice')['greetings']['ready'] == 'Hello again.'
        before_calls = sum(request.url.path == '/admin/regenerate' for request in calls)
        waiting = client.post('/system/voice/regenerate')
        assert waiting.status_code == 400 and 'not reported the saved' in waiting.text
        assert sum(request.url.path == '/admin/regenerate' for request in calls) == before_calls
        before = deepcopy(changes.of('voice'))
        bad = client.post('/settings/voice', data={'return_to': 'voice', 'value-greetings.ready': ''})
        assert bad.status_code == 400 and '1 to 300' in bad.text
        assert changes.of('voice') == before
        reset = client.post('/settings/voice', data={'return_to': 'voice', 'reset': 'greetings.ready'})
        assert reset.status_code == 200 and changes.of('voice') == {}
        # CSRF does not reach the gateway.
        before = len(calls)
        assert client.post('/system/voice/regenerate', headers={'origin': 'https://other.test'}).status_code == 403
        assert len(calls) == before
        # Compute outage still leaves the gateway and cached recordings visible.
        switch.off = True
        offline = client.get('/system/voice')
        assert offline.status_code == 200 and 'Node unavailable: compute' in offline.text and 'Recorded greetings' in offline.text


def test_voice_admin_browser_preview_edit_and_mobile_layout(tmp_path):
    from playwright.sync_api import sync_playwright, expect
    app, changes, original, calls, users, switch = voice_panel(tmp_path)
    with TestClient(app) as server, sync_playwright() as engine:
        sign_in(server, 'jordan')
        browser = engine.chromium.launch(headless=True, args=["--disable-gpu"])
        context = browser.new_context(viewport={'width': 1440, 'height': 1100})
        errors = []
        page = context.new_page()
        page.on('pageerror', lambda problem: errors.append(str(problem)))
        def serve(route):
            if route.request.url.split('?', 1)[0] == 'http://voice.test/strip/stream':
                from front_helpers import serve_browser_request
                return serve_browser_request(server, 'http://voice.test', route)
            request = route.request
            headers = {'HX-Request': request.headers.get('hx-request', 'false')}
            if request.headers.get('content-type'):
                headers['content-type'] = request.headers['content-type']
            response = server.request(request.method, request.url.replace('http://voice.test', ''),
                                      content=request.post_data_buffer, headers=headers)
            route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'text/html'), body=response.content)
        context.route('http://voice.test/**', serve)
        page.goto('http://voice.test/system/voice/status')
        assert page.get_by_role('table',name='Voice latency percentiles').is_visible()
        assert '250.0' in page.locator('#voice-latency').inner_text()
        assert page.locator('#voice-latency script').count() == 0
        page.goto('http://voice.test/system/voice')
        page.select_option('#preview-speaker', '20')
        page.fill('#preview-text', 'A different preview.')
        page.get_by_role('button', name='Generate preview', exact=True).click()
        expect(page.locator('[data-preview-audio]')).to_be_visible()
        assert any(request.url.path == '/admin/preview' and b'A different preview.' in request.content for request in calls)
        speech = page.locator('[data-card-key="settings:Speech"]')
        if not speech.get_attribute('open') == '':
            speech.locator(':scope > summary').click()
        page.locator('[name="value-greetings.ready"]').fill('Welcome from the browser.')
        page.get_by_role('button', name='Save speech', exact=True).click()
        expect(page.locator('.said')).to_contain_text('Saved.')
        assert changes.of('voice')['greetings']['ready'] == 'Welcome from the browser.'
        images = Path('data/voice-admin-previews'); images.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(images / 'desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(images / 'mobile.png'), full_page=True)
        page.set_viewport_size({'width': 320, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        users.change('jordan', own={'add': ['front-voice']})
        page.goto('http://voice.test/voice/desktop')
        expect(page.get_by_role('link', name='Download Voice app', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        browser.close()


def test_voice_page_carries_the_call_audio_to_the_gateway_and_back(tmp_path):
    """The browser's socket ends here; the gateway's is opened with the cookie it gave, and frames pass both ways."""
    from plugin_helpers import serving
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    async def stream(socket):
        if socket.path_params['sid'] != 'ab12' or socket.cookies.get('voice_ab12') != 'gateway-cookie':
            await socket.close(code=1008)
            return
        await socket.accept()
        await socket.send_text('{"type": "session.ready"}')
        await socket.send_bytes(b'\x01\x02' + await socket.receive_bytes())

    with serving(Starlette(routes=[WebSocketRoute('/browser/sessions/{sid}/stream', stream)])) as gateway_url:
        app, *_, users, switch = voice_panel(tmp_path, voice_gateway=gateway_url)
        users.change('pat', own={'add': ['front-voice']})
        with TestClient(app) as client:
            sign_in(client, 'pat')
            started = client.post('/voice/browser/sessions', json={})
            assert started.status_code == 200, started.text
            sid = started.json()['session_id']
            with client.websocket_connect(f'/voice/browser/sessions/{sid}/stream') as socket:
                assert socket.receive_json() == {'type': 'session.ready'}
                socket.send_bytes(b'pcm')
                assert socket.receive_bytes() == b'\x01\x02pcm'
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect):  # one reservation is one call
                with client.websocket_connect(f'/voice/browser/sessions/{sid}/stream'):
                    pass
