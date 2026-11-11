"""Live dispatch layout, retained arrival counts, permissions and missing readings."""
import time
from pathlib import Path

from starlette.testclient import TestClient
from loadcoach.log import Log
from front_helpers import turnstile_to
from test_weightroom import desk, sign_in  # noqa: F401
from test_console_browser import browser_context, playwright
from weightroom.status_workspace import present


def test_arrival_windows_count_starts_once_with_boundaries_and_owner():
    log = Log(':memory:')
    for owner, at in [('pat',10000),('pat',9700),('pat',9699),('pat',9100),('pat',6400),('pat',6399),('pat',10001),('jordan',9999)]:
        number = log.add('request','asked',owner,model='private-model')
        log.db.execute('UPDATE events SET at=? WHERE id=?', (at,number))
        log.add('request','done',owner,of=number)
    assert log.request_counts('pat',10000) == [2,4,5]
    assert log.request_counts(None,10000) == [3,5,6]
    assert log.request_counts('nobody',10000) == [0,0,0]
    log.db.close()


def test_counts_api_is_caller_scoped_and_does_not_contact_sleeping_compute(desk,tmp_path):
    _, switch, users = desk
    log = Log(str(tmp_path/'data/accounts.db'))
    log.add('request','asked','pat',model='private-pat')
    log.add('request','asked','jordan',model='private-admin')
    switch.off = True
    with TestClient(turnstile_to(tmp_path,users,{8442:switch}),base_url='http://front:8442') as api:
        assert api.get('/loadcoach/requests/counts').status_code == 401
        for name,scope,count in [('pat','mine',1),('jordan','all',2)]:
            response = api.get('/loadcoach/requests/counts',headers={'authorization':'Bearer '+users.new_key(name)})
            assert response.status_code == 200
            assert response.json()['scope'] == scope
            assert response.json()['counts'] == [count]*3
            assert 'private-' not in response.text
        users.change('jordan',own={'remove':['see-all']})
        response = api.get('/loadcoach/requests/counts',headers={'authorization':'Bearer '+users.new_key('jordan')})
        assert response.json()['scope'] == 'mine' and response.json()['counts'] == [1]*3
    log.db.close()


def test_dispatch_counts_preserve_batch_grouping_and_unknown_values():
    row = {'id':7,'model':'batch-model','batch_id':'batch-1','user':'pat'}
    now = {'running':[row], 'waiting':[row], 'hosted':[{'id':8,'model':'remote'}],
           'active_batches':[{'id':'batch-1','request_ids':[7]}], 'others_running':2}
    view = present(now,[])
    assert len(view['processing']) == 2 and len(view['queued']) == 1
    assert view['running'] == 4 and view['waiting'] == 1
    absent = present(None,[{'name':'offline','state':'not answering','status_live':False}])
    assert absent['running'] is None and absent['counts'] == [None]*3 and absent['partial']


def test_live_dispatch_layout_filter_refresh_and_narrow_screens(desk,monkeypatch,tmp_path):
    from loadcoach.runner import Runner
    original = Runner.status
    def sample(runner):
        data = original(runner)
        data.pop('model_cards', None)  # Reproduce workers that have not received the policy update.
        data.update(memory={'CUDA0':{'total':24576,'free':8192,'others':1024}, 'AMD@0000:0e:00.0':None, 'Intel@0000:00:02.0':None},
                    loaded=[{'model':'Qwen3 14B','state':'ready','gpus':['CUDA0'],'mib':[15360],'rss_mib':2048,'locked':False}],
                    running=[{'id':41,'model':'Qwen3 14B','user':'jordan','caller':'chat','seconds':42,'priority':'normal'}],
                    waiting=[{'id':42,'model':'Qwen3 14B','user':'jordan','caller':'api','seconds':9,'priority':'normal'}])
        return data
    monkeypatch.setattr(Runner,'status',sample)
    server,_,_ = desk
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page = browser_context(engine,server)
        errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/status')
        assert page.locator('#current-requests table').count() == 2
        assert '41' in page.locator('table[aria-label="Processing now"]').inner_text()
        assert '42' in page.locator('table[aria-label="Waiting to start"]').inner_text()
        assert 'Past requests' not in page.locator('main').inner_text()
        assert 'Arrival counts use records received on front.' not in page.locator('main').inner_text()
        assert 'Sign-in service troubleshooting' not in page.locator('main').inner_text()
        assert page.locator('.status-kpis + #status-search-control + #current-requests').count() == 1
        assert page.locator('#status-capacity-heading #status-controls').count() == 1
        assert 'CPU load' not in page.locator('#status-capacity').inner_text()
        assert '1 processing · 1 queued' in page.locator('.status-node-counts').first.inner_text()
        assert '15.0 GiB VRAM' in page.locator('#status-capacity').inner_text()
        capacity_card = page.locator('#status-capacity > .card').first
        assert 'CUDA0' in capacity_card.inner_text() and 'Intel@' not in capacity_card.inner_text()
        assert 'AMD@0000:0e:00.0' not in capacity_card.inner_text()
        assert capacity_card.locator('.badge.text-bg-secondary').count() == 0
        requests=page.locator('#current-requests').bounding_box(); capacity=page.locator('#status-capacity').bounding_box()
        assert capacity['y'] > requests['y'] + requests['height']
        page.locator('#status-search').fill('42')
        assert page.locator('#requests tr[data-status-search]:visible').count() == 0
        page.evaluate("window.oldStatus=document.querySelector('#status')")
        page.wait_for_function("document.querySelector('#status') !== window.oldStatus")
        playwright.expect(page.locator('#status-search')).to_have_value('42')
        assert page.locator('#requests tr[data-status-search]:visible').count() == 0
        page.locator('#status-search').fill('')
        page.locator('input[name="why"]').fill('maintenance')
        page.evaluate("window.oldStatus=document.querySelector('#status')")
        page.wait_for_timeout(2300)
        assert page.evaluate("document.querySelector('#status') === window.oldStatus")
        page.locator('input[name="why"]').fill('')
        for width in (1600,1000,390):
            page.set_viewport_size({'width':width,'height':1050})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
        page.set_viewport_size({'width':1600,'height':1050})
        page.screenshot(path=str(tmp_path/'status-live.png'),full_page=True)
        assert not errors
        browser.close()
