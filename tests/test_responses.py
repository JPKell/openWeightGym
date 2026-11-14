"""Responses protocol parity, real recorded streams and upstream lifetime checks."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from turnstile.responses import chat_request, response_body, routes


def frame(delta=None, finish=None, **fields):
    return 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}], **fields}) + '\n\n'


def events(text):
    return [json.loads(block.split('data: ', 1)[1]) for block in text.strip().split('\n\n')]


class Source(httpx.AsyncByteStream):
    def __init__(self, parts, fail=False):
        self.parts, self.fail, self.closed = parts, fail, False

    async def __aiter__(self):
        for part in self.parts:
            yield part.encode()
        if self.fail:
            raise httpx.ReadError('connection lost')

    async def aclose(self):
        self.closed = True


@asynccontextmanager
async def response_app(source, *, status=200):
    calls = []

    async def upstream(request):
        calls.append((json.loads(request.content), dict(request.headers)))
        return httpx.Response(status, stream=source, headers={'x-loadcoach-request-id': '42', 'x-suite-node': 'test'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url='http://gateway') as door:
        app = Starlette(routes=routes(lambda key: SimpleNamespace(name='alice', identity='alice', group=SimpleNamespace(max_sensitivity='restricted')) if key == 'key' else None,
                                     SimpleNamespace(door=door)))
        yield app, calls


def request_stream(source, status=200):
    async def run():
        async with response_app(source, status=status) as (app, calls):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                answer = await client.post('/v1/responses', json={'model': 'test', 'input': 'Hi', 'store': False, 'stream': True},
                                           headers={'authorization': 'Bearer key', 'x-loadcoach-trace': 'trace'})
                return answer, calls
    return asyncio.run(run())


def test_text_stream_sequence_identity_final_usage_and_headers():
    source = Source([frame({'role': 'assistant'}), frame({'content': 'Hel'}), frame({'content': 'lo'}),
                     frame(finish='stop'), 'data: ' + json.dumps({'choices': [], 'usage': {
                         'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}) + '\n\n', 'data: [DONE]\n\n'])
    answer, calls = request_stream(source)
    data = events(answer.text)
    assert [e['type'] for e in data] == ['response.created', 'response.in_progress', 'response.output_item.added',
        'response.content_part.added', 'response.output_text.delta', 'response.output_text.delta',
        'response.output_text.done', 'response.content_part.done', 'response.output_item.done', 'response.completed']
    assert [e['sequence_number'] for e in data] == list(range(len(data)))
    assert data[0]['response']['output'] == []
    assert data[2]['item']['content'] == []
    result = data[-1]['response']
    assert result['id'] == data[0]['response']['id']
    assert result['output'][0]['id'] == data[2]['item']['id']
    assert result['output'][0]['content'][0]['text'] == 'Hello'
    assert result['usage']['total_tokens'] == 5
    assert calls[0][0]['stream_options'] == {'include_usage': True}
    assert calls[0][1]['x-loadcoach-trace'] == 'trace'
    assert answer.headers['x-loadcoach-request-id'] == '42'
    assert answer.headers['x-suite-node'] == 'test'
    assert source.closed


@pytest.mark.parametrize('delta,event', [({'reasoning_content': 'Think'}, 'response.reasoning_summary_text.delta'),
                                        ({'refusal': 'No'}, 'response.refusal.delta')])
def test_reasoning_and_refusal_stream_and_nonstream_parity(delta, event):
    usage = {'completion_tokens': 8, 'completion_tokens_details': {'reasoning_tokens': 6}}
    answer, _ = request_stream(Source([frame(delta), frame({'content': 'Answer'}) if 'reasoning_content' in delta else '',
                                      frame(finish='stop', usage=usage), 'data: [DONE]\n\n']))
    data = events(answer.text)
    assert event in [e['type'] for e in data]
    result = data[-1]['response']
    message = {**delta, **({'content': 'Answer'} if 'reasoning_content' in delta else {})}
    ordinary = response_body({'choices': [{'message': message, 'finish_reason': 'stop'}], 'usage': usage}, {'model': 'test'})
    for item in result['output'] + ordinary['output']:
        item.pop('id')
    assert result['output'] == ordinary['output']
    assert result['usage']['output_tokens_details']['reasoning_tokens'] == 6


def test_length_and_error_boundaries():
    answer, _ = request_stream(Source([frame({'content': 'partial'}), frame(finish='length')]))
    result = events(answer.text)[-1]
    assert result['type'] == 'response.incomplete'
    assert result['response']['incomplete_details'] == {'reason': 'max_output_tokens'}
    source = Source(['{"error":{"message":"busy","type":"overloaded","code":"busy"}}'])
    answer, _ = request_stream(source, status=429)
    assert answer.status_code == 429 and answer.json()['error']['code'] == 'busy' and source.closed
    for source, code in [(Source([frame({'content': 'partial'})], fail=True), 'upstream_disconnected'),
                         (Source([frame({'tool_calls': [{'id': 'call'}]})]), 'invalid_upstream_response'),
                         (Source([frame({'content': 'partial'}), 'data: bad\n\n']), 'invalid_upstream_response'),
                         (Source([frame({'content': 'partial'})]), 'upstream_disconnected')]:
        answer, _ = request_stream(source)
        data = events(answer.text)
        assert [e['type'] for e in data][-2:] == ['error', 'response.failed']
        assert data[-1]['response']['error']['code'] == code
        assert source.closed
    source = Source([], fail=True)
    answer, _ = request_stream(source)
    assert answer.status_code == 503 and answer.json()['error']['code'] == 'gateway_unavailable'
    assert source.closed


def test_client_disconnect_closes_upstream():
    async def run():
        source = Source([frame({'content': 'Hello'}), frame(finish='stop')])
        async with response_app(source) as (app, _):
            sent = False
            received = asyncio.Event()

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': json.dumps({'model': 'test', 'input': 'Hi', 'stream': True, 'store': False}).encode()}
                await received.wait()
                return {'type': 'http.disconnect'}

            async def send(message):
                if message['type'] == 'http.response.body':
                    received.set()
                    await asyncio.sleep(1)

            await app({'type': 'http', 'asgi': {'spec_version': '2.0'}, 'method': 'POST', 'path': '/v1/responses',
                       'headers': [(b'authorization', b'Bearer key')], 'query_string': b''}, receive, send)
            assert source.closed
    asyncio.run(run())


def test_recorded_qwen_reasoning_stream():
    raw = (Path(__file__).parent / 'fixtures/responses/qwen3.6-35b-a3b-q4.sse').read_text()
    answer, _ = request_stream(Source([raw]))
    data = events(answer.text)
    assert [e['sequence_number'] for e in data] == list(range(len(data)))
    kinds = [e['type'] for e in data]
    deltas = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ') and line[6:] != '[DONE]']
    reasoning = ''.join(ch['delta'].get('reasoning_content') or '' for c in deltas for ch in c.get('choices', []))
    text = ''.join(ch['delta'].get('content') or '' for c in deltas for ch in c.get('choices', []))
    result = data[-1]['response']
    assert result['output'][0]['summary'][0]['text'] == reasoning and reasoning
    assert result['output'][1]['content'][0]['text'] == text
    reasoning_done = kinds.index('response.reasoning_summary_part.done')
    assert kinds[reasoning_done + 1:reasoning_done + 4] == [
        'response.output_item.done', 'response.output_item.added', 'response.content_part.added']
    usage = next(c['usage'] for c in reversed(deltas) if c.get('usage'))
    assert result['usage']['input_tokens'] == usage['prompt_tokens']
    assert result['usage']['output_tokens'] == usage['completion_tokens']


def test_official_sdk_parses_stream_when_installed():
    openai = pytest.importorskip('openai', reason='Official openai SDK is not installed in the test venv.')

    async def run():
        async with response_app(Source([frame({'content': 'Hi'}), frame(finish='stop')])) as (app, _):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
                client = openai.AsyncOpenAI(api_key='key', base_url='http://test/v1', http_client=http)
                stream = await client.responses.create(model='test', input='Hi', stream=True, store=False)
                data = [event async for event in stream]
                assert data[-1].response.output[0].content[0].text == 'Hi'
    asyncio.run(run())
