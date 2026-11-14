"""Task groups and access rules stay consistent across desktop and mobile menus."""

import pytest

from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.navigation import navigation

MODEL_LINKS = ['Models', 'Images', 'Audio', 'Cloud APIs', 'Roles', 'Dry fit', 'Files', 'Fingerprints', 'Hugging Face', 'Civitai']
MODEL_HEADINGS = {'/models':'Catalogue', '/roles':'Routing & capacity', '/files':'Files & provenance', '/hub':'Downloads'}


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
    assert group['headings'] == (MODEL_HEADINGS if 'manage-models' in can else {'/cloud-apis':'Catalogue'})


@pytest.mark.parametrize('path,active', [('/models/qwen', '/models'), ('/models/images', '/models/images'),
    ('/models/audio', '/models/audio'), ('/fingerprints', '/fingerprints'), ('/hub', '/hub'), ('/civitai', '/civitai')])
def test_model_and_download_routes_keep_their_category(path, active):
    nav = navigation(path, ['manage-models', 'manage-hosted'], True)
    assert nav['section'] == 'Models' and nav['active'] == active
    assert nav['headings'] == MODEL_HEADINGS


def test_desktop_and_mobile_render_the_same_menus(desk):
    from test_console_browser import browser_context, playwright
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        for path, section in [('/chat','Interact'),('/fingerprints','Models'),('/benchmarks/runs','Benchmark'),
                              ('/usage','Logs'),('/status','System'),('/settings','Settings'),('/docs','Docs'),
                              ('/account/appearance','My account')]:
            page.set_viewport_size({'width':1440, 'height':1000})
            page.goto('http://console.test' + path)
            sidebar = page.locator(f'#site-sidebar nav[aria-label="{section} pages"]')
            links = sidebar.locator(':scope > a').all_text_contents()
            headings = sidebar.locator(':scope > .section-label').all_text_contents()
            assert headings, section
            assert 'Other' not in headings, section
            playwright.expect(sidebar.locator('a[aria-current="page"]')).to_have_count(1)
            if section == 'Models':
                assert links == MODEL_LINKS
                assert sidebar.locator('.section-label').all_text_contents() == list(MODEL_HEADINGS.values())
                assert sidebar.locator('.section-label').last.evaluate('el=>el.nextElementSibling.getAttribute("href")') == '/hub'
            page.set_viewport_size({'width':390, 'height':844})
            page.locator('#mobile-menu-toggle').click()
            mobile = page.locator('#mobile-navigation .mobile-category').filter(has=page.locator('summary').filter(has_text=section))
            assert mobile.locator(':scope > a').all_text_contents() == links, section
            assert mobile.locator(':scope > .section-label').all_text_contents() == headings, section
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), section
            page.keyboard.press('Escape')
        browser.close()
