"""Live span policy changes reach the shared store without rebuilding the app."""
import pytest

from loadcoach.log import Log
from loadcoach.observation_store import kept, policy
from loadcoach.setting_catalog import validate
from loadcoach.settings import apply_live
from weightroom.app import Settings, apply_observation_policy
from test_weightroom import desk, sign_in


def test_live_span_policy_changes_writer_sampling_and_resets(tmp_path):
    log = Log(tmp_path / 'events.db')
    settings = Settings(key='test', file={'observation_keep_days': 14})
    try:
        previous = apply_observation_policy(log.db, settings)
        success = {'outcome': 'success', 'trace_id': '0123456789abcdef0123456789abcdef'}
        assert kept(success, policy(log.db)['success_percent'])
        settings, refused = apply_live('weightroom', settings, {
            'observation_keep_days': 8, 'observation_success_percent': 0,
            'observation_max_spans': 100, 'observation_quiet_routes': ['/poll'],
        })
        assert not refused
        previous = apply_observation_policy(log.db, settings, previous)
        assert policy(log.db) == {'keep_days': 8, 'success_percent': 0, 'max_spans': 100, 'quiet_routes': ['/poll']}
        assert not kept(success, policy(log.db)['success_percent'])
        assert kept({**success, 'outcome': 'error'}, policy(log.db)['success_percent'])
        settings, refused = apply_live('weightroom', settings, {})
        assert not refused
        apply_observation_policy(log.db, settings, previous)
        assert policy(log.db)['keep_days'] == 14 and policy(log.db)['success_percent'] == 100
    finally:
        log.db.close()


@pytest.mark.parametrize('name,value', [
    ('observation_keep_days', 0), ('observation_keep_days', 31),
    ('observation_success_percent', -1), ('observation_success_percent', 101),
    ('observation_max_spans', -1), ('observation_max_spans', 100_000_001),
    ('observation_quiet_routes', ['/poll'] * 65), ('observation_quiet_routes', ['relative']),
])
def test_invalid_span_policy_is_refused_before_runtime_application(name, value):
    with pytest.raises(ValueError):
        validate('weightroom', name, value)
    initial = Settings(key='test')
    after, refused = apply_live('weightroom', initial, {name: value})
    assert name in refused and getattr(after, name) == getattr(initial, name)


def test_settings_post_publishes_the_policy_in_the_same_running_app(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    browser.get('/settings')
    response = browser.post('/settings/weightroom', data={
        'value-observation_keep_days': '8', 'value-observation_success_percent': '0',
        'value-observation_max_spans': '100', 'value-observation_quiet_routes': '["/poll"]',
    })
    assert response.status_code == 200 and 'Saved.' in response.text
    with browser.app.state.databases.database('weightroom.accounts').connection() as connection:
        assert policy(connection) == {'keep_days': 8, 'success_percent': 0, 'max_spans': 100, 'quiet_routes': ['/poll']}
