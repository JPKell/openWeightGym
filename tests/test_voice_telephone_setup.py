"""Telephone setup round trips, persistent browser drafts and the real signed media path."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from voice.telephone_admin import edited, EMPTY, verdict
from voice.telephone_settings import validate
from voice.telephone_check import main as simulate
from voice.gateway import build_app
from test_voice_telephone import phone


def test_section_merges_and_safe_new_number_defaults():
    current = edited(EMPTY, {'action': 'save_setup', 'account_sid': 'AC' + 'a' * 32,
                             'public_url': 'https://voice.example.com', 'enabled': False})
    added = edited(current, {'action': 'add_number', 'number': '+15555550123', 'label': 'Office'})
    assert added['numbers'][0]['callers']['mode'] == 'allowlist'
    assert not added['numbers'][0]['enabled']
    assert not current['numbers']
    validate(added)
    assert edited(added, {'action': 'remove_number', 'number': '+15555550123'}) == current
    assert verdict(EMPTY, {})['state'] == 'Off'
    assert verdict({**current, 'enabled': True}, {})['reason'] == 'Save your Twilio auth token.'
    with pytest.raises(ValueError):
        edited(added, {'action': 'add_number', 'number': '+15555550123'})


def test_real_signed_simulation_for_two_numbers_uses_each_phone_account_and_profile(tmp_path):
    from plugin_helpers import serving
    key = tmp_path / 'gateway.key'; key.write_text('app-key')
    token = tmp_path / 'token.key'; token.write_text('twilio-secret')
    for account in ('voice-phone', 'voice-phone-support'):
        (tmp_path / (account + '.key')).write_text(account + '-key')
    sessions = []
    async def upstream(request):
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200, json={'name': 'phone', 'can': ['front-voice']})
        if request.url.path == '/turnstile/ready':
            return httpx.Response(200, json={'state': 'ready'})
        if request.method == 'DELETE':
            return httpx.Response(200, json={})
        if request.url.path == '/voice/sessions':
            sessions.append((request.headers['authorization'], json.loads(request.content)))
            return httpx.Response(200, json={'model': 'small', 'token': 'session-token'})
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
                await self.events.put(json.dumps({'type': 'playback.segment', 'turn': 1, 'segment': 1, 'samples': 2400, 'text': ''}))
                await self.events.put(b'\x10\x27' * 2400)
                await self.events.put(json.dumps({'type': 'turn.completed', 'turn': 1}))
        async def __aiter__(self):
            while True:
                yield await self.events.get()
    @asynccontextmanager
    async def connector(uri, **kwargs):
        yield Compute()
    config = phone(); config['numbers'][1]['mode'] = 'direct'
    settings = dict(key_file=str(key), telephone_token_file=str(token), telephone=config, turnstile='http://turnstile',
        voice='http://compute', voice_turnstile='http://front:8445', prompts=str(tmp_path / 'no-prompts'), max_active_sessions=3)
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), connector)
    with serving(app) as base:
        async def check():
            reports = []
            for number in config['numbers']:
                args = SimpleNamespace(token_file=token, account=config['account_sid'], public_url=config['public_url'], url=base,
                    to_number=number['number'], audio_pcm=b'\x10\x27' * 2400, audio=None, interrupt_audio=None,
                    signature_form='wss-slash', wait=0, echo=False, persist=False, reply_seconds=1)
                reports.append(await simulate(args))
            return reports
        reports = asyncio.run(check())
    assert all(all(stage['passed'] for stage in result['stages']) for result in reports)
    assert [entry[0] for entry in sessions] == ['Bearer voice-phone-key', 'Bearer voice-phone-support-key']
    assert [entry[1]['profile'] for entry in sessions] == ['default', 'support']
    assert all(result['recording'] == 'None' for result in reports)


def test_telephone_browser_number_switches_preserve_drafts_and_fit_palettes(tmp_path):
    from test_voice_admin import voice_panel
    from test_weightroom import sign_in
    from test_console_browser import browser_context, playwright
    config = phone()
    data = {'credentials': {'token': {'saved': True, 'changed': '2026-11-14T00:00:00Z'}, 'api': {'saved': False}},
        'telephone': config, 'accounts': ['voice-phone', 'voice-phone-support'], 'discovered': [],
        'choices_by_account': {'voice-phone': {'profiles': [{'id': 'default'}], 'voices': []},
                               'voice-phone-support': {'profiles': [{'id': 'support'}], 'voices': []}}}
    app, changes, original, calls, users, switch = voice_panel(tmp_path, telephone_data=data)
    with TestClient(app) as server, playwright.sync_playwright() as engine:
        sign_in(server, 'jordan')
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/system/voice/telephone')
        page.locator('#phone-1-greeting').fill('Unsaved office greeting')
        page.get_by_role('link', name='Support', exact=True).click()
        playwright.expect(page.locator('#phone-2-greeting')).to_be_visible()
        page.locator('#phone-2-greeting').fill('Unsaved support greeting')
        page.get_by_role('link', name='Office', exact=True).click()
        playwright.expect(page.locator('#phone-1-greeting')).to_have_value('Unsaved office greeting')
        assert page.locator('#phone-2-greeting').input_value() == 'Unsaved support greeting'
        assert '/voice/inbound' in page.content()
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            for width in (1440, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#phone-1-label').fill('Office edited')
        page.get_by_role('button', name='Save number', exact=True).first.click()
        playwright.expect(page.locator('.said')).to_contain_text('Saved.')
        sent = next(call for call in calls if call.url.path == '/admin/telephone' and call.method == 'POST')
        assert json.loads(sent.content)['label'] == 'Office edited'
        assert not errors
        browser.close()


def test_pending_listener_setting_keeps_public_route_isolation_until_restart(tmp_path):
    import threading
    from loadcoach.settings import with_changes
    key = tmp_path / 'gateway.key'; key.write_text('app-key')
    config = tmp_path / 'voice-gateway.toml'
    config.write_text('key_file = ' + json.dumps(str(key)) + '\npublic_port = 8081\nturnstile = "http://turnstile:8442"\nvoice = "http://compute:8445"\n')
    settings, following = with_changes('voice-gateway', config)
    reported = threading.Event()
    def upstream(request):
        if request.url.path.endswith('/changes'):
            return httpx.Response(200, json={'public_port': 9091, 'modes': ['direct', 'hermes']})
        if request.url.path.endswith('/seen'):
            report = json.loads(request.content)['told']
            assert report['public_port']['value'] == 8081
            reported.set()
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), following=following)
    with TestClient(app, base_url='http://public:8081') as client:
        assert reported.wait(2)
        assert settings['public_port'] == 8081
        assert settings['modes'] == ['direct', 'hermes']
        assert client.get('/admin/status', headers={'authorization': 'Bearer owner'}).status_code == 404
        assert client.get('/browser/config').status_code == 404


def test_front_setup_discovery_and_connect_round_trip_without_compute(tmp_path, monkeypatch):
    from loadcoach.groups import Group
    from loadcoach.users import Users
    from front_helpers import turnstile_to
    from test_weightroom import GROUPS
    from voice.twilio_api import TwilioAPI
    users = Users(str(tmp_path / 'accounts.db'), {**GROUPS, 'app': Group(can=frozenset({'load'}))})
    users.add('owner', 'admin')
    app = turnstile_to(tmp_path, users, {})
    headers = {'authorization': 'Bearer ' + users.new_key('owner')}
    calls = []
    async def numbers(api):
        return [{'sid': 'PN' + 'b' * 32, 'phone_number': '+15555550123', 'voice_url': '', 'voice_method': 'POST', 'capabilities': {'sms': True}}]
    async def connect(api, sid, webhook):
        calls.append((sid, webhook)); return {'connected': True}
    monkeypatch.setattr(TwilioAPI, 'numbers', numbers)
    monkeypatch.setattr(TwilioAPI, 'connect_number', connect)
    with TestClient(app, base_url='http://front:8442') as client:
        def post(**fields):
            return client.post('/loadcoach/telephone', json=fields, headers=headers)
        assert post(action='save_setup', account_sid='AC' + 'a' * 32, public_url='http://127.0.0.1', enabled=False).status_code == 400
        assert post(action='save_setup', account_sid='AC' + 'a' * 32, public_url='https://phone.example.com', enabled=False, public_port=8081).status_code == 200
        assert post(action='add_number', number='+15555550123', label='Office').status_code == 200
        assert post(action='create_account').status_code == 200
        assert post(action='save_number', number='+15555550123', enabled=True, greeting='Welcome.').status_code == 200
        assert post(action='save_setup', enabled=True).status_code == 200
        assert post(action='find_numbers').status_code == 200
        assert post(action='connect_number', sid='PN' + 'b' * 32).json()['connected']
        snapshot = client.get('/loadcoach/telephone', headers=headers).json()
        assert snapshot['telephone']['enabled']
        assert snapshot['telephone']['numbers'][0]['greeting'] == 'Welcome.'
        assert snapshot['discovered'][0]['voice_url'] == 'https://phone.example.com/voice/inbound'
        assert calls == [('PN' + 'b' * 32, 'https://phone.example.com/voice/inbound')]
        assert post(action='remove_number', number='+15555550123').status_code == 200
        assert not client.get('/loadcoach/telephone', headers=headers).json()['telephone']['numbers']
