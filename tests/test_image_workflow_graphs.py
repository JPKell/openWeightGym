"""Image workflow graph documents: validation, versions, publishing, the manage-workflows ability and model-scoped access."""
import copy
import json
import sqlite3

import pytest

from db import database, migrate
from loadcoach.batches import Batches
from loadcoach.groups import ABILITIES
from mirrorwall import graph
from mirrorwall.front import Front
from mirrorwall.schema import MIRRORWALL
from mirrorwall.store import Store
from test_weightroom import desk, sign_in  # noqa: F401

MODELS = [{'id': 'klein-4b', 'family': 'flux2-klein'}, {'id': 'flux2-dev', 'family': 'flux2-dev'}, {'id': 'sdxl-base', 'family': 'sdxl'},
          {'id': 'sdxl-photo', 'family': 'sdxl'}, {'id': 'krea2-turbo', 'family': 'krea2'}, {'id': 'qwen-image', 'family': 'qwen-image'}]


def sample(name):
    return copy.deepcopy(next(one for one in graph.SAMPLES if one['name'] == name))


def test_every_sample_is_a_valid_document_and_only_later_phases_report_problems():
    controlled = [{**model, 'control': model['id'] == 'sdxl-base'} for model in MODELS]
    for one in graph.SAMPLES:
        document, problems = graph.check(one, controlled)
        assert document['api_name'] == graph.slug(one['name'])
        planned = {graph.COMPONENTS[node['type']]['phase'] for node in document['nodes'] if graph.COMPONENTS[node['type']]['phase'] > graph.RUNNABLE_PHASE}
        assert bool([p for p in problems if p['level'] == 'error']) == bool(planned), (one['name'], problems)
    # Without a ControlNet on its model, the structure sample cannot run.
    assert [p['text'] for p in graph.check(sample('Structure-guided generation'), MODELS)[1]] == [
        'This model has no ControlNet. Add one to an exclusive SDXL model entry under Models → Images, or remove the control image.']
    assert 'manage-workflows' in ABILITIES


def mutate(change):
    document = sample('SDXL two-model refine')
    change(document)
    return document


@pytest.mark.parametrize('change,message', [
    (lambda d: d['links'].append({'from': ['refine', 'image'], 'to': ['base', 'init']}), 'loop'),
    (lambda d: d['links'].append({'from': ['seed', 'seed'], 'to': ['out', 'image']}), 'takes Image, not Seed'),
    (lambda d: d['links'].append({'from': ['base', 'image'], 'to': ['out', 'image']}), 'takes one source'),
    (lambda d: d['links'].append({'from': ['refine', 'image'], 'to': ['out', 'image']}), 'listed twice'),
    (lambda d: d['links'].append({'from': ['ghost', 'image'], 'to': ['out', 'image']}), 'does not exist'),
    (lambda d: d['links'].append({'from': ['base', 'latent'], 'to': ['out', 'image']}), 'port that does not exist'),
    (lambda d: d['nodes'][0].update(id='Prompt 1'), 'Step labels'),
    (lambda d: d['nodes'].append(copy.deepcopy(d['nodes'][0])), 'share the label'),
    (lambda d: d['nodes'][0].update(type='shell'), 'Unknown component'),
    (lambda d: next(n for n in d['nodes'] if n['id'] == 'base')['values'].update(steps=900), 'Steps must be a number from 1 to 200'),
    (lambda d: next(n for n in d['nodes'] if n['id'] == 'base')['values'].update(sampler='dpmpp2m'), 'Sampler must be one of'),
    (lambda d: next(n for n in d['nodes'] if n['id'] == 'base')['values'].update(backend='cpu'), 'unknown settings'),
    (lambda d: next(n for n in d['nodes'] if n['id'] == 'refine')['overridable'].update(strength=[0.2, 1.5]), 'bounds must lie within'),
    (lambda d: next(n for n in d['nodes'] if n['id'] == 'refine')['overridable'].update(sampler=[0, 1]), 'cannot be overridden'),
    (lambda d: next(n for n in d['nodes'] if n['id'] == 'refine').update(exposed=['strength']), 'not both'),
    (lambda d: d.update(api_name='Has Spaces'), 'API name'),
    (lambda d: d.update(script='alert(1)'), 'Unsupported workflow fields'),
    (lambda d: d['nodes'][0]['values'].update(text='x' * 300000), '256 KiB'),
])
def test_structural_faults_are_refused_with_a_reason(change, message):
    with pytest.raises(ValueError, match=message):
        graph.check(mutate(change), MODELS)


