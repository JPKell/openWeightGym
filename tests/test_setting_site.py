"""Live site identity, escaped announcements and personal-first presentation defaults."""
import json
import time
from datetime import UTC, datetime

import pytest

from loadcoach.setting_catalog import validate, banner_end
from weightroom.display import site_banner
from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright


@pytest.mark.parametrize('name,wrong',[
    ('site_name',''),('site_name','   '),('site_name','x'*81),('site_name','a\nb'),
    ('site_name',True),('default_palette','unknown'),('default_appearance','auto'),
    ('default_chat_model','x'*161),('default_chat_model','a\nb'),
    ('site_banner','text'),('site_banner',{'other':'text'}),('site_banner',{'text':1}),
    ('site_banner',{'text':'x'*2001}),('site_banner',{'level':'success'}),
    ('site_banner',{'ends_at':'2026-12-01T03:00:00'}),('site_banner',{'ends_at':30}),
    ('site_banner',{'ends_at':'not a date'}),
])
def test_display_contracts_reject_invalid_values(name,wrong):
    with pytest.raises(ValueError):validate('weightroom',name,wrong)


def test_banner_is_explicitly_zoned_optional_and_expires_on_absolute_time():
    end=banner_end('2026-12-01T03:00:00Z')
    assert end==banner_end('2026-11-30T22:00:00-05:00')
    assert banner_end('') is None
    value={'text':'Maintenance','level':'warning','ends_at':'2026-12-01T03:00:00Z'}
    assert site_banner(value,end-1)=={'text':'Maintenance','level':'warning','ends_at':end}
    assert site_banner(value,end) is None
    assert site_banner({'text':''},end) is None
    assert site_banner({'text':'hello','level':'invalid'},end) is None


def save(server,**values):
    from loadcoach.setting_catalog import get
    from loadcoach.settings import written
    fields={'return_to':'/settings'}
    fields.update({'value-'+name:value if get('weightroom',name).control=='plain-text' else written(value)
                   for name,value in values.items()})
    reply=server.post('/settings/weightroom',data=fields)
    assert reply.status_code==200,reply.text
    return reply


def test_brand_and_banner_are_live_escaped_on_pages_signin_and_errors(desk):
    server,wire,users=desk;sign_in(server,'jordan')
    name='Owner <img src=x onerror=alert(1)>'
    save(server,site_name=name,site_banner={'text':'<script>alert(1)</script> Maintenance','level':'danger','ends_at':''})
    for path in ('/settings','/status','/chat','/sign-in','/no-such-page'):
        reply=server.get(path)
        assert 'Owner &lt;img' in reply.text and '<img src=x' not in reply.text,path
        assert '&lt;script&gt;alert(1)&lt;/script&gt; Maintenance' in reply.text,path
        assert 'id="site-banner"' in reply.text and 'alert-danger' in reply.text,path
        assert 'Owner &lt;img src=x onerror=alert(1)&gt;</title>' in reply.text,path
    assert 'weightroom' in server.cookies
    save(server,site_banner={'text':'Expired','ends_at':'2000-01-01T00:00:00Z'})
    assert 'id="site-banner"' not in server.get('/status').text
    server.post('/settings/weightroom',data={'reset':'site_name'})
    assert '<span>openWeightGym</span>' in server.get('/status').text
    save(server,site_banner={'text':''})
    assert 'id="site-banner"' not in server.get('/settings').text


def test_global_chat_model_or_role_yields_to_personal_explicit_and_permission_scope(desk,tmp_path):
    from loadcoach.models import Models
    from loadcoach.roles import Roles
    server,wire,users=desk
    models=Models(tmp_path/'loadcoach.db');roles=Roles(tmp_path/'data/roles.db')
    for name in ('first','preferred','private'):
        models.put(name,{'gpus':0,'flags':{'model':name+'.gguf'}})
    roles.put('chat',{'models':{'preferred':1}})
    users.change('pat',own={'models':['first','preferred']})
    sign_in(server,'jordan');save(server,default_chat_model='preferred');sign_in(server,'pat')
    assert 'value="preferred" data-plugin-model="false" selected' in server.get('/chat').text
    users.choose('pat',{'chat_model':'first'})
    assert 'value="first" data-plugin-model="false" selected' in server.get('/chat').text
    assert 'value="preferred" data-plugin-model="false" selected' in server.get('/chat?model=preferred').text
    chats=server.app.state.conversations.state.chats
    number=chats.start(users.identity('pat'),'Existing conversation')
    chats.say(number,'user','old question');chats.say(number,'assistant','old answer',model='first')
    assert 'value="first" data-plugin-model="false" selected' in server.get('/chat/'+str(number)).text
    users.choose('pat',{'chat_model':''});sign_in(server,'jordan');save(server,default_chat_model='chat:fast');sign_in(server,'pat')
    assert 'value="chat:fast" data-plugin-model="false" selected' in server.get('/chat').text
    sign_in(server,'jordan');save(server,default_chat_model='private');sign_in(server,'pat')
    assert 'value="private"' not in server.get('/chat').text
    sign_in(server,'jordan');save(server,default_chat_model='unknown-later');sign_in(server,'pat')
    assert 'value="unknown-later"' not in server.get('/chat').text
    models.db.close();roles.db.close()


