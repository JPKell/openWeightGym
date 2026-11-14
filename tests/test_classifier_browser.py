"""Real workspace controls and result states in wide and narrow browsers."""

import ast
import json

import pytest

from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright


@pytest.mark.parametrize('width', [1440, 390])
def test_classifier_builder_comparison_drafts_exports_and_errors(desk, width):
    server, _, _ = desk
    sign_in(server, 'jordan')
    calls, failures = [], []
    def mock(route):
        action = route.request.url.rsplit('/', 1)[-1]
        if action == 'models':
            return route.fulfill(json={'models': [{'id': name, 'capabilities': {'classifier': 'kev', 'limits': {'choices': 255}}} for name in ('first', 'second')],
                                      'nodes': [{'name': 'compute', 'cards': [{'card': 'GPU0'}]}], 'manage': True, 'downloads': True})
        body = json.loads(route.request.post_data)
        calls.append(body)
        if failures:
            return route.fulfill(status=failures.pop(), json={'error': {'message': 'Classifier request was refused.'}})
        answers = {}
        for key, q in body['questions'].items():
            if q['type'] == 'noul':
                answers[key] = {'type': 'noul', 'noul': .8 if body['model'] == 'first' else .2}
            elif q['type'] == 'choice':
                keys = list(q['criteria'])
                answers[key] = {'type': 'choice', 'choice': keys[0], 'probabilities': dict(zip(keys, [.8] + [.2 / (len(keys)-1)]*(len(keys)-1))), 'confidence': .7}
            else:
                answers[key] = {'type': 'score', 'score': 1.2, 'legend': dict(enumerate(q['criteria'])),
                                'probabilities': {'0': .2, '1': .4, '2': .4}, 'confidence': .4}
        route.fulfill(json={'model': body['model'], 'answers': answers, 'usage': {'input_tokens': 125, 'output_tokens': 0}})
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        page.route('**/chat/classifier/api/*', mock)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        assert page.goto('http://console.test/chat/classifier').status == 200
        playwright.expect(page.locator('#classifier-model option')).to_have_count(2)
        page.locator('#classifier-compare').select_option('second')
        page.locator('[data-run]').click()
        playwright.expect(page.locator('[data-result]')).to_have_count(4)
        playwright.expect(page.locator('[data-disagreement]')).to_have_count(2)
        assert len(calls) == 2
        page.locator('#classifier-state').fill('Private draft for the first model')
        page.locator('#classifier-model').select_option('second')
        page.locator('#classifier-state').fill('Draft on second')
        page.locator('#classifier-model').select_option('first')
        playwright.expect(page.locator('#classifier-state')).to_have_value('Private draft for the first model')
        page.locator('#classifier-starter').select_option('urgency')
        page.locator('[data-load-starter]').click()
        playwright.expect(page.locator('[data-criterion]')).to_have_count(5)
        page.locator('#classifier-compare').select_option([])
        page.locator('[data-run]').click()
        playwright.expect(page.locator('[data-results]')).to_contain_text('Expected level')
        page.get_by_text('Developer drawer', exact=True).click()
        for kind in ('python', 'openai'):
            page.locator('#classifier-code').select_option(kind)
            ast.parse(page.locator('#classifier-developer').text_content())
        page.get_by_text('History in this tab', exact=True).click()
        with page.expect_download() as download:
            page.locator('[data-export=json]').click()
        assert download.value.suggested_filename.endswith('.json')
        failures.append(501)
        page.locator('[data-run]').click()
        playwright.expect(page.locator('[data-classifier-error]')).to_contain_text('501')
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert errors == []
        browser.close()


def test_classifier_empty_unavailable_and_disabled(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/chat/classifier')
        playwright.expect(page.locator('[data-classifier-empty]')).to_be_visible()
        playwright.expect(page.locator('[data-run]')).to_be_disabled()
        page.route('**/chat/classifier/api/models', lambda route: route.fulfill(status=503, json={'error': {'message': 'Service unavailable'}}))
        page.reload()
        playwright.expect(page.locator('[data-classifier-error]')).to_contain_text('503')
        browser.close()
    saved = server.post('/settings/weightroom', data={'value-allow_classifiers': 'false'})
    assert saved.status_code == 200
    assert server.get('/chat/classifier').status_code == 403
