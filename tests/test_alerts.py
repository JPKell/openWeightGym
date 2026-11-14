"""Operational episodes, isolated evidence sources and the control node permissions. No real power/GPU work."""
import json
import sqlite3
import time

import pytest
from starlette.testclient import TestClient

from weightroom.alerts import Alerts, machine_conditions
from loadcoach.app import build_app
from loadcoach.config import Config
from loadcoach.log import Log
from loadcoach.models import Models
from loadcoach.outside import Services
from loadcoach.reliability import Reliability
from loadcoach.users import Users
from test_weightroom import GROUPS, desk, desk_with_machine, sign_in  # noqa: F401
from front_helpers import loadcoach_through


def condition(active=True, key='hot', **more):
    return dict(key=key, active=active, title='GPU hot', explanation='90°C', **more)


def test_episode_dedup_acknowledgement_unknown_recovery_and_restart(tmp_path):
    path = tmp_path / 'alerts.db'
    kept = Alerts(path)
    for now in (100, 110, 120):
        kept.observe('heat', [condition()], now)
    opened = kept.rows(open_only=True)
    assert len(opened) == 1 and opened[0]['first_at'] == 100 and opened[0]['last_at'] == 120
    identifier = opened[0]['id']
    assert kept.acknowledge(identifier, 'owner') == 1
    assert kept.acknowledge(identifier, 'another') == 0
    kept.observe('heat', None, 130)
    assert kept.rows(open_only=True)[0]['state'] == 'unknown'
    assert kept.rows() == []
    kept.db.close()
    kept = Alerts(path)
    kept.observe('heat', [condition()], 140)
    assert kept.rows(open_only=True)[0]['acknowledged_by'] == 'owner'
    kept.observe('heat', [condition(False, recovery='70°C; cool again')], 150)
    assert not kept.rows(open_only=True)
    assert kept.rows()[0]['recovery'] == '70°C; cool again'
    kept.observe('heat', [condition()], 160)
    assert kept.rows(open_only=True)[0]['id'] != identifier
    assert kept.rows(open_only=True)[0]['acknowledged_at'] is None
    kept.db.close()


def test_missing_gpu_and_partial_readings_do_not_recover(tmp_path):
    kept = Alerts(tmp_path / 'alerts.db')
    kept.observe('heat', [condition()])
    kept.observe('heat', [], missing_recovers=False)
    assert kept.rows(open_only=True)[0]['state'] == 'unknown'
    assert not kept.sources()[0]['available']
    kept.observe('heat', [condition(None)], missing_recovers=False)
    assert kept.rows(open_only=True)[0]['state'] == 'unknown'
    assert kept.review(kept.rows(open_only=True)[0]['id'], 'owner') == 0
    kept.db.close()


def test_memory_event_review_is_separate_from_ack_and_never_replayed(tmp_path):
    kept = Alerts(tmp_path / 'alerts.db')
    reading = condition(manual=True, key='oom:cursor', at=10)
    kept.observe('memory kills', [reading], 20)
    identifier = kept.rows(open_only=True)[0]['id']
    kept.acknowledge(identifier, 'owner')
    kept.observe('memory kills', [], 30)
    kept.observe('memory kills', None, 40)
    assert kept.rows(open_only=True)[0]['state'] == 'active'
    assert kept.review(identifier, 'owner') == 1
    for now in (50, 60):
        kept.observe('memory kills', [reading], now)
    assert not kept.rows(open_only=True)
    assert len(kept.rows()) == 1 and kept.rows()[0]['state'] == 'reviewed'
    assert 'not proof' in kept.rows()[0]['recovery']
    kept.db.close()


def test_machine_service_intent_and_unavailable_heat():
    services, heat = machine_conditions({'services': [
        {'name': 'stopped', 'unit': 'a', 'state': 'inactive', 'result': 'success'},
        {'name': 'failed', 'unit': 'b', 'state': 'failed', 'result': 'exit-code'},
        {'name': 'unknown', 'unit': 'c', 'state': 'inactive'},
        {'name': 'starting', 'unit': 'd', 'state': 'activating'},
        {'name': 'stopped after failure', 'unit': 'e', 'state': 'inactive', 'result': 'exit-code'},
    ], 'cards': [{'card': 0, 'temperature': 90}, {'card': 1, 'temperature': None}, {'card': 2, 'temperature': 70}]}, 85)
    assert [row['active'] for row in services] == [False, True, None, None, None]
    assert [row['active'] for row in heat] == [True, None, False]
    assert 'cleanly stopped' in services[0]['recovery']


