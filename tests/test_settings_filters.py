"""Real settings filters keep native forms, scoped state and refresh behavior."""
import pytest
from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in


@pytest.mark.parametrize('width', [1440, 390])
def test_search_changed_only_empty_state_and_saved_filter_survive_navigation(desk, width):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width, 'height':1000})
        page.goto('http://console.test/settings')
        search = page.get_by_label('Find a setting', exact=True)
        playwright.expect(search).to_be_enabled()
        search.fill('mock input')
        rate = page.locator('[name="value-mock_input_rate"]')
        playwright.expect(rate).to_be_visible()
        playwright.expect(page.locator('[name="value-timezone"]')).to_be_hidden()
        page.get_by_label('Changed only', exact=True).check()
        playwright.expect(page.locator('[data-settings-no-match]')).to_be_visible()
        page.get_by_role('button', name='Clear filters', exact=True).click()
        rate.fill('3.5')
        search.fill('mock input')
        page.get_by_label('Changed only', exact=True).check()
        playwright.expect(rate).to_be_visible()  # Unsent fields count as changed.
        page.get_by_role('button', name='Save display and costs', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Saved.')
        playwright.expect(search).to_have_value('mock input')
        playwright.expect(page.get_by_label('Changed only', exact=True)).to_be_checked()
        playwright.expect(rate).to_have_value('3.5')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.reload()
        playwright.expect(search).to_have_value('mock input')
        playwright.expect(page.get_by_label('Changed only', exact=True)).to_be_checked()
        playwright.expect(rate).to_be_visible()
        assert page.locator('[aria-label="Settings sections"] a').count() >= 1
        browser.close()


def test_filters_reapply_to_poll_replacements_without_touching_the_input(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/settings')
        page.get_by_label('Find a setting', exact=True).fill('mock input')
        page.evaluate('''() => {
            const row = document.querySelector('[name="value-mock_output_rate"]').closest('.settings-row');
            row.replaceWith(row.cloneNode(true));
        }''')
        playwright.expect(page.locator('[name="value-mock_output_rate"]')).to_be_hidden()
        playwright.expect(page.locator('[name="value-mock_input_rate"]')).to_be_visible()
        playwright.expect(page.get_by_label('Find a setting', exact=True)).to_have_value('mock input')
        browser.close()


def test_audio_defaults_reuse_rows_and_reset_through_the_original_form(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    response = server.post('/system/audio', data={'value-audio_lyrics_model':'small'})
    assert response.status_code == 200
    assert 'id="value-audio_lyrics_model"' in response.text
    assert 'name="value-audio_lyrics_model"' in response.text
    assert 'data-setting-changed="true"' in response.text
    reset = server.post('/system/audio', data={'reset':'audio_lyrics_model'})
    assert reset.status_code == 200 and 'Applied immediately.' in reset.text
    assert 'value="chat" selected' in reset.text


def test_no_javascript_keeps_the_native_settings_forms(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(java_script_enabled=False)
        from front_helpers import serve_browser_request
        page.route('http://console.test/**', lambda route: serve_browser_request(server, 'http://console.test', route))
        page.goto('http://console.test/settings')
        playwright.expect(page.get_by_label('Find a setting', exact=True)).to_be_disabled()
        playwright.expect(page.locator('form[action="/settings/weightroom"] button').first).to_be_visible()
        assert 'value-mock_input_rate' in page.content()
        browser.close()
