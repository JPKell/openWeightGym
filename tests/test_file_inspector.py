"""The live file inspector uses real artifact actions and retains safe empty/error states."""

import time
from pathlib import Path
from urllib.parse import quote

import pytest

from test_console_browser import browser_context, playwright
from test_file_library import library_desk  # noqa: F401
from test_weightroom import gguf
from weightroom.file_views import destinations


def test_destination_inventory_uses_matching_place_and_does_not_trust_offline_copies():
    file = dict(path='/cache/a.gguf', place='storage_llm', size=40, facts={'name': 'A'})
    found = dict(artifact_machine='front', nodes=[
        dict(name='front', answering=True, places=[]),
        dict(name='atlas', answering=True, places=[dict(name='image', free=99), dict(name='llm', free=80)],
             files=[dict(file, path='/atlas/a.gguf')]),
        dict(name='nova', answering=False, places=[dict(name='llm', free=80)], files=[file]),
        dict(name='empty', answering=True, places=[], files=[]),
    ])
    atlas, nova, empty = destinations(file, found)
    assert atlas['present'] and atlas['free'] == 80 and atlas['place'] == 'llm'
    assert not nova['present'] and not nova['available'] and nova['free'] is None
    assert not empty['available']
    found['nodes'][1]['files'][0]['size'] = 41
    assert not destinations(file, found)[0]['present']


@pytest.fixture
def inspector(library_desk):
    server, compute, cache, storage, node = library_desk
    file = Path(gguf(cache / 'llm' / 'inspector.gguf', general__name='Inspector <test>',
                     general__architecture='qwen3', general__size_label='32B',
                     qwen3__context_length=40960, qwen3__block_count=64,
                     tokenizer__chat_template='safe <script>window.badMetadata = true</script> ' + 'long metadata ' * 50))
    server.post('/files', data={'do': 'refresh-files'})
    return server, file, storage, node, compute


def test_inspector_search_sort_full_values_dynamic_replacement_and_resizing(inspector, tmp_path):
    server, file, _, _, _ = inspector
    url = 'http://console.test/file?path=' + quote(str(file), safe='')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        playwright.expect(page.locator('[data-file-page]')).to_have_attribute('data-layout', 'inspector')
        playwright.expect(page.locator('#file-metadata')).to_have_attribute('open', '')
        playwright.expect(page.locator('.file-metadata .facts')).to_contain_text('Inspector <test>')
        assert not page.evaluate('window.badMetadata')
        page.get_by_label('Search metadata').fill('missing-key')
        playwright.expect(page.locator('[data-file-metadata-empty]')).to_be_visible()
        page.get_by_label('Search metadata').fill('context')
        playwright.expect(page.locator('[data-file-metadata-row]:visible')).to_have_count(1)
        page.get_by_label('Metadata group').select_option('tokenizer')
        playwright.expect(page.locator('[data-file-metadata-empty]')).to_be_visible()
        page.get_by_label('Search metadata').fill('')
        page.locator('#metadata td details summary').click()
        playwright.expect(page.locator('#metadata pre')).to_contain_text('long metadata ' * 50)
        page.locator('#metadata th').first.focus()
        page.keyboard.press('Enter')
        playwright.expect(page.locator('[data-file-metadata-row]:visible')).to_have_count(1)
        # Replace the actual fragment while keeping the page's script instance alive.
        page.evaluate("async url => { await htmx.ajax('GET', url, {target: '#page-content', select: '#page-content', swap: 'outerHTML'}); }", url)
        page.get_by_label('Search metadata').fill('context')
        playwright.expect(page.locator('[data-file-metadata-row]:visible')).to_have_count(1)
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            for width in (1920, 1440, 1024, 768, 390, 320):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.get_by_label('Search metadata').fill('')
        page.get_by_label('Metadata group').select_option('all')
        page.screenshot(path=str(tmp_path / 'live-inspector.png'), full_page=True)
        assert not errors
        browser.close()


