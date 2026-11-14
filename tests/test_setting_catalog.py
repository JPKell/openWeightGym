"""The setting catalogue is the common contract for files, APIs and live getters."""

import math
import tomllib
from pathlib import Path

import pytest

from loadcoach.setting_catalog import CATALOG, changeable, get, validate

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
