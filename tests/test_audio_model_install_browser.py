"""Audio registration walkthrough using real app APIs, inventories, copies and databases.

The default GGUF fixtures contain contracts, not synthesis weights. Set
OWG_AUDIOCPP_TEST_EXECUTABLE and OWG_AUDIOCPP_TEST_MANIFEST to use an installed
runtime and catalogue, and OWG_AUDIOCPP_TEST_MODEL to use real Kokoro weights.
These checks register models; they do not launch native inference.
"""

from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient
import spotter.app as spotter

from front_helpers import Later, door, machine_key, turnstile_to
from loadcoach.app import build_app as build_loadcoach
from loadcoach.config import Config
from loadcoach.models import Models
from loadcoach.users import Users
from test_console_browser import browser_context, playwright
from test_weightroom import GROUPS, Switch, gguf, sign_in
from weightroom.app import Settings, build_app


@pytest.fixture
def audio_install_console(tmp_path, monkeypatch, request):
    monkeypatch.setattr('weightroom.app.LOOK_EVERY', .05)
    data=tmp_path/'data'; data.mkdir()
    users=Users(str(data/'accounts.db'),GROUPS)
    users.add('jordan','admin'); users.change('jordan',password='correct horse')
    users.add('panel','front')
    cache,storage=tmp_path/'cache'/'audio',tmp_path/'storage'/'audio'
    front_cache,front_storage=tmp_path/'front-cache',tmp_path/'front-storage'
    contract={'family':'kokoro_tts','display_name':'Kokoro TTS','tasks':['tts'],
              'options':{'request':[{'name':'speed','type':'float','min':.25,'max':4}]}}
    native_manifest=os.environ.get('OWG_AUDIOCPP_TEST_MANIFEST')
    if native_manifest:
        contract=json.loads((Path(native_manifest).parent/'model_specs'/'kokoro_tts.json').read_text())
    packages={}
    for tier,root in [('cache',cache),('storage',storage),('front-cache',front_cache/'audio'),('front-storage',front_storage/'audio')]:
        folder=root/(tier+'-voice'); folder.mkdir(parents=True)
        if native_model:=os.environ.get('OWG_AUDIOCPP_TEST_MODEL'):
            main=folder/'model.gguf'; main.hardlink_to(native_model)
        else:
            main=Path(gguf(folder/'model.gguf',general__architecture='audiocpp',
                           audiocpp__model_spec__json=json.dumps(contract)))
        companion=folder/'tokens.json'; companion.write_text('{"voice":"test"}')
        packages[tier]=(main,companion)
    specs=tmp_path/'native'/'model_specs'; specs.mkdir(parents=True)
    (specs/'kokoro_tts.json').write_text(json.dumps(contract))
    manifest=specs.parent/'installation.json'; manifest.write_text('{}')
    if native_manifest:
        manifest=Path(native_manifest)
    later=Later()

    def worker(folder,cache,storage):
        folder.mkdir(exist_ok=True)
        config_file=folder/'loadcoach.toml'; config_file.write_text('')
        database=str(folder/'loadcoach.db')
        places={'audio':str(cache),'storage_audio':str(storage)}
        key=machine_key(folder)
        if folder!=tmp_path:
            Path(key).write_text('secondary-node-key-for-audio-install-test')
        coach=build_loadcoach(Config(database=database,gpus=(),nvidia_smi='no-such-program',path=str(config_file),data='.',
            front='http://front:8442',machine_key_file=key,places=places,
            engines={'audiocpp':{'enabled':True,'executable':os.environ.get('OWG_AUDIOCPP_TEST_EXECUTABLE',sys.executable),
                                'manifest':str(manifest)}}),front=door(later,8442))
        watcher=spotter.build_app(spotter.Settings(services={},machine_key_file=key,database=database,
            places=places,file_index=str(folder/'files-index.json'),history=str(folder/'spotter.db'),disks=(str(folder),)))
        return coach,watcher,database,key

    coach,watcher,database,_=worker(tmp_path,cache,storage)
    wires={8442:Switch(coach),8444:Switch(watcher)}
    machines={}; peers=[]; destination=None
    if getattr(request,'param',False):
        folder=tmp_path/'secondary'
        target_cache,target_storage=folder/'cache'/'audio',folder/'storage'/'audio'
        target_cache.mkdir(parents=True); target_storage.mkdir(parents=True)
        peer,peer_watcher,target_database,key=worker(folder,target_cache,target_storage)
        wires.update({9442:Switch(peer),9444:Switch(peer_watcher)})
        machines['studio-cpu']={'compute':'http://secondary:9442','spotter':'http://secondary:9444','machine_key_file':key}
        peers=[peer,peer_watcher]
        destination={'node':'studio-cpu','cache':target_cache,'database':target_database}
    front=turnstile_to(tmp_path,users,wires,machines=machines,
                       models_folder=str(front_cache),models_storage_folder=str(front_storage))
    later.app=front
    app=build_app(Settings(compute='http://compute',key=users.new_key('panel'),data=str(data)),door(front,8442))
    lifespan=app.router.lifespan_context

    @asynccontextmanager
    async def services(application):
        async with AsyncExitStack() as stack:
            for service in [coach,watcher,*peers,front]:
                await stack.enter_async_context(service.router.lifespan_context(service))
            await stack.enter_async_context(lifespan(application))
            yield

    app.router.lifespan_context=services
    with TestClient(app) as client:
        sign_in(client,'jordan')
        client.audio_target=destination
        yield client,packages,cache,database


