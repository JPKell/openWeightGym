"""Caller-scoped MCP Responses, approval chains, limits, exact output and real SSE deltas."""

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from loadcoach.groups import Group, LEVELS
from loadcoach.log import Log
from loadcoach.users import Users
from turnstile.responses import routes
from turnstile.tool_loop import MODEL_RESULT_BYTES
from weightroom.response_store import Responses
from test_responses import Source, events, frame

MCP = {'type': 'mcp', 'server_label': 'toollocker'}


@pytest.fixture
def mcp(tmp_path):
    source = str(tmp_path/'accounts.db')
    users = Users(source, {'all': Group(can=frozenset({'use-tools'}), max_sensitivity='restricted'),
                          'narrow': Group(can=frozenset({'use-tools'}), tools=('fake__other',))})
    for name, group in [('alice', 'all'), ('bob', 'narrow'), ('app', 'narrow')]:
        users.add(name, group)
    keys = {name: users.new_key(name) for name in ('alice', 'bob')}
    log, store = Log(source), Responses(source)
    tool_schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}
    tools = [{'name': name, 'description': 'A test tool.', 'inputSchema': tool_schema}
             for name in ('fake__echo', 'fake__secret', 'fake__other')]
    rig = SimpleNamespace(mode='normal', model_calls=[], rpc_calls=[], executions=[], removed=False, block=False, closed=False, model_status=0,
                          max_calls=25, max_seconds=900, result={'content': [{'type': 'text', 'text': 'exact <output>'},
                          {'type': 'image', 'mimeType': 'image/png', 'data': 'YWJj'}], 'isError': False,
                          'structuredContent': {'kept': True}})

    async def wire(request):
        body = json.loads(request.content)
        if request.url.path.startswith('/v1/models/'):
            return httpx.Response(200, json={'id': request.url.path.rsplit('/', 1)[-1]})
        user = users.by_key(request.headers.get('authorization', '').removeprefix('Bearer '))
        assert user
        if request.url.path.startswith('/mcp'):
            assert request.url.host == 'front' and request.url.port == 8443
            rig.rpc_calls.append((user.name, body, dict(request.headers)))
            method = body['method']
            allowed = [tool for tool in tools if user.group.may_see_tool(tool['name']) and
                       LEVELS.index('sensitive' if tool['name'] == 'fake__secret' else 'normal') <= LEVELS.index(user.group.max_sensitivity)]
            if method == 'initialize':
                return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': {'protocolVersion': '2025-06-18'}},
                                      headers={'mcp-session-id': 'session_'+user.name})
            assert request.headers.get('mcp-session-id') == 'session_'+user.name
            if method == 'tools/list':
                result = {'tools': allowed}
            else:
                name = body['params']['name']
                if name not in [tool['name'] for tool in allowed] or rig.removed:
                    return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32602, 'message': 'Unknown tool: '+name}})
                rig.executions.append((user.name, name, body['params']['arguments']))
                level = 'sensitive' if name == 'fake__secret' else 'normal'
                log.set_trace_level(user.name, request.headers['x-loadcoach-trace'], level)
                result = rig.result
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})
        rig.model_calls.append((body, dict(request.headers)))
        if rig.model_status:
            return httpx.Response(rig.model_status, json={'error': {'message': 'busy', 'code': 'model_busy'}}, headers={'x-loadcoach-request-id': '42'})
        if rig.block:
            import asyncio
            class BlockingSource(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield frame({'content': 'partial'}).encode()
                    await asyncio.Event().wait()
                async def aclose(self):
                    rig.closed = True
            return httpx.Response(200, stream=BlockingSource(), headers={'x-loadcoach-request-id': '42'})
        definition_names = [tool['function']['name'] for tool in body.get('tools', [])]
        after_tool = body['messages'] and body['messages'][-1]['role'] == 'tool'
        if definition_names and (not after_tool or rig.mode == 'repeat'):
            name = 'fake__secret' if rig.mode == 'sensitive' else next((name for name in definition_names if name.startswith('fake__')), definition_names[0])
            if rig.mode == 'hidden':
                rig.removed = True
            calls = [{'id': 'call_'+str(len(rig.model_calls)), 'type': 'function', 'function': {'name': name, 'arguments': '{"text":"hello"}'}}]
            if rig.mode == 'mixed':
                calls.append({'id': 'client_call', 'type': 'function', 'function': {'name': 'client_fn', 'arguments': '{}'}})
            message, reason = {'content': None, 'tool_calls': calls}, 'tool_calls'
        else:
            message, reason = {'content': 'Final answer'}, 'stop'
        usage = {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}
        headers = {'x-loadcoach-request-id': str(40+len(rig.model_calls)), 'x-suite-node': 'test'}
        if body['stream']:
            if message.get('tool_calls'):
                parts = [frame({'tool_calls': [{'index': index, **call, 'function': {**call['function'], 'arguments': '{"text":'}}
                                                for index, call in enumerate(message['tool_calls'])]}),
                         frame({'tool_calls': [{'index': index, 'function': {'arguments': '"hello"}'}} for index in range(len(message['tool_calls']))]})]
            else:
                parts = [frame({'content': 'Final '}), frame({'content': 'answer'})]
            return httpx.Response(200, stream=Source([*parts, frame(finish=reason, usage=usage)]), headers=headers)
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': reason}], 'usage': usage}, headers=headers)

    service = SimpleNamespace(responses=store, caller=users.by_key, tool_url='http://front:8443/mcp',
                              tool_level=lambda user, trace: log.trace_level(user.name, trace),
                              hosted_models=lambda: {'cloud': {}}, tool_limits=lambda: (rig.max_calls, rig.max_seconds))
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire), base_url='http://front:8442') as door:
            service.door = door
            yield
        for kept in (users, store, log):
            kept.db.close()
    app = Starlette(routes=routes(users.by_key, service), lifespan=lifespan)
    with TestClient(app, base_url='http://front:8442', headers={'authorization': 'Bearer '+keys['alice']}) as client:
        yield client, service, users, keys, rig


