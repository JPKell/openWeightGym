"""Opt-in Chromium qualification over real, separately networked OWG services.

OWG_IMAGE_DOCKER=1 .venv/bin/pytest tests/test_image_install_docker.py -n 0 -q
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get('OWG_IMAGE_DOCKER') != '1', reason='Opt-in Docker deployment')

COMPOSE = Path(__file__).parent / 'docker/images/compose.yaml'
SOURCE = '/state/front/storage/image/'


def until(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.2)
    raise AssertionError('Docker qualification did not finish before its deadline')


@pytest.fixture(scope='module')
def cluster():
    project = 'owg-image-test-' + uuid.uuid4().hex[:10]
    env = {**os.environ, 'OWG_IMAGE_TEST_PORT': '0'}
    def compose(*args):
        return subprocess.check_output(['docker', 'compose', '-p', project, '-f', str(COMPOSE), *args],
                                       env=env, text=True, timeout=180)
    def inside(service, code, *args):
        return compose('exec', '-T', service, 'python', '-c', code, *args).strip()
    def api(method, path, body=None):
        return json.loads(inside('turnstile', """
import httpx,json,pathlib,sys
key=pathlib.Path('/state/front/test-session.key').read_text()
r=httpx.request(sys.argv[1],'http://127.0.0.1:8442'+sys.argv[2],
    headers={'authorization':'Bearer '+key},json=json.loads(sys.argv[3]),timeout=60)
print(json.dumps({'status':r.status_code,'body':r.json()}))
""", method, path, json.dumps(body)))
    try:
        compose('build', 'seed')
        compose('up', '-d', '--no-build')
        port = compose('port', 'console', '8440').strip().rsplit(':', 1)[1]
        key = inside('turnstile', "from pathlib import Path; print(Path('/state/front/test-session.key').read_text())")
        client = httpx.Client(base_url='http://127.0.0.1:' + port, cookies={'weightroom': key}, timeout=60)
        def ready():
            try:
                r = client.get('/mirrorwall-models')
                return r.is_success and len(r.json()['nodes']) == 2 and all(n['state'] == 'ready' for n in r.json()['nodes'])
            except (httpx.HTTPError, ValueError):
                return False
        until(ready)
        yield SimpleNamespace(compose=compose, inside=inside, api=api, client=client, key=key,
                              origin=str(client.base_url).rstrip('/'))
        client.close()
    except BaseException:
        # Keep keys out of diagnostics; app logs contain no qualification credentials.
        print(compose('logs', '--tail', '20'))
        raise
    finally:
        compose('down', '-v', '--remove-orphans')


def installations(cluster, jobs):
    ids = {job['id'] for job in jobs}
    def finished():
        answer = cluster.api('GET', '/turnstile/artifacts/distributions')
        assert answer['status'] == 200, answer
        rows = [row for row in answer['body']['distributions'] if row['id'] in ids]
        if len(rows) == len(ids) and all(row['state'] in ('complete', 'failed') for row in rows):
            assert all(row['state'] == 'complete' for row in rows), [
                {key:row.get(key) for key in ('name','machine','state','why')} for row in rows]
            return rows
    return until(finished)


def saved(cluster, node):
    r = cluster.client.get('/mirrorwall-models', params={'node': node})
    assert r.is_success, r.text
    return {model['name']: model for model in r.json()['models']}


def verify_bundle(cluster, name, nodes, relative):
    def hashes(service, folder):
        return json.loads(cluster.inside(service, """
