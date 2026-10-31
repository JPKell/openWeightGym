"""Plugin management uses openWeightGym permissions; front chat persists native session mappings/events."""

import json
import shutil
import time

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from weightroom.conversations import build_app as chat_app
from plugin_helpers import setup, serving, turns, SOURCE
from plugins._turnstile import Router
from plugins._sdk import AdapterError
from test_weightroom import desk, desk_with_machine, sign_in  # noqa: F401
from front_helpers import door


def test_model_snapshot_refresh_is_passive_and_keeps_cache_during_sleep(tmp_path):
    import asyncio
    from plugins._auth import Authorization
    auth = Authorization(tmp_path / 'snapshot.json')
    snapshot = {'generated_at': time.time(), 'keys': [], 'models': [{'id': 'cached-model'}], 'unlogged': False}
    auth.replace(snapshot)

    async def asleep(request):
        assert request.headers['x-turnstile-no-wake'] == '1'
        return httpx.Response(503, json={'error': {'message': 'Compute is asleep.'}})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(asleep)) as client:
            assert not await auth.refresh(client, 'http://front/loadcoach/plugins/mirror', key='program-key')
    asyncio.run(check())
    assert auth.snapshot() == snapshot


def test_compute_mirror_lists_models_and_front_says_who_may_use_them(desk):
    """Compute's list for the plugin path holds models only. Keys, expiry and an app's limits are front's to apply."""
    import hashlib
    browser, switch, users = desk
    users.groups.put('adapter-app', {'can': ['app-sign-in', 'front-plugins'], 'models': ['hermes:gandalf']})
    users.add('adapter-app', 'adapter-app')
    app_session = users.new_key('jordan', hours=1, via='adapter-app')
    expired = users.new_key('jordan', hours=1)
    with users.db:
        users.db.execute('UPDATE keys SET expires=? WHERE hash=?', (time.time()-1, hashlib.sha256(expired.encode()).hexdigest()))
    own = users.new_key('jordan')
    panel = users.new_key('panel')
    async def mirror():
        async with door(switch.front, 8442) as client:
            assert (await client.get('/loadcoach/plugins/mirror', headers={'authorization': 'Bearer '+own})).status_code == 403
            response = await client.get('/loadcoach/plugins/mirror', headers={'authorization': 'Bearer '+panel})
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
            return response.json()
    # WeightRoom's polling already uses this app/connection on its portal loop.
    # A second event loop would share SQLite across threads unlike production.
    snapshot = browser.portal.call(mirror)
    assert snapshot['keys'] == [] and own not in json.dumps(snapshot) and 'password_hash' not in json.dumps(snapshot)
    via = users.by_key(app_session)
    assert via.group.can == {'front-plugins'} and via.group.models == ('hermes:gandalf',) and via.via == 'adapter-app'
    assert users.by_key(expired) is None
    users.change('adapter-app', enabled=False)
    assert users.by_key(app_session) is None  # the session ends with its app


def test_plugin_management_pages_permissions_pending_settings_and_private_download(desk):
    browser, _, _ = desk
    registry = browser.app.state.plugins
    shutil.copytree(SOURCE / 'hermes', registry.root / 'hermes', ignore=shutil.ignore_patterns('__pycache__', 'settings.json', '.settings.lock'))
    sign_in(browser, 'pat')
    assert browser.get('/plugins').status_code == 403
    sign_in(browser, 'jordan')
    assert 'Hermes Agent' in browser.get('/plugins').text
    assert 'Profiles' not in browser.get('/plugins/hermes/instances/new').text  # schema page renders before discovery
    assert browser.post('/plugins/hermes', data={'enabled': 'on'}).status_code == 200
    registry.save_instance('hermes', 'offline', 'http://127.0.0.1:1', {'cli': 'hermes'}, enabled=True)
    page = browser.get('/plugins/hermes/instances/offline')
    assert page.status_code == 200 and 'desired 1' in page.text and 'Pending' in page.text
    refused = browser.post('/plugins/hermes/instances/offline', data={'do': 'nothing-known'})  # a refused form stays on its page
    assert refused.url.path == '/plugins/hermes/instances/offline' and 'alert-danger' in refused.text and 'Unknown plugin action.' in refused.text
    # A save that is refused comes back as the same page with what was typed: text, the choice, and each box as it was left.
    typed = {'do': 'save', 'url': 'http://typed.example:9', 'health_interval': 'abc', 'sensitivity': 'sensitive', 'setting-cli': 'typed-cli', 'health-allow_start': 'on'}
    refused = browser.post('/plugins/hermes/instances/offline', data=typed)
    assert refused.status_code == 400 and 'Health interval must be an integer.' in refused.text and refused.headers['cache-control'] == 'no-store'
    assert all(kept in refused.text for kept in ('value="http://typed.example:9"', 'value="typed-cli"', 'value="abc"', '<option selected>sensitive', 'name="health-allow_start" checked'))
    assert 'name="enabled">' in refused.text  # it was ticked when saved, and sent unticked
    assert registry.instance('hermes', 'offline')['url'] == 'http://127.0.0.1:1'  # nothing was saved
    refused = browser.post('/plugins/hermes/instances/new', data={**typed, 'instance': 'typedname'})
    assert refused.status_code == 400 and 'value="typedname"' in refused.text and 'value="http://typed.example:9"' in refused.text
    download = browser.get('/plugins/hermes/instances/offline/installer')
    assert download.status_code == 200 and download.headers['cache-control'] == 'no-store'
    assert browser.post('/plugins/hermes', data={'enabled': 'on'}, headers={'origin': 'http://evil.example'}).status_code == 403
    sign_in(browser, 'pat')
    assert browser.get('/plugins/hermes/instances/offline/installer').status_code == 403


