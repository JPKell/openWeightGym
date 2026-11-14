"""Voice boundary checks: real sockets, deterministic speech, no model/GPU required."""

import asyncio
import base64
import json
import math
import struct
from pathlib import Path
import time
import threading

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice import protocol
from voice.desktop_audio import Audio
from voice.service import build_app
from voice.speech import Speech
from voice.telephone import Conversion, signature, valid, valid_stream
from front_helpers import Signed, caller, machine_key


class FakeSpeech:
    """A single utterance on explicit endpoint; synthesized PCM is short and deterministic."""

    ready_since = None

    async def warm(self):
        self.ready_since = time.time()

    def stream(self):
        return {"heard": False}

    async def hear(self, stream, pcm, finish=False):
        stream["heard"] |= any(pcm)
        if finish and stream["heard"]:
            stream["heard"] = False
            return "Hello", "Hello"
        return "", None

    async def say(self, text, speaker=None):
        return b"\0\0" * 7200

    def close(self):
        pass


# Who asks, as turnstile's statement says. The voice service is never given a key.
CALLERS = {name: caller(name, [] if name == "plain" else ["front-voice"], ["small", "agent"]) for name in ("caller", "other", "plain")}
ANSWER = "Hello, here is the plan: open Settings; choose George, then save. "


@pytest.fixture
def voice(tmp_path, request):
    service_key = tmp_path / "service.key"
    service_key.write_text("service")
    calls = []  # what the voice service asked LoadCoach itself, with its own key, through turnstile

    async def coach(request):
        if request.headers.get("authorization") != "Bearer service":
            return httpx.Response(401, json={})
        if request.url.path == "/loadcoach/status":
            return httpx.Response(200, json={"loaded": [{"model": "small", "state": "ready"}], "lock": None, "draining": None})
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': name, 'owned_by': 'loadcoach', 'capabilities': {'chat': True}}
                                                    for name in ['small', 'agent', 'forbidden']]})
        calls.append(request)
        return httpx.Response(200, json={})

    settings = {"compute": "http://turnstile", "key_file": str(service_key), "machine_key_file": machine_key(tmp_path), "hold_every": .1,
                "hold_after_last_call": .1, "max_active_sessions": 2,
                "profiles": {"default": {"model": "small", "modes": ["direct", "hermes"]}}}
    app = build_app(settings, FakeSpeech(), httpx.AsyncClient(transport=httpx.MockTransport(coach)))
    with TestClient(app) as client:
        client.answer = getattr(request, "param", ANSWER)  # what the model says, as the gateway hands it back
        yield client, calls


def create(client, sid="one", key="caller", mode="direct"):
    return client.post("/voice/sessions", auth=Signed(CALLERS[key]), json={"session_id": sid, "conversation_mode": mode})


def socket(client, sid="one", token=None):
    """The sound socket of a session, opened with the session's own token, as the gateway opens it."""
    return client.websocket_connect(f"/voice/stream?session_id={sid}", headers={"authorization": "Bearer " + (token or client.tokens[sid])})


def started(client, sid="one", key="caller", mode="direct"):
    """Make a session and keep its token for its socket."""
    made = create(client, sid, key, mode)
    assert made.status_code == 200, made.text
    client.tokens = {**getattr(client, "tokens", {}), sid: made.json()["token"]}
    return made


def answered(client, ws, text=None):
    """Play the gateway for one turn: take the request voice hands over, and send the model's words back. Gives the request."""
    asked = ws.receive_json()
    assert asked["type"] == "model.request"
    words = client.answer if text is None else text
    ws.send_json({"type": "agent.text.delta", "turn": asked["turn"], "count": 1, "text": words})
    ws.send_json({"type": "agent.text.done", "turn": asked["turn"], "count": 1})
    return asked


