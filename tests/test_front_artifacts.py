"""Model-file setup and optional control node storage remain usable without an inference worker."""

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
    compute.front.state.console_settings.enabled = front_settings
    sign_in(browser, 'jordan')
    for path in ('/files', '/settings', '/compute-nodes', '/users', '/groups', '/tools',
                 '/plugins', '/system/voice', '/system/audio', '/settings/image', '/models'):
        response = browser.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:500])
        assert 'Something went wrong' not in response.text
    files = browser.get('/files?complete=1').text
    assert 'Set up optional control node model storage' in files
    assert 'store (compute)' not in files
    settings = browser.get('/settings').text
    assert 'id="models-folder"' in settings and 'Save file locations' in settings


def test_front_locations_work_without_compute_and_persist(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage = tmp_path / 'front-storage'
    saved = browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage)})
    assert saved.status_code == 200 and 'Control node file locations are available now' in saved.text, saved.text[saved.text.find('said-box'):][:600]
    assert (storage / 'llm').is_dir() and (storage / 'image').is_dir()
    file = gguf(storage / 'llm' / 'front-model.gguf', general__name='Control node model', general__architecture='qwen')
    listing = browser.get('/files?complete=1')
    assert listing.status_code == 200 and 'front-model.gguf' in listing.text and 'Control node model' in listing.text
    assert 'No model-file locations are available yet' not in listing.text
    detail = browser.get('/file', params={'path': file})
    assert detail.status_code == 200 and 'Control node model' in detail.text
    changed = compute.front.state.console_settings.changes.of('turnstile')
    assert changed['models_folder'] == str(storage) and not changed.get('models_storage_folder')
    sign_in(browser, 'pat')
    assert browser.get('/files').status_code == 403
    assert browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': '/tmp/forbidden'}).status_code == 403


@pytest.fixture
def categorized_files(desk, tmp_path):
    server, compute, _ = desk
    compute.off = True
    sign_in(server, 'jordan')
    storage = tmp_path / 'storage'
    server.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage)})
    files = {}
    for kind, folder in [('model', 'llm'), ('image', 'image'), ('audio', 'audio'),
                         ('adapter', 'adapters'), ('projector', 'projectors')]:
        filename = f'category-{kind}.safetensors' if kind == 'image' else f'category-{kind}.gguf'
        path = storage / folder / filename
        if kind == 'image':
            path.write_bytes(b'\x00' * 8)
        else:
            gguf(path, general__name=kind, general__type=kind if kind in ('adapter', 'projector') else 'model')
        files[kind] = path
    return server, files


@pytest.mark.parametrize('kind', ['model', 'image', 'audio', 'adapter', 'projector'])
def test_file_categories_filter_full_pages_and_polling(categorized_files, kind):
    server, files = categorized_files
    listing = server.get('/files', params={'type': kind, 'complete': '1'})
    assert listing.status_code == 200
    assert f'hx-get="/files?part=list&amp;type={kind}"' in listing.text
    assert f'href="/files?type={kind}"' in listing.text  # the type pills
    assert 'href="/files"' in listing.text and 'tier=' not in listing.text  # the Storage row; there is no cache to choose
    poll = server.get('/files', params={'type': kind, 'part': 'list'})
    assert poll.status_code == 200
    for body in (listing.text, poll.text):
        assert files[kind].name in body
        assert all(path.name not in body for other, path in files.items() if other != kind)
    assert f'aria-label="{files[kind].name} in Storage: In storage.' in listing.text
    # Each polled piece of the page, with when the files were last checked and the maintenance panel.
    assert set(re.findall(r'id="([^"]+)"[^>]*hx-swap-oob="true"', poll.text)) == {
        'files-checked', 'files-distributions', 'files-places', 'files-storage', 'files-maintenance'}


def test_file_category_keeps_location_totals_and_storage_actions(categorized_files, desk):
    server, files = categorized_files
    original = server.get('/files?complete=1').text
    filtered = server.get('/files?type=audio&complete=1').text
    model_bytes = lambda text: re.findall(r'<span class="text-secondary">Models</span> <b>([^<]+)</b>', text)
    assert model_bytes(original) == model_bytes(filtered)
    assert 'name="file_type" value="audio"' in filtered
    desk[1].off = False  # deleting a library file requires every node to verify it is unused
    path = files['audio']
    deleted = server.post('/file', data={'do': 'delete', 'path': str(path), 'name': path.name,
                                        'return': 'storage', 'file_type': 'audio'})
    assert deleted.status_code == 200, re.findall(r'role="alert"[^>]*>(.*?)</div>', deleted.text, re.S)
    assert deleted.url.params['type'] == 'audio'
    left = server.get(str(deleted.url) + '&complete=1')
    assert path.name not in left.text.split('id="files-storage-table"', 1)[1] and not path.exists()
    assert all(path.exists() for kind, path in files.items() if kind != 'audio')


