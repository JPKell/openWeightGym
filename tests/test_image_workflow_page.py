"""Live Models → Image workflows page: Stage lanes editor over the real graph API, drafts, versions, publishing and presets."""

import asyncio
import copy

import pytest

from mirrorwall import graph
from mirrorwall.front import Front
from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.navigation import navigation

expect = playwright.expect
MODELS = [{'id': 'klein-4b', 'family': 'flux2-klein'}, {'id': 'sdxl-base', 'family': 'sdxl'}, {'id': 'sdxl-photo', 'family': 'sdxl'}]
UPSCALERS = [{'id': '4x-UltraSharp.pth', 'label': '4x UltraSharp', 'scale': 4, 'nodes': ['atlas']},
             {'id': 'RealESRGAN_x4plus.pth', 'label': 'RealESRGAN x4plus', 'scale': 4, 'nodes': ['atlas', 'orion']}]


@pytest.fixture
def site(desk, monkeypatch):
    server, _, _ = desk

    async def catalogue(self, user, kind):
        return [{'id': m['id'], 'image_workflow': {'family': m['family']}} for m in MODELS]

    async def upscalers(self, user):
        return copy.deepcopy(UPSCALERS)
    async def send(path, body):
        import base64, io
        from PIL import Image
        await asyncio.sleep(.05)
        yield {'type': 'image_generation.progress', 'stage': 'generating'}
        buffer = io.BytesIO(); Image.new('RGB', (64, 64), 'teal').save(buffer, 'PNG')
        yield {'type': 'image_generation.completed', 'b64_json': base64.b64encode(buffer.getvalue()).decode(), 'seed': body.get('seed'),
               'width': 64, 'height': 64, 'node': 'atlas'}
    monkeypatch.setattr(Front, 'media_catalogue', catalogue)
    monkeypatch.setattr(Front, 'graph_upscalers', upscalers)
    monkeypatch.setattr(Front, 'stage_sender', lambda self, user, headers: send)
    return server


def test_route_navigation_and_ability(site):
    sign_in(site, 'pat')
    assert site.get('/models/image-workflows').status_code == 403
    sign_in(site, 'jordan')
    page = site.get('/models/image-workflows')
    assert page.status_code == 200 and 'data-wf-view="lanes"' in page.text and 'data-wf-run-button' in page.text and 'data-wf-run-unavailable' not in page.text
    found = navigation('/models/image-workflows', ['manage-workflows', 'manage-models'], False)
    assert found['section'] == 'Models' and found['active'] == '/models/image-workflows'
    models = next(group for group in found['groups'] if group['name'] == 'Models')
    labels = [label for label, _ in models['links']]
    assert labels[labels.index('Images') + 1] == 'Image workflows'
    only = next(group for group in navigation('/models/image-workflows', ['manage-workflows'], False)['groups'] if group['name'] == 'Models')
    assert only['links'] == [('Image workflows', '/models/image-workflows')]
    assert 'Image workflows' not in str(navigation('/models', ['manage-models'], False))
    library = site.get('/mirrorwall-api/graphs').json()
    assert library['upscalers'] == UPSCALERS and library['catalogue']['sampler_aliases']['dpmpp2m'] == 'dpm++2m'


def editor(site, width=1440, path='/models/image-workflows'):
    engine = playwright.sync_playwright().start()
    browser, page = browser_context(engine, site)
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://console.test' + path)
    expect(page.locator('#graph-choice')).to_be_enabled()
    return engine, browser, page, errors


def choose(page, label):
    page.locator('#graph-choice').select_option(label=label)


