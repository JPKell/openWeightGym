"""Model studio uses real authenticated storage, independent drafts and immutable revisions."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from loadcoach.batches import Batches
from loadcoach.models import Models
from mirrorwall.store import Store
from mirrorwall import model
from test_console_browser import browser_context, playwright
from test_weightroom import desk, gguf, sign_in


@pytest.fixture
def studio(desk,tmp_path):
    server,wire,users=desk
    library=Models(str(tmp_path/'loadcoach.db'))
    file=gguf(tmp_path/'image.gguf',general__architecture='fixture')
    for name,family in [('sketch','sdxl'),('qwen','qwen-image'),('qwen21','qwen-image-2.1')]:
        library.put(name,{'engine':'sdcpp','flags':{'model':file,'ctx-size':0},'gpus':0,'alone':True,
            'sdcpp':{'family':family,'components':{},'defaults':{'width':512,'height':512},
                     'limits':{'max_references':1 if family=='sdxl' else 0}}})
    async def catalogue(user):
        return [{'id':name,'model_id':library.identity(name),'image_workflow':{
            'family':library.get(name)['sdcpp']['family'],'exclusive':True,'defaults':model.defaults(library.get(name)),
            'limits':model.limits(library.get(name)),'reference_megapixels':1,'loras':{}}}
            for name in ('sketch','qwen','qwen21')]
    wire.front.state.mirrorwall.catalogue=catalogue
    sign_in(server,'jordan')
    return server,wire,users,library


@pytest.mark.parametrize('width',[1440,390])
def test_node_dropdown_filters_all_copies_and_edits_the_correct_node(studio,width):
    server,_,_,library=studio
    def copy(name,node,identity,ram=8192):
        original=deepcopy(library.get(name))
        return {**original,'name':'remote-qwen' if name=='qwen' else name,'node':node,
            'model_id':node+'-'+name,'workflow_model':name,'workflow_model_id':identity,
            'gpus':['C0' if node=='compute' else 'R1'],'flags':{'model':'/'+node+'/'+name+'.safetensors','ctx-size':0},
            'sdcpp':{**original['sdcpp'],'host_ram_mib':ram}}
    copies=[copy('sketch','compute',library.identity('sketch')),
            copy('sketch','rig',library.identity('sketch'),16384),copy('qwen','rig',library.identity('qwen'))]
    saved=[]; fail_next=[]
    def capture(route,path):
        base=path.partition('?')[0]
        if base=='/mirrorwall-models' and route.request.method=='GET':
            if fail_next:
                fail_next.pop()
                route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':{'message':'Inventory temporarily unavailable.'}}));return True
            route.fulfill(content_type='application/json',body=json.dumps({'models':copies,'cards':['C0'],
                'node_cards':{'compute':['C0'],'rig':['R1'],'empty':[]},'default_node':'compute',
                'nodes':[{'name':n,'state':'ready'} for n in ('compute','rig','empty')],
                'engines':{'engines':[{'id':'sdcpp','available':True}]}}));return True
        if base=='/mirrorwall-models' and route.request.method=='POST':
            body=route.request.post_data_json
            saved.append((path,body))
            row=next(c for c in copies if c['node']=='rig' and c['name']==body['name'])
            row.update(body['settings'])
            route.fulfill(content_type='application/json',body=json.dumps({'name':body['name']}));return True
        if base=='/mirrorwall-models/check':
            route.fulfill(content_type='application/json',body=json.dumps({'ready':True,'slots':[
                {'slot':'diffusion','need':'required','status':'ok','message':'Fixture'}],'loras':[],'get_diffusion':[]}));return True
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server,capture=capture)
        page.set_viewport_size({'width':width,'height':1100})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/models/images')
        dropdown=page.get_by_label('Compute node',exact=True)
        playwright.expect(dropdown).to_have_value('')
        playwright.expect(dropdown.locator('option').first).to_have_text('All')
        playwright.expect(page.locator('#studio-model-list [data-model]')).to_have_count(3)
        assert page.get_by_text('One setup, many workflows',exact=True).count()==0
        assert page.get_by_role('button',name='Browse node',exact=True).count()==0
        page.evaluate('window.nodeFilterSentinel=true')
        page.locator('#studio-edit-model').click()
        page.locator('#mw-ram').fill('12000')
        page.keyboard.press('Escape')
        page.locator('#workflow-name').fill('Keep this draft')
        dropdown.select_option('rig')
        playwright.expect(page.locator('#studio-model-list [data-model]')).to_have_count(2)
        assert page.evaluate('window.nodeFilterSentinel')
        page.locator('#studio-model-list [data-model=sketch][data-node=rig]').click()
        page.locator('#studio-edit-model').click()
        playwright.expect(page.locator('#mw-ram')).to_have_value('16384')
        playwright.expect(page.locator('#mw-diffusion')).to_have_value('/rig/sketch.safetensors')
        page.locator('#mw-ram').fill('18000')
        page.get_by_role('button',name='Save model',exact=True).click()
        playwright.expect(page.locator('#model-dialog')).not_to_be_visible()
        assert saved[-1][0]=='/mirrorwall-models?node=rig'
        assert saved[-1][1]['settings']['gpus']==['R1']
        assert saved[-1][1]['settings']['flags']['model']=='/rig/sketch.safetensors'
        dropdown.select_option('')
        page.locator('#studio-model-list [data-model=sketch][data-node=compute]').click()
        playwright.expect(page.locator('#workflow-name')).to_have_value('Keep this draft')
        page.locator('#studio-edit-model').click()
        playwright.expect(page.locator('#mw-ram')).to_have_value('12000')
        page.keyboard.press('Escape')
        fail_next.append(True)
        dropdown.select_option('empty')
        playwright.expect(page.locator('#studio-model-list [data-model]')).to_have_count(0)
        playwright.expect(page.locator('#studio-model-name')).to_have_text('Choose an image model')
        playwright.expect(page.locator('#studio-edit-model')).to_be_disabled()
        playwright.expect(page.locator('#studio-error')).to_contain_text('Inventory temporarily unavailable.')
        dropdown.select_option('')
        playwright.expect(page.locator('#studio-error')).not_to_be_visible()
        playwright.expect(page.locator('#mw-new')).to_be_enabled()
        page.locator('#studio-model-list [data-model=remote-qwen]').click()
        page.reload()
        playwright.expect(page.locator('#studio-model-name')).to_have_text('remote-qwen')
        page.locator('#workflow-name').fill('Remote alias workflow')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-status')).to_contain_text('Workflow saved.')
        assert server.get('/mirrorwall-api/workflows').json()['presets'][0]['model']=='qwen'
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        browser.close()


def test_revision_migration_ownership_and_stale_edits(tmp_path):
    batches=Batches(str(tmp_path/'accounts.db'))
    store=Store(batches.db,tmp_path)
    row=store.save_preset('alice','Portrait','model','stable-model',{'steps':10})
    store.save_preset('alice','Portrait','model','stable-model',{'steps':20},row['id'],1)
    assert [r['settings']['steps'] for r in store.preset_history('alice',row['id'])]==[20,10]
    assert store.preset_history('bob',row['id']) is None
    with pytest.raises(ValueError,match='changed elsewhere'):
        store.save_preset('alice','Portrait','model','stable-model',{'steps':30},row['id'],1)
    with pytest.raises(ValueError,match='another model identity'):
        store.save_preset('alice','Portrait','model','replacement-model',{},row['id'],2)
    assert len(store.preset_history('alice',row['id']))==2


def test_existing_latest_revision_is_migrated_without_inventing_old_settings(tmp_path):
    from db import migrate
    from mirrorwall.schema import MIRRORWALL
    batches=Batches(str(tmp_path/'accounts.db'))
    migrate(batches.db.database,batches.db,'mirrorwall.front',MIRRORWALL[:2])
    identifier='workflow_'+'a'*24
    with batches.db:
        batches.db.execute('INSERT INTO mirrorwall_presets VALUES (?,?,?,?,?,?,?,?)',
            (identifier,'alice','Legacy',7,'model-id','model',json.dumps({'steps':12}),1))
    store=Store(batches.db,tmp_path)
    assert [row['version'] for row in store.preset_history('alice',identifier)]==[7]
    store.save_preset('alice','Legacy','model','model-id',{'steps':16},identifier,7)
    assert [row['version'] for row in store.preset_history('alice',identifier)]==[8,7]


def test_workflow_history_api_keeps_private_ownership(studio):
    server,_,_,_=studio
    row=server.post('/mirrorwall-api/presets',json={'name':'Portrait','model':'sketch','settings':{'steps':10}}).json()
    response=server.post('/mirrorwall-api/presets',json={'id':row['id'],'version':1,'name':'Portrait','model':'sketch','settings':{'steps':20}})
    assert response.status_code==200,response.text
    history=server.get('/mirrorwall-api/presets/'+row['id']).json()['versions']
    assert [r['settings']['steps'] for r in history]==[20,10]
    sign_in(server,'pat')
    assert server.get('/mirrorwall-api/presets/'+row['id']).status_code==404
    assert server.get('/models/images').status_code==403
    assert server.get('/models/images/workflows').status_code==200


def test_member_edits_a_personal_copy_and_shared_history_follows_model_identity(studio):
    server,wire,_,library=studio
    shared=server.post('/mirrorwall-api/presets',json={'name':'Team portrait','model':'sketch','settings':{'steps':10},'published':True}).json()
    sign_in(server,'pat')
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server)
        page.goto('http://console.test/models/images/workflows?model=sketch')
        playwright.expect(page.locator('#workflow-name')).to_have_value('Team portrait')
        assert page.locator('#studio-edit-model').count()==0
        page.locator('#workflow-steps').fill('16')
        page.get_by_role('button',name='Save personal copy',exact=True).click()
        playwright.expect(page.locator('#studio-status')).to_contain_text('Workflow saved.')
        playwright.expect(page.locator('#studio-count')).to_contain_text('2 workflows')
        browser.close()
    all_rows=server.get('/mirrorwall-api/workflows').json()['presets']
    personal=next(row for row in all_rows if not row['published'])
    assert personal['id']!=shared['id'] and personal['settings']['steps']==16
    assert server.post('/mirrorwall-api/presets',json={**shared,'settings':{'steps':20},'published':True}).status_code==403
    assert len(server.get('/mirrorwall-api/presets/'+shared['id']).json()['versions'])==1
    prior=wire.front.state.mirrorwall.catalogue
    async def replacement(user):
        return [{**row,'model_id':'different-identity'} for row in await prior(user)]
    wire.front.state.mirrorwall.catalogue=replacement
    assert server.get('/mirrorwall-api/presets/'+shared['id']).status_code==404
    assert all(not row['published'] for row in server.get('/mirrorwall-api/workflows').json()['presets'])


@pytest.mark.parametrize('width',[1600,390,320])
def test_live_studio_versions_drafts_helpers_and_same_page_model_setup(studio,tmp_path,width):
    server,_,_,_=studio
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server)
        page.set_viewport_size({'width':width,'height':1100})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/models/images?model=sketch')
        playwright.expect(page.locator('#studio-model-name')).to_have_text('sketch')
        playwright.expect(page.locator('#studio-save')).to_be_enabled()
        page.locator('#workflow-name').fill('Portrait')
        page.locator('#workflow-steps').fill('20')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-version')).to_have_text('Version 1')
        playwright.expect(page.locator('#studio-draft')).to_have_text('Saved')
        page.locator('#workflow-steps').fill('24')
        page.locator('#studio-model-list [data-model=qwen]').click()
        playwright.expect(page.locator('#workflow-negative_prompt')).to_be_visible()
        playwright.expect(page.locator('#workflow-strength')).not_to_be_visible()
        playwright.expect(page.locator('#studio-workflow-helper')).to_contain_text('Qwen2.5-VL')
        page.locator('#workflow-name').fill('Product photos')
        page.locator('#workflow-negative_prompt').fill('blur')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-version')).to_have_text('Version 1')
        page.locator('#studio-model-list [data-model=sketch]').click()
        playwright.expect(page.locator('#workflow-steps')).to_have_value('24')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-version')).to_have_text('Version 2')
        page.locator('summary').filter(has_text='Version history').click()
        page.locator('#studio-history').select_option('1')
        page.locator('#studio-restore').click()
        playwright.expect(page.locator('#workflow-steps')).to_have_value('20')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-version')).to_have_text('Version 3')
        page.locator('#studio-duplicate').click()
        page.locator('#workflow-name').fill('Portrait detail')
        page.locator('#workflow-steps').fill('28')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-version')).to_have_text('Version 1')
        playwright.expect(page.locator('#studio-count')).to_contain_text('2 workflows')
        page.locator('#studio-edit-model').click()
        playwright.expect(page.locator('#model-dialog')).to_be_visible()
        page.locator('#mw-ram').fill('16384')
        page.locator('#mw-add-lora').click()
        page.locator('#mw-lora-rows [data-lora-field=name]').fill('Partial adapter')
        page.keyboard.press('Escape')
        page.locator('#studio-model-list [data-model=qwen21]').click()
        playwright.expect(page.locator('#studio-workflow-helper')).to_contain_text('Qwen3-VL')
        playwright.expect(page.locator('#workflow-width')).to_have_attribute('step','32')
        page.locator('#studio-model-list [data-model=sketch]').click()
        page.locator('#studio-edit-model').click()
        playwright.expect(page.locator('#mw-ram')).to_have_value('16384')
        playwright.expect(page.locator('#mw-lora-rows [data-lora-field=name]')).to_have_value('Partial adapter')
        page.keyboard.press('Escape')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        folder=Path('build/image-studio');folder.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(folder/f'studio-{width}.png'),full_page=True)
        page.reload()
        page.locator('#studio-workflow-list [data-workflow]').filter(has_text='Portrait detail').click()
        playwright.expect(page.locator('#workflow-steps')).to_have_value('28')
        assert not errors,errors
        browser.close()


def test_rejected_workflow_save_keeps_draft_and_is_retryable(studio):
    server,_,_,_=studio
    def capture(route,path):
        if path=='/mirrorwall-api/presets' and route.request.post_data_json['settings']['steps']==101:
            route.fulfill(status=400,content_type='application/json',body=json.dumps({'error':{'message':'Steps exceed model limit.'}}));return True
    with playwright.sync_playwright() as driver:
        browser,page=browser_context(driver,server,capture=capture)
        page.goto('http://console.test/models/images?model=sketch')
        page.locator('#workflow-name').fill('Retry settings')
        page.locator('#workflow-steps').fill('101')
        # Exercise a server-side refusal rather than the browser max constraint.
        page.locator('#workflow-steps').evaluate('e=>e.max=200')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-save-error')).to_contain_text('Steps exceed')
        playwright.expect(page.locator('#workflow-steps')).to_have_value('101')
        page.locator('#workflow-steps').fill('20')
        page.locator('#studio-save').click()
        playwright.expect(page.locator('#studio-version')).to_have_text('Version 1')
        browser.close()
