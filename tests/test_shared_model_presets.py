"""Preset sharing uses metadata/file identity, never another model's selected artifacts."""

import copy
import json

import pytest

from db import Database, migrate
from db.schemas import MODELS
from db.transfer import copy_store
from loadcoach.models import Models
from test_app import fake_llama_server, gateway, in_an_empty_folder, model  # noqa: F401
from test_model_setup import configuration, native  # noqa: F401
from test_postgresql import postgres  # noqa: F401
from test_weightroom import gguf


def quant(tmp_path, filename, name='Example 8B', size='8B', architecture='qwen'):
    path = tmp_path / filename
    gguf(path, general__name=name, general__size_label=size, general__architecture=architecture,
         **{architecture + '__context_length': 8192})
    return model(str(path), 0)


def shared_contract(source, tmp_path):
    library = Models(source)
    q4 = quant(tmp_path, 'model-Q4_K_M.gguf')
    q8 = quant(tmp_path, 'model-Q8_0.gguf', name='Example_8B_Q8_0', size='8.0B')
    for label, settings in [('source', q4), ('target', q8)]:
        projector, adapter = tmp_path / f'{label}-projector.gguf', tmp_path / f'{label}-adapter.gguf'
        gguf(projector, general__architecture='clip', general__name='Example 8B')
        gguf(adapter, general__architecture='qwen', general__type='adapter')
        settings['flags']['mmproj'] = str(projector)
        settings['adapters'] = {label: {'file': str(adapter), 'scale': .5 if label == 'source' else .75}}
    library.put('source', q4); library.put('target', q8); library.put('alias', copy.deepcopy(q4))
    candidate = configuration({**q4, 'flags': {**q4['flags'], 'ctx-size': 4096}}, -2)
    library.save_preset('source', 'Daily', candidate)
    assert library.preset_group('source')['key'] == library.preset_group('target')['key']
    assert library.presets('alias')[0]['name'] == 'Daily'
    selected = library.presets('target')[0]['configuration']
    assert selected['settings']['flags'] == {**q8['flags'], 'ctx-size': 4096}
    assert selected['settings']['adapters'] == q8['adapters'] and selected['order'] == -2
    before, source_before = library.setup('target'), library.setup('source')
    library.change_setup('target', selected, before, identity=library.identity('target'), revision=0)
    assert library.file_revision('target') == 0 and library.setup('source') == source_before
    receipt = library.setup_undo('target')['receipt']
    library.change_setup('target', before, selected, receipt, identity=library.identity('target'), revision=0)
    assert library.setup('target') == before and library.setup_undo('target') is None
    library.remove('source')
    assert library.presets('target')[0]['name'] == 'Daily'
    library.rename('target', 'renamed')
    library.db.close()
    library = Models(source)
    assert library.presets('renamed')[0]['name'] == 'Daily'
    assert library.delete_preset('alias', 'Daily')
    assert library.presets('renamed') == []
    return library


def test_shared_presets_preserve_target_files_lifecycle_and_undo(tmp_path):
    shared_contract(tmp_path / 'models.db', tmp_path).db.close()


def test_shared_presets_on_postgresql_and_verified_transfer(postgres, tmp_path):
    library = shared_contract(postgres, tmp_path)
    library.save_preset('alias', 'Exported', library.setup('alias'))
    library.db.close()
    target = Database.sqlite(tmp_path / 'copy.db')
    assert copy_store(postgres, target)['state'] == 'verified'
    restored = Models(target)
    assert restored.presets('renamed')[0]['name'] == 'Exported'
    restored.db.close()


@pytest.mark.parametrize('metadata', [
    {'name': 'Example 8B Thinking'}, {'name': 'Example 8B Instruct'},
    {'name': 'Example 8B Heretic'}, {'name': 'Example v2 8B'},
    {'size': '9B'}, {'architecture': 'llama'},
])
def test_unrelated_name_size_architecture_and_variants_do_not_share(tmp_path, metadata):
    library = Models(tmp_path / 'models.db')
    source = quant(tmp_path, 'source.gguf')
    library.put('same-label', source)
    library.put('other', quant(tmp_path, 'other.gguf', **metadata))
    library.save_preset('same-label', 'Daily', configuration(source))
    assert library.presets('other') == []
    library.db.close()


def test_incomplete_metadata_shares_only_the_same_existing_file(tmp_path):
    library = Models(tmp_path / 'models.db')
    first, other = tmp_path / 'first.gguf', tmp_path / 'other.gguf'
    for path in (first, other):
        gguf(path, general__name='No size', general__architecture='qwen')
    link = tmp_path / 'alias.gguf'; link.symlink_to(first)
    for name, path in [('first', first), ('alias', link), ('other', other)]:
        library.put(name, model(str(path), 0))
    library.save_preset('first', 'Daily', library.setup('first'))
    assert library.preset_group('alias')['basis'] == 'file'
    assert library.presets('alias')[0]['configuration']['settings']['flags']['model'] == str(link)
    assert library.presets('other') == []
    for name in ('missing', 'missing-alias'):
        library.put(name, model(str(tmp_path / 'not-here.gguf'), 0))
    library.save_preset('missing', 'Private', library.setup('missing'))
    assert library.presets('missing-alias') == []
    library.remove('missing'); library.put('missing', model(str(tmp_path / 'not-here.gguf'), 0))
    assert library.presets('missing') == []
    library.db.close()


