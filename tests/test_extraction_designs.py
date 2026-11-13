"""Extraction proposals: source fidelity, corrections, exports and responsive workflows."""

import io
import json
import re
import runpy
from pathlib import Path

import pytest
from PIL import Image

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]
LAYOUTS = ('desk', 'review', 'batch')


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-extraction-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page(viewport={'width': 1600, 'height': 1100}, reduced_motion='reduce')
        errors, remote_requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: remote_requests.append(request.url) if request.url.startswith(('https:', 'http:')) else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not remote_requests
        browser.close()


def run(page):
    page.locator('#run').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('ready. No model was called.')


def raw(page):
    page.locator('[data-result-view="raw"]').click()
    return page.locator('#raw-output').inner_text()


def test_receipt_corrections_locate_source_and_update_export(preview, tmp_path):
    page = preview
    page.get_by_role('button', name='Locate tax', exact=True).click()
    playwright.expect(page.locator('[data-region="tax"]')).to_have_attribute('data-active', '')
    page.locator('#field-tax').fill('1.12')
    page.locator('#field-tax').press('Tab')
    playwright.expect(page.locator('#reconciliation')).to_have_text('Checked · line items, subtotal, tax and total agree.')
    page.locator('#review-toggle').click()
    playwright.expect(page.locator('#result-state')).to_have_text('Reviewed')
    assert json.loads(raw(page))['tax'] == 1.12
    with page.expect_download() as downloaded:
        page.locator('#download').click()
    target = tmp_path / downloaded.value.suggested_filename
    downloaded.value.save_as(target)
    assert target.suffix == '.json'
    assert json.loads(target.read_text())['total'] == 23.52
    page.locator('[data-result-view="preview"]').click()
    page.locator('#field-total').fill('30')
    page.locator('#field-total').press('Tab')
    playwright.expect(page.locator('#reconciliation')).to_contain_text('do not agree')
    playwright.expect(page.locator('#review-toggle')).to_have_attribute('aria-pressed', 'false')
    playwright.expect(page.locator('#result-state')).to_have_text('Needs review')


def test_formats_and_tasks_use_correct_sample_data(preview):
    page = preview
    for format_ in ('plain', 'layout', 'markdown', 'json', 'csv'):
        page.locator('#format').select_option(format_)
        run(page)
        text = raw(page)
        assert 'Coffee' in text and '23.52' in text
        if format_ == 'json':
            assert json.loads(text)['tax'] is None
        if format_ == 'csv':
            assert '"Description","Quantity","Amount","Currency",' in text
    page.locator('[data-sample="document"]').click()
    assert '01  LIGHT                       03  SPACE' in raw(page)
    page.locator('#task').select_option('table')
    page.locator('#format').select_option('json')
    run(page)
    table = json.loads(raw(page))
    assert table['columns'] == ['Component', 'Qty', 'Status']
    assert table['rows'][1] == ['Hub', 1, 'Check']
    page.locator('[data-sample="scene"]').click()
    assert 'potted plant' in raw(page)
    page.locator('#task').select_option('text')
    page.locator('#format').select_option('json')
    run(page)
    assert json.loads(raw(page))['text'] == ''
    playwright.expect(page.locator('#review-notice')).to_contain_text('No visible text')


def test_per_image_drafts_survive_design_and_source_switches(preview):
    page = preview
    page.locator('#instructions').fill('Keep currency symbols and spacing.')
    page.locator('#format').select_option('layout')
    for design in LAYOUTS:
        page.locator(f'[data-design="{design}"]').click()
        playwright.expect(page.locator('#extraction-form')).to_have_count(1)
        playwright.expect(page.locator('#instructions')).to_have_value('Keep currency symbols and spacing.')
        playwright.expect(page.locator('#format')).to_have_value('layout')
    page.locator('[data-sample="document"]').click()
    playwright.expect(page.locator('#instructions')).to_have_value('')
    page.locator('[data-sample="receipt"]').click()
    playwright.expect(page.locator('#instructions')).to_have_value('Keep currency symbols and spacing.')
    playwright.expect(page.locator('#result-state')).to_have_text('Settings changed')


def test_batch_selection_search_cancel_and_completion(preview):
    page = preview
    page.locator('[data-design="batch"]').click()
    page.locator('#inbox-search').fill('no-image')
    playwright.expect(page.locator('#inbox-empty')).to_be_visible()
    page.locator('#inbox-search').fill('')
    page.get_by_label('Select workspace-notes.png', exact=True).uncheck()
    playwright.expect(page.locator('#batch-count')).to_have_text('2 selected')
    page.locator('#run-batch').click()
    page.locator('#cancel').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('cancelled')
    page.locator('#run-batch').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('2 samples ready')
    for name in ('cedar-market.png', 'workspace-notes.png', 'studio-corner.svg'):
        page.get_by_role('button', name=name, exact=False).click()
        playwright.expect(page.locator('#source-name')).to_have_text(name)


