"""SMS ownership proofs, personal call delegation, privacy and revocation."""
import asyncio
import json
import time
import pytest
from starlette.testclient import TestClient
from test_voice_admin import voice_panel
from test_voice_telephone import phone
from test_weightroom import sign_in
from voice.telephone_keys import TelephoneKeys
from voice.twilio_api import TwilioAPI
from turnstile.phone_store import PhoneStore


@pytest.fixture
def registration(tmp_path, monkeypatch):
    app, changes, original, calls, users, switch = voice_panel(tmp_path)
    users.change('pat', own={'add': ['front-phone'], 'models': ['small'], 'max_requests': 2})
    users.add('voice-gateway', 'front')
    users.change('voice-gateway', own={'add': ['app-sign-in', 'load', 'front-voice', 'front-phone'], 'models': ['small', 'agent']})
    config = phone(); config['numbers'] = config['numbers'][:1]
    config['numbers'][0]['callers'] = {'mode': 'allowlist', 'entries': []}
    front = switch.front
    front.state.console_settings.changes.put('voice-gateway', 'telephone', config, 'owner')
    TelephoneKeys(tmp_path).save('private-auth-token')
    sent = []
    async def numbers(api):
        return [{'sid': 'PN' + 'b' * 32, 'phone_number': '+15555550123', 'capabilities': {'sms': True},
                 'voice_url': 'https://phone.example.com/voice/inbound', 'voice_method': 'POST'}]
    async def sms(api, sender, number, code):
        sent.append((sender, number, code))
    monkeypatch.setattr(TwilioAPI, 'numbers', numbers)
    monkeypatch.setattr(TwilioAPI, 'send_code', sms)
    store = PhoneStore(front.state.users, tmp_path)
    client = TestClient(front, base_url='http://front:8442')
    headers = {'authorization': 'Bearer ' + users.new_key('pat')}
    yield app, client, headers, users, store, sent, config


def test_sms_ownership_flow_personal_session_limits_and_live_revocation(registration):
    app, client, headers, users, store, sent, config = registration
    def post(**body):
        return client.post('/loadcoach/phone-numbers', headers=headers, json=body)
    assert client.get('/loadcoach/phone-numbers', headers=headers).json()['available']
    assert post(action='send', number='+15555550999').status_code == 200
    code = sent[-1][2]
    pending = client.get('/loadcoach/phone-numbers', headers=headers)
    assert code not in pending.text and '+15555550999' in pending.text
    assert post(action='verify', number='+15555550999', code=code).status_code == 200
    assert store.registered('+15555550999').name == 'pat'
    gateway = {'authorization': 'Bearer ' + users.new_key('voice-gateway')}
    payload = {'number': '+15555550123', 'caller': '+15555550999', 'attestation': 'TN-Validation-Passed-A'}
    admitted = client.post('/loadcoach/telephone/admit', headers=gateway, json=payload)
    assert admitted.json()['accepted'] and admitted.json()['account'] == 'pat'
    key = admitted.json()['session_key']
    me = client.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}).json()
    assert me['name'] == 'pat' and me['via'] == 'voice-gateway'
    assert set(me['models']) <= {'small'} and me['max_requests'] == 2
    assert {'front-phone', 'front-voice'} <= set(me['can'])
    assert not {'sign-in', 'app-sign-in', 'manage-settings'} & set(me['can'])
    assert not client.post('/loadcoach/telephone/admit', headers=gateway, json={**payload, 'attestation': ''}).json()['accepted']
    users.change('pat', own={'add': [], 'models': ['small']})
    assert client.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}).status_code == 401
    assert not client.post('/loadcoach/telephone/admit', headers=gateway, json=payload).json()['accepted']
    users.change('pat', own={'add': ['front-phone'], 'models': ['small']})
    own = store.own(users.identity('pat'))
    assert post(action='remove', id=own[0]['id']).status_code == 200
    assert not store.own(users.identity('pat'))
    assert client.get('/loadcoach/me', headers={'authorization': 'Bearer ' + key}).status_code == 401
    assert not client.post('/loadcoach/telephone/admit', headers=gateway, json=payload).json()['accepted']


