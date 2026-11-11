"""Private, restart-visible database jobs and expiring review tokens."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import shutil
import time
from pathlib import Path

ID = __import__('re').compile(r'[a-f0-9]{32}')
ACTIVE = {'queued', 'running', 'cancelling'}


class DatabaseJobs:
    """Persist receipts without persisting caller credentials or replaying interrupted work."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.tasks = set()
        self._recover()

    def folder(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ValueError('Invalid database operation identifier.')
        return self.root / identifier

    def write(self, identifier, name, value):
        folder = self.folder(identifier)
        folder.mkdir(mode=0o700, exist_ok=True)
        temporary = folder / ('.' + name + '.' + secrets.token_hex(4))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / name)
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def read(self, identifier, name='job.json'):
        return json.loads((self.folder(identifier) / name).read_text())

    def _recover(self):
        for folder in self.root.iterdir():
            if not folder.is_dir() or not ID.fullmatch(folder.name):
                continue
            try:
                row = self.read(folder.name)
                if row.get('download_expires_at', float('inf')) < time.time():
                    (folder / 'export.zip').unlink(missing_ok=True)
                if row['expires_at'] < time.time():
                    if row['state'] not in ACTIVE:
                        shutil.rmtree(folder)
                    continue
                if row['state'] in ACTIVE:
                    with (folder / 'lease').open('a') as lease:
                        try:
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        row.update(state='interrupted', error='Service restarted during this operation. Inspect per-store outcomes before starting new work.')
                        self.write(folder.name, 'job.json', row)
            except (OSError, ValueError, KeyError):
                continue

    def get(self, identifier, owner):
        row = self.read(identifier)
        if row['owner'] != owner:
            raise PermissionError('This operation belongs to another account.')
        if row['expires_at'] < time.time():
            raise ValueError('This operation has expired.')
        return row

    def recent(self, owner):
        self._recover()
        result = []
        for folder in self.root.iterdir():
            if ID.fullmatch(folder.name) and (folder / 'job.json').is_file():
                try:
                    row = self.get(folder.name, owner)
                    if row['kind'] != 'review':
                        result.append(self.public(row))
                except (OSError, ValueError, PermissionError, KeyError):
                    pass
        return sorted(result, key=lambda row: row['created_at'], reverse=True)[:20]

    @staticmethod
    def public(row):
        return {key: value for key, value in row.items() if key not in {'owner', 'plan'}}

    def create(self, owner, kind, scope, *, plan=None):
        self._recover()
        active = 0
        for folder in self.root.iterdir():
            if ID.fullmatch(folder.name) and (folder / 'job.json').is_file():
                try:
                    active += self.read(folder.name)['state'] in ACTIVE
                except (OSError, ValueError, KeyError):
                    continue
        if active >= 2:
            raise ValueError('Two database jobs are already active on this service. Wait for one to finish.')
        if len(list(self.root.iterdir())) >= 200:
            self._recover()
            if len(list(self.root.iterdir())) >= 200:
                raise ValueError('Database operation storage is full. Remove expired receipts before retrying.')
        identifier, now = secrets.token_hex(16), time.time()
        row = dict(id=identifier, owner=owner, kind=kind, scope=scope, state='review' if kind == 'review' else 'queued',
                   created_at=now, expires_at=now + (600 if kind == 'review' else 7 * 86400),
                   outcomes=[], error='', cancel_requested=False)
        self.write(identifier, 'job.json', row)
        if plan is not None:
            self.write(identifier, 'plan.json', plan)
        return row

    def update(self, identifier, **changes):
        descriptor = os.open(self.folder(identifier) / 'metadata.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = self.read(identifier)
            row.update(changes)
            self.write(identifier, 'job.json', row)
            return row

    def cancelled(self, identifier):
        return self.read(identifier)['cancel_requested']

    def start(self, row, worker):
        # Lock before scheduling, so another worker cannot mistake a queued job for a restart.
        lease = (self.folder(row['id']) / 'lease').open('a')
        os.chmod(lease.name, 0o600)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)

        async def run():
            try:
                self.update(row['id'], state='running')
                await worker(row['id'])
                latest = self.read(row['id'])
                if latest['state'] in ACTIVE:
                    self.update(row['id'], state='cancelled' if latest['cancel_requested'] else 'complete', completed_at=time.time())
            except asyncio.CancelledError:
                self.update(row['id'], state='interrupted', error='Service stopped. Review the outcome before retrying.')
                raise
            except Exception:
                self.update(row['id'], state='failed', error='Database operation failed. Check the owning node, permissions, disk space and tool availability.')
            finally:
                lease.close()
        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.public(row)

    def consume(self, identifier, owner):
        """Claim a review once across processes before any irreversible work is started."""
        row = self.get(identifier, owner)
        if row['kind'] != 'review':
            raise ValueError('This is not a database review.')
        descriptor = os.open(self.folder(identifier) / 'consumed', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        directory = os.open(self.folder(identifier), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return self.read(identifier, 'plan.json')

    async def close(self):
        # Let in-flight bounded DB transactions finish, preserving accurate receipts.
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