def test_run_problems_follow_the_installed_models_and_their_families():
    document = sample('Klein multi-reference edit')
    base = next(node for node in document['nodes'] if node['type'] == 'sampler')
    loader = next(node for node in document['nodes'] if node['type'] == 'load_image')
    document['links'].append({'from': [loader['id'], 'image'], 'to': [base['id'], 'init']})
    for index in range(4):
        document['nodes'].append({**copy.deepcopy(loader), 'id': f'extra{index}'})
        document['links'].append({'from': [f'extra{index}', 'image'], 'to': [base['id'], 'references']})
    texts = [p['text'] for p in graph.check(document, MODELS)[1]]
    assert 'Flux 2 klein uses at most 4 reference images.' in texts
    assert any('no start-image pass' in text for text in texts)
    texts = [p['text'] for p in graph.check(sample('Quick draft'), [m for m in MODELS if m['id'] != 'klein-4b'])[1]]
    assert texts == ['Choose an installed image model.']
    unlinked = sample('Quick draft')
    unlinked['links'] = [link for link in unlinked['links'] if link['to'] != ['base', 'prompt']]
    assert 'Prompt needs a source.' in [p['text'] for p in graph.check(unlinked, MODELS)[1]]
    sdxl = sample('SDXL two-model refine')
    next(n for n in sdxl['nodes'] if n['id'] == 'base')['values']['flow_shift'] = 3
    assert 'Flow shift applies to flow-matching models, not SDXL.' in [p['text'] for p in graph.check(sdxl, MODELS)[1]]
    caller = graph.interface(graph.check(sample('SDXL two-model refine'), MODELS)[0])
    assert caller['models'] == ['sdxl-base', 'sdxl-photo'] and caller['outputs'] == [{'step': 'out', 'label': 'Final'}]
    assert {'name': 'refine.strength', 'label': 'Refine · Strength (denoise)', 'default': 0.25, 'min': 0.1, 'max': 0.45} in caller['overrides']
    assert any(one['name'] == 'prompt.text' for one in caller['inputs'])


def test_upscale_steps_and_hires_passes_choose_installed_upscaler_files():
    installed = ['4x-UltraSharp.pth', 'RealESRGAN_x4plus.pth']
    document = sample('Upscaler comparison')
    assert {n['values']['upscaler'] for n in document['nodes'] if n['type'] == 'upscale'} >= {'4x-UltraSharp.pth', '4x_NMKD-Siax_200k.pth'}
    assert not graph.check(document, MODELS)[1]  # without an inventory, files are not checked
    found = graph.check(document, MODELS, installed)[1]
    assert found == [{'node': 'siax', 'level': 'error', 'text': 'That upscaler is not installed on any node.'}]
    next(n for n in document['nodes'] if n['id'] == 'siax')['values']['upscaler'] = ''
    assert {'node': 'siax', 'level': 'error', 'text': 'Choose an upscaler.'} in graph.check(document, MODELS, installed)[1]
    for bad in ('../../etc/passwd', 'up/scaler.pth', 'notes.txt'):
        next(n for n in document['nodes'] if n['id'] == 'siax')['values']['upscaler'] = bad
        with pytest.raises(ValueError, match='must name an upscaler file'):
            graph.check(document, MODELS)
    hires = sample('SDXL two-pass hires')
    base = next(n for n in hires['nodes'] if n['id'] == 'base')
    base['values']['hires_upscaler'] = 'RealESRGAN_x4plus.pth'
    # An installed file is accepted, but a hires pass cannot run one yet, so it is a problem to fix before publishing.
    assert [p['text'] for p in graph.check(hires, MODELS, installed)[1] if p['node'] == 'base'] == [
        'A hires pass with an upscaler file cannot run yet; choose a latent method or add an Upscale step.']
    base['values']['hires_upscaler'] = '4x_NMKD-Siax_200k.pth'
    assert 'That upscaler is not installed on any node.' in [p['text'] for p in graph.check(hires, MODELS, installed)[1]]
    base['values']['hires_upscaler'] = 'bicubic please'
    with pytest.raises(ValueError, match='or an installed upscaler file'):
        graph.check(hires, MODELS)


