"""Simulate greeting packets and desktop DAC timing without opening sound hardware.

Compare the old relative sleeps/no reserve with the production sender and desktop buffer.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice.desktop_audio import Audio
from voice.gateway import prompt_audio


def packets(pcm, legacy):
    """Record packets with 2 ms of send overhead, as if a busy network loop sent them."""
    clock, result = [0.0], []
    async def sleep(delay):
        """Advance the fixture's simulated playback clock."""
        clock[0] += delay
    async def output(frame):
        """Record a PCM output packet and its playback timing."""
        result.append((clock[0], frame))
        clock[0] += .002
    async def send_old():
        """Send a deliberately stale fixture packet to verify rejection."""
        for offset in range(0, len(pcm), 960):
            await output(pcm[offset:offset + 960])
            await sleep(.02)
    with patch('voice.gateway.time', SimpleNamespace(monotonic=lambda: clock[0])), \
         patch('voice.gateway.asyncio', SimpleNamespace(sleep=sleep)):
        asyncio.run(send_old() if legacy else prompt_audio(pcm, output))
    return result


def replay(pcm, legacy):
    """Run the actual PCM conversion and playback buffer against a 10 ms device clock."""
    sent = packets(pcm, legacy)
    audio = Audio()
    audio.event(dict(type='turn.started', turn=-1))
    audio.event(dict(type='playback.segment', turn=-1, segment=-1, samples=len(pcm)//2))
    # Isolate playback; the real echo processor is covered by the echo test suite.
    audio.echo.render = lambda *args, **kwargs: None
    index, began, gaps, output = 0, False, 0, []
    for tick in range(int((sent[-1][0] + 1) * 100)):
        now = tick / 100
        while index < len(sent) and sent[index][0] <= now + 1e-8:
            audio.receive(sent[index][1])
            index += 1
        if legacy:
            audio.buffering = False
        queued = audio.queued_bytes
        frame = audio.render(480, at=now)
        consumed = queued - audio.queued_bytes
        if began and index < len(sent) and consumed < len(frame):
            gaps += len(frame) - consumed
        began |= consumed > 0
        output.append(frame)
        audio.reports(now + .01)
    result = dict(mode='legacy' if legacy else 'buffered', interior_silence_ms=round(gaps / 96, 2),
                  last_packet_seconds=round(sent[-1][0], 3), source_seconds=round(len(pcm)/48000, 3))
    assert legacy or gaps == 0, result
    return result


if __name__ == '__main__':
    report = []
    for name in ('connecting', 'preparing', 'ready'):
        pcm = (Path('voice/prompts') / (name + '.pcm')).read_bytes()
        for legacy in (True, False):
            row = dict(prompt=name, **replay(pcm, legacy))
            report.append(row)
            print(json.dumps(row))
    Path('voice-test/playback-quality.json').write_text(json.dumps(report, indent=2) + '\n')