@pytest.mark.parametrize('tier',['cache','storage','front-cache','front-storage'])
@pytest.mark.parametrize('width',[1440,390])
def test_add_reopen_edit_and_reject_duplicate_audio_model(audio_install_console,tier,width):
    client,packages,cache,database=audio_install_console
    main,companion=packages[tier]
    original={path.name:path.read_bytes() for path in (main,companion)}
    name=tier+'-voice'
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,client)
        page.set_viewport_size({'width':width,'height':1100})
        errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/models/audio?new=1')
        page.locator('#am-kind').select_option('speech')
        option=page.locator(f'#am-file option[data-path="{main}"]')
        playwright.expect(option).to_have_text(('front' if tier.startswith('front-') else 'compute')+' · '+name)
        page.locator('#am-file').select_option(value=option.get_attribute('value'))
        playwright.expect(page.locator('#am-family')).to_have_value('kokoro_tts')
        page.locator('#am-name').fill(name)
        page.locator('#am-companions').select_option(str(companion))
        page.locator('[data-section="hardware"]').click()
        page.get_by_text('CPU only',exact=True).click()
        page.locator('#am-ram').fill('2048')
        page.locator('[data-section="output"]').click()
        page.locator('#am-seconds').fill('20')
        page.locator('#am-option-speed').fill('1.25')
        page.locator('#am-submit').click()
        page.wait_for_url('**/models?kind=audio&install=*')
        deadline=time.monotonic()+10
        library=Models(database)
        while time.monotonic()<deadline and library.stored(name) is None:
            time.sleep(.05)
        saved=library.stored(name)
        assert saved is not None,client.get('/models?kind=audio').text
        target=cache/name/'model.gguf'
        assert saved['flags']['model']==str(target)
        assert saved['audiocpp']['path']==str(target)
        assert saved['audiocpp']['files']==['tokens.json']
        assert saved['audiocpp']['defaults']['speed']==1.25
        assert saved['gpus']==0
        for filename,contents in original.items():
            assert (target.parent/filename).read_bytes()==contents
            assert (main.parent/filename).read_bytes()==contents

        page.goto('http://console.test/models/audio?node=compute&model='+name)
        playwright.expect(page.locator('#am-name')).to_have_value(name)
        page.locator('[data-section="output"]').click()
        playwright.expect(page.locator('#am-seconds')).to_have_value('20')
        playwright.expect(page.locator('#am-option-speed')).to_have_value('1.25')
        page.locator('#am-seconds').fill('25')
        page.locator('#am-submit').click()
        playwright.expect(page.locator('#am-saved')).to_have_text('Saved.')
        assert library.stored(name)['audiocpp']['limits']['max_seconds']==25
        page.reload()
        page.locator('[data-section="output"]').click()
        playwright.expect(page.locator('#am-seconds')).to_have_value('25')

        page.locator('#am-add').click()
        option=page.locator(f'#am-file option[data-path="{target}"]')
        page.locator('#am-file').select_option(value=option.get_attribute('value'))
        page.locator('#am-name').fill(name)
        page.locator('[data-section="hardware"]').click()
        page.get_by_text('CPU only',exact=True).click()
        page.locator('#am-submit').click()
        playwright.expect(page.locator('#am-message')).to_contain_text('already a model named')
        assert library.stored(name)['audiocpp']['limits']['max_seconds']==25
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        library.db.close()
        browser.close()
        assert not errors


def test_cpu_only_node_adds_a_model_without_a_hardware_override(audio_install_console):
    client,packages,_,database=audio_install_console
    main,_=packages['cache']
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,client)
        page.goto('http://console.test/models/audio?new=1')
        option=page.locator(f'#am-file option[data-path="{main}"]')
        page.locator('#am-file').select_option(value=option.get_attribute('value'))
        page.locator('#am-name').fill('cpu-default')
        page.locator('#am-submit').click()
        page.wait_for_url('**/models?kind=audio&install=*')
        library=Models(database)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and library.stored('cpu-default') is None:
            time.sleep(.05)
        assert library.stored('cpu-default')['gpus']==0
        library.db.close()
        browser.close()


