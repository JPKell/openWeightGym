"""Signed control-node remote MCP management, live routing and write-only credentials."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from front_helpers import front_of
from loadcoach.groups import Group,read_group
from loadcoach.users import Users
from loadcoach.setting_catalog import validate
from toollocker.door import Config,build_app
from toollocker.remote_settings import ManagedRemote,RemoteKeys
from test_toollocker import FAKE,far_files,free_port,ask,said
from test_console_browser import browser_context,playwright
from test_postgresql import postgres  # noqa: F401


def gateway(tmp_path,servers=None,source=None):
    source=source or tmp_path/'accounts.db'
    users=Users(source,{'owner':read_group('owner',{'can':['superuser']}),
                        'admin':Group(can=frozenset({'manage-settings'})),'plain':Group()})
    for name in ('owner','admin','plain'):users.add(name,name)
    headers={name:{'authorization':'Bearer '+users.new_key(name)} for name in ('owner','admin','plain')}
    config=Config(servers=servers or {},database=source,machine_key_file=str(tmp_path/'machine.key'))
    bindings=({'stores':{'accounts':{'engine':'postgresql','dsn_file':source.store.dsn_file}},
               'bindings':{'weightroom.accounts':'accounts'}} if getattr(source,'engine','')=='postgresql' else {})
    caller=front_of(lambda _:build_app(config),users,tmp_path,port=8443,databases=bindings,
                    keep_console_settings_on_front=True,toollocker_url='http://front-tools:9443')
    caller.test_headers=headers
    return caller


def save(caller,name='far',**values):
    body={'operation':'save','name':name,'server':{'url':'https://example.test/mcp','public':True},**values}
    response=caller.post('/remote-servers',headers=caller.test_headers['owner'],json=body)
    assert response.status_code==200,response.text
    return response


def refresh(caller):
    caller.portal.call(caller.compute.state.refresh_settings)


@pytest.mark.parametrize('wrong',[
    {'bad_name':{'url':'https://example.test/mcp','public':True}},
    {'a':{'command':['sh']}}, {'a':{'url':'file:///tmp/x'}},
    {'a':{'url':'https://user:secret@example.test/mcp','public':True}},
    {'a':{'url':'https://example.test/mcp?token=private','public':True}},
    {'a':{'url':'https://example.test/mcp#private','public':True}},
    {'a':{'url':'http://example.test/mcp','public':True}},
    {'a':{'url':'https://example.test/mcp','key_file':'owner.key'}},
    {'a':{'url':'https://example.test/mcp','level':'secret'}},
    {'a':{'url':'https://example.test/mcp','public':'yes'}},
    {'a':{'url':'https://example.test/mcp','description':'x'*2001}},
    {str(i):{'url':'https://example.test/mcp','public':True} for i in range(33)},
])
def test_remote_contracts_reject_commands_paths_embedded_secrets_and_invalid_bounds(wrong):
    with pytest.raises(ValueError):validate('toollocker','remote_servers',wrong)


def test_keys_are_atomic_private_blank_preserving_and_metadata_only(tmp_path):
    keys=RemoteKeys(tmp_path/'keys');keys.save('far','fake-private-first')
    assert keys.path('far').stat().st_mode & 0o777 == 0o600
    revision=keys.revision('far');keys.save('far','');assert keys.read('far')=='fake-private-first'
    keys.save('far','fake-private-next');assert keys.revision('far')!=revision
    assert 'fake-private' not in json.dumps(keys.status('far'))
    for bad in ('bad key','bad\nkey','x'*8193,True):
        with pytest.raises(ValueError):keys.save('far',bad)
    assert keys.read('far')=='fake-private-next'
    keys.remove('far');assert not keys.status('far')['saved']


def test_signed_endpoint_and_generic_settings_require_superuser(tmp_path):
    with gateway(tmp_path) as caller:
        for name in ('plain','admin'):
            assert caller.get('/remote-servers',headers=caller.test_headers[name]).status_code==403
            assert caller.post('/remote-servers',headers=caller.test_headers[name],json={}).status_code==403
            for params in ({},{'validate_only':'1'}):
                answer=caller.post('/loadcoach/settings/toollocker',headers=caller.test_headers[name],params=params,json={'remote_servers':{}})
                assert answer.status_code==403
        assert caller.post('/loadcoach/settings/toollocker',headers=caller.test_headers['owner'],json={'remote_servers':{}}).status_code==200
        bad=caller.post('/remote-servers',headers=caller.test_headers['owner'],json={
            'operation':'save','name':'far','server':{'url':'http://127.0.0.1:9/mcp'}})
        assert bad.status_code==400 and 'private server key' in bad.text
        assert caller.get('/remote-servers',headers=caller.test_headers['owner']).json()['servers']==[]
        # Direct requests without the signed control-node identity cannot use the editor.
        async def direct():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=caller.compute),base_url='http://tools') as http:
                return await http.post('/remote-servers',json={})
        assert caller.portal.call(direct).status_code==403


def test_live_private_add_rotate_level_description_remove_and_restart_preserve_keys(tmp_path):
    port=free_port();far=far_files(tmp_path,port,key='fake-remote-first')
    try:
        caller=gateway(tmp_path)
        with caller:
            private={'url':f'http://127.0.0.1:{port}/mcp','description':'Private files','level':'sensitive'}
            response=save(caller,server=private,credential='fake-remote-first');refresh(caller)
            assert 'fake-remote-first' not in response.text
            assert ask(caller,'tools/list',headers=caller.test_headers['plain'])['tools']==[]
            tools=ask(caller,'tools/list',headers=caller.test_headers['owner'])['tools']
            assert {tool['name'] for tool in tools}=={'far__list_folder','far__read_file'}
            assert all('Private files' in tool['description'] for tool in tools)
            path=str(tmp_path/'shared/note.txt')
            assert 'from afar' in said(ask(caller,'tools/call',{'name':'far__read_file','arguments':{'file':path}},caller.test_headers['owner']))
            remote=caller.compute.state.remote_servers['far']
            # The test receiver loads its own credential at startup; rotate that receiver first.
            far.kill();far.wait();far=far_files(tmp_path,port,key='fake-remote-next')
            save(caller,server={**private,'level':'normal','description':'Changed files'},credential='fake-remote-next');refresh(caller)
            assert caller.compute.state.remote_servers['far'] is not remote
            assert ask(caller,'tools/list',headers=caller.test_headers['plain'])['tools']
            assert 'from afar' in said(ask(caller,'tools/call',{'name':'far__read_file','arguments':{'file':path}},caller.test_headers['plain']))
            report=caller.get('/loadcoach/settings',headers=caller.test_headers['owner'])
            assert 'fake-remote-' not in report.text and 'remote_server_key' not in report.text
            records=caller.compute.state.remote_settings.log.read(kind='settings',with_text=True)
            assert 'fake-remote-' not in json.dumps(records)
            unchanged=caller.compute.state.remote_settings.keys.path('far').read_bytes()
            save(caller,server={**private,'level':'normal'},credential='');refresh(caller)
            assert caller.compute.state.remote_settings.keys.path('far').read_bytes()==unchanged
            reply=caller.post('/remote-servers',headers=caller.test_headers['owner'],json={'operation':'remove-key','name':'far'})
            assert reply.status_code==200;refresh(caller)
            assert not ask(caller,'tools/list',headers=caller.test_headers['owner'])['tools']
            caller.post('/remote-servers',headers=caller.test_headers['owner'],json={'operation':'save-key','name':'far','credential':'fake-remote-next'})
            refresh(caller)
            assert ask(caller,'tools/list',headers=caller.test_headers['owner'])['tools']
        # Restart the same installed service configuration, not a new identity or setting store.
        users=Users(tmp_path/'accounts.db',{})
        config=Config(servers={},database=tmp_path/'accounts.db',machine_key_file=str(tmp_path/'machine.key'))
        with front_of(lambda _:build_app(config),users,tmp_path,port=8443,keep_console_settings_on_front=True,toollocker_url='http://front-tools:9443') as restarted:
            assert ask(restarted,'tools/list',headers=caller.test_headers['owner'])['tools']
            response=restarted.post('/remote-servers',headers=caller.test_headers['owner'],json={'operation':'remove','name':'far'})
            assert response.status_code==200
            restarted.portal.call(restarted.compute.state.refresh_settings)
            assert not ask(restarted,'tools/list',headers=caller.test_headers['owner'])['tools']
            assert restarted.compute.state.remote_settings.keys.read('far')=='fake-remote-next'
    finally:far.kill();far.wait()


def test_machine_file_wins_without_rewriting_its_configuration_or_keys(tmp_path):
    owner_file=tmp_path/'owner.key';owner_file.write_text('unchanged-owner-key')
    servers={'fixed':{'command':FAKE,'description':'File-defined'}}
    with gateway(tmp_path,servers) as caller:
        response=save(caller,name='fixed',server={'url':'https://unreachable.example.test/mcp','public':True});refresh(caller)
        assert response.json()['servers'][0]['shadowed']
        assert caller.compute.state.remote_servers=={}
        listed=ask(caller,'tools/list',headers=caller.test_headers['owner'])['tools']
        assert any(tool['name']=='fixed__pid' and 'File-defined' in tool['description'] for tool in listed)
        assert owner_file.read_text()=='unchanged-owner-key'
        assert servers=={'fixed':{'command':FAKE,'description':'File-defined'}}


def test_public_remote_sends_no_credential_and_reflections_are_redacted(tmp_path,monkeypatch):
    seen=[];original=ManagedRemote
    def make(*args,**kwargs):
        remote=original(*args,**kwargs)
        def wire(request):
            seen.append(dict(request.headers));message=json.loads(request.content)
            if message['method']=='notifications/initialized':return httpx.Response(202)
            if message['method']=='tools/call' and message.get('params',{}).get('arguments',{}).get('fail'):
                return httpx.Response(200,json={'jsonrpc':'2.0','id':message['id'],'error':{'message':request.headers.get('authorization','')}})
            result={'protocolVersion':'2025-06-18'} if message['method']=='initialize' else {'tools':[{'name':'echo','inputSchema':{}}]} if message['method']=='tools/list' else {'content':[{'type':'text','text':request.headers.get('authorization','public')}]}
            return httpx.Response(200,json={'jsonrpc':'2.0','id':message['id'],'result':result})
        remote.client=httpx.AsyncClient(transport=httpx.MockTransport(wire));return remote
    monkeypatch.setattr('toollocker.door.ManagedRemote',make)
    with gateway(tmp_path) as caller:
        save(caller)
        until=time.monotonic()+4
        while not ask(caller,'tools/list',headers=caller.test_headers['owner'])['tools'] and time.monotonic()<until:time.sleep(.02)
        assert said(ask(caller,'tools/call',{'name':'far__echo'},caller.test_headers['owner']))=='public'
        assert all('authorization' not in headers for headers in seen)
        save(caller,server={'url':'http://local.test/mcp'},credential='fake-reflected-private');refresh(caller)
        result=ask(caller,'tools/call',{'name':'far__echo'},caller.test_headers['owner'])
        assert 'fake-reflected-private' not in json.dumps(result) and '(redacted)' in said(result)
        failed=ask(caller,'tools/call',{'name':'far__echo','arguments':{'fail':True}},caller.test_headers['owner'])
        assert failed['isError'] and 'fake-reflected-private' not in json.dumps(failed)
        assert 'fake-reflected-private' not in json.dumps(caller.compute.state.remote_settings.log.read(with_text=True))


def test_removing_a_remote_unpublishes_it_and_drains_the_active_call(tmp_path,monkeypatch):
    began,release=asyncio.Event(),asyncio.Event();original=ManagedRemote
    def make(*args,**kwargs):
        remote=original(*args,**kwargs)
        async def wire(request):
            message=json.loads(request.content)
            if message['method']=='notifications/initialized':return httpx.Response(202)
            if message['method']=='initialize':result={'protocolVersion':'2025-06-18'}
            elif message['method']=='tools/list':result={'tools':[{'name':'wait','inputSchema':{}}]}
            else:
                began.set();await release.wait();result={'content':[{'type':'text','text':'finished active call'}]}
            return httpx.Response(200,json={'jsonrpc':'2.0','id':message['id'],'result':result})
        remote.client=httpx.AsyncClient(transport=httpx.MockTransport(wire));return remote
    monkeypatch.setattr('toollocker.door.ManagedRemote',make)
    with gateway(tmp_path) as caller:
        save(caller);refresh(caller);old=caller.compute.state.remote_servers['far']
        async def begin():
            http=httpx.AsyncClient(transport=httpx.ASGITransport(app=caller.door),base_url='http://front:8443')
            task=asyncio.create_task(http.post('/mcp',headers=caller.test_headers['owner'],
                json={'id':2,'method':'tools/call','params':{'name':'far__wait'}}))
            await asyncio.wait_for(began.wait(),2)
            return http,task
        http,task=caller.portal.call(begin)
        response=caller.post('/remote-servers',headers=caller.test_headers['owner'],json={'operation':'remove','name':'far'})
        assert response.status_code==200;refresh(caller)
        assert not ask(caller,'tools/list',headers=caller.test_headers['owner'])['tools'] and not old.client.is_closed
        async def finish():
            release.set();answer=await asyncio.wait_for(task,2);await http.aclose()
            until=time.monotonic()+2
            while not old.client.is_closed and time.monotonic()<until:await asyncio.sleep(.02)
            return answer
        assert 'finished active call' in caller.portal.call(finish).text
        assert old.client.is_closed


@pytest.fixture
def remote_console(tmp_path):
    from weightroom.app import Settings,build_app as console_app
    caller=gateway(tmp_path)
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=caller.door),base_url='http://front:8443')
    app=console_app(Settings(data=str(tmp_path),compute='http://front',key=caller.test_headers['owner']['authorization'][7:]),client,tools_client=client)
    own=app.router.lifespan_context
    @asynccontextmanager
    async def both(app):
        async with caller.front.router.lifespan_context(caller.front),own(app):yield
    app.router.lifespan_context=both
    with TestClient(app) as console:
        console.cookies.set('weightroom',caller.test_headers['owner']['authorization'][7:])
        yield console,caller
    caller.users.db.close()


@pytest.mark.parametrize('width',[390,1440])
def test_remote_editor_native_empty_error_secret_and_offline_states(remote_console,width):
    console,caller=remote_console
    errors=[]
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,console);page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.set_viewport_size({'width':width,'height':1000});page.goto('http://console.test/tools')
            playwright.expect(page.get_by_text('No site-managed remote servers.',exact=True)).to_be_visible()
            page.locator('#remote-new-name').fill('sample');page.locator('#remote-new-url').fill('http://localhost:9/mcp')
            page.get_by_role('button',name='Add server',exact=True).click()
            playwright.expect(page.locator('.said-box')).to_contain_text('private server key')
            assert page.locator('#remote-new-key').input_value()==''
            # Native save through the same route uses a deliberately unreachable local receiver.
            response=console.post('/tools/remotes',data={'operation':'save','name':'sample','url':'http://127.0.0.1:9/mcp',
                'description':'Local receiver test','level':'normal','credential':'fake-ui-private'})
            assert response.status_code==200,response.text
            page.goto('http://console.test/tools')
            assert 'fake-ui-private' not in page.content()
            assert page.locator('#remote-sample-key').input_value()==''
            page.locator('#settings-search').fill('no-such-remote-setting')
            playwright.expect(page.locator('[data-settings-no-match]')).to_be_visible()
            page.locator('#settings-search').fill('Remote MCP')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            from starlette.responses import JSONResponse
            from starlette.routing import request_response
            endpoint=next(route for route in caller.compute.routes if route.path=='/remote-servers')
            original=endpoint.app
            async def unavailable(request):return JSONResponse({'error':{'message':'Unavailable'}},503)
            endpoint.app=request_response(unavailable)
            try:
                page.goto('http://console.test/tools')
                playwright.expect(page.get_by_text('The control node tool service is unavailable.',exact=False)).to_be_visible()
                assert page.locator('#remote-new-name').count()==0
                failed=console.post('/tools/remotes',data={'operation':'save','name':'not-queued','url':'http://127.0.0.1:9/mcp',
                    'level':'normal','credential':'fake-must-not-save'})
                assert failed.status_code==503
                assert 'not-queued' not in caller.compute.state.remote_settings.values()
                assert not caller.compute.state.remote_settings.keys.status('not-queued')['saved']
                assert 'fake-must-not-save' not in failed.text
            finally:endpoint.app=original
            assert not errors
        finally:browser.close()



def test_postgresql_remote_metadata_and_secret_isolation(postgres,tmp_path):
    with gateway(tmp_path,servers={'fixed':{'command':FAKE}},source=postgres) as caller:
        save(caller,name='fixed',credential='fake-postgresql-remote-key')
        refresh(caller)
        assert caller.compute.state.remote_settings.values()['fixed']['public']
        assert caller.compute.state.remote_settings.keys.read('fixed')=='fake-postgresql-remote-key'
        public=caller.get('/loadcoach/settings',headers=caller.test_headers['owner'])
        assert 'fake-postgresql-remote-key' not in public.text
        assert 'fake-postgresql-remote-key' not in json.dumps(caller.compute.state.remote_settings.log.read(with_text=True))
        assert caller.post('/loadcoach/settings/toollocker',headers=caller.test_headers['admin'],json={'remote_servers':None}).status_code==403
    from loadcoach.settings import Changes
    saved=Changes(postgres)
    assert saved.of('toollocker')['remote_servers']['fixed']['public']
    assert 'remote_server_key' not in saved.of('toollocker')
    saved.db.close()



def test_ordinary_manager_has_no_remote_editor_or_write_access(remote_console):
    console,caller=remote_console
    console.cookies.set('weightroom',caller.test_headers['admin']['authorization'][7:])
    response=console.get('/tools')
    assert response.status_code==200 and 'id="remote-servers"' not in response.text
    assert console.post('/tools/remotes',data={'operation':'save'}).status_code==403
