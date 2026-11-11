"""Live Node navigator contracts over isolated services, without host hardware access."""

import httpx
import pytest
from urllib.parse import parse_qs
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import door, machine_key
from loadcoach import statement
from loadcoach.users import Users
from test_console_browser import browser_context, playwright
from test_host_settings import Hosts, host
from test_weightroom import GROUPS, desk, sign_in  # noqa: F401
from turnstile.app import Settings as FrontSettings, build_app as build_front
from weightroom.app import Settings, build_app

expect = playwright.expect


@pytest.fixture
def nodes(tmp_path):
    users = Users(str(tmp_path / 'accounts.db'), GROUPS)
    users.add('jordan', 'admin')
    users.add('panel', 'front')
    admin, panel = users.new_key('jordan'), users.new_key('panel')
    (tmp_path / 'panel.key').write_text(panel)
    primary_key, remote_key = machine_key(tmp_path), str(tmp_path / 'remote.key')
    statement.replace_key(remote_key, 'r' * 40)
    primary = {'loadcoach': {'host_ram_headroom_mib': 512, 'card_margin': 1024, 'model_defaults': {'cache-type-k': 'q8_0', 'cache-type-v': 'q8_0', 'flash-attn': 'on'}, 'models_folder': '/srv/cache', 'models_storage_folder': '/srv/storage', 'keep_text': 50}, 'spotter': {'sleep_after': 0, 'files_every': 10}}
    secondary = {'loadcoach': {'host_ram_headroom_mib': 768, 'card_margin': 1024, 'model_defaults': {'cache-type-k': 'f16'}, 'models_folder': '/remote/cache', 'models_storage_folder': '/remote/storage', 'keep_text': 20}, 'spotter': {'sleep_after': 45, 'files_every': 20}}
    async def identity(request):
        return JSONResponse({'models': [], 'chat': {'models': {}}})

    workers = [host(primary_key, primary), host(remote_key, secondary)]
    for worker in workers:
        worker.app.router.routes.insert(0, Route('/loadcoach/me', identity))
    wire = Hosts(*workers)
    front = build_front(FrontSettings(compute='compute', data=str(tmp_path), machine_key_file=primary_key,
        auth_key_file=str(tmp_path / 'panel.key'), machines_poll_seconds=3600,
        machines={'remote': {'compute': 'http://compute:9442', 'machine_key_file': remote_key}}),
        httpx.AsyncClient(transport=wire), users=users)
    with TestClient(front, base_url='http://front:8442') as api:
        api.portal.call(front.state.machines.poll)
        with TestClient(build_app(Settings(compute='http://front:8442', key=panel, data=str(tmp_path / 'web')), door(front, 8442))) as server:
            server.cookies.set('weightroom', admin)
            yield server, primary, secondary, front


def test_drafts_follow_nodes_and_section_saves_do_not_erase_other_edits(nodes):
    server, primary, secondary, _ = nodes
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/compute-nodes')
        reserve = page.locator('[name="value-host_ram_headroom_mib"]')
        reserve.fill('4096')
        page.locator('#manage-gpus [name="value-card_margin"]').fill('2048')
        page.locator('[name="value-models_folder"]').fill('/draft/cache')
        page.locator('[data-node-choice="remote"]').click()
        expect(page.locator('#selected-node-name')).to_have_text('remote')
        expect(reserve).to_have_value('768')
        reserve.fill('2048')
        page.locator('[data-node-choice="compute"]').click()
        expect(reserve).to_have_value('4096')
        expect(page.locator('[name="value-models_folder"]')).to_have_value('/draft/cache')
        page.get_by_role('button', name='Save GPU and RAM policy').click()
        expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert primary['loadcoach']['host_ram_headroom_mib'] == 4096
        assert primary['loadcoach']['card_margin'] == 2048
        assert secondary['loadcoach']['card_margin'] == 1024
        assert primary['loadcoach']['models_folder'] == '/srv/cache'
        assert secondary['loadcoach']['host_ram_headroom_mib'] == 768
        expect(page.locator('[name="value-models_folder"]')).to_have_value('/draft/cache')
        expect(page.locator('[data-node-draft-status]')).to_contain_text('1 group')
        page.locator('[data-node-choice="remote"]').click()
        expect(reserve).to_have_value('2048')
        page.locator('[data-node-choice="compute"]').click()
        expect(reserve).to_have_value('4096')
        defaults = page.locator('[aria-label="llama.cpp loading defaults"]')
        defaults.locator('details').first.locator('summary').click()
        defaults.locator('[name="value-model_defaults"]').fill('cache-type-k = "q4_0"\ncache-type-v = "q8_0"\nflash-attn = "on"')
        defaults.get_by_role('button', name='Save loadcoach', exact=True).click()
        expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert primary['loadcoach']['model_defaults'] == {'cache-type-k': 'q4_0', 'cache-type-v': 'q8_0', 'flash-attn': 'on'}
        assert secondary['loadcoach']['model_defaults'] == {'cache-type-k': 'f16'}
        logs = page.locator('[data-card-key="settings:Logs and retention"]')
        logs.locator('[name="value-files_every"]').fill('15')
        logs.get_by_role('button', name='Save spotter', exact=True).click()
        expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert primary['spotter']['files_every'] == 15 and secondary['spotter']['files_every'] == 20
        expect(page.locator('[name="value-models_folder"]')).to_have_value('/draft/cache')
        assert not errors
        browser.close()


