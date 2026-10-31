"""Replay a WAV through the native API-key client, optionally including its Tk window.

No microphone or speaker is opened. The real audio callback buffers are exercised at 48 kHz,
with simulated device-time playback acknowledgements. Artifacts stay in ignored voice-test/.
"""

import argparse
import asyncio
import audioop
from collections import deque
import json
from pathlib import Path
import threading
import time
import wave

from voice.desktop import App, conversation
from voice.desktop_audio import Audio


class Replay(Audio):
    """Replace devices with a real-time 48 kHz producer and consumer, retaining production buffers."""

    def __init__(self, path, echo=False, interruption=None):
        """Initialize the verification playback device and observed audio counters."""
        super().__init__()
        with wave.open(str(path), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise ValueError("Use a mono 16-bit WAV")
            self.pcm, _ = audioop.ratecv(source.readframes(source.getnframes()), 2, 1, source.getframerate(), 48000, None)
        self.stopping = threading.Event()
        self.recording = bytearray()
        self.spoke_at = None
        self.clears = 0
        self.simulated_echo = echo
        self.interruption = b""
        if interruption:
            with wave.open(str(interruption), "rb") as source:
                if source.getnchannels() != 1 or source.getsampwidth() != 2:
                    raise ValueError("Use a mono 16-bit interruption WAV")
                self.interruption, _ = audioop.ratecv(source.readframes(source.getnframes()), 2, 1,
                                                      source.getframerate(), 48000, None)
        self.reply_at = None

    def open(self):
        """Open the replay audio device with the requested format."""
        self.thread = threading.Thread(target=self.play, daemon=True)
        self.thread.start()

    def play(self):
        """Queue received PCM for the desktop verification playback."""
        position, started = 0, None
        interrupt_position = 0
        deadline = time.monotonic()
        feedback = deque([bytes(1920)] * 6)  # speaker pickup after 120 ms
        while not self.stopping.is_set():
            now = time.monotonic()
            playback = self.render(960, at=now)
            echo = feedback.popleft()
            feedback.append(audioop.mul(playback, 2, .65) if self.simulated_echo else bytes(1920))
            if self.capture.is_set():
                if started is None:
                    started = now + .3
                if self.reply_at and now >= self.reply_at + .7 and interrupt_position < len(self.interruption):
                    part = self.interruption[interrupt_position:interrupt_position + 1920]
                    self.feed(audioop.add(part + bytes(1920 - len(part)), echo, 2), now=now)
                    interrupt_position += len(part)
                elif now >= started and position < len(self.pcm):
                    part = self.pcm[position:position + 1920]
                    self.feed(audioop.add(part + bytes(1920 - len(part)), echo, 2), now=now)
                    position += len(part)
                    if position == len(self.pcm):
                        self.spoke_at = now + .02
                else:
                    self.feed(echo, now=now)
            else:
                self.feed(echo, now=now)  # train AEC during the gateway greeting, as real capture does
            self.recording.extend(playback)
            deadline += .02
            self.stopping.wait(max(0, deadline - time.monotonic()))

    def event(self, event):
        """Record a playback device event during the verification run."""
        if event["type"] == "playback.segment" and event.get("turn", -1) >= 0 and self.reply_at is None:
            self.reply_at = time.monotonic()
        if event["type"] == "playback.clear" and event.get("turn", -1) >= 0:
            self.clears += 1
        super().event(event)

    def close(self):
        """Close the replay output and release its audio resources."""
        self.stopping.set()
        if hasattr(self, "thread"):
            self.thread.join(2)
        super().close()


def main(args):
    """Run the desktop check voice verification command."""
    key = args.key_file.read_text().strip()
    output = Path("voice-test")
    output.mkdir(exist_ok=True)
    audio = Replay(args.audio, echo=args.echo, interruption=args.interrupt_audio)
    report = {"mode": args.mode, "transcript": [], "answer": [], "errors": []}
    complete_at = None

    def event(value):
        """Record one observed desktop voice protocol event."""
        nonlocal complete_at
        kind = value["type"]
        if kind == "transcript.final":
            report["transcript"].append(value["text"])
        elif kind == "playback.segment" and value["turn"] >= 0:
            report["answer"].append(value["text"])
            if "first_segment_seconds" not in report and audio.spoke_at:
                report["first_segment_seconds"] = time.monotonic() - audio.spoke_at
        elif kind == "session.error":
            report["errors"].append(value.get("code"))
        elif kind == "turn.completed" and value["turn"] >= 0:
            if not args.interrupt_audio or len(report["transcript"]) >= 2:
                complete_at = time.monotonic() + .5

    if args.ui:
        import tkinter as tk
        root = tk.Tk()
        app = App(root, audio_factory=lambda *_: audio, settings_path=output / "desktop-ui-preferences.json")
        app.server.set(args.url)
        app.key.set(key)
        app.mode.set(args.mode)
        original = audio.event

        def observe(value):
            """Record measured playback progress for verification evidence."""
            original(value)
            event(value)

        audio.event = observe
        app.start()
        expires = time.monotonic() + 90

        def watch():
            """Observe desktop voice state until completion or the verification deadline."""
            if (complete_at and time.monotonic() >= complete_at) or time.monotonic() > expires:
                if complete_at:
                    from PIL import ImageGrab
                    ImageGrab.grab().save(output / "desktop-ui.png")
                    report["window_transcript"] = app.transcript.get("1.0", "end").strip()
                app.close()
            else:
                root.after(50, watch)

        root.after(50, watch)
        root.mainloop()
    else:
        async def run():
            """Run the bounded desktop voice connection and fixture exchange."""
            stop = threading.Event()
            task = asyncio.create_task(conversation(args.url, key, args.mode, audio, event, stop))
            try:
                async with asyncio.timeout(90):
                    while not task.done() and not (complete_at and time.monotonic() >= complete_at):
                        await asyncio.sleep(.05)
                    stop.set()
                    await task
            finally:
                stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())
    report["clears"] = audio.clears
    report["completed"] = complete_at is not None
    report["speaker_echo"] = args.echo
    (output / "desktop-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    assert report["transcript"] and report["answer"] and not report["errors"], report
    if args.echo:
        assert report["completed"] and report["clears"] == (1 if args.interrupt_audio else 0), report
    if args.interrupt_audio:
        assert len(report["transcript"]) >= 2, report
    with wave.open(str(output / "desktop-output.wav"), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(48000)
        file.writeframes(audio.recording)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--url", default="http://10.77.10.42:8447")
    parser.add_argument("--mode", choices=("direct", "hermes"), default="direct")
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--echo", action="store_true", help="Feed delayed speaker output back into the mock microphone")
    parser.add_argument("--interrupt-audio", type=Path, help="Speak this WAV over the first reply, with the microphone live")
    main(parser.parse_args())
