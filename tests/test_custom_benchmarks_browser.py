"""JSON-file import and regression inspection in the existing benchmark pages."""

import json

import pytest

from benchmarks.store import Benchmarks
from loadcoach.models import Models
from test_app import model
from test_console_browser import browser_context, playwright
from test_custom_benchmarks import dataset, recorded
from test_weightroom import desk, sign_in  # noqa: F401


@pytest.mark.parametrize('width',[1440,390,320])
def test_custom_import_and_case_regression_browser(desk,tmp_path,width):
    server,_,_ = desk
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page = browser_context(engine,server)
        page.set_viewport_size({'width':width,'height':1000})
        errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://console.test/benchmarks/manage')
        page.locator('#case-file').set_input_files({'name':'checks.json','mimeType':'application/json','buffer':json.dumps(dataset()).encode()})
        playwright.expect(page.locator('#case-dataset')).to_have_value(json.dumps(dataset()))
        page.get_by_role('button',name='Import case set',exact=True).click()
        playwright.expect(page.get_by_text('Imported immutable case-set version.',exact=True)).to_be_visible()
        playwright.expect(page.locator('#benchmark-case-import')).to_contain_text('My <checks>')
        store=Benchmarks(tmp_path/'data'/'benchmarks.db')
        baseline=recorded(store,['{"value":7}']); current=recorded(store,['{"value":8}'])
        library=Models(tmp_path/'loadcoach.db'); library.put('small',model('small.gguf',0))
        with store.db:
            for number in (baseline,current): store.db.execute('UPDATE benchmark_runs SET model_id=? WHERE id=?',(library.identity('small'),number))
        page.goto('http://console.test/benchmarks/run/'+current)
        page.locator('#case-baseline').select_option(baseline)
        page.get_by_role('button',name='Compare cases',exact=True).click()
        playwright.expect(page.get_by_role('table',name='Case regression comparison')).to_contain_text('new failure')
        playwright.expect(page.get_by_text('new failure: 1',exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#benchmark-regressions').screenshot(path=str(tmp_path/f'case-regressions-{width}.png'))
        assert not errors
        library.db.close(); store.db.close(); browser.close()
