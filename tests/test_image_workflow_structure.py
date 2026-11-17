"""Phase 5 of image workflows: ControlNet components and control images, and the tiled detail pass with seam blending."""

import base64
import copy
import json
import struct
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from mirrorwall import components, graph, graph_run, model
from mirrorwall.requests import checked_request
from mirrorwall.runtime import create_app, native_command, png_bytes, unsupported_by_runtime
from test_image_workflow_run import Engine, run, sample
from test_sdcpp import FakeNative, private_job

MODELS = [{'id': 'sdxl-base', 'family': 'sdxl', 'control': True}, {'id': 'klein-4b', 'family': 'flux2-klein'}]


def encoded(width, height, colour='blue'):
    return base64.b64encode(png_bytes(Image.new('RGB', (width, height), colour))).decode()


def safetensors(path, shapes):
    header = json.dumps({name: {'dtype': 'F16', 'shape': list(shape), 'data_offsets': [0, 0]} for name, shape in shapes.items()}).encode()
    path.write_bytes(struct.pack('<Q', len(header)) + header)
    return path


@pytest.fixture
def sdxl(tmp_path):
    files = {name: tmp_path / f'{name}.safetensors' for name in ('checkpoint', 'controlnet')}
    for one in files.values():
        one.write_bytes(b'weights')
    return {'engine': 'sdcpp', 'flags': {'model': str(files['checkpoint']), 'ctx-size': 0}, 'gpus': 1, 'alone': True,
            'sdcpp': {'family': 'sdxl', 'components': {'control_net': str(files['controlnet'])}, 'split_mode': 'single'}}


def test_controlnet_files_are_recognized_from_their_headers_and_offered_to_sdxl_only(tmp_path):
    diffusers = safetensors(tmp_path / 'canny.safetensors', {'controlnet_cond_embedding.conv_in.weight': (16, 3, 3, 3),
                                                              'add_embedding.linear_1.weight': (1280, 2816),
                                                              'controlnet_down_blocks.0.weight': (320, 320, 1, 1)})
    original = safetensors(tmp_path / 'sd15.safetensors', {'control_model.input_hint_block.0.weight': (16, 3, 3, 3),
                                                            'control_model.zero_convs.0.0.weight': (320, 320, 1, 1)})
    assert components.classify(components.shapes_of(diffusers)) == ('controlnet', None, 'sdxl-controlnet')
    assert components.classify(components.shapes_of(original)) == ('controlnet', None, 'sd1-controlnet')
    assert components.needs('sdxl', 'checkpoint')['control_net'][:3] == (False, 'controlnet', 'sdxl-controlnet')
    assert 'control_net' not in components.needs('flux2-klein', 'klein-4b')
    checkpoint = safetensors(tmp_path / 'sdxl.safetensors', {
        'model.diffusion_model.label_emb.0.0.weight': (1280, 2816), 'conditioner.embedders.1.model.ln_final.weight': (1280,),
        'first_stage_model.decoder.conv_in.weight': (512, 4, 3, 3)})
    found = components.check({'flags': {'model': str(checkpoint)}, 'sdcpp': {'family': 'sdxl', 'components': {'control_net': str(diffusers)}}}, str(tmp_path))
    slot = next(one for one in found['slots'] if one['slot'] == 'control_net')
    assert slot['status'] == 'ok' and slot['need'] == 'optional' and found['ready']
    wrong = components.check({'flags': {'model': str(checkpoint)}, 'sdcpp': {'family': 'sdxl', 'components': {'control_net': str(original)}}}, str(tmp_path))
    assert next(one for one in wrong['slots'] if one['slot'] == 'control_net')['status'] == 'wrong' and not wrong['ready']


def test_a_controlnet_loads_only_on_an_exclusive_sdxl_entry_beside_the_unet(sdxl):
    model.check_model('sdxl-control', sdxl, ('CUDA0',))
    command = native_command(private_job(sdxl), 12345)
    assert command[command.index('--control-net') + 1] == sdxl['sdcpp']['components']['control_net']
    assert command[command.index('--backend') + 1] == 'diffusion=cuda0,te=cuda0,vae=cuda0,controlnet=cuda0'
    assert command[command.index('--params-backend') + 1] == 'diffusion=disk,te=disk,vae=disk,controlnet=disk'
    assert 'control_net' in model.files_of(sdxl)
    with pytest.raises(ValueError, match='exclusive model entry'):
        model.check_model('sdxl-control', {**sdxl, 'alone': False}, ('CUDA0',))
    flux = copy.deepcopy(sdxl)
    flux['sdcpp'].update(family='flux2-klein', components={**flux['sdcpp']['components'], 'vae': flux['flags']['model'], 'text_encoder': flux['flags']['model']})
    with pytest.raises(ValueError, match='applies to SDXL'):
        model.check_model('flux-control', flux, ('CUDA0',))


