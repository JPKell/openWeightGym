"""Stored Responses lifecycle, ownership, clearance, chaining and console contracts."""

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.responses import routes
from weightroom.response_store import Responses
from test_responses import Source, events, frame


@pytest.fixture
def stored(tmp_path):
    source = str(tmp_path/'accounts.db')
    users = Users(source, {'owners': Group(max_sensitivity='restricted'), 'members': Group(),
        'readers': Group(can=frozenset({'see-log'}), max_sensitivity='sensitive'),
        'managers': Group(can=frozenset({'manage-users'}), max_sensitivity='sensitive')})
    for name, group in [('alice', 'owners'), ('bob', 'members'), ('reader', 'readers'), ('manager', 'managers')]:
        users.add(name, group)
    keys = {name: users.new_key(name) for name in ('alice', 'bob', 'reader', 'manager')}
    calls = []

    async def model(request):
        if request.headers.get('x-loadcoach-log') == 'off':
            assert 'x-loadcoach-sensitivity' not in request.headers
        body = json.loads(request.content)
        calls.append((body, dict(request.headers)))
        if body['model'] == 'bad':
            return httpx.Response(500, json={'error': {'message': 'bad', 'code': 'model_failed'}})
        if body['stream']:
            parts = [frame({'content': 'Answer'}), frame(finish='length' if body['model'] == 'length' else 'stop',
                usage={'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6})]
            return httpx.Response(200, stream=Source(parts, fail=body['model'] == 'disconnect'), headers={'x-loadcoach-request-id': '123'})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Answer'},
            'finish_reason': 'length' if body['model'] == 'length' else 'stop'}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}, headers={'x-loadcoach-request-id': '123'})

    store = Responses(source)
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(model), base_url='http://gateway') as door:
            service.door = door
            yield
        store.db.close()
        users.db.close()

    service = SimpleNamespace(responses=store)
    app = Starlette(routes=routes(users.by_key, service), lifespan=lifespan)
    with TestClient(app, headers={'authorization': 'Bearer '+keys['alice']}) as client:
        yield client, store, users, keys, calls


def create(client, **body):
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Hello', **body})
    assert answer.status_code == 200, answer.text
    return answer.json()


def as_user(keys, name):
    return {'authorization': 'Bearer '+keys[name]}


def test_create_default_get_input_paging_delete(stored):
    client, store, users, _, _ = stored
    response = create(client, input=[{'role': 'user', 'content': 'one'}, {'role': 'user', 'content': 'two'}])
    assert response['store'] is True
    path = '/v1/responses/'+response['id']
    assert client.get(path).json() == response
    page = client.get(path+'/input_items?limit=1').json()
    assert page['data'][0]['content'] == 'two' and page['has_more']
    assert page['first_id'] == page['last_id'] == page['data'][0]['id']
    next_ = client.get(path+'/input_items', params={'limit': 1, 'after': page['last_id']}).json()
    assert next_['data'][0]['content'] == 'one' and not next_['has_more']
    assert client.get(path+'/input_items?order=asc').json()['data'][0]['id'] == next_['first_id']
    assert client.get(path+'/input_items?limit=0').status_code == 400
    assert client.get(path+'/input_items?order=bad').status_code == 400
    assert client.get(path+'/input_items?after=foreign').status_code == 400
    assert store.one(users.by_name('alice'), response['id'])['request_id'] == '123'
    assert client.delete(path).json() == {'id': response['id'], 'object': 'response.deleted', 'deleted': True}
    assert client.get(path).status_code == client.delete(path).status_code == 404


