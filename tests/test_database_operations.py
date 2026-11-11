"""Real local stores: reviewed deletion, disk reclamation, receipts and restart safety."""
import asyncio
import json
import os
import sqlite3
import time

import pytest

from db import Database, migrate
from db.schemas import SPOTTER
from loadcoach.database_admin import DatabaseSource, summaries, merge_inventory, store_sources, source_identity
from loadcoach.database_jobs import DatabaseJobs
from loadcoach.database_operations import DatabaseOperations, OperationRefused, maintenance_lease
from db.recovery import RecoveryStore


@pytest.fixture
def telemetry(tmp_path, backup_keys):
    backup_keys.configure(tmp_path / 'backups')
    target = Database.sqlite(tmp_path / 'history.db')
    with target.connection() as connection:
        migrate(target, connection, 'spotter.history', SPOTTER)
        with connection:
            connection.execute('CREATE TABLE protected (id INTEGER PRIMARY KEY, at REAL)')
            connection.execute('INSERT INTO protected VALUES (1,1)')
            connection.executemany('INSERT INTO readings VALUES (?,?)', [(1, '{}'), (2, '{}'), (1761955200, '{}')])
    source = DatabaseSource('history.db', target.path, 'Telemetry', database=target, bindings=('spotter.history',))
    operations = DatabaseOperations(lambda: {source.id: source}, tmp_path / 'jobs')
    return operations, source


def preview(operations, source, **values):
    return operations.preview('owner', {'scope': [{'id': source.id, 'identity': source_identity(source)}],
        'action': 'purge', 'cutoff': '2025-11-01', **values})


def finish(operations, review):
    async def run():
        job = await operations.execute('owner', review['review'], True)
        await operations.jobs.close()
        return operations.jobs.get(job['id'], 'owner')
    return asyncio.run(run())


def test_frozen_utc_purge_keeps_new_backdated_rows_boundary_and_protected_tables(telemetry, tmp_path, backup_keys):
    operations, source = telemetry
    review = preview(operations, source)
    assert review['stores'][0]['count'] == 2
    with source.database.transaction() as connection:
        connection.execute('INSERT INTO readings VALUES(?,?)', (1.5, '{}'))
    result = finish(operations, review)
    assert result['state'] == 'complete' and result['outcomes'][0]['records_deleted'] == 2
    with source.database.connection() as connection:
        assert connection.execute('SELECT at FROM readings ORDER BY at').fetchall() == [(1.5,), (1761955200.0,)]
        assert connection.execute('SELECT * FROM protected').fetchall() == [(1, 1.0)]
    from loadcoach.backups import manifest, _safe_extract
    from loadcoach.backup_crypto import decrypt
    receipt = manifest(operations.recovery_root, result['outcomes'][0]['recovery'])
    assert receipt['verification']['state'] == 'verified'
    backup = operations.recovery_root / result['outcomes'][0]['recovery'] / 'archive.tar.gz.age'
    assert backup.stat().st_mode & 0o777 == 0o600
    archive = tmp_path / 'recovered.tar.gz'
    decrypt(backup, backup_keys.identity, archive)
    restored_root = tmp_path / 'recovered'; restored_root.mkdir()
    _safe_extract(archive, restored_root)
    with sqlite3.connect(restored_root / 'payload' / receipt['stores'][0]['archive']) as restored:
        assert restored.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 4
    with pytest.raises(FileExistsError):
        finish(operations, review)


def test_review_owner_expiry_schema_and_store_identity_are_enforced(telemetry, monkeypatch):
    operations, source = telemetry
    review = preview(operations, source)
    with pytest.raises(PermissionError):
        operations.jobs.consume(review['review'], 'other')
    with source.database.transaction() as connection:
        connection.execute('ALTER TABLE readings ADD COLUMN changed INTEGER')
    with pytest.raises(OperationRefused, match='schema changed'):
        finish(operations, review)
    review = preview(operations, source)
    operations.jobs.update(review['review'], expires_at=1)
    with pytest.raises(ValueError, match='expired'):
        finish(operations, review)
    with pytest.raises(OperationRefused, match='binding changed'):
        operations.preview('owner', {'action': 'reclaim', 'scope': [{'id': source.id, 'identity': 'wrong'}]})


