"""Private telephone credential and phone-identity management on front."""

from pathlib import Path
import re

from starlette.responses import JSONResponse
from turnstile.accounts import error, read_json
from loadcoach.users import hash_key
from voice.telephone_keys import TelephoneKeys, atomic_secret
from voice.telephone_admin import effective, edited
from voice.telephone_settings import validate, urls
from voice.twilio_api import TwilioAPI, TwilioError
from weightroom.bootstrap import ensure_service_identity


class TelephoneAdmin:
    def __init__(self, users, log, changes, folder):
        self.users, self.log, self.changes = users, log, changes
        self.keys = TelephoneKeys(folder)
        self.discovery = {}

    async def handle(self, request, user):
        if request.url.path != '/loadcoach/telephone':
            return None
        if user is None:
            return error(401, 'Missing or wrong API key.')
        if not user.can('manage-settings'):
            return error(403, 'Telephone requires manage-settings.')
        if request.method == 'GET':
            accounts = [name for (name,) in self.users.db.execute("SELECT name FROM users WHERE grp='app' AND enabled<>0")
                        if name == 'voice-phone' or name.startswith('voice-phone-')]
            phone = effective(self.changes)
            return JSONResponse({'credentials': self.keys.status(), 'telephone': phone, 'accounts': accounts,
                'discovered': self.discovery.get(phone.get('account_sid'), []),
                'settings': next((one['settings'] for one in self.changes.everything() if one['program'] == 'voice-gateway'), [])},
                headers={'cache-control': 'no-store'})
        if request.method != 'POST':
            return error(405, 'Unsupported telephone operation.')
        form = await read_json(request, {'action', 'token', 'api_sid', 'api_secret', 'kind', 'label', 'account',
            'account_sid', 'public_url', 'public_port', 'public_host', 'enabled', 'number', 'mode', 'profile', 'voice',
            'greeting', 'max_calls_per_hour', 'sid'})
        if not isinstance(form, dict):
            return form
        action = form.get('action')
        try:
            if action in ('save_setup', 'add_number', 'save_number', 'remove_number'):
                phone = edited(effective(self.changes), form)
                validate(phone)
                for number in phone['numbers']:
                    account = self.users.by_name(number['account'])
                    if number.get('enabled') and (account is None or account.group_name != 'app' or not account.can('front-voice')):
                        raise ValueError('Create a phone account with front-voice before enabling this number.')
                port = form.get('public_port')
                if port is not None:
                    try:
                        port = int(port)
                    except (ValueError, TypeError):
                        raise ValueError('Use a valid public port.') from None
                    if not 1 <= port <= 65535:
                        raise ValueError('Use a public port between 1 and 65535.')
                self.changes.put('voice-gateway', 'telephone', phone, user.name)
                if port is not None:
                    self.changes.put('voice-gateway', 'public_port', port, user.name)
                self.log.add('telephone', action, user.name)
                return JSONResponse({'saved': True}, headers={'cache-control': 'no-store'})
            elif action in ('check_credentials', 'find_numbers', 'connect_number'):
                import httpx
                phone = effective(self.changes)
                async with httpx.AsyncClient() as client:
                    api = TwilioAPI(client, phone['account_sid'], self.keys)
                    if action == 'check_credentials':
                        result = await api.account_info()
                    elif action == 'find_numbers':
                        self.discovery[phone['account_sid']] = await api.numbers()
                        result = {'numbers': self.discovery[phone['account_sid']]}
                    else:
                        if not phone.get('public_url'):
                            raise ValueError('Save the public address first.')
                        result = await api.connect_number(form.get('sid'), urls(phone['public_url'])[0])
                        for number in self.discovery.get(phone['account_sid'], []):
                            if number['sid'] == form.get('sid') and result['connected']:
                                number.update(voice_url=urls(phone['public_url'])[0], voice_method='POST')
                        self.log.add('telephone', action, user.name)
                return JSONResponse(result, headers={'cache-control': 'no-store'})
            elif action == 'save_credentials':
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
        except TwilioError as problem:
            return error(400, str(problem))
        except (ValueError, TypeError) as problem:
            # Never echo values submitted to a credential endpoint.
            return error(400, str(problem) if action in ('save_setup', 'add_number', 'save_number', 'remove_number') else
                         'Check the telephone account label or credential fields and try again.')
        except OSError:
            return error(503, 'Telephone credentials could not be saved. Check the keys folder permissions.')
