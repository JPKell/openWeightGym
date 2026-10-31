"""Pinned sherpa-onnx CPU recognition and Kokoro phrase synthesis.

Native work runs in limited executors. Cancellation drops its result, but retains its
worker slot until the native call really finishes, so interruptions cannot grow a queue.
"""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import time

import numpy as np


@dataclass
class Recognition:
    """Keep recent voiced intervals on the recognizer's sample clock, not its delayed return time."""

    native: object
    samples: int = 0
    voiced: deque = field(default_factory=lambda: deque(maxlen=6000))

    def heard(self, result):
        """Reject words timestamped in silence, allowing 300 ms for token alignment error."""
        if not result.timestamps:
            return False
        begin = result.start_time + min(result.timestamps) - .3
        end = result.start_time + max(result.timestamps) + .3
        return any(first <= end and last >= begin for first, last in self.voiced)


class Workers:
    """An executor with no unbounded backlog, including after task cancellation."""

    def __init__(self, count: int):
        """Create the bounded worker pool used for native speech operations."""
        self.pool = ThreadPoolExecutor(max_workers=count, thread_name_prefix="voice")
        self.slots = asyncio.Semaphore(count)

    async def run(self, function, *args):
        """Execute speech work in the configured bounded worker pool."""
        await self.slots.acquire()
        future = asyncio.get_running_loop().run_in_executor(self.pool, function, *args)
        future.add_done_callback(lambda _: self.slots.release())
        return await asyncio.shield(future)

    def close(self):
        """Shut down the speech worker pool and wait for active work."""
        self.pool.shutdown(wait=True, cancel_futures=True)


class Speech:
    """Shared CPU weights, separate recognizer streams and synthesis workers."""

    def __init__(self, settings: dict):
        """Load configured speech models and initialize native worker ownership."""
        self.settings = settings
        self.recognizer = None
        self.synths = asyncio.Queue()
        self.recognition = Workers(2)
        self.synthesis = Workers(int(settings["synthesis"].get("workers", 2)))
        self.loading = asyncio.Lock()
        self.ready_since = None

    def load(self):
        """Load both runtimes with provider='cpu', then exercise each once."""
        import sherpa_onnx as sherpa
        r, s = self.settings["recognition"], self.settings["synthesis"]
        self.recognizer = sherpa.OnlineRecognizer.from_transducer(
            tokens=r["tokens"], encoder=r["encoder"], decoder=r["decoder"], joiner=r["joiner"],
            num_threads=r.get("threads", 2), sample_rate=16000, feature_dim=80,
            decoding_method=r.get("decoding_method", "modified_beam_search"),
            max_active_paths=r.get("max_active_paths", 8), provider="cpu", enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4, rule2_min_trailing_silence=r.get("trailing_silence_ms", 800) / 1000,
            rule3_min_utterance_length=r.get("max_utterance_seconds", 30),
        )
        synths = []
        for _ in range(s.get("workers", 2)):
            config = sherpa.OfflineTtsConfig(model=sherpa.OfflineTtsModelConfig(
                kokoro=sherpa.OfflineTtsKokoroModelConfig(
                    model=s["model"], voices=s["voices"], tokens=s["tokens"],
                    data_dir=s["data_dir"], lexicon=s.get("lexicon", ""), lang="en-us"),
                num_threads=s.get("threads", 2), provider="cpu"), max_num_sentences=1)
            tts = sherpa.OfflineTts(config)
            test = tts.generate("Ready.", sid=s.get("speaker", 0), speed=1)
            if test.sample_rate != 24000 or not len(test.samples):
                raise ValueError("Synthesis must produce 24 kHz sound")
            synths.append(tts)
        stream = self.stream()
        self.feed(stream, bytes(16000), False)
        return synths

    async def warm(self):
        """Load recognition and synthesis resources before accepting live sessions."""
        async with self.loading:
            if self.ready_since is None:
                synths = await self.recognition.run(self.load)
                for synth in synths:
                    self.synths.put_nowait(synth)
                self.ready_since = time.time()

    def stream(self):
        """Create a streaming recognizer for the selected speech profile."""
        return Recognition(self.recognizer.create_stream())

    def feed(self, stream, pcm: bytes, finish: bool):
        """Feed continuous sound; return a partial and at most one final utterance."""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        for offset in range(0, len(samples), 320):
            frame = samples[offset:offset + 320]
            if np.sqrt(np.mean(frame * frame)) > self.settings.get("speech_threshold", 600):
                stream.voiced.append(((stream.samples + offset) / 16000,
                                      (stream.samples + offset + len(frame)) / 16000))
        stream.samples += len(samples)
        native = stream.native
        native.accept_waveform(16000, samples / 32768)
        if finish:
            native.accept_waveform(16000, np.zeros(9600, dtype=np.float32))
            stream.samples += 9600
        while self.recognizer.is_ready(native):
            self.recognizer.decode_stream(native)
        result = self.recognizer.get_result_all(native)
        text = result.text.strip() if stream.heard(result) else ""
        endpoint = finish or self.recognizer.is_endpoint(native)
        if endpoint:
            self.recognizer.reset(native)
        return text, text if endpoint else None

    async def hear(self, stream, pcm: bytes, finish=False):
        """Finish recognition and return the transcript for the supplied audio."""
        return await self.recognition.run(self.feed, stream, pcm, finish)

    async def say(self, text: str, speaker: int | None = None, speed: float = 1) -> bytes:
        """Synthesize one short phrase, retaining the instance through cancellation."""
        tts = await self.synths.get()
        speaker = self.settings["synthesis"].get("speaker", 0) if speaker is None else speaker

        async def generate():
            """Synthesize one phrase into PCM inside the speech worker."""
            try:
                sound = await self.synthesis.run(tts.generate, text, speaker, speed)
                return (np.clip(sound.samples, -1, 1) * 32767).astype("<i2").tobytes()
            finally:
                self.synths.put_nowait(tts)

        # The outer caller can leave; this task owns the instance until generation ends.
        task = asyncio.create_task(generate())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return await asyncio.shield(task)

    def close(self):
        """Release speech workers and loaded native speech resources."""
        self.recognition.close()
        self.synthesis.close()
