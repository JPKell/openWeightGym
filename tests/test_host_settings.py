"""Host-specific settings never fall back to a different machine or change its sleep policy."""

import json

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import door, machine_key
from loadcoach import statement
from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.app import Settings, build_app
from weightroom.app import Settings as WebSettings, build_app as build_web


class Hosts(httpx.AsyncBaseTransport):
    def __init__(self, primary, secondary):
        self.wires = {8442: httpx.ASGITransport(app=primary), 9442: httpx.ASGITransport(app=secondary)}
        self.off = False

    async def handle_async_request(self, request):
        if self.off and request.url.port == 9442:
            raise httpx.ConnectError('secondary unavailable')
        return await self.wires[request.url.port].handle_async_request(request)


def host(key, state):
    async def reply(request):
        path = request.url.path
        if path == '/loadcoach/settings' or path.startswith('/loadcoach/settings/'):
            if request.method == 'POST':
                state[path.rsplit('/', 1)[-1]].update(await request.json())
            return JSONResponse({'programs': [
                {'program': program, 'seen': '2026-10-16T00:00:00', 'settings': [
                    {'name': name, 'value': value, 'when': 'now', 'changed': None, 'file': value, 'says': ''}
                    for name, value in values.items()]}
                for program, values in state.items()]})
        if path == '/loadcoach/status':
            return JSONResponse({'loaded': [], 'running': [], 'waiting': [], 'holds': [], 'memory': {}, 'busy': 0})
        if path == '/v1/models':
            return JSONResponse({'data': []})
        return JSONResponse({'error': {'message': path}}, status_code=404)

    return statement.Statements(Starlette(routes=[Route('/{path:path}', reply, methods=['GET', 'POST'])]), key)


def test_host_settings_identity_authorization_outage_and_sleep_policy(tmp_path):
    users = Users(str(tmp_path / 'accounts.db'), {
        'admin': Group(can=frozenset({'manage-settings'})),
        'plain': Group(), 'system': Group(can=frozenset({'follow-settings'})),
    })
    for name, group in [('jordan', 'admin'), ('pat', 'plain'), ('panel', 'system')]:
        users.add(name, group)
    admin, plain, panel = [users.new_key(name) for name in ('jordan', 'pat', 'panel')]
    (tmp_path / 'panel.key').write_text(panel)
    primary_key = machine_key(tmp_path)
    other_key = str(tmp_path / 'other.key')
    statement.replace_key(other_key, 'r' * 40)
    primary = {'loadcoach': {'host_ram_headroom_mib': 512}, 'spotter': {'sleep_after': 0}}
    secondary = {'loadcoach': {'host_ram_headroom_mib': 768}, 'spotter': {'sleep_after': 45}, 'voice': {'idle_seconds': 120}}
    wire = Hosts(host(primary_key, primary), host(other_key, secondary))
    settings = Settings(compute='compute', data=str(tmp_path), machine_key_file=primary_key,
                        auth_key_file=str(tmp_path / 'panel.key'),
                        machines={'remote': {'compute': 'http://compute:9442', 'machine_key_file': other_key}})
    app = build_app(settings, httpx.AsyncClient(transport=wire), users=users)
    base = '/turnstile/machines/remote/settings'
    headers = {'authorization': 'Bearer ' + admin}
    with TestClient(app, base_url='http://front:8442') as api:
        assert api.get(base).status_code == 401
        assert api.get(base, headers={'authorization': 'Bearer ' + plain}).status_code == 403
        found = api.get(base, headers=headers)
        assert found.status_code == 200 and found.json()['machine'] == 'remote'
        assert [p['program'] for p in found.json()['programs']] == ['loadcoach', 'spotter', 'voice', 'machines']
        assert api.post(base + '/voice', headers=headers, json={'idle_seconds': 9}).status_code == 200
        assert api.post(base + '/loadcoach', headers=headers, json={'host_ram_headroom_mib': 1024}).status_code == 200
        assert secondary['loadcoach']['host_ram_headroom_mib'] == 1024
        assert primary['loadcoach']['host_ram_headroom_mib'] == 512
        assert secondary['spotter']['sleep_after'] == 45 and primary['spotter']['sleep_after'] == 0
        web = build_web(WebSettings(compute='http://front:8442', key=panel, data=str(tmp_path / 'web')), door(app, 8442))
        with TestClient(web) as browser:
            browser.cookies.set('weightroom', admin)
            page = browser.get('/compute-nodes?machine=remote')
            assert page.status_code == 200 and 'name="machine" value="remote"' in page.text
            assert '1024' in page.text and '45' in page.text
            assert 'name="value-wake_mac"' in page.text
            wake_saved = browser.post('/settings/machines', data={'machine': 'remote', 'value-wake_mac': '00:11:22:33:44:55'})
            assert wake_saved.status_code == 200
            assert app.state.console_settings.changes.of('machines:remote')['wake_mac'] == '00:11:22:33:44:55'
            saved = browser.post('/settings/loadcoach', data={'machine': 'remote', 'value-host_ram_headroom_mib': '1200'})
            assert saved.status_code == 200 and secondary['loadcoach']['host_ram_headroom_mib'] == 1200
            assert primary['loadcoach']['host_ram_headroom_mib'] == 512
        wire.off = True
        failed = api.post(base + '/loadcoach', headers=headers, json={'host_ram_headroom_mib': 2048})
        assert failed.status_code == 503 and failed.headers['retry-after']
        assert secondary['loadcoach']['host_ram_headroom_mib'] == 1200 and primary['loadcoach']['host_ram_headroom_mib'] == 512
        assert api.get('/turnstile/machines/unknown/settings', headers=headers).status_code == 404
