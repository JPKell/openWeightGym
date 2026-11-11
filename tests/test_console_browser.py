"""Operator interactions in Chromium over isolated apps and mock journals."""

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import httpx
from starlette.testclient import TestClient

from loadcoach.log import Log
from loadcoach.models import Models
from loadcoach.settings import Changes
from weightroom.app import Settings, build_app
from front_helpers import serve_browser_request, turnstile_to
from test_weightroom import desk, desk_with_machine, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')


@pytest.mark.parametrize('width', [1440, 390])
def test_backup_encryption_setup_and_docs_are_usable(desk, tmp_path, backup_keys, width):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/backups')
        playwright.expect(page.get_by_role('button', name='Back up both machines')).to_be_disabled()
        playwright.expect(page.get_by_role('link', name='Configure recovery encryption')).to_be_visible()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.get_by_role('link', name='Configure recovery encryption').click()
        playwright.expect(page.locator('#backup-key-generation')).to_contain_text('age-keygen -o recovery.key')
        playwright.expect(page.locator('#backup-verify')).to_contain_text('loadcoach.backup_restore')
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        backup_keys.configure(tmp_path / 'backups')
        backup_keys.configure(tmp_path / 'data/backups')
        page.goto('http://console.test/backups')
        playwright.expect(page.get_by_role('button', name='Back up both machines')).to_be_enabled()
        page.get_by_role('button', name='Back up both machines').click()
        page.get_by_role('link', name='Restore preview', exact=True).click()
        playwright.expect(page.get_by_text('Encrypted with age.', exact=False).first).to_be_visible()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not errors
        browser.close()


def browser_context(engine, server, **options):
    browser = engine.chromium.launch(headless=True, args=["--disable-gpu"])
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    def serve(route):
        req = route.request
        path = req.url.replace('http://console.test', '')
        if options.get('capture') and options['capture'](route, path):
            return
        if path.split('?', 1)[0] == '/strip/stream':
            return serve_browser_request(server, 'http://console.test', route)
        response = server.request(req.method, path, content=req.post_data_buffer,
                                  headers={'HX-Request': req.headers.get('hx-request', 'false')})
        route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'text/html'), body=response.content,
                      headers={name: value for name, value in response.headers.items() if name.lower().startswith('hx-')})
    context.route('http://console.test/**', serve)
    return browser, context.new_page()


