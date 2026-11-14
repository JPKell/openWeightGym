"""Persistent sign-in protection and catalogue-backed live credential policies."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from loadcoach.client_address import client_address
from loadcoach.groups import Group
from loadcoach.setting_catalog import default_for, get
from loadcoach.users import SignInRefused, Users, hash_key
from test_postgresql import postgres  # noqa: F401
from test_setting_requests import control  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401


@pytest.fixture
def accounts(tmp_path):
    users = Users(tmp_path/'accounts.db', {'plain':Group()})
    users.add('pat','plain')
    users.change('pat',password='correct horse')
    yield users
    users.db.close()


def refused(users, name='pat', address='192.0.2.1'):
    with pytest.raises(SignInRefused) as problem:
        asyncio.run(users.sign_in(name,'wrong',address=address))
    return problem.value


def test_locks_persist_are_pair_scoped_and_skip_hashing(accounts, monkeypatch):
    users = accounts
    for index in range(5):
        error = refused(users)
        assert error.status == (429 if index == 4 else 401)
    assert error.retry_after == 1800
    reopened = Users(users.db.database, {})
    try:
        def no_hash(*args):
            pytest.fail('Locked attempts must not hash a password')
        with monkeypatch.context() as patch:
            patch.setattr('loadcoach.users.check_password',no_hash)
            assert refused(reopened).status == 429
        assert asyncio.run(reopened.sign_in('pat','correct horse',address='192.0.2.2'))
        assert refused(reopened,'other').status == 401
        assert len(reopened.failures.listing('pat')) == 1
        reopened.failures.unlock('pat')
        assert asyncio.run(reopened.sign_in('pat','correct horse',address='192.0.2.1'))
    finally:
        reopened.db.close()


def test_spraying_counts_all_accounts_and_success_never_resets_it(accounts):
    users = accounts
    for index in range(19):
        assert refused(users,'missing'+str(index)).status == 401
    asyncio.run(users.sign_in('pat','correct horse',address='192.0.2.1'))
    assert refused(users,'missing19').status == 429
    assert refused(users,'pat').status == 429
    users.failures.unlock('pat')
    assert refused(users,'pat').status == 429  # shared address lock remains
    users.failures.unlock('pat','192.0.2.1')
    assert asyncio.run(users.sign_in('pat','correct horse',address='192.0.2.1'))


def test_counter_windows_prune_and_policy_reads_are_live(accounts,monkeypatch):
    clock = [10000.0]
    policy = {name:default_for('turnstile',name) for name in ('sign_in_max_failures','sign_in_window_minutes','sign_in_lockout_minutes')}
    accounts.policy_getter = policy.__getitem__
    monkeypatch.setattr('loadcoach.sign_in.time',SimpleNamespace(time=lambda:clock[0]))
    for _ in range(4):
        assert accounts.failures.failed('pat','a') == 0
    clock[0] += 120
    policy['sign_in_window_minutes'] = 1
    assert accounts.failures.failed('pat','a') == 0  # old four failures have left the new window
    policy['sign_in_max_failures'] = 2
    policy['sign_in_lockout_minutes'] = 2
    assert accounts.failures.failed('pat','a') == 120
    clock[0] += 121
    assert accounts.failures.remaining('pat','a') == 0
    assert accounts.failures.failed('pat','a') == 0


def test_concurrent_process_connections_do_not_lose_failures(accounts):
    source = accounts.db.database
    stores = [Users(source,{}) for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda store:store.failures.failed('pat','same'),stores))
        assert sum(value > 0 for value in results) == 4
        assert accounts.failures.remaining('pat','same') > 0
    finally:
        for store in stores:
            store.db.close()


def test_rename_preserves_the_stricter_existing_pair_lock(accounts):
    for _ in range(5):
        accounts.failures.failed('pat','a')
    accounts.failures.failed('alex','a')
    accounts.rename('pat','alex')
    assert accounts.failures.remaining('alex','a') > 0
    assert not accounts.failures.listing('pat')


@pytest.mark.parametrize('peer,forwarded,trusted,expected',[
    ('192.0.2.1','198.51.100.8',[],'192.0.2.1'),
    ('127.0.0.1','198.51.100.8',['127.0.0.1'],'198.51.100.8'),
    ('127.0.0.1','203.0.113.99,198.51.100.8,10.0.0.1',['127.0.0.1','10.0.0.0/8'],'198.51.100.8'),
    ('::1','2001:db8::8,::1',['::1'],'2001:db8::8'),
    ('127.0.0.1','bad,198.51.100.8',['127.0.0.1'],'127.0.0.1'),
    ('127.0.0.1',','.join(['198.51.100.8']*33),['127.0.0.1'],'127.0.0.1'),
])
def test_only_explicit_proxy_hops_can_supply_a_client_address(peer,forwarded,trusted,expected):
    assert client_address(peer,forwarded,trusted) == expected


@pytest.mark.parametrize('program,name,bad',[
    ('turnstile','sign_in_max_failures',0),('turnstile','sign_in_max_failures',101),
    ('turnstile','sign_in_window_minutes',0),('turnstile','sign_in_window_minutes',1441),
    ('turnstile','sign_in_lockout_minutes',0),('turnstile','sign_in_lockout_minutes',10081),
    ('turnstile','password_min_length',7),('turnstile','password_min_length',129),
    ('turnstile','api_key_max_days',-1),('turnstile','api_key_max_days',36501),
    ('turnstile','session_idle_hours',-1),('turnstile','session_idle_hours',8761),
    ('weightroom','trusted_proxies',['not an address']),('weightroom','trusted_proxies',[True]),
])
def test_security_validation_comes_from_the_catalogue(program,name,bad):
    with pytest.raises(ValueError):
        get(program,name).check(bad)


def test_live_security_policies_apply_without_building_a_new_service(control):
    api, app, _, _ = control
    users = app.state.users
    users.add('pat','admin')
    users.change('pat',password='correct horse')
    original = users.new_key('pat')
    assert api.post('/loadcoach/settings/turnstile',json={
        'sign_in_max_failures':1,'sign_in_window_minutes':1,'sign_in_lockout_minutes':2,
        'password_min_length':16,'api_key_max_days':30,'session_idle_hours':1}).status_code == 200
    for name in ('sign_in_max_failures','sign_in_window_minutes','sign_in_lockout_minutes','password_min_length','api_key_max_days','session_idle_hours'):
        assert users.security_setting(name) != default_for('turnstile',name)
    assert refused(users).retry_after == 120
    with pytest.raises(ValueError,match='16'):
        users.change('pat',password='short password')
    # Existing password and original permanent API key keep working.
    session = asyncio.run(users.sign_in('pat','correct horse',address='elsewhere'))
    assert users.by_key(original) and users.by_key(session)
    assert len(users.temporary_password('pat')) >= 16
    with pytest.raises(ValueError,match='30'):
        users.new_key('pat',days=0)
    expiring = users.new_key('pat')
    assert 29*86400 < users.by_key(expiring).until-time.time() <= 30*86400
    # API key idle time is irrelevant; an expired sign-in stays expired after disabling idle expiry.
    session = users.new_key('pat',hours=24)
    with users.db:
        users.db.execute('UPDATE session_details SET last_seen=? WHERE key_hash=?',(time.time()-7200,hash_key(session)))
    assert not users.by_key(session) and users.by_key(expiring)
    assert api.post('/loadcoach/settings/turnstile',json={'session_idle_hours':0}).status_code == 200
    assert not users.by_key(session)


def test_expiring_api_keys_are_never_treated_as_sessions(accounts):
    permanent, expiring = accounts.new_key('pat'), accounts.new_key('pat',days=30)
    session = accounts.new_key('pat',hours=2)
    rows = accounts.listing('pat')[0]
    assert len(rows['keys']) == 2 and rows['sessions'] == 1
    assert accounts.by_key(session).session and not accounts.by_key(expiring).session
    assert len(accounts.sessions('pat',session)) == 1
    assert not accounts.sign_out(expiring)
    accounts.end_sessions('pat')
    assert not accounts.by_key(session) and accounts.by_key(permanent) and accounts.by_key(expiring)


def test_throttling_has_http_retry_after_and_audit_event_only_in_local_tests(desk):
    browser, switch, _ = desk
    for index in range(5):
        answer = sign_in(browser,'pat','wrong')
        assert answer.status_code == (429 if index == 4 else 401)
    assert int(answer.headers['retry-after']) > 0
    assert 'name="password" type="hidden"' not in answer.text
    events = switch.front.state.log.read(kind='sign-in',what='throttled')
    assert events and events[-1]['user'] == 'pat'
    assert 'wrong' not in str(events)


def test_account_controls_show_key_expiry_and_current_password_policy(desk):
    browser, switch, _ = desk
    sign_in(browser,'pat')
    page = browser.get('/account/keys')
    for days in (0,30,90,365):
        assert f'value="{days}"' in page.text
    saved = browser.post('/account/keys',data={'label':'Expiring notebook','days':'30'})
    assert saved.status_code == 200 and 'Expiring notebook' in saved.text and 'Expires' in saved.text
    switch.front.state.console_settings.changes.put('turnstile','password_min_length',16,'test')
    assert 'minlength="16"' in browser.get('/account').text
    switch.front.state.console_settings.changes.put('turnstile','api_key_max_days',15,'test')
    bounded = browser.get('/account/keys').text
    assert 'value="15" selected' in bounded and 'value="0"' not in bounded
    failed = browser.post('/account/keys',data={'label':'Forbidden never','days':'0'})
    assert failed.status_code == 400 and '15 days' in failed.text


def test_cli_lists_and_unlocks_persistent_locks(accounts):
    from loadcoach.cli import users_cli
    for _ in range(5):
        accounts.failures.failed('pat','192.0.2.1')
    runner = CliRunner()
    supply = lambda:(None,accounts,None)
    listed = runner.invoke(users_cli,['locks','pat'],obj=supply)
    assert listed.exit_code == 0, listed.output
    assert '192.0.2.1' in listed.output
    cleared = runner.invoke(users_cli,['unlock','pat','--address','192.0.2.1'],obj=supply)
    assert cleared.exit_code == 0,cleared.output
    assert not accounts.failures.remaining('pat','192.0.2.1')


def test_postgresql_failure_counters_and_key_kinds(postgres):
    users = Users(postgres, {'plain':Group()})
    users.add('pat','plain')
    try:
        test_concurrent_process_connections_do_not_lose_failures(users)
        users.failures.unlock('pat','same')
        test_expiring_api_keys_are_never_treated_as_sessions(users)
        test_rename_preserves_the_stricter_existing_pair_lock(users)
    finally:
        users.db.close()


@pytest.mark.parametrize('width',[1440,390])
def test_security_and_expiry_controls_render_narrow_empty_and_error_states(desk,width):
    from test_console_browser import browser_context,playwright
    server,switch,_ = desk
    from loadcoach.settings import said
    from turnstile.app import Settings
    switch.front.state.console_settings.changes.seen('turnstile',said(vars(Settings()),{}, {},'turnstile'))
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page = browser_context(engine,server)
        page.set_viewport_size({'width':width,'height':1000})
        page.goto('http://console.test/settings/security')
        field = page.locator('[name="value-sign_in_max_failures"]')
        playwright.expect(field).to_be_visible()
        page.get_by_label('Find a setting',exact=True).fill('sign-in failures')
        playwright.expect(field).to_be_visible()
        page.evaluate("document.querySelector('[name=value-sign_in_max_failures]').closest('.settings-row').replaceWith(document.querySelector('[name=value-sign_in_max_failures]').closest('.settings-row').cloneNode(true))")
        playwright.expect(field).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://console.test/account/keys')
        playwright.expect(page.locator('#keys')).to_contain_text('No keys.')
        page.locator('#label').fill('Notebook')
        page.locator('#days').select_option('30')
        page.get_by_role('button',name='Make a key',exact=True).click()
        playwright.expect(page.locator('#keys')).to_contain_text('Notebook')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        switch.front.state.console_settings.changes.put('turnstile','api_key_max_days',15,'test')
        # Submit a now-refused draft: the shared native error refill retains it.
        page.locator('#label').fill('Refused draft')
        page.locator('#days').select_option('365')
        page.get_by_role('button',name='Make a key',exact=True).click()
        playwright.expect(page.locator('[role=alert]').first).to_contain_text('15 days')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()


def test_proxy_warning_is_based_on_recent_actual_sign_ins(desk):
    server,switch,_ = desk
    sign_in(server,'jordan',**{})
    assert not switch.front.state.console_settings.security_status()['proxy_warning']
    # Simulated local-only evidence; this never contacts the live system.
    log = switch.front.state.log
    with log.db:
        log.db.execute("DELETE FROM events WHERE kind='user' AND what='signed-in'")
    log.add('user','signed-in','jordan','127.0.0.1',at_front_from='127.0.0.1',proxy_seen=True)
    answer = server.get('/settings/security')
    assert answer.status_code == 200 and 'Per-address locks may affect everyone' in answer.text


def test_trusted_proxies_are_top_level_file_only_and_validated_by_loaders(tmp_path):
    from pathlib import Path
    import tomllib
    from weightroom.app import load_settings
    root = Path(__file__).resolve().parents[1]
    for file in ('settings/defaults/weightroom.toml','settings/examples/weightroom.toml'):
        assert tomllib.loads((root/file).read_text())['trusted_proxies'] == []
    (tmp_path/'key').write_text('local test')
    path = tmp_path/'weightroom.toml'
    path.write_text('key_file="key"\ntrusted_proxies=["127.0.0.1","10.0.0.0/8"]\n')
    assert load_settings(path).trusted_proxies == ['127.0.0.1','10.0.0.0/8']
    path.write_text('key_file="key"\ntrusted_proxies=["a hostname"]\n')
    with pytest.raises(SystemExit,match='IP addresses'):
        load_settings(path)
    assert get('weightroom','trusted_proxies').tier == 'file'


def test_legacy_short_passwords_still_authenticate_and_idle_default_is_off(accounts):
    from loadcoach.users import hash_password
    with accounts.db:
        accounts.db.execute('UPDATE users SET password=? WHERE name=?',(hash_password('old'),'pat'))
    session = asyncio.run(accounts.sign_in('pat','old'))
    with accounts.db:
        accounts.db.execute('UPDATE session_details SET last_seen=? WHERE key_hash=?',(time.time()-7200,hash_key(session)))
    assert accounts.security_setting('session_idle_hours') == 0 and accounts.by_key(session)
    with pytest.raises(ValueError,match='8'):
        accounts.change('pat',password='short')


def test_old_keys_migrate_without_replacing_credentials_or_expiry(tmp_path):
    from db import Database,migrate
    from db.schemas import USERS
    source = Database.sqlite(tmp_path/'legacy.db')
    connection = source.connect()
    migrate(source,connection,'loadcoach.users',tuple(step for step in USERS if step.version<8))
    with connection:
        connection.execute('INSERT INTO users(name,grp) VALUES(?,?)',('pat','plain'))
        for key,expiry in [('old-api',None),('old-session',time.time()+3600)]:
            connection.execute('INSERT INTO keys(hash,user,label,created,expires) VALUES(?,?,?,?,?)',
                               (hash_key(key),'pat','old','2026-11-14T00:00:00',expiry))
    connection.close()
    users = Users(source,{'plain':Group()})
    try:
        assert not users.by_key('old-api').session and users.by_key('old-session').session
        assert users.db.execute('SELECT last_seen FROM session_details WHERE key_hash=?',(hash_key('old-session'),)).fetchone()
    finally:
        users.db.close()


@pytest.mark.parametrize('trusted,expected',[([], '127.0.0.1'),(['127.0.0.1'],'198.51.100.8')])
def test_asgi_proxy_policy_precedes_signed_client_attribution(trusted,expected):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from loadcoach.client_address import ProxyAddress
    from loadcoach.client_origin import ClientOrigin,CURRENT
    async def inspect(request):
        return JSONResponse({'peer':request.client.host,'socket':request.scope['suite_socket_peer'],'origin':CURRENT.get()})
    app = ProxyAddress(ClientOrigin(Starlette(routes=[Route('/',inspect)])),trusted=trusted)
    async def check():
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',123)),base_url='http://console') as client:
            return await client.get('/',headers={'x-forwarded-for':'203.0.113.99,198.51.100.8'})
    answer = asyncio.run(check())
    assert answer.json() == {'peer':expected,'socket':'127.0.0.1','origin':expected}


def test_console_server_does_not_rewrite_socket_peers_before_trust_checks(monkeypatch):
    import weightroom.app as service
    from typer.testing import CliRunner
    made = []
    monkeypatch.setattr(service,'load_settings',lambda path:service.Settings(compute='',key='local test'))
    monkeypatch.setattr(service,'build_app',lambda settings:object())
    monkeypatch.setattr(service.uvicorn,'run',lambda app,**settings:made.append(settings))
    answer = CliRunner().invoke(service.cli,[])
    assert answer.exit_code == 0,answer.output
    assert made[0]['proxy_headers'] is False
