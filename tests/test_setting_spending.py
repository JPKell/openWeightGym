"""Live recorded allowances: hosted-only admission, warning headers and calendar windows."""
import json
from datetime import datetime, timezone

import pytest

from loadcoach.setting_catalog import validate
from turnstile.allowances import Allowances
from test_allowances import store, ending, target
from test_hosted_front import rig, ask  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright
from test_postgresql import postgres  # noqa: F401


def reported(app):
    from loadcoach.settings import said
    from turnstile.app import Settings
    app.state.console_settings.changes.seen('turnstile',said(vars(Settings()),{}, {},'turnstile'))


def managed(rig):
    reported(rig.app)
    rig.users.groups.put('admin', {**rig.users.groups.stored('admin'), 'can':[
        'load','hosted','manage-hosted','manage-models','manage-users','manage-groups','see-log','manage-settings']})
    return rig.users.by_name('ada')


def set_mode(rig, mode, period=None):
    values={'allowance_mode':mode, **({'allowance_period':period} if period else {})}
    answer=rig.coach.post('/loadcoach/settings/turnstile',json=values,headers=rig.keys['ada'])
    assert answer.status_code==200, answer.text


@pytest.mark.parametrize('scope',['user','key','group'])
def test_each_allowance_blocks_hosted_only_and_live_mode_changes_apply(rig,scope):
    admin=managed(rig); user=rig.users.by_key(rig.keys['pat']['authorization'].split()[1])
    subject={'user':user.identity,'key':user.key_identity,'group':user.group_name}[scope]
    policy=rig.app.state.allowances; policy.save(admin,scope,subject,1)
    ending(rig.app.state.log,user,1)
    set_mode(rig,'block')
    before=len(rig.service_calls)
    refused=ask(rig)
    assert refused.status_code==403 and refused.json()['error']['code']=='allowance_exceeded'
    assert scope+' allowance' in refused.text and 'rolling window at' in refused.text
    assert len(rig.service_calls)==before
    # A native model remains usable after exactly the same recorded allowance is exhausted.
    native=ask(rig,model='small')
    assert native.status_code==200 and len(rig.service_calls)==before
    set_mode(rig,'warn')
    warned=ask(rig)
    assert warned.status_code==200 and len(rig.service_calls)==before+1
    header=json.loads(warned.headers['x-suite-allowance'])
    assert len(header)==1 and header[0]['scope']==scope and header[0]['remaining']==0
    assert 'authorization' not in warned.headers['x-suite-allowance']
    set_mode(rig,'advisory')
    advisory=ask(rig)
    assert advisory.status_code==200 and 'x-suite-allowance' not in advisory.headers


def test_other_account_and_other_key_allowances_do_not_gate_current_key(rig):
    admin=managed(rig); user=rig.users.by_key(rig.keys['pat']['authorization'].split()[1])
    other=rig.users.by_key(rig.keys['ada']['authorization'].split()[1])
    policy=rig.app.state.allowances; policy.save(admin,'key',other.key_identity,1)
    ending(rig.app.state.log,other,10)
    other_token=rig.users.new_key('pat','Separate caller'); other_user=rig.users.by_key(other_token)
    policy.save(admin,'key',other_user.key_identity,1)
    ending(rig.app.state.log,other_user,10)
    set_mode(rig,'block')
    assert ask(rig).status_code==200
    blocked=rig.coach.post('/v1/chat/completions',headers={'authorization':'Bearer '+other_token},
                          json={'model':'gpt','messages':[]})
    assert blocked.status_code==403 and 'Separate caller' in blocked.text


def test_unknown_zero_and_inflight_costs_do_not_exhaust_allowance(rig):
    admin=managed(rig); user=rig.users.by_key(rig.keys['pat']['authorization'].split()[1])
    rig.app.state.allowances.save(admin,'user',user.identity,1)
    ending(rig.app.state.log,user)
    ending(rig.app.state.log,user,0)
    rig.app.state.log.add('request','asked',user.name,account_id=user.identity,group=user.group_name,key_id=user.key_identity)
    set_mode(rig,'block')
    assert ask(rig).status_code==200


