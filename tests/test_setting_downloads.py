"""Write-only node credentials and live transfer admission without replacing workers."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from html.parser import HTMLParser

from loadcoach.config import Config
from loadcoach.download_keys import DownloadKeys, contracts
from loadcoach.hub import DownloadSlots
from loadcoach.setting_catalog import validate, validate_patch
from loadcoach.settings import Changes, laid_over
from test_app import gateway, fake_llama_server, add_user, in_an_empty_folder  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright
from fake_hub import FakeHub
from test_postgresql import postgres  # noqa: F401


@pytest.mark.parametrize('entry',contracts(),ids=lambda entry:entry.name)
def test_credentials_keep_custom_paths_are_atomic_private_and_write_only(tmp_path,entry):
    source=tmp_path/'settings/loadcoach.toml'; source.parent.mkdir(); source.write_text('')
    custom=tmp_path/'custom'/entry.credential_default
    config=Config(path=str(source), **{entry.credential_file:str(custom)})
    keys=DownloadKeys(config)
    keys.save(entry.name,'fake-first-provider-token')
    assert custom.read_text().strip()=='fake-first-provider-token'
    assert custom.stat().st_mode & 0o777 == 0o600
    before=custom.read_bytes(); keys.save(entry.name,'')
    assert custom.read_bytes()==before
    keys.save(entry.name,'fake-second-provider-token')
    assert keys.read(entry.name)=='fake-second-provider-token'
    assert 'fake-' not in json.dumps(keys.status())
    for bad in ['fake\nprivate', 'fake token', 'x'*8193, 1, None]:
        with pytest.raises(ValueError):keys.save(entry.name,bad)
        assert keys.read(entry.name)=='fake-second-provider-token'
    unrelated=tmp_path/'custom/owner.key'; unrelated.write_text('existing-owner-key')
    keys.remove(entry.name)
    assert not custom.exists() and unrelated.read_text()=='existing-owner-key'
    fallback=DownloadKeys(Config(path=str(source)))
    fallback.save(entry.name,'fake-default-token')
    assert fallback.path(entry.name)==tmp_path/'keys'/entry.credential_default
    assert fallback.read(entry.name)=='fake-default-token'


def test_legacy_secret_override_is_ignored_preserved_and_never_forwarded(tmp_path):
    changes=Changes(tmp_path/'settings.db')
    try:
        changes.put('loadcoach','hub_token','fake-legacy-secret','owner')
        assert changes.of('loadcoach')=={}
        assert changes.listing()==[]
        assert changes.db.execute("SELECT COUNT(*) FROM settings_changed WHERE name='hub_token'").fetchone()[0]==1
        assert laid_over({}, {'hub_token':'fake-legacy-secret'},'loadcoach')=={}
        for proposed in ['fake-new-secret',None]:
            with pytest.raises(ValueError,match='write-only'):
                validate_patch('loadcoach',{}, {'hub_token':proposed})
    finally:changes.db.close()


@pytest.mark.parametrize('wrong',[-1,33,True,'2',float('inf')])
def test_download_cap_ranges_come_from_catalogue(wrong):
    with pytest.raises(ValueError):validate('loadcoach','downloads_at_once',wrong)


def test_live_transfer_limit_raise_lower_disable_and_cancel_without_restart():
    async def exercise():
        limit=[1]; slots=DownloadSlots(lambda:limit[0]); entered=[asyncio.Event() for _ in range(4)]
        finish=[asyncio.Event() for _ in range(4)]; progress=[{} for _ in range(4)]
        async def transfer(i):
            async with slots.slot(progress[i]):
                entered[i].set(); await finish[i].wait()
        tasks=[asyncio.create_task(transfer(i)) for i in range(3)]
        try:
            await asyncio.wait_for(entered[0].wait(),1); await asyncio.sleep(.03)
            assert slots.active==1 and progress[1]['state']==progress[2]['state']=='queued'
            limit[0]=2
            await asyncio.wait_for(entered[1].wait(),1)
            assert slots.active==2 and not entered[2].is_set()
            limit[0]=1; finish[0].set(); await tasks[0]; await asyncio.sleep(.03)
            assert slots.active==1 and not entered[2].is_set()
            tasks.append(asyncio.create_task(transfer(3))); await asyncio.sleep(.03)
            tasks[3].cancel(); await asyncio.gather(tasks[3],return_exceptions=True)
            assert slots.active==1
            limit[0]=0
            await asyncio.wait_for(entered[2].wait(),1)
            assert slots.active==2
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        assert slots.active==0
    asyncio.run(exercise())


def node(caller,headers):
    reply=caller.get('/turnstile/machines',headers=headers)
    assert reply.status_code==200,reply.text
    return reply.json()['machines'][0]['name']


def test_node_proxy_token_reads_next_use_and_never_exposes_values(fake_llama_server,tmp_path):
    provider=FakeHub({'tiny.gguf':b'weights'}); provider.closed=True
    folder=tmp_path/'storage';folder.mkdir()
    with gateway(fake_llama_server,places={'storage_llm':str(folder)},web=provider.client()) as caller:
        admin=add_user(caller,'owner','admin'); ordinary=add_user(caller,'pat','plain',by=admin)
        name=node(caller,admin); endpoint=f'/turnstile/machines/{name}/download-credentials'
        assert caller.get(endpoint,headers=ordinary).status_code==403
        answer=caller.post(endpoint,headers=admin,json={'kind':'hub_token','operation':'save','credential':'good'})
        assert answer.status_code==200 and 'good' not in answer.text, answer.text
        assert answer.json()['credentials']['hub_token']['saved']
        listed=caller.get('/loadcoach/hub',headers=admin,params={'node':name,'repository':provider.repository})
        assert listed.status_code==200,listed.text
        assert provider.seen[-1][2]['authorization']=='Bearer good'
        started=caller.post('/loadcoach/downloads',headers=admin,params={'node':name},
                            json={'repository':provider.repository,'files':['tiny.gguf']})
        assert started.status_code==202, started.text
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and not (folder/'acme--tiny-GGUF/tiny.gguf').exists():time.sleep(.01)
        assert (folder/'acme--tiny-GGUF/tiny.gguf').read_bytes()==b'weights'
        assert caller.post(endpoint,headers=admin,json={'kind':'hub_token','operation':'save','credential':'fake-replacement'}).status_code==200
        caller.get('/loadcoach/hub',headers=admin,params={'node':name,'repository':provider.repository})
        assert provider.seen[-1][2]['authorization']=='Bearer fake-replacement'
        assert caller.post(endpoint,headers=admin,json={'kind':'hub_token','operation':'save','credential':'fake\nrefused'}).status_code==400
        seen=caller.get('/turnstile/machines/'+name+'/settings',headers=admin).text
        assert 'fake-replacement' not in seen and 'fake-replacement' not in str(caller.log.read())
        assert caller.post(endpoint,headers=admin,json={'kind':'hub_token','operation':'remove'}).status_code==200
        assert not caller.get(endpoint,headers=admin).json()['credentials']['hub_token']['saved']


def test_cap_is_live_in_actual_downloads_and_effective_report(fake_llama_server,tmp_path):
    provider=FakeHub({'one.gguf':b'a'*128,'two.gguf':b'b'*128,'three.gguf':b'c'*128});provider.slow=.5
    folder=tmp_path/'storage';folder.mkdir()
    with gateway(fake_llama_server,places={'storage_llm':str(folder)},web=provider.client(),downloads_at_once=1) as caller:
        admin=add_user(caller,'owner','admin');name=node(caller,admin)
        params={'node':name}
        result=caller.post('/loadcoach/downloads',headers=admin,params=params,
                           json={'repository':provider.repository,'files':['one.gguf','two.gguf','three.gguf']})
        assert result.status_code==202,result.text
        rows=caller.get('/loadcoach/downloads',headers=admin,params=params).json()['downloads']
        assert sum(row['state']=='queued' for row in rows)==2
        changed=caller.post(f'/turnstile/machines/{name}/settings/loadcoach',headers=admin,json={'downloads_at_once':2})
        assert changed.status_code==200,changed.text
        report=next(p for p in changed.json()['programs'] if p['program']=='loadcoach')
        assert next(r for r in report['settings'] if r['name']=='downloads_at_once')['value']==2
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            rows=caller.get('/loadcoach/downloads',headers=admin,params=params).json()['downloads']
            if sum(row['state']=='coming' for row in rows)==2:break
            time.sleep(.01)
        assert sum(row['state']=='coming' for row in rows)==2
        for row in rows:
            assert caller.post('/loadcoach/downloads/stop',headers=admin,params=params,json={'path':row['path']}).status_code==200


def fields(markup):
    class Fields(HTMLParser):
        def __init__(self):
            super().__init__(); self.values={}; self.select=None
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if attrs.get('id'):
                self.values[attrs['id']]=attrs
            if tag=='select':self.select=attrs.get('id')
            if tag=='option' and self.select:
                self.values[self.select].setdefault('value',attrs.get('value',''))
                if 'selected' in attrs:self.values[self.select]['value']=attrs.get('value','')
        def handle_endtag(self,tag):
            if tag=='select':self.select=None
    parser=Fields(); parser.feed(markup); return parser.values


def reported(switch):
    from loadcoach.settings import said
    changes=Changes(switch.wire.app.state.databases.database('loadcoach.core'))
    try:
        changes.seen('loadcoach',said({'downloads_at_once':0},{'downloads_at_once':0},{},'loadcoach'))
    finally:changes.db.close()


def selected(browser):
    return fields(browser.get('/settings/downloads').text)['download-machine']['value']


def test_native_download_forms_and_empty_save_preserve_file(desk):
    browser,switch,users=desk;reported(switch);sign_in(browser,'jordan');name=selected(browser)
    path=f'/settings/downloads/{name}/credentials/hub_token'
    saved=browser.post(path,data={'operation':'save','credential':'fake-node-token'})
    assert saved.status_code==200 and 'fake-node-token' not in saved.text
    blank=browser.post(path,data={'operation':'save','credential':''})
    assert blank.status_code==200 and 'Saved' in blank.text
    invalid=browser.post(path,data={'operation':'save','credential':'fake\nnever-refill'})
    assert invalid.status_code==400 and 'fake' not in invalid.text
    assert 'fake-node-token' not in browser.get('/settings/history?export=json').text
    saved=browser.post('/settings/loadcoach',data={'return_to':'/settings/downloads','machine':name,'value-downloads_at_once':'2'})
    assert saved.status_code==200 and 'Downloads and credentials' in saved.text
    controls=fields(saved.text)
    assert 'setting-loadcoach-downloads_at_once' in controls, sorted(controls)
    control=controls['setting-loadcoach-downloads_at_once']
    assert control['value']=='2'


@pytest.mark.parametrize('width',[1440,390])
def test_download_controls_fit_and_refused_values_are_not_retained(desk,width):
    browser,switch,users=desk;reported(switch);sign_in(browser,'jordan')
    with playwright.sync_playwright() as engine:
        chromium,page=browser_context(engine,browser)
        try:
            page.set_viewport_size({'width':width,'height':1000});page.goto('http://console.test/settings/downloads')
            heading=page.locator('summary').filter(has_text='Download credentials');heading.click()
            field=page.locator('#credential-loadcoach-hub_token')
            playwright.expect(field).to_be_visible()
            field.fill('fake-browser-provider-token')
            form=field.locator('xpath=ancestor::form');form.get_by_role('button',name='Save credential',exact=True).click()
            page.goto('http://console.test/settings/downloads')
            page.locator('summary').filter(has_text='Download credentials').click()
            playwright.expect(field).to_have_value('')
            assert 'fake-browser-provider-token' not in page.content()
            page.locator('#settings-search').fill('Hugging Face token')
            playwright.expect(field).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:chromium.close()


def test_audio_packages_share_transfer_slots_and_read_token_after_wait(tmp_path,monkeypatch):
    from soundroom.packages import Packages
    from types import SimpleNamespace
    import hashlib
    data=b'local package'; digest=hashlib.sha256(data).hexdigest(); captured=[]
    async def metadata(web,path,token,**kwargs):
        return {'sha':'a'*40} if '/revision/' in path else [{'path':'model.gguf','type':'file','size':len(data),'lfs':{'oid':digest}}]
    async def fetched(web,repository,entry,target,token,progress):
        captured.append(token);target.write_bytes(data);progress.update(done=len(data),state='checking')
    monkeypatch.setattr('loadcoach.hub.asked',metadata);monkeypatch.setattr('loadcoach.hub.fetch',fetched)
    async def exercise():
        slots=DownloadSlots(lambda:1);token=['fake-original-token']
        packages=Packages(lambda:{'audio':str(tmp_path)},None,lambda:token[0],SimpleNamespace(add=lambda *a,**k:None),
                          lambda:'',lambda:set(),download_slots=slots)
        package={'id':'sample','repo':'acme/sample','files':['model.gguf'],'target_directory':'sample'}
        packages.progress['sample']={'done':0,'state':'coming'}
        async with slots.slot({}):
            task=asyncio.create_task(packages.install(package,'owner','local'))
            await asyncio.sleep(.03)
            assert captured==[] and slots.active==1
            token[0]='fake-current-token'
        await asyncio.wait_for(task,2)
        assert captured==['fake-current-token'] and slots.active==0
        assert (tmp_path/'sample/model.gguf').read_bytes()==data
    asyncio.run(exercise())


def test_offline_save_does_not_queue_or_replace_credentials(desk):
    browser,switch,users=desk;reported(switch);sign_in(browser,'jordan');name=selected(browser)
    endpoint=f'/settings/downloads/{name}/credentials/hub_token'
    assert browser.post(endpoint,data={'credential':'fake-existing','operation':'save'}).status_code==200
    switch.off=True
    failed=browser.post(endpoint,data={'credential':'fake-offline-change','operation':'save'})
    assert failed.status_code==503 and 'fake-offline-change' not in failed.text
    raw=switch.front.state.console_settings.changes.db.execute('SELECT value FROM settings_changed').fetchall()
    assert 'fake-offline-change' not in str(raw)
    switch.off=False
    assert 'Saved' in browser.get('/settings/downloads').text
    # The upstream key remains the earlier one when the real provider reader runs again.
    browser.get('/hub?repository='+switch.hub.repository)
    authenticated=[headers.get('authorization') for host,path,headers in switch.hub.seen if host=='huggingface.co']
    assert 'Bearer fake-existing' in authenticated and 'Bearer fake-offline-change' not in authenticated


def test_civitai_credential_is_read_at_each_provider_operation(desk,monkeypatch):
    browser,switch,users=desk;reported(switch);sign_in(browser,'jordan');name=selected(browser)
    captured=[]
    async def search(http,words,token='',adult=False):
        captured.append(token);return []
    monkeypatch.setattr('mirrorwall.civitai.search',search)
    endpoint=f'/settings/downloads/{name}/credentials/civitai_token'
    for token in ['fake-civitai-first','fake-civitai-second']:
        saved=browser.post(endpoint,data={'credential':token,'operation':'save'})
        assert saved.status_code==200 and token not in saved.text
        # This is the existing real provider-discovery route on the selected node.
        import httpx
        async def discover():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=switch.front),base_url='http://front:8442') as api:
                return await api.get('/loadcoach/hub',params={'node':name,'source':'civitai','search':'local receiver'},
                                     headers={'authorization':'Bearer '+browser.cookies['weightroom']})
        assert browser.portal.call(discover).status_code==200
    assert captured==['fake-civitai-first','fake-civitai-second']


def test_postgresql_secret_rows_are_ignored_without_deletion(postgres):
    changes=Changes(postgres)
    try:
        changes.put('loadcoach','hub_token','fake-pg-private','owner')
        changes.seen('loadcoach',{'hub_token':{'value':'fake-pg-private'},'downloads_at_once':{'value':2}})
        assert changes.of('loadcoach')=={} and changes.listing()==[]
        assert changes.db.execute("SELECT COUNT(*) FROM settings_changed WHERE name='hub_token'").fetchone()[0]==1
        assert 'fake-pg-private' not in str(changes.everything())
    finally:changes.db.close()
