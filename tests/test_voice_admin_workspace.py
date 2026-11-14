"""Voice workspace contracts: independently saved profiles, drafts, polling and review captures."""
from copy import deepcopy
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from loadcoach.settings import said
from weightroom import voice_settings
from test_voice_admin import voice_panel
from test_voice_status_page import payload
from test_voice_telephone import phone
from test_voice_phone_registration import registration
from test_weightroom import sign_in


def test_multiple_profile_edits_and_resets_preserve_other_profiles(tmp_path):
    app, changes, original, *_ = voice_panel(tmp_path)
    original['profiles']['support'] = {**original['profiles']['default'], 'speaker':20, 'max_output_tokens':256}
    changes.seen('voice', said(original, original, {}))
    with TestClient(app) as client:
        sign_in(client, 'jordan')
        page = client.get('/system/voice')
        assert 'data-voice-panel="support"' in page.text
        changed = client.post('/settings/voice', data={'return_to':'voice', 'value-profiles.support.max_output_tokens':'512'})
        assert changed.status_code == 200
        profiles = changes.of('voice')['profiles']
        assert profiles['support']['max_output_tokens'] == 512
        assert profiles['default'] == original['profiles']['default']
        assert client.post('/settings/voice', data={'return_to':'voice', 'reset':'profiles.support.max_output_tokens'}).status_code == 200
        assert 'profiles' not in changes.of('voice')
        refused = client.post('/settings/voice', data={'return_to':'voice','value-profiles.support.max_output_tokens':'0'})
        assert refused.status_code == 400
        assert not changes.of('voice')


def telephone_data():
    return {'credentials': {'token':{'saved':True,'changed':'2026-11-14T00:00:00Z'},'api':{'saved':False}},
        'telephone':phone(), 'accounts':['voice-phone','voice-phone-support'], 'discovered':[],
        'choices_by_account':{'voice-phone':{'profiles':[{'id':'default'}],'voices':[]},
                              'voice-phone-support':{'profiles':[{'id':'support'}],'voices':[]}}}