def test_file_category_survives_polling_refresh_and_reload(categorized_files):
    server, files = categorized_files
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto('http://console.test/files')
            # One library table lists every location's copy; the type pills narrow it.
            page.locator('nav[aria-label="File type"]').get_by_role('link', name='Audio', exact=True).click()
            selected = page.locator('nav[aria-label="File type"] [aria-current="page"]')
            playwright.expect(selected).to_have_text('Audio')
            table = page.locator('#files-storage-table')
            playwright.expect(table).to_contain_text(files['audio'].name)
            assert 'category-model.gguf' not in table.inner_text()
            page.evaluate('window.priorFiles = document.querySelector("#files-storage")')
            page.wait_for_function('document.querySelector("#files-storage") !== window.priorFiles', timeout=10000)
            assert 'category-model.gguf' not in page.locator('#files-storage-table').inner_text()
            page.reload()
            playwright.expect(selected).to_have_text('Audio')
            page.set_viewport_size({'width': 390, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('nav[aria-label="File type"]').get_by_role('link', name='All files', exact=True).click()
            playwright.expect(page.locator('#files-storage-table')).to_contain_text(files['model'].name)
            assert not errors
        finally:
            browser.close()


def test_compute_node_files_join_the_library_and_survive_polling(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    storage = tmp_path / 'front-storage'
    server.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage)})
    gguf(storage / 'llm' / 'front-only.gguf', general__architecture='qwen')
    worker = tmp_path / 'worker'
    worker.mkdir(exist_ok=True)
    worker_file = gguf(worker / 'node-only.gguf', general__name='Node model', general__architecture='qwen')
    Models(str(tmp_path / 'loadcoach.db')).put('on-compute', {'flags': {'model': worker_file}})

    listing = server.get('/files?machine=compute&complete=1')
    assert listing.status_code == 200
    # The node's files join the one library table, beside the control node's, with a column for the node.
    table = listing.text.split('id="files-storage-table"', 1)[1].split('</table>', 1)[0]
    assert 'node-only.gguf' in table and 'Node model' in table and 'front-only.gguf' in table and '>compute<' in table
    assert 'href="/files?machine=compute&amp;type=model"' in listing.text  # the type pills keep the node
    assert 'hx-get="/files?part=list&amp;machine=compute"' in listing.text
    part = server.get('/files?part=list&machine=compute').text
    assert 'id="files-storage" hx-swap-oob="true"' in part and 'node-only.gguf' in part
    shell = server.get('/files?machine=compute').text
    assert 'machine=compute' in shell and 'part=page' in shell

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto('http://console.test/files?machine=compute')
            playwright.expect(page.locator('#files-storage-table')).to_contain_text('node-only.gguf')
            page.evaluate('window.priorFiles = document.querySelector("#files-storage")')
            page.wait_for_function('document.querySelector("#files-storage") !== window.priorFiles', timeout=10000)
            playwright.expect(page.locator('#files-storage-table')).to_contain_text('node-only.gguf')
            page.reload()
            playwright.expect(page.locator('#files-storage-table')).to_contain_text('node-only.gguf')
            assert not errors
        finally:
            browser.close()


def test_compute_node_files_distinguish_empty_offline_unknown_and_forbidden(desk):
    server, compute, _ = desk
    sign_in(server, 'jordan')

    def node_row(text):
        """The compute node's row in the Locations table, as plain text."""
        places = text.split('id="files-places"', 1)[1]
        row = re.search(r'<tr[^>]*>(?:(?!</tr>).)*?>compute<.*?</tr>', places, re.S).group(0)
        return ' '.join(re.sub(r'<[^>]+>', ' ', row).split())

    empty = server.get('/files?machine=compute&complete=1')
    assert empty.status_code == 200 and 'No folders configured.' in node_row(empty.text)
    assert 'No model files found.' in empty.text
    assert server.get('/files?machine=missing&complete=1').status_code == 404
    compute.off = True
    offline = server.get('/files?machine=compute&complete=1')
    assert offline.status_code == 200 and 'This node is offline.' in node_row(offline.text)
    assert 'No folders configured.' not in node_row(offline.text)
    sign_in(server, 'pat')
    assert server.get('/files?machine=compute').status_code == 403
    assert server.get('/files?part=list&machine=compute').status_code == 403


