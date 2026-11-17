"""Image workflow execution: caller inputs, step order and model grouping, fan-out, hand-offs, refusals and the workflow: API."""

import asyncio
import base64
import copy
import io
import json

import pytest
from PIL import Image
from starlette.testclient import TestClient

from mirrorwall import graph, graph_run
from mirrorwall.front import Front
from test_weightroom import desk, sign_in  # noqa: F401

MODELS = [{'id': 'klein-4b', 'family': 'flux2-klein'}, {'id': 'sdxl-base', 'family': 'sdxl'}, {'id': 'sdxl-photo', 'family': 'sdxl'}]
UPSCALERS = [{'id': '4x-UltraSharp.pth', 'nodes': ['atlas']}, {'id': 'RealESRGAN_x4plus.pth', 'nodes': ['orion']},
             {'id': '4x_NMKD-Siax_200k.pth', 'nodes': ['atlas', 'orion']}]


def sample(name):
    document, _ = graph.check(copy.deepcopy(next(one for one in graph.SAMPLES if one['name'] == name)))
    return document


def png(width, height, colour='blue'):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), colour).save(buffer, 'PNG')
    return base64.b64encode(buffer.getvalue()).decode()


class Engine:
    """Stands in for the image API: records each step's request and answers with an image of the asked size."""
    def __init__(self, fail=None, node='atlas'):
        self.sent, self.fail, self.node = [], fail, node

    async def send(self, path, body):
        self.sent.append((path, copy.deepcopy(body)))
        yield {'type': 'image_generation.progress', 'stage': 'generating'}
        if self.fail and self.fail(body):
            yield {'type': 'error', 'error': {'message': 'Fixture failure'}}
            return
        if 'upscale_with' in body:
            source = Image.open(io.BytesIO(base64.b64decode(body['image'])))
            size = (round(source.width * body['upscale_with']['factor']), round(source.height * body['upscale_with']['factor']))
        else:
            size = tuple(map(int, body.get('size', '1024x1024').split('x')))
        yield {'type': 'image_generation.completed', 'b64_json': png(*size), 'seed': body.get('seed'), 'width': size[0], 'height': size[1],
               'node': body.get('node', self.node)}


def run(document, asked=None, engine=None, models=MODELS, upscalers=UPSCALERS, image_of=None):
    engine = engine or Engine()
    prepared, count, images = graph_run.prepare(document, asked or {})
    async def collect():
        return [event async for event in graph_run.run(prepared, count, images, models=models, upscalers=upscalers,
                                                       send=engine.send, image_of=image_of)]
    return asyncio.run(collect()), engine


def test_caller_inputs_map_to_exposed_fields_and_everything_else_is_refused():
    document = sample('Quick draft')
    prepared, count, _ = graph_run.prepare(document, {'prompt': 'A fox', 'seed': 9, 'n': 3, 'workflow_inputs': {'base.steps': 8}})
    nodes = {node['id']: node for node in prepared['nodes']}
    assert nodes['prompt']['values']['text'] == 'A fox' and nodes['seed']['values']['value'] == 9 and count == 3
    assert nodes['base']['values']['steps'] == 8 and document['nodes'] != prepared['nodes']  # the saved document is unchanged
    for asked, why in (({'steps': 30}, 'does not take steps'), ({'negative_prompt': 'x'}, 'does not take negative_prompt'),
                       ({'size': '512x512'}, 'does not take size'), ({'workflow_inputs': {'base.steps': 20}}, 'from 4 to 12'),
                       ({'workflow_inputs': {'base.cfg_scale': 2}}, 'not an input'), ({'workflow_inputs': {'base.steps': 6.5}}, 'whole number'),
                       ({'n': 0}, 'n must be'), ({'n': 17}, 'n must be'), ({'image': png(64, 64)}, 'does not take images'),
                       ({'response_format': 'url'}, 'response_format')):
        with pytest.raises(graph_run.RunError, match=why):
            graph_run.prepare(document, asked)
    assert graph_run.parse_model('workflow:quick-draft@3') == ('quick-draft', 3)
    assert graph_run.parse_model('sdxl-base') is None
    with pytest.raises(graph_run.RunError):
        graph_run.parse_model('workflow:../x')


