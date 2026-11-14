"""Live content policies at admission/storage boundaries, with inherited group ceilings."""
import asyncio
import base64
import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfWriter

from loadcoach.content_limits import ContentLimits, checked, contracts
from loadcoach.groups import Group, read_group, section_of
from loadcoach.setting_catalog import default_for, validate
from weightroom.chat_store import attachments
from weightroom.knowledge import chunks, upload, extract, Problem
from weightroom.knowledge_store import Collections
from weightroom.response_store import Responses
from test_weightroom import desk, sign_in  # noqa: F401
from test_response_storage import stored, create  # noqa: F401
from test_collections import knowledge, create as collection_create, add as document_add, pdf
from test_console_browser import browser_context, playwright
from test_postgresql import postgres  # noqa: F401


def policy():
    values = {entry.name: entry.default for entry in contracts()}
    return values, ContentLimits(values.__getitem__)


def inline(data, name='source.txt'):
    return {'name': name, 'data': base64.b64encode(data).decode()}


@pytest.mark.parametrize('entry', contracts(), ids=lambda entry: entry.name)
def test_content_contracts_cover_global_ceiling_and_group_exemption(entry):
    assert validate('weightroom', entry.name, entry.default) == entry.default
    for wrong in [entry.min-1, True, float('inf'), entry.ceiling+1]:
        with pytest.raises(ValueError):
            validate('weightroom', entry.name, wrong)
    assert checked({entry.name:entry.ceiling+1}, superuser=True)[entry.name] == entry.ceiling+1
    with pytest.raises(ValueError):
        checked({entry.name:True}, superuser=True)


def test_group_roundtrip_current_policy_and_demotion_clamp():
    values, live = policy()
    group = read_group('readers', {'limits': {'chat_max_files':2}})
    user = SimpleNamespace(group=group)
    assert read_group('readers', section_of(group)) == group
    assert live.for_user(user)['chat_max_files'] == 2
    values['chat_max_file_mib'] = 7
    assert live.for_user(user)['chat_max_file_mib'] == 7
    with pytest.raises(ValueError):
        read_group('readers', {'limits': {'chat_max_files':33}})
    large = read_group('owners', {'can':['superuser'], 'limits':{'chat_max_files':50}})
    assert live.for_user(SimpleNamespace(group=large))['chat_max_files'] == 50
    assert live.for_user(SimpleNamespace(group=replace(large, can=frozenset())))['chat_max_files'] == 32
    with pytest.raises(ValueError):
        read_group('readers', {'limits': {'made_up':1}})


def test_chat_file_count_and_text_bytes_change_on_same_objects():
    values, live = policy()
    files = [inline(b'a')] * 2
    values['chat_max_files'] = 1
    with pytest.raises(ValueError, match='up to 1'):
        attachments(files, live.for_user())
    values['chat_max_files'] = 2
    assert len(attachments(files, live.for_user())) == 2
    values['chat_max_text_kib'] = 1
    with pytest.raises(ValueError, match='1 KiB'):
        attachments([inline(b'a'*1025)], live.for_user())
    values['chat_max_text_kib'] = 2
    assert attachments([inline(b'a'*1025)], live.for_user())[0]['size'] == 1025


def test_chat_each_and_combined_bytes_are_live(monkeypatch):
    values, live = policy()
    # Valid raster padded with harmless trailing bytes, avoiding huge decoded images.
    output = io.BytesIO(); Image.new('RGB',(2,2)).save(output,format='PNG')
    data = output.getvalue().ljust(1024*1024+1, b' ')
    values['chat_max_file_mib'] = 1
    with pytest.raises(ValueError, match='1 MiB'):
        attachments([inline(data,'image.png')], live.for_user())
    values['chat_max_file_mib'] = 2
    assert attachments([inline(data,'image.png')], live.for_user())
    values['chat_max_total_mib'] = 2
    with pytest.raises(ValueError, match='together'):
        attachments([inline(data,'one.png'),inline(data,'two.png')],live.for_user())
    values['chat_max_total_mib'] = 3
    assert len(attachments([inline(data,'one.png'),inline(data,'two.png')],live.for_user())) == 2


def test_document_bytes_and_pdf_page_policy_apply_without_restart():
    values, live = policy()
    source = inline(pdf().ljust(1024*1024+1,b' '), 'source.pdf')
    with pytest.raises(Problem, match='1 MiB'):
        upload(source, live.for_user())
    values['document_max_mib'] = 2
    assert upload(source, live.for_user())[1] == 'application/pdf'
    writer = PdfWriter()
    # Two copies of a real selectable-text page, so the page policy is the refusal.
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf()))
    for _ in range(2): writer.add_page(reader.pages[0])
    output = io.BytesIO(); writer.write(output)
    doc={'mime':'application/pdf','original':base64.b64encode(output.getvalue()).decode()}
    values['document_max_pages'] = 1
    with pytest.raises(Problem, match='one to 1 pages'):
        asyncio.run(extract(doc,live.for_user()))
    values['document_max_pages'] = 2
    assert len(asyncio.run(extract(doc,live.for_user()))) == 2


