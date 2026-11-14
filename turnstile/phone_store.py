"""Private telephone proofs in the existing front accounts store."""

import asyncio
from db import migrate
from db.schemas import TELEPHONE
from loadcoach.users import hash_password, check_password
from voice.phone_access import digest


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
