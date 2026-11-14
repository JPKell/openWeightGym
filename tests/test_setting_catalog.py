"""The setting catalogue is the common contract for files, APIs and live getters."""

import math
import json
import asyncio
from copy import deepcopy
import tomllib
from pathlib import Path

import pytest

from loadcoach.setting_catalog import CATALOG, Setting, changeable, get, validate, validate_values

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / 'settings/defaults').glob('*.toml')) + sorted((ROOT / 'settings/examples').glob('*.toml'))


def configured(path):
    values = tomllib.loads(path.read_text())
    turnstile = values.pop('turnstile', {}) if path.stem == 'weightroom' else {}
    return [(path.stem, name, value) for name, value in values.items()] + [('turnstile', name, value) for name, value in turnstile.items()]


@pytest.mark.parametrize('path', FILES, ids=lambda path: str(path.relative_to(ROOT)))
def test_every_defaults_and_example_setting_has_a_valid_contract(path):
    for program, name, value in configured(path):
        assert get(program, name), (path, program, name)
        assert validate(program, name, value) == value


@pytest.mark.parametrize('entry', list(CATALOG.values()), ids=lambda entry: f'{entry.program}.{entry.name}')
def test_catalogue_contracts_reject_wrong_types_and_nonfinite_numbers(entry):
    bad = {'bool': 1, 'int': True, 'float': True, 'text': [], 'choice': [], 'secret': [], 'list': {}, 'table': []}[entry.kind]
    with pytest.raises(ValueError):
        validate(entry.program, entry.name, bad)
    if entry.kind in ('int', 'float'):
        for bad in (math.inf, -math.inf, math.nan):
            with pytest.raises(ValueError):
                validate(entry.program, entry.name, bad)
        if entry.min is not None:
            with pytest.raises(ValueError):
                validate(entry.program, entry.name, entry.min - 1)
        if entry.max is not None:
            with pytest.raises(ValueError):
                validate(entry.program, entry.name, entry.max + 1)


def test_file_values_never_become_changeable_by_inference():
    editable = changeable()
    for entry in CATALOG.values():
        assert (entry.name in editable.get(entry.program, {})) == (entry.tier != 'file' and not entry.retired and not entry.derived)
    assert 'host' not in editable['weightroom']
    with pytest.raises(ValueError, match='Unknown setting'):
        validate('weightroom', 'invented', 'ignored')


def test_compatibility_metadata_is_derived_from_the_catalogue():
    from loadcoach.settings import CHANGEABLE
    from weightroom.setting_labels import LABELS, sections
    assert CHANGEABLE == changeable()
    for entry in CATALOG.values():
        if entry.name in LABELS:
            assert len(LABELS[entry.name]) == 4
    grouped = sections([{'program': 'voice', 'settings': [
        {'name': 'max_active_sessions', 'when': 'now'},
        {'name': 'unrecognised_extension', 'when': 'now'},
    ]}])
    assert [group['title'] for group in grouped] == ['Sessions and limits', 'Additional settings']


EDITABLE = [entry for entry in CATALOG.values() if entry.tier != 'file']


