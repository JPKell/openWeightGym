"""Console navigation, authorized listings and interactive page contracts."""

import json
from datetime import datetime, timezone

import httpx
from types import SimpleNamespace
from starlette.testclient import TestClient

from loadcoach.log import Log
from loadcoach.batches import Batches
from loadcoach.app import build_app as build_loadcoach
from loadcoach.config import Config
from loadcoach.settings import said
from weightroom.navigation import navigation
from weightroom.app import Settings, Person, build_app, current_batch_jobs
from benchmarks.store import Benchmarks
from test_weightroom import GROUPS, Switch, desk, desk_with_machine, sign_in  # noqa: F401
from front_helpers import door, loadcoach_through, machine_key, turnstile_to
from loadcoach.statement import PROOF


def test_navigation_orders_and_detail_parents():
    can = ['manage-models', 'manage-settings', 'manage-users', 'manage-groups', 'front-manage-plugins', 'see-log', 'restart-services']
    nav = navigation('/benchmarks/runs', can, True)
    assert [name for name, _ in nav['links']] == ['Runs', 'Results', 'Node measurements', 'Embedders', 'Compare', 'Manage']
    assert navigation('/benchmarks/embedders', can, True)['active'] == '/benchmarks/embedders'
    nav = navigation('/plugins/activity', can, True)
    assert nav['section'] == 'Logs' and nav['active'] == '/plugins/activity'
    assert [name for name, _ in nav['links']] == ['Usage', 'Requests', 'Batches', 'AI Logs', 'Reliability', 'Plugin logs', 'System logs']
    for path in ('/no-such-page', '/settings', '/models', '/users', '/deploy'):
        missing = navigation(path, [], False)
        assert missing['section'] == '' and missing['links'] == [] and missing['active'] == ''
    assert navigation('/reliability', [], False)['active'] == '/reliability'
    assert navigation('/reliability', [], False)['section'] == 'Logs'
    nav = navigation('/tools', can, True)
    assert nav['section'] == 'Settings'
    assert [name for name, _ in nav['links']] == ['Compute nodes', 'Users', 'Groups', 'Tools', 'Plugins', 'Voice', 'Audio', 'Image', 'General']
    assert navigation('/requests/42', can, True)['active'] == '/requests'
    assert navigation('/output', can, True)['section'] == 'Logs'
    assert navigation('/', [], False)['section'] == 'System'
    assert navigation('/status', [], False)['links'] == [('Status', '/status'), ('Telemetry', '/telemetry')]
    docs = navigation('/docs/voice', can, True)
    assert docs['section'] == 'Docs' and docs['active'] == '/docs/voice'
    assert docs['links'][0] == ('User guide', '/guide')
    assert ('System reference', '/docs') in docs['links']
    assert next(group for group in navigation('/status', [], False)['groups'] if group['name'] == 'Docs')['links'] == [('User guide', '/guide')]
    account = navigation('/account/appearance', can, True)
    assert account['section'] == 'My account' and account['active'] == '/account/appearance'
    assert account['links'] == [('Account', '/account'), ('Appearance', '/account/appearance'), ('Audio', '/account/audio'), ('Logs', '/account/logs')]


