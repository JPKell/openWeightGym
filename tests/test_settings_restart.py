"""Pending changes follow actual process starts and authorized, bounded restart actions."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from loadcoach.settings import Changes, checked_start, follow
from test_weightroom import desk, sign_in


def setting(changes, program='turnstile', name='max_in_flight'):
    return next(row for group in changes.everything() if group['program'] == program
                for row in group['settings'] if row['name'] == name)


def test_restart_pending_survives_reports_and_resets_and_clears_only_on_start(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr('loadcoach.settings.time.time', lambda: now[0])
    changes = Changes(tmp_path/'settings.db')
    report = {'max_in_flight': {'value': 5, 'file': 5}, 'wait': {'value': 180, 'file': 180}}
    changes.seen('turnstile', report, started=100)
    now[0] = 110
    changes.put('turnstile', 'max_in_flight', 8, 'operator')
    changes.put('turnstile', 'wait', 181, 'operator')
    assert setting(changes)['pending_since']
    assert not setting(changes, name='wait')['pending_since']
    now[0] = 120
    changes.seen('turnstile', report, started=100)
    assert setting(changes)['pending_since']
    # A legacy follower reporting after an authority restart cannot erase the known start.
    changes.seen('turnstile', report, started=0)
    assert changes.everything()[0]['started'] == 100
    changes.seen('turnstile', {'max_in_flight': {'value': 8, 'file': 5}}, started=120)
    assert not setting(changes)['pending_since']
    now[0] = 130
    assert changes.take_away('turnstile', 'max_in_flight')
    assert setting(changes)['pending_since']
    first = setting(changes)['pending_since']
    now[0] = 140
    assert not changes.take_away('turnstile', 'max_in_flight')
    assert setting(changes)['pending_since'] == first
    changes.seen('turnstile', report, started=140)
    assert not setting(changes)['pending_since']
    # Returning to the running value before a restart needs no restart.
    now[0] = 150
    changes.put('turnstile', 'max_in_flight', 5, 'operator')
    assert not setting(changes)['pending_since']
    changes.db.close()


@pytest.mark.parametrize('value', [-1, float('inf'), float('nan'), True, '100', None])
def test_process_start_rejects_invalid_timestamps(value):
    with pytest.raises(ValueError, match='timestamp'):
        checked_start(value)


def test_follow_reports_a_stable_process_start(tmp_path):
    reports = []
    count = [0]

    async def ask(method, path, body):
        if method == 'GET':
            count[0] += 1
            if count[0] == 3:
                raise asyncio.CancelledError
            return {'value': count[0]}
        reports.append(body)
        return {}

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await follow('voice', ask, tmp_path/'copy.json', lambda changed: ({'n': changed}, {}), every=0)
    asyncio.run(run())
    assert len(reports) == 2 and reports[0]['started'] == reports[1]['started'] > 0


def test_pending_notice_and_control_restart_use_the_same_components_action(desk, monkeypatch):
    browser, switch, users = desk
    changes = switch.front.state.console_settings.changes
    changes.seen('turnstile', {'max_in_flight': {'value': 100, 'file': 100}}, started=1)
    changes.put('turnstile', 'max_in_flight', 101, 'operator')
    sign_in(browser, 'jordan')
    response = browser.get('/settings/requests')
    assert 'Waiting for restart since' in response.text
    assert 'action="/machine/services/turnstile/restart"' in response.text
    assert 'data-confirm-two' in response.text
    calls = []
    monkeypatch.setattr('weightroom.pairing.restart_program_later', lambda program: calls.append(program) or True)
    saved = browser.post('/machine/services/turnstile/restart', data={'control_node':'1', 'return_to':'/settings/requests'})
    assert saved.status_code == 200 and 'Asked turnstile to restart.' in saved.text
    assert calls == ['turnstile']
    browser.post('/machine/services/ssh/restart', data={'control_node':'1'})
    browser.post('/machine/services/turnstile/stop', data={'control_node':'1'})
    assert calls == ['turnstile']
    sign_in(browser, 'pat')
    assert browser.post('/machine/services/turnstile/restart', data={'control_node':'1'}).status_code == 403
    assert calls == ['turnstile']


def test_control_restart_schedules_only_the_normal_owg_route(tmp_path, monkeypatch):
    from weightroom import pairing
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    command = tmp_path/'.local/bin/owg'
    command.parent.mkdir(parents=True)
    command.write_text('#!/bin/sh\n')
    calls = []
    monkeypatch.setattr(pairing.subprocess, 'run', lambda args, **kw: calls.append((args, kw)))
    assert pairing.restart_program_later('weightroom')
    args, options = calls[0]
    assert args[-5:] == [str(command), '--plain', '--yes', 'restart', 'weightroom']
    assert args[:2] == ['systemd-run', '--user'] and options['check']
    assert not pairing.restart_program_later('weightroom; touch /tmp/unsafe')
    assert len(calls) == 1


@pytest.mark.parametrize('width', [1440, 390])
def test_pending_notice_fits_and_survives_reload_and_action_errors(desk, monkeypatch, width):
    from test_console_browser import browser_context, playwright
    server, switch, _ = desk
    changes = switch.front.state.console_settings.changes
    changes.seen('turnstile', {'max_in_flight': {'value':100, 'file':100}}, started=1)
    changes.put('turnstile', 'max_in_flight', 101, 'operator')
    monkeypatch.setattr('weightroom.pairing.restart_program_later', lambda _: False)
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width, 'height':1000})
        page.goto('http://console.test/settings/requests')
        notice = page.get_by_text('Waiting for restart since', exact=False)
        playwright.expect(notice.first).to_be_visible()
        button = page.locator('form[action="/machine/services/turnstile/restart"] button')
        button.click()
        button.click()
        playwright.expect(page.get_by_text('OWG could not schedule the restart.', exact=False)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.reload()
        playwright.expect(notice.first).to_be_visible()
        assert page.locator('[data-setting-pending="true"]').count() == 1
        browser.close()
