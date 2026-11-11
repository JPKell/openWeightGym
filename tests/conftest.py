"""Shared test set-up: the stand-in nvidia-smi is answered in this process, not by starting one.

LoadCoach asks nvidia-smi what each card holds before and during every model load and benchmark
probe, a fresh Python process each time. That was a large share of the suite's time. Here
Runner.smi answers from fake_nvidia_smi.answer() directly when the configured program is the
stand-in; LoadCoach's reading of the answer (look, uses, free, peaks) runs unchanged. Any other
program, such as a missing one, still goes through the real subprocess call.
test_fake_cards_in_process.py checks that both ways give the same rows.
"""

from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import fake_nvidia_smi
from loadcoach.runner import Runner

REAL_SMI = Runner.smi


@pytest.fixture
def backup_keys(tmp_path):
    """Real age encryption in backup tests; private recovery identities never enter host archives."""
    if not shutil.which('age') or not shutil.which('age-keygen'):
        pytest.fail('Backup security tests require the age package (age and age-keygen).')
    identity = tmp_path / 'test-recovery.key'
    subprocess.run(['age-keygen', '-o', str(identity)], check=True, capture_output=True)
    identity.chmod(0o600)
    recipient = subprocess.run(['age-keygen', '-y', str(identity)], check=True, capture_output=True).stdout

    def configure(root):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        path = root / 'recipients.txt'
        path.write_bytes(recipient)
        path.chmod(0o600)
        return root

    return SimpleNamespace(identity=identity, recipient=recipient.decode().strip(), configure=configure)


@pytest.fixture(autouse=True)
def isolate_host_gpu_discovery(monkeypatch):
    """Tests must never inherit this workstation's physical GPU inventory."""
    monkeypatch.setattr('loadcoach.devices.hardware', lambda **kwargs: [])


def is_stand_in(program: str) -> bool:
    """Whether a configured nvidia-smi is a script that runs fake_nvidia_smi.py."""
    path = Path(program)
    try:
        return path.is_file() and path.stat().st_size < 4096 and "fake_nvidia_smi.py" in path.read_text()
    except (OSError, UnicodeDecodeError):
        return False


def rows(printed: str) -> list[list[str]]:
    """Cells as Runner.smi splits nvidia-smi's CSV output."""
    return [[cell.strip() for cell in line.split(",")] for line in printed.splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def stand_in_cards_answer_in_process(monkeypatch):
    """Answer the stand-in nvidia-smi without starting a process for it."""
    async def smi(self, query: str) -> list[list[str]]:
        if is_stand_in(self.config.nvidia_smi):
            return rows(fake_nvidia_smi.answer([query, "--format=csv,noheader,nounits"]))
        return await REAL_SMI(self, query)
    monkeypatch.setattr(Runner, "smi", smi)
