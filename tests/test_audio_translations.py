"""Public multipart speech translation, model capability refusals and Transcribe integration."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from engines.whisper_cpp.adapter import WhisperCpp
from engines.llama_cpp.adapter import LlamaCpp
from loadcoach.batches import Batches
from soundroom.front import Front
from soundroom.store import Store
from test_app import fake_llama_server, in_an_empty_folder, gateway, add_user, model  # noqa: F401
from test_whispercpp import profile  # noqa: F401
from test_soundroom import wav_bytes


@pytest.mark.parametrize('form', ['json', 'text', 'srt', 'vtt'])
def test_sdk_shaped_multipart_translation_uses_normal_admission(profile, fake_llama_server, monkeypatch, form):
    monkeypatch.setattr(WhisperCpp, 'check_available', lambda self, config, settings: None)
    monkeypatch.setattr(WhisperCpp, 'host_ram_budget', lambda *args: 0)
    monkeypatch.setattr(WhisperCpp, 'launch', lambda self, config, port, settings, cards: LlamaCpp().launch(config, port, settings, cards))
    with gateway(fake_llama_server, {'whisper': profile}, engines={'whispercpp': {'enabled': True, 'executable': fake_llama_server}}) as client:
        headers = add_user(client, 'owner', 'admin')
        audio = wav_bytes()
        response = client.post('/v1/audio/translations', headers=headers, files={'file': ('speech.wav', audio, 'audio/wav')},
            data={'model': 'whisper', 'prompt': 'proper nouns', 'response_format': form, 'temperature': '0.2'})
        assert response.status_code == 200, response.text
        native = response.json()
        body = json.loads(native['body'])
        assert native['path'] == '/v1/audio/translations' and body['model'] == 'whisper'
        assert body['file'] == base64.b64encode(audio).decode()
        assert body['prompt'] == 'proper nouns' and body['temperature'] == .2 and body['response_format'] == form
        assert response.headers.get('x-loadcoach-request-id')
        bad = client.post('/v1/audio/translations', headers=headers, files={'file': ('speech.wav', audio)}, data={'model': 'whisper', 'language': 'es'})
        assert bad.status_code == 400 and 'omit language' in bad.text
        assert bad.json()['error']['code']=='unsupported_parameter'
        assert bad.headers['cache-control']=='private, no-store' and bad.headers['x-content-type-options']=='nosniff'


def test_non_translating_model_gets_explicit_refusal(fake_llama_server):
    with gateway(fake_llama_server, {'chat': model('model.gguf', 0)}) as client:
        headers = add_user(client, 'owner', 'admin')
        response = client.post('/v1/audio/translations', headers=headers, json={'model': 'chat', 'file': 'YWJj'})
        assert response.status_code == 400
        assert response.json()['error']['message'] == 'This model cannot translate speech.'


def test_transcribe_proxy_selects_supported_whisper_format_and_keeps_translation(tmp_path):
    batches = Batches(str(tmp_path/'audio.db'))
    store = Store(batches.db, tmp_path/'data')
    asset = store.save('owner', wav_bytes(), kind='recording')
    calls = []
    def wire(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'whisper', 'engine_id': 'whispercpp', 'translates': True,
                                                     'capabilities': {'audio': 'transcription'}}]})
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'text': 'Good morning.'})
    door = httpx.AsyncClient(transport=httpx.MockTransport(wire), base_url='http://front')
    front = Front(store, SimpleNamespace(door=door, internal_key=lambda name: 'own-key'), None)
    user = SimpleNamespace(identity='owner', name='owner', group_name='members')
    async def transcribe(request):
        return await front.transcribe(request, user)
    with TestClient(Starlette(routes=[Route('/transcribe', transcribe, methods=['POST'])])) as client:
        result = client.post('/transcribe', json={'asset': asset['id'], 'model': 'whisper', 'translate': True})
        assert result.status_code == 200, result.text
        assert result.json()['task'] == 'translate'
        assert calls[0][0] == '/v1/audio/translations' and calls[0][1]['response_format'] == 'json'
        assert 'language' not in calls[0][1]
        assert store.asset('owner', asset['id'])['metadata']['transcripts'][0]['text'] == 'Good morning.'
        client.portal.call(door.aclose)
    store.db.close()
