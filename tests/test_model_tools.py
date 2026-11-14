"""Tool-use ability migration and every authenticated model entry point."""

import json

import pytest

from db import database, migrate
from db.schemas import GROUPS as GROUP_MIGRATIONS
from loadcoach.groups import EVERYTHING, Group, Groups, adjusted, limited
from loadcoach.request_tools import wants_tools, tools_refusal
from loadcoach.users import Users
from test_app import add_user, fake_llama_server, gateway, in_an_empty_folder  # noqa: F401
from test_batches import answers, hand_in, lines, until
from test_hosted_front import rig  # noqa: F401

TOOLS = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object', 'properties': {}}}}]


def check_group_migration(source):
    source = database(source)
    with source.connection() as connection:
        migrate(source, connection, 'loadcoach.groups', GROUP_MIGRATIONS[:1])
        with connection:
            for name, section in [('old', {'can': ['load'], 'models': ['small']}), ('empty', {}), ('system', {'can': ['follow-settings']})]:
                connection.execute('INSERT INTO groups VALUES(?,?)', (name, json.dumps(section)))
        groups = Groups(connection, {})
        assert all('use-tools' in groups.get(name).can for name in groups)
        assert groups.get('old').models == ('small',)
        groups.put('old', {'can': ['load']})
        assert 'use-tools' not in Groups(connection, {}).get('old').can


def test_existing_groups_migrate_once_and_overrides_are_preserved(tmp_path):
    source = str(tmp_path/'accounts.db')
    check_group_migration(source)
    users = Users(source, {})
    users.add('alice', 'empty')
    users.change('alice', own={'remove': ['use-tools']})
    assert not users.by_name('alice').can('use-tools')
    users.add('bob', 'old')
    users.change('bob', own={'add': ['use-tools']})
    assert users.by_name('bob').can('use-tools')
    users.db.close()


def test_app_sessions_need_both_sides_and_superusers_include_tools():
    allowed, denied = Group(can=frozenset({'use-tools'})), Group()
    assert 'use-tools' in EVERYTHING.can
    assert 'use-tools' not in adjusted(allowed, {'remove': ['use-tools']}).can
    assert 'use-tools' in adjusted(denied, {'add': ['use-tools']}).can
    assert 'use-tools' not in limited(allowed, denied).can
    assert 'use-tools' not in limited(denied, allowed).can
    assert 'use-tools' in limited(allowed, allowed).can


def test_app_session_key_rechecks_both_accounts(tmp_path):
    users = Users(str(tmp_path/'accounts.db'), {'allowed': Group(can=frozenset({'use-tools'})), 'denied': Group()})
    users.add('alice', 'allowed'); users.add('app', 'denied')
    key = users.new_key('alice', hours=1, via='app')
    assert not users.by_key(key).can('use-tools')
    users.change('app', own={'add': ['use-tools']})
    assert users.by_key(key).can('use-tools')
    users.change('alice', own={'remove': ['use-tools']})
    assert not users.by_key(key).can('use-tools')
    users.db.close()


@pytest.mark.parametrize('body', [{'tools': TOOLS}, {'tool_choice': 'none'},
    {'messages': [{'role': 'assistant', 'tool_calls': [{'id': 'call'}]}]}, {'messages': [{'role': 'tool', 'content': 'result'}]},
    {'input': [{'type': 'function_call_output', 'call_id': 'call', 'output': 'result'}]}, {'functions': [{'name': 'lookup'}]}])
def test_one_helper_detects_tool_definitions_choices_and_history(body):
    assert wants_tools(body)


def test_ordinary_messages_and_empty_tools_do_not_need_ability():
    assert not wants_tools({'messages': [{'role': 'user', 'content': 'tools'}], 'tools': []})


@pytest.mark.parametrize('path,extra', [('/v1/chat/completions', {'tools': TOOLS}),
    ('/v1/chat/completions', {'tool_choice': 'none'}),
    ('/v1/completions', {'messages': [{'role': 'tool', 'tool_call_id': 'call', 'content': 'result'}]}),
    ('/v1/chat/completions', {'messages': [{'role': 'assistant', 'tool_calls': [{'id': 'call', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]}]})])
def test_local_model_entry_points_refuse_without_ability_after_model_visibility(fake_llama_server, path, extra):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, 'admin', 'admin')
        user = add_user(caller, 'pat', 'plain', by=admin)
        answer = caller.post(path, json={'model': 'small', **extra}, headers=user)
        assert answer.status_code == 403 and answer.json()['error']['code'] == 'tools_not_allowed'
        assert answer.headers['cache-control'] == 'private, no-store'
        assert caller.post(path, json={'model': 'hidden', **extra}, headers=user).status_code == 404
        assert caller.post(path, json={'model': 'small', 'messages': [{'role': 'user', 'content': 'Hi'}]}, headers=user).status_code == 200
        assert caller.post('/loadcoach/users/pat', json={'own': {'add': ['use-tools']}}, headers=admin).status_code == 200
        assert caller.post(path, json={'model': 'small', **extra}, headers=user).status_code == 200


def test_roles_and_responses_refuse_without_ability(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, 'admin', 'admin')
        user = add_user(caller, 'pat', 'plain', by=admin)
        assert caller.post('/loadcoach/roles/tool-role', json={'models': {'small': 1}}, headers=admin).status_code == 201
        answer = caller.post('/v1/chat/completions', json={'model': 'tool-role', 'tools': TOOLS, 'messages': []}, headers=user)
        assert answer.status_code == 403 and answer.json()['error']['code'] == 'tools_not_allowed'
        response_tools = [{'type': 'function', 'name': 'lookup', 'parameters': {'type': 'object', 'properties': {}}}]
        for model, status in [('small', 403), ('hidden', 404)]:
            answer = caller.post('/v1/responses', json={'model': model, 'input': 'Hi', 'tools': response_tools}, headers=user)
            assert answer.status_code == status
            if status == 403:
                assert answer.json()['error']['code'] == 'tools_not_allowed'


def test_batch_line_returns_403_while_plain_line_completes(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, 'admin', 'admin')
        user = add_user(caller, 'pat', 'plain', by=admin)
        batch = hand_in(caller, user, lines({'model': 'small', 'tools': TOOLS}, {'model': 'small', 'messages': []}))
        done = until(caller, user, batch['id'], lambda batch: batch['status'] == 'completed')
        assert done['request_counts'] == {'total': 2, 'completed': 1, 'failed': 1}
        failure = answers(caller, user, done['error_file_id'])[0]
        assert failure['response']['status_code'] == 403 and failure['error']['code'] == 'tools_not_allowed'


def test_hosted_entry_point_does_not_send_without_ability(rig):
    body = {'model': 'gpt', 'messages': [], 'tools': TOOLS}
    answer = rig.coach.post('/v1/chat/completions', json=body, headers=rig.keys['pat'])
    assert answer.status_code == 403 and answer.json()['error']['code'] == 'tools_not_allowed'
    assert not rig.service_calls
    assert all(model['id'] != 'gpt' for model in rig.coach.get('/v1/models', headers=rig.keys['sam']).json()['data'])
    rig.users.change('pat', own={'add': ['use-tools']})
    assert rig.coach.post('/v1/chat/completions', json=body, headers=rig.keys['pat']).status_code == 200
    assert len(rig.service_calls) == 1
