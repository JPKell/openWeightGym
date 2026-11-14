"""Private telephone credential and phone-identity management on the control node."""

from pathlib import Path
import re

from starlette.responses import JSONResponse
from turnstile.accounts import error, read_json
from loadcoach.users import hash_key
from voice.telephone_keys import TelephoneKeys, atomic_secret
from voice.telephone_admin import effective, edited
from voice.telephone_settings import validate, urls
from voice.twilio_api import TwilioAPI, TwilioError
from voice.phone_access import decide
from turnstile.phone_store import PhoneStore
from turnstile.phone_registration import PhoneRegistration, phone_allowed
from weightroom.bootstrap import ensure_service_identity


class TelephoneAdmin:
    def __init__(self, users, log, changes, folder):
        self.users, self.log, self.changes = users, log, changes
        self.keys = TelephoneKeys(folder)
        self.discovery = {}
        self.store = PhoneStore(users, self.keys.folder)
        self.registration = PhoneRegistration(self)

    async def admit(self, request, user):
        """Only the signature-verifying gateway can exchange a caller match for a limited session."""
        if user is None or user.name != 'voice-gateway' or not user.can('app-sign-in'):
            return error(403, 'Telephone admission requires the voice gateway identity.')
        form = await read_json(request, {'number', 'caller', 'attestation', 'digits'})
        if not isinstance(form, dict):
            return form
        if any(not isinstance(form.get(name, ''), str) or len(form.get(name, '')) > 128 for name in ('number', 'caller', 'attestation', 'digits')):
            return error(400, 'Use valid caller admission fields.')
        phone = effective(self.changes)
        number = next((one for one in phone['numbers'] if one['number'] == form.get('number') and one.get('enabled')), None)
        if not phone.get('enabled') or not number:
            return JSONResponse({'accepted': False, 'reason': 'unknown_number'})
        pin_saved = self.store.pin_saved(number['number'])
        pin_ok = await self.store.check_pin(number['number'], form.get('digits', ''))
        registered = self.store.registered(form.get('caller', ''))
        if registered and not phone_allowed(registered):
            registered = None
        decision = decide(number, form.get('caller', ''), form.get('attestation', ''), pin_ok, pin_saved,
                          registered=registered.name if registered else None)
        if decision['accepted']:
            account = self.users.by_name(decision['account'])
            if account is None or not (account.can('front-voice') or decision.get('registered') and phone_allowed(account)):
                decision = {'accepted': False, 'reason': 'account_unavailable'}
            elif decision['account'] != number['account']:
                from loadcoach.groups import limited
                if not decision.get('registered') and not limited(account.group, user.group).can & {'front-voice'}:
                    decision = {'accepted': False, 'reason': 'account_unavailable'}
                else:
                    if decision.get('registered') and not user.can('front-phone'):
                        decision = {'accepted': False, 'reason': 'account_unavailable'}
                    else:
                        decision['session_key'] = self.store.mint_session(account, user, form.get('caller'), bool(decision.get('registered')))
            else:
                try:
                    owner = self.users.by_key((self.keys.folder / (number['account'] + '.key')).read_text().strip())
                except OSError:
                    owner = None
                if owner is None or owner.name != number['account']:
                    decision = {'accepted': False, 'reason': 'account_unavailable'}
        return JSONResponse(decision, headers={'cache-control': 'no-store'})

    async def handle(self, request, user):
        if request.url.path == '/loadcoach/phone-numbers':
            return await self.registration.handle(request, user)
        if request.url.path == '/loadcoach/telephone/admit' and request.method == 'POST':
            return await self.admit(request, user)
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
            for number in phone['numbers']:
                number['pin_saved'] = self.store.pin_saved(number['number'])
            registered = []
            for owner, name in self.users.db.execute('SELECT identity,name FROM users WHERE enabled<>0'):
                registered.extend({**one, 'owner': name, 'can_call': bool(phone_allowed(self.users.by_identity(owner)))}
                                  for one in self.store.own(owner, mask=True))
            number_facts = {}
            for number in phone['numbers']:
                try:
                    account = self.users.by_key((self.keys.folder / (number['account'] + '.key')).read_text().strip())
                except OSError:
                    account = None
                access = number.get('callers', {'mode': 'allowlist'})
                number_facts[number['number']] = {'account_ready': bool(account and account.name == number['account'] and account.can('front-voice')),
                    'models': account.group.models if account else [], 'pin_saved': number['pin_saved'],
                    'has_callers': bool(access.get('entries') or number.get('accept_user_numbers', access.get('mode') != 'anyone')
                                        and any(one['can_call'] for one in registered))}
            return JSONResponse({'credentials': self.keys.status(), 'telephone': phone, 'accounts': accounts,
                'discovered': self.discovery.get(phone.get('account_sid'), []),
                'caller_accounts': [name for (name,) in self.users.db.execute('SELECT name FROM users WHERE enabled<>0 ORDER BY name')],
                'registered_numbers': registered,
                'number_facts': number_facts,
                'settings': next((one['settings'] for one in self.changes.everything() if one['program'] == 'voice-gateway'), [])},
                headers={'cache-control': 'no-store'})
        if request.method != 'POST':
            return error(405, 'Unsupported telephone operation.')
        form = await read_json(request, {'action', 'token', 'api_sid', 'api_secret', 'kind', 'label', 'account',
            'account_sid', 'public_url', 'public_port', 'public_host', 'enabled', 'number', 'mode', 'profile', 'voice',
            'greeting', 'max_calls_per_hour', 'sid', 'access_mode', 'caller_number', 'caller_note', 'caller_account',
            'accept_user_numbers', 'require_attested_caller_id', 'pin', 'refusal_greeting', 'owner', 'id'})
        if not isinstance(form, dict):
            return form
        action = form.get('action')
        try:
            if action in ('save_setup', 'add_number', 'save_number', 'remove_number', 'save_access', 'add_caller', 'remove_caller'):
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
                if action == 'remove_number':
                    self.store.remove_pin(form['number'])
                if port is not None:
                    self.changes.put('voice-gateway', 'public_port', port, user.name)
                self.log.add('telephone', action, user.name)
                return JSONResponse({'saved': True}, headers={'cache-control': 'no-store'})
            elif action == 'revoke_user_number':
                owner = self.users.by_name(form.get('owner', ''))
                if not owner:
                    raise ValueError('Choose an existing account.')
                self.store.remove_number(owner.identity, form.get('id', ''))
                self.log.add('telephone', action, user.name, account=owner.name)
                return JSONResponse({'saved': True}, headers={'cache-control': 'no-store'})
            elif action in ('set_pin', 'remove_pin'):
                phone = effective(self.changes)
                if not any(one['number'] == form.get('number') for one in phone['numbers']):
                    raise ValueError('Choose an existing telephone number.')
                if action == 'set_pin':
                    await self.store.set_pin(form['number'], form.get('pin', ''))
                else:
                    self.store.remove_pin(form['number'])
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
            return error(400, str(problem) if action in ('save_setup', 'add_number', 'save_number', 'remove_number', 'save_access', 'add_caller', 'remove_caller') else
                         'Check the telephone account label or credential fields and try again.')
        except OSError:
            return error(503, 'Telephone credentials could not be saved. Check the keys folder permissions.')
