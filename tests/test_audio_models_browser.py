"""Live Audio studio contracts over isolated apps and node-scoped mock inventories."""

import copy
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from test_console_browser import browser_context, playwright
from test_ui_consistency import MEASURE
from test_weightroom import desk, sign_in  # noqa: F401


def model(name, node, family='kokoro', kind='speech', speed=1):
    path = f'/audio/{node}/{name}/{name}.gguf'
    return dict(name=name, engine='audiocpp', gpus=['A'], alone=kind == 'music', order=30,
                flags={'model':path, 'ctx-size':0}, loaded='ready', size=1024,
                running=0, enabled=True, future_telemetry='not a setting',
                audiocpp=dict(family=family, kind=kind, path=path, host_ram_mib=8192,
                    defaults={'speed':speed, 'future_option':4} if kind == 'speech' else {'bpm':120},
                    files=['tokens.bin'], session_options={'future_session':True},
                    limits={'max_seconds':600,'max_takes':8,'max_text_chars':20000,'max_audio_mib':32,'future_limit':7}))


@pytest.fixture
def studio(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    nodes = [dict(name=name, state='ready', accepting_work=True) for name in ('atlas','nova','scout')]
    inventories = {'atlas':[model('speaker','atlas'),model('song','atlas','ace_step','music')],
                   'nova':[model('speaker','nova',speed=2)], 'scout':[]}
    families = [dict(family='kokoro',name='Kokoro',kinds=['speech'],options={'request':[
                    dict(name='speed',type='float',min=.25,max=4,description='Speech speed multiplier.')]}),
                dict(family='ace_step',name='ACE-Step',kinds=['music'],options={'request':[
                    dict(name='bpm',type='int',min=20,max=300)]})]
    files = [dict(name=node,answering=True,files=[file for row in rows for file in [
        dict(path=row['flags']['model'],tier='cache',facts={'what':'audio','family':row['audiocpp']['family']}),
        dict(path=row['flags']['model'].rsplit('/',1)[0]+'/tokens.bin',tier='cache',facts={})]])
        for node,rows in inventories.items()]
    files.append(dict(name='vault',answering=True,files=[dict(path='/storage/voice.gguf',tier='storage',facts={'what':'audio','family':'kokoro'})]))
    state = dict(inventories=inventories, files=files, failed=set(), posts=[], requests=[], install=False, file_error=False)

    def capture(route, path):
        parsed = urlsplit(path)
        if parsed.path == '/models/audio':
            html = server.get(path).text
            html = re.sub(r'(<script type="application/json" id="am-nodes">).*?(</script>)',
                          lambda match:match[1]+json.dumps(nodes)+match[2],html,flags=re.S)
            html = re.sub(r'(<select id="am-node"[^>]*>).*?(</select>)',
                          lambda match:match[1]+'<option value="">All nodes</option>'+''.join(f'<option>{node["name"]}</option>' for node in nodes)+match[2],html,flags=re.S)
            route.fulfill(content_type='text/html',body=html)
            return True
        if parsed.path == '/model-files':
            route.fulfill(status=503 if state['file_error'] else 200,content_type='application/json',
                          body=json.dumps({'error':{'message':'File scan unavailable'}} if state['file_error'] else {'nodes':state['files']}))
            return True
        if parsed.path == '/soundroom-models':
            node = parse_qs(parsed.query).get('node',[''])[0]
            state['requests'].append(node)
            assert node in state['inventories'], 'Every request must explicitly name its compute node.'
            if node in state['failed']:
                route.fulfill(status=503,content_type='application/json',body='{"error":{"message":"Offline"}}')
                return True
            if route.request.method == 'POST':
                body = route.request.post_data_json
                state['posts'].append((node,copy.deepcopy(body)))
                assert body['settings'].keys() <= {'engine','flags','audiocpp','gpus','alone','allow_split','adapters','min_context','order'}
                row = next((one for one in inventories[node] if one['name'] == body['name']),None)
                if row:
                    row.update(body['settings'])
                else:
                    inventories[node].append(dict(name=body['name'],**body['settings']))
                result = {'kind':'model-install','id':'audio-fixture'} if state['install'] else {'name':body['name']}
            else:
                result = dict(models=copy.deepcopy(inventories[node]),cards=['A','B'],catalogue={'families':families},
                              engines={'engines':[dict(id='audiocpp',available=True)]})
            route.fulfill(content_type='application/json',body=json.dumps(result))
            return True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine,server,capture=capture)
        errors = []
        page.on('pageerror',lambda error:errors.append(str(error)))
        yield page,state
        browser.close()
        assert not errors