def test_save_publish_versions_and_delete_through_the_real_api(site):
    sign_in(site, 'jordan')
    engine, browser, page, errors = editor(site)
    try:
        choose(page, 'Upscaler comparison')
        expect(page.locator('#graph-version')).to_have_text('Sample · read-only')
        # The sample names an upscaler no node has; the problem is shown and the file is kept, marked.
        expect(page.locator('[data-wf-problems]')).to_contain_text('That upscaler is not installed on any node.')
        page.locator('.wf-nodes [data-node="siax"] .wf-node-title').click()
        expect(page.locator('.wf-inspector [data-field="upscaler"] option:checked')).to_have_text('4x_NMKD-Siax_200k.pth (not installed)')
        page.locator('.wf-inspector [data-field="upscaler"]').select_option('RealESRGAN_x4plus.pth')
        expect(page.locator('[data-wf-problems]')).to_have_attribute('data-state', 'ready')
        page.locator('#graph-name').fill('Upscaler shoot-out')
        page.locator('#graph-save').click()
        expect(page.locator('#graph-status')).to_contain_text('Saved version 1.')
        expect(page.locator('#graph-version')).to_have_text('Version 1')
        assert 'workflow=' in page.url
        page.locator('#graph-publish').click()
        expect(page.locator('#graph-published')).to_have_text('Published · v1')
        expect(page.locator('#graph-status')).to_contain_text('workflow:upscaler-shoot-out')
        # Move a card to a later lane: a layout change is still a change worth saving.
        page.locator('.wf-nodes [data-node="seed"]').focus()
        page.keyboard.press('Alt+ArrowRight')
        expect(page.locator('#graph-version')).to_have_text('Version 1 · unsaved changes')
        page.locator('#graph-save').click()
        expect(page.locator('#graph-status')).to_contain_text('Callers still run version 1 until you publish this one.')
        expect(page.locator('#graph-publish')).to_have_text('Publish v2')
        stored = site.get('/mirrorwall-api/graphs').json()['workflows'][0]
        assert stored['version'] == 2 and stored['published'] == 1
        assert next(n for n in stored['document']['nodes'] if n['id'] == 'seed')['lane'][0] == 1
        # Earlier versions open as a draft of the next version.
        expect(page.locator('#graph-versions option')).to_have_count(3)
        page.locator('#graph-versions').select_option('1')
        expect(page.locator('#graph-status')).to_contain_text('Saving makes it version 3')
        expect(page.locator('.wf-nodes [data-node="seed"]')).to_have_attribute('data-lane', '0')
        # Drafts survive switching to another workflow and back.
        choose(page, 'Quick draft')
        choose(page, 'Upscaler shoot-out (unsaved) · published v1')
        expect(page.locator('#graph-version')).to_have_text('Version 2 · unsaved changes')
        page.locator('#graph-unpublish').click()
        expect(page.locator('#graph-published')).to_be_hidden()
        page.locator('#graph-delete').click()
        expect(page.locator('#graph-delete')).to_have_attribute('data-confirm-armed', 'true')
        assert site.get('/mirrorwall-api/graphs').json()['workflows']  # not yet
        page.locator('#graph-delete').click()
        expect(page.locator('#graph-status')).to_contain_text('Deleted “Upscaler shoot-out”')
        assert site.get('/mirrorwall-api/graphs').json()['workflows'] == []
        assert not errors
    finally:
        browser.close(); engine.stop()


def test_new_workflow_problems_and_refused_saves(site):
    sign_in(site, 'jordan')
    engine, browser, page, errors = editor(site)
    try:
        page.locator('#graph-new').click()
        expect(page.locator('#graph-version')).to_have_text('New · not saved')
        expect(page.locator('.wf-nodes [data-node="sampler1"]')).to_contain_text('← Model')
        expect(page.locator('[data-wf-problems]')).to_have_attribute('data-state', 'ready')
        expect(page.locator('[data-wf-run-plan]')).to_have_text('Run plan: 1 step · 1 model load')
        page.locator('#graph-name').fill('Quick draft')  # the sample's API name is free until a saved workflow takes it
        page.locator('#graph-save').click()
        expect(page.locator('#graph-version')).to_have_text('Version 1')
        page.locator('#graph-duplicate').click()
        page.locator('#graph-name').fill('Quick draft')
        page.locator('#graph-save').click()
        expect(page.locator('#graph-error')).to_contain_text('already uses the API name')
        expect(page.locator('#graph-version')).to_have_text('New · not saved')
        assert not errors
    finally:
        browser.close(); engine.stop()


