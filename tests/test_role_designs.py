"""Browser contracts for sample-only Roles proposals and their routing explanations."""

import runpy
from pathlib import Path

import pytest

from loadcoach.roles import loaded_factors, pick

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preview(tmp_path):
    render = runpy.run_path(str(ROOT / 'scripts/preview-role-designs.py'))['render']
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
        assert not network, 'A sample preview must never contact live services.'
        browser.close()


def test_all_designs_fit_themes_and_phone_with_accessible_fields(preview):
    page = preview
    for theme in ('light', 'dark'):
        if page.locator('html').get_attribute('data-bs-theme') != theme:
            page.locator('#theme').click()
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for layout in ('studio', 'guided', 'canvas'):
                page.locator(f'[data-design="{layout}"]').click()
                for role in ('code', 'vision'):
                    page.locator('[data-role-choice]').filter(has=page.locator('[data-name]', has_text=role)).click()
                    steps = (1, 2, 3) if layout == 'guided' else (None,)
                    for step in steps:
                        if step:
                            page.locator(f'[data-step-to="{step}"]').click()
                        else:
                            page.locator('#tuning').evaluate('(el) => {el.open = true;}')
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, layout, step)
                        assert page.evaluate('''() => {
                            const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                            return ids.length === new Set(ids).size;
                        }''')
                        assert page.evaluate('''() => [...document.querySelectorAll('input,select')]
                            .filter(el => el.getBoundingClientRect().width)
                            .every(el => el.labels?.length || el.getAttribute('aria-label'))''')


def test_request_modes_match_production_picker_for_each_sample_state(preview):
    page = preview
    models = [
        {'name': 'DeepSeek Coder V2', 'weight': 3, 'reliability': .99, 'load': 24, 'speed': 38},
        {'name': 'Qwen3 32B', 'weight': 2.8, 'reliability': .99, 'load': 18, 'speed': 55},
        {'name': 'Qwen3 8B', 'weight': 1, 'reliability': .98, 'load': 8, 'speed': 90},
    ]
    for state in ('normal', 'cold', 'busy'):
        page.locator('#sample-state').select_option(state)
        candidates = []
        for model in models:
            ready = model['name'] == 'Qwen3 32B' and state == 'normal'
            resident = model['name'] == 'Qwen3 32B' and state != 'cold'
            candidates.append({**model, 'warm': ready, 'resident': resident,
                               'score': model['weight'] * model['reliability'],
                               **loaded_factors({}, resident)})
        candidates.sort(key=lambda model: -model['score'])
        for mode in ('default', 'best', 'available', 'fast'):
            page.locator('#request-mode').select_option(mode)
            expected = pick(None if mode == 'default' else mode, candidates)[0]
            expect(page.locator('#picked-model')).to_have_text(expected)
            expect(page.locator('#request-code')).to_have_text('code' + ('' if mode == 'default' else ':' + mode))


def test_drafts_survive_switches_and_save_only_the_selected_role(preview):
    page = preview
    weight = page.locator('[data-candidate="deepseek-coder-v2"] [data-weight]')
    weight.fill('8')
    expect(page.locator('#picked-model')).to_have_text('DeepSeek Coder V2')
    page.locator('[data-design="canvas"]').click()
    expect(weight).to_have_value('8')
    page.locator('[data-role-choice]').filter(has_text='chat').click()
    page.locator('#role-name').fill('conversation')
    page.locator('#save').click()
    expect(page.locator('#said')).to_contain_text('Saved conversation in this browser')
    page.locator('[data-role-choice]').filter(has_text='code').click()
    expect(weight).to_have_value('8')
    expect(page.locator('#save-state')).to_have_text('Unsaved changes')
    page.locator('#discard').click()
    expect(weight).to_have_value('3')
    page.reload()
    page.locator('[data-role-choice]').filter(has_text='conversation').click()
    expect(page.locator('#role-name')).to_have_value('conversation')
    expect(page.locator('#save-state')).to_contain_text('no unsaved changes')