def test_warn_metadata_is_present_for_stream_requested_hosted_calls(rig):
    admin=managed(rig); user=rig.users.by_key(rig.keys['pat']['authorization'].split()[1])
    rig.app.state.allowances.save(admin,'user',user.identity,1)
    set_mode(rig,'warn')
    result=rig.coach.post('/v1/chat/completions',headers=rig.keys['pat'],json={'model':'gpt','messages':[],'stream':True})
    assert result.status_code==200 and json.loads(result.headers['x-suite-allowance'])[0]['allowance']==1


@pytest.mark.parametrize('period',['rolling_24h','calendar_month'])
def test_period_reads_same_retained_records_and_updates_without_restart(tmp_path,monkeypatch,period):
    users,log,key=store(str(tmp_path/'accounts.db')); user=users.by_key(key)
    selected={'allowance_mode':'block','allowance_period':'rolling_24h'}
    zone=['America/New_York']
    service=Allowances(users,log,setting=selected.__getitem__,timezone=lambda:zone[0])
    admin=users.by_name('jordan'); service.save(admin,'user',user.identity,1)
    now=datetime(2026,11,15,12,tzinfo=timezone.utc).timestamp()
    try:
        with monkeypatch.context() as clock:
            clock.setattr('loadcoach.log.time.time',lambda:now-2*86400)
            ending(log,user,2)
        assert target(service.report(user,now=now),'user',user.identity)['recorded']==0
        selected['allowance_period']=period
        report=service.report(user,now=now)
        row=target(report,'user',user.identity)
        assert row['recorded']==(2 if period=='calendar_month' else 0)
        if period=='calendar_month':
            assert report['since']==datetime(2026,11,1,4,tzinfo=timezone.utc).timestamp()
            assert row['reset_at']==datetime(2026,12,1,5,tzinfo=timezone.utc).timestamp()
            zone[0]='UTC'
            assert service.report(user,now=now)['since']==datetime(2026,11,1,tzinfo=timezone.utc).timestamp()
    finally: log.db.close(); users.db.close()


@pytest.mark.parametrize('mode',['advisory','warn','block'])
def test_modes_and_periods_validate_through_catalogue(mode):
    assert validate('turnstile','allowance_mode',mode)==mode
    with pytest.raises(ValueError):validate('turnstile','allowance_mode','soft')
    with pytest.raises(ValueError):validate('turnstile','allowance_period','year')


def test_personal_threshold_uses_current_policy_and_account_choices(rig):
    from turnstile.personal import notices
    admin=managed(rig); user=rig.users.by_key(rig.keys['pat']['authorization'].split()[1])
    policy=rig.app.state.allowances; policy.save(admin,'user',user.identity,10)
    ending(rig.app.state.log,user,8.5)
    set_mode(rig,'warn')
    changes=rig.app.state.console_settings.changes
    changes.put('weightroom','alert_allowance_percent',90,'ada')
    assert not [n for n in notices(rig.users,rig.app.state.log,user,policy)['items'] if n['category']=='Spending']
    changes.put('weightroom','alert_allowance_percent',80,'ada')
    assert len([n for n in notices(rig.users,rig.app.state.log,user,policy)['items'] if n['category']=='Spending'])==1
    rig.users.choose('pat',{'alert_spending':False})
    assert not [n for n in notices(rig.users,rig.app.state.log,user,policy)['items'] if n['category']=='Spending']


def test_allowances_native_settings_save_refusal_and_reset(desk):
    browser,switch,users=desk; reported(switch.front); sign_in(browser,'jordan')
    page=browser.get('/allowances')
    assert 'name="value-allowance_mode"' in page.text
    saved=browser.post('/settings/turnstile',data={'return_to':'/allowances','value-allowance_mode':'"block"'})
    assert saved.status_code==200 and 'Saved.' in saved.text and 'Block allowances' in saved.text
    refused=browser.post('/settings/turnstile',data={'return_to':'/allowances','value-allowance_mode':'"invented"'})
    assert refused.status_code==400 and 'Spending allowances' in refused.text
    reset=browser.post('/settings/turnstile',data={'return_to':'/allowances','reset':'allowance_mode'})
    assert reset.status_code==200 and 'Advisory allowances' in reset.text


