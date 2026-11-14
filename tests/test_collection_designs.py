"""Offline coordinated designs: evidence, drafts, review gates and responsive layouts."""

import json
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ('desk', 'ledger', 'guide')


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-collection-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100}, reduced_motion='reduce')
        errors, remote = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: remote.append(request.url) if request.url.startswith(('http:', 'https:')) else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not remote, 'The previews must never call models or remote services.'
        browser.close()


def test_business_catalogue_filters_and_collection_identity(preview):
    page = preview
    expect(page.locator('#collection-list .collection-item')).to_have_count(5)
    page.locator('#collection-state').select_option('Needs attention')
    expect(page.locator('#collection-list .collection-item')).to_have_count(1)
    expect(page.locator('#collection-list')).to_contain_text('Records retention')
    page.locator('#collection-state').select_option('')
    page.locator('#collection-search').fill('invoice')
    page.locator('#collection-list h3 a').click()
    expect(page.locator('#collection-heading h1')).to_have_text('Invoice & payment controls')
    expect(page.locator('#expected-answer')).to_contain_text('30 days')
    expect(page.locator('#document-rows')).to_contain_text('payment-policy.pdf')
    page.locator('[data-go="overview"]').click()
    expect(page.locator('#collection-search')).to_have_value('invoice')
    page.locator('#collection-search').fill('not-a-use-case')
    expect(page.locator('#collection-empty')).to_be_visible()


@pytest.mark.parametrize('design', DESIGNS)
def test_retrieval_threshold_citations_review_and_export(preview, tmp_path, design):
    page = preview
    page.locator(f'button[data-design="{design}"]').click()
    page.locator('[data-page="collection"]').click()
    page.locator('#collection-minimum').fill('.9')
    page.locator('#query-form button[type="submit"]').click()
    expect(page.locator('#no-evidence')).to_be_visible()
    expect(page.locator('#model-answer')).not_to_be_visible()
    page.locator('#collection-minimum').fill('.65')
    page.locator('#collection-answer-model').select_option('Qwen3-32B')
    page.locator('#query-form button[type="submit"]').click()
    expect(page.locator('#model-answer')).to_contain_text('Finance and Legal')
    page.locator('[data-open-source]').click()
    expect(page.locator('#source-meta')).to_contain_text('Page 7')
    expect(page.locator('#source-text')).to_contain_text('written approval')
    page.keyboard.press('Escape')
    page.locator('#compare-models').click()
    expect(page.locator('#comparison-rows tr')).to_have_count(2)
    expect(page.locator('#comparison-rows tr').nth(1)).to_contain_text('Review missing / changed facts')
    page.locator('[data-assessment="Matches reference"]').click()
    page.locator('#review-note').fill('Both approvals and timing are supported by page 7.')
    with page.expect_download() as downloaded:
        page.locator('#export-review').click()
    target = tmp_path / downloaded.value.suggested_filename
    downloaded.value.save_as(target)
    report = json.loads(target.read_text())
    assert report['sample'] is True
    assert report['assessments'][0]['assessment'] == 'Matches reference'
    assert report['assessments'][0]['note'].endswith('page 7.')


def test_custom_question_never_gets_a_fabricated_prepared_answer_and_drafts_survive(preview):
    page = preview
    page.locator('[data-page="collection"]').click()
    page.locator('#collection-question').fill('A custom question without a prepared answer')
    page.locator('#query-form button[type="submit"]').click()
    expect(page.locator('#action-status')).to_contain_text('No prepared result')
    expect(page.locator('#retrieval-result')).not_to_be_visible()
    for design in DESIGNS:
        page.locator(f'button[data-design="{design}"]').click()
        page.locator('[data-page="workflows"]').click()
        page.locator('[data-page="collection"]').click()
        expect(page.locator('#collection-question')).to_have_value('A custom question without a prepared answer')
    page.locator('#scenario').select_option('error')
    page.locator('#retry-connection').click()
    expect(page.locator('#collection-question')).to_have_value('A custom question without a prepared answer')
    page.locator('#reference-question').select_option('missing')
    page.locator('#compare-models').click()
    expect(page.locator('#comparison-rows tr').first).to_contain_text('unavailable')