def test_lane_positions_are_kept_and_editor_link_flags_dropped():
    document = sample('Quick draft')
    document['nodes'][0]['lane'] = [2, 0]
    document['nodes'][1]['lane'] = [None, 1.5]
    document['links'][0]['auto'] = True
    checked = graph.check(document, MODELS)[0]
    assert checked['nodes'][0]['lane'] == [2, 0] and checked['nodes'][1]['lane'] == [None, 1.5] and 'lane' not in checked['nodes'][2]
    assert all(link.keys() == {'from', 'to'} for link in checked['links'])
    for bad in ([1], [-1, 0], ['a', 0], {'lane': 1}):
        document['nodes'][0]['lane'] = bad
        with pytest.raises(ValueError, match='lane position'):
            graph.check(document, MODELS)
    document['nodes'][0]['lane'] = None
    document['links'][0]['auto'] = 'yes'
    with pytest.raises(ValueError, match='Each link'):
        graph.check(document, MODELS)


def test_versions_publishing_pins_and_removal(tmp_path):
    store = Store(Batches(str(tmp_path / 'accounts.db')).db, tmp_path)
    document = graph.check(sample('SDXL two-model refine'), MODELS)[0]
    first = store.save_graph('author-1', document)
    assert first['version'] == 1 and first['published'] == 0 and store.runnable_graph('sdxl-two-model-refine') is None
    with pytest.raises(ValueError, match='changed elsewhere'):
        store.save_graph('author-2', document, first['id'])
    with pytest.raises(ValueError, match='already uses the API name'):
        store.save_graph('author-2', document)
    store.publish_graph(first['id'], 1, 1)
    changed = copy.deepcopy(document)
    next(n for n in changed['nodes'] if n['id'] == 'refine')['values']['steps'] = 20
    second = store.save_graph('author-2', changed, first['id'], 1)
    assert second['version'] == 2 and second['published'] == 1 and second['author'] == 'author-2'
    # Unpinned calls keep running the published version until another is published.
    assert store.runnable_graph('sdxl-two-model-refine')['version'] == 1
    assert store.runnable_graph('sdxl-two-model-refine', 2) is None  # never published
    with pytest.raises(ValueError, match='changed elsewhere'):
        store.publish_graph(first['id'], 2, 1)
    store.publish_graph(first['id'], 2, 2)
    assert store.runnable_graph('sdxl-two-model-refine')['version'] == 2
    assert store.runnable_graph('sdxl-two-model-refine', 1)['document']['nodes'] == document['nodes']  # pinned
    with pytest.raises(ValueError, match='no such version'):
        store.publish_graph(first['id'], 7, 2)
    store.publish_graph(first['id'], 0, 2)
    assert store.runnable_graph('sdxl-two-model-refine') is None and store.runnable_graph('sdxl-two-model-refine', 1) is None
    assert [one['version'] for one in store.graph_versions(first['id'])] == [2, 1]
    assert not store.remove_graph(first['id'], 1)
    assert store.remove_graph(first['id'], 2)
    assert store.graphs() == [] and store.graph_versions(first['id']) == []


