"""Models → Sampler comparison: every chosen scheduler × sampler as one ordinary image job, per-line results and the synchronized-zoom viewer."""

import pytest
from PIL import Image

from mirrorwall.front import COMPARISON_IMAGES, Front
from mirrorwall.model import SAMPLERS, SCHEDULERS
from mirrorwall.runtime import png_bytes
from test_console_browser import browser_context, playwright
from test_image_studio import studio  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.navigation import navigation

expect = playwright.expect
BASE = {'model': 'sketch', 'prompt': 'A lighthouse', 'width': 512, 'height': 512, 'steps': 20, 'cfg_scale': 5, 'seed': 77}


def test_each_scheduler_tries_every_sampler_with_one_seed_and_a_larger_limit():
    front = Front.__new__(Front)
    rows = front.lines({'request': {**BASE, 'n': 2}, 'sweep': {'scheduler': ['karras', 'beta'], 'sampler': ['euler', 'dpm++2m', 'er_sde']}})
    assert [(row['body']['scheduler'], row['body']['sampler'], row['body']['seed']) for row in rows[:4]] == [
        ('karras', 'euler', 77), ('karras', 'euler', 78), ('karras', 'dpm++2m', 77), ('karras', 'dpm++2m', 78)]
    assert len(rows) == 12 and rows[6]['body']['scheduler'] == 'beta'
    assert len(front.lines({'request': BASE, 'sweep': {'scheduler': list(SCHEDULERS), 'sampler': list(SAMPLERS)}})) == len(SCHEDULERS) * len(SAMPLERS)
    with pytest.raises(ValueError, match=f'limited to {COMPARISON_IMAGES} images; this one would make 672'):
        front.lines({'request': {**BASE, 'n': 2}, 'sweep': {'scheduler': list(SCHEDULERS), 'sampler': list(SAMPLERS)}})
    with pytest.raises(ValueError, match='limited to 128 images'):
        front.lines({'request': {**BASE, 'n': 5}, 'sweep': {'seed': list(range(30))}})
    for bad in ({'sampler': ['euler', 'euler']}, {'sampler': ['nope']}, {'scheduler': []}):
        with pytest.raises(ValueError):
            front.lines({'request': BASE, 'sweep': bad})


def test_navigation_lists_the_page_beside_images_for_model_managers():
    found = navigation('/models/images/samplers', ['manage-models'], False)
    labels = [label for label, _ in next(group for group in found['groups'] if group['name'] == 'Models')['links']]
    assert found['active'] == '/models/images/samplers' and found['headings']['/models/images/samplers'] == 'Image setup'
    assert labels.index('Sampler comparison') == labels.index('Audio') + 1  # after the catalogue, under its own heading
    assert 'Sampler comparison' not in str(navigation('/models', [], False))


@pytest.fixture
def comparison(studio, monkeypatch):
    server, wire, users, library = studio
    front = wire.front.state.mirrorwall
    owner = users.by_name('jordan').identity
    held = {'number': None}

    def run(number, limit=None):
        """Stands in for the GPU: answers lines in order, refusing the lcm sampler as a runtime would."""
        store = front.batches.store
        done = 0
        while (limit is None or done < limit) and (line := store.next_line(number)):
            body = line['body']
            if body['sampler'] == 'lcm':
                store.keep(number, line['line'], 400, '', {'error': {'message': 'The runtime does not offer the sampler lcm.'}})
            else:
                colour = (len(body['sampler']) * 20 % 255, len(body['scheduler']) * 30 % 255, 90)
                front.store.save(owner, png_bytes(Image.new('RGB', (64, 64), colour)), kind='output', filename='out.png', job=number,
                                 metadata={'seed': body['seed'], 'batch_line': str(line['line']), 'settings': body})
                store.keep(number, line['line'], 200, '', {'data': []})
            done += 1
        if store.next_line(number) is None:
            store.finish(number)
        else:  # take back the look-ahead
            store.not_begun(number, store.next_line(number)['line']); store.not_begun(number, store.next_line(number)['line'])

    async def model_ids(user):
        return {}
    def work(number, user_for):
        held['number'] = number
        run(number, limit=3)
    monkeypatch.setattr(front.batches, 'model_ids', model_ids)
    monkeypatch.setattr(front.batches, 'work', work)
    return server, front, held, run


