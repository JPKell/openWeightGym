"""Speaker-triggered generation, authenticated refresh and durable all-or-nothing greetings."""

import asyncio
import base64
from copy import deepcopy
from pathlib import Path

import httpx

from front_helpers import Signed, caller, machine_key
import pytest
from starlette.testclient import TestClient

from voice import prompt_recordings as prompts
from voice.service import build_app


def bundle(speaker=26):
    return dict(format=1, sample_rate=24000, speaker=speaker, revision=str(speaker % 10) * 64,
                audio={name: base64.b64encode(b'\x10\x27' * 240).decode() for name in prompts.TEXTS})


def test_cache_installs_only_complete_recordings_and_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / 'voice-prompts.json'
    cache = prompts.PromptCache(path)
    assert not cache.audio
    cache.install(bundle())
    assert prompts.PromptCache(path).audio == cache.audio
    previous = path.read_bytes()
    broken = bundle(3)
    broken['audio'].pop('ready')
    with pytest.raises(ValueError):
        cache.install(broken)
    assert path.read_bytes() == previous and cache.bundle['speaker'] == 26
    def fail(*args):
        raise OSError('simulated disk error')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        cache.install(bundle(3))
    assert path.read_bytes() == previous and cache.bundle['speaker'] == 26
    assert list(tmp_path.iterdir()) == [path]


def test_direct_refresh_reuses_etag_and_preserves_cache_when_compute_is_offline(tmp_path):
    cache = prompts.PromptCache(tmp_path / 'voice-prompts.json')
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url.host == 'compute' and request.url.path == '/voice/prompts'
        assert request.headers['authorization'] == 'Bearer gateway-key'
        assert request.url.params['profile'] == 'default'
        if len(requests) == 1:
            return httpx.Response(200, json=bundle())
        assert request.headers['if-none-match'] == '"' + cache.revision + '"'
        if len(requests) == 2:
            return httpx.Response(304)
        raise httpx.ConnectError('compute asleep')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await prompts.refresh(cache, http, 'http://compute', 'gateway-key')
            await prompts.refresh(cache, http, 'http://compute', 'gateway-key')
            with pytest.raises(httpx.ConnectError):
                await prompts.refresh(cache, http, 'http://compute', 'gateway-key')
    asyncio.run(run())
    assert cache.bundle['speaker'] == 26
    assert prompts.PromptCache(cache.path).revision == cache.revision


def test_speaker_change_regenerates_all_phrases_and_never_publishes_a_stale_set(tmp_path):
    key = tmp_path / 'service.key'
    key.write_text('service')
    settings = dict(key_file=str(key), compute='http://turnstile', machine_key_file=machine_key(tmp_path),
                    profiles={'default': dict(model='small', speaker=26)}, synthesis={})
    class Speech:
        ready_since = None
        calls = []
        change_during_render = False
        async def warm(self):
            self.ready_since = 1
        async def say(self, text, speaker):
            self.calls.append((text, speaker))
            if self.change_during_render:
                settings['profiles']['default']['speaker'] = 26
            return b'\x10\x27' * 240
        def close(self):
            pass
    speech = Speech()
    # Who asks is said by turnstile's statement: the gateway's own account, or one that may not.
    gateway, plain = Signed(caller('gateway', ['follow-settings'])), Signed(caller('plain'))
    with TestClient(build_app(settings, speech, httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))))) as client:
        assert client.get('/voice/prompts').status_code == 401
        assert client.get('/voice/prompts', headers={'authorization': 'Bearer gateway'}).status_code == 401  # a key means nothing here
        assert client.get('/voice/prompts', auth=plain).status_code == 403
        assert not speech.calls and speech.ready_since is None
        headers = {}
        response = client.get('/voice/prompts', auth=gateway, headers=headers)
        assert response.status_code == 200 and response.json()['speaker'] == 26
        assert len(speech.calls) == len(prompts.TEXTS)
        assert all(speaker == 26 for _, speaker in speech.calls)
        headers['if-none-match'] = response.headers['etag']
        assert client.get('/voice/prompts', auth=gateway, headers=headers).status_code == 304
        assert len(speech.calls) == len(prompts.TEXTS)
        settings['profiles']['default']['speaker'] = 3
        response = client.get('/voice/prompts', auth=gateway, headers=headers)
        assert response.status_code == 200 and response.json()['speaker'] == 3
        assert len(speech.calls) == 2 * len(prompts.TEXTS)
        headers['if-none-match'] = response.headers['etag']
        settings['profiles']['default']['speaker'] = 4
        speech.change_during_render = True
        assert client.get('/voice/prompts', auth=gateway, headers=headers).status_code == 503
        speech.change_during_render = False
        response = client.get('/voice/prompts', auth=gateway, headers=headers)
        assert response.status_code == 200 and response.json()['speaker'] == 26
        assert client.get('/voice/prompts?profile=missing', auth=gateway, headers=headers).status_code == 404


def test_bad_audio_never_replaces_the_cache(tmp_path):
    cache = prompts.PromptCache(tmp_path / 'voice-prompts.json')
    cache.install(bundle())
    for value in ('?', base64.b64encode(b'odd').decode(), '', 'A' * 1280001):
        broken = deepcopy(bundle(3))
        broken['audio']['connecting'] = value
        with pytest.raises(ValueError):
            cache.install(broken)
        assert cache.bundle['speaker'] == 26
