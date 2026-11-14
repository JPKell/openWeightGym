"""Console component contracts over isolated apps, without inference or GPU access."""

import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from jinja2 import Environment

from loadcoach.log import Log
from loadcoach.models import Models
from test_weightroom import desk, desk_with_machine, gguf, sign_in  # noqa: F401
from test_workflow_pages import seed

ROOT = Path(__file__).resolve().parents[1]
from weightroom.system_docs import SECTIONS
TEMPLATE_ROOTS = tuple(ROOT / name / 'pages' for name in ('weightroom', 'mirrorwall', 'soundroom'))
HOST = '5855323a162516e680c785a3dd60f563bfc44129d809af71eb8674ff7316901d'
# Explicit routes keep this check portable: it does not depend on ignored audit artifacts.
PAGES = (
    '/', '/chat', '/chat/classifier', '/collections', '/workflows', '/chat/images', '/chat/music',
    '/chat/sounds', '/chat/speech', '/chat/transcribe', '/voice', '/voice/desktop', '/models',
    '/models/images/workflows', '/models/audio', '/dry-fit', '/roles', '/files',
    '/hub', '/civitai', '/fingerprints', '/cloud-apis', '/benchmarks', '/benchmarks/runs',
    '/benchmarks/results', '/benchmarks/compare',
    '/benchmarks/manage', '/usage', '/allowances', '/requests', '/batches', '/responses', '/log', '/reliability',
    '/plugins/activity', '/output', '/status', '/system/voice', '/system/audio',
    '/telemetry', '/alerts', '/schedules', '/compute-nodes', '/users', '/groups',
    '/tools', '/plugins', '/backups', '/databases', '/settings', '/docs',
    *(url for section in SECTIONS for url in (section['url'], *(topic['url'] for topic in section['topics'])) if url != '/docs'),
    '/account', '/account/keys', '/account/appearance', '/account/audio', '/account/workspace', '/account/privacy', '/account/logs', '/sign-in',
    '/docs/modules/weightroom?module=weightroom%2Fapp.py', '/groups/admin',
    '/models/sample', '/plugins/hermes', f'/telemetry/{HOST}', '/users/pat',
    '/users/jordan', '/plugins/hermes/instances/jeeves',
    '/backups/20261014T144500Z-39f0240e98ec64b9a76dd384', '/models/images',
    '/models/picture', '/benchmarks/node-measurements', '/system/voice/status',
    '/settings/image', '/models/images?model=picture',
    '/files?tier=storage', '/files?machine=compute', '/models?kind=image', '/models?kind=audio',
    '/benchmarks/node-measurements?kind=embedding', '/benchmarks/node-measurements?kind=image',
    '/benchmarks/node-measurements?kind=audio', '/system/voice/telephone',
)

MEASURE = r"""() => {
  const main = document.querySelector('main');
  const visible = e => {
    const rect = e.getBoundingClientRect(), style = getComputedStyle(e);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' &&
      !e.closest('[hidden]');
  };
  const describe = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') +
    (e.name ? '[name=' + e.name + ']' : '') + ': ' +
    (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 65);
  const style = e => {
    const s = getComputedStyle(e);
    return {element: describe(e), size: s.fontSize, weight: s.fontWeight,
      padding: [s.paddingTop, s.paddingRight, s.paddingBottom, s.paddingLeft]};
  };
  const titles = [...main.querySelectorAll('h1')].filter(visible).map(style);
  const headers = [...main.querySelectorAll('.card-header')].filter(visible).map(style);
  const sections = [...main.querySelectorAll('.section-title')].filter(visible).map(style);
  const selector = 'input.form-control:not([type=file]):not([type=hidden]), ' +
    'select.form-select:not([multiple]), button.btn, a.btn, summary.btn, .input-group-text';
  const controls = [...main.querySelectorAll(selector)].filter(visible)
    .filter(e => !e.closest('.dropdown-menu, .chat-model-menu, table.printed'))
    .filter(e => !(e.tagName === 'SELECT' && e.size > 1))
    .map(e => {
      const r = e.getBoundingClientRect();
      return {element: describe(e), height: Math.round(r.height * 10) / 10,
        top: r.top, bottom: r.bottom, left: r.left, right: r.right,
        form: e.closest('form'), sticky: e.closest('.sticky-actions')};
    });
  const mismatches = [];
  for (let i = 0; i < controls.length; i++) {
    const a = controls[i];
    for (const b of controls.slice(i + 1)) {
      if (a.form !== b.form || a.sticky !== b.sticky) continue;
      // Compare adjacent controls in the same row, not a sticky footer over a scrolled field.
      const overlap = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      const gap = Math.max(b.left - a.right, a.left - b.right);
      if (overlap < Math.min(a.height, b.height) / 2 || gap < -2 || gap > 64) continue;
      if (Math.abs(a.height - b.height) > 1) {
        mismatches.push([a.element, a.height, b.element, b.height]);
      }
    }
  }
  const invalidControls = controls.filter(c => ![32, 40].some(height => Math.abs(c.height - height) <= 1))
    .map(c => [c.element, c.height]);
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT), renderedText = [];
  while (walker.nextNode()) {
    const parent = walker.currentNode.parentElement;
    if (parent && visible(parent) && !parent.closest('pre, code, script, style, textarea, select, .answer, .said-user')) renderedText.push(walker.currentNode.textContent);
  }
  const rawValues = [...new Set((renderedText.join(' ').match(/\bNone\b|\bnull\b|\bNaN\b|\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?/g) || []))];
  const fields = [...main.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')].filter(visible);
  const named = field => [...field.labels || []].some(label => label.textContent.trim()) || field.getAttribute('aria-label')?.trim() ||
    field.getAttribute('aria-labelledby')?.split(/\s+/).some(id => document.getElementById(id)?.textContent.trim()) || field.getAttribute('title')?.trim();
  const namelessFields = fields.filter(field => !named(field)).map(describe);
  const kpis = [...main.querySelectorAll('.kpi-row > .kpi')].filter(visible).map(el => ({
    ...style(el), label: !!el.querySelector('.kpi-label'),
    value: el.querySelector(':scope > .kpi-value') ? style(el.querySelector(':scope > .kpi-value')) : null,
  }));
  return {titles, headers, sections, mismatches, invalidControls, rawValues, namelessFields, kpis,
    headerDescriptions: [...main.querySelectorAll('.page-header > .d-flex > .min-w-0 > p')].map(e => e.textContent),
    overflow: document.documentElement.scrollWidth > innerWidth,
    theme: document.documentElement.dataset.bsTheme,
    palette: document.documentElement.dataset.siteTheme};
}"""