def test_filters_persist_through_swaps_and_lifecycle_confirmation(nodes):
    server, _, _, _ = nodes
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/compute-nodes')
        page.get_by_label('Find a node', exact=True).fill('remote')
        expect(page.locator('[data-node-choice="compute"]')).to_be_hidden()
        page.locator('[data-node-choice="remote"]').click()
        expect(page.get_by_label('Find a node', exact=True)).to_have_value('remote')
        page.get_by_label('Find a node', exact=True).fill('no-match')
        expect(page.locator('[data-node-empty]')).to_be_visible()
        page.get_by_label('Find a node', exact=True).fill('')
        page.get_by_role('button', name='Disable node', exact=True).click()
        expect(page.locator('#node-lifecycle')).to_contain_text('remote is active')
        page.get_by_role('button', name='Are you sure? Disable node', exact=True).click()
        expect(page.locator('#node-lifecycle')).to_contain_text('remote is disabled')
        page.get_by_label('Filter nodes by status').select_option('attention')
        expect(page.locator('[data-node-choice="compute"]')).to_be_hidden()
        expect(page.locator('[data-node-choice="remote"]')).to_be_visible()
        page.get_by_role('button', name='Retire node', exact=True).click()
        page.get_by_role('button', name='Are you sure? Retire node', exact=True).click()
        expect(page.locator('#node-lifecycle')).to_contain_text('remote is retired')
        expect(page.locator('[data-node-empty]')).to_be_visible()
        assert not page.locator('form[data-node-draft]').count()
        page.get_by_label('Filter nodes by status').select_option('retired')
        expect(page.locator('[data-node-choice="remote"]')).to_be_visible()
        browser.close()


def test_edits_made_during_a_save_remain_unsent_even_when_returning_to_original_value(nodes):
    server, primary, _, _ = nodes
    pending = []

    def capture(route, path):
        if path == '/settings/loadcoach' and route.request.method == 'POST':
            pending.append(route)
            return True
        return False

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        page.goto('http://console.test/compute-nodes')
        reserve = page.locator('[name="value-host_ram_headroom_mib"]')
        reserve.fill('4096')
        page.get_by_role('button', name='Save GPU and RAM policy').click()
        assert pending
        reserve.fill('512')
        route = pending.pop()
        response = server.post('/settings/loadcoach', content=route.request.post_data_buffer,
                               headers={'content-type': 'application/x-www-form-urlencoded', 'HX-Request': 'true'})
        route.fulfill(status=response.status_code, content_type='text/html', body=response.content)
        expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert primary['loadcoach']['host_ram_headroom_mib'] == 4096
        expect(reserve).to_have_value('512')
        expect(page.locator('[data-node-draft-status]')).to_contain_text('1 group')
        browser.close()


def test_rejected_save_keeps_drafts_when_switching_nodes(nodes):
    server, primary, _, _ = nodes

    def capture(route, path):
        if path == '/settings/loadcoach' and route.request.method == 'POST':
            fields = {key: values[-1] for key, values in parse_qs(route.request.post_data).items()}
            fields['value-keep_text'] = 'not-a-number'
            response = server.post(path, data=fields)
            assert response.status_code == 400
            route.fulfill(status=400, content_type='text/html', body=response.content)
            return True
        return False

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        page.goto('http://console.test/compute-nodes')
        cache = page.locator('[name="value-models_folder"]')
        cache.fill('/draft/cache')
        cache.locator('xpath=ancestor::form').get_by_role('button', name='Save loadcoach', exact=True).click()
        expect(page.locator('.said-box.alert-danger')).to_be_visible()
        page.locator('[data-node-choice="remote"]').click()
        page.locator('[data-node-choice="compute"]').click()
        expect(cache).to_have_value('/draft/cache')
        assert primary['loadcoach']['models_folder'] == '/srv/cache'
        browser.close()


