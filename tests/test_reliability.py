"""Reliability state, actual proxy outcomes, feedback authority and operator recovery."""

import json
import sqlite3
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx

import pytest

from loadcoach.reliability import Reliability, answer_outcome
from test_app import GROUPS, _signed, add_user, fake_llama_server, gateway, in_an_empty_folder, model, wait_for_status  # noqa: F401
from test_role_evidence import catalogue


def store():
    return Reliability(sqlite3.connect(':memory:'))


@pytest.mark.parametrize('outcome', ['cancelled', 'benchmark', 'restart', 'refused', 'unscored'])
def test_non_model_outcomes_neither_penalize_nor_break_failure_streak(outcome):
    kept = store(); key = ('id', 'fp', 'chat')
    for number, result in enumerate(['error', outcome, 'empty', 'timeout'], 1):
        kept.record(number, 'pat', 'small', key, 0, result, now=1000)
    summary = kept.summary(key, now=1001)
    assert summary['samples'] == 3 and summary['factor'] == .5
    assert summary['excluded'] and summary['streak'] == 3
    assert summary['ignored'] == {outcome: 1}
    assert kept.summary(('id', 'another-fp', 'chat'), now=1001)['factor'] == 1
    assert not kept.summary(('other-id', 'fp', 'chat'), now=1001)['excluded']
    assert not kept.summary(('id', 'fp', 'code'), now=1001)['excluded']


def test_cooldown_single_recovery_reset_epoch_expiry_regression_and_bounds():
    kept = store(); key = ('id', 'fp', 'chat')
    for number in range(1, 4):
        kept.record(number, 'pat', 'small', key, 0, 'error', now=1000)
    with pytest.raises(ValueError):
        kept.begin_recovery(key, now=1100)
    kept.begin_recovery(key, now=1301)
    with pytest.raises(ValueError):
        kept.begin_recovery(key, now=1301)
    with pytest.raises(ValueError):
        kept.reset(key)
    kept.record(4, 'pat', 'small', key, 0, 'success', recovery=True, now=1302)
    kept.recovering.discard(key)
    assert not kept.summary(key, now=1303)['excluded']
    kept.reset(key)
    kept.record(5, 'pat', 'small', key, 0, 'error', now=1304)
    assert kept.summary(key, now=1304)['factor'] == 1
    assert kept.db.execute('SELECT COUNT(*) FROM reliability_outcomes').fetchone()[0] == 4
    for number in range(10, 110):
        kept.record(number, 'pat', 'small', key, 1, 'success' if number < 60 else 'empty', now=1400+number)
    assert kept.summary(key, now=1600)['regression']
    assert not kept.summary(key, now=1600+8*86400)['samples']
    assert kept.summary(key, now=1600)['factor'] == .5


def test_duplicate_outcomes_and_feedback_replacement_do_not_amplify_adjustments():
    kept = store(); key = ('id', 'fp', 'chat')
    kept.record(1, 'pat', 'small', key, 0, 'success')
    kept.record(1, 'pat', 'small', key, 0, 'error')
    kept.feedback(1, 'pat', 1)
    assert kept.summary(key)['factor'] == 1.1
    kept.feedback(1, 'pat', -1)
    assert kept.summary(key)['factor'] == .9 and kept.summary(key)['negative'] == 1
    kept.feedback(1, 'pat', 0)
    assert kept.summary(key)['factor'] == 1
    with pytest.raises(LookupError):
        kept.feedback(1, 'other', 1)
    for invalid in (True, 2, '1', None):
        with pytest.raises(ValueError):
            kept.feedback(1, 'pat', invalid)


