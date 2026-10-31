"""Real periodic table updates preserve selected text and resume after it clears."""
import pytest

from loadcoach.log import Log
from test_console_browser import browser_context
from loadcoach.models import Models
from test_weightroom import desk, desk_with_machine, gguf, sign_in

playwright = pytest.importorskip('playwright.sync_api')


def select_text(page, selector):
    return page.evaluate('''selector => {
        const target=document.querySelector(selector), walker=document.createTreeWalker(target,NodeFilter.SHOW_TEXT);
        let node; while ((node=walker.nextNode()) && !node.textContent.trim()) {}
        const range=document.createRange(); range.selectNodeContents(node);
        const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range);
        return selection.toString();
    }''', selector)


@pytest.mark.parametrize('selected,refreshing', [
    ('#status h2', '#status'),
    ('#requests td', '#current-requests'),
    ('#recent-requests td', '#recent-requests'),
])
def test_status_polling_defers_selected_section_and_resumes(desk_with_machine, tmp_path, selected, refreshing):
    server, _, _, _, _ = desk_with_machine
    log = Log(str(tmp_path/'loadcoach.db'))
    number = log.add('request','asked','jordan',model='example')
    log.add('request','done','jordan',model='example',of=number,seconds=.1)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/status')
        playwright.expect(page.locator('#strip .strip')).to_be_visible()
        text = select_text(page, selected)
        assert text
        page.evaluate('''selector => {
            window.selectedSection=document.querySelector(selector);
            window.priorStrip=document.querySelector('#strip .strip');
        }''', refreshing)
        # The independent three-second header update keeps running while the selection holds.
        page.wait_for_function("document.querySelector('#strip .strip') !== window.priorStrip", timeout=6000)
        assert page.evaluate('getSelection().toString()') == text
        assert page.evaluate('selector => document.querySelector(selector) === window.selectedSection', refreshing)
        page.evaluate('getSelection().removeAllRanges()')
        page.wait_for_function('selector => document.querySelector(selector) !== window.selectedSection', arg=refreshing, timeout=7000)
        assert not errors
        browser.close()


def test_static_selection_does_not_stop_status_and_explicit_refresh_still_applies(desk_with_machine):
    server, _, _, _, _ = desk_with_machine
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/status')
        text = select_text(page, '#page-content h1')
        page.evaluate("window.priorStatus=document.querySelector('#status')")
        page.wait_for_function("document.querySelector('#status') !== window.priorStatus", timeout=5000)
        assert page.evaluate('getSelection().toString()') == text
        select_text(page, '#status h2')
        page.evaluate("window.priorStatus=document.querySelector('#status')")
        # An explicit request is not a poll; changing filters or taking an action still works.
        page.evaluate("async () => {await htmx.ajax('GET','/status?part=status',{target:'#status',swap:'outerHTML'});}")
        assert page.evaluate("document.querySelector('#status') !== window.priorStatus")
        browser.close()


def test_selection_started_during_a_poll_response_is_not_replaced(desk_with_machine):
    server, _, _, _, _ = desk_with_machine
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        page = None
        intercepted = []
        def capture(route, path):
            if path == '/status?part=status' and not intercepted:
                intercepted.append(select_text(page, '#status h2'))
            return False
        browser, page = browser_context(engine, server, capture=capture)
        page.goto('http://console.test/status')
        page.evaluate('''() => {
            window.priorStatus=document.querySelector('#status');
            document.addEventListener('htmx:afterRequest',event => {
                if (event.detail.requestConfig?.path === '/status?part=status') window.pollCompleted=true;
            });
        }''')
        page.wait_for_function('window.pollCompleted === true', timeout=5000)
        assert intercepted and page.evaluate('getSelection().toString()') == intercepted[0]
        assert page.evaluate("document.querySelector('#status') === window.priorStatus")
        browser.close()


