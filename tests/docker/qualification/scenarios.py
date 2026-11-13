"""Real user workflows in Chromium, against the disposable qualification cluster."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from playwright.sync_api import expect, sync_playwright

from browser import session_key


def select_nodes(choices, nodes):
    for choice in choices.all():
        if choice.is_checked() != (choice.input_value() in nodes):
            choice.locator('xpath=..').click()


def download_model(page, origin, repository, filename, place):
    page.goto(origin + '/hub')
    page.locator('#hub-query').fill(repository)
    page.get_by_role('button', name='Search', exact=True).click()
    file = page.locator('input[name=file][value="' + filename + '"]')
    expect(file).to_be_visible(timeout=60000)
    file.check()
    page.locator('select[name=place]').select_option(place)
    page.get_by_role('button', name='Download selected files', exact=True).click()
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        page.goto(origin + '/files?tier=storage')
        page.wait_for_function("!document.querySelector('[data-deferred-loader][aria-busy=true]')", timeout=60000)
        if page.locator('a[href^="/file?"]').filter(has_text=Path(filename).name).count():
            return {'downloaded': filename}
        page.wait_for_timeout(1000)
    raise AssertionError('The model download did not appear in Files.')


def install_llm(page, origin, name, nodes, filename, context):
    page.goto(origin + '/models?new=1')
    option = page.locator('#file option').filter(has_text=filename).first
    expect(option).to_have_count(1, timeout=60000)
    page.locator('#file').select_option(option.get_attribute('value'))
    page.locator('#name').fill(name)
    page.locator('#min-context').fill('512')
    page.locator('#ctx').fill(str(context))
    select_nodes(page.locator('input[name=node]'), nodes)
    page.get_by_role('button', name='Add', exact=True).click()
    page.wait_for_url('**/models*', timeout=60000)
    page.goto(origin + '/models')
    for node in nodes:
        link = page.locator('a.inventory-placement[href="/models/' + name + '?node=' + node + '"]')
        expect(link).to_be_visible(timeout=180000)
        expect(link).to_have_attribute('href', '/models/' + name + '?node=' + node)
    return {'installed': name, 'nodes': nodes}



def install_image(page, origin, name, nodes):
    page.goto(origin + '/models/images?new=1')
    expect(page.locator('#model-dialog')).to_be_visible()
    option = page.locator('#mw-diffusion option').filter(has_text='sd_xl_turbo_1.0.q8_0.gguf')
    expect(option).to_have_count(1, timeout=60000)
    page.locator('#mw-name').fill(name)
    page.locator('#mw-family').select_option('sdxl')
    for field, value in [('width','512'),('height','512'),('steps','1'),('cfg_scale','1')]:
        page.locator('#mw-default-' + field).fill(value)
        page.locator('#mw-default-' + field).blur()
    page.locator('#mw-threads').fill('8')
    page.locator('#mw-ram').fill('12288')
    page.get_by_text('CPU only', exact=True).click()
    page.locator('#mw-diffusion').select_option(option.get_attribute('value'))
    expect(page.locator('#mw-check')).to_contain_text('Everything this model needs is here', timeout=60000)
    select_nodes(page.locator('#mw-install-targets input'), nodes)
    with page.expect_response(lambda r: '/mirrorwall-models' in r.url and r.request.method == 'POST', timeout=120000) as answer:
        page.get_by_role('button', name='Save model', exact=True).click()
    assert answer.value.status == 202, answer.value.text()
    page.wait_for_url('**/models?kind=image&install=*')
    expect(page.locator('[role=status]').filter(has_text='Installed').first).to_be_visible(timeout=600000)
    return {'installed_image': name, 'nodes': nodes}



def benchmark_nodes(page, origin, name, nodes, benchmark):
    page.goto(origin + '/benchmarks/runs?benchmark=' + benchmark)
    page.locator('#benchmark').select_option(benchmark)
    expect(page.locator('input[name=run-target]').first).to_be_attached(timeout=60000)
    selected = []
    for choice in page.locator('input[name=run-target]').all():
        target = json.loads(choice.input_value())
        if target['model'] == name and target['machine'] in nodes:
            if not choice.is_checked():
                choice.locator('xpath=..').click()
            selected.append(target['machine'])
    assert sorted(selected) == sorted(nodes), selected
    page.get_by_role('button', name='Review & queue', exact=True).click()
    page.get_by_role('button', name='Queue reviewed runs', exact=True).click()
    expect(page.locator('main')).to_contain_text('Queued ' + str(len(nodes)) + ' runs', timeout=60000)
    return {'queued_benchmark': benchmark, 'model': name, 'nodes': nodes}


def generate_image(page, origin, name, output):
    page.goto(origin + '/chat/images')
    page.locator('#mw-model').select_option(name)
    page.locator('#mw-prompt').fill('A red apple on a wooden table, daylight, photograph.')
    for key, value in [('width', '512'), ('height', '512'), ('steps', '1'), ('seed', '42')]:
        page.locator('#mw-' + key).fill(value)
    before = page.locator('#mw-thumbnails img').count()
    page.locator('#mw-generate').click()
    page.wait_for_function('(before) => document.querySelectorAll("#mw-thumbnails img").length > before',
                           arg=before, timeout=240000)
    page.locator('#mw-thumbnails img').first.click()
    expect(page.locator('#mw-download')).to_be_visible()
    page.wait_for_function('document.querySelector("#mw-large").complete && document.querySelector("#mw-large").naturalWidth > 0')
    size = page.locator('#mw-large').evaluate('(image) => [image.naturalWidth,image.naturalHeight]')
    assert size == [512, 512], size
    with page.expect_download() as download:
        page.locator('#mw-download').click()
    download.value.save_as(str(output / 'generated-image.png'))
    return {'generated_image': name, 'size': size, 'downloaded': 'generated-image.png'}


def speak(page, origin, name, output):
    page.goto(origin + '/chat/speech')
    page.locator('#sr-model').select_option(name)
    prompt = 'Welcome to the gym. Your models are ready.'
    page.locator('#sr-prompt').fill(prompt)
    before = page.locator('#sr-list button').count()
    page.locator('#sr-generate').click()
    page.wait_for_function('(before) => document.querySelectorAll("#sr-list button").length > before',
                           arg=before, timeout=180000)
    page.locator('#sr-list button').filter(has_text=prompt).first.click()
    audio = page.locator('audio').first
    page.wait_for_function('document.querySelector("audio").readyState >= 2', timeout=30000)
    duration = audio.evaluate('(audio) => audio.duration')
    assert duration > 0
    with page.expect_download() as download:
        page.locator('#sr-download').click()
    download.value.save_as(str(output / 'generated-speech.wav'))
    return {'speech': name, 'seconds': duration, 'downloaded': 'generated-speech.wav'}


def transcribe(page, origin, name, file):
    if not file or not file.is_file():
        raise ValueError('Transcription needs --file pointing to an audio recording.')
    page.goto(origin + '/chat/transcribe')
    page.locator('#tr-model').select_option(name)
    page.locator('#tr-file').set_input_files(file)
    page.locator('#tr-go').click()
    expect(page.locator('#tr-result')).to_contain_text('Welcome to the gym', timeout=120000)
    return {'transcription': name, 'text': page.locator('#tr-result').inner_text()}


def backups(page, origin, nodes):
    page.goto(origin + '/backups')
    select_nodes(page.locator('form[action="/backups/create"] input[type=checkbox]'), nodes)
    page.get_by_role('button', name='Back up selected nodes', exact=True).click()
    expect(page.locator('main')).to_contain_text('Verified backups:', timeout=180000)
    return {'verified_backups': nodes, 'message': page.locator('.said-box').inner_text()}


def tools(page, origin):
    page.goto(origin + '/tools')
    page.locator('#tools-connect').click()
    expect(page.locator('#tools-state')).to_have_text('2 tools available to your account.')
    def call(name, arguments, status, excerpt):
        page.locator('#tools-tool').select_option('files__' + name)
        for key, value in arguments.items():
            page.locator('#tool-argument-' + key).fill(value)
        page.locator('#tools-call').click()
        expect(page.locator('#tools-status')).to_have_text(status)
        expect(page.locator('#tools-answer')).to_contain_text(excerpt)
    root = '/state/front/qualification-shared'
    call('list_folder', {}, 'Returned.', root)
    call('list_folder', {'folder': root}, 'Returned.', 'hello.txt')
    call('read_file', {'file': root + '/hello.txt'}, 'Returned.',
         'Hello from the isolated qualification tool server.')
    call('read_file', {'file': '/etc/passwd'}, 'Tool failed or refused.',
         'outside the folders this tool may read')
    return {'tools': ['files__list_folder', 'files__read_file'], 'read': True, 'outside_root_refused': True}


def workflow(page, origin, name, collection):
    page.goto(origin + '/workflows')
    page.locator('#workflow-title').fill('Qualification complete workflow')
    choice = page.locator('#workflow-collection option').filter(has_text=collection).first
    expect(choice).to_have_count(1)
    page.locator('#workflow-collection').select_option(choice.get_attribute('value'))
    page.locator('#workflow-question').fill('What time does the gym open and close?')
    page.locator('#workflow-minimum').fill('0.1')
    for step in ['research', 'draft']:
        page.locator('#' + step + '-model').select_option(name)
        page.locator('#' + step + '-prompt').select_option('builtin:' + step + '-v1')
    page.get_by_role('button', name='Create workflow', exact=True).click()
    for step in ['research', 'draft']:
        page.get_by_role('button', name='Start ' + step, exact=True).click()
        deadline = time.monotonic() + 180
        accept = page.get_by_role('button', name='Accept ' + step, exact=True)
        while time.monotonic() < deadline and not accept.count():
            page.wait_for_timeout(2000)
            page.reload()
            assert not page.get_by_role('button', name='Resume / retry ' + step, exact=True).count(), page.locator('main').inner_text()
        expect(accept).to_be_visible()
        output = page.locator('#' + step + '-output')
        expect(output).to_contain_text('06:30')
        expect(output).to_contain_text('21:00')
        expect(output).to_contain_text('qualification.txt')
        accept.click()
    expect(page.locator('main')).to_contain_text('normal · completed')
    return {'workflow': page.url, 'model': name, 'research': 'accepted', 'draft': 'accepted'}


def wake_overflow(page, origin, api_origin, name):
    """Receive real WOL on the isolated network and emulate only gamma powering on."""
    container = 'owg-qualification-gamma-1'
    receipt = '/state/front/qualification-wake.json'
    def packet():
        result = subprocess.run(['docker', 'exec', 'owg-qualification-turnstile-1', 'cat', receipt],
                                capture_output=True, text=True)
        return json.loads(result.stdout) if result.returncode == 0 else None
    subprocess.run(['docker', 'exec', 'owg-qualification-turnstile-1', 'rm', '-f', receipt], check=True)
    subprocess.run(['docker', 'stop', container], check=True, capture_output=True)
    try:
        page.wait_for_timeout(7000)
        for path in ['/status', '/models', '/usage', '/backups']:
            assert page.goto(origin + path).status == 200
        assert packet() is None, 'Passive page reads must not wake a node.'
        calls = [page.context.new_page(), page.context.new_page()]
        for call in calls:
            call.goto(api_origin)
            call.locator('#key').fill(session_key())
            call.locator('#message').fill('Write a long story about the gym.')
        calls[0].locator('#fields').fill(json.dumps({'model': name, 'node': 'beta', 'stream': False,
                                                   'max_tokens': 1800, 'ignore_eos': True}))
        calls[0].locator('#send').click()
        page.wait_for_timeout(2300)
        calls[1].locator('#fields').fill(json.dumps({'model': name, 'stream': False, 'max_tokens': 8}))
        calls[1].locator('#send').click()
        deadline = time.monotonic() + 20
        while not (received := packet()) and time.monotonic() < deadline:
            page.wait_for_timeout(150)
        assert received and received['node'] == 'gamma' and received['bytes'] == 102, 'No valid overflow magic packet.'
        subprocess.run(['docker', 'start', container], check=True, capture_output=True)
        statuses = []
        for call in calls:
            call.wait_for_function('!document.querySelector("#send").disabled', timeout=90000)
            statuses.append(call.locator('#status').inner_text())
            assert statuses[-1].startswith('200 '), call.locator('#raw').inner_text()
            call.close()
        page.goto(origin + '/status')
        return {'passive_reads_wake': False, 'overflow_magic_packet': received, 'statuses': statuses}
    finally:
        subprocess.run(['docker', 'start', container], capture_output=True)


def routing(page, origin, api_origin, name, nodes):
    api = page.context.new_page()
    api.goto(api_origin)
    api.locator('#key').fill(session_key())
    results = []
    def send(fields, expected):
        api.locator('#fields').fill(json.dumps({'model': name, 'stream': False, 'max_tokens': 8, **fields}))
        api.locator('#message').fill('Say hello.')
        api.locator('#send').click()
        api.wait_for_function('!document.querySelector("#send").disabled', timeout=90000)
        status = api.locator('#status').inner_text()
        assert status.startswith(str(expected) + ' '), status
        value = json.loads(api.locator('#raw').inner_text())
        results.append({'node': fields.get('node'), 'status': expected, 'model': value.get('model')})
        return value
    for node in nodes:
        value = send({'node': node}, 200)
        assert value['model'].startswith('/state/' + node + '/'), value['model']
    send({'node': 'qualification-no-such-node'}, 404)
    def policy(enabled):
        page.goto(origin + '/settings')
        field = page.locator('#setting-turnstile-allow_caller_to_specify_destination')
        if not field.is_visible():
            page.locator('main').get_by_text('Requests and queue', exact=True).click()
        if field.is_checked() != enabled:
            field.set_checked(enabled)
            field.locator('xpath=ancestor::*[@data-settings-section]').locator('[data-save-settings]').click()
            page.wait_for_url('**/settings?said=Saved.**')
    try:
        policy(False)
        refused = send({'node': nodes[0]}, 400)
        assert 'Settings' in refused['error']['message']
        send({}, 200)
    finally:
        policy(True)
        api.close()
    return {'routing': results, 'destination_policy_restored': True}



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=['download-llm', 'download-image', 'install-image', 'install-llm',
                                           'benchmark', 'generate-image', 'speech', 'transcribe', 'backups', 'routing', 'tools', 'wake', 'workflow'])
    parser.add_argument('--origin', default='http://127.0.0.1:19440')
    parser.add_argument('--name', default='qualification-smollm')
    parser.add_argument('--api-origin', default='http://127.0.0.1:19442')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--benchmark', default='load')
    parser.add_argument('--repository')
    parser.add_argument('--model-file')
    parser.add_argument('--context', type=int, default=2048)
    parser.add_argument('--collection', default='Qualification knowledge')
    parser.add_argument('--node', action='append', default=[])
    parser.add_argument('--output', type=Path, default=Path('/tmp/owg-qualification-scenarios'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, executable_path='/usr/bin/chromium')
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_cookies([{'name': 'weightroom', 'value': session_key(), 'url': args.origin}])
        errors = []
        context.on('page', lambda page: page.on('pageerror', lambda error: errors.append(str(error))))
        page = context.new_page()
        try:
            result = (download_model(page, args.origin, args.repository or 'bartowski/SmolLM2-135M-Instruct-GGUF',
                                     args.model_file or 'SmolLM2-135M-Instruct-Q4_K_M.gguf', 'storage_llm') if args.scenario == 'download-llm' else
                      download_model(page, args.origin, args.repository or 'OlegSkutte/sdxl-turbo-GGUF',
                                     args.model_file or 'sd_xl_turbo_1.0.q8_0.gguf', 'storage_image') if args.scenario == 'download-image' else
                      install_image(page, args.origin, args.name, args.node or ['beta']) if args.scenario == 'install-image' else
                      benchmark_nodes(page, args.origin, args.name, args.node or ['alpha', 'beta', 'gamma'], args.benchmark) if args.scenario == 'benchmark' else
                      generate_image(page, args.origin, args.name, args.output) if args.scenario == 'generate-image' else
                      speak(page, args.origin, args.name, args.output) if args.scenario == 'speech' else
                      transcribe(page, args.origin, args.name, args.file) if args.scenario == 'transcribe' else
                      backups(page, args.origin, args.node or ['front', 'alpha', 'beta', 'gamma']) if args.scenario == 'backups' else
                      tools(page, args.origin) if args.scenario == 'tools' else
                      workflow(page, args.origin, args.name, args.collection) if args.scenario == 'workflow' else
                      wake_overflow(page, args.origin, args.api_origin, args.name) if args.scenario == 'wake' else
                      routing(page, args.origin, args.api_origin, args.name, args.node or ['alpha', 'beta', 'gamma']) if args.scenario == 'routing' else
                      install_llm(page, args.origin, args.name, args.node or ['alpha'],
                                  args.model_file or 'SmolLM2-135M-Instruct-Q4_K_M.gguf', args.context))
            assert not errors, errors
            print(json.dumps(result))
            (args.output / (args.scenario + '.json')).write_text(json.dumps(result, indent=2))
        finally:
            page.screenshot(path=str(args.output / (args.scenario + '.png')), full_page=True)
            (args.output / (args.scenario + '.txt')).write_text(page.locator('main').inner_text())
            browser.close()
