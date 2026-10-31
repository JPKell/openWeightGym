"""Gateway authorization, public/private routing, and signed telephone admission."""

import json

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect
from starlette.testclient import TestClient

from voice.gateway import build_app
from voice.telephone import signature


@pytest.mark.parametrize("signed_stream_url", ["wss://public.test/voice/stream", "wss://public.test/voice/stream/",
                                               "https://public.test/voice/stream", "https://public.test/voice/stream/"])
def test_browser_key_stays_server_side_and_public_port_exposes_only_twilio(tmp_path, signed_stream_url):
    key = tmp_path / "gateway.key"; key.write_text("gateway-key")
    caller = tmp_path / "browser.key"; caller.write_text("private-browser-key")
    token = tmp_path / "twilio.key"; token.write_text("twilio-secret")
    passed = []

    async def upstream(request):
        passed.append(request)
        return httpx.Response(200, json={})

    settings = dict(key_file=str(key), browser_key_file=str(caller), turnstile="http://turnstile:8442", compute="http://compute:8442",
                    voice="http://compute:8445", origins=["https://private.test"], modes=["direct"], public_port=8081,
                    twilio=dict(token_file=str(token), webhook_url="https://public.test/voice/inbound",
                                stream_url="wss://public.test/voice/stream", account_sid="ACtest"))
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    with TestClient(app, base_url="https://private.test:8446") as browser, TestClient(app, base_url="http://public.test:8081") as public:
        assert public.get("/").status_code == 404
        assert public.get("/browser/config").status_code == 404
        assert public.post("/voice/inbound", data={"CallSid": "CA123"}).status_code == 403
        assert browser.post("/browser/sessions", json={}).status_code == 403
        started = browser.post("/browser/sessions", json={}, headers={"origin": "https://private.test"})
        assert started.status_code == 200
        assert app.state.browser_sessions[started.json()["session_id"]]["key"] == "private-browser-key"
        assert "private-browser-key" not in started.text + started.headers.get("set-cookie", "")
        assert "HttpOnly" in started.headers["set-cookie"] and "Secure" in started.headers["set-cookie"]
        assert browser.post("/browser/sessions", json={"conversation_mode": "hermes"}, headers={"origin": "https://private.test"}).status_code == 403
        assert not passed  # starting asks nothing of compute, so it cannot wake it
        params = {"CallSid": "CA123", "AccountSid": "ACtest", "From": "+15555550123", "FutureField": "kept"}
        sig = signature("twilio-secret", "https://public.test/voice/inbound", {k: [v] for k, v in params.items()})
        call = public.post("/voice/inbound", data=params, headers={"x-twilio-signature": sig})
        assert call.status_code == 200 and '<Connect><Stream url="wss://public.test/voice/stream">' in call.text
        again = public.post("/voice/inbound", data=params, headers={"x-twilio-signature": sig})
        assert again.text == call.text  # webhook retries share one reservation
        upgrade = signature("twilio-secret", signed_stream_url, {})
        with public.websocket_connect("/voice/stream", headers={"x-twilio-signature": upgrade}) as ws:
            ws.send_json({"event": "start", "start": {"callSid": "CA123", "customParameters": {"session": "wrong", "nonce": "wrong"}}})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with public.websocket_connect("/voice/stream", headers={"x-twilio-signature": "wrong"}):
                pass
        params["CallSid"] = "CAother"
        assert public.post("/voice/inbound", data=params, headers={"x-twilio-signature": sig}).status_code == 403


def test_weightroom_reserves_a_call_as_its_person_without_origin_or_browser_account(tmp_path):
    """WeightRoom sends its signed-in person's key; it holds the cookie and opens the socket itself."""
    key = tmp_path / "gateway.key"; key.write_text("gateway-key")
    settings = dict(key_file=str(key), turnstile="http://turnstile:8442", compute="http://compute:8442",
                    voice="http://compute:8445", origins=["https://private.test"], modes=["direct"])
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))))
    with TestClient(app, base_url="http://127.0.0.1:8446") as console:
        started = console.post("/browser/sessions", json={}, headers={"authorization": "Bearer person-key"})
        assert started.status_code == 200 and "set-cookie" not in started.headers
        sid = started.json()["session_id"]
        assert app.state.browser_sessions[sid]["key"] == "person-key" and app.state.browser_sessions[sid]["console"]
        assert started.json()["cookie"] == app.state.browser_sessions[sid]["cookie"]
        with pytest.raises(WebSocketDisconnect):  # the cookie is still required
            with console.websocket_connect(f"/browser/sessions/{sid}/stream", headers={"cookie": f"voice_{sid}=wrong"}):
                pass


