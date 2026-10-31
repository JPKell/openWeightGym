"""Soft allowances reuse immutable attribution and retained request endings without gating work."""
import asyncio
import json
from pathlib import Path

import pytest

from loadcoach.log import Log
from loadcoach.statement import caller_of,user_of
from loadcoach.users import Users,hash_key
from turnstile.allowances import Allowances
from test_app import add_user,fake_llama_server,gateway,in_an_empty_folder
from test_weightroom import GROUPS,desk,sign_in
from test_postgresql import postgres


def store(path):
    users=Users(path,GROUPS)
    for name,group in (('pat','plain'),('jordan','admin'),('other','plain')):users.add(name,group)
    key=users.new_key('pat','Agent')
    return users,Log(path),key


def ending(log,user,cost='missing',what='done',parent=None):
    data=dict(group=user.group_name,account_id=user.identity,key_id=user.key_identity)
    first=parent or log.add('request','asked',user.name,**data)
    return log.add('request',what,user.name,of=first,**data,**({'cost':cost} if cost!='missing' else {})),first


def target(report,scope,subject):return next(r for r in report['rows'] if (r['scope'],r['subject'])==(scope,subject))


def test_soft_remaining_unknown_endings_duplicates_and_retries(tmp_path):
    users,log,key=store(str(tmp_path/'front.db'));service=Allowances(users,log)
    pat=users.by_key(key);admin=users.by_name('jordan')
    service.save(admin,'user',pat.identity,5)
    service.save(admin,'group','plain',10)
    service.save(admin,'key',pat.key_identity,2)
    ending(log,pat,1)
    _,first=ending(log,pat,99)
    ending(log,pat,.5,parent=first)  # a later definitive ending replaces the earlier one
    ending(log,pat,0,what='stopped')
    ending(log,pat,what='failed')
    ending(log,pat,.25,what='hung up')  # a retry has its own charge
    log.add('request','asked',pat.name,group='plain',account_id=pat.identity,key_id=pat.key_identity)
    result=service.report(pat)
    for scope,subject,limit in (('user',pat.identity,5),('key',pat.key_identity,2),('group','plain',10)):
        row=target(result,scope,subject)
        assert row['recorded']==1.75 and row['remaining']==limit-1.75
        assert row['requests']==5 and row['unknown_costs']==1 and row['unresolved']==1
        assert not row['manage']
    assert log.verify()[1] is None
    assert hash_key(key) not in json.dumps(result) and key not in json.dumps(result)
    assert user_of(caller_of(pat))==pat
    old=caller_of(pat);old.pop('key_identity');assert user_of(old).key_identity is None
    log.db.close();users.db.close()


def test_allowance_scope_rename_recreate_delete_and_window(tmp_path,monkeypatch):
    users,log,key=store(str(tmp_path/'front.db'));service=Allowances(users,log)
    pat=users.by_key(key);admin=users.by_name('jordan')
    service.save(admin,'user',pat.identity,1);service.save(admin,'key',pat.key_identity,1)
    with monkeypatch.context() as clock:
        clock.setattr('loadcoach.log.time.time',lambda:1)
        ending(log,pat,99)
    ending(log,pat,3)
    users.rename('pat','alex');current=users.by_key(key)
    row=target(service.report(current),'user',pat.identity)
    assert row['name']=='alex' and row['recorded']==3 and row['remaining']==0 and row['over_by']==2
    with pytest.raises(PermissionError):service.save(current,'user',pat.identity,99)
    assert not any(r['scope']=='key' and r['owner']!=pat.identity for r in service.report(current)['rows'])
    users.remove_key('alex',hash_key(key)[:8])
    assert not users.db.execute("SELECT 1 FROM spending_allowances WHERE scope='key'").fetchone()
    users.remove('alex');users.add('alex','plain')
    new=users.by_name('alex');assert new.identity!=pat.identity
    row=target(service.report(new),'user',new.identity)
    assert row['recorded']==0 and row['allowance']==0
    log.add('request','done','alex',group='plain',cost=7)  # do not guess an old account identity
    assert service.report(new)['legacy_records']==1
    assert target(service.report(new),'user',new.identity)['recorded']==0
    service.save(admin,'group','plain',10);users.groups.remove('plain')
    assert not users.db.execute("SELECT 1 FROM spending_allowances WHERE scope='group'").fetchone()
    log.db.close();users.db.close()


