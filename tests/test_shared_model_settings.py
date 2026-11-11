"""Shared model settings across copies, reviewed multi-node saves and moving a model between nodes."""
import json
import time
from copy import deepcopy

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import machine_key
from loadcoach import statement
from loadcoach.files import SPARE
from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.app import Settings, build_app
from turnstile.model_configuration import comparable, shared, translate, with_shared


def node(name, files):
    return {'name': name, 'answering': True, 'places': [{'name': 'llm', 'folder': f'/{name}/cache', 'tier': 'cache', 'free': 10 * SPARE}],
            'files': [{'path': f'/{name}/cache/{file}', 'size': 1, 'place': 'llm', 'tier': 'cache'} for file in files]}


def test_translate_finds_the_same_place_and_relative_file_on_another_node():
    nodes = [node('atlas', ['m.gguf', 'sub/proj.gguf']), node('orion', ['m.gguf', 'sub/proj.gguf']), node('nova', ['m.gguf'])]
    assert translate('/atlas/cache/sub/proj.gguf', 'atlas', 'orion', nodes) == '/orion/cache/sub/proj.gguf'
    assert translate('/atlas/cache/sub/proj.gguf', 'atlas', 'nova', nodes) is None
    assert translate('/atlas/cache/sub/proj.gguf', 'atlas', 'atlas', nodes) == '/atlas/cache/sub/proj.gguf'
    # A file outside every managed place is shared only by its identical path.
    nodes[1]['files'].append({'path': '/srv/shared.gguf', 'size': 1, 'place': 'other'})
    assert translate('/srv/shared.gguf', 'atlas', 'orion', nodes) == '/srv/shared.gguf'


def test_shared_settings_replace_projector_adapters_order_and_generation_but_keep_loading():
    source = {'flags': {'model': '/atlas/cache/m.gguf', 'mmproj': '/atlas/cache/p.gguf', 'temp': .3, 'ctx-size': 32768},
              'adapters': {'style': {'file': '/atlas/cache/a.gguf', 'scale': .5}}, 'order': 4}
    target = {'flags': {'model': '/orion/cache/m.gguf', 'temp': .9, 'ctx-size': 4096, 'threads': 8, 'top-k': 20},
              'adapters': {'old': {'file': '/orion/cache/old.gguf'}}, 'gpus': ['CUDA0'], 'order': 10}
    merged = with_shared(target, shared(source), lambda path: path.replace('/atlas/', '/orion/'))
    assert merged['flags'] == {'model': '/orion/cache/m.gguf', 'ctx-size': 4096, 'threads': 8, 'temp': .3, 'mmproj': '/orion/cache/p.gguf'}
    assert merged['adapters'] == {'style': {'file': '/orion/cache/a.gguf', 'scale': .5}}
    assert merged['gpus'] == ['CUDA0'] and merged['order'] == 4
    assert 'mmproj' not in with_shared(merged, shared(target), lambda path: path)['flags']
    assert comparable(shared(source)) == comparable(shared(merged))
    assert shared({'flags': {}})['order'] == shared({'flags': {}, 'order': 10})['order']


