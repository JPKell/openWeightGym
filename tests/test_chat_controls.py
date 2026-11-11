"""Chat rail actions and shared prompt controls without using a model or GPU."""

import shutil
from pathlib import Path
from urllib.parse import parse_qs

from loadcoach.models import Models
from plugin_helpers import SOURCE
from weightroom.chat_store import Conversations
from test_weightroom import desk, sign_in  # noqa: F401


def history(desk, tmp_path):
    server, _, users = desk
    registry = server.app.state.plugins
    shutil.copytree(SOURCE / 'hermes', registry.root / 'hermes', ignore=shutil.ignore_patterns('__pycache__', 'settings.json', '.settings.lock'))
    registry.save_instance('hermes', 'offline', 'http://127.0.0.1:1', {}, enabled=True)
    registry.enable('hermes', True)
    registry.inventory('hermes', 'offline', [{'id': 'gandalf'}])
    registry.model('hermes', 'offline', 'gandalf', 'hermes:gandalf', True)
    users.change('jordan', own={'add': ['front-voice']})
    models = Models(str(tmp_path / 'loadcoach.db'))
    for name in ('small', 'second'):
        models.put(name, {'flags': {'model': str(tmp_path / (name + '.gguf'))}})
    store = Conversations(str(tmp_path / 'data' / 'chat.db'))
    owner = users.by_name('jordan').identity
    numbers = []
    for index in range(24):
        number = store.start(owner, f'Conversation {index:02}', system='Be clear.', sampling={'temperature': .5})
        store.say(number, 'user', 'An earlier question')
        store.say(number, 'assistant', '\n\n'.join(['A long paragraph for scrolling. ' * 8] * 12), model='small', finish='stop')
        numbers.append(number)
    return store, numbers


def lined_up(row, *controls):
    """The controls sit left to right on one line, the opened options reach both of its ends, and
    Reasoning comes before Temporary on one line. Measured at once, so a scroll cannot skew it."""
    found = row.evaluate("""(row, controls) => {
      const box = selector => { const r = row.querySelector(selector).getBoundingClientRect(); return [r.left, r.right, (r.top + r.bottom) / 2]; };
      return {controls: controls.map(box), system: box('[name=system]'), temperature: box('[name=temperature]'), min_p: box('[name=min_p]'),
              thinking: box('[name=thinking]'), temporary: box('[name=temporary]')};
    }""", list(controls))
    boxes = found['controls']
    for before, after in zip(boxes, boxes[1:]):
        assert before[1] < after[0] and abs(before[2] - after[2]) < 2, found
    start, end = boxes[0][0], boxes[-1][1]
    assert abs(found['system'][0] - start) < 1 and abs(found['system'][1] - end) < 1, found
    assert abs(found['temperature'][0] - start) < 1 and abs(found['min_p'][1] - end) < 1, found
    assert found['thinking'][1] < found['temporary'][0] and abs(found['thinking'][2] - found['temporary'][2]) < 1, found


def test_chat_option_save_and_sidebar_actions_preserve_owner_checks(desk, tmp_path):
    server, _, _ = desk
    store, numbers = history(desk, tmp_path)
    ref = numbers[-1]
    owner = desk[2].by_name('jordan').identity
    sign_in(server, 'jordan')
    page = server.get(f'/chat/{ref}')
    assert page.status_code == 200 and 'chat-side"' not in page.text and 'chat-settings' not in page.text
    assert 'Plugin conversation' in page.text and 'data-plugin-conversation' in page.text
    assert 'd-none" data-plugin-conversation data-plugin-active="false" hidden' in page.text
    assert page.text.index('>Conversations</a>') < page.text.index('>Voice</a>') < page.text.index('aria-label="Your conversations"')
    for path in ('/collections', '/workflows', '/voice'):
        section = server.get(path)
        assert 'aria-label="Your conversations"' not in section.text and 'Find a conversation' not in section.text
    before = store.get(ref, owner)['messages']
    saved = server.post(f'/chat/{ref}', data={'do': 'options', 'system': 'Use short sentences.', 'temperature': '2', 'thinking': 'on'})
    assert saved.status_code == 200 and saved.json() == {'saved': True}
    assert store.get(ref, owner)['sampling'] == {'temperature': 2}
    assert store.get(ref, owner)['messages'] == before
    invalid = server.post(f'/chat/{ref}', data={'do': 'options', 'temperature': '2.01'})
    assert invalid.status_code == 400 and '0 to 2' in invalid.json()['error']['message']
    assert store.get(ref, owner)['system'] == 'Use short sentences.'
    renamed = server.post(f'/chat/{ref}', data={'do': 'rename', 'title': 'New title'}, headers={'accept': 'application/json'})
    assert renamed.status_code == 200 and renamed.json()['title'] == 'New title'
    sign_in(server, 'pat')
    for values in [{'do': 'options', 'system': 'tampered'}, {'do': 'rename', 'title': 'tampered'}, {'do': 'delete'}]:
        result = server.post(f'/chat/{ref}', data=values, headers={'accept': 'application/json'})
        assert result.status_code == 404
    assert store.get(ref, owner)['title'] == 'New title'
    sign_in(server, 'jordan')
    deleted = server.post(f'/chat/{ref}', data={'do': 'delete'}, headers={'accept': 'application/json'})
    assert deleted.status_code == 200 and store.get(ref, owner) is None
    assert store.get(numbers[0], owner) is not None