def test_model_and_node_drafts_preserve_unknown_settings_and_save_to_selected_node(studio):
    page,state = studio
    page.goto('http://console.test/models/audio?model=speaker')
    playwright.expect(page.locator('#am-total-models')).to_have_text('2')
    playwright.expect(page.locator('#am-total-installations')).to_have_text('3')
    page.locator('[data-section="output"]').click()
    page.locator('#am-option-speed').fill('1.5')
    page.locator('[aria-label="Configure speaker on nova: Loaded"]').click()
    playwright.expect(page.locator('#am-option-speed')).to_have_value('2')
    page.locator('[aria-label="Configure speaker on atlas: Loaded"]').click()
    playwright.expect(page.locator('#am-option-speed')).to_have_value('1.5')
    page.locator('[data-section="advanced"]').click()
    page.locator('#am-session').fill('{unfinished')
    page.locator('#am-profiles [data-model="song"] [data-identity]').click()
    page.locator('#am-profiles [data-model="speaker"] [data-identity]').click()
    playwright.expect(page.locator('#am-session')).to_have_value('{unfinished')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-message')).to_contain_text('JSON')
    assert not state['posts']
    page.locator('#am-session').fill('{"future_session":true}')
    page.locator('[data-section="hardware"]').click()
    page.get_by_text('CPU only',exact=True).click()
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    node,body = state['posts'][0]
    assert node == 'atlas' and not body['new']
    assert body['settings']['gpus'] == 0
    assert body['settings']['order'] == 30
    assert body['settings']['audiocpp']['defaults'] == {'speed':1.5,'future_option':4}
    assert body['settings']['audiocpp']['limits']['future_limit'] == 7
    assert body['settings']['audiocpp']['files'] == ['tokens.bin']
    assert state['inventories']['nova'][0]['audiocpp']['defaults']['speed'] == 2
    page.reload()
    page.locator('[data-section="output"]').click()
    playwright.expect(page.locator('#am-option-speed')).to_have_value('1.5')


def test_filters_clear_empty_nodes_and_add_from_remote_storage(studio):
    page,state = studio
    page.goto('http://console.test/models/audio')
    page.locator('[data-audio-task="music"]').click()
    playwright.expect(page.locator('#am-profiles [data-model]')).to_have_count(1)
    page.locator('#am-search').fill('no-match')
    playwright.expect(page.locator('#am-profiles')).to_contain_text('No models match')
    page.locator('#am-clear').click()
    page.locator('#am-node').select_option('scout')
    playwright.expect(page.locator('#am-profiles [data-model]')).to_have_count(0)
    page.locator('#am-add').click()
    page.locator('#am-file').select_option(label='/storage/voice.gguf · vault · storage')
    page.locator('#am-name').fill('new-voice')
    page.locator('[data-section="hardware"]').click()
    playwright.expect(page.locator('#am-target-node')).to_have_value('scout')
    page.get_by_text('CPU only',exact=True).click()
    page.locator('[data-section="output"]').click()
    page.locator('#am-seconds').fill('0')
    page.locator('[data-section="files"]').click()
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#panel-output')).to_be_visible()
    assert not state['posts']
    page.locator('#am-seconds').fill('100')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    node,body = state['posts'][0]
    assert node == 'scout' and body['new'] and body['source_machine'] == 'vault'
    assert body['settings']['flags']['model'] == '/storage/voice.gguf'
    playwright.expect(page.locator('#am-profiles [data-model="new-voice"]')).to_be_visible()


def test_retry_retains_invalid_drafts_and_marks_last_known_offline_inventory(studio):
    page,state = studio
    page.goto('http://console.test/models/audio?model=speaker')
    page.locator('[data-section="advanced"]').click()
    page.locator('#am-defaults').fill('{unfinished')
    state['failed'].add('atlas')
    state['file_error'] = True
    page.locator('#am-retry').evaluate('button => button.click()')
    playwright.expect(page.locator('#am-load-error')).to_contain_text('atlas')
    playwright.expect(page.locator('#am-load-error')).to_contain_text('File scan unavailable')
    playwright.expect(page.locator('#am-defaults')).to_have_value('{unfinished')
    playwright.expect(page.locator('#am-submit')).to_be_disabled()
    page.locator('#am-availability').select_option('offline')
    playwright.expect(page.locator('#am-profiles [data-model]')).to_have_count(2)
    state['failed'].clear(); state['file_error'] = False
    page.locator('#am-retry').click()
    playwright.expect(page.locator('#am-load-error')).not_to_be_visible()
    playwright.expect(page.locator('#am-defaults')).to_have_value('{unfinished')
    playwright.expect(page.locator('#am-submit')).to_be_enabled()
    page.locator('#am-reset').click()
    playwright.expect(page.locator('#am-defaults')).to_have_value('{\n  "future_option": 4\n}')


def test_existing_companion_paths_survive_an_unavailable_file_scan(studio):
    page,state = studio
    state['file_error'] = True
    page.goto('http://console.test/models/audio?model=speaker')
    playwright.expect(page.locator('#am-name')).to_have_value('speaker')
    playwright.expect(page.locator('#am-companions option:checked')).to_have_text('tokens.bin')
    page.locator('[data-section="hardware"]').click()
    page.get_by_text('CPU only',exact=True).click()
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    assert state['posts'][0][1]['settings']['audiocpp']['files'] == ['tokens.bin']