def test_sms_expiry_five_attempts_resend_and_duplicate_privacy(registration):
    app, client, headers, users, store, sent, config = registration
    owner = users.identity('pat')
    code = asyncio.run(store.start_verification(owner, '+15555550999'))
    with pytest.raises(ValueError, match='Wait|wait'):
        asyncio.run(store.start_verification(owner, '+15555550999'))
    for _ in range(5):
        with pytest.raises(ValueError, match='invalid or expired'):
            asyncio.run(store.verify(owner, '+15555550999', '999999' if code != '999999' else '888888'))
    with pytest.raises(ValueError, match='invalid or expired'):
        asyncio.run(store.verify(owner, '+15555550999', code))
    store.db.execute('UPDATE telephone_sms_attempts SET created=created-61'); store.db.commit()
    fresh = asyncio.run(store.start_verification(owner, '+15555550999'))
    store.db.execute('UPDATE telephone_verifications SET expires=?', (time.time()-1,)); store.db.commit()
    with pytest.raises(ValueError, match='invalid or expired'):
        asyncio.run(store.verify(owner, '+15555550999', fresh))
    store.db.execute('UPDATE telephone_sms_attempts SET created=created-61'); store.db.commit()
    fresh = asyncio.run(store.start_verification(owner, '+15555550999'))
    asyncio.run(store.verify(owner, '+15555550999', fresh))
    users.add('other', 'plain')
    store.db.execute('UPDATE telephone_sms_attempts SET created=created-61'); store.db.commit()
    with pytest.raises(ValueError, match='at most three'):
        asyncio.run(store.start_verification(owner, '+15555550888'))
    store.db.execute('DELETE FROM telephone_sms_attempts'); store.db.commit()
    with pytest.raises(ValueError, match='cannot be registered') as refused:
        asyncio.run(store.start_verification(users.identity('other'), '+15555550999'))
    assert 'pat' not in str(refused.value)


def test_three_number_limit_and_account_removal_erases_numbers(registration):
    app, client, headers, users, store, sent, config = registration
    owner = users.identity('pat')
    for index in range(3):
        code = asyncio.run(store.start_verification(owner, '+1555555099' + str(index)))
        asyncio.run(store.verify(owner, '+1555555099' + str(index), code))
        store.db.execute('UPDATE telephone_sms_attempts SET created=created-61'); store.db.commit()
    store.db.execute('DELETE FROM telephone_sms_attempts'); store.db.commit()
    with pytest.raises(ValueError, match='up to three'):
        asyncio.run(store.start_verification(owner, '+15555550888'))
    users.remove('pat')
    assert not store.own(owner)
    assert not store.db.execute('SELECT 1 FROM telephone_verifications WHERE owner=?', (owner,)).fetchone()


def test_registered_numbers_are_masked_to_admin_and_no_code_or_hash_is_exported(registration):
    app, client, headers, users, store, sent, config = registration
    code = asyncio.run(store.start_verification(users.identity('pat'), '+15555550999'))
    asyncio.run(store.verify(users.identity('pat'), '+15555550999', code))
    admin = {'authorization': 'Bearer ' + users.new_key('jordan')}
    response = client.get('/loadcoach/telephone', headers=admin)
    assert '+15555550999' not in response.text and '0999' in response.text
    assert code not in response.text and 'scrypt$' not in response.text
    record = client.get('/loadcoach/users/pat', headers=admin)
    assert '+15555550999' not in record.text and record.json()['phone_numbers'][0]['number'].endswith('0999')
    ident = store.own(users.identity('pat'))[0]['id']
    revoked = client.post('/loadcoach/telephone', headers=admin, json={'action': 'revoke_user_number', 'owner': 'pat', 'id': ident})
    assert revoked.status_code == 200 and not store.own(users.identity('pat'))


