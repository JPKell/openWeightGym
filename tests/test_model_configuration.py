"""The overview keeps the complete model configuration and its per-node save semantics."""
from copy import deepcopy

import pytest

from engines import engine_for
from engines.llama_cpp.form import settings_from
from loadcoach.models import Models
from test_console_browser import browser_context, playwright
from test_model_matrix import Form, matrix  # noqa: F401
from test_model_setup import desk, fake_llama_server, native, setup_desk, fixture_model  # noqa: F401
from test_weightroom import sign_in
from turnstile.model_configuration import GLOBAL_FLAGS
from weightroom.app import read


def test_every_saved_setting_survives_an_unchanged_overview_save(matrix):
    browser, fleet = matrix
    for name, models in fleet.models.items():
        model = models['qwen']
        model.update(order=-12, min_context=1024, alone=True)
        model['flags'].update({'temp': None, 'top-k': 0, 'seed': 0, 'ignore-eos': False,
                               'logit-bias': ['100+1', '200-1'], 'chat-template-kwargs': '{"enable_thinking":false}',
                               'mmproj': f'/{name}/cache/p.gguf',
                               'parallel': -1, 'kv-unified': True, 'kv-unified-per-slot': 0,
                               'threads': None if name == 'compute' else 8, 'embedding': False,
                               'new-runtime-option': [1, 2]})
        model['adapters'] = {'style': {'file': f'/{name}/cache/a.gguf', 'scale': 0}}
        if name == 'compute':
            model.update(gpus='any', allow_split=True)
    before = deepcopy(fleet.models)
    response = browser.get('/models/qwen?node=compute')
    assert response.status_code == 200
    form = Form(response.text, 'data-model-settings')
    saved = browser.post('/models/qwen?node=compute', data=form.data(do='save-settings'))
    assert saved.status_code == 200 and 'No changes to save.' in saved.text
    assert fleet.models == before


def test_every_shared_generation_setting_has_a_control_or_picker_option(matrix):
    browser, _ = matrix
    response = browser.get('/models/qwen?node=compute')
    form = Form(response.text, 'data-model-settings')
    direct = {value for name, value in form.fields if name == 'flag'}
    for flag in GLOBAL_FLAGS - direct:
        assert f'<option value="{flag}">' in response.text, flag


def test_null_override_is_explicit_and_can_be_removed():
    form = {'file': ['/model.gguf'], 'ctx': ['4096'], 'flag': ['threads', 'temp'], 'value': ['8', '0.6'],
            'suppress_flag': ['threads', 'temp']}
    assert settings_from(form, read)['flags'] == {'model': '/model.gguf', 'ctx-size': 4096, 'threads': None, 'temp': None}
    form.pop('suppress_flag')
    assert settings_from(form, read)['flags']['threads'] == 8


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_node_scope_edits_selected_nodes_and_keeps_every_draft(matrix, width):
    server, fleet = matrix
    fleet.models['compute']['qwen']['gpus'] = 1
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/models/qwen?node=compute')
        page.get_by_role('button', name='Edit Context and requests', exact=True).click()
        page.locator('#ctx').fill('16384')
        # Scope changes never overwrite a draft with a different node's values.
        page.locator('label:has([data-model-node][value="rig"])').click()
        playwright.expect(page.locator('[data-model-kpi="context"]')).to_have_text('Varies by node')
        page.locator('#ctx').fill('24576')
        playwright.expect(page.locator('[name="n:rig:ctx"]')).to_have_value('24576')
        page.locator('label:has([data-model-node][value="compute"])').click()
        page.locator('[name="n:rig:ctx"]').fill('2048')
        playwright.expect(page.locator('#ctx')).to_have_value('24576')
        # Hardware controls stay matched when only one node has a legacy GPU-count control.
        page.locator('label:has([data-model-node][value="compute"])').click()
        page.get_by_role('button', name='Edit Hardware', exact=True).click()
        page.locator('[name="alone"]').check()
        playwright.expect(page.locator('[name="n:rig:alone"]')).to_be_checked()
        page.locator('label:has([data-model-node][value="compute"])').click()
        # Hidden node fields still belong to the same settings form and submit.
        sent = page.locator('#model-settings').evaluate('form => Object.fromEntries(new FormData(form))')
        assert sent['ctx'] == '24576' and sent['n:rig:ctx'] == '2048'
        for key in ('context', 'generation', 'hardware', 'model', 'adapters', 'advanced'):
            page.locator(f'#model-group-{key} [data-model-edit]').click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), key
        page.locator('[data-matrix-differences]').check()
        page.locator('#generation-temp').evaluate("input => { input.value = '-1'; }")
        assert not page.locator('#model-settings').evaluate('form => form.reportValidity()')
        playwright.expect(page.locator('#generation-temp')).to_be_visible()
        playwright.expect(page.locator('[data-matrix-differences]')).not_to_be_checked()
        # Offline inventory prevents server mutations but never blocks local inspection.
        playwright.expect(page.get_by_role('button', name='Save', exact=True)).to_be_disabled()
        page.get_by_role('button', name='Discard changes', exact=True).click()
        playwright.expect(page.locator('#ctx')).to_have_value('8192')
        playwright.expect(page.locator('#model-group-context [data-matrix-row]').first).to_have_attribute('data-differs', 'true')
        assert not errors
        browser.close()


