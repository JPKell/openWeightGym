"""Public probes, route instructions and lossless legacy import without compute."""
import asyncio
import json
from urllib.parse import urlsplit

import httpx
from starlette.testclient import TestClient
from starlette.datastructures import QueryParams
from voice.gateway import build_app
from voice.telephone import signature, valid_stream
from voice.telephone_keys import TelephoneKeys
from voice.telephone_routes import probe_url, valid_probe, check_route, public_dns, detected_routes
from voice.telephone_settings import configuration
from test_voice_telephone import phone


def test_public_head_requires_fresh_exact_signature_and_never_allocates(tmp_path):
    key = tmp_path / 'gateway.key'; key.write_text('gateway')
    token = tmp_path / 'voice-twilio.key'; token.write_text('secret')
    def never(request):
        raise AssertionError('Probe must not contact any upstream')
    settings = dict(key_file=str(key), telephone=phone(), turnstile='http://front', voice='http://compute', public_port=8081)
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(never)))
    url = probe_url('https://phone.example.com/voice/inbound')
    headers = {'x-twilio-signature': signature('secret', url, {})}
    with TestClient(app, base_url='http://public:8081') as client:
        target = '/voice/inbound?' + urlsplit(url).query
        assert client.head(target, headers=headers).status_code == 204
        assert client.head(target).status_code == 403
        assert client.head(target+'&extra=1', headers=headers).status_code == 403
        old = probe_url('https://phone.example.com/voice/inbound', at=1)
        assert client.head('/voice/inbound?' + urlsplit(old).query, headers={'x-twilio-signature': signature('secret', old, {})}).status_code == 403
        assert not app.state.phone_calls
        for path in ('/admin/status', '/admin/telephone', '/browser/config', '/loadcoach/phone-numbers'):
            assert client.head(path, headers=headers).status_code == 404
        settings['telephone']['enabled'] = False
        assert client.head(target, headers=headers).status_code == 404


def test_legacy_import_keeps_file_references_and_exact_query_urls(tmp_path):
    token = tmp_path / 'custom/token'; token.parent.mkdir(); token.write_text('old-secret')
    key = tmp_path / 'custom/account'; key.write_text('old-key')
    legacy = dict(token_file=str(token), key_file=str(key), account_sid=phone()['account_sid'],
        webhook_url='https://phone.example.com/twilio/voice?tenant=one', stream_url='wss://phone.example.com/twilio/media?tenant=one')
    keys = TelephoneKeys(tmp_path / 'keys')
    keys.import_legacy(legacy, 'https://phone.example.com', 'jordan.phone')
    current = phone(); current['numbers'][0]['account'] = 'jordan.phone'
    result = configuration({'telephone': current}, keys)
    assert result['webhook_url'] == legacy['webhook_url'] and result['stream_url'] == legacy['stream_url']
    assert keys.account_path('jordan.phone') == key
    assert keys.path('token') == token
    assert not (keys.folder / 'voice-twilio.key').exists()
    assert (keys.folder / 'voice-twilio-source.json').stat().st_mode & 0o777 == 0o600
    assert 'old-secret' not in (keys.folder / 'voice-twilio-source.json').read_text()
    keys.save(token='rotated-secret')
    assert token.read_text().strip() == 'rotated-secret'
    url = probe_url(result['webhook_url'])
    assert valid_probe(result, 'rotated-secret', QueryParams(urlsplit(url).query), signature('rotated-secret', url, {}))
    stream_slash = 'wss://phone.example.com/twilio/media/?tenant=one'
    assert valid_stream('rotated-secret', result['stream_url'], signature('rotated-secret', stream_slash, {}))
    current['public_url'] = 'https://other.example.com'
    assert configuration({'telephone': current}, keys)['webhook_url'] == 'https://other.example.com/voice/inbound'


