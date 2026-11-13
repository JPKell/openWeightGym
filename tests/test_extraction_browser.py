"""Live desk component interactions over a real extraction store and fixture inference transport."""

import re

import pytest

from test_extraction import extraction, raster  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright

expect = playwright.expect


@pytest.fixture
def workspace(desk, extraction):
    server, _, _ = desk
    api, service, users, keys, calls = extraction
    sign_in(server, 'jordan')
    def capture(route, path):
        if not path.startswith('/chat/extraction/api/'):
            return False
        request = route.request
        target = path.replace('/chat/extraction/api/', '/owg/', 1)
        for browser, backend in [('models', 'extraction-models'), ('assets', 'extraction-assets'), ('jobs', 'extractions'), ('presets', 'extraction-presets')]:
            if target == '/owg/' + browser or target.startswith('/owg/' + browser + '/'):
                target = target.replace('/owg/' + browser, '/owg/' + backend, 1)
                break
        response = api.request(request.method, target, content=request.post_data_buffer,
                               headers={key: value for key, value in request.headers.items() if key in ('content-type', 'idempotency-key')})
        route.fulfill(status=response.status_code, body=response.content,
                      headers={key: value for key, value in response.headers.items() if key in ('content-type', 'content-disposition', 'cache-control')})
        return True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        page.set_viewport_size({'width': 1600, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/chat/extraction')
        expect(page.locator('#model option[value=vision]')).to_have_count(1)
        yield page, api, calls
        assert not errors
        browser.close()


def upload(page, names):
    page.locator('#image-file').set_input_files([{'name': name, 'mimeType': 'image/png', 'buffer': raster()} for name in names])
    expect(page.locator('#source-name')).to_have_text(names[0])


def test_batch_has_independent_settings_and_waits_for_all_results(workspace):
    page, api, calls = workspace
    expect(page.get_by_text('Turn images into descriptions, readable text and useful data.', exact=True)).to_have_count(0)
    expect(page.locator('#toggle-batch')).to_have_text('Batch inbox')
    upload(page, ['receipt.png', 'table.png'])
    expect(page.locator('#toggle-batch')).to_have_attribute('aria-expanded', 'true')
    expect(page.locator('#toggle-batch')).to_have_text('Hide batch')
    expect(page.locator('#inbox-panel')).to_be_visible()
    page.locator('#toggle-batch').click()
    expect(page.locator('#toggle-batch')).to_have_text('Batch inbox')
    expect(page.locator('#toggle-batch')).to_have_attribute('aria-expanded', 'false')
    expect(page.locator('#inbox-panel')).to_be_hidden()
    page.locator('#toggle-batch').click()
    expect(page.locator('#toggle-batch')).to_have_text('Hide batch')
    expect(page.locator('#inbox-panel')).to_be_visible()
    page.locator('#task').select_option('receipt'); page.locator('#format').select_option('json')
    page.locator('#instructions').fill('Keep all line items.')
    page.get_by_role('button', name=re.compile('table.png')).click()
    expect(page.locator('#instructions')).to_have_value('')
    page.locator('#task').select_option('table'); page.locator('#format').select_option('csv')
    page.locator('#run-batch').click()
    expect(page.locator('#action-status')).to_contain_text('Extraction finished', timeout=10000)
    jobs = api.get('/owg/extractions').json()['data']
    assert {job['task'] for job in jobs} == {'receipt', 'table'}, jobs
    assert len(calls) == 2
    page.get_by_role('button', name=re.compile('receipt.png')).click()
    expect(page.locator('#instructions')).to_have_value('Keep all line items.')
    page.locator('#field-tax').fill('2.12')
    page.locator('#save-review').click()
    expect(page.locator('#result-state')).to_have_text('needs review')
    page.locator('#review-toggle').click()
    expect(page.locator('#result-state')).to_have_text('Reviewed')
    page.locator('#task').select_option('describe')
    expect(page.locator('#result-state')).to_have_text('Settings changed')


def test_schema_errors_stay_visible_and_custom_settings_do_not_appear_stale(workspace):
    page, _, _ = workspace
    upload(page, ['receipt.png'])
    page.locator('#task').select_option('custom')
    page.locator('#schema').fill('{bad')
    page.locator('#run').click()
    expect(page.locator('#error-notice')).to_be_visible()
    expect(page.locator('#error-message')).to_contain_text('valid JSON')
    page.locator('#schema').fill('{"type":"object","properties":{"merchant":{"type":["string","null"]}},"required":["merchant"],"additionalProperties":false}')
    page.locator('#run').click()
    expect(page.locator('#result-state')).to_have_text('completed')
    for width in (1600, 1024, 760, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width


def test_invalid_json_draft_cannot_be_silently_marked_reviewed(workspace):
    page, api, _ = workspace
    upload(page, ['receipt.png'])
    page.locator('#task').select_option('receipt'); page.locator('#run').click()
    expect(page.locator('#result-state')).to_have_text('completed')
    job = api.get('/owg/extractions').json()['data'][0]
    page.locator('[data-result-view="raw"]').click()
    page.locator('#raw-editor').fill('null')
    assert not page.locator('#raw-editor').evaluate('element => element.checkValidity()')
    page.locator('[data-result-view="preview"]').click()
    page.locator('#review-toggle').click()
    expect(page.locator('#error-message')).to_contain_text('JSON object')
    assert api.get('/owg/extractions/' + job['id']).json()['revision'] == job['revision']
    page.locator('#field-tax').fill('2.12'); page.locator('#save-review').click()
    expect(page.locator('#result-state')).to_have_text('needs review')
    corrected = api.get('/owg/extractions/' + job['id']).json()
    assert corrected['data']['tax'] == '2.12' and corrected['revision'] == job['revision'] + 1


def test_shared_confirmation_and_no_model_validation(workspace):
    page, api, _ = workspace
    upload(page, ['receipt.png'])
    page.locator('#delete-image').click()
    expect(page.locator('#source-name')).to_have_text('receipt.png')
    page.locator('#delete-image').click()
    expect(page.locator('#source-name')).to_have_text('No image selected')
    assert api.get('/owg/extractions').json()['data'] == []


def test_shared_header_fits_long_account_names_at_320px(workspace):
    page, _, _ = workspace
    page.set_viewport_size({'width': 320, 'height': 1000})
    page.locator('.user-menu > summary').evaluate('element => element.textContent = "operator-with-a-long-name"')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
