"""Image compatibility uses trusted header identities across every node's model storage."""
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


def test_files_cached_as_unknown_before_a_family_existed_are_identified_again(tmp_path):
    """Krea 2 files indexed by an older release stayed 'unknown' until the identity version changed."""
    from tests.test_mirrorwall_components import safetensors
    from mirrorwall.components import IDENTITY_VERSION
    folder = tmp_path / 'image'
    folder.mkdir()
    weights = safetensors(folder / 'krea2.safetensors', {'model.diffusion_model.txtfusion.projector.weight': (1, 12)})
    index = FileIndex(None, describe=facts)
    spots = {'image': str(folder)}
    index.scan(spots)
    index.kept['files'][weights]['facts']['image_identity'] = {'kind': 'unknown', 'family': None, 'variant': None,
                                                                'label': 'unknown', 'version': IDENTITY_VERSION - 1}
    index.scan(spots)
    assert index.kept['files'][weights]['facts']['image_identity']['family'] == 'krea2'


def test_language_models_already_indexed_gain_a_text_encoder_identity_without_a_full_reread(tmp_path):
    from test_mirrorwall_components import gguf
    folder = tmp_path / 'llm'
    folder.mkdir()
    encoder = gguf(folder / 'Qwen3-4B.gguf', {'token_embd.weight': (151936, 2560)}, {'general.architecture': 'qwen3'})
    read = []
    def describe(file):
        read.append(file.name)
        return {'what': 'model', 'architecture': 'qwen3'}
    index = FileIndex(None, describe=describe)
    spots = {'llm': str(folder)}
    index.scan(spots)
    index.scan(spots)
    assert read == ['Qwen3-4B.gguf']
    identity = index.kept['files'][encoder]['facts']['image_identity']
    assert (identity['kind'], identity['variant']) == ('text_encoder', 'qwen3-4b')
    assert index.kept['files'][encoder]['facts']['architecture'] == 'qwen3'


def test_library_check_offers_language_models_as_encoders_and_names_their_node(tmp_path):
    from test_mirrorwall_components import gguf
    storage = tmp_path / 'storage'
    (storage / 'image').mkdir(parents=True)
    (storage / 'llm').mkdir()
    weights = gguf(storage / 'image' / 'klein-4b.gguf', {'model.diffusion_model.img_in.weight': (3072, 128),
                                                         'model.diffusion_model.double_blocks.0.img_attn.proj.weight': (3072, 3072)})
    encoder = gguf(storage / 'llm' / 'Qwen3-4B.gguf', {'token_embd.weight': (151936, 2560)},
                   {'general.architecture': 'qwen3', 'tokenizer.ggml.model': 'gpt2'})
    users = Users(str(tmp_path / 'accounts.db'), {'manager': Group(can=frozenset({'manage-models'}))})
    users.add('manager', 'manager')
    headers = {'authorization': 'Bearer ' + users.new_key('manager')}
    settings = Settings(compute='unreachable', data=str(tmp_path), models_storage_folder=str(storage))
    wire = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with TestClient(build_app(settings, wire, users=users)) as client:
        answer = client.post('/turnstile/models/image-check', headers=headers, json={
            'source_machine': 'front', 'settings': {'flags': {'model': weights}, 'sdcpp': {'family': 'flux2-klein'}}})
        assert answer.status_code == 200, answer.text
        slot = next(one for one in answer.json()['slots'] if one['slot'] == 'text_encoder')
        assert slot['candidates'] == [encoder]
        assert slot['choices'] == [{'path': encoder, 'machine': 'front'}]
        answer = client.post('/turnstile/models/image-check', headers=headers, json={
            'source_machine': 'front', 'settings': {'flags': {'model': weights},
                                                    'sdcpp': {'family': 'flux2-klein', 'components': {'text_encoder': encoder}}}})
        slot = next(one for one in answer.json()['slots'] if one['slot'] == 'text_encoder')
        assert slot['status'] == 'ok', slot
