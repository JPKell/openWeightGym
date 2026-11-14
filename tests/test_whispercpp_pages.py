"""Whisper's individual model page uses its native profile and shared media controls."""
import pytest

from loadcoach.models import Models
from test_whispercpp import profile  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright


@pytest.mark.parametrize('width',[1440,390])
@pytest.mark.parametrize('theme',['light','dark'])
def test_whisper_individual_model_page_preserves_native_settings(desk,profile,tmp_path,width,theme):
    client,_,_=desk
    library=Models(str(tmp_path/'loadcoach.db'));library.put('whisper',profile)
    sign_in(client,'jordan')
    response=client.get('/models/whisper?node=compute')
    assert response.status_code==200,response.text
    assert 'whisper.cpp' in response.text and 'Transcription' in response.text and '8192 MiB' in response.text
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,client)
        page.set_viewport_size({'width':width,'height':1000})
        page.goto('http://console.test/models/whisper?node=compute')
        page.evaluate('(theme)=>document.documentElement.setAttribute("data-bs-theme",theme)',theme)
        playwright.expect(page.get_by_role('link',name='Edit settings',exact=True)).to_have_attribute('href','/models/audio?node=compute&model=whisper')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        browser.close()
    assert library.stored('whisper')==profile
    library.db.close()
