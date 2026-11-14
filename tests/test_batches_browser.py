"""The live batch ledger uses durable records and preserves API authorization."""

import csv
import io
import json

import pytest

from loadcoach.batches import Batches
from test_console_browser import browser_context, playwright
from test_weightroom import desk, sign_in  # noqa: F401

expect = playwright.expect


@pytest.fixture
def records(desk, tmp_path):
    server, _, users = desk
    store = Batches(str(tmp_path / 'data/accounts.db'))
    def create(owner, name, state='in_progress', answers=1, errors=0):
        lines = [{'custom_id': f'q{i}', 'body': {'model': 'small', 'messages': []}} for i in range(3)]
        file = store.put_file(owner, 'input.jsonl', 'batch', b'{}\n')
        batch = store.start(owner, '/v1/chat/completions', file['id'], lines, {'name': name})
        for i in range(answers + errors):
            store.keep(batch['id'], i, 200 if i < answers else 400, f'request-{i}', {'sample': i})
        if state == 'completed':
            store.finish(batch['id'])
        elif state == 'cancelled':
            store.cancelling(batch['id']); store.finish(batch['id'])
        return batch['id']
    ids = [create('jordan', 'Support triage', answers=1, errors=1),
           create('jordan', 'Invoice extraction', 'completed', answers=2, errors=1),
           create('pat', 'Private results', 'completed', answers=3),
           create('pat', 'Team work'), create('jordan', '<img src=x onerror="window.unsafe=true">'),
           create('jordan', 'Partial archive', 'cancelled')]
    sign_in(server, 'jordan')
    return server, store, ids, users


def test_filters_export_pagination_and_permissions_use_real_records(records):
    server, store, ids, _ = records
    text = server.get('/batches?status=errors&owner=jordan&limit=1').text
    assert '2 batches' in text and 'part=list' in text and 'status=errors' in text
    assert 'BACKGROUND WORK' not in text and 'Track background requests' not in text
    rows = list(csv.DictReader(io.StringIO(server.get('/batches?status=errors&owner=jordan&limit=1&export=csv').text)))
    assert len(rows) == 2 and {row['id'] for row in rows} == set(ids[:2])
    sign_in(server, 'pat')
    page = server.get('/batches')
    assert 'Support triage' not in page.text and 'Private results' in page.text
    rows = list(csv.DictReader(io.StringIO(server.get('/batches?owner=jordan&export=csv').text)))
    assert not rows
    private = store.get(ids[1])['output_file_id']
    assert server.get('/batches/' + private).status_code == 404
    assert server.post('/batches', data={'batch': ids[0], 'do': 'cancel'}).status_code == 400
    assert store.get(ids[0])['status'] == 'in_progress'


def test_ledger_fits_themes_dialogs_and_retains_filters_during_refresh(records, tmp_path):
    server, store, ids, _ = records
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/batches')
        for width in (1440, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for theme in ('light', 'dark'):
                page.emulate_media(color_scheme=theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        assert page.locator('#batches-table').bounding_box()['y'] < 420
        expect(page.locator('#batch-visible-count')).to_have_text('6 batches')
        row = page.locator(f'tr[data-batch="{ids[0]}"]')
        expect(row.locator('[data-progress-label]')).to_have_text('2 / 3 processed')
        row.locator('[data-open]').click()
        expect(page.locator('#detail-name')).to_have_text('Support triage')
        expect(page.locator('#batch-detail-dialog [data-remaining]')).to_have_text('1')
        for width in (1440, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#copy-id').click()
        expect(page.locator('#copy-id')).to_have_text('Copied')
        # A poll cannot dismiss the open record or a pending cancellation confirmation.
        assert page.evaluate('''() => {
          const target = document.getElementById('batches');
          const event = new CustomEvent('htmx:beforeRequest', {bubbles:true,cancelable:true,
            detail:{requestConfig:{elt:target},target}});
          target.dispatchEvent(event); return event.defaultPrevented;
        }''')
        page.keyboard.press('Escape')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('#batch-status').select_option('errors')
        expect(page.locator('#batch-visible-count')).to_have_text('2 batches')
        page.locator('#batch-owner').select_option('jordan')
        page.locator('#batch-search').fill('Support')
        expect(page.locator('#batch-visible-count')).to_have_text('1 batch')
        store.keep(ids[0], 2, 200, 'last-request', {'answer': 'done'})
        page.evaluate('''() => {const target=document.getElementById('batches');
          htmx.ajax('GET',target.getAttribute('hx-get'),{target:'#batches',swap:'outerHTML'});}''')
        expect(page.locator('#batches-table [data-progress-label]')).to_have_text('3 / 3 processed')
        expect(page.locator('#batch-search')).to_have_value('Support')
        expect(page.locator('#batch-status')).to_have_value('errors')
        assert page.locator('#batches-table time').first.evaluate('(time) => time.textContent === SuiteTime.format(time.dateTime, false)')
        page.screenshot(path=str(tmp_path / 'batch-ledger.png'), full_page=True)
        page.locator('#batch-search').fill('nothing matches')
        expect(page.locator('#batches-table')).to_contain_text('No matching batches')
        page.get_by_role('link', name='Clear', exact=True).click()
        page.locator(f'tr[data-batch="{ids[4]}"] [data-open]').click()
        expect(page.locator('#detail-name')).to_contain_text('<img src=x')
        assert page.evaluate('window.unsafe') is None
        page.keyboard.press('Escape')
        page.locator('[data-batch-guide]').first.click()
        expect(page.locator('#batch-submission')).to_have_attribute('open','')
        assert not errors
        browser.close()


def test_cancellation_keeps_partial_answers_filters_and_downloads(records):
    server, store, ids, _ = records
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/batches?owner=jordan')
        row = page.locator(f'tr[data-batch="{ids[0]}"]')
        cancel = row.locator('[value="cancel"]')
        cancel.click()
        expect(cancel).to_have_text('Are you sure?')
        assert store.get(ids[0])['status'] == 'in_progress'
        cancel.click()
        expect(page.locator('.said-box')).to_contain_text('Cancelled.')
        expect(page.locator('#batch-owner')).to_have_value('jordan')
        assert store.get(ids[0])['status'] == 'cancelled'
        row = page.locator(f'tr[data-batch="{ids[0]}"]')
        expect(row.get_by_role('link', name='Answers', exact=True)).to_be_visible()
        output = store.get(ids[0])['output_file_id']
        answer = server.get('/batches/' + output)
        assert json.loads(answer.text)['custom_id'] == 'q0'
        # Administrator sees others' rows, but only the owner can retrieve their files.
        page.goto('http://console.test/batches')
        other = page.locator(f'tr[data-batch="{ids[2]}"]')
        expect(other.get_by_role('link', name='Answers', exact=True)).to_have_count(0)
        browser.close()