def create(client, **body):
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Use a tool', 'tools': [MCP], **body})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_loop_exact_results_accounting_and_one_caller_trace(mcp):
    client, _, _, _, rig = mcp
    response = create(client, tools=[{**MCP, 'allowed_tools': ['fake__echo']}])
    assert response['status'] == 'completed'
    assert [item['type'] for item in response['output']] == ['mcp_list_tools', 'mcp_call', 'message']
    assert json.loads(response['output'][1]['output']) == rig.result
    assert response['output'][-1]['content'][0]['text'] == 'Final answer'
    assert response['usage']['total_tokens'] == 12
    traces = {headers['x-loadcoach-trace'] for _, _, headers in rig.rpc_calls} | {headers['x-loadcoach-trace'] for _, headers in rig.model_calls}
    assert traces == {response['id']}
    assert rig.executions == [('alice', 'fake__echo', {'text': 'hello'})]
    assert client.get('/v1/responses/'+response['id']).json() == response


def test_two_accounts_and_app_session_only_receive_their_tools(mcp):
    client, _, users, keys, rig = mcp
    alice = create(client)
    client.headers['authorization'] = 'Bearer '+keys['bob']
    bob = create(client, tools=[{**MCP, 'allowed_tools': ['fake__echo', 'fake__other', 'hidden__tool']}])
    assert {tool['name'] for tool in alice['output'][0]['tools']} == {'fake__echo', 'fake__secret', 'fake__other'}
    assert [tool['name'] for tool in bob['output'][0]['tools']] == ['fake__other']
    key = users.new_key('alice', hours=1, via='app')
    client.headers['authorization'] = 'Bearer '+key
    response = create(client)
    assert [tool['name'] for tool in response['output'][0]['tools']] == ['fake__other']


