"""Validated personal presentation choices; these never grant access."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math
import time

DEFAULTS = {
    'show_telemetry': True, 'text_size': 'standard', 'motion': 'system',
    'timezone': 'device', 'date_format': 'readable', 'clock': '24',
    'start_page': '/status', 'chat_model': '',
    'alert_security': True, 'alert_spending': True, 'alert_jobs': False,
    'notices_read_at': 0.0,
}
OPTIONS = {
    'text_size': ('standard', 'large'), 'motion': ('system', 'reduce'),
    'date_format': ('readable', 'iso', 'device'), 'clock': ('24', '12'),
    'start_page': ('/status', '/chat', '/requests'),
}


def validate(chosen):
    """Reject unknown fields and invalid values before committing any changes."""
    if not isinstance(chosen, dict):
        raise ValueError('Preferences must be an object.')
    for key, value in chosen.items():
        if key not in DEFAULTS or type(value) is not type(DEFAULTS[key]):
            raise ValueError(f'Unknown preference or incorrect value type: {key}.')
        if key in OPTIONS and value not in OPTIONS[key]:
            raise ValueError(f'Choose a supported {key.replace("_", " ")}.')
        if key == 'timezone' and value != 'device':
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('Choose a valid IANA time zone.') from None
        if key == 'chat_model' and (len(value) > 160 or any(ord(char) < 32 for char in value)):
            raise ValueError('Choose a valid default model or role.')
        if key == 'notices_read_at' and (not math.isfinite(value) or not 0 <= value <= time.time() + 5):
            raise ValueError('Invalid notification read time.')
