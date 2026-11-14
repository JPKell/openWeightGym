"""Non-secret telephone configuration shared by the gateway and console."""

from copy import deepcopy
import ipaddress
import re
from urllib.parse import urlsplit

E164 = re.compile(r"\+[1-9][0-9]{7,14}\Z")


def urls(base):
    """Derive Twilio's exact signed URLs from one public HTTPS base."""
    base = base.rstrip('/')
    return base + '/voice/inbound', 'wss://' + base.removeprefix('https://') + '/voice/stream'


def configuration(settings):
    """An explicit site table wins; absent tables preserve legacy signed URLs exactly."""
    if 'telephone' in settings:
        phone = deepcopy(settings['telephone'])
        phone['webhook_url'], phone['stream_url'] = urls(phone.get('public_url', ''))
        phone['token_file'] = settings.get('telephone_token_file', 'keys/voice-twilio.key')
        return phone
    legacy = settings.get('twilio', {})
    return {**legacy, 'enabled': bool(legacy.get('token_file')), 'legacy': bool(legacy),
            'numbers': [{**legacy, 'label': 'Legacy', 'enabled': True, 'legacy': True,
                         'callers': {'mode': 'anyone'}}] if legacy else []}


def validate(phone, profiles=None, voices=None):
    """Validate untrusted site settings; credentials and account keys never belong here."""
    if not isinstance(phone, dict) or set(phone) - {'enabled', 'account_sid', 'public_url', 'numbers'}:
        raise ValueError('Choose valid telephone settings.')
    if type(phone.get('enabled')) is not bool:
        raise ValueError('Telephone enabled must be true or false.')
    if not isinstance(phone.get('account_sid'), str) or not re.fullmatch(r'AC[0-9a-fA-F]{32}', phone['account_sid']):
        raise ValueError('Account SID must be AC followed by 32 hexadecimal characters.')
    if not isinstance(phone.get('public_url'), str):
        raise ValueError('Use a public HTTPS address.')
    try:
        address = urlsplit(phone['public_url'])
    except ValueError:
        raise ValueError('Use a valid public HTTPS address.') from None
    if address.scheme != 'https' or not address.hostname or address.query or address.fragment or address.username or address.password:
        raise ValueError('Use a public HTTPS address without credentials, query or fragment.')
    host = address.hostname.lower().rstrip('.')
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError('Use a public address, not a private or loopback address.')
    except ValueError as error:
        if 'Use a public' in str(error):
            raise
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
            raise ValueError('Use a public address, not a private or loopback address.') from None
    try:
        address.port
    except ValueError:
        raise ValueError('Use a valid public port.') from None
    numbers = phone.get('numbers')
    if not isinstance(numbers, list) or len(numbers) > 100:
        raise ValueError('Use a list of at most 100 telephone numbers.')
    seen_numbers, seen_labels = set(), set()
    permitted = {'label', 'number', 'enabled', 'mode', 'profile', 'voice', 'greeting', 'account',
                 'callers', 'max_calls_per_hour', 'accept_user_numbers', 'require_attested_caller_id'}
    for number in numbers:
        if not isinstance(number, dict) or set(number) - permitted:
            raise ValueError('Choose valid number settings.')
        label, digits = number.get('label', ''), number.get('number', '')
        if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,63}', label) or label.casefold() in seen_labels:
            raise ValueError('Use a unique number label (1–64 letters, digits, spaces, underscores or hyphens).')
        if not isinstance(digits, str) or not E164.fullmatch(digits) or digits in seen_numbers:
            raise ValueError('Use unique E.164 telephone numbers, such as +15555550123.')
        seen_labels.add(label.casefold()); seen_numbers.add(digits)
        if type(number.get('enabled', True)) is not bool:
            raise ValueError('Number enabled must be true or false.')
        profile, mode = number.get('profile', 'default'), number.get('mode', 'direct')
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', profile):
            raise ValueError('Choose an available voice profile.')
        if profiles is not None and profile not in profiles:
            raise ValueError('Choose an available voice profile.')
        if mode not in (profiles[profile].get('modes', ['direct']) if profiles is not None else ['direct', 'hermes']):
            raise ValueError('Choose a mode allowed by this profile.')
        if not isinstance(number.get('account'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', number['account']):
            raise ValueError('Choose a phone account.')
        if number.get('voice') and (voices is None or number['voice'] not in voices.get(number['account'], ())):
            raise ValueError('Choose a SoundRoom voice available to the phone account.')
        if not isinstance(number.get('greeting', ''), str) or len(number.get('greeting', '')) > 300:
            raise ValueError('Use a greeting of at most 300 characters.')
        limit = number.get('max_calls_per_hour', 60)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('Use a rate limit between 1 and 10000 calls per hour.')
    return phone