def test_adapter_models_remain_in_web_chat_when_compute_service_is_off(desk_with_machine):
    import hashlib
    browser, compute, machine, users, _ = desk_with_machine
    registry = browser.app.state.plugins
    shutil.copytree(SOURCE / 'hermes', registry.root / 'hermes', ignore=shutil.ignore_patterns('__pycache__', 'settings.json', '.settings.lock'))
    registry.save_instance('hermes', 'offline', 'http://127.0.0.1:1', {}, enabled=True)
    registry.enable('hermes', True)
    registry.inventory('hermes', 'offline', [{'id': 'gandalf'}])
    registry.model('hermes', 'offline', 'gandalf', 'hermes:gandalf', True)
    sign_in(browser, 'jordan')
    key = browser.cookies['weightroom']
    user = users.by_key(key)
    registry.auth.replace({'generated_at': time.time(), 'keys': [{'hash': hashlib.sha256(key.encode()).hexdigest(), 'expires': user.until,
        'user': {'name': user.name, 'identity': users.identity(user.name), 'can': sorted(user.group.can), 'model_access': '*', 'models': [], 'roles': []}}], 'models': []})
    compute.off = True
    for spotter_off in (False, True):
        machine.off = spotter_off
        chat = browser.get('/chat').text
        assert 'hermes:gandalf' in chat and '· Plugin' in chat and 'Plugin conversation' in chat
        assert 'data-plugin-model="true"' in chat and 'Adaptor conversation' not in chat
        page = browser.get('/models')
        assert page.status_code == 200 and 'hermes:gandalf' in page.text and 'Adaptor' in page.text


def test_front_chat_session_mapping_final_answer_events_and_edit_reset(tmp_path):
    registry, cli, adapter, _ = setup(tmp_path)
    user = {'name': 'alice', 'identity': 'person-one', 'can': ['front-plugins'], 'model_access': '*', 'models': [], 'roles': [], 'chat': {'temporary': False}, 'max_sensitivity': 'normal'}
    def identify(authorization):  # turnstile's, from the accounts on front
        if authorization != 'Bearer user-key':
            raise AdapterError('Not authorized.', 401, 'invalid_api_key')
        return user
    async def compute_off(*_):
        return None  # compute is not answering: the plugin path does not need it
    async def me(request):  # turnstile answers this from the accounts, whether or not compute answers
        return httpx.Response(200, json=user) if request.headers.get('authorization') == 'Bearer user-key' else httpx.Response(401)
    compute = httpx.AsyncClient(base_url='http://compute', transport=httpx.MockTransport(me))
    with serving(adapter) as address:
        registry.save_instance('hermes', 'local', address, registry.instance('hermes', 'local')['settings'], enabled=True)
        registry.inventory('hermes', 'local', [{'id': 'gandalf'}])
        registry.model('hermes', 'local', 'gandalf', 'hermes:gandalf', True)
        adapters = httpx.AsyncClient(timeout=None, limits=httpx.Limits(max_keepalive_connections=0))
        router = Router(registry, adapters, identify, compute_off)
        async def routed(request):
            return await router.intercept(request)
        turnstile = Starlette(routes=[Route('/{path:path}', routed, methods=['GET', 'POST'])])
        with serving(turnstile) as turnstile_url:
            inference = httpx.AsyncClient(base_url=turnstile_url, timeout=None, limits=httpx.Limits(max_keepalive_connections=0))
            app = chat_app(str(tmp_path / 'chat.db'), compute, inference, plugins=registry)
            headers = {'authorization': 'Bearer user-key'}
            with TestClient(app) as caller:
                start = caller.post('/loadcoach/conversations', headers=headers, json={'model': 'hermes:gandalf', 'content': 'hello', 'session_mode': 'persistent'})
                assert start.status_code == 201, start.text
                ref = start.json()['id']
                caller.get(f'/loadcoach/conversations/{ref}/stream', headers=headers)
                talk = caller.get(f'/loadcoach/conversations/{ref}', headers=headers).json()
                answer = talk['messages'][-1]
                assert answer['content'] == 'Final answer' and answer['reasoning'] == 'Available reasoning', answer
                assert answer['adaptor']['plugin'] == 'hermes' and answer['plugin_events']
                assert not answer.get('prompt_tokens') and not answer.get('answer_tokens')
                sid = answer['adaptor']['session_id']
                assert sid in json.dumps(talk['sessions'])
                caller.post(f'/loadcoach/conversations/{ref}/messages', headers=headers, json={'content': 'next', 'model': 'hermes:gandalf'})
                caller.get(f'/loadcoach/conversations/{ref}/stream', headers=headers)
                assert '--resume' in turns(cli)[1]['argv']
                caller.post(f'/loadcoach/conversations/{ref}/messages', headers=headers, json={'do': 'edit', 'content': 'replacement', 'model': 'hermes:gandalf'})
                caller.get(f'/loadcoach/conversations/{ref}/stream', headers=headers)
                assert '--resume' not in turns(cli)[2]['argv'] and '--continue' in turns(cli)[2]['argv']
            import asyncio
            asyncio.run(inference.aclose())
            asyncio.run(adapters.aclose())
    registry.db.close()
