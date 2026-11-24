"""Video pages reuse authenticated session routes and platform service policy."""
import json
from copy import deepcopy

import pytest

from test_weightroom import desk, sign_in
from test_video_store import clip
from test_video_requests import profile
from test_console_browser import browser_context,playwright


def test_video_pages_and_model_permissions(desk):
    browser,_,_=desk
    sign_in(browser,'jordan')
    for path in ('/chat/video','/models/video','/settings/video'):
        response=browser.get(path)
        assert response.status_code==200,(path,response.text[:500])
    assert 'id="video-player"' in browser.get('/chat/video').text
    assert 'data-media-kind="video"' in browser.get('/models/video').text
    sign_in(browser,'pat')
    assert browser.get('/models/video').status_code==403
    assert browser.get('/video-models').status_code==403
    assert browser.get('/settings/video').status_code==403
    assert browser.get('/chat/video').status_code==200


def test_video_proxy_rejects_foreign_origin_and_unknown_operations(desk):
    browser,_,_=desk
    sign_in(browser,'jordan')
    assert browser.post('/video-api/jobs',headers={'origin':'https://other.example'},json={}).status_code==403
    assert browser.get('/video-api/../../loadcoach/settings').status_code==404
    assert browser.post('/video-api/settings',json={}).status_code==404


@pytest.mark.parametrize('width',[1440,390])
def test_live_canvas_keeps_model_drafts_and_selection_during_polling(desk,clip,width):
    server,_,_=desk
    sign_in(server,'jordan')
    settings=profile()
    models=[{'id':name,'model_id':name,'capabilities':{'video_generation':True},'video_workflow':{
        **settings['sdcpp'],'defaults':{'width':832,'height':480,'seconds':5,'fps':16,'steps':20,'seed':-1,'guidance':6,'sampler':'euler','scheduler':'discrete'}}} for name in ('wan-a','wan-b')]
    jobs=[{'id':'clip-1','asset_id':'image_'+'1'*32,'model':'wan-a','status':'completed','seconds':'0.3125',
           'frames':5,'fps':16,'size':'256x256','created_at':1,'progress':100,
           'settings':{'model':'wan-a','prompt':'Saved clip','seconds':0.3125,'fps':16,'width':256,'height':256},
           'assets':[{'metadata':{'poster_available':True,'mode':'text'}}]}]
    submitted=[];deleted=[]
    for item in models:item['video_workflow']['loras']={'motion':{'scale':.7}}
    def capture(route,path):
        if path=='/video-api/state':
            route.fulfill(content_type='application/json',body=json.dumps({'models':models,'jobs':jobs,'can_unlogged':True}));return True
        if path=='/video-api/jobs' and route.request.method=='POST':
            submitted.append(route.request.post_data_json)
            route.fulfill(status=202,content_type='application/json',body=json.dumps({'id':'new-job'}));return True
        if path=='/video-api/jobs/clip-1' and route.request.method=='DELETE':
            deleted.append(path);jobs.clear();route.fulfill(content_type='application/json',body='{"deleted":true}');return True
        if path.startswith('/video-api/jobs/clip-1/content'):
            if 'variant=thumbnail' in path:
                from test_mirrorwall import image_bytes
                route.fulfill(content_type='image/png',body=image_bytes())
            else:route.fulfill(content_type='video/webm',body=clip)
            return True
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server,capture=capture)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.set_viewport_size({'width':width,'height':1000});page.goto('http://console.test/chat/video')
        playwright.expect(page.locator('#video-model')).to_have_value('wan-a')
        page.get_by_text('Advanced',exact=True).click()
        page.locator('#video-prompt').fill('Keep the unsent draft')
        page.locator('[data-lora=motion]').fill('.3')
        page.locator('#video-size').select_option('480x832')
        page.locator('#video-model').select_option('wan-b');page.locator('#video-prompt').fill('Second model draft')
        page.locator('#video-model').select_option('wan-a')
        playwright.expect(page.locator('#video-prompt')).to_have_value('Keep the unsent draft')
        playwright.expect(page.locator('[data-lora=motion]')).to_have_value('0.3')
        playwright.expect(page.locator('#video-gallery > button')).to_have_attribute('aria-selected','true')
        page.wait_for_function('document.getElementById("video-player").readyState>=2')
        assert page.locator('#video-player').evaluate('v=>v.videoWidth')==256
        models[0]['video_workflow']['envelope']={'at':2}  # a finished run publishes a newer measurement
        page.clock.install();page.clock.fast_forward(3100)
        playwright.expect(page.locator('#video-size')).to_have_value('480x832')
        playwright.expect(page.locator('#video-gallery > button')).to_have_attribute('aria-selected','true')
        playwright.expect(page.locator('#video-prompt')).to_have_value('Keep the unsent draft')
        page.locator('#video-generate').click()
        assert submitted[-1]['prompt']=='Keep the unsent draft'
        assert submitted[-1]['model']=='wan-a' and submitted[-1]['size']=='480x832'
        assert submitted[-1]['loras']=={'motion':.3}
        page.locator('#video-delete').click();assert not deleted
        page.locator('#video-delete').click()
        playwright.expect(page.locator('#video-gallery > button')).to_have_count(0)
        assert deleted==['/video-api/jobs/clip-1']
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        assert not errors,errors
        browser.close()


