"""Function tools in Responses: validated schemas, exact arguments, streams and replay."""

import asyncio

import httpx
import pytest

from turnstile.responses import chat_request
from weightroom.extraction import Problem
from test_response_storage import stored, create  # noqa: F401
from test_responses import Source, events, frame, response_app, request_stream

TOOL = {'type': 'function', 'name': 'lookup', 'description': 'Find the weather', 'parameters': {
    'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city'], 'additionalProperties': False}, 'strict': True}


def permit(users):
    users.change('alice', own={'add': ['use-tools']})


def test_function_definitions_choices_and_parallel_are_translated():
    sent = chat_request({'model': 'test', 'input': 'Hi', 'tools': [TOOL],
                         'tool_choice': {'type': 'function', 'name': 'lookup'}, 'parallel_tool_calls': False})
    assert sent['tools'] == [{'type': 'function', 'function': {key: value for key, value in TOOL.items() if key != 'type'}}]
    assert sent['tool_choice'] == {'type': 'function', 'function': {'name': 'lookup'}}
    assert sent['parallel_tool_calls'] is False
    optional = {'type': 'function', 'name': 'optional', 'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}
    assert chat_request({'model': 'test', 'input': 'Hi', 'tools': [optional]})['tools'][0]['function']['parameters'] == optional['parameters']


@pytest.mark.parametrize('extra', [{'tools': [TOOL]*129}, {'tools': [TOOL, TOOL]},
    {'tools': [{**TOOL, 'name': 'invalid space'}]}, {'tools': [{**TOOL, 'name': 123}]},
    {'tools': [{**TOOL, 'parameters': {'$ref': 'https://example.com/schema'}}]},
    {'tools': [{**TOOL, 'parameters': {'type': 'object', 'properties': {}, 'required': 1}}]},
    {'tools': [{**TOOL, 'strict': 'true'}]}, {'tool_choice': 'invalid'},
    {'tool_choice': {'type': 'function', 'name': 'hidden'}}, {'parallel_tool_calls': 1}])
def test_invalid_tools_choices_and_unsafe_schemas_are_refused(extra):
    with pytest.raises(Problem):
        chat_request({'model': 'test', 'input': 'Hi', **extra})


def test_function_roundtrip_and_stored_chain(stored):
    client, store, users, _, calls = stored
    permit(users)
    first = create(client, model='functions', tools=[TOOL])
    assert first['status'] == 'completed' and len(first['output']) == 1
    call = first['output'][0]
    assert call['type'] == 'function_call' and call['call_id'] == 'call_lookup'
    assert call['arguments'] == '{"city":"Victoria"}' and call['name'] == 'lookup'
    result = {'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'sunny'}
    second = create(client, model='functions', input=[result], previous_response_id=first['id'])
    assert second['output'][0]['content'][0]['text'] == 'Weather: sunny'
    assert calls[-1][0]['messages'][-2:] == [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call_lookup', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"city":"Victoria"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_lookup', 'content': 'sunny'}]
    assert store.one(users.by_name('alice'), second['id'])['request']['input'][0]['type'] == 'function_call_output'
    third = create(client, model='functions', input=[call, result], store=False)
    assert third['status'] == 'completed'


def test_stream_function_arguments_identity_and_storage(stored):
    client, _, users, _, _ = stored
    permit(users)
    response = client.post('/v1/responses', json={'model': 'functions', 'input': 'Weather?', 'tools': [TOOL], 'stream': True})
    data = events(response.text)
    assert [event['type'] for event in data] == ['response.created', 'response.in_progress', 'response.output_item.added',
        'response.function_call_arguments.delta', 'response.function_call_arguments.delta', 'response.function_call_arguments.done',
        'response.output_item.done', 'response.completed']
    assert [event['sequence_number'] for event in data] == list(range(len(data)))
    result, item = data[-1]['response'], data[-1]['response']['output'][0]
    assert item['arguments'] == ''.join(event['delta'] for event in data if event['type'].endswith('arguments.delta'))
    assert item['id'] == data[2]['item']['id'] and item['call_id'] == 'call_lookup'
    assert result['usage']['total_tokens'] == 6
    assert client.get('/v1/responses/'+result['id']).json() == result


def test_parallel_calls_have_independent_output_indices():
    source = Source([frame({'tool_calls': [
        {'index': 0, 'id': 'call_a', 'function': {'name': 'first', 'arguments': '{'}},
        {'index': 1, 'id': 'call_b', 'function': {'name': 'second', 'arguments': '{"b":'}}]}),
        frame({'tool_calls': [{'index': 0, 'function': {'arguments': '}'}}, {'index': 1, 'function': {'arguments': '2}'}}]}),
        frame(finish='tool_calls')])
    answer, _ = request_stream(source)
    data = events(answer.text)
    assert data[-1]['type'] == 'response.completed'
    assert [item['arguments'] for item in data[-1]['response']['output']] == ['{}', '{"b":2}']
    assert [event['output_index'] for event in data if event['type'] == 'response.function_call_arguments.done'] == [0, 1]


def test_function_tools_require_permission_before_translation(stored):
    client, _, _, _, calls = stored
    answer = client.post('/v1/responses', json={'model': 'test', 'input': [
        {'type': 'function_call_output', 'call_id': 'call', 'output': 'result'}]})
    assert answer.status_code == 403 and answer.json()['error']['code'] == 'tools_not_allowed'
    assert not calls


def test_sdk_parses_function_output_when_installed():
    openai = pytest.importorskip('openai', reason='Official openai SDK is not installed in the test venv.')
    async def run():
        source = Source([frame({'tool_calls': [{'index': 0, 'id': 'call_lookup', 'function': {'name': 'lookup', 'arguments': '{}'}}]}), frame(finish='tool_calls')])
        async with response_app(source) as (app, _):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
                client = openai.AsyncOpenAI(api_key='key', base_url='http://test/v1', http_client=http)
                stream = await client.responses.create(model='test', input='Hi', stream=True, store=False)
                data = [event async for event in stream]
                assert data[-1].response.output[0].call_id == 'call_lookup'
    asyncio.run(run())
