"""The live desk across four real signed LoadCoach apps, isolated from production."""
import hashlib
import io
import json
import sqlite3
import time
import zipfile
import os

import httpx
import pytest
from starlette.testclient import TestClient

from db import Database, migrate
from db.schemas import SPOTTER
from front_helpers import Later, door, machine_key
from loadcoach.app import build_app as build_loadcoach
from loadcoach.config import Config
from loadcoach.users import Users
from test_weightroom import GROUPS, sign_in
from turnstile.app import Settings as TurnstileSettings, build_app as build_turnstile
from weightroom.app import Settings, build_app
from test_postgresql import postgres  # noqa: F401


@pytest.fixture
def fleet(tmp_path, request, backup_keys):
    data = tmp_path / 'front'; data.mkdir()
    backup_keys.configure(data / 'backups')
    users = Users(str(data / 'accounts.db'), GROUPS)
    for name, group in [('jordan', 'admin'), ('pat', 'plain'), ('panel', 'front')]:
        users.add(name, group)
        users.change(name, password='correct horse')
    deferred = Later()
    names = ('compute', 'cpu', 'gpu2', 'gpu3')
    wires, histories, keys = {}, {}, {}
    for name in names:
        folder = tmp_path / name; folder.mkdir()
        settings = folder / 'settings'; settings.mkdir()
        kept = folder / 'data'; kept.mkdir()
        backup_keys.configure(kept / 'backups')
        key = folder / 'machine.key'; key.write_text('fixture-' + name + '-unique-key-value-' + name)
        keys[name] = str(key)
        (settings / 'loadcoach.toml').write_text('data="../data"\n')
        (settings / 'spotter.toml').write_text('data="../data"\n')
        mixed = getattr(request, 'param', None) == 'mixed' and name == 'gpu2'
        target = request.getfixturevalue('postgres') if mixed else Database.sqlite(kept / 'spotter.db')
        if mixed:
            (settings / 'spotter.toml').write_text('data="../data"\n[databases.stores.history]\nengine="postgresql"\ndsn_file=' + json.dumps(target.store.dsn_file) + '\n[databases.bindings]\n"spotter.history"="history"\n')
        with target.connection() as connection:
            migrate(target, connection, 'spotter.history', SPOTTER)
            with connection:
                connection.executemany('INSERT INTO readings VALUES(?,?)', [(1, '{}'), (2, '{}'), (1761955200, '{}')])
        histories[name] = target
        app = build_loadcoach(Config(database=str(kept / 'loadcoach.db'), data=str(kept), path=str(settings / 'loadcoach.toml'),
            machine_name=name, machine_key_file=keys[name], front='http://front:8442', nvidia_smi='no-such-program'),
            front=door(deferred, 8442))
        wires[name] = httpx.ASGITransport(app=app, client=('10.0.0.9', 1))

    class Fleet(httpx.AsyncBaseTransport):
        off = set()
        calls = []
        async def handle_async_request(self, request):
            self.calls.append((request.url.host, request.url.path))
            if request.url.host in self.off:
                raise httpx.ConnectError('Fixture node is off')
            return await wires[request.url.host].handle_async_request(request)
    transport = Fleet()
    machines = {name: {'compute': f'http://{name}:8442', 'machine_key_file': keys[name]} for name in names[1:]}
    front = deferred.app = build_turnstile(TurnstileSettings(compute='compute', data=str(data),
        machine_key_file=keys['compute'], machines=machines, front_settings=True),
        client=httpx.AsyncClient(transport=transport), users=users)
    application = build_app(Settings(compute='http://front:8442', data=str(data), key=users.new_key('panel')), door(front, 8442))
    with TestClient(application) as browser:
        sign_in(browser, 'jordan')
        yield browser, transport, histories, data


def wait_job(browser, identifier):
    for _ in range(200):
        answer = browser.get('/databases/api/jobs/' + identifier)
        assert answer.status_code == 200, answer.text
        job = answer.json()
        if job['state'] not in {'queued', 'running', 'cancelling'}:
            return job
        time.sleep(.02)
    pytest.fail('The database fixture job did not finish')


