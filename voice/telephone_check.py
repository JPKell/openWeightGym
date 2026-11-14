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
from websockets.exceptions import WebSocketException
from voice.telephone import signature


class CheckFailed(ValueError):
    def __init__(self, stages):
        super().__init__('The simulated call did not complete. Check caller access, the public route and Voice status.')
        self.stages = stages


async def main(args):
    """Run the shared protocol check, returning safe stage evidence on failure."""
    args.telephone_stages = []
    try:
        return await _check(args)
    except (httpx.HTTPError, OSError, WebSocketException, TimeoutError, ElementTree.ParseError, AssertionError, ValueError):
        stages = args.telephone_stages
        names = ['Signed webhook', 'Signed media stream', 'Spoken reply', 'Playback marks']
        stages.extend({'stage': name, 'passed': False, 'ms': None} for name in names if name not in {row['stage'] for row in stages})
        raise CheckFailed(stages) from None


async def _check(args):
    """Run the phone check voice verification command."""
    token = args.token_file.read_text().strip()
    call, stream = "CA" + secrets.token_hex(16), "MZ" + secrets.token_hex(16)
    form = {"CallSid": call, "AccountSid": args.account, "From": getattr(args, "caller", "+15555550123")}
    if getattr(args, "to_number", None):
        form["To"] = args.to_number
    stages = args.telephone_stages
    phase = time.monotonic()
    sig = signature(token, args.public_url + "/voice/inbound", {k: [v] for k, v in form.items()})
    async with httpx.AsyncClient() as http:
        reply = await http.post(args.url + "/voice/inbound", data=form, headers={"x-twilio-signature": sig})
        reply.raise_for_status()
    parameters = {p.attrib["name"]: p.attrib["value"] for p in ElementTree.fromstring(reply.text).findall(".//Parameter")}
    if not parameters.get('session') or not parameters.get('nonce'):
        raise ValueError('The signed webhook did not admit this caller.')
    stages.append({"stage": "Signed webhook", "ms": round((time.monotonic()-phase)*1000, 1), "passed": True})
    print("Signed webhook accepted", flush=True)
    if getattr(args, 'audio_pcm', None) is not None:
        pcm, _ = audioop.ratecv(args.audio_pcm, 2, 1, 24000, 8000, None)
    else:
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
    pending_audio_at = None
    interrupt_position = 0
    last_loud, speech_began = 0, None
    interruption_ms = []
    feedback = deque(maxlen=100)
    began = time.monotonic()
    async with connect(url, additional_headers={"x-twilio-signature": signed}, max_size=16384) as ws:
        stages.append({"stage": "Signed media stream", "ms": round((time.monotonic()-began)*1000, 1), "passed": True})
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
            nonlocal first_answer, marks, answer_started, pending_audio_at
            play_until = time.monotonic()
            while True:
                item = await playback.get()
                if item["event"] == "media":
                    sound = base64.b64decode(item["media"]["payload"])
                    pending_audio_at = time.monotonic()
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
                    name = item['mark'].get('name', '')
                    if stopped_speaking and first_answer is None and name.startswith('t') and ':' in name:
                        try:
                            is_reply = int(name.split(':')[0][1:]) > 0
                        except ValueError:
                            is_reply = False
                        if is_reply and pending_audio_at is not None:
                            answer_started = pending_audio_at
                            first_answer = max(0, answer_started - stopped_speaking)
                    await ws.send(json.dumps({"event": "mark", "streamSid": stream, "mark": item["mark"]}))

        playing = asyncio.create_task(player())
        try:
            async with asyncio.timeout(args.wait + len(mulaw) / 8000 + getattr(args, 'reply_seconds', (24 if interruption else 16))):
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
    output = None
    if getattr(args, 'persist', True):
        folder = Path('voice-test'); folder.mkdir(parents=True, exist_ok=True)
        output = folder / (call + '.wav')
        with wave.open(str(output), 'wb') as file:
            file.setnchannels(1); file.setsampwidth(2); file.setframerate(8000)
            file.writeframes(audioop.ulaw2lin(b''.join(received), 2))
    report = {"recording": str(output), "audio_seconds": sum(map(len, received))/8000,
                      "marks": marks, "clears": clears, "first_audio_seconds": first_answer,
                      "interruption_ms": interruption_ms,
                      "speaker_echo": args.echo,
                      "signature_form": args.signature_form,
                      "interruption_sent": bool(interrupt_position),
                      "elapsed": time.monotonic()-began}
    stages.extend([{'stage': 'Spoken reply', 'ms': round(first_answer*1000, 1) if first_answer is not None else None,
                    'passed': bool(received) and first_answer is not None},
                   {'stage': 'Playback marks', 'ms': round((time.monotonic()-began)*1000, 1), 'passed': bool(marks)}])
    report['stages'] = stages
    if output:
        output.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    assert received and marks and first_answer is not None, "Telephone call ended without playback audio"
    if interruption:
        assert interrupt_position >= len(interruption) and clears == 1, "Expected one voice interruption"

    return report
