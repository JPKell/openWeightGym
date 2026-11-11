"""Engine/node forms over isolated apps; qualification uses CPU-rendered Chromium."""
import pytest
from engines import engine_for

from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_weightroom import desk, gguf, sign_in  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_engine_model_selection_and_pending_node_settings_in_browser(desk, tmp_path, monkeypatch, width):
    server, _, _ = desk
    sign_in(server, "jordan")
    # The in-process desk omits worker startup; refresh its settings report explicitly.
    assert server.post("/compute-nodes/gpus", data={"machine": "compute"}).status_code == 200
    weights = tmp_path / "example.gguf"
    gguf(weights, general__architecture="qwen", qwen__context_length=2048)
    library = Models(str(tmp_path / "loadcoach.db"))
    library.put('available', {'flags':{'model':str(weights),'ctx-size':2048},'gpus':0})
    adapter = engine_for('llama_cpp'); describe = adapter.describe
    monkeypatch.setattr(adapter, 'describe', lambda config, settings=None: {**describe(config,settings),'available':True})
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto("http://console.test/models?new=1&node=compute")  # as the Add a model button opens it
        playwright.expect(page.get_by_label("Engine", exact=True)).to_have_value("llama_cpp")
        page.locator("#name").fill("engine-example")
        page.locator("#file").select_option(index=1)
        page.locator("#ctx").fill("2048")
        assert page.locator("#cards, #allow-split").count() == 0
        page.get_by_role("button", name="Add", exact=True).click()
        page.get_by_role('link',name='Open model',exact=True).click(timeout=10000)
        # The ASGI bridge follows redirects internally; HTTP tests cover their URLs.
        playwright.expect(page.get_by_role("heading", name="engine-example", exact=True)).to_be_visible()
        assert page.locator('form[action="/models/engine-example?node=compute"][data-model-settings]').count() == 1
        playwright.expect(page.locator('[name="engine"]')).to_have_value("llama_cpp")
        assert library.stored("engine-example")["engine"] == "llama_cpp"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.goto("http://console.test/compute-nodes?machine=compute")
        settings = page.get_by_role("region", name="llama.cpp engine settings")
        settings.get_by_label("Executable on compute", exact=True).fill("/pending/server")
        settings.get_by_role("button", name="Save", exact=True).click()
        playwright.expect(settings).to_contain_text("Changes pending restart")
        playwright.expect(settings.get_by_label("Executable on compute", exact=True)).to_have_value("/pending/server")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"engine-node-{width}.png"), full_page=True)
        assert not errors
        browser.close()
    library.db.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_strata_selection_renders_its_own_fields_and_node_settings(desk, tmp_path, monkeypatch, width):
    server, _, _ = desk
    sign_in(server, "jordan")
    engine = engine_for("strata")
    describe = engine.describe
    def available(config, settings=None):
        return {**describe(config, settings), "enabled": True, "available": True, "why": ""}
    monkeypatch.setattr(engine, "describe", available)  # fixture metadata only; no runtime enabled
    weights = gguf(tmp_path / 'weights.gguf', general__architecture='qwen',qwen__context_length=2048)
    tokenizer = tmp_path / 'tokenizer'; tokenizer.mkdir()
    for name in engine.model.TOKENIZER_FILES:
        (tokenizer / name).write_text('{}')
    library = Models(str(tmp_path / 'loadcoach.db'))
    library.put('available', {'flags':{'model':weights,'ctx-size':2048},'gpus':0})
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        page.set_viewport_size({"width": width, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://console.test/models?new=1&node=compute")  # as the Add a model button opens it
        page.get_by_label("Engine", exact=True).select_option("strata")
        playwright.expect(page.locator("#tokenizer")).to_be_visible()
        assert page.locator("#cards").count() == page.locator("#allow-split").count() == 0
        page.locator("#name").fill("strata-example")
        page.locator("#file").select_option(index=1)
        page.locator("#tokenizer").fill(str(tokenizer))
        page.get_by_role("button", name="Add", exact=True).click()
        page.get_by_role('link',name='Open model',exact=True).click(timeout=10000)
        playwright.expect(page.get_by_role("heading", name="strata-example", exact=True)).to_be_visible()
        playwright.expect(page.locator("#tokenizer")).to_have_value(str(tokenizer))
        assert page.locator("#placement-cpu").count() == page.locator("#allow-split").count() == 0
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.goto("http://console.test/compute-nodes?machine=compute")
        strata = page.get_by_role("region", name="Strata engine settings")
        playwright.expect(strata.get_by_label("Pinned Strata source directory on compute", exact=True)).to_be_visible()
        playwright.expect(strata.get_by_label("Allow Strata hybrid host RAM use", exact=True)).not_to_be_checked()
        assert not errors
        browser.close()
