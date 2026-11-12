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
    state = dict(inventories=inventories, files=files, failed=set(), posts=[], requests=[], install=False, file_error=False,
                 cards={node:['A','B'] for node in inventories}, nodes=nodes, families=families,
                 jobs={}, advance_installs=True, progress_error=False)
    waiting={}

    def capture(route, path):
        parsed = urlsplit(path)
        if parsed.path == '/models/audio':
            html = server.get(path).text
            html = re.sub(r'(<script type="application/json" id="am-nodes">).*?(</script>)',
                          lambda match:match[1]+json.dumps(nodes)+match[2],html,flags=re.S)
            route.fulfill(content_type='text/html',body=html)
            return True
        if parsed.path == '/model-files':
            route.fulfill(status=503 if state['file_error'] else 200,content_type='application/json',
                          body=json.dumps({'error':{'message':'File scan unavailable'}} if state['file_error'] else {'nodes':state['files']}))
            return True
        if parsed.path == '/soundroom-installations':
            jobs=[]
            for number in parse_qs(parsed.query).get('install',[]):
                job=state['jobs'][number]
                if state['advance_installs']:
                    job['state']='complete'
                if job['state']=='complete' and number in waiting:
                    target,body=waiting.pop(number)
                    inventories[target].append(dict(name=body['name'],**copy.deepcopy(body['settings'])))
                jobs.append(job)
            route.fulfill(status=503 if state['progress_error'] else 200,content_type='application/json',
                          body=json.dumps({'error':{'message':'Progress temporarily unavailable'}} if state['progress_error'] else {'installations':jobs}))
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
                if state['install']:
                    jobs=[]
                    for target in body.get('machines',[node]):
                        job=dict(kind='model-install',id=f'audio-{target}-{len(state["posts"])}',name=body['name'],machine=target,state='queued')
                        state['jobs'][job['id']]=job
                        waiting[job['id']]=(target,copy.deepcopy(body))
                        jobs.append(copy.deepcopy(job))
                    result={'jobs':jobs}
                else:
                    row = next((one for one in inventories[node] if one['name'] == body['name']),None)
                    if row:
                        row.update(body['settings'])
                    else:
                        inventories[node].append(dict(name=body['name'],**body['settings']))
                    result={'name':body['name']}
            else:
                result = dict(models=copy.deepcopy(inventories[node]),cards=state['cards'][node],catalogue={'families':families},
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


def test_task_choices_and_add_from_remote_storage_on_selected_node(studio):
    page,state = studio
    page.goto('http://console.test/models/audio')
    page.locator('[data-audio-task="music"]').click()
    playwright.expect(page.locator('#am-profiles [data-model]')).to_have_count(1)
    page.locator('[data-audio-task="sound"]').click()
    playwright.expect(page.locator('#am-profiles')).to_contain_text('No models for this task')
    page.locator('[data-audio-task="all"]').click()
    playwright.expect(page.locator('#am-profiles [data-model]')).to_have_count(2)
    page.locator('#am-add').click()
    page.locator('#am-install-targets input[value="atlas"]').locator('..').click()
    page.locator('#am-install-targets input[value="scout"]').locator('..').click()
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('new-voice')
    page.locator('[data-section="hardware"]').click()
    playwright.expect(page.locator('#am-install-targets input[value="scout"]')).to_be_checked()
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
    _,body = state['posts'][0]
    assert body['machines'] == ['scout'] and body['new'] and body['source_machine'] == 'vault'
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
    playwright.expect(page.locator('#am-profiles [data-model]')).to_have_count(2)
    playwright.expect(page.get_by_role('button',name='Configure speaker on atlas: Node unavailable',exact=True)).to_be_visible()
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


@pytest.mark.parametrize('width',[1440,390])
def test_audio_task_filters_files_and_retains_the_selected_task_and_draft(studio,width):
    page,state=studio
    page.set_viewport_size({'width':width,'height':1100})
    page.goto('http://console.test/models/audio?new=1')
    task=page.locator('#am-kind')
    files=page.locator('#am-file')
    playwright.expect(task).to_have_value('')
    task_box,file_box=task.bounding_box(),files.bounding_box()
    if width==1440:
        assert task_box['x']<file_box['x'] and abs(task_box['y']-file_box['y'])<1
    else:
        assert task_box['y']<file_box['y']
    task.select_option('speech')
    playwright.expect(files.locator('option[data-path]')).to_have_count(3)
    playwright.expect(files.locator('option[data-path*="song"]')).to_have_count(0)
    files.select_option(label='vault · storage')
    page.locator('#am-name').fill('task-first-model')
    task.select_option('music')
    playwright.expect(files).to_have_value('')
    playwright.expect(page.locator('#am-root')).to_have_value('')
    playwright.expect(page.locator('#am-companions option')).to_have_count(0)
    playwright.expect(files.locator('option[data-path]')).to_have_count(1)
    files.select_option(label='atlas · song')
    playwright.expect(page.locator('#am-family')).to_have_value('ace_step')
    page.locator('[data-section="output"]').click()
    page.locator('#am-option-bpm').fill('130')
    page.locator('#am-profiles [data-model="speaker"] [data-identity]').click()
    page.locator('#am-add').click()
    playwright.expect(task).to_have_value('music')
    playwright.expect(files.locator('option:checked')).to_have_text('atlas · song')
    page.locator('#am-retry').evaluate('button=>button.click()')
    playwright.expect(files.locator('option[data-path]')).to_have_count(1)
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    body=state['posts'][0][1]
    assert body['settings']['audiocpp']['kind']=='music'
    assert body['settings']['audiocpp']['defaults']=={'bpm':130}
    assert body['settings']['alone'] is True
    assert body['settings']['flags']['model']=='/audio/atlas/song/song.gguf'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_task_filter_uses_embedded_capabilities_and_keeps_unknown_files_under_all_tasks(studio):
    page,state=studio
    state['files'][-1]['files'].extend([
        dict(path='/storage/effects/model.gguf',tier='storage',facts={'what':'audio','family':'embedded-synth','kinds':['sound','music']}),
        dict(path='/storage/unknown/model.gguf',tier='storage',facts={'what':'audio'}),
    ])
    page.goto('http://console.test/models/audio?new=1')
    task,files=page.locator('#am-kind'),page.locator('#am-file')
    task.select_option('sound')
    playwright.expect(files.locator('option[data-path]')).to_have_count(1)
    files.select_option(label='vault · effects')
    playwright.expect(page.locator('#am-family')).to_have_value('embedded-synth')
    task.select_option('transcription')
    playwright.expect(files).to_have_value('')
    playwright.expect(files.locator('option')).to_have_text('No model files for this task')
    playwright.expect(page.locator('#am-file-note')).to_contain_text('All tasks')
    task.select_option('')
    playwright.expect(files.locator('option[data-path="/storage/unknown/model.gguf"]')).to_have_count(1)
    task.select_option('music')
    playwright.expect(files.locator('option[data-path]')).to_have_count(2)
    files.select_option(label='vault · effects')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    assert state['posts'][0][1]['settings']['audiocpp']['kind']=='music'


def test_all_tasks_preserves_an_existing_models_task(studio):
    page,state=studio
    state['families'][0]['kinds'].append('clone')
    state['inventories']['atlas'][0]['audiocpp']['kind']='clone'
    page.goto('http://console.test/models/audio?model=speaker')
    playwright.expect(page.locator('#am-kind')).to_have_value('clone')
    page.locator('#am-kind').select_option('')
    playwright.expect(page.locator('#am-file option:checked')).to_have_text('atlas · speaker')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    assert state['posts'][0][1]['settings']['audiocpp']['kind']=='clone'


def test_file_capabilities_limit_the_tasks_of_a_broader_runtime_family(studio):
    page,state=studio
    state['families'][0]['kinds'].append('clone')
    state['files'][-1]['files'][0]['facts']['kinds']=['clone']
    page.goto('http://console.test/models/audio?new=1')
    page.locator('#am-kind').select_option('speech')
    playwright.expect(page.locator('#am-file option[data-machine="vault"]')).to_have_count(0)
    page.locator('#am-kind').select_option('')
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    assert state['posts'][0][1]['settings']['audiocpp']['kind']=='clone'


def test_new_model_destination_uses_cpu_when_target_has_no_audio_gpus(studio):
    page,state=studio
    state['cards']['scout']=[]
    page.goto('http://console.test/models/audio?new=1')
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('cpu-destination')
    playwright.expect(page.get_by_role('group',name='Install on compute nodes')).to_be_visible()
    playwright.expect(page.locator('#panel-files')).to_be_visible()
    page.locator('#am-install-targets input[value="scout"]').locator('..').click()
    page.locator('#am-install-targets input[value="atlas"]').locator('..').click()
    playwright.expect(page.locator('#am-file option:checked')).to_have_text('vault · storage')
    playwright.expect(page.locator('#am-name')).to_have_value('cpu-destination')
    page.locator('[data-section="hardware"]').click()
    playwright.expect(page.locator('[data-hardware-mode][value="cpu"]')).to_be_checked()
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    node,body=state['posts'][0]
    assert body['machines']==['scout'] and body['settings']['gpus']==0


def test_multiple_node_pills_retain_drafts_and_track_every_install(studio):
    page,state=studio
    state['install']=True
    state['nodes'][2]['accepting_work']=False
    page.goto('http://console.test/models/audio?new=1')
    targets=page.get_by_role('group',name='Install on compute nodes')
    playwright.expect(targets.locator('input[value="scout"]')).to_be_disabled()
    targets.locator('input[value="nova"]').locator('..').click()
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('fleet-voice')
    page.locator('#am-profiles [data-model="speaker"] [data-identity]').click()
    page.locator('[data-section="output"]').click()
    page.locator('#am-option-speed').fill('1.75')
    playwright.expect(targets.locator('input[value="atlas"]')).to_be_disabled()
    page.locator('#am-add').click()
    playwright.expect(page.locator('#am-name')).to_have_value('fleet-voice')
    for node in ('atlas','nova'):
        playwright.expect(targets.locator(f'input[value="{node}"]')).to_be_checked()
    page.locator('#am-retry').evaluate('button=>button.click()')
    playwright.expect(page.locator('#am-submit')).to_have_text('Install on 2 nodes')
    playwright.expect(page.locator('#am-save-summary')).to_have_text('fleet-voice → atlas, nova')
    dialogs=[]
    page.on('dialog',lambda dialog:(dialogs.append(dialog.type),dialog.dismiss()))
    page.evaluate('window.audioStudioDocument=true')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-install-text')).to_contain_text('fleet-voice on atlas: Installed')
    playwright.expect(page.locator('#am-install-text')).to_contain_text('fleet-voice on nova: Installed')
    playwright.expect(page.locator('#am-profiles [data-model="fleet-voice"] [data-placements] button')).to_have_count(2)
    playwright.expect(page.locator('#am-name')).to_have_value('')
    assert urlsplit(page.url).path=='/models/audio' and page.evaluate('window.audioStudioDocument')
    assert not dialogs
    assert len(state['posts'])==1
    body=state['posts'][0][1]
    assert body['machines']==['atlas','nova'] and body['source_machine']=='vault'
    page.locator('#am-profiles [data-model="speaker"] [data-identity]').click()
    page.locator('[data-section="output"]').click()
    playwright.expect(page.locator('#am-option-speed')).to_have_value('1.75')


def test_background_installs_preserve_the_next_draft_and_report_partial_failure(studio):
    page,state=studio
    state['install']=True; state['advance_installs']=False
    page.goto('http://console.test/models/audio?new=1')
    page.locator('#am-install-targets input[value="nova"]').locator('..').click()
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('pending-voice')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-install-text')).to_contain_text('pending-voice on atlas: Queued')
    playwright.expect(page.locator('#am-name')).to_have_value('')
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('pending-voice')
    playwright.expect(page.locator('#am-submit')).to_be_disabled()
    page.locator('#am-name').fill('next-voice')
    page.locator('#am-kind').select_option('speech')
    for job in state['jobs'].values():
        job.update(state='complete' if job['machine']=='atlas' else 'failed',why='' if job['machine']=='atlas' else 'Destination stopped answering')
    playwright.expect(page.locator('#am-install-text')).to_contain_text('nova: Failed · Destination stopped answering')
    playwright.expect(page.locator('#am-profiles [data-model="pending-voice"]')).to_be_visible()
    playwright.expect(page.locator('#am-name')).to_have_value('next-voice')
    playwright.expect(page.locator('#am-kind')).to_have_value('speech')
    playwright.expect(page.locator('#am-file option:checked')).to_have_text('vault · storage')
    playwright.expect(page.locator('#am-submit')).to_be_enabled()
    assert len(state['posts'])==1 and urlsplit(page.url).path=='/models/audio'


def test_install_progress_can_reconnect_without_resubmitting_or_losing_a_draft(studio):
    page,state=studio
    state['install']=True; state['progress_error']=True
    page.goto('http://console.test/models/audio?new=1')
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('reconnect-voice')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-install-retry')).to_be_visible()
    page.locator('#am-name').fill('next-model-draft')
    state['progress_error']=False
    page.locator('#am-install-retry').click()
    playwright.expect(page.locator('#am-install-text')).to_contain_text('reconnect-voice on atlas: Installed')
    playwright.expect(page.locator('#am-install-retry')).not_to_be_visible()
    playwright.expect(page.locator('#am-name')).to_have_value('next-model-draft')
    assert len(state['posts'])==1


def test_destination_validation_preserves_selections_when_node_goes_offline(studio):
    page,state=studio
    page.goto('http://console.test/models/audio?new=1')
    targets=page.get_by_role('group',name='Install on compute nodes')
    targets.locator('input[value="atlas"]').locator('..').click()
    playwright.expect(page.locator('#am-submit')).to_be_disabled()
    playwright.expect(page.locator('#am-target-note')).to_have_text('Select at least one destination node.')
    targets.locator('input[value="atlas"]').locator('..').click()
    targets.locator('input[value="nova"]').locator('..').click()
    state['failed'].add('nova')
    page.locator('#am-retry').evaluate('button=>button.click()')
    playwright.expect(targets.locator('input[value="nova"]')).to_be_enabled()
    playwright.expect(targets.locator('input[value="nova"]')).to_be_checked()
    playwright.expect(page.locator('#am-submit')).to_be_disabled()
    assert not state['posts']
    state['failed'].clear()
    page.locator('#am-retry').click()
    playwright.expect(targets.locator('input[value="nova"]')).to_be_enabled()
    playwright.expect(targets.locator('input[value="nova"]')).to_be_checked()
    playwright.expect(page.locator('#am-submit')).to_be_enabled()


def test_pinned_gpus_must_exist_on_every_selected_destination(studio):
    page,state=studio
    state['cards']['nova']=['B']
    page.goto('http://console.test/models/audio?new=1')
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('fleet-voice')
    page.locator('[data-section="hardware"]').click()
    page.get_by_text('Pin to GPUs',exact=True).click()
    page.locator('[data-gpu-card][value="A"]').check()
    page.locator('#am-install-targets input[value="nova"]').locator('..').click()
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-message')).to_contain_text('Incompatible nodes: nova')
    assert not state['posts']
    page.get_by_text('CPU only',exact=True).click()
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
    assert state['posts'][0][1]['machines']==['atlas','nova']
    assert state['posts'][0][1]['settings']['gpus']==0


@pytest.mark.parametrize('width',[1440,390,320])
def test_install_destination_stays_visible_in_every_configuration_section(studio,width):
    page,_=studio
    page.set_viewport_size({'width':width,'height':1100})
    page.goto('http://console.test/models/audio?new=1')
    target=page.get_by_role('group',name='Install on compute nodes')
    first=target.locator('input').first
    playwright.expect(first).to_be_enabled()
    page.locator('#am-add').click()
    playwright.expect(first).to_be_focused()
    playwright.expect(target).to_be_in_viewport()
    for section in ('files','hardware','output','advanced'):
        page.locator(f'[data-section="{section}"]').click()
        playwright.expect(target).to_be_visible()
        assert not target.evaluate('node=>Boolean(node.closest("[data-panel]"))')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


@pytest.mark.parametrize('suffix,facts',[
    ('gguf',{}),
    ('gguf',{'what':'model','architecture':'audiocpp'}),
    ('safetensors',{}),
    ('pt',{'what':'audio','family':'kokoro'}),
])
def test_audio_choices_show_machine_and_folder_and_exclude_other_models(studio,suffix,facts):
    page,state = studio
    for node,tier in [('atlas','cache'),('vault','storage')]:
        source=next(one for one in state['files'] if one['name'] == node)
        source['files'].extend([
            dict(path=f'/custom/{tier}/voice.{suffix}',place=('' if tier == 'cache' else 'storage_')+('weights' if facts else 'audio'),tier=tier,facts=facts),
            dict(path=f'/custom/{tier}/moving.{suffix}',place='audio',tier=tier,facts=facts,moving=True),
            dict(path=f'/custom/{tier}/config.json',tier=tier,facts={'what':'audio'}),
            dict(path=f'/custom/{tier}/llm.gguf',tier=tier,facts={'what':'model','architecture':'qwen3'}),
            dict(path=f'/custom/{tier}/image.safetensors',tier=tier,facts={'what':'image','family':'sdxl'}),
            dict(path=f'/custom/{tier}/unknown.gguf',tier=tier),
        ])
    page.goto('http://console.test/models/audio?new=1')
    for index,(tier,node) in enumerate([('cache','atlas'),('storage','vault')]):
        path=f'/custom/{tier}/voice.{suffix}'
        option=page.locator(f'#am-file option[data-path="{path}"]')
        playwright.expect(option).to_have_count(1)
        playwright.expect(option).to_have_text(f'{node} · {tier}')
        assert '/' not in option.inner_text()
        assert not option.get_attribute('title')
        page.locator('#am-file').select_option(value=option.get_attribute('value'))
        if facts.get('what') == 'audio':
            playwright.expect(page.locator('#am-family')).to_have_value('kokoro')
        else:
            if index == 0:
                playwright.expect(page.locator('#am-family')).to_have_value('')
            playwright.expect(page.locator('#am-family option[value="sdxl"]')).to_have_count(0)
            page.locator('#am-family').select_option('kokoro')
        playwright.expect(page.locator('#am-root')).to_have_value(f'/custom/{tier}')
    playwright.expect(page.locator('#am-file option[data-path*="/moving."]')).to_have_count(0)
    playwright.expect(page.locator('#am-file option[data-path$="/config.json"]')).to_have_count(0)
    for excluded in ('llm.gguf','image.safetensors','unknown.gguf'):
        playwright.expect(page.locator(f'#am-file option[data-path$="/{excluded}"]')).to_have_count(0)
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
    page.locator('[data-audio-task="sound"]').click()
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
    page.locator('#am-file').select_option(label='vault · storage')
    page.locator('#am-name').fill('invalid name with spaces')
    page.locator('#am-submit').click()
    assert not state['posts']
    assert page.locator('#am-name').evaluate('input => !input.checkValidity()')
    page.locator('#am-name').fill('first-voice')
    page.locator('#am-submit').click()
    playwright.expect(page.locator('#am-profiles [data-model="first-voice"]')).to_be_visible()
    playwright.expect(page.locator('#am-profiles')).not_to_contain_text('No audio models yet')