def test_control_requests_are_bounded_and_need_a_model_with_a_controlnet(sdxl):
    image = encoded(64, 64)
    resolved = checked_request({'model': 'm', 'prompt': 'A house', 'control': {'image': image, 'strength': .7, 'edges': True}}, sdxl)
    assert resolved['control'] == {'image': image, 'strength': .7, 'edges': True}
    assert checked_request({'model': 'm', 'prompt': 'A house', 'control': {'image': image}}, sdxl)['control']['strength'] == .9
    for bad, why in (({'image': image, 'strength': 3}, 'strength'), ({'image': image, 'edges': 'yes'}, 'edges'),
                     ({'strength': .5}, 'takes image'), ({'image': image, 'path': '/x'}, 'takes image'), ({'image': 'nope!'}, '')):
        with pytest.raises(ValueError, match=why):
            checked_request({'model': 'm', 'prompt': 'A house', 'control': bad}, sdxl)
    plain = copy.deepcopy(sdxl)
    plain['sdcpp']['components'] = {}
    with pytest.raises(ValueError, match='has no ControlNet'):
        checked_request({'model': 'm', 'prompt': 'A house', 'control': {'image': image}}, plain)
    with pytest.raises(ValueError, match='cannot be combined'):
        checked_request({'model': 'm', 'image': image, 'control': {'image': image}, 'upscale_with': {'upscaler': 'a.pth'}}, sdxl)


def test_the_control_image_reaches_the_native_request_and_its_bytes_stay_out_of_the_settings(sdxl):
    native, job = FakeNative(), private_job(sdxl)
    with TestClient(create_app(job, native)) as client:
        headers = {'authorization': 'Bearer ' + job['token']}
        answer = client.post('/v1/images/generations', headers=headers, json={
            'model': 'sdxl-control', 'prompt': 'A house', 'size': '512x512', 'steps': 4, 'control': {'image': encoded(80, 60), 'strength': .6, 'edges': True}})
        assert answer.status_code == 200, answer.text
        payload = native.payloads[0]
        assert payload['control_strength'] == .6 and base64.b64decode(payload['control_image'])
        assert payload['image_preprocess'] == 'target=control,mode=crop-resize,filter=lanczos,canny=true'
        assert answer.json()['data'][0]['settings']['control'] == {'strength': .6, 'edges': True}
        # A start image and a depth map given as is: both prepared, with no edge detection.
        client.post('/v1/images/edits', headers=headers, json={
            'model': 'sdxl-control', 'prompt': 'A house', 'size': '512x512', 'steps': 4, 'image': encoded(512, 512),
            'reference_megapixels': .262144, 'control': {'image': encoded(512, 512), 'edges': False}})
        assert native.payloads[1]['init_image'] and native.payloads[1]['image_preprocess'] == 'target=control,mode=crop-resize,filter=lanczos'
    assert unsupported_by_runtime({'control_image': 'x'}, {'features': {'controlnet': False}}) == 'a ControlNet control image'
    assert unsupported_by_runtime({'control_image': 'x'}, {'features': {'controlnet': True}}) is None


def test_an_sdxl_start_image_may_be_prepared_at_the_output_size_beyond_the_reference_size(sdxl):
    settings = copy.deepcopy(sdxl)
    settings['sdcpp']['reference_megapixels'] = 1
    image = encoded(64, 64)
    assert checked_request({'model': 'm', 'prompt': 'x', 'size': '1024x1024', 'image': image, 'reference_megapixels': 1.048576}, settings)
    with pytest.raises(ValueError, match='reference-size limit'):
        checked_request({'model': 'm', 'prompt': 'x', 'size': '1024x1024', 'image': image, 'reference_megapixels': 1.5}, settings)


def test_a_sampler_with_a_control_image_sends_it_and_records_only_its_settings():
    document = sample('Structure-guided generation')
    events, engine = run(document, {'prompt': 'A cabin', 'image': encoded(300, 200, 'white')}, models=MODELS)
    path, body = engine.sent[0]
    assert path == '/v1/images/generations' and body['control']['strength'] == .7 and body['control']['edges'] is True
    assert Image.open(__import__('io').BytesIO(base64.b64decode(body['control']['image']))).size == (300, 200)
    output = next(event for event in events if event['type'] == 'workflow.output')
    assert output['stages']['base']['control'] == {'strength': .7, 'edges': True}
    # A Flux model has no ControlNet here.
    flux = copy.deepcopy(document)
    next(node for node in flux['nodes'] if node['id'] == 'model')['values']['model'] = 'klein-4b'
    assert any('no ControlNet' in one['text'] for one in graph.problems(flux, MODELS))


