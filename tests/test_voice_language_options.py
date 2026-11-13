"""Language and built-in voice choices stay scoped to the caller and selected audio models."""
import asyncio
import json
import struct

import httpx
import pytest
from starlette.testclient import TestClient

from front_helpers import Signed, caller, machine_key
from soundroom.voice import AudioSpeech
from test_voice import FakeSpeech, CALLERS, answered, socket, voice  # noqa: F401
from voice import options, protocol
from voice.service import build_app


@pytest.fixture
def service(tmp_path):
    key = tmp_path / 'service.key'
    key.write_text('service')
    calls = []
    catalogue = [{'id': 'small', 'owned_by': 'loadcoach', 'capabilities': {'chat': True}},
        {'id': 'asr', 'owned_by': 'loadcoach', 'capabilities': {'audio': 'transcription'},
         'audio_profile': {'options': [{'name': 'language', 'values': ['en', 'es']}]}},
        {'id': 'tts', 'owned_by': 'loadcoach', 'capabilities': {'audio': 'speech'},
         'audio_profile': {'options': [{'name': 'voice', 'values': ['Ryan', 'Vivian']},
                                     {'name': 'language', 'values': ['English', 'Spanish']}]}},
        {'id': 'private', 'owned_by': 'loadcoach', 'capabilities': {'audio': 'speech'}}]

    async def coach(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': catalogue})
        calls.append(request)
        return httpx.Response(200, json={})

    settings = {'compute': 'http://turnstile', 'key_file': str(key), 'machine_key_file': machine_key(tmp_path),
        'max_active_sessions': 3, 'profiles': {'audio': {'model': 'small', 'speech': 'audiocpp',
            'recognition_model': 'asr', 'synthesis_model': 'tts', 'modes': ['direct']}}}
    person = caller('permitted', ['front-voice'], ['small', 'asr', 'tts'])
    with TestClient(build_app(settings, FakeSpeech(), httpx.AsyncClient(transport=httpx.MockTransport(coach)))) as client:
        yield client, Signed(person), calls, settings


def test_public_rosters_are_declared_per_model_and_permission_filtered(service):
    client, signed, calls, _ = service
    response = client.get('/voice/options', auth=signed)
    assert response.status_code == 200
    models = {model['id']: model for model in response.json()['audio_models']}
    assert set(models) == {'asr', 'tts'}
    assert models['tts']['voices'] == [{'id': 'Ryan', 'name': 'Ryan'}, {'id': 'Vivian', 'name': 'Vivian'}]
    assert models['tts']['languages'] == ['en', 'es']
    assert not calls  # discovery never warms or holds a model


def test_languages_and_builtin_voice_are_validated_before_model_holds(service):
    client, signed, calls, settings = service
    base = {'session_id': 'chosen', 'profile': 'audio', 'conversation_mode': 'direct'}
    selected = {'language': 'es', 'response_language': 'en', 'speech_voice': 'Vivian',
                'recognition_model': 'asr', 'synthesis_model': 'tts'}
    for bad in ({'language': 'xx'}, {'language': []}, {'response_language': 'ja'},
                {'speech_voice': 'Invented'}, {'speech_voice': True}):
        response = client.post('/voice/sessions', auth=signed, json={**base, 'options': {**selected, **bad}})
        assert response.status_code == 400 and response.json()['error']['code'] == 'invalid_options'
        assert not calls
    assert client.post('/voice/sessions', auth=signed, json={**base, 'options': selected}).status_code == 200
    assert client.post('/voice/sessions', auth=signed, json={**base, 'session_id': 'ordinary'}).status_code == 200
    sessions = client.app.state.sessions
    assert sessions['chosen'].audio_speech.profile['language'] == 'es'
    assert sessions['chosen'].audio_speech.profile['speech_voice'] == 'Vivian'
    assert 'language' not in settings['profiles']['audio']
    assert sessions['ordinary'].options == {}
    assert 'Reply only in English.' in options.instructions(sessions['chosen'].audio_speech.profile)


def test_audio_transport_sends_selected_language_and_builtin_voice():
    requests = []

    async def request(method, path, body):
        requests.append((path, body))
        return b'{"text":"Buenos dias"}' if path.endswith('transcriptions') else b'PCM'

    speech = AudioSpeech({'recognition_model': 'asr', 'synthesis_model': 'tts',
                         'language': 'es', 'speech_voice': 'Vivian'}, request)

    async def run():
        stream = speech.stream()
        await speech.hear(stream, struct.pack('<1600h', *([3000, -3000] * 800)), True)
        assert await speech.say('Good morning.') == b'PCM'
    asyncio.run(run())
    assert requests[0][1]['language'] == 'es'
    assert requests[1][1] == {'model': 'tts', 'input': 'Good morning.', 'response_format': 'pcm', 'voice': 'Vivian'}


def test_cpu_and_hermes_reject_unsupported_language_overrides():
    for chosen in ({'language': 'es'}, {'response_language': 'es'}, {'speech_voice': 'Ryan'}):
        with pytest.raises(ValueError):
            options.checked(chosen, {}, 'direct', [])
    with pytest.raises(ValueError):
        options.checked({'response_language': 'en'}, {}, 'hermes', [])


def test_partial_transcripts_replace_repeated_hypotheses_and_language_reaches_model(voice, monkeypatch):
    client, _ = voice
    original = FakeSpeech.hear

    async def partial(self, stream, pcm, finish=False):
        if not finish:
            stream['heard'] |= any(pcm)
            return 'Hello so far', None
        return await original(self, stream, pcm, finish)

    monkeypatch.setattr(FakeSpeech, 'hear', partial)
    made = client.post('/voice/sessions', auth=Signed(CALLERS['caller']), json={
        'session_id': 'partial', 'conversation_mode': 'direct',
        'options': {'language': 'en', 'response_language': 'en', 'system_prompt': 'Be brief.'}})
    assert made.status_code == 200
    with socket(client, 'partial', made.json()['token']) as ws:
        ws.send_json(protocol.start('partial', 'direct'))
        assert ws.receive_json()['type'] == 'session.ready'
        ws.send_bytes(b'\x10\x27' * 320)
        assert ws.receive_json() == {'type': 'transcript.partial', 'turn': 1, 'text': 'Hello so far'}
        ws.send_bytes(b'\x10\x27' * 320)
        ws.send_json({'type': 'input.end'})
        assert ws.receive_json()['type'] == 'turn.started'
        assert ws.receive_json()['type'] == 'transcript.final'
        asked = answered(client, ws)
        assert asked['body']['messages'][0]['content'].startswith('Be brief.\nReply only in English.')