def test_model_cannot_call_a_tool_excluded_by_allowed_tools(mcp):
    client, _, _, _, rig = mcp
    rig.mode = 'sensitive'
    response = create(client, tools=[{**MCP, 'allowed_tools': ['fake__echo']}])
    assert response['output'][1]['error']['content']['isError']
    assert not rig.executions
    assert 'not offered' in rig.model_calls[-1][0]['messages'][-1]['content']


@pytest.mark.parametrize('url', ['https://foreign.example/mcp', 'http://front:9999/mcp', 'http://user:secret@front:8443/mcp', '/mcp/../other', 'http://['])
def test_foreign_server_urls_are_refused_before_any_tool_call(mcp, url):
    client, _, _, _, rig = mcp
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Hi', 'tools': [{**MCP, 'server_url': url}]})
    assert answer.status_code == 400 and answer.json()['error']['message'] == "Only this server's ToolLocker is available."
    assert not rig.rpc_calls and not rig.model_calls


@pytest.mark.parametrize('approve', [True, False])
def test_approval_continuation_without_repeating_tools_and_decline(mcp, approve):
    client, _, _, _, rig = mcp
    first = create(client, tools=[{**MCP, 'require_approval': 'always'}])
    pending = first['output'][-1]
    assert pending['type'] == 'mcp_approval_request' and first['status'] == 'completed' and not rig.executions
    answer = client.post('/v1/responses', json={'model': 'test', 'previous_response_id': first['id'], 'input': [
        {'type': 'mcp_approval_response', 'approval_request_id': pending['id'], 'approve': approve, 'reason': 'reviewed'}]})
    assert answer.status_code == 200, answer.text
    response = answer.json()
    assert response['status'] == 'completed'
    assert len(rig.executions) == (1 if approve else 0)
    assert response['output'][-1]['content'][0]['text'] == 'Final answer'
    assert 'declined' in rig.model_calls[-1][0]['messages'][-1]['content'] if not approve else True
    third = create(client, input='Continue', previous_response_id=response['id'])
    assert third['status'] == 'completed'


def test_approval_ids_are_validated_before_execution_and_need_storage(mcp):
    client, _, _, _, rig = mcp
    bad = client.post('/v1/responses', json={'model': 'test', 'input': 'Hi', 'store': False,
                                           'tools': [{**MCP, 'require_approval': 'always'}]})
    assert bad.status_code == 400 and not rig.rpc_calls
    first = create(client, tools=[{**MCP, 'require_approval': 'always'}])
    answer = client.post('/v1/responses', json={'model': 'test', 'previous_response_id': first['id'], 'input': [
        {'type': 'mcp_approval_response', 'approval_request_id': 'foreign', 'approve': True}]})
    assert answer.status_code == 404 and not rig.executions


def test_hidden_mid_run_fails_at_call_time_and_model_sees_error(mcp):
    client, _, _, _, rig = mcp
    rig.mode = 'hidden'
    response = create(client)
    call = next(item for item in response['output'] if item['type'] == 'mcp_call')
    assert call['status'] == 'failed' and not rig.executions
    assert 'Unknown tool' in rig.model_calls[-1][0]['messages'][-1]['content']
    assert response['status'] == 'completed'


def test_limits_and_sensitive_result_stop_before_second_hosted_call(mcp):
    client, service, users, _, rig = mcp
    rig.mode, rig.max_calls = 'repeat', 2
    response = create(client)
    assert response['status'] == 'incomplete' and response['incomplete_details']['reason'] == 'max_tool_calls'
    assert len(rig.executions) == 2
    rig.model_calls.clear(); rig.executions.clear()
    rig.mode, rig.max_calls = 'sensitive', 25
    response = create(client, model='cloud')
    assert response['status'] == 'failed' and response['error']['code'] == 'tool_result_too_sensitive'
    assert len(rig.model_calls) == 1 and len(rig.executions) == 1
    record = service.responses.one(users.by_name('alice'), response['id'])
    assert record['level'] == 'sensitive'