def test_system_logs_keep_exact_filter_snapshots_during_outages(desk_with_machine, tmp_path):
    server, compute, machine, users, _ = desk_with_machine
    sign_in(server, 'jordan')
    url = '/output?source=loadcoach&lines=500'
    live = server.get(url)
    assert live.status_code == 200 and 'loadcoach &lt;b&gt;started&lt;/b&gt;' in live.text
    assert 'id="journal-offline"' not in live.text
    compute.off = machine.off = True
    cached = server.get(url)
    assert cached.status_code == 200 and 'Showing logs last fetched' in cached.text
    assert 'loadcoach &lt;b&gt;started&lt;/b&gt;' in cached.text
    assert 'id="source"' in cached.text and 'value="500" selected' in cached.text
    assert 'id="asking"' not in cached.text  # Cannot ask an offline model to explain old lines.
    for path in ('/output?source=kernel&lines=500', '/output?source=loadcoach&lines=100'):
        empty = server.get(path)
        assert empty.status_code == 200 and 'No logs have been fetched for these filters yet' in empty.text
        assert 'loadcoach &lt;b&gt;started&lt;/b&gt;' not in empty.text
    compute.off = machine.off = False
    (tmp_path / 'bin/journalctl').write_text('#!/bin/sh\necho "2026-10-08T10:01:00 loadcoach restored"\n')
    fresh = server.get(url)
    assert fresh.status_code == 200 and 'loadcoach restored' in fresh.text
    assert 'id="journal-offline"' not in fresh.text
    compute.off = machine.off = True
    updated = server.get(url)
    assert 'loadcoach restored' in updated.text and 'loadcoach &lt;b&gt;started&lt;/b&gt;' not in updated.text
    users.add('operator', 'admin')
    users.change('operator', password='correct horse')
    sign_in(server, 'operator')
    other = server.get(url)
    assert 'No logs have been fetched for these filters yet' in other.text and 'loadcoach restored' not in other.text
    sign_in(server, 'jordan')
    users.change('jordan', group='plain')
    denied = server.get(url)
    assert denied.status_code == 403 and 'loadcoach &lt;b&gt;started&lt;/b&gt;' not in denied.text


def test_system_logs_remain_live_when_only_the_model_service_is_off(desk_with_machine):
    server, compute, _, _, _ = desk_with_machine
    sign_in(server, 'jordan')
    compute.off = True
    logs = server.get('/output')
    assert logs.status_code == 200 and 'loadcoach &lt;b&gt;started&lt;/b&gt;' in logs.text
    assert 'id="journal-offline"' not in logs.text


def test_admin_docs_map_and_component_outlines(desk):
    browser, _, _ = desk
    sign_in(browser, 'pat')
    assert browser.get('/docs').status_code == 403
    assert browser.get('/docs/voice').status_code == 403
    sign_in(browser, 'jordan')
    overview = browser.get('/docs')
    assert overview.status_code == 200
    assert '<title id="map-title">openWeightGym component map</title>' in overview.text
    for component in ('loadcoach', 'toollocker', 'weightroom', 'turnstile', 'voice', 'spotter',
                      'hosted-providers', 'plugins', 'logs', 'models', 'benchmarks', 'data',
                      'accounts', 'collections', 'operations', 'mirrorwall'):
        assert f'href="/docs/{component}"' in overview.text
        page = browser.get('/docs/' + component)
        assert page.status_code == 200 and '<h2 ' in page.text
    voice = browser.get('/docs/voice').text
    assert '<h1 class="page-title wrap">' in voice and 'aria-label="Source modules"' in voice
    assert 'gateway.py' in voice and 'desktop.py' in voice
    assert browser.get('/docs/not-a-topic').status_code == 404
    images = browser.get('/docs/mirrorwall').text
    assert '<title id="flow-title">Image generation workflow</title>' in images and 'id="models"' in images
    assert all(name in images for name in ('Flux 2 klein 9B', 'Flux 2 Dev', 'SDXL checkpoint', 'Qwen3 8B', 'Mistral Small 3.2 24B'))
    assert 'id="flow-title"' not in browser.get('/docs/voice').text  # only the topic with its own guide shows one


def test_current_batch_jobs_includes_one_running_benchmark_without_duplicate_probe():
    store = Benchmarks(':memory:')
    running = store.create('owner', 'small', 'load')
    queued = store.create('owner', 'small', 'load')
    store.state(running, 'running')
    worker = SimpleNamespace(current=running, task=SimpleNamespace(done=lambda: False, cancelling=lambda: 0))
    now = {'active_batches': [{'id': running, 'model': 'small', 'user': 'owner', 'request_ids': []}]}
    owner = Person('owner', [])
    jobs = current_batch_jobs(now, owner, worker, store)
    assert len(jobs) == 1 and jobs[0]['id'] == running
    assert jobs[0]['href'] == '/benchmarks/run/' + running
    assert queued not in [job['id'] for job in jobs]
    assert current_batch_jobs({}, Person('other', []), worker, store) == []
    assert current_batch_jobs({}, Person('operator', ['see-all']), worker, store)[0]['id'] == running
    store.paused(True)
    assert current_batch_jobs({}, owner, worker, store) == []
    store.paused(False)
    store.finish(running, 'completed')
    assert current_batch_jobs({}, owner, worker, store) == []
    store.db.close()


