"""Regression contracts for failures reproduced by the live console audit."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from test_weightroom import desk, sign_in  # noqa: F401


def test_proxy_origin_checks_accept_external_https_and_reject_foreign_mutations(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    for path, method in [('/chat/classifier/api/run', 'POST'),
                         ('/chat/extraction/api/assets', 'POST'),
                         ('/chat/extraction/api/presets/exp_' + '0' * 32, 'DELETE')]:
        assert browser.request(method, path, headers={'origin': 'https://evil.test'}, json={}).status_code == 403
        # The ASGI app sees HTTP behind the console's HTTPS reverse proxy.
        assert browser.request(method, path, headers={'origin': 'https://testserver'}, json={}).status_code != 403


def test_usage_preserves_node_and_level_and_export_stays_relative(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    response = browser.get('/usage?complete=1&node=compute&level=normal')
    import re
    for name, value in [('node', 'compute'), ('level', 'normal')]:
        select = re.search(r'<select[^>]*name="' + name + r'".*?</select>', response.text, re.S)[0]
        assert any('selected' in option and 'value="' + value + '"' in option for option in re.findall(r'<option[^>]*>', select))
    page = browser.get('/requests?complete=1&user=jordan').text
    href = re.search(r'href="([^"]+)"[^>]*>Export page CSV', page)[1]
    assert href.startswith('/requests?') and 'user=jordan' in href



def test_unknown_event_group_keeps_deleted_users_and_empty_groups_valid(desk):
    from test_account_settings import api
    browser, switch, _ = desk
    sign_in(browser, 'jordan')
    switch.front.state.log.add('qa', 'past', 'removed-owner')
    key = browser.cookies['weightroom']
    response = api(browser, switch, key, 'GET', '/loadcoach/events?group=(unknown)')
    assert response.status_code == 200
    assert {e['user'] for e in response.json()['events']} == {'removed-owner'}
    assert api(browser, switch, key, 'GET', '/loadcoach/events?group=missing').json()['events'] == []


def test_chat_picker_excludes_reported_non_chat_capabilities():
    from weightroom.account_views import chat_models
    person = SimpleNamespace(me={'models': ['chat', 'embed', 'video', 'old-embed'],
        'chat': {'models': {'chat': {'chat': True}, 'embed': {'chat': False},
                           'video': {'video_generation': True}, 'old-embed': {'embedding': True}}}})
    assert chat_models(person) == ['chat']


def test_no_log_generation_requires_live_policy_and_account_permission():
    from turnstile.batches import FrontBatches
    user = SimpleNamespace(can=lambda ability: True)
    replies = [None, httpx.Response(503), httpx.Response(200, json={'chat': {'unlogged': False}}),
               httpx.Response(200, json={'chat': {'unlogged': True}})]
    async def run():
        async def ask(*args):
            return replies.pop(0)
        batches = FrontBatches(None, None, ask, None, None)
        assert [await batches.can_unlogged(user) for _ in range(4)] == [False, False, False, True]
        user.can = lambda ability: False
        assert not await batches.can_unlogged(user)
    asyncio.run(run())


def test_tool_chat_read_reloads_after_permission_await():
    from weightroom.tool_chat import ToolChat
    original = {'id': 1, 'kind': 'tools', 'level': 'normal', 'messages': []}
    chats = SimpleNamespace(get=lambda number, owner: dict(original), touch=lambda *args: True)
    chat = ToolChat(chats, {'1': object()}, None, None, None, None)
    async def allowed(*args):
        original['messages'] = [{'role': 'assistant', 'content': 'Proposal ready'}]
        chat.turns.clear()
    chat.allowed = allowed
    request = Request({'type': 'http', 'path_params': {'ref': '1'}, 'query_string': b'', 'headers': []})
    import json
    result = json.loads(asyncio.run(chat.one(request, SimpleNamespace(name='owner', info={}))).body)
    assert result['pending'] is False and result['messages'][0]['content'] == 'Proposal ready'


@pytest.mark.parametrize('reply, explanation', [
    ({'choices': [{'finish_reason': 'length', 'message': {'role': 'assistant', 'content': ''}}]}, 'output limit'),
    ({'choices': [{'message': {'role': 'assistant', 'content': '', 'reasoning_content': 'private reasoning'}}]}, 'reasoning without a final answer'),
    ({'choices': [None]}, 'No answer was accepted'),
    ({'choices': {'unexpected': 'shape'}}, 'No answer was accepted'),
])
def test_cited_answer_refusals_explain_observed_limits_without_echoing_reasoning(reply, explanation):
    from weightroom.knowledge import cited_answer, Problem
    with pytest.raises(Problem, match=explanation) as caught:
        cited_answer(reply, [])
    assert 'private reasoning' not in str(caught.value)


def test_database_headroom_permission_failure_is_safe_and_actionable(monkeypatch):
    from loadcoach import database_operations as operations
    def denied(*args):
        raise PermissionError('secret database path')
    monkeypatch.setattr(operations, '_storage_headroom', denied)
    with pytest.raises(operations.OperationRefused, match='filesystem permissions') as caught:
        operations.storage_headroom(None, 100)
    assert 'secret database path' not in str(caught.value)
