"""The bounded Twilio REST operations used by telephone setup and verification."""

import json
import re
from urllib.parse import urlsplit

import httpx


class TwilioError(ValueError):
    """A credential-free message safe to show in the telephone panel."""


class TwilioAPI:
    def __init__(self, client, account_sid, keys):
        if not isinstance(account_sid, str) or not re.fullmatch(r'AC[0-9a-fA-F]{32}', account_sid):
            raise TwilioError('Save a valid Twilio Account SID first.')
        self.client, self.account, self.keys = client, account_sid, keys
        self.root = '/2010-04-01/Accounts/' + account_sid

    def auth(self):
        token = self.keys.read('token')
        if not token:
            raise TwilioError('Save the Twilio auth token first.')
        saved = self.keys.read('api')
        try:
            api = json.loads(saved) if saved else {}
            return (api['sid'], api['secret']) if api else (self.account, token)
        except (ValueError, KeyError, TypeError):
            raise TwilioError('Save the optional API key again.') from None

    async def request(self, method, path, **kwargs):
        auth = self.auth()
        try:
            response = await self.client.request(method, 'https://api.twilio.com' + path,
                auth=auth, timeout=httpx.Timeout(10, connect=3), follow_redirects=False, **kwargs)
        except httpx.TimeoutException:
            raise TwilioError('Twilio timed out. Try again shortly.') from None
        except httpx.HTTPError:
            raise TwilioError('Twilio could not be reached. Check the control node’s Internet connection.') from None
        if not response.is_success:
            messages = {401: 'Twilio rejected the credentials (HTTP 401).', 403: 'Twilio refused this operation (HTTP 403).',
                404: 'Twilio could not find that account or number (HTTP 404).', 429: 'Twilio’s rate limit was reached. Wait before trying again (HTTP 429).'}
            message = messages.get(response.status_code, f'Twilio returned HTTP {response.status_code}. Check the account in Twilio’s console.')
            try:
                code = response.json().get('code')
                if type(code) is int and 10000 <= code <= 99999:
                    message += ' Twilio error code: ' + str(code) + '.'
            except (ValueError, AttributeError):
                pass
            raise TwilioError(message)
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError()
            return body
        except ValueError:
            raise TwilioError('Twilio returned an unreadable response.') from None

    async def account_info(self):
        result = await self.request('GET', self.root + '.json')
        return {'name': result.get('friendly_name', ''), 'status': result.get('status', 'unknown')}

    async def numbers(self):
        path, result = self.root + '/IncomingPhoneNumbers.json?PageSize=100', []
        for _ in range(10):
            page = await self.request('GET', path)
            for one in page.get('incoming_phone_numbers', []):
                result.append({name: one.get(name) for name in ('sid', 'phone_number', 'friendly_name', 'voice_url', 'voice_method', 'capabilities')})
            path = page.get('next_page_uri')
            if not path:
                return result
            parsed = urlsplit(path)
            if parsed.scheme or parsed.netloc or parsed.path != self.root + '/IncomingPhoneNumbers.json':
                raise TwilioError('Twilio returned an invalid next-page address.')
        raise TwilioError('This account has too many numbers to list at once. Use manual setup.')

    async def connect_number(self, sid, webhook):
        if not isinstance(sid, str) or not re.fullmatch(r'PN[0-9a-fA-F]{32}', sid):
            raise TwilioError('Choose a number returned by Find my numbers.')
        result = await self.request('POST', self.root + '/IncomingPhoneNumbers/' + sid + '.json',
                                    data={'VoiceUrl': webhook, 'VoiceMethod': 'POST'})
        return {'connected': result.get('voice_url') == webhook and result.get('voice_method') == 'POST'}

    async def send_code(self, sender, number, code):
        from voice.telephone_settings import E164
        if not all(isinstance(value, str) and E164.fullmatch(value) for value in (sender, number)) or not re.fullmatch(r'[0-9]{6}', code):
            raise TwilioError('Choose valid verification numbers and code.')
        await self.request('POST', self.root + '/Messages.json', data={'From': sender, 'To': number,
            'Body': 'Your openWeightGym telephone verification code is ' + code + '. It expires in 10 minutes.'})