import hashlib,json,pathlib,sys
folder=pathlib.Path(sys.argv[1])
print(json.dumps({p:hashlib.file_digest((folder/p).open('rb'),'sha256').hexdigest() for p in json.loads(sys.argv[2])}))
""", folder, json.dumps(relative)))
    original = hashes('turnstile', SOURCE)
    assert hashes('turnstile', '/state/front/cache/image/') == original
    for node in nodes:
        model = saved(cluster, node)[name]
        assert model['engine'] == 'sdcpp' and model['gpus'] == 0
        root = f'/state/{node}/cache/image/'
        assert model['flags']['model'].startswith(root)
        for path in model['sdcpp']['components'].values():
            assert path.startswith(root)
        for entry in model['sdcpp'].get('loras', {}).values():
            assert entry['file'].startswith(root)
        if model['sdcpp'].get('upscaler'):
            assert model['sdcpp']['upscaler'].startswith(root)
        assert hashes(node, root) == original
        # Separate volumes ensure a source path cannot accidentally resolve on a destination.
        assert cluster.inside(node, "from pathlib import Path; print(Path('/state/front/storage').exists())") == 'False'
    assert hashes('turnstile', SOURCE) == original


@pytest.mark.parametrize('nodes,family,files,width', [
    (['compute'], 'sdxl', {'diffusion': 'sdxl/checkpoint.safetensors'}, 1440),
    (['compute', 'rig'], 'qwen-image', {'diffusion': 'qwen/diffusion.safetensors',
        'text_encoder': 'qwen/encoder.safetensors', 'vae': 'qwen/vae.safetensors',
        'upscaler': 'upscalers/4x.pth'}, 390),
])
def test_browser_adds_image_models_and_copies_the_bundle(cluster, nodes, family, files, width):
    from playwright.sync_api import expect, sync_playwright
    name = 'docker-' + family
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=['--disable-gpu'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_cookies([{'name': 'weightroom', 'value': cluster.key, 'url': cluster.origin}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(cluster.origin + '/models/images?new=1')
        expect(page.locator('#model-dialog')).to_be_visible()
        expect(page.locator('#mw-diffusion option[value="' + SOURCE + files['diffusion'] + '"]')).to_have_count(1)
        page.locator('#mw-name').fill(name)
        page.locator('#mw-family').select_option(family)
        page.locator('[data-hardware-prefix=mw] input[data-hardware-mode][value=cpu]').check(force=True)
        for slot, file in files.items():
            page.locator('#mw-' + slot).select_option(SOURCE + file)
        relative = list(files.values())
        if family == 'qwen-image':
            page.locator('#mw-add-lora').click()
            page.get_by_label('LoRA Name', exact=True).fill('style')
            page.get_by_label('LoRA File', exact=True).fill(SOURCE + 'adapters/style.safetensors')
            page.get_by_label('LoRA Strength', exact=True).fill('.5')
            page.get_by_label('LoRA Strength', exact=True).blur()
            relative.append('adapters/style.safetensors')
        expect(page.locator('#mw-check')).to_contain_text('Everything this model needs is here')
        for target in page.locator('#mw-install-targets input').all():
            target.set_checked(target.input_value() in nodes, force=True)
        expect(page.locator('#mw-diffusion option:checked')).to_have_text(
            'front · ' + files['diffusion'].split('/')[-2] + ' · ' + files['diffusion'].split('/')[-1])
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        with page.expect_response(lambda r: '/mirrorwall-models?' in r.url and r.request.method == 'POST') as response:
            page.get_by_role('button', name='Save model', exact=True).click()
        answer = response.value
        assert answer.status == 202, answer.text()
        page.wait_for_url('**/models?kind=image&install=*')
        jobs = [{'id': id} for id in parse_qs(urlsplit(page.url).query)['install']]
        assert len(jobs) == len(nodes)
        installations(cluster, jobs)
        verify_bundle(cluster, name, nodes, relative)
        if len(nodes) == 1:
            assert name not in saved(cluster, 'rig')
        page.goto(cluster.origin + '/models/images?model=' + name)
        expect(page.locator('#studio-model-list')).to_contain_text(name)
        expect(page.locator('#mw-node')).to_have_value('')
        expect(page.locator('#studio-model-list [data-model="' + name + '"]')).to_have_count(len(nodes))
        for node in nodes:
            page.locator('#mw-node').select_option(node)
            expect(page.locator('#studio-model-list [data-model="' + name + '"]')).to_have_count(1)
            page.locator('#studio-model-list [data-model="' + name + '"]').click()
            page.locator('#studio-edit-model').click()
            expect(page.locator('#mw-diffusion')).to_have_value(f'/state/{node}/cache/image/' + files['diffusion'])
            page.keyboard.press('Escape')
        page.locator('#mw-node').select_option('')
        expect(page.locator('#studio-model-list [data-model="' + name + '"]')).to_have_count(len(nodes))
        assert not errors, errors
        browser.close()


def qwen_settings():
    return {'engine': 'sdcpp', 'flags': {'model': SOURCE + 'qwen/diffusion.safetensors', 'ctx-size': 0},
            'gpus': 0, 'sdcpp': {'family': 'qwen-image', 'components': {
                'text_encoder': SOURCE + 'qwen/encoder.safetensors', 'vae': SOURCE + 'qwen/vae.safetensors'},
                'loras': {'style': {'file': SOURCE + 'adapters/style.safetensors', 'scale': .5}}}}


def test_concurrent_multi_node_installs_share_verified_copies(cluster):
    def add(name):
        settings = qwen_settings()
        settings['flags']['model'] = SOURCE + 'concurrent/diffusion.safetensors'
        return cluster.client.post('/mirrorwall-models', json={
            'new': True, 'name': name, 'machines': ['compute', 'rig'],
            'source_machine': 'front', 'settings': settings})
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(add, ['concurrent-a', 'concurrent-b']))
    jobs = []
    for response in responses:
        assert response.status_code == 202, response.text
        jobs.extend(response.json()['jobs'])
    assert len({j['id'] for j in jobs}) == 4
    installations(cluster, jobs)
    for name in ('concurrent-a', 'concurrent-b'):
        verify_bundle(cluster, name, ['compute', 'rig'], [
            'concurrent/diffusion.safetensors', 'qwen/encoder.safetensors', 'qwen/vae.safetensors',
            'adapters/style.safetensors'])


def test_preflight_failure_starts_no_partial_multi_node_install(cluster):
    initial = cluster.client.post('/mirrorwall-models', json={
        'new': True, 'name': 'preflight-taken', 'machines': ['compute'],
        'source_machine': 'front', 'settings': qwen_settings()})
    assert initial.status_code == 202, initial.text
    installations(cluster, initial.json()['jobs'])
    before = cluster.api('GET', '/turnstile/artifacts/distributions')['body']['distributions']
    for name, nodes in [('preflight-taken', ['rig', 'compute']), ('unknown-destination', ['compute', 'missing'])]:
        response = cluster.client.post('/mirrorwall-models', json={
            'new': True, 'name': name, 'machines': nodes, 'source_machine': 'front', 'settings': qwen_settings()})
        assert response.status_code in (400, 409), response.text
        assert name not in saved(cluster, 'rig')
    after = cluster.api('GET', '/turnstile/artifacts/distributions')['body']['distributions']
    assert {j['id'] for j in after} == {j['id'] for j in before}


def test_killed_front_retains_request_and_browser_can_retry(cluster):
    """Stop a real transfer at its receiver, kill Turnstile, then retry its saved request."""
    settings=qwen_settings();settings['flags']['model']=SOURCE+'restart/diffusion.safetensors'
    cluster.compose('pause','compute-files')
    try:
        answer=cluster.client.post('/mirrorwall-models',json={
            'new':True,'name':'restart-recovery','machines':['compute'],'source_machine':'front','settings':settings})
        assert answer.status_code==202,answer.text
        original=answer.json()['jobs'][0]['id']
        def copying():
            jobs=cluster.api('GET','/turnstile/artifacts/distributions')['body']['distributions']
            return next(j for j in jobs if j['id']==original)['state']=='copying'
        until(copying,seconds=20)
        time.sleep(2.2)  # Include an in-flight progress checkpoint, not only acceptance.
        cluster.compose('kill','-s','SIGKILL','turnstile')
    finally:
        cluster.compose('unpause','compute-files')
    cluster.compose('up','-d','--no-deps','turnstile')
    def ready():
        try:return cluster.client.get('/mirrorwall-models').is_success
        except httpx.HTTPError:return False
    until(ready)
    jobs=cluster.api('GET','/turnstile/artifacts/distributions')['body']['distributions']
    interrupted=next(j for j in jobs if j['id']==original)
    assert interrupted['state']=='failed' and interrupted['interrupted'] and interrupted['can_retry']
    from playwright.sync_api import sync_playwright,expect
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True,args=['--disable-gpu'])
        context=browser.new_context(viewport={'width':390,'height':1000})
        context.add_cookies([{'name':'weightroom','value':cluster.key,'url':cluster.origin}])
        page=context.new_page()
        page.goto(cluster.origin+'/models?kind=image&install='+original)
        expect(page.get_by_role('button',name='Retry installation',exact=True)).to_be_visible()
        page.get_by_role('button',name='Retry installation',exact=True).click()
        page.wait_for_url(lambda url:parse_qs(urlsplit(url).query).get('install') not in (None,[original]))
        retried=parse_qs(urlsplit(page.url).query)['install'][0]
        installations(cluster,[{'id':retried}])
        verify_bundle(cluster,'restart-recovery',['compute'],[
            'restart/diffusion.safetensors','qwen/encoder.safetensors','qwen/vae.safetensors','adapters/style.safetensors'])
        browser.close()
