"""Real WebRTC AEC on generated speech, room reflections and simultaneous caller speech."""

import audioop
from collections import deque
from pathlib import Path

import numpy as np
import pytest

from voice.desktop_audio import Audio
from voice.echo import Echo
from voice.telephone import Conversion


def speech(name, rate, seconds, current=False):
    """Repeat the checked-in TTS fixture; no microphone, model or network is needed."""
    # Keep the caller/reference baseline fixed when production greetings are re-recorded.
    folder = Path(__file__).parents[1] / "voice/prompts" if current else Path(__file__).parent / "fixtures/voice"
    pcm = (folder / (name + ".pcm")).read_bytes()
    pcm, _ = audioop.ratecv(pcm, 2, 1, 24000, rate, None)
    return np.resize(np.frombuffer(pcm, dtype="<i2"), int(rate * seconds)).astype(float)


def pcm(samples):
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


def longest_speech(samples):
    """Match the service's consecutive 20 ms loud-frame interruption rule."""
    longest = current = 0
    for i in range(0, len(samples) - 319, 320):
        current = current + 20 if np.sqrt(np.mean(samples[i:i + 320] ** 2)) > 600 else 0
        longest = max(longest, current)
    return longest


@pytest.mark.parametrize("delay", [.04, .12, .24])
@pytest.mark.parametrize("current_greeting", [False, True])
def test_echo_removed_and_double_talk_preserved(delay, current_greeting):
    far = speech("connecting", 16000, 10, current=current_greeting)
    caller = np.zeros_like(far)
    caller[64000:112000] = speech("ready", 16000, 3)
    echo = np.zeros_like(far)
    for extra, gain in [(0, .6), (.021, .15), (.04, .08)]:
        shift = int((delay + extra) * 16000)
        echo[shift:] += far[:-shift] * gain
    aec = Echo()
    cleaned = []
    for i in range(0, len(far), 320):
        at = 10 + i / 16000
        aec.render(pcm(far[i:i + 320]), 16000, at=at)
        cleaned.append(aec.clean(pcm(echo[i:i + 320] + caller[i:i + 320]), at=at))
    result = np.frombuffer(b"".join(cleaned), dtype="<i2").astype(float)
    assert longest_speech(result[:64000]) < 120  # no self-interruption, including startup
    assert longest_speech(result[64000:112000]) >= 120  # caller still interrupts
    erle = 10 * np.log10(np.mean(echo[16000:60000] ** 2) / (np.mean(result[16000:60000] ** 2) + 1))
    assert erle > 15
    # AEC adds a small algorithmic delay; compare speech after allowing up to 20 ms.
    correlation = max(np.corrcoef(caller[68000:108000], result[68000 + lag:108000 + lag])[0, 1]
                      for lag in range(321))
    assert correlation > .6  # high-pass filtering and residual suppression change the waveform


@pytest.mark.parametrize("rate", [44100, 48000])
def test_desktop_real_playback_feedback_and_simultaneous_voice(rate):
    audio = Audio()
    audio.input_rate = audio.output_rate = rate
    audio.capture.set()
    far = speech("connecting", 24000, 8)
    caller = speech("ready", rate, 3)
    audio.event(dict(type="turn.started", turn=1))
    audio.event(dict(type="playback.segment", turn=1, segment=1, samples=len(far)))
    delayed = deque([bytes(rate // 50 * 2)] * 6)
    cleaned = []
    for frame in range(400):
        at = 10 + frame * .02
        audio.receive(pcm(far[frame * 480:(frame + 1) * 480]))
        played = audio.render(rate // 50, at=at)
        mic = audioop.mul(delayed.popleft(), 2, .65)
        delayed.append(played)
        if 200 <= frame < 350:
            i = (frame - 200) * (rate // 50)
            mic = audioop.add(mic, pcm(caller[i:i + rate // 50]), 2)
        audio.feed(mic, now=at)
        while not audio.incoming.empty():
            cleaned.append(audio.incoming.get_nowait())
        audio.reports(at + .02)
    result = np.frombuffer(b"".join(cleaned), dtype="<i2").astype(float)
    assert longest_speech(result[:63000]) < 120
    assert longest_speech(result[65000:111000]) >= 120
    assert audio.problem is None


def test_telephone_mulaw_feedback_and_voice_after_clear():
    phone = Conversion()
    far = speech("connecting", 24000, 8)
    caller = speech("ready", 8000, 3)
    delayed = deque([b"\xff" * 160] * 10)
    cleaned = []
    for frame in range(400):
        at = 10 + frame * .02
        played = phone.send(pcm(far[frame * 480:(frame + 1) * 480]), at=at)
        mic = audioop.mul(audioop.ulaw2lin(delayed.popleft(), 2), 2, .65)
        delayed.append(played)
        if 200 <= frame < 350:
            i = (frame - 200) * 160
            mic = audioop.add(mic, pcm(caller[i:i + 160]), 2)
        cleaned.append(phone.receive(audioop.lin2ulaw(mic, 2), at=at))
    result = np.frombuffer(b"".join(cleaned), dtype="<i2").astype(float)
    assert longest_speech(result[:63000]) < 120
    assert longest_speech(result[65000:111000]) >= 120
    phone.send(pcm(far[:4800]), at=18)
    phone.clear(at=18.02)
    assert phone.play_until == 18.02
    assert not np.any(phone.echo.positions >= round(18.02 * 16000))


def test_echo_reference_is_bounded_and_rejects_partial_frames():
    echo = Echo()
    for at in range(100):
        echo.render(bytes(32000), 16000, at=at)
    assert len(echo.samples) == len(echo.positions) == 48000
    with pytest.raises(ValueError, match="whole 10 ms"):
        echo.clean(bytes(318))


def test_telephone_reference_does_not_turn_packet_jitter_into_audio_gaps(monkeypatch):
    phone = Conversion()
    starts = []
    monkeypatch.setattr(phone.echo, "render", lambda pcm, rate, at: starts.append(at))
    for frame in range(150):
        phone.send(bytes(960), at=10 + frame * .0204)
    assert starts == pytest.approx([10 + frame * .02 for frame in range(150)])
    phone.send(bytes(960), at=14)
    assert starts[-1] == 14