def test_current_requests_shows_running_batch_once_and_omits_waiting_batches(desk, monkeypatch):
    browser, switch, _ = desk
    original = switch.handle_async_request
    executing, queued = 'batch_executing', 'batch_queued'
    def request(number, batch=None):
        return {'id': number, 'batch_id': batch, 'model': 'small', 'user': 'jordan',
                'caller': '127.0.0.1', 'priority': 'low' if batch else 'normal', 'seconds': 4}
    active = {'id': executing, 'model': 'small', 'user': 'jordan', 'caller': 'batch',
              'seconds': 4, 'request_ids': [1], 'href': '/batches', 'description': 'running batch'}
    async def status_only(req):
        response = await original(req)
        if req.url.path == '/loadcoach/status':
            await response.aread()
            facts = response.json()
            facts.update(running=[request(1, executing), request(3)], waiting=[request(2, queued)], active_batches=[active])
            # Still compute's answer, as turnstile checks, and still a stream, as turnstile passes it on.
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(facts).encode()), headers={PROOF: response.headers[PROOF], 'content-type': 'application/json'})
        return response
    monkeypatch.setattr(switch, 'handle_async_request', status_only)
    sign_in(browser, 'jordan')
    for path in ['/status', '/status?part=status', '/status?part=requests']:
        page = browser.get(path).text
        assert 'Current requests (2)' in page
        assert page.count('>' + executing + '</a>') == 1 and queued not in page
        assert '/requests/1"' not in page and '/requests/2"' not in page
        assert '/requests/3"' in page


def test_batches_stop_all_cancels_beyond_current_page_with_owner_scope_and_keeps_answers(desk, tmp_path):
    browser, _, _ = desk
    batches = Batches(str(tmp_path / 'loadcoach.db'))
    lines = [{'custom_id': 'one', 'body': {'model': 'small'}},
             {'custom_id': 'two', 'body': {'model': 'small'}}]
    own = [batches.start('pat', '/v1/chat/completions', 'input', lines)['id'] for _ in range(12)]
    other = batches.start('jordan', '/v1/chat/completions', 'input', lines)['id']
    finished = batches.start('pat', '/v1/chat/completions', 'input', lines)['id']
    batches.keep(own[0], 0, 200, '123', {'answer': 'kept'})
    batches.keep(finished, 0, 200, '124', {'answer': 'finished'})
    batches.finish(finished)
    original_output = batches.get(finished)['output_file_id']
    try:
        sign_in(browser, 'pat')
        page = browser.get('/batches?limit=10').text
        assert 'value="cancel-all" data-confirm-two>Stop all</button>' in page and 'Page 1 of 2' in page
        result = browser.post('/batches?limit=10', data={'do': 'cancel-all'})
        assert result.status_code == 200 and 'Stopped 12 batches.' in result.text
        assert all(batches.get(number)['status'] == 'cancelled' for number in own)
        assert batches.get(other)['status'] == 'in_progress'
        kept = batches.get(own[0])
        assert kept['request_counts']['completed'] == 1
        assert b'kept' in batches.content(kept['output_file_id'])
        assert batches.get(finished)['status'] == 'completed'
        assert batches.get(finished)['output_file_id'] == original_output
        assert 'Stopped 0 batches.' in browser.post('/batches', data={'do': 'cancel-all'}).text
        another = batches.start('pat', '/v1/chat/completions', 'input', lines)['id']
        sign_in(browser, 'jordan')
        result = browser.post('/batches', data={'do': 'cancel-all'})
        assert 'Stopped 2 batches.' in result.text
        assert batches.get(other)['status'] == batches.get(another)['status'] == 'cancelled'
    finally:
        batches.db.close()


