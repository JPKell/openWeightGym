"""Shared reranker configuration controls in both console appearances and narrow layouts."""
import pytest
import sys
from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_model_setup import desk, fake_llama_server, fixture_model, setup_desk, SEES, HELP  # noqa: F401
from test_weightroom import sign_in

@pytest.fixture
def native(tmp_path, fake_llama_server):
    """Advertise the real installed runtime's reranking switch in this fake server."""
    help_text=HELP+'--reranking                  reranking endpoint\n'
    executable=tmp_path/'rerank-native'
    executable.write_text(f"#!{sys.executable}\nimport os,sys\nif sys.argv[1:]==['--help']:\n print({help_text!r})\nelse:\n os.execv({fake_llama_server!r}, [{fake_llama_server!r}, *sys.argv[1:]])\n")
    executable.chmod(0o755)
    return str(executable)

@pytest.mark.parametrize('width',[1440,390])
@pytest.mark.parametrize('theme',['light','dark'])
def test_reranker_matrix_control_roundtrip_and_type_filter(setup_desk,tmp_path,width,theme):
    server,_,_=setup_desk
    library=Models(str(tmp_path/'loadcoach.db'));library.put('example',fixture_model(tmp_path))
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,server)
        page.set_viewport_size({'width':width,'height':1000})
        page.goto('http://console.test/models/example')
        page.evaluate('(theme)=>document.documentElement.setAttribute("data-bs-theme",theme)',theme)
        page.get_by_role('button',name='Edit Model',exact=True).click()
        page.locator('#reranking').check()
        page.evaluate("async()=>await htmx.ajax('GET','/strip',{target:'#strip',swap:'innerHTML'})")
        playwright.expect(page.locator('#reranking')).to_be_checked()
        page.get_by_role('button',name='Save',exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert library.stored('example')['flags']['reranking'] is True
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.goto('http://console.test/models?kind=rerank')
        playwright.expect(page.get_by_role('link',name='Reranker',exact=True)).to_be_visible()
        page.goto('http://console.test/hub?kind=rerank&q=acme')
        playwright.expect(page.locator('#hub-kind')).to_have_value('rerank')
        playwright.expect(page.locator('#hub-results a.discovery-repository').first).to_have_attribute('href', '/hub?repository=acme/tiny-GGUF&kind=rerank')
        browser.close()
    library.db.close()
