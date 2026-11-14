"""Persistent per-account/address and address-wide sign-in failure windows."""
import hashlib
import json
import math
import time


class FailureCounter:
    """Small bounded windows; every writer serializes the two affected counter rows."""

    def __init__(self, connection, setting):
        self.db, self.setting = connection, setting

    @staticmethod
    def scope(account, address):
        account = account.lower() or 'invalid:empty'
        if len(account) > 128:
            account = 'invalid:' + hashlib.sha256(account.encode()).hexdigest()
        return account, str(address or 'unknown')[:100]

    def remaining(self, account, address, now=None):
        now = time.time() if now is None else now
        account, address = self.scope(account, address)
        with self.db:
            rows = self.db.execute('SELECT locked_until FROM sign_in_failures WHERE address=? AND account IN (?,?)',
                                   (address,account,'')).fetchall()
        return max((max(0, math.ceil(until-now)) for (until,) in rows), default=0)

    def failed(self, account, address):
        """Atomically append a failure and return the active lock's Retry-After seconds."""
        account, address = self.scope(account, address)
        now, window = time.time(), self.setting('sign_in_window_minutes')*60
        maximum, lockout = self.setting('sign_in_max_failures'), self.setting('sign_in_lockout_minutes')*60
        until = 0
        with self.db:
            if self.db.engine == 'sqlite':
                self.db.execute('BEGIN IMMEDIATE')
            for who, limit in ((account, maximum), ('', maximum*4)):
                self.db.execute('INSERT INTO sign_in_failures(account,address,failures,locked_until,updated) VALUES(?,?,?,?,?) ON CONFLICT(account,address) DO NOTHING',
                                (who,address,'[]',0,now))
                locked_row = ' FOR UPDATE' if self.db.engine == 'postgresql' else ''
                failures, locked = self.db.execute('SELECT failures,locked_until FROM sign_in_failures WHERE account=? AND address=?'+locked_row,
                                                  (who,address)).fetchone()
                if locked > now:
                    until = max(until,locked)
                    continue
                recent = [at for at in json.loads(failures) if at > now-window]
                recent.append(now)
                if len(recent) >= limit:
                    locked, recent = now+lockout, []
                self.db.execute('UPDATE sign_in_failures SET failures=?,locked_until=?,updated=? WHERE account=? AND address=?',
                                (json.dumps(recent),locked,now,who,address))
                until = max(until,locked)
            # Counts are ephemeral security state, never account credentials or audit entries.
            self.db.execute('DELETE FROM sign_in_failures WHERE updated<? AND locked_until<?', (now-7*86400,now))
        return max(0,math.ceil(until-now))

    def succeeded(self, account, address):
        """A correct sign-in clears only its pair; it never resets the spraying counter."""
        account, address = self.scope(account,address)
        with self.db:
            self.db.execute('DELETE FROM sign_in_failures WHERE account=? AND address=?', (account,address))

    def listing(self, account=''):
        with self.db:
            rows = self.db.execute('SELECT account,address,locked_until FROM sign_in_failures WHERE locked_until>? ORDER BY account,address', (time.time(),)).fetchall()
        return [{'account':who, 'address':address, 'until':until} for who,address,until in rows
                if not account or who in (account.lower(),'')]

    def renamed(self, old, new):
        """Move pair counters inside the account rename transaction; keep the stricter lock."""
        for address, failures, locked, updated in self.db.execute(
                'SELECT address,failures,locked_until,updated FROM sign_in_failures WHERE account=?', (old,)).fetchall():
            other = self.db.execute('SELECT failures,locked_until,updated FROM sign_in_failures WHERE account=? AND address=?',
                                    (new,address)).fetchone()
            if other:
                failures = json.dumps(sorted(json.loads(failures)+json.loads(other[0]))[-400:])
                locked, updated = max(locked,other[1]), max(updated,other[2])
            self.db.execute('INSERT INTO sign_in_failures(account,address,failures,locked_until,updated) VALUES(?,?,?,?,?) '
                            'ON CONFLICT(account,address) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until,updated=excluded.updated',
                            (new,address,failures,locked,updated))
        self.db.execute('DELETE FROM sign_in_failures WHERE account=?', (old,))

    def unlock(self, account, address=''):
        """An explicit address also clears its shared spraying counter."""
        account = account.lower()
        with self.db:
            if address:
                _,address = self.scope(account,address)
                count = self.db.execute('DELETE FROM sign_in_failures WHERE account IN (?,?) AND address=?', (account,'',address)).rowcount
            else:
                count = self.db.execute('DELETE FROM sign_in_failures WHERE account=?', (account,)).rowcount
        return count
