"""Browser checks for the standalone, sample-only model settings design proposals."""

import re
import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-model-designs.py'))['render']
    output = tmp_path / 'index.html'
    output.write_text(render())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'The preview must never contact real services.'
        browser.close()


def unique_ids(page):
    return page.evaluate('''() => {
        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
        return ids.length === new Set(ids).size;
    }''')


def test_every_design_fits_both_themes_and_engines(preview):
    page = preview
    for engine in ('llama_cpp', 'strata'):
        page.locator('#sample').select_option(engine)
        for theme in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                for design in ('sheet', 'summary', 'matrix'):
                    page.locator(f'button[data-design="{design}"]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (engine, theme, width, design)
                    assert unique_ids(page), (engine, design)


def test_one_or_many_nodes_with_mixed_values(preview):
    page = preview
    maximum = page.locator('#section-context [data-input="ctx"]')
    expect(maximum).to_have_value('32768')
    page.locator('#scope-nodes label').filter(has_text='orion').click()
    expect(page.locator('#section-context .scope-badge')).to_have_text('2 nodes · atlas and orion')
    expect(maximum).to_have_value('')
    expect(maximum).to_have_attribute('placeholder', 'Mixed')
    expect(page.locator('#section-context [data-key="context"] [data-mixed]')).to_contain_text('orion 12,288')
    maximum.fill('24576')
    expect(page.locator('#change-count')).to_have_text('2 unsaved changes · atlas and orion')
    # Shared settings are one change for every node, whatever the selection.
    page.locator('#section-generation [data-key="temp"] input').fill('0.7')
    expect(page.locator('#change-count')).to_contain_text('3 unsaved changes')
    page.locator('#preview').click()
    rows = page.locator('#review-changes tr')
    expect(rows).to_have_count(3)
    expect(rows.filter(has_text='Temperature')).to_contain_text('All nodes')
    # orion's single 24 GB card no longer holds the larger context, so saving asks for acknowledgement.
    expect(page.locator('#review-warnings')).to_contain_text('orion: needs about')
    page.locator('#review-acknowledge').check()
    page.locator('#apply').click()
    expect(page.locator('#said')).to_contain_text('Saved 3 changes for atlas and orion and shared settings')
    expect(page.locator('#model-state')).to_contain_text('old settings until it reloads')
    expect(page.locator('#change-count')).to_have_text('No unsaved changes.')
    page.locator('#undo').click()
    expect(page.locator('#change-count')).to_contain_text('3 unsaved changes')


def test_warnings_need_acknowledgement_and_errors_block(preview):
    page = preview
    page.locator('#section-context [data-input="ctx"]').fill('65536')
    page.locator('#preview').click()
    expect(page.locator('#review-warnings')).to_contain_text('more than the 40,960')
    expect(page.locator('#apply')).to_be_disabled()
    page.locator('#review-acknowledge').check()
    expect(page.locator('#apply')).to_be_enabled()
    page.locator('#review [data-close]').click()
    page.locator('#section-hardware').get_by_text('Pin to GPUs').click()
    page.locator('#preview').click()
    expect(page.locator('#review-errors')).to_contain_text('choose at least one GPU')
    expect(page.locator('#apply')).to_be_disabled()


def test_summary_cards_open_one_group_to_edit(preview):
    page = preview
    page.locator('button[data-design="summary"]').click()
    card = page.locator('#section-context')
    expect(card.locator('[data-summary]')).to_contain_text('4 requests at once, up to 8,192 tokens each')
    card.get_by_role('button', name='Edit Context and requests').click()
    card.locator('[data-key="parallel_count"] input').fill('8')
    card.get_by_role('button', name='Done editing Context and requests').click()
    expect(card.locator('[data-summary]')).to_contain_text('8 requests at once, up to 8,192 tokens each')
    expect(card).to_have_class(re.compile('is-changed'))


def test_matrix_edits_ticked_columns_together(preview):
    page = preview
    page.locator('button[data-design="matrix"]').click()
    threads = page.locator('.matrix-row').filter(has_text='--threads')
    expect(threads.locator('td').nth(1)).to_have_class(re.compile('differs'))
    page.get_by_label('Edit atlas together with other ticked nodes').check()
    page.get_by_label('Edit orion together with other ticked nodes').check()
    page.get_by_label('threads on atlas').fill('20')
    expect(page.get_by_label('threads on orion')).to_have_value('20')
    expect(page.get_by_label('threads on nova')).to_have_value('16')
    page.locator('#matrix-differences').check()
    expect(page.locator('.matrix-row').filter(has_text='Temperature')).to_be_hidden()
    expect(threads).to_be_visible()


def test_copy_to_several_nodes_and_move_to_one(preview):
    page = preview
    page.locator('#open-transfer').click()
    page.locator('#transfer-targets label').filter(has_text='cedar').click()
    page.locator('#transfer-targets label').filter(has_text='lyra').click()
    expect(page.locator('#transfer-plan')).to_contain_text('lyra reports no GPU')
    expect(page.locator('#transfer-submit')).to_be_disabled()
    page.locator('#transfer-device').select_option('cpu')
    expect(page.locator('#transfer-submit')).to_have_text('Copy to 2 nodes')
    expect(page.locator('#transfer-targets input[value="vega"]')).to_be_disabled()
    page.locator('#transfer-submit').click()
    expect(page.locator('#said')).to_contain_text('Copied qwen3-32b to cedar and lyra')
    expect(page.locator('#locations tr')).to_have_count(5)
    expect(page.locator('#change-count')).to_have_text('No unsaved changes.')

    page.locator('#locations tr').filter(has_text='orion').get_by_role('button', name='Copy or move from orion').click()
    page.locator('.transfer-action label').filter(has_text='Move').click()
    expect(page.locator('#transfer-targets input:not([disabled])')).to_have_count(0)
    expect(page.locator('#transfer-submit')).to_be_disabled()


def test_move_removes_the_source_and_keeps_its_settings(preview):
    page = preview
    page.locator('#open-transfer').click()
    page.locator('.transfer-action label').filter(has_text='Move').click()
    page.locator('#transfer-targets label').filter(has_text='cedar').click()
    page.locator('#transfer-targets label').filter(has_text='lyra').click()
    expect(page.locator('#transfer-targets input:checked')).to_have_count(1)
    page.locator('#transfer-targets label').filter(has_text='cedar').click()
    expect(page.locator('#transfer-plan')).to_contain_text('Then atlas stops offering it')
    page.locator('#transfer-submit').click()
    expect(page.locator('#said')).to_contain_text('Moved qwen3-32b from atlas to cedar')
    expect(page.locator('#locations')).not_to_contain_text('atlas')
    expect(page.locator('#section-context .scope-badge')).to_have_text('This node · cedar')
    expect(page.locator('#section-context [data-input="ctx"]')).to_have_value('32768')


def test_presets_and_location_toggle_confirm(preview):
    page = preview
    page.get_by_role('button', name='Load preset Daily 16K').click()
    expect(page.locator('#section-context [data-input="ctx"]')).to_have_value('16384')
    expect(page.locator('#change-count')).to_contain_text('unsaved change')
    page.locator('#discard').click()
    expect(page.locator('#section-context [data-input="ctx"]')).to_have_value('32768')
    toggle = page.get_by_role('button', name='Disable on orion')
    toggle.click()
    expect(toggle).to_have_text('Confirm disable')
    toggle.click()
    expect(page.locator('#said')).to_contain_text('Disabled qwen3-32b on orion')


def test_strata_keeps_only_its_own_settings(preview):
    page = preview
    page.locator('#sample').select_option('strata')
    for group in ('generation', 'adapters', 'advanced'):
        expect(page.locator(f'#section-{group}')).to_have_count(0)
    expect(page.locator('#section-context [data-key="parallel_enabled"]')).to_have_count(0)
    expect(page.locator('#section-hardware input[type="radio"]')).to_have_count(3)
    expect(page.locator('#section-model')).to_contain_text('Prepared tokenizer directory')
    page.locator('#section-context [data-input="ctx"]').fill('9000')
    page.locator('#preview').click()
    expect(page.locator('#review-errors')).to_contain_text('between 256 and 8,192')
    page.locator('#review [data-close]').click()
    page.locator('#open-transfer').click()
    expect(page.locator('#transfer-device option[value="cpu"]')).to_be_hidden()