def test_quality_ratings_outlive_the_reliability_window_and_count_per_fingerprint():
    kept = store()
    for number, fp in enumerate(('fp', 'fp', 'fp', 'new'), 1):
        kept.record(number, 'pat', 'small', ('id', fp, 'chat' if number % 2 else 'code'), 0, 'success', now=1000)
    kept.feedback(1, 'pat', 1, 'right')
    kept.feedback(2, 'pat', -1)
    kept.feedback(3, None, 1, 'checked', rater='judge')
    kept.feedback(4, 'pat', -1, 'off topic')
    kept.feedback(2, 'pat', 0)  # removed: no longer counted
    kept.feedback(2, 'pat', -1, 'rambled')
    kept.prune(7)  # every outcome is long gone; the ratings stay
    assert kept.db.execute('SELECT COUNT(*) FROM reliability_outcomes').fetchone()[0] == 0
    rows = {row['fingerprint']: row for row in kept.quality(explained=10)}
    assert (rows['fp']['good'], rows['fp']['bad'], rows['fp']['score']) == (2, 1, 66.7)
    assert {one['explanation'] for one in rows['fp']['explanations']} == {'right', 'rambled', 'checked'}
    assert next(one for one in rows['fp']['explanations'] if one['explanation'] == 'checked')['rater'] == 'judge'
    assert rows['new']['score'] == 0.0 and kept.rating(4) == {'value': -1, 'explanation': 'off topic', 'rater': 'pat'}


@pytest.mark.parametrize('envelope, expected', [
    ({'choices': [{'message': {'content': 'hello'}}]}, 'success'),
    ({'choices': [{'message': {'content': ' \n', 'reasoning_content': 'thinking'}}]}, 'empty'),
    ({'choices': []}, 'empty'),
    ({'choices': [{'message': {'tool_calls': [{'id': 'tool'}]}}]}, 'success'),
    ({'choices': [{'text': 'completion'}]}, 'success'),
    ({'not_a_chat': True}, 'unscored'),
])
def test_answer_classification(envelope, expected):
    assert answer_outcome(json.dumps(envelope).encode(), 'application/json', '/v1/chat/completions') == expected
    if 'choices' in envelope:
        stream = b'data: ' + json.dumps(envelope).encode() + b'\n\ndata: [DONE]\n\n'
        assert answer_outcome(stream, 'text/event-stream', '/v1/chat/completions') == expected


def test_completed_silent_stream_is_empty_but_unknown_or_malformed_bodies_are_unscored():
    assert answer_outcome(b': PROCESSING\n\ndata: [DONE]\n\n', 'text/event-stream', '/v1/chat/completions') == 'empty'
    assert answer_outcome(b'data: broken\n\n', 'text/event-stream', '/v1/chat/completions') == 'unscored'


def chat(caller, user, model='work:best', **more):
    return caller.post('/v1/chat/completions', headers=user, json={'model': model, 'messages': [{'role': 'user', 'content': 'hello'}], **more})


def test_real_outcomes_change_role_preference_and_feedback_is_owner_checked(fake_llama_server):
    with gateway(fake_llama_server, {name: model(name+'.gguf', 1) for name in ('a', 'b')}) as caller:
        owner = add_user(caller, 'owner', 'admin')
        pat = add_user(caller, 'pat', 'plain', by=owner)
        ids = catalogue(caller, owner)
        assert caller.post('/loadcoach/roles/work', headers=owner, json={'models': {'a': 1, 'b': 1}}).status_code == 201
        failed = chat(caller, pat, status=500, reply={'error': {'message': 'model failed'}})
        assert failed.status_code == 500
        preview = caller.get('/loadcoach/pick?model=work:best', headers=pat).json()
        assert preview['picked'] == 'b'
        fault = next(row for row in preview['models'] if row['name'] == 'a')
        assert fault['reliability']['counts']['error'] == 1 and fault['score'] == .5
        good = chat(caller, pat, reply={'choices': [{'message': {'content': 'hello'}}]})
        request = int(good.headers['x-loadcoach-request-id'])
        path = f'/loadcoach/requests/{request}/feedback'
        other = add_user(caller, 'other', 'plain', by=owner)
        assert caller.post(path, headers=other, json={'value': 1}).status_code == 404
        assert caller.get(path, headers=other).json()['may_feedback'] is False
        assert caller.post(path, headers=pat, json={'value': True}).status_code == 400
        assert caller.post(path, headers=pat, json={'value': -1, 'explanation': 'x' * 2001}).status_code == 400
        assert caller.post(path, headers=pat, json={'value': -1, 'explanation': 'wrong capital'}).status_code == 200
        assert caller.get(path, headers=pat).json() == {'may_feedback': True, 'feedback': -1, 'explanation': 'wrong capital'}
        # rate-answers (an app's judge; admin has it) may rate anyone's answer, and the rating is kept as quality.
        assert caller.get(path, headers=owner).json()['may_feedback'] is True
        assert caller.post(path, headers=owner, json={'value': -1, 'explanation': 'rambled on review'}).status_code == 200
        quality = caller.get('/loadcoach/quality', headers=owner).json()['rows']
        assert [(row['model'], row['good'], row['bad'], row['score']) for row in quality] == [('b', 0, 1, 0.0)]
        assert quality[0]['explanations'][0]['rater'] == 'owner' and quality[0]['explanations'][0]['explanation'] == 'rambled on review'
        assert 'explanations' not in caller.get('/loadcoach/quality', headers=pat).json()['rows'][0]
        assert caller.post('/loadcoach/requests/999999/feedback', headers=pat, json={'value': 1}).status_code == 404
        assert caller.get('/loadcoach/reliability', headers=pat).json()['scope'] == 'your requests'
        assert caller.get('/loadcoach/reliability', headers=owner).json()['scope'] == 'all users'
        # Direct selection is preserved, and exact fingerprints can be inspected/reset.
        assert chat(caller, pat, 'a', reply={'choices': [{'message': {'content': 'yes'}}]}).status_code == 200
        reset = {'model': 'a', 'role': 'work', 'fingerprint': ids['a'][1], 'action': 'reset'}
        assert caller.post('/loadcoach/reliability', headers=pat, json=reset).status_code == 403
        assert caller.post('/loadcoach/reliability', headers=owner, json=dict(reset, fingerprint='wrong')).status_code == 409
        assert caller.post('/loadcoach/reliability', headers=owner, json=reset).status_code == 200
        assert caller.get('/loadcoach/pick?model=work:best', headers=pat).json()['models'][0]['name'] == 'a'


