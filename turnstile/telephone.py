"""Private telephone credential and phone-identity management on front."""

from pathlib import Path
import re

from starlette.responses import JSONResponse
from turnstile.accounts import error, read_json
from loadcoach.users import hash_key
from voice.telephone_keys import TelephoneKeys, atomic_secret
from weightroom.bootstrap import ensure_service_identity


class TelephoneAdmin:
    def __init__(self, users, log, changes, folder):
        self.users, self.log, self.changes = users, log, changes
        self.keys = TelephoneKeys(folder)

    async def handle(self, request, user):
        if request.url.path != '/loadcoach/telephone':
            return None
        if user is None:
            return error(401, 'Missing or wrong API key.')
        if not user.can('manage-settings'):
            return error(403, 'Telephone requires manage-settings.')
        if request.method == 'GET':
            return JSONResponse({'credentials': self.keys.status()}, headers={'cache-control': 'no-store'})
        if request.method != 'POST':
            return error(405, 'Unsupported telephone operation.')
        form = await read_json(request, {'action', 'token', 'api_sid', 'api_secret', 'kind', 'label', 'account'})
        if not isinstance(form, dict):
            return form
        action = form.get('action')
        try:
            if action == 'save_credentials':
                self.keys.save(form.get('token', ''), form.get('api_sid', ''), form.get('api_secret', ''))
            elif action == 'remove_credential':
                kind = form.get('kind')
                if kind == 'token':
                    phone = self.changes.of('voice-gateway').get('telephone') or {'account_sid': '', 'public_url': '', 'numbers': []}
                    self.changes.put('voice-gateway', 'telephone', {**phone, 'enabled': False}, user.name)
                self.keys.remove(kind)
            elif action in ('create_account', 'rotate_account'):
                label = form.get('label', '')
                if not isinstance(label, str) or not re.fullmatch(r'[a-z0-9_-]{0,50}', label):
                    raise ValueError('Use a phone account label of up to 50 lowercase letters, digits, underscores or hyphens.')
                name = 'voice-phone' + ('-' + label if label else '')
                key_file = self.keys.folder / (name + '.key')
                if action == 'rotate_account':
                    account = self.users.by_name(name)
                    if account is None or account.group_name != 'app' or not key_file.is_file():
                        raise ValueError('Create this phone account before rotating its key.')
                    previous = key_file.read_text().strip()
                    owner = self.users.by_key(previous)
                    if owner is None or owner.name != name:
                        raise ValueError('Restore the active phone credential before rotating it.')
                    replacement = self.users.new_key(name, 'telephone')
                    try:
                        atomic_secret(key_file, replacement)
                    except OSError:
                        self.users.remove_key(name, hash_key(replacement))
                        raise
                    self.users.remove_key(name, hash_key(previous))
                else:
                    ensure_service_identity(self.users, self.log, name, 'app', key_file, ('front-voice',), user.name)
                self.log.add('telephone', action, user.name, account=name)
                return JSONResponse({'account': name, 'url': '/users/' + name}, headers={'cache-control': 'no-store'})
            else:
                return error(400, 'Choose a telephone operation.')
            self.log.add('telephone', action, user.name)
            return JSONResponse({'credentials': self.keys.status()}, headers={'cache-control': 'no-store'})
        except (ValueError, TypeError):
            # Never echo values submitted to a credential endpoint.
            return error(400, 'Check the telephone account label or credential fields and try again.')
        except OSError:
            return error(503, 'Telephone credentials could not be saved. Check the keys folder permissions.')
