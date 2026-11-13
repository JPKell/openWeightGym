"""The front machine's private browser page, which asks for no sign-in, and signed Twilio voice gateway."""

from loadcoach import observability as observation

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
import secrets
import time
from typing import Annotated
from urllib.parse import parse_qs, quote, urlsplit
from xml.sax.saxutils import escape

import httpx
import typer
import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect

from loadcoach.config import CLI, data_folder
from loadcoach.settings import with_changes
from voice import protocol, prompt_recordings, latency
from voice.hermes import Bridge, configuration
from voice.telephone import Conversion, valid, valid_stream
from voice.options import FIELDS as CALL_FIELDS, MAX_CALL_BYTES

START_FAILURES = {'busy', 'unauthorized', 'unavailable', 'invalid_options', 'invalid_voice',
                  'model_unavailable', 'speech_service_permissions', 'mode_unavailable', 'invalid_profile', 'startup_timeout'}


async def prompt_audio(pcm, output):
    """Send 24 kHz greetings with 120 ms of lead and deadlines that do not accumulate drift."""
    began = time.monotonic()
    for offset in range(0, len(pcm), 960):
        await asyncio.sleep(max(0, began + offset / 48000 - .12 - time.monotonic()))
        await output(pcm[offset:offset + 960])
    # Keep the prompt's completion ordered after its nominal playback duration.
    await asyncio.sleep(max(0, began + len(pcm) / 48000 - time.monotonic()))


def may(person, mode):
    """Check whether the account has the requested front voice ability."""
    abilities = person.get("can", [])
    return "superuser" in abilities or "front-voice" in abilities


RECORDING_BYTES = 16000 * 2 * 60 * 30  # thirty minutes of the caller's 16 kHz sound; the assistant's 24 kHz side gets the same time
KEY_CHECK_SECONDS = 10  # how often a live call asks Turnstile whether its key is still allowed