def test_offline_node_locations_are_queued_then_delivered_only_to_that_node(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage = str(tmp_path / 'node-storage')
    response = browser.post('/settings/model-files', data={'file_machine': 'compute', 'models_folder': storage})
    assert response.status_code == 200 and 'Locations saved on the control node' in response.text
    pending = compute.front.state.console_settings.changes.of('node-files:compute')['pending']
    assert pending['locations'] == {'models_folder': storage}  # the retired second folder is left as it is
    assert compute.front.state.console_settings.changes.of('turnstile').get('models_folder', '') == ''
    compute.off = False
    Changes(str(tmp_path / 'loadcoach.db')).seen('loadcoach', said(
        {'models_folder': '', 'models_storage_folder': ''}, {}, {}))
    response = browser.get('/settings?file_machine=compute')
    assert response.status_code == 200 and 'Locations saved on the control node' not in response.text, compute.front.state.log.db.execute("SELECT what, detail FROM events WHERE kind='settings'").fetchall()
    assert storage in response.text and 'name="models_storage_folder"' not in response.text
    assert not compute.front.state.console_settings.changes.of('node-files:compute')


def test_front_paths_validate_before_creating_or_saving_folders(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    # A relative path, and a retired second folder inside model storage, are refused before anything is made.
    for form in ({'models_folder': 'relative'}, {'models_folder': str(tmp_path / 'nested'), 'models_storage_folder': str(tmp_path / 'nested' / 'llm')}):
        response = browser.post('/settings/model-files', data={'file_machine': 'front', **form})
        assert response.status_code == 400
    assert not (tmp_path / 'nested').exists()
    assert compute.front.state.console_settings.changes.of('turnstile').get('models_folder', '') == ''


def former_folder(browser, storage, former):
    """Control node model storage, beside a retired second folder an older installation still has."""
    saved = browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage),
                                                       'models_storage_folder': str(former)})
    assert saved.status_code == 200, saved.text[saved.text.find('said-box'):][:400]