def test_templates_use_shared_components():
    """Pages and fragments must reuse the components indexed in AGENTS.md."""
    for root in TEMPLATE_ROOTS:
        for path in root.rglob('*.html'):
            source = path.read_text()
            Environment().parse(source)
            if path.name == 'parts.html':
                continue
            assert not re.search(r'<h[1-6]\b', source), path
            assert not re.search(r'<\w+\b[^>]*class=["\'][^"\']*\bcard-header\b', source), path
            assert not re.search(r'<\w+\b[^>]*class=["\'](?:card[\s"\']|[^"\']*\scard[\s"\'])', source), path
            # Mask Jinja before checking class tokens, including conditional attributes.
            markup = re.sub(r'{{.*?}}|{%.*?%}|{#.*?#}', 'template', source, flags=re.S)
            classes = re.findall(r'<[^>]+\bclass=["\']([^"\']*)["\']', markup)
            if path.name != 'kpi_parts.html':
                assert not any({'kpi-row', 'kpi', 'kpi-value'} & set(value.split()) for value in classes), path
            assert not any('alert' in value.split() for value in classes), path
            assert not re.search(r'<details\b[^>]*class=["\']more["\']', markup), path
            if path.name != 'navigation_parts.html':
                assert not any('nav-pills' in value.split() for value in classes), path
            # A spanning row with only a message belongs in empty_row; group headings use th. The node
            # matrix's rows of shared settings (data-matrix-shared) span its node columns with controls.
            assert not re.search(r'<tr\b(?![^>]*data-matrix-shared)[^>]*>\s*<td\b[^>]*\bcolspan=[^>]+>'
                                 r'(?:(?!</?t[drh]\b).)*</td>\s*</tr>', markup, re.S), path


@pytest.mark.parametrize('service,title', [('loadcoach', 'LoadCoach'), ('toollocker', 'ToolLocker')])
def test_standalone_diagnostics_keep_controls_and_fit(service, title):
    from loadcoach.web import diagnostic_page
    from test_console_browser import playwright

    markup = diagnostic_page(ROOT / service / 'test.html').body
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('http://diagnostic.test/**', lambda route: route.fulfill(
            content_type='text/html' if route.request.url == 'http://diagnostic.test/' else 'application/json',
            body=markup if route.request.url == 'http://diagnostic.test/' else '{"data":[]}'))
        page.goto('http://diagnostic.test/')
        playwright.expect(page.get_by_role('heading', level=1)).to_have_text(title + ' test page')
        page.get_by_label('API key', exact=False).fill('test-key')
        playwright.expect(page.get_by_label('API key', exact=False)).to_have_value('test-key')
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()


