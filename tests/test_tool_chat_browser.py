"""Tool chat console layout, exact results and approval drafts in desktop/mobile themes."""
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from weightroom.app import build_app, Settings
from test_tool_chat import tool_chat  # noqa: F401
from test_console_browser import browser_context, playwright

@pytest.fixture
def tool_console(tool_chat,tmp_path):
    _,_,rig=tool_chat
    data=tmp_path/'console';data.mkdir()
    for name,info in rig.infos.items():
        info.update(group='members',choices={'show_telemetry':False},compute=True,roles=[],machines=[])
    app=build_app(Settings(data=str(data),compute='http://front',key='alice'),rig.http,tools_client=rig.http)
    with TestClient(app) as console:
        console.cookies.set('weightroom','alice')
        yield console,rig


def test_tool_chat_access_is_framed_and_navigation_scoped(tool_console):
    console,rig=tool_console
    allowed=console.get('/chat/tools');assert allowed.status_code==200,allowed.text
    assert 'Tool chat</a>' in allowed.text
    rig.infos['alice']['can']=[]
    refused=console.get('/chat/tools')
    assert refused.status_code==403 and 'Tool chat needs' in refused.text and 'Tool chat</a>' not in refused.text
    assert console.get('/tools').status_code==403


@pytest.mark.parametrize('width',[1440,390])
@pytest.mark.parametrize('theme',['light','dark'])
def test_tool_chat_direct_and_approval_ui(tool_console,width,theme):
    console,rig=tool_console
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,console)
        page.set_viewport_size({'width':width,'height':1000})
        errors=[];page.on('pageerror',lambda error:errors.append(error.stack))
        page.goto('http://console.test/chat/tools')
        page.evaluate('(theme)=>document.documentElement.setAttribute("data-bs-theme",theme)',theme)
        playwright.expect(page.locator('#tool-chat-status')).to_have_text('Ready.')
        page.locator('#tool-chat-argument-0').fill('<safe>')
        page.get_by_role('button',name='Run tool',exact=True).click()
        playwright.expect(page.locator('#tool-chat-history')).to_contain_text('exact <result>')
        playwright.expect(page.locator('#tool-chat-history')).to_contain_text('isError: false')
        assert page.locator('#tool-chat-history img').count()==0
        page.locator('#tool-chat-composer textarea').fill('Use a tool')
        page.get_by_role('button',name='Send',exact=True).click()
        playwright.expect(page.locator('#tool-chat-proposals form')).to_have_count(1)
        proposal=page.locator('#tool-chat-proposals form')
        proposal.get_by_role('button',name='Edit arguments',exact=True).click()
        proposal.locator('textarea').fill('{"text":"edited in browser"}')
        page.wait_for_timeout(1200)
        playwright.expect(proposal.locator('textarea')).to_have_value('{"text":"edited in browser"}')
        proposal.get_by_role('button',name='Run',exact=True).click()
        playwright.expect(page.locator('#tool-chat-history')).to_contain_text('Finished')
        assert rig.executions[-1][1]['arguments']['text']=='edited in browser'
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        from test_ui_consistency import MEASURE
        measurements=page.evaluate(MEASURE)
        assert not measurements['namelessFields'], measurements['namelessFields']
        assert not measurements['invalidControls'], measurements['invalidControls']
        assert not measurements['mismatches'], measurements['mismatches']
        assert not errors
        browser.close()


@pytest.mark.parametrize('width',[1440,390])
def test_tool_chat_empty_models_and_unavailable_states(tool_console,width):
    console,rig=tool_console
    rig.infos['alice']['chat']['models']={}
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,console)
        page.set_viewport_size({'width':width,'height':1000})
        page.goto('http://console.test/chat/tools')
        playwright.expect(page.get_by_text('No tool-capable model is available',exact=False)).to_be_visible()
        playwright.expect(page.get_by_role('button',name='Send',exact=True)).to_be_disabled()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        rig.no_tools=True
        assert console.get('/chat/tools').status_code==403
        rig.no_tools=False;rig.unavailable=True
        response=page.goto('http://console.test/chat/tools')
        assert response.status==503
        playwright.expect(page.get_by_role('alert')).to_contain_text('ToolLocker')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        browser.close()


def test_tool_chat_sidebar_rename_and_delete_use_shared_handlers(tool_console):
    console,rig=tool_console
    number=console.post('/chat/tools/api/',json={}).json()['id']
    with playwright.sync_playwright() as engine:
        browser,page=browser_context(engine,console)
        page.goto('http://console.test/chat/tools/'+number)
        page.locator('[data-conversation-heading]').dblclick()
        form=page.locator('#chat-main > [data-chat-rename]')
        form.locator('input[name=title]').fill('<b>Safe renamed tools</b>')
        form.get_by_role('button',name='Save conversation name').click()
        playwright.expect(page.locator('[data-conversation-heading]')).to_have_text('<b>Safe renamed tools</b>')
        row=page.locator('[data-conversation="'+number+'"]')
        playwright.expect(row.locator('[data-chat-title]')).to_have_text('<b>Safe renamed tools</b>')
        assert page.locator('[data-conversation-heading] b').count()==0
        row.get_by_role('button',name='Delete <b>Safe renamed tools</b>').click()
        row.get_by_role('button',name='Delete <b>Safe renamed tools</b>').click()
        page.wait_for_url('**/chat/tools')
        assert console.get('/chat/tools/api/'+number).status_code==404
        browser.close()