def test_browser_page_is_refused_until_its_account_is_set(tmp_path):
    """Without `browser_key_file` there is no account to be, so nobody is let in."""
    key = tmp_path / "gateway.key"; key.write_text("gateway-key")
    settings = dict(key_file=str(key), turnstile="http://turnstile:8442", compute="http://compute:8442",
                    voice="http://compute:8445", origins=["http://localhost:8446"])
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))))
    with TestClient(app, base_url="http://localhost:8446") as browser:
        refused = browser.post("/browser/sessions", headers={"origin": "http://localhost:8446"}, json={})
        assert refused.status_code == 503 and not app.state.browser_sessions


def test_desktop_key_goes_only_to_turnstile_which_answers_each_turn(tmp_path):
    """A native caller owns its API key; neither cookies nor the app's key replace it. Compute's voice is given only a session token."""
    import asyncio
    import time
    from contextlib import asynccontextmanager

    key = tmp_path / "gateway.key"; key.write_text("app-key")
    requests, frames = [], []

    async def upstream(request):
        requests.append(request)
        if request.method == "DELETE":  # straight to compute's voice, with the session's own token
            assert request.headers["authorization"] == "Bearer session-token"
            return httpx.Response(200, json={"closed": True})
        assert request.headers["authorization"] == "Bearer caller-key"
        if request.url.path == "/turnstile/ready":
            return httpx.Response(200, json={"state": "ready"})
        if request.url.path == "/loadcoach/me":
            return httpx.Response(200, json={"name": "caller", "can": ["front-voice"]})
        if request.url.path == "/voice/sessions":
            return httpx.Response(200, json={"session_id": "s", "conversation_mode": "direct", "model": "small", "token": "session-token"})
        if request.url.path == "/v1/chat/completions" and json.loads(request.content).get("stream"):
            said = {"choices": [{"delta": {"reasoning_content": "secret reasoning", "content": "Hi there."}}]}
            return httpx.Response(200, content="data: " + json.dumps(said) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={})

    class Compute:
        def __init__(self):
            self.events = asyncio.Queue()
        async def recv(self):
            return json.dumps({"type": "session.ready"})
        async def send(self, value):
            frames.append(value)
            if isinstance(value, bytes):
                await self.events.put(json.dumps({"type": "transcript.final", "turn": 1, "text": "Hello"}))
                body = {"model": "small", "stream": True, "messages": [{"role": "user", "content": "Hello"}]}
                await self.events.put(json.dumps({"type": "model.request", "turn": 1, "body": body}))
        async def __aiter__(self):
            while True:
                yield await self.events.get()

    @asynccontextmanager
    async def connector(uri, **options):
        assert options["additional_headers"] == {"authorization": "Bearer session-token"}  # never the caller's key
        yield Compute()

    settings = dict(key_file=str(key), turnstile="http://turnstile:8442", compute="http://compute:8442",
                    voice="http://compute:8445", desktop_port=8447, prompts=str(tmp_path / "no-prompts"))
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), connector)
    with TestClient(app, base_url="http://front:8447") as client:
        assert client.get("/").status_code == 404
        assert client.post("/browser/sessions", json={}).status_code == 404
        for headers in ({}, {"authorization": "Bearer caller-key", "origin": "http://website"}):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/desktop/stream", headers=headers):
                    pass
        with client.websocket_connect("/desktop/stream", headers={"authorization": "Bearer caller-key"}) as socket:
            assert socket.receive_json()["state"] == "checking_key"
            assert socket.receive_json()["type"] == "session.ready"
            socket.send_bytes(bytes(640))
            assert socket.receive_json()["text"] == "Hello"
            deadline = time.monotonic() + 5
            while not any('"agent.text.done"' in frame for frame in frames if isinstance(frame, str)) and time.monotonic() < deadline:
                time.sleep(0.02)
            socket.send_json({"type": "session.cancel"})
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
    assert bytes(640) in frames
    words = [json.loads(frame) for frame in frames if isinstance(frame, str) and "agent.text" in frame]
    assert words == [{"type": "agent.text.delta", "turn": 1, "count": 1, "text": "Hi there."}, {"type": "agent.text.done", "turn": 1, "count": 1}]
    asked = [r for r in requests if r.url.path == "/v1/chat/completions" and json.loads(r.content).get("stream")]
    assert len(asked) == 1 and str(asked[0].url).startswith("http://turnstile:8442/") and asked[0].headers["x-loadcoach-trace"]
    assert any(r.method == "DELETE" and r.url.path.startswith("/voice/sessions/") for r in requests)
    assert not any(r.url.path.endswith("sign-out") or r.url.path.endswith("sign-in") for r in requests)
    with TestClient(app, base_url="http://public:8081") as public:
        with pytest.raises(WebSocketDisconnect):
            with public.websocket_connect("ws://public:8081/desktop/stream", headers={"authorization": "Bearer caller-key"}):
                pass