@pytest.mark.parametrize('design', DESIGNS)
def test_workflow_review_requires_explicit_starts_and_preserves_accepted_research(preview, design):
    page = preview
    page.locator(f'button[data-design="{design}"]').click()
    page.locator('[data-page="workflows"]').click()
    page.get_by_role('button', name='Supplier approval briefing', exact=False).click()
    page.locator('#run-action').click()
    expect(page.locator('#run-status')).to_have_text('Ready for draft')
    expect(page.locator('#draft-output')).not_to_be_visible()
    page.locator('#run-action').click()
    expect(page.locator('#run-status')).to_have_text('Review draft')
    page.locator('#run-action').click()
    expect(page.locator('#run-status')).to_have_text('Completed')
    page.get_by_role('button', name='Contract retention summary', exact=False).click()
    expect(page.locator('#research-state')).to_have_text('Accepted')
    before = page.locator('#research-answer').text_content()
    page.locator('#run-source').click()
    expect(page.locator('#source-meta')).to_contain_text('revision 15')
    page.keyboard.press('Escape')
    page.locator('#run-action').click()
    expect(page.locator('#research-answer')).to_have_text(before)
    expect(page.locator('#run-status')).to_have_text('Review draft')
    page.locator('#workflow-search').fill('no-such-run')
    expect(page.locator('#workflow-empty')).to_be_visible()
    expect(page.locator('#workflow-detail')).not_to_be_visible()


def test_create_plan_and_empty_collection_flow(preview):
    page = preview
    page.locator('[data-page="collection"]').click()
    page.locator('#collection-workflow').click()
    page.locator('#workflow-title').fill('Procurement model comparison')
    page.locator('#new-workflow-form button[type="submit"]').click()
    expect(page.locator('#run-title')).to_have_text('Procurement model comparison')
    expect(page.locator('#run-status')).to_have_text('Ready for research')
    expect(page.locator('#research-output')).not_to_be_visible()
    page.locator('#run-action').click()
    expect(page.locator('#run-status')).to_have_text('Review research')
    page.locator('#scenario').select_option('empty')
    expect(page.locator('#workflow-empty')).to_be_visible()
    page.locator('[data-page="overview"]').click()
    expect(page.locator('#collection-empty')).to_be_visible()
    page.locator('#page-overview .page-actions [data-create-collection]').click()
    page.locator('#collection-title').fill('<b>New test set</b>')
    page.locator('#new-collection-form button[type="submit"]').click()
    expect(page.locator('#collection-heading h1')).to_have_text('<b>New test set</b>')
    assert page.locator('#collection-heading h1 b').count() == 0
    page.locator('#collection-file').set_input_files({'name': 'business-policy.txt', 'mimeType': 'text/plain', 'buffer': b'Sample policy'})
    expect(page.locator('#document-rows')).to_contain_text('Pending')
    page.locator('[data-index-document]').click()
    expect(page.locator('#document-rows [data-state]')).to_have_text('Ready')


def test_all_nine_screens_fit_themes_sizes_and_states_without_duplicate_ids(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1600, 1024, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for design in DESIGNS:
                page.locator(f'button[data-design="{design}"]').click()
                for view in ('overview', 'collection', 'workflows'):
                    page.locator(f'[data-page="{view}"]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, design, view)
                    assert page.evaluate('''() => {
                        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                        return ids.length === new Set(ids).size;
                    }''')
                    expect(page.locator(f'#page-{view}')).to_be_visible()
    page.locator('#scenario').select_option('error')
    expect(page.locator('#connection-error')).to_be_visible()
    page.locator('#scenario').select_option('empty')
    expect(page.locator('#workflow-empty')).to_be_visible()