class Fleet:
    """Two LoadCoach workers, `compute` and `rig`, each with one copy of the same weights."""

    def __init__(self, tmp_path, rig_files=('m.gguf', 'p.gguf', 'a.gguf')):
        self.models = {
            'compute': {'qwen': {'flags': {'model': '/compute/cache/m.gguf', 'ctx-size': 8192, 'temp': .6}, 'gpus': 'any', 'order': 10}},
            'rig': {'qwen': {'flags': {'model': '/rig/cache/m.gguf', 'ctx-size': 4096, 'temp': .6, 'threads': 8}, 'gpus': 0, 'order': 10}}}
        self.files = {'compute': ('m.gguf', 'p.gguf', 'a.gguf'), 'rig': rig_files}
        self.refuse, self.deleted = set(), []
        keys = {'compute': machine_key(tmp_path), 'rig': str(tmp_path / 'rig.key')}
        statement.replace_key(keys['rig'], 'r' * 40)
        self.keys = keys

    def token(self, name, configuration):
        return format(abs(hash(json.dumps([self.models[name]['qwen'], configuration], sort_keys=True))), '064x')[-64:]

    @staticmethod
    def changes(kept, configuration):
        """The settings a candidate changes. As LoadCoach does, a setting at its default is no change."""
        def plain(settings):
            settings = {key: value for key, value in settings.items() if not (key == 'engine' and value == 'llama_cpp')}
            return settings | {'flags': {key: value for key, value in settings.get('flags', {}).items() if (key, value) != ('reasoning', 'auto')}}
        before, after = plain(kept), plain({**configuration['settings'], 'order': configuration['order']})
        fields = sorted({*before, *after} - {'flags'}) + sorted({*before['flags'], *after['flags']})
        return [{'field': field, 'before': before.get(field, before['flags'].get(field)), 'after': after.get(field, after['flags'].get(field)),
                 'before_set': True, 'after_set': True} for field in fields
                if (before.get(field), before['flags'].get(field)) != (after.get(field), after['flags'].get(field))]

    def worker(self, name):
        async def answer(request):
            path, body = request.url.path, (json.loads(await request.body()) if request.method == 'POST' else {})
            models = self.models[name]
            if path == '/loadcoach/models':
                return JSONResponse({'defaults': {}, 'cards': ['CUDA0'], 'models': [
                    {'name': label, **deepcopy(settings), 'sha256': 'same', 'engine': 'llama_cpp', 'model_id': f'{name}-{label}', 'size': 1 << 30}
                    for label, settings in models.items()]})
            if path == '/loadcoach/files':
                return JSONResponse(node(name, self.files[name]))
            if path == '/loadcoach/model-setup' and request.method == 'GET':
                return JSONResponse({'presets': [], 'undo': None, 'can_undo': False})
            if path == '/loadcoach/model-setup':
                configuration = body['configuration']
                if body['action'] == 'preview':
                    return JSONResponse({'candidate': configuration, 'token': self.token(name, configuration), 'errors': [], 'warnings': [],
                                         'changes': self.changes(models['qwen'], configuration)})
                if name in self.refuse:
                    return JSONResponse({'error': {'message': 'No room on its cards.'}}, 400)
                if body['token'] != self.token(name, configuration):
                    return JSONResponse({'error': {'message': 'The configuration changed. Preview it again.'}}, 409)
                models['qwen'] = {**configuration['settings'], 'order': configuration['order']}
                return JSONResponse({'applied': 'qwen'})
            if path.startswith('/loadcoach/models/'):
                label = path.removeprefix('/loadcoach/models/')
                if request.method == 'DELETE':
                    if name in self.refuse:
                        return JSONResponse({'error': {'message': 'It is locked.'}}, 409)
                    self.deleted.append((name, label)); models.pop(label, None)
                    return JSONResponse({'still_finishing': 0})
                if name in self.refuse:
                    return JSONResponse({'error': {'message': 'No room on its cards.'}}, 400)
                models[label] = body
                return JSONResponse(body, 201 if request.query_params.get('new') else 200)
            if path == '/loadcoach/files/hash':
                return JSONResponse({'sha256': 'f' * 64})
            if path == '/loadcoach/engines':
                return JSONResponse({'engines': [{'id': 'llama_cpp', 'enabled': True, 'available': True}]})
            return JSONResponse({'error': {'message': 'unused fixture endpoint'}}, 404)
        return statement.Statements(Starlette(routes=[Route('/{path:path}', answer, methods=['GET', 'POST', 'DELETE'])]), self.keys[name])


@pytest.fixture
def fleet(tmp_path):
    users = Users(str(tmp_path / 'accounts.db'), {'manager': Group(can=frozenset({'manage-models'}))})
    users.add('manager', 'manager')
    headers = {'authorization': 'Bearer ' + users.new_key('manager')}
    two = Fleet(tmp_path)

    class Wire(httpx.AsyncBaseTransport):
        wires = {8442: httpx.ASGITransport(app=two.worker('compute')), 9442: httpx.ASGITransport(app=two.worker('rig'))}

        async def handle_async_request(self, request):
            return await self.wires[request.url.port].handle_async_request(request)
    settings = Settings(compute='compute', data=str(tmp_path), machine_key_file=two.keys['compute'], ports=(8442, 8443, 8444),
                        machines={'rig': {'compute': 'http://compute:9442', 'spotter': 'http://compute:9444', 'machine_key_file': two.keys['rig']}})
    with TestClient(build_app(settings, httpx.AsyncClient(transport=Wire()), users=users), base_url='http://front:8442') as front:
        yield front, headers, two


def reviewed(two, name, settings, order=10):
    configuration = {'settings': settings, 'order': order}
    return {'machine': name, 'name': 'qwen', 'configuration': configuration, 'token': two.token(name, configuration)}


def test_a_projector_saved_on_one_copy_reaches_the_other_with_its_own_path(fleet):
    front, headers, two = fleet
    changed = {**two.models['compute']['qwen'], 'flags': {**two.models['compute']['qwen']['flags'], 'mmproj': '/compute/cache/p.gguf'}, 'order': 3}
    answer = front.post('/turnstile/machines/compute/model-api/models/qwen', headers=headers, json=changed)
    assert answer.status_code == 200, answer.text
    assert two.models['rig']['qwen']['flags']['mmproj'] == '/rig/cache/p.gguf'
    assert two.models['rig']['qwen']['order'] == 3
    assert two.models['rig']['qwen']['flags']['ctx-size'] == 4096 and two.models['rig']['qwen']['gpus'] == 0


def test_a_shared_file_missing_on_a_copy_changes_nothing(tmp_path, fleet):
    front, headers, two = fleet
    two.files['rig'] = ('m.gguf',)
    before = deepcopy(two.models)
    changed = {**before['compute']['qwen'], 'adapters': {'style': {'file': '/compute/cache/a.gguf'}}}
    answer = front.post('/turnstile/machines/compute/model-api/models/qwen', headers=headers, json=changed)
    assert answer.status_code == 409 and 'a.gguf on rig is missing' in answer.json()['error']['message']
    assert two.models == before