def test_desktop_refused_key_never_prepares_a_session(tmp_path):
    key = tmp_path / "gateway.key"; key.write_text("app-key")
    paths = []

    async def upstream(request):
        paths.append((request.method, request.url.path))
        return httpx.Response(401, json={})

    settings = dict(key_file=str(key), turnstile="http://turnstile:8442", compute="http://compute:8442",
                    voice="http://compute:8445", desktop_port=8447, prompts=str(tmp_path / "no-prompts"))
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    with TestClient(app, base_url="http://front:8447") as client:
        with client.websocket_connect("/desktop/stream", headers={"authorization": "Bearer wrong"}) as socket:
            assert socket.receive_json()["state"] == "checking_key"
            assert socket.receive_json() == {"type": "session.error", "code": "unauthorized"}
    assert not any(method == "POST" for method, _ in paths)


def test_browser_revoked_key_ends_active_direct_session(tmp_path):
    """The server-held browser key is checked again during a live call without a real voice backend."""
    import asyncio
    from contextlib import asynccontextmanager

    app_key = tmp_path / "gateway.key"; app_key.write_text("app-key")
    browser_key = tmp_path / "browser.key"; browser_key.write_text("browser-key")
    revoked, paths = [False], []

    async def upstream(request):
        paths.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/turnstile/ready":
            assert request.headers["authorization"] == "Bearer app-key"
            return httpx.Response(200, json={"state": "ready"})
        if request.url.path.startswith("/voice/sessions/") and request.method == "DELETE":
            assert request.headers["authorization"] == "Bearer session-token"
            return httpx.Response(200, json={})
        assert request.headers["authorization"] == "Bearer browser-key"
        if request.url.path == "/voice/sessions" and request.method == "POST":
            return httpx.Response(200, json={"model": "allowed-model", "token": "session-token"})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": []})
        if request.url.path == "/loadcoach/me":
            return httpx.Response(401 if revoked[0] else 200,
                                  json={} if revoked[0] else {"name": "voice-browser", "can": ["front-voice"]})
        raise AssertionError(f"unexpected upstream request: {request.method} {request.url.path}")

    class Compute:
        async def recv(self):
            return json.dumps({"type": "session.ready"})

        async def send(self, value):
            pass

        async def __aiter__(self):
            while True:
                await asyncio.sleep(1)
                yield json.dumps({"type": "session.state", "state": "listening"})

    @asynccontextmanager
    async def connector(uri, **options):
        assert options["additional_headers"] == {"authorization": "Bearer session-token"}
        yield Compute()

    settings = dict(key_file=str(app_key), browser_key_file=str(browser_key),
                    turnstile="http://turnstile:8442", voice="http://compute:8445",
                    origins=["https://private.test"], modes=["direct"],
                    prompts=str(tmp_path / "no-prompts"))
    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), connector)
    with TestClient(app, base_url="https://private.test:8446") as client:
        started = client.post("/browser/sessions", json={}, headers={"origin": "https://private.test"})
        assert started.status_code == 200
        sid = started.json()["session_id"]
        assert "browser-key" not in started.text + started.headers.get("set-cookie", "")
        cookie = started.headers["set-cookie"].split(";", 1)[0]
        with client.websocket_connect(f"/browser/sessions/{sid}/stream",
                                      headers={"origin": "https://private.test", "cookie": cookie}) as socket:
            assert socket.receive_json()["type"] == "session.ready"
            revoked[0] = True
            seen = []
            with pytest.raises(WebSocketDisconnect):
                while True:
                    seen.append(socket.receive_json())
            assert {"type": "session.error", "code": "unauthorized"} in seen, seen[-5:]
        assert sid not in app.state.browser_sessions
    assert any(path == "/loadcoach/me" and auth == "Bearer browser-key" for _, path, auth in paths)
    assert sum(path == "/v1/chat/completions" for _, path, _ in paths) == 1  # startup probe only


