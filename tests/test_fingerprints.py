"""Record explorer over the real archive API, including fragment replacements and evidence."""

import json
import runpy
from datetime import datetime
from pathlib import Path

import pytest

from loadcoach.models import Models
from test_weightroom import desk, sign_in  # noqa: F401
from weightroom.fingerprint_views import directory_url, fingerprint_context
from weightroom.display import moment

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def archive(desk, tmp_path):
    server, switch, _ = desk
    records = runpy.run_path(str(ROOT / 'scripts/preview-fingerprint-designs.py'))['samples']()
    records[0]['files']['model']['says'].update(chat_template=False, embedding_length=0)
    records[1]['model'] = '<img src=x onerror=alert(1)>'
    records[-1]['files']['model']['says'] = None
    library = Models(str(tmp_path / 'loadcoach.db'))
    with library.db:
        for record in records:
            record['first'] = datetime.fromisoformat(record['first']).timestamp()
            details = {key: value for key, value in record.items() if key not in ('fingerprint', 'first')}
            library.db.execute('INSERT INTO fingerprints (fingerprint, first, details) VALUES (?, ?, ?)',
                               (record['fingerprint'], record['first'], json.dumps(details)))
    sign_in(server, 'jordan')
    return server, switch, records


def test_archive_filters_detail_fragments_and_permissions(archive):
    server, _, records = archive
    directory = server.get('/fingerprints')
    assert directory.status_code == 200
    assert directory.text.count(' data-record ') == 24
    assert 'data-fingerprint-page' in directory.text and 'At a glance' in directory.text
    assert 'Model provenance' not in directory.text
    assert 'Model weights' in directory.text
    for file in records[0]['files'].values():
        assert file['path'] in directory.text and file['sha256'] in directory.text
    assert '&lt;img src=x onerror=alert(1)&gt;' in directory.text
    assert '<img src=x onerror=alert(1)>' not in directory.text
    filtered = server.get('/fingerprints', params={'start':'qwen3-32b', 'context':'16384', 'view':'directory'}).text
    assert filtered.count(' data-record ') == 1 and '16,384' in filtered
    record = records[2]
    detail = server.get('/fingerprints', params={'start':record['fingerprint'][:12]}).text
    for file in record['files'].values():
        assert file['path'] in detail and file['sha256'] in detail
    assert 'Projector' in detail and 'false' in detail
    peek = server.get('/fingerprints', params={'peek':record['fingerprint']}).text
    assert 'id="record-peek"' in peek and 'id="page-content"' not in peek
    assert record['files']['model']['sha256'] in peek and record['model'] in peek
    assert 'That fingerprint is not in this archive' in server.get('/fingerprints?peek=missing').text
    sign_in(server, 'pat')
    for query in ('', '?peek=' + record['fingerprint'], '?start=' + record['fingerprint']):
        assert server.get('/fingerprints' + query).status_code == 403


@pytest.mark.parametrize('value', ['https://evil.test/fingerprints', '//evil.test/fingerprints', '/users', '/fingerprints?return=https://evil.test', 'https://['])
def test_record_return_links_remain_inside_the_archive(value):
    target = directory_url(value)
    assert target.startswith('/fingerprints') and 'evil' not in target and 'return=' not in target


def test_unique_prefix_links_and_context_filters_use_recorded_values(archive):
    _, _, records = archive
    one = records[0]
    assert fingerprint_context(records, {'start':one['fingerprint'].upper()[:16]})['one'] == one
    assert fingerprint_context(records, {'start':one['fingerprint'], 'view':'directory'})['one'] is None
    assert len(fingerprint_context(records, {'model':'qwen3-32b'})['found']) == 3
    assert not fingerprint_context(records, {'context':'not-a-number'})['found']


def test_live_preview_keeps_the_directory_and_its_filters(archive):
    from test_console_browser import browser_context, playwright
    server, _, records = archive
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/fingerprints?model=qwen3-32b&view=directory')
        for fact in ('chat template', 'false', 'embedding length', '0'):
            playwright.expect(page.locator('#record-peek')).to_contain_text(fact)
        mark = records[8]['fingerprint']
        page.locator(f'[data-fingerprint="{mark}"] [data-field="model"]').click()
        playwright.expect(page.locator('#record-peek')).to_have_attribute('data-selected-fingerprint', mark)
        playwright.expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(3)
        playwright.expect(page.locator('#model-filter')).to_have_value('qwen3-32b')
        page.locator('#record-peek').get_by_role('link', name='Open complete record').click()
        playwright.expect(page.locator('#record-hash')).to_have_text(mark)
        page.get_by_role('link', name='All fingerprints').click()
        playwright.expect(page.locator('#start')).to_have_value('')
        playwright.expect(page.locator('#model-filter')).to_have_value('qwen3-32b')
        playwright.expect(page.locator('#fingerprint-rows [data-record]')).to_have_count(3)
        browser.close()


