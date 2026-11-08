"""Model-file setup and optional front storage remain usable without an inference worker."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import re
import threading
import time

import pytest
import httpx

from test_weightroom import desk, gguf, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright
from loadcoach.settings import Changes, said
from loadcoach.models import Models


@pytest.mark.parametrize('front_settings', [True, False])
def test_cold_settings_and_files_are_accessible_without_compute(desk, front_settings):
    browser, compute, _ = desk
    compute.off = True
    compute.front.state.front_settings.enabled = front_settings
    sign_in(browser, 'jordan')
    for path in ('/files', '/settings', '/compute-nodes', '/users', '/groups', '/tools',
                 '/plugins', '/system/voice', '/system/audio', '/settings/image', '/models'):
        response = browser.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:500])
        assert 'Something went wrong' not in response.text
    files = browser.get('/files?complete=1').text
    assert 'Set up optional front cache or storage' in files
    assert 'store (compute)' not in files
    settings = browser.get('/settings').text
    assert 'id="models-folder"' in settings and 'Save file locations' in settings


def test_front_locations_work_without_compute_and_persist(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    cache, storage = tmp_path / 'front-cache', tmp_path / 'front-storage'
    saved = browser.post('/settings/model-files', data={
        'file_machine': 'front', 'models_folder': str(cache), 'models_storage_folder': str(storage)})
    assert saved.status_code == 200 and 'Front file locations are available now' in saved.text, saved.text[saved.text.find('said-box'):][:600]
    assert (cache / 'llm').is_dir() and (storage / 'image').is_dir()
    file = gguf(storage / 'llm' / 'front-model.gguf', general__name='Front model', general__architecture='qwen')
    listing = browser.get('/files?tier=storage&complete=1')
    assert listing.status_code == 200 and 'front-model.gguf' in listing.text and 'Front model' in listing.text
    assert 'No model-file locations are available yet' not in listing.text
    detail = browser.get('/file', params={'path': file})
    assert detail.status_code == 200 and 'Front model' in detail.text
    changed = compute.front.state.front_settings.changes.of('turnstile')
    assert changed['models_folder'] == str(cache) and changed['models_storage_folder'] == str(storage)
    sign_in(browser, 'pat')
    assert browser.get('/files').status_code == 403
    assert browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': '/tmp/forbidden'}).status_code == 403


@pytest.fixture
def categorized_files(desk, tmp_path):
    server, compute, _ = desk
    compute.off = True
    sign_in(server, 'jordan')
    cache, storage = tmp_path / 'cache', tmp_path / 'storage'
    server.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache),
                                             'models_storage_folder': str(storage)})
    files = {}
    for kind, folder in [('model', 'llm'), ('image', 'image'), ('audio', 'audio'),
                         ('adapter', 'adapters'), ('projector', 'projectors')]:
        filename = f'category-{kind}.safetensors' if kind == 'image' else f'category-{kind}.gguf'
        for tier, root in [('cache', cache), ('storage', storage)]:
            path = root / folder / filename
            if kind == 'image':
                path.write_bytes(b'\x00' * 8)
            else:
                gguf(path, general__name=kind, general__type=kind if kind in ('adapter', 'projector') else 'model')
            files[tier, kind] = path
    return server, files


@pytest.mark.parametrize('tier', ['cache', 'storage'])
@pytest.mark.parametrize('kind', ['model', 'image', 'audio', 'adapter', 'projector'])
def test_file_categories_filter_full_pages_and_polling(categorized_files, tier, kind):
    server, files = categorized_files
    listing = server.get('/files', params={'tier': tier, 'type': kind, 'complete': '1'})
    assert listing.status_code == 200
    assert f'hx-get="/files?part=list&amp;tier={tier}&amp;type={kind}"' in listing.text
    assert f'href="/files?tier=storage&amp;type={kind}"' in listing.text
    assert f'href="/files?tier=cache&amp;type={kind}"' in listing.text
    poll = server.get('/files', params={'tier': tier, 'type': kind, 'part': 'list'})
    assert poll.status_code == 200
    for body in (listing.text, poll.text):
        assert files[tier, kind].name in body
        assert all(path.name not in body for (location, other), path in files.items() if location == tier and other != kind)
    if tier == 'storage':
        assert 'In cache' in listing.text  # filtering retains knowledge of the matching cached copy
    else:
        assert poll.text.count('hx-swap-oob="true"') == 4


def test_file_category_keeps_location_totals_and_storage_actions(categorized_files, desk):
    server, files = categorized_files
    original = server.get('/files?tier=storage&complete=1').text
    filtered = server.get('/files?tier=storage&type=audio&complete=1').text
    model_bytes = lambda text: re.findall(r'<span class="text-secondary">Models</span> <b>([^<]+)</b>', text)
    assert model_bytes(original) == model_bytes(filtered)
    assert 'name="file_type" value="audio"' in filtered
    desk[1].off = False  # deleting a library file requires every node to verify it is unused
    path = files['storage', 'audio']
    deleted = server.post('/file', data={'do': 'delete', 'path': str(path), 'name': path.name,
                                        'return': 'storage', 'file_type': 'audio'})
    assert deleted.status_code == 200, re.findall(r'role="alert"[^>]*>(.*?)</div>', deleted.text, re.S)
    assert deleted.url.params['type'] == 'audio'
    empty = server.get(str(deleted.url) + '&complete=1')
    assert 'No matching files are in storage.' in empty.text
    assert all(path.exists() for (tier, kind), path in files.items() if tier == 'storage' and kind != 'audio')


def test_file_category_survives_location_changes_refresh_and_reload(categorized_files):
    server, files = categorized_files
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto('http://console.test/files')
            page.locator('nav[aria-label="File type"]').get_by_role('link', name='Audio', exact=True).click()
            playwright.expect(page.locator('#files-audio-table')).to_contain_text(files['cache', 'audio'].name)
            playwright.expect(page.locator('#files-models')).to_have_count(0)
            page.locator('nav[aria-label="File location"]').get_by_role('link', name='Storage', exact=True).click()
            selected = page.locator('nav[aria-label="File type"] [aria-current="page"]')
            playwright.expect(selected).to_have_text('Audio')
            playwright.expect(page.locator('#files-storage-table')).to_contain_text(files['storage', 'audio'].name)
            page.evaluate('window.priorFiles = document.querySelector("#files-storage")')
            page.wait_for_function('document.querySelector("#files-storage") !== window.priorFiles', timeout=10000)
            assert 'category-model.gguf' not in page.locator('#files-storage-table').inner_text()
            page.reload()
            playwright.expect(selected).to_have_text('Audio')
            page.set_viewport_size({'width': 390, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('nav[aria-label="File type"]').get_by_role('link', name='All files', exact=True).click()
            playwright.expect(page.locator('#files-storage-table')).to_contain_text(files['storage', 'model'].name)
            assert not errors
        finally:
            browser.close()


def test_compute_node_pill_lists_its_files_and_preserves_selection(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    cache, storage = tmp_path / 'front-cache', tmp_path / 'front-storage'
    server.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache),
                                             'models_storage_folder': str(storage)})
    gguf(cache / 'llm' / 'front-only.gguf', general__architecture='qwen')
    worker = tmp_path / 'worker'
    worker.mkdir(exist_ok=True)
    worker_file = gguf(worker / 'node-only.gguf', general__name='Node model', general__architecture='qwen')
    Models(str(tmp_path / 'loadcoach.db')).put('on-compute', {'flags': {'model': worker_file}})

    listing = server.get('/files?machine=compute&complete=1')
    assert listing.status_code == 200
    assert 'href="/files?machine=compute" aria-current="page">Compute</a>' in listing.text
    table = listing.text.split('id="files-node-table"', 1)[1].split('</table>', 1)[0]
    assert worker_file in table and 'Node model' in table and 'front-only.gguf' not in table
    assert 'href="/models/on-compute?node=compute"' in table
    assert 'href="/file?' not in table
    assert 'hx-get="/files?part=list&amp;machine=compute"' in listing.text
    part = server.get('/files?part=list&machine=compute').text
    assert 'id="files-node" hx-swap-oob="true"' in part and worker_file in part
    assert 'id="files-models"' not in part
    shell = server.get('/files?machine=compute').text
    assert 'machine=compute' in shell and 'part=page' in shell

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto('http://console.test/files')
            page.get_by_role('link', name='Compute', exact=True).click()
            playwright.expect(page.locator('nav[aria-label="File location"] [aria-current="page"]')).to_have_text('Compute')
            playwright.expect(page.locator('#files-node-table')).to_contain_text(worker_file)
            assert 'front-only.gguf' not in page.locator('#files-node-table').inner_text()
            page.evaluate('window.priorFiles = document.querySelector("#files-node")')
            page.wait_for_function('document.querySelector("#files-node") !== window.priorFiles', timeout=10000)
            playwright.expect(page.locator('#files-node-table')).to_contain_text(worker_file)
            page.reload()
            playwright.expect(page.locator('nav[aria-label="File location"] [aria-current="page"]')).to_have_text('Compute')
            assert not errors
        finally:
            browser.close()


def test_compute_node_files_distinguish_empty_offline_unknown_and_forbidden(desk):
    server, compute, _ = desk
    sign_in(server, 'jordan')
    empty = server.get('/files?machine=compute&complete=1')
    assert empty.status_code == 200 and 'No model files found on this node.' in empty.text
    assert server.get('/files?machine=missing&complete=1').status_code == 404
    compute.off = True
    offline = server.get('/files?machine=compute&complete=1')
    assert offline.status_code == 200 and 'This node is offline.' in offline.text
    assert 'No model files found on this node.' not in offline.text
    assert 'href="/files?machine=compute" aria-current="page">Compute</a>' in offline.text
    sign_in(server, 'pat')
    assert server.get('/files?machine=compute').status_code == 403
    assert server.get('/files?part=list&machine=compute').status_code == 403


def test_offline_node_locations_are_queued_then_delivered_only_to_that_node(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    cache, storage = str(tmp_path / 'node-cache'), str(tmp_path / 'node-storage')
    response = browser.post('/settings/model-files', data={
        'file_machine': 'compute', 'models_folder': cache, 'models_storage_folder': storage})
    assert response.status_code == 200 and 'Locations saved on front' in response.text
    pending = compute.front.state.front_settings.changes.of('node-files:compute')['pending']
    assert pending['locations'] == {'models_folder': cache, 'models_storage_folder': storage}
    assert compute.front.state.front_settings.changes.of('turnstile').get('models_folder', '') == ''
    compute.off = False
    Changes(str(tmp_path / 'loadcoach.db')).seen('loadcoach', said(
        {'models_folder': '', 'models_storage_folder': ''}, {}, {}))
    response = browser.get('/settings?file_machine=compute')
    assert response.status_code == 200 and 'Locations saved on front' not in response.text, compute.front.state.log.db.execute("SELECT what, detail FROM events WHERE kind='settings'").fetchall()
    assert cache in response.text and storage in response.text
    assert not compute.front.state.front_settings.changes.of('node-files:compute')


def test_front_paths_validate_before_creating_or_saving_folders(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    for cache, storage in (('relative', ''), (str(tmp_path / 'same'), str(tmp_path / 'same')),
                           (str(tmp_path / 'nested'), str(tmp_path / 'nested' / 'llm'))):
        response = browser.post('/settings/model-files', data={
            'file_machine': 'front', 'models_folder': cache, 'models_storage_folder': storage})
        assert response.status_code == 400
    assert not (tmp_path / 'nested').exists()
    assert compute.front.state.front_settings.changes.of('turnstile').get('models_folder', '') == ''


def test_front_library_promotes_without_overwriting_and_disabling_preserves_files(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    cache, storage = tmp_path / 'cache', tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache),
                                              'models_storage_folder': str(storage)})
    source = Path(gguf(storage / 'llm' / 'tiny.gguf', general__architecture='qwen'))
    original = source.read_bytes()
    promoted = browser.post('/file', data={'do': 'copy', 'path': str(source), 'to': 'llm', 'return': 'storage'})
    assert promoted.status_code == 200
    target = cache / 'llm' / source.name
    deadline = time.monotonic() + 5
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert source.read_bytes() == target.read_bytes() == original
    repeated = browser.post('/file', data={'do': 'copy', 'path': str(source), 'to': 'llm', 'return': 'storage'})
    assert 'already exists' in repeated.text and target.read_bytes() == original
    outside = tmp_path / 'private.gguf'
    outside.write_bytes(b'private')
    store, owner = compute.front.state.front_files, compute.front.state.users.by_name('jordan')
    escaped = browser.portal.call(store.request, owner, 'GET', '/loadcoach/file?path=' + str(outside))
    assert escaped.status_code == 400
    disabled = browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': '', 'models_storage_folder': ''})
    assert disabled.status_code == 200 and browser.get('/files').status_code == 200
    assert source.read_bytes() == target.read_bytes() == original


@pytest.mark.parametrize('action', ['copy', 'move'])
def test_front_promotion_returns_before_copy_finishes_and_prevents_conflicting_actions(desk, tmp_path, monkeypatch, action):
    from turnstile.artifacts import carry
    browser, compute, _ = desk
    sign_in(browser, 'jordan')
    cache, storage = tmp_path / 'cache', tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache),
                                              'models_storage_folder': str(storage)})
    source = Path(gguf(storage / 'llm' / 'slow.gguf', general__architecture='qwen'))
    original = source.read_bytes()
    started, release = threading.Event(), threading.Event()

    def slow_copy(source, target, progress):
        progress['done'] = 1
        started.set()
        if not release.wait(5):
            raise OSError('Copy was not released by the test.')
        carry(source, target, progress)

    monkeypatch.setattr('turnstile.artifacts.carry', slow_copy)
    form = {'do': action, 'path': str(source), 'to': 'llm', 'return': 'storage'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(browser.post, '/file', data=form, follow_redirects=False)
        try:
            assert started.wait(2)
            response = pending.result(timeout=1)
            assert response.status_code == 303 and 'tier=storage' in response.headers['location']
            inventory = browser.get('/files?tier=storage&complete=1').text
            assert ('copying' if action == 'copy' else 'moving') + ' to llm' in inventory
            assert 'Promote to cache' not in inventory
            assert not (cache / 'llm' / source.name).exists() and source.is_file()
            repeated = browser.post('/file', data=form)
            assert repeated.status_code == 400 and 'already being copied or moved' in repeated.text
            deleted = browser.post('/file', data={**form, 'do': 'delete', 'name': source.name})
            assert deleted.status_code == 400 and source.is_file()
            cleanup = browser.post('/files', data={'do': 'clear-cache'})
            assert 'Wait for active front file transfers' in cleanup.text
        finally:
            release.set()
    deadline = time.monotonic() + 5
    while compute.front.state.front_files.tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert (cache / 'llm' / source.name).read_bytes() == original
    assert source.exists() == (action == 'copy')
    if action == 'copy':
        assert source.read_bytes() == original
        assert 'In cache' in browser.get('/files?tier=storage&complete=1').text


def test_storage_promote_button_shows_progress_then_cached(desk, tmp_path, monkeypatch):
    from turnstile.artifacts import carry
    server, compute, _ = desk
    compute.off = True
    sign_in(server, 'jordan')
    cache, storage = tmp_path / 'cache', tmp_path / 'storage'
    server.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache),
                                             'models_storage_folder': str(storage)})
    source = Path(gguf(storage / 'llm' / 'browser.gguf', general__architecture='qwen'))
    release = threading.Event()

    def slow_copy(source, target, progress):
        if not release.wait(15):
            raise OSError('Copy was not released by the browser test.')
        carry(source, target, progress)

    monkeypatch.setattr('turnstile.artifacts.carry', slow_copy)
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto('http://console.test/files?tier=storage')
            page.get_by_role('button', name='Promote to cache', exact=True).click()
            playwright.expect(page.get_by_role('button', name='Copying…', exact=True)).to_be_disabled()
            assert 'The form could not be submitted' not in page.content()
            assert source.is_file() and not (cache / 'llm' / source.name).exists()
            release.set()
            playwright.expect(page.locator('#files-storage-table')).to_contain_text('In cache', timeout=10000)
            assert source.read_bytes() == (cache / 'llm' / source.name).read_bytes()
            assert not errors
        finally:
            release.set()
            browser.close()


def test_front_promotion_failure_is_visible_and_can_be_retried(desk, tmp_path, monkeypatch):
    from turnstile.artifacts import carry
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    cache, storage = tmp_path / 'cache', tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache),
                                              'models_storage_folder': str(storage)})
    source = Path(gguf(storage / 'llm' / 'retry.gguf', general__architecture='qwen'))
    def refused(*args):
        raise OSError('Cache drive refused the copy.')
    monkeypatch.setattr('turnstile.artifacts.carry', refused)
    form = {'do': 'copy', 'path': str(source), 'to': 'llm', 'return': 'storage'}
    response = browser.post('/file', data=form)
    assert response.status_code == 200
    deadline = time.monotonic() + 5
    while compute.front.state.front_files.tasks and time.monotonic() < deadline:
        time.sleep(.01)
    listing = browser.get('/files?tier=storage&complete=1').text
    assert 'Cache drive refused the copy.' in listing and 'Promote to cache' in listing
    assert source.is_file() and not (cache / 'llm' / source.name).exists()
    assert 'Cache drive refused the copy.' in browser.get('/file', params={'path': str(source)}).text
    monkeypatch.setattr('turnstile.artifacts.carry', carry)
    assert browser.post('/file', data=form).status_code == 200
    deadline = time.monotonic() + 5
    while compute.front.state.front_files.tasks and time.monotonic() < deadline:
        time.sleep(.01)
    listing = browser.get('/files?tier=storage&complete=1').text
    assert 'In cache' in listing and 'Cache drive refused the copy.' not in listing
    assert (cache / 'llm' / source.name).read_bytes() == source.read_bytes()


def test_queued_node_paths_recheck_permissions_before_delivery(desk, tmp_path):
    browser, compute, users = desk
    compute.off = True
    sign_in(browser, 'jordan')
    browser.post('/settings/model-files', data={'file_machine': 'compute', 'models_folder': str(tmp_path / 'cache'),
                                              'models_storage_folder': str(tmp_path / 'storage')})
    users.change('jordan', group='plain')
    compute.off = False
    browser.portal.call(compute.front.state.publish_file_settings)
    assert compute.front.state.front_settings.changes.of('node-files:compute').get('pending')
    assert not Changes(str(tmp_path / 'loadcoach.db')).of('loadcoach')


def test_offline_node_folder_reset_preserves_other_queued_locations(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage = str(tmp_path / 'storage')
    browser.post('/settings/model-files', data={'file_machine': 'compute', 'models_folder': str(tmp_path / 'cache'),
                                              'models_storage_folder': storage})
    reset = browser.post('/settings/loadcoach', data={'machine': 'compute', 'reset': 'models_folder'})
    assert reset.status_code == 200 and 'File locations are saved on front' in reset.text
    queued = compute.front.state.front_settings.changes.of('node-files:compute')['pending']['locations']
    assert queued == {'models_folder': None, 'models_storage_folder': storage}
    local = Changes(str(tmp_path / 'loadcoach.db'))
    local.seen('loadcoach', said({'models_folder': '', 'models_storage_folder': ''}, {}, {}))
    local.put('loadcoach', 'models_folder', str(tmp_path / 'old-cache'), 'jordan')
    compute.off = False
    browser.get('/settings?file_machine=compute')
    assert local.of('loadcoach') == {'models_storage_folder': storage}
    assert not compute.front.state.front_settings.changes.of('node-files:compute')


def test_front_downloads_use_the_existing_verified_hub_contract(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage = tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': '', 'models_storage_folder': str(storage)})
    store = compute.front.state.front_files
    browser.portal.call(store.web.aclose)
    store.web = compute.hub.client()
    page = browser.get('/hub?repository=acme/tiny-GGUF')
    assert page.status_code == 200 and 'tiny-q4.gguf' in page.text
    started = browser.post('/hub', data={'repository': 'acme/tiny-GGUF', 'file': 'tiny-q4.gguf', 'place': 'storage_llm'})
    assert started.status_code == 200 and 'coming down to storage_llm' in started.text
    target = storage / 'llm' / 'acme--tiny-GGUF' / 'tiny-q4.gguf'
    deadline = time.monotonic() + 3
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert target.read_bytes() == compute.hub.files['tiny-q4.gguf']
    assert not target.with_suffix('.gguf.part').exists()


def test_front_downloads_never_go_into_the_cache(desk, tmp_path):
    """With a cache alone on front, nothing is offered or accepted as a download destination."""
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    cache = tmp_path / 'cache'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache), 'models_storage_folder': ''})
    store = compute.front.state.front_files
    browser.portal.call(store.web.aclose)
    store.web = compute.hub.client()
    page = browser.get('/hub?repository=acme/tiny-GGUF')
    assert page.status_code == 200 and 'tiny-q4.gguf' in page.text and 'name="place"' not in page.text
    assert 'Set a model storage folder</a>' in page.text
    refused = browser.post('/hub', data={'repository': 'acme/tiny-GGUF', 'file': 'tiny-q4.gguf', 'place': 'llm'})
    assert 'never the cache' in refused.text and not (cache / 'llm' / 'acme--tiny-GGUF').exists()
    civitai = browser.post('/mirrorwall-discovery', json={'repository': 'civitai/123', 'files': ['a.safetensors'], 'place': 'image'})
    assert civitai.status_code == 400 and 'never the cache' in civitai.json()['error']['message']
    assert not (cache / 'image').exists() or not any((cache / 'image').iterdir())


def test_front_civitai_search_uses_civitai_metadata_and_current_preview_policy(desk, monkeypatch, tmp_path):
    """Front-only discovery keeps provider fields and cannot bypass the caller's preview policy."""
    from mirrorwall import civitai
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    assert browser.post('/settings/model-files', data={'file_machine': 'front',
        'models_folder': str(tmp_path / 'front-cache'), 'models_storage_folder': ''}).status_code == 200
    calls = []

    async def search(http, words, token='', adult=False):
        """Return the exact model-version contract expected by the discovery cards."""
        calls.append((words, adult))
        return [{'name': 'Test model · v1', 'repository': 'civitai/123', 'kind': 'Checkpoint',
                 'base_model': 'SDXL', 'url': 'https://civitai.com/models/1?modelVersionId=123'}]

    monkeypatch.setattr(civitai, 'search', search)
    response = browser.get('/mirrorwall-discovery?q=style')
    assert response.status_code == 200
    assert response.json()['found'][0]['name'] == 'Test model · v1'
    assert response.json()['found'][0]['kind'] == 'Checkpoint'
    assert calls == [('style', False)]

    async def forged_search():
        """Try an explicit adult flag through the same authenticated front artifact route."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=compute.front), base_url='http://front:8442') as client:
            return await client.get('/turnstile/artifacts/hub?source=civitai&search=style&adult=true',
                                    headers={'authorization': 'Bearer ' + browser.cookies['weightroom']})

    assert browser.portal.call(forged_search).status_code == 200
    assert calls[-1] == ('style', False)


def test_civitai_cards_reject_wrong_provider_data_and_show_model_metadata(desk):
    """Wrong-shaped provider results show a useful error rather than blank undefined cards."""
    server, _, _ = desk
    sign_in(server, 'jordan')
    result = {'found': [{'id': 'huggingface/repository'}]}

    def capture(route, path):
        """Exercise the real card renderer with both the regressed and corrected contracts."""
        if path.startswith('/mirrorwall-discovery?'):
            import json
            route.fulfill(status=200, content_type='application/json', body=json.dumps(result))
            return True
        return False

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/civitai')
        page.get_by_role('textbox', name='Search Civitai', exact=True).fill('style')
        page.get_by_role('button', name='Search', exact=True).click()
        playwright.expect(page.locator('#mw-message')).to_contain_text('incomplete model data')
        assert page.locator('#mw-found article').count() == 0
        result['found'] = [{'name': 'Test model · v1', 'repository': 'civitai/123', 'kind': 'Checkpoint',
                            'base_model': 'SDXL', 'url': 'https://civitai.com/models/1?modelVersionId=123'}]
        page.get_by_role('button', name='Search', exact=True).click()
        playwright.expect(page.locator('#mw-found .card-heading')).to_have_text('Test model · v1')
        playwright.expect(page.locator('#mw-found')).to_contain_text('Checkpoint · SDXL')
        assert 'undefined' not in page.locator('#mw-found').inner_text()
        assert not errors
        browser.close()


def test_front_cache_cleanup_keeps_conflicting_storage_copies(desk, tmp_path):
    browser, compute, _ = desk
    sign_in(browser, 'jordan')
    cache, storage = tmp_path / 'cache', tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(cache),
                                              'models_storage_folder': str(storage)})
    source = Path(gguf(cache / 'llm' / 'tiny.gguf', general__architecture='qwen'))
    original = source.read_bytes()
    retained = storage / 'llm' / source.name
    retained.write_bytes(original.replace(b'qwen', b'abcd'))
    store = compute.front.state.front_files
    response = browser.post('/files', data={'do': 'clear-cache'})
    assert response.status_code == 200
    deadline = time.monotonic() + 3
    while store.cleanup.get('state') == 'running' and time.monotonic() < deadline:
        time.sleep(.02)
    assert source.read_bytes() == original and retained.read_bytes() != original
    assert any('differ' in reason for reason in store.cleanup['failures'])
    retained.write_bytes(original)
    browser.post('/files', data={'do': 'clear-cache'})
    deadline = time.monotonic() + 3
    while store.cleanup.get('state') == 'running' and time.monotonic() < deadline:
        time.sleep(.02)
    assert not source.exists() and retained.read_bytes() == original


@pytest.mark.parametrize('width', [1440, 768, 390, 320])
def test_offline_storage_setup_and_model_list_spacing_in_browser(desk, tmp_path, width):
    server, compute, _ = desk
    compute.off = True
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto('http://console.test/files')
        page.get_by_role('link', name='Set up optional front cache or storage', exact=True).click()
        page.get_by_label('Active cache path', exact=True).fill(str(tmp_path / 'cache'))
        page.get_by_label('Storage library path', exact=True).fill(str(tmp_path / 'storage'))
        page.get_by_role('button', name='Save file locations', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Front file locations are available now')
        page.goto('http://console.test/files')
        page.locator('#files-places').wait_for()  # the file data comes after the page
        assert not page.get_by_text('No model-file locations are available yet.', exact=False).count()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://console.test/models')
        playwright.expect(page.locator('#models-table')).to_be_visible()
        listing = page.locator('#models').bounding_box()
        addition = page.locator('#add-model').bounding_box()
        assert addition['y'] - listing['y'] - listing['height'] >= 15
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
