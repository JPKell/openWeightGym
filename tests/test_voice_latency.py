"""Clock locality, bounded summaries, private diagnostics and client acknowledgement semantics."""
import asyncio
from contextlib import asynccontextmanager
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from voice import latency
from voice.gateway import build_app


def test_compute_durations_keep_first_audio_and_first_synthesis_without_exporting_clocks():
    turn = latency.Turn(3,10,10.2,'input_end_received',.05)
    turn.synthesis(.3)
    turn.audio(10.9)
    turn.audio(12)
    turn.synthesis(.7)
    result = turn.event('completed')
    assert result == dict(basis='input_end_received',state='completed',recognition_block_seconds=.05,
                          endpoint_to_final_seconds=.2,synthesis_seconds=.3,end_to_audio_seconds=.9,final_to_audio_seconds=.7)
    assert not any(key.endswith('_at') for key in result)


def test_recent_turns_deduplicate_percentiles_and_keep_different_protocols_apart():
    recent = latency.Recent()
    for n,value in enumerate((.1,.2,.3,.4),1):
        recent.update('s',n,'desktop','direct',dict(end_to_audio_seconds=value,basis='input_end_received',model='m',profile='p',revision='r1'))
    recent.update('s',1,'desktop','direct',dict(end_to_audio_seconds=.1))
    recent.update('s',5,'desktop','direct',dict(end_to_audio_seconds=9,basis='input_end_received',model='m',profile='p',revision='r2'))
    recent.update('s',6,'twilio','hermes',dict(end_to_audio_seconds=5,basis='last_loud_frame_received'))
    snapshot = recent.snapshot()
    assert snapshot['count'] == 6 and len(snapshot['summaries']) == 3
    row = next(r for r in snapshot['summaries'] if r['revision']=='r1')
    assert (row['count'],row['p50_ms'],row['p95_ms']) == (4,200,400)
    assert not any('clear_ack_seconds' in r['values'] for r in snapshot['recent'])
    snapshot['recent'][0]['values']['end_to_audio_seconds'] = 0
    assert recent.snapshot()['recent'][0]['values']['end_to_audio_seconds'] == 5


def test_metrics_reject_nonfinite_boolean_unbounded_private_and_unrecognized_fields(monkeypatch):
    now = [100000.]
    monkeypatch.setattr(latency,'time',SimpleNamespace(time=lambda:now[0]))
    recent = latency.Recent()
    for n,value in enumerate((True,float('nan'),float('inf'),-1,7201,10**1000,'1'),1):
        recent.update('s',n,'browser','direct',dict(end_to_audio_seconds=value,basis=[],text='private',audio='private',key='private'))
    assert all(row['values']=={} for row in recent.snapshot()['recent'])
    assert recent.update('s',True,'browser','direct',{}) is None
    assert recent.update('s',-1,'browser','direct',{}) is None
    for n in range(8,209):
        recent.update('s',n,'browser','direct',dict(end_to_audio_seconds=.1))
    assert recent.snapshot()['count'] == 200 and len(recent.snapshot()['recent']) == 20
    now[0] += 86401
    assert recent.snapshot()['count'] == 0 and recent.snapshot()['summaries'] == []