@pytest.mark.parametrize('entry', EDITABLE, ids=lambda entry: f'{entry.program}.{entry.name}')
def test_loader_and_management_api_refuse_the_same_invalid_value(entry, tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from types import SimpleNamespace
    from turnstile.settings import ConsoleSettings

    wrong = {'bool': 1, 'int': True, 'float': True, 'text': [], 'choice': [], 'secret': [], 'list': {}, 'table': []}[entry.kind]
    with pytest.raises(ValueError) as refused:
        validate_values(entry.program, {entry.name: wrong})
    api = ConsoleSettings(tmp_path / 'settings.db', SimpleNamespace(add=lambda *a, **kw: None), None,
                          programs={entry.program})
    api.changes.seen(entry.program, {entry.name: {'value': wrong}})
    user = SimpleNamespace(name='operator', can=lambda ability: True)

    async def handle(request):
        return await api.handle(request, user)

    try:
        with TestClient(Starlette(routes=[Route('/loadcoach/settings/{program}', handle, methods=['POST'])])) as client:
            response = client.post('/loadcoach/settings/' + entry.program, json={entry.name: wrong})
        assert response.status_code == 400
        assert response.json()['error']['message'] == str(refused.value)
        assert api.changes.of(entry.program) == {}
    finally:
        api.changes.db.close()


def test_secret_kind_never_reaches_settings_seen_even_from_a_follower(monkeypatch, tmp_path):
    from loadcoach.settings import Changes, said
    monkeypatch.setitem(CATALOG, ('weightroom', 'credential'), Setting('weightroom', 'credential', 'secret'))
    secret = 'private-catalogue-credential'
    assert 'credential' not in said({'credential': secret}, {'credential': secret}, {}, 'weightroom')
    changes = Changes(tmp_path / 'settings.db')
    try:
        changes.seen('weightroom', {'credential': {'value': secret},
                                   'channels': {'value': {'nested': {'password': secret}}}})
        raw = changes.db.execute('SELECT told FROM settings_seen').fetchone()[0]
        assert secret not in raw and 'credential' not in json.loads(raw)
    finally:
        changes.db.close()


def test_generic_live_application_restores_file_and_unsaid_defaults():
    from loadcoach.settings import apply_live
    from weightroom.app import Settings
    before = Settings(key='private', file={'alert_gpu_temperature': 90})
    changed, refused = apply_live('weightroom', before, {'alert_gpu_temperature': 95, 'backup_retention': 4})
    assert not refused and changed.alert_gpu_temperature == 95 and changed.backup_retention == 4
    reset, refused = apply_live('weightroom', changed, {})
    assert not refused and reset.alert_gpu_temperature == 90 and reset.backup_retention == 10
    invalid, refused = apply_live('weightroom', reset, {'alert_gpu_temperature': 999})
    assert invalid.alert_gpu_temperature == 90 and 'alert_gpu_temperature' in refused


def test_settings_display_uses_the_configured_zone_and_control_node_label(monkeypatch):
    from weightroom import display
    monkeypatch.setattr(display, 'server_zone', lambda: 'UTC')
    assert display.configured_timezone() == 'UTC'
    assert display.configured_timezone('America/Vancouver') == 'America/Vancouver'
    assert display.node_label('front') == 'Control node'
    assert display.node_label('worker') == 'worker'


def test_live_reader_uses_current_store_and_original_file_after_reset(tmp_path):
    from types import SimpleNamespace
    from turnstile.settings import ConsoleSettings
    api = ConsoleSettings(tmp_path / 'settings.db', SimpleNamespace(), None,
                          defaults={'turnstile': {'response_keep_days': 30}})
    try:
        api.changes.put('turnstile', 'response_keep_days', 7, 'operator')
        assert api.live('turnstile', 'response_keep_days', 7) == 7
        api.changes.put('turnstile', 'response_keep_days', 14, 'operator')
        assert api.live('turnstile', 'response_keep_days', 7) == 14
        api.changes.take_away('turnstile', 'response_keep_days')
        assert api.live('turnstile', 'response_keep_days', 7) == 30
        api.changes.put('turnstile', 'response_keep_days', -1, 'operator')
        assert api.live('turnstile', 'response_keep_days', 7) == 30
        api.changes.put('turnstile', 'artifact_machine', 'other', 'operator')
        assert api.live('turnstile', 'artifact_machine', 'original') == 'original'
    finally:
        api.changes.db.close()


def live_examples(entry):
    """Two valid, different values, preserving domain-specific table contracts."""
    values = {(program, name): value for path in FILES for program, name, value in configured(path)}
    base = deepcopy(values.get((entry.program, entry.name)))
    special = {
        'engine_gpus': ({}, {'llama_cpp': []}),
        'model_defaults': ({}, {'ctx-size': 1024}),
        'toolsets': ({}, {'catalogue': {'tools': []}}),
        'tool_levels': ({}, {'catalogue': 'normal'}),
        'third_party_services': (['ollama'], ['docker']),
        'disabled_gpus': ([], ['unavailable-test-card']),
        'models_folder': ('/tmp/catalogue-original', '/tmp/catalogue-changed'),
        'models_storage_folder': ('/tmp/catalogue-original', '/tmp/catalogue-changed'),
        'telephone': ({'enabled': False, 'account_sid': '', 'public_url': '', 'numbers': []},
                      {'enabled': False, 'account_sid': 'AC' + '0' * 32,
                       'public_url': 'https://example.com', 'numbers': []}),
    }
    if entry.name in special:
        return special[entry.name]
    if entry.kind == 'bool':
        return False, True
    if entry.kind in ('int', 'float'):
        base = base if base is not None else max(1, entry.min or 0)
        return base, base - 1 if entry.max is not None and base >= entry.max else base + 1
    if entry.kind in ('text', 'choice'):
        return (entry.choices[0], entry.choices[-1]) if entry.choices else ('original', 'changed')
    if entry.kind == 'list':
        return ['direct'], ['direct', 'hermes']
    if entry.name == 'greetings':
        changed = deepcopy(base)
        changed[next(iter(changed))] += ' Testing.'
        return base, changed
    return {'default': {'model': 'original'}}, {'default': {'model': 'changed'}}


LIVE = [entry for entry in CATALOG.values() if entry.tier == 'now']


@pytest.mark.parametrize('entry', LIVE, ids=lambda entry: f'{entry.program}.{entry.name}')
def test_every_live_setting_changes_through_its_service_getter_without_restart(entry, tmp_path):
    import httpx
    from types import SimpleNamespace
    from loadcoach.settings import taken, with_changes, written, apply_live
    from turnstile.settings import ConsoleSettings

    base, changed = live_examples(entry)
    validate(entry.program, entry.name, base)
    validate(entry.program, entry.name, changed)
    assert base != changed
    (tmp_path / 'loadcoach.toml').write_text('')
    path = tmp_path / (entry.program + '.toml')
    path.write_text(written({entry.name: base}))
    if entry.program in ('voice', 'voice-gateway'):
        settings, following = with_changes(entry.program, path)

        def authority(request):
            return httpx.Response(200, json={entry.name: changed} if request.method == 'GET' else {})

        async def once():
            async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as client:
                try:
                    await asyncio.wait_for(following(client, 'http://authority', 'test-key'), 0.02)
                except TimeoutError:
                    pass

        asyncio.run(once())
        assert settings[entry.name] == changed
    elif entry.program == 'turnstile':
        api = ConsoleSettings(tmp_path / 'settings.db', SimpleNamespace(), None)
        try:
            assert api.live(entry.program, entry.name, base) == base
            api.changes.put(entry.program, entry.name, changed, 'operator')
            assert api.live(entry.program, entry.name, base) == changed
        finally:
            api.changes.db.close()
    elif entry.program == 'weightroom':
        from weightroom.app import Settings
        settings = Settings(key='test-key', file={entry.name: base})
        settings, refused = apply_live(entry.program, settings, {entry.name: changed})
        actual = getattr(settings, entry.name)
        assert not refused and (list(actual) if entry.kind == 'list' else actual) == changed
    else:
        from loadcoach.config import load_config, config_from
        from spotter.app import load_settings, settings_from
        from toollocker.door import load_config as tools_config, config_from as tools_from
        readers = {'loadcoach': (load_config, lambda path, values: config_from(path, values, {entry.name: base})),
                   'spotter': (load_settings, settings_from), 'toollocker': (tools_config, tools_from)}
        read, make = readers[entry.program]
        settings = read(path)
        updated, refused = taken(entry.program, settings, {entry.name: changed}, make)
        assert not refused and getattr(updated, entry.name) == changed
