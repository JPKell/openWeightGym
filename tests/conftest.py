"""Shared test set-up: the stand-in nvidia-smi is answered in this process, not by starting one.

LoadCoach asks nvidia-smi what each card holds before and during every model load and benchmark
probe, a fresh Python process each time. That was a large share of the suite's time. Here
Runner.smi answers from fake_nvidia_smi.answer() directly when the configured program is the
stand-in; LoadCoach's reading of the answer (look, uses, free, peaks) runs unchanged. Any other
program, such as a missing one, still goes through the real subprocess call.
test_fake_cards_in_process.py checks that both ways give the same rows.
"""

import ast
import inspect
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
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


# ---- the quick default run -------------------------------------------------------------------
# A plain `pytest` leaves out the tests that drive Chromium: they are most of the suite's CPU
# time. `pytest --browser` runs everything, and naming files or tests runs all of those,
# browser tests included. A test counts as a browser test when its body, a helper it calls or
# one of its fixtures uses Playwright, so plain tests in the same file still run by default.

BROWSER_WORDS = re.compile(r'\b(playwright|chromium|browser_context)\b')
TESTS = Path(__file__).resolve().parent


def pytest_addoption(parser):
    parser.addoption('--browser', action='store_true',
                     help='also run the Chromium tests a plain run leaves out')


def drives_browser(function, seen) -> bool:
    """Whether this test-suite function, or one it names, uses Playwright."""
    function = inspect.unwrap(function)
    code = getattr(function, '__code__', None)
    if code is None or code in seen or not code.co_filename.startswith(str(TESTS)):
        return False
    seen.add(code)
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    if BROWSER_WORDS.search(source):
        return True
    names = {node.id for node in ast.walk(ast.parse(textwrap.dedent(source))) if isinstance(node, ast.Name)}
    return any(callable(found := function.__globals__.get(name)) and inspect.isfunction(found)
               and drives_browser(found, seen) for name in names)


def is_browser_test(item) -> bool:
    seen = set()
    if drives_browser(item.function, seen):
        return True
    for name in item.fixturenames:
        for definition in item._fixtureinfo.name2fixturedefs.get(name, ()):
            if drives_browser(definition.func, seen):
                return True
    return False


def pytest_collection_modifyitems(config, items):
    for item in items:
        if hasattr(item, 'function') and is_browser_test(item):
            item.add_marker(pytest.mark.browser)
    if config.getoption('--browser') or config.args_source != pytest.Config.ArgsSource.TESTPATHS:
        return
    left_out = [item for item in items if item.get_closest_marker('browser')]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = [item for item in items if not item.get_closest_marker('browser')]