def test_readiness_never_warms_or_holds(voice):
    client, calls = voice
    assert client.get("/voice/ready", headers={"authorization": "Bearer caller"}).status_code == 401  # only a statement says who asks
    result = client.get("/voice/ready", auth=Signed(CALLERS["caller"]))
    assert result.status_code == 200
    assert not result.json()["profiles"]["default"]["can_start"]
    assert not calls


def test_call_options_preserve_permissions_and_other_sessions_defaults(voice):
    """Per-call model and answer choices never mutate a shared profile or bypass its caller."""
    client, calls = voice
    signed = Signed(CALLERS['caller'])
    body = {'session_id': 'custom', 'conversation_mode': 'direct',
            'options': {'model': 'agent', 'max_output_tokens': 64, 'system_prompt': 'Be concise.', 'speed': .75}}
    made = client.post('/voice/sessions', auth=signed, json=body)
    assert made.status_code == 200 and made.json()['model'] == 'agent'
    assert create(client, sid='ordinary').json()['model'] == 'small'
    assert {json.loads(r.content)['model'] for r in calls if r.url.path == '/loadcoach/hold'} == {'agent', 'small'}
    assert client.post('/voice/sessions', auth=signed, json={**body, 'options': {'model': 'small'}}).status_code == 409
    assert client.post('/voice/sessions', auth=signed, json={**body, 'session_id': 'forbidden', 'options': {'model': 'forbidden'}}).status_code == 403
    assert client.post('/voice/sessions', auth=Signed(CALLERS['plain']), json={**body, 'session_id': 'plain'}).status_code == 403


@pytest.mark.parametrize('options', [{'speed': 0}, {'speed': True}, {'max_output_tokens': 1000000},
                                    {'system_prompt': ''}, {'speaker': 999}, {'trailing_silence_ms': 500},
                                    {'key_file': '/private/key'}, {'model': {'nested': 'small'}}])
def test_invalid_call_options_do_not_load_models(voice, options):
    """Malformed, unsupported and unbounded settings fail before speech or model preparation."""
    client, calls = voice
    result = client.post('/voice/sessions', auth=Signed(CALLERS['caller']),
                         json={'session_id': 'invalid', 'conversation_mode': 'direct', 'options': options})
    assert result.status_code == 400 and result.json()['error']['code'] == 'invalid_options'
    assert not calls


def test_public_call_choices_are_scoped_and_do_not_warm_or_hold(voice):
    """The browser sees only permitted model names and public profile fields."""
    client, calls = voice
    assert client.get('/voice/options', auth=Signed(CALLERS['plain'])).status_code == 403
    result = client.get('/voice/options', auth=Signed(CALLERS['caller']))
    assert result.status_code == 200 and result.json()['models'] == ['small', 'agent']
    assert result.json()['profiles'] == [{'id': 'default', 'model': 'small', 'modes': ['direct', 'hermes']}]
    assert not calls