def test_model_copy_is_bounded_but_full_tool_output_is_saved(mcp):
    client, _, _, _, rig = mcp
    rig.result = {'content': [{'type': 'text', 'text': 'x'*100_000}], 'isError': False}
    response = create(client)
    call = next(item for item in response['output'] if item['type'] == 'mcp_call')
    assert json.loads(call['output']) == rig.result
    content = rig.model_calls[-1][0]['messages'][-1]['content']
    assert MODEL_RESULT_BYTES < len(content.encode()) < MODEL_RESULT_BYTES+150 and 'truncated' in content


def test_mixed_client_function_ends_response_after_server_tools(mcp):
    client, _, _, _, rig = mcp
    rig.mode = 'mixed'
    response = create(client, tools=[MCP, {'type': 'function', 'name': 'client_fn', 'parameters': {'type': 'object', 'properties': {}}}])
    assert [item['type'] for item in response['output']] == ['mcp_list_tools', 'mcp_call', 'function_call']
    assert len(rig.model_calls) == 1 and len(rig.executions) == 1


@pytest.mark.parametrize('approval', ['never', 'always'])
def test_stream_mcp_event_order_stable_item_ids_and_real_argument_deltas(mcp, approval):
    client, _, _, _, rig = mcp
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Hi', 'stream': True,
                                              'tools': [{**MCP, 'require_approval': approval}]})
    assert answer.status_code == 200, answer.text
    data = events(answer.text)
    assert [event['sequence_number'] for event in data] == list(range(len(data)))
    kinds = [event['type'] for event in data]
    assert kinds[:4] == ['response.created', 'response.in_progress', 'response.output_item.added', 'response.mcp_list_tools.in_progress']
    assert kinds[-1] == 'response.completed'
    added = {event['item']['id']: event['item']['type'] for event in data if event['type'] == 'response.output_item.added'}
    for event in data:
        if event['type'] == 'response.output_item.done':
            assert added[event['item']['id']] == event['item']['type']
    if approval == 'never':
        assert kinds.index('response.mcp_list_tools.completed') < kinds.index('response.mcp_call.in_progress') < kinds.index('response.mcp_call.completed')
        assert [event['delta'] for event in data if event['type'] == 'response.mcp_call_arguments.delta'] == ['{"text":', '"hello"}']
        assert 'response.output_text.delta' in kinds and len(rig.executions) == 1
    else:
        assert data[-1]['response']['output'][-1]['type'] == 'mcp_approval_request' and not rig.executions
    assert answer.headers['x-loadcoach-request-id']


def test_elapsed_time_limit_and_log_off_never_execute_tools(mcp):
    client, _, _, _, rig = mcp
    rig.max_seconds = 0
    response = create(client)
    assert response['status'] == 'incomplete' and response['incomplete_details'] == {'reason': 'max_tool_time'}
    assert not rig.executions and not rig.model_calls
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Hi', 'tools': [MCP], 'store': False},
                         headers={'x-loadcoach-log': 'off'})
    assert answer.status_code == 400 and not rig.executions


def test_shared_client_uses_real_signed_toollocker_and_rechecks_permission(tmp_path):
    import asyncio
    from test_toollocker import key_for, accounts, FAKE
    from toollocker.door import Config, build_app
    from front_helpers import front_of
    from turnstile.tool_loop import ToolClient
    authorization = key_for(tmp_path, 'hermes', 'plain')['authorization']
    config = Config(servers={'fake': {'command': FAKE}}, database=str(tmp_path/'users.db'), machine_key_file=str(tmp_path/'machine.key'),
                    toolsets={'echo': {'tools': ['fake__echo']}})
    with front_of(lambda _: build_app(config), accounts(tmp_path), tmp_path, port=8443,
                  toollocker_url='http://front-tools:9443', keep_console_settings_on_front=True) as server:
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.door), base_url='http://front:8443') as http:
                tools = ToolClient(http, 'http://front:8443/mcp/echo', authorization, 'response-real-tools')
                assert [tool['name'] for tool in await tools.listing()] == ['fake__echo']
                result = await tools.call('fake__echo', {'text': 'exact', 'times': 2})
                assert result['content'][0]['text'] == 'exactexact' and not result['isError']
                users = accounts(tmp_path)
                users.change('hermes', own={'remove_tools': ['fake__echo']})
                users.db.close()
                hidden = await tools.call('fake__echo', {'text': 'hidden'})
                assert hidden['isError'] and 'Unknown tool' in hidden['content'][0]['text']
        server.portal.call(run)