@pytest.fixture
def ui_host(monkeypatch):
    # Install before desk_with_machine constructs its machine registry.
    from turnstile.machines import Machines
    initialize = Machines.__init__

    def known_host(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        self.known_hosts[HOST] = {'hostname': 'fixture-node', 'machines': ['compute'], 'last_seen': time.time()}

    monkeypatch.setattr(Machines, '__init__', known_host)


@pytest.fixture
def ui_console(ui_host, desk_with_machine, tmp_path, backup_keys):
    server, _, _, users, _ = desk_with_machine
    users.change('jordan', own={'add': ['front-voice']})
    registry = server.app.state.plugins
    shutil.copytree(ROOT / 'plugins/hermes', registry.root / 'hermes',
                    ignore=shutil.ignore_patterns('__pycache__', 'settings.json', '.settings.lock'))
    registry.save_instance('hermes', 'jeeves', 'http://127.0.0.1:1', {'cli': 'hermes'}, enabled=False)
    from loadcoach.backups import BackupFile, create_backup
    saved = tmp_path / 'fixture.toml'
    saved.write_text('value = 1\n')
    backup_keys.configure(tmp_path / 'data/backups')
    create_backup(tmp_path / 'data/backups', '20261014T144500Z-39f0240e98ec64b9a76dd384', 'front',
                  [BackupFile(saved, 'settings/fixture.toml', 'settings')], [], [], [], 0)
    workflow = seed(server, users)
    owner = users.identity('jordan')
    collection = server.portal.call(lambda: server.app.state.conversations.state.workflows.store.one(owner, workflow))['collection']
    chunk = server.portal.call(lambda: server.app.state.conversations.state.knowledge.store.chunks(owner, collection))[0]['id']
    from test_response_storage import seed_response
    saved_response = seed_response(server, users, 'jordan', text='A stored input for layout review.')
    chats = server.app.state.conversations.state.chats
    conversation = chats.start(owner, 'Layout review')
    chats.say(conversation, 'user', 'How does the page fit?')
    chats.say(conversation, 'assistant', 'A saved reply for the layout review.', model='sample')
    comparison = chats.start(owner, 'Compare layouts', compare=['sample', 'sample'])
    chats.say(comparison, 'user', 'Compare the layouts.')
    for branch in (0, 1):
        chats.say(comparison, 'assistant', 'A comparison reply.', model='sample', branch=branch)
    file = gguf(tmp_path / 'sample.gguf', general__architecture='qwen', qwen__context_length=8192)
    library = Models(str(tmp_path / 'loadcoach.db'))
    library.put('sample', {'gpus': 0, 'flags': {'model': file, 'ctx-size': 8192}})
    library.put('picture', {'engine': 'sdcpp', 'flags': {'model': file, 'ctx-size': 0},
                           'gpus': ['A'], 'alone': True,
                           'sdcpp': {'family': 'sdxl', 'components': {}, 'host_ram_mib': 8192,
                                     'defaults': {'width': 512, 'height': 512}, 'limits': {'max_references': 1}}})
    log = Log(str(tmp_path / 'data/accounts.db'))
    number = log.add('request', 'asked', 'jordan', model='sample')
    log.add('request', 'done', 'jordan', of=number, answer_tokens=10)
    store = server.app.state.benchmarks.state.store
    run = store.create('jordan', 'sample', 'load')
    store.finish(run, 'completed')
    paths = (*PAGES, f'/responses/{saved_response["id"]}', '/responses?q=no-such-input', f'/benchmarks/run/{run}', f'/collections/{collection}',
             f'/collections/{collection}/sources/{chunk}', f'/chat/{conversation}', f'/chat/{comparison}',
             '/file?path=' + quote(file, safe=''), f'/log/{number}', f'/requests/{number}', f'/workflows/{workflow}')
    yield server, paths
    assert not store.db.execute("SELECT id FROM benchmark_runs WHERE state IN ('queued','running')").fetchall()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('theme,palette', [('light', 'slate'), ('dark', 'slate'), ('light', 'forest')])
def test_all_console_pages_share_sizes_and_fit(ui_console, tmp_path, width, theme, palette):
    """Every audited route renders in two widths, both appearances and a second palette."""
    from test_console_browser import browser_context, playwright
    server, paths = ui_console
    failures = []
    measurements = []
    errors = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        page.emulate_media(color_scheme=theme)
        # Exercise the real account-scoped palette preference reader on every navigation.
        page.add_init_script('''(() => {
            const save = () => {
                const identity = document.documentElement?.dataset.themeUser;
                if (identity) localStorage.setItem('weightroom-theme-v1:' + encodeURIComponent(identity),
                    JSON.stringify(%s));
            };
            new MutationObserver(save).observe(document, {childList: true, subtree: true, attributes: true, attributeFilter: ['data-theme-user']});
            save();
        })();''' % json.dumps({'mode': theme, 'palette': palette}))
        page.on('pageerror', lambda error: errors.append(str(error)))
        for path in paths:
            sign_in(server, 'jordan')
            response = page.goto('http://console.test' + path)
            if response.status != 200:
                failures.append(f'{path}: HTTP {response.status}')
                continue
            page.wait_for_function('''!document.querySelector('[data-deferred-loader][hx-trigger="load"][aria-busy="true"], .htmx-settling')''')
            result = page.evaluate(MEASURE)
            measurements.append({'path': path, **result})
            problems = []
            if result['headerDescriptions']:
                problems.append(('page descriptions', result['headerDescriptions']))
            if len(result['titles']) != 1 or any(t['size'] != '24px' for t in result['titles']):
                problems.append(('page title', result['titles']))
            wrong_headers = [h for h in result['headers'] if (h['size'], h['weight'], h['padding']) !=
                             ('14px', '600', ['16px', '20px', '16px', '20px'])]
            if wrong_headers:
                problems.append(('card headers', wrong_headers))
            wrong_sections = [h for h in result['sections'] if h['size'] != '14.4px' or h['weight'] != '600']
            if wrong_sections:
                problems.append(('section titles', wrong_sections))
            if result['mismatches'] or result['invalidControls']:
                problems.append(('controls', result['mismatches'], result['invalidControls']))
            if result['namelessFields']:
                problems.append(('fields without accessible names', result['namelessFields']))
            wrong_kpis = [item for item in result['kpis'] if not item['label'] or not item['value'] or
                          item['value']['weight'] != '600' or
                          item['value']['size'] != ('27.2px' if width > 767 else '21.6px') or
                          item['padding'] != ['10.4px', '14.4px', '10.4px', '14.4px']]
            if wrong_kpis:
                problems.append(('KPI presentation', wrong_kpis))
            if result['rawValues'] and not path.startswith('/docs'):
                problems.append(('raw display values', result['rawValues']))
            if result['overflow']:
                problems.append(('horizontal overflow', width))
            expected_palette = 'slate' if path == '/sign-in' else palette
            if (result['theme'], result['palette']) != (theme, expected_palette):
                problems.append(('appearance', result['theme'], result['palette']))
            if problems:
                failures.append(f'{path}: {problems}')
                page.screenshot(path=str(tmp_path / f'failure-{len(failures)}.png'), full_page=True)
        browser.close()
    output = Path(os.environ.get('UI_REPORT_DIR', tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / f'{width}-{theme}-{palette}.json').write_text(json.dumps(measurements, indent=2))
    assert not errors, errors
    assert not failures, '\n'.join(failures)


@pytest.mark.parametrize('width', [1440, 390])
def test_all_console_neutral_surfaces_retheme_without_reloading(ui_console, width):
    from test_console_browser import browser_context, playwright
    from test_themes import change_theme

    server, paths = ui_console
    failures = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        for path in paths:
            sign_in(server, 'jordan')
            response = page.goto('http://console.test' + path)
            assert response.status == 200, path
            page.wait_for_function('!document.querySelector("[data-deferred-loader][aria-busy=true], .htmx-settling")')
            if not page.locator('html').get_attribute('data-theme-user'):
                continue
            change_theme(page, 'slate', 'dark')
            page.evaluate('''() => {
                const selector = 'main pre, main code, main kbd, main .kbd, main .frame, main .card, ' +
                    'main .card-header, main .card-footer, main .form-control, main .form-select, ' +
                    'main .input-group-text, main .text-bg-light, main .text-bg-dark, main .text-bg-secondary';
                window.themeSurfaces = [...document.querySelectorAll(selector)].map(node => ({
                    node, background: getComputedStyle(node).backgroundColor
                })).filter(sample => sample.background !== 'rgba(0, 0, 0, 0)');
            }''')
            change_theme(page, 'forest', 'dark')
            stuck = page.evaluate('''() => window.themeSurfaces.filter(({node, background}) =>
                node.isConnected && getComputedStyle(node).backgroundColor === background
            ).map(({node, background}) => ({tag: node.tagName, id: node.id, classes: node.className, background}))''')
            if stuck:
                failures.append({'path': path, 'surfaces': stuck})
        browser.close()
    assert not failures, json.dumps(failures, indent=2)


@pytest.mark.parametrize('sidebar', ['closed', 'open'])
def test_all_console_pages_share_outer_layout_when_resized(ui_console, tmp_path, sidebar):
    """Page gutters stay equal through navigation, rail toggles and breakpoint crossings."""
    from test_console_browser import browser_context, playwright
    server, paths = ui_console
    widths = (1440, 320, 390, 575, 576, 767, 768, 900, 992, 1024, 1920, 2560)
    failures = []
    cases = [(path, 'jordan', 200) for path in paths]
    cases.extend([('/no-such-page', 'jordan', 404), ('/settings', 'pat', 403),
                  ('/models/no-such-model', 'jordan', 404)])
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.add_init_script(f"localStorage.setItem('weightroom-sidebar', '{sidebar}')")
        for path, account, status in cases:
            sign_in(server, account)
            response = page.goto('http://console.test' + path)
            assert response.status == status, (path, response.status, failures)
            for width in widths:
                page.set_viewport_size({'width': width, 'height': 1000})
                # Let matchMedia move the navigation before measuring the new layout.
                page.evaluate('() => new Promise(requestAnimationFrame)')
                layout = page.evaluate('''() => {
                    const main = document.querySelector('#page-content');
                    const rect = main.getBoundingClientRect(), style = getComputedStyle(main);
                    const rail = document.querySelector('#site-sidebar');
                    const start = rail?.getBoundingClientRect().right || 0;
                    const viewport = document.documentElement.clientWidth;
                    const header = main.querySelector('.page-header');
                    // Chat and sign-in deliberately center an inner reading/form column.
                    const centered = header?.closest('.chat-main, .entry');
                    return {
                        left: rect.left - start + parseFloat(style.paddingLeft),
                        right: viewport - rect.right + parseFloat(style.paddingRight),
                        start, mainLeft: rect.left, mainRight: rect.right, viewport,
                        headerLeft: centered ? null : header?.getBoundingClientRect().left,
                        contentLeft: rect.left + parseFloat(style.paddingLeft),
                        overflow: document.documentElement.scrollWidth > viewport,
                        overflowing: [...document.querySelectorAll('body *')]
                            .filter(e => e.getBoundingClientRect().right > viewport + 1)
                            .slice(0, 8).map(e => [e.tagName, e.className]),
                        sidebar: document.documentElement.dataset.sidebar,
                        hasRail: !!rail,
                    };
                }''')
                problems = []
                if abs(layout['left'] - layout['right']) > 1 or not 12 <= layout['left'] <= 28:
                    problems.append(('gutters', layout['left'], layout['right']))
                if abs(layout['mainLeft'] - layout['start']) > 1 or abs(layout['mainRight'] - layout['viewport']) > 1:
                    problems.append(('container bounds', layout['start'], layout['mainLeft'], layout['mainRight']))
                if layout['overflow']:
                    problems.append(('overflow', layout['overflowing']))
                if path != '/sign-in' and layout['headerLeft'] is not None:
                    if abs(layout['headerLeft'] - layout['contentLeft']) > 1:
                        problems.append(('header alignment', layout['headerLeft'], layout['contentLeft']))
                if layout['hasRail']:
                    expected = 'closed' if width < 768 else sidebar
                    if layout['sidebar'] != expected:
                        problems.append(('sidebar', layout['sidebar'], expected))
                if problems:
                    failures.append((path, width, sidebar, problems))
                if (path, width) in {('/system/voice', 390), ('/system/voice', 1440),
                                     ('/models/images', 768), ('/usage', 2560)}:
                    page.screenshot(path=str(tmp_path / f'{sidebar}-{path.strip("/").replace("/", "-")}-{width}.png'))
            # Keep a desktop viewport for the next full navigation as well.
            page.set_viewport_size({'width': 1440, 'height': 1000})
        browser.close()
    assert not failures, '\n'.join(map(str, failures))


def test_component_tokens_resize_fields_and_keep_native_card_behavior():
    """A single token edit can shrink controls; forms/actions and native details keep working."""
    from jinja2 import FileSystemLoader
    from test_console_browser import playwright
    environment = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=True)
    fragment = environment.from_string('''
        {% from 'parts.html' import card, page_header, form_section %}
        {{ page_header(title, 'Shared components') }}
        {% set actions %}<form id="header-form"><button class="btn btn-sm">Refresh</button></form>{% endset %}
        {% call card(title, actions=actions, id='fixed') %}
          <form id="fields"><div class="row g-3">{% call form_section('Options', 'This node') %}
          <div class="col"><label for="field" class="form-label">Value</label><input id="field" class="form-control"></div>
          <div class="col"><label for="time" class="form-label">Time</label><input id="time" type="time" class="form-control" value="03:00"></div>
          <div class="col"><label for="select" class="form-label">Choice</label><select id="select" class="form-select"><option>One</option></select></div>
          <div class="col-12"><button class="btn">Save</button></div>{% endcall %}</div></form>
          <div class="input-group input-group-sm"><input id="compact" class="form-control"><span class="input-group-text">s</span><button class="btn">Apply</button></div>
        {% endcall %}
        {% call card('Collapsed', false, key='collapsed', id='closed', body_attrs={'id':'body-fragment'}) %}Content{% endcall %}
        {% call card('Expanded', true, key='expanded', id='opened') %}Content{% endcall %}
    ''').render(title='<img src=x onerror=alert(1)>')
    assert '&lt;img' in fragment and '<img' not in fragment
    styles = '\n'.join((ROOT / 'weightroom/static' / name).read_text() for name in ('tabler.min.css', 'style.css'))
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 390, 'height': 1000})
        page.set_content('<!doctype html><style>' + styles + '</style><main>' + fragment + '</main>')
        assert page.locator('#fixed').evaluate('(e) => e.tagName') == 'SECTION'
        assert page.locator('#closed').get_attribute('open') is None
        assert page.locator('#opened').get_attribute('open') == ''
        page.locator('#closed > summary').focus()
        page.keyboard.press('Enter')
        assert page.locator('#closed').get_attribute('open') == ''
        assert page.locator('#header-form button').evaluate('(e) => e.form.id') == 'header-form'
        assert page.locator('#fields button').evaluate('(e) => e.form.id') == 'fields'
        assert page.locator('#body-fragment').get_attribute('class') == 'card-body'
        page.evaluate('''() => {
          const style = document.documentElement.style;
          style.setProperty('--suite-page-title-size', '28px');
          style.setProperty('--suite-card-title-size', '18px');
          style.setProperty('--suite-section-title-size', '16px');
          style.setProperty('--suite-card-header-padding', '12px 18px');
          style.setProperty('--suite-control-height', '36px');
          style.setProperty('--suite-control-compact-height', '28px');
        }''')
        assert page.locator('.page-title').evaluate('(e) => getComputedStyle(e).fontSize') == '28px'
        assert page.locator('#fixed .card-header').evaluate('(e) => getComputedStyle(e).padding') == '12px 18px'
        assert page.locator('#fixed .card-heading').evaluate('(e) => getComputedStyle(e).fontSize') == '18px'
        assert page.locator('.section-title').evaluate('(e) => getComputedStyle(e).fontSize') == '16px'
        for selector, expected in [('#fields input, #fields select, #fields button', 36), ('.input-group-sm > *', 28), ('#header-form button', 28)]:
            heights = page.locator(selector).evaluate_all('(items) => items.map(e => e.getBoundingClientRect().height)')
            assert all(abs(height - expected) < 1 for height in heights), (selector, heights)
        browser.close()


