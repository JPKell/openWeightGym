"""Caller decisions, privacy-safe correlation and bounded webhook rate accounting."""

from collections import defaultdict, deque
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import tempfile
import time


def correlation_key(folder):
    """Create once with an atomic link, including simultaneous front/gateway starts."""
    path = Path(folder) / 'voice-phone-correlation.key'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, pending = tempfile.mkstemp(prefix='.phone-correlation-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w') as output:
                output.write(secrets.token_hex(32)); output.flush(); os.fsync(output.fileno())
            try:
                os.link(pending, path)
            except FileExistsError:
                pass
        finally:
            Path(pending).unlink(missing_ok=True)
    return path.read_text().strip()


def digest(folder, value):
    return hmac.new(correlation_key(folder).encode(), value.encode(), hashlib.sha256).hexdigest()


def masked(number):
    return number[:2] + ' ••• ••• ' + number[-4:] if isinstance(number, str) and number.startswith('+') and len(number) >= 9 else 'Hidden'


def decide(number, caller, attestation='', pin_ok=False, pin_saved=False, registered=None):
    """Unattested caller ID never selects a personal account through a shared number PIN."""
    access = number.get('callers', {'mode': 'allowlist'})
    mode, fallback = access.get('mode', 'allowlist'), number.get('account')
    match = next((entry for entry in access.get('entries', []) if entry['number'] == caller), None)
    if registered and number.get('accept_user_numbers', mode != 'anyone'):
        match = {'account': registered, 'registered': True}
    account = match.get('account') or fallback if match else fallback
    needs_attestation = bool(match) and number.get('require_attested_caller_id', account != fallback or match.get('registered', False))
    if match and (not needs_attestation or attestation == 'TN-Validation-Passed-A'):
        return {'accepted': True, 'account': account, 'registered': bool(match.get('registered'))}
    if match and needs_attestation:
        if pin_saved and pin_ok:
            return {'accepted': True, 'account': fallback}
        return {'accepted': False, 'reason': 'bad_pin' if pin_saved and pin_ok is False else 'unattested',
                'pin_needed': pin_saved}
    if mode == 'anyone':
        return {'accepted': True, 'account': fallback}
    if mode == 'pin':
        return {'accepted': bool(pin_saved and pin_ok), 'account': fallback,
                'reason': 'bad_pin', 'pin_needed': pin_saved}
    return {'accepted': False, 'reason': 'not_listed'}


class RateLimits:
    def __init__(self):
        self.rows = defaultdict(deque)

    def count(self, key):
        rows, now = self.rows[key], time.monotonic()
        while rows and rows[0] <= now - 3600:
            rows.popleft()
        return len(rows)

    def allowed(self, number, caller, limit, continuation=False):
        if len(self.rows) > 20000:
            for key in list(self.rows):
                if not self.count(key):
                    del self.rows[key]
            if len(self.rows) > 20000:
                return False
        bound = limit + (1 if continuation else 0)
        return self.count(('number', number)) < bound and self.count(('caller', number, caller)) < bound

    def charge(self, number, caller):
        now = time.monotonic()
        self.rows[('number', number)].append(now)
        self.rows[('caller', number, caller)].append(now)