def test_phone_registration_browser_uses_sms_code_without_exposing_it(registration):
    from test_console_browser import browser_context, playwright
    app, client, headers, users, store, sent, config = registration
    with TestClient(app) as server, playwright.sync_playwright() as engine:
        sign_in(server, 'pat')
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/account')
        page.locator('#register-phone-number').fill('+15555550999')
        page.get_by_role('button', name='Send verification code', exact=True).click()
        playwright.expect(page.locator('#phone-verification-code')).to_be_visible()
        code = sent[-1][2]
        assert code not in page.content()
        page.locator('#phone-verification-code').fill(code)
        page.get_by_role('button', name='Verify number', exact=True).click()
        playwright.expect(page.locator('#phone-numbers')).to_contain_text('+15555550999')
        assert page.locator('#phone-verification-code').count() == 0
        for width in (390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()


def test_sms_unavailable_without_capable_connected_sender(registration, monkeypatch):
    app, client, headers, users, store, sent, config = registration
    async def no_sms(api):
        return [{'sid': 'PN' + 'b' * 32, 'phone_number': '+15555550123', 'capabilities': {'sms': False},
                 'voice_url': 'https://phone.example.com/voice/inbound', 'voice_method': 'POST'}]
    monkeypatch.setattr(TwilioAPI, 'numbers', no_sms)
    status = client.get('/loadcoach/phone-numbers', headers=headers).json()
    assert not status['available'] and 'supports SMS' in status['reason']
    refused = client.post('/loadcoach/phone-numbers', headers=headers, json={'action': 'send', 'number': '+15555550999'})
    assert refused.status_code == 400 and not sent


def test_real_signed_registered_call_forwards_model_requests_as_user(registration, tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import httpx
    from front_helpers import door
    from plugin_helpers import serving
    from voice.gateway import build_app
    from voice.telephone_check import main as simulate
    app, client, headers, users, store, sent, config = registration
    code = asyncio.run(store.start_verification(users.identity('pat'), '+15555550999'))
    asyncio.run(store.verify(users.identity('pat'), '+15555550999', code))
    key_file = tmp_path / 'gateway.key'; key_file.write_text(users.new_key('voice-gateway'))
    front_http = door(client.app, 8442)
    attributed = []
    async def upstream(request):
        if request.url.path in ('/loadcoach/telephone/admit', '/loadcoach/me', '/loadcoach/sign-out'):
            return await front_http.send(request)
        if request.url.path == '/turnstile/ready':
            return httpx.Response(200, json={'state': 'ready'})
        if request.method == 'DELETE':
            return httpx.Response(200, json={})
        who = await front_http.get('/loadcoach/me', headers={'authorization': request.headers['authorization']})
        assert who.status_code == 200 and who.json()['name'] == 'pat'
        attributed.append((request.url.path, who.json()['name']))
        if request.url.path == '/voice/sessions':
            return httpx.Response(200, json={'model': 'small', 'token': 'session-token'})
        assert request.url.path == '/v1/chat/completions'
        if json.loads(request.content).get('stream'):
            return httpx.Response(200, content='data: {"choices":[{"delta":{"content":"Hello."}}]}\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json={})
    class Compute:
        def __init__(self):
            self.events = asyncio.Queue(); self.started = False
        async def recv(self):
            return json.dumps({'type': 'session.ready'})
        async def send(self, value):
            if isinstance(value, bytes) and not self.started:
                self.started = True
                await asyncio.sleep(.2)
                await self.events.put(json.dumps({'type': 'turn.started', 'turn': 1}))
                await self.events.put(json.dumps({'type': 'model.request', 'turn': 1, 'body': {'model': 'small', 'stream': True, 'messages': [{'role': 'user', 'content': 'Hello'}]}}))
            elif isinstance(value, str) and json.loads(value).get('type') == 'agent.text.done':
                await self.events.put(json.dumps({'type': 'playback.segment', 'turn': 1, 'segment': 1, 'samples': 2400, 'text': ''}))
                await self.events.put(b'\x10\x27' * 2400)
                await self.events.put(json.dumps({'type': 'turn.completed', 'turn': 1}))
        async def __aiter__(self):
            while True:
                yield await self.events.get()
    @asynccontextmanager
    async def connector(uri, **kwargs):
        yield Compute()
    settings = dict(key_file=str(key_file), telephone_token_file=str(tmp_path / 'voice-twilio.key'), telephone=config,
        turnstile='http://front:8442', voice_turnstile='http://front:8445', voice='http://compute:8445', prompts=str(tmp_path / 'no-prompts'))
    gateway = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), connector)
    with serving(gateway) as base:
        args = SimpleNamespace(token_file=tmp_path / 'voice-twilio.key', account=config['account_sid'], public_url=config['public_url'],
            url=base, to_number='+15555550123', caller='+15555550999', audio_pcm=b'\x10\x27' * 2400,
            audio=None, interrupt_audio=None, signature_form='wss', wait=0, echo=False, persist=False, reply_seconds=1.5)
        report = asyncio.run(simulate(args))
    assert all(stage['passed'] for stage in report['stages'])
    assert attributed == [('/voice/sessions', 'pat'), ('/v1/chat/completions', 'pat'), ('/v1/chat/completions', 'pat')]
    assert not store.db.execute('SELECT 1 FROM telephone_sessions').fetchone()  # hang-up revokes the limited session
