"""Signed caller admission, PIN retries and account selection before any compute request."""
import asyncio
from copy import deepcopy
from urllib.parse import urlsplit
from xml.etree import ElementTree
import json

import pytest
from starlette.testclient import TestClient
from loadcoach.groups import Group
from loadcoach.users import Users
from front_helpers import turnstile_to, door
from test_weightroom import GROUPS
from test_voice_telephone import phone
from voice.gateway import build_app
from voice.phone_access import decide
from voice.telephone import signature
from turnstile.phone_store import PhoneStore


@pytest.fixture
def admission(tmp_path):
    users = Users(str(tmp_path / 'accounts.db'), {**GROUPS,
        'app': Group(can=frozenset({'load', 'front-voice'})),
        'gateway': Group(can=frozenset({'load', 'front-voice', 'app-sign-in'}), models=('small',))})
    users.add('owner', 'admin'); users.add('voice-gateway', 'gateway'); users.add('person', 'app')
    users.change('person', own={'models': ['small', 'private']})
    app_key = users.new_key('voice-gateway')
    (tmp_path / 'gateway.key').write_text(app_key)
    (tmp_path / 'token.key').write_text('twilio-secret')
    front = turnstile_to(tmp_path, users, {})
    panel = TestClient(front, base_url='http://front:8442')
    owner = {'authorization': 'Bearer ' + users.new_key('owner')}
    assert panel.post('/loadcoach/telephone', headers=owner, json={'action': 'create_account'}).status_code == 200
    config = phone(); config['numbers'] = config['numbers'][:1]
    config['numbers'][0]['greeting'] = ''
    front.state.console_settings.changes.put('voice-gateway', 'telephone', config, 'owner')
    seen = []
    http = door(front, 8442)
    async def capture(request):
        seen.append(request.url.path)
    http.event_hooks['request'] = [capture]
    settings = dict(key_file=str(tmp_path / 'gateway.key'), telephone_token_file=str(tmp_path / 'token.key'),
        telephone=config, turnstile='http://front:8442', voice='http://compute:8445')
    gateway = build_app(settings, http)
    with TestClient(gateway, base_url='http://public:8081') as client:
        def send(call='CAone', caller='+15555550999', digits=None, action=None, attestation='', number='+15555550123'):
            front.state.console_settings.changes.put('voice-gateway', 'telephone', config, 'owner')
            params = {'CallSid': call, 'AccountSid': config['account_sid'], 'From': caller, 'To': number}
            if digits is not None:
                params['Digits'] = digits
            if attestation:
                params['StirVerstat'] = attestation
            target = action or 'https://phone.example.com/voice/inbound'
            signed = signature('twilio-secret', target, {key: [value] for key, value in params.items()})
            parsed = urlsplit(target)
            return client.post(parsed.path + ('?' + parsed.query if parsed.query else ''), data=params, headers={'x-twilio-signature': signed})
        send.panel, send.owner = panel, owner
        yield send, config, gateway, users, PhoneStore(users, tmp_path), seen


def test_anyone_allowlist_precedence_and_personal_account_session(admission):
    send, config, app, users, store, seen = admission
    number = config['numbers'][0]
    number['callers'] = {'mode': 'allowlist', 'entries': []}
    assert '<Hangup/>' in send().text and not app.state.phone_calls
    number['callers']['entries'] = [{'number': '+15555550999', 'account': 'person', 'note': 'Owner'}]
    assert '<Hangup/>' in send(call='CAunattested').text
    allowed = send(call='CApersonal', attestation='TN-Validation-Passed-A')
    assert '<Connect>' in allowed.text
    item = app.state.phone_calls['CApersonal']
    delegated = users.by_key(item['session_key'])
    assert delegated.name == 'person' and delegated.via == 'voice-gateway'
    assert delegated.group.models == ('small',) and not delegated.can('app-sign-in')
    number['callers']['mode'] = 'anyone'
    assert '<Connect>' in send(call='CApublic', caller='+15555550888').text
    assert set(seen) == {'/loadcoach/telephone/admit'}  # no compute, model or hold request


def test_pin_hashing_three_attempts_and_idempotent_retry(admission):
    send, config, app, users, store, seen = admission
    number = config['numbers'][0]
    number['callers'] = {'mode': 'pin', 'entries': []}
    asyncio.run(store.set_pin(number['number'], '684297'))
    verifier = users.db.execute('SELECT verifier FROM telephone_pins').fetchone()[0]
    assert verifier.startswith('scrypt$') and '684297' not in verifier
    response = send()
    assert '<Gather' in response.text and not app.state.phone_calls
    for attempt in range(3):
        action = ElementTree.fromstring(response.text).find('Gather').attrib['action']
        response = send(digits='111111', action=action)
        if attempt < 2:
            again = send(digits='111111', action=action)
            assert again.text == response.text  # Twilio's delivery retry does not spend another attempt
    assert '<Hangup/>' in response.text and '<Gather' not in response.text
    assert not app.state.phone_calls
    assert [row['reason'] for row in app.state.phone_attempts['Office'] if row['outcome'] == 'rejected'] == ['bad_pin'] * 3
    assert '<Hangup/>' in send(digits='684297').text  # cannot reset the same CallSid's attempt budget
    challenge = send(call='CAgood')
    action = ElementTree.fromstring(challenge.text).find('Gather').attrib['action']
    assert '<Connect>' in send(call='CAgood', digits='684297', action=action).text
    assert app.state.phone_calls['CAgood']['number']['account'] == 'voice-phone'
    assert set(seen) == {'/loadcoach/telephone/admit'}