@pytest.mark.parametrize('value',[-1,True,None,'1',float('nan'),float('inf'),1e10])
def test_allowance_rejects_invalid_values(tmp_path,value):
    users,log,key=store(str(tmp_path/'front.db'));service=Allowances(users,log)
    with pytest.raises(ValueError):service.save(users.by_name('jordan'),'user',users.identity('pat'),value)
    log.db.close();users.db.close()


def test_api_soft_limit_never_blocks_native_requests(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin=add_user(caller,'owner','admin')
        principal=caller.users.by_key(admin['authorization'].split()[1])
        scope={'scope':'user','subject':principal.identity,'amount':.001}
        assert caller.post('/loadcoach/allowances',headers=admin,json=scope).status_code==200
        ending(caller.log,principal,100)
        async def send():
            # Concurrent native requests after exceeding the advisory allowance remain admitted.
            import httpx
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=caller.app),base_url='http://front:8442') as api:
                return await asyncio.gather(*(api.post('/v1/chat/completions',headers=admin,json={'model':'small','messages':[]}) for _ in range(2)))
        answers=caller.portal.call(send)
        assert all(r.status_code==200 for r in answers)
        report=caller.get('/loadcoach/allowances',headers=admin).json()
        row=target(report,'user',principal.identity)
        assert row['remaining']==0 and row['over_by']>99
        facts=caller.get('/loadcoach/requests/'+answers[0].headers['x-loadcoach-request-id'],headers=admin).json()
        assert facts['asked']['account_id']==principal.identity==facts['ended']['account_id']
        assert facts['asked']['key_id']==principal.key_identity==facts['ended']['key_id']
        assert caller.post('/loadcoach/allowances',headers=admin,json={**scope,'amount':0}).status_code==200
        assert target(caller.get('/loadcoach/allowances',headers=admin).json(),'user',principal.identity)['remaining'] is None
        denied=caller.get('/loadcoach/allowances')
        assert denied.status_code==401 and denied.headers['cache-control']=='no-store'


def test_front_allowance_forms_offline_and_scope(desk,tmp_path):
    browser,switch,users=desk;sign_in(browser,'jordan')
    identity=users.identity('pat')
    page=browser.post('/allowances',data={'target':'user:'+identity,'amount':'4.50'})
    assert page.headers['cache-control']=='no-store'
    assert page.status_code==200 and '4.5000' in page.text and 'Spending allowances' in page.text
    assert browser.post('/allowances',data={'target':'user:'+identity,'amount':'NaN'}).status_code==400
    switch.off=True
    assert 'may be stale' in browser.get('/allowances').text
    sign_in(browser,'pat')
    page=browser.get('/allowances')
    assert page.status_code==200 and 'View only' in page.text and 'jordan /' not in page.text
    assert browser.post('/allowances',data={'target':'user:'+identity,'amount':'100'}).status_code==403
    assert 'Spending allowances and remaining' in browser.get('/usage').text


def test_allowances_postgresql_transfer_and_unresolved(tmp_path,postgres):
    from db import Database
    from db.transfer import copy_store
    users,log,key=store(postgres);service=Allowances(users,log)
    pat=users.by_key(key);service.save(users.by_name('jordan'),'user',pat.identity,9)
    ending(log,pat,2)
    log.add('request','asked','pat',account_id=pat.identity,key_id=pat.key_identity,group='plain')
    result=target(service.report(pat),'user',pat.identity)
    assert result['remaining']==7 and result['unresolved']==1
    users.db.close();log.db.close()
    copied=Database.sqlite(tmp_path/'copied.db')
    assert copy_store(postgres,copied)['state']=='verified'
    users=Users(copied,GROUPS);log=Log(copied)
    assert target(Allowances(users,log).report(users.by_key(key)),'user',pat.identity)['remaining']==7
    users.db.close();log.db.close()