def test_a_former_folders_file_copies_into_model_storage_without_overwriting_and_disabling_preserves_files(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage, former = tmp_path / 'storage', tmp_path / 'former'
    former_folder(browser, storage, former)
    source = Path(gguf(former / 'llm' / 'tiny.gguf', general__architecture='qwen'))
    original = source.read_bytes()
    detail = browser.get('/file', params={'path': str(source)}).text
    assert '<option value="llm" selected>' in detail  # moving out of the former folder is what is offered
    promoted = browser.post('/file', data={'do': 'copy', 'path': str(source), 'to': 'llm', 'return': 'storage'})
    assert promoted.status_code == 200
    target = storage / 'llm' / source.name
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
def test_front_copy_returns_before_it_finishes_and_prevents_conflicting_actions(desk, tmp_path, monkeypatch, action):
    from turnstile.artifacts import carry
    browser, compute, _ = desk
    sign_in(browser, 'jordan')
    storage, former = tmp_path / 'storage', tmp_path / 'former'
    former_folder(browser, storage, former)
    source = Path(gguf(former / 'llm' / 'slow.gguf', general__architecture='qwen'))
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
            assert response.status_code == 303 and response.headers['location'].startswith('/files?')
            inventory = browser.get('/files?complete=1').text
            assert ('copying' if action == 'copy' else 'moving') + ' to llm' in inventory, re.findall(r'aria-label="slow[^"]*"[^>]*>', inventory)
            assert f'aria-label="{source.name} in Storage: It is being copied or moved now." checked disabled' in inventory, re.findall(r'aria-label="slow[^"]*"[^>]*>', inventory)
            assert not (storage / 'llm' / source.name).exists() and source.is_file()
            repeated = browser.post('/file', data=form)
            assert repeated.status_code == 400 and 'already being copied or moved' in repeated.text
            deleted = browser.post('/file', data={**form, 'do': 'delete', 'name': source.name})
            assert deleted.status_code == 400 and source.is_file()
        finally:
            release.set()
    deadline = time.monotonic() + 5
    while compute.front.state.front_files.tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert (storage / 'llm' / source.name).read_bytes() == original
    assert source.exists() == (action == 'copy')
    if action == 'copy':
        assert source.read_bytes() == original


def test_a_front_copy_failure_is_visible_and_can_be_retried(desk, tmp_path, monkeypatch):
    from turnstile.artifacts import carry
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage, former = tmp_path / 'storage', tmp_path / 'former'
    former_folder(browser, storage, former)
    source = Path(gguf(former / 'llm' / 'retry.gguf', general__architecture='qwen'))
    def refused(*args):
        raise OSError('The storage drive refused the copy.')
    monkeypatch.setattr('turnstile.artifacts.carry', refused)
    form = {'do': 'copy', 'path': str(source), 'to': 'llm', 'return': 'storage'}
    response = browser.post('/file', data=form)
    assert response.status_code == 200
    deadline = time.monotonic() + 5
    while compute.front.state.front_files.tasks and time.monotonic() < deadline:
        time.sleep(.01)
    listing = browser.get('/files?complete=1').text
    assert 'The storage drive refused the copy.' in listing
    assert source.is_file() and not (storage / 'llm' / source.name).exists()
    assert 'The storage drive refused the copy.' in browser.get('/file', params={'path': str(source)}).text
    monkeypatch.setattr('turnstile.artifacts.carry', carry)
    assert browser.post('/file', data=form).status_code == 200
    deadline = time.monotonic() + 5
    while compute.front.state.front_files.tasks and time.monotonic() < deadline:
        time.sleep(.01)
    listing = browser.get('/files?complete=1').text
    assert 'The storage drive refused the copy.' not in listing
    assert (storage / 'llm' / source.name).read_bytes() == source.read_bytes()


def test_queued_node_paths_recheck_permissions_before_delivery(desk, tmp_path):
    browser, compute, users = desk
    compute.off = True
    sign_in(browser, 'jordan')
    browser.post('/settings/model-files', data={'file_machine': 'compute', 'models_folder': str(tmp_path / 'storage')})
    users.change('jordan', group='plain')
    compute.off = False
    browser.portal.call(compute.front.state.publish_file_settings)
    assert compute.front.state.console_settings.changes.of('node-files:compute').get('pending')
    assert not Changes(str(tmp_path / 'loadcoach.db')).of('loadcoach')


def test_offline_node_folder_reset_preserves_other_queued_locations(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage = str(tmp_path / 'former')  # a retired second folder, still set on an older node
    browser.post('/settings/model-files', data={'file_machine': 'compute', 'models_folder': str(tmp_path / 'storage'),
                                              'models_storage_folder': storage})
    reset = browser.post('/settings/loadcoach', data={'machine': 'compute', 'reset': 'models_folder'})
    assert reset.status_code == 200 and 'File locations are saved on the control node' in reset.text
    queued = compute.front.state.console_settings.changes.of('node-files:compute')['pending']['locations']
    assert queued == {'models_folder': None, 'models_storage_folder': storage}
    local = Changes(str(tmp_path / 'loadcoach.db'))
    local.seen('loadcoach', said({'models_folder': '', 'models_storage_folder': ''}, {}, {}))
    local.put('loadcoach', 'models_folder', str(tmp_path / 'old-storage'), 'jordan')
    compute.off = False
    browser.get('/settings?file_machine=compute')
    assert local.of('loadcoach') == {'models_storage_folder': storage}
    assert not compute.front.state.console_settings.changes.of('node-files:compute')


def test_front_downloads_use_the_existing_verified_hub_contract(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    storage = tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage)})
    store = compute.front.state.front_files
    browser.portal.call(store.web.aclose)
    store.web = compute.hub.client()
    page = browser.get('/hub?repository=acme/tiny-GGUF')
    assert page.status_code == 200 and 'tiny-q4.gguf' in page.text
    started = browser.post('/hub', data={'repository': 'acme/tiny-GGUF', 'file': 'tiny-q4.gguf', 'place': 'llm'})
    assert started.status_code == 200 and 'coming down to llm' in started.text
    target = storage / 'llm' / 'acme--tiny-GGUF' / 'tiny-q4.gguf'
    deadline = time.monotonic() + 3
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert target.read_bytes() == compute.hub.files['tiny-q4.gguf']
    assert not target.with_suffix('.gguf.part').exists()


