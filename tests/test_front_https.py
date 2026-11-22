"""scripts/front-https.sh writes an nginx site that serves the console over HTTP/2 (QA-044).

ssh, openssl and curl are stand-ins that record what the script would send to the control node.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SSH = '''#!/bin/sh
# Answers `nginx -v` with the version under test; anything sent on stdin is kept, one file per call.
case "$*" in *"nginx -v"*) echo "nginx version: nginx/$NGINX_VERSION"; exit 0;; esac  # the remote 2>&1 brings it to stdout
n=$(ls "$SENT" | wc -l); cat > "$SENT/$n"
'''
OPENSSL = '#!/bin/sh\nexit 0\n'  # the CA and certificate already exist and are not near their end
CURL = "#!/bin/sh\nprintf '%s' \"$CURL_PROTOCOL\"\n"


def run(tmp_path, version, protocol='2'):
    tools, sent, ca = tmp_path / 'bin', tmp_path / 'sent', tmp_path / 'ca'
    for folder in (tools, sent, ca):
        folder.mkdir(exist_ok=True)
    for name, body in (('ssh', SSH), ('openssl', OPENSSL), ('curl', CURL)):
        (tools / name).write_text(body)
        (tools / name).chmod(0o755)
    for name in ('home-ca.key', 'home-ca.crt', 'front.home.arpa.key', 'front.home.arpa.crt'):
        (ca / name).write_text('stand-in')
    env = {**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}', 'SENT': str(sent), 'NGINX_VERSION': version,
           'CURL_PROTOCOL': protocol, 'CA_DIR': str(ca), 'FRONT': '10.0.0.5', 'LAN': '10.0.0.0/255.255.255.0', 'KEEP_HTTP': '1'}
    done = subprocess.run(['sh', str(ROOT / 'scripts/front-https.sh')], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    site = next(text for text in (one.read_text() for one in sorted(sent.iterdir())) if 'server_name' in text)
    return site, done.stdout


@pytest.mark.parametrize('version, listen, directive', [
    ('1.28.3', 'listen 10.0.0.5:443 ssl;', True),       # k-srv's Ubuntu release: http2 is its own directive
    ('1.25.1', 'listen 10.0.0.5:443 ssl;', True),
    ('1.24.0', 'listen 10.0.0.5:443 ssl http2;', False),  # older Debian and Ubuntu releases take the listen flag
    ('1.18.0', 'listen 10.0.0.5:443 ssl http2;', False),
])
def test_the_site_turns_http2_on_in_the_form_its_nginx_reads(tmp_path, version, listen, directive):
    site, said = run(tmp_path, version)
    assert listen in site and bool(re.search(r'(?m)^\s*http2 on;$', site)) is directive
    assert 'proxy_pass http://127.0.0.1:8440;' in site and 'proxy_buffering off;' in site  # streams still pass straight through
    assert 'HTTPS answers over HTTP/2' in said


def test_an_answer_over_http1_is_reported(tmp_path):
    _, said = run(tmp_path, '1.28.3', protocol='1.1')
    assert 'HTTPS answers over HTTP/1.1' in said and 'check a browser' in said
