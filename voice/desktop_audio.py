"""Bounded microphone and playback buffers shared by the native client and audio tests."""

import audioop
from contextlib import suppress
from collections import deque
from dataclasses import dataclass
import queue
import threading
import time

from voice.echo import Echo


@dataclass
class Chunk:
    """One resampled playback frame and the progress it confirms after reaching the device."""

    pcm: bytes
    report: dict
    offset: int = 0


class Audio:
    """PortAudio callbacks do only bounded PCM work; network and Tk run on other threads."""

    def __init__(self, input_device=None, output_device=None):
        """Configure microphone and playback devices with bounded audio queues."""
        self.input_device, self.output_device = input_device, output_device
        self.input_rate = self.output_rate = 48000
        self.input_channels = self.output_channels = 1
        self.input_stream = self.output_stream = None
        self.capture = threading.Event()
        self.muted = threading.Event()
        self.echo_enabled = threading.Event()
        self.echo_enabled.set()
        self.echo = Echo()
        self.incoming = queue.Queue(maxsize=25)  # 500 ms at 20 ms per frame
        self.controls = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.playback = deque()
        self.pending = deque()
        self.queued_bytes = 0
        self.buffering = True
        self.input_state = self.output_state = None
        self.input_tail = b""
        self.turn = self.segment = None
        self.expected = self.received = 0
        self.accept_audio = False
        self.clear_ack = False
        self.problem = None

    def open(self):
        """Open each selected device at its own native rate, allowing 44.1 and 48 kHz."""
        import sounddevice as sd
        try:
            source = sd.query_devices(self.input_device, "input")
            sink = sd.query_devices(self.output_device, "output")
            self.input_rate, self.output_rate = int(source["default_samplerate"]), int(sink["default_samplerate"])
            self.input_channels = min(2, source["max_input_channels"])
            self.output_channels = min(2, sink["max_output_channels"])
            self.output_stream = sd.RawOutputStream(device=self.output_device, samplerate=self.output_rate,
                channels=self.output_channels, dtype="int16", latency="low", callback=self._output)
            self.input_stream = sd.RawInputStream(device=self.input_device, samplerate=self.input_rate,
                channels=self.input_channels, dtype="int16", latency="low", callback=self._input)
            self.output_stream.start()
            self.input_stream.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        """Release devices even after a failed open, and discard all pending sound."""
        self.capture.clear()
        for stream in (self.input_stream, self.output_stream):
            if stream is not None:
                with suppress(Exception):
                    stream.abort()
                with suppress(Exception):
                    stream.close()
        self.input_stream = self.output_stream = None
        self.clear()

    def feed(self, pcm, now=None):
        """Cancel speaker echo while preserving speech, then send fixed 20 ms microphone frames."""
        now = time.monotonic() - len(pcm) / (2 * self.input_channels * self.input_rate) if now is None else now
        frame_at = now - len(self.input_tail) / 32000
        if self.input_channels == 2:
            pcm = audioop.tomono(pcm, 2, .5, .5)
        pcm, self.input_state = audioop.ratecv(pcm, 2, 1, self.input_rate, 16000, self.input_state, 1, 1)
        self.input_tail += pcm
        while len(self.input_tail) >= 640:
            frame, self.input_tail = self.input_tail[:640], self.input_tail[640:]
            clean = self.echo.clean(frame, at=frame_at)
            frame_at += .02
            if not self.capture.is_set():
                continue  # still train AEC during the connection greeting
            if self.echo_enabled.is_set():
                frame = clean
            try:
                self.incoming.put_nowait(bytes(640) if self.muted.is_set() else frame)
            except queue.Full:
                self.problem = "The connection cannot keep up with the microphone. Reconnect to try again."
                return

    def _input(self, data, frames, timing, status):
        """Convert microphone callback frames into the bounded input queue."""
        if status.input_overflow:
            self.problem = "The microphone lost audio. Try another device or reconnect."
        try:
            self.feed(bytes(data), now=time.monotonic() + timing.inputBufferAdcTime - timing.currentTime)
        except Exception:
            self.problem = "Microphone conversion failed."

    def _output(self, data, frames, timing, status):
        """Fill the playback callback buffer and report actual playback progress."""
        try:
            pcm = self.render(frames, time.monotonic() + max(0, timing.outputBufferDacTime - timing.currentTime))
            data[:] = audioop.tostereo(pcm, 2, 1, 1) if self.output_channels == 2 else pcm
        except Exception:
            data[:] = bytes(len(data))
            self.problem = "Speaker playback failed."

    def clear(self):
        """Forget queued and scheduled playback reports; cleared audio is never acknowledged."""
        with self.lock:
            self.playback.clear()
            self.pending.clear()
            self.queued_bytes = 0
            self.buffering = True
            self.accept_audio = False
            self.segment = None
            self.output_state = None

    def event(self, event):
        """Apply sound control in socket order before accepting any subsequent PCM."""
        kind = event["type"]
        if kind == "playback.clear":
            self.clear()
            # This confirms only the application queue, not hardware sound already scheduled.
            if self.clear_ack:
                self.control("playback.cleared", turn=event["turn"])
        elif kind == "turn.started":
            with self.lock:
                self.turn = event["turn"]
                self.segment = None
                self.accept_audio = True
        elif kind == "playback.segment":
            with self.lock:
                if not self.accept_audio or event["turn"] != self.turn:
                    return
                self.segment = event["segment"]
                self.expected, self.received = event["samples"], 0
                self.output_state = None
        elif kind == "session.ready":
            self.clear_ack = event.get("queue_clear_ack") is True
            self.capture.set()

    def receive(self, pcm):
        """Resample output at the device's rate and cap the local playback buffer at one second."""
        with self.lock:
            if not self.accept_audio or self.segment is None:
                return
            if not pcm or len(pcm) % 2 or len(pcm) > 4800:
                raise ValueError("Invalid sound frame from server")
            self.received += len(pcm) // 2
            if self.received > self.expected:
                raise ValueError("Sound exceeds the announced segment")
            converted, self.output_state = audioop.ratecv(pcm, 2, 1, 24000, self.output_rate, self.output_state)
            if self.queued_bytes + len(converted) > self.output_rate * 2:
                raise ValueError("Speaker playback cannot keep up")
            report = dict(type="playback.progress", turn=self.turn, segment=self.segment, samples=self.received)
            self.playback.append(Chunk(converted, report))
            self.queued_bytes += len(converted)

    def render(self, frames, at=None):
        """Fill a device buffer and schedule acknowledgements for when its sound reaches the DAC."""
        at = time.monotonic() if at is None else at
        result = bytearray(frames * 2)
        written = 0
        with self.lock:
            # A small reserve absorbs packet/device scheduling jitter. Complete short
            # segments bypass the reserve so their tails and acknowledgements never stall.
            if self.buffering and (self.queued_bytes >= int(self.output_rate * .06) * 2 - 2
                                   or self.received >= self.expected):
                self.buffering = False
            while not self.buffering and self.playback and written < len(result):
                chunk = self.playback[0]
                count = min(len(result) - written, len(chunk.pcm) - chunk.offset)
                result[written:written + count] = chunk.pcm[chunk.offset:chunk.offset + count]
                written += count
                chunk.offset += count
                self.queued_bytes -= count
                if chunk.offset == len(chunk.pcm):
                    self.playback.popleft()
                    self.pending.append((at + written / (self.output_rate * 2), chunk.report))
            if not self.playback:
                self.buffering = True
        # Use device output, not received network packets: clear and underruns must match
        # what the microphone can actually hear. Scheduled hardware sound survives clear.
        self.echo.render(bytes(result), self.output_rate, at=at)
        return bytes(result)

    def reports(self, now=None):
        """Return only device-time-confirmed progress, coalescing frames within each segment."""
        now = time.monotonic() if now is None else now
        result = {}
        with self.lock:
            while self.pending and self.pending[0][0] <= now:
                _, report = self.pending.popleft()
                result[report["turn"], report["segment"]] = report
        return list(result.values())

    def control(self, kind, **values):
        """Queue a UI control without allowing a stopped network to accumulate clicks."""
        if kind == "session.cancel" and values.get("interrupt"):
            self.clear()
        try:
            self.controls.put_nowait(dict(type=kind, **values))
        except queue.Full:
            self.problem = "The connection stopped responding. Please reconnect."