def test_chat_sidebar_options_and_scroll_in_chromium(desk, tmp_path):
    from playwright.sync_api import sync_playwright, expect
    server, _, _ = desk
    store, numbers = history(desk, tmp_path)
    ref = numbers[-1]
    owner = desk[2].by_name('jordan').identity
    sign_in(server, 'jordan')
    submitted = []
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--disable-gpu"])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page.add_init_script("""window.eventSources = [];
        window.EventSource = class {
          constructor() { window.eventSources.push(this); } addEventListener() {} close() {}
        };""")
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        def serve(route):
            request = route.request
            path = request.url.replace('http://chat.test', '')
            values = parse_qs(request.post_data or '', keep_blank_values=True)
            if request.method == 'POST' and values.get('do') == ['again']:
                submitted.append(values)
                response = server.get(f'/chat/{ref}')  # inspect the form without generating an answer
            else:
                headers = {name: request.headers[name] for name in ('accept', 'content-type', 'hx-request') if name in request.headers}
                response = server.request(request.method, path, content=request.post_data_buffer, headers=headers)
            route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'text/html'), body=response.content)
        context.route('http://chat.test/**', serve)
        page.goto(f'http://chat.test/chat/{ref}')
        rail = page.locator('#site-sidebar')
        scroll = rail.locator('.chat-conversations-scroll')
        assert scroll.evaluate('el => el.scrollHeight > el.clientHeight')
        fraction = rail.locator('.chat-sidebar').bounding_box()['height'] / rail.bounding_box()['height']
        assert .35 < fraction < .42
        search = page.get_by_role('searchbox', name='Find a conversation')
        search.fill('save')  # hidden rename buttons must not become search matches
        expect(page.locator('[data-conversation]:visible')).to_have_count(0)
        search.fill('Conversation 00')
        expect(page.locator('[data-conversation]:visible')).to_have_count(1)
        link = page.locator(f'[data-conversation="{numbers[0]}"] [data-chat-title]')
        link.dblclick()
        rename = page.locator(f'[data-conversation="{numbers[0]}"] [data-chat-rename]')
        expect(rename).to_be_visible()
        rename.locator('[name=title]').fill('Renamed from the rail')
        rename.get_by_role('button', name='Save conversation name').click()
        expect(link).to_have_text('Renamed from the rail')
        search.fill('')
        page.get_by_role('button', name='Delete Renamed from the rail', exact=True).click()
        page.locator('#danger-confirmation-check').check()
        page.get_by_role('button', name='Continue', exact=True).click()
        expect(page.locator(f'[data-conversation="{numbers[0]}"]')).to_have_count(0)
        assert page.url.endswith(f'/chat/{ref}')
        main = page.locator('.composer')
        main.locator('[data-composer-options] > summary').click()
        lined_up(main.locator('.composer-actions'), '.chat-primary-model', 'summary', 'button[value=say]')
        model_select = main.locator('[name=model]')
        picker = main.get_by_role('button', name='Model', exact=True)
        picker.click()
        menu = page.get_by_role('listbox').filter(visible=True)
        expect(menu).to_be_visible()
        page.evaluate("""() => {
          window.eventSources[0].onmessage({data: JSON.stringify('<span data-test-streamed>changed</span>')});
        }""")
        expect(page.locator('#strip [data-test-streamed]')).to_be_visible()
        expect(menu).to_be_visible()
        menu.get_by_role('option', name='second', exact=True).click()
        expect(menu).not_to_be_visible()
        expect(model_select).to_have_value('second')
        plugin_conversation = main.locator('[data-plugin-conversation]')
        expect(plugin_conversation).not_to_be_visible()
        picker.click()
        page.get_by_role('listbox').filter(visible=True).get_by_role('option', name='hermes:gandalf · Plugin', exact=True).click()
        expect(plugin_conversation).to_be_visible()
        expect(plugin_conversation.locator('[name=session_mode]')).to_be_enabled()
        picker.click()
        page.get_by_role('listbox').filter(visible=True).get_by_role('option', name='small', exact=True).click()
        expect(plugin_conversation).not_to_be_visible()
        main.locator('[name=system]').fill('Persisted without sending a message.')
        main.locator('[name=temperature]').fill('1.25')
        main.locator('[name=thinking]').uncheck()
        expect(main.locator('[data-options-status]')).to_have_text('Saved.')
        assert store.get(ref, owner)['system'] == 'Persisted without sending a message.'
        main.locator('[data-composer-options] > summary').click()
        again = page.locator('.retry-options')
        expect(again.locator('[name=system]')).not_to_be_visible()
        again.locator('[data-composer-options] > summary').click()
        lined_up(again, '.chat-primary-model', 'button[value=again]', 'summary')
        expect(again.locator('[name=system]')).to_have_value('Persisted without sending a message.')
        expect(again.locator('[name=thinking]')).not_to_be_checked()
        again.locator('[name=system]').fill('Changed beside Ask again.')
        again.get_by_role('button', name='Model', exact=True).click()
        page.get_by_role('listbox').filter(visible=True).get_by_role('option', name='second', exact=True).click()
        expect(main.locator('[name=system]')).to_have_value('Changed beside Ask again.')
        expect(main.locator('[name=model]')).to_have_value('second')
        # Telemetry swaps restore saved details state globally. These two panels share
        # settings but must keep independent open/closed state and the focused draft.
        field = again.locator('[name=system]')
        field.focus()
        field.evaluate('el => el.setSelectionRange(0, 7)')
        page.evaluate("""() => {
          const property = Object.getOwnPropertyDescriptor(HTMLDetailsElement.prototype, 'open');
          window.detailsOpenWrites = 0;
          Object.defineProperty(HTMLDetailsElement.prototype, 'open', {
            configurable: property.configurable,
            get: property.get,
            set(value) { window.detailsOpenWrites += 1; property.set.call(this, value); }
          });
        }""")
        for _ in range(2):
            page.evaluate("async () => { await htmx.ajax('GET', '/strip', {target: '#strip', swap: 'innerHTML'}); }")
            expect(again.locator('[data-composer-options]')).to_have_attribute('open', '')
            expect(main.locator('[data-composer-options]')).not_to_have_attribute('open', '')
            expect(field).to_be_focused()
            expect(field).to_have_value('Changed beside Ask again.')
            expect(again.locator('[name=model]')).to_have_value('second')
            expect(again.locator('[name=thinking]')).not_to_be_checked()
            assert field.evaluate('el => [el.selectionStart, el.selectionEnd]') == [0, 7]
        assert page.evaluate('window.detailsOpenWrites') == 0
        with page.expect_navigation():
            again.get_by_role('button', name='Ask again', exact=True).click()
        expect(page.locator('.retry-options [name=system]')).to_have_value('Changed beside Ask again.')
        assert submitted[-1]['system'] == ['Changed beside Ask again.'] and submitted[-1]['temperature'] == ['1.25']
        page.reload()
        page.locator('.composer [data-composer-options] > summary').click()
        expect(page.locator('.composer [name=system]')).to_have_value('Changed beside Ask again.')
        expect(page.locator('.composer [name=model]')).to_have_value('second')
        assert store.get(ref, owner)['system'] == 'Changed beside Ask again.'
        # A small scroll cannot hide the bubble; sustained upward movement can.
        page.locator('.composer [data-composer-options] > summary').click()
        page.evaluate('scrollTo(0, document.body.scrollHeight)')
        page.wait_for_timeout(100)
        page.evaluate('scrollBy(0, -40)')
        page.wait_for_timeout(100)
        expect(page.locator('.composer')).not_to_have_class('composer composer-hidden')
        page.evaluate('scrollBy(0, -220)')
        expect(page.locator('.composer')).to_have_class('composer composer-hidden')
        page.evaluate('scrollBy(0, 40)')
        page.wait_for_timeout(100)
        expect(page.locator('.composer')).to_have_class('composer composer-hidden')
        page.evaluate('scrollBy(0, 220)')
        expect(page.locator('.composer')).not_to_have_class('composer composer-hidden')
        images = Path('data/chat-controls-previews'); images.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(images / 'desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(images / 'mobile-before-check.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.get_by_role('button', name='Menu', exact=True).click()
        expect(page.locator('#mobile-navigation .chat-conversations-scroll')).to_be_visible()
        page.screenshot(path=str(images / 'mobile-rail.png'))
        page.get_by_role('link', name='Voice', exact=True).click()
        expect(page.get_by_role('button', name='Start conversation', exact=True)).to_be_visible()
        page.get_by_role('button', name='Menu', exact=True).click()
        expect(page.get_by_role('searchbox', name='Find a conversation')).to_have_count(0)
        page.goto('http://chat.test/chat')
        page.locator('[data-composer-options] > summary').click()
        expect(page.get_by_role('button', name='Choose', exact=True)).to_be_visible()
        lined_up(page.locator('.composer-actions'), '.chat-primary-model', 'summary', 'button:not([type=button])')
        assert page.locator('.option-fields').evaluate('el => el.scrollWidth <= el.clientWidth'), page.locator('.option-fields').evaluate(
            "el => [...el.querySelectorAll('*')].filter(node => node.getBoundingClientRect().right > el.getBoundingClientRect().right + 1).map(node => node.className)")
        page.locator('[data-files]').set_input_files({'name': 'notes.txt', 'mimeType': 'text/plain', 'buffer': b'Private attachment'})
        expect(page.locator('[data-file-list]')).to_contain_text('notes.txt')
        assert page.get_by_label('Temporary', exact=True).is_enabled()
        assert page.get_by_label('Reasoning', exact=True).is_enabled()
        page.screenshot(path=str(images / 'mobile-new.png'))
        assert not errors, errors
        browser.close()