def test_dynamic_image_cards_use_shared_templates(ui_console):
    """Search results and workflow submissions use cloned components with unchanged payloads."""
    from test_console_browser import browser_context, playwright
    server, _ = ui_console
    sign_in(server, 'jordan')
    submitted = []
    unsafe_name = '<img src=x onerror=alert(1)>'

    def capture(route, path):
        value = None
        if path.startswith('/mirrorwall-models'):
            value = {'models': [{'name': 'picture', 'engine': 'sdcpp', 'node': 'compute',
                                'flags': {'model': '/models/picture.safetensors'},
                                'sdcpp': {'family': 'sdxl', 'defaults': {'width': 512, 'height': 512}}}],
                     'cards': [], 'nodes': [{'name': 'compute', 'state': 'ready'}],
                     'engines': [], 'default_node': 'compute'}
        elif path == '/mirrorwall-api/workflows':
            value = {'models': [{'id': 'picture', 'image_workflow': {'family': 'sdxl','defaults':{'width':512,'height':512}}}],
                     'assets': [], 'references': [], 'presets': [], 'jobs': [], 'can_manage': True}
        elif path.startswith('/mirrorwall-discovery?'):
            value = {'found': [{'name': unsafe_name, 'kind': 'checkpoint', 'base_model': 'SDXL',
                                'url': 'https://example.invalid/model', 'repository': 'civitai/123'}]}
        elif path == '/mirrorwall-api/presets':
            body = json.loads(route.request.post_data)
            submitted.append(body)
            value = {'id': 'saved', 'name': body['name'], 'version': 1,'model':body['model'],'settings':body['settings'],'published':False}
        elif path == '/mirrorwall-api/presets/saved':
            value = {'versions': []}
        if value is None:
            return False
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
        return True

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            page.goto('http://console.test/civitai')
            page.locator('#mw-q').fill('fixture')
            page.get_by_role('button', name='Search', exact=True).click()
            heading = page.locator('#mw-found .card-heading')
            playwright.expect(heading).to_have_text(unsafe_name)
            assert heading.locator('img').count() == 0
            assert heading.evaluate('(e) => getComputedStyle(e).fontSize') == '14px'
            assert page.locator('#mw-found [data-discovery-body]').count() == 1
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            page.goto('http://console.test/models/images/workflows')
            playwright.expect(page.locator('#workflow-name')).to_be_visible()
            assert page.locator('#editor-panel > section > .card-header').count() == 1
            assert page.locator('#editor-panel > section > .card-body').count() == 1
            page.locator('#workflow-name').fill('Shared component workflow')
            page.get_by_role('button', name='Save workflow', exact=True).click()
            playwright.expect(page.get_by_role('status').filter(has_text='Workflow saved.')).to_be_visible()
            assert submitted[-1]['model'] == 'picture'
            assert submitted[-1]['settings']['width'] == 512
            assert submitted[-1]['name'] == 'Shared component workflow'
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not errors, errors
        browser.close()


def test_dynamic_warning_and_empty_rows_keep_their_actions(ui_console):
    from test_console_browser import browser_context, playwright

    server, _ = ui_console
    sign_in(server, 'jordan')

    def capture(route, path):
        if path.startswith('/mirrorwall-models/check'):
            matches = route.request.post_data_json['settings']['sdcpp']['family'] == 'sdxl'
            route.fulfill(content_type='application/json', body=json.dumps({
                'ready': matches, 'detected_family': 'sdxl', 'family_matches': matches,
                'slots': [{'slot': 'diffusion', 'status': 'ok', 'need': 'required', 'message': 'Present'}],
                'loras': [], 'get_diffusion': [],
            }))
            return True

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/models/images?model=picture')
        playwright.expect(page.locator('#mw-name')).to_have_value('picture')
        page.locator('#studio-edit-model').click()
        page.locator('#mw-family').select_option('flux2-klein')
        playwright.expect(page.locator('#mw-check').get_by_role('alert')).to_contain_text('These weights are SDXL')
        page.get_by_role('button', name='Use SDXL', exact=True).click()
        playwright.expect(page.locator('#mw-family')).to_have_value('sdxl')
        playwright.expect(page.locator('#mw-check').get_by_role('alert')).to_have_count(0)
        page.goto('http://console.test/usage?model=absent')
        page.locator('#usage-group-by').select_option('model')
        empty = page.locator('#usage-breakdown tbody [data-empty-row]')
        playwright.expect(empty).to_have_text('No usage in this range.')
        playwright.expect(empty.locator('td')).to_have_attribute('colspan', '10')
        assert not errors
        browser.close()


