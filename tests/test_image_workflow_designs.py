"""Image workflow proposals stay offline and share one editor: Send to / Use from, insertion, validation, runs and API examples."""

import json
import runpy
from pathlib import Path

import pytest

from mirrorwall import graph

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
LAYOUTS = ('canvas', 'lanes', 'split')


def test_samples_use_real_ports_matching_types_and_no_loops():
    for sample in graph.SAMPLES:
        nodes = {node['id']: node for node in sample['nodes']}
        assert len(nodes) == len(sample['nodes']), sample['name']
        for link in sample['links']:
            source, target = nodes[link['from'][0]], nodes[link['to'][0]]
            output = next(port for port in graph.COMPONENTS[source['type']]['outputs'] if port['name'] == link['from'][1])
            entry = next(port for port in graph.COMPONENTS[target['type']]['inputs'] if port['name'] == link['to'][1])
            assert output['type'] == entry['type'], (sample['name'], link)
        for node in sample['nodes']:
            for port in graph.COMPONENTS[node['type']].get('inputs', []):
                linked = [link for link in sample['links'] if link['to'] == [node['id'], port['name']]]
                assert not port['required'] or linked, (sample['name'], node['id'], port['name'])
                assert port['multiple'] or len(linked) <= 1, (sample['name'], node['id'], port['name'])
            if node['type'] == 'sampler':
                assert node['values']['sampler'] in graph.NATIVE_SAMPLERS and node['values']['scheduler'] in graph.NATIVE_SCHEDULERS
        assert any(node['type'] in ('output', 'compare') for node in sample['nodes']), sample['name']
        # Topological order exists: repeatedly removing nodes without incoming links empties the graph.
        remaining, links = set(nodes), list(sample['links'])
        while remaining:
            free = {one for one in remaining if not any(link['to'][0] == one for link in links)}
            assert free, sample['name'] + ' has a loop'
            remaining -= free
            links = [link for link in links if link['from'][0] not in free]
    assert json.dumps(graph.catalogue())


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-image-workflow-designs.py'))['render']())
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1200})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        browser.close()
        assert not errors
        assert not requests, 'Design proposals must not contact services.'


def choose(page, name):
    page.locator('#workflow-choice').select_option(label='Sample · ' + name)


def select_node(page, identifier):
    page.locator(f'.wf-nodes [data-node="{identifier}"] .wf-node-title').click()


def port(page, node, name):
    return page.locator(f'.wf-nodes [data-node="{node}"] [data-port="in:{name}"]')


def links(page):
    return page.locator('.wf-links path').count()


@pytest.mark.parametrize('layout', LAYOUTS)
def test_every_layout_draws_links_and_fits(preview, layout):
    page = preview
    page.locator(f'[data-design={layout}]').click()
    for name in ('Quick draft', 'Sampler shoot-out', 'Krea Turbo draft → SDXL detail'):
        choose(page, name)
        expect(page.locator('.wf-nodes [data-node]').first).to_be_visible()
        page.wait_for_timeout(100)
        assert links(page) > 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_send_to_fans_out_use_from_rebinds_and_loops_are_refused(preview):
    page = preview
    page.locator('#duplicate-workflow').click()
    expect(page.locator('#workflow-version')).to_have_text('New · not saved')
    select_node(page, 'base')
    page.locator('[data-add="sampler"]').click()  # binds model, prompt, seed and size automatically
    expect(port(page, 'sampler1', 'model')).to_contain_text('← Model')
    expect(port(page, 'sampler1', 'references')).to_contain_text('← Base')  # Klein takes references, not a start image
    expect(port(page, 'sampler1', 'init')).to_have_count(0)
    select_node(page, 'prompt')
    destination = page.locator('[data-destination="sampler1.prompt"]')
    expect(destination).to_be_checked()
    destination.uncheck()
    expect(port(page, 'sampler1', 'prompt')).to_contain_text('needs a source')
    expect(page.locator('[data-wf-problems]')).to_contain_text('Prompt needs a source.')
    expect(page.locator('[data-wf-run-button]')).to_be_disabled()
    destination.check()
    expect(page.locator('[data-wf-run-button]')).to_be_enabled()
    # Fan-out: one output, several destinations; a destination upstream of the source is refused.
    select_node(page, 'sampler1')
    page.locator('[data-destination="out.image"]').check()
    expect(page.locator('[data-destination="base.init"]')).to_be_disabled()
    expect(page.locator('.wf-send-to')).to_contain_text('Would create a loop')
    expect(port(page, 'out', 'image')).to_contain_text('← Sampler 1')
    select_node(page, 'out')
    page.locator('.wf-use-from select').first.select_option('base.image')
    expect(port(page, 'out', 'image')).to_contain_text('← Base')
    page.locator('[data-wf-undo]').click()
    expect(port(page, 'out', 'image')).to_contain_text('← Sampler 1')
    page.locator('[data-wf-redo]').click()
    expect(port(page, 'out', 'image')).to_contain_text('← Base')


