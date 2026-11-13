"""Bounded choices for one voice call; configured profiles remain the operator's defaults."""

import math

MAX_CALL_BYTES = 24 * 1024
FIELDS = {'model', 'synthesis_model', 'recognition_model', 'speaker', 'speed',
          'max_output_tokens', 'system_prompt', 'trailing_silence_ms',
          'language', 'response_language', 'speech_voice'}

LANGUAGES = {'en': 'English', 'es': 'Spanish', 'fr': 'French', 'de': 'German', 'it': 'Italian',
             'pt': 'Portuguese', 'ja': 'Japanese', 'ko': 'Korean', 'zh': 'Chinese', 'hi': 'Hindi',
             'ar': 'Arabic', 'nl': 'Dutch', 'pl': 'Polish', 'ru': 'Russian', 'tr': 'Turkish',
             'uk': 'Ukrainian', 'vi': 'Vietnamese', 'id': 'Indonesian'}


def audio_kind(model):
    """Read current audio capabilities, including older catalogues with boolean task keys."""
    caps = model.get('capabilities', {})
    return caps.get('audio') if isinstance(caps.get('audio'), str) else next(
        (kind for kind in ('speech', 'clone', 'transcription') if caps.get(kind)), '')


def audio_choices(model):
    """Expose only declared voice/language choices and configured defaults, without model loads."""
    profile = model.get('audio_profile', {})
    options = profile.get('options', [])
    voices = next((one.get('values', []) for one in options if one.get('name') == 'voice'), [])
    default = profile.get('defaults', {}).get('voice', '')
    voices = [value for value in voices if isinstance(value, str) and 0 < len(value) <= 128]
    if isinstance(default, str) and default and default not in voices:
        voices.append(default)
    languages = next((one.get('values', []) for one in options if one.get('name') == 'language'), [])
    languages = [code for code, name in LANGUAGES.items() if code in languages or name in languages or name.lower() in languages]
    return {'id': model['id'], 'name': model['id'], 'kind': audio_kind(model),
            'voices': [{'id': value, 'name': value} for value in voices], 'languages': languages,
            'default_voice': default if isinstance(default, str) else ''}


def instructions(profile):
    """Apply bounded, server-validated response language instructions to this call only."""
    text = profile.get('system_prompt', 'Answer briefly for a spoken conversation.')
    if profile.get('response_language') == 'auto':
        text += '\nReply in the same language as the user. Do not repeat the reply in another language.'
    elif profile.get('response_language'):
        text += '\nReply only in ' + LANGUAGES[profile['response_language']] + '. Do not include a translation or repeat the reply in another language.'
    return text


def checked(value, profile, mode, speakers):
    """Validate per-call scalars before model permissions and capabilities are checked."""
    if not isinstance(value, dict) or value.keys() - FIELDS:
        raise ValueError('Unknown voice option.')
    result = dict(value)
    audio = profile.get('speech') == 'audiocpp'
    for name in ('model', 'synthesis_model', 'recognition_model'):
        if name in result and (not isinstance(result[name], str) or not 0 < len(result[name]) <= 128):
            raise ValueError('Choose a configured model.')
    if not audio and result.keys() & {'synthesis_model', 'recognition_model', 'trailing_silence_ms'}:
        raise ValueError('These options require audio.cpp speech.')
    if mode != 'direct' and result.keys() & {'model', 'system_prompt', 'max_output_tokens', 'response_language'}:
        raise ValueError('Hermes uses its own model and instructions.')
    if 'speaker' in result and (audio or type(result['speaker']) is not int or result['speaker'] not in dict(speakers)):
        raise ValueError('Choose an installed CPU voice.')
    if 'speech_voice' in result and (not audio or not isinstance(result['speech_voice'], str) or not 0 < len(result['speech_voice']) <= 128):
        raise ValueError('Choose a voice declared by the selected speech model.')
    for name in ('language', 'response_language'):
        if name == 'response_language' and audio and result.get(name) == 'auto':
            continue
        if name in result and (not isinstance(result[name], str) or result[name] not in LANGUAGES
                               or not audio and result[name] != 'en'):
            raise ValueError('Choose a supported speech language.')
    if 'speed' in result and (type(result['speed']) not in (int, float) or not math.isfinite(result['speed']) or not .5 <= result['speed'] <= 2):
        raise ValueError('Speech speed must be between 0.5 and 2.')
    if 'max_output_tokens' in result and (type(result['max_output_tokens']) is not int or not 16 <= result['max_output_tokens'] <= min(1024, profile.get('max_context_tokens', 8192) - 512)):
        raise ValueError('Reply length exceeds the call limit.')
    if 'system_prompt' in result and (not isinstance(result['system_prompt'], str) or not 1 <= len(result['system_prompt']) <= 4000):
        raise ValueError('Instructions need 1 to 4000 characters.')
    if 'trailing_silence_ms' in result and (type(result['trailing_silence_ms']) is not int or not 300 <= result['trailing_silence_ms'] <= 3000):
        raise ValueError('Pause before reply must be between 300 and 3000 ms.')
    return result
