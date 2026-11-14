"""Retired and computed values stay stored without cluttering the settings workspace."""
from loadcoach.settings import Changes


def test_machine_flags_hide_obsolete_values_without_deleting_saved_rows(tmp_path):
    changes = Changes(tmp_path / 'settings.db')
    try:
        changes.put('weightroom', 'compute', 'owner-value', 'owner')
        changes.seen('weightroom', {name: {'value': value} for name, value in
            [('compute', 'old'), ('spotter', 'old'), ('door', 'derived'), ('wake_mac', ''),
             ('wake_address', ''), ('host', '127.0.0.1')]})
        assert [s['name'] for s in changes.everything()[0]['settings']] == ['host']
        raw = {s['name']: s for s in changes.everything(include_retired=True)[0]['settings']}
        assert raw['compute']['retired'] and raw['door']['derived'] and raw['wake_mac']['deprecated']
        assert changes.of('weightroom')['compute'] == 'owner-value'
        changes.seen('turnstile', {'keep_console_settings_on_front': {'value': True}, 'wake_mac': {'value': '00:11:22:33:44:55'}})
        rows = next(p['settings'] for p in changes.everything() if p['program'] == 'turnstile')
        assert len(rows) == 1 and rows[0]['name'] == 'wake_mac' and rows[0]['deprecated']
    finally:
        changes.db.close()