def build_app(settings, client=None, connector=connect, following=None, prompt_cache=None):
    """Build both source adapters; private browser routes never trust proxy headers."""
    http = client or httpx.AsyncClient(timeout=65)
    app_key = Path(settings["key_file"]).read_text().strip()
    turnstile = settings["turnstile"].rstrip("/")
    voice = settings["voice"].rstrip("/")
    voice_door = settings.get("voice_turnstile", turnstile.rsplit(":", 1)[0] + ":8445")
    browser_sessions, phone_calls = {}, {}
    sockets = set()
    desktop_sessions = set()
    assets = Path(__file__).with_name("static")
    prompts = Path(settings.get("prompts", str(Path(__file__).with_name("prompts"))))
    cache_lock = asyncio.Lock()
    latencies = latency.Recent()
    from loadcoach.client_origin import ClientOrigin, forward as forward_origin

    async def carry_context(request):
        """Propagate context only to configured openWeightGym doors, never arbitrary external services."""
        allowed = {urlsplit(turnstile).netloc, urlsplit(voice_door).netloc}
        if request.url.netloc.decode() not in allowed:
            return
        if current := observation.CURRENT.get():
            request.headers['traceparent'] = '00-'+current.root.trace_id+'-'+current.span_id+'-01'
        if settings.get('context_key_file'):
            await forward_origin(request, Path(settings['context_key_file']).read_text().strip(), 'voice-gateway')
    http.event_hooks.setdefault('request', []).append(carry_context)

    def origin_ok(request):
        """Check whether a browser WebSocket came from an allowed origin."""
        return request.headers.get("origin", "") in settings.get("origins", [])

    async def ask(method, url, key, no_wake=False, **kwargs):
        """Send a private gateway request through the configured openWeightGym client."""
        return await http.request(method, url, headers={"authorization": "Bearer " + key,
            **({'x-turnstile-no-wake': '1'} if no_wake else {})}, **kwargs)

    async def reap():
        """Remove expired browser sessions and release their reserved resources."""
        while True:
            await asyncio.sleep(10)
            for sid, item in list(browser_sessions.items()):
                if not item.get("connected") and time.monotonic() - item["created"] > 60:
                    browser_sessions.pop(sid, None)
            for call, item in list(phone_calls.items()):
                if time.monotonic() - item["created"] > 1800:
                    phone_calls.pop(call, None)

    async def refresh_prompts():
        """Keep offline greetings in sync with the selected voice, through turnstile without waking compute."""
        while True:
            try:
                async with cache_lock:
                    await prompt_recordings.refresh(prompt_cache, http, voice_door, app_key, settings.get("profile", "default"))
            except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError):
                pass  # keep the last complete recordings and retry when compute is available
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(app):
        """Start background workers and release service resources on shutdown."""
        task = asyncio.create_task(reap())
        recordings = asyncio.create_task(refresh_prompts()) if prompt_cache is not None else None
        # Settings changed on the site are fetched from LoadCoach, through turnstile, and put into `settings`: see loadcoach/settings.py.
        in_step = asyncio.create_task(following(http, turnstile, app_key)) if following else None
        yield
        task.cancel()
        if recordings:
            recordings.cancel()
            with suppress(asyncio.CancelledError):
                await recordings
        if in_step:
            in_step.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for socket in list(sockets):
            with suppress(Exception):
                await socket.close()
        if client is None:
            await http.aclose()
        await observation.shutdown(app)

    async def page(request):
        """Render the private browser voice client page."""
        return FileResponse(assets / "index.html", headers={"cache-control": "no-store"})

    async def start(request):
        """Reserve one browser connection. A browser here signs in as the one account in `browser_key_file`.

        WeightRoom's voice page reserves for its signed-in person instead, sending their key: the
        socket then comes from WeightRoom, not a browser, so it gets the cookie and needs no Origin.
        """
        scheme, _, console = request.headers.get("authorization", "").partition(" ")
        console = console if scheme.lower() == "bearer" else ""
        if not console and not origin_ok(request):
            return JSONResponse({"error": "Origin refused"}, status_code=403)
        try:
            if len(await request.body()) > MAX_CALL_BYTES:
                return JSONResponse({'error': 'Voice settings are too large.'}, status_code=413)
            asked = await request.json()
            mode, voice, record = asked.get("conversation_mode", "direct"), asked.get("voice") or None, asked.get("record", False) is True
            profile_name, options = asked.get('profile') or None, asked.get('options', {})
            key = console or Path(settings["browser_key_file"]).read_text().strip()
        except (ValueError, KeyError, AttributeError, OSError):
            return JSONResponse({"error": "Voice is unavailable"}, status_code=503)
        if mode not in settings.get("modes", ["direct"]):
            return JSONResponse({"error": "That mode is not available"}, status_code=403)
        if (profile_name is not None and (not isinstance(profile_name, str) or not protocol.ID.fullmatch(profile_name))
                or not isinstance(options, dict) or options.keys() - CALL_FIELDS):
            return JSONResponse({'error': 'Choose valid voice settings.'}, status_code=400)
        if voice is not None and (not settings.get("soundroom_profile") or mode != "direct" or not isinstance(voice, str) or not protocol.ID.fullmatch(voice)):
            return JSONResponse({"error": "That voice is not available"}, status_code=403)
        if len(browser_sessions) >= settings.get("max_starting_sessions", 4) + settings.get("max_active_sessions", 1):
            return JSONResponse({"error": "Voice is busy"}, status_code=429)
        # Nothing is asked of compute here. It is woken, and decides what this account may do, once the socket connects.
        sid, cookie = secrets.token_hex(16), secrets.token_urlsafe(32)
        browser_sessions[sid] = dict(key=key, mode=mode, voice=voice, record=record and voice is not None, cookie=cookie, created=time.monotonic(),
                                     console=bool(console), profile=profile_name, options=options)
        if console:
            return JSONResponse({"session_id": sid, "cookie": cookie}, headers={"cache-control": "no-store"})
        response = JSONResponse({"session_id": sid}, headers={"cache-control": "no-store"})
        response.set_cookie("voice_" + sid, cookie, httponly=True, samesite="strict", max_age=1800,
                            secure=request.headers.get("origin", "").startswith("https://"))
        return response

    async def inbound(request):
        """Check every webhook parameter before reserving a telephone session."""
        phone = settings.get("twilio", {})
        if not phone.get("token_file"):
            return Response(status_code=404)
        body = await request.body()
        if len(body) > 16384:
            return Response(status_code=413)
        params = parse_qs(body.decode(), keep_blank_values=True)
        token = Path(phone["token_file"]).read_text().strip()
        if not valid(token, phone["webhook_url"], params, request.headers.get("x-twilio-signature", "")):
            print(json.dumps({"event": "voice.phone.rejected", "stage": "webhook", "reason": "invalid_signature"}), flush=True)
            return Response(status_code=403)
        call = params.get("CallSid", [""])[0]
        if not protocol.ID.fullmatch(call) or params.get("AccountSid", [""])[0] != phone["account_sid"]:
            return Response(status_code=403)
        if call not in phone_calls:
            if sum(not p.get("closed") for p in phone_calls.values()) >= 16 or len(phone_calls) >= 1024:
                return Response(status_code=429)
            phone_calls[call] = dict(id=secrets.token_hex(16), nonce=secrets.token_urlsafe(32),
                                    created=time.monotonic(), connected=False, caller=params.get("From", [""])[0])
        item = phone_calls[call]
        xml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="' + escape(phone["stream_url"], {'"': '&quot;'}) + '">'
               '<Parameter name="session" value="' + item["id"] + '"/><Parameter name="nonce" value="' + item["nonce"] + '"/>'
               '</Stream></Connect></Response>')
        return Response(xml, media_type="application/xml")

    async def controller(socket, sid, key, mode, source, stream_sid=None, voice_id=None, record=False, chosen_profile=None, options=None):
        """Run one call with bounded source queues, startup, prompts and cleanup.

        A browser caller may choose a SoundRoom voice: the call then uses `soundroom_profile`, whose
        speech is audio.cpp, and is spoken in that voice. Such a call's exchanges are kept as a
        conversation in WeightRoom, and with `record` both sides' sound are kept as SoundRoom recordings.
        """
        profile_name = chosen_profile or (settings["soundroom_profile"] if voice_id and settings.get("soundroom_profile") else settings.get("profile", "default"))
        queue = asyncio.Queue(maxsize=25)
        queued_bytes = 0
        conversion = Conversion(cancel_echo=source == "twilio")
        marks, segment, turn, segment_played = {}, None, None, 0
        next_mark = 0
        send_lock = asyncio.Lock()
        upstream = None
        bridge = None
        agent_task = None
        active = False
        media_clock = None
        stopped_turns = set()
        delivered_segments = {}
        playback_times = {}
        timed_turn = None
        token = ""  # the voice session's own, for its sound socket, given when it is made
        session_voice = voice  # the node the session started on, which keeps it until it ends; the front says which
        conversation = None  # the WeightRoom conversation a SoundRoom call's exchanges are kept in
        heard_pcm, said_pcm = bytearray(), bytearray()  # both sides' sound, kept only when recording

        async def send(value):
            """Send one serialized voice event to the connected client."""
            async with send_lock:
                async with asyncio.timeout(2):
                    if isinstance(value, bytes):
                        await socket.send_bytes(value)
                    else:
                        await socket.send_json(value)

        def measure(number, values, log=False):
            """Merge bounded per-turn latency observations for the active session."""
            row = latencies.update(sid, number, source, mode, values)
            if log and row:
                print(json.dumps(dict(event="voice.latency", **row)), flush=True)

        async def output(value):
            """Send ordered PCM frames to the current client's playback transport."""
            nonlocal segment, turn, segment_played, next_mark, timed_turn
            if record and isinstance(value, bytes) and len(said_pcm) < RECORDING_BYTES * 3 // 2:
                said_pcm.extend(value)
            if isinstance(value, dict):
                value = dict(value)
                measured = value.pop('latency', {})
                number = value.get('turn')
                if value['type'] == 'session.ready' and source in ('browser','desktop'):
                    value['queue_clear_ack'] = True
                if value['type'] == 'turn.started' and type(number) is int:
                    timed_turn = number
                    if number > 0:
                        playback_times[number] = {}
                        for old in list(playback_times):
                            if old < number-31:
                                del playback_times[old]
                if isinstance(measured, dict) and measured:
                    measure(number, measured, value['type'] in ('turn.completed','turn.interrupted','session.error'))
                if value['type'] == 'playback.clear' and number in playback_times:
                    playback_times[number].setdefault('clear',time.monotonic())
            elif timed_turn in playback_times:
                playback_times[timed_turn].setdefault('audio',time.monotonic())
            if isinstance(value, dict) and value["type"] == "playback.segment" and value["turn"] >= 0:
                delivered_segments[value["segment"]] = {**value, "acknowledged": False}
            if source in ("browser", "desktop"):
                await send(value)
                return
            if isinstance(value, bytes):
                if turn in stopped_turns or segment is None:
                    return
                ulaw = conversion.send(value)
                await send(dict(event="media", streamSid=stream_sid, media={"payload": base64.b64encode(ulaw).decode()}))
                segment_played += len(value) // 2
                next_mark += 1
                mark = str(next_mark)
                marks[mark] = dict(type="playback.progress", turn=turn, segment=segment, samples=segment_played)
                if len(marks) > 60:
                    raise ValueError("Telephone playback stalled")
                await send(dict(event="mark", streamSid=stream_sid, mark={"name": mark}))
            elif value["type"] == "playback.clear":
                stopped_turns.add(value["turn"])
                marks.clear()  # returned marks after clear do not mean heard
                segment = None
                conversion.clear()
                await send(dict(event="clear", streamSid=stream_sid))
            elif value["type"] == "turn.started":
                turn = value["turn"]
            elif value["type"] == "playback.segment":
                turn, segment, segment_played = value["turn"], value["segment"], 0

        async def prompt(name):
            """Play the selected greeting or control prompt through the active transport."""
            pcm = prompt_cache.audio.get(name) if prompt_cache is not None else None
            if pcm is None:
                path = prompts / (name + ".pcm")
                if not path.exists():
                    return
                pcm = path.read_bytes()
            await output(dict(type="turn.started", turn=-1))
            stopped_turns.discard(-1)
            await output(dict(type="playback.segment", turn=-1, segment=-1, text="", samples=len(pcm) // 2))
            await prompt_audio(pcm, output)
            await output(dict(type="turn.completed", turn=-1))
            await asyncio.sleep(.15)

        def acknowledged(event):
            """Record client playback progress without treating it as physical audio proof."""
            segment = delivered_segments.get(event.get("segment"))
            number, played = event.get('turn'), event.get('samples')
            clocks = playback_times.get(number) if type(number) is int else None
            if (segment and clocks is not None and number == segment['turn'] and number not in stopped_turns
                    and type(played) is int and 0 < played <= segment['samples'] and 'audio' in clocks
                    and 'ack' not in clocks):
                clocks['ack'] = time.monotonic()
                measure(number, {'audio_to_ack_seconds':clocks['ack']-clocks['audio']}, log=True)
            if bridge and segment and not segment["acknowledged"] and event.get("turn") == segment["turn"] and event.get("samples") == segment["samples"] and segment["turn"] not in stopped_turns:
                segment["acknowledged"] = True
                bridge.delivery.setdefault(segment["turn"], []).append(segment["text"])

        def enqueue(item):
            """Cap sound at 500 ms even when a source sends larger frames."""
            nonlocal queued_bytes
            size = len(item) if isinstance(item, bytes) else 0
            if record and size and len(heard_pcm) < RECORDING_BYTES:
                heard_pcm.extend(item)
            if queued_bytes + size > 16000:
                raise ValueError("Input sound is arriving too quickly")
            queue.put_nowait(item)
            queued_bytes += size

        async def receive():
            """Read while starting as well as while speaking; never accumulate early audio."""
            nonlocal media_clock
            while True:
                value = await socket.receive()
                if value["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect()
                if source in ("browser", "desktop"):
                    data = value.get("bytes")
                    if data is not None:
                        protocol.check_audio(data)
                        if active:
                            enqueue(data)
                    else:
                        event = protocol.message(value["text"])
                        if event["type"] == "session.cancel" and not event.get("interrupt"):
                            return
                        if event["type"] == 'playback.cleared':
                            number = event.get('turn')
                            clocks = playback_times.get(number) if type(number) is int else None
                            if clocks is not None and 'clear' in clocks and 'cleared' not in clocks:
                                clocks['cleared'] = time.monotonic()
                                measure(number, {'clear_ack_seconds':clocks['cleared']-clocks['clear']}, log=True)
                            continue  # queue state is not heard speech and never enters compute's history
                        if event["type"] == "playback.progress":
                            acknowledged(event)
                        if active and not (event["type"] == "playback.progress" and event.get("turn", 0) < 0):
                            enqueue(event)
                else:
                    event = json.loads(value["text"])
                    if event.get("streamSid") != stream_sid:
                        raise ValueError("Wrong stream")
                    if event["event"] == "stop":
                        return
                    if event["event"] == "media":
                        payload = base64.b64decode(event["media"]["payload"], validate=True)
                        if not 0 < len(payload) <= 800:
                            raise ValueError("Oversized telephone frame")
                        timestamp = int(event["media"]["timestamp"]) / 1000
                        if media_clock is None:
                            media_clock = time.monotonic() - timestamp - len(payload) / 8000
                        pcm = conversion.receive(payload, at=media_clock + timestamp)
                        if active and pcm:
                            enqueue(pcm)
                    elif event["event"] == "mark":
                        report = marks.pop(event["mark"]["name"], None)
                        if active and report and report["turn"] >= 0:
                            acknowledged(report)
                            enqueue(report)

        async def prepare():
            """Authorize and prepare the requested compute voice session."""
            nonlocal bridge, token, session_voice
            agent_settings = configuration(settings) if mode == "hermes" and settings.get("hermes") else None
            agent_model = {"agent_model": agent_settings["model"]} if agent_settings else {}
            readiness = await ask("GET", turnstile + "/turnstile/ready", key if source == "desktop" else app_key,
                                  params={"conversation_mode": mode, "profile": profile_name, **agent_model}, timeout=2)
            if not readiness.is_success:
                raise ValueError("unauthorized" if readiness.status_code in (401, 403) else "unavailable")
            state = readiness.json().get("state")
            greeting = asyncio.create_task(prompt("connecting" if state == "ready" else "preparing"))
            try:
                if source in ("desktop", "browser", "twilio"):
                    # Turnstile can wake only for this caller's previously accepted key.
                    # Never sign it out: a desktop API key belongs to its owner.
                    result = await ask("GET", turnstile + "/loadcoach/me", key)
                    if result.status_code in (401, 403):
                        raise ValueError("unauthorized")
                    if not result.is_success:
                        raise ValueError("unavailable")
                    if not may(result.json(), mode):
                        raise ValueError("unauthorized")
                    from types import SimpleNamespace
                    info = result.json()
                    observation.identify(SimpleNamespace(identity=info.get('identity'), name=info['name'],
                                                         key_identity=None, can=lambda ability:ability in info['can']))
                if mode == "hermes":
                    if not agent_settings:
                        raise ValueError("Hermes unavailable")
                    bridge = Bridge(http, agent_settings, sid)
                    await bridge.ready()
                chosen_voice = {}
                if voice_id:
                    reference = await ask("GET", turnstile + "/soundroom/voices/" + quote(voice_id, safe="") + "/reference", key, timeout=10)
                    if not reference.is_success:
                        raise ValueError("unauthorized" if reference.status_code in (401, 403, 404) else "unavailable")
                    chosen_voice = {"voice": reference.json()}
                preparing = asyncio.create_task(ask("POST", voice_door + "/voice/sessions", key, json={
                    "session_id": sid, "source": source, "profile": profile_name,
                    "conversation_mode": mode, "max_wait": 60, **agent_model, **chosen_voice,
                    **({'options': options} if options else {})}))
                try:
                    while not preparing.done():
                        done, _ = await asyncio.wait({preparing}, timeout=10)
                        if not done:
                            await greeting
                            await prompt("waiting")
                    result = await preparing
                finally:
                    if not preparing.done():
                        preparing.cancel()
                if not result.is_success:
                    failure = result.json().get('error')
                    code = failure.get('code') if isinstance(failure, dict) else None
                    raise ValueError(code if code in START_FAILURES else "busy" if result.status_code == 429 else "unavailable")
                token = result.json()["token"]
                session_voice = (result.json().get("voice_url") or voice).rstrip("/")
                if mode == "direct":
                    # Exercise the chosen model with the caller's own key, as every turn will.
                    tried = await ask("POST", turnstile + "/v1/chat/completions", key, json={
                        "model": result.json()["model"], "messages": [{"role": "user", "content": "Say ready."}],
                        "max_tokens": 2, "chat_template_kwargs": {"enable_thinking": False}})
                    if not tried.is_success:
                        raise ValueError("unauthorized" if tried.status_code in (401, 403) else "unavailable")
                await greeting
            finally:
                if not greeting.done():
                    greeting.cancel()

        async def agent_answer(event):
            """Request a bounded Hermes answer and forward its spoken final response."""
            async def progress(state):
                """Forward bounded Hermes progress events to the current voice client."""
                if source in ("browser", "desktop"):
                    await output(dict(type="session.state", state=state))
            try:
                text = await bridge.answer(event["text"], progress, event["turn"])
                text = protocol.speakable(text)[:1500]
                if event["turn"] in stopped_turns:
                    return
                # Keep agent messages bounded too.
                count = 0
                for i in range(0, len(text), 1000):
                    count += 1
                    await upstream.send(json.dumps(dict(type="agent.text.delta", turn=event["turn"], count=count, text=text[i:i + 1000])))
                await upstream.send(json.dumps(dict(type="agent.text.done", turn=event["turn"], count=count)))
            except asyncio.CancelledError:
                raise
            except Exception:
                await upstream.send(json.dumps(dict(type="agent.error", turn=event["turn"], code="agent_failed")))
                await prompt("unavailable")

        async def run():
            """Run the active gateway voice conversation until completion or cancellation."""
            nonlocal upstream, active, agent_task
            async with asyncio.timeout(settings.get("startup_seconds", 60)) as startup:
                await prepare()
                uri = session_voice.replace("http://", "ws://").replace("https://", "wss://") + "/voice/stream?session_id=" + sid
                # The session's own token, not the caller's key: the key goes only to turnstile.
                async with connector(uri, additional_headers={"authorization": "Bearer " + token},
                                     max_size=protocol.ASK_LIMIT, max_queue=4, write_limit=4096, open_timeout=5) as connected:
                    upstream = connected
                    await upstream.send(json.dumps(protocol.start(sid, mode)))
                    first = protocol.message(await upstream.recv())
                    if first["type"] != "session.ready":
                        raise ValueError("Voice did not become ready")
                    await prompt("ready")
                    await output(first)
                    active = True
                    startup.reschedule(asyncio.get_running_loop().time() + 1800)
                    await pump()

        async def model_answer(event):
            """Link one model call to context supplied by the authenticated private voice stream."""
            with observation.remote(event.get('observation'), 'voice_model'):
                return await model_answer_turn(event)

        async def model_answer_turn(event):
            """Ask the model for one turn of a direct conversation, through turnstile with the caller's key, and hand voice its words as they come."""
            count, words = 0, []
            try:
                async with http.stream("POST", turnstile + "/v1/chat/completions", json=event["body"], timeout=120,
                                       headers={"authorization": "Bearer " + key, "X-LoadCoach-Trace": sid}) as response:
                    if not response.is_success:
                        raise ValueError("model_failed")
                    async for line in response.aiter_lines():
                        if not line.startswith("data: ") or line[6:] == "[DONE]":
                            continue
                        for choice in json.loads(line[6:]).get("choices", []):
                            if text := choice.get("delta", {}).get("content"):
                                count += 1
                                words.append(text[:2000])
                                await upstream.send(json.dumps(dict(type="agent.text.delta", turn=event["turn"], count=count, text=text[:2000])))
                await upstream.send(json.dumps(dict(type="agent.text.done", turn=event["turn"], count=count)))
                if voice_id:
                    await keep_exchange(event["body"], "".join(words))
            except asyncio.CancelledError:
                raise
            except Exception:
                await upstream.send(json.dumps(dict(type="agent.error", turn=event["turn"], code="model_failed")))
                await prompt("unavailable")

        async def keep_exchange(body, answered):
            """Keep a SoundRoom call's finished exchange in WeightRoom, with the caller's key. A failure is logged, never spoken."""
            nonlocal conversation
            said = next((one.get("content") for one in reversed(body.get("messages", [])) if one.get("role") == "user"), "")
            if not isinstance(said, str) or not said.strip():
                return
            try:
                kept = await ask("POST", settings.get("conversations_url", "http://127.0.0.1:8440") + "/loadcoach/conversations/spoken", key, timeout=5,
                                 json={"conversation": conversation, "said": said, "answered": protocol.speakable(answered), "model": body.get("model", ""), "voice": voice_id})
                conversation = kept.json()["id"] if kept.status_code == 201 else conversation
            except (httpx.HTTPError, ValueError, KeyError):
                print(json.dumps({"event": "voice.conversation_not_kept", "session": sid}), flush=True)

        async def keep_recordings():
            """Keep both sides of a recorded call as SoundRoom recordings, as Opus to stay small."""
            for pcm, rate, side in ((heard_pcm, 16000, "caller"), (said_pcm, 24000, "assistant")):
                if not pcm:
                    continue
                try:
                    process = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
                                                                   "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
                                                                   stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    encoded, _ = await process.communicate(bytes(pcm))
                    name = f"spoken-chat-{time.strftime('%Y%m%d-%H%M%S')}-{side}.ogg"
                    await ask("POST", turnstile + "/soundroom/assets", key, timeout=30,
                              json={"b64_audio": base64.b64encode(encoded).decode(), "filename": name, "kind": "recording"})
                except (OSError, httpx.HTTPError):
                    print(json.dumps({"event": "voice.recording_not_kept", "session": sid, "side": side}), flush=True)

        async def still_allowed():
            """A key that is taken away or disabled ends the call. Turnstile is asked every so often, from the accounts."""
            while True:
                await asyncio.sleep(KEY_CHECK_SECONDS)
                try:
                    me = await ask("GET", turnstile + "/loadcoach/me", key, timeout=5)
                except httpx.HTTPError:
                    continue  # turnstile itself is down: each turn's request finds that out
                if me.status_code in (401, 403) or (me.is_success and not may(me.json(), mode)):
                    raise ValueError("unauthorized")

        async def pump():
            """Bridge client and compute voice events with shared cancellation and cleanup."""
            nonlocal agent_task
            async def to_compute():
                """Forward validated client audio and controls to the compute session."""
                nonlocal queued_bytes
                while True:
                    item = await queue.get()
                    if isinstance(item, bytes):
                        queued_bytes -= len(item)
                    await upstream.send(item if isinstance(item, bytes) else json.dumps(item))

            async def from_compute():
                """Forward compute audio and session events to the connected client."""
                nonlocal agent_task
                async for value in upstream:
                    if isinstance(value, bytes):
                        await output(value)
                        continue
                    event = protocol.message(value, protocol.ASK_LIMIT)
                    asking = event["type"] == "model.request" if mode == "direct" else event["type"] == "transcript.final"
                    if asking:
                        if agent_task and not agent_task.done():
                            agent_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await asyncio.wait_for(agent_task, 12)
                        agent_task = asyncio.create_task(model_answer(event) if mode == "direct" else agent_answer(event))
                    if event["type"] == "model.request":
                        continue  # what to ask, with the conversation so far: for the model, never for the caller's screen
                    if event["type"] == "playback.clear":
                        stopped_turns.add(event["turn"])
                    await output(event)
                    if event["type"] == "turn.interrupted" and agent_task:
                        agent_task.cancel()
                    elif event["type"] == "session.error":
                        await prompt("unavailable")
            sender = asyncio.create_task(to_compute())
            receiver = asyncio.create_task(from_compute())
            watching = asyncio.create_task(still_allowed())
            try:
                done, _ = await asyncio.wait({sender, receiver, watching}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                for task in (sender, receiver, watching):
                    task.cancel()
                await asyncio.gather(sender, receiver, watching, return_exceptions=True)

        sockets.add(socket)
        listener = asyncio.create_task(receive())
        running = asyncio.create_task(run())
        try:
            done, _ = await asyncio.wait({listener, running}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except (Exception, asyncio.CancelledError) as problem:
            if running.done() and not listener.done():
                with suppress(Exception):
                    if source in ("browser", "desktop"):
                        code = str(problem) if str(problem) in START_FAILURES else "unavailable"
                        await output(dict(type="session.error", code=code))
                    await output(dict(type="playback.clear", turn=turn or 0))
                    await prompt("busy" if str(problem) == "busy" else "unavailable")
        finally:
            for task in (listener, running, agent_task):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(listener, running, *([agent_task] if agent_task else []), return_exceptions=True)
            with suppress(Exception):
                if token:
                    await ask("DELETE", session_voice + "/voice/sessions/" + sid, token, timeout=2)
            if record:
                with suppress(Exception):
                    await keep_recordings()
            if bridge:
                with suppress(Exception):
                    await bridge.close()
            sockets.discard(socket)
            with suppress(Exception):
                await socket.close()

    async def browser(socket):
        """Authenticate and serve the selected private browser voice WebSocket."""
        sid = socket.path_params["sid"]
        item = browser_sessions.get(sid)
        if socket.scope["server"][1] == settings.get("public_port", 8081) or not item or item.get("connected") or not (item["console"] or origin_ok(socket)) or not secrets.compare_digest(socket.cookies.get("voice_" + sid, ""), item["cookie"]):
            await socket.close(code=1008)
            return
        item["connected"] = True
        await socket.accept()
        try:
            await controller(socket, sid, item["key"], item["mode"], "browser", voice_id=item.get("voice"), record=item.get("record", False),
                             chosen_profile=item.get('profile'), options=item.get('options'))
        finally:
            browser_sessions.pop(sid, None)

    async def desktop(socket):
        """Admit one native PCM socket with the owner's API key, never a browser cookie."""
        scheme, _, key = socket.headers.get("authorization", "").partition(" ")
        mode = socket.query_params.get("conversation_mode", "direct")
        voice_id = socket.query_params.get("voice") or None  # a SoundRoom voice of the key's account, when the gateway offers them
        if (not settings.get("desktop_port") or "origin" in socket.headers or
                scheme.lower() != "bearer" or not 1 <= len(key) <= 512 or
                mode not in settings.get("modes", ["direct"]) or
                voice_id and (not settings.get("soundroom_profile") or mode != "direct" or not protocol.ID.fullmatch(voice_id))):
            await socket.close(code=1008)
            return
        if len(desktop_sessions) + len(browser_sessions) >= settings.get("max_starting_sessions", 4) + settings.get("max_active_sessions", 1):
            await socket.close(code=1013)
            return
        sid = secrets.token_hex(16)
        desktop_sessions.add(sid)
        await socket.accept()
        try:
            await socket.send_json(dict(type="session.state", state="checking_key"))
            await controller(socket, sid, key, mode, "desktop", voice_id=voice_id)
        finally:
            desktop_sessions.discard(sid)

    async def telephone(socket):
        """Validate the signed Twilio stream and bridge its telephone audio."""
        phone = settings.get("twilio", {})
        if not phone.get("token_file"):
            await socket.close(code=1008)
            return
        token = Path(phone["token_file"]).read_text().strip()
        if not valid_stream(token, phone["stream_url"], socket.headers.get("x-twilio-signature", "")):
            print(json.dumps({"event": "voice.phone.rejected", "stage": "upgrade", "reason": "invalid_signature"}), flush=True)
            await socket.close(code=1008)
            return
        await socket.accept()
        call = None
        claimed = False
        try:
            async with asyncio.timeout(5):
                event = json.loads(await socket.receive_text())
                if event.get("event") == "connected":
                    event = json.loads(await socket.receive_text())
                start = event["start"]
                call = start["callSid"]
                item = phone_calls.get(call)
                custom = start.get("customParameters", {})
                if not item or item["connected"] or custom.get("session") != item["id"] or not secrets.compare_digest(custom.get("nonce", ""), item["nonce"]):
                    raise ValueError("Unexpected call")
                if start.get("accountSid") != phone["account_sid"] or start.get("mediaFormat") != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}:
                    raise ValueError("Unexpected telephone format")
                stream_sid = start["streamSid"]
                item["connected"] = True
                claimed = True
                print(json.dumps({"event": "voice.phone.connected", "call": call}), flush=True)
            phone_voice = phone.get("voice") if settings.get("soundroom_profile") and phone.get("mode", "direct") == "direct" else None
            await controller(socket, item["id"], Path(phone["key_file"]).read_text().strip(), phone.get("mode", "direct"), "twilio", stream_sid,
                             voice_id=phone_voice)
        except (ValueError, KeyError, TypeError, TimeoutError, WebSocketDisconnect):
            if not claimed:
                print(json.dumps({"event": "voice.phone.rejected", "stage": "start", "reason": "invalid_start"}), flush=True)
            with suppress(Exception):
                await socket.close(code=1008)
        finally:
            if claimed and call in phone_calls:
                phone_calls[call]["closed"] = True  # a replayed webhook cannot repeat a finished call

    async def browser_config(request):
        """Return permitted public browser voice configuration without credentials: modes, and SoundRoom voices if offered.

        The voices are those of the browser account, or of whoever WeightRoom's voice page sends the key of.
        """
        voices, choices, problem = [], {}, None
        scheme, _, console = request.headers.get("authorization", "").partition(" ")
        try:
            key = console if scheme.lower() == "bearer" and console else Path(settings["browser_key_file"]).read_text().strip()
            response = await ask('GET', voice_door + '/voice/options', key, timeout=5, no_wake=True)
            if response.is_success:
                choices = response.json()
            else:
                problem = 'Voice settings are unavailable. Check the speech service and your access.'
            if settings.get("soundroom_profile"):
                listed = await ask("GET", turnstile + "/soundroom/voices", key, timeout=3, no_wake=True)
                voices = [{"id": one["id"], "name": one["name"]} for one in listed.json().get("voices", [])] if listed.is_success else []
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            problem = 'Voice settings could not be read. Check the speech service connection.'
        return JSONResponse({**choices, 'modes': settings.get('modes', ['direct']), 'voices': voices,
                             'default_profile': settings.get('profile', 'default'), 'cloned_profile': settings.get('soundroom_profile'),
                             'configuration_error': problem}, headers={'cache-control': 'no-store'})

    async def admin(request):
        """Private operator controls, authorized afresh by front's existing accounts."""
        scheme, _, key = request.headers.get('authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not key:
            return Response(status_code=401)
        try:
            who = await http.get(
                turnstile + '/loadcoach/me', headers={'authorization': 'Bearer ' + key, 'x-turnstile-no-wake': '1'}, timeout=2)
            if not who.is_success:
                return Response(status_code=who.status_code)
            if not set(who.json().get('can', [])) & {'manage-settings', 'superuser'}:
                return Response(status_code=403)
            headers = {'authorization': 'Bearer ' + key, 'x-turnstile-no-wake': '1'}
            action = request.path_params.get('action', 'status')
            if action == 'status' and request.method == 'GET':
                services = [{'name': 'Voice gateway', 'state': 'Available', 'address': 'front'},
                            {'name': 'Turnstile / accounts', 'state': 'Available', 'address': turnstile}]
                readiness = {}
                try:
                    reply = await http.get(voice_door + '/voice/ready', headers=headers, timeout=3)
                    readiness = reply.json() if reply.is_success else {}
                    services.append({'name': 'Compute voice', 'state': 'Available' if reply.is_success else f'Not ready (HTTP {reply.status_code})', 'address': voice_door})
                except (httpx.HTTPError, ValueError):
                    services.append({'name': 'Compute voice', 'state': 'Unavailable', 'address': voice_door})
                try:
                    reply = await http.get(turnstile + '/loadcoach/status', headers=headers, timeout=2)
                    services.append({'name': 'LoadCoach', 'state': 'Available' if reply.is_success else f'Unavailable (HTTP {reply.status_code})', 'address': turnstile})
                except httpx.HTTPError:
                    services.append({'name': 'LoadCoach', 'state': 'Unavailable', 'address': turnstile})
                hermes = settings.get('hermes', {})
                services.append({'name': 'Hermes', 'state': 'Starts per call' if hermes.get('python') else 'Configured' if hermes else 'Not configured',
                                 'address': hermes.get('url', 'front')})
                return JSONResponse({'services': services, 'readiness': readiness,
                    'active': len(sockets), 'latency': latencies.snapshot(),
                    'modes': settings.get('modes', ['direct']), 'profile': settings.get('profile', 'default'),
                    'recordings': {'source': 'Cached on front' if prompt_cache and prompt_cache.bundle else 'Bundled defaults',
                                   **({key: prompt_cache.bundle[key] for key in ('speaker', 'revision')}
                                      if prompt_cache and prompt_cache.bundle else {}),
                                   'texts': prompt_cache.bundle.get('texts', prompt_recordings.TEXTS) if prompt_cache and prompt_cache.bundle else prompt_recordings.TEXTS}},
                    headers={'cache-control': 'no-store'})
            if action == 'recordings' and request.method == 'GET':
                name = request.query_params.get('name', '')
                if name not in prompt_recordings.TEXTS:
                    return Response(status_code=404)
                pcm = prompt_cache.audio.get(name) if prompt_cache else None
                if pcm is None:
                    try:
                        pcm = (prompts / (name + '.pcm')).read_bytes()
                    except OSError:
                        return Response(status_code=404)
                return Response(prompt_recordings.wav(pcm), media_type='audio/wav', headers={'cache-control': 'no-store'})
            if action == 'regenerate' and request.method == 'POST':
                if prompt_cache is None:
                    return JSONResponse({'error': {'code': 'front_cache_unavailable'}}, status_code=503)
                if cache_lock.locked():
                    return JSONResponse({'error': {'code': 'greetings_busy_try_again'}}, status_code=409)
                async with cache_lock:
                    reply = await http.post(voice_door + '/voice/prompts', headers=headers,
                        params={'profile': settings.get('profile', 'default')}, timeout=120)
                    if not reply.is_success:
                        return Response(reply.content, status_code=reply.status_code, media_type='application/json')
                    prompt_cache.install(reply.json())
                return JSONResponse({'saved': True, 'revision': prompt_cache.revision})
            if action == 'preview' and request.method == 'POST':
                body = await request.body()
                if len(body) > 4096:
                    return Response(status_code=413)
                reply = await http.post(voice_door + '/voice/preview', headers={**headers, 'content-type': 'application/json'}, content=body, timeout=120)
                return Response(reply.content, status_code=reply.status_code, media_type=reply.headers.get('content-type'), headers={'cache-control': 'no-store'})
            return Response(status_code=404)
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            return JSONResponse({'error': {'code': 'voice_admin_unavailable'}}, status_code=503)

    app = Starlette(lifespan=lifespan, routes=[Route("/", page), Route("/browser/config", browser_config),
        Route('/admin/{action}', admin, methods=['GET', 'POST']),
        Route("/browser/sessions", start, methods=["POST"]),
        WebSocketRoute("/browser/sessions/{sid}/stream", browser),
        WebSocketRoute("/desktop/stream", desktop),
        Route("/voice/inbound", inbound, methods=["POST"]), Route("/twilio/voice", inbound, methods=["POST"]),
        WebSocketRoute("/voice/stream", telephone), WebSocketRoute("/twilio/media", telephone),
        Mount("/static", StaticFiles(directory=assets), name="static")])

    app.state.latency = latencies

    class PublicRoutes:
        """Only Twilio paths exist on the public forwarding port, including upgrades."""
        def __init__(self, app):
            """Bind the gateway app to the telephone-only public route allowlist."""
            self.app = app

        async def __call__(self, scope, receive, send):
            """Limit the public telephone listener to its explicitly allowed routes."""
            if scope["type"] in ("http", "websocket") and scope["server"][1] == settings.get("desktop_port"):
                if scope["type"] != "websocket" or scope["path"] != "/desktop/stream":
                    if scope["type"] == "http":
                        await Response(status_code=404)(scope, receive, send)
                    else:
                        await send({"type": "websocket.close", "code": 1008})
                    return
            if scope["type"] in ("http", "websocket") and scope["server"][1] == settings.get("public_port", 8081):
                permitted = ("/voice/inbound", "/twilio/voice") if scope["type"] == "http" else ("/voice/stream", "/twilio/media")
                if scope["path"] not in permitted:
                    if scope["type"] == "http":
                        await Response(status_code=404)(scope, receive, send)
                    else:
                        await send({"type": "websocket.close", "code": 1008})
                    return
            await self.app(scope, receive, send)

    app.add_middleware(PublicRoutes)
    app.add_middleware(ClientOrigin)
    observation.configured(app, 'voice-gateway', settings)

    app.state.browser_sessions = browser_sessions
    return app


cli = typer.Typer(**CLI)


@cli.command()
def serve(config: Annotated[Path, typer.Option()] = Path("settings/voice-gateway.toml")):
    """Run private browser voice and the signed public telephone routes."""
    from turnstile.app import listening
    settings, following = with_changes("voice-gateway", config)
    sockets = [listening(settings.get("host", "127.0.0.1"), settings.get("port", 8446))]
    if settings.get("desktop_port"):
        sockets.append(listening(settings.get("desktop_host", "127.0.0.1"), settings["desktop_port"]))
    if settings.get("public_port"):
        sockets.append(listening(settings.get("public_host", "0.0.0.0"), settings["public_port"]))
    cache = prompt_recordings.PromptCache(data_folder(config, settings) / "voice-prompts.json")
    uvicorn.Server(uvicorn.Config(build_app(settings, following=following, prompt_cache=cache), access_log=False, ws_max_size=16384, ws_max_queue=4)).run(sockets=sockets)


def main():
    """Run the configured private browser, desktop and telephone gateway listeners."""
    cli()
