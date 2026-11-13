"""Real user workflows in Chromium, against the disposable qualification cluster."""
import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import expect, sync_playwright

from browser import session_key


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


def install_llm(page, origin, name, nodes):
    page.goto(origin + '/models?new=1')
    option = page.locator('#file option').filter(has_text='SmolLM2-135M-Instruct-Q4_K_M.gguf').first
    expect(option).to_have_count(1, timeout=60000)
    page.locator('#file').select_option(option.get_attribute('value'))
    page.locator('#name').fill(name)
    page.locator('#min-context').fill('512')
    page.locator('#ctx').fill('2048')
    for choice in page.locator('input[name=node]').all():
        choice.set_checked(choice.input_value() in nodes, force=True)
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
    for target in page.locator('#mw-install-targets input').all():
        target.set_checked(target.input_value() in nodes, force=True)
    with page.expect_response(lambda r: '/mirrorwall-models' in r.url and r.request.method == 'POST', timeout=120000) as answer:
        page.get_by_role('button', name='Save model', exact=True).click()
    assert answer.value.status == 202, answer.value.text()
    page.wait_for_url('**/models?kind=image&install=*')
    expect(page.locator('[role=status]').filter(has_text='Installed').first).to_be_visible(timeout=600000)
    return {'installed_image': name, 'nodes': nodes}



def benchmark_nodes(page, origin, name, nodes, benchmark):
    page.goto(origin + '/benchmarks/node-measurements?benchmark=' + benchmark)
    page.locator('#benchmark').select_option(benchmark)
    expect(page.locator('input[name=run-target]').first).to_be_attached(timeout=60000)
    selected = []
    for choice in page.locator('input[name=run-target]').all():
        target = json.loads(choice.input_value())
        if target['model'] == name and target['machine'] in nodes:
            choice.check(force=True)
            selected.append(target['machine'])
    assert sorted(selected) == sorted(nodes), selected
    page.get_by_role('button', name='Review & queue', exact=True).click()
    page.get_by_role('button', name='Queue reviewed runs', exact=True).click()
    expect(page.locator('main')).to_contain_text('Queued ' + str(len(nodes)) + ' runs', timeout=60000)
    return {'queued_benchmark': benchmark, 'model': name, 'nodes': nodes}



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=['download-llm', 'download-image', 'install-image', 'install-llm', 'benchmark'])
    parser.add_argument('--origin', default='http://127.0.0.1:19440')
    parser.add_argument('--name', default='qualification-smollm')
    parser.add_argument('--benchmark', default='load')
    parser.add_argument('--node', action='append', default=[])
    parser.add_argument('--output', type=Path, default=Path('/tmp/owg-qualification-scenarios'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, executable_path='/usr/bin/chromium')
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_cookies([{'name': 'weightroom', 'value': session_key(), 'url': args.origin}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            result = (download_model(page, args.origin, 'bartowski/SmolLM2-135M-Instruct-GGUF',
                                     'SmolLM2-135M-Instruct-Q4_K_M.gguf', 'storage_llm') if args.scenario == 'download-llm' else
                      download_model(page, args.origin, 'OlegSkutte/sdxl-turbo-GGUF',
                                     'sd_xl_turbo_1.0.q8_0.gguf', 'storage_image') if args.scenario == 'download-image' else
                      install_image(page, args.origin, args.name, args.node or ['beta']) if args.scenario == 'install-image' else
                      benchmark_nodes(page, args.origin, args.name, args.node or ['alpha', 'beta', 'gamma'], args.benchmark) if args.scenario == 'benchmark' else
                      install_llm(page, args.origin, args.name, args.node or ['alpha']))
            assert not errors, errors
            print(json.dumps(result))
        finally:
            page.screenshot(path=str(args.output / (args.scenario + '.png')), full_page=True)
            (args.output / (args.scenario + '.txt')).write_text(page.locator('main').inner_text())
            browser.close()
