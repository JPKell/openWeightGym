"""Build the CPU installer and transcribe/translate real audio with mounted large-v3 weights."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import tomllib

from starlette.testclient import TestClient

from engines.whisper_cpp.install import REVISION
from engines.whisper_cpp.runtime import create_app

ROOT = Path('/qualification')
MODEL = Path('/models/ggml-large-v3.bin')
ROOT.mkdir(exist_ok=True)
command = [sys.executable, '/app/engines/whisper_cpp/install.py', '--root', str(ROOT), '--backend', 'cpu']
if Path('/source').is_dir():
    command += ['--source', '/source']
subprocess.run(command, check=True)
assert MODEL.stat().st_size > 2_000_000_000, 'Mount the real large-v3 model'
installation = tomllib.loads((ROOT/'settings/loadcoach.toml').read_text())['engines']['whispercpp']
assert json.loads(Path(installation['manifest']).read_text())['revision'] == REVISION
spanish = ROOT/'spanish.wav'
subprocess.run(['espeak-ng', '-v', 'es', '-s', '140', '-w', str(spanish),
                'Buenos días. Esta es una prueba de traducción al inglés.'], check=True)
settings = {'engine': 'whispercpp', 'flags': {'model': str(MODEL), 'ctx-size': 0}, 'gpus': 0,
            'whispercpp': {'threads': 6, 'timeout': 1800}}
job = {'settings': settings, 'installation': installation, 'cards': [], 'port': 0, 'token': 'qualification-only'}
records = []
with TestClient(create_app(job), headers={'authorization': 'Bearer qualification-only'}) as client:
    for path, file, expected in [('/v1/audio/transcriptions', Path('/source/samples/jfk.wav'), 'country'),
                                 ('/v1/audio/translations', spanish, 'english')]:
        if not file.exists():
            file = Path('/qualification/jfk.wav')
            import urllib.request
            urllib.request.urlretrieve('https://raw.githubusercontent.com/ggml-org/whisper.cpp/'+REVISION+'/samples/jfk.wav', file)
        began = time.monotonic()
        answer = client.post(path, json={'model': 'large-v3', 'file': base64.b64encode(file.read_bytes()).decode(), 'response_format': 'json'})
        assert answer.status_code == 200, answer.text
        text = answer.json()['text']
        assert expected in text.lower(), text
        record = {'endpoint': path, 'text': text, 'seconds': round(time.monotonic()-began, 2), 'revision': REVISION}
        records.append(record)
        print(json.dumps(record), flush=True)
(ROOT/'results.json').write_text(json.dumps(records, indent=2)+'\n')