def test_chunk_width_overlap_and_source_count_change_without_restart():
    values, live = policy(); pages=[{'page':1,'text':'x'*1000}]
    values.update(chunk_bytes=200, chunk_overlap_bytes=0, chunks_per_source=4)
    with pytest.raises(Problem, match='4 chunks'):
        chunks(pages,4096,live.for_user())
    values['chunks_per_source'] = 5
    first = chunks(pages,4096,live.for_user()); assert len(first) == 5
    values['chunk_bytes'] = 500
    assert len(chunks(pages,4096,live.for_user())) == 2
    values['chunk_overlap_bytes'] = 100
    assert len(chunks(pages,4096,live.for_user())) == 3


def test_collection_account_document_and_chunk_counts_change_without_restart(tmp_path):
    values, live = policy(); store=Collections(tmp_path/'chat.db')
    model={'model_id':'m','fingerprint':'f','build':'b','id':'embed'}
    try:
        values['collections_per_account']=1
        first=store.create('owner','First','normal',model,live.for_user())
        with pytest.raises(ValueError,match='1 collections'):
            store.create('owner','Second','normal',model,live.for_user())
        values['collections_per_account']=2
        store.create('owner','Second','normal',model,live.for_user())
        values['documents_per_collection']=1
        doc=store.add('owner',first,'source','text/plain','eA==','hash',live.for_user())
        with pytest.raises(ValueError,match='1 documents'):
            store.add('owner',first,'second','text/plain','eA==','hash',live.for_user())
        values['documents_per_collection']=2
        assert store.add('owner',first,'second','text/plain','eA==','hash',live.for_user())
        token=store.begin('owner',first,doc)
        parts=[{'page':0,'line':1,'text':'x','vector':[1,0]}]*2
        values['chunks_per_collection']=1
        with pytest.raises(ValueError,match='1 indexed chunks'):
            store.finish('owner',first,doc,token,1,parts,live.for_user())
        values['chunks_per_collection']=2
        assert store.finish('owner',first,doc,token,1,parts,live.for_user())
    finally: store.db.close()


def test_responses_limits_refuse_before_inference_and_apply_next_request(stored):
    client, store, users, _, calls=stored
    values, store.limits=policy(); values['responses_max_records']=1
    create(client); count=len(calls)
    assert client.post('/v1/responses',json={'model':'test','input':'next'}).status_code == 409
    assert len(calls) == count
    values['responses_max_records']=2
    create(client)
    # Byte policy may tighten while records remain: admission refuses without deleting any.
    values['responses_max_mib']=1
    with store.db: store.db.execute('UPDATE responses SET stored_size=1048576')
    values['responses_max_records']=3
    answer=client.post('/v1/responses',json={'model':'test','input':'next'})
    assert answer.status_code==409
    values['responses_max_mib']=3
    create(client)
    assert store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0] == 3


def test_image_and_extraction_size_policies_are_live(tmp_path):
    from mirrorwall.store import Store
    from weightroom.extraction import image_upload
    values, live=policy(); output=io.BytesIO(); Image.new('RGB',(1100,1000)).save(output,format='PNG')
    data=output.getvalue()
    values['image_max_megapixels']=1
    with pytest.raises(ValueError,match='Mi pixels'):
        Store.decoded(data,limits=live.for_user())
    values['image_max_megapixels']=2
    assert Store.decoded(data,limits=live.for_user())[:2] == (1100,1000)
    data=data.ljust(1024*1024+1,b' ')
    values['extraction_max_mib']=1
    with pytest.raises(ValueError,match='1 MiB'):
        image_upload('image.png',data,live.for_user())
    values['extraction_max_mib']=2
    assert image_upload('image.png',data,live.for_user())['width']==1100


def test_audio_upload_policy_on_same_store(tmp_path,monkeypatch):
    from soundroom.store import Store
    from db import database
    values, live=policy(); source=database(tmp_path/'audio.db'); connection=source.connect(check_same_thread=False)
    store=Store(connection,tmp_path); data=b'a'*(1024*1024+1)
    monkeypatch.setattr('soundroom.store.probe',lambda content:(1,48000,1))
    try:
        values['audio_upload_mib']=1
        with pytest.raises(ValueError,match='size limit'):
            store.save('owner',data,temporary=True,limits=live.for_user())
        values['audio_upload_mib']=2
        assert store.save('owner',data,temporary=True,limits=live.for_user())['bytes']==len(data)
    finally: connection.close()


