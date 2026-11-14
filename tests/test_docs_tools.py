"""Docs tools keep the real caller, shared library permissions, bounds and citation addresses."""

import asyncio
import json
from functools import partial
from pathlib import Path

import httpx
import pytest

from front_helpers import front_of
from loadcoach import statement
from toollocker.docs import DocsTools, descriptions
from toollocker.door import Config, build_app
from test_weightroom import desk, sign_in  # noqa: F401


@pytest.fixture
def docs_gateway(desk, tmp_path, monkeypatch):
    server, _, users = desk
    key_file = str(tmp_path / 'machine.key')
    server.app.state.docs_api.key_file = key_file
    async def start(adapter):
        """Reach the real console endpoint through the isolated browser transport."""
        async def forward(request):
            """Release this event loop while the console handles the nested signed request."""
            result = await asyncio.to_thread(partial(server.get, str(request.url), headers=dict(request.headers)))
            return httpx.Response(result.status_code, content=result.content, headers=result.headers)
        adapter.client = httpx.AsyncClient(base_url=adapter.url, transport=httpx.MockTransport(forward))
    monkeypatch.setattr(DocsTools, 'start', start)
    gateway = build_app(Config(servers={}, docs_url='http://testserver', machine_key_file=key_file,
                               database=str(tmp_path / 'docs-tools.db')))
    with front_of(lambda _: gateway, users, tmp_path, port=8443,
                  toollocker_url='http://front-tools:9443', keep_console_settings_on_front=True) as caller:
        async def forward_tool(request):
            """Let console Try a tool use actual Turnstile and signed ToolLocker dispatch."""
            result = await asyncio.to_thread(partial(caller.request, request.method, request.url.raw_path.decode(),
                                                     content=request.content, headers=dict(request.headers)))
            return httpx.Response(result.status_code, content=result.content, headers=result.headers)
        # The service already owns this client; only its test wire is replaced.
        server.app.state.tool_http._transport = httpx.MockTransport(forward_tool)
        yield server, users, caller


