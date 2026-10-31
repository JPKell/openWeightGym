"""Collection forms, private source links, offline metadata and real browser uploads."""
import base64
from pathlib import Path

import pytest

from test_collections import EMBED
from test_weightroom import desk,sign_in


def seed(browser,users):
    service=browser.app.state.conversations.state.knowledge
    def populate():
        owner=users.identity('jordan')
        number=service.store.create(owner,'Cedar sources','normal',EMBED)
        raw=b'The Cedar budget is 47.\n<script>untrusted source</script>'
        doc=service.store.add(owner,number,'notes.txt','text/plain',base64.b64encode(raw).decode(),'digest')
        token=service.store.begin(owner,number,doc)
        assert service.store.finish(owner,number,doc,token,1,[dict(page=0,line=1,text=raw.decode(),vector=[1.,0.])])
        return number,doc,service.store.chunks(owner,number)[0]['id']
    return browser.portal.call(populate)


def test_collection_forms_source_links_identity_and_offline_front_reads(desk):
    browser,switch,users=desk
    number,doc,chunk=seed(browser,users);path='/collections/'+number
    sign_in(browser,'jordan')
    page=browser.get(path);assert page.status_code==200,page.text
    for label in ('Cedar sources','Search and ask','Rebuild embedding space','notes.txt','Collections'):
        assert label in page.text
    helper='Collections make human evaluation across multiple models repeatable'
    assert helper not in page.text and '>All collections</a>' not in page.text
    assert page.text.index('Delete collection and sources') < page.text.index('>Embedding space</summary>')
    assert helper in browser.get('/collections').text
    source=browser.get(path+'/sources/'+chunk)
    assert source.status_code==200 and '&lt;script&gt;untrusted source&lt;/script&gt;' in source.text
    assert '<script>untrusted source</script>' not in source.text
    downloaded=browser.get(path+'/documents/'+doc+'/download')
    assert downloaded.status_code==200 and downloaded.headers['content-disposition']=='attachment'
    bad=browser.post(path,data={'do':'query','question':'Cedar','minimum':'NaN'})
    assert bad.status_code==400 and 'numeric minimum' in bad.text
    upload=browser.post(path,data={'do':'upload','name':'extra.txt','data':base64.b64encode(b'extra source').decode()})
    assert upload.status_code==200 and 'extra.txt' in upload.text
    assert browser.post(path,data={'do':'rebuild','model':'embed'}).status_code==400
    switch.off=True
    offline=browser.get(path)
    assert offline.status_code==200 and 'notes.txt' in offline.text and 'Node unavailable: compute' in offline.text
    switch.off=False
    sign_in(browser,'pat')
    assert browser.get(path).status_code==404 and browser.get(path+'/sources/'+chunk).status_code==404
    sign_in(browser,'jordan')
    assert browser.post('/users/jordan',data={'do':'rename','name':'renamed-owner'}).status_code==200
    assert browser.get(path).status_code==200


def test_collection_upload_and_source_pages_in_chromium(desk):
    from playwright.sync_api import sync_playwright,expect
    server,_,users=desk
    number,doc,chunk=seed(server,users);sign_in(server,'jordan')
    folder=Path('data/m105-verification');folder.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as engine:
        browser=engine.chromium.launch(headless=True)
        context=browser.new_context()
        # This layout test serves finite responses; do not route the live telemetry SSE.
        context.add_init_script('window.EventSource = class { close() {} };')
        page=context.new_page();errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def serve(route):
            if route.request.url.split('?', 1)[0] == 'http://knowledge.test/strip/stream':
                from front_helpers import serve_browser_request
                return serve_browser_request(server, 'http://knowledge.test', route)
            request=route.request
            response=server.request(request.method,request.url.replace('http://knowledge.test',''),
                content=request.post_data_buffer,headers={'HX-Request':request.headers.get('hx-request','false')})
            route.fulfill(status=response.status_code,headers={key:value for key,value in response.headers.items() if key in ('content-type','location')},body=response.content)
        context.route('http://knowledge.test/**',serve)
        for width in (1440,390,320):
            page.set_viewport_size({'width':width,'height':1000})
            page.goto('http://knowledge.test/collections/'+number)
            expect(page.get_by_role('heading',name='Cedar sources')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(folder/f'collections-{width}.png'),full_page=True)
            page.goto('http://knowledge.test/collections/'+number+'/sources/'+chunk)
            expect(page.get_by_text('<script>untrusted source</script>',exact=False)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.goto('http://knowledge.test/collections/'+number)
        page.locator('#collection-file').set_input_files({'name':'browser.txt','mimeType':'text/plain','buffer':b'Browser source text.'})
        expect(page.get_by_role('button',name='Upload source')).to_be_enabled()
        page.get_by_role('button',name='Upload source').click()
        expect(page.get_by_role('link',name='browser.txt',exact=True)).to_be_visible()
        assert not errors
        browser.close()
