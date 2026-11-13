"""Browser contracts for the three sample-only Cloud API proposals."""

import runpy
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    output = tmp_path / 'index.html'
    output.write_text(runpy.run_path(str(ROOT / 'scripts/preview-cloud-designs.py'))['render']())
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1500, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith('http') else None)
        page.goto(output.as_uri())
        yield page
        assert not errors
        assert not network, 'Design previews must never contact real services.'
        browser.close()


def test_layouts_states_themes_and_settings_fit_small_screens(preview):
    page = preview
    for width in (1500, 768, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        for theme in ('light', 'dark'):
            if page.locator('html').get_attribute('data-bs-theme') != theme:
                page.locator('#theme').click()
            expect(page.locator('html')).to_have_attribute('data-bs-theme', theme)
            for layout in ('desk', 'gallery', 'matrix'):
                page.locator(f'[data-design="{layout}"]').click()
                expect(page.locator(f'[data-design="{layout}"]')).to_have_attribute('aria-pressed', 'true')
                for scenario, count in [('one', 1), ('ten', 10), ('empty', 0)]:
                    page.locator('#scenario').select_option(scenario)
                    expect(page.locator('#total-accounts')).to_have_text(str(count))
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, layout, scenario)
                page.locator('#scenario').select_option('six')
                if layout == 'gallery':
                    page.locator('#account-cards [data-open][data-section="models"]').first.click()
                elif layout == 'matrix':
                    page.locator('#matrix-rows [data-section="models"]').first.click()
                for group in ('connection', 'access', 'models', 'limits'):
                    page.locator(f'.ca-settings-nav [data-editor-section="{group}"]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, layout, group)
                    assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                        .filter(el => el.getBoundingClientRect().width && !el.closest('[hidden]'))
                        .every(el => el.type === 'hidden' || el.labels?.length || el.getAttribute('aria-label'))''')
                assert page.evaluate('''() => {
                    const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                    return ids.length === new Set(ids).size;
                }''')
                if layout == 'gallery':
                    page.keyboard.press('Escape')


def test_drafts_follow_accounts_across_designs_and_saves_are_scoped(preview):
    page = preview
    page.locator('#account-url').fill('https://example.invalid/custom/v1')
    page.locator('[data-account="openrouter-research"]').first.click()
    expect(page.locator('#account-url')).to_have_value('https://openrouter.ai/api/v1')
    page.locator('[data-account="openrouter-production"]').first.click()
    expect(page.locator('#account-url')).to_have_value('https://example.invalid/custom/v1')
    expect(page.locator('[data-draft="connection"]')).to_have_text('Unsaved changes')
    page.locator('.ca-settings-nav [data-editor-section="models"]').click()
    page.locator('#model-rows [name="ours"]').first.fill('production-fast-renamed')
    page.locator('[data-design="gallery"]').click()
    page.locator('#account-cards [data-account="openrouter-production"][data-section="limits"]').click()
    page.locator('#account-limit').fill('99')
    page.locator('#limits-form button').click()
    page.locator('.ca-settings-nav [data-editor-section="connection"]').click()
    expect(page.locator('#account-url')).to_have_value('https://example.invalid/custom/v1')
    expect(page.locator('[data-draft="connection"]')).to_have_text('Unsaved changes')
    page.locator('.ca-settings-nav [data-editor-section="models"]').click()
    expect(page.locator('#model-rows [name="ours"]').first).to_have_value('production-fast-renamed')
    page.keyboard.press('Escape')
    page.locator('[data-design="matrix"]').click()
    page.locator('#matrix-rows [data-account="openrouter-production"][data-section="models"]').click()
    expect(page.locator('#model-rows [name="ours"]').first).to_have_value('production-fast-renamed')


def test_same_endpoint_accounts_unique_names_key_install_and_removal(preview):
    page = preview
    page.locator('[data-add]').first.click()
    page.locator('#new-name').fill('openrouter-production')
    page.locator('#add-form button[type="submit"], #add-form button[name="do"]').click()
    expect(page.locator('#add-error')).to_contain_text('already used')
    page.locator('#new-name').fill('openrouter-new-team')
    page.locator('#add-form button[name="do"]').click()
    expect(page.locator('#total-accounts')).to_have_text('7')
    expect(page.locator('#total-endpoints')).to_have_text('4')
    expect(page.locator('#editor-status')).to_have_text('Needs API key')
    page.locator('#account-key').fill('sample-key-never-retained')
    page.locator('#save-key').click()
    expect(page.locator('#account-key')).to_have_value('')
    expect(page.locator('#editor-status')).to_have_text('Key installed')
    assert 'sample-key-never-retained' not in page.evaluate('JSON.stringify(localStorage)')
    page.locator('.ca-settings-nav [data-editor-section="limits"]').click()
    page.locator('#remove-form button').click()
    expect(page.locator('#total-accounts')).to_have_text('7')
    expect(page.locator('#remove-form button')).to_have_text('Are you sure?')
    page.keyboard.press('Escape')
    expect(page.locator('#remove-form button')).to_have_text('Remove account')
    expect(page.locator('#remove-form input[type="checkbox"]')).to_have_count(0)
    page.locator('#remove-form button').click()
    page.locator('#remove-form button').click()
    expect(page.locator('#total-accounts')).to_have_text('6')
    expect(page.locator('[data-account="openrouter-production"]').first).to_be_visible()


def test_model_discovery_validation_filters_and_error_keep_drafts(preview):
    page = preview
    page.locator('.ca-settings-nav [data-editor-section="models"]').click()
    page.locator('#model-rows [name="in"]').first.fill('-1')
    page.locator('#models-form button[name="do"]').click()
    expect(page.locator('#editor-message')).to_contain_text('zero or a positive')
    page.locator('#model-rows [name="in"]').first.fill('0.5')
    page.locator('#discover').click()
    page.get_by_label('sample/vision', exact=True).check()
    page.locator('#offers-form button').click()
    expect(page.locator('#model-rows [name="theirs"]').nth(2)).to_have_value('sample/vision')
    page.locator('#models-form button[name="do"]').click()
    expect(page.locator('#total-models')).to_have_text('11')
    page.locator('#scenario').select_option('error')
    expect(page.locator('#editor-message')).to_contain_text('Sample discovery failed')
    page.locator('#model-rows [name="ours"]').first.fill('keep-my-draft')
    page.locator('#discover').click()
    expect(page.locator('#model-rows [name="ours"]').first).to_have_value('keep-my-draft')
    page.locator('#search').fill('unmatched account')
    expect(page.locator('#empty-title')).to_have_text('No accounts match')
    page.locator('#search').fill('')
    expect(page.locator('#model-rows [name="ours"]').first).to_have_value('keep-my-draft')
    page.locator('#status-filter').select_option('key')
    expect(page.locator('#result-count')).to_have_text('1 of 6 accounts')
