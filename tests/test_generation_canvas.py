"""Canvas studio uses real authenticated assets and the ordinary image job contract."""
import base64
import io
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from mirrorwall.requests import checked_request
from mirrorwall.runtime import create_app, png_bytes
from test_console_browser import browser_context, playwright
from test_image_studio import studio  # noqa: F401
from test_sdcpp import FakeNative, image_settings, private_job  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401


def picture(size=(512, 320), color='blue'):
    return png_bytes(Image.new('RGB', size, color))


@pytest.fixture
def canvas(studio, monkeypatch):
    server, wire, users, library = studio
    original = wire.front.state.mirrorwall.catalogue

    async def catalogue(user):
        rows = await original(user)
        rows[0]['image_workflow'].update(upscaling=True, reference_megapixels=.5)
        rows[0]['image_workflow']['limits'].update(max_width=1024, max_height=1024,
                                                  max_pixels=1048576, max_upscale=4, max_steps=30)
        rows[0]['image_workflow']['loras']={'texture':{'scale':.5}}
        return rows

    monkeypatch.setattr(wire.front.state.mirrorwall, 'catalogue', catalogue)
    owner = users.by_name('jordan').identity
    store = wire.front.state.mirrorwall.store
    store.save(owner, picture(), kind='output', filename='result.png', metadata={
        'seed': 42, 'model_id': library.identity('sketch'),
        'settings': {'model': 'sketch', 'prompt': 'A blue landscape', 'width': 512,
                     'height': 320, 'steps': 28, 'seed': 42, 'sampler': 'euler', 'scheduler': 'discrete'}})
    return server, wire, users, library


@pytest.mark.parametrize('width', [1600, 390, 320])
def test_canvas_drafts_upload_resize_and_existing_job_submission(canvas, width):
    server, wire, users, _ = canvas
    submitted = []

    def capture(route, path):
        if path == '/mirrorwall-api/jobs' and route.request.method == 'POST':
            asked = route.request.post_data_json
            wire.front.state.mirrorwall.lines(asked)
            wire.front.state.mirrorwall.fits({'id': 'sketch', 'image_workflow': {
                'family': 'sdxl', 'exclusive': True, 'upscaling': True,
                'reference_megapixels': .5, 'limits': {'max_references': 1, 'max_upscale': 4}}},
                asked, users.by_name('jordan'))
            submitted.append(asked)
            route.fulfill(content_type='application/json', body='{"id":"accepted-job"}')
            return True

    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server, capture=capture)
        page.set_viewport_size({'width': width, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/chat/images')
        playwright.expect(page.locator('#mw-model')).to_have_value('sketch')
        page.locator('#mw-prompt').fill('A private landscape draft')
        page.locator('#mw-negative').fill('noise')
        page.locator('#mw-advanced > summary').click()
        page.locator('[data-lora=texture]').fill('0.9')
        page.locator('#mw-reference-box > summary').click()
        page.locator('#mw-upload').set_input_files({'name': 'source.png', 'mimeType': 'image/png', 'buffer': picture()})
        playwright.expect(page.locator('#mw-references > div')).to_have_count(1)
        page.locator('#mw-model').select_option('qwen')
        playwright.expect(page.locator('#mw-steps')).to_have_value('50')
        page.locator('#mw-prompt').fill('A typographic poster')
        playwright.expect(page.locator('#mw-negative')).to_have_value('')
        page.locator('#mw-model').select_option('sketch')
        playwright.expect(page.locator('#mw-prompt')).to_have_value('A private landscape draft')
        playwright.expect(page.locator('#mw-negative')).to_have_value('noise')
        playwright.expect(page.locator('[data-lora=texture]')).to_have_value('0.9')
        playwright.expect(page.locator('#mw-references > div')).to_have_count(1)
        page.locator('#mw-generate').click()
        playwright.expect(page.locator('#mw-generate')).to_be_enabled()
        assert submitted[0]['request']['negative_prompt'] == 'noise'
        assert submitted[0]['request']['loras'] == {'texture':.9}
        assert 'guidance' not in submitted[0]['request']
        page.locator('[data-mw-tool=resize]').click()
        page.locator('#mw-resize-upload').set_input_files({'name': 'resize.png', 'mimeType': 'image/png', 'buffer': picture()})
        playwright.expect(page.locator('#mw-resize-width')).to_have_value('1024')
        page.locator('#mw-resize-width').fill('1501')
        playwright.expect(page.locator('#mw-resize-height')).to_have_value('938')
        page.locator('#mw-resize-prompt').fill('Preserve composition; refine fabric texture')
        playwright.expect(page.locator('#mw-generate')).to_be_enabled()
        assert page.locator('#mw-form').evaluate('form => form.checkValidity()')
        page.locator('#mw-generate').click()
        playwright.expect(page.locator('#mw-generate')).to_be_enabled()
        request = submitted[-1]['request']
        assert request['detail_resize'] == {'width': 1501, 'height': 938}
        assert request['strength'] == .25 and request['n'] == 1 and request['upscale'] == 1
        assert request['prompt'] == 'Preserve composition; refine fabric texture'
        assert 'negative_prompt' not in request
        assert len(submitted[-1]['references']) == 1 and submitted[-1]['mask'] is None
        assert request['width'] * request['height'] <= 1048576
        page.locator('[data-mw-tool=generate]').click()
        playwright.expect(page.locator('#mw-prompt')).to_have_value('A private landscape draft')
        playwright.expect(page.locator('#mw-negative')).to_have_value('noise')
        page.locator('[data-mw-view=library]').click()
        page.locator('#mw-favorite').click()
        page.locator('#mw-library-filter').select_option('favorites')
        playwright.expect(page.locator('#mw-thumbnails > button')).to_have_count(1)
        page.locator('[data-mw-view=queue]').click()
        playwright.expect(page.locator('#mw-queue-empty')).to_be_visible()
        page.locator('[data-mw-view=generate]').click()
        playwright.expect(page.locator('#mw-prompt')).to_have_value('A private landscape draft')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        folder = Path('build/generation-canvas')
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'canvas-{width}.png'), full_page=True)
        assert not errors
        browser.close()


