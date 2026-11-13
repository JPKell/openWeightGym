"""Browser-driven qualification against the disposable Docker cluster.

Run with a Python environment containing Playwright and Chromium:
  python tests/docker/qualification/browser.py --output /tmp/owg-qualification
The session is read from the qualification container, never from a host install.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

COMPOSE = Path(__file__).with_name('compose.yaml')


def session_key():
    return subprocess.check_output([
        'docker', 'compose', '-f', str(COMPOSE), 'exec', '-T', 'turnstile',
        'cat', '/state/front/qualification-session.key'], text=True).strip()


def audit(origin, output, widths=(1440, 390), paths=None):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, executable_path='/usr/bin/chromium',
                                         args=['--disable-gpu'])
        context = browser.new_context()
        context.add_cookies([{'name': 'weightroom', 'value': session_key(), 'url': origin}])
        page = context.new_page()
        page.goto(origin + '/status', wait_until='domcontentloaded')
        discovered = page.locator('#mobile-navigation a[href]').evaluate_all(
            'links => links.map(link => link.getAttribute("href"))')
        selected = paths or list(dict.fromkeys(['/status', *discovered, '/account',
                                                '/account/appearance', '/account/audio', '/account/logs']))
        for width in widths:
            page.set_viewport_size({'width': width, 'height': 1000})
            for path in selected:
                errors, failed = [], []
                def on_error(error):
                    errors.append(str(error))
                def on_response(response):
                    if response.status >= 400 and urlsplit(response.url).netloc == urlsplit(origin).netloc:
                        failed.append({'url': response.url.removeprefix(origin), 'status': response.status})
                page.on('pageerror', on_error)
                page.on('response', on_response)
                started = time.monotonic()
                row = {'path': path, 'width': width}
                try:
                    response = page.goto(origin + path, wait_until='domcontentloaded', timeout=60000)
                    page.locator('[data-deferred-loader][aria-busy=true]').wait_for(state='detached', timeout=30000) if page.locator('[data-deferred-loader][aria-busy=true]').count() == 1 else None
                    page.wait_for_function("!document.querySelector('[data-deferred-loader][aria-busy=true]')", timeout=30000)
                    page.wait_for_timeout(300)
                    row.update(status=response.status if response else None,
                               title=page.title(), headings=page.locator('main h1').all_text_contents(),
                               overflow=page.evaluate('document.documentElement.scrollWidth > innerWidth'),
                               alerts=page.locator('main [role="alert"]:visible').all_text_contents(),
                               text=page.locator('main').inner_text()[:16000])
                    filename = path.strip('/').replace('/', '-') or 'index'
                    page.screenshot(path=str(output / f'{filename}-{width}.png'), full_page=True,
                                    timeout=10000)
                except Exception as problem:
                    row['exception'] = str(problem)
                finally:
                    page.remove_listener('pageerror', on_error)
                    page.remove_listener('response', on_response)
                row.update(errors=errors, failed=failed, seconds=round(time.monotonic()-started, 2))
                results.append(row)
                (output / 'audit.json').write_text(json.dumps(results, indent=2))
                print(json.dumps({k: v for k, v in row.items() if k not in ('text',)}), flush=True)
        browser.close()
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:19440')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--width', type=int, action='append')
    parser.add_argument('--path', action='append')
    args = parser.parse_args()
    audit(args.origin.rstrip('/'), args.output, args.width or (1440, 390), args.path)
