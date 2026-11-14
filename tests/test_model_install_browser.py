"""Models pills and cache/storage pickers in Chromium; no real model loads."""
import json
import re
from urllib.parse import urlsplit

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


@pytest.mark.parametrize('width',[1440,390,320])
def test_image_and_audio_add_use_remote_storage_choices(desk,width):
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
            value = {'models':[],'cards':['CUDA0'],'nodes':[{'name':'compute','state':'ready'},{'name':'rig','state':'ready'},
                {'name':'retired','state':'offline','accepting_work':False,'lifecycle':'disabled'}],'engines':{'engines':[{'id':'sdcpp','available':True}]}}
        if base == '/mirrorwall-models/check': value = {'ready':False,'slots':[],'loras':[],'get_diffusion':[]}
        if base == '/soundroom-installations': value = {'installations':[{'id':'fixture','name':'song','machine':'compute','state':'complete'}]}
        if base == '/soundroom-models' and route.request.method == 'GET':
            value = {'models':[],'cards':['CUDA0'],'catalogue':{'families':[{'family':'ace_step','name':'ACE','kinds':['music'],
                'licence':'MIT','commercial':'yes','packages':[{'id':'ace','name':'ACE','folder':'/cache/audio/ace',
                    'files':{'ace.gguf':'/cache/audio/ace/ace.gguf'},'installed':None}]}]}}
        if base in ('/mirrorwall-models','/soundroom-models') and route.request.method == 'POST':
            if base == '/mirrorwall-models' and json.loads(route.request.post_data)['name'] == 'bad name':
                route.fulfill(status=409,content_type='application/json',body=json.dumps({'error':{
                    'message':'Use 1–128 letters, numbers, dots, underscores or hyphens for the model name.'}}))
                return True
            sent.append(json.loads(route.request.post_data))
            value={'jobs':[{'kind':'model-install','id':'primary'},{'kind':'model-install','id':'rig'}]} if base=='/mirrorwall-models' else {'kind':'model-install','id':'fixture'}
            route.fulfill(status=202,content_type='application/json',body=json.dumps(value))
            return True
        if value is not None:
            route.fulfill(content_type='application/json',body=json.dumps(value)); return True
    with playwright.sync_playwright() as driver:
        browser,page = browser_context(driver,server,capture=capture)
        page.set_viewport_size({'width':width,'height':1100})
        errors=[]; page.on('pageerror',lambda error: errors.append(str(error)))
        page.goto('http://console.test/models/images?new=1')
        playwright.expect(page.locator('#mw-diffusion option')).to_have_count(2)
        for slot in ('diffusion','text_encoder','vae','clip_l','clip_g','upscaler'):
            option=page.locator('#mw-'+slot+' option').nth(1)
            playwright.expect(option).to_have_text('front · image · model.safetensors')
            playwright.expect(option).to_have_attribute('value','/store/image/model.safetensors')
            playwright.expect(option).to_have_attribute('title','/store/image/model.safetensors')
        page.locator('#mw-diffusion').select_option(label='front · image · model.safetensors')
        page.locator('#mw-name').fill('picture')
        targets=page.locator('#mw-install-targets')
        playwright.expect(targets.locator('input[value=retired]')).to_be_disabled()
        targets.locator('.form-selectgroup-item').filter(has_text='compute').click()
        page.get_by_role('button',name='Save model',exact=True).click()
        playwright.expect(page.locator('#mw-message')).to_contain_text('Choose at least one destination node')
        assert not sent
        targets.locator('.form-selectgroup-item').filter(has_text='compute').click()
        targets.locator('.form-selectgroup-item').filter(has_text='rig').click()
        page.locator('#mw-name').fill('bad name')
        dialog=page.locator('#model-dialog')
        dialog.evaluate('el => {el.scrollTop=el.scrollHeight;}')
        page.get_by_role('button',name='Save model',exact=True).click()
        notice=page.locator('#mw-message')
        playwright.expect(notice).to_contain_text('Use 1–128 letters')
        assert notice.evaluate("el => getComputedStyle(el).backgroundColor") == dialog.evaluate("el => getComputedStyle(el).backgroundColor")
        assert dialog.evaluate('el => el.scrollTop > el.clientHeight')
        for fraction in (1,.5,.25):
            dialog.evaluate('(el,fraction) => {el.scrollTop=el.scrollHeight*fraction;}',fraction)
            playwright.expect(notice).to_be_in_viewport()
            assert notice.evaluate('''el => {
                const box=el.getBoundingClientRect(),dialog=el.closest('dialog').getBoundingClientRect();
                return Math.abs(box.top-dialog.top-1)<2 && box.bottom<=dialog.bottom &&
                    el.contains(document.elementFromPoint(box.left+box.width/2,box.top+box.height/2));
            }''')
        playwright.expect(page.locator('#mw-name')).to_have_value('bad name')
        page.locator('#mw-name').fill('picture')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.get_by_role('button',name='Save model',exact=True).click()
        page.wait_for_url('**/models?kind=image&install=primary&install=rig')
        assert sent[-1]['source_machine']=='front' and sent[-1]['settings']['flags']['model']=='/store/image/model.safetensors'
        assert sent[-1]['machines']==['compute','rig']
        page.goto('http://console.test/models/audio?new=1')
        playwright.expect(page.locator('#am-file option[data-path="/store/audio/ace/ace.gguf"]')).to_have_count(1)
        page.locator('#am-file').select_option(page.locator('#am-file option[data-path="/store/audio/ace/ace.gguf"]').get_attribute('value'))
        page.locator('#am-family').select_option('ace_step')
        assert page.get_by_role('heading',name='Catalogue',exact=True).count() == 0
        assert page.get_by_role('button',name='Install',exact=True).count() == 0
        page.locator('#am-name').fill('song')
        page.get_by_role('button',name='Add model',exact=True).click()
        page.wait_for_timeout(200)
        assert not page.locator('#am-message').is_visible(), page.locator('#am-message').text_content()
        assert len(sent) == 2, page.locator('#am-form').evaluate('form => [...form.elements].filter(one => !one.checkValidity()).map(one => ({id:one.id,value:one.value,message:one.validationMessage}))')
        playwright.expect(page.locator('#am-install-text')).to_contain_text('song on compute: Installed')
        assert urlsplit(page.url).path=='/models/audio'
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert sent[-1]['source_machine']=='front' and sent[-1]['settings']['flags']['model']=='/store/audio/ace/ace.gguf'
        assert not errors
        browser.close()


