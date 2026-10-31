"""audio.cpp speech for the voice service: the same calls as voice/speech.py, on openWeightGym audio models.

A voice profile with `speech = "audiocpp"` names a transcription model and a speech model. Speech is
gathered until a pause, then transcribed whole on the GPU (no partial transcripts); each phrase is
synthesized in the session's own voice when the caller chose one, else in the model's default.
Both go through turnstile with the voice service's own key, so LoadCoach admits them as live work.
"""
import base64
import io
import json
import math
import struct
import time
import wave

RATE = 16000
PRE_ROLL_MS = 300


def loudness(pcm):
    """Root-mean-square level of 16-bit mono PCM, on the same scale as the voice service's threshold."""
    count = len(pcm) // 2
    if not count:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[:count * 2])
    return math.sqrt(sum(sample * sample for sample in samples) / count)


def as_wav(pcm, rate=RATE):
    """Wrap PCM in a WAV header."""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return stream.getvalue()


class Utterance:
    """One caller's sound since the last utterance ended."""

    def __init__(self):
        """Start with nothing heard."""
        self.reset()

    def reset(self):
        """Forget what was heard, after it was transcribed or found to be silence."""
        self.pcm = bytearray()
        self.voiced = False
        self.silent_ms = 0.0


class AudioSpeech:
    """Recognition and synthesis through openWeightGym's audio models, shaped like voice.speech.Speech."""

    def __init__(self, profile: dict, request, threshold: float = 600):
        """`request(method, path, json)` asks turnstile with the service key and returns the raw answer bytes."""
        self.profile, self.request, self.threshold = profile, request, threshold
        self.ready_since = None

    def models(self):
        """The audio models this profile keeps resident while sessions use it."""
        return {self.profile["recognition_model"], self.profile["synthesis_model"]}

    async def warm(self):
        """Nothing loads here: the service holds the models, and LoadCoach loads them on first use."""
        self.ready_since = self.ready_since or time.time()

    def stream(self):
        """A fresh utterance buffer for one session."""
        return Utterance()

    async def hear(self, stream, pcm: bytes, finish: bool = False):
        """Gather sound; at a pause, the utterance's limit or `finish`, transcribe it and return it as final."""
        milliseconds = len(pcm) / 2 / RATE * 1000
        loud = loudness(pcm) > self.threshold
        if loud:
            stream.voiced = True
            stream.silent_ms = 0
        elif stream.voiced:
            stream.silent_ms += milliseconds
        stream.pcm.extend(pcm)
        if not stream.voiced:
            del stream.pcm[:max(0, len(stream.pcm) - RATE * 2 * PRE_ROLL_MS // 1000)]  # keep the onset's start
        ended = stream.voiced and (stream.silent_ms >= self.profile.get("trailing_silence_ms", 700)
                                   or len(stream.pcm) >= RATE * 2 * self.profile.get("max_utterance_seconds", 30))
        if not (ended or finish):
            return "", None
        heard, voiced = bytes(stream.pcm), stream.voiced
        stream.reset()
        if not voiced:
            return "", ""
        answer = await self.request("POST", "/v1/audio/transcriptions", {
            "model": self.profile["recognition_model"], "file": base64.b64encode(as_wav(heard)).decode(),
            **({"language": self.profile["language"]} if self.profile.get("language") else {})})
        text = json.loads(answer).get("text", "").strip()
        return text, text

    async def say(self, text: str, speaker=None, voice: dict | None = None, speed: float = 1) -> bytes:
        """24 kHz mono PCM for one phrase, in the session's voice when it has one."""
        asked = {"model": self.profile["synthesis_model"], "input": text, "response_format": "pcm"}
        if voice:
            asked |= {"voice_audio": voice["audio"], **({"voice_text": voice["text"]} if voice.get("text") else {})}
        pcm = await self.request("POST", "/v1/audio/speech", asked)
        if speed != 1:
            from soundroom.runtime import ffmpeg
            pcm, _ = await ffmpeg(['-f', 's16le', '-ar', '24000', '-ac', '1', '-i', 'pipe:0',
                                   '-af', f'atempo={speed}', '-f', 's16le', 'pipe:1'], pcm)
        return pcm

    def close(self):
        """Nothing native is held here."""