@pytest.mark.parametrize('address,selected,refreshing', [
    ('/models', '#models td', '#models'),
    ('/log', '#log td', '#log'),
    ('/batches', '#batches td', '#batches'),
    ('/files', '#files-places', '#files-places'),
    ('/files', '#files-models td', '#files-models'),
    ('/files', '#files-adapters td', '#files-adapters'),
    ('/files', '#files-projectors td', '#files-projectors'),
    ('/files', '#downloads td', '#downloads'),
    ('/benchmarks/embedders', '#benchmark-embedder-active td', '#benchmark-embedder-active > details'),
    ('/benchmarks/runs', '#benchmark-active td', '#benchmark-active'),
    ('/benchmarks/runs', '#benchmark-queue td', '#benchmark-queue'),
    ('/benchmarks/runs', '#benchmark-history td', '#benchmark-history'),
])
def test_other_tables_defer_polling_and_resume(desk_with_machine, tmp_path, address, selected, refreshing):
    server, _, _, _, _ = desk_with_machine
    folder = tmp_path / 'models'; folder.mkdir()
    model = gguf(folder / 'fixture.gguf', general__name='Selection fixture')
    Models(str(tmp_path/'loadcoach.db')).put('fixture', {'gpus': 0, 'flags': {'model': model, 'ctx-size': 2048}})
    Log(str(tmp_path/'loadcoach.db')).add('request', 'asked', 'jordan', model='fixture')
    partial = folder / 'acme--fixture'; partial.mkdir()
    (partial / 'download.gguf.part').write_bytes(b'partial download')
    # Seed a running record without submitting work or invoking an inference engine.
    store = server.app.state.benchmarks.state.store
    number = store.create('jordan', 'fixture', 'load')
    store.activate(number, {'fingerprint': 'fp', 'subject': {'settings': {}}, 'environment': {}})
    store.state(number, 'running')
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test' + address)
        playwright.expect(page.locator('#strip .strip')).to_be_visible()
        # History starts closed; selecting its visible row also exercises remembered state.
        if selected.startswith('#benchmark-history'):
            page.locator('#benchmark-history > summary').click()
        text = select_text(page, selected)
        assert text
        page.evaluate('''selector => {
            window.selectedSection=document.querySelector(selector);
            window.priorStrip=document.querySelector('#strip .strip');
        }''', refreshing)
        page.wait_for_function("document.querySelector('#strip .strip') !== window.priorStrip", timeout=6000)
        assert page.evaluate('getSelection().toString()') == text
        assert page.evaluate('selector => document.querySelector(selector) === window.selectedSection', refreshing)
        page.evaluate('getSelection().removeAllRanges()')
        page.wait_for_function('selector => document.querySelector(selector) !== window.selectedSection', arg=refreshing, timeout=7000)
        assert not errors
        browser.close()


@pytest.mark.parametrize('oob', [None, 'innerHTML:#benchmark-history', 'data'])
def test_undeclared_oob_response_preserves_inflight_selection(desk, oob):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        page = None
        intercepted = []
        def capture(route, path):
            if path.startswith('/benchmarks/runs?part=live'):
                if not intercepted:
                    intercepted.append(select_text(page, '#benchmark-history td'))
                if oob:
                    attribute = 'data-hx-swap-oob="true"' if oob == 'data' else f'hx-swap-oob="{oob}"'
                    route.fulfill(content_type='text/html', body=f'<div id="benchmark-history" {attribute}><table><tr><td>Updated history</td></tr></table></div>')
                    return True
            return False
        browser, page = browser_context(engine, server, capture=capture)
        page.goto('http://console.test/benchmarks/runs')
        page.locator('#benchmark-history > summary').click()
        page.evaluate('''() => {
            document.querySelector('[hx-get^="/benchmarks/runs?part=live"]').removeAttribute('data-refresh-companions');
            window.priorCell=document.querySelector('#benchmark-history td');
            document.addEventListener('htmx:afterRequest',event => {
                if (event.detail.requestConfig?.path.startsWith('/benchmarks/runs?part=live')) window.pollCompleted=true;
            });
        }''')
        page.wait_for_function('window.pollCompleted === true', timeout=5000)
        assert intercepted and page.evaluate('getSelection().toString()') == intercepted[0]
        assert page.evaluate("document.querySelector('#benchmark-history td') === window.priorCell")
        page.evaluate('getSelection().removeAllRanges()')
        page.wait_for_function("document.querySelector('#benchmark-history td') !== window.priorCell", timeout=5000)
        browser.close()
