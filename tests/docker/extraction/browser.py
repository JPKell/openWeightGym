"""Live Chromium/API qualification inside Docker, with real GPU vision inference.

Run: docker compose -f tests/docker/extraction/compose.yaml run --rm browser
Sources are synthetic; credentials come only from this disposable cluster.
"""

import base64
import io
import json
import os
from pathlib import Path
import re
import time

import httpx
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from playwright.sync_api import expect, sync_playwright

from weightroom.extraction import SCHEMAS

OUT = Path('/evidence')
MODEL = 'extraction-gemma'
KEY = Path('/state/front/qualification-session.key').read_text().strip()
API = httpx.Client(base_url='http://turnstile:8442', headers={'authorization': 'Bearer ' + KEY}, timeout=240)
REPORT = []


def note(name, **detail):
    row = {'test': name, 'at': time.time(), **detail}
    REPORT.append(row)
    OUT.joinpath('results.json').write_text(json.dumps(REPORT, indent=2))
    print(json.dumps(row), flush=True)


def checked(method, path, **kwargs):
    result = API.request(method, path, **kwargs)
    assert result.is_success, (path, result.status_code, result.text[:800])
    return result


def wait_job(identifier):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        job = checked('GET', '/owg/extractions/' + identifier).json()
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(.5)
    raise AssertionError('Timed out waiting for ' + identifier)


def fixtures():
    font = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
    def make(name, rows, width=800, height=1000):
        image = Image.new('RGB', (width, height), '#fffdf7')
        draw = ImageDraw.Draw(image)
        for x, y, text, size in rows:
            draw.text((x, y), text, fill='#202624', font=ImageFont.truetype(font, size))
        path = OUT / name
        image.save(path)
        return path
    receipt = make('receipt.png', [
        (55, 55, 'CEDAR MARKET', 48), (55, 120, '18 Oak Street, Vancouver', 28),
        (55, 185, '2026-11-13  15:42', 30), (55, 245, 'RECEIPT 001 - CAD', 30),
        (55, 340, 'Coffee     2 x 4.50    9.00', 34),
        (55, 400, 'Pastry     1 x 5.40    5.40', 34),
        (55, 460, 'Tea        1 x 8.00    8.00', 34),
        (55, 570, 'Subtotal             22.40', 34),
        (55, 630, 'Tax                   1.12', 34),
        (55, 715, 'TOTAL                23.52', 38),
        (55, 830, 'VISA **** 1842', 30), (55, 910, 'Thank you!', 30),
    ])
    document = make('document.png', [
        (45, 45, 'WORKSPACE NOTES', 40), (45, 140, 'LIGHT', 32), (430, 140, 'SOUND', 32),
        (45, 195, 'Face the window.', 24), (430, 195, 'Use soft surfaces.', 24),
        (45, 240, 'Keep glare away.', 24), (430, 240, 'Leave room for focus.', 24),
        (45, 365, 'SETUP CHECKLIST', 32), (45, 440, 'Component    Qty   Status', 32),
        (45, 505, 'Sensor         4   Ready', 32), (45, 565, 'Hub            1   Check', 32),
        (45, 625, 'Cable          8   Ready', 32),
    ], height=760)
    make('refund-fr.png', [(55,55,'MARCHÉ DU CÈDRE',44),(55,130,'REMBOURSEMENT - CAD',30),
        (55,200,'2026-11-13',30),(55,320,'Café    2 x -4.50   -9.00',32),
        (55,450,'Sous-total          -9.00',32),(55,510,'Taxe                 0.00',32),
        (55,610,'TOTAL               -9.00',36)])
    return receipt, document