def test_errors_keep_console_status_identity_and_safe_details(desk, monkeypatch):
    """404/403/500 differ, keep the right navigation and never expose exception details."""
    from starlette.routing import Route
    from starlette.exceptions import HTTPException
    from starlette.templating import Jinja2Templates

    server, _, _ = desk
    unknown = server.get('/no-such-page')
    assert unknown.status_code == 404 and '<main ' in unknown.text
    assert '<title>Page not found · openWeightGym</title>' in unknown.text
    assert 'href="/status"' in unknown.text and 'Not here' not in unknown.text
    sign_in(server, 'pat')
    for path, explanation in [('/settings', 'settings'), ('/models', 'models'),
                              ('/users', 'users'), ('/compute-nodes', 'settings'), ('/docs/api', 'documentation')]:
        refused = server.get(path)
        assert refused.status_code == 403
        assert 'Access denied' in refused.text and explanation in refused.text
        assert 'Ask your administrator' in refused.text
        assert 'said-box' not in refused.text
        assert ('nav-link active' in refused.text) == (path == '/docs/api')
        assert ('sidebar-toggle' in refused.text) == (path == '/docs/api')
    sign_in(server, 'jordan')
    for path, explanation in [('/models/no-such-model', 'no-such-model'),
                              ('/requests/999999', 'request'), ('/benchmarks/run/nope', 'run'),
                              ('/docs/no-such-topic', 'Documentation page not found')]:
        missing = server.get(path)
        assert missing.status_code == 404 and 'Page not found' in missing.text
        assert explanation.lower() in missing.text.lower()
        assert 'Access denied' not in missing.text
    escaped = server.get('/models/%3Cimg%20src=x%20onerror=alert(1)%3E')
    assert escaped.status_code == 404 and '&lt;img' in escaped.text and '<img src=x' not in escaped.text
    original = Jinja2Templates.TemplateResponse

    def broken_home(self, request, name, *args, **kwargs):
        if name == 'home.html':
            raise RuntimeError('private exception details')
        return original(self, request, name, *args, **kwargs)

    monkeypatch.setattr(Jinja2Templates, 'TemplateResponse', broken_home)
    failed = server.get('/')
    assert failed.status_code == 500 and 'Something went wrong' in failed.text
    assert failed.headers['x-request-id'] in failed.text
    assert 'private exception details' not in failed.text and 'jordan</summary>' in failed.text
    assert failed.headers['cache-control'] == 'no-store'

    async def unavailable(request):
        raise RuntimeError('private API details')

    async def wrong_method(request):
        raise HTTPException(405, headers={'Allow': 'POST'})

    server.app.router.routes.extend([Route('/loadcoach/test-error', unavailable), Route('/test-method', wrong_method)])
    api = server.get('/loadcoach/test-error')
    assert api.status_code == 500 and api.json() == {'error': {'message': 'Internal server error.'}}
    method = server.get('/test-method')
    assert method.status_code == 405 and method.headers['allow'] == 'POST' and '<main ' in method.text
    server.cookies.clear()
    # A stale cookie and an unavailable identity service must not break the error renderer.
    server.cookies.set('weightroom', 'expired')
    assert server.get('/no-such-page').status_code == 404
    import httpx
    request = httpx.AsyncClient.request

    async def no_identity(self, method, url, *args, **kwargs):
        if url == '/loadcoach/me':
            raise httpx.ConnectError('identity unavailable')
        return await request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, 'request', no_identity)
    unavailable_identity = server.get('/no-such-page')
    assert unavailable_identity.status_code == 404 and 'Page not found' in unavailable_identity.text


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_error_pages_are_readable_and_navigable(desk, monkeypatch, width, theme):
    """The reason stays below the heading, remains visible, and fits both screen sizes."""
    from test_console_browser import browser_context, playwright
    from starlette.templating import Jinja2Templates
    original = Jinja2Templates.TemplateResponse

    def broken_status(self, request, name, *args, **kwargs):
        if name == 'home.html' and 'simulate_error' in request.query_params:
            raise RuntimeError('private exception details')
        return original(self, request, name, *args, **kwargs)

    monkeypatch.setattr(Jinja2Templates, 'TemplateResponse', broken_status)
    server, _, _ = desk
    output = Path(os.environ.get('UI_REPORT_DIR', 'data/qualification/usability/v1'))
    output.mkdir(parents=True, exist_ok=True)
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        page.emulate_media(color_scheme=theme)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        for account, path, status, heading, section in [
            ('jordan', '/no-such-page', 404, 'Page not found', None),
            ('jordan', '/models/no-such-model', 404, 'Page not found', 'Models'),
            ('jordan', '/requests/999999', 404, 'Page not found', 'Logs'),
            ('jordan', '/benchmarks/run/nope', 404, 'Page not found', 'Benchmark'),
            ('pat', '/settings', 403, 'Access denied', None),
            ('pat', '/models', 403, 'Access denied', None),
            ('pat', '/users', 403, 'Access denied', None),
            ('pat', '/deploy', 404, 'Page not found', None),
            ('pat', '/?simulate_error=1', 500, 'Something went wrong', 'System'),
            ('', '/no-such-page', 404, 'Page not found', None),
        ]:
            server.cookies.clear()
            if account:
                sign_in(server, account)
            response = page.goto('http://console.test' + path)
            assert response.status == status
            playwright.expect(page.get_by_role('heading', name=heading, exact=True)).to_be_visible()
            reason = page.locator('main [role=alert]')
            playwright.expect(reason).to_be_visible()
            assert page.locator('main h1').bounding_box()['y'] + page.locator('main h1').bounding_box()['height'] <= reason.bounding_box()['y']
            assert page.evaluate("getComputedStyle(document.querySelector('main [role=alert]')).position") == 'static'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            active = page.locator('.category-links [aria-current]')
            assert active.all_text_contents() == ([section] if section else [])
            if status == 500:
                playwright.expect(page.locator('main code')).to_have_text(re.compile(r'^[0-9a-f]{32}$'))
                assert 'private exception details' not in page.content()
            name = (account or 'signed-out') + path.replace('/', '-').replace('?', '-').replace('=', '-') + f'-{width}-{theme}.png'
            page.screenshot(path=str(output / name), full_page=True)
            if width == 1440 and theme == 'light' and path == '/settings':
                page.wait_for_timeout(7200)
                playwright.expect(reason).to_be_visible()
            page.get_by_role('link', name='Go to Status', exact=True).click()
            playwright.expect(page.get_by_role('heading', name='Status' if account else 'Sign in', exact=True)).to_be_visible()
        assert not errors
        browser.close()