def test_fan_out_shares_prompt_model_and_seed_and_every_compare_image_is_returned():
    events, engine = run(sample('Sampler shoot-out'), {'n': 2})
    bodies = [body for _, body in engine.sent]
    assert [body['sampler'] for body in bodies[:4]] == ['euler', 'dpm++2m', 'res_multistep', 'er_sde']
    assert {body['seed'] for body in bodies} == {1234}  # the sample's seed is fixed, so every item repeats it
    assert {body['model'] for body in bodies} == {'sdxl-base'} and all(body['size'] == '1024x1024' for body in bodies)
    outputs = [event for event in events if event['type'] == 'workflow.output']
    assert len(outputs) == 8 and outputs[0]['output'] == 'Samplers · Euler · simple' and outputs[0]['stages']['euler']['sampler'] == 'euler'
    assert [event['state'] for event in events if event['type'] == 'workflow.step'][:2] == ['started', 'completed']
    assert events[0] == {'type': 'workflow.started', 'items': 2, 'steps': 8, 'plan': ['euler', 'dpm', 'res', 'ersde']}


def test_seed_modes_fixed_increment_and_random():
    document = sample('Quick draft')
    for mode, expected in (('fixed', [1234, 1234]), ('increment', [1234, 1235])):
        next(node for node in document['nodes'] if node['id'] == 'seed')['values']['mode'] = mode
        _, engine = run(document, {'n': 2})
        assert [body['seed'] for _, body in engine.sent] == expected
    next(node for node in document['nodes'] if node['id'] == 'seed')['values']['mode'] = 'random'
    _, engine = run(document, {'n': 2})
    first, second = [body['seed'] for _, body in engine.sent]
    assert second == (first + 1) % 2**31


def test_steps_on_one_model_run_together_and_a_refine_pass_starts_from_the_image():
    document = sample('Krea Turbo draft → SDXL detail')
    models = [*MODELS, {'id': 'krea2-turbo', 'family': 'krea2'}]
    events, engine = run(document, models=models)
    paths = [(path, body.get('model'), 'upscale_with' in body) for path, body in engine.sent]
    assert paths == [('/v1/images/generations', 'krea2-turbo', False), ('/v1/images/edits', 'krea2-turbo', True),
                     ('/v1/images/edits', 'sdxl-base', False)]
    detail = engine.sent[2][1]
    # Sized from the 2048 × 2048 start image within the 4 MP reference limit; the start image is sent at exactly that size.
    assert detail['strength'] == 0.3 and len(detail['references']) == 1 and detail['size'] == '2000x2000'
    assert detail['reference_megapixels'] == 4.0
    assert Image.open(io.BytesIO(base64.b64decode(detail['references'][0]))).size == (2000, 2000)
    assert 'negative_prompt' not in detail and 'guidance' not in detail
    assert [event['output'] for event in events if event['type'] == 'workflow.output'] == ['Draft', 'Final']


def test_upscale_steps_stay_on_the_node_that_made_the_image_when_it_holds_the_file():
    events, engine = run(sample('Upscaler comparison'))
    upscales = [body for _, body in engine.sent if 'upscale_with' in body]
    assert [(one['upscale_with']['upscaler'], one.get('node')) for one in upscales] == [
        ('4x-UltraSharp.pth', 'atlas'), ('RealESRGAN_x4plus.pth', None), ('4x_NMKD-Siax_200k.pth', 'atlas'), ('4x-UltraSharp.pth', 'atlas')]
    assert all(one['model'] == 'sdxl-base' for one in upscales)
    sizes = {event['output']: (event['width'], event['height']) for event in events if event['type'] == 'workflow.output'}
    assert sizes['Upscalers · UltraSharp then 2× more'] == (4096, 4096) and sizes['Upscalers · Siax 2×'] == (2048, 2048)