def test_allowances_table_in_chromium(desk):
    from playwright.sync_api import sync_playwright,expect
    browser,_,users=desk;sign_in(browser,'jordan')
    folder=Path('data/m107-verification');folder.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as engine:
        chrome=engine.chromium.launch(headless=True);context=chrome.new_context();page=context.new_page();errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def serve(route):
            request=route.request
            if request.url.startswith('http://allowance.test/strip/stream'):
                route.fulfill(status=204)  # A live SSE body never completes through TestClient's sync route bridge.
                return
            response=browser.request(request.method,request.url.replace('http://allowance.test',''),content=request.post_data_buffer)
            route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k in ('content-type','location')},body=response.content)
        context.route('http://allowance.test/**',serve)
        for width in (1440,390,320):
            page.set_viewport_size({'width':width,'height':1000});page.goto('http://allowance.test/allowances')
            expect(page.get_by_role('heading',name='Spending allowances',exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(folder/f'allowances-{width}.png'),full_page=True)
        page.set_viewport_size({'width':1440,'height':1000})
        row=page.get_by_role('row').filter(has=page.get_by_role('rowheader',name='pat',exact=True))
        row.get_by_role('spinbutton').fill('12.75');row.get_by_role('button',name='Save',exact=True).click()
        expect(page.get_by_role('row').filter(has=page.get_by_role('rowheader',name='pat',exact=True))).to_contain_text('$12.7500')
        assert not errors;chrome.close()


def test_allowances_group_snapshot_and_partial_unattributed_cost(tmp_path):
    users,log,key=store(str(tmp_path/'front.db'));service=Allowances(users,log)
    pat=users.by_key(key);other=users.by_name('other');admin=users.by_name('jordan')
    service.save(admin,'group','plain',20)
    ending(log,pat,.1);ending(log,pat,.2)
    ending(log,other,3)  # background work has user/group attribution but no key
    users.change('other',group='careful')
    result=service.report(pat)
    assert target(result,'group','plain')['recorded']==3.3
    assert target(result,'key',pat.key_identity)['recorded']==.3
    assert result['unattributed_keys']==0  # no unrelated user's metadata count leaks
    assert service.report(admin)['unattributed_keys']==1
    assert target(service.report(other),'group','plain')['recorded']==3.3  # prior principal snapshot
    current=users.by_name('other')
    assert not any(r['scope']=='group' and r['subject']=='plain' for r in service.report(current)['rows'])
    log.db.close();users.db.close()


def test_legacy_accounts_copy_refuses_populated_new_policies(tmp_path):
    from typer.testing import CliRunner
    from weightroom.app import cli
    source=str(tmp_path/'old.db');users,log,key=store(source)
    Allowances(users,log).save(users.by_name('jordan'),'group','plain',20)
    users.db.close();log.db.close()
    config=tmp_path/'front.toml';config.write_text('compute="http://c:8442"\ndata="new"\n')
    result=CliRunner().invoke(cli,['--config',str(config),'accounts-from',source])
    assert result.exit_code and 'cannot copy spending policies' in str(result.exception)


def test_unresolved_count_uses_the_parent_index(tmp_path):
    """A time-ordered scan of every earlier request took 41 s on the front and stalled turnstile."""
    users,log,key=store(str(tmp_path/'front.db'));pat=users.by_key(key)
    ending(log,pat,1)
    asked=[]
    log.db.raw.set_trace_callback(lambda sql:asked.append(sql) if 'NOT EXISTS' in sql else None)
    Allowances(users,log).report(pat)
    log.db.raw.set_trace_callback(None)
    plan=' '.join(str(row) for row in log.db.raw.execute('EXPLAIN QUERY PLAN '+asked[0]))
    assert 'SEARCH e USING INDEX events_request_parent' in plan
    log.db.close();users.db.close()
