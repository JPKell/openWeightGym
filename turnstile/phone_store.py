"""Private telephone proofs in the existing control node accounts store."""

import asyncio
from dataclasses import replace
import secrets
import time
from db import migrate
from db.schemas import TELEPHONE
from loadcoach.users import hash_password, check_password, hash_key
from loadcoach.groups import limited
from voice.phone_access import digest, masked


class PhoneStore:
    def __init__(self, users, folder):
        self.users, self.db, self.folder = users, users.db, folder
        migrate(self.db.database, self.db, 'loadcoach.telephone', TELEPHONE)

    def pin_saved(self, number):
        return bool(self.db.execute('SELECT 1 FROM telephone_pins WHERE number_key=?', (digest(self.folder, number),)).fetchone())

    async def set_pin(self, number, pin):
        import re
        if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{4,12}', pin):
            raise ValueError('Use a PIN of 4 to 12 digits.')
        verifier = await asyncio.to_thread(hash_password, pin)
        with self.db:
            self.db.execute('INSERT INTO telephone_pins(number_key,verifier) VALUES (?,?) ON CONFLICT(number_key) DO UPDATE SET verifier=excluded.verifier',
                            (digest(self.folder, number), verifier))

    async def check_pin(self, number, pin):
        if not isinstance(pin, str) or not 4 <= len(pin) <= 12 or not pin.isascii() or not pin.isdigit():
            return False
        row = self.db.execute('SELECT verifier FROM telephone_pins WHERE number_key=?', (digest(self.folder, number),)).fetchone()
        return bool(row and await asyncio.to_thread(check_password, row[0], pin))

    def remove_pin(self, number):
        with self.db:
            self.db.execute('DELETE FROM telephone_pins WHERE number_key=?', (digest(self.folder, number),))

    def own(self, owner, mask=False):
        return [{'id': key, 'number': masked(number) if mask else number, 'created': created}
                for key, number, created in self.db.execute('SELECT number_key,number,created FROM telephone_numbers WHERE owner=? ORDER BY created', (owner,))]

    def pending(self, owner):
        return [{'number': number, 'expires': expires} for number, expires in self.db.execute(
            'SELECT number,expires FROM telephone_verifications WHERE owner=? AND expires>? AND attempts<5 ORDER BY created', (owner, time.time()))]

    def registered(self, number):
        row = self.db.execute('SELECT owner FROM telephone_numbers WHERE number_key=?', (digest(self.folder, number),)).fetchone()
        return self.users.by_identity(row[0]) if row else None

    def lock_owner(self, owner):
        if self.db.engine == 'sqlite':
            self.db.execute('BEGIN IMMEDIATE')
        else:
            self.db.execute('SELECT identity FROM users WHERE identity=? FOR UPDATE', (owner,)).fetchone()

    def sms_budget(self, owner, key):
        now = time.time()
        user_count, latest = self.db.execute('SELECT count(*),max(created) FROM telephone_sms_attempts WHERE owner=? AND created>?', (owner, now-3600)).fetchone()
        number_count, number_latest = self.db.execute('SELECT count(*),max(created) FROM telephone_sms_attempts WHERE number_key=? AND created>?', (key, now-3600)).fetchone()
        if user_count >= 3 or number_count >= 3 or (latest and latest > now-60) or (number_latest and number_latest > now-60):
            raise ValueError('Too many verification messages. Wait before sending another code (at most three per hour).')

    async def start_verification(self, owner, number):
        from voice.telephone_settings import E164
        if not isinstance(number, str) or not E164.fullmatch(number):
            raise ValueError('Use an E.164 phone number, such as +15555550123.')
        key, now = digest(self.folder, number), time.time()
        self.sms_budget(owner, key)
        code = f'{secrets.randbelow(1000000):06d}'
        verifier = await asyncio.to_thread(hash_password, code)
        with self.db:
            self.lock_owner(owner)
            self.db.execute('DELETE FROM telephone_verifications WHERE expires<=?', (now,))
            self.db.execute('DELETE FROM telephone_sms_attempts WHERE created<=?', (now-3600,))
            self.sms_budget(owner, key)
            registered = self.db.execute('SELECT owner FROM telephone_numbers WHERE number_key=?', (key,)).fetchone()
            pending = self.db.execute('SELECT owner FROM telephone_verifications WHERE number_key=?', (key,)).fetchone()
            if registered or (pending and pending[0] != owner):
                raise ValueError('This number cannot be registered. Check the number or ask an administrator.')
            occupied = {row[0] for row in self.db.execute('SELECT number_key FROM telephone_numbers WHERE owner=?', (owner,))}
            occupied.update(row[0] for row in self.db.execute('SELECT number_key FROM telephone_verifications WHERE owner=? AND expires>?', (owner, now)))
            if key not in occupied and len(occupied) >= 3:
                raise ValueError('You may register up to three numbers. Remove one before adding another.')
            updated = self.db.execute('INSERT INTO telephone_verifications VALUES (?,?,?,?,?,?,0,?) ON CONFLICT(number_key) DO UPDATE SET number=excluded.number,owner=excluded.owner,challenge=excluded.challenge,verifier=excluded.verifier,expires=excluded.expires,attempts=0,created=excluded.created WHERE telephone_verifications.owner=excluded.owner',
                (key, number, owner, secrets.token_hex(16), verifier, now+600, now)).rowcount
            if not updated:
                raise ValueError('This number cannot be registered. Check the number or ask an administrator.')
            self.db.execute('INSERT INTO telephone_sms_attempts VALUES (?,?,?)', (key, owner, now))
        return code

    async def verify(self, owner, number, code):
        from voice.telephone_settings import E164
        if not isinstance(number, str) or not E164.fullmatch(number):
            raise ValueError('Use a valid phone number to verify.')
        key, now = digest(self.folder, number), time.time()
        with self.db:
            row = self.db.execute('SELECT challenge,verifier FROM telephone_verifications WHERE owner=? AND number_key=? AND expires>? AND attempts<5', (owner, key, now)).fetchone()
            if not row:
                raise ValueError('The code is invalid or expired. Request a new code.')
            if not self.db.execute('UPDATE telephone_verifications SET attempts=attempts+1 WHERE owner=? AND number_key=? AND challenge=? AND expires>? AND attempts<5', (owner, key, row[0], now)).rowcount:
                raise ValueError('The code is invalid or expired. Request a new code.')
        correct = isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit() and await asyncio.to_thread(check_password, row[1], code)
        if not correct:
            raise ValueError('The code is invalid or expired. Request a new code after five incorrect attempts.')
        with self.db:
            self.lock_owner(owner)
            current = self.db.execute('SELECT challenge FROM telephone_verifications WHERE owner=? AND number_key=? AND expires>?', (owner, key, time.time())).fetchone()
            if not current or current[0] != row[0]:
                raise ValueError('The code is invalid or expired. Request a new code.')
            if len(self.own(owner)) >= 3:
                raise ValueError('You may register up to three numbers.')
            self.db.execute('INSERT INTO telephone_numbers VALUES (?,?,?,?)', (key, number, owner, time.time()))
            self.db.execute('DELETE FROM telephone_verifications WHERE owner=? AND number_key=?', (owner, key))

    def remove_number(self, owner, key):
        import re
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ValueError('Choose one of your registered numbers.')
        with self.db:
            for (key_hash,) in self.db.execute('SELECT key_hash FROM telephone_sessions WHERE number_key=?', (key,)).fetchall():
                self.db.execute('DELETE FROM keys WHERE hash=? AND user=(SELECT name FROM users WHERE identity=?)', (key_hash, owner))
            self.db.execute('DELETE FROM telephone_numbers WHERE owner=? AND number_key=?', (owner, key))
            self.db.execute('DELETE FROM telephone_verifications WHERE owner=? AND number_key=?', (owner, key))

    def mint_session(self, account, app, number=None, registered=False):
        key = self.users.new_key(account.name, 'telephone', hours=.5, via=app.name)
        with self.db:
            self.db.execute('INSERT INTO telephone_sessions VALUES (?,?,?)',
                (hash_key(key), digest(self.folder, number) if registered else None, 'registered' if registered else 'allowlist'))
        return key

    def scope_session(self, user, key):
        row = self.db.execute('SELECT number_key,kind FROM telephone_sessions WHERE key_hash=?', (hash_key(key),)).fetchone()
        if not row:
            return user
        person = self.users.by_identity(user.identity) if user else None
        app = self.users.by_name('voice-gateway')
        if not person or not app or user.via != app.name or not app.can('app-sign-in'):
            return None
        if row[1] == 'registered':
            own = self.db.execute('SELECT owner FROM telephone_numbers WHERE number_key=?', (row[0],)).fetchone()
            if not (person.can('front-phone') or person.can('superuser')) or not app.can('front-phone') or not own or own[0] != person.identity:
                return None
            person = replace(person, group=replace(person.group, can=person.group.can | {'front-voice', 'front-phone'}))
        group = limited(person.group, app.group)
        if 'front-voice' not in group.can:
            return None
        return replace(user, group=group)
