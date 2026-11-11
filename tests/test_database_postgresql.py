"""Maintenance contracts against an explicitly selected disposable PostgreSQL database."""
import asyncio

from db import migrate
from db.schemas import SPOTTER
from loadcoach.database_admin import DatabaseSource, summaries
from loadcoach.database_operations import DatabaseOperations
from test_database_operations import preview, finish
from test_postgresql import postgres  # noqa: F401


def seeded(postgres, tmp_path, backup_keys):
    backup_keys.configure(tmp_path / 'backups')
    with postgres.connection() as connection:
        migrate(postgres, connection, 'spotter.history', SPOTTER)
        with connection:
            connection.executemany('INSERT INTO readings VALUES(?,?)', [(1, '{}'), (2, '{}'), (1761955200, '{}')])
    source = DatabaseSource('history', None, 'Telemetry', database=postgres, bindings=('spotter.history',))
    return DatabaseOperations(lambda: {source.id: source}, tmp_path / 'jobs'), source


def test_postgresql_reviewed_purge_then_measured_full_reclamation(postgres, tmp_path, backup_keys):
    operations, source = seeded(postgres, tmp_path, backup_keys)
    summary = summaries({source.id: source}, 'gpu')[0]
    assert summary['engine'] == 'PostgreSQL' and summary['records_estimated'] and not summary['error']
    review = preview(operations, source)
    result = finish(operations, review)
    assert result['state'] == 'complete', result
    with postgres.connection() as connection:
        assert connection.execute('SELECT at FROM readings').fetchall() == [(1761955200.0,)]
    reclaim = finish(operations, preview(operations, source, action='reclaim'))
    assert reclaim['state'] == 'complete', reclaim
    outcome = reclaim['outcomes'][0]
    assert outcome['method'] == 'VACUUM FULL' and outcome['after_bytes'] > 0


def test_postgresql_full_reclamation_lock_timeout_preserves_data_and_backup(postgres, tmp_path, backup_keys):
    operations, source = seeded(postgres, tmp_path, backup_keys)
    review = preview(operations, source, action='reclaim')
    with postgres.connection() as reader:
        reader.execute('SELECT * FROM readings').fetchall()
        result = finish(operations, review)
    assert result['state'] == 'failed'
    assert result['outcomes'][0]['recovery']
    with postgres.connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 3


def test_postgresql_shared_store_lease_blocks_other_nodes(postgres, tmp_path, backup_keys):
    from loadcoach.database_operations import maintenance_lease
    from loadcoach.database_admin import source_identity
    operations, source = seeded(postgres, tmp_path, backup_keys)
    review = preview(operations, source)
    with maintenance_lease(source, source_identity(source)):
        result = finish(operations, review)
    assert result['state'] == 'failed'
    assert 'Another node holds maintenance' in result['outcomes'][0]['error']
