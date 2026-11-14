"""Whisper GGML files, fixed memory envelopes and single-request runtime identity."""

from copy import deepcopy
from pathlib import Path
import struct

from soundroom.model import (NAME, at_context, base, contexts, embedding, kin_base, layouts,
                            memory_base, parallel_capacity, required_devices, fixed_split, answered_settings)


def header(path):
    """Read the native GGML magic and vocabulary size; GGUF is a different format."""
    with Path(path).open('rb') as stream:
        raw = stream.read(8)
    if len(raw) != 8 or raw[:4] != b'lmgg':
        raise ValueError('whisper.cpp requires a ggml-*.bin Whisper model, not an audio.cpp/llama.cpp GGUF.')
    return struct.unpack('<I', raw[4:])[0]


def multilingual(settings):
    """Advertise translation only for a verified multilingual Whisper vocabulary."""
    try:
        return header(settings.get('flags', {}).get('model', '')) >= 51865
    except (OSError, ValueError, TypeError):
        return False


def check_model(name, settings, cards):
    """Validate the engine/file pair, typed limits and an explicit CPU or one-GPU layout."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('Invalid Whisper model name.')
    if not isinstance(settings, dict) or settings.keys() - {'engine', 'flags', 'whispercpp', 'gpus', 'alone'}:
        raise ValueError('Whisper models accept engine, flags, whispercpp, gpus and alone.')
    flags = settings.get('flags', {})
    if settings.get('engine') != 'whispercpp' or flags.keys() != {'model', 'ctx-size'} or flags['ctx-size'] != 0:
        raise ValueError('Whisper requires engine=whispercpp, flags.model and ctx-size=0.')
    path = flags['model']
    if not isinstance(path, str) or '\0' in path or not Path(path).is_absolute() or Path(path).suffix.lower() != '.bin':
        raise ValueError('Choose an absolute path to a whisper.cpp ggml-*.bin model.')
    if Path(path).exists():
        header(path)
    options = settings.get('whispercpp', {})
    if not isinstance(options, dict) or options.keys() - {'host_ram_mib', 'threads', 'timeout', 'max_audio_mib', 'max_seconds'}:
        raise ValueError('Unknown whispercpp profile option.')
    for key, low, high, default in [('host_ram_mib', 1024, 131072, 8192), ('threads', 1, 32, 4),
                                    ('timeout', 30, 7200, 1800), ('max_audio_mib', 1, 64, 32), ('max_seconds', 1, 3600, 600)]:
        value = options.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'Whisper {key} must be {low}–{high}.')
    wanted = settings.get('gpus', 1)
    if not (type(wanted) is int and wanted in (0, 1) or isinstance(wanted, list) and len(wanted) == 1 and wanted[0] in cards):
        raise ValueError('Whisper runs on CPU (0) or exactly one GPU.')
    if type(settings.get('alone', True)) is not bool:
        raise ValueError('alone must be true or false.')


def effective(settings, defaults):
    """Ignore llama.cpp defaults and conservatively reserve the chosen GPU exclusively."""
    return {'alone': True, **deepcopy(settings)}


def files_of(settings):
    """The single native weights file participates in fingerprints, moves and deletion protection."""
    return {'model': settings['flags']['model']}


def file_selection(settings):
    """Bind revision tracking to the selected weights."""
    return tuple(files_of(settings).items())


def fingerprint_settings(settings, hashes):
    """Keep profile options while replacing its location with the content hash."""
    result = base(settings)
    result['flags']['model'] = hashes['model']
    return result


def retarget(settings, target):
    """Retain the destination's weights when applying a profile preset."""
    result = base(settings)
    result['flags']['model'] = target['flags']['model']
    return result


def profile(settings, manifest=''):
    """Supply the common Audio studio and Voice transcription capability contract."""
    options = settings.get('whispercpp', {})
    from voice.options import LANGUAGES
    return {'family': 'whispercpp', 'kind': 'transcription', 'translates': multilingual(settings),
            'limits': {'max_audio_mib': options.get('max_audio_mib', 32), 'max_seconds': options.get('max_seconds', 600)},
            'device': 'cpu' if settings.get('gpus', 1) == 0 else 'gpu',
            'options': [{'name': 'language', 'values': list(LANGUAGES) if multilingual(settings) else ['en']}], 'defaults': {},
            'help': 'whisper.cpp GGML .bin weights; multilingual models can translate speech to English.'}
