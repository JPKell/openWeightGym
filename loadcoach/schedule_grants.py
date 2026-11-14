"""Revocable, job-specific grants for recurring work. Never general API/session credentials."""
import hashlib
import json
import re
import secrets
import time

from db import migrate
from db.schemas import SCHEDULE_GRANTS
from .schedule_time import validate


class ScheduleGrants:
    """Durable hashed authorization grants for scheduled compute work."""
    def __init__(self, db, users):
        """Open schedule-grant storage and apply its portable migrations."""
        self.db, self.users = db, users
        migrate(db.database, db, 'loadcoach.schedules', SCHEDULE_GRANTS)

    def owner(self, identity):
        """The account a grant was made by, as control node says it is now. Ask `users.fresh()` first."""
        return self.users.by_identity(identity)

    def permitted(self, owner, spec):
        """Check current account abilities for the requested schedule kind."""
        return bool(owner and owner.can('manage-settings') and (spec['kind'] == 'backup' or owner.can('benchmark')))

    def put(self, identifier, token, spec, owner):
        """Persist a hashed schedule grant with its owner and validated specification."""
        spec = validate(spec)
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('Schedule ID must be 32 hexadecimal digits.')
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError('Provide a scoped token of 32 to 128 characters.')
        if not self.permitted(owner, spec):
            raise PermissionError('Schedules require manage-settings and benchmarks also require benchmark.')
        identity = owner.identity
        encoded, digest = json.dumps(spec, sort_keys=True), hashlib.sha256(token.encode()).hexdigest()
        with self.db:
            found = self.db.execute('SELECT owner_id, spec, token_hash FROM schedule_grants WHERE id=?', (identifier,)).fetchone()
            if found and found != (identity, encoded, digest):
                raise ValueError('That schedule ID is already in use.')
            self.db.execute('INSERT INTO schedule_grants VALUES(?, ?, ?, ?, 1, ?) ON CONFLICT(id) DO NOTHING', (identifier, identity, digest, encoded, time.time()))

    def authorized(self, identifier, token, due=None):
        """Resolve a grant only while its owner and current permissions still allow it."""
        row = self.db.execute('SELECT owner_id, spec, token_hash, enabled, valid_from FROM schedule_grants WHERE id=?', (identifier,)).fetchone()
        if not row or not row[3] or not secrets.compare_digest(row[2], hashlib.sha256(token.encode()).hexdigest()):
            raise PermissionError('Missing, revoked or paused schedule authorization.')
        if due is not None and due < row[4]:
            raise PermissionError('This occurrence predates schedule authorization.')
        spec, owner = json.loads(row[1]), self.owner(row[0])
        if not self.permitted(owner, spec):
            raise PermissionError('The schedule owner is disabled, removed or no longer permitted.')
        return owner, spec

    def replace(self, identifier, token, spec, owner, enabled=True):
        """Replace a grant token and schedule specification after validation."""
        spec = validate(spec)
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError('Provide a scoped token of 32 to 128 characters.')
        if type(enabled) is not bool:
            raise ValueError('enabled must be true or false.')
        if not self.permitted(owner, spec):
            raise PermissionError('Schedules require manage-settings and benchmarks also require benchmark.')
        identity = owner.identity
        with self.db:
            row = self.db.execute('SELECT owner_id FROM schedule_grants WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('No such schedule grant.')
            if row[0] != identity:
                raise PermissionError('Only the schedule owner may edit its authorization.')
            self.db.execute('UPDATE schedule_grants SET spec=?,token_hash=?,enabled=?,valid_from=? WHERE id=? AND owner_id=?',
                (json.dumps(spec, sort_keys=True), hashlib.sha256(token.encode()).hexdigest(), int(enabled), time.time(), identifier, identity))

    def enable(self, identifier, enabled):
        """Enable or disable the selected schedule authorization grant."""
        row = self.db.execute('SELECT owner_id, spec FROM schedule_grants WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('No such schedule grant.')
        if enabled and not self.permitted(self.owner(row[0]), json.loads(row[1])):
            raise PermissionError('The schedule owner is no longer permitted.')
        with self.db:
            self.db.execute('UPDATE schedule_grants SET enabled=? WHERE id=?', (int(enabled), identifier))
