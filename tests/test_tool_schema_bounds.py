"""Typed tool arguments carry their schema's numeric range, string length and pattern, and say so."""
import json

from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in  # noqa: F401

SCHEMA = {'type': 'object', 'required': ['query'], 'properties': {
    'query': {'type': 'string', 'description': 'Words to find.', 'maxLength': 300, 'minLength': 2},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
    'share': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 1},
    'slug': {'type': 'string', 'pattern': '^[a-z-]+$'},
    'loose': {'type': 'string', 'pattern': '(?<unclosed'},  # unreadable here: left to the server
}}


def test_typed_tool_arguments_apply_and_describe_schema_bounds(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    calls = []

    def capture(route, path):
        if path == '/tools/message':
            message = json.loads(route.request.post_data)['message']
            if message['method'] == 'tools/call':
                calls.append(message['params'])
            result = {'protocolVersion': '2025-06-18'} if message['method'] == 'initialize' else (
                {'tools': [{'name': 'docs__search', 'description': 'Search', 'inputSchema': SCHEMA}]}
                if message['method'] == 'tools/list' else {'content': [{'type': 'text', 'text': 'found'}]})
            route.fulfill(status=200, content_type='application/json', headers={'mcp-session-id': 'mock'},
                          body=json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}))
            return True

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/tools')
        page.get_by_role('button', name='Connect', exact=True).click()
        playwright.expect(page.locator('#tools-state')).to_have_text('1 tools available to your account.')
        query, limit, share, slug = (page.locator('#tool-argument-' + name) for name in ('query', 'limit', 'share', 'slug'))
        assert (query.get_attribute('maxlength'), query.get_attribute('minlength')) == ('300', '2')
        assert (limit.get_attribute('min'), limit.get_attribute('max')) == ('1', '10')
        assert share.get_attribute('min') is None and share.get_attribute('max') is None  # exclusive: checked by script, not min/max
        hints = page.locator('#tools-arguments').inner_text()
        assert 'Words to find. · Allowed: at least 2 characters, at most 300 characters' in hints
        assert 'Allowed: at least 1, at most 10' in hints and 'Allowed: more than 0, less than 1' in hints
        assert 'matching ^[a-z-]+$' in hints and '(?<unclosed' not in hints  # an unreadable pattern is not claimed

        query.fill('docs')
        for field, value, said in ((limit, '11', 'Correct the limit argument'), (share, '1', 'Correct the share argument'),
                                   (slug, 'Not A Slug', 'Correct the slug argument')):
            field.fill(value)
            page.get_by_role('button', name='Call', exact=True).click()
            playwright.expect(page.locator('#tools-status')).to_contain_text(said)
            field.fill('')
        assert calls == []  # nothing out of bounds reached the server
        limit.fill('10'); share.fill('0.5'); slug.fill('a-slug')
        page.locator('#tool-argument-loose').fill('anything')
        page.get_by_role('button', name='Call', exact=True).click()
        playwright.expect(page.locator('#tools-answer')).to_have_text('found')
        assert calls[-1]['arguments'] == {'query': 'docs', 'limit': 10, 'share': 0.5, 'slug': 'a-slug', 'loose': 'anything'}
        assert not errors
        browser.close()