def test_stream_disconnect_closes_the_shared_model_run(mcp):
    import asyncio
    client, _, _, keys, rig = mcp
    rig.block = True
    async def run():
        sent, received = False, asyncio.Event()
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': json.dumps({'model': 'test', 'input': 'Hi', 'stream': True, 'tools': [MCP]}).encode()}
            await received.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.body':
                received.set()
                await asyncio.sleep(1)
        await client.app({'type': 'http', 'asgi': {'spec_version': '2.0'}, 'method': 'POST', 'path': '/v1/responses',
                          'headers': [(b'authorization', ('Bearer '+keys['alice']).encode())], 'query_string': b'',
                          'scheme': 'http', 'server': ('front', 8442)}, receive, send)
        assert rig.closed
    client.portal.call(run)


def test_time_limit_cancels_an_active_model_stream(mcp):
    client, _, _, _, rig = mcp
    rig.block, rig.max_seconds = True, .02
    response = create(client)
    assert response['status'] == 'incomplete' and response['incomplete_details']['reason'] == 'max_tool_time'
    assert rig.closed and not rig.executions


@pytest.mark.parametrize('is_error', [False, True])
def test_current_sdk_parses_mcp_items_and_stream_events_when_installed(mcp, is_error):
    openai = pytest.importorskip('openai', reason='Official openai SDK is not installed in the test venv.')
    from openai import _base_client as sdk_base
    sdk_httpx = getattr(sdk_base, 'httpx2', httpx)
    client, _, _, keys, rig = mcp
    rig.result = {**rig.result, 'isError': is_error}
    async def run():
        async with sdk_httpx.AsyncClient(transport=sdk_httpx.ASGITransport(client.app)) as http:
            sdk = openai.AsyncOpenAI(api_key=keys['alice'], base_url='http://front:8442/v1', http_client=http)
            stream = await sdk.responses.create(model='test', input='Hi', tools=[MCP], stream=True)
            data = [event async for event in stream]
            result = data[-1].response
            assert result.status == 'completed' and result.output[0].type == 'mcp_list_tools'
            call = next(item for item in result.output if item.type == 'mcp_call')
            if is_error:
                assert call.error.type == 'mcp_tool_execution_error'
                assert call.error.content == rig.result
            else:
                assert json.loads(call.output) == rig.result
    client.portal.call(run)


@pytest.mark.parametrize('stream', [False, True])
def test_error_before_first_byte_preserves_upstream_status_and_headers(mcp, stream):
    client, _, _, _, rig = mcp
    rig.model_status = 429
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Hi', 'tools': [MCP], 'stream': stream})
    assert answer.status_code == 429 and answer.json()['error']['code'] == 'model_busy'
    assert answer.headers['x-loadcoach-request-id'] == '42'


@pytest.mark.parametrize('stream', [False, True])
def test_storage_failure_closes_response_instead_of_hanging(mcp, monkeypatch, stream):
    client, service, _, _, _ = mcp
    def unavailable(*args, **kwargs):
        raise OSError('temporary fixture database failure')
    monkeypatch.setattr(service.responses, 'save', unavailable)
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Hi', 'tools': [MCP], 'stream': stream})
    if stream:
        assert events(answer.text)[-1]['response']['error']['code'] == 'storage_unavailable'
    else:
        assert answer.status_code == 503 and answer.json()['error']['code'] == 'storage_unavailable'