@pytest.mark.parametrize('width',[1440,390])
def test_allowance_policy_filters_and_error_states_in_browser(desk,width):
    browser,switch,users=desk; reported(switch.front); sign_in(browser,'jordan')
    with playwright.sync_playwright() as engine:
        chromium,page=browser_context(engine,browser)
        try:
            page.set_viewport_size({'width':width,'height':1000})
            page.goto('http://console.test/allowances')
            mode=page.locator('#setting-turnstile-allowance_mode')
            playwright.expect(mode).to_have_value('"advisory"')
            mode.select_option('"warn"')
            page.get_by_role('button',name='Save turnstile',exact=True).click()
            playwright.expect(page.locator('.said-box')).to_contain_text('Saved.')
            page.goto('http://console.test/allowances')
            playwright.expect(mode).to_have_value('"warn"')
            page.locator('#settings-search').fill('allowance period')
            playwright.expect(page.locator('#setting-turnstile-allowance_period')).to_be_visible()
            page.locator('#settings-search').fill('no-such-setting')
            playwright.expect(page.locator('[data-settings-no-match]')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally: chromium.close()


def test_empty_zone_uses_host_zone_and_december_rolls_into_new_year(tmp_path,monkeypatch):
    users,log,key=store(str(tmp_path/'accounts.db'))
    selected={'allowance_mode':'block','allowance_period':'calendar_month'}
    service=Allowances(users,log,setting=selected.__getitem__)
    monkeypatch.setenv('TZ','America/New_York')
    try:
        since,until,_=service.window(datetime(2026,12,15,tzinfo=timezone.utc).timestamp())
        assert since==datetime(2026,12,1,5,tzinfo=timezone.utc).timestamp()
        assert until==datetime(2027,1,1,5,tzinfo=timezone.utc).timestamp()
    finally: log.db.close(); users.db.close()


def test_real_sse_warning_header_is_authoritative_and_not_sent_to_provider(rig):
    import httpx
    admin=managed(rig); user=rig.users.by_key(rig.keys['pat']['authorization'].split()[1])
    rig.app.state.allowances.save(admin,'user',user.identity,1)
    set_mode(rig,'warn')
    captured=[]
    def stream(request):
        captured.append(dict(request.headers))
        return httpx.Response(200,headers={'content-type':'text/event-stream','x-suite-allowance':'provider-forged'},
            stream=httpx.ByteStream(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'))
    hosted=rig.app.state.routing.hosted; previous=hosted.client
    hosted.client=httpx.AsyncClient(transport=httpx.MockTransport(stream))
    try:
        result=ask(rig)
        assert result.status_code==200 and 'data: [DONE]' in result.text
        assert json.loads(result.headers['x-suite-allowance'])[0]['scope']=='user'
        assert 'x-suite-allowance' not in captured[0]
        set_mode(rig,'advisory')
        assert 'x-suite-allowance' not in ask(rig).headers
    finally:
        rig.coach.portal.call(hosted.client.aclose)
        hosted.client=previous


def test_postgresql_allowance_policy_reuses_recorded_costs(postgres,tmp_path):
    from test_weightroom import GROUPS
    from loadcoach.users import Users
    from loadcoach.log import Log
    users=Users(postgres,GROUPS); log=Log(postgres)
    selected={'allowance_mode':'advisory','allowance_period':'rolling_24h'}
    service=Allowances(users,log,setting=selected.__getitem__)
    try:
        users.add('pat','plain'); users.add('jordan','admin')
        user=users.by_key(users.new_key('pat'))
        service.save(users.by_name('jordan'),'user',user.identity,1)
        ending(log,user,1)
        assert service.admission(user)==(None,{})
        selected['allowance_mode']='block'
        assert service.admission(user)[0]['code']=='allowance_exceeded'
        selected['allowance_period']='calendar_month'
        assert service.admission(user)[0]['code']=='allowance_exceeded'
    finally: log.db.close(); users.db.close()