def test_reclamation_measures_real_sqlite_file_shrink_and_refuses_low_disk(telemetry, monkeypatch):
    operations, source = telemetry
    with source.database.transaction() as connection:
        connection.execute('CREATE TABLE blobs(data BLOB)')
        connection.executemany('INSERT INTO blobs VALUES(?)', [(bytes(16384),)] * 100)
    with source.database.transaction() as connection:
        connection.execute('DELETE FROM blobs')
    review = preview(operations, source, action='reclaim')
    result = finish(operations, review)
    assert result['state'] == 'complete'
    outcome = result['outcomes'][0]
    assert outcome['before_bytes'] > outcome['after_bytes']
    assert outcome['reclaimed_bytes'] > 1000000
    from types import SimpleNamespace
    monkeypatch.setattr('loadcoach.database_operations.shutil.disk_usage', lambda _: SimpleNamespace(free=1))
    with pytest.raises(OperationRefused, match='headroom'):
        preview(operations, source, action='reclaim')


def test_orphans_missing_configured_stores_and_shared_file_identity(tmp_path):
    live, stale = tmp_path / 'live.db', tmp_path / 'old.db'
    for path in (live, stale):
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE items(id INTEGER)')
    stores = [RecoveryStore('live', Database.sqlite(live), ('spotter.history',)),
              RecoveryStore('missing', Database.sqlite(tmp_path / 'missing.db'), ('loadcoach.core',))]
    sources = store_sources(stores, tmp_path, {'old.db': 'Historical known filename'})
    rows = summaries(sources, 'front')
    assert next(row for row in rows if row['id'] == 'old.db')['expected'] is False
    assert next(row for row in rows if row['id'] == 'missing.db')['error']
    assert not (tmp_path / 'missing.db').exists()
    shared = next(row for row in rows if row['id'] == 'live.db')
    merged = merge_inventory([shared, {**shared, 'machine': 'worker'}])
    assert len(merged) == 1 and len(merged[0]['consumers']) == 2


def test_restart_marks_unleased_jobs_interrupted_and_keeps_owner_private(tmp_path):
    jobs = DatabaseJobs(tmp_path / 'jobs')
    row = jobs.create('owner', 'purge', ['history'])
    jobs.update(row['id'], state='running', outcomes=[{'id': 'history', 'records_deleted': 5}])
    recovered = DatabaseJobs(tmp_path / 'jobs').get(row['id'], 'owner')
    assert recovered['state'] == 'interrupted' and recovered['outcomes'][0]['records_deleted'] == 5
    assert (jobs.folder(row['id']) / 'job.json').stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        jobs.get('../outside', 'owner')


def test_custom_deletion_triggers_require_a_retention_policy_review(telemetry):
    operations, source = telemetry
    with source.database.transaction() as connection:
        connection.execute('CREATE TRIGGER custom_cleanup AFTER DELETE ON readings BEGIN DELETE FROM protected; END')
    with pytest.raises(OperationRefused, match='trigger or incoming reference'):
        preview(operations, source)
    with source.database.connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM protected').fetchone()[0] == 1


def test_shared_file_maintenance_lease_prevents_a_second_job(telemetry):
    operations, source = telemetry
    review = preview(operations, source)
    with maintenance_lease(source, source_identity(source)):
        result = finish(operations, review)
    assert result['state'] == 'failed'
    assert 'holds this physical database' in result['outcomes'][0]['error']
    with source.database.connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 3


def test_cancellation_stops_after_current_bounded_transaction(telemetry, monkeypatch):
    import threading
    operations, source = telemetry
    with source.database.transaction() as connection:
        connection.executemany('INSERT INTO readings VALUES(?,?)', [(at, '{}') for at in range(10, 1010)])
    review = preview(operations, source)
    entered, release = threading.Event(), threading.Event()
    original = operations._purge_batch
    def first_batch(*args):
        deleted = original(*args)
        entered.set()
        release.wait(5)
        return deleted
    monkeypatch.setattr(operations, '_purge_batch', first_batch)
    async def run():
        job = await operations.execute('owner', review['review'], True)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            operations.jobs.update(job['id'], cancel_requested=True)
        finally:
            release.set()
        await operations.jobs.close()
        return operations.jobs.get(job['id'], 'owner')
    result = asyncio.run(run())
    assert result['state'] == 'cancelled'
    assert result['outcomes'][0]['records_deleted'] == 250
    with source.database.connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 753
