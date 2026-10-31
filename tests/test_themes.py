"""Browser checks for account-scoped themes, using isolated ASGI apps and no real GPUs."""

import re
from pathlib import Path

import pytest

from test_weightroom import desk, sign_in  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


def contrast(first, second):
    def luminance(color):
        values = [int(color[index:index + 2], 16) for index in (1, 3, 5)] if color.startswith("#") else [int(value) for value in re.findall(r"\d+", color)[:3]]
        channels = [value / 255 for value in values]
        channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(value * factor for value, factor in zip(channels, (.2126, .7152, .0722)))
    bright, dim = sorted((luminance(first), luminance(second)), reverse=True)
    return (bright + .05) / (dim + .05)


def test_theme_picker_contrast_persistence_account_isolation_and_mobile(desk, tmp_path):
    server, _, users = desk
    sign_in(server, "jordan")
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--disable-gpu"])
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, color_scheme="light")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            if route.request.url.split('?', 1)[0] == 'http://themes.test/strip/stream':
                from front_helpers import serve_browser_request
                return serve_browser_request(server, 'http://themes.test', route)
            response = server.get(route.request.url.replace("http://themes.test", ""))
            route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "text/html"), body=response.content)

        context.route("http://themes.test/**", serve)
        page.goto("http://themes.test/account/appearance")
        page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
        assert page.locator("[name=palette]").count() == 17
        assert page.locator("[data-theme-options] > .theme-option").count() == 17
        assert page.locator("html").get_attribute("data-theme-user") == users.identity("jordan")
        palettes = page.locator("[name=palette]").evaluate_all("inputs => inputs.map(input => input.value)")
        added = {"sunflower", "carbon-cyan", "carbon-green", "carbon-amber", "carbon-violet"}
        assert added <= set(palettes)
        assert page.locator(".theme-group-heading").count() == 0
        removed = {"linen", "amber", "mint", "lavender"}
        assert not removed & set(palettes)
        assert palettes == ["sunflower", "clay", "coffee", "sage", "forest", "lagoon",
                            "ocean", "snow", "slate", "indigo", "orchid", "rose", "graphite",
                            "carbon-cyan", "carbon-green", "carbon-amber", "carbon-violet"]
        carbon_text, carbon_accents = set(), set()
        for mode in ("light", "dark"):
            for palette in palettes:
                page.locator(f'[name=palette][value="{palette}"]').check()
                page.select_option("#theme-mode", mode)
                assert page.locator("html").get_attribute("data-site-theme") == palette
                assert page.locator("html").get_attribute("data-bs-theme") == mode
                assert not page.locator("#theme-mode").is_disabled()
                colors = page.evaluate("""() => {
                    const root = getComputedStyle(document.documentElement);
                    const token = name => root.getPropertyValue('--theme-' + name).trim();
                    const body = getComputedStyle(document.body);
                    const button = getComputedStyle(document.querySelector('[data-theme-form] .btn-primary'));
                    return {bg: token('bg'), surface: token('surface'), text: token('text'),
                        muted: token('muted'), accent: token('accent'), button: token('button'),
                        bodyText: body.color, bodyBg: body.backgroundColor,
                        buttonText: button.color, buttonBg: button.backgroundColor};
                }""")
                for foreground, background in (("text", "bg"), ("text", "surface"), ("muted", "surface"), ("accent", "surface")):
                    assert contrast(colors[foreground], colors[background]) >= 4.5, (palette, mode, foreground)
                assert contrast("#ffffff", colors["button"]) >= 4.5, (palette, mode, "button")
                assert contrast(colors["bodyText"], colors["bodyBg"]) >= 4.5, (palette, mode, "body CSS")
                assert contrast(colors["buttonText"], colors["buttonBg"]) >= 4.5, (palette, mode, "button CSS")
                if mode == "dark" and palette.startswith("carbon-"):
                    assert colors["bg"] == "#101112" and colors["surface"] == "#1a1c1f"
                    carbon_text.add(colors["text"])
                    carbon_accents.add(colors["accent"])
        assert len(carbon_text) == len(carbon_accents) == 4

        page.locator('[name=palette][value="ocean"]').check()
        page.select_option("#theme-mode", "light")
        page.get_by_role("button", name="Save theme", exact=True).click()
        assert "Theme saved" in page.locator("#theme-status").inner_text()
        page.reload()
        assert page.locator("html").get_attribute("data-site-theme") == "ocean"
        assert page.locator("html").get_attribute("data-bs-theme") == "light"
        page.goto("http://themes.test/models")
        assert page.locator("html").get_attribute("data-site-theme") == "ocean"

        sign_in(server, "pat")
        page.goto("http://themes.test/account/appearance")
        assert page.locator("html").get_attribute("data-site-theme") == "slate"
        sign_in(server, "jordan")
        page.reload()
        assert page.locator("html").get_attribute("data-site-theme") == "ocean"
        page.locator('[name=palette][value="rose"]').check()
        page.get_by_role("button", name="Reset preview", exact=True).click()
        assert page.locator("html").get_attribute("data-site-theme") == "ocean"

        images = Path("data/theme-previews")
        images.mkdir(parents=True, exist_ok=True)
        page.locator('[data-theme-form]').screenshot(path=str(images / "light-palettes.png"), animations="disabled", style="header.sticky-top { visibility: hidden !important; }")
        page.select_option("#theme-mode", "dark")
        page.locator('[name=palette][value="indigo"]').check()
        page.locator('[data-theme-form]').screenshot(path=str(images / "dark-palettes.png"), animations="disabled", style="header.sticky-top { visibility: hidden !important; }")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(images / "mobile.png"))

        page.get_by_role("button", name="Use default", exact=True).click()
        page.emulate_media(color_scheme="dark")
        playwright.expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark")
        page.emulate_media(color_scheme="light")
        playwright.expect(page.locator("html")).to_have_attribute("data-bs-theme", "light")
        page.emulate_media(color_scheme="dark")
        for palette in sorted(added):
            page.locator(f'[name=palette][value="{palette}"]').check()
            page.select_option("#theme-mode", "system")
            page.get_by_role("button", name="Save theme", exact=True).click()
            page.reload()
            assert page.locator("html").get_attribute("data-site-theme") == palette
            assert page.locator("html").get_attribute("data-bs-theme") == "dark"
            assert not page.locator("#theme-mode").is_disabled()
            page.emulate_media(color_scheme="light")
            playwright.expect(page.locator("html")).to_have_attribute("data-bs-theme", "light")
            page.emulate_media(color_scheme="dark")
            playwright.expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark")
        for palette in sorted(removed):
            page.evaluate("""([identity, palette]) => localStorage.setItem(
                'weightroom-theme-v1:' + encodeURIComponent(identity), JSON.stringify({palette, mode: 'dark'}))""",
                [users.identity("jordan"), palette])
            page.reload()
            assert page.locator("html").get_attribute("data-site-theme") == "slate"
            assert page.locator("html").get_attribute("data-bs-theme") == "dark"
        page.evaluate("() => { Storage.prototype.setItem = () => { throw new Error('disabled'); }; }")
        page.get_by_role("button", name="Save theme", exact=True).click()
        assert "could not save" in page.locator("#theme-status").inner_text()
        assert errors == []
        browser.close()