def test_fleet_inventory_and_archive_restore_all_stores_then_report_missing_node(fleet, tmp_path):
    browser, wire, _, _ = fleet
    snapshot = browser.get('/databases/api/snapshot').json()
    assert snapshot['complete'] and len(snapshot['nodes']) == 5
    assert len(snapshot['databases']) == 15
    answer = browser.post('/databases/api/export', json={'scope': 'all'})
    assert answer.status_code == 202, answer.text
    job = wait_job(browser, answer.json()['id'])
    assert job['state'] == 'complete', job
    archive = browser.get('/databases/api/jobs/' + job['id'] + '/download')
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        assert len(manifest['stores']) == 15 and manifest['omissions'] == []
        for i, store in enumerate(manifest['stores']):
            sql = bundle.read(store['file'])
            assert hashlib.sha256(sql).hexdigest() == store['sha256']
            with sqlite3.connect(tmp_path / f'restored-{i}.db') as restored:
                restored.executescript(sql.decode())
                assert restored.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type='table'").fetchone()[0] > 0
    wire.off.add('gpu3')
    snapshot = browser.get('/databases/api/snapshot').json()
    assert not snapshot['complete'] and len(snapshot['nodes']) == 5
    assert next(node for node in snapshot['nodes'] if node['name'] == 'gpu3')['answering'] is False
    assert browser.post('/databases/api/export', json={'scope': 'all'}).status_code == 409
    partial = browser.post('/databases/api/export', json={'scope': 'all', 'partial': True})
    assert partial.status_code == 202
    job = wait_job(browser, partial.json()['id'])
    assert job['state'] == 'partial'
    with zipfile.ZipFile(io.BytesIO(browser.get('/databases/api/jobs/' + job['id'] + '/download').content)) as bundle:
        assert json.loads(bundle.read('manifest.json'))['omissions'][0]['machine'] == 'gpu3'
    # Explicit selection is independent of the incomplete global scope.
    front = next(row for row in snapshot['databases'] if row['machine'] == 'front')
    assert browser.post('/databases/api/export', json={'scope': [front['key']]}).status_code == 202


def test_node_scoped_dump_lazy_details_and_reviewed_purge(fleet):
    browser, wire, histories, _ = fleet
    snapshot = browser.get('/databases/api/snapshot').json()
    cpu = next(row for row in snapshot['databases'] if row['machine'] == 'cpu' and row['id'] == 'spotter.db')
    assert cpu['records'] is None and cpu['tables'] == []
    detail = browser.get(cpu['detail_url'])
    assert detail.status_code == 200 and detail.json()['tables'][0]['records'] == 3
    dumped = browser.get(cpu['dump_url'] + '?table=readings')
    assert dumped.status_code == 200 and 'CREATE TABLE readings' in dumped.text
    review = browser.post('/databases/api/preview', json={'scope': [cpu['key']], 'action': 'purge', 'cutoff': '2025-11-01'})
    assert review.status_code == 200, review.text
    assert review.json()['stores'][0]['count'] == 2
    assert browser.post('/databases/api/execute', json={'review': review.json()['review'], 'confirmed': False}).status_code == 409
    result = browser.post('/databases/api/execute', json={'review': review.json()['review'], 'confirmed': True})
    assert result.status_code == 202, result.text
    job = wait_job(browser, result.json()['id'])
    assert job['state'] == 'complete', job
    for name, target in histories.items():
        with target.connection() as connection:
            assert connection.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == (1 if name == 'cpu' else 3)
    remote_id = job['outcomes'][0]['job']
    assert browser.get(f'/databases/cpu/operation/jobs/{remote_id}/recovery').status_code == 200
    assert browser.post('/databases/api/execute', json={'review': review.json()['review'], 'confirmed': True}).status_code == 409
    assert browser.get('/databases/unknown/spotter.db/dump').status_code == 503


def test_permissions_job_ownership_origin_and_orphan_exclusion(fleet):
    browser, _, _, data = fleet
    with sqlite3.connect(data / 'old.db') as connection:
        connection.execute('CREATE TABLE old(id INTEGER)')
    snapshot = browser.get('/databases/api/snapshot').json()
    assert any(row['id'] == 'old.db' for row in snapshot['discovered'])
    job = browser.post('/databases/api/export', json={'scope': 'all'}).json()
    assert all(row['id'] != 'old.db' for row in job['scope'])
    assert browser.post('/databases/api/export', json={'scope': 'all'}, headers={'origin': 'https://another.example'}).status_code == 403
    sign_in(browser, 'pat')
    assert browser.get('/databases').status_code == 403
    assert browser.get('/databases/api/snapshot').status_code == 403
    assert browser.post('/databases/api/export', json={'scope': 'all'}).status_code == 403