def test_cloud_api_key_status_install_and_replace(desk, tmp_path):
    server, _, _ = desk
    sign_in(server, "jordan")
    added = server.post("/cloud-apis", data={"do": "add", "name": "rented", "url": "https://provider.example/v1"})
    assert added.status_code == 200
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://console.test/cloud-apis")
        playwright.expect(page.locator('p[role="status"]')).to_contain_text("No API key installed")
        field = page.get_by_label("API key", exact=True)
        playwright.expect(field).to_have_attribute("type", "password")
        field.fill("first-provider-secret")
        page.get_by_role("button", name="Install API key", exact=True).click()
        playwright.expect(page.locator('p[role="status"]')).to_have_text("API key installed.")
        field = page.get_by_label("Replace API key", exact=True)
        playwright.expect(field).to_have_value("")
        assert "first-provider-secret" not in page.content()
        field.fill("replacement-secret")
        page.get_by_role("button", name="Update API key", exact=True).click()
        playwright.expect(page.get_by_label("Replace API key", exact=True)).to_have_value("")
        assert "replacement-secret" not in page.content()
        credential = tmp_path / "rented.key"
        assert credential.read_text() == "replacement-secret\n"
        page.set_viewport_size({"width": 390, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # Even server-side validation failures must not refill a secret input.
        refused = server.post("/cloud-apis", data={"do": "key", "name": "rented", "api_key": "bad secret"})
        assert refused.status_code == 400 and "bad secret" not in refused.text
        assert credential.read_text() == "replacement-secret\n"
        assert not errors
        browser.close()


def test_expired_telemetry_session_opens_one_login_and_keeps_password_focus(desk_with_machine):
    server, _, _, _, _ = desk_with_machine
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/status')
        playwright.expect(page.locator('#strip .strip')).to_be_visible()
        server.cookies.clear()
        # A background refresh must navigate to sign-in, rather than nest a
        # fresh autofocus form in the telemetry area every three seconds.
        page.evaluate("void htmx.ajax('GET', '/strip', {target: '#strip', swap: 'innerHTML'})")
        page.wait_for_url('**/sign-in')
        playwright.expect(page.locator('form[action="/sign-in"]')).to_have_count(1)
        playwright.expect(page.locator('#strip')).to_have_count(0)
        page.locator('#name').fill('jordan')
        page.locator('#name').press('Tab')
        playwright.expect(page.locator('#password')).to_be_focused()
        page.keyboard.type('first')
        for _ in range(2):
            page.evaluate("async () => { await fetch('/strip'); }")
            playwright.expect(page.locator('#password')).to_be_focused()
            page.keyboard.type('-more')
        playwright.expect(page.locator('#password')).to_have_value('first-more-more')
        playwright.expect(page.locator('form[action="/sign-in"]')).to_have_count(1)
        assert not errors
        browser.close()


def test_file_settings_stay_independent_across_polling_and_reload(desk, tmp_path):
    server, switch, _ = desk
    settings = switch.front.state.front_settings.changes
    for name in ('toollocker', 'turnstile'):
        settings.seen(name, {'fixture_path': {'value': 'fixture', 'file': 'fixture'}})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/settings')
        files = page.locator('[data-card-key="machine-values"]')
        files.locator(':scope > summary').click()
        playwright.expect(files).to_have_attribute('open', '')
        for _ in range(2):
            page.evaluate("async () => { await htmx.ajax('GET', '/strip', {target: '#strip', swap: 'innerHTML'}); }")
            playwright.expect(files).to_have_attribute('open', '')
        page.reload()
        playwright.expect(files).to_have_attribute('open', '')
        table = page.get_by_role('table', name='Read-only machine values', exact=True)
        assert 'fixture_path' in table.inner_text()
        assert table.locator('tbody tr').filter(has_text='fixture_path').count() == 2
        assert not errors
        browser.close()


def test_log_diagnostic_model_is_remembered_per_account_in_the_browser():
    script = (Path(__file__).parents[1] / 'weightroom/static/sort.js').read_text()
    markup = '''<!doctype html><html data-theme-user="jordan"><body>
      <select data-diagnostic-model><option>alpha</option><option>beta</option></select>
      <script>''' + script + '''</script></body></html>'''
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--disable-gpu"])
        page = browser.new_page()
        page.route('http://diagnostic.test/**', lambda route: route.fulfill(content_type='text/html', body=markup))
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://diagnostic.test/log')
        choice = page.locator('[data-diagnostic-model]')
        playwright.expect(choice).to_have_value('alpha')
        choice.select_option('beta')
        page.reload()
        playwright.expect(choice).to_have_value('beta')
        page.evaluate("document.documentElement.dataset.themeUser='pat'; document.querySelector('[data-diagnostic-model]').value='alpha'; rememberDiagnosticModel()")
        playwright.expect(choice).to_have_value('alpha')
        assert not errors
        browser.close()


def test_current_requests_can_stop_one_unlogged_row_without_request_links(desk, tmp_path):
    _, wire, users = desk
    # desk's console already polls its front on another event loop. The extra console
    # needs its own front/accounts connection, as independent processes have in production.
    front = turnstile_to(tmp_path, users, {8442: wire})
    turnstile = httpx.ASGITransport(app=front, client=('127.0.0.1', 1))
    active = ['volatile-one', 'volatile-two']
    controls = []
    class LiveWire(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == '/loadcoach/stop-unlogged':
                controls.append((json.loads(request.content), request.headers['authorization']))
                active.remove(controls[-1][0]['handle'])
                return httpx.Response(200, json={'stopped': True})
            response = await turnstile.handle_async_request(request)
            if request.url.path == '/loadcoach/status' and response.is_success:
                status = json.loads(await response.aread())
                status['running'] = [{'id': 0, 'model': 'small', 'user': 'jordan', 'caller': '10.0.0.2',
                    'priority': 'normal', 'seconds': 1, 'stop_handle': handle, 'may_stop': True, 'unlogged': True} for handle in active]
                return httpx.Response(200, json=status)
            return response
    settings = Settings(compute='http://compute', key=users.new_key('panel'), data=str(tmp_path/'front-control'))
    with TestClient(build_app(settings, httpx.AsyncClient(transport=LiveWire(), base_url='http://front:8442'))) as server:
        sign_in(server, 'jordan')
        assert server.post('/requests/unlogged/stop', data={'handle': active[0]}, headers={'origin': 'https://elsewhere.invalid'}).status_code == 403
        assert not controls
        with playwright.sync_playwright() as engine:
            browser, page = browser_context(engine, server)
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://console.test/status')
            forms = page.locator('form[action="/requests/unlogged/stop"]')
            playwright.expect(forms).to_have_count(2)
            assert page.locator('#requests a[href^="/requests/"]').count() == 0
            assert page.locator('#requests tbody').inner_text().count('Unlogged') == 2
            forms.first.get_by_role('button', name='Stop', exact=True).click()
            page.get_by_role('button', name='Are you sure? Stop', exact=True).click()
            playwright.expect(page.locator('.said-box')).to_have_text('Stopped the unlogged request. The others carry on.')
            playwright.expect(forms).to_have_count(1)
            assert active == ['volatile-two']
            assert controls == [({'handle': 'volatile-one'}, 'Bearer '+server.cookies['weightroom'])]
            assert not errors
            browser.close()


def test_api_key_removal_confirmation_has_no_repeated_body_text(desk):
    server, _, users = desk
    users.new_key('jordan', 'confirmation-check')
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/account')
        row = page.locator('#keys tbody tr').filter(has_text='confirmation-check')
        row.get_by_role('button', name='Remove', exact=True).click()
        dialog = page.get_by_role('dialog', name='Confirm action', exact=True)
        playwright.expect(dialog).to_be_visible()
        assert dialog.locator('p').count() == 0
        playwright.expect(dialog.get_by_label('Yes, remove', exact=True)).to_be_visible()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        playwright.expect(row).to_have_count(1)
        row.get_by_role('button', name='Remove', exact=True).click()
        dialog.get_by_label('Yes, remove', exact=True).check()
        dialog.get_by_role('button', name='Continue', exact=True).click()
        playwright.expect(row).to_have_count(0)
        browser.close()


def test_account_sections_pagination_and_display_auto_save(desk, tmp_path):
    server, _, _ = desk
    log = Log(str(tmp_path / 'loadcoach.db'))
    for name, tokens in [('small', 1000000), ('large', 2000000)]:
        start = log.add('request', 'asked', 'jordan', model=name)
        log.add('request', 'done', 'jordan', model=name, of=start, prompt_tokens=tokens, answer_tokens=100)
    for _ in range(30):
        log.add('request', 'asked', 'jordan', model='small')
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        images = Path('data/console-previews'); images.mkdir(parents=True, exist_ok=True)
        page.goto('http://console.test/account')
        assert page.locator('.category-links a.active').count() == 0
        assert 'btn-primary' in page.locator('.user-menu > summary').get_attribute('class')
        assert page.locator('#site-sidebar .section-link').all_text_contents() == ['Account', 'Appearance', 'Audio', 'Logs', 'Logout']
        assert page.locator('#page-content details.card').count() == 0
        password = page.locator('#account-password-heading').bounding_box()
        keys = page.locator('#account-keys-heading').bounding_box()
        assert abs(password['y'] - keys['y']) < 1 and password['x'] < keys['x']
        page.screenshot(path=str(images / 'account.png'))
        page.get_by_role('link', name='Appearance', exact=True).click()
        page.locator('#show_telemetry').uncheck()
        playwright.expect(page.locator('.said-box')).to_have_text('Kept.')
        playwright.expect(page.locator('#show_telemetry')).not_to_be_checked()
        playwright.expect(page.locator('header .band')).to_have_count(0)
        playwright.expect(page.locator('#pulse')).to_have_count(0)
        page.goto('http://console.test/account/appearance')
        playwright.expect(page.locator('#show_telemetry')).not_to_be_checked()
        playwright.expect(page.locator('header .band')).to_have_count(0)
        assert page.locator('[name="palette"]').count() == 17
        assert page.locator('#page-content details.card').count() == 0
        page.screenshot(path=str(images / 'account-appearance.png'))
        page.locator('#site-sidebar').get_by_role('link', name='Logs', exact=True).click()
        page.select_option('#account-log-limit', '10')
        playwright.expect(page.locator('#account-events nav')).to_contain_text('Page 1 of 4')
        page.get_by_role('link', name='Next', exact=True).click()
        playwright.expect(page.locator('#account-events nav')).to_contain_text('Page 2 of 4')
        page.screenshot(path=str(images / 'account-logs.png'))
        assert not errors
        browser.close()


def test_confirm_cards_pagination_and_live_usage(desk, tmp_path):
    server, _, _ = desk
    log = Log(str(tmp_path / 'loadcoach.db'))
    for name, inputs, outputs, price in [('pat', 1_000_000, 2_000_000, {}), ('jordan', 100, 50, {'cost': 4})]:
        start = log.add('request', 'asked', name, '10.0.0.2', 'sample')
        log.add('request', 'done', name, '10.0.0.2', 'sample', of=start, prompt_tokens=inputs, answer_tokens=outputs,
                group='plain' if name == 'pat' else 'admin', **price)
    for _ in range(30):
        log.add('request', 'asked', 'jordan', '10.0.0.1', 'sample')
    sign_in(server, 'jordan')
    controls = []
    def capture(route, path):
        if path == '/control':
            controls.append(parse_qs(route.request.post_data))
            route.fulfill(status=200, content_type='text/html', body=server.get('/status').content)
            return True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/status')
        controls_box = page.locator('#status-controls').bounding_box()
        cards_box = page.locator('#status-capacity > .card').first.bounding_box()
        heading_box = page.locator('#status-capacity-heading .section-title').bounding_box()
        ledger_box = page.locator('#status-capacity').bounding_box()
        assert controls_box['y'] < cards_box['y']
        assert abs((heading_box['y'] + heading_box['height']/2) - (controls_box['y'] + controls_box['height']/2)) <= 2
        assert abs((controls_box['x'] + controls_box['width']) - (ledger_box['x'] + ledger_box['width'])) <= 2
        assert page.locator('#status-controls .input-group input[name="why"]').count() == 1
        assert page.locator('#status-controls .input-group button[value="drain"]').count() == 1
        assert abs(page.locator('#status-control-machine').bounding_box()['y'] - page.locator('button[value="unload"]').bounding_box()['y']) <= 2
        assert page.locator('#status-controls').evaluate('(element) => element.scrollWidth <= element.clientWidth')
        images = Path('data/console-previews'); images.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(images / 'status.png'))
        page.get_by_role('button', name='Pause intake', exact=True).click()
        playwright.expect(page.locator('button[value="drain"]')).to_have_text('Are you sure?')
        page.get_by_role('button', name='Clear queue', exact=True).click()
        playwright.expect(page.locator('button[value="drain"]')).to_have_text('Pause intake')
        assert not controls
        confirm = page.locator('button[value="clear"]')
        playwright.expect(confirm).to_have_text('Are you sure?')
        assert confirm.evaluate('e=>e.hasAttribute("data-confirm-two") || e.form.hasAttribute("data-confirm-two")')
        # Status polling preserves confirmation state and collapsed request cards.
        page.locator('[data-card-key="current-requests-processing"] > summary').click()
        page.evaluate('window.priorStatus = document.querySelector("#status")')
        page.wait_for_function('document.querySelector("#status") !== window.priorStatus', timeout=5000)
        playwright.expect(page.locator('[data-card-key="current-requests-processing"]')).not_to_have_attribute('open', '')
        playwright.expect(confirm).to_have_text('Are you sure?')
        confirm.click()
        page.wait_for_load_state()
        assert len(controls) == 1 and controls[0]['do'] == ['clear'] and controls[0]['machine'] == ['compute']
        page.goto('http://console.test/requests?limit=10')
        assert page.locator('#recent-requests tbody tr').count() == 10
        page.get_by_role('link', name='Next', exact=True).click()
        playwright.expect(page.locator('#recent-requests nav')).to_contain_text('Page 2 of 4')
        page.select_option('#request-limit', '25')
        playwright.expect(page.locator('#recent-requests nav')).to_contain_text('Page 1 of 2')
        assert page.locator('#recent-requests tbody tr').count() == 25
        page.goto('http://console.test/usage')
        # The heading leads straight into the dashboard: the filter rail on the left, the report beside it.
        rail, report, header = (page.locator(selector).bounding_box() for selector in ('.usage-rail', '.usage-main', '#usage-header'))
        assert rail['x'] + rail['width'] < report['x'] and abs(rail['y'] - report['y']) < 2
        assert rail['y'] - (header['y'] + header['height']) < 40
        playwright.expect(page.locator('[data-derived="requests"]')).to_contain_text('a day')
        playwright.expect(page.locator('[data-composition] .usage-segment')).to_have_count(2)
        playwright.expect(page.locator('[data-filter-count]')).to_have_text('None')
        assert page.locator('#usage-input-rate').input_value() == '2.0'
        assert page.locator('#usage-output-rate').input_value() == '10.0'
        page.locator('#usage-input-rate').fill('3')
        playwright.expect(page.locator('[data-usage-total="effective_cost"]')).to_have_text('$27.0000')
        playwright.expect(page.locator('[data-usage-total="recorded_cost"]')).to_have_text('$4.0000')
        page.select_option('#usage-group-by', 'user')
        playwright.expect(page.locator('#usage-range-heading')).to_have_text('Selected range by user')
        assert page.locator('#usage-breakdown tbody tr').count() == 2
        page.locator('[data-users-groups] .user-picker > button').click()
        page.get_by_role('searchbox', name='Search users').fill('jordan')
        page.locator('.user-picker-results button').filter(has_text='jordan').click()
        assert page.locator('#usage-user').input_value() == 'jordan'
        page.select_option('#usage-group', 'plain')
        assert page.locator('#usage-user').input_value() == ''
        page.locator('[data-users-groups] .user-picker > button').click()
        assert page.locator('.user-picker-results button').all_text_contents() == ['All users', 'pat']
        page.locator('#usage-start').click()
        page.screenshot(path=str(images / 'usage.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(images / 'usage-mobile.png'))
        overflow = page.evaluate("[...document.querySelectorAll('body *')].filter(el => el.getBoundingClientRect().right > innerWidth && getComputedStyle(el).position !== 'fixed').slice(0, 12).map(el => ({tag:el.tagName, id:el.id, cls:el.className, right:el.getBoundingClientRect().right}))")
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), overflow
        assert not errors
        browser.close()


def test_system_logs_selection_refresh_and_tools_console(desk_with_machine, tmp_path):
    server, _, _, _, _ = desk_with_machine
    model = tmp_path / 'sample.gguf'; model.touch()
    Models(str(tmp_path / 'loadcoach.db')).put('sample', {'flags': {'model': str(model)}})
    sign_in(server, 'jordan')
    sent = []; tools = []
    def capture(route, path):
        if path == '/ask':
            sent.append(parse_qs(route.request.post_data))
            route.fulfill(status=200, content_type='text/html', body='<p>Mock answer</p>'); return True
        if path == '/tools/message':
            message = json.loads(route.request.post_data)['message']; tools.append(message)
            result = {'protocolVersion': '2025-06-18'} if message['method'] == 'initialize' else (
                {'tools': [{'name': 'files__read', 'description': 'Read a file', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}]}
                if message['method'] == 'tools/list' else {'content': [{'type': 'text', 'text': 'example contents'}]})
            route.fulfill(status=200, content_type='application/json', headers={'mcp-session-id': 'mock'},
                          body=json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result})); return True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/output?source=loadcoach')
        assert 'journalctl --user -u loadcoach.service' in page.locator('main').text_content()
        page.select_option('#lines', '5000')
        page.wait_for_function('new URL(location.href).searchParams.get("lines") === "5000"')
        assert '-n 5000' in page.locator('main').text_content()
        page.evaluate("() => {const node = [...document.querySelector('#printed td').childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.includes('loadcoach')); const range = document.createRange(); const start = node.textContent.indexOf('loadcoach'); range.setStart(node, start); range.setEnd(node, start + 'loadcoach'.length); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);}")
        playwright.expect(page.locator('[data-log-selection]')).to_have_value('loadcoach')
        page.locator('#words').fill('Why did it start?')
        page.get_by_role('button', name='Ask', exact=True).click()
        playwright.expect(page.locator('#answer')).to_contain_text('Mock answer')
        assert sent[0]['selection'] == ['loadcoach'] and '<b>started</b>' in sent[0]['log'][0]
        page.get_by_role('button', name='Use whole log').click()
        page.get_by_role('button', name='Ask', exact=True).click()
        page.wait_for_function('document.querySelector("#asking").classList.contains("htmx-request") === false')
        assert len(sent) == 2 and 'selection' not in sent[1]
        question = page.locator('#words').bounding_box(); model_box = page.locator('#ask-model').bounding_box()
        assert question['width'] > model_box['width'] and model_box['y'] > question['y']
        page.goto('http://console.test/tools')
        page.get_by_role('button', name='Connect', exact=True).click()
        playwright.expect(page.locator('#tools-state')).to_have_text('1 tools available to your account.')
        page.locator('#tool-argument-path').fill('/example.txt')
        page.get_by_role('button', name='Call', exact=True).click()
        playwright.expect(page.locator('#tools-answer')).to_have_text('example contents')
        assert tools[-1]['params'] == {'name': 'files__read', 'arguments': {'path': '/example.txt'}}
        images = Path('data/console-previews'); images.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(images / 'tools.png'))
        assert not errors
        browser.close()
