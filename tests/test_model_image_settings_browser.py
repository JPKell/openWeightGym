"""Desktop and narrow Chromium acceptance over real isolated authenticated routes."""
import json
from pathlib import Path

import pytest
from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_weightroom import desk, gguf, sign_in


@pytest.mark.parametrize('width',[1440,390,320])
def test_model_defaults_workflow_versions_and_civitai_destinations(desk,tmp_path,monkeypatch,width):
    server,wire,_=desk
    library=Models(str(tmp_path/'loadcoach.db'))
    file=gguf(tmp_path/'trained.gguf',general__architecture='qwen',qwen__context_length=8192)
    library.put('available',{'flags':{'model':file,'ctx-size':8192},'gpus':0})
    profiles=[{'id':name,'model_id':'model-'+name,'image_workflow':{
        'family':family,'exclusive':True,'defaults':{'width':512,'height':512,'steps':6,'n':1,'guidance':1,'cfg_scale':7 if family=='sdxl' else 1,'sampler':'euler','scheduler':'discrete' if family=='sdxl' else 'flux2','seed':-1,'upscale':1},
        'limits':{'max_width':1024,'max_height':1024,'max_steps':30,'max_images':4,'max_references':0 if family=='sdxl' else 1,'max_pixels':1048576},'reference_megapixels':.25 if family=='sdxl' else 1,'loras':{}}} for name,family in [('SDXL','sdxl'),('Flux','flux2-klein')]]
    async def catalogue(user):return profiles
    monkeypatch.setattr(wire.front.state.mirrorwall,'catalogue',catalogue)
    sign_in(server,'jordan')
    submitted=[]
    def capture(route,path):
        if path=='/mirrorwall-api/jobs' and route.request.method=='POST':
            submitted.append(route.request.post_data_json)
            route.fulfill(content_type='application/json',body='{"id":"native-job"}');return True
        if path.startswith('/mirrorwall-discovery'):
            value=({'files':[{'file':'weights.safetensors','size':1000}], 'places':[{'name':'image'},{'name':'storage_image'},{'name':'encoders'},{'name':'storage_encoders'}]}
                if 'repository=' in path else {'found':[{'name':'Source model','kind':'Checkpoint','base_model':'SDXL','url':'https://civitai.com/models/1','repository':'civitai/1'}]})
            route.fulfill(content_type='application/json',body=json.dumps(value));return True
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server,capture=capture)
        page.set_viewport_size({'width':width,'height':1100});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/models?new=1')
        page.locator('#file').select_option(index=1)
        playwright.expect(page.locator('#ctx')).to_have_value('8192');playwright.expect(page.locator('#min-context')).to_have_value('4096')
        page.locator('#ctx').fill('6000');page.locator('#name').fill('manual')
        playwright.expect(page.locator('#ctx')).to_have_value('6000')
        assert page.locator('#cards, #allow-split').count()==0
        if width==1440:
            positions=[page.locator(selector).bounding_box() for selector in ['#model-nodes','#file','#model-engine','#name','#projector','#min-context','#ctx']]
            assert positions[0]['x']<positions[1]['x']<positions[2]['x'] and positions[3]['x']<positions[4]['x']<positions[5]['x']<positions[6]['x']
            assert positions[0]['y']<positions[3]['y'] and len({round(box['y']+box['height']) for box in positions[3:]})==1  # the second row's fields line up
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        folder=Path('data/qualification/model-image-settings-20261027');folder.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(folder/f'models-{width}.png'),full_page=True)
        page.goto('http://console.test/models/images/workflows')
        playwright.expect(page.locator('#workflow-model')).to_have_value('SDXL')
        page.locator('#workflow-name').fill('SDXL product')
        page.locator('#workflow-negative_prompt').fill('blur, text')
        page.get_by_role('button',name='Save workflow',exact=True).click()
        playwright.expect(page.locator('main').get_by_role('status')).to_contain_text('Workflow saved.')
        page.locator('#workflow-steps').fill('9')
        page.get_by_role('button',name='Save new version',exact=True).click()
        playwright.expect(page.get_by_text('Version 2',exact=True)).to_be_visible()
        page.reload();page.locator('#workflow-workflow').select_option(index=1)
        playwright.expect(page.locator('#workflow-negative_prompt')).to_have_value('blur, text')
        playwright.expect(page.locator('#workflow-steps')).to_have_value('9')
        page.locator('#workflow-model').select_option('Flux')
        playwright.expect(page.locator('#workflow-negative_prompt')).to_be_hidden()
        playwright.expect(page.locator('#workflow-strength')).to_be_hidden()
        page.locator('#workflow-model').select_option('SDXL')
        page.screenshot(path=str(folder/f'workflows-{width}.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.goto('http://console.test/chat/images')
        playwright.expect(page.locator('#mw-model')).to_have_value('SDXL')
        page.locator('#mw-preset').select_option(index=1)
        page.locator('#mw-prompt').fill('A red ceramic owl')
        assert page.locator('#mw-form').evaluate('form=>form.checkValidity()')
        page.get_by_role('button',name='Generate',exact=True).click()
        playwright.expect(page.locator('#mw-generate')).to_be_enabled()
        assert submitted and submitted[0]['request']['negative_prompt']=='blur, text'
        assert submitted[0]['request']['reference_megapixels']==.25
        page.goto('http://console.test/civitai');page.locator('#mw-q').fill('example');page.get_by_role('button',name='Search',exact=True).click()
        playwright.expect(page.get_by_role('link',name='Model page',exact=True)).to_have_attribute('href','https://civitai.com/models/1')
        page.get_by_role('button',name='Choose file',exact=True).click()
        playwright.expect(page.get_by_label('Download destination',exact=True)).to_be_visible()
        assert page.get_by_label('Download destination',exact=True).locator('option').all_text_contents()==['Image storage','Encoder storage']  # never a cache
        playwright.expect(page.get_by_text('Downloads go into storage, never the cache.',exact=False)).to_be_visible()
        page.screenshot(path=str(folder/f'civitai-{width}.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors
        browser.close()


@pytest.mark.parametrize('width',[1440,390,320])
def test_fixed_fit_selection_survives_type_filter_and_navigation_permissions(desk,tmp_path,width):
    server,wire,_=desk;library=Models(str(tmp_path/'loadcoach.db'),{'flash-attn':'on'})
    file=gguf(tmp_path/'base.gguf',general__architecture='tiny')
    library.put('language',{'flags':{'model':file,'ctx-size':8192},'gpus':['A']})
    library.measured('language',library.get('language'),[500])
    settings={'engine':'sdcpp','flags':{'model':file,'ctx-size':0},'gpus':['B'],'alone':True,
        'sdcpp':{'family':'sdxl','host_ram_mib':2048,'components':{},'defaults':{},'limits':{}}}
    library.put('picture',settings)
    library.measured('picture',library.get('picture'),[1000],image_profile={'qualified':True})
    library.put('unqualified',{**settings,'sdcpp':{**settings['sdcpp'],'limits':{'max_width':512}}})
    sign_in(server,'jordan')
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server);page.set_viewport_size({'width':width,'height':1000})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/dry-fit')
        page.get_by_label('language on A',exact=True).check()
        page.get_by_role('button',name='Image',exact=True).click()
        page.get_by_label('picture on B',exact=True).check()
        assert page.locator('[data-model="picture"] .fit-context').count()==0
        assert page.locator('tr[data-model="picture"] .fit-on:checked').count()==1
        playwright.expect(page.get_by_text('Unknown / unqualified memory:',exact=False)).to_be_visible()
        page.get_by_role('button',name='LLM',exact=True).click()
        assert page.get_by_label('language on A',exact=True).is_checked()
        assert page.locator('tr[data-model="picture"] .fit-on:checked').count()==1
        assert page.locator('.fit-pick[data-model="picture"]').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        folder=Path('data/qualification/model-image-settings-20261027');page.screenshot(path=str(folder/f'fit-{width}.png'),full_page=True)
        page.goto('http://console.test/settings/image')
        assert page.get_by_role('navigation',name='Settings pages').is_visible() if width==1440 else True
        assert page.locator('#site-sidebar a[href="/settings/image"][aria-current="page"]').count()==1
        assert page.locator('#mobile-navigation a[href="/settings/image"][aria-current="page"]').count()==1
        assert not errors
        browser.close()