def test_compute_evidence_reuses_exclusion_and_real_spending_without_leaking_text(tmp_path):
    path = tmp_path / 'core.db'
    users = Users(tmp_path / 'accounts.db', GROUPS)  # the accounts are the control node's: turnstile is in the control node, as on the two machines
    for name, group in [('jordan', 'admin'), ('pat', 'plain'), ('panel', 'front')]:
        users.add(name, group)
    keys = {name: {'authorization': 'Bearer ' + users.new_key(name)} for name in ('jordan', 'pat', 'panel')}
    weights = tmp_path / 'model.gguf'
    weights.write_bytes(b'fake model')
    library = Models(path)
    library.put('tiny', {'gpus': 1, 'flags': {'model': str(weights)}})
    stat = weights.stat()
    library.keep_hash(str(weights), 'sha256-fixture', stat.st_size, stat.st_mtime)
    key = (library.identity('tiny'), library.fingerprint(library.get('tiny'), 'tiny'), 'chat')
    reliability = Reliability(sqlite3.connect(tmp_path / 'roles.db'))  # kept on the control node, in turnstile's folder
    for number in range(1, 4):
        reliability.record(number, 'pat', 'tiny', key, 0, 'error')
    Services(tmp_path / 'accounts.db', str(tmp_path)).put('provider', {'url': 'https://example.invalid/v1', 'ceiling': 1, 'models': {}})  # the control node's
    users.db.execute('UPDATE groups SET settings=? WHERE name=?', (json.dumps({'can': ['load'], 'max_hosted_spend': 1}), 'plain'))
    users.db.commit()
    log = Log(path)
    log.add('request', 'done', 'pat', model='tiny', cost=2, service='provider', text={'prompt': 'private prompt'})
    caller = loadcoach_through(tmp_path, users, database=str(path), nvidia_smi='missing-test-program').__enter__()
    assert caller.get('/loadcoach/alert-evidence', headers=keys['pat']).status_code == 403
    response = caller.get('/loadcoach/alert-evidence', headers=keys['panel'])
    assert response.status_code == 200 and 'private prompt' not in response.text
    assert response.json()['model failures'][0]['active'] is True
    assert all(row['active'] for row in response.json()['spending']) and len(response.json()['spending']) == 2
    weights.unlink()
    assert caller.get('/loadcoach/alert-evidence', headers=keys['panel']).json()['model failures'][0]['active'] is None
    weights.write_bytes(b'fake model')
    stat = weights.stat()
    library.keep_hash(str(weights), 'sha256-fixture', stat.st_size, stat.st_mtime)
    reliability.reset(key)
    assert caller.get('/loadcoach/alert-evidence', headers=keys['jordan']).json()['model failures'][0]['active'] is False
    for db in (log.db, users.db):  # compute's copy of the entries and the control node's, which is the log spending is counted from
        db.execute('UPDATE events SET at=?', (time.time()-86401,))
        db.commit()
    assert not any(row['active'] for row in caller.get('/loadcoach/alert-evidence', headers=keys['panel']).json()['spending'])
    reliability.db.close()
    users.db.close()
    log.db.close()


def test_alert_page_permissions_ack_csrf_and_history(desk):
    browser, _, _ = desk
    sign_in(browser, 'pat')
    assert browser.get('/alerts').status_code == 403
    assert 'href="/alerts"' not in browser.get('/').text
    kept = browser.app.state.alerts
    kept.observe('GPU temperature', [condition(key='gpu:0')])
    identifier = kept.rows(open_only=True)[0]['id']
    assert browser.post('/alerts/' + identifier, data={'do': 'acknowledge'}).status_code == 403
    sign_in(browser, 'jordan')
    assert 'GPU hot' in browser.get('/alerts').text
    assert browser.post('/alerts/' + identifier, data={'do': 'acknowledge'}, headers={'origin': 'https://elsewhere.invalid'}).status_code == 403
    assert browser.post('/alerts/' + identifier, data={'do': 'acknowledge'}).status_code == 200
    assert kept.rows(open_only=True)[0]['acknowledged_by'] == 'jordan'
    assert browser.post('/alerts/' + identifier, data={'do': 'review'}).status_code == 409
    kept.observe('GPU temperature', [condition(False, key='gpu:0')])
    page = browser.get('/alerts?scope=history&source=GPU+temperature')
    assert 'recovered' in page.text and 'Acknowledged by jordan' in page.text
    assert browser.get('/alerts?page=bad').status_code == 400