@pytest.mark.parametrize('audio_install_console',[True],indirect=True)
def test_visible_destination_installs_on_the_selected_compute_node(audio_install_console):
    client,packages,_,source_database=audio_install_console
    main,companion=packages['front-storage']
    destination=client.audio_target
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,client)
        page.set_viewport_size({'width':390,'height':1100})
        page.goto('http://console.test/models/audio?new=1')
        target=page.get_by_role('group',name='Install on compute nodes')
        playwright.expect(target).to_be_visible()
        option=page.locator(f'#am-file option[data-path="{main}"]')
        page.locator('#am-file').select_option(value=option.get_attribute('value'))
        page.locator('#am-name').fill('chosen-destination')
        page.locator('#am-companions').select_option(str(companion))
        target.locator('input[value="compute"]').locator('..').click()
        target.locator('input[value="'+destination['node']+'"]').locator('..').click()
        playwright.expect(page.locator('#panel-files')).to_be_visible()
        playwright.expect(page.locator('#am-name')).to_have_value('chosen-destination')
        page.locator('#am-submit').click()
        page.wait_for_url('**/models?kind=audio&install=*')
        library=Models(destination['database'])
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and library.stored('chosen-destination') is None:
            time.sleep(.05)
        saved=library.stored('chosen-destination')
        assert saved is not None
        expected=destination['cache']/'front-storage-voice'/'model.gguf'
        assert saved['flags']['model']==str(expected)
        assert saved['gpus']==0
        assert saved['audiocpp']['files']==['tokens.json']
        assert expected.read_bytes()==main.read_bytes()
        source=Models(source_database)
        assert source.stored('chosen-destination') is None
        source.db.close(); library.db.close(); browser.close()


@pytest.mark.parametrize('audio_install_console',[True],indirect=True)
@pytest.mark.parametrize('taken',[False,True])
def test_multi_node_audio_install_preflights_all_destinations_and_copies_companions(audio_install_console,taken):
    client,packages,cache,database=audio_install_console
    main,companion=packages['front-storage']
    destination=client.audio_target
    libraries=[Models(database),Models(destination['database'])]
    name='fleet-voice'

    def await_saved(library):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and library.stored(name) is None:
            time.sleep(.05)
        assert library.stored(name) is not None
        return library.stored(name)

    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,client)
        page.set_viewport_size({'width':390,'height':1100})

        def setup():
            page.goto('http://console.test/models/audio?new=1')
            option=page.locator(f'#am-file option[data-path="{main}"]')
            page.locator('#am-file').select_option(value=option.get_attribute('value'))
            page.locator('#am-name').fill(name)
            page.locator('#am-companions').select_option(str(companion))
            page.locator('#am-install-targets input[value="studio-cpu"]').locator('..').click()

        if taken:
            setup()
            page.locator('#am-install-targets input[value="compute"]').locator('..').click()
            page.locator('#am-submit').click()
            page.wait_for_url('**/models?kind=audio&install=*')
            await_saved(libraries[1])

        setup()
        playwright.expect(page.locator('#am-submit')).to_have_text('Install on 2 nodes')
        with page.expect_response(lambda response:'/soundroom-models?' in response.url and response.request.method=='POST') as received:
            page.locator('#am-submit').click()
        response=received.value
        if taken:
            assert response.status==409
            playwright.expect(page.locator('#am-message')).to_contain_text('Nothing was started')
            assert libraries[0].stored(name) is None
            assert not (cache/main.parent.name).exists()
            for node in ('compute','studio-cpu'):
                playwright.expect(page.locator(f'#am-install-targets input[value="{node}"]')).to_be_checked()
        else:
            assert response.status==202,response.text()
            assert response.request.post_data_json['machines']==['compute','studio-cpu']
            page.wait_for_url('**/models?kind=audio&install=*')
            jobs=parse_qs(urlsplit(page.url).query)['install']
            assert len(jobs)==len(set(jobs))==2
            for library,folder in zip(libraries,[cache,destination['cache']]):
                saved=await_saved(library)
                target=folder/main.parent.name/main.name
                assert saved['flags']['model']==saved['audiocpp']['path']==str(target)
                assert saved['gpus']==0 and saved['audiocpp']['files']==['tokens.json']
                assert target.read_bytes()==main.read_bytes()
                assert (target.parent/companion.name).read_bytes()==companion.read_bytes()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        for library in libraries:
            library.db.close()
        browser.close()
