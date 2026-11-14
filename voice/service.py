"""Compute's voice service: bounded speech sessions and one conversation owner per call.

It turns speech into text and text into speech. It never asks a model itself: for each turn it
hands the gateway on the control node what to ask, and the gateway asks it through turnstile
with the caller's own key, and sends the answer's words back. Who the caller is comes from
turnstile's signed statement when the session is made (see loadcoach/statement.py). The sound
socket then takes the session's own token, which the gateway was given for that one call.
"""

from __future__ import annotations

from loadcoach import observability as observation

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Annotated
from urllib.parse import quote

import httpx
import typer
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from loadcoach.config import CLI
from loadcoach.settings import with_changes
from loadcoach.statement import Statements, user_of
from voice import protocol, prompt_recordings, latency, options as call_options
from voice.speech import Speech
from voice.speakers import choices as speaker_choices


class Refused(Exception):
    """A public failure with no caller text, credentials or internal exception in it."""

    def __init__(self, code: str, status=503):
        """Retain the voice protocol error code and HTTP refusal status."""
        self.code, self.status = code, status


def bearer(connection) -> str:
    """Read the request's bearer credential from its authorization header."""
    scheme, _, value = connection.headers.get("authorization", "").partition(" ")
    return value if scheme.lower() == "bearer" else ""


def caller(request) -> dict:
    """Who turnstile says is asking, in its signed statement. Nobody else is known here."""
    said = getattr(request.state, "said", None)
    if not said or not said.get("caller"):
        raise Refused("unauthorized", 401)
    return said["caller"]


def allowed(person: dict, mode: str) -> bool:
    """Check whether an account may use the selected voice conversation mode."""
    abilities = person["group"]["can"]
    return "superuser" in abilities or "front-voice" in abilities


def may_use(person: dict, model: str) -> bool:
    """Check whether the account's model access permits the selected voice model."""
    return user_of(person).group.may_use(model)


# The pieces of one answer that may wait to be spoken. A model's answer arrives a few words at a
# time, much faster than it is spoken, and its length is bounded by the profile's max_output_tokens.
ANSWER_PIECES = 4096


@dataclass
class Session:
    """Private in-memory state; tokens and caller words never go to this service's log."""

    id: str
    token: str  # the session's own, for its sound socket: given to the gateway once, when it is made
    person: dict  # the caller, as turnstile's statement carried them
    profile: str
    mode: str
    source: str
    agent_model: str = ""
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    connected: bool = False
    prepared: bool = False
    turn: int = 0
    generation: asyncio.Task | None = None
    preparing: asyncio.Task | None = None
    socket: WebSocket | None = None
    history: list = field(default_factory=list)
    segments: dict = field(default_factory=dict)
    incoming: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=25))
    incoming_bytes: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=ANSWER_PIECES))
    interrupted: set = field(default_factory=set)
    sent_samples: int = 0
    played_samples: int = 0
    playback_changed: asyncio.Event = field(default_factory=asyncio.Event)
    last_count: int = 0
    agent_done: bool = False
    timings: dict = field(default_factory=dict)
    observation: dict = field(default_factory=dict)
    voice: dict | None = None  # a SoundRoom voice the caller chose: its reference recording (base64 WAV) and words
    options: dict = field(default_factory=dict)
    audio_speech: object | None = None
    held_models: set = field(default_factory=set)

    async def send(self, value):
        """Serialize control and PCM writes, and bound a stuck socket's write time."""
        async with self.send_lock:
            async with asyncio.timeout(2):
                if isinstance(value, bytes):
                    await self.socket.send_bytes(value)
                else:
                    await self.socket.send_json(value)


