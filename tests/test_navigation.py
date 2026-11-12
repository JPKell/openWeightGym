"""Models navigation shares its order, grouping and access rules across both menus."""

import pytest

from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.navigation import navigation

MODEL_LINKS = ['Models', 'Images', 'Audio', 'Roles', 'Cloud APIs', 'Files', 'Dry fit', 'Fingerprints', 'Hugging Face', 'Civitai']


@pytest.mark.parametrize('can', [[], ['manage-hosted'], ['manage-models'], ['manage-models', 'manage-hosted']])
def test_model_navigation_order_headings_and_permissions(can):
    nav = navigation('/models', can, True)
    group = next((group for group in nav['groups'] if group['name'] == 'Models'), None)
    if not can:
        assert group is None
        return
    expected = MODEL_LINKS if 'manage-models' in can else ['Cloud APIs']
    if 'manage-hosted' not in can:
        expected = [label for label in expected if label != 'Cloud APIs']
    assert [label for label, _ in group['links']] == expected
    assert group['headings'] == ({'/hub':'Downloads'} if 'manage-models' in can else {})


@pytest.mark.parametrize('path,active', [('/models/qwen', '/models'), ('/models/images', '/models/images'),
    ('/models/audio', '/models/audio'), ('/fingerprints', '/fingerprints'), ('/hub', '/hub'), ('/civitai', '/civitai')])
def test_model_and_download_routes_keep_their_category(path, active):
    nav = navigation(path, ['manage-models', 'manage-hosted'], True)
    assert nav['section'] == 'Models' and nav['active'] == active
    assert nav['headings'] == {'/hub':'Downloads'}


def test_desktop_and_mobile_render_the_same_model_menu(desk):
    from test_console_browser import browser_context, playwright
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/fingerprints')
        sidebar = page.locator('#site-sidebar nav[aria-label="Models pages"]')
        assert sidebar.locator(':scope > a').all_text_contents() == MODEL_LINKS
        playwright.expect(sidebar.locator('.section-label')).to_have_text('Downloads')
        assert sidebar.evaluate('el => el.querySelector(".section-label").nextElementSibling.getAttribute("href")') == '/hub'
        playwright.expect(sidebar.locator('a[aria-current="page"]')).to_have_text('Fingerprints')
        page.set_viewport_size({'width':390, 'height':844})
        page.locator('#mobile-menu-toggle').click()
        mobile = page.locator('#mobile-navigation .mobile-category').filter(has=page.get_by_role('link', name='Models', exact=True))
        assert mobile.locator(':scope > a').all_text_contents() == MODEL_LINKS
        playwright.expect(mobile.locator('.section-label')).to_have_text('Downloads')
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        browser.close()