def test_several_nodes_apply_together_or_not_at_all(fleet):
    front, headers, two = fleet
    before = deepcopy(two.models)
    compute = {**before['compute']['qwen'], 'flags': {**before['compute']['qwen']['flags'], 'ctx-size': 16384, 'temp': .8}}
    rig = {**before['rig']['qwen'], 'flags': {**before['rig']['qwen']['flags'], 'ctx-size': 2048, 'temp': .8}}
    compute.pop('order'); rig.pop('order')
    # Shared settings must agree between the copies.
    disagree = [reviewed(two, 'compute', compute), reviewed(two, 'rig', {**rig, 'flags': {**rig['flags'], 'temp': .1}})]
    assert front.post('/turnstile/models/configure', headers=headers, json={'changes': disagree}).status_code == 400
    # A shared change must include every copy.
    alone = front.post('/turnstile/models/configure', headers=headers, json={'changes': [reviewed(two, 'compute', compute)]})
    assert alone.status_code == 409 and 'include rig' in alone.json()['error']['message']
    # A refusal on the second node restores the first.
    two.refuse.add('rig')
    refused = front.post('/turnstile/models/configure', headers=headers, json={'changes': [reviewed(two, 'compute', compute), reviewed(two, 'rig', rig)]})
    assert refused.status_code == 409 and refused.json()['error']['message'].startswith('rig: No room')
    assert two.models['compute']['qwen']['flags'] == before['compute']['qwen']['flags']
    two.refuse.clear()
    applied = front.post('/turnstile/models/configure', headers=headers, json={'changes': [reviewed(two, 'compute', compute), reviewed(two, 'rig', rig)]})
    assert applied.status_code == 200, applied.text
    assert two.models['compute']['qwen']['flags']['ctx-size'] == 16384 and two.models['rig']['qwen']['flags']['ctx-size'] == 2048
    assert two.models['rig']['qwen']['flags']['temp'] == .8 and two.models['rig']['qwen']['flags']['threads'] == 8
    # Loading settings alone may change on one node.
    local = {**rig, 'flags': {**rig['flags'], 'ctx-size': 1024}}
    assert front.post('/turnstile/models/configure', headers=headers, json={'changes': [reviewed(two, 'rig', local)]}).status_code == 200
    assert two.models['rig']['qwen']['flags']['ctx-size'] == 1024


def test_unauthorised_and_malformed_multi_node_saves_are_refused(fleet):
    front, headers, two = fleet
    assert front.post('/turnstile/models/configure', json={'changes': []}).status_code == 401
    assert front.post('/turnstile/models/configure', headers=headers, json={'changes': []}).status_code == 400
    twice = reviewed(two, 'rig', two.models['rig']['qwen'])
    assert front.post('/turnstile/models/configure', headers=headers, json={'changes': [twice, twice]}).status_code == 400


def test_a_move_requires_one_destination_and_a_known_source(fleet):
    front, headers, two = fleet
    body = {'name': 'qwen', 'settings': two.models['compute']['qwen'], 'move': True, 'source_machine': 'compute', 'source_model_id': 'compute-qwen'}
    for wrong in ({'machines': ['rig', 'compute']}, {'machine': 'compute'}, {'machine': 'rig', 'source_model_id': ''}):
        answer = front.post('/turnstile/models/install', headers=headers, json={**body, **wrong})
        assert answer.status_code == 409 and 'one node to one other node' in answer.json()['error']['message']


@pytest.mark.parametrize('removal', ['works', 'refused'])
def test_a_move_copies_verifies_and_only_then_removes_the_source(fleet, removal):
    front, headers, two = fleet
    two.models['rig'] = {}
    if removal == 'refused':
        two.refuse.add('compute')  # the source refuses to remove its copy; the new copy stays usable

    body = {'name': 'qwen', 'machine': 'rig', 'settings': two.models['compute']['qwen'], 'move': True,
            'source_machine': 'compute', 'source_model_id': 'compute-qwen'}
    answer = front.post('/turnstile/models/install', headers=headers, json=body)
    assert answer.status_code == 202, answer.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = front.get('/turnstile/artifacts/distributions', headers=headers).json()['distributions'][0]
        if job['state'] in ('complete', 'failed'):
            break
        time.sleep(.05)
    assert two.models['rig']['qwen']['flags']['model'] == '/rig/cache/m.gguf'
    if removal == 'works':
        assert job['state'] == 'complete' and job['moved_from'] == 'compute' and job['machine'] == 'rig'
        assert two.deleted == [('compute', 'qwen')] and 'qwen' not in two.models['compute']
    else:
        assert job['state'] == 'failed' and 'compute kept its copy' in job['why']
        assert 'qwen' in two.models['compute']
