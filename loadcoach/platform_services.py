"""Front-wide service choices shared by setup, browser pages and API dispatch."""

from collections.abc import Mapping
from dataclasses import dataclass


SERVICE_SETTINGS = {
    'allow_image_generation': 'images',
    'allow_speech': 'speech',
    'allow_audio_generation': 'audio',
    'allow_model_downloads': 'downloads',
    'allow_classifiers': 'classifiers',
}
SPEECH_TASKS = frozenset({'speech', 'clone', 'design', 'transcription', 'alignment', 'diarization'})


@dataclass(frozen=True)
class PlatformServices:
    images: bool = True
    speech: bool = True
    audio: bool = True
    downloads: bool = True
    classifiers: bool = True

    @classmethod
    def from_settings(cls, settings, overrides=None):
        """Missing settings preserve older installations; saved overrides take precedence."""
        get = settings.get if isinstance(settings, Mapping) else lambda key, default: getattr(settings, key, default)
        return cls(**{service: (overrides or {}).get(key, get(key, True)) is True
                      for key, service in SERVICE_SETTINGS.items()})

    @property
    def audio_models(self):
        return self.speech or self.audio

    def kind_enabled(self, kind):
        return self.classifiers if kind == 'classifier' else self.images if kind == 'image' else self.audio_models if kind == 'audio' else True

    def audio_task_enabled(self, task):
        return self.speech if task in SPEECH_TASKS else self.audio if task in ('music', 'sound') else self.audio_models

    def model_enabled(self, model):
        capabilities = model.get('capabilities', {})
        if capabilities.get('classifier') or model.get('type') == 'classifier' or model.get('systemone'):
            return self.classifiers
        if model.get('engine_id', model.get('engine')) == 'sdcpp' or capabilities.get('image_generation'):
            return self.images
        if model.get('engine_id', model.get('engine')) == 'audiocpp' or capabilities.get('audio'):
            task = model.get('audiocpp', {}).get('kind') or model.get('audio_profile', {}).get('kind') or capabilities.get('audio')
            return self.audio_task_enabled(task)
        return True

    def file_enabled(self, file):
        facts = file.get('facts') or {}
        kind = facts.get('what', file.get('what'))
        if not self.kind_enabled(kind):
            return False
        tasks = facts.get('kinds')
        return kind != 'audio' or not tasks or any(self.audio_task_enabled(task) for task in tasks)

    def download_enabled(self, repository):
        return self.downloads and (self.images or not str(repository).startswith('civitai/'))

    def path_enabled(self, path, method='GET', query=None):
        """Apply the same policy to visible links, bookmarked pages and service requests."""
        path = path.rstrip('/') or '/'
        query = query or {}
        if path.startswith(('/chat/classifier', '/owg/classifier')) or path in ('/v1/systemone', '/v1/classifier'):
            return self.classifiers
        if path.startswith(('/civitai', '/mirrorwall-discovery')):
            return self.images and self.downloads
        if path.startswith(('/hub', '/downloads')):
            return self.downloads
        if path.startswith(('/chat/images', '/models/images', '/mirrorwall-', '/settings/image', '/mirrorwall/')):
            return self.images
        if path.startswith(('/voice', '/system/voice', '/chat/speech', '/chat/transcribe')):
            return self.speech
        if path.startswith(('/chat/music', '/chat/sounds')):
            return self.audio
        if path.startswith(('/models/audio', '/soundroom-', '/system/audio', '/account/audio', '/soundroom/')):
            if path.endswith(('/transcribe', '/voices')) or '/voices/' in path:
                return self.speech
            if path.endswith('/lyrics'):
                return self.audio
            return self.audio_models
        if path.startswith('/v1/images/') and method == 'POST':
            return self.images
        if path.startswith('/v1/audio/') and method == 'POST':
            return self.audio_models if path == '/v1/audio/generations' else self.speech
        if path in ('/loadcoach/hub', '/turnstile/artifacts/hub'):
            return self.downloads and (query.get('source') != 'civitai' or self.images)
        if path in ('/loadcoach/downloads', '/turnstile/artifacts/downloads') and method == 'POST':
            return self.downloads
        if path in ('/models', '/benchmarks/node-measurements'):
            return self.kind_enabled(query.get('kind'))
        if path == '/files':
            return self.kind_enabled(query.get('type'))
        if path == '/turnstile/models/image-check':
            return self.images
        if (path == '/loadcoach/audio/packages' or path.endswith('/model-api/audio/packages')) and method == 'POST':
            return self.downloads and self.audio_models
        return True


class PlatformServiceGate:
    """Reject disabled front services before they can submit work or contact providers."""

    def __init__(self, app, policy):
        self.app, self.policy = app, policy

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            from starlette.requests import HTTPConnection
            from starlette.responses import JSONResponse
            request = HTTPConnection(scope)
            if not self.policy().path_enabled(scope['path'], scope['method'], request.query_params):
                answer = JSONResponse({'error': {'message': 'This service is disabled in General settings → Platform services.'}},
                                      403, headers={'cache-control': 'no-store'})
                await answer(scope, receive, send)
                return
        await self.app(scope, receive, send)
