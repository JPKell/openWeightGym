"""Bounded choices for one voice call; configured profiles remain the operator's defaults."""

import math

MAX_CALL_BYTES = 24 * 1024
FIELDS = {'model', 'synthesis_model', 'recognition_model', 'speaker', 'speed',
          'max_output_tokens', 'system_prompt', 'trailing_silence_ms'}


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
    if mode != 'direct' and result.keys() & {'model', 'system_prompt', 'max_output_tokens'}:
        raise ValueError('Hermes uses its own model and instructions.')
    if 'speaker' in result and (audio or type(result['speaker']) is not int or result['speaker'] not in dict(speakers)):
        raise ValueError('Choose an installed CPU voice.')
    if 'speed' in result and (type(result['speed']) not in (int, float) or not math.isfinite(result['speed']) or not .5 <= result['speed'] <= 2):
        raise ValueError('Speech speed must be between 0.5 and 2.')
    if 'max_output_tokens' in result and (type(result['max_output_tokens']) is not int or not 16 <= result['max_output_tokens'] <= min(1024, profile.get('max_context_tokens', 8192) - 512)):
        raise ValueError('Reply length exceeds the call limit.')
    if 'system_prompt' in result and (not isinstance(result['system_prompt'], str) or not 1 <= len(result['system_prompt']) <= 4000):
        raise ValueError('Instructions need 1 to 4000 characters.')
    if 'trailing_silence_ms' in result and (type(result['trailing_silence_ms']) is not int or not 300 <= result['trailing_silence_ms'] <= 3000):
        raise ValueError('Pause before reply must be between 300 and 3000 ms.')
    return result