def test_front_download_uses_its_saved_token_with_compute_offline(desk, tmp_path):
    browser, compute, _ = desk
    sign_in(browser, 'jordan')
    compute.off = True
    storage = tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage)})
    store = compute.front.state.front_files
    browser.portal.call(store.web.aclose)
    store.web = compute.hub.client()
    compute.hub.closed = True
    endpoint = '/settings/downloads/front/credentials/hub_token'
    for token in ['fake-old-token', 'good']:
        saved = browser.post(endpoint, data={'operation': 'save', 'credential': token})
        assert saved.status_code == 200
    page = browser.get('/hub?repository=acme/tiny-GGUF')
    assert page.status_code == 200 and 'tiny-q4.gguf' in page.text
    assert compute.hub.seen[-1][2]['authorization'] == 'Bearer good'
    started = browser.post('/hub', data={'repository': 'acme/tiny-GGUF', 'file': 'tiny-q4.gguf', 'place': 'llm'})
    assert started.status_code == 200 and 'coming down to llm' in started.text
    target = storage / 'llm' / 'acme--tiny-GGUF' / 'tiny-q4.gguf'
    deadline = time.monotonic() + 3
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert target.read_bytes() == compute.hub.files['tiny-q4.gguf']
    assert all(headers.get('authorization') == 'Bearer good'
               for host, path, headers in compute.hub.seen if host == 'huggingface.co')


def test_front_civitai_uses_current_provider_token_for_discovery_and_download(desk, tmp_path, monkeypatch):
    from mirrorwall import civitai
    browser, compute, _ = desk
    sign_in(browser, 'jordan')
    compute.off = True
    storage = tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage)})
    captured = []
    async def search(http, words, token='', adult=False):
        captured.append(('search', token))
        return []
    async def files(http, repository, token=''):
        captured.append(('files', token))
        return [{'path': 'model.safetensors', 'size': 4, 'sha256': 'a'*64}]
    async def fetch(http, repository, entry, target, token, progress):
        captured.append(('fetch', token))
        target.write_bytes(b'data')
    monkeypatch.setattr(civitai, 'search', search)
    monkeypatch.setattr(civitai, 'files', files)
    monkeypatch.setattr(civitai, 'fetch', fetch)
    for token in ['fake-first-civitai', 'fake-current-civitai']:
        saved = browser.post('/settings/downloads/front/credentials/civitai_token',
                             data={'operation': 'save', 'credential': token})
        assert saved.status_code == 200
        assert browser.get('/mirrorwall-discovery?q=style').status_code == 200
    assert captured == [('search', 'fake-first-civitai'), ('search', 'fake-current-civitai')]
    started = browser.post('/mirrorwall-discovery', json={'repository': 'civitai/123',
                           'files': ['model.safetensors'], 'place': 'image'})
    assert started.status_code == 202, started.text
    target = storage / 'image' / 'civitai--123' / 'model.safetensors'
    deadline = time.monotonic() + 3
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert target.read_bytes() == b'data'
    assert captured[-2:] == [('files', 'fake-current-civitai'), ('fetch', 'fake-current-civitai')]


def test_front_downloads_need_model_storage_and_offer_its_llm_folder_first(desk, tmp_path):
    """With no model storage on the control node, nothing is offered or accepted as a download destination."""
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, 'jordan')
    store, owner = compute.front.state.front_files, compute.front.state.users.by_name('jordan')
    browser.portal.call(store.web.aclose)
    store.web = compute.hub.client()
    refused = browser.portal.call(store.request, owner, 'POST', '/loadcoach/downloads',
                                  b'{"repository": "acme/tiny-GGUF", "files": ["tiny-q4.gguf"], "place": "llm"}')
    assert refused.status_code == 400 and 'Set a model storage folder' in refused.json()['error']['message']
    listed = browser.portal.call(store.request, owner, 'GET', '/loadcoach/hub?repository=acme/tiny-GGUF', b'').json()
    assert listed['places'] == []
    storage = tmp_path / 'storage'
    browser.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': str(storage)})
    page = browser.get('/hub?repository=acme/tiny-GGUF').text
    assert '<option value="llm" selected>' in page and 'Download to model storage' in page


def test_front_civitai_search_uses_civitai_metadata_and_current_preview_policy(desk, monkeypatch, tmp_path):
    """Control-node-only discovery keeps provider fields and cannot bypass the caller's preview policy."""
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
        """Try an explicit adult flag through the same authenticated control node artifact route."""
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


@pytest.mark.parametrize('width', [1440, 768, 390, 320])
def test_offline_storage_setup_and_model_list_spacing_in_browser(desk, tmp_path, width):
    server, compute, _ = desk
    compute.off = True
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto('http://console.test/files')
        page.get_by_role('link', name='Set up optional control node model storage', exact=True).click()
        playwright.expect(page.get_by_label('Former second model folder', exact=True)).to_have_count(0)
        page.get_by_label('Model storage path', exact=True).fill(str(tmp_path / 'storage'))
        page.get_by_role('button', name='Save file locations', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Control node file locations are available now')
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