def test_destructive_submitters_have_shared_confirmation(ui_console):
    from test_console_browser import browser_context, playwright
    server, paths = ui_console
    failures = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        for path in paths:
            sign_in(server, 'jordan')
            page.goto('http://console.test' + path)
            unsafe = page.evaluate(r"""() => [...document.querySelectorAll('form button')].filter(b => {
              if (b.type !== 'submit' || b.closest('dialog')) return false;
              const label = b.getAttribute('aria-label') || b.textContent;
              return /\b(delete|remove|forget|revoke|reset embedding|rotate|stop|unload|cancel|disable|clear queue|clear cache|cleanup nodes|restart|reboot|power off)\b/i.test(label) &&
                !b.matches('[data-confirm-two], [data-confirm-loss]') && !b.form?.matches('[data-confirm-two]') &&
                !b.form?.querySelector('input[type=checkbox][required]');
            }).map(b => b.outerHTML.slice(0, 220))""")
            if unsafe: failures.append((path, unsafe))
        browser.close()
    assert not failures, failures


def test_shared_confirmation_blocks_native_and_htmx_until_approved(tmp_path):
    from jinja2 import FileSystemLoader
    from test_console_browser import playwright
    environment = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=True)
    fragment = environment.from_string("""
      {% from 'parts.html' import confirm_two, confirm_loss, danger_confirmation %}
      <form method="post" action="/action" target="result"><input name="kept" value="original">
      <button name="action" value="stop" {{ confirm_two() }}>Stop</button>
      <button name="action" value="delete" {{ confirm_loss() }}>Delete saved output</button></form>
      <form hx-post="/action" hx-target="#response"><button name="action" value="clear" {{ confirm_two() }}>Clear queue</button></form>
      <div id="response"></div><iframe name="result" title="Result" hidden></iframe>
      {{ danger_confirmation() }}
    """).render()
    markup = '<html><meta charset="utf-8"><body>' + fragment + '<script>' + (ROOT / 'weightroom/static/htmx.min.js').read_text() + '</script><script>' + (ROOT / 'weightroom/static/console.js').read_text() + '</script></body></html>'
    requests = []
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page()
        def serve(route):
            if route.request.method == 'POST':
                requests.append(route.request.post_data)
                route.fulfill(content_type='text/html', body='Completed')
            else: route.fulfill(content_type='text/html', body=markup)
        page.route('http://confirmation.test/**', serve)
        page.goto('http://confirmation.test/')
        page.get_by_role('button', name='Stop', exact=True).click()
        assert not requests
        with page.expect_response(lambda response: response.url.endswith('/action') and response.request.method == 'POST'):
            page.get_by_role('button', name='Are you sure? Stop', exact=True).click()
        page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        playwright.expect(page.get_by_role('button', name='Stop', exact=True)).to_be_enabled()
        assert requests == ['kept=original&action=stop']
        page.get_by_role('button', name='Delete saved output', exact=True).click()
        assert not page.locator('#danger-confirmation [data-confirm-description]').count()
        playwright.expect(page.get_by_role('dialog')).to_be_visible()
        page.get_by_role('button', name='Continue', exact=True).click()
        assert len(requests) == 1
        page.keyboard.press('Escape')
        playwright.expect(page.get_by_role('dialog')).not_to_be_visible()
        page.get_by_role('button', name='Delete saved output', exact=True).click()
        page.get_by_label('Yes, delete saved output').check()
        page.get_by_role('button', name='Continue', exact=True).click()
        page.wait_for_timeout(50)
        assert requests[-1] == 'kept=original&action=delete'
        page.get_by_role('button', name='Clear queue', exact=True).click()
        assert len(requests) == 2
        page.get_by_role('button', name='Are you sure? Clear queue', exact=True).click()
        playwright.expect(page.locator('#response')).to_have_text('Completed')
        assert requests[-1] == 'action=clear' and len(requests) == 3
        browser.close()


def test_submission_feedback_preserves_values_blocks_duplicates_and_restores():
    from test_console_browser import playwright
    markup = """<html><meta charset="utf-8"><body><form method="post" action="/native" target="result"><input name="kept" value="original"><button name="action" value="deploy">Deploy</button></form><iframe name="result" title="Result" hidden></iframe><form hx-post="/htmx" hx-target="#response"><button name="action" value="start">Start run</button></form><div id="response"></div>"""
    markup += '<script>' + (ROOT / 'weightroom/static/htmx.min.js').read_text() + '</script><script>' + (ROOT / 'weightroom/static/console.js').read_text() + '</script></body></html>'
    pending = []
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page()
        def serve(route):
            if route.request.method == 'POST': pending.append(route)
            else: route.fulfill(content_type='text/html', body=markup)
        def posted():
            """The held POST. The button is disabled on submit, before the request reaches the route, so wait for the request itself."""
            for _ in range(250):
                if pending:
                    return pending[0]
                page.wait_for_timeout(20)  # route handlers run only while Playwright is being waited on
            raise AssertionError('the form was never posted')
        page.route('http://feedback.test/**', serve)
        page.goto('http://feedback.test/')
        page.get_by_role('button', name='Deploy', exact=True).click()
        button = page.get_by_role('button', name='Working…', exact=True)
        playwright.expect(button).to_be_disabled()
        assert posted().request.post_data == 'kept=original&action=deploy'
        page.evaluate("document.querySelector('form').requestSubmit(document.querySelector('button'))")
        page.wait_for_timeout(200)  # time for a duplicate request to arrive, if one were sent
        assert len(pending) == 1
        pending.pop().fulfill(content_type='text/html', body='Done')
        page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        playwright.expect(page.get_by_role('button', name='Deploy', exact=True)).to_be_enabled()
        page.get_by_role('button', name='Start run', exact=True).click()
        playwright.expect(page.get_by_role('button', name='Working…', exact=True)).to_be_disabled()
        assert posted().request.post_data == 'action=start'
        pending.pop().fulfill(status=500, content_type='text/html', body='Try again')
        playwright.expect(page.get_by_role('button', name='Start run', exact=True)).to_be_enabled()
        assert not page.locator('form[aria-busy=true]').count()
        browser.close()


def test_sign_in_keeps_safe_deep_links_and_rejects_external_destinations(desk):
    server, _, _ = desk
    path = '/account/logs?scope=mine'
    redirect = server.get(path, follow_redirects=False)
    assert redirect.status_code == 303 and 'next=' in redirect.headers['location']
    login = server.get(redirect.headers['location'])
    assert 'name="next" value="/account/logs?scope=mine"' in login.text
    failure = server.post('/sign-in', data={'name': 'pat', 'password': 'wrong', 'next': path})
    assert failure.status_code == 401 and 'name="next" value="/account/logs?scope=mine"' in failure.text
    fragment = server.get('/chat?part=main&model=small', follow_redirects=False)
    assert fragment.headers['location'] == '/sign-in?next=%2Fchat%3Fmodel%3Dsmall'
    success = server.post('/sign-in', data={'name': 'pat', 'password': 'correct horse', 'next': path}, follow_redirects=False)
    assert success.status_code == 303 and success.headers['location'] == path
    for destination in ['https://elsewhere.test/', '//elsewhere.test', '/\\elsewhere.test', '/%2felsewhere.test', '/%0d%0aLocation:evil', 'javascript:alert(1)']:
        server.cookies.clear()
        refused = server.post('/sign-in', data={'name': 'pat', 'password': 'correct horse', 'next': destination}, follow_redirects=False)
        assert refused.status_code == 303 and refused.headers['location'] == '/status'


