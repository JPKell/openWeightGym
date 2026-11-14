"""Owner-scoped SMS verification, using connected Twilio numbers and the accounts store."""

import time
import httpx
from starlette.responses import JSONResponse
from turnstile.accounts import read_json, error
from voice.telephone_admin import effective
from voice.telephone_settings import urls
from voice.phone_access import masked
from voice.twilio_api import TwilioAPI, TwilioError


def phone_allowed(user):
    return user and (user.can('front-phone') or user.can('superuser'))


class PhoneRegistration:
    def __init__(self, admin):
        self.admin = admin
        self.cache = None

    async def sender(self):
        phone, keys = effective(self.admin.changes), self.admin.keys
        if not phone.get('enabled') or not phone.get('public_url') or not keys.status()['token']['saved']:
            raise ValueError('Telephone SMS verification is unavailable. An administrator must enable and connect a Twilio number first.')
        tag = (phone['account_sid'], str(phone['numbers']), phone['public_url'], str(keys.status()))
        if not self.cache or self.cache[0] != tag or self.cache[1] < time.monotonic()-60:
            async with httpx.AsyncClient() as client:
                numbers = await TwilioAPI(client, phone['account_sid'], keys).numbers()
            self.cache = tag, time.monotonic(), numbers
        for chosen in phone['numbers']:
            for number in self.cache[2]:
                if (number.get('phone_number') == chosen['number'] and number.get('capabilities', {}).get('sms')
                        and number.get('voice_url') == urls(phone['public_url'])[0] and number.get('voice_method') == 'POST'):
                    return {**chosen, 'sid': number['sid']}
        raise ValueError('No connected Twilio number supports SMS. An administrator must connect an SMS-capable number on Telephone.')

    async def handle(self, request, user):
        if user is None:
            return error(401, 'Sign in to manage your phone numbers.')
        if not phone_allowed(user):
            return error(403, 'Registering phone numbers requires front-phone.')
        store = self.admin.store
        if request.method == 'GET':
            result = {'numbers': store.own(user.identity), 'pending': store.pending(user.identity), 'available': False}
            try:
                sender = await self.sender()
                result.update(available=True, sender={'label': sender['label'], 'number': masked(sender['number'])})
            except (ValueError, OSError) as problem:
                result['reason'] = str(problem) if isinstance(problem, ValueError) else 'SMS verification credentials are unavailable.'
            return JSONResponse(result, headers={'cache-control': 'no-store'})
        if request.method != 'POST':
            return error(405, 'Unsupported phone-number operation.')
        form = await read_json(request, {'action', 'number', 'code', 'id'})
        if not isinstance(form, dict):
            return form
        try:
            action = form.get('action')
            if action == 'send':
                sender = await self.sender()
                code = await store.start_verification(user.identity, form.get('number'))
                phone = effective(self.admin.changes)
                async with httpx.AsyncClient() as client:
                    await TwilioAPI(client, phone['account_sid'], self.admin.keys).send_code(sender['number'], form['number'], code)
            elif action == 'verify':
                await store.verify(user.identity, form.get('number', ''), form.get('code', ''))
            elif action == 'remove':
                store.remove_number(user.identity, form.get('id', ''))
            else:
                raise ValueError('Choose a phone-number operation.')
            self.admin.log.add('telephone', 'number_' + action, user.name)
            return JSONResponse({'saved': True}, headers={'cache-control': 'no-store'})
        except (ValueError, OSError) as problem:
            return error(400, str(problem) if isinstance(problem, ValueError) else 'Phone verification could not be completed. Try again later.')