def test_cloned_call_models_are_permitted_held_and_isolated_per_session(tmp_path):
    """A caller's speech-model choice changes only its own holds and speech configuration."""
    key = tmp_path / 'service.key'; key.write_text('service')
    held = []
    catalogue = [{'id': name, 'owned_by': 'loadcoach', 'capabilities': {kind: True}}
                 for name, kind in [('small', 'chat'), ('clone', 'clone'), ('alternate', 'clone'), ('asr', 'transcription')]]
    async def coach(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': catalogue})
        if request.url.path == '/loadcoach/hold':
            held.append(json.loads(request.content)['model'])
        return httpx.Response(200, json={})
    settings = {'compute': 'http://turnstile', 'key_file': str(key), 'machine_key_file': machine_key(tmp_path),
        'max_active_sessions': 2, 'profiles': {'soundroom': {'model': 'small', 'speech': 'audiocpp',
            'recognition_model': 'asr', 'synthesis_model': 'clone', 'modes': ['direct']}}}
    app = build_app(settings, FakeSpeech(), httpx.AsyncClient(transport=httpx.MockTransport(coach)))
    person = caller('permitted', ['front-voice'], ['small', 'clone', 'alternate', 'asr'])
    body = {'session_id': 'custom', 'profile': 'soundroom', 'conversation_mode': 'direct',
            'voice': {'audio': 'eA==', 'text': 'A reference.'},
            'options': {'synthesis_model': 'alternate', 'recognition_model': 'asr', 'trailing_silence_ms': 1200}}
    with TestClient(app) as client:
        assert client.post('/voice/sessions', auth=Signed(person), json=body).status_code == 200
        assert client.post('/voice/sessions', auth=Signed(person), json={**body, 'session_id': 'default', 'options': {}}).status_code == 200
        assert app.state.sessions['custom'].audio_speech.profile['trailing_silence_ms'] == 1200
        assert settings['profiles']['soundroom']['synthesis_model'] == 'clone'
        assert set(held) == {'small', 'clone', 'alternate', 'asr'}
        before = len(held)
        limited = caller('limited', ['front-voice'], ['small', 'clone', 'asr'])
        refused = client.post('/voice/sessions', auth=Signed(limited), json={**body, 'session_id': 'forbidden'})
        assert refused.status_code == 403 and len(held) == before


def test_starting_session_does_not_queue_duplicate_hold_renewals(tmp_path):
    """The keepalive cannot enqueue another load while the initial hold is still waiting."""
    from concurrent.futures import ThreadPoolExecutor
    key = tmp_path / 'service.key'; key.write_text('service')
    entered, release = threading.Event(), threading.Event()
    holds = []
    async def coach(request):
        if request.url.path == '/loadcoach/hold':
            holds.append(request)
            entered.set()
            await asyncio.to_thread(release.wait, 2)
        return httpx.Response(200, json={})
    settings = {'compute': 'http://turnstile', 'key_file': str(key), 'machine_key_file': machine_key(tmp_path),
                'hold_every': .01, 'profiles': {'default': {'model': 'small', 'modes': ['direct']}}}
    app = build_app(settings, FakeSpeech(), httpx.AsyncClient(transport=httpx.MockTransport(coach)))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        preparing = executor.submit(create, client)
        try:
            assert entered.wait(1)
            time.sleep(.06)  # several renewal ticks while the real startup boundary stays blocked
            assert len(holds) == 1
        finally:
            release.set()
        assert preparing.result(timeout=2).status_code == 200


def test_hermes_holds_its_selected_model_and_checks_its_permissions(voice):
    client, calls = voice
    signed = Signed(CALLERS["caller"])
    body = dict(session_id="agent-call", conversation_mode="hermes", agent_model="agent")
    assert client.post("/voice/sessions", auth=signed, json={**body, "agent_model": "forbidden"}).status_code == 403
    assert not calls
    assert client.post("/voice/sessions", auth=signed, json=body).status_code == 200
    assert client.post("/voice/sessions", auth=signed, json={**body, "agent_model": "small"}).status_code == 409
    time.sleep(.2)  # includes a periodic hold refresh
    held = [json.loads(r.content)["model"] for r in calls if r.url.path == "/loadcoach/hold"]
    assert len(held) >= 2 and set(held) == {"agent"}
    assert not any(r.url.path == "/v1/chat/completions" for r in calls)
    # The mock reports only the direct model as loaded; agent readiness must not borrow it.
    assert client.get("/voice/ready", auth=signed).json()["profiles"]["default"]["model"]
    ready = client.get("/voice/ready?conversation_mode=hermes&agent_model=agent", auth=signed).json()
    assert not ready["profiles"]["default"]["model"]


def test_voice_requires_ability_and_the_sessions_own_token_on_its_socket(voice):
    client, _ = voice
    assert create(client, key="plain").status_code == 403
    assert client.post("/voice/sessions", headers={"authorization": "Bearer caller"}, json={"session_id": "one", "conversation_mode": "direct"}).status_code == 401
    started(client)
    assert create(client, key="other").status_code == 409
    again = create(client)  # the same caller asking again is given the same session
    assert again.status_code == 200 and again.json()["token"] == client.tokens["one"]
    for token in ("caller", client.tokens["one"][:-1]):
        with pytest.raises(WebSocketDisconnect):
            with socket(client, token=token):
                pass
    assert client.delete("/voice/sessions/one", headers={"authorization": "Bearer wrong"}).status_code == 403


def test_voice_ability_grants_direct_and_hermes_subject_to_model_access(voice):
    client, calls = voice
    browser = Signed(caller("voice-browser", ["front-voice"], ["small"]))
    hermes = dict(session_id="browser-hermes", conversation_mode="hermes", agent_model="small")
    assert client.post("/voice/sessions", auth=browser, json=hermes).status_code == 200
    direct = dict(session_id="browser-direct", conversation_mode="direct")
    assert client.post("/voice/sessions", auth=browser, json=direct).status_code == 200


def test_direct_audio_turn_hands_the_gateway_what_to_ask_and_credits_playback(voice):
    client, calls = voice
    started(client)
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "direct"))
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_bytes(b"\x10\x27" * 320)
        ws.send_json({"type": "input.end"})
        first = ws.receive_json()
        assert first["type"] == "turn.started" and first["latency"]["basis"] == "input_end_received"
        assert ws.receive_json()["text"] == "Hello"
        asked = answered(client, ws)
        segment = ws.receive_json()
        assert segment["type"] == "playback.segment"
        assert segment["text"] == "Hello, here is the plan: open Settings; choose George, then save."
        assert "reasoning" not in segment["text"]
        heard = 0
        while heard < segment["samples"]:
            pcm = ws.receive_bytes()
            heard += len(pcm) // 2
            ws.send_json(dict(type="playback.progress", turn=1, segment=segment["segment"], samples=heard))
        done = ws.receive_json()
        assert done["type"] == "turn.completed"
        measured = done["latency"]
        assert measured["end_to_audio_seconds"] >= measured["final_to_audio_seconds"] >= 0
        assert measured["synthesis_seconds"] >= 0 and measured["state"] == "completed"
        assert measured["model"] == "small" and measured["profile"] == "default"
        assert len(measured["revision"]) == 16
        assert "Hello" not in json.dumps(measured)
        ws.send_json({"type": "session.cancel"})
    body = asked["body"]
    assert (body["model"], body["stream"], body["messages"][-1]) == ("small", True, {"role": "user", "content": "Hello"})
    assert not [r for r in calls if r.url.path == "/v1/chat/completions"]  # the voice service asks no model itself