def test_load_image_resize_and_combine_run_on_the_control_node():
    document, _ = graph.check({'name': 'Resize then upscale', 'nodes': [
        {'id': 'photo', 'type': 'load_image', 'exposed': ['asset']},
        {'id': 'fit', 'type': 'resize', 'values': {'mode': 'fill', 'width': 512, 'height': 256}},
        {'id': 'up', 'type': 'upscale', 'values': {'upscaler': '4x-UltraSharp.pth', 'factor': 2}},
        {'id': 'out', 'type': 'output'}],
        'links': [{'from': ['photo', 'image'], 'to': ['fit', 'image']}, {'from': ['fit', 'image'], 'to': ['up', 'image']},
                  {'from': ['up', 'image'], 'to': ['out', 'image']}]})
    with pytest.raises(graph_run.RunError, match='needs an image'):
        run(document)
    events, engine = run(document, {'image': png(300, 300)})
    assert engine.sent[0][1]['model'] == 'klein-4b'  # a node is reached through any model of the caller
    output = next(event for event in events if event['type'] == 'workflow.output')
    assert (output['width'], output['height']) == (1024, 512)
    events, _ = run(document, {'workflow_inputs': {'photo': 'image_' + '0' * 32}}, image_of=lambda identifier: png(100, 50))
    assert next(event for event in events if event['type'] == 'workflow.output')['width'] == 1024
    with pytest.raises(graph_run.RunError, match='no such image'):
        run(document, {'workflow_inputs': {'photo': 'image_' + '1' * 32}}, image_of=lambda identifier: None)


def test_a_failed_step_names_itself_and_stops_the_run():
    engine = Engine(fail=lambda body: body.get('sampler') == 'res_multistep')
    with pytest.raises(graph_run.RunError, match='Res multistep · beta: Fixture failure'):
        run(sample('Sampler shoot-out'), engine=engine)
    assert len(engine.sent) == 3


def test_hires_with_an_upscaler_file_is_a_problem_until_it_can_run():
    document = sample('SDXL two-pass hires')
    assert not [one for one in graph.problems(document) if one['level'] == 'error']
    next(node for node in document['nodes'] if node['id'] == 'base')['values']['hires_upscaler'] = '4x-UltraSharp.pth'
    assert any('hires pass with an upscaler file' in one['text'] for one in graph.problems(document))
    _, engine = run(sample('SDXL two-pass hires'))
    assert engine.sent[0][1]['advanced']['hires'] == {'upscaler': 'Latent', 'scale': 1.5, 'steps': 14, 'denoising_strength': 0.38}


@pytest.fixture
def api(desk, monkeypatch):
    site, switch, users = desk
    async def models(self, user):
        return copy.deepcopy(MODELS)
    async def upscalers(self, user):
        return copy.deepcopy(UPSCALERS)
    engine = Engine()
    monkeypatch.setattr(Front, 'graph_models', models)
    monkeypatch.setattr(Front, 'graph_upscalers', upscalers)
    monkeypatch.setattr(Front, 'stage_sender', lambda self, user, headers: engine.send)
    sign_in(site, 'jordan')
    document = sample('Quick draft')
    saved = site.post('/mirrorwall-api/graphs', json={'document': document}).json()
    assert site.post(f"/mirrorwall-api/graphs/{saved['id']}/publish", json={'version': 1, 'expected_version': 1}).status_code == 200
    with TestClient(switch.front, base_url='http://front:8442') as client:
        yield client, users, engine, site, saved


def test_workflow_api_runs_lists_and_follows_model_access(api):
    client, users, engine, site, saved = api
    key = {'authorization': 'Bearer ' + users.new_key('jordan')}
    listed = client.get('/v1/workflows', headers=key).json()['data']
    assert [one['id'] for one in listed] == ['workflow:quick-draft'] and listed[0]['overrides'][0]['name'] == 'base.steps'
    answer = client.post('/v1/images/generations', headers=key, json={'model': 'workflow:quick-draft', 'prompt': 'A fox', 'n': 2,
                                                                       'workflow_inputs': {'base.steps': 5}})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body['workflow'] == {'id': saved['id'], 'api_name': 'quick-draft', 'version': 1}
    assert [(one['output'], one['item']) for one in body['data']] == [('Final', 0), ('Final', 1)]
    assert all(one['asset_id'].startswith('image_') and one['b64_json'] for one in body['data'])
    assert engine.sent[0][1]['prompt'] == 'A fox' and engine.sent[0][1]['steps'] == 5
    library = site.get('/mirrorwall-api/assets').json()
    saved_asset = next(one for one in library['assets'] if one['id'] == body['data'][0]['asset_id'])
    assert saved_asset['metadata']['workflow']['api_name'] == 'quick-draft' and saved_asset['metadata']['stages']['base']['steps'] == 5
    streamed = client.post('/v1/images/generations', headers=key, json={'model': 'workflow:quick-draft@1', 'prompt': 'A fox', 'stream': True})
    kinds = [json.loads(line[5:])['type'] for line in streamed.text.splitlines() if line.startswith('data:')]
    assert kinds[0] == 'workflow.started' and 'image_generation.completed' in kinds and kinds[-1] == 'workflow.completed'
    for asked, status, why in (({'model': 'workflow:quick-draft', 'steps': 9}, 400, 'does not take steps'),
                               ({'model': 'workflow:quick-draft@2'}, 404, 'no such workflow'),
                               ({'model': 'workflow:nothing'}, 404, 'no such workflow')):
        refused = client.post('/v1/images/generations', headers=key, json=asked)
        assert refused.status_code == status and why in refused.json()['error']['message']
    assert client.post('/v1/images/generations', json={'model': 'workflow:quick-draft'}).status_code == 401


