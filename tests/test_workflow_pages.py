"""Private workflow history, plain-text previews and compact browser controls."""
from pathlib import Path

from test_weightroom import desk,sign_in
from test_collection_pages import seed as source_seed
from test_collections import CHAT
from weightroom.workflow_store import builtin,digest
from weightroom.knowledge import ANSWER_SYSTEM


def seed(browser,users):
    collection,doc,chunk=source_seed(browser,users)
    service=browser.app.state.conversations.state.workflows
    def populate():
        owner=users.identity('jordan');knowledge=browser.app.state.conversations.state.knowledge
        coll=knowledge.store.one(owner,collection)
        config={'version':1,'source_limit':3,'temperature':0,'outputs':{'research':1024,'draft':2048},
                'models':{s:CHAT for s in ('research','draft')},'prompts':{s:builtin(s) for s in ('research','draft')},'systems':{}}
        for step in ('research','draft'):
            system=ANSWER_SYSTEM+'\n\nWriting task:\n'+builtin(step)['body']
            config['systems'][step]={'content':system,'digest':digest(system)}
        number=service.store.create(owner,'Cedar draft','What is the budget?',.65,coll,config)
        attempt=service.store.begin(owner,number)
        service.store.sources(owner,number,attempt['epoch'],knowledge.store.chunks(owner,collection))
        service.store.receipt(owner,number,attempt['id'],{'request':'12','trace':'test','stage':'research'})
        service.store.usage(owner,number,attempt['id'],{'requests':[dict(request='12',cost=None,prompt_tokens=22,output_tokens=8)],'cost':None})
        service.store.finish(owner,number,attempt,'review_research',{'answer':'<script>research as data</script>\nThe budget is 47.','source_ids':[chunk],'abstain':False})
        service.store.accept(owner,number,'research')
        attempt=service.store.begin(owner,number)
        service.store.finish(owner,number,attempt,'failed_draft',error='Draft transport failed. Accepted research is retained.')
        return number
    return browser.portal.call(populate)


def test_workflow_forms_history_and_offline_owner_access(desk):
    browser,switch,users=desk;number=seed(browser,users);path='/workflows/'+number
    sign_in(browser,'jordan')
    page=browser.get(path)
    assert page.status_code==200,page.text
    for label in ('Cedar draft','Accepted','Resume / retry draft','Attempts and usage','Unavailable','22','Frozen configuration'):
        assert label in page.text
    helper='Research from your private collections, review and accept the research'
    assert '>All workflows</a>' not in page.text and 'What is the budget?' not in page.text
    assert 'Refresh request usage' not in page.text
    assert helper in browser.get('/workflows').text
    assert '<script>research as data</script>' not in page.text
    assert '&lt;script&gt;research as data&lt;/script&gt;' in page.text
    response=browser.post('/workflows',data={'do':'prompt','name':'Brief','kind':'research','body':'Keep source facts. <script>prompt as data</script>'})
    assert response.status_code==200 and 'Brief' in response.text
    response=browser.post('/workflows',data={'do':'prompt','name':'Brief','kind':'research','body':'A later immutable version.'})
    assert response.status_code==200 and 'v2' in response.text
    switch.off=True
    page=browser.get(path);assert page.status_code==200 and 'research as data' in page.text and 'Node unavailable: compute' in page.text
    switch.off=False;sign_in(browser,'sam')
    assert browser.get(path).status_code==404
    sign_in(browser,'jordan')
    assert browser.post(path,data={'do':'delete'}).status_code==200
    assert browser.get(path).status_code==404


def test_workflow_pages_and_prompt_versions_in_chromium(desk):
    from playwright.sync_api import sync_playwright,expect
    server,_,users=desk;number=seed(server,users);sign_in(server,'jordan')
    folder=Path('data/m106-verification');folder.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True, args=["--disable-gpu"]);context=browser.new_context()
        # This layout test serves finite responses; do not route the live telemetry SSE.
        context.add_init_script('window.EventSource = class { close() {} };')
        page=context.new_page();errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def serve(route):
            if route.request.url.split('?', 1)[0] == 'http://workflow.test/strip/stream':
                from front_helpers import serve_browser_request
                return serve_browser_request(server, 'http://workflow.test', route)
            request=route.request
            response=server.request(request.method,request.url.replace('http://workflow.test',''),content=request.post_data_buffer,
                headers={'HX-Request':request.headers.get('hx-request','false')})
            route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k in ('content-type','location')},body=response.content)
        context.route('http://workflow.test/**',serve)
        for width in (1440,390,320):
            page.set_viewport_size({'width':width,'height':1000})
            page.goto('http://workflow.test/workflows/'+number)
            expect(page.get_by_role('button',name='Resume / retry draft')).to_be_visible()
            expect(page.locator('#research-output')).to_contain_text('<script>research as data</script>')
            summary=page.get_by_text('Frozen configuration and prompt versions',exact=True)
            if not summary.evaluate('(element)=>element.closest("details").open'):summary.click()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(folder/f'workflow-{width}.png'),full_page=True)
        page.goto('http://workflow.test/workflows')
        library=page.get_by_text('Prompt library',exact=True)
        if not library.evaluate('(element)=>element.closest("details").open'):library.click()
        page.locator('#prompt-name').fill('Browser prompt')
        page.locator('#prompt-body').fill('Write a brief source-based note.')
        page.get_by_role('button',name='Save new prompt version').click()
        library=page.get_by_text('Prompt library',exact=True)
        if not library.evaluate('(element)=>element.closest("details").open'):library.click()
        expect(page.get_by_text('Browser prompt · research · v1',exact=True)).to_be_visible()
        assert not errors
        browser.close()