def test_every_cell_selects_the_preview_and_text_can_still_be_selected(archive):
    from test_console_browser import browser_context, playwright
    server, _, records = archive
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/fingerprints')
        initial_url = page.url
        for index, field in enumerate(('model', 'first', 'filename', 'context'), start=1):
            mark = records[index]['fingerprint']
            row = page.locator(f'[data-fingerprint="{mark}"]')
            row.locator(f'[data-field="{field}"]').click()
            playwright.expect(page.locator('#record-peek')).to_have_attribute('data-selected-fingerprint', mark)
            playwright.expect(row).to_have_attribute('data-selected', 'true')
            assert page.url == initial_url
        # Keep drag-to-select usable even though the whole row is clickable.
        row = page.locator(f'[data-fingerprint="{records[5]["fingerprint"]}"]')
        row.locator('[data-field="model"]').evaluate('''el => {
            const selection = window.getSelection(), range = document.createRange();
            range.selectNodeContents(el); selection.removeAllRanges(); selection.addRange(range);
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
        }''')
        playwright.expect(page.locator('#record-peek')).to_have_attribute('data-selected-fingerprint', records[4]['fingerprint'])
        page.evaluate('window.getSelection().removeAllRanges()')
        row.locator('[data-open]').focus()
        page.keyboard.press('Enter')
        playwright.expect(page.locator('#record-peek')).to_have_attribute('data-selected-fingerprint', records[5]['fingerprint'])
        browser.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_live_explorer_selection_filters_pagination_and_downloads(archive, width, tmp_path):
    from test_console_browser import browser_context, playwright
    server, _, records = archive
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width, 'height':1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/fingerprints')
        playwright.expect(page.locator('#fingerprint-rows [data-record]:visible')).to_have_count(12)
        page.screenshot(path=str(tmp_path / f'fingerprint-directory-{width}.png'), full_page=True)
        page.locator('[data-fingerprint-page-step="1"]').click()
        playwright.expect(page.locator('#page-summary')).to_have_text('Showing 13–24 of 24')
        page.locator('#page-size').select_option('all')
        playwright.expect(page.locator('#fingerprint-rows [data-record]:visible')).to_have_count(24)
        page.locator('#fingerprints-table th').last.click()
        playwright.expect(page.locator('#fingerprint-rows [data-record]:visible').first.locator('[data-field="context"]')).to_have_text('32,768')
        page.locator('#start').fill('qwen3-32b')
        playwright.expect(page.locator('#match-count')).to_have_text('3 of 24 records')
        page.locator('#context-filter').select_option('16384')
        playwright.expect(page.locator('#match-count')).to_have_text('1 of 24 records')
        with page.expect_download() as saved:
            page.locator('#export-directory').click()
        exported = json.loads(Path(saved.value.path()).read_text())
        assert len(exported) == 1 and exported[0]['settings']['flags']['ctx-size'] == 16384
        page.locator('#start').fill('not-present')
        playwright.expect(page.locator('#match-count')).to_have_text('0 of 24 records')
        playwright.expect(page.locator('#export-directory')).to_be_disabled()
        playwright.expect(page.get_by_text('No matching fingerprints', exact=True)).to_be_visible()
        page.locator('#start').fill(records[2]['fingerprint'][:16])
        page.locator('#context-filter').select_option('')
        playwright.expect(page.locator('#match-count')).to_have_text('1 of 24 records')
        page.locator('#fingerprint-rows [data-field="model"]').click()
        if width > 900:
            playwright.expect(page.locator('#record-peek')).to_have_attribute('data-selected-fingerprint', records[2]['fingerprint'])
            for key, value in records[2]['files']['model']['says'].items():
                playwright.expect(page.locator('#record-peek')).to_contain_text(str(value))
            playwright.expect(page.locator('#record-peek')).to_contain_text(records[2]['files']['model']['sha256'])
            page.locator('#record-peek').get_by_role('link', name='Open complete record').click()
        playwright.expect(page.locator('#record-hash')).to_have_text(records[2]['fingerprint'])
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        playwright.expect(page.locator('#file-records [data-file-hash]')).to_have_count(2)
        page.screenshot(path=str(tmp_path / f'fingerprint-detail-{width}.png'), full_page=True)
        page.locator('#setting-search').fill('seed')
        playwright.expect(page.locator('#setting-rows tr:visible')).to_have_count(1)
        playwright.expect(page.locator('#setting-rows tr:visible td')).to_have_text('0')
        with page.expect_download() as saved:
            page.get_by_role('button', name='Download JSON').click()
        exported = json.loads(Path(saved.value.path()).read_text())
        assert moment(exported.pop('first')).timestamp() == records[2]['first']
        assert exported == {key:value for key,value in records[2].items() if key != 'first'}
        page.get_by_role('link', name='All fingerprints').click()
        playwright.expect(page.locator('#start')).to_have_value(records[2]['fingerprint'][:16])
        assert not errors
        page.screenshot(path=str(tmp_path / f'fingerprints-{width}.png'), full_page=True)
        browser.close()


@pytest.mark.parametrize('theme,palette', [('light','slate'), ('dark','slate'), ('light','forest')])
@pytest.mark.parametrize('width', [1440, 390])
def test_live_evidence_layouts_and_themes(archive, theme, palette, width, tmp_path):
    from test_console_browser import browser_context, playwright
    from test_themes import change_theme
    server, _, records = archive
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width, 'height':1000})
        page.goto('http://console.test/fingerprints')
        change_theme(page, palette, theme)
        background = page.locator('.fp-archive').evaluate('(el) => getComputedStyle(el).backgroundColor')
        for query in ('', '?start=' + records[2]['fingerprint'], '?start=' + records[-1]['fingerprint'], '?start=not-found&view=directory'):
            page.goto('http://console.test/fingerprints' + query)
            change_theme(page, palette, theme)
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), (theme, palette, width, query)
            assert page.locator('main h1:visible').count() == 1
            assert page.evaluate('''() => {
                const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                return new Set(ids).size === ids.length;
            }''')
        assert background != 'rgba(0, 0, 0, 0)'
        browser.close()