def test_telephone_save_keeps_other_number_draft_and_clears_secrets(tmp_path):
    from test_console_browser import browser_context, playwright
    app,*_ = voice_panel(tmp_path,telephone_data=telephone_data())
    with TestClient(app) as server,playwright.sync_playwright() as engine:
        sign_in(server,'jordan');browser,page=browser_context(engine,server)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/system/voice/telephone')
        page.locator('#phone-1-greeting').fill('Unsent office draft')
        page.get_by_role('link',name='Support',exact=True).click()
        page.locator('#phone-2-greeting').fill('Saved support greeting')
        page.get_by_role('button',name='Save number',exact=True).last.click()
        playwright.expect(page.locator('.said')).to_contain_text('Saved.')
        assert page.url.split('?')[0].endswith('/system/voice/telephone')
        page.get_by_role('link',name='Office',exact=True).click()
        playwright.expect(page.locator('#phone-1-greeting')).to_have_value('Unsent office draft')
        page.get_by_role('link',name='Setup',exact=True).click()
        page.locator('[data-card-key="voice-phone-credentials"] > summary').click()
        page.locator('#telephone-token').fill('never-retain-this-secret')
        page.get_by_role('link',name='Office',exact=True).click()
        page.get_by_role('link',name='Support',exact=True).focus()
        page.keyboard.press('ArrowDown')
        playwright.expect(page.get_by_role('link',name='Setup',exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        page.get_by_role('button',name='Check credentials',exact=True).click()
        playwright.expect(page.locator('.said')).to_contain_text('Check completed.')
        assert page.locator('#telephone-token').input_value() == ''
        page.get_by_role('link',name='Office',exact=True).click()
        playwright.expect(page.locator('#phone-1-greeting')).to_have_value('Unsent office draft')
        assert not errors
        browser.close()


def test_settings_card_save_retains_other_card_draft_and_selection(tmp_path):
    from test_console_browser import browser_context, playwright
    app, changes, original,*_ = voice_panel(tmp_path)
    with TestClient(app) as server,playwright.sync_playwright() as engine:
        sign_in(server,'jordan');browser,page=browser_context(engine,server)
        page.goto('http://console.test/system/voice')
        page.locator('[name="value-profiles.default.system_prompt"]').fill('Unsent conversation draft')
        page.locator('[name="value-greetings.ready"]').fill('Saved greeting')
        page.get_by_role('button',name='Save speech',exact=True).click()
        playwright.expect(page.locator('.said')).to_contain_text('Saved.')
        playwright.expect(page.locator('[name="value-profiles.default.system_prompt"]')).to_have_value('Unsent conversation draft')
        assert changes.of('voice')['greetings']['ready'] == 'Saved greeting'
        assert 'profiles' not in changes.of('voice')
        browser.close()


@pytest.mark.parametrize('state',['ready','empty'])
def test_all_voice_pages_palettes_widths_poll_replacements_and_screenshots(tmp_path,state):
    from test_console_browser import browser_context, playwright
    data=telephone_data() if state=='ready' else {'credentials':{'token':{},'api':{}},'telephone':{}}
    status=payload() if state=='ready' else 503
    app,*_ = voice_panel(tmp_path,telephone_data=data,voice_status_data=status)
    images=Path('data/voice-admin-previews');images.mkdir(parents=True,exist_ok=True)
    with TestClient(app) as server,playwright.sync_playwright() as engine:
        sign_in(server,'jordan');browser,page=browser_context(engine,server)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        for name,path in [('settings','/system/voice'),('telephone','/system/voice/telephone'),('status','/system/voice/status')]:
            page.goto('http://console.test'+path)
            assert page.get_by_role('navigation',name='Voice administration').is_visible()
            for theme in ('light','dark'):
                page.emulate_media(color_scheme=theme)
                playwright.expect(page.locator('html')).to_have_attribute('data-bs-theme',theme)
                for width in (1440,390,320):
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(name,theme,width)
                    if width!=320:
                        page.screenshot(path=str(images/f'p9-{name}-{state}-{theme}-{width}.png'),full_page=False)
            if name=='status':
                if isinstance(status,dict):
                    status['calls']=[{'id':'new-call','source':'desktop','account':'pat','mode':'direct','profile':'default','model':'small','turns':0,'state':'listening'}]
                page.get_by_role('link',name='Phone',exact=True).click()
                page.evaluate("htmx.ajax('GET','/system/voice/status?part=content',{target:'#voice-status-content',swap:'outerHTML'})")
                playwright.expect(page.locator('[data-voice-choice="telephone"]')).to_have_attribute('aria-current','page')
                playwright.expect(page.locator('[data-voice-panel="telephone"]')).to_be_visible()
                if isinstance(status,dict):
                    playwright.expect(page.locator('.voice-verdict')).to_contain_text('1 active call')
        assert not errors
        browser.close()


def test_account_phone_section_layout_and_screenshots(registration):
    from test_console_browser import browser_context, playwright
    app,client,headers,users,store,sent,config=registration
    with TestClient(app) as server,playwright.sync_playwright() as engine:
        sign_in(server,'pat');browser,page=browser_context(engine,server)
        page.goto('http://console.test/account')
        page.locator('#register-phone-number').fill('+15555550999')
        page.get_by_role('button',name='Send verification code').click()
        playwright.expect(page.locator('#phone-verification-code')).to_be_visible()
        images=Path('data/voice-admin-previews');images.mkdir(parents=True,exist_ok=True)
        for theme,width in [('light',1440),('dark',390)]:
            page.emulate_media(color_scheme=theme)
            playwright.expect(page.locator('html')).to_have_attribute('data-bs-theme',theme)
            page.set_viewport_size({'width':width,'height':1000})
            page.locator('#phone-numbers').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(images/f'p9-account-{theme}-{width}.png'),full_page=False)
        browser.close()


def test_deleted_user_caller_entries_are_removed_before_name_reuse(tmp_path):
    app,changes,original,calls,users,switch=voice_panel(tmp_path)
    config=phone()
    config['numbers'][0]['callers']={'mode':'allowlist','entries':[{'number':'+15555550999','account':'pat','note':'Personal'}]}
    switch.front.state.console_settings.changes.put('voice-gateway','telephone',config,'owner')
    with TestClient(switch.front) as client:
        response=client.delete('/loadcoach/users/pat',headers={'authorization':'Bearer '+users.new_key('jordan')})
        assert response.status_code==200
        saved=switch.front.state.console_settings.changes.of('voice-gateway')['telephone']
        assert saved['numbers'][0]['callers']['entries']==[]
        assert '+15555550999' not in str(saved)


def test_telephone_strip_uses_live_readiness_before_claiming_ready():
    from voice.telephone_admin import verdict
    result=verdict(phone(),{'token':{'saved':True}},entry={'can_start':False,'reason':'Compute is asleep.'})
    assert result=={'state':'Needs setup','reason':'Compute is asleep.'}
    assert verdict(phone(),{'token':{'saved':True}},active=1,entry={'can_start':True})['state']=='Receiving calls'


def test_settings_reset_keeps_other_unsent_fields_in_the_same_card(tmp_path):
    from test_console_browser import browser_context,playwright
    app,changes,original,*_=voice_panel(tmp_path)
    changes.put('voice','greetings',{'ready':'Saved replacement'},'owner')
    with TestClient(app) as server,playwright.sync_playwright() as engine:
        sign_in(server,'jordan');browser,page=browser_context(engine,server)
        page.goto('http://console.test/system/voice')
        page.locator('[name="value-greetings.waiting"]').fill('Unsent waiting phrase')
        page.locator('button[name="reset"][value="greetings.ready"]').click()
        playwright.expect(page.locator('.said')).to_contain_text('Saved.')
        playwright.expect(page.locator('[name="value-greetings.ready"]')).to_have_value(voice_settings.TEXTS['ready'])
        playwright.expect(page.locator('[name="value-greetings.waiting"]')).to_have_value('Unsent waiting phrase')
        browser.close()
