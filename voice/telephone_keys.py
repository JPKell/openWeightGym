"""Owner-only, atomic, write-only telephone credentials, read on every use."""

import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone


def atomic_secret(path, value):
    """Install a complete 0600 secret without exposing a partially written credential."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(prefix='.telephone-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


class TelephoneKeys:
    """Keep credentials out of settings, templates, logs and API responses."""

    def __init__(self, folder):
        self.folder = Path(folder)

    def path(self, kind):
        if kind not in ('token', 'api'):
            raise ValueError('Choose auth token or API key.')
        return self.folder / ('voice-twilio.key' if kind == 'token' else 'voice-twilio-api.key')

    def read(self, kind):
        try:
            return self.path(kind).read_text().strip()
        except FileNotFoundError:
            return ''

    def status(self):
        result = {}
        for kind in ('token', 'api'):
            try:
                result[kind] = {'saved': True, 'changed': datetime.fromtimestamp(self.path(kind).stat().st_mtime, timezone.utc).isoformat()}
            except FileNotFoundError:
                result[kind] = {'saved': False, 'changed': None}
        return result

    def save(self, token='', api_sid='', api_secret=''):
        # Validate all supplied fields before changing either credential.
        for value in (token, api_secret):
            if not isinstance(value, str) or (value and (len(value) > 8192 or any(not 33 <= ord(char) <= 126 for char in value))):
                raise ValueError('Enter a credential without spaces or control characters (at most 8192 characters).')
        if not isinstance(api_sid, str) or (api_sid and not re.fullmatch(r'SK[0-9a-fA-F]{32}', api_sid)):
            raise ValueError('API key SID must be SK followed by 32 hexadecimal characters.')
        if bool(api_sid) != bool(api_secret):
            raise ValueError('Enter both the API key SID and secret, or leave both empty to keep the saved key.')
        if token:
            atomic_secret(self.path('token'), token)
        if api_secret:
            atomic_secret(self.path('api'), json.dumps({'sid': api_sid, 'secret': api_secret}))

    def remove(self, kind):
        self.path(kind).unlink(missing_ok=True)