def test_a_caller_without_every_model_cannot_see_or_run_the_workflow(api, monkeypatch):
    client, users, engine, *_ = api
    async def fewer(self, user):
        return [] if user.name == 'pat' else copy.deepcopy(MODELS)
    monkeypatch.setattr(Front, 'graph_models', fewer)
    key = {'authorization': 'Bearer ' + users.new_key('pat')}
    assert client.get('/v1/workflows', headers=key).json()['data'] == []
    assert client.post('/v1/images/generations', headers=key, json={'model': 'workflow:quick-draft'}).status_code == 404
    assert engine.sent == []


def test_test_runs_stream_drafts_and_saved_versions_for_managers(api):
    client, users, engine, site, saved = api
    draft = sample('Sampler shoot-out')
    answer = site.post('/mirrorwall-api/graphs/draft/run', json={'document': draft, 'inputs': {'prompt.text': 'A heron'}})
    assert answer.status_code == 201, answer.text
    state = poll(site, answer.json()['id'])
    assert state['status'] == 'completed' and state['plan']['steps'] == 4
    assert len(state['outputs']) == 4 and all(event['asset_url'] and 'b64_json' not in event for event in state['outputs'])
    assert all(step['state'] == 'completed' for step in state['steps'].values()) and engine.sent[-1][1]['prompt'] == 'A heron'
    failing = copy.deepcopy(draft)
    next(node for node in failing['nodes'] if node['id'] == 'model')['values']['model'] = 'missing'
    refused = site.post('/mirrorwall-api/graphs/draft/run', json={'document': failing})
    assert refused.status_code == 400 and 'every model' in refused.json()['error']['message']
    engine.fail = lambda body: body.get('sampler') == 'dpm++2m'
    state = poll(site, site.post('/mirrorwall-api/graphs/draft/run', json={'document': draft}).json()['id'])
    assert state['status'] == 'failed' and state['error'] == 'DPM++ 2M · Karras: Fixture failure' and len(state['outputs']) == 0
    engine.fail = None
    answer = site.post(f"/mirrorwall-api/graphs/{saved['id']}/run", json={'version': 1, 'inputs': {'base.steps': 40}})
    assert answer.status_code == 400 and 'from 4 to 12' in answer.json()['error']['message']
    run_id = answer.json().get('id') if answer.status_code == 201 else None
    sign_in(site, 'pat')
    assert site.post(f"/mirrorwall-api/graphs/{saved['id']}/run", json={'version': 1}).status_code == 403
    assert site.get('/mirrorwall-api/graph-runs/' + str(run_id)).status_code in (403, 404)


def poll(site, identifier):
    import time
    for _ in range(200):
        state = site.get('/mirrorwall-api/graph-runs/' + identifier).json()
        if state['status'] != 'running':
            return state
        time.sleep(.02)
    raise AssertionError('the test run did not finish')


def test_references_are_prepared_at_no_more_than_the_smallest_ones_size():
    document = sample('Klein multi-reference edit')
    _, engine = run(document, {'image': [png(768, 768), png(1024, 1024), png(512, 384)]})
    body = engine.sent[0][1]
    assert len(body['references']) == 3 and body['reference_megapixels'] == pytest.approx(.19)
    assert body['size'] == '768x768'  # no size step: the first reference sets it