def test_exclusion_durable_recovery_is_controlled_and_model_and_role_are_isolated(fake_llama_server, tmp_path):
    database = str(tmp_path/'reliability.db')
    models = {name: model(name+'.gguf', 1) for name in ('a', 'b')}
    with gateway(fake_llama_server, models, database=database) as caller:
        owner = add_user(caller, 'owner', 'admin')
        ids = catalogue(caller, owner)
        caller.post('/loadcoach/roles/work', headers=owner, json={'models': {'a': 1}})
        caller.post('/loadcoach/roles/other', headers=owner, json={'models': {'a': 1, 'b': 1}})
        for _ in range(3):
            assert chat(caller, owner, reply={'choices': [{'message': {'content': ''}}]}).status_code == 200
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] is None
        assert chat(caller, owner).status_code == 503
        assert caller.get('/loadcoach/pick?model=other:best', headers=owner).json()['picked'] == 'a'
        recovery = {'model': 'a', 'role': 'work', 'fingerprint': ids['a'][1], 'action': 'recover'}
        assert caller.post('/loadcoach/reliability', headers=owner, json=recovery).status_code == 409
    with gateway(fake_llama_server, models, database=database) as caller:
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] is None
        db = sqlite3.connect(Path(database).with_name('front') / 'roles.db')  # reliability is kept on the front machine
        db.execute('UPDATE reliability_state SET blocked_until=?', (time.time()-1,)); db.commit()
        before = caller.get('/loadcoach/events?kind=request', headers=owner).json()['events']
        assert caller.get('/loadcoach/reliability', headers=owner).json()['rows'][0]['state'] == 'recovery required'
        assert caller.get('/loadcoach/events?kind=request', headers=owner).json()['events'] == before
        recovered = caller.post('/loadcoach/reliability', headers=owner, json=recovery)
        assert recovered.status_code == 200 and recovered.json()['reliability']['state'] == 'eligible'
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] == 'a'
        last = db.execute('SELECT request, recovery FROM reliability_outcomes ORDER BY request DESC LIMIT 1').fetchone()
        assert last[1] == 1
        assert caller.post(f'/loadcoach/requests/{last[0]}/feedback', headers=owner, json={'value': 1}).status_code == 400
        assert caller.post('/loadcoach/reliability', headers=owner, json=recovery).status_code == 409
        db.close()