def test_unattested_match_falls_back_to_phone_account_only(admission):
    send, config, app, users, store, seen = admission
    number = config['numbers'][0]
    number['callers'] = {'mode': 'allowlist', 'entries': [{'number': '+15555550999', 'account': 'person'}]}
    asyncio.run(store.set_pin(number['number'], '246810'))
    challenge = send()
    action = ElementTree.fromstring(challenge.text).find('Gather').attrib['action']
    assert '<Connect>' in send(digits='246810', action=action).text
    assert app.state.phone_calls['CAone']['number']['account'] == 'voice-phone'
    assert not app.state.phone_calls['CAone']['session_key']


def test_rate_limit_and_unknown_numbers_never_reserve_calls(admission, capsys):
    send, config, app, users, store, seen = admission
    config['numbers'][0]['max_calls_per_hour'] = 1
    assert '<Connect>' in send().text
    assert '<Connect>' in send().text  # retry is the original reservation
    assert '<Hangup/>' in send(call='CAsecond').text
    assert '<Hangup/>' in send(call='CAthird', caller='+15555550888').text
    before = len(seen)
    assert '<Hangup/>' in send(call='CAunknown', number='+15555550000').text
    assert len(seen) == before and len(app.state.phone_calls) == 1
    logs = capsys.readouterr().out
    assert '+15555550999' not in logs and '+15555550000' not in logs
    assert 'rate_limited' in logs and 'unknown_number' in logs and 'caller_hash' in logs


def test_admin_default_account_entry_skips_pin_and_invalid_pins_are_rejected(admission):
    send, config, app, users, store, seen = admission
    number = config['numbers'][0]
    number['callers'] = {'mode': 'pin', 'entries': [{'number': '+15555550999'}]}
    assert '<Connect>' in send().text
    for value in ('', 'abc', '123', '1' * 13):
        with pytest.raises(ValueError):
            asyncio.run(store.set_pin(number['number'], value))


@pytest.mark.parametrize('access', [None, {'mode': 'open'}, {'mode': 'pin', 'pin_hash': 'forbidden'},
    {'mode': 'allowlist', 'entries': {}}, {'mode': 'allowlist', 'entries': [{'number': '555'}]},
    {'mode': 'allowlist', 'entries': [{'number': '+15555550999', 'account': '../bad'}]},
    {'mode': 'allowlist', 'entries': [{'number': '+15555550999', 'note': 'x' * 301}]},
    {'mode': 'allowlist', 'entries': [{'number': '+15555550999'}, {'number': '+15555550999'}]}])
def test_caller_settings_validation(access):
    from voice.telephone_settings import validate
    config = phone(); config['numbers'][0]['callers'] = access
    with pytest.raises(ValueError):
        validate(config)


def test_caller_admin_endpoints_do_not_export_pin_hashes_or_plaintext(admission):
    send, config, app, users, store, seen = admission
    def post(**fields):
        return send.panel.post('/loadcoach/telephone', headers=send.owner, json=fields)
    assert post(action='set_pin', number='+15555550123', pin='903728').status_code == 200
    assert post(action='save_access', number='+15555550123', access_mode='allowlist',
                require_attested_caller_id=True, accept_user_numbers=True).status_code == 200
    assert post(action='add_caller', number='+15555550123', caller_number='+15555550999', caller_account='person', caller_note='Owner').status_code == 200
    result = send.panel.get('/loadcoach/telephone', headers=send.owner)
    assert result.json()['telephone']['numbers'][0]['pin_saved']
    assert '903728' not in result.text and 'scrypt$' not in result.text
    assert post(action='remove_caller', number='+15555550123', caller_number='+15555550999').status_code == 200
    assert post(action='remove_pin', number='+15555550123').status_code == 200
    assert not store.pin_saved('+15555550123')


def test_telephone_caller_access_browser_pin_is_never_prefilled(tmp_path):
    from test_voice_admin import voice_panel
    from test_weightroom import sign_in
    from test_console_browser import browser_context, playwright
    config = phone(); config['numbers'] = config['numbers'][:1]
    data = {'credentials': {'token': {'saved': True}, 'api': {}}, 'telephone': config,
            'accounts': ['voice-phone'], 'caller_accounts': ['person']}
    app, _, _, calls, _, _ = voice_panel(tmp_path, telephone_data=data)
    with TestClient(app) as server, playwright.sync_playwright() as engine:
        sign_in(server, 'jordan')
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/system/voice/telephone')
        page.locator('#phone-access-1-mode').select_option('pin')
        page.get_by_role('button', name='Save caller access', exact=True).click()
        playwright.expect(page.locator('.said')).to_contain_text('Saved.')
        request = next(call for call in calls if call.method == 'POST' and call.url.path == '/admin/telephone')
        assert json.loads(request.content)['access_mode'] == 'pin'
        page.get_by_text('Keypad PIN', exact=True).click()
        field = page.locator('#phone-access-1-pin')
        assert field.get_attribute('type') == 'password' and field.input_value() == ''
        field.fill('903728')
        page.get_by_role('button', name='Save PIN', exact=True).click()
        playwright.expect(page.locator('.said')).to_contain_text('Saved.')
        assert '903728' not in page.content() and page.locator('#phone-access-1-pin').input_value() == ''
        page.set_viewport_size({'width': 320, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
