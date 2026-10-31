"""Safety gates for the offline DB1 inventory, using disposable stores only."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from db.consolidation import audit, compare, readonly
from db.consolidation_import import telemetry, reliability
from db.consolidation_import import tasks as import_tasks, settings as import_settings


def store(tmp_path, name, sql):
    path = tmp_path / name
    with sqlite3.connect(path) as connection:
        connection.executescript(sql)
    return path


def test_readonly_refuses_missing_and_journalled_inputs_and_cannot_write(tmp_path):
    absent = tmp_path / "missing.db"
    with pytest.raises(ValueError), readonly(absent):
        pass
    assert not absent.exists()
    path = store(tmp_path, "snapshot #1?.db", "CREATE TABLE x(id); INSERT INTO x VALUES(1);")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with readonly(path) as connection:
        assert connection.execute("SELECT * FROM x").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM x")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    (tmp_path / (path.name + "-wal")).touch()
    with pytest.raises(ValueError), readonly(path):
        pass


def test_comparison_covers_every_key_and_never_emits_credentials(tmp_path):
    source = store(tmp_path, "source.db", """
        CREATE TABLE keys(hash TEXT PRIMARY KEY, password TEXT, settings TEXT);
        INSERT INTO keys VALUES('secret-key-hash','private-password','{"a":1,"b":2}');
        INSERT INTO keys VALUES('missing-hash','private-password','{}');
        INSERT INTO keys VALUES('changed-hash','old-password','{}');
    """)
    target = store(tmp_path, "target.db", """
        CREATE TABLE keys(hash TEXT PRIMARY KEY, password TEXT, settings TEXT, extra TEXT);
        INSERT INTO keys VALUES('secret-key-hash','private-password','{ "b": 2, "a": 1 }','new');
        INSERT INTO keys VALUES('changed-hash','new-password','{}','new');
    """)
    with readonly(source) as old, readonly(target) as new:
        result = compare(old, new, "keys")
    assert result["counts"] == {"identical": 1, "only_compute": 1, "different": 1}
    assert result["destination_only_columns"] == ["extra"]
    assert len({row["key_sha256"] for row in result["records"]}) == 3
    assert [row["changed_columns"] for row in result["records"] if row["state"] == "different"] == [["password"]]
    report = json.dumps(result)
    for value in ("secret-key-hash", "private-password", "old-password", "new-password", "missing-hash"):
        assert value not in report


def test_comparison_maps_forwarded_events_and_texts_and_rejects_other_machine(tmp_path):
    source = store(tmp_path, "old.db", """
        CREATE TABLE events(id INTEGER PRIMARY KEY,detail TEXT,hash TEXT,machine TEXT,source INTEGER);
        INSERT INTO events VALUES(7,'{"of":6}','old-chain',NULL,NULL);
        INSERT INTO events VALUES(8,'{}','old-chain',NULL,NULL);
        CREATE TABLE texts(entry INTEGER PRIMARY KEY,asked TEXT,said TEXT);
        INSERT INTO texts VALUES(7,'{"question":"hello"}','{}');
    """)
    target = store(tmp_path, "new.db", """
        CREATE TABLE events(id INTEGER PRIMARY KEY,detail TEXT,hash TEXT,machine TEXT,source INTEGER);
        INSERT INTO events VALUES(999,'{"of":6}','new-chain','compute',7);
        INSERT INTO events VALUES(1000,'{}','new-chain','other',8);
        CREATE TABLE texts(entry INTEGER PRIMARY KEY,asked TEXT,said TEXT);
        INSERT INTO texts VALUES(999,'{"question":"hello"}','{}');
        INSERT INTO texts VALUES(7,'{}','{}');
    """)
    with readonly(source) as old, readonly(target) as new:
        assert compare(old, new, "events")["counts"] == {"identical": 1, "only_compute": 1}
        assert compare(old, new, "texts")["counts"] == {"identical": 1}


def test_missing_source_column_and_duplicate_destination_are_conflicts(tmp_path):
    source = store(tmp_path, "old.db", "CREATE TABLE x(id TEXT PRIMARY KEY,value TEXT); INSERT INTO x VALUES('a','keep');")
    target = store(tmp_path, "new.db", "CREATE TABLE x(id TEXT); INSERT INTO x VALUES('a');")
    with readonly(source) as old, readonly(target) as new:
        result = compare(old, new, "x")
        assert result["counts"] == {"different": 1}
        assert result["records"][0]["changed_columns"] == ["value"]
    with sqlite3.connect(target) as connection:
        connection.execute("INSERT INTO x VALUES('a')")
    with readonly(source) as old, readonly(target) as new:
        assert compare(old, new, "x")["records"][0]["changed_columns"] == ["duplicate_destination_key"]


def test_blob_contents_and_composite_sample_keys_are_compared(tmp_path):
    source = store(tmp_path, "old.db", """
        CREATE TABLE benchmark_samples(run TEXT,sample TEXT,output BLOB,PRIMARY KEY(run,sample));
        INSERT INTO benchmark_samples VALUES('run','a',X'000102');
        INSERT INTO benchmark_samples VALUES('run','b',X'000103');
    """)
    target = store(tmp_path, "new.db", """
        CREATE TABLE benchmark_samples(run TEXT,sample TEXT,output BLOB,PRIMARY KEY(run,sample));
        INSERT INTO benchmark_samples VALUES('run','a',X'000102');
        INSERT INTO benchmark_samples VALUES('run','b',X'000104');
    """)
    with readonly(source) as old, readonly(target) as new:
        assert compare(old, new, "benchmark_samples")["counts"] == {"identical": 1, "different": 1}


def test_audit_keeps_spool_hashes_grants_settings_and_unknown_tables_blocked(tmp_path):
    source = store(tmp_path, "old.db", """
        CREATE TABLE file_hashes(path TEXT PRIMARY KEY); INSERT INTO file_hashes VALUES('model');
        CREATE TABLE schedule_grants(id TEXT PRIMARY KEY); INSERT INTO schedule_grants VALUES('grant');
        CREATE TABLE forwarded(through INTEGER); INSERT INTO forwarded VALUES(5);
        CREATE TABLE settings_changed(program TEXT,name TEXT,PRIMARY KEY(program,name));
        INSERT INTO settings_changed VALUES('voice-gateway','profile');
        CREATE TABLE unexpected(id INTEGER PRIMARY KEY); INSERT INTO unexpected VALUES(1);
    """)
    empty = store(tmp_path, "empty.db", "CREATE TABLE unrelated(id);")
    result = audit(source, empty, empty, empty)
    for name in ("file_hashes", "schedule_grants", "forwarded", "settings_changed"):
        assert result["tables"][name]["ownership"] == "retain_compute"
    assert result["checks"]["settings_changed_programs"] == {"voice-gateway": 1}
    assert result["gate"]["unclassified_tables"] == ["unexpected"]
    assert result["gate"]["deletion_authorized"] is False


def test_legacy_telemetry_uses_physical_host_and_preserves_timestamp_conflicts(tmp_path):
    source = store(tmp_path, "old.db", "CREATE TABLE users(name TEXT PRIMARY KEY);")
    front = store(tmp_path, "front.db", """
        CREATE TABLE machine_telemetry(fingerprint TEXT,at REAL,numbers TEXT,PRIMARY KEY(fingerprint,at));
        INSERT INTO machine_telemetry VALUES('correct',1,'{"gpu":2}');
        INSERT INTO machine_telemetry VALUES('wrong',2,'{"gpu":2}');
        INSERT INTO machine_telemetry VALUES('correct',3,'{"gpu":99}');
    """)
    history = store(tmp_path, "spotter.db", """
        CREATE TABLE readings(at REAL PRIMARY KEY,numbers TEXT);
        INSERT INTO readings VALUES(1,'{ "gpu": 2 }');
        INSERT INTO readings VALUES(2,'{"gpu":2}');
        INSERT INTO readings VALUES(3,'{"gpu":2}');
    """)
    with pytest.raises(ValueError, match="fingerprint"):
        audit(source, front, front, front, history)
    result = audit(source, front, front, front, history, telemetry_host="correct")
    assert result["spotter"]["counts"] == {"identical": 1, "only_compute": 1, "different": 1}
    assert result["spotter"]["span"] == (1.0, 3.0)


def test_audit_reports_broken_file_links_and_unmatched_tasks(tmp_path):
    source = store(tmp_path, "old.db", """
        CREATE TABLE batches(id TEXT PRIMARY KEY,input_file TEXT,output_file TEXT,error_file TEXT,status TEXT);
        INSERT INTO batches VALUES('task','gone','kept',NULL,'completed');
        CREATE TABLE files(id TEXT PRIMARY KEY); INSERT INTO files VALUES('kept');
        CREATE TABLE external_tasks(id TEXT PRIMARY KEY); INSERT INTO external_tasks VALUES('task');
    """)
    front = store(tmp_path, "front.db", "CREATE TABLE benchmark_runs(id TEXT PRIMARY KEY,state TEXT);")
    result = audit(source, front, front, front)
    assert result["checks"]["missing_batch_files"] == [{"batch": "task", "column": "input_file", "file": "gone"}]
    assert result["checks"]["tasks_without_front_run"] == ["task"]
    assert result["checks"]["batch_states"] == {"completed": 1}


def test_history_import_preview_idempotency_and_atomic_conflict(tmp_path):
    from loadcoach.log import Log
    history = store(tmp_path, "history.db", """
        CREATE TABLE readings(at REAL PRIMARY KEY,numbers TEXT);
        INSERT INTO readings VALUES(1,'{"memory":100}');
        INSERT INTO readings VALUES(2,'{"memory":200}');
    """)
    log = Log(str(tmp_path / "front.db"))
    host = "a" * 64
    with log.db:
        log.db.execute("INSERT INTO machine_hosts VALUES(?,?,?,?)", (host, "test", '["compute"]', 2))
    with readonly(history) as old:
        assert telemetry(old, log.db, host)["missing"] == 2
        assert log.db.execute("SELECT COUNT(*) FROM machine_telemetry").fetchone() == (0,)
        assert telemetry(old, log.db, host, apply=True)["after"] == 2
        assert telemetry(old, log.db, host, apply=True)["missing"] == 0
        with pytest.raises(ValueError, match="known"):
            telemetry(old, log.db, "b" * 64, apply=True)
    with log.db:
        log.db.execute("UPDATE machine_telemetry SET numbers='{}' WHERE at=2")
        log.db.execute("DELETE FROM machine_telemetry WHERE at=1")
    with readonly(history) as old, pytest.raises(ValueError, match="conflict"):
        telemetry(old, log.db, host, apply=True)
    assert log.db.execute("SELECT COUNT(*) FROM machine_telemetry").fetchone() == (1,)
    log.db.close()


@pytest.mark.parametrize("bad", ['[]', '{"memory":"unknown"}', '{"memory":NaN}'])
def test_history_import_rejects_invalid_series_without_writes(tmp_path, bad):
    from loadcoach.log import Log
    history = store(tmp_path, "history.db", "CREATE TABLE readings(at REAL PRIMARY KEY,numbers TEXT);")
    with sqlite3.connect(history) as connection:
        connection.execute("INSERT INTO readings VALUES(1,?)", (bad,))
    log = Log(":memory:")
    log.db.execute("INSERT INTO machine_hosts VALUES(?,?,?,?)", ("a" * 64, "test", '[]', 1))
    with readonly(history) as old, pytest.raises(ValueError):
        telemetry(old, log.db, "a" * 64, apply=True)
    assert log.db.execute("SELECT COUNT(*) FROM machine_telemetry").fetchone() == (0,)


def test_reliability_import_requires_event_and_immutable_identity_and_keeps_state(tmp_path):
    from loadcoach.log import Log
    from loadcoach.reliability import Reliability
    from loadcoach.users import Users
    from loadcoach.groups import read_group

    old = Log(str(tmp_path / "old.db"))
    past = Reliability(old.db)
    front = Log(str(tmp_path / "front.db"))
    users = Users(front.db.database, {"admin": read_group("admin", {"can": ["superuser"]})})
    users.add("owner", "admin")
    identity = users.identity("owner")
    current = Reliability(Log(str(tmp_path / "roles.db")).db)
    key = ("model-id", "fingerprint", "")
    past.record(7, "owner", "small", key, 0, "success", now=10, owner_id=identity)
    old.db.close()
    with readonly(tmp_path / "old.db") as source:
        with pytest.raises(ValueError, match="attribution"):
            reliability(source, front.db, current.db, apply=True)
        front.take("compute", [{"id": 7, "at": 10, "kind": "request", "what": "asked", "user": "owner",
            "caller": "test", "model": "small", "trace": None, "level": None,
            "detail": json.dumps({"model_id": "model-id"}), "text": None, "hash": None}], [], 0)
        current.reset(key)
        state = current.state(key)
        assert reliability(source, front.db, current.db)["missing"] == 1
        assert reliability(source, front.db, current.db, apply=True)["after"] == 1
        assert reliability(source, front.db, current.db, apply=True)["missing"] == 0
        assert current.state(key) == state
        with current.db:
            current.db.execute("UPDATE reliability_outcomes SET feedback=1")
        with pytest.raises(ValueError, match="conflict"):
            reliability(source, front.db, current.db, apply=True)
    front.db.close(); users.db.close(); current.db.close()


def test_terminal_task_import_preserves_bytes_ids_and_retention_holes_and_refuses_collisions(tmp_path):
    from db import migrate
    from db.schemas import TASKS, FRONT_TASKS
    from loadcoach.batches import Batches
    source = Batches(str(tmp_path / 'source.db'))
    migrate(source.db.database, source.db, 'loadcoach.tasks', TASKS)
    file = source.put_file('owner', 'task.jsonl', 'batch', b'original bytes')
    source.start('owner', '/loadcoach/task', file['id'], [{'custom_id': 'task', 'body': {'model': 'small'}}], number='batch_old')
    with source.db:
        source.db.execute('INSERT INTO external_tasks VALUES(?,?,?,?,?,?,?)', ('batch_old', 'hash', 'small', 'fp', '{}', '{}', 'mid'))
    target = Batches(str(tmp_path / 'target.db'))
    migrate(target.db.database, target.db, 'turnstile.tasks', FRONT_TASKS)
    source.db.close()
    with readonly(tmp_path / 'source.db') as old, pytest.raises(ValueError, match='terminal'):
        import_tasks(old, target.db, apply=True)
    source = Batches(str(tmp_path / 'source.db'))
    with source.db:
        source.db.execute("UPDATE batches SET status='completed',output_file='already-pruned',ended=1")
    source.db.close()
    with readonly(tmp_path / 'source.db') as old:
        preview = import_tasks(old, target.db)
        assert preview['tables']['external_tasks']['missing'] == 1
        assert target.listing('owner') == []
        assert import_tasks(old, target.db, apply=True)['tables']['batches']['after'] == 1
        assert target.content(file['id']) == b'original bytes'
        assert target.get('batch_old')['output_file_id'] == 'already-pruned'
        assert import_tasks(old, target.db, apply=True)['tables']['files']['missing'] == 0
        with target.db:
            target.db.execute("UPDATE files SET content=?", (b'conflicting bytes',))
        with pytest.raises(ValueError, match='conflict'):
            import_tasks(old, target.db, apply=True)
    target.db.close()


def test_front_settings_import_copies_only_front_programs_and_never_overwrites(tmp_path):
    from loadcoach.settings import Changes
    source = Changes(str(tmp_path / 'old.db'))
    target = Changes(str(tmp_path / 'new.db'))
    source.put('voice-gateway', 'hermes_model', 'chosen', 'owner')
    source.put('loadcoach', 'model_idle_minutes', 0, 'owner')
    source.seen('weightroom', {'timezone': {'value': 'UTC'}})
    source.seen('loadcoach', {'max_wait': {'value': 600}})
    source.db.close()
    with readonly(tmp_path / 'old.db') as old:
        assert import_settings(old, target.db, apply=True)['tables']['settings_changed']['after'] == 1
        assert target.of('voice-gateway') == {'hermes_model': 'chosen'}
        assert target.of('loadcoach') == {}
        assert import_settings(old, target.db, apply=True)['tables']['settings_seen']['missing'] == 0
        target.put('voice-gateway', 'hermes_model', 'newer', 'owner')
        with pytest.raises(ValueError, match='conflict'):
            import_settings(old, target.db, apply=True)
    target.db.close()
def test_deployed_front_task_schema_upgrades_without_inheriting_recreated_names(tmp_path):
    from db import Database, migrate
    from db.schemas import FRONT_TASKS
    import json

    database = Database.sqlite(tmp_path / 'front.db')
    connection = database.connect()
    # The first deployed schema predates the explicit ownership column.
    initial = FRONT_TASKS[0]
    from db.migrations import Migration
    migrate(database, connection, 'turnstile.tasks', (
        Migration(initial.version, initial.name, initial.sqlite.replace(', owner_id TEXT', '')),
    ))
    with connection:
        for number, subject in [('frozen', {'owner_id': 'original-identity'}), ('unknown', {})]:
            connection.execute('INSERT INTO external_tasks(id,token_hash,model,fingerprint,subject,environment,machine) VALUES(?,?,?,?,?,?,?)',
                (number, 'hash', 'model', 'fingerprint', json.dumps(subject), '{}', 'compute'))
    migrate(database, connection, 'turnstile.tasks', FRONT_TASKS)
    migrate(database, connection, 'turnstile.tasks', FRONT_TASKS)
    assert dict(connection.execute('SELECT id,owner_id FROM external_tasks')) == {
        'frozen': 'original-identity', 'unknown': None,
    }
    connection.close()


def retirement_fixture(tmp_path):
    """A complete synthetic recovery pair, with deliberately bloated legacy compute storage."""
    import shutil
    import tarfile
    from db import Database, migrate
    from db import schemas
    from db.consolidation_retire import prepare
    target = tmp_path / 'project/data/loadcoach.db'
    target.parent.mkdir(parents=True)
    database = Database.sqlite(target)
    connection = database.connect()
    streams = {
        'groups': schemas.GROUPS, 'users': schemas.USERS, 'roles': schemas.ROLES,
        'role_evidence': schemas.ROLE_EVIDENCE, 'reliability': schemas.RELIABILITY,
        'batches': schemas.BATCHES, 'tasks': schemas.TASKS, 'settings': schemas.SETTINGS,
        'models': schemas.MODELS, 'log': schemas.LOG, 'outside': schemas.OUTSIDE_SERVICES,
    }
    for name, migrations in streams.items():
        migrate(database, connection, 'loadcoach.' + name, migrations)
    migrate(database, connection, 'weightroom.benchmarks', schemas.BENCHMARKS)
    with connection:
        connection.execute("INSERT INTO groups(name,settings) VALUES('owner','{}')")
        connection.execute("INSERT INTO users(name,grp,identity) VALUES('owner','owner','immutable')")
        connection.execute("INSERT INTO models(name,settings) VALUES('kept',?)", ('{"flags":{}}',))
        connection.execute('CREATE TABLE temporary_bloat(payload BLOB)')
        connection.execute('INSERT INTO temporary_bloat VALUES(?)', (b'x' * 2**20,))
        connection.execute('DROP TABLE temporary_bloat')
    connection.close()
    recovery, outside = tmp_path / 'recovery', tmp_path / 'outside'
    recovery.mkdir(); outside.mkdir()
    for name in ('loadcoach', 'spotter', 'accounts', 'roles', 'benchmarks'):
        shutil.copy2(target, recovery / (name + '.db'))
    for side, names in [('compute', ('loadcoach', 'spotter')), ('front', ('accounts', 'roles', 'benchmarks'))]:
        folder = recovery / side; folder.mkdir()
        (folder / 'manifest.json').write_text(json.dumps({'id': 'paired-fixture', 'machine': side, 'verification': {'state': 'verified'}}))
        with tarfile.open(folder / 'archive.tar.gz', 'w:gz') as archive:
            for name in names:
                archive.add(recovery / (name + '.db'), arcname='payload/data/' + name + '.db')
    for name in ('loadcoach', 'spotter'):
        shutil.copy2(recovery / (name + '.db'), outside / (name + '.db'))
    (outside / 'retention.json').write_text(json.dumps({'backup_id': 'paired-fixture', 'owner_instruction_required': True}))
    report = audit(recovery / 'loadcoach.db', recovery / 'accounts.db', recovery / 'roles.db', recovery / 'benchmarks.db')
    approved = tmp_path / 'approved.json'; approved.write_text(json.dumps(report))
    return database, prepare(target, recovery, outside, approved)


def test_retirement_preserves_live_tables_vacuums_and_transfers_without_resurrection(tmp_path):
    from db import Database
    from db.consolidation_retire import retire
    from db.schemas import RETIRED_COMPUTE_TABLES
    from db.transfer import copy_store
    from loadcoach.app import build_app
    from loadcoach.config import Config
    database, proof = retirement_fixture(tmp_path)
    assert retire(database.path, proof)['state'] == 'preview'
    result = retire(database.path, proof, apply=True)
    assert result['state'] == 'retired' and result['after_bytes'] < result['before_bytes']
    assert retire(database.path, proof, apply=True)['state'] == 'already_retired'
    with readonly(database.path) as source:
        assert not set(RETIRED_COMPUTE_TABLES) & set(row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        assert source.execute('SELECT name,settings FROM models').fetchall() == [('kept', '{"flags":{}}')]
    with pytest.raises(ValueError, match='retired front-owned'):
        build_app(Config(database=str(database.path)))
    copy = Database.sqlite(tmp_path / 'transferred.db')
    assert copy_store(database, copy)['state'] == 'verified'
    with copy.connection() as source:
        assert not set(RETIRED_COMPUTE_TABLES) & set(row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def test_retirement_refuses_changed_rows_recovery_and_live_journals(tmp_path):
    from db.consolidation_retire import retire
    database, proof = retirement_fixture(tmp_path)
    with database.connection() as connection:
        with connection:
            connection.execute("UPDATE groups SET settings='changed'")
    with pytest.raises(ValueError, match='data changed'):
        retire(database.path, proof, apply=True)
    with database.connection() as connection:
        with connection:
            connection.execute("UPDATE groups SET settings='{}'")
    journal = Path(str(database.path) + '-wal')
    journal.touch()
    with pytest.raises(ValueError, match='journal'):
        retire(database.path, proof, apply=True)
    journal.unlink()
    asset = Path(next(iter(proof['assets'])))
    asset.write_bytes(asset.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='recovery asset changed'):
        retire(database.path, proof, apply=True)


def test_retirement_rolls_back_drops_if_retained_data_changes(tmp_path, monkeypatch):
    from dataclasses import replace
    from db import schema_version
    from db import consolidation_retire as retirement
    database, proof = retirement_fixture(tmp_path)
    def unsafe(database, connection):
        connection.execute('DROP TABLE keys')
        connection.execute("UPDATE models SET settings='changed'")
    monkeypatch.setattr(retirement, 'COMPUTE_RETIREMENT', (replace(retirement.COMPUTE_RETIREMENT[0], sqlite=unsafe),))
    with pytest.raises(ValueError, match='Retained compute data changed'):
        retirement.retire(database.path, proof, apply=True)
    with readonly(database.path) as source:
        assert source.execute("SELECT 1 FROM sqlite_master WHERE name='keys'").fetchone()
        assert source.execute('SELECT settings FROM models').fetchone() == ('{"flags":{}}',)
        assert schema_version(source, 'loadcoach.retirement') == 0


@pytest.mark.parametrize('change', ["UPDATE groups SET settings='new'", 'DELETE FROM users'])
def test_retirement_preparation_refuses_new_conflicts_or_unmoved_rows(tmp_path, change):
    import tarfile
    from db import Database
    from db.consolidation_retire import prepare
    database, _ = retirement_fixture(tmp_path)
    recovery = tmp_path / 'recovery'
    with Database.sqlite(recovery / 'accounts.db').transaction() as connection:
        connection.execute(change)
    with tarfile.open(recovery / 'front/archive.tar.gz', 'w:gz') as archive:
        for name in ('accounts', 'roles', 'benchmarks'):
            archive.add(recovery / (name + '.db'), arcname='payload/data/' + name + '.db')
    with pytest.raises(ValueError, match='unmoved or newly conflicting'):
        prepare(database.path, recovery, tmp_path / 'outside', tmp_path / 'approved.json')
