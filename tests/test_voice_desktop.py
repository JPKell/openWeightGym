"""Native PCM timing, backpressure, interruption and API-key handling without sound hardware."""

import asyncio
from contextlib import asynccontextmanager
import json
import queue
import os
from pathlib import Path
import threading

import pytest

from voice.desktop import conversation, endpoint, read_key, save_key, save_preferences
from voice.desktop_audio import Audio


def segment(audio, turn=1, segment=1, samples=480):
    audio.event(dict(type="turn.started", turn=turn))
    audio.event(dict(type="playback.segment", turn=turn, segment=segment, samples=samples))


def test_native_rates_and_microphone_bounds():
    for rate in (44100, 48000):
        audio = Audio()
        audio.input_rate = rate
        audio.feed(bytes(rate // 50 * 2))
        assert audio.incoming.empty()  # no early microphone audio
        audio.capture.set()
        for _ in range(20):
            audio.feed(b"\x10\x27" * (rate // 50))
        frames = []
        while not audio.incoming.empty():
            frames.append(audio.incoming.get_nowait())
        assert 19 <= len(frames) <= 20 and all(len(frame) == 640 for frame in frames)
        assert len(audio.input_tail) < 640
        audio.muted.set()
        audio.feed(b"\x10\x27" * (rate // 50))
        assert not any(audio.incoming.get_nowait())
        for _ in range(30):
            audio.feed(bytes(rate // 50 * 2))
        assert audio.incoming.qsize() == 25 and audio.problem


@pytest.mark.parametrize("acknowledge",[False,True])
def test_playback_reports_wait_for_device_time_and_clear_discards_old_sound(acknowledge):
    audio = Audio()
    audio.event(dict(type="session.ready",queue_clear_ack=acknowledge))
    segment(audio)
    audio.receive(b"\x10\x27" * 480)
    first = audio.render(480, at=10)
    assert any(first) and not audio.reports(10.009)
    audio.render(480, at=10.01)
    assert not audio.reports(10.015)
    assert audio.reports(10.03) == [dict(type="playback.progress", turn=1, segment=1, samples=480)]
    segment(audio, turn=2)
    audio.receive(b"\x10\x27" * 480)
    audio.render(960, at=20)
    audio.event(dict(type="playback.clear", turn=2))
    if acknowledge:
        assert audio.controls.get_nowait() == dict(type="playback.cleared", turn=2)
    else:
        assert audio.controls.empty()
    assert not audio.reports(21)  # a DAC-time report queued before clear is discarded
    audio.receive(b"\x10\x27" * 480)
    assert not any(audio.render(960))  # late audio is dropped until turn.started
    segment(audio, turn=3)
    audio.receive(b"\x10\x27" * 480)
    assert any(audio.render(960))


def test_playback_is_bounded_and_keeps_each_segments_progress():
    audio = Audio()
    segment(audio, samples=24000 * 2)
    with pytest.raises(ValueError, match="cannot keep up"):
        for _ in range(60):
            audio.receive(bytes(960))
    assert audio.queued_bytes <= audio.output_rate * 2
    audio.clear()
    for number in (1, 2):
        segment(audio, segment=number)
        audio.receive(bytes(960))
    audio.render(1920, at=10)
    assert [r["segment"] for r in audio.reports(11)] == [1, 2]


def test_manual_interrupt_still_clears_playback():
    audio = Audio()
    segment(audio)
    audio.receive(b"\x10\x27" * 480)
    audio.control("session.cancel", interrupt=True)
    assert not any(audio.render(960))
    assert audio.controls.get_nowait() == dict(type="session.cancel", interrupt=True)


def test_preferences_never_save_keys_and_urls_cannot_contain_credentials(tmp_path):
    file = tmp_path / "settings.json"
    save_preferences(file, dict(server="http://front:8447", mode="direct", key="secret", transcript="private"))
    assert json.loads(file.read_text()) == dict(server="http://front:8447", mode="direct")
    assert endpoint("https://front/", "hermes") == "wss://front/desktop/stream?conversation_mode=hermes"
    for wrong in ("http://key@front", "http://front/?key=secret", "file:///tmp", "http://front/path", "http://front/#secret"):
        with pytest.raises(ValueError):
            endpoint(wrong, "direct")


def test_client_uses_header_and_cleans_up_devices_on_disconnect(monkeypatch):
    async def check():
        stop = threading.Event()
        events, sent = [], []

        class Device(Audio):
            def open(self):
                self.opened = True
            def close(self):
                self.closed = True
                super().close()

        class Socket:
            async def send(self, value):
                sent.append(value)
            async def __aiter__(self):
                yield json.dumps(dict(type="session.ready"))
                stop.set()
                await asyncio.sleep(1)

        @asynccontextmanager
        async def connect(uri, **options):
            assert "secret-key" not in uri
            assert options["additional_headers"] == {"authorization": "Bearer secret-key"}
            assert options["proxy"] is None
            yield Socket()

        monkeypatch.setattr("voice.desktop.connect", connect)
        audio = Device()
        await conversation("http://front:8447", "secret-key", "direct", audio, events.append, stop)
        assert audio.opened and audio.closed and not audio.capture.is_set()
        assert any(json.loads(s)["type"] == "session.cancel" for s in sent)
    asyncio.run(check())


def test_key_file_roundtrip_replacement_and_removal(tmp_path):
    path = tmp_path / "profile" / "voice-desktop.key"
    assert read_key(path) == ""
    save_key(path, "  first-test-key\n")
    assert read_key(path) == "first-test-key"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        path.chmod(0o644)
    save_key(path, "replacement-test-key")
    assert read_key(path) == "replacement-test-key"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]
    save_key(path, "")
    assert read_key(path) == ""
    save_key(path, "")  # clearing an already absent file is harmless


def test_invalid_keys_and_failed_replace_preserve_saved_key(tmp_path, monkeypatch):
    path = tmp_path / "voice-desktop.key"
    save_key(path, "existing-test-key")
    for value in ("contains spaces", "embedded\nnewline", "x" * 513, "nonascii-é"):
        with pytest.raises(ValueError):
            save_key(path, value)
        assert read_key(path) == "existing-test-key"
    def fail_replace(*args):
        raise PermissionError("simulated write failure")
    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(PermissionError):
        save_key(path, "new-test-key")
    assert read_key(path) == "existing-test-key"
    assert list(tmp_path.iterdir()) == [path]
    path.write_text("invalid key")
    with pytest.raises(ValueError):
        read_key(path)


def test_playback_buffers_packet_jitter_without_delaying_short_tails():
    audio = Audio()
    segment(audio, samples=4800)
    frame = b"\x10\x27" * 480
    audio.receive(frame)
    assert not any(audio.render(480, at=10))  # wait for a short reserve
    assert audio.reports(11) == []  # silence never acknowledges unheard sound
    audio.receive(frame)
    audio.receive(frame)
    assert any(audio.render(480, at=10.02))
    assert any(audio.render(480, at=10.03))
    assert audio.reports(11)[0]["samples"] == 480
    audio.event(dict(type="playback.clear", turn=1))
    assert not any(audio.render(480, at=12))
    assert audio.reports(13) == []
    segment(audio, turn=2, samples=100)
    audio.receive(frame[:200])
    assert any(audio.render(480, at=14))  # final short packet must not wait for more
    assert audio.reports(15) == [dict(type="playback.progress", turn=2, segment=1, samples=100)]
