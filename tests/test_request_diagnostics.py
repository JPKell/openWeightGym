"""Diagnostic evidence through the real openWeightGym boundary, without models or GPUs."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from loadcoach.diagnostics import Timeline, distribution, presentation, related
from loadcoach.groups import Group
from loadcoach.log import Log
from test_postgresql import postgres  # noqa: F401
from test_outside import OUTWARD, service  # noqa: F401
from test_app import (GROUPS, _signed, add_user, fake_llama_server, gateway,  # noqa: F401
                      in_an_empty_folder, model, wait_for_status)


def test_stream_observation_handles_split_frames_and_does_not_count_headers_or_keep_text(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr('loadcoach.diagnostics.time.monotonic', lambda: clock[0])
    timing = Timeline({'messages': [], 'stream': True}, {'flags': {}}, False)
    clock[0] = 11
    timing.stage('loading')
    clock[0] = 13
    timing.stage('upstream')
    clock[0] = 14
    timing.stage('response')
    # HTTP bytes and role-only SSE events are not the first generated output.
    timing.chunk(b': heartbeat\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\n', 'text/event-stream', True)
    assert timing.first_output is None
    clock[0] = 15
    frame = ('data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': 'private reasoning 🐦'}}]}, ensure_ascii=False) + '\n\n').encode()
    for byte in frame:
        timing.chunk(bytes([byte]), 'text/event-stream', True)
    clock[0] = 16
    timing.chunk(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"files_read","arguments":"SECRET ARGUMENTS"}}]}}]}\n\n', 'text/event-stream', True)
    timing.chunk(b'data: {"timings":{"prompt_ms":250,"predicted_ms":500},"choices":[]}\n\ndata: [DONE]\n\n', 'text/event-stream', True)
    clock[0] = 17
    timing.finish()
    recorded = timing.snapshot()
    assert recorded['first_byte_seconds'] == 4 and recorded['first_output_seconds'] == 5
    assert recorded['total_seconds'] == 7
    assert [p['seconds'] for p in recorded['phases']] == [1, 2, 1, 1, 2]
    assert recorded['native'] == {'prompt_seconds': .25, 'generation_seconds': .5}
    assert recorded['tools'] == [{'tool': 'files_read', 'offset': 6}]
    assert not any(secret in json.dumps(recorded) for secret in ('private reasoning', 'SECRET ARGUMENTS'))


def test_native_measurements_are_structured_finite_and_never_inferred_from_json_first_byte():
    timing = Timeline({}, None, True)
    body = json.dumps({'choices': [{'message': {'content': '"prompt_ms": 123456'}}],
                       'timings': {'prompt_ms': float('nan'), 'predicted_ms': True}}).encode()
    timing.chunk(body, 'application/json', True)
    timing.finish(body, 'application/json')
    assert timing.snapshot()['native'] == {} and timing.first_output is None
    compressed = Timeline({}, None, True)
    compressed.chunk(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n', 'text/event-stream', False)
    assert compressed.first_output is None
    # An oversized line is ignored, the next complete frame is still observed.
    timing = Timeline({}, None, True)
    timing.chunk(b'data: ' + b'x' * (1024 * 1024 + 1), 'text/event-stream', True)
    assert len(timing.pending) == 0
    timing.chunk(b'\ndata: {"choices":[{"delta":{"content":"ok"}}]}\n', 'text/event-stream', True)
    assert timing.first_output is not None
    assert Timeline({'messages': None}, None, True).snapshot()['context']['images'] is False


def test_cold_and_warm_requests_keep_actual_configuration_and_native_timing(fake_llama_server):
    settings = model('vision.gguf', 1, mmproj='/private/model-projector.gguf', takes=.15)
    with gateway(fake_llama_server, {'vision': settings}, keep_text=0) as caller:
        owner = add_user(caller, 'pat', 'admin')
        body = {'model': 'vision', 'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,PRIVATE_IMAGE'}}]}],
                'reply': {'choices': [{'message': {'content': 'PRIVATE ANSWER'}}], 'timings': {'prompt_ms': 12, 'predicted_ms': 30}}}
        answers = [caller.post('/v1/chat/completions', headers=owner, json=body) for _ in range(2)]
        facts = [caller.get('/loadcoach/requests/' + a.headers['x-loadcoach-request-id'], headers=owner).json() for a in answers]
        raw = [f['ended']['diagnostics'] for f in facts]
        assert any(p['stage'] == 'loading' and p['seconds'] >= .1 for p in raw[0]['phases'])
        assert not any(p['stage'] == 'loading' for p in raw[1]['phases'])
        assert raw[0]['context']['projector'] == 'model-projector.gguf'
        assert raw[0]['context']['images'] and raw[0]['context']['cards'] == ['GPU0']
        assert raw[0]['native'] == {'prompt_seconds': .012, 'generation_seconds': .03}
        assert raw[0]['first_output_seconds'] is None  # nonstreaming, not an invented TTFT
        assert facts[0]['text'] is None
        assert not any(secret in json.dumps(facts[0]['diagnostics']) for secret in ('PRIVATE_IMAGE', 'PRIVATE ANSWER', '/private/'))
        assert caller.log.verify()[1] is None  # nested evidence is covered by front's existing chain


def test_metadata_permissions_live_queue_cancellation_and_no_log(fake_llama_server):
    groups = {**GROUPS, 'observer': Group(can=frozenset({'load', 'see-log'}))}
    with gateway(fake_llama_server, groups=groups, keep_text=5, unlogged=True) as caller, ThreadPoolExecutor() as others:
        admin = add_user(caller, 'jordan', 'admin')
        owner = add_user(caller, 'pat', 'plain', by=admin)
        stranger = add_user(caller, 'cam', 'plain', by=admin)
        observer = add_user(caller, 'observer', 'observer', by=admin)
        working = others.submit(caller.post, '/v1/chat/completions', headers=owner, json={'model': 'small', 'sleep': 10})
        entry = wait_for_status(_signed(caller, admin), lambda s: s['running'])['running'][0]['id']
        path = f'/loadcoach/requests/{entry}'
        assert caller.get(path, headers=stranger).status_code == 403
        metadata = caller.get(path, headers=observer).json()
        assert metadata['diagnostics']['available'] and metadata['text'] is None
        assert caller.get(f'/loadcoach/peek/{entry}', headers=observer).status_code == 403
        waiting = others.submit(caller.post, '/v1/chat/completions', headers=owner, json={'model': 'small'})
        queued = wait_for_status(_signed(caller, admin), lambda s: s['waiting'])['waiting'][0]['id']
        assert caller.get(f'/loadcoach/requests/{queued}', headers=owner).json()['diagnostics']['phases'][-1]['label'] == 'Queue / admission'
        caller.post(f'/loadcoach/stop/{queued}', headers=owner)
        waiting.result()
        caller.post(f'/loadcoach/stop/{entry}', headers=owner)
        working.result()
        over = caller.get(path, headers=owner).json()
        assert over['ended']['what'] == 'stopped' and over['diagnostics']['metrics'][0]['seconds'] > 0
        before = caller.log.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        quiet = caller.post('/v1/chat/completions', headers={**admin, 'x-loadcoach-log': 'off'}, json={'model': 'small'})
        assert quiet.status_code == 200 and 'x-loadcoach-request-id' not in quiet.headers
        caller.get('/loadcoach/events', headers=admin)  # flush compute's spool through the normal route
        assert caller.log.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == before


def test_streamed_output_and_load_failure_have_diagnostics_without_changing_responses(fake_llama_server):
    with gateway(fake_llama_server, {'small': model('small.gguf', 1), 'broken': model('broken.gguf', 1, fail=True)}) as caller:
        admin = add_user(caller, 'jordan', 'admin')
        response = caller.post('/v1/chat/completions', headers=admin,
                               json={'model': 'small', 'stream': True, 'messages': [{'role': 'user', 'content': 'hello'}]})
        assert 'data: [DONE]' in response.text
        facts = caller.get('/loadcoach/requests/' + response.headers['x-loadcoach-request-id'], headers=admin).json()
        assert facts['ended']['diagnostics']['first_output_seconds'] is not None
        assert facts['diagnostics']['phases'][-1]['label'] == 'Receiving generated output'
        failed = caller.post('/v1/chat/completions', headers=admin, json={'model': 'broken'})
        assert failed.status_code == 502
        caller.get('/loadcoach/events', headers=admin)
        entry = next(e for e in caller.log.read(kind='request', model='broken') if e['what'] == 'asked')
        facts = caller.get(f'/loadcoach/requests/{entry.get("source", entry["id"])}', headers=admin).json()
        assert facts['ended']['status'] == 502 and facts['diagnostics']['available']
        assert any(e['what'] == 'failed' for e in facts['related']['models'])


def test_role_selection_and_hosted_requests_keep_their_existing_route(fake_llama_server, service, tmp_path):
    keys = tmp_path / 'keys'
    keys.mkdir()
    (keys / 'rented.key').write_text('good-key')
    with gateway(fake_llama_server, groups=OUTWARD, keys=str(keys)) as caller:
        admin = add_user(caller, 'jordan', 'admin')
        caller.post('/loadcoach/roles/chat', headers=admin, json={'models': {'small': 1}})
        answer = caller.post('/v1/chat/completions', headers=admin, json={'model': 'chat:best'})
        facts = caller.get('/loadcoach/requests/' + answer.headers['x-loadcoach-request-id'] + '?summary=1', headers=admin).json()
        assert facts['asked']['role'] == facts['ended']['diagnostics']['role'] == 'chat:best'
        assert any(g['dimension'] == 'role' for g in facts['distribution']['groups'])
        caller.post('/loadcoach/services/rented', headers=admin, json={'url': service, 'models': {'outside': {'theirs': 'vendor/small'}}})
        answer = caller.post('/v1/chat/completions', headers=admin, json={'model': 'outside', 'stream': True})
        assert answer.status_code == 200 and '"content": "hi"' in answer.text
        facts = caller.get('/loadcoach/requests/' + answer.headers['x-loadcoach-request-id'], headers=admin).json()
        assert facts['diagnostics']['context']['source'] == 'hosted'
        assert all(p['stage'] not in ('queue', 'loading') for p in facts['ended']['diagnostics']['phases'])


def test_legacy_measurements_are_labelled_and_missing_values_stay_unavailable():
    asked = {'at': '2026-10-15T01:00:00'}
    ended = {'at': '2026-10-15T01:00:05', 'waited': 2, 'ran': 3}
    result = presentation(asked, ended)
    assert not result['available']
    assert result['metrics'][0]['seconds'] == 5 and result['metrics'][0]['basis'].startswith('Inferred')
    assert all(m['seconds'] is None for m in result['metrics'][1:])
    assert result['phases'][0]['basis'].startswith('Legacy')


def test_correlations_and_distributions_obey_owner_scope_and_do_not_return_text(tmp_path):
    log = Log(str(tmp_path / 'events.db'))
    for owner, seconds, status in [('pat', 1, 200), ('pat', 3, 503), ('cam', 900, 500)]:
        start = log.add('request', 'asked', owner, model='vision', trace='shared-trace', role='chat')
        log.add('request', 'done', owner, model='vision', trace='shared-trace', of=start, status=status, fingerprint='fp',
                diagnostics={'total_seconds': seconds, 'role': 'chat'})
    tool = log.add('tool', 'called', 'pat', trace='shared-trace', tool='files_read', text={'arguments': 'PRIVATE'})
    log.add('tool', 'returned', 'pat', trace='shared-trace', tool='files_read', of=tool, took=.25, text={'result': 'PRIVATE'})
    asked = next(e for e in log.read(kind='request', user='pat') if e['what'] == 'asked')
    ended = {'fingerprint': 'fp', 'at': asked['at']}
    own = distribution(log, asked, ended, 'pat')
    assert {g['dimension'] for g in own['groups']} == {'model', 'role', 'fingerprint'}
    assert all(g['requests'] == 2 and g['errors'] == 1 and g['p95'] == 3 for g in own['groups'])
    assert distribution(log, asked, ended, None)['groups'][0]['p95'] == 900
    correlated = related(log, asked, ended, False)
    assert len(correlated['trace']) == 6 and correlated['models'] == []
    assert correlated['trace'][-1]['took'] == .25
    assert 'PRIVATE' not in json.dumps(correlated)
    log.db.close()


def test_diagnostic_history_survives_front_reopen(tmp_path):
    path = str(tmp_path / 'events.db')
    log = Log(path)
    entry = log.add('request', 'asked', 'pat', model='small')
    log.add('request', 'done', 'pat', of=entry, status=200, diagnostics={'version': 1, 'total_seconds': 1.25})
    log.db.close()
    log = Log(path)
    records = log.about(entry, request=True)
    assert presentation(records[0], records[1])['metrics'][0]['seconds'] == 1.25
    assert log.verify()[1] is None
    log.db.close()


def test_postgresql_diagnostics_use_the_same_existing_event_store(postgres):
    log = Log(postgres)
    entry = log.add('request', 'asked', 'pat', model='small', trace='pg-trace', role='chat')
    log.add('request', 'done', 'pat', model='small', trace='pg-trace', of=entry, status=200, fingerprint='fp',
            diagnostics={'version': 1, 'total_seconds': 1.25, 'role': 'chat'})
    records = log.about(entry, request=True)
    assert distribution(log, records[0], records[1], 'pat')['groups'][0]['p95'] == 1.25
    assert len(related(log, records[0], records[1], False)['events']) == 2
    assert log.verify()[1] is None
    log.db.close()