@pytest.mark.parametrize('suffix,facts',[
    ('gguf',{}),
    ('gguf',{'what':'model','architecture':'audiocpp'}),
    ('safetensors',{'what':'image','family':'sdxl'}),
    ('pt',{'what':'audio','family':'kokoro'}),
])
def test_managed_cache_and_storage_weights_do_not_require_audio_classification(studio,suffix,facts):
    page,state = studio
    for node,tier in [('atlas','cache'),('vault','storage')]:
        source=next(one for one in state['files'] if one['name'] == node)
        source['files'].extend([
            dict(path=f'/custom/{tier}/voice.{suffix}',tier=tier,facts=facts),
            dict(path=f'/custom/{tier}/moving.{suffix}',tier=tier,facts=facts,moving=True),
            dict(path=f'/custom/{tier}/config.json',tier=tier,facts={'what':'audio'}),
        ])
    page.goto('http://console.test/models/audio?new=1')
    for index,(tier,node) in enumerate([('cache','atlas'),('storage','vault')]):
        path=f'/custom/{tier}/voice.{suffix}'
        option=page.locator('#am-file option').filter(has_text=f'{path} · {node} · {tier}')
        playwright.expect(option).to_have_count(1)
        page.locator('#am-file').select_option(label=f'{path} · {node} · {tier}')
        if facts.get('what') == 'audio':
            playwright.expect(page.locator('#am-family')).to_have_value('kokoro')
        else:
            if index == 0:
                playwright.expect(page.locator('#am-family')).to_have_value('')
            playwright.expect(page.locator('#am-family option[value="sdxl"]')).to_have_count(0)
            page.locator('#am-family').select_option('kokoro')
        playwright.expect(page.locator('#am-root')).to_have_value(f'/custom/{tier}')
    playwright.expect(page.locator('#am-file option').filter(has_text='/moving.')).to_have_count(0)
    playwright.expect(page.locator('#am-file option').filter(has_text='/config.json')).to_have_count(0)
    page.locator('#am-name').fill('stored-voice')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    node,body=state['posts'][0]
    assert node == 'atlas'
    assert body['source_machine'] == 'vault'
    assert body['settings']['flags']['model'] == f'/custom/storage/voice.{suffix}'
    assert body['settings']['audiocpp']['family'] == 'kokoro'


@pytest.mark.parametrize('width',[1440,768,390,320])
def test_studio_sections_fit_both_appearances_and_have_accessible_controls(studio,width):
    page,state = studio
    page.set_viewport_size({'width':width,'height':1100})
    page.goto('http://console.test/models/audio?model=speaker')
    playwright.expect(page.locator('#am-name')).to_have_value('speaker')
    for theme in ('light','dark'):
        page.evaluate('(theme) => {document.documentElement.dataset.bsTheme=theme}',theme)
        for section in ('files','hardware','output','advanced'):
            page.locator(f'[data-section="{section}"]').click()
            result = page.evaluate(MEASURE)
            assert not result['overflow'],(width,theme,section)
            assert not result['namelessFields']
            assert not result['invalidControls']
            assert not result['mismatches']
            assert page.evaluate('''() => {const ids=[...document.querySelectorAll('[id]')].map(e=>e.id);return ids.length===new Set(ids).size}''')
    page.locator('#am-search').fill('absent')
    assert not page.evaluate(MEASURE)['overflow']


@pytest.mark.parametrize('offline',[False,True])
def test_initial_empty_and_unreachable_fleets_remain_clear_and_recoverable(studio,offline):
    page,state = studio
    for rows in state['inventories'].values():
        rows.clear()
    if offline:
        state['failed'].update(state['inventories'])
    page.goto('http://console.test/models/audio?new=1')
    if offline:
        playwright.expect(page.locator('#am-load-error')).to_be_visible()
        playwright.expect(page.locator('#am-profiles')).to_contain_text('Inventory is unavailable')
        playwright.expect(page.locator('#am-total-models')).to_have_text('—')
        playwright.expect(page.locator('#am-submit')).to_be_disabled()
        state['failed'].clear()
        page.locator('#am-retry').click()
    playwright.expect(page.locator('#am-profiles')).to_contain_text('No audio models yet')
    playwright.expect(page.locator('#am-submit')).to_be_enabled()
    page.locator('#am-file').select_option(label='/storage/voice.gguf · vault · storage')
    page.locator('#am-name').fill('invalid name with spaces')
    page.locator('#am-submit').click()
    assert not state['posts']
    assert page.locator('#am-name').evaluate('input => !input.checkValidity()')
    page.locator('#am-name').fill('first-voice')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-profiles [data-model="first-voice"]')).to_be_visible()
    playwright.expect(page.locator('#am-profiles')).not_to_contain_text('No audio models yet')
