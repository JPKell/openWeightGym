"""Delayed sections initialize their controls, expose errors and preserve navigation."""

from test_console_browser import browser_context, playwright
from test_deferred_pages import deferred_console  # noqa: F401


def test_usage_appears_after_navigation_and_retries_a_failed_report(deferred_console):
    server, _, state, _, _ = deferred_console
    pending = {}

    def capture(route, path):
        """Hold the report response while the already loaded page remains usable."""
        if path == '/usage?part=report':
            pending['report'] = route
            return True
        return False

    def finish():
        """Deliver the real success or error fragment through the intercepted browser request."""
        response = server.get('/usage?part=report', headers={'HX-Request': 'true'})
        pending.pop('report').fulfill(status=response.status_code, content_type='text/html', body=response.content)

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/usage', wait_until='load')
        playwright.expect(page.get_by_role('heading', name='Usage', exact=True)).to_be_visible()
        playwright.expect(page.locator('#usage-report')).to_contain_text('Loading usage data')
        playwright.expect(page.locator('.category-links a[href="/models"]')).to_be_visible()
        assert page.locator('#usage-chart').count() == 0
        state['fail'] = True
        finish()
        playwright.expect(page.locator('#usage-report')).to_contain_text('Usage is temporarily unavailable.')
        state['fail'] = False
        page.get_by_role('button', name='Retry loading', exact=True).click()
        finish()
        playwright.expect(page.locator('#usage-chart svg')).to_be_visible()
        page.get_by_role('button', name='By model', exact=True).click()
        playwright.expect(page.locator('#usage-chart-heading')).to_contain_text('by model')
        page.set_viewport_size({'width': 390, 'height': 844})
        # The chart redraws from a ResizeObserver after the next layout, so the page fits once that has run.
        page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()


def test_model_files_load_after_the_model_list_into_the_open_add_model_card(deferred_console):
    server, calls, _, _, _ = deferred_console
    pending = {}

    def capture(route, path):
        """Keep the expensive file chooser pending while the model list is visible."""
        if path == '/models?part=editor':
            pending['editor'] = route
            return True
        return False

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        # Add a model is always open; its editor is a fragment of its own, so the list does not wait for it.
        with page.expect_request('**/models?part=editor'):
            page.goto('http://console.test/models', wait_until='load')
        playwright.expect(page.locator('#models-table')).to_be_visible()
        assert '/turnstile/models/files' not in calls
        assert page.locator('#add-model').evaluate('el => el.tagName') == 'SECTION'
        playwright.expect(page.locator('#model-editor')).to_contain_text('Loading node engines and model files')
        playwright.expect(page.locator('#add-model .card-header [data-model-node]')).to_be_checked()
        page.locator('#model-nodes .form-selectgroup-label').click()
        response = server.get('/models?part=editor', headers={'HX-Request': 'true'})
        pending.pop('editor').fulfill(status=response.status_code, content_type='text/html', body=response.content)
        picker = page.locator('[data-model-file]')
        playwright.expect(picker).to_be_visible()
        playwright.expect(picker).to_have_attribute('data-initialized', 'true')
        playwright.expect(page.locator('[data-model-node]')).not_to_be_checked()
        playwright.expect(page.locator('[data-file-warning]')).to_have_text('Choose at least one compute node.')
        playwright.expect(page.locator('[data-file-submit]')).to_be_disabled()
        assert calls.count('/turnstile/models/files') == 1
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