def test_insert_after_reroutes_and_family_rules_block_runs(preview):
    page = preview
    page.locator('#duplicate-workflow').click()
    select_node(page, 'base')
    page.locator('[data-add-after-type]').select_option('upscale')
    page.locator('[data-add-after]').click()
    expect(port(page, 'upscale1', 'image')).to_contain_text('← Base')
    expect(port(page, 'out', 'image')).to_contain_text('← Upscale')
    # Klein has no start-image pass in this runtime; four references is its limit.
    page.locator('[data-add="load_image"]').click()
    select_node(page, 'load_image1')
    page.locator('[data-destination="base.init"]').check()
    expect(page.locator('[data-wf-problems]')).to_contain_text('Flux 2 klein has no start-image pass here')
    expect(page.locator('[data-wf-run-button]')).to_be_disabled()
    page.locator('[data-destination="base.init"]').uncheck()
    for _ in range(5):
        page.locator('[data-add="load_image"]').click()
    for index in range(1, 6):
        select_node(page, f'load_image{index}')
        page.locator('[data-destination="base.references"]').check()
    expect(page.locator('[data-wf-problems]')).to_contain_text('uses at most 4 reference images')
    page.locator('button[data-wf-view=outline]').click()
    expect(page.locator('.wf-outline')).to_contain_text('To: Base · Reference images')
    page.locator('.wf-outline [data-node="base"]').click()
    expect(page.locator('.wf-chips button')).to_have_count(5)
    page.locator('.wf-chips button').last.click()
    expect(page.locator('[data-wf-problems]')).not_to_contain_text('reference images')


def test_test_run_returns_labelled_outputs_and_api_example_uses_overrides(preview):
    page = preview
    choose(page, 'Sampler shoot-out')
    expect(page.locator('[data-wf-run-plan]')).to_have_text('4 steps · 1 model load')
    page.locator('[data-wf-run-button]').click()
    expect(page.locator('.wf-result')).to_have_count(4, timeout=10000)
    expect(page.locator('.wf-result').first).to_contain_text('Samplers · Euler · simple · seed 1234')
    choose(page, 'SDXL two-model refine')
    expect(page.locator('[data-wf-run-plan]')).to_have_text('2 steps · 2 model loads')
    page.locator('#run-refine-strength').fill('0.9')
    page.locator('[data-wf-run-button]').click()
    expect(page.locator('#feedback-text')).to_contain_text('must be from 0.1 to 0.45')
    page.locator('[data-wf-api] summary').click()
    code = page.locator('[data-wf-api-code]')
    expect(code).to_contain_text('"model": "workflow:sdxl-two-model-refine"')
    expect(code).to_contain_text('"refine.strength": 0.25')
    page.locator('[data-wf-api-language=python]').click()
    expect(code).to_contain_text('requests.post(')
    page.locator('#duplicate-workflow').click()
    page.locator('#workflow-name').fill('Portrait refine')
    page.locator('#save-workflow').click()
    expect(page.locator('#feedback-text')).to_contain_text('workflow:portrait-refine@1')
    expect(page.locator('#workflow-version')).to_have_text('Version 1')


def test_drafts_survive_workflow_and_layout_switches(preview):
    page = preview
    page.locator('#duplicate-workflow').click()
    select_node(page, 'base')
    page.locator('.wf-inspector [data-field="steps"]').fill('9')
    page.locator('.wf-inspector [data-field="steps"]').dispatch_event('change')
    choose(page, 'Sampler shoot-out')
    page.locator('#workflow-choice').select_option(label='Quick draft copy (unsaved)')
    expect(page.locator('.wf-nodes [data-node="base"]')).to_contain_text('9 steps')
    for layout in LAYOUTS:
        page.locator(f'[data-design={layout}]').click()
        expect(page.locator('.wf-nodes [data-node="base"]')).to_contain_text('9 steps')


@pytest.mark.parametrize('width', [390, 320])
def test_narrow_screens_start_in_outline_without_overflow(tmp_path, width):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-image-workflow-designs.py'))['render']())
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(output.as_uri())
        expect(page.locator('.wf-outline')).to_be_visible()
        expect(page.locator('.wf-surface')).to_be_hidden()
        page.locator('.wf-outline [data-node="base"]').click()
        expect(page.locator('.wf-inspector [data-node-label]')).to_have_value('base')
        for layout in LAYOUTS:
            page.locator(f'[data-design={layout}]').click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), layout
        page.locator('#theme').click()
        assert page.evaluate("document.documentElement.dataset.bsTheme") == 'dark'
        browser.close()
        assert not errors


