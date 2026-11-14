"""Opt-in pinned CPU installer and real large-v3 transcription/translation qualification."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(not os.environ.get('OWG_WHISPER_MODEL'), reason='Set OWG_WHISPER_MODEL to a real ggml-large-v3.bin for Docker qualification')
def test_pinned_whisper_cpu_installer_and_large_v3(tmp_path):
    root = Path(__file__).resolve().parents[1]
    model = Path(os.environ['OWG_WHISPER_MODEL']).resolve()
    subprocess.run(['docker', 'build', '-f', str(root/'tests/docker/whispercpp/Dockerfile'), '-t', 'owg-whisper-test:local', str(root)], check=True)
    subprocess.run(['docker', 'run', '--rm', '-v', str(model)+':/models/ggml-large-v3.bin:ro',
                    '-v', str(tmp_path)+':/qualification', 'owg-whisper-test:local'], check=True)
    assert (tmp_path/'results.json').is_file()