def test_migration_adds_graph_tables_to_an_existing_store_without_touching_presets(tmp_path):
    path = tmp_path / 'accounts.db'
    source = database(str(path))
    with source.connect() as connection:
        migrate(source, connection, 'mirrorwall.front', MIRRORWALL[:3])
        connection.execute("INSERT INTO mirrorwall_presets VALUES ('workflow_'||'a'||'0000000000000000000000a','owner','Daily',2,'m','klein','{\"steps\":6}',1)")
        connection.execute("INSERT INTO mirrorwall_preset_versions VALUES ('workflow_a0000000000000000000000a',2,'Daily','m','klein','{\"steps\":6}',1)")
    store = Store(Batches(str(path)).db, tmp_path)
    assert store.presets('owner')[0]['settings'] == {'steps': 6}
    assert store.graphs() == []
    with sqlite3.connect(path) as raw:
        assert {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {'mirrorwall_graphs', 'mirrorwall_graph_versions'}


def test_workflow_api_needs_the_ability_to_change_and_access_to_every_model_to_run(desk, monkeypatch):
    browser, switch, users = desk
    allowed = {'jordan': MODELS, 'pat': [m for m in MODELS if m['id'] != 'sdxl-photo']}

    async def catalogue(self, user, kind):
        return [{'id': m['id'], 'image_workflow': {'family': m['family']}} for m in allowed[user.name]]
    monkeypatch.setattr(Front, 'media_catalogue', catalogue)
    sign_in(browser, 'pat')
    assert browser.post('/mirrorwall-api/graphs', json={'document': sample('Quick draft')}).status_code == 403
    sign_in(browser, 'jordan')
    library = browser.get('/mirrorwall-api/graphs').json()
    assert library['can_manage'] and len(library['templates']) == len(graph.SAMPLES) and library['catalogue']['samplers'][5] == 'dpm++2m'
    assert browser.post('/mirrorwall-api/graphs', json={'document': {'name': 'Bad', 'nodes': 'x'}}).status_code == 400
    refine = browser.post('/mirrorwall-api/graphs', json={'document': sample('SDXL two-model refine')})
    assert refine.status_code == 201, refine.text
    draft = browser.post('/mirrorwall-api/graphs', json={'document': sample('Quick draft')}).json()
    tiled = browser.post('/mirrorwall-api/graphs', json={'document': sample('Structure-guided generation')}).json()  # no ControlNet
    assert tiled['problems'] and browser.post(f"/mirrorwall-api/graphs/{tiled['id']}/publish", json={'version': 1, 'expected_version': 1}).status_code == 400
    for one in (refine.json(), draft):
        published = browser.post(f"/mirrorwall-api/graphs/{one['id']}/publish", json={'version': 1, 'expected_version': 1})
        assert published.status_code == 200 and published.json()['published'] == 1
    assert browser.post(f"/mirrorwall-api/graphs/{draft['id']}/publish", json={'version': 1, 'expected_version': 1},
                        headers={'origin': 'https://elsewhere.example'}).status_code == 403
    detail = browser.get(f"/mirrorwall-api/graphs/{draft['id']}").json()
    assert detail['versions'][0]['published'] and detail['document']['api_name'] == 'quick-draft'
    sign_in(browser, 'pat')
    listed = browser.get('/mirrorwall-api/graphs').json()
    # Pat may not use sdxl-photo, so the refine workflow is hidden; drafts and templates are managers' only.
    assert [one['api_name'] for one in listed['workflows']] == ['quick-draft'] and 'templates' not in listed
    assert 'document' not in listed['workflows'][0] and listed['workflows'][0]['inputs']
    assert browser.get(f"/mirrorwall-api/graphs/{refine.json()['id']}").status_code == 404
    assert browser.get(f"/mirrorwall-api/graphs/{tiled['id']}").status_code == 404
    assert browser.delete(f"/mirrorwall-api/graphs/{draft['id']}?version=1").status_code == 403
    sign_in(browser, 'jordan')
    assert browser.delete(f"/mirrorwall-api/graphs/{draft['id']}?version=2").status_code == 409
    assert browser.delete(f"/mirrorwall-api/graphs/{draft['id']}?version=1").json() == {'removed': draft['id']}
    assert browser.get(f"/mirrorwall-api/graphs/{draft['id']}").status_code == 404