def test_route_probe_reports_http_failures_and_rejects_private_dns(tmp_path, monkeypatch):
    from voice import telephone_routes as routes
    token = tmp_path / 'token'; token.write_text('secret')
    async def safe(url): pass
    monkeypatch.setattr(routes, 'public_dns', safe)
    def response(request):
        assert request.method == 'HEAD'
        assert request.headers['x-twilio-signature'] == signature('secret', str(request.url), {})
        return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            return await check_route(dict(enabled=True, token_file=str(token), webhook_url='https://phone.example.com/voice/inbound'), client)
    result = asyncio.run(check())
    assert not result['passed'] and 'HTTP 302' in result['message']
    async def private():
        import pytest
        with pytest.raises(ValueError, match='private or loopback'):
            await public_dns('https://127.0.0.1')
    asyncio.run(private())
    monkeypatch.setattr(routes.shutil, 'which', lambda command: None)
    instructions = asyncio.run(detected_routes(9081))
    assert not instructions['tailscale'] and not instructions['cloudflared']
    assert '9081' in instructions['funnel_command'] and 'stream_timeout 1h' in instructions['caddy']
    assert 'proxy_set_header Upgrade' in instructions['nginx'] and 'http_status:404' in instructions['cloudflare']


def test_gateway_legacy_import_is_explicit_and_does_not_replace_existing_site_settings(tmp_path):
    key = tmp_path / 'gateway.key'; key.write_text('gateway')
    old = tmp_path / 'phone.key'; old.write_text('old-phone')
    token = tmp_path / 'old-token.key'; token.write_text('secret')
    saved = []
    def upstream(request):
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200, json={'name':'voice-phone', 'can':['manage-settings','front-voice']})
        if request.url.path == '/loadcoach/telephone':
            saved.append(json.loads(request.content)); return httpx.Response(200, json={'saved':True})
        raise AssertionError(request.url)
    settings = dict(key_file=str(key), turnstile='http://front', voice='http://compute',
        twilio=dict(token_file=str(token), key_file=str(old), account_sid=phone()['account_sid'],
        webhook_url='https://phone.example.com/twilio/voice?tenant=one', stream_url='wss://phone.example.com/twilio/media?tenant=one'))
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    with TestClient(app) as client:
        headers={'authorization':'Bearer owner'}
        assert client.post('/admin/telephone', json={'action':'save_credentials','token':'replacement'}, headers=headers).status_code == 409
        assert token.read_text() == 'secret' and old.read_text() == 'old-phone'
        assert client.post('/admin/telephone', json={'action':'import_legacy','number':'bad'}, headers=headers).status_code == 400
        assert not saved
        assert client.post('/admin/telephone', json={'action':'import_legacy','number':'+15555550123'}, headers=headers).status_code == 200
        number = saved[0]['telephone']['numbers'][0]
        assert number['callers']['mode'] == 'anyone' and number['account'] == 'voice-phone'
        settings['telephone'] = saved[0]['telephone']
        assert client.post('/admin/telephone', json={'action':'import_legacy','number':'+15555550123'}, headers=headers).status_code == 409
        assert token.read_text() == 'secret' and old.read_text() == 'old-phone'


def test_route_instructions_and_legacy_import_browser(tmp_path):
    from test_voice_admin import voice_panel
    from test_weightroom import sign_in
    from test_console_browser import browser_context, playwright
    data = {'credentials':{'token':{},'api':{}}, 'telephone':{}, 'legacy':True,
        'routes':asyncio.run(detected_routes(8081))}
    app, *_ = voice_panel(tmp_path, telephone_data=data)
    with TestClient(app) as server, playwright.sync_playwright() as engine:
        sign_in(server,'jordan'); browser,page=browser_context(engine,server)
        page.goto('http://console.test/system/voice/telephone')
        assert page.get_by_role('heading',name='Set in the machine file').is_visible()
        page.get_by_text('Tailscale Funnel',exact=True).click()
        assert page.get_by_text('tailscale funnel --bg http://127.0.0.1:8081',exact=True).is_visible()
        page.get_by_text('Cloudflare Tunnel',exact=True).click()
        assert 'http_status:404' in page.content()
        assert page.get_by_role('button',name='Check from outside').is_visible()
        page.set_viewport_size({'width':320,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
