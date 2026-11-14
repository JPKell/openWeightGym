"""Telephone configuration and signed multi-number admission, without compute."""
from copy import deepcopy

import httpx
import pytest
from starlette.testclient import TestClient

from loadcoach.settings import Changes, CHANGEABLE, laid_over
from voice.gateway import build_app
from voice.telephone import signature
from voice.telephone_settings import configuration, validate, urls


def phone():
    return dict(enabled=True, account_sid='AC' + 'a' * 32, public_url='https://phone.example.com',
                numbers=[dict(label='Office', number='+15555550123', enabled=True, account='voice-phone',
                              profile='default', mode='direct', voice='', greeting='Welcome.', max_calls_per_hour=60, callers={'mode': 'anyone'}),
                         dict(label='Support', number='+15555550124', enabled=True, account='voice-phone-support',
                              profile='support', mode='hermes', callers={'mode': 'anyone'})])


def test_site_settings_round_trip_and_legacy_precedence(tmp_path):
    store = Changes(tmp_path / 'settings.db')
    value = phone()
    store.put('voice-gateway', 'telephone', value, 'owner')
    assert store.of('voice-gateway')['telephone'] == value
    legacy = dict(twilio={'token_file': '/old/token', 'webhook_url': 'https://old.example/in?x=1',
                          'stream_url': 'wss://old.example/socket/', 'mode': 'direct'})
    assert configuration(legacy)['webhook_url'] == legacy['twilio']['webhook_url']
    assert configuration(legacy)['numbers'][0]['legacy']
    merged = laid_over(legacy, store.of('voice-gateway'), 'voice-gateway')
    assert configuration(merged)['webhook_url'] == 'https://phone.example.com/voice/inbound'
    assert not configuration(merged).get('legacy')
    assert CHANGEABLE['voice-gateway']['public_host'] == CHANGEABLE['voice-gateway']['public_port'] == 'restart'
    assert CHANGEABLE['voice-gateway']['telephone'] == 'now'
    assert urls('https://phone.example.com/prefix/') == ('https://phone.example.com/prefix/voice/inbound', 'wss://phone.example.com/prefix/voice/stream')


@pytest.mark.parametrize('field,value', [
    ('account_sid', 'ACbad'), ('account_sid', ''), ('enabled', 1), ('public_url', 'http://phone.example.com'),
    ('public_url', 'https://127.0.0.1'), ('public_url', 'https://10.1.1.1'), ('public_url', 'https://[::1]'),
    ('public_url', 'https://localhost'), ('public_url', 'https://host.local'), ('public_url', 'https://u:p@example.com'),
    ('public_url', 'https://example.com?x=1'), ('public_url', 'https://example.com#x'),
    ('public_url', 'https://example.com:bad'), ('public_url', None), ('numbers', {}), ('token', 'secret')])
def test_invalid_telephone_table(field, value):
    settings = phone(); settings[field] = value
    with pytest.raises(ValueError):
        validate(settings)


@pytest.mark.parametrize('field,value', [('label', ''), ('number', '5555'), ('enabled', 'yes'), ('mode', 'bad'),
    ('profile', '../bad'), ('account', '../owner'), ('greeting', 'x' * 301), ('voice', '../unknown'),
    ('max_calls_per_hour', 0), ('max_calls_per_hour', True), ('token_file', '/etc/passwd')])
def test_invalid_number(field, value):
    settings = phone(); settings['numbers'][0][field] = value
    with pytest.raises(ValueError):
        validate(settings)


@pytest.mark.parametrize('field', ['label', 'number'])
def test_number_uniqueness(field):
    settings = phone(); settings['numbers'][1][field] = settings['numbers'][0][field]
    with pytest.raises(ValueError):
        validate(settings)


def test_profile_modes_and_account_voice_validation():
    settings = phone(); settings['numbers'][0]['voice'] = 'narrator'
    profiles = {'default': {'modes': ['direct']}, 'support': {'modes': ['hermes']}}
    assert validate(settings, profiles, {'voice-phone': ['narrator']}) == settings
    with pytest.raises(ValueError):
        validate(settings, profiles, {'voice-phone-support': ['narrator']})
    settings['numbers'][0]['voice'] = ''
    settings['numbers'][1]['mode'] = 'direct'
    with pytest.raises(ValueError):
        validate(settings, profiles)
    settings['numbers'][1]['profile'] = 'missing'
    with pytest.raises(ValueError):
        validate(settings, profiles)


def test_signed_to_routing_disabled_and_dynamic_changes(tmp_path):
    key = tmp_path / 'gateway.key'; key.write_text('app-key')
    token = tmp_path / 'twilio.key'; token.write_text('twilio-secret')
    settings = dict(key_file=str(key), turnstile='http://turnstile', voice='http://compute',
                    telephone_token_file=str(token), telephone=phone(), public_port=8081)
    def never(request):
        if request.url.path == '/loadcoach/telephone/admit':
            import json
            account = 'voice-phone' if json.loads(request.content)['number'] == '+15555550123' else 'voice-phone-support'
            return httpx.Response(200, json={'accepted': True, 'account': account})
        raise AssertionError('Admission must not wake or contact compute')
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(never)))
    with TestClient(app, base_url='http://public:8081') as client:
        def inbound(number, call):
            params = dict(AccountSid=settings['telephone']['account_sid'], CallSid=call, To=number, From='+15555550999')
            signed = signature('twilio-secret', 'https://phone.example.com/voice/inbound', {k: [v] for k, v in params.items()})
            return client.post('/voice/inbound', data=params, headers={'x-twilio-signature': signed})
        assert inbound('+15555550123', 'CAoffice').status_code == 200
        assert inbound('+15555550124', 'CAsupport').status_code == 200
        assert app.state.phone_calls['CAoffice']['number']['account'] == 'voice-phone'
        assert app.state.phone_calls['CAsupport']['number']['profile'] == 'support'
        settings['telephone']['numbers'][0]['account'] = 'changed'
        assert app.state.phone_calls['CAoffice']['number']['account'] == 'voice-phone'  # frozen at admission
        assert '<Hangup/>' in inbound('+15555550000', 'CAunknown').text
        settings['telephone']['numbers'][1]['enabled'] = False
        assert '<Hangup/>' in inbound('+15555550124', 'CAdisabled').text
        assert len(app.state.phone_calls) == 2
        settings['telephone']['enabled'] = False
        assert inbound('+15555550123', 'CAoff').status_code == 404
        settings['telephone']['enabled'] = True
        assert client.post('/voice/inbound', data={'CallSid': 'CAwrong'}).status_code == 403