def test_mobile_menu_has_both_levels_keyboard_return_and_desktop_rail(desk):
    from test_console_browser import browser_context, playwright
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.goto('http://console.test/chat')
        menu = page.get_by_role('button', name='Menu', exact=True)
        playwright.expect(menu).to_be_visible()
        playwright.expect(page.locator('#sidebar-toggle')).not_to_be_visible()
        menu.click()
        dialog = page.get_by_role('dialog', name='Chat options', exact=True)
        playwright.expect(dialog).to_be_visible()
        playwright.expect(dialog.get_by_role('searchbox', name='Find a conversation')).to_be_visible()
        dialog.get_by_role('button', name='Show main menu', exact=True).click()
        dialog = page.get_by_role('dialog', name='Navigation', exact=True)
        assert dialog.locator('summary').all_text_contents() == ['Interact','Models','Benchmark','Logs','System','Settings','Docs']
        dialog.locator('summary').filter(has_text='Settings').click()
        playwright.expect(dialog.get_by_role('link', name='General', exact=True)).to_be_visible()
        assert dialog.locator('.chat-sidebar').count() == 1
        page.keyboard.press('Escape')
        playwright.expect(menu).to_be_focused()
        page.set_viewport_size({'width': 1440, 'height': 1000})
        playwright.expect(menu).not_to_be_visible()
        playwright.expect(page.locator('#site-sidebar')).to_be_visible()
        assert page.locator('[data-desktop-conversations] .chat-sidebar').count() == 1
        category = page.locator('.category-links a[href="/chat"]')
        playwright.expect(category).to_have_text('Interact')
        playwright.expect(category).to_have_attribute('aria-current', 'true')
        playwright.expect(category.locator('use')).to_have_attribute('href', '/static/icons.svg?v=3#chat')
        links = page.locator('#site-sidebar nav').bounding_box()
        history = page.locator('.chat-sidebar').bounding_box()
        assert links['x'] + links['width'] < history['x']
        control = page.locator('#sidebar-toggle')
        assert control.bounding_box()['y'] > 800
        control.click()
        playwright.expect(page.locator('#site-sidebar')).not_to_be_visible()
        control.click()
        playwright.expect(page.locator('#site-sidebar')).to_be_visible()
        browser.close()


def test_chat_keeps_answering_model_visible_and_explains_no_access(desk):
    from test_console_browser import browser_context, playwright
    server, _, users = desk
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        for width in (1440,390):
            page.set_viewport_size({'width':width,'height':1000})
            page.goto('http://console.test/chat')
            playwright.expect(page.get_by_role('button',name='Model',exact=True)).to_be_visible()
            playwright.expect(page.get_by_role('textbox',name='Message',exact=True)).to_be_visible()
            assert page.locator('[data-composer-options]').get_attribute('open') is None
        browser.close()
    users.change('pat', own={'models': []})
    empty = server.get('/chat')
    assert 'No models are available to your account' in empty.text
    assert 'disabled aria-describedby=chat-no-models' in empty.text


def test_user_facing_product_name_does_not_rename_runtime_identity(desk):
    server, _, _ = desk
    sign_in(server, 'pat')
    home = server.get('/')
    assert '<span>weightRoom</span>' in home.text and '· openWeightGym</title>' in home.text
    assert 'weightroom' in server.cookies
    manifest = server.get('/static/site.webmanifest').json()
    assert manifest['name'] == manifest['short_name'] == 'openWeightGym'
    assert 'Sign in · openWeightGym</title>' in server.get('/sign-in').text
    model_copy = (ROOT / 'weightroom/pages/model_locations.html').read_text()
    assert 'The gateway may place' in model_copy and 'Turnstile may place' not in model_copy


def test_status_and_empty_state_copy_is_plain(desk):
    server, _, _ = desk
    sign_in(server,'jordan')
    assert 'Every fingerprint there has been' not in server.get('/models?complete=1').text
    assert 'No conversations yet.' in server.get('/chat').text
    status=(ROOT/'weightroom/pages/status_nodes.html').read_text()
    assert 'No GPUs are reserved exclusively' in status and 'Nobody holds' not in status


def test_display_preserves_zero_formats_counts_and_escapes_logs(desk):
    from weightroom import display
    assert display.value(None) == '—' and display.value(float('nan')) == '—'
    assert display.value(0) == 0 and display.count(10000.0) == '10,000'
    assert display.count(None) == '—' and display.count(0) == '0'
    line = display.log_line('2026-10-08T10:00:00Z <script>private</script>')
    assert '<time ' in line and '&lt;script&gt;' in line and '<script>' not in line
    server, _, _ = desk
    sign_in(server,'pat')
    home = server.get('/usage?complete=1')
    assert 'data-usage-total="total_tokens">0</strong>' in home.text
    assert 'data-usage-total="total_tokens">0.0' not in home.text


def test_times_use_browser_zone_keep_utc_and_label_tables_once(desk):
    from test_console_browser import playwright
    from weightroom import display
    mark = str(display.timestamp('2026-10-30T12:00:00Z'))
    script = (ROOT/'weightroom/static/console.js').read_text()
    markup = '<html><meta charset="utf-8"><body><table><tbody><tr><td>' + mark + mark + '</td></tr></tbody></table><div id="outside">' + mark + '</div><script>' + script + '</script></body></html>'
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True,args=['--disable-gpu'])
        for zone,hour in [('America/Vancouver','05:00'),('Asia/Tokyo','21:00')]:
            context=browser.new_context(timezone_id=zone,locale='en-GB')
            page=context.new_page();page.set_content(markup)
            assert hour in page.locator('table time').first.inner_text()
            assert page.locator('time').first.get_attribute('title') == '2026-10-30T12:00:00Z'
            assert page.locator('[data-table-time-zone]').all_text_contents() == ['Times: '+zone]
            page.evaluate('SuiteTime.localize()')
            assert page.locator('[data-table-time-zone]').count() == 1
            page.evaluate("document.querySelector('td').insertAdjacentHTML('beforeend',document.querySelector('time').outerHTML);document.body.dispatchEvent(new CustomEvent('htmx:afterSwap',{bubbles:true,detail:{target:document.querySelector('td')}}))")
            assert hour in page.locator('table time').last.inner_text()
            context.close()
        browser.close()


def test_local_time_filters_serialize_in_the_existing_server_zone():
    from test_console_browser import playwright
    from weightroom.display import input_utc
    assert input_utc('2026-10-30T12:00:00','UTC') == '2026-10-30T12:00:00+00:00'
    script=(ROOT/'weightroom/static/console.js').read_text()
    html='<html><meta charset="utf-8"><form method="get" action="/filter" target="result"><label for="at">Around</label><input id="at" name="at" type="datetime-local" data-input-zone="UTC" data-instant="2026-10-30T12:00:00Z"><button>Show</button></form><iframe name="result" title="Result" hidden></iframe><script>'+script+'</script></html>'
    urls=[]
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True,args=['--disable-gpu'])
        context=browser.new_context(timezone_id='America/Vancouver')
        page=context.new_page()
        def serve(route):
            if '/filter?' in route.request.url:
                urls.append(route.request.url);route.fulfill(content_type='text/html',body='Done')
            else:route.fulfill(content_type='text/html',body=html)
        page.route('http://time.test/**',serve);page.goto('http://time.test/')
        playwright.expect(page.get_by_label('Around')).to_have_value('2026-10-30T05:00')
        with page.expect_response(lambda r:'/filter?' in r.url):page.get_by_role('button',name='Show',exact=True).click()
        assert 'at=2026-10-30T12%3A00' in urls[0]
        playwright.expect(page.get_by_label('Around')).to_have_value('2026-10-30T05:00')
        browser.close()


