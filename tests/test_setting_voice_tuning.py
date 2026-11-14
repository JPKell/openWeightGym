"""Nested tuning preserves machine paths and the actual service lifetime contract."""
import asyncio
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from loadcoach.setting_catalog import validate_values, validate
from loadcoach.settings import nested_value, with_changes, written
from voice.speech import Speech


def test_followed_nested_tuning_freezes_restart_fields_and_keeps_paths(tmp_path):
    path = tmp_path / 'voice.toml'
    path.write_text(written({'recognition': {'encoder': '/owner/model', 'threads': 2, 'trailing_silence_ms': 800},
                             'synthesis': {'model': '/owner/tts', 'workers': 2}}))
    settings, following = with_changes('voice', path)
    changed = {'recognition.threads': 4, 'recognition.trailing_silence_ms': 1500, 'synthesis.workers': 3}
    reports = []

    def authority(request):
        if request.method == 'POST':
            import json
            reports.append(json.loads(request.content))
        return httpx.Response(200, json=changed if request.method == 'GET' else {})

    async def once():
        async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as client:
            try:
                await asyncio.wait_for(following(client, 'http://authority', 'unit-test-key'), .02)
            except TimeoutError:
                pass

    asyncio.run(once())
    assert settings['recognition'] == {'encoder': '/owner/model', 'threads': 2, 'trailing_silence_ms': 1500,
                                       'decoding_method': 'modified_beam_search', 'max_active_paths': 8, 'max_utterance_seconds': 30}
    assert settings['synthesis']['workers'] == 2 and settings['synthesis']['model'] == '/owner/tts'
    assert reports[-1]['told']['recognition.threads']['value'] == 2
    assert reports[-1]['told']['recognition.trailing_silence_ms']['value'] == 1500
    restarted, _ = with_changes('voice', path)
    assert restarted['recognition']['threads'] == 4 and restarted['synthesis']['workers'] == 3
    changed.clear()
    asyncio.run(once())
    assert settings['recognition']['trailing_silence_ms'] == 800
    assert settings['recognition']['threads'] == 2


@pytest.mark.parametrize('name,value', [('recognition.threads', 0), ('recognition.threads', 65),
    ('recognition.max_active_paths', 0), ('recognition.decoding_method', 'unknown'),
    ('recognition.trailing_silence_ms', 99), ('recognition.max_utterance_seconds', 301),
    ('synthesis.workers', 0), ('synthesis.workers', 17), ('synthesis.speaker', -1)])
def test_nested_loader_validation_matches_the_site_contract(name, value):
    from loadcoach.settings import set_nested
    values = {}
    set_nested(values, name, value)
    with pytest.raises(ValueError): validate('voice', name, value)
    with pytest.raises(ValueError): validate_values('voice', values)


def test_new_sessions_snapshot_endpoint_settings_and_change_real_endpoint_behavior():
    class Native:
        def accept_waveform(self, *args): pass
    class Recognizer:
        def create_stream(self): return Native()
        def is_ready(self, stream): return False
        def get_result_all(self, stream):
            return SimpleNamespace(text='Hello', start_time=0, timestamps=[.1])
        def reset(self, stream): pass
    values = {'recognition': {'trailing_silence_ms': 800, 'max_utterance_seconds': 30}, 'synthesis': {'workers': 1}}
    speech = Speech(values)
    speech.recognizer = Recognizer()
    try:
        existing = speech.stream()
        values['recognition'].update(trailing_silence_ms=1600, max_utterance_seconds=60)
        new = speech.stream()
        assert (existing.trailing_silence_ms, existing.max_utterance_seconds) == (800, 30)
        assert (new.trailing_silence_ms, new.max_utterance_seconds) == (1600, 60)
        voiced = np.full(3200, 1000, dtype='<i2').tobytes()
        silence = bytes(32000)
        speech.feed(existing, voiced, False); speech.feed(new, voiced, False)
        assert speech.feed(existing, silence, False)[1] == 'Hello'
        assert speech.feed(new, silence, False)[1] is None
        assert speech.feed(new, silence, False)[1] == 'Hello'
    finally:
        speech.close()


@pytest.mark.parametrize('program', ['voice', 'voice-gateway'])
def test_real_example_and_default_files_validate_together(tmp_path, program):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'settings'
    (tmp_path / 'defaults').mkdir()
    for destination, source in ((tmp_path / (program + '.toml'), root / 'examples' / (program + '.toml')),
                                (tmp_path / 'defaults' / (program + '.toml'), root / 'defaults' / (program + '.toml'))):
        destination.write_text(source.read_text())
    values, _ = with_changes(program, tmp_path / (program + '.toml'))
    validate_values(program, values)