def test_video_model_editor_uses_shared_setup_and_workflow_controls(desk):
    server,_,_=desk;sign_in(server,'jordan')
    settings=profile()
    public={'id':'wan','model_id':'video-id','video_workflow':{**settings['sdcpp'],'defaults':{
        'width':832,'height':480,'seconds':5,'fps':16,'steps':20,'seed':-1,'guidance':6,'sampler':'euler','scheduler':'discrete'}}}
    saved=[];image_weights=False
    def capture(route,path):
        base=path.partition('?')[0]
        if base=='/video-models' and route.request.method=='GET':
            route.fulfill(content_type='application/json',body=json.dumps({'models':[{'name':'wan','model_id':'video-id','node':'compute',**settings}],
                'cards':['C0'],'nodes':[{'name':'compute','state':'ready'}],'engines':{'engines':[{'id':'sdcpp','available':True}]}}));return True
        if base=='/video-models/check':
            found={'ready':True,'slots':[{'slot':'diffusion','need':'required','status':'ok','message':'Fixture'}],'loras':[],'get_diffusion':[]}
            if image_weights: found.update(ready=False,detected_family='sdxl',family_matches=False,variant_label='SDXL')
            route.fulfill(content_type='application/json',body=json.dumps(found));return True
        if base=='/mirrorwall-api/workflows':
            route.fulfill(content_type='application/json',body=json.dumps({'models':[public],'presets':[],'can_manage':True}));return True
        if base=='/video-models' and route.request.method=='POST':
            saved.append(route.request.post_data_json);route.fulfill(content_type='application/json',body=json.dumps({'name':'wan'}));return True
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server,capture=capture);errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/models/video')
        playwright.expect(page.locator('#studio-model-name')).to_have_text('wan')
        playwright.expect(page.locator('#workflow-seconds')).to_be_visible()
        playwright.expect(page.locator('#workflow-n')).to_have_count(0)
        page.locator('#studio-edit-model').click()
        playwright.expect(page.locator('#mw-family')).to_have_value('wan2.1')
        # Video is always admitted alone: the switch is on, locked and says why (QA-025).
        playwright.expect(page.locator('#mw-alone')).to_be_checked();playwright.expect(page.locator('#mw-alone')).to_be_disabled()
        playwright.expect(page.locator('#mw-alone-why')).to_have_text('Video models always keep their GPUs to themselves.')
        page.locator('#mw-ram').fill('18000');page.get_by_role('button',name='Save model',exact=True).click()
        playwright.expect(page.locator('#model-dialog')).not_to_be_visible()
        assert saved[-1]['settings']['sdcpp']['mode']=='vid_gen'
        assert saved[-1]['settings']['alone'] is True
        assert saved[-1]['settings']['sdcpp']['host_ram_mib']==18000
        # Image weights in video setup keep a video family and say what they are (QA-024).
        image_weights=True
        page.locator('#mw-new').click()
        playwright.expect(page.locator('#mw-family')).to_have_value('wan2.1')
        page.locator('#mw-diffusion').evaluate("(input) => { input.value = '/compute/sd_xl_base_1.0.safetensors'; input.dispatchEvent(new Event('change', {bubbles: true})); }")
        playwright.expect(page.locator('#mw-check')).to_contain_text('These weights are SDXL, which is not a video model')
        playwright.expect(page.locator('#mw-family')).to_have_value('wan2.1')
        playwright.expect(page.locator('#mw-alone')).to_be_disabled()
        assert not errors,errors
        browser.close()


def test_an_empty_video_catalogue_says_to_add_a_video_model(desk):
    """The empty catalogue names the page's own media (QA-026)."""
    server,_,_=desk;sign_in(server,'jordan')
    def capture(route,path):
        base=path.partition('?')[0]
        if base=='/video-models':
            route.fulfill(content_type='application/json',body=json.dumps({'models':[],'cards':[],'nodes':[{'name':'compute','state':'ready'}],'engines':{'engines':[]}}));return True
        if base=='/mirrorwall-api/workflows':
            route.fulfill(content_type='application/json',body=json.dumps({'models':[],'presets':[],'can_manage':True}));return True
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server,capture=capture)
        page.goto('http://console.test/models/video')
        playwright.expect(page.locator('#studio-models-empty, [id$=models-empty]').first).to_have_text('No video models in this selection. Add a video model to begin.')
        browser.close()


def test_temporary_clips_are_offered_only_when_the_compute_policy_allows_them(desk):
    """jordan may generate without logs, but the node's policy does not allow it, so the studio does not offer it."""
    browser,_,_=desk
    sign_in(browser,'jordan')
    assert browser.get('/video-api/state').json()['can_unlogged'] is False
