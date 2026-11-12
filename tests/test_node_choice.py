"""Phase 1 of docs/peer-nodes-plan.md: the front keeps every task record, and callers may name a node.

Covers the `allow_caller_to_specify_destination` setting (Settings → General), tasks placed on
nodes other than the default, the renamed settings keys and the renamed task tables and stream.
"""

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

from loadcoach.setting_names import BACKUP_SUFFIX, current_names, rename_in_file, rename_installation
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder  # noqa: F401  (fixtures)
from test_front_tasks import legacy_task, task_gateway
from test_tasks import create
from test_weightroom import desk, sign_in  # noqa: F401  (desk is a fixture)

ROOT = Path(__file__).resolve().parent.parent


def destination_allowed(caller, allowed: bool) -> None:
    """Change the setting as Settings → General does, in the front's console settings store."""
    caller.front.state.console_settings.changes.put('turnstile', 'allow_caller_to_specify_destination', allowed, 'owner')


# The setting


def test_naming_the_node_is_refused_with_a_clear_message_only_while_the_setting_is_off(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        pinned = {'id': 'batch_' + 'a' * 24, 'token': 'b' * 43, 'model': 'small', 'machine': 'compute'}
        destination_allowed(caller, False)
        refused = caller.post('/loadcoach/tasks', headers=owner, json=pinned)
        assert refused.status_code == 400
        assert "Allow callers to choose the node" in refused.json()['error']['message']
        assert "Settings → General" in refused.json()['error']['message']
        placed = create(caller, owner)  # without a node, the front still places it
        status = caller.get('/loadcoach/tasks/' + placed['id'], headers={'x-loadcoach-task': placed['token']}).json()
        assert status['machine'] == 'compute'
        destination_allowed(caller, True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            accepted = caller.post('/loadcoach/tasks', headers=owner, json=pinned)
            if 'fingerprinted' not in accepted.text:
                break
            time.sleep(.02)
        assert accepted.status_code == 201, accepted.text


def test_the_setting_is_on_by_default_and_listed_under_general(desk):
    import tomllib
    from loadcoach.settings import CHANGEABLE, NOW, said
    from weightroom.setting_labels import LABELS
    browser, switch, _ = desk
    defaults = tomllib.loads((ROOT / 'settings/defaults/weightroom.toml').read_text())['turnstile']
    assert defaults['allow_caller_to_specify_destination'] is True
    assert CHANGEABLE['turnstile']['allow_caller_to_specify_destination'] == NOW
    assert LABELS['allow_caller_to_specify_destination'][:2] == ('Allow callers to choose the node', 'Requests and queue')
    store = switch.front.state.console_settings.changes
    store.seen('turnstile', said(defaults, defaults, {}))
    sign_in(browser, 'pat')
    assert browser.post('/settings/turnstile', data={'value-allow_caller_to_specify_destination': 'false'}).status_code == 403
    assert 'allow_caller_to_specify_destination' not in store.of('turnstile')
    sign_in(browser, 'jordan')
    page = browser.get('/settings').text
    assert 'General settings' in page and 'Allow callers to choose the node' in page
    assert 'name="value-allow_caller_to_specify_destination"' in page
    saved = browser.post('/settings/turnstile', data={'value-allow_caller_to_specify_destination': 'false'})
    assert saved.status_code == 200, saved.text
    assert store.of('turnstile')['allow_caller_to_specify_destination'] is False
    assert switch.front.state.task_records.destination_allowed() is False
    assert browser.post('/settings/turnstile', data={'reset': 'allow_caller_to_specify_destination'}).status_code == 200
    assert switch.front.state.task_records.destination_allowed() is True


# Records on the front, work on the nodes


def test_tasks_are_created_on_the_front_for_any_node_and_legacy_ids_keep_their_node(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        legacy = legacy_task(caller, 'owner')
        made = create(caller, owner)
        scoped = {'x-loadcoach-task': made['token']}
        status = caller.get('/loadcoach/tasks/' + made['id'], headers=scoped)
        assert status.headers.get('x-suite-data-owner') == 'front'
        old = caller.get('/loadcoach/tasks/' + legacy['id'], headers={'x-loadcoach-task': legacy['token']})
        assert old.status_code == 200 and old.json()['id'] == legacy['id']
        assert 'x-suite-data-owner' not in old.headers  # answered by the node that created it
        with sqlite3.connect(tmp_path / 'compute.db') as node:
            assert [row[0] for row in node.execute('SELECT id FROM benchmark_tasks')] == [legacy['id']]


def test_unknown_node_names_are_refused_and_records_name_the_chosen_node(fake_llama_server, fake_cards, tmp_path):
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        missing = {'id': 'batch_' + 'c' * 24, 'token': 'd' * 43, 'model': 'small', 'machine': 'no-such-node'}
        assert caller.post('/loadcoach/tasks', headers=owner, json=missing).status_code in (400, 404, 409)
        records = caller.front.state.task_records
        assert records.names == ['compute']
        assert records.get(missing['id']) is None


def test_a_running_benchmark_probe_stays_listed_on_status_when_the_front_keeps_its_record(fake_llama_server, fake_cards, tmp_path, monkeypatch):
    import asyncio
    import threading
    from loadcoach.probes import Probes
    entered, release = threading.Event(), threading.Event()

    async def sample(self, task, options, kept=None):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return {'values': {'success': True}, 'output': '', 'log': ''}
    monkeypatch.setattr(Probes, 'sample', sample)
    with task_gateway(fake_llama_server, fake_cards, tmp_path) as caller:
        owner = add_user(caller, 'owner', 'admin')
        made = create(caller, owner)
        results = []
        thread = threading.Thread(target=lambda: results.append(caller.post(
            '/loadcoach/tasks/' + made['id'] + '/probe', headers={'x-loadcoach-task': made['token']}, json={'context': 128})))
        thread.start()
        try:
            assert entered.wait(3)
            active = caller.get('/loadcoach/status', headers=owner).json()['active_batches']
            assert [job['id'] for job in active] == [made['id']]
        finally:
            release.set()
            thread.join(3)
        assert results[0].status_code == 200, results[0].text


def test_benchmark_targets_send_a_node_only_when_the_caller_chose_one():
    text = (ROOT / 'benchmarks/app.py').read_text()
    assert "'pinned': bool(target.get('machine'))" in text
    assert "target.get('pinned') or not target['model_id']" in text


# Renamed settings keys


def test_old_setting_names_are_read_under_their_new_names_and_conflicts_are_refused():
    said = []
    assert current_names({'front_settings': True}, 'weightroom.toml', said.append) == {'keep_console_settings_on_front': True}
    assert 'front_settings is now called keep_console_settings_on_front' in said[0]
    assert current_names({'keep_console_settings_on_front': False}, 'x', said.append) == {'keep_console_settings_on_front': False}
    assert current_names({'front_toollocker': True, 'toollocker_runs_on_front': True}, 'x', said.append) == {'toollocker_runs_on_front': True}
    with pytest.raises(ValueError, match='both set, to different values'):
        current_names({'front_settings': True, 'keep_console_settings_on_front': False}, 'x', said.append)
    said.clear()
    assert current_names({'front_tasks': True, 'wait': 5}, 'x', said.append) == {'wait': 5}
    assert len(said) == 1 and 'front_tasks is ignored' in said[0] and 'Settings → General' in said[0]


def test_old_keys_in_a_settings_file_are_renamed_once_with_a_backup(tmp_path):
    path = tmp_path / 'weightroom.toml'
    original = ('# front_settings = false is how it used to be\nfront_settings = true\n\n[turnstile]\n'
                'front_settings = true   # keep it\nfront_tasks = true\n[voice]\nfront_settings = "unrelated"\n')
    path.write_text(original)
    path.chmod(0o640)
    assert rename_in_file(path, 'turnstile') == ['front_settings']
    text = path.read_text()
    assert 'keep_console_settings_on_front = true   # keep it\nfront_tasks = true\n' in text
    assert text.startswith('# front_settings = false is how it used to be\nfront_settings = true\n')  # other tables untouched
    assert '[voice]\nfront_settings = "unrelated"\n' in text
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    assert backup.read_text() == original and (backup.stat().st_mode & 0o777) == 0o640
    assert (path.stat().st_mode & 0o777) == 0o640
    assert rename_in_file(path, 'turnstile') == []  # a second run changes nothing
    both = tmp_path / 'both.toml'
    both.write_text('[turnstile]\nfront_settings = true\nkeep_console_settings_on_front = true\n')
    assert rename_in_file(both, 'turnstile') == [] and not both.with_name('both.toml' + BACKUP_SUFFIX).exists()


def test_an_installation_is_renamed_in_both_settings_files(tmp_path):
    (tmp_path / 'weightroom.toml').write_text('[turnstile]\nfront_settings = true\n')
    (tmp_path / 'loadcoach.toml').write_text('front_toollocker = true\ndata = "."\n[machines.rig]\nfront_toollocker = 1\n')
    said = []
    rename_installation(tmp_path, said.append)
    assert (tmp_path / 'weightroom.toml').read_text() == '[turnstile]\nkeep_console_settings_on_front = true\n'
    assert (tmp_path / 'loadcoach.toml').read_text() == 'toollocker_runs_on_front = true\ndata = "."\n[machines.rig]\nfront_toollocker = 1\n'
    assert len(said) == 2 and all(BACKUP_SUFFIX in line for line in said)


def test_owg_update_renames_keys_with_only_the_checkout_files(tmp_path, capsys):
    """`owg update` runs scripts/update-runtime-source.py from the new checkout, maybe without its packages importable."""
    import importlib.util
    import shutil
    spec = importlib.util.spec_from_file_location('update_runtime_source', ROOT / 'scripts/update-runtime-source.py')
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    script.renamed_settings(tmp_path)  # a checkout without the rename module has nothing to rename
    (tmp_path / 'loadcoach').mkdir()
    shutil.copy(ROOT / 'loadcoach/setting_names.py', tmp_path / 'loadcoach/setting_names.py')
    (tmp_path / 'settings').mkdir()
    (tmp_path / 'settings/weightroom.toml').write_text('[turnstile]\nfront_settings = true\n')
    script.renamed_settings(tmp_path)
    assert (tmp_path / 'settings/weightroom.toml').read_text() == '[turnstile]\nkeep_console_settings_on_front = true\n'
    assert 'renamed front_settings to keep_console_settings_on_front' in capsys.readouterr().out
    (tmp_path / 'settings/loadcoach.toml').write_bytes(b'front_toollocker = \xff\n')  # unreadable: reported, the update goes on
    script.renamed_settings(tmp_path)
    assert 'were not renamed' in capsys.readouterr().out


def test_turnstile_and_loadcoach_start_with_the_old_names(tmp_path):
    from loadcoach.config import load_config
    from turnstile.app import load_settings
    front = tmp_path / 'weightroom.toml'
    front.write_text('compute = "http://compute:8442"\n[turnstile]\nfront_settings = true\nfront_tasks = false\n')
    settings = load_settings(front)
    assert settings.keep_console_settings_on_front is True and settings.allow_caller_to_specify_destination is True
    front.write_text('compute = "http://compute:8442"\n[turnstile]\nfront_settings = true\nkeep_console_settings_on_front = false\n')
    with pytest.raises(SystemExit, match='both set'):
        load_settings(front)
    node = tmp_path / 'loadcoach.toml'
    node.write_text('front = "http://front:8442"\nfront_toollocker = true\nfront_tasks = true\n')
    assert load_config(node).toollocker_runs_on_front is True


# Renamed tables and stream


def stored(connection) -> tuple[set, list]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    streams = sorted(row[0] for row in connection.execute("SELECT DISTINCT stream FROM suite_schema_migrations"))
    return tables, streams


def test_task_tables_and_stream_are_renamed_once_and_again_after_restoring_an_older_copy(tmp_path):
    import shutil
    from db import Database, migrate
    from db.migrations import Migration
    from db.schemas import TASK_RECORDS
    path = tmp_path / 'front.db'
    database = Database.sqlite(path)
    connection = database.connect()
    # The deployed history: the first three migrations, recorded under the old stream key and names.
    old_names = {1: 'front-owned external tasks and evidence outbox', 2: 'immutable front task ownership'}
    migrate(database, connection, 'turnstile.tasks',
            [Migration(one.version, old_names.get(one.version, one.name), one.sqlite) for one in TASK_RECORDS[:3]])
    with connection:
        connection.execute("INSERT INTO external_tasks(id,token_hash,model,fingerprint,subject,environment,machine) VALUES('batch_old','h','m','f','{}','{}','compute')")
    connection.close()
    before = tmp_path / 'before.db'
    shutil.copy2(path, before)

    def upgrade(file):
        target = Database.sqlite(file)
        opened = target.connect()
        try:
            migrate(target, opened, 'turnstile.benchmark_tasks', TASK_RECORDS)
            migrate(target, opened, 'turnstile.benchmark_tasks', TASK_RECORDS)  # a rerun changes nothing
            return stored(opened), opened.execute('SELECT id FROM benchmark_tasks').fetchall(), dict(opened.execute(
                "SELECT version,name FROM suite_schema_migrations WHERE stream='turnstile.benchmark_tasks'"))
        finally:
            opened.close()

    (tables, streams), rows, names = upgrade(path)
    assert {'benchmark_tasks', 'benchmark_task_evidence', 'benchmark_task_finishes', 'benchmark_task_reservations'} <= tables
    assert not {'external_tasks', 'front_task_evidence', 'front_task_finishes', 'external_task_reservations'} & tables
    assert streams == ['turnstile.benchmark_tasks'] and rows == [('batch_old',)]
    assert names == {one.version: one.name for one in TASK_RECORDS}
    # A copy restored from before the rename is renamed again at the next start.
    assert upgrade(before)[1] == [('batch_old',)]


def test_a_nodes_own_older_task_table_is_renamed_and_keeps_its_rows(tmp_path):
    from db import Database, migrate
    from db.schemas import TASKS
    database = Database.sqlite(tmp_path / 'loadcoach.db')
    connection = database.connect()
    try:
        migrate(database, connection, 'loadcoach.tasks', TASKS[:2])
        with connection:
            connection.execute("INSERT INTO external_tasks(id,token_hash,model,fingerprint,subject,environment) VALUES('batch_node','h','m','f','{}','{}')")
        migrate(database, connection, 'loadcoach.tasks', TASKS)
        migrate(database, connection, 'loadcoach.tasks', TASKS)
        tables, _ = stored(connection)
        assert 'benchmark_tasks' in tables and 'external_tasks' not in tables
        assert connection.execute('SELECT id FROM benchmark_tasks').fetchall() == [('batch_node',)]
    finally:
        connection.close()


# Nothing left under the old names


OLD = re.compile(r"\b(front_tasks|FrontTasks|FrontSettings|FRONT_TASKS|FrontOwnedBatches|front_task_finishes|front_task_evidence)\b"
                 r"|front-owned tasks")
# Where an old name is still needed: the rename itself, its migrations, readers of older files and reports.
KEPT = {'loadcoach/setting_names.py', 'db/schemas.py', 'db/migrations.py', 'weightroom/app.py',
        'scripts/split-benchmarks.py', 'loadcoach/tasks.py'}


def test_no_code_or_page_uses_the_old_names():
    found = []
    for folder in ('turnstile', 'weightroom', 'loadcoach', 'benchmarks', 'soundroom', 'db', 'toollocker', 'scripts', 'voice', 'spotter'):
        for source in (ROOT / folder).rglob('*'):
            if source.suffix not in ('.py', '.html', '.js', '.sh') or '__pycache__' in source.parts:
                continue
            relative = source.relative_to(ROOT).as_posix()
            if relative in KEPT:
                continue
            found += [f'{relative}: {match.group(0)}' for match in OLD.finditer(source.read_text(encoding='utf-8'))]
    assert not found, found
