"""Only the documented Twilio endpoints are used; API errors never expose credentials."""
import asyncio
import base64
from urllib.parse import parse_qs
import httpx
import pytest
from voice.telephone_keys import TelephoneKeys
from voice.twilio_api import TwilioAPI, TwilioError

SID = 'AC' + 'a' * 32
PN = 'PN' + 'b' * 32


def run(tmp_path, handler, operation, api=False):
    keys = TelephoneKeys(tmp_path)
    keys.save('auth-token', 'SK' + 'c' * 32 if api else '', 'api-secret' if api else '')
    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await operation(TwilioAPI(client, SID, keys))
    return asyncio.run(call())


@pytest.mark.parametrize('api', [False, True])
def test_accounts_and_credential_preference(tmp_path, api):
    def reply(request):
        expected = ('SK' + 'c' * 32 + ':api-secret') if api else SID + ':auth-token'
        assert request.headers['authorization'] == 'Basic ' + base64.b64encode(expected.encode()).decode()
        assert request.url == 'https://api.twilio.com/2010-04-01/Accounts/' + SID + '.json'
        assert request.extensions['timeout']['connect'] == 3
        return httpx.Response(200, json={'friendly_name': 'Office', 'status': 'active', 'auth_token': 'never-returned'})
    assert run(tmp_path, reply, lambda api: api.account_info(), api) == {'name': 'Office', 'status': 'active'}


@pytest.mark.parametrize('status', [401, 403, 404, 429, 500])
def test_plain_errors_without_response_secrets(tmp_path, status):
    with pytest.raises(TwilioError, match=str(status)) as caught:
        run(tmp_path, lambda request: httpx.Response(status, json={'message': 'auth-token api-secret private'}), lambda api: api.account_info())
    assert 'auth-token' not in str(caught.value) and 'api-secret' not in str(caught.value)


def test_timeout_and_invalid_response(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout('auth-token')
    with pytest.raises(TwilioError, match='timed out'):
        run(tmp_path, timeout, lambda api: api.account_info())
    with pytest.raises(TwilioError, match='unreadable'):
        run(tmp_path, lambda request: httpx.Response(200, text='not JSON'), lambda api: api.account_info())


def test_number_pagination_and_exact_connect_fields(tmp_path):
    requests = []
    def reply(request):
        requests.append(request)
        if request.method == 'POST':
            assert parse_qs(request.content.decode()) == {'VoiceUrl': ['https://voice.example.com/voice/inbound'], 'VoiceMethod': ['POST']}
            return httpx.Response(200, json={'voice_url': 'https://voice.example.com/voice/inbound', 'voice_method': 'POST'})
        return httpx.Response(200, json={'incoming_phone_numbers': [{'sid': PN, 'phone_number': '+15555550123', 'capabilities': {'sms': True}}],
            'next_page_uri': None if len(requests) > 1 else '/2010-04-01/Accounts/' + SID + '/IncomingPhoneNumbers.json?Page=1'})
    assert len(run(tmp_path, reply, lambda api: api.numbers())) == 2
    assert run(tmp_path, reply, lambda api: api.connect_number(PN, 'https://voice.example.com/voice/inbound')) == {'connected': True}
    assert str(requests[-1].url).endswith('/IncomingPhoneNumbers/' + PN + '.json')


def test_hostile_pagination_never_sends_credentials_elsewhere(tmp_path):
    requests = []
    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={'next_page_uri': 'https://attacker.example/steal'})
    with pytest.raises(TwilioError, match='next-page'):
        run(tmp_path, reply, lambda api: api.numbers())
    assert len(requests) == 1