def test_node_pills_ticked_nodes_and_the_name_a_file_gives(desk, tmp_path):
    server, _, _ = desk
    file = gguf(tmp_path / 'Tiny-Model.Q4.gguf', general__architecture='qwen', qwen__context_length=8192)
    Models(str(tmp_path / 'loadcoach.db')).put('available', {'flags':{'model':file,'ctx-size':2048},'gpus':0})
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/models')
        nodes = page.locator('nav[aria-label="Compute node"]')
        playwright.expect(nodes.get_by_role('link', name='All', exact=True)).to_have_attribute('aria-current', 'page')
        nodes.get_by_role('link', name='compute', exact=True).click()
        playwright.expect(nodes.get_by_role('link', name='compute', exact=True)).to_have_attribute('aria-current', 'page')
        assert '?node=compute' in page.url
        playwright.expect(page.locator('nav[aria-label="Model type"]').get_by_role('link', name='Image', exact=True)).to_have_attribute('href', '/models?kind=image&node=compute')
        # Add a model is open, and ticks the node on view; there is no separate node browser.
        added = page.locator('#add-model')
        playwright.expect(added.locator('[data-model-node]')).to_be_checked()
        header = added.locator('.card-header')
        playwright.expect(header.locator('#model-nodes')).to_be_visible()
        assert header.locator('legend').count() == 0
        assert added.locator('#model-nodes').evaluate('el => new FormData(el.closest("form")).getAll("node")') == ['compute']
        edge = header.locator('.card-actions').evaluate('el => el.getBoundingClientRect().right')
        assert abs(added.locator('#model-nodes').bounding_box()['x'] + added.locator('#model-nodes').bounding_box()['width'] - edge) < 2
        assert added.get_by_role('button', name='Browse node files', exact=True).count() == 0
        assert added.locator('#projector option').first.inner_text() == 'None'
        # Files are labelled by where they are, then their own name; the full path is only the title.
        option = added.locator('#file option').nth(1)
        assert option.inner_text().startswith('compute · ') and 'Tiny-Model.Q4.gguf' in option.inner_text() and '/' not in option.inner_text()
        assert option.get_attribute('title') == file
        # Choosing a file names the model after it, until a name of one's own is typed.
        added.locator('#file').select_option(index=1)
        playwright.expect(added.locator('#name')).to_have_value('tiny-model.q4')
        playwright.expect(added.locator('[data-trained-note]')).to_have_text('Trained context: 8,192 tokens.')
        added.locator('#name').fill('mine')
        added.locator('#file').select_option(index=0); added.locator('#file').select_option(index=1)
        playwright.expect(added.locator('#name')).to_have_value('mine')
        # With no node ticked Add cannot be used, and why is said beneath the fields, not between them.
        added.locator('#model-nodes .form-selectgroup-label').click()  # the box itself is drawn under its label
        playwright.expect(added.locator('[data-model-node]')).not_to_be_checked()
        playwright.expect(added.locator('[data-file-warning]')).to_have_text('Choose at least one compute node.')
        playwright.expect(added.locator('[data-file-submit]')).to_be_disabled()
        fields = [added.locator(selector).bounding_box() for selector in ('#name', '#projector', '#min-context', '#ctx')]
        assert len({round(box['y'] + box['height']) for box in fields}) == 1
        assert added.locator('[data-file-warning]').bounding_box()['y'] >= max(box['y'] + box['height'] for box in fields)
        assert added.locator('[data-trained-note]').bounding_box()['y'] >= max(box['y'] + box['height'] for box in fields)
        assert not errors
        browser.close()