def test_native_gateway_consumes_metrics_and_queue_acks_without_forwarding_them_or_waking_for_status(tmp_path,capsys):
    key = tmp_path/'gateway.key';key.write_text('app-key')
    sent = []
    async def respond(request):
        if request.method == 'DELETE':
            return httpx.Response(200,json={})
        if request.url.path == '/turnstile/ready':
            return httpx.Response(200,json={'state':'ready'})
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200,json={'name':'caller','can':['front-voice']})
        if request.url.path == '/voice/sessions':
            return httpx.Response(200,json={'token':'session-token','model':'small'})
        return httpx.Response(200,json={})
    class Compute:
        def __init__(self):
            self.events = asyncio.Queue()
        async def recv(self):
            return json.dumps({'type':'session.ready'})
        async def send(self,value):
            sent.append(value)
            if isinstance(value,bytes):
                measured = dict(basis='input_end_received',profile='default',model='small',revision='r1',state='started')
                await self.events.put(json.dumps(dict(type='turn.started',turn=1,latency=measured)))
                await self.events.put(json.dumps(dict(type='playback.segment',turn=1,segment=1,text='private utterance',samples=480)))
                await self.events.put(bytes(960))
            elif json.loads(value).get('interrupt'):
                await self.events.put(json.dumps(dict(type='playback.clear',turn=1)))
                await self.events.put(json.dumps(dict(type='turn.interrupted',turn=1,latency=dict(state='interrupted',end_to_audio_seconds=.1))))
        async def __aiter__(self):
            while True:
                yield await self.events.get()
    @asynccontextmanager
    async def connector(*args,**kwargs):
        yield Compute()
    app = build_app(dict(key_file=str(key),turnstile='http://front:8442',voice='http://compute:8445',
                         desktop_port=8447,prompts=str(tmp_path/'none')),
                    httpx.AsyncClient(transport=httpx.MockTransport(respond)),connector)
    with TestClient(app,base_url='http://front:8447') as client:
        with client.websocket_connect('/desktop/stream',headers={'authorization':'Bearer caller-key'}) as ws:
            assert ws.receive_json()['state'] == 'checking_key'
            assert ws.receive_json()['type'] == 'session.ready'
            ws.send_bytes(bytes(640))
            first = ws.receive_json()
            assert first == dict(type='turn.started',turn=1)  # internal durations are removed
            assert ws.receive_json()['type'] == 'playback.segment'
            assert ws.receive_bytes() == bytes(960)
            ws.send_json(dict(type='playback.progress',turn=1,segment=1,samples=481))
            ws.send_json(dict(type='playback.progress',turn=1,segment=1,samples=480))
            ws.send_json(dict(type='session.cancel',interrupt=True))
            assert ws.receive_json()['type'] == 'playback.clear'
            assert ws.receive_json() == dict(type='turn.interrupted',turn=1)
            ws.send_json(dict(type='playback.cleared',turn=1))
            deadline = time.monotonic()+2
            while time.monotonic()<deadline:
                row = client.portal.call(app.state.latency.snapshot)['recent'][0]
                if 'clear_ack_seconds' in row['values']:
                    break
                time.sleep(.01)
            assert row['values']['clear_ack_seconds'] >= 0 and row['values']['audio_to_ack_seconds'] >= 0
            assert row['values']['end_to_audio_seconds'] == .1 and row['values']['state'] == 'interrupted'
            ws.send_json(dict(type='session.cancel'))
    assert not any('playback.cleared' in value for value in sent if isinstance(value,str))
    output = capsys.readouterr().out
    assert 'voice.latency' in output and all(secret not in output for secret in ('private utterance','caller-key','session-token','app-key'))


def test_browser_worklet_acknowledges_only_explicit_server_queue_clears():
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.evaluate("""() => {
            window.reports=[]; window.sampleRate=48000;
            window.AudioWorkletProcessor=class {constructor(){this.port={postMessage:m=>reports.push(m)};}};
            window.registerProcessor=(name,kind)=>{window.Player=kind;};
        }""")
        page.add_script_tag(content=Path('voice/static/audio.js').read_text())
        result = page.evaluate("""() => {
            const player=new Player();player.queued=400;player.accept=true;
            player.port.onmessage({data:{type:'clear',turn:8}});
            player.port.onmessage({data:{type:'clear'}});
            player.port.onmessage({data:{type:'clear',turn:-1}});
            return {reports,queued:player.queued,accept:player.accept};
        }""")
        assert result == dict(reports=[dict(type='playback.cleared',turn=8)],queued=0,accept=False)
        browser.close()
