"""Private credentials and normal app identities: permissions, failure recovery and privacy."""
import json
import stat

import pytest
from starlette.testclient import TestClient
from loadcoach.groups import Group
from loadcoach.settings import Changes
from loadcoach.users import Users
from front_helpers import turnstile_to
from test_weightroom import GROUPS, sign_in
from test_voice_admin import voice_panel
from voice.telephone_keys import TelephoneKeys


def test_keys_write_only_modes_empty_keeps_and_atomic_failure(tmp_path, monkeypatch):
    keys = TelephoneKeys(tmp_path / 'keys')
    keys.save('auth-secret', 'SK' + 'b' * 32, 'api-secret')
    assert stat.S_IMODE(keys.path('token').stat().st_mode) == 0o600
    assert stat.S_IMODE(keys.path('api').stat().st_mode) == 0o600
    assert stat.S_IMODE(keys.folder.stat().st_mode) == 0o700
    assert keys.status()['token']['saved'] and keys.status()['token']['changed']
    assert 'secret' not in json.dumps(keys.status())
    keys.save()
    assert keys.read('token') == 'auth-secret'
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr('voice.telephone_keys.os.replace', fail)
    with pytest.raises(OSError):
        keys.save('replacement')
    assert keys.read('token') == 'auth-secret'
    assert not list(keys.folder.glob('.telephone-*'))
    keys.remove('api')
    assert not keys.status()['api']['saved']


@pytest.mark.parametrize('values', [dict(token='contains space'), dict(api_sid='bad', api_secret='secret'),
    dict(api_sid='SK' + 'a' * 32), dict(api_secret='secret'), dict(token=42), dict(token='x' * 8193)])
def test_credentials_validate_before_writing(tmp_path, values):
    keys = TelephoneKeys(tmp_path)
    keys.save('original')
    with pytest.raises(ValueError):
        keys.save(**values)
    assert keys.read('token') == 'original'


def test_front_telephone_management_accounts_rotation_revocation_and_privacy(tmp_path, capsys, monkeypatch):
    users = Users(str(tmp_path / 'accounts.db'), {**GROUPS, 'app': Group(can=frozenset({'load'}))})
    users.add('owner', 'admin'); users.add('ordinary', 'plain')
    owner, ordinary = users.new_key('owner'), users.new_key('ordinary')
    app = turnstile_to(tmp_path, users, {})
    headers = {'authorization': 'Bearer ' + owner}
    with TestClient(app, base_url='http://front:8442') as client:
        assert client.get('/loadcoach/telephone').status_code == 401
        assert client.post('/loadcoach/telephone', json={'action': 'save_credentials'}, headers={'authorization': 'Bearer ' + ordinary}).status_code == 403
        saved = client.post('/loadcoach/telephone', json={'action': 'save_credentials', 'token': 'unprinted-secret'}, headers=headers)
        assert saved.status_code == 200 and 'unprinted-secret' not in saved.text
        keys = TelephoneKeys(tmp_path)
        assert keys.read('token') == 'unprinted-secret'
        created = client.post('/loadcoach/telephone', json={'action': 'create_account', 'label': 'support'}, headers=headers)
        assert created.json() == {'account': 'voice-phone-support', 'url': '/users/voice-phone-support'}
        first = (tmp_path / 'voice-phone-support.key').read_text().strip()
        assert users.by_key(first).name == 'voice-phone-support'
        assert users.by_key(first).can('front-voice')
        assert client.post('/loadcoach/telephone', json={'action': 'create_account', 'label': 'support'}, headers=headers).status_code == 200
        assert (tmp_path / 'voice-phone-support.key').read_text().strip() == first
        users.change('voice-phone-support', own={'add': ['front-voice'], 'models': ['small'], 'max_requests': 2})
        with monkeypatch.context() as patch:
            def fail(*args):
                raise OSError('disk unavailable')
            patch.setattr('turnstile.telephone.atomic_secret', fail)
            assert client.post('/loadcoach/telephone', json={'action': 'rotate_account', 'label': 'support'}, headers=headers).status_code == 503
        assert users.by_key(first) is not None
        assert users.db.execute('SELECT count(*) FROM keys WHERE user=?', ('voice-phone-support',)).fetchone()[0] == 1
        rotated = client.post('/loadcoach/telephone', json={'action': 'rotate_account', 'label': 'support'}, headers=headers)
        assert rotated.status_code == 200
        second = (tmp_path / 'voice-phone-support.key').read_text().strip()
        assert second != first and users.by_key(first) is None
        assert users.by_key(second).group.models == ('small',) and users.by_key(second).group.max_requests == 2
        assert first not in rotated.text and second not in rotated.text
        assert stat.S_IMODE((tmp_path / 'voice-phone-support.key').stat().st_mode) == 0o600
        assert client.post('/loadcoach/telephone', json={'action': 'remove_credential', 'kind': 'token'}, headers=headers).status_code == 200
        assert not keys.read('token')
        assert not Changes(tmp_path / 'data/accounts.db').of('voice-gateway')['telephone']['enabled']
        store = Changes(tmp_path / 'data/accounts.db')
        assert 'unprinted-secret' not in '\n'.join(store.db.iterdump())
        users.change('owner', enabled=False)
        assert client.get('/loadcoach/telephone', headers=headers).status_code == 401
    assert 'unprinted-secret' not in capsys.readouterr().out


