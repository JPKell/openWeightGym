"""Replay a WAV through the deployed browser gateway protocol, including playback reports."""

import argparse
import asyncio
import audioop
import json
from pathlib import Path
import time
import wave

import httpx
from websockets.asyncio.client import connect


async def main(args):
    """Run the browser check voice verification command."""
    with wave.open(str(args.audio), "rb") as file:
        pcm, _ = audioop.ratecv(file.readframes(file.getnframes()), 2, 1, file.getframerate(), 16000, None)
    pcm = bytes(int(32000 * args.delay)) + pcm
    began = time.monotonic()
    async with httpx.AsyncClient(timeout=65) as http:
        response = await http.post(args.url + "/browser/sessions", headers={"origin": args.url}, json={"conversation_mode": args.mode})
        response.raise_for_status()
        sid = response.json()["session_id"]
        cookie = "; ".join(k + "=" + v for k, v in http.cookies.items())
    uri = args.url.replace("http://", "ws://").replace("https://", "wss://") + "/browser/sessions/" + sid + "/stream"
    report = {"session": sid, "source": "browser", "transcript": [], "answer": []}
    stopped, feeder = None, None
    async with connect(uri, origin=args.url, additional_headers={"cookie": cookie}) as ws:
        async def microphone():
            """Feed the selected microphone recording into the browser voice session."""
            nonlocal stopped
            for i in range(0, len(pcm), 640):
                await ws.send(pcm[i:i + 640])
                await asyncio.sleep(.02)
            stopped = time.monotonic()
            while True:
                await ws.send(bytes(640))
                await asyncio.sleep(.02)

        turn, segment, played = -1, -1, 0
        try:
            async with asyncio.timeout(65):
                async for value in ws:
                    if isinstance(value, bytes):
                        if turn >= 0 and "first_audio_seconds" not in report:
                            report["first_audio_seconds"] = time.monotonic() - stopped
                        await asyncio.sleep(len(value) / 48000)
                        played += len(value) // 2
                        await ws.send(json.dumps(dict(type="playback.progress", turn=turn, segment=segment, samples=played)))
                        continue
                    event = json.loads(value)
                    if event["type"] == "session.ready":
                        report["startup_seconds"] = time.monotonic() - began
                        feeder = asyncio.create_task(microphone())
                    elif event["type"] == "playback.segment":
                        turn, segment, played = event["turn"], event["segment"], 0
                        if turn >= 0:
                            report["answer"].append(event["text"])
                    elif event["type"] == "transcript.final":
                        report["transcript"].append(event["text"])
                    elif event["type"] == "turn.completed" and event["turn"] >= 0:
                        break
                    elif event["type"] == "session.error":
                        raise RuntimeError(event.get("code"))
        finally:
            if feeder:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
            await ws.send(json.dumps({"type": "session.cancel"}))
    (Path("voice-test") / (sid + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--mode", default="direct")
    parser.add_argument("--url", default="http://localhost:18446")
    parser.add_argument("--delay", type=float, default=.5)
    asyncio.run(main(parser.parse_args()))
