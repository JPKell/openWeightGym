"""CPU-only console form round trips and responsive theme acceptance."""
import json
from pathlib import Path

import pytest

from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_weightroom import desk, gguf, sign_in  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_image_editor_roundtrips_pinned_hardware_and_structured_defaults(desk, tmp_path, width):
    server, _, _ = desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    file = gguf(tmp_path / 'picture.gguf')
    settings = {'engine': 'sdcpp', 'flags': {'model': file, 'ctx-size': 0}, 'gpus': ['A'], 'alone': True,
                'sdcpp': {'family': 'sdxl', 'components': {}, 'host_ram_mib': 8192, 'defaults': {'width': 512, 'height': 512}, 'limits': {'max_references': 1}}}
    library.put('picture', settings)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto('http://console.test/models/picture')
        assert page.locator('textarea').count() == 0
        page.get_by_role('link', name='Edit settings', exact=True).click()
        playwright.expect(page.locator('#mw-name')).to_have_value('picture')
        playwright.expect(page.locator('[data-hardware-mode][value="pin"]')).to_be_checked()
        playwright.expect(page.locator('#mw-default-width')).to_have_value('512')
        page.locator('#mw-default-width').fill('768')
        page.get_by_role('button', name='Save model', exact=True).click()
        playwright.expect(page.locator('#mw-saved')).to_have_text('Saved.')
        assert library.stored('picture')['gpus'] == ['A']
        assert library.stored('picture')['sdcpp']['defaults']['width'] == 768
        page.reload()
        playwright.expect(page.locator('#mw-default-width')).to_have_value('768')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("[...document.querySelectorAll('main *')].filter(e=>e.getBoundingClientRect().right>innerWidth).slice(0,10).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right,e.textContent,e.parentElement.outerHTML.slice(0,700)])")
        folder = Path('data/qualification/console-polish/after'); folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'image-editor-{width}.png'), full_page=True)
        page.get_by_text('Advanced (JSON)', exact=True).click()
        page.locator('#mw-loras').fill('{broken')
        page.get_by_role('button', name='Save model', exact=True).click()
        playwright.expect(page.locator('#mw-message')).to_contain_text('JSON')
        assert library.stored('picture')['sdcpp']['defaults']['width'] == 768
        assert not errors
        browser.close()
    library.db.close()


def test_audio_family_filter_defaults_and_cpu_selection(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    selected = {'name': 'speaker', 'engine': 'audiocpp', 'gpus': ['A'], 'alone': False,
                'flags': {'model': '/models/audio/speaker.gguf', 'ctx-size': 0},
                'audiocpp': {'family': 'kokoro', 'kind': 'speech', 'path': '/models/audio/speaker.gguf', 'defaults': {'speed': 1, 'future_option': 4}, 'limits': {'max_seconds': 600, 'max_takes': 8}, 'session_options': {}}}
    sent = []
    def capture(route, path):
        if path.startswith('/soundroom-models'):
            if route.request.method == 'POST':
                body = route.request.post_data_json; sent.append(body)
                selected.update(body['settings'])
                value = {'name': 'speaker'}
            else:
                value = {'models': [selected], 'cards': ['A', 'B'], 'engines': {'engines': []}, 'catalogue': {'families': [
                    {'family': 'kokoro', 'name': 'Kokoro', 'kinds': ['speech'], 'options': {'request': [{'name': 'speed', 'type': 'float', 'min': .25, 'max': 4}]}},
                    {'family': 'ace_step', 'name': 'ACE-Step', 'kinds': ['music']}]}}
            route.fulfill(content_type='application/json', body=json.dumps(value)); return True
        if path == '/model-files':
            route.fulfill(content_type='application/json', body=json.dumps({'nodes': [{'name': 'compute', 'answering': True, 'files': [
                {'path': '/models/audio/speaker.gguf', 'tier': 'cache', 'facts': {'what': 'audio', 'family': 'kokoro'}}]}]})); return True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/models/audio?model=speaker')
        playwright.expect(page.locator('#am-name')).to_have_value('speaker')
        assert page.locator('#am-family option').all_text_contents() == ['Choose a compatible model family', 'Kokoro']
        page.locator('[data-section="output"]').click()
        page.locator('#am-option-speed').fill('1.5')
        page.locator('[data-section="hardware"]').click()
        page.locator('[data-gpu-card][value="B"]').check()
        assert page.locator('[data-gpu-card]:checked').count() == 1
        playwright.expect(page.locator('[data-gpu-card][value="A"]')).not_to_be_checked()
        page.get_by_text('CPU only', exact=True).click()
        page.get_by_role('button', name='Save model', exact=True).click()
        playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
        assert sent[0]['settings']['gpus'] == 0
        assert sent[0]['settings']['audiocpp']['defaults'] == {'speed': 1.5, 'future_option': 4}
        assert not errors
        browser.close()


def test_purpose_settings_save_through_existing_program_and_hide_duplicates(desk):
    from loadcoach.settings import said
    server, wire, _ = desk
    wire.front.state.front_settings.changes.seen('toollocker', said({'call_timeout': 30}, {}, {'call_timeout': 'Time allowed for one tool call.'}))
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/settings?open=toollocker')
        assert page.locator('[name^="value-profiles."], [name^="value-audio_"]').count() == 0
        page.get_by_label('Tool call timeout', exact=True).fill('45')
        page.get_by_role('button', name='Save tools', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_have_text('Saved.')
        assert wire.front.state.front_settings.changes.of('toollocker')['call_timeout'] == 45
        for width in [1440, 390, 320]:
            page.set_viewport_size({'width': width, 'height': 1000})
            page.goto('http://console.test/settings?open=toollocker')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            folder = Path('data/qualification/console-polish/after'); folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f'settings-{width}.png'), full_page=True)
        assert not errors
        browser.close()


def test_llm_hardware_modes_keep_the_legacy_post_shape(desk, tmp_path):
    server, _, _ = desk
    file = gguf(tmp_path / 'language.gguf', general__architecture='qwen', qwen__context_length=8192)
    Models(str(tmp_path / 'loadcoach.db')).put('language', {'gpus': 1, 'flags': {'model': file, 'ctx-size': 8192}})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/models/language')
        page.get_by_text('CPU only', exact=True).click()
        fields = lambda: page.locator('[data-model-settings]').evaluate('form => [...new FormData(form)]')
        assert ['cpu', 'yes'] in fields() and not any(key == 'gpu_card' for key, _ in fields())
        page.get_by_text('Pin to GPUs', exact=True).click()
        page.locator('[data-gpu-card][value="A"]').check()
        assert ['gpu_card', 'A'] in fields() and ['cpu', 'yes'] not in fields()
        page.get_by_text('Automatic GPU', exact=True).click()
        assert not any(key == 'gpu_card' for key, _ in fields())
        playwright.expect(page.locator('#allow-split')).to_be_visible()
        page.locator('#parallel-enabled').check()
        page.locator('#parallel-count').fill('3')
        page.locator('#parallel-context').fill('4096')
        playwright.expect(page.locator('[data-context-summary]')).to_have_text('3 requests, up to 4,096 tokens each')
        browser.close()