def test_canvas_poll_error_recovery_sweep_validation_and_empty_model(canvas, monkeypatch):
    server, wire, _, _ = canvas
    fail = [False]
    jobs = []

    def capture(route, path):
        if path == '/mirrorwall-api/state' and fail[0]:
            route.fulfill(status=503, content_type='application/json', body='{"error":{"message":"Inventory unavailable"}}')
            return True
        if path == '/mirrorwall-api/jobs' and route.request.method == 'POST':
            jobs.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body='{"id":"accepted-job"}')
            return True

    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server, capture=capture)
        page.goto('http://console.test/chat/images')
        playwright.expect(page.locator('#mw-model')).to_have_value('sketch')
        page.locator('#mw-prompt').fill('Keep this draft')
        fail[0] = True
        playwright.expect(page.locator('#mw-message')).to_have_text('Inventory unavailable', timeout=5000)
        fail[0] = False
        playwright.expect(page.locator('#mw-message')).to_be_hidden(timeout=5000)
        playwright.expect(page.locator('#mw-prompt')).to_have_value('Keep this draft')
        page.locator('[data-mw-tool=sweep]').click()
        page.locator('#mw-sweep-values').fill('42, invalid')
        page.locator('#mw-generate').click()
        playwright.expect(page.locator('#mw-message')).to_contain_text('finite numbers')
        assert not jobs
        page.locator('#mw-sweep-values').fill('42, 43')
        page.locator('#mw-generate').click()
        playwright.expect(page.locator('#mw-generate')).to_be_enabled()
        assert jobs[-1]['sweep'] == {'seed': [42, 43]}
        async def unavailable(user):
            return []
        monkeypatch.setattr(wire.front.state.mirrorwall, 'catalogue', unavailable)
        playwright.expect(page.locator('#mw-model option')).to_have_count(0, timeout=5000)
        playwright.expect(page.locator('#mw-generate')).to_be_disabled()
        playwright.expect(page.locator('#mw-availability')).to_contain_text('No available image model')
        playwright.expect(page.locator('#mw-prompt')).to_have_value('Keep this draft')
        browser.close()


def test_detail_resize_forwards_source_to_detail_pass_and_exact_final_size(image_settings, monkeypatch):
    from mirrorwall import runtime
    settings = deepcopy(image_settings)
    settings['sdcpp'].update(family='sdxl', upscaler=settings['flags']['model'])
    source = picture((512, 512))
    native = FakeNative()
    calls = []

    async def upscale(job, image, target, folder, label):
        assert native.stops > 0
        calls.append((image.getpixel((0, 0)), target, label))
        return image.resize(target)

    monkeypatch.setattr(runtime, 'upscale', upscale)
    job = private_job(settings)
    asked = {'model': 'sdxl', 'prompt': 'Refine the texture', 'width': 256, 'height': 256,
             'seed': 123, 'strength': .25, 'references': [base64.b64encode(source).decode()],
             'detail_resize': {'width': 501, 'height': 333}, 'upscale': 1}
    with TestClient(create_app(job, native)) as client:
        response = client.post('/v1/images/edits', headers={'authorization': 'Bearer ' + job['token']}, json=asked)
    assert response.status_code == 200, response.text
    answer = response.json()['data'][0]
    image = Image.open(io.BytesIO(base64.b64decode(answer['b64_json'])))
    assert image.size == (501, 333)
    assert answer['settings']['detail_resize'] == {'width': 501, 'height': 333}
    assert answer['seed'] == 123 and native.payloads[0]['strength'] == .25
    assert Image.open(io.BytesIO(base64.b64decode(native.payloads[0]['init_image']))).size == (256, 256)
    assert calls == [((255, 0, 0), (501, 333), 'detail-output-0')]
    assert Image.open(io.BytesIO(source)).getpixel((0, 0)) == (0, 0, 255)