def test_spotter_memory_evidence_heat_and_compute_outage(desk_with_machine, tmp_path):
    browser, compute, machine, users, _ = desk_with_machine
    # The fake systemctl emits an explicit result; every hardware command is a stand-in.
    binary = tmp_path / 'bin' / 'systemctl'
    binary.write_text('#!/bin/sh\nprintf "ActiveState=failed\\nResult=oom-kill\\nInvocationID=run-1\\n"\n')
    kernel = tmp_path / 'bin' / 'journalctl'
    kernel.write_text('#!/bin/sh\necho \'{"__CURSOR":"event-1","__REALTIME_TIMESTAMP":"10000000","MESSAGE":"private process arguments"}\'\n')
    sign_in(browser, 'jordan')
    page = browser.get('/alerts')
    assert 'GPU 1 temperature' in page.text and 'loadcoach service failed' in page.text
    assert 'Kernel memory kill' in page.text and 'private process arguments' not in page.text
    kept = browser.app.state.alerts
    count = len(kept.rows(open_only=True))
    browser.get('/alerts')
    assert len(kept.rows(open_only=True)) == count
    compute.off = True
    page = browser.get('/alerts')
    assert page.status_code == 200 and 'Unavailable; recovery unknown' in page.text
    compute.off = False
    machine.off = True
    page = browser.get('/alerts')
    heat = next(row for row in kept.rows(open_only=True) if row['condition'] == 'gpu:1')
    assert heat['state'] == 'unknown' and page.status_code == 200


def test_kernel_access_failure_is_unknown_not_healthy(desk_with_machine, tmp_path):
    browser, _, _, _, _ = desk_with_machine
    (tmp_path / 'bin' / 'journalctl').write_text('#!/bin/sh\nexit 1\n')
    sign_in(browser, 'jordan')
    browser.get('/alerts')
    assert not next(row for row in browser.app.state.alerts.sources() if row['source'] == 'kernel journal')['available']


@pytest.mark.parametrize('empty_status', [0, 1])
def test_readable_kernel_without_memory_kills_is_available(desk_with_machine, tmp_path, empty_status):
    browser, _, _, _, _ = desk_with_machine
    kernel = tmp_path / 'bin' / 'journalctl'
    kernel.write_text('''#!/bin/sh
[ "$1" = "_TRANSPORT=kernel" ] || exit 2
case "$*" in
  *" -g "*) exit ''' + str(empty_status) + ''' ;;
  *) echo '{"_TRANSPORT":"kernel","MESSAGE":"private unrelated boot message"}' ;;
esac
''')
    sign_in(browser, 'jordan')
    page = browser.get('/alerts')
    assert next(row for row in browser.app.state.alerts.sources() if row['source'] == 'kernel journal')['available']
    assert 'private unrelated boot message' not in page.text
    assert not any(row['source'] == 'memory kills' for row in browser.app.state.alerts.rows(open_only=True))


def test_empty_kernel_without_confirmed_access_stays_unavailable(desk_with_machine, tmp_path):
    browser, _, _, _, _ = desk_with_machine
    (tmp_path / 'bin' / 'journalctl').write_text('''#!/bin/sh
case "$*" in
  *" -g "*) exit 1 ;;
  *) exit 0 ;;
esac
''')
    sign_in(browser, 'jordan')
    browser.get('/alerts')
    assert not next(row for row in browser.app.state.alerts.sources() if row['source'] == 'kernel journal')['available']


def test_installer_and_migration_know_alert_store(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from test_installer import support
    from db.migration import host_inventory
    monkeypatch.setattr('db.migration.systemctl', lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=''))
    folder = tmp_path / 'settings'
    folder.mkdir()
    (folder / 'weightroom.toml').write_text('data="../data"\n')
    assert '"weightroom.alerts" = "alerts"' in support.database_block('weightroom', 'sqlite', folder/'weightroom.toml', {})
    assert any(row[0] == 'alerts' for row in support.configured_targets(folder/'weightroom.toml', 'weightroom'))
    assert any(row['binding'] == 'weightroom.alerts' for row in host_inventory(tmp_path)['references'])
    config = folder/'weightroom.toml'
    config.write_text('data="../data"\n[databases.stores.chat]\nengine="sqlite"\npath="../data/chat.db"\n[databases.bindings]\n"weightroom.chat"="chat"\n')
    assert any(row[0] == 'default_weightroom_alerts' and row[2] == tmp_path/'data'/'alerts.db'
               for row in support.configured_targets(config, 'weightroom'))