def test_add_a_model_needs_a_compute_node_and_asks_for_every_ticked_one_together(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    file = gguf(tmp_path / 'Tiny-Model.Q4.gguf', general__architecture='tiny')
    # The picker's value, as the page writes it and a browser sends it back.
    chosen = {'prepare_files':'yes', 'artifact':json.dumps({'machine':'compute', 'path':file}), 'name':'tiny', 'ctx':'2048'}
    none = browser.post('/models', data=chosen)
    assert none.status_code == 400 and 'Choose at least one compute node.' in none.text
    picker = re.search(r'<select id="file".*?</select>', none.text, re.S)[0]
    assert 'data-model-node checked' not in none.text
    selected = [option for option in re.findall(r'<option[^>]*>', picker) if re.search(r'\sselected[\s>]', option)]
    assert len(selected) == 1 and f'title="{file}"' in selected[0]  # the chosen file stays chosen
    # Turnstile is asked once for the ticked nodes, each named once; it refuses all of them or none.
    both = browser.post('/models', data={**chosen, 'node':['compute', 'compute'], 'shown_node':'compute'})
    assert both.status_code == 400 and 'compute: llama.cpp is not available on compute. Nothing was started.' in both.text
    assert 'data-model-node checked' in both.text and 'value="tiny"' in both.text
    # The Embedding model box (llama.cpp's --embedding) stays ticked when the add is refused.
    embedder = browser.post('/models', data={**chosen, 'embedding':'yes', 'node':'compute', 'shown_node':'compute'})
    assert re.search(r'<input id="model-embedding" name="embedding" value="yes" type="checkbox"[^>]* checked>', embedder.text)
