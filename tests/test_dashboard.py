"""Retained usage periods, owner-only history, and account navigation."""

from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from loadcoach.log import Log
from loadcoach.usage import report
from test_usage import recorded
from test_weightroom import desk, sign_in  # noqa: F401


def test_personal_periods_include_all_retained_history_without_other_users(monkeypatch):
    log = Log(':memory:')
    for day, user, tokens in [('2010-01-01', 'owner', 1), ('2009-01-01', 'other', 999),
                              ('2026-09-13', 'owner', 10), ('2026-10-06', 'owner', 100),
                              ('2026-10-12', 'owner', 1000)]:
        recorded(log, monkeypatch, day + 'T12:00:00+00:00', user, prompt_tokens=tokens, answer_tokens=0)
    for period, tokens in [('1', 1000), ('7', 1100), ('30', 1110), ('all', 1111)]:
        result = report(log.db, {'period': period, 'end': '2026-10-12',
                                 'bucket': 'hour' if period == '1' else 'day'}, owner='owner')
        assert result['totals']['total_tokens'] == tokens
        assert result['choices']['user'] == ['owner']
        if period == '1':
            assert result['bucket'] == 'hour' and len(result['hourly']) == 24
            assert result['hourly'][12]['label'] == '12:00' and result['hourly'][12]['total_tokens'] == 1000
        else:
            assert result['bucket'] == 'day' and result['hourly'] == []
        if period == 'all':
            assert result['start'] == '2010-01-01' and len(result['daily']) > 3660
    log.db.close()


def test_account_logs_remain_personal_for_admins_and_paginate(desk, tmp_path, monkeypatch):
    browser, switch, users = desk
    log = Log(str(tmp_path / 'loadcoach.db'))
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    for offset, tokens, name in [(0, 100, 'small'), (6, 200, 'bigger'), (29, 400, 'older'), (40, 800, 'oldest')]:
        at = (today - timedelta(days=offset)).isoformat()
        start = recorded(log, monkeypatch, at, 'jordan', model=name, what='asked')
        recorded(log, monkeypatch, at, 'jordan', model=name, of=start, prompt_tokens=tokens, answer_tokens=1)
    for index in range(14):
        log.add('request', 'asked', 'jordan', model='pending')
    secret_start = log.add('request', 'asked', 'pat', model='private-model')
    log.add('request', 'done', 'pat', model='private-model', of=secret_start, prompt_tokens=99999, answer_tokens=99999)
    with TestClient(switch.front, base_url="http://front:8442") as api:
        headers = {'authorization': 'Bearer ' + users.new_key('jordan')}
        own = api.get('/loadcoach/usage?mine=1&period=all', headers=headers).json()
        assert not own['everyone'] and own['totals']['input_tokens'] == 1500
        assert api.get('/loadcoach/usage?mine=1&user=pat', headers=headers).status_code == 403
        requests = api.get('/loadcoach/requests?mine=1&user=pat', headers=headers).json()
        assert requests['total'] == 0  # An exact user filter cannot broaden the personal scope.
        requests = api.get('/loadcoach/requests?mine=1', headers=headers).json()
        assert requests['total'] == 18 and all(row['user'] == 'jordan' for row in requests['requests'])
        events = api.get('/loadcoach/account-events?user=pat&limit=10&page=2', headers=headers).json()
        assert len(events['events']) == 10 and all(row['user'] == 'jordan' for row in events['events'])
        assert api.get('/loadcoach/account-events?limit=no', headers=headers).status_code == 400
    sign_in(browser, 'jordan')
    for path in ['/account', '/account/appearance', '/account/logs?limit=10']:
        html = browser.get(path).text
        categories = html.split('<div class="navbar-nav category-links d-none d-md-flex">')[1].split('</div>')[0]
        assert 'nav-link active' not in categories
        assert 'class="btn btn-primary btn-sm" aria-current="page"' in html
        assert all('href="' + link + '"' in html for link in ['/account', '/account/appearance', '/account/logs'])
    account = browser.get('/account').text
    assert '<details class="card' not in account and 'API keys' in account and 'Password' in account
    assert 'What I am shown' not in account and 'Asked for' not in account
    appearance = browser.get('/account/appearance').text
    assert appearance.index('What I am shown') < appearance.index('Themes')
    assert 'data-auto-submit' in appearance and 'data-theme-form' in appearance
    assert '<details class="card' not in appearance
    logs = browser.get('/account/logs?limit=10').text
    assert 'Asked for' in logs and 'id="latest"' in logs and 'Page 1 of' in logs
    assert 'private-model' not in logs
    older = browser.get('/account/logs?limit=10&page=2&part=list').text
    assert 'Page 2 of' in older and 'private-model' not in older
    log.db.close()
