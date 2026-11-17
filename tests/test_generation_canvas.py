"""Canvas studio uses real authenticated assets and the ordinary image job contract."""
import base64
import io
import json
import zipfile
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
        page.locator('#mw-model').select_option('moss')
        playwright.expect(page.locator('#mw-steps')).to_have_value('50')
        playwright.expect(page.locator('#mw-cfg')).to_have_value('3')
        playwright.expect(page.locator('#mw-negative')).to_be_enabled()
        playwright.expect(page.locator('#mw-prompt-help')).to_contain_text('text only')
        playwright.expect(page.locator('#mw-width')).to_have_attribute('step', '16')
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
        playwright.expect(page.locator('#mw-thumbnails .mw-thumb')).to_have_count(1)
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


def test_canvas_two_click_delete_resets_when_image_changes_or_escape_is_pressed(canvas):
    server, wire, users, _ = canvas
    store = wire.front.state.mirrorwall.store
    owner = users.by_name('jordan').identity
    original = store.listing(owner, kind='output')[0]
    added = store.save(owner, picture(color='red'), kind='output', metadata={'seed': 43})
    reference = store.save(owner, picture(), kind='reference', filename='saved-reference.png')
    deleted = []
    def capture(route, path):
        if route.request.method == 'DELETE':
            deleted.append(path.rsplit('/', 1)[-1])
        return False
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server, capture=capture)
        page.goto('http://console.test/chat/images')
        page.locator('.ig-result-details').evaluate('(details) => details.open=true')
        button = page.locator('#mw-delete')
        button.click()
        playwright.expect(button).to_have_text('Delete this image?')
        assert not deleted and not page.locator('#danger-confirmation').is_visible()
        page.locator(f'.mw-thumb[data-id="{original["id"]}"]').click()
        playwright.expect(button).to_have_text('Delete image')
        button.click(); page.keyboard.press('Escape')
        playwright.expect(button).to_have_text('Delete image')
        assert not deleted
        button.click(); button.click()
        playwright.expect(page.locator(f'.mw-thumb[data-id="{original["id"]}"]')).to_have_count(0)
        assert deleted == [original['id']]
        assert store.asset(owner, added['id']) is not None
        page.locator('[data-mw-tool=reference]').click()
        page.locator('#mw-library').click()
        remove_reference = page.locator('#mw-dialog [data-confirm-two]')
        remove_reference.click()
        assert store.asset(owner, reference['id']) is not None
        assert not page.locator('#danger-confirmation').is_visible()
        remove_reference.click()
        playwright.expect(remove_reference).to_have_count(0)
        assert store.asset(owner, reference['id']) is None
        browser.close()


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_gallery_thumbnail_rail_keyboard_filter_download_and_delete(canvas, width):
    server, wire, users, _ = canvas
    store = wire.front.state.mirrorwall.store
    owner = users.by_name('jordan').identity
    for seed in range(12):
        store.save(owner, picture(color='red'), kind='output', metadata={'seed': seed})
    rows = store.listing(owner, kind='output')
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/chat/images')
        page.locator('[data-mw-view=gallery]').click()
        thumbs = page.locator('#mw-thumbnails .mw-thumb')
        playwright.expect(thumbs).to_have_count(len(rows))
        viewer = page.locator('.ig-viewer').bounding_box()
        rail = page.locator('.ig-history').bounding_box()
        artboard = page.locator('#mw-canvas').bounding_box()
        header_height = page.locator('body > header').bounding_box()['height']
        playwright.expect(page.locator('#mw-page-header .page-title')).to_be_hidden()
        playwright.expect(page.locator('#mw-viewer-heading')).to_be_hidden()
        playwright.expect(page.locator('.ig-history .section-title')).to_be_hidden()
        assert abs(artboard['height'] - (1000 - header_height - 32)) < 2
        assert header_height <= artboard['y'] <= header_height + 18
        assert page.locator('.ig-subnav').bounding_box()['y'] >= artboard['y'] + artboard['height']
        assert page.locator('.ig-viewer-bar').bounding_box()['y'] >= artboard['y'] + artboard['height']
        assert page.locator('#mw-page-header .page-actions').bounding_box()['y'] >= artboard['y'] + artboard['height']
        assert rail['x'] >= viewer['x'] + viewer['width']
        assert rail['width'] <= 140
        assert page.locator('#mw-thumbnails').evaluate('el => el.scrollHeight > el.clientHeight')
        page.keyboard.press('ArrowDown')
        playwright.expect(thumbs.nth(1)).to_have_attribute('aria-pressed', 'true')
        page.keyboard.press('ArrowUp')
        playwright.expect(thumbs.first).to_have_attribute('aria-pressed', 'true')
        page.keyboard.press('ArrowLeft')
        playwright.expect(thumbs.last).to_have_attribute('aria-pressed', 'true')
        assert page.locator('#mw-thumbnails').evaluate('el => el.scrollTop > 0')
        for _ in range(2):
            page.keyboard.press('ArrowRight')
        playwright.expect(thumbs.nth(1)).to_have_attribute('aria-pressed', 'true')
        page.locator('#mw-favorite').click()
        page.locator('#mw-library-filter').select_option('favorites')
        playwright.expect(thumbs).to_have_count(1)
        thumbs.first.click()
        page.keyboard.press('ArrowRight')
        playwright.expect(thumbs.first).to_have_attribute('aria-pressed', 'true')
        # The browser bridge omits attachment headers; verify the real download response.
        download = server.get(page.locator('#mw-download').get_attribute('href'))
        assert download.content == store.content(owner, rows[1]['id'])
        assert download.headers['content-disposition'].startswith('attachment;')
        assert page.locator('#mw-delete').bounding_box()['y'] > page.locator('#mw-canvas').bounding_box()['y']
        thumbs.first.focus()
        page.keyboard.press('Delete')
        playwright.expect(page.locator('#mw-delete')).to_have_text('Delete this image?')
        page.dispatch_event('body', 'keydown', {'key': 'Delete', 'repeat': True})
        assert store.asset(owner, rows[1]['id']) is not None
        page.keyboard.press('Escape')
        playwright.expect(page.locator('#mw-delete')).to_have_text('Delete image')
        page.keyboard.press('Delete')
        page.keyboard.press('Delete')
        playwright.expect(thumbs).to_have_count(0)
        assert store.asset(owner, rows[1]['id']) is None
        playwright.expect(page.locator('#mw-empty')).to_be_visible()
        page.locator('#mw-library-filter').select_option('all')
        thumbs.first.click()
        page.locator('[data-mw-view=generate]').click()
        playwright.expect(page.locator('#mw-page-header .page-title')).to_be_visible()
        page.locator('#mw-prompt').fill('A draft to keep')
        page.keyboard.press('ArrowRight')
        page.keyboard.press('Delete')
        playwright.expect(page.locator('#mw-prompt')).to_have_value('A draft to keep')
        playwright.expect(thumbs.first).to_have_attribute('aria-pressed', 'true')
        page.locator('[data-mw-view=gallery]').click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        folder = Path('build/generation-canvas')
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'gallery-{width}.png'), full_page=True)
        thumbs.last.click()
        focused_id = thumbs.last.get_attribute('data-id')
        scroll = page.locator('#mw-thumbnails').evaluate('el => el.scrollTop')
        store.save(owner, picture(color='green'), kind='output', metadata={'seed': 99})
        playwright.expect(thumbs).to_have_count(len(rows), timeout=5000)
        playwright.expect(page.locator('.mw-thumb[aria-pressed=true]')).to_have_attribute('data-id', focused_id)
        assert page.evaluate('document.activeElement.dataset.id') == focused_id
        assert abs(page.locator('#mw-thumbnails').evaluate('el => el.scrollTop') - scroll) < 2
        assert not errors
        browser.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_main_canvas_previews_switch_and_compact_queue_survive_polling(canvas, width):
    server, wire, users, _ = canvas
    store = wire.front.state.mirrorwall.store
    owner = users.by_name('jordan').identity
    saved = store.listing(owner, kind='output')[0]
    preview = base64.b64encode(picture(color='green')).decode()
    jobs = [
        {'id': 'live', 'model': 'sketch', 'status': 'in_progress',
         'request_counts': {'total': 3, 'completed': 1},
         'progress': {'stage': 'sampling', 'step': 2, 'total_steps': 28, 'b64_json': preview}},
        {'id': 'paused', 'model': 'A very long model name that must fit on a single row',
         'status': 'paused', 'request_counts': {'total': 20, 'completed': 2}},
        {'id': 'failed', 'model': 'sketch', 'status': 'failed',
         'error': 'An extended error message that must remain available without expanding the queue row.'},
        {'id': 'queued', 'model': 'sketch', 'status': 'in_progress', 'progress': {'stage': 'queued'},
         'request_counts': {'total': 2, 'completed': 0}},
    ]
    controls = []

    def capture(route, path):
        if path == '/mirrorwall-api/state':
            data = server.get(path).json()
            data['jobs'] = jobs
            route.fulfill(content_type='application/json', body=json.dumps(data))
            return True
        if path.startswith('/mirrorwall-api/jobs/'):
            controls.append((path, route.request.post_data_json))
            route.fulfill(content_type='application/json', body='{}')
            return True

    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server, capture=capture)
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto('http://console.test/chat/images')
        playwright.expect(page.locator('#mw-preview-label')).to_contain_text('step 2 of 28')
        playwright.expect(page.locator('#mw-large')).to_have_attribute('src', 'data:image/png;base64,' + preview)
        playwright.expect(page.locator('#mw-delete')).to_be_disabled()
        page.locator('#mw-canvas').click()
        page.keyboard.press('Delete')
        assert store.asset(owner, saved['id']) is not None
        page.locator('#mw-toggle-preview').click()
        playwright.expect(page.locator('#mw-large')).to_have_attribute('src', f'/mirrorwall-api/assets/{saved["id"]}/content')
        jobs[0]['progress']['step'] = 3
        with page.expect_response(lambda response: response.url.endswith('/mirrorwall-api/state')):
            pass
        playwright.expect(page.locator('#mw-preview-label')).to_be_hidden()
        page.locator('#mw-toggle-preview').click()
        playwright.expect(page.locator('#mw-preview-label')).to_contain_text('step 3 of 28')
        page.locator('#mw-preview').uncheck()
        playwright.expect(page.locator('#mw-preview-label')).to_be_hidden()
        playwright.expect(page.locator('#mw-delete')).to_be_enabled()
        page.locator('#mw-preview').check()
        playwright.expect(page.locator('#mw-preview-label')).to_be_visible()
        page.locator('[data-mw-view=queue]').click()
        playwright.expect(page.locator('#mw-preview-label')).to_be_visible()
        queue_rows = page.locator('#mw-jobs > .mw-job')
        playwright.expect(queue_rows).to_have_count(4)
        playwright.expect(queue_rows.nth(3).locator('[data-state]')).to_have_text('Queued')
        playwright.expect(queue_rows.nth(3).locator('[data-pause]')).to_be_visible()
        playwright.expect(queue_rows.nth(3).locator('[data-stop]')).to_be_visible()
        for row in queue_rows.all():
            assert row.bounding_box()['height'] <= 42
        queue_rows.first.locator('[data-pause]').click()
        assert controls[-1] == ('/mirrorwall-api/jobs/live', {'action': 'pause'})
        queue_rows.nth(1).locator('[data-pause]').click()
        assert controls[-1] == ('/mirrorwall-api/jobs/paused', {'action': 'resume'})
        queue_rows.first.locator('[data-stop]').click()
        assert controls[-1] == ('/mirrorwall-api/jobs/live', {'action': 'cancel'})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        folder = Path('build/generation-canvas')
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'queue-{width}.png'), full_page=True)
        result = store.save(owner, picture(color='green'), kind='output', metadata={'seed': 44, 'job': 'live'})
        jobs[0]['status'] = 'completed'
        playwright.expect(page.locator('#mw-large')).to_have_attribute('src', f'/mirrorwall-api/assets/{result["id"]}/content', timeout=5000)
        playwright.expect(page.locator('#mw-toggle-preview')).to_be_disabled()
        playwright.expect(page.locator('#mw-delete')).to_be_enabled()
        browser.close()


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_canvas_library_selection_zip_and_partial_delete_preserve_remaining_images(canvas, tmp_path, width):
    server, wire, users, _ = canvas
    store = wire.front.state.mirrorwall.store
    owner = users.by_name('jordan').identity
    rows = [store.listing(owner, kind='output')[0],
            store.save(owner, picture(color='red'), kind='output', metadata={'seed': 43}),
            store.save(owner, picture(color='green'), kind='output', metadata={'seed': 44})]
    refuse = [True]
    def capture(route, path):
        if refuse[0] and route.request.method == 'DELETE' and path.endswith('/'+rows[1]['id']):
            route.fulfill(status=409,content_type='application/json',body='{"error":{"message":"This image is used by an unfinished job."}}')
            return True
        return False
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server, capture=capture)
        page.set_viewport_size({'width': width, 'height': 1100})
        page.goto('http://console.test/chat/images')
        page.locator('[data-mw-view=library]').click()
        playwright.expect(page.locator('[data-select-image]')).to_have_count(3)
        page.locator('#mw-select-all').click()
        playwright.expect(page.locator('#mw-selected-count')).to_have_text('3 selected')
        page.locator('#mw-clear-selection').click()
        playwright.expect(page.locator('#mw-delete-selected')).to_be_disabled()
        boxes = [page.locator(f'[data-select-image][data-id="{row["id"]}"]') for row in rows[:2]]
        for box in boxes: box.check()
        with page.expect_response(lambda response: response.url.endswith('/mirrorwall-api/state')):
            pass
        for box in boxes: playwright.expect(box).to_be_checked()
        with page.expect_download() as downloading:
            page.locator('#mw-download-selected').click()
        target = tmp_path/'images.zip'; downloading.value.save_as(target)
        with zipfile.ZipFile(target) as archive:
            assert set(archive.namelist()) == {row['id']+'.png' for row in rows[:2]}
            for row in rows[:2]:
                assert archive.read(row['id']+'.png') == store.content(owner, row['id'])
        button = page.locator('#mw-delete-selected')
        button.click()
        assert store.asset(owner, rows[0]['id']) is not None
        boxes[0].uncheck()
        playwright.expect(button).to_have_text('Delete selected')
        boxes[0].check()
        button.click(); button.click()
        playwright.expect(page.locator('#mw-message')).to_contain_text('1 deleted; 1 could not be deleted')
        playwright.expect(page.locator('#mw-selected-count')).to_have_text('1 selected')
        playwright.expect(boxes[1]).to_be_checked()
        assert store.asset(owner, rows[0]['id']) is None
        assert store.asset(owner, rows[1]['id']) is not None
        assert store.asset(owner, rows[2]['id']) is not None
        assert not page.locator('#danger-confirmation').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        refuse[0] = False
        page.locator('#mw-select-all').click()
        playwright.expect(page.locator('#mw-selected-count')).to_have_text('2 selected')
        button.click(); button.click()
        playwright.expect(page.locator('[data-select-image]')).to_have_count(0)
        playwright.expect(page.locator('#mw-library-empty')).to_be_visible()
        playwright.expect(page.locator('#mw-download-selected')).to_be_disabled()
        playwright.expect(button).to_be_disabled()
        assert store.listing(owner, kind='output') == []
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