def test_empty_editor_tables_have_next_steps_and_clear_placeholder_on_add(ui_console):
    from test_console_browser import browser_context, playwright
    server, _ = ui_console
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,server)
        for path,selector,text in [('/models/images','#mw-lora-rows','No image adapters'),('/models/sample','[data-adapter-rows]','No adapters')]:
            page.goto('http://console.test'+path)
            body=page.locator(selector)
            # Adapter sections are allowed to remain inside their native disclosure.
            assert text in body.text_content()
            page.evaluate("selector => {const body=document.querySelector(selector);const row=body.insertRow();row.insertCell().textContent='New item';}",selector)
            playwright.expect(body.locator('[data-empty-row]')).to_have_count(0)
        browser.close()


def test_models_list_leads_and_add_explains_disabled_state(ui_console):
    from test_console_browser import browser_context, playwright
    server,_=ui_console;sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,server)
        page.goto('http://console.test/models')
        assert page.locator('#add-model').evaluate('el => el.tagName') == 'SECTION'  # a card that cannot be collapsed
        assert page.locator('#models-table').bounding_box()['y'] < page.locator('#add-model').bounding_box()['y']
        assert not page.get_by_text('Storage originals are retained', exact=False).count()
        assert not page.get_by_role('link', name='View model fingerprints').count()
        page.get_by_role('link',name='Add a model',exact=True).click()
        button=page.locator('[data-file-submit]')
        playwright.expect(button).to_be_disabled()
        assert button.get_attribute('title') == page.locator('#model-add-reason').inner_text()
        assert 'status not reported' in page.locator('#model-nodes').inner_text()
        assert page.get_by_role('button',name='Browse node files',exact=True).count() == 0
        browser.close()


def test_status_is_the_landing_page_without_home_navigation(ui_console):
    from test_console_browser import browser_context, playwright
    server, _ = ui_console
    signed_in = sign_in(server, 'pat')
    assert signed_in.url.path == '/status'
    redirect = server.get('/', follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers['location'] == '/status'
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/')
        playwright.expect(page.get_by_role('heading', name='Status', exact=True)).to_be_visible()
        assert page.locator('.category-links [aria-current]').all_text_contents() == ['System']
        assert not page.get_by_role('link', name='Home', exact=True).count()
        assert page.locator('.navbar-brand').get_attribute('href') == '/status'
        assert server.get('/static/site.webmanifest').json()['start_url'] == '/status'
        browser.close()


def test_every_account_has_usage_help_but_source_reference_stays_private(desk):
    server,_,_=desk;sign_in(server,'pat')
    guide=server.get('/docs/web-app/api-keys')
    assert guide.status_code == 200 and 'Create a key' in guide.text
    assert 'http://testserver:8442/v1' in guide.text and 'href="/docs/api"' not in guide.text
    assert server.get('/docs/web-app').status_code == 200
    assert server.get('/docs/web-app/chat').status_code == 200
    assert 'href="/docs/web-app/chat"' not in server.get('/chat').text
    assert 'href="/docs/web-app/collections"' in server.get('/collections').text
    assert 'href="/docs/web-app/workflows"' in server.get('/workflows').text
    assert 'href="/docs/web-app/api-keys"' in server.get('/account').text
    assert server.get('/docs').url.path == '/docs'
    assert server.get('/docs/api').status_code == 403
    moved=server.get('/guide',follow_redirects=False)
    assert moved.status_code == 308 and moved.headers['location'] == '/docs/web-app'
    server.cookies.clear()
    assert server.get('/docs/web-app',follow_redirects=False).status_code == 303


def test_failed_polls_show_connection_loss_until_each_region_recovers(desk):
    from test_console_browser import browser_context, playwright
    server,_,_=desk;sign_in(server,'pat')
    failing = {'value':False}
    def capture(route,path):
        if path.startswith('/retry-test'):
            route.fulfill(status=503 if failing['value'] else 200,content_type='text/html',body='<div id=retry-test hx-get=/retry-test hx-trigger="every 0.2s" hx-swap=outerHTML>Fresh</div>')
            return True
        return False
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,server,capture=capture)
        # This fixture bridges SSE with a finite snapshot; isolate actual polling failures.
        page.add_init_script("""const NativeEventSource = window.EventSource;
          window.EventSource = class { constructor(url) { if (!String(url).includes('/strip/stream')) return new NativeEventSource(url); } close() {} };""")
        page.goto('http://console.test/')
        page.evaluate("""document.querySelector('main').insertAdjacentHTML('beforeend','<div id=retry-test hx-get=/retry-test hx-trigger="every 0.2s" hx-swap=outerHTML>Saved</div>');htmx.process(document.querySelector('#retry-test'))""")
        failing['value']=True
        bar=page.locator('#connection-status')
        playwright.expect(bar).to_be_visible()
        playwright.expect(bar).to_have_text('Connection lost — retrying')
        page.evaluate("document.dispatchEvent(new CustomEvent('suite:connection',{detail:{source:'another-stream',okay:false,retrying:true}}))")
        failing['value']=False
        playwright.expect(page.locator('#retry-test')).to_have_text('Fresh')
        playwright.expect(bar).to_be_visible()
        page.evaluate("document.dispatchEvent(new CustomEvent('suite:connection',{detail:{source:'another-stream',okay:true}}))")
        playwright.expect(bar).not_to_be_visible()
        browser.close()


def test_chat_refresh_recovers_without_losing_the_finished_reply():
    from test_console_browser import playwright
    attempts = []
    markup = """<html><meta charset="utf-8"><body>
      <div id="connection-status" hidden></div>
      <div class="chat-main" id="chat-main"><div data-stream="/stream">Original streamed answer</div></div>
      <script>window.EventSource = class {
        constructor() { window.stream = this; this.listeners = {}; }
        addEventListener(name, fn) { this.listeners[name] = fn; }
        close() {}
      };</script>"""
    for name in ('console', 'chat'):
        markup += '<script>' + (ROOT / f'weightroom/static/{name}.js').read_text() + '</script>'
    markup += '</body></html>'
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def serve(route):
            if 'part=main' in route.request.url:
                attempts.append(True)
                route.fulfill(status=503 if len(attempts) == 1 else 200,
                              content_type='text/html', body='<p>Finished answer</p>')
            else:
                route.fulfill(content_type='text/html', body=markup)

        page.route('http://reply.test/**', serve)
        page.goto('http://reply.test/chat/1')
        page.evaluate('window.stream.listeners.done()')
        playwright.expect(page.locator('#connection-status')).to_have_text('Connection lost — retrying')
        playwright.expect(page.locator('[data-stream]')).to_have_text('Original streamed answer')
        playwright.expect(page.locator('.chat-main')).to_have_text('Finished answer')
        playwright.expect(page.locator('#connection-status')).not_to_be_visible()
        assert len(attempts) == 2 and not errors, errors
        browser.close()


def test_system_logs_refusal_is_permanent_and_unavailable_link_is_hidden(desk_with_machine):
    server, _, _, _, _ = desk_with_machine
    sign_in(server, 'pat')
    response = server.get('/output')
    assert response.status_code == 403 and 'restart-services ability' in response.text
    assert 'said-box' not in response.text and 'href="/output"' not in server.get('/usage?complete=1').text