def test_empty_node_selection_is_refused_on_client_and_server(inspector):
    server, file, _, _, _ = inspector
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/file?path=' + quote(str(file), safe=''))
        page.get_by_label('Distribute to compute', exact=True).uncheck()
        playwright.expect(page.get_by_role('button', name='Distribute', exact=True)).to_be_disabled()
        page.evaluate("document.querySelector('[data-file-distribute]').requestSubmit()")
        playwright.expect(page.locator('[data-file-page]')).to_be_visible()
        browser.close()
    refused = server.post('/file', data={'path': str(file), 'do': 'distribute'})
    assert refused.status_code == 400 and 'Choose at least one destination node.' in refused.text


def test_real_copy_move_distribution_and_confirmed_deletion(inspector):
    server, file, storage, node, _ = inspector
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/file?path=' + quote(str(file), safe=''))
        page.get_by_role('button', name='Distribute', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Distributing inspector.gguf.')
        deadline = time.monotonic() + 10
        target = node / 'llm' / file.name
        while not target.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert target.read_bytes() == file.read_bytes()
        page.goto('http://console.test/file?path=' + quote(str(file), safe=''))
        page.get_by_label('Destination', exact=True).select_option('storage_llm')
        page.get_by_role('button', name='Copy file', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Copying inspector.gguf to storage_llm.')
        copied = storage / 'llm' / file.name
        deadline = time.monotonic() + 10
        while not copied.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert copied.read_bytes() == file.read_bytes()
        deadline = time.monotonic() + 10
        while 'View transfer progress' in server.get('/file', params={'path': str(file)}).text and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        # The real server refuses overwriting the copy; errors render in the same inspector.
        refused = server.post('/file', data={'path': str(file), 'to': 'storage_llm', 'do': 'move'})
        assert refused.status_code == 400 and 'The destination already exists. No file was overwritten.' in refused.text
        assert 'data-file-page' in refused.text
        page.goto('http://console.test/file?path=' + quote(str(copied), safe=''))
        page.locator('.file-delete summary').click()
        page.locator('#name').fill(copied.name)
        page.get_by_role('button', name='Delete this file', exact=True).click()
        playwright.expect(page.locator('#danger-confirmation')).to_be_visible()
        assert copied.exists()
        page.locator('#danger-confirmation-check').check()
        page.get_by_role('button', name='Continue', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Deleted inspector.gguf.')
        playwright.expect(page.locator('#danger-confirmation')).to_be_hidden()
        assert not copied.exists() and file.exists() and target.exists()
        page.goto('http://console.test/file?path=' + quote(str(file), safe=''))
        page.get_by_label('Action', exact=True).select_option('move')
        page.get_by_label('Destination', exact=True).select_option('storage_llm')
        original = file.read_bytes()
        page.get_by_role('button', name='Move file', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Moving inspector.gguf to storage_llm.')
        deadline = time.monotonic() + 10
        while file.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert not file.exists() and copied.read_bytes() == original and target.exists()
        browser.close()


@pytest.mark.parametrize('kind', ['adapter', 'mmproj', 'unreadable'])
def test_companion_and_unreadable_states_remain_usable(inspector, kind):
    server, file, _, _, _ = inspector
    if kind == 'unreadable':
        file.write_bytes(b'invalid gguf')
    else:
        gguf(file, general__type=kind, general__architecture='clip' if kind == 'mmproj' else 'qwen3', general__name='Companion')
    server.post('/files', data={'do': 'refresh-files'})
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/file?path=' + quote(str(file), safe=''))
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if kind == 'unreadable':
            playwright.expect(page.locator('#file-metadata')).to_have_count(0)
            playwright.expect(page.get_by_role('heading', name='Inside the file')).to_be_visible()
        else:
            playwright.expect(page.get_by_role('button', name='Make the model', exact=True)).to_be_disabled()
        playwright.expect(page.get_by_role('button', name='Copy file', exact=True)).to_be_visible()
        browser.close()