@pytest.mark.parametrize('width',[390,1440])
def test_defaults_inherit_live_saved_theme_wins_reset_resumes_and_banner_expires(desk,width):
    server,wire,users=desk;sign_in(server,'jordan');save(server,default_palette='forest',default_appearance='dark',site_name='W'*80)
    errors=[]
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,server)
        page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.set_viewport_size({'width':width,'height':1000});page.goto('http://console.test/account/appearance')
            playwright.expect(page.locator('html')).to_have_attribute('data-site-theme','forest')
            playwright.expect(page.locator('html')).to_have_attribute('data-bs-theme','dark')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('[name=palette][value=ocean]').check();page.select_option('#theme-mode','light')
            page.get_by_role('button',name='Save theme',exact=True).click()
            save(server,default_palette='rose',default_appearance='dark')
            page.reload()
            playwright.expect(page.locator('html')).to_have_attribute('data-site-theme','ocean')
            playwright.expect(page.locator('html')).to_have_attribute('data-bs-theme','light')
            page.get_by_role('button',name='Use default',exact=True).click()
            playwright.expect(page.locator('html')).to_have_attribute('data-site-theme','rose')
            assert page.evaluate('localStorage.getItem("weightroom-theme-v1:"+encodeURIComponent(document.documentElement.dataset.themeUser))') is None
            save(server,default_palette='clay',default_appearance='light')
            page.reload()
            playwright.expect(page.locator('html')).to_have_attribute('data-site-theme','clay')
            playwright.expect(page.locator('html')).to_have_attribute('data-bs-theme','light')
            end=datetime.fromtimestamp(time.time()+20,UTC).isoformat()
            save(server,site_banner={'text':'Scheduled maintenance test','level':'warning','ends_at':end})
            page.clock.install();page.goto('http://console.test/status')
            playwright.expect(page.locator('#site-banner')).to_contain_text('Scheduled maintenance test')
            page.clock.fast_forward(25000)
            playwright.expect(page.locator('#site-banner')).to_have_count(0)
            page.goto('http://console.test/settings')
            page.locator('#settings-search').fill('site announcement')
            page.get_by_text('Advanced · Site announcement',exact=True).click()
            playwright.expect(page.locator('#setting-weightroom-site_banner')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
        finally:browser.close()


def test_catalogue_default_report_covers_optional_dictionary_fields(tmp_path):
    from loadcoach.settings import said
    path=tmp_path/'voice-gateway.toml';path.write_text('[hermes]\ntoolsets=["search"]\n')
    report=said({'hermes':{'toolsets':['search']}},{'hermes':{'toolsets':['search']}},{},'voice-gateway',path=path)
    assert 'default' in report['hermes.toolsets']


def test_native_display_refusals_preserve_effective_policy_and_permissions(desk):
    server,wire,users=desk;sign_in(server,'jordan')
    save(server,site_name='Training room')
    for fields in ({'value-site_name':''}, {'value-default_palette':'"invented"'},
                   {'value-site_banner':'text = "Maintenance"\nlevel = "success"'}):
        response=server.post('/settings/weightroom',data={'return_to':'/settings',**fields})
        assert response.status_code==400
        assert '<span>Training room</span>' in response.text and 'said-box' in response.text
    sign_in(server,'pat')
    assert server.post('/settings/weightroom',data={'value-site_name':'Unauthorized'}).status_code==403
    assert '<span>Training room</span>' in server.get('/chat').text
