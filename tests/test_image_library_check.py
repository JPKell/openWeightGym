"""Image compatibility uses trusted header identities across cache/storage nodes."""
import json
from types import SimpleNamespace

import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from loadcoach.file_index import FileIndex
from loadcoach.groups import Group
from loadcoach.users import Users
from mirrorwall.assets import facts
from mirrorwall.components import check, identify
from mirrorwall.ui import routes
from test_mirrorwall_components import safetensors
from turnstile.app import Settings, build_app


def checkpoint(path):
    return safetensors(path, {
        'model.diffusion_model.label_emb.0.0.weight': (1280, 2816),
        'conditioner.embedders.1.model.ln_final.weight': (1280,),
        'first_stage_model.decoder.conv_in.weight': (512, 4, 3, 3),
    })


def test_inventory_check_never_opens_a_remote_path_on_front(tmp_path, monkeypatch):
    path = checkpoint(tmp_path / 'image.safetensors')
    remote = '/remote/storage/image.safetensors'
    inventory = [{**identify(path), 'path': remote}]
    monkeypatch.setattr('mirrorwall.components.identify', lambda *args: (_ for _ in ()).throw(AssertionError('Local read')))
    settings = {'flags': {'model': remote}, 'sdcpp': {'family': 'sdxl'}}
    assert check(settings, '', inventory=inventory)['ready']
    missing = check({**settings, 'flags': {'model': path}}, '', inventory=inventory)
    assert not missing['ready'] and missing['slots'][0]['found']['kind'] == 'missing'
    assert missing['slots'][0]['candidates'] == [remote]


def test_old_image_index_gains_identities_without_rereading_language_files(tmp_path):
    folder = tmp_path / 'image'
    folder.mkdir()
    image = checkpoint(folder / 'model.safetensors')
    language = folder / 'language.gguf'
    language.write_bytes(b'language')
    read = []
    def describe(file):
        read.append(file.name)
        return facts(file) if file.suffix == '.safetensors' else {'what': 'model'}
    index = FileIndex(None, describe=describe)
    spots = {'image': str(folder)}
    index.scan(spots)
    del index.kept['files'][image]['facts']['image_identity']
    read.clear()
    index.scan(spots)
    assert read == ['model.safetensors']
    assert index.kept['files'][image]['facts']['image_identity']['family'] == 'sdxl'
    read.clear()
    index.scan(spots)
    assert read == []


def test_new_draft_check_uses_library_but_existing_model_stays_on_its_node():
    called = []
    def people(handler):
        async def endpoint(request):
            return await handler(request, SimpleNamespace(can=['manage-models'], key='session'))
        return endpoint
    async def api(*args, **kwargs):
        called.append((args, kwargs))
        return httpx.Response(200, json={'ready': True})
    with TestClient(Starlette(routes=routes(people, None, api, api))) as client:
        for new in (True, False):
            r = client.post('/mirrorwall-models/check?node=rig', json={
                'new': new, 'settings': {'sdcpp': {'family': 'sdxl'}}, 'source_machine': 'front'})
            assert r.status_code == 200
        assert called[0][0][:2] == ('POST', '/turnstile/models/image-check')
        assert called[0][1]['json']['source_machine'] == 'front'
        assert called[1][0][-1] == '/loadcoach/images/check'


def test_library_check_requires_permission_and_lists_only_managed_files(tmp_path):
    storage = tmp_path / 'storage'
    image = storage / 'image'
    image.mkdir(parents=True)
    file = checkpoint(image / 'checkpoint.safetensors')
    outside = checkpoint(tmp_path / 'outside.safetensors')
    users = Users(str(tmp_path / 'accounts.db'), {
        'manager': Group(can=frozenset({'manage-models'})), 'reader': Group()})
    for name in ('manager', 'reader'):
        users.add(name, name)
    headers = {'authorization': 'Bearer ' + users.new_key('manager')}
    settings = Settings(compute='unreachable', data=str(tmp_path), models_storage_folder=str(storage))
    wire = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with TestClient(build_app(settings, wire, users=users)) as client:
        path = '/turnstile/models/image-check'
        body = {'source_machine': 'front', 'settings': {'flags': {'model': file}, 'sdcpp': {'family': 'sdxl'}}}
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers={'authorization': 'Bearer ' + users.new_key('reader')}, json=body).status_code == 403
        answer = client.post(path, headers=headers, json=body)
        assert answer.status_code == 200 and answer.json()['ready'], answer.text
        body['settings']['flags']['model'] = outside
        answer = client.post(path, headers=headers, json=body)
        assert not answer.json()['ready'] and answer.json()['slots'][0]['found']['kind'] == 'missing'
        for bad in ([], {}, {'settings': []}, {'settings': {}, 'source_machine': []}):
            assert client.post(path, headers=headers, content=json.dumps(bad)).status_code == 400