def test_automatic_recognition_labels_the_endpoint_as_inferred(voice,monkeypatch):
    async def automatic(self,stream,pcm,finish=False):
        return ('Hello','Hello') if pcm else ('',None)
    monkeypatch.setattr(FakeSpeech,'hear',automatic)
    client,_ = voice
    started(client)
    with socket(client) as ws:
        ws.send_json(protocol.start('one','direct'))
        assert ws.receive_json()['type'] == 'session.ready'
        ws.send_bytes(b'\x10\x27'*320)
        event = ws.receive_json()
        assert event['type'] == 'turn.started'
        assert event['latency']['basis'] == 'last_loud_frame_received'
        assert event['latency']['endpoint_to_final_seconds'] >= 0
        assert 'input_end_received' not in event['latency'].values()
        ws.send_json(dict(type='session.cancel'))

def test_silence_never_asks_the_model_and_words_for_no_turn_are_ignored(voice):
    client, _ = voice
    started(client)
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "direct"))
        ws.receive_json()
        ws.send_bytes(bytes(640))
        ws.send_json({"type": "input.end"})
        ws.send_json({"type": "ping", "token": "alive"})
        assert ws.receive_json()["type"] == "pong"  # no model.request came before it
        ws.send_json({"type": "agent.text.delta", "turn": 1, "count": 1, "text": "Injected"})
        ws.send_json({"type": "ping", "token": "still"})
        assert ws.receive_json() == {"type": "pong", "token": "still"}
        ws.send_json({"type": "session.cancel"})