def build_app(settings: dict, speech=None, client=None, following=None) -> Starlette:
    """Build the service with replaceable speech and HTTP boundaries for tests."""
    speech = speech or Speech(settings)
    http = client or httpx.AsyncClient(timeout=65)
    sessions: dict[str, Session] = {}
    retained: dict[str, float] = {}
    warming = asyncio.Lock()
    model_slot = asyncio.Semaphore(settings.get("model_parallel", 1))
    service_key = Path(settings["key_file"]).read_text().strip()
    compute = settings["compute"].rstrip("/")
    closing = False
    prompt_lock = asyncio.Lock()
    prompt_bundle = None

    async def coach(method, path, key, no_wake=False, **kwargs):
        """Send an authenticated private request to the existing model API."""
        try:
            reply = await http.request(method, compute + path,
                                       headers={"authorization": "Bearer " + key,
                                                **({'x-turnstile-no-wake': '1'} if no_wake else {})}, **kwargs)
        except httpx.HTTPError:
            raise Refused("compute_unavailable") from None
        if not reply.is_success:
            code = "unauthorized" if reply.status_code in (401, 403) else "busy" if reply.status_code in (429, 503) else "model_failed"
            raise Refused(code, reply.status_code)
        return reply.json()

    def profile(session):
        """Resolve the selected configured voice profile and its permitted model."""
        return {**settings["profiles"][session.profile], **session.options}

    def model(session):
        """Hold the model that will answer this call, including the gateway's Hermes choice."""
        return session.agent_model or profile(session)["model"]

    speeches = {}  # audio.cpp speech, one for each profile that asks for it

    async def raw(method, path, body, priority=None):
        """Ask turnstile with the service's own key for an audio model's answer, as bytes. Live speech may ask to go first."""
        headers = {"authorization": "Bearer " + service_key, **({"x-loadcoach-priority": priority} if priority else {})}
        try:
            reply = await http.request(method, compute + path, headers=headers, json=body, timeout=120)
        except httpx.HTTPError:
            raise Refused("compute_unavailable") from None
        if not reply.is_success:
            raise Refused("unauthorized" if reply.status_code in (401, 403) else "busy" if reply.status_code in (429, 503) else "speech_failed", reply.status_code)
        return reply.content

    def speech_of(session_or_profile):
        """The profile's audio.cpp speech when it asks for it; otherwise the shared CPU speech."""
        session = session_or_profile if isinstance(session_or_profile, Session) else None
        name = session.profile if session else session_or_profile
        chosen = profile(session) if session else settings["profiles"][name]
        if chosen.get("speech") != "audiocpp":
            return speech
        if session and session.options:
            if session.audio_speech is None:
                from soundroom.voice import AudioSpeech
                session.audio_speech = AudioSpeech(chosen, lambda method, path, body: raw(method, path, body, chosen.get("priority")),
                                                   settings.get("speech_threshold", 600))
            return session.audio_speech
        if name not in speeches:
            from soundroom.voice import AudioSpeech
            speeches[name] = AudioSpeech(chosen, lambda method, path, body: raw(method, path, body, chosen.get("priority")),
                                         settings.get("speech_threshold", 600))
        return speeches[name]

    def models_of(session):
        """Every model a session keeps resident: the one that answers, and its audio models."""
        heard = speech_of(session)
        return {model(session), *(heard.models() if heard is not speech else ())}

    def revision(session):
        """Return the active speech profile's configuration revision."""
        return hashlib.sha256(json.dumps(profile(session), sort_keys=True).encode()).hexdigest()[:16]

    async def hold(model):
        """Acquire or renew the configured model residency hold for a voice session."""
        try:
            await coach("POST", "/loadcoach/hold", service_key,
                        json={"model": model, "seconds": settings.get("hold_seconds", 30)})
        except Refused as problem:
            if problem.status == 403:
                raise Refused('speech_service_permissions', 503) from None
            if problem.status == 404:
                raise Refused('model_unavailable', 503) from None
            raise

    async def prepare(session):
        """Share speech warmup and model loading; each caller retains its own reservation."""
        async with warming:
            await speech_of(session).warm()
            for chosen in models_of(session):
                await hold(chosen)
                session.held_models.add(chosen)
        session.prepared = True
        print(json.dumps({"event": "voice.ready", "session": session.id, "account": session.person["name"],
                          "mode": session.mode, "source": session.source,
                          "startup_seconds": round(time.monotonic() - session.created, 3)}), flush=True)

    async def cancel(session, why):
        """Invalidate a turn before awaiting anything, then clear playback and cancel work."""
        old = session.turn
        session.interrupted.add(old)
        if session.generation:
            session.generation.cancel()
        session.playback_changed.set()
        if session.socket:
            await session.send({"type": "playback.clear", "turn": old, "why": why})
            timing = session.timings.get(old)
            measured = timing.event("interrupted") if timing else {}
            await session.send({"type": "turn.interrupted", "turn": old, "why": why, "latency": measured})
            if timing:
                print(json.dumps(dict(event="voice.latency", session=session.id, turn=old, **measured)), flush=True)
        session.sent_samples = session.played_samples = 0

    async def end(session):
        """End exactly once, leaving another session's shared model hold intact."""
        if sessions.pop(session.id, None) is None:
            return
        for task in (session.generation, session.preparing):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        for chosen in models_of(session):
            if session.prepared:
                retained[chosen] = time.monotonic() + settings.get("hold_after_last_call", 120)
            elif chosen not in retained:
                retained[chosen] = time.monotonic()
        if session.socket:
            with suppress(Exception):
                await session.socket.close()
        print(json.dumps({"event": "voice.closed", "session": session.id}), flush=True)

    async def maintain():
        """Expire abandoned sessions and refresh holds. A lost key is the gateway's to notice: it holds the key."""
        while True:
            await asyncio.sleep(settings.get("hold_every", 10))
            now = time.monotonic()
            for session in list(sessions.values()):
                busy = session.generation and not session.generation.done()
                expired = now - session.created > settings.get("max_session_minutes", 30) * 60
                expired |= not session.connected and now - session.created > 65
                expired |= session.connected and not busy and now - session.touched > settings.get("idle_seconds", 120)
                try:
                    if expired:
                        raise Refused("expired")
                except Refused:
                    await end(session)
            models = {chosen for s in sessions.values() for chosen in s.held_models}
            for chosen in models | set(retained):
                try:
                    if chosen in models or retained.get(chosen, 0) > now:
                        await hold(chosen)
                    else:
                        await coach("DELETE", "/loadcoach/hold/" + quote(chosen, safe=""), service_key)
                        retained.pop(chosen, None)
                except Refused:
                    for session in list(sessions.values()):
                        if model(session) == chosen:
                            await end(session)

    @asynccontextmanager
    async def lifespan(app):
        """Start background workers and release service resources on shutdown."""
        nonlocal closing
        maintenance = asyncio.create_task(maintain())
        # Settings changed on the site are fetched from LoadCoach and put into `settings`: see loadcoach/settings.py.
        in_step = asyncio.create_task(following(http, compute, service_key)) if following else None
        yield
        closing = True
        maintenance.cancel()
        if in_step:
            in_step.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        for session in list(sessions.values()):
            await end(session)
        for model in retained:
            with suppress(Exception):
                await coach("DELETE", "/loadcoach/hold/" + quote(model, safe=""), service_key)
        if client is None:
            await http.aclose()
        await asyncio.to_thread(speech.close)
        await observation.shutdown(app)

    async def recordings(request):
        """Give the gateway a complete greeting set for the currently selected speaker."""
        nonlocal prompt_bundle
        try:
            person = caller(request)
            abilities = person['group']['can']
            admin = bool(set(abilities) & {'manage-settings', 'superuser'})
            if not allowed(person, "direct") and "follow-settings" not in abilities and not admin:
                raise Refused("unauthorized", 403)
            if request.method == 'POST' and not admin:
                raise Refused('unauthorized', 403)
            name = request.query_params.get("profile", "default")
            if name not in settings["profiles"]:
                raise Refused("invalid_profile", 404)
            if request.method == 'POST' and prompt_lock.locked():
                raise Refused('speech_admin_busy', 409)
            async with prompt_lock:
                speaker, revision = prompt_recordings.specification(settings, name)
                headers = {"etag": '"' + revision + '"', "cache-control": "private, no-cache"}
                if request.method == 'GET' and request.headers.get("if-none-match") == headers["etag"]:
                    return Response(status_code=304, headers=headers)
                if request.method == 'POST' or prompt_bundle is None or prompt_bundle["revision"] != revision:
                    bundle = await prompt_recordings.generate(speech, speaker, revision, prompt_recordings.texts(settings))
                    if prompt_recordings.specification(settings, name) != (speaker, revision):
                        raise Refused("speaker_changed", 503)
                    prompt_bundle = bundle
                return JSONResponse(prompt_bundle, headers=headers)
        except Refused as problem:
            return JSONResponse({"error": {"code": problem.code}}, status_code=problem.status)
        except (ValueError, KeyError, RuntimeError):
            return JSONResponse({"error": {"code": "greetings_unavailable"}}, status_code=503)

    async def preview(request):
        """Explicit administrator synthesis, serialized with greeting generation; no model hold."""
        try:
            if not set(caller(request)['group']['can']) & {'manage-settings', 'superuser'}:
                raise Refused('unauthorized', 403)
            if len(await request.body()) > 4096:
                raise Refused('preview_too_large', 413)
            asked = await request.json()
            if not isinstance(asked, dict):
                raise Refused('invalid_preview', 400)
            text, speaker = asked.get('text', ''), asked.get('speaker')
            if (not isinstance(text, str) or not text.strip() or len(text) > 300
                    or type(speaker) is not int or speaker not in dict(speaker_choices(settings.get('synthesis', {})))):
                raise Refused('invalid_preview', 400)
            if prompt_lock.locked():
                raise Refused('speech_admin_busy', 409)
            async with prompt_lock:
                await speech.warm()
                pcm = await speech.say(text, speaker)
            if not pcm or len(pcm) % 2 or len(pcm) > 960000:
                raise Refused('preview_failed')
            return Response(prompt_recordings.wav(pcm), media_type='audio/wav', headers={'cache-control': 'no-store'})
        except Refused as problem:
            return JSONResponse({'error': {'code': problem.code}}, status_code=problem.status)
        except (ValueError, KeyError, TypeError, RuntimeError):
            return JSONResponse({'error': {'code': 'preview_failed'}}, status_code=503)

    async def ready(request):
        """An observation only: no warming, model load or wake is triggered."""
        try:
            caller(request)
            state = await coach("GET", "/loadcoach/status", service_key)
            audio_profiles = any(profile.get('speech') == 'audiocpp' for profile in settings['profiles'].values())
            catalogue = await coach('GET', '/v1/models', service_key, no_wake=True) if audio_profiles else {}
            installed = {one['id'] for one in catalogue.get('data', [])}
            profiles = {}
            for name, p in settings["profiles"].items():
                chosen = request.query_params.get("agent_model") if request.query_params.get("conversation_mode") == "hermes" else None
                loaded = any(m["model"] == (chosen or p["model"]) and m["state"] == "ready" for m in state["loaded"])
                room = len(sessions) < settings.get("max_active_sessions", 1)
                heard = speech_of(name)
                ready_since = heard.ready_since if heard is speech else True  # audio.cpp speech loads on demand
                recognition = p.get('recognition_model') in installed if heard is not speech else bool(ready_since)
                synthesis = p.get('synthesis_model') in installed if heard is not speech else bool(ready_since)
                available = bool(recognition and synthesis and loaded and room and not state.get("draining") and not state.get("lock"))
                reason = (None if available else 'Compute is draining.' if state.get('draining') else
                          'Compute models are locked.' if state.get('lock') else 'Voice session capacity is full.' if not room else
                          'Speech recognition is not ready or its model is not installed.' if not recognition else
                          'Speech synthesis is not ready or its model is not installed.' if not synthesis else
                          'Model ' + (chosen or p['model']) + ' is not loaded.')
                profiles[name] = dict(recognition=recognition, synthesis=synthesis,
                                      model=loaded, since=heard.ready_since, can_start=available,
                                      model_name=chosen or p['model'], modes=p.get("modes", ["direct"]), reason=reason)
            return JSONResponse(dict(profiles=profiles, active=sum(s.connected for s in sessions.values()),
                                     starting=sum(not s.connected for s in sessions.values()),
                                     maximum=settings.get("max_active_sessions", 1)))
        except Refused as problem:
            return JSONResponse({"error": {"code": problem.code}}, status_code=problem.status)

    async def choices(request: Request):
        """List caller-permitted call choices without warming speech or loading a model."""
        try:
            person = caller(request)
            if not allowed(person, 'direct'):
                raise Refused('unauthorized', 403)
            catalogue = await coach('GET', '/v1/models', service_key, no_wake=True)
            models = [one for one in catalogue.get('data', []) if one.get('owned_by') == 'loadcoach' and may_use(person, one['id'])]
            return JSONResponse({
                'profiles': [{'id': name, **{key: value for key, value in configured.items()
                    if key in (call_options.FIELDS - {'system_prompt'}) | {'speech', 'modes'}}} for name, configured in settings['profiles'].items()],
                'models': [one['id'] for one in models if one.get('capabilities', {}).get('chat')],
                'synthesis_models': [one['id'] for one in models if one.get('capabilities', {}).get('audio') == 'clone'],
                'recognition_models': [one['id'] for one in models if one.get('capabilities', {}).get('audio') == 'transcription'],
                'speakers': [{'id': identifier, 'name': label} for identifier, label in speaker_choices(settings.get('synthesis', {}))],
                'audio_models': [call_options.audio_choices(one) for one in models
                                 if call_options.audio_kind(one) in ('speech', 'clone', 'transcription')],
                'languages': [{'id': code, 'name': label} for code, label in call_options.LANGUAGES.items()],
            }, headers={'cache-control': 'no-store'})
        except Refused as problem:
            return JSONResponse({'error': {'code': problem.code}}, status_code=problem.status)

    async def create(request: Request):
        """Authorize and prepare a bounded compute voice session."""
        session = None
        try:
            person = caller(request)
            asked = await request.json()
            sid, name, mode = asked["session_id"], asked.get("profile", "default"), asked["conversation_mode"]
            if not isinstance(sid, str) or not protocol.ID.fullmatch(sid) or mode not in protocol.MODES:
                raise Refused("invalid_session", 400)
            if name not in settings["profiles"] or mode not in settings["profiles"][name].get("modes", ["direct"]):
                raise Refused("mode_unavailable", 403)
            if not allowed(person, mode):
                raise Refused('unauthorized', 403)
            try:
                options = call_options.checked(asked.get('options', {}), settings['profiles'][name], mode,
                                               speaker_choices(settings.get('synthesis', {})))
            except ValueError:
                raise Refused('invalid_options', 400) from None
            if options.keys() & {'model', 'synthesis_model', 'recognition_model', 'speech_voice', 'language', 'response_language'}:
                catalogue = await coach('GET', '/v1/models', service_key)
                for field, capability in (('model', 'chat'), ('synthesis_model', 'clone'), ('recognition_model', 'transcription')):
                    if field not in options:
                        continue
                    if not may_use(person, options[field]):
                        raise Refused('unauthorized', 403)
                    selected = next((one for one in catalogue.get('data', []) if one['id'] == options[field] and one.get('owned_by') == 'loadcoach'), {})
                    if not (selected.get('capabilities', {}).get(capability) or
                            call_options.audio_kind(selected) in (('speech', 'clone') if field == 'synthesis_model' else (capability,))):
                        raise Refused('model_unavailable', 400)
                effective = {**settings['profiles'][name], **options}
                for field, language in (('recognition_model', 'language'), ('synthesis_model', 'response_language')):
                    if settings['profiles'][name].get('speech') != 'audiocpp':
                        continue
                    selected = next((one for one in catalogue.get('data', []) if one['id'] == effective[field]), {})
                    declared = call_options.audio_choices(selected) if selected else {}
                    if language in options and options[language] != 'auto' and declared.get('languages') and options[language] not in declared['languages']:
                        raise Refused('invalid_options', 400)
                    if field == 'synthesis_model' and 'speech_voice' in options and (
                            declared.get('kind') != 'speech' or options['speech_voice'] not in
                            {voice['id'] for voice in declared.get('voices', [])}):
                        raise Refused('invalid_options', 400)
            agent_model = asked.get("agent_model", settings["profiles"][name]["model"]) if mode == "hermes" else ""
            if not isinstance(agent_model, str) or (mode == "hermes" and not agent_model):
                raise Refused("invalid_session", 400)
            if not allowed(person, mode) or not may_use(person, agent_model or options.get('model', settings["profiles"][name]["model"])):
                raise Refused("unauthorized", 403)
            voice = asked.get("voice")
            if voice is not None and (settings["profiles"][name].get("speech") != "audiocpp" or not isinstance(voice, dict) or voice.keys() - {"audio", "text"}
                                      or not isinstance(voice.get("audio"), str) or not 0 < len(voice["audio"]) <= 12_000_000
                                      or not isinstance(voice.get("text", ""), str) or len(voice.get("text", "")) > 4000):
                raise Refused("invalid_voice", 400)
            if sid in sessions:
                existing = sessions[sid]
                if existing.person["name"] != person["name"] or (existing.profile, existing.mode, existing.agent_model, existing.options, existing.voice) != (name, mode, agent_model, options, voice):
                    raise Refused("session_conflict", 409)
                if not existing.prepared:
                    raise Refused("starting", 409)
                return JSONResponse({"session_id": sid, "conversation_mode": mode, "model": model(existing), "token": existing.token})
            if closing or len(sessions) >= settings.get("max_active_sessions", 1):
                raise Refused("busy", 429)
            if sum(not s.prepared for s in sessions.values()) >= settings.get("max_starting_sessions", 4):
                raise Refused("busy", 429)
            session = Session(sid, secrets.token_urlsafe(32), person, name, mode, asked.get("source", "browser"),
                              agent_model=agent_model, observation=observation.wire(), voice=voice, options=options)
            sessions[sid] = session
            session.preparing = asyncio.create_task(prepare(session))

            async def disconnected():
                """Watch for caller departure during voice-session preparation."""
                while not await request.is_disconnected():
                    await asyncio.sleep(.1)

            gone = asyncio.create_task(disconnected())
            try:
                async with asyncio.timeout(min(60, max(1, float(asked.get("max_wait", 60))))):
                    done, _ = await asyncio.wait({session.preparing, gone}, return_when=asyncio.FIRST_COMPLETED)
                    if gone in done:
                        raise Refused("disconnected", 499)
                    await session.preparing
            finally:
                gone.cancel()
            return JSONResponse({"session_id": sid, "conversation_mode": mode, "profile": name, "revision": revision(session),
                                 "model": model(session), "token": session.token})
        except (Refused, KeyError, ValueError, TypeError, TimeoutError) as problem:
            if session:
                await end(session)
            code = problem.code if isinstance(problem, Refused) else 'startup_timeout' if isinstance(problem, TimeoutError) else "startup_failed"
            status = problem.status if isinstance(problem, Refused) else 400 if isinstance(problem, (KeyError, ValueError, TypeError)) else 503
            return JSONResponse({"error": {"code": code, "message": code.replace("_", " ")}}, status_code=status)
        except BaseException:
            if session:
                await end(session)
            raise

    async def delete(request):
        """Cancel and remove the caller's selected voice session."""
        session = sessions.get(request.path_params["sid"])
        if session:
            if not secrets.compare_digest(session.token, bearer(request)):
                return JSONResponse({"error": {"code": "unauthorized"}}, status_code=403)
            await end(session)
        return JSONResponse({"closed": True})

    def heard_history(session):
        """Build assistant history only from confirmed complete playback segments."""
        result = []
        for item in session.history:
            result.append({"role": "user", "content": item["user"]})
            parts = [s["text"] for s in session.segments.values() if s["turn"] == item["turn"] and s["played"] == s["samples"]]
            text = protocol.speakable(" ".join(parts))
            if text:
                result.append({"role": "assistant", "content": text})
        # A conservative UTF-8 byte budget bounds tokens for the English profile, and keeps the
        # request the gateway is handed well inside one message on the sound socket.
        budget = min(48_000, max(512, profile(session).get("max_context_tokens", 8192) - profile(session).get("max_output_tokens", 256) - 512))
        while len(result) > 1 and len(json.dumps(result).encode()) > budget:
            result.pop(0)
        return result

    @observation.traced('synthesis')
    async def speak(session, turn, phrase):
        """Speak conversational text only; old delivery annotations must never reach TTS."""
        phrase = protocol.speakable(phrase)
        if not phrase:
            return
        began = time.monotonic()
        timing = session.timings[turn]
        timing.values.setdefault("final_to_phrase_seconds", round(max(0,began-timing.final),6))
        speed = profile(session).get('speed', 1)
        pcm = await speech_of(session).say(phrase, profile(session).get("speaker"),
            **({"voice": session.voice} if session.voice else {}), **({'speed': speed} if speed != 1 else {}))
        synthesis_seconds = time.monotonic() - began
        if turn != session.turn or turn in session.interrupted:
            return
        if len(pcm) > 24_000 * 2 * 20:
            raise Refused("speech_too_long")
        timing.synthesis(synthesis_seconds)
        segment = max(session.segments, default=0) + 1
        session.segments[segment] = dict(turn=turn, text=phrase, samples=len(pcm) // 2, played=0)
        await session.send(dict(type="playback.segment", turn=turn, segment=segment, text=phrase, samples=len(pcm) // 2, latency=timing.event()))
        for offset in range(0, len(pcm), 960):
            if turn != session.turn or turn in session.interrupted:
                return
            # At most 200 ms handed downstream without playback confirmation.
            async with asyncio.timeout(3):
                while session.sent_samples - session.played_samples >= 4800:
                    session.playback_changed.clear()
                    await session.playback_changed.wait()
            frame = pcm[offset:offset + 960]
            session.sent_samples += len(frame) // 2
            await session.send(frame)
            timing.audio(time.monotonic())
        print(json.dumps({"event": "voice.synthesis", "session": session.id, "turn": turn,
                          "synthesis_seconds": round(synthesis_seconds, 3),
                          "including_playback_seconds": round(time.monotonic() - began, 3), "samples": len(pcm) // 2}), flush=True)

    async def answer(session, turn, text):
        """Give each voice turn an independent attempt under its immutable session owner."""
        with observation.job('voice_turn', session.id+':'+str(turn), user_of(session.person)):
            return await answer_turn(session, turn, text)

    async def answer_turn(session, turn, text):
        """Only final-answer deltas enter synthesis; reasoning and tool data are ignored.

        In either mode the words come from the gateway. For a direct conversation it is handed the
        request to make, through turnstile with the caller's key; for Hermes it asks its agent.
        """
        phrases = protocol.Phrases()

        async def spoken(seconds):
            """Turn final generated text into ordered synthesized speech frames."""
            async with asyncio.timeout(seconds):
                while True:
                    event = await session.agent.get()
                    if event is None:
                        break
                    for phrase in phrases.add(event):
                        await speak(session, turn, phrase)

        try:
            if session.mode == "direct":
                async with asyncio.timeout(30):
                    await model_slot.acquire()
                try:
                    p = profile(session)
                    messages = [{"role": "system", "content": call_options.instructions(p)
                                 + "\n" + protocol.PLAYBACK_INSTRUCTIONS}]
                    messages += heard_history(session)
                    await session.send(dict(type="model.request", turn=turn, observation=observation.wire(), body={
                        "model": p["model"], "messages": messages, "stream": True, "max_tokens": p.get("max_output_tokens", 128),
                        "chat_template_kwargs": {"enable_thinking": False}}))
                    await spoken(120)
                finally:
                    model_slot.release()
            else:
                await spoken(120)
            for phrase in phrases.add("", done=True):
                await speak(session, turn, phrase)
            if turn == session.turn and turn not in session.interrupted:
                if not any(s["turn"] == turn for s in session.segments.values()):
                    await speak(session, turn, "Sorry, could you say that again?")
                measured = session.timings[turn].event("completed")
                await session.send(dict(type="turn.completed", turn=turn, latency=measured))
                print(json.dumps(dict(event="voice.latency", session=session.id, turn=turn, **measured)), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            if turn == session.turn and turn not in session.interrupted:
                measured = session.timings[turn].event("failed")
                await session.send(dict(type="session.error", code="turn_failed", turn=turn, latency=measured))
                print(json.dumps(dict(event="voice.latency", session=session.id, turn=turn, **measured)), flush=True)
                if isinstance(problem, TimeoutError):
                    await end(session)

    async def recognize(session):
        """Consume a bounded continuous input queue even while the assistant is speaking."""
        heard = speech_of(session)
        stream = heard.stream()
        loud_ms = 0
        had_speech = False
        last_partial = ''
        last_loud = endpoint = None
        while True:
            pcm, finish, received = await session.incoming.get()
            session.incoming_bytes -= len(pcm)
            import numpy as np
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            loud = len(samples) and float(np.sqrt(np.mean(samples * samples))) > settings.get("speech_threshold", 600)
            loud_ms = loud_ms + len(samples) / 16 if loud else 0
            had_speech |= bool(loud)
            if loud:
                last_loud = received
            if finish:
                endpoint = received
            speaking = session.generation and not session.generation.done() or session.sent_samples > session.played_samples
            if speaking and loud_ms >= 120 and session.turn not in session.interrupted:
                await cancel(session, "caller_speaking")
            began = time.monotonic()
            partial, final = await heard.hear(stream, pcm, finish)
            final_at = time.monotonic()
            if partial:
                session.touched = time.monotonic()
                if final is None and had_speech and partial != last_partial:
                    await session.send(dict(type='transcript.partial', turn=session.turn + 1, text=partial[:4096]))
                    last_partial = partial
            if final is not None:
                last_partial = ''
            if final is not None and not had_speech:
                last_loud = endpoint = None
                continue  # a recognizer hallucination over silence is never a conversation turn
            if final:
                had_speech = False
                if len(final) > 4096:
                    raise Refused("utterance_too_long")
                if session.generation and not session.generation.done():
                    await cancel(session, "next_utterance")
                session.turn += 1
                turn = session.turn
                basis = "input_end_received" if endpoint is not None else "last_loud_frame_received"
                session.timings[turn] = latency.Turn(turn, endpoint if endpoint is not None else last_loud,
                                                    final_at, basis, final_at-began)
                session.timings[turn].values.update(profile=session.profile, model=model(session), revision=revision(session))
                last_loud = endpoint = None
                session.timings = {n:t for n,t in session.timings.items() if n >= turn-31}
                print(json.dumps({"event": "voice.transcript", "session": session.id, "turn": turn,
                                  "characters": len(final), "final_block_seconds": round(time.monotonic() - began, 3)}), flush=True)
                session.agent = asyncio.Queue(maxsize=ANSWER_PIECES)
                session.last_count = 0
                session.agent_done = False
                session.history.append({"turn": turn, "user": final})
                # Limit even a long call's in-memory history and playback ledger.
                session.history[:] = session.history[-32:]
                oldest = session.history[0]["turn"]
                session.segments = {n: s for n, s in session.segments.items() if s["turn"] >= oldest}
                await session.send(dict(type="turn.started", turn=turn, latency=session.timings[turn].event()))
                await session.send(dict(type="transcript.final", turn=turn, text=final))
                session.generation = asyncio.create_task(answer(session, turn, final))

    async def stream(socket: WebSocket):
        """Run the authorized compute voice stream with bounded queues and cancellation."""
        session = None
        try:
            sid = socket.query_params.get("session_id", "")
            session = sessions.get(sid)
            if not session or not secrets.compare_digest(bearer(socket), session.token):
                session = None
                await socket.close(code=1008)
                return
            if session.connected or not session.prepared:
                session = None
                await socket.close(code=1008)
                return
            session.connected = True
            session.socket = socket
            observation.adopt(session.observation)
            observation.identify(user_of(session.person))
            await socket.accept()
            async with asyncio.timeout(5):
                first = protocol.message(await socket.receive_text())
            protocol.check_start(first, session.id, session.mode)
            await session.send(dict(type="session.ready", session_id=sid, conversation_mode=session.mode,
                                    input=protocol.INPUT, output=protocol.OUTPUT, protocol_version=1,
                                    revision=revision(session), max_session_minutes=settings.get("max_session_minutes", 30)))
            hearing = asyncio.create_task(recognize(session))
            try:
                while True:
                    receiving = asyncio.create_task(socket.receive())
                    done, _ = await asyncio.wait({receiving, hearing}, return_when=asyncio.FIRST_COMPLETED)
                    if hearing in done:
                        receiving.cancel()
                        await hearing
                    data = await receiving
                    if data["type"] == "websocket.disconnect":
                        break
                    if data.get("bytes") is not None:
                        pcm = data["bytes"]
                        protocol.check_audio(pcm)
                        if session.incoming_bytes + len(pcm) > 16000:
                            raise Refused("input_overload_repeat")
                        session.incoming.put_nowait((pcm, False, time.monotonic()))
                        session.incoming_bytes += len(pcm)
                        continue
                    event = protocol.message(data["text"])
                    kind = event["type"]
                    if kind == "input.end":
                        session.incoming.put_nowait((b"", True, time.monotonic()))
                    elif kind == "session.cancel":
                        if event.get("interrupt"):
                            await cancel(session, "caller_requested")
                        else:
                            break
                    elif kind == "playback.progress":
                        segment = session.segments.get(event.get("segment"))
                        played = event.get("samples", 0)
                        if segment and event.get("turn") == segment["turn"] and segment["turn"] not in session.interrupted:
                            if type(played) is not int or not segment["played"] <= played <= segment["samples"]:
                                raise ValueError("Invalid playback count")
                            session.played_samples += played - segment["played"]
                            segment["played"] = played
                            session.playback_changed.set()
                            session.touched = time.monotonic()
                    elif kind.startswith("agent."):
                        if event.get("turn") != session.turn or session.turn in session.interrupted:
                            continue
                        if kind == "agent.text.delta":
                            count = event.get("count")
                            if type(count) is not int:
                                raise ValueError("Invalid sequence")
                            if count <= session.last_count:
                                continue
                            if count != session.last_count + 1 or session.agent_done:
                                raise ValueError("Agent text gap")
                            text = event.get("text")
                            if not isinstance(text, str) or len(text) > 2000:
                                raise ValueError("Invalid agent text")
                            session.last_count = count
                            session.agent.put_nowait(text)
                        elif kind == "agent.text.done":
                            if event.get("count") != session.last_count or session.agent_done:
                                raise ValueError("Invalid agent completion")
                            session.agent_done = True
                            session.agent.put_nowait(None)
                        elif kind == "agent.error":
                            await cancel(session, "agent_failed")
                        elif kind != "agent.status":
                            raise ValueError("Unknown agent event")
                    elif kind == "ping":
                        await session.send({"type": "pong", "token": event.get("token")})
                    else:
                        raise ValueError("Unknown control")
            finally:
                hearing.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await hearing
        except (WebSocketDisconnect, Refused, ValueError, KeyError, TypeError, asyncio.QueueFull, TimeoutError):
            with suppress(Exception):
                await socket.send_json({"type": "session.error", "code": "connection_ended"})
        finally:
            if session:
                await end(session)
            else:
                with suppress(Exception):
                    await socket.close(code=1008)

    app = Starlette(lifespan=lifespan, routes=[Route("/voice/ready", ready), Route("/voice/prompts", recordings, methods=['GET', 'POST']),
        Route('/voice/preview', preview, methods=['POST']), Route('/voice/options', choices),
        Route("/voice/sessions", create, methods=["POST"]),
        Route("/voice/sessions/{sid}", delete, methods=["DELETE"]),
        WebSocketRoute("/voice/stream", stream)])
    app.add_middleware(Statements, key_file=settings.get("machine_key_file", ""))  # who each caller is, as turnstile signed it
    observation.configured(app, 'voice', settings)
    app.state.sessions = sessions
    return app


cli = typer.Typer(**CLI)


@cli.command()
def serve(config: Annotated[Path, typer.Option()] = Path("settings/voice.toml")):
    """Run CPU speech and voice sessions on compute."""
    settings, following = with_changes("voice", config)
    uvicorn.run(build_app(settings, following=following), host=settings.get("host", "127.0.0.1"), port=settings.get("port", 8445),
                ws_max_size=16384, ws_max_queue=4, access_log=False)


def main():
    """Run the configured recognition and synthesis service."""
    cli()
