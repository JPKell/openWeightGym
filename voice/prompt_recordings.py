"""Versioned greeting recordings, generated on compute and cached atomically on front."""

import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import wave

TEXTS = json.loads((Path(__file__).with_name('prompts') / 'text.json').read_text())
MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def texts(settings):
    """Fixed greeting names, with short editable phrases and checked-in defaults."""
    changed = settings.get('greetings', {})
    if not isinstance(changed, dict) or changed.keys() - TEXTS.keys():
        raise ValueError('Unknown greeting name')
    result = {**TEXTS, **changed}
    if any(not isinstance(text, str) or not text.strip() or len(text) > 300 for text in result.values()):
        raise ValueError('Each greeting needs 1 to 300 characters')
    return result


def wav(pcm):
    """Wrap the same 24 kHz mono PCM played by the gateway for browser preview."""
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(pcm)
    return output.getvalue()


def specification(settings, profile='default'):
    """A speaker and revision tied to synthesis configuration and the greeting texts."""
    synthesis = settings.get('synthesis', {})
    speaker = settings['profiles'][profile].get('speaker', synthesis.get('speaker', 0))
    if type(speaker) is not int or speaker < 0:
        raise ValueError('Invalid greeting speaker')
    model = {key: synthesis.get(key, '') for key in ('model', 'voices', 'tokens', 'data_dir', 'lexicon')}
    revision = hashlib.sha256(json.dumps(dict(speaker=speaker, model=model, texts=texts(settings)), sort_keys=True).encode()).hexdigest()
    return speaker, revision


async def generate(speech, speaker, revision, phrases=None):
    """Render all phrases with one explicit speaker before publishing any of them."""
    await speech.warm()
    audio = {}
    phrases = TEXTS if phrases is None else phrases
    for name, text in phrases.items():
        pcm = await speech.say(text, speaker)
        audio[name] = base64.b64encode(pcm).decode('ascii')
    bundle = dict(format=1, sample_rate=24000, speaker=speaker, revision=revision, audio=audio, texts=phrases)
    decode(bundle)
    return bundle


def decode(bundle):
    """Validate a complete, bounded set before it can replace known-good greetings."""
    if 'texts' in bundle and (not isinstance(bundle['texts'], dict) or set(bundle['texts']) != set(TEXTS)):
        raise ValueError('Invalid greeting texts')
    if 'texts' in bundle:
        texts({'greetings': bundle['texts']})
    if (bundle.get('format') != 1 or bundle.get('sample_rate') != 24000
            or type(bundle.get('speaker')) is not int or bundle['speaker'] < 0
            or not isinstance(bundle.get('revision'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', bundle['revision'])
            or not isinstance(bundle.get('audio'), dict) or set(bundle['audio']) != set(TEXTS)):
        raise ValueError('Invalid greeting bundle')
    result = {}
    for name, encoded in bundle['audio'].items():
        if not isinstance(encoded, str) or len(encoded) > 1280000:
            raise ValueError('Invalid greeting size')
        pcm = base64.b64decode(encoded, validate=True)
        if not pcm or len(pcm) % 2 or len(pcm) > 960000:
            raise ValueError('Invalid greeting PCM')
        result[name] = pcm
    return result


class PromptCache:
    """Keep the last complete set across compute outages and gateway restarts."""

    def __init__(self, path):
        """Configure revisioned prompt assets and their bounded audio cache."""
        self.path = Path(path)
        self.bundle, self.audio = None, {}
        try:
            if self.path.stat().st_size <= MAX_BUNDLE_BYTES:
                bundle = json.loads(self.path.read_bytes())
                self.audio = decode(bundle)
                self.bundle = bundle
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # a missing/bad cache leaves checked-in greetings available

    @property
    def revision(self):
        """Return the current prompt asset revision used to invalidate cached audio."""
        return self.bundle['revision'] if self.bundle else ''

    def install(self, bundle):
        """Write to a sibling temporary file, then swap the entire in-memory set at once."""
        audio = decode(bundle)
        payload = json.dumps(bundle).encode()
        if len(payload) > MAX_BUNDLE_BYTES:
            raise ValueError('Greeting bundle too large')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.voice-prompts-', delete=False) as file:
                temporary = Path(file.name)
                file.write(payload)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.bundle, self.audio = deepcopy(bundle), audio


async def refresh(cache, http, url, key, profile='default'):
    """Fetch from compute through turnstile without waking it; unchanged sets transfer no audio."""
    headers = {'authorization': 'Bearer ' + key, 'x-turnstile-no-wake': '1'}
    if cache.revision:
        headers['if-none-match'] = '"' + cache.revision + '"'
    async with http.stream('GET', url + '/voice/prompts', params={'profile': profile}, headers=headers,
                           timeout=60) as response:
        if response.status_code == 304:
            return
        response.raise_for_status()
        payload = bytearray()
        async for part in response.aiter_bytes():
            payload.extend(part)
            if len(payload) > MAX_BUNDLE_BYTES:
                raise ValueError('Greeting bundle too large')
    cache.install(json.loads(payload))
