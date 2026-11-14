"""Private Whisper HTTP adapter; its entire native lifetime belongs to LoadCoach."""

import asyncio
from contextlib import asynccontextmanager
import io
import json
import math
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import anyio
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engines.whisper_cpp import model
from soundroom.requests import MAX_REQUEST_BYTES, encoded_audio
from soundroom.runtime import Native as AudioNative, free_port, to_wav

TRANSCRIBE = '/v1/audio/transcriptions'
TRANSLATE = '/v1/audio/translations'
FIELDS = {'model', 'file', 'language', 'prompt', 'temperature', 'response_format', 'timestamp_granularities', 'user'}


def checked(path, asked):
    """Reject unsupported fields and timing formats before loading native weights."""
    if path not in (TRANSCRIBE, TRANSLATE):
        raise ValueError('This model supports transcription and speech translation only.')
    if not isinstance(asked, dict) or asked.keys() - FIELDS:
        raise ValueError('Unknown Whisper request field(s).')
    if path == TRANSLATE and 'language' in asked:
        raise ValueError('Translation always returns English; omit language.')
    form = asked.get('response_format', 'json')
    if form == 'verbose_json' or 'timestamp_granularities' in asked:
        raise ValueError('whisper.cpp verbose_json and word/segment timestamp granularities are unavailable: native token timings do not match OpenAI word timings. Use json, text, srt or vtt.')
    if form not in ('json', 'text', 'srt', 'vtt'):
        raise ValueError('response_format must be json, text, srt or vtt.')
    for name, maximum in [('language', 16), ('prompt', 20000)]:
        if not isinstance(asked.get(name, ''), str) or len(asked.get(name, '')) > maximum:
            raise ValueError(f'{name} must be text of at most {maximum} characters.')
    value = asked.get('temperature', 0)
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('temperature must be a number from 0 to 1.')
    return asked


class Native(AudioNative):
    """Share supervised output draining and shutdown with the audio.cpp native boundary."""
    async def start(self):
        """Start whisper-server privately and wait for its model-ready health endpoint."""
        if self.process and self.process.returncode is None:
            return
        port = free_port()
        options = self.job['settings'].get('whispercpp', {})
        command = [self.job['installation']['executable'], '--host', '127.0.0.1', '--port', str(port),
                   '--model', self.job['settings']['flags']['model'], '--language', 'auto', '--threads', str(options.get('threads', 4))]
        if not self.job['cards']:
            command.append('--no-gpu')
        self.process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                         stderr=asyncio.subprocess.STDOUT, limit=2**22)
        self.reader = asyncio.create_task(self.read_output())
        self.client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=options.get('timeout', 1800))
        began = time.monotonic()
        while time.monotonic()-began < options.get('timeout', 1800):
            if self.process.returncode is not None:
                raise RuntimeError('whisper.cpp failed to load: '+self.output[-1200:])
            try:
                if (await self.client.get('/health', timeout=2)).is_success:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
        raise TimeoutError('whisper.cpp model loading timed out.')


def create_app(job, native=None):
    """Expose only authenticated health and supported audio routes to the owning Runner."""
    native = native or Native(job)
    busy = False
    @asynccontextmanager
    async def lifespan(app):
        """Ensure failed startup, shutdown and cancelled work release the subordinate process."""
        try:
            await native.start()
            yield
        finally:
            with anyio.CancelScope(shield=True):
                await native.stop()

    def authorized(request):
        """The private child accepts only its owning Runner's per-launch credential."""
        import secrets
        return secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer '+job['token'])

    async def health(request):
        """Never report ready after a failed/cancelled native process."""
        if not authorized(request):
            return Response(status_code=401)
        return JSONResponse({'status': 'ok'}, status_code=200 if native.process and native.process.returncode is None else 503)

    async def inference(request):
        """Normalize bounded uploads and preserve native response bytes for each supported format."""
        nonlocal busy
        if not authorized(request):
            return Response(status_code=401)
        if busy:
            return JSONResponse({'error': {'message': 'This runtime already has an admitted request.'}}, 409)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_REQUEST_BYTES:
                    return JSONResponse({'error': {'message': 'Audio request exceeds 96 MiB.'}}, 413)
            asked = checked(request.url.path, json.loads(raw))
            options = job['settings'].get('whispercpp', {})
            audio = encoded_audio(asked.get('file'), options.get('max_audio_mib', 32))
            if request.url.path == TRANSLATE and not model.multilingual(job['settings']):
                raise ValueError('This model cannot translate speech.')
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': {'message': str(error)}}, 400)
        busy, completed = True, False
        try:
            wav = await to_wav(audio, 16000, 1)
            with wave.open(io.BytesIO(wav)) as recording:
                if recording.getnframes()/recording.getframerate() > options.get('max_seconds', 600):
                    raise ValueError('Audio exceeds this profile’s maximum duration.')
            fields = {'response_format': asked.get('response_format', 'json'), 'temperature': str(asked.get('temperature', 0)),
                      'language': asked.get('language') or 'auto', 'prompt': asked.get('prompt', ''),
                      'translate': 'true' if request.url.path == TRANSLATE else 'false'}
            answer = await native.client.post('/inference', files={'file': ('audio.wav', wav, 'audio/wav'),
                       **{key: (None, value) for key, value in fields.items()}})
            if not answer.is_success:
                raise RuntimeError('whisper.cpp refused the audio request (HTTP '+str(answer.status_code)+').')
            completed = True
            return Response(answer.content, answer.status_code, media_type=answer.headers.get('content-type', 'application/json'),
                            headers={'cache-control': 'no-store'})
        except ValueError as error:
            return JSONResponse({'error': {'message': str(error)}}, 400)
        except Exception as error:
            return JSONResponse({'error': {'message': str(error)}}, 502)
        finally:
            with anyio.CancelScope(shield=True):
                if not completed:
                    await native.stop()
                busy = False

    return Starlette(lifespan=lifespan, routes=[Route('/health', health), Route(TRANSCRIBE, inference, methods=['POST']),
                                              Route(TRANSLATE, inference, methods=['POST'])])


if __name__ == '__main__':
    import uvicorn
    job = json.loads(Path(sys.argv[1]).read_text())
    uvicorn.run(create_app(job), host='127.0.0.1', port=job['port'], access_log=False, log_level='warning', timeout_graceful_shutdown=10)