def test_mode_mismatch_and_capacity(voice):
    client, _ = voice
    started(client)
    started(client, "two", "other")
    assert create(client, "three").status_code == 429
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "hermes"))
        assert ws.receive_json()["type"] == "session.error"
    assert create(client, "three").status_code == 200


def test_telephone_resampling_keeps_time_and_signature_checks_all_fields():
    converter = Conversion()
    pcm = b"\0\0" * 480
    ulaw = b"".join(converter.send(pcm) for _ in range(100))
    assert len(ulaw) == 16000
    received = b"".join(converter.receive(ulaw[i:i + 160]) for i in range(0, len(ulaw), 160))
    assert 0 <= 32000 - len(received) / 2 <= 320  # at most one pending 20 ms frame
    params = {"CallSid": ["CA123"], "Extra": ["future-field"]}
    signed = signature("secret", "https://example.test/voice/inbound", params)
    assert valid("secret", "https://example.test/voice/inbound", params, signed)
    assert not valid("secret", "https://evil.test/voice/inbound", params, signed)
    assert not valid("secret", "https://example.test/voice/inbound", {"CallSid": ["CA123"]}, signed)


def test_stream_signature_variants_do_not_accept_other_hosts_paths_or_credentials():
    url = "wss://example.test/voice/stream"
    for signed_url in (url, url + "/", "https://example.test/voice/stream", "https://example.test/voice/stream/"):
        assert valid_stream("secret", url, signature("secret", signed_url, {}))
    for signed_url in ("wss://evil.test/voice/stream", "wss://example.test/other", "https://example.test/voice/inbound",
                       "wss://example.test/voice/stream?injected=true"):
        assert not valid_stream("secret", url, signature("secret", signed_url, {}))
    for supplied in ("", "wrong", "é", signature("wrong-key", url, {})):
        assert not valid_stream("secret", url, supplied)
    assert not valid_stream("", url, signature("", url, {}))


def test_protocol_rejects_odd_or_oversized_audio_and_hides_thinking():
    for pcm in (b"", b"x", bytes(3202)):
        with pytest.raises(ValueError):
            protocol.check_audio(pcm)
    segmenter = protocol.Phrases()
    assert segmenter.add("\n  <think>secret thoughts. ") == []
    assert segmenter.add("</think>Hello. ") == ["Hello."]
    assert protocol.speakable("1. [Python](https://python.org)\nSee https://example.test/x") == "Python See"
    assert protocol.speakable("[Interrupted; remaining audio was not confirmed heard.]") == ""
    assert protocol.speakable("Interrupted; remaining audio was not confirmed heard.") == ""
    assert protocol.speakable("Yes. [Previous output was not confirmed heard. Do not repeat tool actions.] Go ahead.") == "Yes. Go ahead."


