"""The shared sound socket: fixed PCM formats and bounded, checked messages."""

import json
import re

VERSION = 1
INPUT = {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
OUTPUT = {"encoding": "pcm_s16le", "sample_rate": 24000, "channels": 1}
ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
MODES = {"direct", "hermes"}
PLAYBACK_INSTRUCTIONS = (
    "Conversation history contains only assistant speech delivered to the caller. "
    "Consecutive user messages can be a continuing utterance after a reply was cut short. "
    "Answer the latest request naturally, using those messages together. "
    "Never read internal playback status or delivery annotations aloud."
)
PLAYBACK_NOTE = re.compile(
    r"\[?\s*(?:Interrupted;\s*remaining audio was not confirmed heard\.?|"
    r"Previous output was not confirmed heard\.\s*Do not repeat tool actions\.?)\s*\]?", re.I)


def speakable(text: str) -> str:
    """Keep conversational words, removing delivery notes, Markdown syntax, code and URLs."""
    text = PLAYBACK_NOTE.sub(" ", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\s]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*#]+|\d+\.)\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


# The largest message on the sound socket: a turn's request to the model, with the conversation
# heard so far, which the voice service keeps under 48,000 bytes. Every other message is small.
ASK_LIMIT = 65536


def message(text: str, limit: int = 16384) -> dict:
    """Read a small JSON message, refusing malformed or oversized input."""
    if len(text) > limit:
        raise ValueError("Message too large")
    value = json.loads(text)
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("Expected a message with a type")
    return value


def start(session_id: str, mode: str) -> dict:
    """The first message, sent only after the session has been prepared."""
    return dict(type="session.start", protocol_version=VERSION, session_id=session_id,
                conversation_mode=mode, input=INPUT, output=OUTPUT)


def check_start(value: dict, session_id: str, mode: str) -> None:
    """Formats and identity must match exactly; sound is never guessed at."""
    expected = start(session_id, mode)
    if any(value.get(k) != v for k, v in expected.items()):
        raise ValueError("Session, mode or audio format does not match")


def check_audio(data: bytes) -> None:
    """Accept whole samples in frames no longer than 100 ms."""
    if not data or len(data) % 2 or len(data) > 3200:
        raise ValueError("Expected 1 to 100 ms of mono 16 kHz PCM")


class Phrases:
    """Keep sentence prosody intact; split oversized text only to bound synthesis work."""

    limit = 240

    def __init__(self):
        """Initialize streamed text buffering for speakable phrase segmentation."""
        self.pending = ""
        self.thinking = False

    def add(self, text: str, done: bool = False) -> list[str]:
        """Accumulate generated text and return newly complete speakable phrases."""
        self.pending += text
        result = []
        while self.pending:
            self.pending = self.pending.lstrip()
            # A model that ignores thinking=false must not read its private reasoning aloud.
            if self.pending.startswith("<think>"):
                self.thinking = True
                self.pending = self.pending[7:]
            if self.thinking:
                end = self.pending.find("</think>")
                if end < 0:
                    self.pending = self.pending[-8:]
                    break
                self.pending = self.pending[end + 8:]
                self.thinking = False
                continue
            # Commas, colons and semicolons belong to the same TTS call: splitting
            # there adds synthesis latency and resets the voice's sentence intonation.
            found = re.search(r'[.!?]["\')\]]*\s', self.pending)
            length = found.end() if found and found.end() <= self.limit else 0
            if not length and len(self.pending) > self.limit:
                length = self.pending.rfind(" ", 0, self.limit) + 1 or self.limit
            if not length and done:
                length = len(self.pending)
            if not length:
                break
            phrase, self.pending = self.pending[:length], self.pending[length:]
            phrase = re.sub(r"[<>{}`*_#]", "", phrase).strip()
            if phrase:
                result.append(phrase)
        return result