def test_phone_permission_and_write_only_forms_in_browser(tmp_path):
    from playwright.sync_api import sync_playwright, expect
    app, changes, original, calls, users, switch = voice_panel(tmp_path)
    with TestClient(app) as server, sync_playwright() as engine:
        sign_in(server, 'pat')
        assert server.get('/system/voice/telephone').status_code == 403
        assert server.post('/system/voice/telephone/save_credentials', data={'token': 'never-forwarded'}).status_code == 403
        sign_in(server, 'jordan')
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        def serve(route):
            if route.request.url.split('?', 1)[0] == 'http://voice.test/strip/stream':
                from front_helpers import serve_browser_request
                return serve_browser_request(server, 'http://voice.test', route)
            request = route.request
            response = server.request(request.method, request.url.replace('http://voice.test', ''),
                content=request.post_data_buffer, headers={'content-type': request.headers.get('content-type', ''),
                    'HX-Request': request.headers.get('hx-request', 'false')})
            route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'text/html'), body=response.content)
        context.route('http://voice.test/**', serve)
        page = context.new_page(); errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://voice.test/system/voice/telephone')
        expect(page.get_by_role('heading', name='Telephone', exact=True)).to_be_visible()
        assert page.locator('#telephone-token').input_value() == ''
        assert page.locator('#telephone-token').get_attribute('type') == 'password'
        page.fill('#telephone-token', 'browser-auth-secret')
        page.get_by_role('button', name='Save credentials', exact=True).click()
        expect(page.locator('.said')).to_contain_text('Saved.')
        assert page.locator('#telephone-token').input_value() == ''
        sent = next(call for call in calls if call.url.path == '/admin/telephone' and call.method == 'POST')
        assert json.loads(sent.content)['token'] == 'browser-auth-secret'
        assert 'browser-auth-secret' not in page.content()
        for width in (390, 320):
            page.set_viewport_size({'width': width, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://voice.test/users/pat')
        assert 'call in by telephone as yourself' in page.content()
        page.goto('http://voice.test/groups/plain')
        assert 'call in by telephone as yourself' in page.content()
        assert not errors
        browser.close()


def test_gateway_credentials_route_rechecks_permission_and_stays_private(tmp_path):
    import httpx
    from voice.gateway import build_app
    key = tmp_path / 'gateway.key'; key.write_text('gateway-key')
    calls = []
    def answer(request):
        calls.append(request.url.path)
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200, json={'can': ['manage-settings'] if request.headers['authorization'] == 'Bearer operator' else []})
        assert request.url.path == '/loadcoach/telephone'
        return httpx.Response(200, json={'credentials': {'token': {'saved': True}}})
    app = build_app(dict(key_file=str(key), turnstile='http://turnstile:8442', voice='http://compute'),
        httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    with TestClient(app, base_url='http://private:8446') as client:
        assert client.post('/admin/telephone', headers={'authorization': 'Bearer ordinary'}, json={'token': 'secret'}).status_code == 403
        assert '/loadcoach/telephone' not in calls
        assert client.post('/admin/telephone', headers={'authorization': 'Bearer operator'}, json={'token': 'secret'}).status_code == 200
        calls.clear()
        assert client.post('http://public:8081/admin/telephone', headers={'authorization': 'Bearer operator'}, json={'token': 'secret'}).status_code == 404
        assert not calls
