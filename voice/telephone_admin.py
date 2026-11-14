"""Presentation and validation of the non-secret telephone editor."""

from copy import deepcopy
from voice.telephone_settings import validate, urls, E164

EMPTY = {'enabled': False, 'account_sid': '', 'public_url': '', 'numbers': []}


def effective(changes):
    override = changes.of('voice-gateway')
    known = next((one for one in changes.everything() if one['program'] == 'voice-gateway'), {})
    current = {one['name']: one['value'] for one in known.get('settings', [])}
    return deepcopy(override.get('telephone', current.get('telephone', EMPTY)))


def edited(phone, form):
    """Merge one form section, preserving every unsubmitted number and field."""
    phone = deepcopy(phone)
    action = form['action']
    if action == 'save_setup':
        for name in ('account_sid', 'public_url'):
            if name in form:
                phone[name] = form[name].strip()
        phone['enabled'] = form.get('enabled') is True
    elif action in ('add_number', 'save_number', 'remove_number', 'save_access', 'add_caller', 'remove_caller'):
        number = form.get('number', '')
        if not isinstance(number, str) or not E164.fullmatch(number):
            raise ValueError('Use an E.164 number such as +15555550123.')
        selected = next((one for one in phone['numbers'] if one['number'] == number), None)
        if action == 'add_number':
            if selected:
                raise ValueError('This number is already on the Telephone page.')
            selected = dict(number=number, label=form.get('label') or number, enabled=False, mode='direct', profile='default',
                voice='', greeting='', account='voice-phone', callers={'mode': 'allowlist', 'entries': []}, max_calls_per_hour=60)
            # Labels allow a friendly name, but a raw E.164 string is not an account label.
            if not form.get('label'):
                selected['label'] = 'Phone ' + number[-4:]
            phone['numbers'].append(selected)
        elif selected is None:
            raise ValueError('Choose an existing telephone number.')
        if action == 'remove_number':
            phone['numbers'].remove(selected)
        elif action == 'save_access':
            selected['callers'] = {**selected.get('callers', {'entries': []}), 'mode': form.get('access_mode', 'allowlist')}
            for name in ('accept_user_numbers', 'require_attested_caller_id'):
                selected[name] = form.get(name) is True
            selected['refusal_greeting'] = form.get('refusal_greeting', '')
        elif action == 'add_caller':
            access = selected.setdefault('callers', {'mode': 'allowlist', 'entries': []})
            access.setdefault('entries', []).append({'number': form.get('caller_number', ''), 'note': form.get('caller_note', ''),
                'account': form.get('caller_account') or selected['account']})
        elif action == 'remove_caller':
            access = selected.setdefault('callers', {'mode': 'allowlist', 'entries': []})
            access['entries'] = [entry for entry in access.get('entries', []) if entry['number'] != form.get('caller_number')]
        elif action == 'save_number':
            for name in ('label', 'account', 'mode', 'profile', 'voice', 'greeting'):
                if name in form:
                    selected[name] = form[name]
            selected['enabled'] = form.get('enabled') is True
            if 'max_calls_per_hour' in form:
                try:
                    selected['max_calls_per_hour'] = int(form['max_calls_per_hour'])
                except (ValueError, TypeError):
                    raise ValueError('Use a whole number for the hourly call limit.') from None
    return phone


def verdict(phone, credentials, active=0):
    if not phone.get('enabled'):
        return {'state': 'Off', 'reason': 'Enable telephone after completing setup.'}
    for ready, reason in ((bool(phone.get('account_sid')), 'Save your Twilio Account SID.'),
                          (credentials.get('token', {}).get('saved'), 'Save your Twilio auth token.'),
                          (bool(phone.get('public_url')), 'Set up a public HTTPS address.'),
                          (any(one.get('enabled') for one in phone.get('numbers', [])), 'Add and enable a telephone number.')):
        if not ready:
            return {'state': 'Needs setup', 'reason': reason}
    return {'state': 'Receiving calls' if active else 'Ready', 'reason': 'Telephone setup is complete. Check Voice status for compute readiness.'}
