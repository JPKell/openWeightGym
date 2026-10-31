"""Request diagnostics remain useful offline and within existing visibility rules."""

import json

from loadcoach.log import Log
from test_weightroom import desk, desk_with_machine, sign_in  # noqa: F401


def recorded(tmp_path, owner='pat'):
    log = Log(str(tmp_path / 'data/accounts.db'), keep_text=5)
    entry = log.add('request', 'asked', owner, model='vision', trace='image-check', path='chat/completions',
                    role='chat:fast', picked='vision', because='Loaded model preference')
    tool = log.add('tool', 'called', owner, trace='image-check', tool='files_read', text={'arguments': 'PRIVATE_TOOL_ARGUMENT'})
    log.add('tool', 'returned', owner, trace='image-check', tool='files_read', of=tool, took=.2)
    log.add('request', 'done', owner, model='vision', trace='image-check', of=entry, status=500,
            why='image input is not supported <script>throw Error("unsafe")</script>', fingerprint='fp-vision',
            diagnostics={'version': 1, 'total_seconds': 2.5, 'first_byte_seconds': .8, 'first_output_seconds': None,
                         'context': {'source': 'local', 'images': True, 'vision_configured': False, 'projector': None,
                                     'context_tokens': 8192, 'cards': ['GPU0'], 'tools_offered': 1},
                         'native': {}, 'role': 'chat:fast',
                         'phases': [{'stage': 'queue', 'offset': 0, 'seconds': .5},
                                    {'stage': 'loading', 'offset': .5, 'seconds': 1},
                                    {'stage': 'upstream', 'offset': 1.5, 'seconds': .5},
                                    {'stage': 'response', 'offset': 2, 'seconds': .5}]})
    log.keep_asked(entry, json.dumps({'messages': [{'role': 'user', 'content': 'PRIVATE_PROMPT'}]}))
    log.keep_said(entry, {'content': 'PRIVATE_ANSWER'})
    log.db.close()
    return entry


def test_request_page_explains_image_failure_and_preserves_metadata_offline(desk_with_machine, tmp_path):
    server, compute, _, _, _ = desk_with_machine
    entry = recorded(tmp_path)
    sign_in(server, 'jordan')
    response = server.get(f'/requests/{entry}')
    assert response.status_code == 200
    html = response.text
    assert 'id="request-diagnostics"' in html and 'Waiting for model readiness' in html
    assert 'Images were sent with no projector configured' in html
    assert 'Loaded model preference' in html and 'chat:fast' in html
    assert 'Prompt processing' in html and 'Unavailable' in html
    assert 'PRIVATE_TOOL_ARGUMENT' not in html
    assert '<script>throw Error' not in html and '&lt;script&gt;throw Error' in html
    assert '/output?source=loadcoach&amp;at=' in html and '/log?trace=image-check' in html
    assert 'Recent latency and errors' in html and 'All authorized users' in html
    compute.off = True
    offline = server.get(f'/requests/{entry}')
    assert offline.status_code == 200 and 'Waiting for model readiness' in offline.text
    assert 'Node unavailable: compute' in offline.text
    sign_in(server, 'pat')
    own = server.get(f'/requests/{entry}')
    assert own.status_code == 200 and 'Your requests only' in own.text
    assert '/output?source=loadcoach' not in own.text
    assert 'PRIVATE_PROMPT' in own.text and 'PRIVATE_ANSWER' in own.text


def test_unfinished_and_legacy_requests_render_without_compute(desk, tmp_path):
    server, compute, _ = desk
    sign_in(server, 'pat')
    log = Log(str(tmp_path / 'data/accounts.db'))
    pending = log.add('request', 'asked', 'pat', model='old-model')
    old = log.add('request', 'asked', 'pat', model='old-model')
    log.add('request', 'done', 'pat', model='old-model', of=old, status=200, waited=1, ran=2)
    log.db.close()
    compute.off = True
    live = server.get(f'/requests/{pending}?part=live')
    assert live.status_code == 200 and 'live progress is unknown' in live.text
    assert 'Detailed timing was not recorded' in live.text
    legacy = server.get(f'/requests/{old}')
    assert legacy.status_code == 200 and 'Legacy measurement' in legacy.text and 'Inferred from event timestamps' in legacy.text


def test_request_diagnostics_desktop_mobile_links_and_refresh_selection(desk_with_machine, tmp_path):
    from test_console_browser import browser_context, playwright

    server, _, _, _, _ = desk_with_machine
    entry = recorded(tmp_path)
    sign_in(server, 'pat')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://console.test/requests/{entry}')
        assert [text.strip() for text in page.locator('main details.card > summary').all_text_contents()[:2]] == ['Request', 'Response']
        playwright.expect(page.locator('#request-diagnostics')).to_be_visible()
        playwright.expect(page.locator('#request-measurements')).to_contain_text('Unavailable')
        playwright.expect(page.locator('#request-activity')).to_contain_text('files_read')
        page.get_by_role('link', name='Trace image-check', exact=True).click()
        page.wait_for_url('**/log?trace=image-check')
        page.goto(f'http://console.test/requests/{entry}')
        # The existing refresh guard must protect selections in the new tables too.
        page.evaluate(f"""() => {{ const target = document.querySelector('#request');
            target.setAttribute('hx-get', '/requests/{entry}?part=live');
            target.setAttribute('hx-trigger', 'every 0.2s'); htmx.process(target); }}""")
        page.evaluate("""() => { const node = document.querySelector('#request-timeline td').firstChild;
            const range = document.createRange(); range.selectNodeContents(node);
            getSelection().removeAllRanges(); getSelection().addRange(range); }""")
        selected = page.evaluate('getSelection().toString()')
        page.wait_for_timeout(500)
        assert page.evaluate('getSelection().toString()') == selected
        page.evaluate('getSelection().removeAllRanges()')
        for width in (1440, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(tmp_path / f'request-diagnostics-{width}.png'), full_page=True)
        assert not errors
        browser.close()