def lane(page, node):
    card = page.locator(f'.wf-nodes [data-node="{node}"]')
    return int(card.get_attribute('data-lane')), int(card.get_attribute('data-row'))


def drag_to(page, node, x, y):
    head = page.locator(f'.wf-nodes [data-node="{node}"] .wf-node-head').bounding_box()
    page.mouse.move(head['x'] + 30, head['y'] + 8)
    page.mouse.down()
    page.mouse.move(head['x'] + 50, head['y'] + 20, steps=3)
    page.mouse.move(x, y, steps=8)
    expect(page.locator('.wf-lane-drop')).to_be_visible()
    page.mouse.up()


def test_lanes_move_cards_by_drag_and_keyboard_but_never_before_their_sources(preview):
    page = preview
    expect(page.locator('button[data-wf-view=lanes]')).to_have_attribute('aria-pressed', 'true')
    choose(page, 'Upscaler comparison')
    page.locator('#duplicate-workflow').click()
    assert lane(page, 'siax') == (2, 2) and lane(page, 'again') == (3, 0)
    below = page.locator('.wf-nodes [data-node="again"]').bounding_box()
    drag_to(page, 'siax', below['x'] + 60, below['y'] + below['height'] + 30)
    assert lane(page, 'siax') == (3, 1)
    expect(page.locator('[data-wf-status]')).to_have_text('Siax 2× is in lane 4, position 2.')
    above = page.locator('.wf-nodes [data-node="again"]').bounding_box()
    drag_to(page, 'siax', above['x'] + 60, above['y'] - 4)
    assert lane(page, 'siax') == (3, 0) and lane(page, 'again') == (3, 1)
    # A step cannot move before what it uses; the reason is announced.
    prompt = page.locator('.wf-nodes [data-node="prompt"]').bounding_box()
    drag_to(page, 'base', prompt['x'] + 60, prompt['y'] + 40)
    assert lane(page, 'base')[0] == 1
    expect(page.locator('[data-wf-status]')).to_contain_text('Base uses')
    # Moving a source later pushes everything that uses it along.
    page.locator('.wf-nodes [data-node="seed"]').focus()
    page.keyboard.press('Alt+ArrowRight')
    assert lane(page, 'seed')[0] == 1 and lane(page, 'base')[0] == 2 and lane(page, 'sharp')[0] == 3
    page.keyboard.press('Alt+ArrowLeft')
    assert lane(page, 'seed')[0] == 0 and lane(page, 'base')[0] == 1
    page.locator('[data-wf-undo]').click()
    assert lane(page, 'seed')[0] == 1
    page.locator('[data-wf-tidy]').click()
    assert lane(page, 'seed') == (0, 2) and lane(page, 'siax') == (2, 2)
    expect(page.locator('[data-wf-status]')).to_have_text('Lanes follow the run order again.')
    page.locator('button[data-wf-view=canvas]').click()
    expect(page.locator('#editor')).to_have_attribute('data-wf-layout', 'canvas')
    page.wait_for_timeout(100)
    assert links(page) > 0


def test_upscale_steps_offer_every_installed_upscaler_and_flag_missing_ones(preview):
    page = preview
    page.locator('#duplicate-workflow').click()
    select_node(page, 'base')
    page.locator('[data-add-after-type]').select_option('upscale')
    page.locator('[data-add-after]').click()
    expect(page.locator('[data-wf-problems]')).to_contain_text('Choose an upscaler.')
    picker = page.locator('.wf-inspector [data-field="upscaler"]')
    expect(picker.locator('option')).to_have_count(6)
    picker.select_option('RealESRGAN_x2plus.pth')
    expect(page.locator('.wf-nodes [data-node="upscale1"]')).to_contain_text('×2 · RealESRGAN x2 plus')
    expect(page.locator('[data-wf-problems]')).not_to_contain_text('upscaler')
    # A second upscaler chained after the first.
    page.locator('[data-add-after-type]').select_option('upscale')
    page.locator('[data-add-after]').click()
    page.locator('.wf-inspector [data-field="upscaler"]').select_option('4x-UltraSharp.pth')
    expect(port(page, 'upscale2', 'image')).to_contain_text('← Upscale')
    expect(port(page, 'out', 'image')).to_contain_text('← Upscale 2')
    # The hires pass lists installed upscalers after the latent methods.
    select_node(page, 'base')
    choose(page, 'SDXL two-pass hires')
    select_node(page, 'base')
    page.locator('.wf-advanced summary').click()
    expect(page.locator('.wf-inspector [data-field="hires_upscaler"] optgroup[label="Installed upscalers"] option')).to_have_count(5)