def test_transport_timeout_counts_but_user_stop_and_unlogged_requests_do_not(fake_llama_server, monkeypatch):
    with gateway(fake_llama_server, unlogged=True) as caller, ThreadPoolExecutor() as pool:
        owner = add_user(caller, 'owner', 'admin')
        catalogue(caller, owner)
        send = httpx.AsyncClient.send
        async def time_out(client, request, **kwargs):
            if request.url.path == '/v1/chat/completions' and request.url.host != 'compute':  # LoadCoach's own request to the model, not turnstile's to LoadCoach
                raise httpx.ReadTimeout('test upstream timeout')
            return await send(client, request, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(httpx.AsyncClient, 'send', time_out)
            assert chat(caller, owner, 'small').status_code == 502
        rows = caller.get('/loadcoach/reliability', headers=owner).json()['rows']
        assert rows[0]['counts']['timeout'] == 1
        stopped = pool.submit(chat, caller, owner, 'small', drip=20)
        entry = wait_for_status(_signed(caller, owner), lambda s: s['running'])['running'][0]['id']
        assert caller.post(f'/loadcoach/stop/{entry}', headers=owner).status_code == 200
        stopped.result(timeout=3)
        rows = caller.get('/loadcoach/reliability', headers=owner).json()['rows']
        assert rows[0]['ignored'] == {'cancelled': 1} and rows[0]['samples'] == 1
        assert chat(caller, {**owner, 'x-loadcoach-log': 'off'}, 'small', status=500, reply={'error': {'message': 'fault'}}).status_code == 500
        assert caller.get('/loadcoach/reliability', headers=owner).json()['rows'] == rows


def test_feedback_survives_user_rename_but_recreated_names_cannot_inherit_requests(fake_llama_server):
    with gateway(fake_llama_server) as caller:
        owner = add_user(caller, 'owner', 'admin')
        pat = add_user(caller, 'pat', 'plain', by=owner)
        catalogue(caller, owner)
        good = chat(caller, pat, 'small', reply={'choices': [{'message': {'content': 'hello'}}]})
        path = '/loadcoach/requests/' + good.headers['x-loadcoach-request-id'] + '/feedback'
        assert caller.post('/loadcoach/users/pat/rename', headers=owner, json={'name': 'alex'}).status_code == 200
        assert caller.post(path, headers=pat, json={'value': 1}).status_code == 200
        recreated = add_user(caller, 'pat', 'plain', by=owner)
        assert caller.post(path, headers=recreated, json={'value': -1}).status_code == 404
        assert not caller.get('/loadcoach/reliability', headers=recreated).json()['rows']


def test_old_exclusion_stays_manageable_after_outcomes_are_pruned_and_model_is_renamed(fake_llama_server, tmp_path):
    database = str(tmp_path/'retention.db')
    with gateway(fake_llama_server, database=database) as caller:
        owner = add_user(caller, 'owner', 'admin')
        ids = catalogue(caller, owner)
        caller.post('/loadcoach/roles/work', headers=owner, json={'models': {'small': 1}})
        for _ in range(3):
            chat(caller, owner, reply={'choices': [{'message': {'content': ''}}]})
        db = sqlite3.connect(Path(database).with_name('front') / 'roles.db')  # reliability is kept on the front machine
        kept = Reliability(db)
        db.execute('UPDATE reliability_outcomes SET at=?', (time.time()-100*86400,)); db.commit()
        kept.prune(90)
        assert caller.post('/loadcoach/model-rename', headers=owner, json={'name': 'small', 'to': 'renamed'}).status_code == 200
        report = caller.get('/loadcoach/reliability?model=renamed', headers=owner).json()['rows']
        assert len(report) == 1 and report[0]['current'] and report[0]['may_manage']
        assert report[0]['excluded'] and report[0]['last_request'] is None and report[0]['samples'] == 0
        reset = {'model': 'renamed', 'role': 'work', 'fingerprint': ids['small'][1], 'action': 'reset'}
        assert caller.post('/loadcoach/reliability', headers=owner, json=reset).status_code == 200
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] == 'renamed'
        db.close()


def test_error_recording_reliability_cannot_break_a_successful_proxy_answer(fake_llama_server, monkeypatch):
    with gateway(fake_llama_server) as caller:
        owner = add_user(caller, 'owner', 'admin')
        catalogue(caller, owner)
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('disk is full')
        monkeypatch.setattr(Reliability, 'record', fail)
        result = chat(caller, owner, 'small', reply={'choices': [{'message': {'content': 'hello'}}]})
        assert result.status_code == 200 and result.json()['choices'][0]['message']['content'] == 'hello'
        events = caller.log.read(0, 100, None, 'reliability')  # on record at the front machine, where reliability is kept
        assert events[-1]['what'] == 'record-failed'