def test_native_group_save_inherit_validation_and_authoritative_me(desk):
    browser, switch, users=desk; sign_in(browser,'jordan')
    assert browser.post('/groups',data={'name':'readers'}).status_code==200
    fields={'do':'limits','priority':'normal','max_sensitivity':'normal','session_hours':'12','value-chat_max_files':'2'}
    saved=browser.post('/groups/readers',data=fields)
    assert saved.status_code==200, saved.text
    assert users.groups.get('readers').limits=={'chat_max_files':2}
    rejected=browser.post('/groups/readers',data={**fields,'value-chat_max_files':'33'})
    assert rejected.status_code==400 and users.groups.get('readers').limits['chat_max_files']==2
    fields['value-chat_max_files']=''
    assert browser.post('/groups/readers',data=fields).status_code==200
    assert users.groups.get('readers').limits=={}
    users.groups.put('plain',{**users.groups.stored('plain'),'limits':{'chat_max_files':1}})
    # Move the existing test account; both its current session and direct API resolve current policy.
    users.change('jordan',group='plain')
    answer=browser.get('/chat')
    assert answer.status_code==200 and 'up to 1,' in answer.text
    import httpx
    async def me():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=switch.front),base_url='http://front:8442') as client:
            return await client.get('/loadcoach/me',headers={'authorization':'Bearer '+browser.cookies['weightroom']})
    assert browser.portal.call(me).json()['limits']=={'chat_max_files':1}


@pytest.mark.parametrize('width',[1440,390])
def test_group_limit_drafts_and_current_hints_at_each_width(desk,width):
    browser, switch, users=desk; sign_in(browser,'jordan')
    browser.post('/groups',data={'name':'readers'}); browser.post('/groups',data={'name':'writers'})
    with playwright.sync_playwright() as engine:
        chromium, page=browser_context(engine,browser)
        try:
            page.set_viewport_size({'width':width,'height':1000})
            page.goto('http://console.test/groups/readers')
            control=page.locator('#setting-weightroom-chat_max_files')
            playwright.expect(control).to_have_value('')
            playwright.expect(control).to_have_attribute('placeholder','Inherit: 4')
            control.fill('2')
            page.locator('[data-group-choice="writers"]').click()
            page.locator('[data-group-choice="readers"]').click()
            playwright.expect(control).to_have_value('2')
            page.locator('[name=do][value=save]').click()
            playwright.expect(page.locator('.said-box')).to_contain_text('Saved.')
            assert users.groups.get('readers').limits['chat_max_files']==2
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.goto('http://console.test/settings/limits')
            playwright.expect(page.locator('#setting-weightroom-chat_max_files')).to_have_value('4')
            page.locator('#settings-search').fill('no-such-limit')
            playwright.expect(page.locator('[data-settings-no-match]')).to_be_visible()
        finally: chromium.close()


def test_global_limits_save_and_reset_without_restart(desk):
    browser, switch, users=desk; sign_in(browser,'jordan')
    # Updates use the same running authority and ordinary generic settings endpoint.
    fields={'program':'weightroom','return_to':'/settings/limits','value-chat_max_files':'6'}
    answer=browser.post('/settings/weightroom',data=fields)
    assert answer.status_code==200 and 'Saved.' in answer.text
    # The background follower publishes settings to this process; no process construction occurs.
    import time
    until=time.monotonic()+3
    while time.monotonic()<until:
        view=browser.get('/chat')
        if 'up to 6,' in view.text:break
        time.sleep(.02)
    assert 'up to 6,' in view.text
    browser.post('/settings/weightroom',data={'program':'weightroom','return_to':'/settings/limits','reset':'chat_max_files'})
    until=time.monotonic()+3
    while time.monotonic()<until:
        view=browser.get('/chat')
        if 'up to 4,' in view.text:break
        time.sleep(.02)
    assert 'up to 4,' in view.text


def test_postgresql_group_policy_and_collection_limits(postgres):
    from loadcoach.users import Users
    users=Users(postgres, {'plain':read_group('plain',{'limits':{'collections_per_account':1}})})
    store=Collections(postgres)
    try:
        users.add('owner','plain')
        live=ContentLimits(); limits=live.for_user(users.by_name('owner'))
        model={'model_id':'m','fingerprint':'f','build':'b','id':'embed'}
        store.create('owner','First','normal',model,limits)
        with pytest.raises(ValueError,match='1 collections'):
            store.create('owner','Second','normal',model,limits)
        users.groups.put('plain',{'limits':{'collections_per_account':2}})
        store.create('owner','Second','normal',model,live.for_user(users.by_name('owner')))
        assert len(store.listing('owner'))==2
    finally: store.db.close(); users.db.close()
