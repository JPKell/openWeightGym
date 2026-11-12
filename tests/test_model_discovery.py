"""Provider pagination and authenticated model-source workspaces without external traffic."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_console_browser import browser_context, playwright
from test_themes import change_theme
from test_weightroom import desk, sign_in  # noqa: F401

from loadcoach import hub
from mirrorwall import civitai


def test_provider_cursor_pages_keep_requests_on_the_provider():
    seen = []
    def answer(request):
        seen.append(request)
        if request.url.host == 'huggingface.co':
            return httpx.Response(200, json=[{'id': 'acme/model', 'downloads': 100}],
                headers={'link': '<https://untrusted.invalid/api/models?cursor=next-hf>; rel="next"'})
        return httpx.Response(200, json={'items': [
            {'id': 1, 'name': 'Landscape', 'type': 'LORA', 'modelVersions': [
                {'id': 10, 'name': 'v1', 'baseModel': 'SDXL 1.0', 'images': [
                    {'url': 'https://image.civitai.com/adult.png', 'nsfw': 'X'}]}]},
            {'id': 2, 'name': 'Hidden', 'nsfw': True, 'modelVersions': [{'id': 20}]}],
            'metadata': {'nextPage': 'https://untrusted.invalid/?cursor=next-civitai'}})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            first = await hub.search_page(client, 'model', 'private', 'previous-hf')
            assert first['next_cursor'] == 'next-hf'
            assert first['found'][0]['repository'] == 'acme/model'
            assert seen[-1].url.params['cursor'] == 'previous-hf'
            assert seen[-1].url.params['limit'] == '20'
            assert seen[-1].headers['authorization'] == 'Bearer private'
            for sort in ('downloads', 'likes', 'createdAt', 'invalid'):
                for direction in ('-1', '1', 'invalid'):
                    await hub.search_page(client, 'model', sort=sort, direction=direction)
                    assert seen[-1].url.params['sort'] == ('downloads' if sort == 'invalid' else sort)
                    assert seen[-1].url.params['direction'] == ('1' if direction == '1' else '-1')
                    assert seen[-1].url.params['limit'] == '20'
            second = await civitai.search_page(client, 'landscape', 'private', cursor='previous-civitai', kind='LORA', base='SDXL 1.0')
            assert second['next_cursor'] == 'next-civitai'
            assert len(second['found']) == 1 and second['found'][0]['preview'] is None
            assert seen[-1].url.params['cursor'] == 'previous-civitai'
            assert seen[-1].url.params['types'] == 'LORA'
            assert seen[-1].url.params['baseModels'] == 'SDXL 1.0'
            assert 'page' not in seen[-1].url.params
            await hub.search_page(client, 'model', cursor=first['next_cursor'])
            await civitai.search_page(client, 'landscape', cursor=second['next_cursor'])
            assert {request.url.host for request in seen} == {'huggingface.co', 'civitai.com'}
    asyncio.run(check())


def test_hub_sort_reaches_both_storage_authorities(desk, tmp_path, monkeypatch):  # noqa: F811
    server, _, _ = desk
    calls = []
    async def source(http, words, token='', cursor='', sort='downloads', direction='-1'):
        calls.append((words, cursor, sort, direction))
        return {'found': [], 'next_cursor': ''}
    monkeypatch.setattr(hub, 'search_page', source)
    sign_in(server, 'jordan')
    for front_storage in (False, True):
        if front_storage:
            server.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': '', 'models_storage_folder': str(tmp_path / 'storage')})
        for sort in ('downloads', 'likes', 'createdAt'):
            response = server.get('/hub', params={'q': 'model', 'sort': sort, 'direction': '1', 'cursor': 'older'})
            assert response.status_code == 200
            assert calls[-1] == ('model', 'older', sort, '1')


def test_paginated_discovery_proxies_keep_filters_and_access_checks(desk, tmp_path, monkeypatch):  # noqa: F811
    server, _, _ = desk
    calls = []
    async def source(http, words, token='', adult=False, cursor='', kind='', base=''):
        calls.append((words, adult, cursor, kind, base))
        return {'found': [], 'next_cursor': 'next'}
    monkeypatch.setattr(civitai, 'search_page', source)
    sign_in(server, 'pat')
    assert server.get('/mirrorwall-discovery?q=style&paged=1').status_code == 403
    server.post('/sign-out'); sign_in(server, 'jordan')
    for front_storage in (False, True):
        if front_storage:
            server.post('/settings/model-files', data={'file_machine': 'front', 'models_folder': '', 'models_storage_folder': str(tmp_path / 'storage')})
        response = server.get('/mirrorwall-discovery?q=style&paged=1&cursor=older&kind=LORA&base=SDXL%201.0&adult=true')
        assert response.status_code == 200 and response.json()['next_cursor'] == 'next'
        assert calls[-1] == ('style', False, 'older', 'LORA', 'SDXL 1.0')


@pytest.mark.parametrize('width,theme,palette', [(1440, 'light', 'slate'), (390, 'dark', 'forest'), (320, 'light', 'slate')])
def test_hub_pages_and_repository_fragment(desk, monkeypatch, tmp_path, width, theme, palette):  # noqa: F811
    server, _, _ = desk
    calls = []
    async def search(http, words, token='', cursor='', sort='downloads', direction='-1'):
        calls.append((cursor, sort, direction))
        rows = [{'repository': 'acme/tiny-GGUF' if i == 0 else f'acme/model-{i}',
                 'downloads': 123456, 'likes': 1200, 'made': '2026-11-10'} for i in range(20)]
        return {'found': rows if not cursor else [], 'next_cursor': 'next-hf' if not cursor else ''}
    monkeypatch.setattr(hub, 'search_page', search)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/hub'); change_theme(page, palette, theme)
        page.locator('[name=q]').fill('tiny'); page.get_by_role('button', name='Search', exact=True).click()
        playwright.expect(page.locator('#hub-results tbody tr')).to_have_count(20)
        page.get_by_role('link', name='acme/tiny-GGUF', exact=True).click()
        playwright.expect(page.locator('#hub-files')).to_be_visible()
        playwright.expect(page.locator('#hub-results tbody tr')).to_have_count(20)
        page.get_by_label('tiny-q4.gguf', exact=True).check()
        assert page.locator('[name=file]:checked').count() == 1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        folder = Path(os.environ.get('DISCOVERY_SCREENSHOTS', tmp_path)); folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'hub-{width}.png'), full_page=True)
        page.get_by_role('link', name='Next', exact=True).click()
        playwright.expect(page.locator('#hub-found')).to_contain_text('Page 2')
        playwright.expect(page.locator('#hub-found')).to_contain_text('Nothing found')
        assert calls[-1] == ('next-hf', 'downloads', '-1')
        page.get_by_role('link', name='Previous', exact=True).click()
        playwright.expect(page.locator('#hub-results tbody tr')).to_have_count(20)
        page.reload(); playwright.expect(page.locator('#hub-results tbody tr')).to_have_count(20)
        for sort, label in [('likes', 'Likes'), ('createdAt', 'Created'), ('downloads', 'Downloads')]:
            page.locator('#hub-results thead').get_by_role('link', name=label, exact=True).click()
            playwright.expect(page.locator('#hub-results th[aria-sort="descending"]')).to_contain_text(label)
            assert calls[-1] == ('', sort, '-1')
            assert page.locator('[name=sort]').input_value() == sort
            page.locator('#hub-results thead').get_by_role('link', name=label, exact=True).click()
            playwright.expect(page.locator('#hub-results th[aria-sort="ascending"]')).to_contain_text(label)
            assert calls[-1] == ('', sort, '1')
        page.get_by_role('link', name='Next', exact=True).click()
        playwright.expect(page.locator('#hub-found')).to_contain_text('Page 2')
        assert calls[-1] == ('next-hf', 'downloads', '1')
        page.get_by_role('link', name='Previous', exact=True).click()
        playwright.expect(page.locator('#hub-results tbody tr')).to_have_count(20)
        page.locator('[name=q]').fill('another model')
        page.get_by_role('button', name='Search', exact=True).click()
        playwright.expect(page.locator('[name=q]')).to_have_value('another model')
        assert calls[-1] == ('', 'downloads', '1')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('width,theme,palette', [(1440, 'light', 'slate'), (390, 'dark', 'forest'), (320, 'light', 'slate')])
def test_civitai_bounded_pages_filters_errors_and_download(desk, tmp_path, width, theme, palette):  # noqa: F811
    server, _, _ = desk; sign_in(server, 'jordan')
    requests = []; downloads = []; fail = False
    def capture(route, path):
        nonlocal fail
        if not path.startswith('/mirrorwall-discovery'):
            return False
        if route.request.method == 'POST':
            downloads.append(route.request.post_data_json)
            value = {'downloading': ['weights.safetensors']}
        else:
            params = parse_qs(urlsplit(path).query); requests.append(params)
            if 'repository' in params:
                value = {'files': [{'file': 'weights.safetensors', 'size': 2**30}],
                         'places': [{'name': 'image'}, {'name': 'storage_image'}]}
            elif fail:
                route.fulfill(status=502, content_type='application/json', body='{"error":{"message":"Provider unavailable. Please retry."}}'); return True
            elif params.get('kind') == ['LORA']:
                value = {'found': [], 'next_cursor': ''}
            else:
                start, count = (25, 2) if 'cursor' in params else (0, 25)
                value = {'found': [{'name': f'Landscape model {i} · v1', 'repository': f'civitai/{i+1}',
                    'kind': 'Checkpoint', 'base_model': 'SDXL 1.0', 'url': f'https://civitai.com/models/{i+1}',
                    'trigger_words': ['landscape']} for i in range(start, start + count)],
                    'next_cursor': 'provider-next' if start == 0 else ''}
        route.fulfill(content_type='application/json', body=json.dumps(value)); return True
    with playwright.sync_playwright() as driver:
        browser, page = browser_context(driver, server, capture=capture)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/civitai'); change_theme(page, palette, theme)
        page.locator('#mw-q').fill('landscape'); page.get_by_role('button', name='Search', exact=True).click()
        playwright.expect(page.locator('#mw-found article')).to_have_count(12)
        assert page.get_by_role('link', name='Previous', exact=True).is_hidden()
        page.get_by_role('button', name='Choose file', exact=True).first.click()
        page.get_by_role('button', name='weights.safetensors', exact=False).click()
        playwright.expect(page.locator('#mw-success')).to_contain_text('Download started')
        assert downloads[-1] == {'repository': 'civitai/1', 'files': ['weights.safetensors'], 'place': 'storage_image'}
        folder = Path(os.environ.get('DISCOVERY_SCREENSHOTS', tmp_path)); folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'civitai-{width}.png'), full_page=True)
        for count in (12, 1, 2):
            page.get_by_role('link', name='Next', exact=True).click()
            playwright.expect(page.locator('#mw-found article')).to_have_count(count)
        assert requests[-1]['cursor'] == ['provider-next']
        assert page.get_by_role('link', name='Next', exact=True).is_hidden()
        page.get_by_role('link', name='Previous', exact=True).click()
        playwright.expect(page.locator('#mw-found article')).to_have_count(1)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        fail = True
        page.get_by_role('button', name='Search', exact=True).click()
        playwright.expect(page.locator('#mw-message')).to_contain_text('Provider unavailable')
        playwright.expect(page.get_by_role('button', name='Search', exact=True)).to_be_enabled()
        fail = False
        page.get_by_role('button', name='Search', exact=True).click()
        playwright.expect(page.locator('#mw-found article')).to_have_count(12)
        page.locator('#mw-type').select_option('LORA')
        playwright.expect(page.locator('#mw-empty')).to_contain_text('No matching model versions')
        playwright.expect(page.locator('#mw-found article')).to_have_count(0)
        assert requests[-1]['kind'] == ['LORA']
        assert not errors, errors
        browser.close()