def test_greeting_pacing_has_bounded_lead_without_accumulated_send_delays(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from voice.gateway import prompt_audio

    now, packets = [10.0], []
    async def sleep(delay):
        now[0] += delay
    async def output(pcm):
        packets.append((now[0], pcm))
        now[0] += .002  # socket/CPU overhead must not lengthen each 20 ms packet
    monkeypatch.setattr("voice.gateway.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("voice.gateway.asyncio", SimpleNamespace(sleep=sleep))
    pcm = b"\x10\x27" * 120000  # five seconds at 24 kHz
    asyncio.run(prompt_audio(pcm, output))
    assert b"".join(packet for _, packet in packets) == pcm
    assert now[0] == pytest.approx(15.0)  # old relative sleeps took 5.5 seconds
    assert packets[5][0] < 10.02  # initial burst gives clients time to buffer
    for index, (at, packet) in enumerate(packets):
        assert 0 <= 10 + index * .02 - at + .002 <= .122001
        assert len(packet) <= 960


def test_a_browser_call_in_a_soundroom_voice_uses_the_audio_profile_and_the_voice_reference(tmp_path):
    """The chosen voice is fetched with the caller's key and handed to the voice service with the audio profile."""
    import asyncio
    from contextlib import asynccontextmanager

    app_key = tmp_path / "gateway.key"; app_key.write_text("app-key")
    browser_key = tmp_path / "browser.key"; browser_key.write_text("browser-key")
    sessions, ready = [], []

    async def upstream(request):
        if request.url.path == "/turnstile/ready":
            ready.append(request.url.params["profile"])
            return httpx.Response(200, json={"state": "ready"})
        if request.url.path.startswith("/voice/sessions/") and request.method == "DELETE":
            return httpx.Response(200, json={})
        assert request.headers["authorization"] == "Bearer browser-key"
        if request.url.path == '/voice/options':
            return httpx.Response(200, json={'models': ['allowed-model'], 'profiles': []})
        if request.url.path == "/soundroom/voices":
            return httpx.Response(200, json={"voices": [{"id": "voice_abc", "name": "Narrator", "mine": True, "description": ""}]})
        if request.url.path == "/soundroom/voices/voice_abc/reference":
            return httpx.Response(200, json={"audio": "UklGRg==", "text": "Hello there."})
        if request.url.path == "/voice/sessions" and request.method == "POST":
            sessions.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "allowed-model", "token": "session-token"})
        if request.url.path == "/v1/chat/completions":
            if not json.loads(request.content).get("stream"):
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"Hi there."}}]}\n\ndata: [DONE]\n\n',
                                  headers={"content-type": "text/event-stream"})
        if request.url.path == "/loadcoach/conversations/spoken":
            kept.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "7"})
        if request.url.path == "/loadcoach/me":
            return httpx.Response(200, json={"name": "voice-browser", "can": ["front-voice"]})
        raise AssertionError(f"unexpected upstream request: {request.method} {request.url.path}")

    kept, answered = [], asyncio.Event()

    class Compute:
        async def recv(self):
            return json.dumps({"type": "session.ready"})

        async def send(self, value):
            if isinstance(value, str) and json.loads(value).get("type") == "agent.text.done":
                answered.set()

        async def __aiter__(self):
            if voice_chosen:
                yield json.dumps({"type": "model.request", "turn": 1, "body": {"model": "allowed-model", "stream": True,
                                                                               "messages": [{"role": "user", "content": "Hello?"}]}})
                await asyncio.wait_for(answered.wait(), 5)
                await asyncio.sleep(.2)

    @asynccontextmanager
    async def connector(uri, **options):
        yield Compute()

    base = dict(key_file=str(app_key), browser_key_file=str(browser_key), turnstile="http://turnstile:8442", voice="http://compute:8445",
                origins=["https://private.test"], modes=["direct"], prompts=str(tmp_path / "no-prompts"))
    origin = {"origin": "https://private.test"}
    voice_chosen = False
    plain = build_app(base, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), connector)
    with TestClient(plain, base_url="https://private.test:8446") as client:
        assert client.get("/browser/config").json()["voices"] == []
        assert client.post("/browser/sessions", json={"voice": "voice_abc"}, headers=origin).status_code == 403
    voice_chosen = True
    app = build_app({**base, "soundroom_profile": "soundroom"}, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), connector)
    with TestClient(app, base_url="https://private.test:8446") as client:
        assert client.get("/browser/config").json()["voices"] == [{"id": "voice_abc", "name": "Narrator"}]
        assert client.post("/browser/sessions", json={"voice": "../etc"}, headers=origin).status_code == 403
        started = client.post("/browser/sessions", json={"voice": "voice_abc"}, headers=origin)
        sid, cookie = started.json()["session_id"], started.headers["set-cookie"].split(";", 1)[0]
        with client.websocket_connect(f"/browser/sessions/{sid}/stream", headers={**origin, "cookie": cookie}) as socket:
            assert socket.receive_json()["type"] == "session.ready"
            with pytest.raises(WebSocketDisconnect):
                while True:
                    socket.receive_json()
    assert ready == ["soundroom"]
    assert sessions[0]["profile"] == "soundroom" and sessions[0]["voice"] == {"audio": "UklGRg==", "text": "Hello there."}
    assert kept == [{"conversation": None, "said": "Hello?", "answered": "Hi there.", "model": "allowed-model", "voice": "voice_abc"}]