def test_large_request_history_does_not_scan_every_ending_for_each_request():
    log = Log(':memory:')
    for index in range(2000):
        owner = 'pat' if index % 2 else 'jordan'
        number = log.add('request', 'asked', owner, '10.0.0.1', 'model')
        log.add('request', 'done', owner, '10.0.0.1', 'model', of=number)
        log.add('request', 'peeked', 'jordan', '10.0.0.2', 'model', of=number)
    # A work budget is deterministic across machines. The old correlated/scan
    # plans exceed this by orders of magnitude even on this modest history.
    steps = 0
    def budget():
        nonlocal steps
        steps += 1000
        return steps > 3_000_000
    log.db.set_progress_handler(budget, 1000)
    try:
        result = log.requests(None, 25, completed=True)
        assert result['total'] == 2000 and len(result['requests']) == 25
        assert all(row['outcome'] == 'done' for row in result['requests'])
        steps = 0
        own = log.requests('pat', 25, offset=25, completed=True)
        assert own['total'] == 1000 and own['page'] == 2
        assert all(row['user'] == 'pat' for row in own['requests'])
    finally:
        log.db.set_progress_handler(None, 0)
        log.db.close()


def test_tools_configuration_and_access(desk):
    browser, switch, _ = desk
    config = {'call_timeout': 30, 'servers': {'files': {'command': ['python', '-m', 'toollocker.tools.files']}},
              'toolsets': {'reading': {'tools': ['files__*']}}}
    store = switch.front.state.front_settings.changes
    store.seen('toollocker', said(config, {}, {}))
    with store.db:
        store.db.execute('CREATE TABLE IF NOT EXISTS tool_schemas (tool TEXT PRIMARY KEY, schema TEXT NOT NULL)')
        store.db.execute('INSERT INTO tool_schemas VALUES (?, ?)', ('files__read', json.dumps({'name': 'read', 'description': 'Read a file'})))
    sign_in(browser, 'jordan')
    page = browser.get('/tools')
    assert page.status_code == 200 and 'files__read' in page.text and '/mcp/reading' in page.text
    assert 'Try a tool' in page.text and 'Tool service settings' in page.text
    assert 'name="key"' not in page.text
    assert browser.post('/tools/message', json={'message': {'method': 'unknown'}}).status_code == 400
    sign_in(browser, 'pat')
    assert browser.get('/tools').status_code == 403
    assert browser.post('/tools/message', json={'message': {'method': 'tools/list'}}).status_code == 403


