"""Disk selection is validated once and reaches actual telemetry without restart."""
import pytest
from loadcoach.setting_catalog import validate
from loadcoach.settings import Changes
from spotter.app import settings_from
from test_spotter import made_up


def test_disk_selection_changes_real_readings_and_resets(tmp_path, monkeypatch):
    first, second = tmp_path / 'one', tmp_path / 'two'
    first.mkdir(); second.mkdir()
    (tmp_path / 'loadcoach.toml').write_text('machine_key_file = "machine.key"\n')
    (tmp_path / 'spotter.toml').write_text('')
    with made_up(tmp_path, monkeypatch, disks=(str(first),), path=str(tmp_path / 'spotter.toml'),
                 file={'disks': [str(first)]}) as (api, keys, _, _, _):
        changes = Changes(tmp_path / 'loadcoach.db')
        try:
            def paths():
                answer = api.get('/spotter/now?node=compute', headers=keys['pat'])
                assert answer.status_code == 200, answer.text
                return [row['path'] for row in answer.json()['disks']]
            assert paths() == [str(first)]
            changes.put('spotter', 'disks', [str(second), str(tmp_path / 'missing')], 'owner')
            assert paths() == [str(second)]
            seen = next(p for p in changes.everything() if p['program'] == 'spotter')
            assert next(s for s in seen['settings'] if s['name'] == 'disks')['when'] == 'now'
            changes.take_away('spotter', 'disks')
            assert paths() == [str(first)]
        finally:
            changes.db.close()


@pytest.mark.parametrize('value', [['relative'], [''], ['/bad\x00path'], ['/'] * 33, '/', [1]])
def test_bad_disk_paths_are_rejected_by_loader_and_catalogue(tmp_path, value):
    (tmp_path / 'loadcoach.toml').write_text('machine_key_file = "machine.key"\n')
    (tmp_path / 'spotter.toml').write_text('')
    with pytest.raises(ValueError):
        validate('spotter', 'disks', value)
    with pytest.raises(SystemExit):
        settings_from(tmp_path / 'spotter.toml', {'disks': value})