@pytest.mark.parametrize('changes', [
    {'detail_resize': {'width': True, 'height': 512}},
    {'detail_resize': {'width': 1025, 'height': 512}},
    {'detail_resize': {'width': 8192, 'height': 8192}},
    {'detail_resize': {'width': 512, 'height': 512, 'extra': 1}},
    {'references': []}, {'n': 2}, {'upscale': 2}, {'mask': 'eA=='},
])
def test_detail_resize_rejects_unsupported_requests_before_native_work(image_settings, changes):
    settings = deepcopy(image_settings)
    settings['sdcpp'].update(family='sdxl', upscaler=settings['flags']['model'])
    asked = {'model': 'sdxl', 'prompt': 'Refine', 'width': 256, 'height': 256,
             'references': [base64.b64encode(picture()).decode()],
             'detail_resize': {'width': 512, 'height': 512}, **changes}
    with pytest.raises(ValueError):
        checked_request(asked, settings)


def test_detail_resize_upscaler_failure_is_not_plain_interpolation(image_settings, monkeypatch):
    from mirrorwall import runtime
    settings = deepcopy(image_settings)
    settings['sdcpp'].update(family='sdxl', upscaler=settings['flags']['model'])

    async def fail(*args):
        raise RuntimeError('AI upscaler failed')

    monkeypatch.setattr(runtime, 'upscale', fail)
    job = private_job(settings)
    asked = {'model': 'sdxl', 'prompt': 'Refine', 'width': 256, 'height': 256,
             'references': [base64.b64encode(picture()).decode()],
             'detail_resize': {'width': 512, 'height': 512}}
    with TestClient(create_app(job, FakeNative())) as client:
        answer = client.post('/v1/images/edits', headers={'authorization': 'Bearer ' + job['token']}, json=asked)
    assert answer.status_code == 502 and 'AI upscaler failed' in answer.json()['error']['message']
    for family, exclusive in [('flux2-dev', True), ('qwen-image', True), ('sdxl', False)]:
        settings['sdcpp']['family'] = family
        settings['alone'] = exclusive
        with pytest.raises(ValueError):
            checked_request(asked, settings)


def test_ordinary_multi_image_output_upscale_is_preserved(image_settings, monkeypatch):
    from mirrorwall import runtime
    settings = deepcopy(image_settings)
    settings['sdcpp']['upscaler'] = settings['flags']['model']

    async def upscale(job, image, target, folder, label):
        return image.resize(target)

    monkeypatch.setattr(runtime, 'upscale', upscale)
    job = private_job(settings)
    with TestClient(create_app(job, FakeNative())) as client:
        answer = client.post('/v1/images/generations', headers={'authorization': 'Bearer ' + job['token']},
                             json={'model': 'flux', 'prompt': 'An ordinary image batch',
                                   'width': 256, 'height': 256, 'n': 2, 'upscale': 2})
    assert answer.status_code == 200, answer.text
    assert [(row['width'], row['height']) for row in answer.json()['data']] == [(512, 512), (512, 512)]


def test_detail_resize_front_keeps_ownership_and_temporary_policy(canvas, monkeypatch):
    server, wire, users, library = canvas
    front = wire.front.state.mirrorwall

    async def model_ids(user):
        return {'sketch': library.identity('sketch')}

    monkeypatch.setattr(front.batches, 'model_ids', model_ids)
    monkeypatch.setattr(front.batches, 'work', lambda *args: None)
    source = server.post('/mirrorwall-api/assets', json={
        'filename': 'source.png', 'b64_json': base64.b64encode(picture()).decode()}).json()
    asked = {'request': {'model': 'sketch', 'prompt': 'Refine texture', 'width': 512, 'height': 320,
                        'strength': .25, 'detail_resize': {'width': 1024, 'height': 640}},
             'references': [source['id']]}
    response = server.post('/mirrorwall-api/jobs', json=asked)
    assert response.status_code == 201, response.text
    assert front.store.jobs(users.by_name('jordan').identity)[0]['id'] == response.json()['id']
    sign_in(server, 'pat')
    refused = server.post('/mirrorwall-api/jobs', json=asked)
    assert refused.status_code == 400 and 'of yours' in refused.json()['error']['message']
    sign_in(server, 'jordan')
    temporary = front.store.save(users.by_name('jordan').identity, picture(), kind='reference',
                                 filename='temporary.png', temporary=True)
    refused = server.post('/mirrorwall-api/jobs', json={**asked, 'references': [temporary['id']]})
    assert refused.status_code == 400 and 'Temporary references' in refused.json()['error']['message']
