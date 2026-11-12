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
        page.get_by_role("button", name="Edit Context and requests", exact=True).click()
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
        page.get_by_role('button', name='Presets', exact=True).click()
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
        page.get_by_role('button', name='Presets', exact=True).click()
        page.get_by_role('button', name='Undo last change', exact=True).click()
        page.get_by_role('button', name='Are you sure? Undo last change', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Configuration restored.')
        assert library.stored('second-quant') == other
        page.get_by_role("button", name="Presets", exact=True).click()
        preset.get_by_role("button", name="Delete preset", exact=True).click()
        page.locator("#danger-confirmation-check").check()
        page.get_by_role("button", name="Continue", exact=True).click()
        playwright.expect(page.get_by_text("Preset deleted.", exact=True)).to_be_visible()
        assert library.presets("example") == []
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()
    library.db.close()


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_configuration_overview_preserves_drafts_across_groups_views_and_polling(setup_desk, tmp_path, width):
    server, _, _ = setup_desk
    library = Models(str(tmp_path / "loadcoach.db"))
    library.put("example", fixture_model(tmp_path))
    sign_in(server, "jordan")
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({"width": width, "height": 1000})
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://console.test/models/example")
        playwright.expect(page.locator('[data-model-summary]:visible')).to_have_count(6)
        playwright.expect(page.locator('[data-model-group-editor]:visible')).to_have_count(0)
        page.get_by_role('button', name='Edit Context and requests', exact=True).click()
        page.locator('#ctx').fill('4096')
        page.get_by_role('button', name='Edit Generation defaults', exact=True).click()
        page.locator('#generation-temp').fill('0.8')
        for view in ('nodes', 'presets', 'facts', 'maintenance', 'configuration'):
            page.locator(f'[data-model-view="{view}"]').click()
            playwright.expect(page.locator(f'[data-model-panel="{view}"]')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.evaluate("async () => { await htmx.ajax('GET', '/strip', {target: '#strip', swap: 'innerHTML'}); }")
        playwright.expect(page.locator('#ctx')).to_have_value('4096')
        playwright.expect(page.locator('#generation-temp')).to_have_value('0.8')
        playwright.expect(page.locator('[data-model-dirty]')).to_contain_text('Unsaved changes')
        page.get_by_role('button', name='Done editing Generation defaults', exact=True).click()
        playwright.expect(page.locator('#model-group-context [data-model-summary]')).to_contain_text('4,096')
        playwright.expect(page.locator('#model-group-generation [data-model-summary]')).to_contain_text('0.8')
        page.get_by_role('button', name='Discard changes', exact=True).click()
        playwright.expect(page.locator('#ctx')).to_have_value(str(fixture_model(tmp_path)['flags']['ctx-size']))
        playwright.expect(page.locator('[data-model-dirty]')).to_have_text('No unsaved changes.')
        assert not errors
        browser.close()
    library.db.close()