def test_visibility_and_delete_use_independent_abilities_and_clearance(stored):
    client, _, users, keys, _ = stored
    ordinary = create(client)
    sensitive = create(client, input='secret')
    secret = client.post('/v1/responses', json={'model': 'test', 'input': 'secret'}, headers={'x-loadcoach-sensitivity': 'restricted'}).json()
    path = '/v1/responses/'+ordinary['id']
    assert client.get(path, headers=as_user(keys, 'bob')).status_code == 404
    assert client.get(path, headers=as_user(keys, 'reader')).status_code == 200
    assert client.delete(path, headers=as_user(keys, 'reader')).status_code == 404
    assert client.get('/v1/responses/'+secret['id'], headers=as_user(keys, 'reader')).status_code == 404
    assert client.delete('/v1/responses/'+secret['id'], headers=as_user(keys, 'manager')).status_code == 404
    assert client.delete(path, headers=as_user(keys, 'manager')).status_code == 200
    users.change('alice', own={'max_sensitivity': 'normal'})
    assert client.get('/v1/responses/'+secret['id']).status_code == 404
    assert client.get('/v1/responses/'+sensitive['id']).status_code == 200


def test_chain_rebuilds_messages_drops_old_instructions_and_preserves_level(stored):
    client, _, _, keys, calls = stored
    first = client.post('/v1/responses', json={'model': 'test', 'input': 'first', 'instructions': 'old'},
                        headers={'x-loadcoach-sensitivity': 'sensitive'}).json()
    second = create(client, input='second', instructions='new', previous_response_id=first['id'])
    assert calls[-1][0]['messages'] == [{'role': 'system', 'content': 'new'}, {'role': 'user', 'content': 'first'},
                                      {'role': 'assistant', 'content': 'Answer'}, {'role': 'user', 'content': 'second'}]
    assert calls[-1][1]['x-loadcoach-sensitivity'] == 'sensitive'
    create(client, input='third', previous_response_id=second['id'], store=False)
    assert calls[-1][0]['messages'][0] == {'role': 'user', 'content': 'first'}
    assert calls[-1][0]['messages'][-1] == {'role': 'user', 'content': 'third'}
    other = client.post('/v1/responses', json={'model': 'test', 'input': 'continue', 'previous_response_id': first['id']},
                        headers=as_user(keys, 'reader'))
    assert other.status_code == 404


@pytest.mark.parametrize('extra', [{}, {'store': True}])
def test_log_off_requires_explicit_store_false(stored, extra):
    client, store, _, _, calls = stored
    assert client.post('/v1/responses', json={'model': 'test', 'input': 'private', **extra},
                       headers={'x-loadcoach-log': 'off'}).status_code == 400
    assert not calls
    assert client.post('/v1/responses', json={'model': 'test', 'input': 'private', 'store': False},
                       headers={'x-loadcoach-log': 'off'}).status_code == 200
    assert store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] == 0


@pytest.mark.parametrize('model,status', [('test', 'completed'), ('length', 'incomplete'), ('disconnect', 'failed')])
def test_stream_persists_final_object_before_terminal_event(stored, model, status):
    client, _, _, _, _ = stored
    response = client.post('/v1/responses', json={'model': model, 'input': 'Hello', 'stream': True})
    result = events(response.text)[-1]['response']
    assert result['status'] == status
    assert client.get('/v1/responses/'+result['id']).json() == result


def test_failed_nonstreaming_requests_are_retained(stored):
    client, store, users, _, _ = stored
    assert client.post('/v1/responses', json={'model': 'bad', 'input': 'Hi'}).status_code == 500
    records, total = store.listing(users.by_name('alice'), {})
    assert total == 1 and records[0]['status'] == 'failed'


def test_expiry_account_cleanup_and_quotas(stored, monkeypatch):
    client, store, users, _, calls = stored
    result = create(client)
    with store.db:
        store.db.execute('UPDATE responses SET expires=1')
    assert client.get('/v1/responses/'+result['id']).status_code == 404
    store.prune()
    assert store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] == 0
    monkeypatch.setattr('weightroom.response_store.MAX_RECORDS', 1)
    create(client)
    count = len(calls)
    answer = client.post('/v1/responses', json={'model': 'test', 'input': 'Hello'})
    assert answer.status_code == 409 and len(calls) == count
    store.forget(users.identity('alice'))
    assert store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] == 0


def test_account_rename_keeps_access_but_recreated_name_does_not(stored):
    client, _, users, _, _ = stored
    response = create(client)
    users.rename('alice', 'renamed')
    path = '/v1/responses/'+response['id']
    assert client.get(path).status_code == 200
    users.remove('renamed'); users.add('alice', 'owners')
    assert client.get(path, headers={'authorization': 'Bearer '+users.new_key('alice')}).status_code == 404


