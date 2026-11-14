"""Bounded owner-private extraction data in the control node's existing backed-up accounts store."""

import hashlib
import json
import secrets
import time

from db import database, migrate
from db.schemas import EXTRACTIONS
from weightroom.extraction import Problem

ACTIVE = ('queued', 'running')


def encoded(value):
    """Serialize private records deterministically as Unicode JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Extractions:
    """Own extraction assets, jobs, immutable revisions and presets on the control node."""
    def __init__(self, source):
        """Open the accounts store and apply extraction migrations."""
        source = database(source)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, 'weightroom.extractions', EXTRACTIONS)

    def asset(self, owner, identifier, content=False):
        """Read an unexpired source belonging to this stable owner."""
        columns = 'id,owner,name,mime,width,height,size,digest,created,expires' + (',original,prepared' if content else '')
        row = self.db.execute('SELECT ' + columns + ' FROM extraction_assets WHERE id=? AND owner=? AND expires>?',
                              (identifier, owner, time.time())).fetchone()
        return dict(zip(columns.split(','), row)) if row else None

    def add_asset(self, owner, asset):
        """Persist a verified source and derivative under account storage limits."""
        now, identifier = time.time(), 'exa_' + secrets.token_hex(16)
        with self.db:
            count, total = self.db.execute('SELECT COUNT(*),COALESCE(SUM(stored_size),0) FROM extraction_assets WHERE owner=?', (owner,)).fetchone()
            stored_size = len(asset['original']) + len(asset['prepared'])
            if count >= 100 or total + stored_size > 100 * 1024 * 1024:
                raise Problem('Keep at most 100 images and 100 MiB of stored original/derived image data per account. Delete older sources first.', 409, 'storage_limit')
            self.db.execute('INSERT INTO extraction_assets (id,owner,name,mime,width,height,size,digest,original,prepared,created,expires,stored_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, owner, *[asset[key] for key in ('name', 'mime', 'width', 'height', 'size', 'digest', 'original', 'prepared')], now, now + 86400, stored_size))
        return self.asset(owner, identifier)

    def one(self, owner, identifier):
        """Read one owned job and decode its structured records."""
        columns = 'id,owner,asset,spec,state,result,raw,error,provenance,usage,revision,reviewed,created,updated,expires'
        row = self.db.execute('SELECT ' + columns + ' FROM extraction_jobs WHERE id=? AND owner=? AND expires>?',
                              (identifier, owner, time.time())).fetchone()
        if not row:
            return None
        result = dict(zip(columns.split(','), row))
        for key in ('spec', 'result', 'error', 'provenance', 'usage'):
            result[key] = json.loads(result[key])
        result['reviewed'] = bool(result['reviewed'])
        return result

    def listing(self, owner, cursor='', limit=50):
        # Opaque IDs break timestamp ties; cursors always select records inside the caller's scope.
        """Return a bounded owner-scoped page using creation time and ID."""
        before = self.one(owner, cursor) if cursor else None
        query, values = 'SELECT id FROM extraction_jobs WHERE owner=? AND expires>?', [owner, time.time()]
        if cursor and not before:
            raise Problem('Unknown page cursor.')
        if before:
            query += ' AND (created<? OR (created=? AND id<?))'
            values += [before['created'], before['created'], cursor]
        query += ' ORDER BY created DESC,id DESC LIMIT ?'
        rows = self.db.execute(query, (*values, limit + 1)).fetchall()
        jobs = [self.one(owner, row[0]) for row in rows[:limit]]
        return jobs, jobs[-1]['id'] if len(rows) > limit else None

    def create(self, owner, spec, idempotency):
        """Create a bounded job or return the identical idempotent request."""
        now = time.time()
        digest = hashlib.sha256(encoded(spec).encode()).hexdigest()
        with self.db:
            row = self.db.execute('SELECT id,request_hash FROM extraction_jobs WHERE owner=? AND idempotency=?', (owner, idempotency)).fetchone()
            if row:
                if row[1] != digest:
                    raise Problem('This idempotency key belongs to different settings.', 409, 'idempotency_conflict')
                return self.one(owner, row[0]), False
            if not self.asset(owner, spec['asset_id']):
                raise Problem('No source image of yours has this ID.', 404, 'asset_not_found')
            if self.db.execute('SELECT COUNT(*) FROM extraction_jobs WHERE owner=?', (owner,)).fetchone()[0] >= 200:
                raise Problem('Keep at most 200 extraction jobs; delete older results first.', 409, 'storage_limit')
            if self.db.execute("SELECT COUNT(*) FROM extraction_jobs WHERE owner=? AND state IN ('queued','running')", (owner,)).fetchone()[0] >= 20:
                raise Problem('At most twenty extractions may be pending for one account.', 429, 'concurrency_limit')
            identifier = 'ext_' + secrets.token_hex(16)
            expires = now + (86400 if spec['temporary'] else 30 * 86400)
            self.db.execute('INSERT INTO extraction_jobs (id,owner,asset,spec,state,result,raw,error,provenance,usage,revision,reviewed,idempotency,request_hash,created,updated,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, owner, spec['asset_id'], encoded(spec), 'queued', '{}', '', '{}', '{}', '{}', 1, 0, idempotency, digest, now, now, expires))
            self.db.execute('UPDATE extraction_assets SET expires=CASE WHEN expires<? THEN ? ELSE expires END WHERE id=? AND owner=?',
                            (expires, expires, spec['asset_id'], owner))
        return self.one(owner, identifier), True

    def state(self, owner, identifier, state, error=None):
        """Transition only a pending job to the requested state."""
        with self.db:
            return self.db.execute("UPDATE extraction_jobs SET state=?,error=?,updated=? WHERE id=? AND owner=? AND state IN ('queued','running')",
                                   (state, encoded(error or {}), time.time(), identifier, owner)).rowcount == 1

    def finish(self, owner, identifier, data, raw, provenance, usage, needs_review):
        """Atomically retain validated output and its immutable first revision."""
        now = time.time()
        with self.db:
            changed = self.db.execute("UPDATE extraction_jobs SET state=?,result=?,raw=?,provenance=?,usage=?,updated=? WHERE id=? AND owner=? AND state='running'",
                ('needs_review' if needs_review else 'completed', encoded(data), raw, encoded(provenance), encoded(usage), now, identifier, owner)).rowcount
            if changed:
                self.db.execute('INSERT INTO extraction_revisions (job,revision,data,reviewed,created) VALUES (?,?,?,?,?)', (identifier, 1, encoded(data), 0, now))
        return bool(changed)

    def failed(self, owner, identifier, error, raw='', provenance=None, usage=None):
        """Retain failure evidence without overwriting terminal results."""
        with self.db:
            self.db.execute("UPDATE extraction_jobs SET state='failed',error=?,raw=?,provenance=?,usage=?,updated=? WHERE id=? AND owner=? AND state IN ('queued','running')",
                            (encoded(error), raw, encoded(provenance or {}), encoded(usage or {}), time.time(), identifier, owner))

    def review(self, owner, identifier, expected, data, reviewed, needs_review):
        """Save a correction with optimistic revision checking."""
        now = time.time()
        with self.db:
            changed = self.db.execute("UPDATE extraction_jobs SET result=?,reviewed=?,revision=revision+1,state=?,updated=? WHERE id=? AND owner=? AND revision=? AND state IN ('completed','needs_review')",
                (encoded(data), int(reviewed), 'needs_review' if needs_review else 'completed', now, identifier, owner, expected)).rowcount
            if not changed:
                raise Problem('Result changed or cannot be reviewed. Reload before saving.', 409, 'revision_conflict')
            self.db.execute('INSERT INTO extraction_revisions (job,revision,data,reviewed,created) VALUES (?,?,?,?,?)',
                            (identifier, expected + 1, encoded(data), int(reviewed), now))
        return self.one(owner, identifier)

    def remove(self, owner, identifier):
        """Delete a terminal job and its source when no references remain."""
        job = self.one(owner, identifier)
        if not job:
            raise Problem('No extraction of yours has this ID.', 404, 'extraction_not_found')
        if job['state'] in ACTIVE:
            raise Problem('Cancel this job before deleting it.', 409)
        with self.db:
            self.db.execute('DELETE FROM extraction_revisions WHERE job=?', (identifier,))
            self.db.execute('DELETE FROM extraction_jobs WHERE id=? AND owner=?', (identifier, owner))
            if not self.db.execute('SELECT id FROM extraction_jobs WHERE asset=? LIMIT 1', (job['asset'],)).fetchone():
                self.db.execute('DELETE FROM extraction_assets WHERE id=? AND owner=?', (job['asset'], owner))

    def remove_asset(self, owner, identifier):
        """Delete an owned source only when no retained job uses it."""
        if not self.asset(owner, identifier):
            raise Problem('No source image of yours has this ID.', 404, 'asset_not_found')
        with self.db:
            if self.db.execute('SELECT id FROM extraction_jobs WHERE asset=? AND owner=? LIMIT 1', (identifier, owner)).fetchone():
                raise Problem('Delete this image’s extraction jobs first.', 409, 'asset_in_use')
            self.db.execute('DELETE FROM extraction_assets WHERE id=? AND owner=?', (identifier, owner))

    def interrupted(self):
        """Fail abandoned work after restart without automatic replay."""
        with self.db:
            self.db.execute("UPDATE extraction_jobs SET state='failed',error=?,updated=? WHERE state IN ('queued','running')",
                            (encoded({'code': 'interrupted', 'message': 'Control node gateway restarted. Retry explicitly; previous upstream outcome may be unknown.'}), time.time()))

    def prune(self):
        """Expire retained jobs, revisions and unreferenced sources."""
        now = time.time()
        with self.db:
            self.db.execute('DELETE FROM extraction_revisions WHERE job IN (SELECT id FROM extraction_jobs WHERE expires<?)', (now,))
            self.db.execute("DELETE FROM extraction_jobs WHERE expires<? AND state NOT IN ('queued','running')", (now,))
            self.db.execute('DELETE FROM extraction_assets WHERE expires<? AND id NOT IN (SELECT asset FROM extraction_jobs)', (now,))

    def presets(self, owner):
        """Read this owner's task presets in creation order."""
        return [{'id': row[0], 'name': row[1], 'spec': json.loads(row[2])} for row in self.db.execute(
            'SELECT id,name,spec FROM extraction_presets WHERE owner=? ORDER BY created DESC', (owner,)).fetchall()]

    def save_preset(self, owner, name, spec):
        """Save a bounded task preset without source or retry references."""
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise Problem('Preset name must contain 1–80 characters.')
        values = {key: value for key, value in spec.items() if key not in ('asset_id', 'retry_of')}
        with self.db:
            if len(self.presets(owner)) >= 30:
                raise Problem('Keep at most thirty extraction presets.', 409)
            identifier = 'exp_' + secrets.token_hex(16)
            self.db.execute('INSERT INTO extraction_presets (id,owner,name,spec,created) VALUES (?,?,?,?,?)',
                            (identifier, owner, name.strip(), encoded(values), time.time()))
        return identifier

    def forget(self, owner):
        """Remove all extraction data belonging to a deleted identity."""
        with self.db:
            self.db.execute('DELETE FROM extraction_revisions WHERE job IN (SELECT id FROM extraction_jobs WHERE owner=?)', (owner,))
            for table in ('extraction_jobs', 'extraction_assets', 'extraction_presets'):
                self.db.execute('DELETE FROM ' + table + ' WHERE owner=?', (owner,))