def test_tool_messages_use_the_session_identity(desk, tmp_path):
    _, _, users = desk
    switch = Switch(build_loadcoach(Config(database=str(tmp_path / 'loadcoach.db'), nvidia_smi='no-such-program', machine_key_file=machine_key(tmp_path))))
    forwarded = []
    def tool_service(request):
        forwarded.append(request)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {'tools': []}}, headers={'mcp-session-id': 'session-1'})
    settings = Settings(compute='http://compute', key=users.new_key('panel'), data=str(tmp_path / 'other'))
    with TestClient(build_app(settings, door(turnstile_to(tmp_path, users, {8442: switch}), 8442),
                             tools_client=httpx.AsyncClient(transport=httpx.MockTransport(tool_service), base_url='http://tools'))) as browser:
        sign_in(browser, 'jordan')
        session_key = browser.cookies['weightroom']
        response = browser.post('/tools/message', json={'message': {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                                       'toolset': 'reading', 'mcp-session-id': 'session-1'})
        assert response.status_code == 200 and response.headers['mcp-session-id'] == 'session-1'
        assert forwarded[0].url.path == '/mcp/reading'
        assert forwarded[0].headers['authorization'] == 'Bearer ' + session_key
        assert forwarded[0].headers['mcp-session-id'] == 'session-1'
        assert session_key not in response.text and session_key not in browser.get('/tools').text
        for toolset in ['../private', '/reading', 'reading?key=secret']:
            assert browser.post('/tools/message', json={'message': {'method': 'tools/list'}, 'toolset': toolset}).status_code == 400
        assert len(forwarded) == 1


def test_request_pages_and_group_time_log_filters(desk, tmp_path, monkeypatch):
    browser, _, users = desk
    log = Log(str(tmp_path / 'loadcoach.db'))
    def event(at, what, name, **details):
        with monkeypatch.context() as clock:
            clock.setattr('loadcoach.log.time.time', lambda: datetime.fromisoformat(at).replace(tzinfo=timezone.utc).timestamp())
            return log.add('request', what, name, '10.0.0.2', 'test-model', **details)
    first = event('2026-10-01T10:00:00', 'asked', 'pat')
    event('2026-10-01T10:00:05', 'done', 'pat', of=first, prompt_tokens=100, answer_tokens=20, group='plain')
    second = event('2026-10-01T11:00:00', 'asked', 'jordan')
    event('2026-10-01T11:00:02', 'stopped', 'jordan', of=second)
    third = event('2026-10-01T12:00:00', 'asked', 'pat')
    api = loadcoach_through(tmp_path / 'data', users, database=str(tmp_path / 'loadcoach.db')).__enter__()  # front's folder is the page's: one log there
    pat = {'authorization': 'Bearer ' + users.new_key('pat')}
    admin = {'authorization': 'Bearer ' + users.new_key('jordan')}
    own = api.get('/loadcoach/requests?limit=1', headers=pat).json()
    assert own['total'] == 2 and own['requests'][0]['id'] == third
    older = api.get('/loadcoach/requests?limit=1&page=2', headers=pat).json()
    assert older['requests'][0]['id'] == first and older['requests'][0]['duration'] == 5
    completed = api.get('/loadcoach/requests?completed=1', headers=pat).json()
    assert completed['total'] == 1 and completed['requests'][0]['outcome'] == 'done'
    assert api.get('/loadcoach/requests?limit=bad', headers=pat).status_code == 400
    events = api.get('/loadcoach/events?group=plain&start=2026-10-01T10:00&end=2026-10-01T10:01', headers=admin).json()['events']
    assert {entry['id'] for entry in events} == {first, first + 1}
    assert not api.get('/loadcoach/events?group=admin', headers=pat).json()['events']
    assert api.get('/loadcoach/events?start=bad', headers=admin).status_code == 400
    assert api.get('/loadcoach/events?start=2026-10-02&end=2026-10-01', headers=admin).status_code == 400
    assert api.get('/loadcoach/events?timezone=bogus', headers=admin).status_code == 400
    choices = api.get('/loadcoach/event-choices', headers=pat).json()
    assert list(choices['user_groups']) == ['pat'] and choices['choices']['user'] == ['pat']
    sign_in(browser, 'jordan')
    home = browser.get('/status').text
    assert 'Hello,' not in home and 'Break the lock' not in home and 'type="checkbox"' not in home.split('<main', 1)[1].split('</main>', 1)[0]
    assert home.index('status-controls') < home.index('class="row g-3 mb-4"') < home.index('status-lock-column') < home.index('id="current-requests"') < home.index('Past requests')
    controls_tag = home[home.index('<div id="status-controls"'):].split('>', 1)[0]
    assert 'hx-preserve' in controls_tag
    assert 'Time requested' in home and 'Current requests (0)' in home
    assert 'stopped' in browser.get('/requests?limit=1&page=2').text
    assert 'Stop this request' not in browser.get(f'/requests/{first}').text
    usage = browser.get('/usage?start=2026-10-01&end=2026-10-01&group=plain').text
    assert usage.index('id="usage-filters"') < usage.index('data-usage-total') < usage.index('Mock input rate / M tokens')
    assert 'fallback_input_tokens' in usage and 'Date range' in usage and 'data-users-groups' in usage
