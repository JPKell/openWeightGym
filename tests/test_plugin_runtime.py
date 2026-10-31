"""Independent CLI adapter protocol, sessions, receipts and process-group cancellation."""

import json
import os
import time

import httpx
import pytest
from starlette.testclient import TestClient

from plugin_helpers import setup, serving, turns
from plugins._runtime import build_app

BODY = {"model": "gandalf", "messages": [{"role": "user", "content": "hello"}]}


def test_sessions_streams_final_answer_and_profile_policy(tmp_path):
    registry, cli, app, headers = setup(tmp_path)
    with TestClient(app) as client:
        assert client.get('/v1/models').status_code == 401
        inventory = client.get('/v1/models', headers=headers).json()['data']
        assert [row['id'] for row in inventory] == ['default', 'gandalf']
        settings = {"cli": str(cli), "profiles": {"gandalf": {"approval": "yolo", "accept_hooks": True, "workspace": "/tmp", "reasoning": "high"}}, "run_budget": 5}
        applied = client.put('/adapter/v1/settings', headers=headers, json={"revision": 2, "settings": settings,
                       "policy": {"allow_start": False, "allow_restart": False}})
        assert applied.status_code == 200
        prompt = "quotes ' \" $() `touch /nope` ;\nUnicode: 🧙"
        body = {**BODY, "messages": [{"role": "user", "content": prompt}], "stream": True}
        persistent = {**headers, "x-suite-session-mode": "persistent", "idempotency-key": "first"}
        response = client.post('/v1/chat/completions', headers=persistent, json=body)
        assert response.status_code == 200
        sid = response.headers['x-suite-session-id']
        assert sid.startswith('ps_') and response.headers['x-suite-adaptor'] == 'hermes'
        chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and line != 'data: [DONE]']
        deltas = [chunk['choices'][0]['delta'] for chunk in chunks]
        assert ''.join(delta.get('content', '') for delta in deltas) == 'Final answer'
        assert ''.join(delta.get('reasoning_content', '') for delta in deltas) == 'Available reasoning'
        assert any(delta.get('plugin_events', [{}])[0].get('type') == 'tool_use' for delta in deltas)
        assert 'total_tokens' not in response.text and '"tokens"' not in response.text and '"usage"' not in response.text
        invocation = turns(cli)[0]
        assert invocation['query'] == prompt
        assert invocation['argv'][:3] == ['-p', 'gandalf', 'chat']
        assert '--continue' in invocation['argv'] and '--yolo' in invocation['argv'] and '--accept-hooks' in invocation['argv']
        assert '--usage-file' not in invocation['argv']
        replay = client.post('/v1/chat/completions', headers=persistent, json=body)
        assert replay.headers['x-suite-request-id'] == response.headers['x-suite-request-id'] and len(turns(cli)) == 1
        assert client.post('/v1/chat/completions', headers=persistent, json=BODY).status_code == 409
        continued = client.post('/v1/chat/completions', headers={**headers, "x-suite-session-mode": "persistent", "x-suite-session-id": sid}, json=BODY)
        assert continued.status_code == 200 and '--resume' in turns(cli)[1]['argv']
        assert continued.json()['choices'][0]['message']['content'] == 'Final answer'
        assert client.post('/v1/chat/completions', headers={**headers, "x-suite-user-id": "other", "x-suite-session-mode": "persistent", "x-suite-session-id": sid}, json=BODY).status_code == 404
        assert client.post('/v1/chat/completions', headers={**headers, "x-suite-session-id": sid}, json=BODY).status_code == 400
        assert client.post('/v1/chat/completions', headers={**headers, "x-suite-instance": "remote"}, json=BODY).status_code == 403
        assert client.post('/adapter/v1/control', headers=headers, json={"action": "restart"}).status_code == 403
        assert client.put('/adapter/v1/settings', headers=headers, json={"revision": 1, "settings": settings, "policy": {"allow_start": False, "allow_restart": False}}).status_code == 409
        assert client.post('/v1/chat/completions', headers=headers, json={**BODY, 'tools': [{'type': 'function'}]}).status_code == 400
        assert client.post('/v1/chat/completions', headers=headers, json={**BODY, 'n': True}).status_code == 400
        # A failed run has a durable receipt and cannot be retried by repeating a key.
        failed = client.post('/v1/chat/completions', headers={**headers, 'idempotency-key': 'fail'}, json={**BODY, 'messages': [{'role': 'user', 'content': 'FAIL'}]})
        assert failed.status_code == 502
        receipt = client.get('/adapter/v1/runs/' + failed.headers['x-suite-request-id'], headers=headers).json()
        assert receipt['status'] == 'failed' and receipt['response'] is None
    registry.db.close()


def test_real_disconnect_kills_cli_children_and_releases_session(tmp_path):
    registry, cli, app, headers = setup(tmp_path)
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        with client.stream('POST', '/v1/chat/completions', headers={**headers, 'x-suite-session-mode': 'persistent', 'idempotency-key': 'blocked'}, json={**BODY, 'stream': True, 'messages': [{'role': 'user', 'content': 'BLOCK'}]}) as response:
            assert response.status_code == 200
            rid = response.headers['x-suite-request-id']
            deadline = time.monotonic() + 5
            while not cli.with_suffix('.pid').exists():
                assert time.monotonic() < deadline
                time.sleep(.02)
            child = int(cli.with_suffix('.pid').read_text())
            sid = response.headers['x-suite-session-id']
            assert client.post('/v1/chat/completions', headers={**headers, 'x-suite-session-mode': 'persistent', 'x-suite-session-id': sid}, json=BODY).status_code == 409
            app.state.runtime.config['settings']['max_concurrent'] = 1
            assert client.post('/v1/chat/completions', headers=headers, json=BODY).status_code == 429
        deadline = time.monotonic() + 6
        while client.get('/adapter/v1/runs/' + rid, headers=headers).json()['status'] == 'running':
            assert time.monotonic() < deadline
            time.sleep(.05)
        assert client.get('/adapter/v1/runs/' + rid, headers=headers).json()['status'] == 'cancelled'
        process = f'/proc/{child}/stat'
        assert not os.path.exists(process) or open(process).read().split()[2] == 'Z'
        assert not app.state.runtime.active
    registry.db.close()


def test_single_instance_runtime_lock_and_restart_uncertain_receipt(tmp_path):
    registry, _, app, _ = setup(tmp_path)
    config = app.state.runtime.file
    with TestClient(app):
        with pytest.raises(BlockingIOError):
            build_app(config, registry.manifest('hermes'))
        with app.state.runtime.db:
            app.state.runtime.db.execute("INSERT INTO requests VALUES ('old','owner','gandalf','session','idem','fp','running',0,NULL,NULL,NULL)")
    restarted = build_app(config, registry.manifest('hermes'))
    with TestClient(restarted):
        assert restarted.state.runtime.db.execute("SELECT status FROM requests WHERE id='old'").fetchone()[0] == 'uncertain'
    registry.db.close()
