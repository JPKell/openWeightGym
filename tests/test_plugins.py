"""Discovery, private pending settings, model aliases, health policy and installer contents."""

import asyncio
import io
import json
import os
import runpy
import subprocess
import sys
import tarfile

import httpx
import pytest
from starlette.testclient import TestClient

from plugin_helpers import setup
from plugins._controller import Controller
from plugins._installer import bundle
from plugins._sdk import AdapterError, Manifest, ALIAS


def test_other_http_plugin_works_without_hermes_specific_routing(tmp_path):
    """Install a distinct minimal HTTP driver through the same discovery/server/turnstile path."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    from plugin_helpers import serving
    from plugins._runtime import build_app
    from plugins._registry import Registry
    from plugins._turnstile import Router
    from plugins._sdk import AdapterError, atomic_json
    async def external(request):
        return JSONResponse({'reply': 'HTTP says ' + (await request.json())['messages'][-1]['content']})
    with serving(Starlette(routes=[Route('/answer', external, methods=['POST'])])) as external_url:
        folder = tmp_path / 'plugins/httpdemo'
        folder.mkdir(parents=True)
        atomic_json(folder / 'manifest.json', {'protocol': 1, 'id': 'httpdemo', 'name': 'HTTP example', 'version': '1',
                    'capabilities': ['send', 'receive', 'models'], 'schema': 'schema.json', 'driver': 'adapter.py', 'class': 'HTTP'})
        atomic_json(folder / 'schema.json', {'type': 'object', 'additionalProperties': False, 'properties': {'endpoint': {'type': 'string'}}, 'required': ['endpoint']})
        (folder / 'README.md').write_text('A minimal HTTP service integration.')
        (folder / 'adapter.py').write_text('''from plugins._sdk import Adapter
import httpx
class HTTP(Adapter):
    async def models(self, settings):return [{'id':'provider/model','name':'HTTP model'}]
    async def events(self, run, settings):
        async with httpx.AsyncClient() as client:
            response=await client.post(settings['endpoint'],json={'messages':run.messages})
            response.raise_for_status()
            yield {'type':'result','text':response.json()['reply']}
''')
        registry = Registry(folder.parent, tmp_path / 'data')
        registry.save_instance('httpdemo', 'local', 'http://adapter', {'endpoint': external_url + '/answer'}, enabled=True)
        registry.enable('httpdemo', True)
        user = {'name': 'alice', 'identity': 'id-one', 'can': ['front-plugins'], 'model_access': '*', 'max_sensitivity': 'normal'}
        def identify(authorization):  # turnstile's, from the accounts on the control node
            if authorization != 'Bearer caller':
                raise AdapterError('Not authorized.', 401, 'invalid_api_key')
            return user
        file = tmp_path / 'adapter/settings.json'
        atomic_json(file, {'plugin': 'httpdemo', 'instance': 'local', 'settings': {'endpoint': external_url + '/answer'}, 'revision': 1,
                   'key_file': 'turnstile.key', 'policy': {'allow_start': False, 'allow_restart': False}})
        (file.parent / 'turnstile.key').write_text(registry.pairing_key('httpdemo', 'local'))
        with serving(build_app(file, registry.manifest('httpdemo'))) as url:
            registry.save_instance('httpdemo', 'local', url, {'endpoint': external_url + '/answer'}, enabled=True)
            registry.inventory('httpdemo', 'local', [{'id': 'provider/model'}])
            registry.model('httpdemo', 'local', 'provider/model', 'httpdemo:custom', True)
            async def offline(*_):return None  # compute is not answering
            client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
            router = Router(registry, client, identify, offline)
            async def routed(request):return await router.intercept(request)
            with TestClient(Starlette(routes=[Route('/v1/chat/completions', routed, methods=['POST'])])) as caller:
                answer = caller.post('/v1/chat/completions', headers={'authorization': 'Bearer caller'}, json={'model': 'httpdemo:custom', 'messages': [{'role': 'user', 'content': 'hello'}]})
                assert answer.status_code == 200 and answer.json()['choices'][0]['message']['content'] == 'HTTP says hello'
                assert answer.headers['x-suite-adaptor'] == 'httpdemo'
            asyncio.run(client.aclose())
        registry.db.close()


def test_discovery_is_data_only_and_aliases_are_unique(tmp_path):
    registry, _, app, _ = setup(tmp_path)
    marker = tmp_path / 'executed'
    driver = registry.root / 'hermes/adapter.py'
    driver.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    assert registry.scan()[0]['valid'] and not marker.exists()
    registry.inventory('hermes', 'local', [{'id': 'gandalf'}, {'id': 'a:b'}, {'id': 'a-b'}, {'id': 'a'*190}])
    aliases = registry.aliases()
    assert len(aliases) == 4 and all(ALIAS.fullmatch(alias) for alias in aliases)
    registry.model('hermes', 'local', 'gandalf', 'hermes:renamed', True)
    registry.inventory('hermes', 'local', [{'id': 'gandalf'}])
    assert registry.resolve('hermes:renamed')[2] == 'gandalf'
    registry.save_instance('hermes', 'remote', 'https://agent.example:8450', {}, enabled=True)
    registry.inventory('hermes', 'remote', [{'id': 'gandalf'}])
    with pytest.raises(AdapterError):
        registry.model('hermes', 'remote', 'gandalf', 'hermes:renamed', True)
    assert registry.owns_prefix('hermes:unknown')
    document = json.loads((registry.root / 'hermes/manifest.json').read_text())
    document['driver'] = '../outside.py'
    (registry.root / 'hermes/manifest.json').write_text(json.dumps(document))
    assert not registry.scan()[0]['valid'] and not marker.exists()
    with TestClient(app):
        pass
    registry.db.close()


def test_plugin_folder_cannot_shadow_framework_modules(tmp_path):
    """Fresh startup cannot import a disabled plugin whose name matches an SDK concept."""
    import shutil, subprocess, sys
    from plugin_helpers import SOURCE
    root = tmp_path / 'plugins'
    root.mkdir()
    for source in SOURCE.glob('*.py'):
        shutil.copy2(source, root / source.name)
    folder = root / 'sdk'
    shutil.copytree(SOURCE / 'hermes', folder, ignore=shutil.ignore_patterns('__pycache__', 'settings.json', '.settings.lock'))
    document = json.loads((folder / 'manifest.json').read_text())
    document['id'] = 'sdk'
    (folder / 'manifest.json').write_text(json.dumps(document))
    marker = tmp_path / 'imported-plugin'
    (folder / '__init__.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    script = "from pathlib import Path; from plugins._registry import Registry; r=Registry(Path('plugins'),Path('data')); assert r.scan()[0]['valid']; r.db.close()"
    checked = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    assert not marker.exists()


def test_pending_revisions_sync_and_health_gates(tmp_path):
    registry, cli, app, headers = setup(tmp_path)
    async def operations():
        async def offline(request):
            raise httpx.ConnectError('offline')
        async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
            controller = Controller(registry, client)
            saved = registry.save_instance('hermes', 'local', 'http://adapter', {'cli': str(cli), 'run_budget': 6}, enabled=True)
            assert saved['revision'] == 2 and saved['applied_revision'] == 0
            with pytest.raises(AdapterError):
                await controller.sync('hermes', 'local')
            assert registry.instance('hermes', 'local')['error']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            controller = Controller(registry, client)
            await controller.discover('hermes', 'local')
            current = registry.instance('hermes', 'local')
            assert current['applied_revision'] == current['revision'] == 2 and not current['error']
            assert not any(item['enabled'] for item in current['models'].values())
            assert (await controller.health('hermes', 'local'))['external']['state'] == 'healthy'
            with pytest.raises(AdapterError):
                await controller.control('hermes', 'local', 'start')
            registry.save_instance('hermes', 'local', 'http://adapter', current['settings'], enabled=True, health={'allow_start': True})
            await controller.control('hermes', 'local', 'start')
            assert registry.instance('hermes', 'local')['applied_revision'] == 3
    with TestClient(app):
        asyncio.run(operations())
    public = (registry.root / 'hermes/settings.json').read_text()
    assert registry.pairing_key('hermes', 'local') not in public
    assert (registry.root / 'hermes/settings.json').stat().st_mode & 0o777 == 0o600
    for url in ('http://user:secret@host', 'http://host:wrong', 'file:///tmp/test'):
        with pytest.raises(AdapterError):
            registry.save_instance('hermes', 'bad', url, {})
    with pytest.raises(AdapterError):
        registry.save_instance('hermes', 'bad', 'http://host', {}, health={'auto_restart': True})
    registry.db.close()


def test_stopped_gateway_is_startable_even_when_status_exits_nonzero(monkeypatch):
    """A stopped messaging gateway does not imply that CLI chat cannot run."""
    from plugins.hermes.adapter import Hermes
    async def execute(settings, arguments, timeout=30):
        return (1, 'Gateway is not running') if arguments[-1] == 'status' else (0, 'gandalf stopped')
    monkeypatch.setattr('plugins.hermes.adapter.execute', execute)
    answer = asyncio.run(Hermes().health({'health_profile': 'gandalf'}))
    assert answer['state'] == 'stopped' and answer['chat_available']


def test_installer_contains_selected_plugin_only_and_pairing_is_private(tmp_path):
    registry, _, app, _ = setup(tmp_path)
    (registry.root / 'hermes/unlisted-secret.txt').write_text('do not package')
    archive = tarfile.open(fileobj=io.BytesIO(bundle(registry, 'hermes', 'local')), mode='r:gz')
    names = archive.getnames()
    assert not any('unlisted' in name or '.settings.lock' in name or 'runtime.db' in name for name in names)
    assert any(name.endswith('plugins/hermes/adapter.py') for name in names)
    assert any(name.endswith('db/database.py') for name in names)
    assert any(name.endswith('db/migrations.py') for name in names)
    assert any(name.endswith('db/schemas.py') for name in names)
    assert any(name.endswith('db/drivers/sqlite.py') for name in names)
    assert not any(name.endswith('registry.py') for name in names)
    prefix = 'suite-hermes-local/'
    readme = archive.extractfile(prefix + 'README.md').read().decode()
    assert 'suite-adapter-hermes-local.service' in readme
    assert "package directory 'db' does not exist" in readme
    assert '--no-service' in readme and 'plugins/hermes/README.md' in readme
    assert '@PLUGIN@' not in readme and '@INSTANCE@' not in readme
    assert registry.pairing_key('hermes', 'local') not in readme
    assert archive.extractfile(prefix + 'turnstile.key').read().decode().strip() == registry.pairing_key('hermes', 'local')
    assert archive.getmember(prefix + 'turnstile.key').mode == 0o600
    compile(archive.extractfile(prefix + 'install.py').read(), 'install.py', 'exec')
    with TestClient(app):
        pass
    registry.db.close()


def test_extracted_installer_copies_importable_runtime_and_preserves_state(tmp_path, monkeypatch):
    """Exercise the downloaded installer outside the checkout, without pip/network/systemd."""
    registry, _, app, _ = setup(tmp_path)
    archive = tarfile.open(fileobj=io.BytesIO(bundle(registry, 'hermes', 'local')), mode='r:gz')
    extracted = tmp_path / 'extracted'
    archive.extractall(extracted, filter='data')
    source = extracted / 'suite-hermes-local'
    destination = tmp_path / 'installed'
    calls = []
    real_run = subprocess.run

    def install_command(command, **kwargs):
        calls.append(command)
        # The pip build must receive every explicitly declared package and startup import.
        assert (destination / 'plugins/_runtime.py').is_file()
        assert (destination / 'db/drivers/sqlite.py').is_file()
        assert (destination / 'db/migrations.py').is_file()
        assert (destination / 'db/schemas.py').is_file()
        assert (destination / 'loadcoach/observability.py').is_file()
        assert (destination / 'loadcoach/observation_store.py').is_file()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, 'run', install_command)
    monkeypatch.setattr(sys, 'argv', ['install.py', '--directory', str(destination), '--no-service'])
    runpy.run_path(str(source / 'install.py'), run_name='__main__')
    assert len(calls) == 2 and calls[-1][-1] == str(destination)
    assert not any(command[0] == 'systemctl' for command in calls)
    # Import and initialize only extracted code, using the test interpreter's dependencies.
    smoke = '''import sys
sys.meta_path = [finder for finder in sys.meta_path if not getattr(finder, '__module__', '').startswith('__editable__')]
from pathlib import Path
from starlette.testclient import TestClient
from plugins._runtime import build_app
import db.connection
assert Path(db.connection.__file__).is_relative_to(Path.cwd())
app = build_app(Path('settings.json'))
with TestClient(app):
    assert app.state.runtime.db.engine == 'sqlite'
assert Path('data/runtime.db').is_file()
'''
    result = real_run([sys.executable, '-c', smoke], cwd=destination,
                      env={**os.environ, 'PYTHONPATH': str(destination)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    preserved = {name: (destination / name).read_bytes()
                 for name in ('settings.json', 'keys/turnstile.key', 'data/runtime.db')}
    (source / 'turnstile.key').write_text('different-key-not-to-overwrite-existing-state')
    (source / 'initial-settings.json').write_text(json.dumps({'plugin': 'hermes', 'instance': 'local', 'settings': {}}))
    runpy.run_path(str(source / 'install.py'), run_name='__main__')
    assert all((destination / name).read_bytes() == content for name, content in preserved.items())
    assert (destination / 'keys/turnstile.key').stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['install.py', '--directory', str(destination)])
    runpy.run_path(str(source / 'install.py'), run_name='__main__')
    unit = (tmp_path / '.config/systemd/user/suite-adapter-hermes-local.service').read_text()
    assert f'WorkingDirectory={destination}\n' in unit
    with TestClient(app):
        pass
    registry.db.close()


def test_adapter_observation_health_forwarding_and_optional_failure(tmp_path, monkeypatch):
    from loadcoach.log import Log
    from loadcoach.observation_store import sources
    registry, _, app, headers = setup(tmp_path)
    log = Log(str(tmp_path / "front-observations.db"))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            controller = Controller(registry, client)
            controller.observation_log = log
            await controller.collect_observations("hermes", "local")
            health = sources(log.db)
            assert health[0]["machine"] == "adapter:hermes:local"
            assert health[0]["recording"] and health[0]["backlog_count"] >= 0
            def unavailable(*args, **kwargs):
                raise OSError("secret-destination-canary")
            # A failed optional destination cannot stop the settings/health controller.
            with monkeypatch.context() as patch:
                patch.setattr("loadcoach.observation_store.receive", unavailable)
                await controller.collect_observations("hermes", "local")
            log.db.close()

    with TestClient(app) as client:
        assert client.get("/adapter/v1/observations").status_code == 401
        asyncio.run(check())
    registry.db.close()