@pytest.mark.parametrize('fleet', ['mixed'], indirect=True)
def test_real_mixed_engine_selection_exports_pg_and_sqlite_without_false_mismatch(fleet, tmp_path):
    import subprocess
    import uuid
    from db.postgresql_tools import _run
    browser, _, histories, _ = fleet
    snapshot = browser.get('/databases/api/snapshot').json()
    assert snapshot['complete']
    assert {row['engine'] for row in snapshot['databases']} == {'SQLite', 'PostgreSQL'}
    stores = [row for row in snapshot['databases'] if row['machine'] in {'gpu2', 'cpu'} and 'spotter.history' in row['bindings']]
    assert len(stores) == 2
    result = browser.post('/databases/api/export', json={'scope': [row['key'] for row in stores]})
    assert result.status_code == 202, result.text
    job = wait_job(browser, result.json()['id'])
    assert job['state'] == 'complete', job
    target = histories['gpu2']
    temporary = 'owg_export_' + uuid.uuid4().hex[:12]
    with target.connection(isolation_level=None) as admin:
        admin.execute('CREATE DATABASE ' + temporary)
        try:
            with zipfile.ZipFile(io.BytesIO(browser.get('/databases/api/jobs/' + job['id'] + '/download').content)) as bundle:
                manifest = json.loads(bundle.read('manifest.json'))
                for store in manifest['stores']:
                    path = tmp_path / (store['machine'] + '.sql'); path.write_bytes(bundle.read(store['file']))
                    if store['engine'] == 'PostgreSQL':
                        _run(target, 'psql', ['--no-psqlrc', '--set=ON_ERROR_STOP=1', '--file=' + str(path)], dbname=temporary)
                        with target.connection(dbname=temporary) as restored:
                            assert restored.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 3
                    else:
                        with sqlite3.connect(tmp_path / 'restored-sqlite.db') as restored:
                            restored.executescript(path.read_text())
                            assert restored.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 3
        finally:
            admin.execute('DROP DATABASE ' + temporary + ' WITH (FORCE)')


def test_live_operations_desk_filters_expands_and_previews_at_narrow_widths(fleet):
    from test_console_browser import browser_context, playwright
    server, wire, _, _ = fleet
    snapshot = server.get('/databases/api/snapshot').json()
    cpu = next(row for row in snapshot['databases'] if row['machine'] == 'cpu' and row['id'] == 'spotter.db')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            page.goto('http://console.test/databases')
            playwright.expect(page.locator('[data-database]')).to_have_count(15)
            for width in (1600, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#node-filter').select_option('cpu')
                playwright.expect(page.locator('[data-database]')).to_have_count(2)
                row = page.locator(f'[data-database="{cpu["key"]}"]')
                row.locator('[data-expand]').click()
                playwright.expect(page.get_by_role('table', name='Tables in spotter.db on cpu', exact=True)).to_be_visible()
                playwright.expect(page.locator(f'#detail-{cpu["key"]} [data-records]').first).to_have_text('3')
                row.locator('[data-select]').check()
                page.locator('#node-filter').select_option('front')
                playwright.expect(page.locator('#selection')).to_contain_text('1 selected across all filters')
                page.locator('#purge-selected').click()
                page.locator('#cutoff').fill('2025-11-01')
                page.locator('#preview-purge').click()
                playwright.expect(page.locator('#maintenance-preview')).to_be_visible()
                playwright.expect(page.locator('#maintenance-rows')).to_contain_text('Spotter telemetry')
                page.locator('#confirm-maintenance').check()
                playwright.expect(page.locator('#apply-maintenance')).to_be_enabled()
                page.locator('#cutoff').fill('2025-10-01')
                playwright.expect(page.locator('#apply-maintenance')).to_be_disabled()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.keyboard.press('Escape')
                page.locator('#clear-selection').click()
                page.locator('#node-filter').select_option('cpu')
                row.locator('[data-expand]').click()
                page.locator('#node-filter').select_option('all')
        wire.off.add('gpu3')
        page.locator('#refresh').click()
        playwright.expect(page.locator('#coverage')).to_contain_text('3 / 4')
        page.locator('#export-all').click()
        playwright.expect(page.locator('#download-export')).to_be_disabled()
        page.locator('#allow-partial').check()
        playwright.expect(page.locator('#download-export')).to_be_enabled()
        page.keyboard.press('Escape')
        page.locator('#search').fill('no-such-database')
        playwright.expect(page.locator('#inventory [data-empty-row]')).to_be_visible()
        assert not errors
        browser.close()