def test_zero_weights_requirements_and_empty_model_state(preview):
    page = preview
    page.locator('[data-candidate="qwen3-32b"] [data-weight]').fill('0')
    expect(page.locator('#picked-model')).to_have_text('DeepSeek Coder V2')
    expect(page.locator('[data-candidate="qwen3-32b"] [data-exclusion]')).to_have_text('Paused · weight is 0')
    page.locator('#tuning > summary').click()
    page.locator('[name="requires-pictures"]').check()
    expect(page.locator('#picked-model')).to_have_text('No eligible model')
    expect(page.locator('#save')).to_be_disabled()
    page.locator('[name="requires-tools"]').uncheck()
    page.locator('#add-model').select_option('qwen2.5-vl-7b')
    page.locator('#add-model-button').click()
    expect(page.locator('#picked-model')).to_have_text('Qwen2.5 VL 7B')
    page.locator('#minimum-context').fill('65536')
    expect(page.locator('#picked-model')).to_have_text('No eligible model')
    page.locator('#discard').click()
    while page.locator('[data-remove]').count():
        page.locator('[data-remove]').first.click()
    expect(page.locator('#models-empty')).to_be_visible()
    expect(page.locator('#picked-model')).to_have_text('No eligible model')
    expect(page.locator('#save')).to_be_disabled()
    page.locator('#discard').click()
    expect(page.locator('[data-candidate]')).to_have_count(3)


def test_benchmark_shares_and_invalid_values(preview):
    page = preview
    page.locator('#tuning > summary').click()
    page.locator('#benchmark-code_retrieval').fill('3')
    page.locator('#benchmark-ifeval').fill('1')
    expect(page.locator('[data-share="code_retrieval"]')).to_have_text('75% of benchmark quality')
    expect(page.locator('[data-share="ifeval"]')).to_have_text('25% of benchmark quality')
    expect(page.locator('#picked-model')).to_have_text('Qwen3 32B')
    page.locator('#evidence > summary').click()
    rows = page.locator('#evidence-rows tr')
    expect(rows.filter(has_text='DeepSeek Coder V2').locator('[data-evidence-quality]')).to_have_text('75%')
    expect(rows.filter(has_text='DeepSeek Coder V2').locator('[data-evidence-score]')).to_have_text('5.197')
    for selector, invalid in [('#loaded-bonus', '101'), ('#fast-bonus', '-1'), ('#minimum-context', '12.5'), ('#benchmark-ifeval', '')]:
        field = page.locator(selector)
        old = field.input_value()
        field.fill(invalid)
        expect(page.locator('#save')).to_be_disabled()
        expect(page.locator('#picked-model')).to_have_text('Check your settings')
        field.fill(old)
    page.locator('#role-name').fill('chat')
    expect(page.locator('#save')).to_be_disabled()
    page.locator('#role-name').fill('code')
    expect(page.locator('#save')).to_be_enabled()


def test_guided_creation_validates_and_keeps_changes_across_layouts(preview):
    page = preview
    page.locator('[data-design="guided"]').click()
    page.locator('#new-role').click()
    expect(page.locator('#new-name')).to_be_focused()
    page.locator('#new-name').fill('chat')
    page.locator('#create-form button[type="submit"], #create-form button:not([type])').click()
    expect(page.locator('#create-error')).to_contain_text('already in use')
    page.locator('#new-name').fill('research')
    page.locator('#create-form button:not([type])').click()
    expect(page.locator('#models-panel')).to_be_visible()
    expect(page.locator('#identity')).to_be_hidden()
    page.locator('#next-step').click()
    expect(page.locator('#said')).to_contain_text('at least one eligible model')
    expect(page.locator('#review-summary')).to_be_hidden()
    page.locator('#add-model').select_option('qwen3-32b')
    page.locator('#add-model-button').click()
    page.locator('#next-step').click()
    expect(page.locator('#review-summary')).to_contain_text('research has 1 eligible model')
    expect(page.locator('#save')).to_be_enabled()
    page.locator('#save').click()
    page.locator('[data-design="studio"]').click()
    expect(page.locator('#role-name')).to_have_value('research')
    expect(page.locator('#request-code')).to_have_text('research')
    page.locator('#new-role').click()
    page.keyboard.press('Escape')
    expect(page.locator('#create-dialog')).not_to_be_visible()


def test_storage_failure_keeps_draft_unsaved_and_names_are_escaped(preview):
    page = preview
    page.locator('#role-name').fill('<img src=x onerror=alert(1)>')
    expect(page.locator('#picked-model')).to_have_text('Check your settings')
    assert page.locator('#role-list img').count() == 0
    page.locator('#role-name').fill('coding')
    page.evaluate("() => { Storage.prototype.setItem = () => { throw new Error('blocked'); }; }")
    page.locator('#save').click()
    expect(page.locator('#said')).to_contain_text('could not save')
    expect(page.locator('#save-state')).to_have_text('Unsaved changes')