def test_alert_store_is_backed_up_and_exported(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    browser.app.state.alerts.observe('GPU temperature', [condition(key='gpu:0')])
    assert browser.post('/backups/create').status_code == 200
    assert 'weightroom.alerts' in browser.get('/backups').text
    exported = browser.get('/databases/front/alerts.db/dump?table=operational_alerts')
    assert exported.status_code == 200 and 'GPU hot' in exported.text


def test_alert_collection_obeys_maintenance_boundary(desk, tmp_path, monkeypatch):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    gate = next(m.kwargs['gate'] for m in browser.app.user_middleware if 'gate' in m.kwargs)
    marker = tmp_path / '.suite-maintenance.json'
    monkeypatch.setattr(gate, 'path', marker)
    sources = browser.app.state.alerts.sources()
    marker.write_text(json.dumps({'token': 'private-maintenance-test'}))
    assert browser.get('/alerts').status_code == 503
    # Even an internally accepted read must not collect or write across the gate.
    response = browser.get('/alerts', headers={'x-suite-maintenance': 'private-maintenance-test'})
    assert response.status_code == 200
    assert browser.app.state.alerts.sources() == sources
    marker.unlink()
    assert gate.active == 0


def test_alert_collection_marks_all_monitoring_sources_no_wake(desk_with_machine, monkeypatch):
    import httpx
    browser, compute, machine, _, _ = desk_with_machine
    sign_in(browser, 'jordan')
    compute.off = machine.off = True
    paths = {'/loadcoach/alert-evidence', '/spotter/alert-evidence', '/loadcoach/observation-metrics'}
    observed = set()
    original = httpx.AsyncClient.get

    async def inspect(client, path, **kwargs):
        if path in paths:
            observed.add(path)
            assert kwargs['headers']['x-turnstile-no-wake'] == '1'
        return await original(client, path, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, 'get', inspect)
    assert browser.get('/alerts').status_code == 200
    assert observed == paths
    assert not any(row['available'] for row in browser.app.state.alerts.sources() if row['source'] != 'observability')


def test_inflight_alert_snapshot_is_discarded_when_maintenance_starts(desk, tmp_path, monkeypatch):
    browser, wire, _ = desk
    sign_in(browser, 'jordan')
    gate = next(m.kwargs['gate'] for m in browser.app.user_middleware if 'gate' in m.kwargs)
    marker = tmp_path / '.suite-maintenance.json'
    monkeypatch.setattr(gate, 'path', marker)
    sources = browser.app.state.alerts.sources()
    original = wire.handle_async_request

    async def maintain_during_read(request):
        if request.url.path == '/loadcoach/alert-evidence':
            assert gate.active >= 2  # page request plus collector
            marker.write_text(json.dumps({'token': 'inflight-maintenance-test'}))
        return await original(request)

    monkeypatch.setattr(wire, 'handle_async_request', maintain_during_read)
    assert browser.get('/alerts').status_code == 200
    assert browser.app.state.alerts.sources() == sources
    assert gate.active == 0
    marker.unlink()


def test_heat_threshold_is_validated_and_taken_on_live(desk):
    browser, switch, users = desk
    sign_in(browser, 'jordan')
    # The Settings follower publishes WeightRoom's current fields at startup.
    key = browser.cookies['weightroom']
    compute = TestClient(switch.front, base_url='http://front:8442')  # LoadCoach, through turnstile
    headers = {'authorization': 'Bearer ' + key}
    for value in (False, 49, 111, 'hot'):
        assert compute.post('/loadcoach/settings/weightroom', headers=headers, json={'alert_gpu_temperature': value}).status_code == 400
    assert compute.post('/loadcoach/settings/weightroom', headers=headers, json={'alert_gpu_temperature': 90}).status_code == 200
    for _ in range(100):
        if 'GPU threshold: 90°C' in browser.get('/alerts').text:
            break
        time.sleep(.01)
    else:
        raise AssertionError('The new threshold was not taken on live')
