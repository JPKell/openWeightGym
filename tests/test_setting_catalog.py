"""The setting catalogue is the common contract for files, APIs and live getters."""

import math
import json
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
