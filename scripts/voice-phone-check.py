"""Simulate a signed Twilio inbound call and real-time media/mark playback.

No telephone is dialled. The same public webhook and WebSocket protocol are exercised.
"""

import argparse
import asyncio
import audioop
import base64
from collections import deque
import json
from pathlib import Path
import secrets
import time
import wave
from xml.etree import ElementTree

import httpx
from websockets.asyncio.client import connect
from voice.telephone import signature


async def main(args):
    """Run the phone check voice verification command."""
    token = args.token_file.read_text().strip()
    call, stream = "CA" + secrets.token_hex(16), "MZ" + secrets.token_hex(16)
    form = {"CallSid": call, "AccountSid": args.account, "From": "+15555550123"}
    sig = signature(token, args.public_url + "/voice/inbound", {k: [v] for k, v in form.items()})
    async with httpx.AsyncClient() as http:
        reply = await http.post(args.url + "/voice/inbound", data=form, headers={"x-twilio-signature": sig})
        reply.raise_for_status()
    parameters = {p.attrib["name"]: p.attrib["value"] for p in ElementTree.fromstring(reply.text).findall(".//Parameter")}
    print("Signed webhook accepted", flush=True)
    with wave.open(str(args.audio), "rb") as source:
        pcm, _ = audioop.ratecv(source.readframes(source.getnframes()), 2, 1, source.getframerate(), 8000, None)
    mulaw = audioop.lin2ulaw(pcm, 2)
    interruption = b""
    if args.interrupt_audio:
        with wave.open(str(args.interrupt_audio), "rb") as source:
            pcm, _ = audioop.ratecv(source.readframes(source.getnframes()), 2, 1, source.getframerate(), 8000, None)
        interruption = audioop.lin2ulaw(pcm, 2)
    # Independently reproduce Twilio's URL forms instead of copying gateway normalization.
    signing_url = args.public_url + "/voice/stream"
    if args.signature_form.startswith("wss"):
        signing_url = signing_url.replace("https://", "wss://").replace("http://", "ws://")
    if args.signature_form.endswith("slash"):
        signing_url += "/"
    signed = signature(token, signing_url, {})
    url = args.url.replace("http://", "ws://").replace("https://", "wss://") + "/voice/stream"
    received, marks, clears = [], 0, 0
    stopped_speaking, first_answer = None, None
    answer_started = None
    interrupt_position = 0
    last_loud, speech_began = 0, None
    interruption_ms = []
    feedback = deque(maxlen=100)
    began = time.monotonic()
    async with connect(url, additional_headers={"x-twilio-signature": signed}, max_size=16384) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({"event": "start", "streamSid": stream, "start": {
            "accountSid": args.account, "callSid": call, "streamSid": stream, "customParameters": parameters,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))

        async def microphone():
            """Send the selected verification recording as telephone audio frames."""
            nonlocal stopped_speaking, last_loud, speech_began, interrupt_position
            audio = b"\xff" * (8000 * args.wait) + mulaw + b"\xff" * 8000 * 30
            clock = time.monotonic()
            for i in range(0, len(audio), 160):
                # A capture packet is available after its samples have been recorded.
                await asyncio.sleep(max(0, clock + (i + 160) / 8000 - time.monotonic()))
                now = time.monotonic()
                frame = audio[i:i + 160]
                if answer_started and now >= answer_started + .7 and interrupt_position < len(interruption):
                    frame = interruption[interrupt_position:interrupt_position + 160].ljust(160, b"\xff")
                    interrupt_position += 160
                caller_frame = frame
                if args.echo:
                    echo = bytearray(len(frame) * 2)
                    capture_at = clock + i / 8000
                    # Align sample positions, including partial TTS frames. Copying whole
                    # packets on 20 ms ticks creates artificial jumps in the echo path.
                    for at, sound in feedback:
                        offset = round((at - capture_at) * 8000)
                        source, target = max(0, -offset), max(0, offset)
                        count = min(len(sound) // 2 - source, len(frame) - target)
                        if count > 0:
                            echo[target * 2:(target + count) * 2] = sound[source * 2:(source + count) * 2]
                    while feedback and feedback[0][0] + len(feedback[0][1]) / 16000 <= capture_at + len(frame) / 8000:
                        feedback.popleft()
                    frame = audioop.lin2ulaw(audioop.add(audioop.ulaw2lin(frame, 2), bytes(echo), 2), 2)
                if audioop.rms(audioop.ulaw2lin(caller_frame, 2), 2) > 600:
                    if now - last_loud > .6:
                        speech_began = now
                    last_loud = now
                await ws.send(json.dumps({"event": "media", "streamSid": stream,
                                          "media": {"track": "inbound", "timestamp": str(i // 8), "payload": base64.b64encode(frame).decode()}}))
                if stopped_speaking is None and i + 160 >= 8000 * args.wait + len(mulaw):
                    stopped_speaking = time.monotonic()

        feeder = asyncio.create_task(microphone())
        playback = asyncio.Queue(maxsize=60)

        async def player():
            """Replay received telephone PCM for the local verification session."""
            nonlocal first_answer, marks, answer_started
            play_until = time.monotonic()
            while True:
                item = await playback.get()
                if item["event"] == "media":
                    sound = base64.b64decode(item["media"]["payload"])
                    if stopped_speaking and first_answer is None:
                        answer_started = time.monotonic()
                        first_answer = answer_started - stopped_speaking
                    received.append(sound)
                    if play_until < time.monotonic() - .1:
                        play_until = time.monotonic()
                    if args.echo:
                        echo = audioop.mul(audioop.ulaw2lin(sound, 2), 2, .65)
                        feedback.append((play_until + .12, echo))
                    play_until += len(sound) / 8000
                    await asyncio.sleep(max(0, play_until - time.monotonic()))
                else:
                    marks += 1
                    await ws.send(json.dumps({"event": "mark", "streamSid": stream, "mark": item["mark"]}))

        playing = asyncio.create_task(player())
        try:
            async with asyncio.timeout(args.wait + len(mulaw) / 8000 + (24 if interruption else 16)):
                async for value in ws:
                    event = json.loads(value)
                    if event["event"] in ("media", "mark"):
                        playback.put_nowait(event)
                    elif event["event"] == "clear":
                        clears += 1
                        if speech_began:
                            interruption_ms.append((time.monotonic() - speech_began) * 1000)
                        while not playback.empty():
                            discarded = playback.get_nowait()
                            if discarded["event"] == "mark":
                                await ws.send(json.dumps({"event": "mark", "streamSid": stream, "mark": discarded["mark"]}))
        except TimeoutError:
            await ws.send(json.dumps({"event": "stop", "streamSid": stream, "stop": {"callSid": call}}))
        finally:
            feeder.cancel()
            playing.cancel()
            await asyncio.gather(feeder, playing, return_exceptions=True)
    output = Path("voice-test") / (call + ".wav")
    with wave.open(str(output), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(audioop.ulaw2lin(b"".join(received), 2))
    report = {"recording": str(output), "audio_seconds": sum(map(len, received))/8000,
                      "marks": marks, "clears": clears, "first_audio_seconds": first_answer,
                      "interruption_ms": interruption_ms,
                      "speaker_echo": args.echo,
                      "signature_form": args.signature_form,
                      "interruption_sent": bool(interrupt_position),
                      "elapsed": time.monotonic()-began}
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    assert received and marks and first_answer is not None, "Telephone call ended without playback audio"
    if interruption:
        assert interrupt_position >= len(interruption) and clears == 1, "Expected one voice interruption"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--url", default="http://localhost:18482")
    parser.add_argument("--public-url", default="https://kellrondgames.com")
    parser.add_argument("--wait", type=int, default=9)
    parser.add_argument("--echo", action="store_true", help="Mix delayed speaker output back into inbound telephone audio")
    parser.add_argument("--interrupt-audio", type=Path, help="Speak this WAV over the first response to test recovery")
    parser.add_argument("--signature-form", choices=("wss", "wss-slash", "https", "https-slash"), default="wss-slash",
                        help="Public URL form used to sign the WebSocket upgrade")
    asyncio.run(main(parser.parse_args()))
