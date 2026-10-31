"""Twilio signatures and stateful telephone sound conversion."""

import audioop
import base64
import hashlib
import hmac
import time


def signature(token: str, url: str, params: dict[str, list[str]]) -> str:
    """Twilio's form signature, including every parameter and distinct value."""
    body = url + "".join(k + v for k in sorted(params) for v in sorted(set(params[k])))
    return base64.b64encode(hmac.new(token.encode(), body.encode(), hashlib.sha1).digest()).decode()


def valid(token: str, url: str, params: dict, supplied: str) -> bool:
    """Verify the Twilio signature using the configured secret and exact public URL."""
    return bool(token and supplied and supplied.isascii()) and hmac.compare_digest(signature(token, url, params), supplied)


def valid_stream(token: str, stream_url: str, supplied: str) -> bool:
    """Validate the configured public stream URL, including Twilio's handshake URL variants.

    WSS signatures can use the TwiML URL or its HTTPS upgrade form, with an appended
    slash as documented by Twilio. Never build candidates from proxy/client headers.
    """
    upgrade = stream_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    urls = {stream_url, upgrade}
    return any(valid(token, url, {}, supplied) for base in urls for url in (base, base.rstrip("/") + "/"))


class Conversion:
    """Convert telephone audio and cancel delayed line/speaker echo against transmitted μ-law."""

    def __init__(self, cancel_echo=True):
        """Initialize streaming telephone audio conversion and resampling state."""
        self.up = self.down = None
        self.echo = None
        if cancel_echo:
            from voice.echo import Echo
            self.echo = Echo()
        self.tail = b""
        self.play_until = 0.0

    def receive(self, mulaw: bytes, at=None) -> bytes:
        """Keep the caller live during playback; return only whole, echo-cleaned 20 ms frames."""
        at = time.monotonic() - len(mulaw) / 8000 if at is None else at
        frame_at = at - len(self.tail) / 32000
        pcm = audioop.ulaw2lin(mulaw, 2)
        result, self.up = audioop.ratecv(pcm, 2, 1, 8000, 16000, self.up)
        if self.echo is None:
            return result  # offline format conversion has no live speaker reference
        self.tail += result
        frames = []
        while len(self.tail) >= 640:
            frame, self.tail = self.tail[:640], self.tail[640:]
            frames.append(self.echo.clean(frame, at=frame_at))
            frame_at += .02
        return b"".join(frames)

    def send(self, pcm: bytes, at=None) -> bytes:
        """Model Twilio's ordered playback queue so bursty sends provide a paced AEC reference."""
        at = time.monotonic() if at is None else at
        result, self.down = audioop.ratecv(pcm, 2, 1, 24000, 8000, self.down, 1, 1)
        mulaw = audioop.lin2ulaw(result, 2)
        # Twilio plays queued samples continuously. Small network/scheduler delays between
        # sends must not insert tiny holes into the reference on every packet. Re-anchor
        # only after a real gap; AEC's delay estimator handles the remaining transport lag.
        start = at if self.play_until < at - .1 else self.play_until
        if self.echo is not None:
            self.echo.render(audioop.ulaw2lin(mulaw, 2), 8000, at=start)
        self.play_until = start + len(mulaw) / 8000
        return mulaw

    def clear(self, at=None):
        """Drop unplayed reference on Twilio clear, without resetting the learned echo path."""
        at = time.monotonic() if at is None else at
        self.play_until = at
        if self.echo is not None:
            self.echo.clear(at)
