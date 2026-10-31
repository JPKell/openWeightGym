"""WebRTC AEC3 with a bounded, clocked reference of sound sent to the speakers."""

import audioop
import threading
import time

import numpy as np
from pywebrtc_audio import AudioProcessor


class Echo:
    """Cancel speaker pickup without muting simultaneous speech; one instance per call."""

    def __init__(self):
        """Create the configured echo canceller and its audio conversion state."""
        self.processor = AudioProcessor(sample_rate=16000, echo_cancellation=True,
                                        noise_suppression=True, auto_gain_control=False)
        self.samples = np.zeros(48000, dtype=np.int16)  # three seconds, never a growing queue
        self.positions = np.full(48000, -1, dtype=np.int64)
        self.lock = threading.Lock()
        self.resample = None

    def render(self, pcm, rate, at=None):
        """Remember actual device output (including silence), or paced telephone output."""
        at = time.monotonic() if at is None else at
        with self.lock:
            # Match telephone interpolation exactly; do not low-pass an already 16 kHz
            # reference. Filtering is needed only when reducing the device's sample rate.
            pcm, self.resample = audioop.ratecv(pcm, 2, 1, rate, 16000, self.resample, 1, int(rate > 16000))
            samples = np.frombuffer(pcm, dtype="<i2")
            if len(samples) > len(self.samples):
                raise ValueError("Echo reference exceeds three seconds")
            positions = round(at * 16000) + np.arange(len(samples), dtype=np.int64)
            slots = positions % len(self.samples)
            self.samples[slots], self.positions[slots] = samples, positions

    def clean(self, pcm, at=None):
        """Process whole 10 ms frames at 16 kHz against the matching output timeline."""
        if not pcm or len(pcm) % 320:
            raise ValueError("Echo cancellation needs whole 10 ms PCM frames")
        at = time.monotonic() if at is None else at
        near = np.frombuffer(pcm, dtype="<i2")
        positions = round(at * 16000) + np.arange(len(near), dtype=np.int64)
        slots = positions % len(self.samples)
        with self.lock:
            far = np.where(self.positions[slots] == positions, self.samples[slots], 0).astype(np.int16)
            return self.processor.process(near, far).astype("<i2", copy=False).tobytes()

    def clear(self, at=None):
        """Forget unplayed telephone output, retaining the history needed for its echo tail."""
        at = time.monotonic() if at is None else at
        with self.lock:
            self.positions[self.positions >= round(at * 16000)] = -1