def test_job_lines_report_order_images_and_failures_only_to_the_owner(comparison):
    server, front, held, run = comparison
    started = server.post('/mirrorwall-api/jobs', json={'request': BASE, 'sweep': {'scheduler': ['karras', 'beta'], 'sampler': ['euler', 'lcm']}})
    assert started.status_code == 201, started.text
    number = started.json()['id']
    lines = server.get(f'/mirrorwall-api/jobs/{number}/lines').json()
    assert [(one['scheduler'], one['sampler'], one['state']) for one in lines['lines']] == [
        ('karras', 'euler', 'done'), ('karras', 'lcm', 'failed'), ('beta', 'euler', 'done'), ('beta', 'lcm', 'waiting')]
    assert lines['lines'][1]['error'] == 'The runtime does not offer the sampler lcm.' and lines['lines'][0]['asset_id'].startswith('image_')
    assert lines['lines'][0]['seed'] == 77 and lines['status'] == 'in_progress'
    run(number)
    assert server.get(f'/mirrorwall-api/jobs/{number}/lines').json()['status'] == 'completed'
    sign_in(server, 'pat')
    assert server.get(f'/mirrorwall-api/jobs/{number}/lines').status_code in (403, 404)


@pytest.mark.parametrize('width', [1440, 390])
def test_page_runs_a_comparison_and_zooms_every_image_to_the_same_region(comparison, width, tmp_path):
    server, front, held, run = comparison
    engine = playwright.sync_playwright().start()
    browser, page = browser_context(engine, server)
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto('http://console.test/models/images/samplers')
        expect(page.locator('#sc-model')).to_have_value('sketch')
        expect(page.locator('#sc-count')).to_contain_text(f'{len(SCHEDULERS)} schedulers × {len(SAMPLERS)} samplers = {len(SCHEDULERS) * len(SAMPLERS)} images')
        page.locator('[data-sc-none="scheduler"]').click()
        expect(page.locator('#sc-run')).to_be_disabled()
        for value in ('karras', 'beta'):
            page.locator(f'#sc-schedulers input[value="{value}"]').check()
        page.locator('[data-sc-none="sampler"]').click()
        for value in ('euler', 'dpm++2m', 'lcm'):
            page.locator(f'#sc-samplers input[value="{value}"]').check()
        expect(page.locator('#sc-count')).to_have_text('2 schedulers × 3 samplers = 6 images')
        # References: offered only for models that take them, and sent with every combination.
        expect(page.locator('#sc-reference-box')).to_be_visible()
        picture = tmp_path / 'start.png'
        Image.new('RGB', (400, 400), (200, 120, 40)).save(picture)
        page.locator('#sc-upload').set_input_files(str(picture))
        expect(page.locator('#sc-references .ig-reference')).to_have_count(1)
        expect(page.locator('#sc-references')).to_contain_text('400 × 400')
        expect(page.locator('#sc-upload')).to_be_disabled()  # this model takes one
        expect(page.locator('#sc-count')).to_contain_text('smaller than the reference size')
        expect(page.locator('#sc-run')).to_be_disabled()
        page.locator('#sc-reference-mp').fill('0.1')
        expect(page.locator('#sc-count')).to_have_text('2 schedulers × 3 samplers = 6 images')
        expect(page.locator('#sc-strength')).to_be_visible()
        page.locator('#sc-model').select_option('qwen')
        expect(page.locator('#sc-reference-box')).to_be_hidden()
        page.locator('#sc-model').select_option('sketch')
        expect(page.locator('#sc-references .ig-reference')).to_have_count(1)
        page.locator('#sc-prompt').fill('A lighthouse at dusk')
        page.locator('#sc-cfg').fill('4.5')
        page.locator('#sc-seed').fill('1234')
        with page.expect_request(lambda sent: sent.url.endswith('/mirrorwall-api/jobs') and sent.method == 'POST') as posted:
            page.locator('#sc-run').click()
        body = posted.value.post_data_json
        assert len(body['references']) == 1 and body['request']['reference_megapixels'] == 0.1 and body['request']['strength'] == 0.75
        expect(page.locator('#sc-status')).to_contain_text('Running · 2 of 6 done · 1 failed')
        # One continuous grid in run order, with no per-scheduler sets.
        expect(page.locator('#sc-grid > .sc-tile')).to_have_count(6)
        expect(page.locator('.sc-tile [data-scheduler]')).to_have_text(['karras'] * 3 + ['beta'] * 3)
        first = page.locator('.sc-tile').first
        expect(first.locator('figcaption')).to_contain_text('euler · karras')
        expect(first.locator('[data-detail]')).to_have_text('seed 1234 · 28 steps · CFG 4.5')
        expect(page.locator('.sc-tile').nth(2)).to_have_attribute('data-state', 'failed')
        expect(page.locator('.sc-tile').nth(2)).to_contain_text('does not offer the sampler lcm')
        expect(page.locator('.sc-tile').nth(3)).to_have_attribute('data-state', 'running')
        run(held['number'])
        expect(page.locator('#sc-status')).to_contain_text('Comparison complete: 4 of 6 done · 2 failed', timeout=8000)
        expect(page.locator('#sc-pause')).to_be_hidden()
        # Zoom: every image shows the same region, at full resolution.
        page.locator('#sc-zoom').fill('4')
        expect(page.locator('#sc-zoom-value')).to_have_text('4×')
        frame = page.locator('.sc-tile').nth(1).locator('.sc-frame')
        frame.scroll_into_view_if_needed()
        box = frame.bounding_box()
        page.mouse.move(box['x'] + box['width'] * .25, box['y'] + box['height'] * .75)
        focus = page.evaluate("['--sc-x','--sc-y'].map(name => parseFloat(getComputedStyle(document.getElementById('sc-grid')).getPropertyValue(name)))")
        assert focus == [pytest.approx(25, abs=1), pytest.approx(75, abs=1)]
        origins = page.locator('.sc-tile[data-state=done] img').evaluate_all('images => images.map(image => getComputedStyle(image).transformOrigin)')
        assert len(set(origins)) == 1 and len(origins) == 4
        assert page.locator('.sc-tile[data-state=done] img').first.get_attribute('src').endswith('/content')
        frame.focus(); page.keyboard.press('ArrowRight')
        assert page.evaluate("parseFloat(getComputedStyle(document.getElementById('sc-grid')).getPropertyValue('--sc-x'))") == pytest.approx(focus[0] + 5)
        # Lock position: the pointer no longer moves the region; a click does.
        page.locator('#sc-lock').check()
        here = "['--sc-x','--sc-y'].map(name => parseFloat(getComputedStyle(document.getElementById('sc-grid')).getPropertyValue(name)))"
        frame.scroll_into_view_if_needed()
        box = frame.bounding_box()  # ticking the box may have scrolled the grid
        locked = page.evaluate(here)
        page.mouse.move(box['x'] + box['width'] * .8, box['y'] + box['height'] * .2)
        assert page.evaluate(here) == locked
        page.mouse.click(box['x'] + box['width'] * .5, box['y'] + box['height'] * .5)
        assert page.evaluate(here) == [pytest.approx(50, abs=1), pytest.approx(50, abs=1)]
        page.mouse.move(box['x'] + box['width'] * .1, box['y'] + box['height'] * .9)
        assert page.evaluate(here) == [pytest.approx(50, abs=1), pytest.approx(50, abs=1)]
        page.locator('#sc-lock').uncheck()
        # Image size and full-width results.
        page.locator('#sc-size').fill('400')
        expect(page.locator('#sc-size-value')).to_have_text('400 px')
        assert page.locator('.sc-tile').first.bounding_box()['width'] >= 400 or width < 600
        before = page.locator('#sc-grid').bounding_box()['width']
        page.locator('#sc-hide-settings').click()
        expect(page.locator('#sc-form')).to_be_hidden()
        expect(page.locator('#sc-hide-settings')).to_have_text('Show settings')
        if width > 1000:
            assert page.locator('#sc-grid').bounding_box()['width'] > before + 300
        page.reload()
        expect(page.locator('#sc-form')).to_be_hidden()
        expect(page.locator('#sc-size-value')).to_have_text('400 px')
        page.locator('#sc-hide-settings').click()
        expect(page.locator('#sc-form')).to_be_visible()
        page.locator('#sc-earlier').select_option(index=1)
        expect(page.locator('.sc-tile')).to_have_count(6)
        # Select two and show only those.
        page.locator('.sc-tile').nth(0).locator('[data-pick]').check()
        page.locator('.sc-tile').nth(4).locator('[data-pick]').check()
        page.locator('#sc-selected-only').check()
        expect(page.locator('.sc-tile:visible')).to_have_count(2)
        # The comparison can be reopened later.
        page.reload()
        page.locator('#sc-earlier').select_option(index=1)
        expect(page.locator('.sc-tile')).to_have_count(6)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
    finally:
        browser.close(); engine.stop()