def test_chain_depth_is_bounded(stored):
    client, store, _, _, _ = stored
    response = create(client)
    with store.db:
        store.db.execute('UPDATE responses SET previous=id')
    assert client.post('/v1/responses', json={'model': 'test', 'input': 'again', 'previous_response_id': response['id']}).json()['error']['code'] == 'chain_limit'


def test_retention_setting_prunes_old_created_records(stored):
    client, store, _, _, _ = stored
    create(client)
    import time
    with store.db:
        store.db.execute('UPDATE responses SET created=?', (time.time()-86400*2,))
    store.keep_days = lambda: 1
    store.prune()
    assert store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] == 0


from test_weightroom import desk, sign_in  # noqa: E402, F401
from turnstile.responses import response_body  # noqa: E402


def seed_response(browser, users, owner='pat', text='<script>private & escaped</script>', **settings):
    request = {'model': 'sample', 'input': [{'id': 'msg_test', 'type': 'message', 'role': 'user', 'content': text}], **settings}
    response = response_body({'choices': [{'message': {'content': 'Answer', 'reasoning_content': 'Reasoning'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7}}, request)
    browser.app.state.responses.save(users.by_name(owner), request, response, 'normal', 42)
    return response


def test_console_scoping_filters_export_escape_and_confirmation(desk):
    browser, _, users = desk
    first = seed_response(browser, users)
    seed_response(browser, users, 'jordan', text='other input')
    sign_in(browser, 'pat')
    listing = browser.get('/responses').text
    assert first['id'] in listing and '&lt;script&gt;' in listing and 'other input' not in listing
    assert 'All accessible responses' not in listing
    assert 'No stored responses match' in browser.get('/responses?status=failed').text
    detail = browser.get('/responses/'+first['id'])
    assert detail.status_code == 200 and 'data-confirm-two' in detail.text
    assert '<script>private' not in detail.text and 'Reasoning' in detail.text
    assert '/requests/42' in detail.text
    assert browser.get('/responses/'+first['id']+'?format=json').json() == first
    sign_in(browser, 'jordan')
    assert 'other input' in browser.get('/responses').text
    assert first['id'] not in browser.get('/responses').text
    page = browser.get('/responses?scope=all&user=pat&q=escaped&limit=1').text
    assert first['id'] in page and 'other input' not in page and 'responses-user' in page
    assert first['id'] not in browser.get('/responses?scope=all&group=admin').text
    assert first['id'] in browser.get('/responses?scope=all&group=plain').text
    assert browser.post('/responses/'+first['id'], data={'do': 'delete'}).status_code == 200
    assert browser.get('/responses/'+first['id']).status_code == 404


def test_console_pagination_and_clearance_after_session_changes(desk):
    browser, _, users = desk
    for index in range(11):
        seed_response(browser, users, text='input '+str(index))
    sign_in(browser, 'pat')
    page = browser.get('/responses?limit=10').text
    assert 'Page 1 of 2' in page
    assert 'Page 2 of 2' in browser.get('/responses?limit=10&page=2').text
    response = seed_response(browser, users, 'jordan', text='sensitive')
    with browser.app.state.responses.db:
        browser.app.state.responses.db.execute('UPDATE responses SET level=? WHERE id=?', ('restricted', response['id']))
    sign_in(browser, 'jordan')
    assert browser.get('/responses/'+response['id']).status_code == 200
    users.change('jordan', own={'max_sensitivity': 'normal'})
    assert browser.get('/responses/'+response['id']).status_code == 404


def test_account_delete_cleans_responses_through_front_hook(desk):
    browser, switch, users = desk
    response = seed_response(browser, users)
    sign_in(browser, 'jordan')
    answer = browser.post('/users/pat', data={'do': 'remove'})
    assert answer.status_code == 200
    assert browser.app.state.responses.db.execute('SELECT COUNT(*) FROM responses WHERE id=?', (response['id'],)).fetchone()[0] == 0
