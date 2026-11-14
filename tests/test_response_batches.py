"""Mixed chat/Responses batches use the same adapter and retain per-line failures."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from loadcoach.batches import Batches, lines_of
from loadcoach.groups import Group
from loadcoach.log import Log
from loadcoach.users import Users
from turnstile.batches import FrontBatches
from turnstile.responses import routes
from weightroom.response_store import Responses


def test_mixed_batch_uses_responses_adapter_and_per_line_errors(tmp_path):
    async def run():
        source = str(tmp_path/'accounts.db')
        users = Users(source, {'members': Group()})
        users.add('alice', 'members')
        key = users.new_key('alice')
        store, log, responses = Batches(source), Log(source), Responses(source)
        calls = []

        async def chat(request):
            body = await request.json()
            calls.append((body, dict(request.headers)))
            assert 'messages' in body and 'input' not in body
            return JSONResponse({'choices': [{'message': {'role': 'assistant', 'content': 'Hello'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}, headers={'x-loadcoach-request-id': '42'})

        service = SimpleNamespace(responses=responses)
        app = Starlette(routes=[Route('/v1/chat/completions', chat, methods=['POST']), *routes(users.by_key, service)])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://front') as door:
            service.door = door
            front = FrontBatches(store, log, None, door, lambda name: key)
            raw = [{'custom_id': 'chat', 'url': '/v1/chat/completions', 'body': {'model': 'test', 'messages': [{'role': 'user', 'content': 'chat'}]}},
                   {'custom_id': 'response', 'url': '/v1/responses', 'body': {'model': 'test', 'input': 'response'}},
                   *[{'custom_id': 'bad'+str(index), 'url': '/v1/responses', 'body': {'model': 'test', 'input': 'bad', **extra}}
                     for index, extra in enumerate([{'store': True}, {'stream': True}, {'stream': False}, {'previous_response_id': 'resp_foreign'}])]]
            content = '\n'.join(json.dumps(line) for line in raw).encode()
            file = store.put_file('alice', 'mixed.jsonl', 'batch', content)
            lines = lines_of(content, '/v1/chat/completions')
            batch = store.start('alice', '/v1/chat/completions', file['id'], lines)
            await front.work_through(batch['id'], users.by_name)
            result = store.get(batch['id'])
            assert result['status'] == 'completed' and result['request_counts'] == {'total': 6, 'completed': 2, 'failed': 4}
            good = [json.loads(row) for row in store.content(result['output_file_id']).decode().splitlines()]
            bad = [json.loads(row) for row in store.content(result['error_file_id']).decode().splitlines()]
            assert good[0]['response']['body']['choices'][0]['message']['content'] == 'Hello'
            response = good[1]['response']['body']
            assert response['object'] == 'response' and response['output'][0]['content'][0]['text'] == 'Hello'
            assert response['store'] is False and good[1]['response']['request_id'] == '42'
            assert all(line['response']['status_code'] == 400 and line['error']['code'] == 'unsupported_parameter' for line in bad)
            assert [body['messages'][0]['content'] for body, _ in calls] == ['chat', 'response']
            assert all(headers['x-loadcoach-trace'] == batch['id'] and headers['x-loadcoach-priority'] == 'low' for _, headers in calls)
            assert responses.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] == 0
        for kept in (users, store, log, responses):
            kept.db.close()
    asyncio.run(run())


def test_responses_batch_endpoint_survives_store_reopen(tmp_path):
    source = str(tmp_path/'batch.db')
    content = b'{"custom_id":"chat","url":"/v1/chat/completions","body":{"model":"test","messages":[]}}'
    store = Batches(source)
    file = store.put_file('alice', 'in.jsonl', 'batch', content)
    batch = store.start('alice', '/v1/responses', file['id'], lines_of(content, '/v1/responses'))
    store.db.close()
    store = Batches(source)
    assert store.next_line(batch['id'])['endpoint'] == '/v1/chat/completions'
    store.db.close()


@pytest.mark.parametrize('content', [b'not JSON', b'[]', b'null', b'{"url":"/v1/embeddings","custom_id":"x","body":{"model":"test"}}'])
def test_unrelated_or_invalid_lines_still_refused(content):
    with pytest.raises(ValueError):
        lines_of(content, '/v1/responses')
