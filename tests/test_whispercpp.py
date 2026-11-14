"""Whisper native files, optional installation, private translation and admission contracts."""

import base64
import io
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import wave

import httpx
import pytest
from starlette.testclient import TestClient

from engines import catalogue, checked_installations, engine_for
from engines.whisper_cpp import model, runtime
from soundroom.install import configure
from test_app import fake_llama_server, in_an_empty_folder  # noqa: F401


@pytest.fixture
def profile(tmp_path):
    weights = tmp_path/'ggml-large-v3.bin'
    weights.write_bytes(b'lmgg'+struct.pack('<I', 51866))
    return {'engine': 'whispercpp', 'flags': {'model': str(weights), 'ctx-size': 0}, 'gpus': 0,
            'whispercpp': {'host_ram_mib': 8192}}


def test_native_format_and_capabilities_are_engine_specific(profile, tmp_path):
    model.check_model('whisper', profile, ())
    engine = engine_for(profile)
    assert engine.capabilities(profile)['translates']
    assert engine.capabilities(profile)['transcription']
    assert not any(engine.capabilities(profile)[name] for name in ('chat', 'embeddings', 'image_generation', 'tools'))
    assert model.contexts(profile) == [0] and engine.host_ram_budget(None, profile) == 8192
    bad = tmp_path/'wrong.bin'; bad.write_bytes(b'GGUF0000')
    with pytest.raises(ValueError, match='GGUF'):
        model.check_model('bad', {**profile, 'flags': {'model': str(bad), 'ctx-size': 0}}, ())
    Path(profile['flags']['model']).write_bytes(b'lmgg'+struct.pack('<I', 51864))
    assert not engine.capabilities(profile)['translates']
    with pytest.raises(ValueError, match='one GPU'):
        model.check_model('bad', {**profile, 'gpus': 2}, ('CUDA0', 'CUDA1'))


def test_optional_runtime_uses_shared_engine_node_and_component_contract(tmp_path):
    config = SimpleNamespace(engines={'whispercpp': {'enabled': True, 'executable': '/bin/true'}}, llama_server='missing')
    checked_installations(config.engines, 'missing')
    whisper = next(row for row in catalogue(config) if row['id'] == 'whispercpp')
    assert whisper['available'] and whisper['schema']['configuration_key'] == 'whispercpp'
    assert [field['name'] for field in whisper['schema']['node']] == ['enabled', 'executable', 'cli', 'manifest']
    path = tmp_path/'loadcoach.toml'; path.write_text('custom = "preserve"\n[engines.audiocpp]\nenabled = false\n')
    configure(path, '/whisper/server', '/whisper/cli', '/whisper/installation.json', engine='whispercpp')
    import tomllib
    saved = tomllib.loads(path.read_text())
    assert saved['custom'] == 'preserve' and saved['engines']['audiocpp']['enabled'] is False
    assert saved['engines']['whispercpp']['executable'] == '/whisper/server'


@pytest.mark.parametrize('form', ['json', 'text', 'srt', 'vtt'])
@pytest.mark.parametrize('translate', [False, True])
def test_private_adapter_preserves_supported_native_formats_and_request_fields(profile, monkeypatch, form, translate):
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed')); stream.writeframes(b'\0\0'*160)
    wav = raw.getvalue()
    async def converted(data, rate, channels):
        assert data == wav and (rate, channels) == (16000, 1)
        return data
    monkeypatch.setattr(runtime, 'to_wav', converted)
    seen = []
    def upstream(request):
        seen.append(request.content)
        return httpx.Response(200, content=b'{"text":"exact"}' if form == 'json' else b'exact output\n',
                              headers={'content-type': 'application/json' if form == 'json' else 'text/plain'})
    class Native:
        async def start(self):
            self.process = SimpleNamespace(returncode=None)
            self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url='http://private')
        async def stop(self):
            await self.client.aclose()
    job = {'token': 'private', 'settings': profile, 'cards': [], 'installation': {}}
    with TestClient(runtime.create_app(job, Native())) as client:
        assert client.get('/health').status_code == 401
        answer = client.post(runtime.TRANSLATE if translate else runtime.TRANSCRIBE, headers={'authorization': 'Bearer private'},
                             json={'file': base64.b64encode(wav).decode(), 'response_format': form, 'prompt': 'terms', 'temperature': .2})
        assert answer.status_code == 200, answer.text
        assert answer.content == (b'{"text":"exact"}' if form == 'json' else b'exact output\n')
        assert b'name="prompt"\r\n\r\nterms' in seen[0] and b'name="temperature"\r\n\r\n0.2' in seen[0]
        assert ('\r\n\r\ntrue' if translate else '\r\n\r\nfalse').encode() in seen[0]


def test_unsupported_timing_and_translation_language_fail_before_loading():
    for asked in [{'response_format': 'verbose_json'}, {'timestamp_granularities': ['word']}, {'temperature': True}]:
        with pytest.raises(ValueError):
            runtime.checked(runtime.TRANSCRIBE, asked)
    with pytest.raises(ValueError, match='omit language'):
        runtime.checked(runtime.TRANSLATE, {'language': 'es'})
    with pytest.raises(ValueError, match='only'):
        runtime.checked('/v1/chat/completions', {})


def test_whisper_catalogue_and_voice_recognition_choices_use_the_native_profile(profile, fake_llama_server):
    from test_app import gateway, add_user
    from voice.options import audio_choices
    with gateway(fake_llama_server, {'whisper': profile}, engines={'whispercpp': {'enabled': True, 'executable': '/bin/true'}}) as client:
        headers = add_user(client, 'owner', 'admin')
        row = client.get('/v1/models/whisper', headers=headers).json()
        assert row['engine_id'] == 'whispercpp' and row['translates'] is True
        assert row['audio_profile']['kind'] == 'transcription'
        assert 'es' in audio_choices(row)['languages']
        answer = client.post('/v1/chat/completions', headers=headers, json={'model': 'whisper', 'messages': [{'role': 'user', 'content': 'Hi'}]})
        assert answer.status_code == 400


def test_component_installer_options_are_forwarded_and_checked(tmp_path):
    from test_setup_recovery import script_functions, shell
    source = script_functions('install.sh') + '''
parse_arguments --dir "$INSTALL" --topology compute --yes --whispercpp install --whispercpp-backend cpu
runtime_options
printf '%s\\n' "${RUNTIME_OPTIONS[@]}"
'''
    answer = shell(tmp_path, source, env={'INSTALL': str(tmp_path/'installed')})
    assert answer.returncode == 0, answer.stdout+answer.stderr
    assert '--whispercpp\ninstall\n--whispercpp-backend\ncpu' in answer.stdout
