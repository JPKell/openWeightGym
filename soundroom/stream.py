"""Bounded capture of audio answers for logs: settings and outcomes kept, audio bytes never."""
import json

MAX_EVENT = 192 * 1024 * 1024
COMPLETED = ("audio_generation.completed",)
PAYLOAD = ("b64_wav", "audio", "audio_base64")


def metadata(value):
    """Keep reproducibility, settings and error facts without duplicating audio in logs."""
    if isinstance(value, dict):
        return {key: metadata(item) for key, item in value.items() if key not in PAYLOAD}
    if isinstance(value, list):
        return [metadata(item) for item in value]
    return value


def encode(event):
    """Encode one JSON event as server-sent events."""
    return ("event: " + event.get("type", "error") + "\ndata: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()


def completed(whole):
    """Recognize a finished take independently of chat's [DONE] convention."""
    for line in whole.splitlines():
        if line.startswith(b"data:"):
            try:
                value = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("type") in COMPLETED:
                return True
    return False


class Capture:
    """Bound diagnostic memory independently of take count and audio length."""

    def __init__(self, content_type):
        """Start an empty, bounded capture for one answer."""
        self.sse = "text/event-stream" in content_type
        self.buffer = bytearray()
        self.records = []
        self.overflow = self.error = False
        self.count = 0

    def accept(self, value):
        """Keep the facts of one event, never its audio."""
        if not isinstance(value, dict):
            return
        if value.get("error"):
            self.error = True
        if value.get("type") in COMPLETED:
            self.count += 1
        if value.get("type") in (*COMPLETED, "error") or "error" in value:
            self.records.append(metadata(value))

    def feed(self, chunk):
        """Take the next piece of the answer."""
        if self.overflow:
            return
        self.buffer.extend(chunk.replace(b"\r", b"") if self.sse else chunk)
        if len(self.buffer) > MAX_EVENT:
            self.overflow = True
            self.buffer.clear()
            return
        if self.sse:
            while b"\n\n" in self.buffer:
                frame, _, rest = self.buffer.partition(b"\n\n")
                self.buffer = bytearray(rest)
                for line in frame.splitlines():
                    if line.startswith(b"data:"):
                        try:
                            self.accept(json.loads(line[5:]))
                        except ValueError:
                            pass

    def snapshot(self):
        """What the log keeps of the answer."""
        if self.sse:
            return b"".join(encode(value) for value in self.records)
        if self.overflow:
            return b'{"audio_capture_truncated":true}'
        try:
            value = json.loads(self.buffer)
            self.error = bool(value.get("error"))
            self.count = len(value.get("data", []))
            return json.dumps(metadata(value)).encode()
        except (ValueError, AttributeError):
            return bytes(self.buffer[:4096])

    def outcome(self):
        """How the answer ended, for reliability."""
        if self.error:
            return "error"
        return "success" if self.count else "unscored" if self.overflow else "error"