def test_pairing_panel_generates_real_code_without_losing_node_drafts(desk):
    server, _, _ = desk
    sign_in(server, 'sam')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/compute-nodes')
        expect(page.locator('#add-node')).to_be_hidden()
        page.locator('[name="rotation_days"]').fill('45')
        page.get_by_role('button', name='+ Add GPU node', exact=True).click()
        expect(page.locator('#add-node')).to_be_visible()
        page.get_by_role('button', name='Make a pairing code', exact=True).click()
        expect(page.locator('#pairing-command')).to_contain_text('--join http://testserver:8440 --code ')
        expect(page.locator('[name="rotation_days"]')).to_have_value('45')
        command = page.locator('#pairing-command').inner_text()
        page.locator('#rotation-days').fill('60')
        page.get_by_role('button', name='Save schedule', exact=True).click()
        expect(page.locator('.said-box')).to_contain_text('every 60 days')
        expect(page.locator('#pairing-command')).to_have_text(command)
        page.keyboard.press('Escape')
        expect(page.locator('#add-node')).to_be_hidden()
        expect(page.locator('[data-toggle-pairing]')).to_be_focused()
        browser.close()


def test_live_layout_fits_each_width_theme_and_has_named_fields(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, 'jordan')
    # Prime the complete compute settings report, as the running worker does at startup.
    server.post('/compute-nodes/gpus', data={'machine': 'compute'})
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/compute-nodes')
        expect(page.locator('#manage-gpus [name="value-card_margin"]')).to_have_count(1)
        expect(page.locator('[aria-label="llama.cpp runtime"] [name="value-model_defaults"]')).to_have_count(1)
        expect(page.locator('[data-card-key="settings:Advanced"], [data-card-key="settings:RAM policy"]')).to_have_count(0)
        assert page.locator('[name="value-model_defaults"]').evaluate('el => el.form.getAttribute("action")') == '/settings/loadcoach'
        page.screenshot(path=str(tmp_path / 'compute-nodes-default.png'), full_page=True)
        for theme in ('light', 'dark'):
            page.evaluate('(theme) => {document.documentElement.dataset.bsTheme = theme}', theme)
            for width in (1600, 1440, 1024, 768, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                page.locator('.compute-node-panels details').evaluate_all('items => items.forEach(item => {item.open = true})')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
                assert page.evaluate('''() => [...document.querySelectorAll('main input, main select, main textarea')]
                    .filter(el => el.getBoundingClientRect().width)
                    .every(el => el.labels?.length || el.getAttribute('aria-label'))''')
                assert page.evaluate('''() => { const ids = [...document.querySelectorAll('[id]')].map(el => el.id); return ids.length === new Set(ids).size; }''')
                if theme == 'light' and width in (1440, 390):
                    page.screenshot(path=str(tmp_path / f'compute-nodes-{width}.png'), full_page=True)
        assert not errors
        browser.close()


def test_default_selection_handles_historical_and_empty_fleets(nodes):
    server, _, _, front = nodes
    # Historical-only fleets must remain navigable without a node named compute.
    machines = front.state.machines
    machines.names = []
    machines.seen.clear()
    machines.lifecycle = {'retained': {'state': 'retired', 'changed_at': 1000, 'changed_by': 'jordan'}}
    response = server.get('/compute-nodes')
    assert response.status_code == 200 and 'id="selected-node-name"' in response.text and '>retained</h2>' in response.text
    assert server.get('/compute-nodes?machine=missing').status_code == 404
    machines.lifecycle = {}
    response = server.get('/compute-nodes')
    assert response.status_code == 200 and 'No compute nodes yet' in response.text


def test_pairing_keeps_the_selected_node_and_supports_plain_html_navigation(nodes):
    server, _, _, front = nodes
    front.state.users.add('sam', 'superuser')
    server.cookies.set('weightroom', front.state.users.new_key('sam'))
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/compute-nodes?machine=remote')
        page.get_by_role('button', name='+ Add GPU node', exact=True).click()
        page.get_by_role('button', name='Make a pairing code', exact=True).click()
        expect(page.locator('#pairing-command')).to_contain_text('--code ')
        expect(page.locator('#selected-node-name')).to_have_text('remote')
        page.locator('[data-node-choice="compute"]').click()
        expect(page.locator('#selected-node-name')).to_have_text('compute')
        expect(page.locator('#pairing-command')).to_be_visible()
        expect(page.locator('#add-node [name=machine]')).to_have_value('compute')
        browser.close()
    # With scripts disabled, the same top action opens the frame through its GET form.
    response = server.get('/compute-nodes?machine=remote&open=add-node')
    assert response.status_code == 200
    assert 'aria-expanded="true" aria-controls="add-node"' in response.text
    assert 'id="add-node" hidden' not in response.text
