"""Replay a WAV as a continuous microphone, play/acknowledge returned PCM in real time.

This exercises the real model and speech service. It never opens the hardware microphone.
Use --key-file; keys are read privately and never printed. Outputs are under voice-test/.
"""

import argparse
import asyncio
import audioop
import json
from pathlib import Path
import secrets
import time
import wave

import httpx
from websockets.asyncio.client import connect
from voice import protocol


async def main(args):
    """Run the call check voice verification command."""
    key = args.key_file.read_text().strip()
    headers = {"authorization": "Bearer " + key}
    sid = "check-" + secrets.token_hex(8)
    with wave.open(str(args.audio), "rb") as source:
        assert source.getnchannels() == 1 and source.getsampwidth() == 2
        pcm, _ = audioop.ratecv(source.readframes(source.getnframes()), 2, 1, source.getframerate(), 16000, None)
    pcm = bytes(16000) + pcm
    report = {"session": sid, "audio": args.audio.name}
    began = time.monotonic()
    async with httpx.AsyncClient(timeout=65) as http:
        for attempt in range(20):
            try:
                await http.get(args.url + "/voice/ready", headers=headers, timeout=1)
                break
            except httpx.ConnectError:
                if attempt == 19:
                    raise
                await asyncio.sleep(.25)
        result = await http.post(args.url + "/voice/sessions", headers=headers,
                                 json={"session_id": sid, "conversation_mode": "direct"})
        result.raise_for_status()
        report["startup_seconds"] = time.monotonic() - began
        socket_url = args.url.replace("http://", "ws://") + "/voice/stream?session_id=" + sid
        try:
            async with connect(socket_url, additional_headers=headers, max_size=16384) as ws:
                await ws.send(json.dumps(protocol.start(sid, "direct")))
                assert json.loads(await ws.recv())["type"] == "session.ready"
                playback, transcript, texts = [], [], []
                stop_speaking = None

                async def microphone():
                    """Send the verification recording as ordered microphone frames."""
                    nonlocal stop_speaking
                    for i in range(0, len(pcm), 640):
                        await ws.send(pcm[i:i + 640])
                        await asyncio.sleep(.02)
                    stop_speaking = time.monotonic()
                    while True:
                        await ws.send(bytes(640))
                        await asyncio.sleep(.02)

                feed = asyncio.create_task(microphone())
                segment, heard, turn = None, 0, 0
                try:
                    async with asyncio.timeout(60):
                        while True:
                            value = await ws.recv()
                            if isinstance(value, bytes):
                                if "first_audio_seconds" not in report:
                                    report["first_audio_seconds"] = time.monotonic() - (stop_speaking or time.monotonic())
                                playback.append(value)
                                await asyncio.sleep(len(value) / 48000)
                                heard += len(value) // 2
                                await ws.send(json.dumps(dict(type="playback.progress", turn=turn, segment=segment, samples=heard)))
                                continue
                            event = json.loads(value)
                            if event["type"] == "transcript.final":
                                transcript.append(event["text"])
                                report["endpoint_seconds"] = time.monotonic() - stop_speaking
                            elif event["type"] == "playback.segment":
                                segment, turn, heard = event["segment"], event["turn"], 0
                                texts.append(event["text"])
                            elif event["type"] == "turn.completed":
                                break
                            elif event["type"] == "session.error":
                                raise RuntimeError(event["code"])
                finally:
                    feed.cancel()
                    await asyncio.gather(feed, return_exceptions=True)
                report.update(transcript=" ".join(transcript), answer=" ".join(texts), output_seconds=sum(map(len, playback))/48000)
                out = Path("voice-test")
                out.mkdir(exist_ok=True)
                with wave.open(str(out / (sid + ".wav")), "wb") as file:
                    file.setnchannels(1)
                    file.setsampwidth(2)
                    file.setframerate(24000)
                    file.writeframes(b"".join(playback))
                (out / (sid + ".json")).write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report, indent=2))
        finally:
            await http.delete(args.url + "/voice/sessions/" + sid, headers=headers)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--url", default="http://10.77.10.84:8445")
    asyncio.run(main(parser.parse_args()))
