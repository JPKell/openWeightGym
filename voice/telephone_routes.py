"""Public-route instructions and signed, session-free reachability probes."""

import asyncio
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from voice.telephone import signature, valid


def append_query(url, query):
    return url + ('&' if urlsplit(url).query else '?') + query


def webhook_target(base, supplied, challenges):
    """Signed machine-file query fields are retained; only issued PIN callbacks add a field."""
    expected = parse_qs(urlsplit(base).query, keep_blank_values=True)
    query = {key: supplied.getlist(key) for key in supplied}
    if query == expected:
        return base
    extra = query.pop('owg_pin', []) if 'owg_pin' not in expected else []
    if query == expected and len(extra) == 1 and extra[0] in challenges:
        return append_query(base, 'owg_pin=' + extra[0])
    return None


def probe_url(webhook, at=None, nonce=None):
    return append_query(webhook, 'probe=' + (nonce or secrets.token_urlsafe(24)) + '&at=' + str(int(time.time() if at is None else at)))


def valid_probe(phone, token, params, supplied):
    query = {key: params.getlist(key) for key in params}
    nonce, at = query.pop('probe', []), query.pop('at', [])
    if query != parse_qs(urlsplit(phone['webhook_url']).query, keep_blank_values=True) or len(nonce) != 1 or len(at) != 1:
        return False
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', nonce[0]) or not re.fullmatch(r'[0-9]{1,12}', at[0]) or abs(time.time()-int(at[0])) > 30:
        return False
    return valid(token, probe_url(phone['webhook_url'], int(at[0]), nonce[0]), {}, supplied)


async def public_dns(url):
    address = urlsplit(url)
    if address.scheme != 'https' or not address.hostname or address.username or address.password or address.fragment:
        raise ValueError('Use a public HTTPS address without embedded credentials.')
    try:
        answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(address.hostname, address.port or 443, type=1), 3)
    except (OSError, TimeoutError, ValueError):
        raise ValueError('The public hostname could not be resolved. Check DNS.') from None
    if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
        raise ValueError('The public hostname resolves to a private or loopback address.')


async def check_route(phone, client):
    if not phone.get('enabled'):
        raise ValueError('Enable telephone before checking the public route.')
    token = Path(phone['token_file']).read_text().strip()
    target = probe_url(phone['webhook_url'])
    await public_dns(target)
    began = time.monotonic()
    try:
        response = await client.head(target, headers={'x-twilio-signature': signature(token, target, {})},
            timeout=httpx.Timeout(10, connect=3), follow_redirects=False)
    except httpx.HTTPError:
        raise ValueError('The public HTTPS route could not be reached. Check TLS, forwarding and the public listener.') from None
    return {'passed': response.status_code == 204, 'message': 'Public URL accepted the signed probe.' if response.status_code == 204 else
            f'The public route returned HTTP {response.status_code}; expected 204. Check the proxy, public port and auth token.',
            'stages': [{'stage': 'Public HTTPS route', 'passed': response.status_code == 204, 'ms': round((time.monotonic()-began)*1000, 1)}]}


async def detected_routes(port):
    result = {'tailscale': bool(shutil.which('tailscale')), 'cloudflared': bool(shutil.which('cloudflared')), 'funnel_url': ''}
    if result['tailscale']:
        process = None
        try:
            process = await asyncio.create_subprocess_exec('tailscale', 'status', '--json', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await asyncio.wait_for(process.communicate(), 2)
            hostname = json.loads(output).get('Self', {}).get('DNSName', '').rstrip('.')
            if re.fullmatch(r'[A-Za-z0-9.-]+\.ts\.net', hostname):
                result['funnel_url'] = 'https://' + hostname
        except (OSError, ValueError, TypeError, TimeoutError):
            if process is not None and process.returncode is None:
                process.kill(); await process.wait()
    result['funnel_command'] = 'tailscale funnel --bg http://127.0.0.1:' + str(port)
    result['caddy'] = 'voice.example.com {\n    reverse_proxy 127.0.0.1:' + str(port) + ' {\n        stream_timeout 1h\n    }\n}'
    result['nginx'] = ('location / {\n    proxy_pass http://127.0.0.1:' + str(port) + ';\n    proxy_http_version 1.1;\n'
        '    proxy_set_header Upgrade $http_upgrade;\n    proxy_set_header Connection "upgrade";\n'
        '    proxy_read_timeout 1h;\n    proxy_send_timeout 1h;\n}')
    result['cloudflare'] = ('tunnel: YOUR_TUNNEL_ID\ncredentials-file: /path/to/YOUR_TUNNEL_ID.json\ningress:\n'
        '  - hostname: voice.example.com\n    service: http://127.0.0.1:' + str(port) + '\n  - service: http_status:404')
    return result