def test_empty_failure_no_model_and_schema_validation(preview):
    page = preview
    for scenario in ('empty', 'no-model'):
        page.locator('#scenario').select_option(scenario)
        playwright.expect(page.locator('#run')).to_be_disabled()
    page.locator('#scenario').select_option('error')
    playwright.expect(page.locator('#copy')).to_be_disabled()
    page.locator('#retry').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('1 sample ready')
    page.locator('#task').select_option('custom')
    page.locator('#schema').fill('{broken')
    page.locator('#run').click()
    playwright.expect(page.locator('#action-status')).to_contain_text('Schema:')
    page.locator('#reset').click()
    page.locator('#task').select_option('custom')
    run(page)
    assert json.loads(raw(page)) == {'merchant': 'Cedar Market', 'total': 23.52}


@pytest.mark.parametrize('method', ['file', 'drop', 'paste'])
def test_upload_is_local_and_never_displays_fixture_as_extracted(preview, method):
    page = preview
    buffer = io.BytesIO()
    Image.new('RGB', (50, 30), 'white').save(buffer, format='PNG')
    if method == 'file':
        page.locator('#image-file').set_input_files({'name': 'my-receipt.png', 'mimeType': 'image/png', 'buffer': buffer.getvalue()})
    else:
        page.evaluate('''({bytes, method}) => {
          const transfer = new DataTransfer();
          transfer.items.add(new File([new Uint8Array(bytes)], 'my-receipt.png', {type:'image/png'}));
          if (method === 'drop') document.querySelector('#drop-zone').dispatchEvent(new DragEvent('drop', {dataTransfer:transfer, bubbles:true}));
          else document.dispatchEvent(new ClipboardEvent('paste', {clipboardData:transfer, bubbles:true}));
        }''', {'bytes': list(buffer.getvalue()), 'method': method})
    playwright.expect(page.locator('#source-name')).to_have_text('my-receipt.png')
    playwright.expect(page.locator('#run')).to_be_disabled()
    playwright.expect(page.locator('#copy')).to_be_disabled()
    playwright.expect(page.locator('#result-empty')).to_be_visible()
    assert page.locator('#source-image').get_attribute('src').startswith('blob:')
    page.locator('#image-file').set_input_files({'name': 'not-image.svg', 'mimeType': 'image/svg+xml', 'buffer': b'<svg/>'})
    playwright.expect(page.locator('#action-status')).to_contain_text('Choose a PNG, JPEG or WebP')
    page.locator('[data-sample="receipt"]').click()
    playwright.expect(page.locator('#run')).to_be_enabled()


def test_keyboard_copy_fallback_and_formula_safe_csv(preview):
    page = preview
    page.locator('#field-merchant').fill('=HYPERLINK("example")')
    page.locator('#field-merchant').press('Tab')
    # Edits are rendered as text, never interpreted as markup.
    playwright.expect(page.locator('#receipt-merchant')).to_have_text('=HYPERLINK("example")')
    page.locator('#copy').click()
    playwright.expect(page.locator('#action-status')).to_have_text(re.compile(r'Output copied\.|Clipboard access is unavailable\.'))
    page.locator('#format').select_option('csv')
    run(page)
    assert "'=HYPERLINK" in raw(page)
    page.locator('[data-design="review"]').focus()
    page.keyboard.press('Enter')
    playwright.expect(page.locator('#extraction-workspace')).to_have_attribute('data-layout', 'review')
    # Keep the chooser's user gesture on this page when browser suites run concurrently.
    page.bring_to_front()
    page.locator('#drop-zone').focus()
    playwright.expect(page.locator('#drop-zone')).to_be_focused()
    with page.expect_file_chooser():
        page.locator('#drop-zone').press('Enter')


def test_all_designs_themes_states_and_narrow_layouts(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1600, 1024, 760, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for design in LAYOUTS:
                page.locator(f'[data-design="{design}"]').click()
                for scenario in ('ready', 'empty', 'error', 'no-model'):
                    page.locator('#scenario').select_option(scenario)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, design, scenario)
                page.locator('#scenario').select_option('ready')
                page.locator('[data-sample="document"]').click()
                page.locator('[data-result-view="raw"]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, design, 'layout text')
                page.locator('[data-sample="receipt"]').click()
                page.locator('[data-result-view="preview"]').click()
        # Palette changes also flow through the shared Appearance code without a reload.
        before = page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--theme-surface")')
        page.evaluate('''() => { const option = document.querySelector('[data-theme-options] input[value="clay"]'); option.checked = true; option.dispatchEvent(new Event('change', {bubbles:true})); }''')
        assert page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--theme-surface")') != before
        page.evaluate('''() => { const option = document.querySelector('[data-theme-options] input[value="slate"]'); option.checked = true; option.dispatchEvent(new Event('change', {bubbles:true})); }''')
