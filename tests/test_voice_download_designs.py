"""Download-page proposals share real setup content and work without backend requests."""

import runpy
from pathlib import Path

import pytest

from test_weightroom import desk, sign_in  # noqa: F401

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-voice-download-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render('https://console.example.test'))
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url)
                if request.url.startswith(('http:', 'https:', 'ws:', 'wss:')) else None)
        page.goto(output.as_uri())
        yield page
        browser.close()
        assert not errors
        assert not requests, 'Download proposals must not contact the server automatically.'


@pytest.mark.parametrize('design', ('hub', 'guide', 'reference'))
def test_layouts_keep_download_setup_and_real_links_in_both_appearances(preview, design):
    page = preview
    page.locator(f'[data-download-design={design}]').click()
    panel = page.locator(f'[data-download-layout={design}]')
    expect(panel).to_be_visible()
    download = panel.get_by_role('link', name='Download Voice app', exact=True)
    expect(download).to_have_attribute('href', 'https://console.example.test/static/LoadCoach-Voice.zip')
    assert download.get_attribute('download') is not None
    expect(panel.locator('a[href="https://console.example.test/account"]')).to_be_visible()
    assert panel.locator('a[href="https://console.example.test/voice"]').count() == 1
    assert 'Extract the entire ZIP' in panel.inner_text()
    assert page.locator('input[type=password], [data-design=desk], #vd-transcript').count() == 0
    if design == 'hub':
        assert '<span' not in panel.inner_text()
        assert panel.locator('.vdl-os-mark').count() == 3
    for width in (1600, 1100, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        for _ in range(2):
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            expect(download).to_be_visible()
            expect(page.get_by_role('heading', level=1)).to_have_text('Voice desktop app')
            page.locator('#vdl-theme').click()


def test_guided_setup_switches_platforms_and_mobile_console_navigation(preview):
    page = preview
    page.locator('[data-download-design=guide]').click()
    for platform, launcher in [('macos', 'Voice.command'), ('linux', 'sh Voice.command'), ('windows', 'Voice.cmd')]:
        page.locator(f'[data-platform={platform}]').click()
        panel = page.locator(f'[data-platform-panel={platform}]')
        expect(panel).to_be_visible()
        expect(panel).to_contain_text(launcher)
        assert page.locator('[data-platform-panel]:visible').count() == 1
    page.locator('[data-download-design=reference]').click()
    page.locator('[data-download-design=guide]').click()
    expect(page.locator('[data-platform=windows]')).to_have_attribute('aria-pressed', 'true')
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#mobile-menu-toggle').click()
    expect(page.get_by_role('dialog', name='Navigation', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('#mobile-menu-toggle')).to_be_focused()


def test_preview_rejects_credential_bearing_console_urls():
    render = runpy.run_path(str(ROOT / 'scripts/preview-voice-download-designs.py'))['render']
    for base in ('file:///tmp', 'https://key@console.example.test', 'javascript:alert(1)'):
        with pytest.raises(ValueError):
            render(base)


def test_live_download_retains_permissions_content_and_routes(tmp_path):
    from test_voice_admin import TestClient, sign_in, voice_panel

    app, _, _, _, users, _ = voice_panel(tmp_path)
    with TestClient(app) as client:
        sign_in(client, 'pat')
        assert client.get('/voice/desktop').status_code == 403
        users.change('pat', own={'add': ['front-voice']})
        response = client.get('/voice/desktop')
        assert response.status_code == 200
        for text in ('Windows', 'macOS', 'Linux', 'Python 3.13', 'Tcl/Tk', 'PortAudio',
                     'Extract the entire ZIP', 'provided by your administrator',
                     'the transcript is sent to Hermes'):
            assert text in response.text
        for path in ('/static/LoadCoach-Voice.zip', '/voice', '/account'):
            assert f'href="{path}"' in response.text
        assert 'data-voice-download' in response.text
        assert '/static/voice-download.css?v=1' in response.text
        assert '/static/voice-download.js?v=1' in response.text
        assert 'data-download-design=' not in response.text


@pytest.mark.parametrize('width', (1440, 390))
def test_live_guided_setup_platforms_copy_theme_and_replacement(desk, width):
    from test_console_browser import browser_context
    from test_themes import change_theme
    from test_ui_consistency import MEASURE

    server, _, users = desk
    users.change('jordan', own={'add': ['front-voice']})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto('http://console.test/voice/desktop')
        expect(page.locator('[data-platform-panel=windows]')).to_be_visible()
        expect(page.get_by_role('link', name='Download Voice app', exact=True)).to_have_attribute('href', '/static/LoadCoach-Voice.zip')
        for platform, launcher in [('macos', 'Voice.command'), ('linux', 'sh Voice.command')]:
            page.locator(f'[data-platform={platform}]').focus()
            page.keyboard.press('Enter')
            panel = page.locator(f'[data-platform-panel={platform}]')
            expect(panel).to_be_visible()
            expect(panel).to_contain_text(launcher)
            assert page.locator('[data-platform-panel]:visible').count() == 1
        page.get_by_text('Ubuntu dependency command', exact=True).click()
        page.evaluate("""() => { document.execCommand = command => {
            window.copiedCommand = command;
            window.copiedText = document.querySelector('textarea').value;
            return true;
        }; }""")
        page.get_by_role('button', name='Copy this Shell block').click()
        assert page.evaluate('window.copiedCommand') == 'copy'
        assert page.evaluate('window.copiedText') == 'sudo apt install python3-tk python3-venv libportaudio2'
        for palette, mode in [('carbon-cyan', 'dark'), ('slate', 'light'), ('forest', 'dark')]:
            change_theme(page, palette, mode)
            result = page.evaluate(MEASURE)
            assert not result['overflow']
            assert not result['invalidControls']
            assert not result['namelessFields']
            assert all(title['size'] == '24px' for title in result['titles'])
            assert all(title['size'] == '14.4px' for title in result['sections'])
            expect(page.locator('[data-platform-panel=linux]')).to_be_visible()
        page.evaluate("""() => {
            const root = document.querySelector('[data-voice-download]');
            const replacement = root.cloneNode(true);
            delete replacement.dataset.voicePlatform;
            root.replaceWith(replacement);
            document.dispatchEvent(new CustomEvent('htmx:afterSwap', {detail:{target:replacement}}));
        }""")
        expect(page.locator('[data-platform-panel=windows]')).to_be_visible()
        page.locator('[data-platform=macos]').click()
        expect(page.locator('[data-platform-panel=macos]')).to_be_visible()
        if width == 1440:
            destination = ROOT / 'build/voice-download-designs/live-guided-setup.png'
            destination.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination), full_page=True)
        assert not errors
        browser.close()


def test_live_setup_without_javascript_keeps_every_platform_readable(desk):
    from front_helpers import serve_browser_request

    server, _, users = desk
    users.change('jordan', own={'add': ['front-voice']})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.route('http://console.test/**', lambda route: serve_browser_request(server, 'http://console.test', route))
        page.goto('http://console.test/voice/desktop')
        expect(page.locator('[data-platform-navigation]')).to_be_hidden()
        for platform in ('windows', 'macos', 'linux'):
            expect(page.locator(f'[data-platform-panel={platform}]')).to_be_visible()
        expect(page.get_by_role('link', name='Download Voice app', exact=True)).to_be_visible()
        browser.close()