def test_new_shared_flags_save_and_discard_dynamic_rows(setup_desk, tmp_path):
    server, _, _ = setup_desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    original = fixture_model(tmp_path)
    library.put('example', original)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.goto('http://console.test/models/example')
        page.get_by_role('button', name='Edit Generation defaults', exact=True).click()
        page.locator('#add-generation-flag').select_option('reasoning-format')
        page.locator('[data-add-generation]').click()
        page.get_by_label('value of reasoning-format', exact=True).fill('none')
        playwright.expect(page.locator('[data-model-dirty]')).to_contain_text('Unsaved changes')
        page.get_by_role('button', name='Discard changes', exact=True).click()
        playwright.expect(page.get_by_label('value of reasoning-format', exact=True)).to_have_count(0)
        page.get_by_role('button', name='Edit Advanced loading flags', exact=True).click()
        page.get_by_role('button', name='Add flag', exact=True).click()
        playwright.expect(page.locator('[data-model-added]')).to_have_count(1)
        page.locator('[data-model-added] input[name="flag"]').fill('threads')
        page.locator('[data-model-added] input[name="value"]').fill('3')
        page.locator('[data-model-added] input[name="suppress_flag"]').check()
        assert page.locator('#model-settings').evaluate("form => new FormData(form).getAll('suppress_flag')") == ['threads']
        page.locator('[data-model-added] input[name="suppress_flag"]').uncheck()
        page.get_by_role('button', name='Done editing Advanced loading flags', exact=True).click()
        page.get_by_role('button', name='Save', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert library.stored('example')['flags']['threads'] == 3
        browser.close()
    library.db.close()


def test_native_validation_reveals_a_hidden_editor_and_rejected_save_keeps_draft(setup_desk, tmp_path):
    server, _, _ = setup_desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    original = fixture_model(tmp_path)
    library.put('example', original)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/models/example')
        page.get_by_role('button', name='Edit Context and requests', exact=True).click()
        page.locator('#ctx').fill('0')
        page.get_by_role('button', name='Done editing Context and requests', exact=True).click()
        page.get_by_role('button', name='Save', exact=True).click()
        playwright.expect(page.locator('#ctx')).to_be_visible()
        assert library.stored('example') == original
        page.locator('#ctx').fill('4096')
        page.get_by_role('button', name='Edit Advanced loading flags', exact=True).click()
        page.get_by_label('another flag', exact=True).fill('unknown-runtime-flag')
        page.get_by_label('its value', exact=True).fill('1')
        page.get_by_role('button', name='Save', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('not advertised')
        playwright.expect(page.locator('#ctx')).to_have_value('4096')
        playwright.expect(page.get_by_role('button', name='Discard changes', exact=True)).to_be_enabled()
        assert library.stored('example') == original
        page.get_by_role('button', name='Discard changes', exact=True).click()
        playwright.expect(page.locator('#ctx')).to_have_value(str(original['flags']['ctx-size']))
        assert not errors
        browser.close()
    library.db.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_strata_schema_fields_remain_available_without_llama_controls(desk, tmp_path, width):
    server, _, _ = desk
    library = Models(str(tmp_path / 'loadcoach.db'))
    settings = {'engine': 'strata', 'flags': {'model': '/prepared/weights.gguf', 'ctx-size': 512},
                'strata': {'tokenizer': '/prepared/tokenizer'}, 'gpus': ['missing-gpu'], 'alone': True, 'min_context': 256}
    library.put('hybrid', settings)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': width, 'height': 1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/models/hybrid?node=compute')
        playwright.expect(page.locator('[data-model-group]')).to_have_count(3)
        expected = {'flags.model': 'file', 'strata.tokenizer': 'tokenizer', 'flags.ctx-size': 'ctx', 'gpus': 'gpu_card'}
        assert set(expected) == {field['name'] for field in engine_for('strata').schema()['model']}
        sent = page.locator('#model-settings').evaluate('form => Object.fromEntries(new FormData(form))')
        assert all(field in sent for field in (*expected.values(), 'min_context', 'alone', 'order'))
        assert sent['gpu_card'] == 'missing-gpu'
        assert not {'flag', 'value', 'parallel_controls', 'adapter', 'projector', 'cpu', 'allow_split'} & sent.keys()
        for key in ('context', 'hardware', 'model'):
            page.locator(f'#model-group-{key} [data-model-edit]').click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
    library.db.close()