def run_page(page):
    page.locator('#run').click()
    expect(page.locator('#result-state')).to_have_text(re.compile('completed|needs review|failed'), timeout=240000)
    assert page.locator('#result-state').inner_text() != 'failed', page.locator('#error-message').inner_text()


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    # Only this disposable cluster's account is touched; never a host app or real user key.
    while True:
        old = checked('GET', '/owg/extractions').json()['data']
        if not old:
            break
        for job in old:
            if job['status'] in ('queued', 'running'):
                checked('POST', '/owg/extractions/' + job['id'] + '/cancel', json={})
            checked('DELETE', '/owg/extractions/' + job['id'])
    for preset in checked('GET', '/owg/extraction-presets').json()['presets']:
        checked('DELETE', '/owg/extraction-presets/' + preset['id'])
    receipt, document = fixtures()
    deadline = time.monotonic() + 120
    while True:
        models = checked('GET', '/owg/extraction-models').json()['models']
        if any(model['id'] == MODEL for model in models):
            break
        assert time.monotonic() < deadline, 'Install the stored extraction-gemma model with its projector before running qualification.'
        time.sleep(1)
    note('model_inventory', model=MODEL, models=models, source='Docker development checkout')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox', '--disable-gpu'])
        context = browser.new_context(viewport={'width':1600, 'height':1100}, accept_downloads=True)
        context.add_cookies([{'name':'weightroom', 'value':KEY, 'url':'http://console:8440'}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        response = page.goto('http://console:8440/chat/extraction', wait_until='networkidle')
        assert response.status == 200
        expect(page.locator('#model option[value="' + MODEL + '"]')).to_have_count(1)
        page.locator('#image-file').set_input_files(str(receipt))
        expect(page.locator('#source-name')).to_have_text('receipt.png')
        page.locator('#task').select_option('receipt'); page.locator('#format').select_option('json')
        run_page(page)
        listed = checked('GET', '/owg/extractions').json()['data']
        job = next(job for job in listed if job['asset']['name'] == 'receipt.png')
        assert job['data']['merchant'].lower() == 'cedar market', job['data']
        assert job['data']['total'] == '23.52', job['data']
        assert job['data']['tax'] == '1.12', job['data']
        assert len(job['data']['items']) == 3, job['data']
        note('live_receipt', id=job['id'], data=job['data'], provenance=job['provenance'], usage=job['usage'])
        page.screenshot(path=str(OUT / 'desk-receipt.png'), full_page=True)
        page.locator('#field-tax').fill('2.12')
        page.locator('#save-review').click()
        expect(page.locator('#result-state')).to_have_text('needs review')
        original = checked('GET', '/owg/extractions/' + job['id'] + '/export?format=json&revision=original').json()
        corrected = checked('GET', '/owg/extractions/' + job['id']).json()
        assert original['tax'] == '1.12' and corrected['data']['tax'] == '2.12'
        assert any(warning['code'] == 'totals_mismatch' for warning in corrected['warnings'])
        page.locator('#review-toggle').click(); expect(page.locator('#result-state')).to_have_text('Reviewed')
        for format_ in ('plain', 'layout', 'markdown', 'json', 'csv'):
            exported = checked('GET', '/owg/extractions/' + job['id'] + '/export?format=' + format_)
            assert 'Coffee' in exported.text
        with page.expect_download() as download:
            page.locator('#download').click()
        download.value.save_as(str(OUT / download.value.suggested_filename))
        note('corrections_exports', original_tax=original['tax'], corrected_tax=corrected['data']['tax'], formats=5)
        page.locator('#image-file').set_input_files(str(document))
        expect(page.locator('#toggle-batch')).to_have_attribute('aria-expanded', 'true')
        page.get_by_role('button', name=re.compile('document.png')).click()
        page.locator('#task').select_option('text'); page.locator('#format').select_option('layout')
        page.locator('#instructions').fill('Preserve the two columns above the checklist.')
        run_page(page)
        page.locator('[data-result-view="raw"]').click()
        layout = page.locator('#raw-output').inner_text()
        assert 'LIGHT' in layout and 'SOUND' in layout and 'Sensor' in layout
        note('live_layout_text', contains_columns=True, output=layout)
        page.get_by_role('button', name=re.compile('receipt.png')).click()
        page.locator('#task').select_option('describe'); page.locator('#format').select_option('markdown')
        page.get_by_role('button', name=re.compile('document.png')).click()
        expect(page.locator('#instructions')).to_have_value('Preserve the two columns above the checklist.')
        page.locator('#task').select_option('table'); page.locator('#format').select_option('csv')
        page.locator('#instructions').fill('Extract only the setup checklist table.')
        page.locator('#run-batch').click()
        expect(page.locator('#action-status')).to_contain_text('Extraction finished', timeout=240000)
        assert 'Sensor' in page.locator('#raw-output').inner_text()
        jobs = checked('GET', '/owg/extractions').json()['data']
        table = next(row for row in jobs if row['task'] == 'table')
        description = next(row for row in jobs if row['task'] == 'describe')
        assert table['status'] in ('completed', 'needs_review') and len(table['data']['rows']) == 3, table
        assert 'receipt' in description['data']['description'].lower(), description['data']
        note('live_batch', table=table['data'], description=description['data'], jobs=[table['id'], description['id']])
        page.screenshot(path=str(OUT / 'batch.png'), full_page=True)
        page.get_by_role('button', name=re.compile('receipt.png')).click()
        page.locator('#task').select_option('custom'); page.locator('#format').select_option('json')
        run_page(page)
        custom = next(row for row in checked('GET', '/owg/extractions').json()['data'] if row['task'] == 'custom')
        assert custom['data']['total'] == '23.52', custom
        note('live_custom_fields', data=custom['data'])
        # Clipboard fallback remains usable on this Docker HTTP origin.
        page.locator('#copy').click()
        expect(page.locator('#action-status')).to_contain_text(re.compile('copied|selected'))
        page.get_by_text('Extraction presets', exact=True).click()
        page.locator('#preset-name').fill('Receipt fields')
        page.locator('#save-preset').click()
        expect(page.locator('#preset option')).to_have_count(2)
        page.locator('#task').select_option('describe')
        page.locator('#preset').select_option(label='Receipt fields')
        expect(page.locator('#task')).to_have_value('custom')
        note('preset_and_clipboard', passed=True)
        for theme in ('light', 'dark'):
            page.evaluate('''mode => { const form=document.createElement('form'); form.dataset.themeForm=''; form.innerHTML='<div data-theme-options></div>'; const select=document.createElement('select'); select.name='mode'; select.add(new Option(mode,mode)); form.append(select); document.body.append(form); select.dispatchEvent(new Event('change',{bubbles:true})); form.remove(); }''', theme)
            for width in (1600, 1024, 760, 390, 320):
                page.set_viewport_size({'width':width, 'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
            page.screenshot(path=str(OUT / ('mobile-' + theme + '.png')), full_page=True)
        assert not errors, errors
        note('browser_responsive', themes=2, widths=[1600,1024,760,390,320], errors=errors)
        # Navigation preserves the requested ordering in desktop and mobile menus.
        links = page.locator('#site-sidebar a').evaluate_all('els => els.map(el=>el.getAttribute("href"))')
        assert links.index('/chat/extraction') == links.index('/chat/images') + 1
        note('navigation', below_image_generation=True)
        browser.close()
    image_url = 'data:image/png;base64,' + base64.b64encode(receipt.read_bytes()).decode()
    schema = {'type':'object','properties':{'merchant':{'type':['string','null']},'total':{'type':['string','null']}},'required':['merchant','total'],'additionalProperties':False}
    sdk = OpenAI(base_url='http://turnstile:8442/v1', api_key=KEY, timeout=240)
    sdk_response = sdk.responses.create(**{'model':MODEL,'store':False,
        'input':[{'role':'user','content':[{'type':'input_text','text':'Extract merchant and total as JSON. Use decimal strings.'}, {'type':'input_image','image_url':image_url}]}],
        'text':{'format':{'type':'json_schema','name':'receipt','schema':schema,'strict':True}},'max_output_tokens':1024})
    response = sdk_response.model_dump()
    assert sdk_response.output_text
    parsed = json.loads(response['output'][0]['content'][0]['text'])
    assert parsed['total'] == '23.52', response
    note('responses_api', status=response['status'], data=parsed, usage=response['usage'])
    completion = sdk.chat.completions.create(model=MODEL, messages=[{'role':'user','content':[
        {'type':'text','text':'Extract merchant and total as JSON decimal strings.'}, {'type':'image_url','image_url':{'url':image_url}}]}],
        response_format={'type':'json_schema','json_schema':{'name':'receipt','schema':schema,'strict':True}}, max_tokens=1024)
    assert json.loads(completion.choices[0].message.content)['total'] == '23.52'
    note('openai_sdk_chat', usage=completion.usage.model_dump())
    asset_id = custom['asset_id']
    mcp = httpx.Client(base_url='http://turnstile:8443', headers={'authorization':'Bearer '+KEY}, timeout=40)
    tools = mcp.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).json()['result']['tools']
    assert any(tool['name'] == 'extraction__extract_receipt' for tool in tools), tools
    started = mcp.post('/mcp', json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
        'name':'extraction__extract_receipt','arguments':{'asset_id':asset_id,'model':MODEL,'output_format':'json'}}}).json()
    assert not started['result'].get('isError'), started
    tool_job = json.loads(started['result']['content'][0]['text'])
    result = wait_job(tool_job['id'])
    assert result['data']['total'] == '23.52', result
    status = mcp.post('/mcp', json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
        'name':'extraction__extraction_status','arguments':{'job_id':tool_job['id']}}}).json()
    assert not status['result'].get('isError'), status
    note('mcp_tools', names=[tool['name'] for tool in tools], job=tool_job['id'], usage=result['usage'])
    # The existing OpenAI Batch route accepts the same ordinary vision/schema request.
    line = {'custom_id':'receipt-summary', 'method':'POST', 'url':'/v1/chat/completions', 'body':{
        'model':MODEL, 'messages':[{'role':'user','content':[{'type':'text','text':'Extract merchant and total as JSON decimal strings.'},
            {'type':'image_url','image_url':{'url':image_url}}]}],
        'response_format':{'type':'json_schema','json_schema':{'name':'receipt','schema':schema,'strict':True}},
        'max_tokens':1024}}
    uploaded = sdk.files.create(file=('receipt.jsonl', (json.dumps(line)+'\n').encode(), 'application/jsonl'), purpose='batch')
    batch = sdk.batches.create(input_file_id=uploaded.id, endpoint='/v1/chat/completions', completion_window='24h')
    deadline = time.monotonic() + 240
    while batch.status not in ('completed','failed','cancelled','expired'):
        assert time.monotonic() < deadline, batch
        time.sleep(.5)
        batch = sdk.batches.retrieve(batch.id)
    assert batch.status == 'completed' and batch.request_counts.completed == 1, batch
    batch_output = sdk.files.content(batch.output_file_id).text
    row = json.loads(batch_output.splitlines()[0])
    values = json.loads(row['response']['body']['choices'][0]['message']['content'])
    assert values['total'] == '23.52', row
    note('openai_sdk_batch', batch=batch.id, counts=batch.request_counts.model_dump(), data=values)
    cancelled = checked('POST','/owg/extractions',json={'asset_id':asset_id,'model':MODEL,'task':'describe','output_format':'json'}).json()
    stopped = checked('POST','/owg/extractions/'+cancelled['id']+'/cancel',json={}).json()
    assert stopped['status'] == 'cancelled', stopped
    assert wait_job(cancelled['id'])['status'] == 'cancelled'
    note('live_cancel', job=cancelled['id'], status='cancelled')
    checked('POST', '/loadcoach/roles/extraction-vision', json={'models':{MODEL:1}})
    role_job = checked('POST','/owg/extractions',json={'asset_id':asset_id,'model':'extraction-vision','task':'custom','output_format':'json','schema':schema}).json()
    routed = wait_job(role_job['id'])
    assert routed['status'] in ('completed','needs_review') and routed['data']['total'] == '23.52', routed
    assert routed['provenance']['node'] == 'alpha' and routed['provenance']['model'] == MODEL, routed['provenance']
    note('live_role', requested_model='extraction-vision', provenance=routed['provenance'])
    # Harder synthetic cases report measured outcomes; no model accuracy is inferred from a schema pass.
    corpus = []
    with Image.open(receipt) as image:
        image.rotate(90, expand=True).save(OUT/'rotated-receipt.png')
        image.filter(ImageFilter.GaussianBlur(1.2)).save(OUT/'blurred-receipt.png')
    Image.new('RGB',(800,600),'white').save(OUT/'empty-image.png')
    for filename, task in [('rotated-receipt.png','receipt'),('blurred-receipt.png','receipt'),('refund-fr.png','receipt'),('empty-image.png','text')]:
        source = checked('POST','/owg/extraction-assets',files={'image':(filename,(OUT/filename).read_bytes(),'image/png')}).json()
        created = checked('POST','/owg/extractions',json={'asset_id':source['id'],'model':MODEL,'task':task,'output_format':'json','temporary':True}).json()
        output = wait_job(created['id'])
        assert output['status'] in ('completed','needs_review'), output
        if task == 'receipt':
            expected={'merchant':'marché du cèdre','tax':'0.00','total':'-9.00'} if filename=='refund-fr.png' else {'merchant':'cedar market','tax':'1.12','total':'23.52'}
            score=sum(str(output['data'].get(key,'')).casefold()==value for key,value in expected.items())
            corpus.append({'source':filename,'exact_fields':score,'fields':3,'warnings':output['warnings'],'data':output['data'],'usage':output['usage']})
        else:
            assert output['data']['lines']==[], output
            corpus.append({'source':filename,'empty_text':True,'warnings':output['warnings'],'usage':output['usage']})
    note('quality_corpus', cases=corpus)
    note('complete', passed=True, model_downloads=[])


if __name__ == '__main__':
    try:
        run()
    except BaseException as error:
        OUT.mkdir(parents=True, exist_ok=True)
        note('failure', error=str(error))
        raise