@pytest.mark.parametrize('missing', [False, True])
def test_file_and_entry_presets_remain_accessible_when_metadata_becomes_available(tmp_path, missing):
    library = Models(tmp_path / 'models.db')
    path = tmp_path / 'download.gguf'
    if not missing:
        gguf(path, general__architecture='qwen')
    library.put('download', model(str(path), 0))
    library.save_preset('download', 'Daily', library.setup('download'))
    # A completed download or better metadata must not make old presets disappear.
    gguf(path, general__architecture='qwen', general__name='Example 8B', general__size_label='8B')
    library.put('sibling', quant(tmp_path, 'sibling.gguf'))
    library.save_preset('sibling', 'Daily', library.setup('sibling'))
    rows = library.presets('download')
    assert len(rows) == 2 and len({row['name'] for row in rows}) == 2
    local = next(row for row in rows if not row['scope'].startswith('family:'))
    library.save_preset('download', local['name'], configuration(model(str(path), 0, **{'ctx-size': 4096})))
    assert next(row for row in library.presets('download') if row['name'] == local['name'])['configuration']['settings']['flags']['ctx-size'] == 4096
    assert library.delete_preset('download', local['name'])
    assert len(library.presets('sibling')) == len(library.presets('download')) == 1
    library.db.close()


def legacy_contract(database, tmp_path, fail=False):
    connection = database.connect()
    migrate(database, connection, 'loadcoach.models', MODELS[:5])
    with connection:
        for index in (1, 2):
            settings = quant(tmp_path, f'quant-{index}.gguf')
            settings['flags']['ctx-size'] = index * 1024
            identity, name = f'model_{index}', f'alias-{index}'
            connection.execute('INSERT INTO models(name,settings,identity) VALUES (?,?,?)', (name, json.dumps(settings), identity))
            connection.execute('INSERT INTO model_identities VALUES (?,0,NULL)', (identity,))
            connection.execute('INSERT INTO model_presets VALUES (?,?,?,?)',
                               (identity, 'Daily', json.dumps(configuration(settings)), index))
    if fail:
        from unittest.mock import patch
        from db.connection import Connection
        original = Connection.execute
        def fail_write(current, statement, parameters=()):
            if 'INSERT INTO shared_model_presets' in statement:
                raise RuntimeError('fixture preset migration failure')
            return original(current, statement, parameters)
        with patch.object(Connection, 'execute', fail_write), pytest.raises(RuntimeError, match='migration failure'):
            migrate(database, connection, 'loadcoach.models', MODELS)
        assert connection.execute('SELECT COUNT(*) FROM model_presets').fetchone()[0] == 2
        assert connection.execute("SELECT MAX(version) FROM suite_schema_migrations WHERE stream='loadcoach.models'").fetchone()[0] == 5
    connection.close()
    library = Models(database)
    presets = library.presets('alias-2')
    assert {row['name'] for row in presets} == {'Daily', 'Daily [alias-1]'}
    assert {row['configuration']['settings']['flags']['ctx-size'] for row in presets} == {1024, 2048}
    assert library.db.execute('SELECT COUNT(*) FROM model_presets').fetchone()[0] == 0
    library.db.close()
    library = Models(database)
    assert len(library.presets('alias-1')) == 2  # migration is idempotent
    library.db.close()


def test_existing_presets_migrate_without_losing_conflicting_names(tmp_path):
    legacy_contract(Database.sqlite(tmp_path / 'legacy.db'), tmp_path, fail=True)


def test_existing_postgresql_presets_migrate_without_losing_conflicts(postgres, tmp_path):
    legacy_contract(postgres, tmp_path, fail=True)


def test_api_revalidates_shared_preset_edits_for_target_and_preserves_undo(native, tmp_path):
    q4 = quant(tmp_path, 'source.gguf')
    q8 = quant(tmp_path, 'target.gguf')
    with gateway(native, {'source': q4, 'target': q8}) as caller:
        def request(name, action, **body):
            return caller.post('/loadcoach/model-setup', json={'name': name, 'action': action, **body})
        def save(name, context, replace=False):
            settings = q4 if name == 'source' else q8
            candidate = configuration({**settings, 'flags': {**settings['flags'], 'ctx-size': context}})
            report = request(name, 'preview', configuration=candidate).json()
            return request(name, 'save-preset', preset='Daily', configuration=candidate,
                           token=report['token'], acknowledge=True, replace=replace)
        assert save('source', 4096).status_code == 200
        available = caller.get('/loadcoach/model-setup?name=target').json()
        assert available['preset_group']['basis'] == 'family' and available['presets'][0]['name'] == 'Daily'
        assert save('target', 6000).status_code == 409  # explicit replacement applies across aliases
        report = request('target', 'preview', preset='Daily').json()
        assert report['candidate']['settings']['flags']['model'] == q8['flags']['model']
        assert save('source', 6000, replace=True).status_code == 200
        assert request('target', 'apply', preset='Daily', token=report['token'], acknowledge=True).status_code == 409
        report = request('target', 'preview', preset='Daily').json()
        assert request('target', 'apply', preset='Daily', token=report['token'], acknowledge=True).status_code == 200
        state = caller.get('/loadcoach/model-setup?name=target').json()
        undo = request('target', 'preview', undo=state['undo']['receipt']).json()
        assert request('target', 'apply', undo=state['undo']['receipt'], token=undo['token'], acknowledge=True).status_code == 200
        models = {row['name']: row for row in caller.get('/loadcoach/models').json()['models']}
        assert models['target']['flags']['model'] == q8['flags']['model']
        assert models['target']['flags']['ctx-size'] == models['source']['flags']['ctx-size'] == 2048
        assert request('target', 'delete-preset', preset='Daily').status_code == 200
        assert caller.get('/loadcoach/model-setup?name=source').json()['presets'] == []