@pytest.mark.parametrize("before_audio", [False, True])
def test_interrupted_reply_does_not_become_assistant_dialogue(voice, monkeypatch, before_audio):
    """Unplayed text and transport notes must not train the next response to repeat a status."""
    client, _ = voice
    if before_audio:
        original = FakeSpeech.say

        async def slow(self, text, speaker=None):
            await asyncio.sleep(.2)
            return await original(self, text, speaker)

        monkeypatch.setattr(FakeSpeech, "say", slow)
    started(client)
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "direct")); ws.receive_json()
        ws.send_bytes(b"\x10\x27" * 320); ws.send_json({"type": "input.end"})
        assert ws.receive_json()["type"] == "turn.started"
        assert ws.receive_json()["type"] == "transcript.final"
        answered(client, ws)
        if not before_audio:
            assert ws.receive_json()["type"] == "playback.segment"
        # No complete segment was heard. Continue talking while audio is still queued.
        ws.send_json({"type": "session.cancel", "interrupt": True})
        while True:
            event = ws.receive()
            if event.get("text") and json.loads(event["text"])["type"] == "turn.interrupted":
                break
        ws.send_bytes(b"\x10\x27" * 320); ws.send_json({"type": "input.end"})
        assert ws.receive_json()["type"] == "turn.started"
        assert ws.receive_json()["type"] == "transcript.final"
        messages = answered(client, ws)["body"]["messages"]
        assert ws.receive_json()["type"] == "playback.segment"
        assert [m["role"] for m in messages] == ["system", "user", "user"]
        assert "Never read internal playback status" in messages[0]["content"]
        assert "remaining audio was not confirmed heard" not in json.dumps(messages)
        ws.send_json({"type": "session.cancel"})


@pytest.mark.parametrize("voice", ["[Interrupted; remaining audio was not confirmed heard.]"], indirect=True)
def test_delivery_annotation_only_output_gets_natural_recovery(voice):
    client, _ = voice
    started(client)
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "direct")); ws.receive_json()
        ws.send_bytes(b"\x10\x27" * 320); ws.send_json({"type": "input.end"})
        assert ws.receive_json()["type"] == "turn.started"
        assert ws.receive_json()["type"] == "transcript.final"
        answered(client, ws)
        segment = ws.receive_json()
        assert segment["type"] == "playback.segment"
        assert segment["text"] == "Sorry, could you say that again?"
        ws.send_json({"type": "session.cancel"})


def test_delayed_recognition_from_silence_cannot_borrow_new_speech():
    """A result about old silence is still rejected when fresh caller audio has arrived."""
    from types import SimpleNamespace

    class Native:
        def accept_waveform(self, rate, samples):
            pass

    class Recognizer:
        result = SimpleNamespace(text="I AM", start_time=5.12, timestamps=[0])
        create_stream = lambda self: Native()
        is_ready = lambda self, stream: False
        is_endpoint = lambda self, stream: True
        get_result_all = lambda self, stream: self.result
        reset = lambda self, stream: None

    speech = Speech({"synthesis": {"workers": 1}})
    speech.recognizer = Recognizer()
    try:
        stream = speech.stream()
        assert speech.feed(stream, bytes(16000 * 2 * 6), False) == ("", "")
        assert speech.feed(stream, b"\x10\x27" * 320, False) == ("", None)
        speech.recognizer.result = SimpleNamespace(text="Hello", start_time=6.08, timestamps=[.1])
        assert speech.feed(stream, b"", True) == ("Hello", "Hello")
        assert stream.samples == 96000 + 320 + 9600
    finally:
        speech.close()


def test_slow_playback_stops_after_bounded_unacknowledged_audio(voice):
    client, _ = voice
    started(client)
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "direct")); ws.receive_json()
        ws.send_bytes(b"\x10\x27" * 320); ws.send_json({"type": "input.end"})
        ws.receive_json(); ws.receive_json()
        answered(client, ws)
        ws.receive_json()
        sound = b"".join(ws.receive_bytes() for _ in range(10))
        assert len(sound) == 9600  # 200 ms, then the service must wait for played samples
        assert ws.receive_json()["type"] == "session.error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_late_synthesis_cannot_revive_an_interrupted_turn(voice, monkeypatch):
    async def late(self, text, speaker=None):
        try:
            await asyncio.sleep(.3)
        except asyncio.CancelledError:
            await asyncio.sleep(.1)  # a native worker cannot necessarily stop immediately
        return bytes(960)

    monkeypatch.setattr(FakeSpeech, "say", late)
    client, _ = voice
    started(client)
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "direct")); ws.receive_json()
        ws.send_bytes(b"\x10\x27" * 320); ws.send_json({"type": "input.end"})
        assert ws.receive_json()["type"] == "turn.started"
        ws.receive_json()
        answered(client, ws)
        ws.send_json({"type": "session.cancel", "interrupt": True})
        assert ws.receive_json()["type"] == "playback.clear"
        assert ws.receive_json()["type"] == "turn.interrupted"
        time.sleep(.4)
        ws.send_json({"type": "ping", "token": "after-late-worker"})
        assert ws.receive_json() == {"type": "pong", "token": "after-late-worker"}
        ws.send_json({"type": "session.cancel"})


