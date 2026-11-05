"""Models pills and cache/storage pickers in Chromium; no real model loads."""
import json
import re

import pytest

from loadcoach.files import SPARE
from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_weightroom import desk, gguf, sign_in


@pytest.mark.parametrize('width', [1440, 390])
def test_model_type_pills_and_cache_warning_block_add(desk, tmp_path, monkeypatch, width):
    server, _, _ = desk
    file = gguf(tmp_path / 'available.gguf', general__architecture='tiny')
    library = Models(str(tmp_path / 'loadcoach.db'))
    library.put('available', {'flags':{'model':file,'ctx-size':2048},'gpus':0})
    sign_in(server,'jordan')
    def capture(route,path):
        if path == '/models?low=1':
            response = server.get('/models?new=1&complete=1')  # the editor and its inventory, in the page itself
            text = response.text
            match = re.search(r'(<script type="application/json" id="model-file-inventory">)(.*?)(</script>)', text, re.S)
            inventory = json.loads(match[2])
            source = inventory[0]; cache = source['places'][0]
            cache['free'] = SPARE
            source['places'].append({**cache,'name':'storage_'+cache['name'],'folder':'/stored','tier':'storage'})
            source['files'][0].update(path='/stored/available.gguf',place='storage_'+cache['name'],tier='storage')
            text = text[:match.start(2)] + json.dumps(inventory) + text[match.end(2):]
            text = text.replace(file,'/stored/available.gguf')
            route.fulfill(content_type='text/html',body=text)
            return True
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server, capture=capture)
        page.set_viewport_size({'width':width,'height':1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/models?new=1')
        pills = page.locator('nav[aria-label="Model type"]')
        page.locator('#file').select_option(index=1)
        playwright.expect(page.locator('[data-file-submit]')).to_be_enabled()
        assert page.locator('#file').evaluate('node => node.tagName') == 'SELECT'
        pills.get_by_role('link',name='Image',exact=True).click()
        playwright.expect(page.locator('#models-table')).to_contain_text('No models yet.')
        playwright.expect(page.get_by_role('link',name='Add image model',exact=True)).to_be_visible()
        pills.get_by_role('link',name='Audio',exact=True).click()
        playwright.expect(page.get_by_role('link',name='Add audio model',exact=True)).to_be_visible()
        # A storage-only model needs room on the source cache before Add can be used.
        page.goto('http://console.test/models?low=1')
        page.locator('#file').select_option(index=1)
        playwright.expect(page.locator('[data-file-submit]')).to_be_disabled()
        playwright.expect(page.locator('[data-file-warning]')).to_contain_text('Not enough cache space')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()


def test_image_and_audio_add_use_remote_storage_choices(desk):
    server, _, _ = desk
    sign_in(server,'jordan')
    sent = []
    inventory = {'nodes':[
        {'name':'compute','answering':True,'places':[{'name':'audio','folder':'/cache/audio','tier':'cache','free':10*SPARE},
                                                  {'name':'image','folder':'/cache/image','tier':'cache','free':10*SPARE}],'files':[]},
        {'name':'front','answering':True,'places':[{'name':'storage_audio','folder':'/store/audio','tier':'storage','free':10*SPARE},
                                                {'name':'storage_image','folder':'/store/image','tier':'storage','free':10*SPARE}],
         'files':[{'path':'/store/audio/ace/ace.gguf','size':100,'place':'storage_audio','tier':'storage','facts':{'what':'audio'}},
                  {'path':'/store/image/model.safetensors','size':100,'place':'storage_image','tier':'storage','facts':{'what':'image'}}]}]}
    def capture(route,path):
        base = path.partition('?')[0]
        value = None
        if base == '/model-files': value = inventory
        if base == '/mirrorwall-models' and route.request.method == 'GET':
            value = {'models':[],'cards':['CUDA0'],'nodes':[{'name':'compute','state':'ready'}],'engines':{'engines':[{'id':'sdcpp','available':True}]}}
        if base == '/mirrorwall-models/check': value = {'ready':False,'slots':[],'loras':[],'get_diffusion':[]}
        if base == '/soundroom-models' and route.request.method == 'GET':
            value = {'models':[],'cards':['CUDA0'],'catalogue':{'families':[{'family':'ace_step','name':'ACE','kinds':['music'],
                'licence':'MIT','commercial':'yes','packages':[{'id':'ace','name':'ACE','folder':'/cache/audio/ace',
                    'files':{'ace.gguf':'/cache/audio/ace/ace.gguf'},'installed':None}]}]}}
        if base in ('/mirrorwall-models','/soundroom-models') and route.request.method == 'POST':
            sent.append(json.loads(route.request.post_data))
            route.fulfill(status=202,content_type='application/json',body='{"kind":"model-install","id":"fixture"}')
            return True
        if value is not None:
            route.fulfill(content_type='application/json',body=json.dumps(value)); return True
    with playwright.sync_playwright() as driver:
        browser,page = browser_context(driver,server,capture=capture)
        errors=[]; page.on('pageerror',lambda error: errors.append(str(error)))
        page.goto('http://console.test/models/images?new=1')
        playwright.expect(page.locator('#mw-diffusion option')).to_have_count(2)
        page.locator('#mw-diffusion').select_option('/store/image/model.safetensors')
        page.locator('#mw-name').fill('picture')
        page.get_by_role('button',name='Save model',exact=True).click()
        page.wait_for_url('**/models?kind=image&install=fixture')
        assert sent[-1]['source_machine']=='front' and sent[-1]['settings']['flags']['model']=='/store/image/model.safetensors'
        page.goto('http://console.test/models/audio?new=1')
        playwright.expect(page.locator('#am-file option')).to_have_count(2)
        page.locator('#am-file').select_option(index=1)
        page.locator('#am-family').select_option('ace_step')
        assert page.get_by_role('heading',name='Catalogue',exact=True).count() == 0
        assert page.get_by_role('button',name='Install',exact=True).count() == 0
        page.locator('#am-name').fill('song')
        page.get_by_role('button',name='Add model',exact=True).click()
        page.wait_for_timeout(200)
        assert not page.locator('#am-message').is_visible(), page.locator('#am-message').text_content()
        assert len(sent) == 2, page.locator('#am-form').evaluate('form => [...form.elements].filter(one => !one.checkValidity()).map(one => ({id:one.id,value:one.value,message:one.validationMessage}))')
        page.wait_for_url('**/models?kind=audio&install=fixture',timeout=5000)
        assert sent[-1]['source_machine']=='front' and sent[-1]['settings']['flags']['model']=='/store/audio/ace/ace.gguf'
        assert not errors
        browser.close()
