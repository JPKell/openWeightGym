"""Selected live layouts preserve real data, private access and unsent workflow plans."""

from html.parser import HTMLParser
from playwright.sync_api import sync_playwright, expect

from test_weightroom import desk, sign_in  # noqa: F401
from test_collection_pages import seed as source_seed
from test_workflow_pages import seed as workflow_seed
from front_helpers import serve_browser_request


def nodes(markup):
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self.stack = [], []

        def handle_starttag(self, tag, attrs):
            node = {'tag': tag, 'attrs': dict(attrs), 'text': ''}
            self.rows.append(node)
            if tag not in ('input', 'link', 'meta', 'br', 'hr', 'img'):
                self.stack.append(node)

        def handle_endtag(self, tag):
            for i in range(len(self.stack)-1, -1, -1):
                if self.stack[i]['tag'] == tag:
                    del self.stack[i:]
                    break

        def handle_data(self, data):
            for node in self.stack:
                node['text'] += data

    parser = Parser()
    parser.feed(markup)
    return parser.rows


def matching(rows, key, value=None):
    return [node for node in rows if key in node['attrs'] and
            (value is None or (value in node['attrs'][key].split() if key == 'class' else node['attrs'][key] == value))]


def test_collection_overview_counts_and_native_filters_are_owner_scoped(desk):
    server, _, users = desk
    collection, _, _ = source_seed(server, users)
    sign_in(server, 'jordan')
    html = nodes(server.get('/collections').text)
    assert matching(html, 'data-evaluation-row')[0]['attrs']['data-state'] == 'ready'
    assert matching(html, 'data-evaluation-count')[0]['text'] == '1 of 1 collections'
    assert '1 source' in matching(html, 'class', 'collection-catalogue')[0]['text']
    filtered = nodes(server.get('/collections?q=missing&state=attention').text)
    assert 'hidden' in matching(filtered, 'data-evaluation-row')[0]['attrs']
    assert 'hidden' not in matching(filtered, 'data-evaluation-empty')[0]['attrs']
    page = nodes(server.get('/workflows?new=1&collection=' + collection).text)
    assert any(option['attrs'].get('value') == collection
               for option in matching(page, 'selected'))
    sign_in(server, 'pat')
    assert not matching(nodes(server.get('/collections').text), 'data-evaluation-row')
    assert server.get('/collections/' + collection).status_code == 404


def test_workflow_desk_defaults_to_a_saved_run_and_labels_retained_research(desk):
    server, _, users = desk
    workflow = workflow_seed(server, users)
    sign_in(server, 'jordan')
    html = nodes(server.get('/workflows').text)
    selected = [node for node in matching(html, 'class', 'workflow-choice') if node['attrs'].get('aria-current') == 'page']
    assert selected[0]['attrs']['href'] == '/workflows/' + workflow
    assert 'Cedar draft' in matching(html, 'id', 'workflow-workspace')[0]['text']
    assert 'Accepted' in matching(html, 'id', 'research-output')[0]['text']
    assert matching(html, 'aria-current', 'step')[0]['text'].endswith('Draft')
    filtered = nodes(server.get('/workflows?state=completed').text)
    assert 'hidden' in matching(filtered, 'class', 'workflow-choice')[0]['attrs']
    assert not matching(filtered, 'id', 'workflow-workspace')
    assert 'No matching runs' in matching(filtered, 'data-evaluation-empty')[0]['text']


def test_workflow_navigation_retains_unsent_plan_and_filters_without_inference(desk, tmp_path):
    server, _, users = desk
    first = workflow_seed(server, users)
    second = workflow_seed(server, users)
    sign_in(server, 'jordan')
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
        context.add_init_script('window.EventSource = class { close() {} };')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        context.route('http://evaluation.test/**', lambda route: serve_browser_request(server, 'http://evaluation.test', route))
        page.goto('http://evaluation.test/workflows/' + first)
        page.locator('[data-evaluation-open="new-workflow"]').click()
        page.locator('#workflow-title').fill('Keep this unsent plan')
        page.locator('#workflow-question').fill('Which fact should be checked?')
        page.locator('#workflow-search').fill('Cedar')
        page.locator(f'.workflow-choice[href*="/workflows/{second}"]').click()
        expect(page.locator('#workflow-title')).to_have_value('Keep this unsent plan')
        expect(page.locator('#workflow-question')).to_have_value('Which fact should be checked?')
        expect(page.locator('#workflow-search')).to_have_value('Cedar')
        expect(page.locator('#new-workflow')).to_have_attribute('open', '')
        assert page.url.endswith(second + '?q=Cedar')
        # Filtering does not remove the selected run or any accepted output from its inspector.
        page.locator('#workflow-search').fill('does not exist')
        expect(page.locator('[data-evaluation-empty]')).to_be_visible()
        expect(page.locator('#research-output')).to_be_visible()
        page.screenshot(path=str(tmp_path / 'workflow-drafts.png'), full_page=True)
        assert not errors
        browser.close()


def test_selected_layouts_fit_both_themes_and_empty_states_with_native_fallback(desk, tmp_path):
    server, _, users = desk
    workflow = workflow_seed(server, users)
    sign_in(server, 'jordan')
    collection = server.portal.call(lambda: server.app.state.conversations.state.workflows.store.one(users.identity('jordan'), workflow))['collection']
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context(reduced_motion='reduce')
        context.add_init_script('window.EventSource = class { close() {} };')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        context.route('http://evaluation.test/**', lambda route: serve_browser_request(server, 'http://evaluation.test', route))
        for width in (1440, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1100})
            for path in ('/collections', '/collections/' + collection, '/workflows', '/workflows/' + workflow):
                page.goto('http://evaluation.test' + path)
                for mode in ('light', 'dark'):
                    page.evaluate('(mode) => {document.documentElement.dataset.bsTheme=mode}', mode)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (path, width, mode)
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                        return ids.length === new Set(ids).size;
                    }''')
                if width in (1440, 390):
                    page.screenshot(path=str(tmp_path / (path.replace('/', '-') + f'-{width}.png')), full_page=True)
        sign_in(server, 'pat')
        for path in ('/collections', '/workflows'):
            page.goto('http://evaluation.test' + path)
            expect(page.locator('[data-evaluation-empty]')).to_be_visible()
        assert not errors
        context.close()
        # GET filters and the creation disclosure are fully rendered without JavaScript.
        context = browser.new_context(java_script_enabled=False)
        context.route('http://evaluation.test/**', lambda route: serve_browser_request(server, 'http://evaluation.test', route))
        page = context.new_page()
        page.goto('http://evaluation.test/collections?new=1')
        expect(page.locator('#collection-title')).to_be_visible()
        browser.close()