@pytest.mark.parametrize('width', [1600, 390])
def test_image_details_list_sampler_and_scheduler_in_a_compact_grid_in_every_view(canvas, width):
    server, *_ = canvas
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/chat/images')
        facts = page.locator('#mw-result-settings')
        for view in ('generate', 'gallery', 'library'):
            page.locator(f'[data-mw-view={view}]').click()
            details = page.locator('.ig-result-details')
            if not details.evaluate('node => node.open'):
                details.locator('summary').click()
            playwright.expect(facts.locator('> div')).to_have_count(7)
            labels = facts.locator('dt').all_text_contents()
            assert labels == ['Model', 'Seed', 'Size', 'Steps', 'Sampler', 'Scheduler', 'Prompt']
            playwright.expect(facts.locator('> div', has_text='Sampler').locator('dd')).to_have_text('euler')
            playwright.expect(facts.locator('> div', has_text='Scheduler').locator('dd')).to_have_text('discrete')
            boxes = facts.locator('> div').evaluate_all('rows => rows.map(row => [row.offsetTop, row.offsetWidth])')
            columns = 3 if width > 1000 else 2
            assert len({top for top, _ in boxes[:columns]}) == 1 and boxes[columns][0] > boxes[0][0]  # rows of three (two when narrow)
            prompt = facts.locator('.ig-fact-wide')
            playwright.expect(prompt).to_contain_text('A blue landscape')
            assert prompt.evaluate('row => row.offsetWidth') >= facts.evaluate('list => list.clientWidth') - 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
