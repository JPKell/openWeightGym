"""Optional whisper.cpp adapter; LoadCoach retains admission and child process ownership."""

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from engines.contracts import Installation, Launch
from engines.whisper_cpp import model
from loadcoach.devices import cuda_environment
from loadcoach.engine_memory import delegated_parent
from soundroom.engine import AudioCpp


def node_options(config):
    """Read the explicitly enabled native installation without executing it."""
    options = getattr(config, 'engines', {}).get('whispercpp', {})
    if not isinstance(options, dict) or options.keys() - {'enabled', 'executable', 'cli', 'manifest'}:
        raise ValueError('engines.whispercpp accepts enabled, executable, cli and manifest.')
    if type(options.get('enabled', False)) is not bool:
        raise ValueError('engines.whispercpp.enabled must be true or false.')
    for key in ('executable', 'cli', 'manifest'):
        if key in options and (not isinstance(options[key], str) or not options[key] or '\0' in options[key]):
            raise ValueError('Invalid whisper.cpp installation path.')
    return {'enabled': False, 'executable': 'whisper-server', 'cli': 'whisper-cli', 'manifest': '', **options}


class WhisperCpp(AudioCpp):
    """Use the common audio engine boundary with native Whisper launch and request semantics."""
    id, label, model = 'whispercpp', 'whisper.cpp', model

    def installation(self, config):
        """Describe the optional configured server."""
        options = node_options(config)
        return Installation(self.id, options['enabled'], options['executable'])

    def capabilities(self, settings):
        """Offer transcription and verified translation, excluding all other inference tasks."""
        return {**super().capabilities({}), 'audio': 'transcription', 'transcription': True,
                'translates': model.multilingual(settings)}

    def schema(self):
        """Extend existing engine-node fields for the Whisper runtime."""
        schema = super().schema()
        return {**schema, 'configuration_key': self.id, 'kinds': ['transcription'], 'limits': {},
                'node': [{**field, 'label': field['label'].replace('audiocpp_server', 'whisper-server').replace('audiocpp_cli', 'whisper-cli'),
                          'default': field['default'].replace('audiocpp_server', 'whisper-server').replace('audiocpp_cli', 'whisper-cli') if isinstance(field['default'], str) else field['default']}
                         for field in schema['node']]}

    def describe(self, config, settings=None):
        """Expose optional availability to Components and every node runtime selector."""
        options = node_options(config)
        available = bool(options['enabled'] and shutil.which(options['executable']))
        return {'id': self.id, 'label': self.label, **options, 'available': available, 'settings': options,
                'why': '' if available else 'Optional whisper.cpp is not installed/enabled; use --whispercpp install.',
                'schema': self.schema(), 'capabilities': self.capabilities(settings or {})}

    def host_ram_budget(self, config, settings):
        """Reserve the profile's delegated host RAM ceiling, including native decoding buffers."""
        return settings.get(self.id, {}).get('host_ram_mib', 8192)

    def check_available(self, config, settings):
        """Recheck native weights and runtime immediately before admission."""
        options = node_options(config)
        if not options['enabled'] or not shutil.which(options['executable']):
            raise ValueError('whisper.cpp is not installed/enabled; use --whispercpp install.')
        if options['manifest']:
            backend = json.loads(Path(options['manifest']).read_text())['backend']
            if backend == 'cpu' and settings.get('gpus', 1) != 0:
                raise ValueError('This whisper.cpp runtime was built for CPU; select CPU only or install a GPU backend.')
        model.header(settings['flags']['model'])
        delegated_parent()

    def launch(self, config, port, settings, cards):
        """Own a private authenticated adapter and native server in Runner's memory group."""
        self.check_available(config, settings)
        if len(cards) != (1 if settings.get('gpus', 1) else 0):
            raise ValueError('The admitted GPU count does not match the Whisper profile.')
        directory = tempfile.TemporaryDirectory(prefix='suite-whisper-engine-')
        try:
            token = secrets.token_urlsafe(32)
            path = Path(directory.name)/'launch.json'
            path.write_text(json.dumps({'port': port, 'token': token, 'settings': settings, 'installation': node_options(config), 'cards': list(cards)}))
            path.chmod(0o600)
            with open(shutil.which(node_options(config)['executable']), 'rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            backend = json.loads(Path(node_options(config)['manifest']).read_text())['backend'] if node_options(config)['manifest'] else 'cuda'
            if backend == 'vulkan' and cards:
                details = next((one for one in config.gpu_details if one['id'] == cards[0]), {})
                device = details.get('llama_device', cards[0])
                if not device.startswith('Vulkan') or not device[6:].isdigit():
                    raise ValueError('The selected GPU has no verified Vulkan device mapping on this node.')
                around = {**os.environ, 'GGML_VK_VISIBLE_DEVICES': device[6:], 'CUDA_VISIBLE_DEVICES': ''}
            else:
                around = cuda_environment(config, cards)
            return Launch([sys.executable, '-I', '-B', str(Path(__file__).with_name('runtime.py')), str(path)],
                          {**around, 'GGML_CUDA_ENABLE_UNIFIED_MEMORY': '0'},
                          process_group=True, memory_mib=self.host_ram_budget(config, settings),
                          headers={'authorization': 'Bearer '+token}, cleanup=directory.cleanup,
                          build='whispercpp:'+checksum, stop_seconds=20)
        except BaseException:
            directory.cleanup()
            raise

    def check_request(self, path, asked):
        """Reject all work outside the native transcription/translation contract."""
        from engines.whisper_cpp.runtime import checked
        checked(path, asked)

    async def version(self, executable):
        """Use the engine-specific binary identity in measurements and request records."""
        return (await super().version(executable)).replace('audiocpp:', 'whispercpp:', 1)

    def failure_reason(self, output):
        """Give the operator bounded native diagnostics without retaining uploaded audio."""
        return 'Whisper runtime stopped; check its GGML model and GPU/RAM budget.'

    def parse_form(self, submitted, read):
        """Parse a typed Whisper profile through the existing audio form boundary."""
        options = json.loads(read('audio_options', '{}'))
        gpus = read('audio_gpus', '1')
        return {'engine': self.id, 'flags': {'model': read('model', ''), 'ctx-size': 0}, 'whispercpp': options,
                'gpus': int(gpus) if gpus.isdigit() else [gpus], 'alone': True}