def test_open_as_advanced_converts_a_simple_workflow(site):
    sign_in(site, 'jordan')
    saved = site.post('/mirrorwall-api/presets', json={'name': 'Daily SDXL', 'model': 'sdxl-base', 'settings': {
        'size': '832x1216', 'steps': 30, 'sampler': 'dpmpp2m', 'scheduler': 'karras', 'cfg_scale': 5.5, 'negative_prompt': 'blurry'}})
    assert saved.status_code == 200, saved.text
    preset = saved.json()['id']
    engine, browser, page, errors = editor(site, path='/models/image-workflows?preset=' + preset)
    try:
        expect(page.locator('#graph-status')).to_contain_text('Converted “Daily SDXL”')
        expect(page.locator('#graph-name')).to_have_value('Daily SDXL (advanced)')
        base = page.locator('.wf-nodes [data-node="sampler1"]')
        expect(base).to_contain_text('30 steps · dpm++2m / karras · CFG 5.5')
        expect(page.locator('.wf-nodes [data-node="size1"]')).to_contain_text('832 × 1216')
        expect(base.locator('[data-port="in:negative"]')).to_contain_text('← Negative prompt')
        assert 'preset=' not in page.url
        assert not errors
    finally:
        browser.close(); engine.stop()


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_page_fits_every_width_and_narrow_screens_start_in_outline(site, width):
    sign_in(site, 'jordan')
    engine, browser, page, errors = editor(site, width)
    try:
        choose(page, 'Sampler shoot-out')
        if width < 700:
            expect(page.locator('.wf-outline')).to_be_visible()
            page.locator('.wf-outline [data-node="dpm"]').click()
            expect(page.locator('.wf-inspector [data-node-label]')).to_have_value('dpm')
        else:
            expect(page.locator('.wf-nodes [data-node="compare"]')).to_have_attribute('data-lane', '2')
            expect(page.locator('.wf-links path')).to_have_count(20)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
    finally:
        browser.close(); engine.stop()


def test_keyboard_only_linking(site):
    sign_in(site, 'jordan')
    engine, browser, page, errors = editor(site)
    try:
        page.locator('#graph-new').click()
        page.locator('.wf-nodes [data-node="output1"]').focus()
        page.keyboard.press('Enter')
        expect(page.locator('.wf-inspector [data-node-title]')).to_be_focused()
        source = page.get_by_label('Image (required)')
        source.focus()
        source.select_option('')
        expect(page.locator('[data-wf-problems]')).to_contain_text('Image needs a source.')
        source = page.get_by_label('Image (required)')
        source.select_option('sampler1.image')
        expect(page.locator('[data-wf-problems]')).to_have_attribute('data-state', 'ready')
        assert not errors
    finally:
        browser.close(); engine.stop()


def test_test_run_of_unsaved_changes_shows_steps_and_saved_outputs(site):
    sign_in(site, 'jordan')
    engine, browser, page, errors = editor(site)
    try:
        choose(page, 'Sampler shoot-out')
        page.locator('[data-wf-run-inputs] textarea').fill('A heron at dawn')
        page.locator('[data-wf-run-button]').click()
        expect(page.locator('#graph-status')).to_contain_text('Test run complete: 4 images saved to your library.')
        expect(page.locator('[data-wf-stages] .wf-stage[data-state=done]')).to_have_count(4)
        expect(page.locator('[data-wf-stages] [data-step="dpm"]')).to_contain_text('done · 64 × 64 · atlas')
        expect(page.locator('[data-wf-results] figure')).to_have_count(4)
        expect(page.locator('[data-wf-results] figcaption').first).to_contain_text('Samplers · Euler · simple · seed 1234')
        assert page.locator('[data-wf-results] img').first.evaluate('image => image.complete && image.naturalWidth > 0')
        expect(page.locator('[data-wf-run-button]')).to_be_enabled()
        outputs = site.get('/mirrorwall-api/assets').json()['assets']
        assert len(outputs) == 4 and outputs[0]['metadata']['workflow']['draft'] is True
        assert not errors
    finally:
        browser.close(); engine.stop()
