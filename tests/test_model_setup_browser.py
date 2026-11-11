"""The real model editor's preset workflow in desktop/mobile Chromium."""

import json

import pytest

from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_model_setup import desk, fake_llama_server, fixture_model, native, setup_desk, SEES  # noqa: F401
from test_weightroom import gguf, sign_in


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_preset_workflow_in_browser(setup_desk, tmp_path, width):
    server, _, _ = setup_desk
    library = Models(str(tmp_path / "loadcoach.db"))
    original = fixture_model(tmp_path)
    gguf(original['flags']['model'], general__architecture='qwen', general__name='Example',
         general__size_label='8B', qwen__context_length=8192, tokenizer__chat_template=SEES)
    other_file = tmp_path / 'example-Q8_0.gguf'
    gguf(other_file, general__architecture='qwen', general__name='Example',
         general__size_label='8B', qwen__context_length=8192, tokenizer__chat_template=SEES)
    other = {**original, 'flags': {**original['flags'], 'model': str(other_file)}}
    library.put("example", original)
    library.put('second-quant', other)
    sign_in(server, "jordan")
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto("http://console.test/models/example")
        # Save preset keeps the settings typed as a named preset, beside Save, and leaves the model as it was.
        page.locator("#ctx").fill("4096")
        page.locator("#preset-name").fill("Daily <safe>")
        page.evaluate("async () => { await htmx.ajax('GET', '/strip', {target: '#strip', swap: 'innerHTML'}); }")
        playwright.expect(page.locator("#preset-name")).to_be_focused()
        page.locator("#preset-name").press("Enter")  # Enter in the name saves the preset, not the settings
        playwright.expect(page.get_by_text("Saved the preset Daily <safe>.", exact=True)).to_be_visible()
        assert library.stored("example") == original
        assert page.locator("#configuration-preview").count() == 0
        preset = page.get_by_role("table", name="Model presets", exact=True)
        playwright.expect(preset.get_by_text("Daily <safe>", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # Another quant of the same model loads it into its settings; Save applies it there, keeping its own file.
        page.goto('http://console.test/models/second-quant')
        preset.get_by_role('button', name='Load preset', exact=True).click()
        playwright.expect(page.get_by_text('Loaded the preset Daily <safe>. Save to apply it.', exact=True)).to_be_visible()
        playwright.expect(page.locator('#ctx')).to_have_value('4096')
        assert library.stored('second-quant') == other
        page.get_by_role('button', name='Save', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Saved.')  # with any warnings noted after it
        assert library.stored('second-quant')['flags']['model'] == str(other_file)
        assert library.stored('second-quant')['flags']['ctx-size'] == 4096
        assert library.stored('example') == original
        # Undo asks twice, then restores at once.
        page.get_by_role('button', name='Undo last change', exact=True).click()
        page.get_by_role('button', name='Are you sure? Undo last change', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Configuration restored.')
        assert library.stored('second-quant') == other
        preset.get_by_role("button", name="Delete preset", exact=True).click()
        page.locator("#danger-confirmation-check").check()
        page.get_by_role("button", name="Continue", exact=True).click()
        playwright.expect(page.get_by_text("Preset deleted.", exact=True)).to_be_visible()
        assert library.presets("example") == []
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()
    library.db.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_model_cards_share_preferences_across_models(setup_desk, tmp_path, width):
    server, _, _ = setup_desk
    library = Models(str(tmp_path / "loadcoach.db"))
    original = fixture_model(tmp_path)
    library.put("example", original)
    other_file = tmp_path / "other.gguf"
    gguf(other_file, general__architecture="qwen", general__name="Other",
         general__description="Different metadata count", qwen__context_length=8192)
    library.put("other", {**original, "flags": {**original["flags"], "model": str(other_file)}})
    sign_in(server, "jordan")
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({"width": width, "height": 1000})
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://console.test/models/example")
        # Adopt this model's previous choices once, then prefer the shared choices
        # over contradictory old preferences belonging to another model.
        storage_key = page.evaluate("`weightroom-cards-v1:${encodeURIComponent(document.documentElement.dataset.themeUser)}`")
        # Seed before the next document starts; the outgoing page normally captures
        # its current cards on unload and would overwrite a manual storage edit.
        page.add_init_script("""{
            const key = """ + json.dumps(storage_key) + """;
            if (!sessionStorage.getItem('model-cards-fixture-seeded')) {
                localStorage.setItem(key, JSON.stringify({
                    '/models/example::Settings': false, '/models/example::Benchmark results': false,
                    '/models/other::Settings': false, '/models/other::Benchmark results': false
                }));
                sessionStorage.setItem('model-cards-fixture-seeded', 'yes');
            }
        }""")
        page.reload()
        settings = page.locator("main").get_by_text("Settings", exact=True).locator("xpath=ancestor::details[1]")
        benchmarks = page.get_by_text("Benchmark results", exact=True).locator("xpath=ancestor::details[1]")
        flags = page.get_by_text("Every flag this llama-server knows", exact=True).locator("xpath=ancestor::details[1]")
        presets = page.get_by_text("Model presets", exact=True).locator("xpath=ancestor::details[1]")
        metadata = page.locator("#file-metadata")
        for card in (settings, benchmarks):
            playwright.expect(card).not_to_have_attribute("open", "")
        page.goto("http://console.test/models/other")
        for card in (settings, benchmarks):
            playwright.expect(card).not_to_have_attribute("open", "")
            card.locator(":scope > summary").click()
        page.get_by_text("Advanced loading flags", exact=True).click()
        flags.locator(":scope > summary").click()
        page.get_by_text("File facts", exact=True).click()
        metadata.locator(":scope > summary").click()
        presets.locator(":scope > summary").click()
        page.goto("http://console.test/models/example")
        for card in (settings, benchmarks, flags, metadata):
            playwright.expect(card).to_have_attribute("open", "")
        playwright.expect(presets).not_to_have_attribute("open", "")
        page.evaluate("async () => { await htmx.ajax('GET', '/strip', {target: '#strip', swap: 'innerHTML'}); }")
        for card in (settings, benchmarks, flags, metadata):
            playwright.expect(card).to_have_attribute("open", "")
        settings.locator(":scope > summary").click()
        page.goto("http://console.test/models/other")
        playwright.expect(settings).not_to_have_attribute("open", "")
        settings.locator(":scope > summary").click()
        playwright.expect(flags).to_have_attribute("open", "")
        page.reload()
        playwright.expect(settings).to_have_attribute("open", "")
        playwright.expect(flags).to_have_attribute("open", "")
        playwright.expect(presets).not_to_have_attribute("open", "")
        # A different signed-in account in the same browser keeps its own defaults.
        sign_in(server, "sam")
        page.goto("http://console.test/models/example")
        playwright.expect(presets).to_have_attribute("open", "")
        playwright.expect(flags).not_to_have_attribute("open", "")
        sign_in(server, "jordan")
        page.goto("http://console.test/models/other")
        playwright.expect(presets).not_to_have_attribute("open", "")
        playwright.expect(flags).to_have_attribute("open", "")
        assert not errors
        browser.close()
    library.db.close()