def rpc(caller, key, method, params=None, path='/mcp/docs'):
    result = caller.post(path, headers={'authorization': 'Bearer '+key},
                         json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    assert result.status_code == 200, result.text
    return result.json()


def result_data(reply):
    value = reply['result']
    assert not value.get('isError'), value
    return json.loads(value['content'][0]['text'])


def test_tools_list_obeys_toolset_use_tools_and_patterns(docs_gateway):
    server, users, caller = docs_gateway
    admin = users.new_key('jordan')
    plain = users.new_key('pat')
    offered = rpc(caller,admin,'tools/list')['result']['tools']
    assert {one['name'] for one in offered} == {'docs__'+one['name'] for one in descriptions()}
    assert rpc(caller,plain,'tools/list')['result']['tools'] == []
    users.change('pat', own={'add':['use-tools'], 'add_tools':['docs__search'], 'remove_tools':['docs__read']})
    offered = {one['name'] for one in rpc(caller,plain,'tools/list')['result']['tools']}
    assert 'docs__search' in offered and 'docs__read' not in offered
    assert 'error' in rpc(caller,plain,'tools/call',{'name':'docs__read','arguments':{'url':'/docs/web-app/chat'}})
    assert rpc(caller,admin,'tools/list',path='/mcp')['result']['tools']


def test_public_and_admin_reads_use_the_same_library_visibility(docs_gateway):
    server, users, caller = docs_gateway
    users.change('pat', own={'add':['use-tools']})
    plain = users.new_key('pat')
    sign_in(server, 'pat')
    def call(name, arguments):
        return rpc(caller,plain,'tools/call',{'name':'docs__'+name,'arguments':arguments})
    topics = result_data(call('list_topics',{}))
    assert {one['url'] for one in topics['sections']} == {'/docs/getting-started','/docs/web-app'}
    found = result_data(call('search',{'q':'Shift+Enter'}))
    assert found['hits'] and len(found['hits']) <= 10
    assert all('#' in hit['url'] for hit in found['hits'])
    for hit in found['hits']:
        response = server.get(hit['url'].split('#')[0])
        assert response.status_code == 200 and 'id="'+hit['url'].split('#')[1]+'"' in response.text
    read = result_data(call('read',{'url':'/docs/web-app/tools'}))
    assert 'ToolLocker setup and adding tools' not in read['text']
    assert call('read',{'url':'/docs/api'})['result']['isError']
    assert call('lookup_setting',{'q':'chat_keep_days'})['result']['isError']
    assert call('code_outline',{'module':'loadcoach/users.py'})['result']['isError']


def test_every_tool_works_through_settings_try_a_tool(docs_gateway):
    server, _, _ = docs_gateway
    sign_in(server,'jordan')
    assert 'Try a tool' in server.get('/tools').text
    examples = {
        'list_topics':{}, 'search':{'q':'chat'}, 'read':{'url':'/docs/web-app/chat','anchor':'chat-options'},
        'find_endpoint':{'q':'/v1/responses'}, 'lookup_setting':{'q':'chat_keep_days'},
        'explain_ability':{'q':'use-tools'}, 'code_outline':{'module':'loadcoach/users.py'},
        'code_symbol':{'module':'loadcoach/users.py','symbol':'Users.sign_in'},
    }
    for name, arguments in examples.items():
        response = server.post('/tools/message',json={'toolset':'docs','message':{
            'id':1,'method':'tools/call','params':{'name':'docs__'+name,'arguments':arguments}}})
        assert response.status_code == 200, (name,response.text)
        data = result_data(response.json())
        assert data
        if 'text' in data:
            assert len(data['text']) <= 8000
            page, _, anchor = data['url'].partition('#')
            resolved = server.get(page)
            assert resolved.status_code == 200
            assert not anchor or 'id="'+anchor+'"' in resolved.text


def test_proof_replay_forgery_bounds_and_path_rejection(docs_gateway):
    server, users, caller = docs_gateway
    sign_in(server,'jordan')
    path='/owg/docs/search?q=chat'
    key=statement.read_key(server.app.state.docs_api.key_file)
    claimed=statement.caller_of(users.by_name('jordan'))
    header,nonce=statement.make(key,'GET',path,b'',caller=claimed,purpose='docs-tool')
    accepted=server.get(path,headers={statement.HEADER:header})
    assert accepted.status_code==200 and accepted.headers[statement.PROOF]==statement.proof(key,nonce)
    assert server.get(path,headers={statement.HEADER:header}).status_code==401
    assert server.get(path,headers={statement.HEADER:'forged'}).status_code==401
    server.cookies.clear()
    assert server.get(path).status_code==401
    admin=users.new_key('jordan')
    for arguments in ({'url':'../../settings/weightroom.toml'}, {'url':'https://example.com/docs'},
                      {'url':'/docs/web-app/chat','offset':2000001}):
        assert rpc(caller,admin,'tools/call',{'name':'docs__read','arguments':arguments})['result']['isError']
    assert rpc(caller,admin,'tools/call',{'name':'docs__code_outline','arguments':{'module':'../keys/machine.key'}})['result']['isError']
    read=result_data(rpc(caller,admin,'tools/call',{'name':'docs__read','arguments':{'url':'/docs/api/custom'}}))
    assert len(read['text'])==8000 and read['next_offset']==8000
    following=result_data(rpc(caller,admin,'tools/call',{'name':'docs__read','arguments':{'url':'/docs/api/custom','offset':read['next_offset']}}))
    assert following['offset']==8000 and following['text']!=read['text']


def test_catalogue_pages_are_bounded_complete_and_scope_preserving(docs_gateway):
    from weightroom import system_docs
    _, users, caller = docs_gateway
    admin = users.new_key('jordan')
    offset, found = 0, set()
    while True:
        page = result_data(rpc(caller, admin, 'tools/call', {'name': 'docs__list_topics', 'arguments': {'offset': offset}}))
        assert len(json.dumps(page)) < 64000
        for section in page['sections']:
            found.add(section['url'])
            found.update(topic['url'] for topic in section['topics'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert found == {item['url'] for section in system_docs.visible({'manage-settings'}) for item in (section, *section['topics'])}
