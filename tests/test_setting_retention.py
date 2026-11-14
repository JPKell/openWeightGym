"""Live control-node retention and safe retirement of forwarded node retention."""
import time

from loadcoach.config import load_config
from loadcoach.log import Log, SENT_KEPT
from loadcoach.setting_catalog import changeable_for, validate
from loadcoach.settings import Changes, laid_over, live
from test_app import add_user, fake_llama_server, gateway


def test_control_node_event_retention_is_read_again_at_each_prune(tmp_path, monkeypatch):
    changes = Changes(tmp_path / 'settings.db')
    log = Log(tmp_path / 'events.db', keep_days=lambda: live('turnstile', 'keep_days', 90, changes=changes))
    now = time.time()
    with monkeypatch.context() as old:
        old.setattr('loadcoach.log.time.time', lambda: now - 10 * 86400)
        log.add('test', 'old')
    log.add('test', 'recent')
    try:
        assert log.prune() == 0
        changes.put('turnstile', 'keep_days', 7, 'operator')
        assert log.keep_days == 7 and log.prune() == 1
        changes.put('turnstile', 'keep_days', 0, 'operator')
        assert log.keep_days == 0 and log.prune() == 0
        changes.take_away('turnstile', 'keep_days')
        assert log.keep_days == 90
    finally:
        changes.db.close()
        log.db.close()


def test_forwarding_nodes_ignore_retired_overrides_without_deleting_them(tmp_path):
    path = tmp_path / 'loadcoach.toml'
    path.write_text('front = "http://control:8442"\nkeep_days = 3\n')
    changes = Changes(tmp_path / 'loadcoach.db')
    try:
        changes.put('loadcoach', 'keep_days', 999, 'owner')
        config = load_config(path)
        assert config.keep_days == 0 and config.file['keep_days'] == 3
        assert changes.of('loadcoach')['keep_days'] == 999
        assert 'keep_days' not in changeable_for('loadcoach', vars(config))
        assert laid_over({'front': 'http://control', 'keep_days': 3}, {'keep_days': 999}, 'loadcoach')['keep_days'] == 3
        path.write_text('keep_days = 3\n')
        standalone = load_config(path)
        assert standalone.keep_days == 999
        assert changeable_for('loadcoach', vars(standalone))['keep_days'] == 'now'
        validate('loadcoach', 'keep_days', 0)
    finally:
        changes.db.close()


def test_retired_rows_are_hidden_but_remain_available_for_machine_inspection(tmp_path):
    changes = Changes(tmp_path / 'settings.db')
    try:
        changes.put('loadcoach', 'keep_days', 999, 'owner')
        changes.seen('loadcoach', {'keep_days': {'value': 0, 'retired': True}, 'keep_text': {'value': 50}})
        assert [row['name'] for row in changes.everything()[0]['settings']] == ['keep_text']
        assert {row['name'] for row in changes.everything(include_retired=True)[0]['settings']} == {'keep_days', 'keep_text'}
        assert changes.of('loadcoach')['keep_days'] == 999
    finally:
        changes.db.close()


def test_forwarded_spool_keeps_unsent_events_and_bounds_acknowledged_copies(tmp_path, monkeypatch):
    log = Log(tmp_path / 'events.db', keep_days=0, chained=False)
    now = time.time()
    try:
        with monkeypatch.context() as old:
            old.setattr('loadcoach.log.time.time', lambda: now - SENT_KEPT - 1)
            acknowledged = log.add('test', 'old-acknowledged', 'owner', trace='old')
        recent = log.add('test', 'recent-acknowledged', 'owner', trace='recent')
        with monkeypatch.context() as old:
            old.setattr('loadcoach.log.time.time', lambda: now - SENT_KEPT - 1)
            pending = log.add('test', 'old-unsent', 'owner', trace='pending')
        log.db.execute('INSERT INTO forwarded(through) VALUES(?)', (recent,))
        assert log.prune() == 1
        assert {r[0] for r in log.db.execute('SELECT id FROM events')} == {recent, pending}
        assert acknowledged not in {r[0] for r in log.db.execute('SELECT id FROM events')}
    finally:
        log.db.close()


def test_forwarding_node_page_and_post_do_not_offer_retired_retention(fake_llama_server, tmp_path):
    with gateway(fake_llama_server, database=str(tmp_path / 'core.db')) as api:
        admin = add_user(api, 'owner', 'admin')
        found = api.get('/turnstile/machines/compute/settings', headers=admin)
        assert found.status_code == 200
        rows = next(p['settings'] for p in found.json()['programs'] if p['program'] == 'loadcoach')
        assert 'keep_days' not in {row['name'] for row in rows}
        assert 'keep_text' in {row['name'] for row in rows}
        refused = api.post('/turnstile/machines/compute/settings/loadcoach', json={'keep_days': 1}, headers=admin)
        assert refused.status_code in (400, 403)
