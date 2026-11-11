"""Host-local database maintenance with frozen, expiring, single-use reviews."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from contextlib import contextmanager
from starlette.responses import JSONResponse

from db import connect
from db.recovery import RecoveryStore
from .database_admin import source_identity, summaries, catalogs
from .database_jobs import DatabaseJobs
from .backups import create_backup, new_id, manifest as backup_manifest
from .backup_crypto import recipients


class OperationRefused(ValueError):
    """An operator-readable refusal containing no driver or credential text."""


def revision(source):
    with read_connection(source) as connection:
        if source.database.engine == 'postgresql':
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '5000ms'")
            rows = connection.execute("SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema NOT LIKE 'pg_%' AND table_schema <> 'information_schema' ORDER BY table_schema,table_name,ordinal_position").fetchall()
            rows += connection.execute("SELECT c.oid::text, t.tgname, pg_get_triggerdef(t.oid), pg_get_functiondef(t.tgfoid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND n.nspname NOT LIKE 'pg_%' ORDER BY c.oid,t.tgname").fetchall()
            rows += connection.execute("SELECT c.conname, c.conrelid::text, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname NOT LIKE 'pg_%' ORDER BY c.conrelid,c.conname").fetchall()
            if connection.execute("SELECT to_regclass('suite_schema_migrations')").fetchone()[0]:
                rows += connection.execute('SELECT stream,version FROM suite_schema_migrations ORDER BY stream,version').fetchall()
        else:
            rows = connection.execute('SELECT name,sql FROM sqlite_schema ORDER BY name').fetchall()
            if any(row[0] == 'suite_schema_migrations' for row in rows):
                rows += connection.execute('SELECT stream,version FROM suite_schema_migrations ORDER BY stream,version').fetchall()
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


@contextmanager
def read_connection(source):
    connection = source.database.connect(connect_timeout=2) if source.database.engine == 'postgresql' else connect(source.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        yield connection
    finally:
        connection.close()


def storage_headroom(source, required):
    """Measure the actual database filesystem, refusing unobservable remote PostgreSQL disks."""
    if source.path is not None:
        folders = [source.path.parent]
    else:
        with source.database.connection(connect_timeout=2) as connection:
            address, directory = connection.execute("SELECT inet_server_addr()::text, current_setting('data_directory')").fetchone()
            tablespaces = connection.execute("SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE oid IN (SELECT dattablespace FROM pg_database WHERE datname=current_database() UNION SELECT reltablespace FROM pg_class WHERE reltablespace<>0)").fetchall()
        address = address.split('/')[0] if address else None
        local = {'127.0.0.1', '::1', None}
        try:
            local.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None))
        except OSError:
            pass
        if address not in local or not Path(directory).is_dir():
            raise OperationRefused('Cannot verify free space on the PostgreSQL server filesystem. Run full disk reclamation on that server, or choose routine VACUUM for reuse.')
        folders = [Path(directory), *(Path(row[0]) for row in tablespaces if row[0])]
        if any(not folder.is_dir() for folder in folders):
            raise OperationRefused('A PostgreSQL tablespace filesystem cannot be checked from this node. Use routine reuse or perform full reclamation on its server.')
    free = min(shutil.disk_usage(folder).free for folder in folders)
    if free < required + 64 * 1024 * 1024:
        raise OperationRefused('Insufficient database filesystem headroom. Free space and repeat the preview.')
    return {'free_bytes': free, 'required_bytes': required + 64 * 1024 * 1024}


@contextmanager
def maintenance_lease(source, identity):
    """Coordinate shared stores across service roots and PostgreSQL clients on other nodes."""
    if source.database.engine == 'sqlite':
        with source.path.open('rb') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OperationRefused('Another maintenance job holds this physical database.') from None
            yield
    else:
        with source.database.connection(connect_timeout=2, isolation_level=None) as connection:
            acquired = connection.execute("SELECT pg_try_advisory_lock(hashtext('suite-database-maintenance'),hashtext(?))", (identity,)).fetchone()[0]
            if not acquired:
                raise OperationRefused('Another node holds maintenance on this PostgreSQL database.')
            try:
                yield
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtext('suite-database-maintenance'),hashtext(?))", (identity,))


class DatabaseOperations:
    def __init__(self, sources, root, audit=None, *, machine='compute', recovery_prefix=''):
        self.sources = sources
        self.jobs = DatabaseJobs(root)
        self.audit = audit or (lambda *args, **kwargs: None)
        self.machine, self.recovery_prefix = machine, recovery_prefix
        self.recovery_root = Path(root).parent / 'backups'

    async def handle(self, request, owner):
        """Small authenticated API; caller and origin checks belong to the service wrapper."""
        operation = request.path_params['operation'].split('/')
        try:
            if request.method == 'GET' and operation[0] == 'details' and len(operation) == 2:
                source = self.sources().get(operation[1])
                if source is None:
                    return JSONResponse({'error': {'message': 'Unknown database store.'}}, status_code=404)
                summary = await asyncio.to_thread(summaries, {source.id: source}, '')
                rows = await asyncio.to_thread(catalogs, {source.id: source}, '')
                return JSONResponse({**summary[0], **rows[0]}, headers={'cache-control': 'no-store'})
            if request.method == 'POST' and operation == ['preview']:
                result = await asyncio.to_thread(self.preview, owner, await request.json())
                return JSONResponse(result, headers={'cache-control': 'no-store'})
            if request.method == 'POST' and operation == ['execute']:
                body = await request.json()
                result = await self.execute(owner, body.get('review'), body.get('confirmed'))
                return JSONResponse(result, status_code=202)
            if request.method == 'GET' and operation == ['jobs']:
                return JSONResponse({'jobs': self.jobs.recent(owner)}, headers={'cache-control': 'no-store'})
            if len(operation) in {2, 3} and operation[0] == 'jobs':
                row = self.jobs.get(operation[1], owner)
                if request.method == 'GET' and len(operation) == 2:
                    return JSONResponse(self.jobs.public(row), headers={'cache-control': 'no-store'})
                if request.method == 'POST' and operation[2:] == ['cancel']:
                    self.jobs.update(row['id'], cancel_requested=True)
                    return JSONResponse({'id': row['id'], 'notice': 'Cancellation requested; the current transaction may finish.'})
                if request.method == 'GET' and operation[2:] == ['recovery']:
                    if row['state'] in {'running', 'queued', 'cancelling'}:
                        raise OperationRefused('Wait for maintenance to stop before inspecting its recovery receipts.')
                    identifiers = [item['recovery'] for item in row['outcomes'] if item.get('recovery')]
                    if not identifiers:
                        raise OperationRefused('No recovery copy was created for this job.')
                    return JSONResponse({'machine': self.machine, 'recovery_sets': [backup_manifest(self.recovery_root, identifier) for identifier in identifiers],
                        'instructions': 'Encrypted archives stay on the owning server. Recover using your offline age identity and the documented backup restore procedure.'}, headers={'cache-control': 'no-store'})
            return JSONResponse({'error': {'message': 'Unknown database operation.'}}, status_code=404)
        except PermissionError:
            return JSONResponse({'error': {'message': 'This database operation belongs to another account.'}}, status_code=403)
        except (ValueError, OSError, KeyError, TypeError) as problem:
            message = str(problem) if isinstance(problem, OperationRefused) else 'The operation or review is invalid, expired or already used. Refresh and preview again.'
            return JSONResponse({'error': {'message': message}}, status_code=409)

    def _source(self, identifier, identity=None):
        source = self.sources().get(identifier)
        if source is None or not source.expected:
            raise OperationRefused('Maintenance requires a configured, bound database store.')
        if identity and source_identity(source) != identity:
            raise OperationRefused('The store binding changed. Refresh inventory and preview again.')
        return source

    def preview(self, owner, body):
        action = body.get('action')
        if action not in {'purge', 'reclaim'}:
            raise OperationRefused('Choose purge or reclaim.')
        scope = body.get('scope')
        if not isinstance(scope, list) or not scope or len(scope) > 100:
            raise OperationRefused('Select between 1 and 100 database stores.')
        cutoff = None
        if action == 'purge':
            try:
                date = datetime.strptime(body.get('cutoff', ''), '%Y-%m-%d').replace(tzinfo=UTC)
                if date > datetime.now(UTC) or date.year < 1970:
                    raise ValueError()
                cutoff = date.timestamp()
            except (ValueError, TypeError):
                raise OperationRefused('Choose a valid past UTC cutoff date.') from None
        mode = body.get('mode', 'full')
        if mode not in {'full', 'reuse'}:
            raise OperationRefused('Choose full reclamation or routine reuse.')
        plan, seen, reviewed_keys = [], set(), 0
        for item in scope:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('identity'), str) or not item['identity']:
                raise OperationRefused('Refresh the database inventory before maintenance.')
            source = self._source(item['id'], item['identity'])
            if item['identity'] in seen:
                continue
            seen.add(item['identity'])
            summary = summaries({source.id: source}, '')[0]
            if summary['error']:
                raise OperationRefused(summary['error'])
            row = dict(id=source.id, identity=item['identity'], engine=summary['engine'], action=action,
                       revision=revision(source), size=summary['size'], keys=[], eligible=False,
                       method='SQLite VACUUM' if summary['engine'] == 'SQLite' else 'VACUUM FULL' if mode == 'full' else 'VACUUM (ANALYZE)',
                       mode=mode, cutoff=cutoff, count=0, reason='Protected: no approved date-retention policy.')
            if action == 'purge' and 'spotter.history' in source.bindings:
                # Only Spotter owns this contract. Other date-like columns are never inferred.
                with read_connection(source) as connection:
                    if summary['engine'] == 'PostgreSQL':
                        connection.execute("SET LOCAL statement_timeout = '10000ms'")
                        guarded = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass('readings') AND NOT tgisinternal) OR EXISTS(SELECT 1 FROM pg_constraint WHERE confrelid=to_regclass('readings'))").fetchone()[0]
                    else:
                        guarded = bool(connection.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name='readings'").fetchone())
                        for name, in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                            guarded = guarded or any(row[2] == 'readings' for row in connection.execute('PRAGMA foreign_key_list(' + source.database.quote_identifier(name) + ')'))
                    if guarded:
                        raise OperationRefused('Telemetry has a custom trigger or incoming reference. Its retention policy needs review before deletion.')
                    keys = connection.execute('SELECT at FROM readings WHERE at < ? ORDER BY at LIMIT 1000001', (cutoff,)).fetchall()
                if len(keys) > 1000000:
                    raise OperationRefused('More than one million telemetry readings match this store. Choose an earlier cutoff and purge in stages.')
                reviewed_keys += len(keys)
                if reviewed_keys > 1000000:
                    raise OperationRefused('More than one million telemetry readings match this node review. Select fewer stores or an earlier cutoff and work in stages.')
                row.update(keys=[key[0] for key in keys], count=len(keys), eligible=True,
                           reason='Spotter telemetry readings only; strict at < UTC cutoff. New readings are excluded from the frozen review.')
            if action == 'reclaim' and (summary['engine'] == 'SQLite' or mode == 'full'):
                row['headroom'] = storage_headroom(source, 2 * summary['size'])
            plan.append(row)
        if action == 'reclaim' or any(row['count'] for row in plan):
            try:
                recipients(self.recovery_root)
            except Exception:
                raise OperationRefused('Configure encrypted backups on this owning node before maintenance: age tools and public recovery recipients are required. See Data and backups.') from None
        if shutil.disk_usage(self.jobs.root).free < 64 * 1024 ** 2 + reviewed_keys * 64:
            raise OperationRefused('Insufficient local space to persist this maintenance review.')
        review = self.jobs.create(owner, 'review', scope, plan=plan)
        return {'review': review['id'], 'expires_at': review['expires_at'], 'action': action,
                'stores': [{key: value for key, value in row.items() if key not in {'keys', 'revision'}} for row in plan],
                'notice': 'A verified recovery copy is created before each store is changed. Purge does not shrink files. Jobs stop between batches/stores on cancellation; completed work is not rolled back.'}

    async def execute(self, owner, review, confirmed):
        if confirmed is not True:
            raise OperationRefused('Confirm the reviewed deletion or maintenance locks first.')
        plan = self.jobs.consume(review, owner)
        for item in plan:
            source = self._source(item['id'], item['identity'])
            if await asyncio.to_thread(revision, source) != item['revision']:
                raise OperationRefused('The schema changed. Create a new maintenance preview.')
        row = self.jobs.create(owner, plan[0]['action'], [{'id': item['id'], 'identity': item['identity']} for item in plan])

        async def worker(identifier):
            outcomes = []
            for item in plan:
                if self.jobs.cancelled(identifier):
                    break
                result = {'id': item['id'], 'state': 'running', 'records_deleted': 0}
                outcomes.append(result)
                self.jobs.update(identifier, outcomes=outcomes)
                try:
                    source = self._source(item['id'], item['identity'])
                    if item['action'] == 'purge' and not item['count']:
                        result.update(state='skipped', reason=item['reason'])
                        self.jobs.update(identifier, outcomes=outcomes)
                        continue
                    with maintenance_lease(source, item['identity']):
                        if await asyncio.to_thread(revision, source) != item['revision']:
                            raise OperationRefused('Schema changed since preview; no further changes were applied.')
                        if shutil.disk_usage(self.jobs.root).free < item['size'] * 3 + 64 * 1024 * 1024:
                            raise OperationRefused('Insufficient space for a verified recovery copy.')
                        backup_id = new_id()
                        store_name = self.recovery_prefix + source.stores[0] if source.stores else source.id
                        await asyncio.to_thread(create_backup, self.recovery_root, backup_id, self.machine, [], [], [], [], 0,
                            stores=[RecoveryStore(store_name, source.database, source.bindings)])
                        result['recovery'] = backup_id
                        self.audit('recovery-verified', owner, database=source.id, job=identifier)
                        if self.jobs.cancelled(identifier):
                            result['state'] = 'cancelled'
                        elif item['action'] == 'purge':
                            for offset in range(0, len(item['keys']), 250):
                                if self.jobs.cancelled(identifier):
                                    break
                                deleted = await asyncio.to_thread(self._purge_batch, source, item, item['keys'][offset:offset + 250])
                                result['records_deleted'] += deleted
                                self.jobs.update(identifier, outcomes=outcomes)
                            result['state'] = 'cancelled' if self.jobs.cancelled(identifier) else 'complete'
                        else:
                            result.update(await asyncio.to_thread(self._reclaim, source, item))
                            result['state'] = 'complete'
                except Exception as problem:
                    result.update(state='failed', error=str(problem) if isinstance(problem, OperationRefused) else 'Maintenance failed; check locks, permissions, native tools and disk space on the owning database server.')
                    self.jobs.update(identifier, outcomes=outcomes, state='partial' if any(row['state'] == 'complete' or row['records_deleted'] for row in outcomes) else 'failed')
                    self.audit('maintenance-failed', owner, database=item['id'], job=identifier)
                    break
                self.jobs.update(identifier, outcomes=outcomes)
                self.audit('maintenance-finished', owner, database=item['id'], job=identifier, state=result['state'], records=result['records_deleted'])
        return self.jobs.start(row, worker)

    def _purge_batch(self, source, plan, keys):
        self._source(source.id, plan['identity'])
        if revision(source) != plan['revision']:
            raise OperationRefused('The schema changed during purge. Completed batches are recorded; refresh and preview the remaining records.')
        connection = (source.database.connect(connect_timeout=2) if source.database.engine == 'postgresql' else
                      connect(source.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=2))
        try:
            connection.execute('BEGIN' if source.database.engine == 'postgresql' else 'BEGIN IMMEDIATE')
            self._source(source.id, plan['identity'])
            if source.database.engine == 'postgresql':
                connection.execute("SET LOCAL lock_timeout = '2000ms'")
                connection.execute("SET LOCAL statement_timeout = '10000ms'")
            deleted = connection.execute('DELETE FROM readings WHERE at < ? AND at IN (' + ','.join('?' for _ in keys) + ')', (plan['cutoff'], *keys)).rowcount
            connection.commit()
            return deleted
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _reclaim(self, source, plan):
        before = summaries({source.id: source}, '')[0]['size']
        if source.database.engine == 'sqlite':
            storage_headroom(source, before * 2)
            connection = connect(source.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=2, isolation_level=None)
            started = time.monotonic()
            connection.set_progress_handler(lambda: int(time.monotonic() - started > 300), 10000)
            try:
                if connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                    raise OperationRefused('SQLite WAL checkpoint is busy. Finish active readers/writers and preview again.')
                connection.execute('VACUUM')
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            finally:
                connection.close()
        else:
            if plan['mode'] == 'full':
                storage_headroom(source, before * 2)
            with source.database.connection(connect_timeout=2, isolation_level=None) as connection:
                connection.execute("SET lock_timeout = '2000ms'")
                connection.execute("SET statement_timeout = '300000ms'")
                connection.execute('VACUUM (FULL, ANALYZE)' if plan['mode'] == 'full' else 'VACUUM (ANALYZE)')
        after = summaries({source.id: source}, '')[0]['size']
        return {'before_bytes': before, 'after_bytes': after, 'reclaimed_bytes': max(0, before - after), 'method': plan['method']}