def test_tiles_cover_the_image_with_aligned_overlapping_tiles_flush_with_the_edges():
    boxes = graph_run.tiles(2048, 1500, 1024, 64)
    lefts, tops = sorted({box[0] for box in boxes}), sorted({box[1] for box in boxes})
    assert lefts == [0, 960, 1024] and tops == [0, 476] and len(boxes) == 6
    assert all((box[2] - box[0]) % 16 == 0 and (box[3] - box[1]) % 16 == 0 and box[2] <= 2048 and box[3] <= 1500 for box in boxes)
    assert max(box[2] for box in boxes) == 2048 and max(box[3] for box in boxes) == 1500
    assert graph_run.tiles(900, 700, 1024, 64) == [(0, 0, 896, 688), (4, 0, 900, 688), (0, 12, 896, 700), (4, 12, 900, 700)]
    assert graph_run.tiles(1024, 1024, 1024, 64) == [(0, 0, 1024, 1024)]
    with pytest.raises(graph_run.RunError, match='at least 256'):
        graph_run.tiles(200, 900, 1024, 64)


def test_blending_fades_each_tile_in_across_the_overlap_and_keeps_the_rest_exact():
    base = Image.new('RGB', (400, 256), (0, 0, 0))
    left, right = (0, 0, 256, 256), (144, 0, 400, 256)
    result = graph_run.blend(base, [(left, Image.new('RGB', (256, 256), (200, 0, 0))), (right, Image.new('RGB', (256, 256), (0, 0, 200)))], 64)
    assert result.getpixel((100, 128)) == (200, 0, 0) and result.getpixel((300, 128)) == (0, 0, 200)
    across = [result.getpixel((x, 128))[2] for x in range(144, 208)]
    assert across == sorted(across) and 0 < across[0] < 10 and across[-1] > 190  # a smooth ramp, not a hard seam
    assert result.getpixel((208, 128)) == (0, 0, 200)


def test_tiled_detail_runs_one_low_strength_pass_per_tile_and_returns_the_full_size_image():
    document = sample('Tiled 4× upscale')
    events, engine = run(document, {}, models=MODELS, upscalers=[{'id': '4x-UltraSharp.pth', 'nodes': ['atlas']}])
    tiles = [body for path, body in engine.sent if 'strength' in body and body.get('size') and 'upscale_with' not in body and path == '/v1/images/edits']
    assert len(tiles) == 9 and {body['size'] for body in tiles} == {'1024x1024'}
    assert {(body['strength'], body['steps'], body['sampler'], body['scheduler'], body['seed']) for body in tiles} == {(.3, 20, 'dpm++2m', 'karras', 1234)}
    assert all(body['reference_megapixels'] == 1.048576 for body in tiles)
    progress = [event['tile'] for event in events if event.get('stage') == 'tiles']
    assert progress == list(range(1, 10))
    done = next(event for event in events if event['type'] == 'workflow.step' and event['step'] == 'tiles' and event['state'] == 'completed')
    assert done['tiles'] == 9 and (done['width'], done['height']) == (2048, 2048)
    output = next(event for event in events if event['type'] == 'workflow.output')
    assert (output['width'], output['height']) == (4096, 4096) and output['stages']['tiles']['tiles'] == 9
    # Too many tiles is refused before any is sent.
    many = copy.deepcopy(document)
    next(node for node in many['nodes'] if node['id'] == 'tiles')['values']['tile'] = 512
    next(node for node in many['nodes'] if node['id'] == 'up1')['values']['factor'] = 4
    with pytest.raises(graph_run.RunError, match='at most 64 are allowed'):
        run(many, {}, models=MODELS, upscalers=[{'id': '4x-UltraSharp.pth', 'nodes': ['atlas']}])


def test_tiled_detail_needs_an_sdxl_model_and_a_sensible_overlap():
    document = sample('Tiled 4× upscale')
    tiles = next(node for node in document['nodes'] if node['id'] == 'tiles')
    tiles['values']['overlap'] = 256
    tiles['values']['tile'] = 512
    assert any('less than half' in one['text'] for one in graph.problems(document, MODELS))
    next(node for node in document['nodes'] if node['id'] == 'model')['values']['model'] = 'klein-4b'
    assert any('tiled detail needs an SDXL model' in one['text'] for one in graph.problems(document, MODELS))