@pytest.mark.parametrize("caller_speaking", [False, True])
def test_desktop_speaker_feedback_against_service_interrupt_detection(voice, monkeypatch, caller_speaking):
    """Echo-cancelled capture keeps genuine spoken interruption available."""
    heard_echo = threading.Event()
    blocks = 0
    original = FakeSpeech.hear

    async def hear(self, stream, pcm, finish=False):
        nonlocal blocks
        result = await original(self, stream, pcm, finish)
        if pcm:
            blocks += 1
            if blocks == 11:  # initial utterance plus ten feedback frames
                heard_echo.set()
        return result

    async def say(self, text, speaker=None):
        return b"\x10\x27" * 7200

    monkeypatch.setattr(FakeSpeech, "hear", hear)
    monkeypatch.setattr(FakeSpeech, "say", say)
    client, _ = voice
    started(client)
    audio = Audio()
    with socket(client) as ws:
        ws.send_json(protocol.start("one", "direct"))
        audio.event(ws.receive_json())
        ws.send_bytes(b"\x10\x27" * 320)
        ws.send_json({"type": "input.end"})
        audio.event(ws.receive_json())
        assert ws.receive_json()["type"] == "transcript.final"
        answered(client, ws)
        audio.event(ws.receive_json())
        for _ in range(10):
            audio.receive(ws.receive_bytes())
        for frame in range(11):
            now = 10 + frame * .02
            pcm = audio.render(960, at=now)
            if caller_speaking:
                pcm = struct.pack("<960h", *(int(10000 * math.sin(i * math.tau * 750 / 48000)) for i in range(960)))
            audio.feed(pcm, now=now)
            while not audio.incoming.empty():
                ws.send_bytes(audio.incoming.get_nowait())
        assert heard_echo.wait(2)
        ws.send_json({"type": "ping", "token": "feedback-processed"})
        if caller_speaking:
            clear = ws.receive_json()
            assert clear["type"] == "playback.clear" and clear["why"] == "caller_speaking"
            assert ws.receive_json()["type"] == "turn.interrupted"
        assert ws.receive_json() == {"type": "pong", "token": "feedback-processed"}
        ws.send_json({"type": "session.cancel"})


def test_spoken_sentences_keep_comma_colon_and_semicolon_prosody_across_deltas():
    splitter = protocol.Phrases()
    assert splitter.add("Yes, ") == []
    assert splitter.add("here is the plan: ") == []
    assert splitter.add("open Settings; ") == []
    assert splitter.add("choose George, then save.") == []
    assert splitter.add(" Next, ") == ["Yes, here is the plan: open Settings; choose George, then save."]
    assert splitter.add("start a call.", done=True) == ["Next, start a call."]
    assert protocol.Phrases().add('He said, "Ready?" Then wait.', done=True) == ['He said, "Ready?"', 'Then wait.']


def test_long_speech_remains_bounded_without_losing_text():
    text = "This sentence has several clauses, " * 15 + "and now it ends."
    splitter = protocol.Phrases()
    phrases = []
    for start in range(0, len(text), 7):
        phrases.extend(splitter.add(text[start:start + 7]))
    phrases.extend(splitter.add("", done=True))
    assert " ".join(phrases) == text
    assert all(len(phrase) <= splitter.limit for phrase in phrases)
