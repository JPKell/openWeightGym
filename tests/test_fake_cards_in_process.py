"""The stand-in nvidia-smi answers the same in the test process as through the real subprocess call."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from conftest import REAL_SMI
from loadcoach.runner import Runner

QUERIES = ("--query-gpu=index,uuid,name,memory.total,driver_version",
           "--query-gpu=index,uuid,memory.total,memory.used",
           "--query-compute-apps=pid,gpu_uuid,used_memory")


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    """The stand-in script as the fake_cards fixture makes it, with a desktop, a live process and an ended one."""
    folder, script = tmp_path / "cards", tmp_path / "nvidia-smi"
    folder.mkdir()
    monkeypatch.setenv("FAKE_CARDS", str(folder))
    (folder / "0.json").write_text(json.dumps({"0": 500}))
    (folder / f"{os.getpid()}.json").write_text(json.dumps({"1": 3000}))
    (folder / "999999999.json").write_text(json.dumps({"0": 7000}))  # no such process: counts for nothing
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {os.path.join(os.path.dirname(__file__), 'fake_nvidia_smi.py')} \"$@\"\n")
    script.chmod(0o755)
    return SimpleNamespace(config=SimpleNamespace(nvidia_smi=str(script)))


@pytest.mark.parametrize("query", QUERIES)
def test_in_process_answers_match_the_real_subprocess_call(stand_in, query):
    through_process = asyncio.run(REAL_SMI(stand_in, query))
    in_process = asyncio.run(Runner.smi(stand_in, query))
    assert in_process == through_process and through_process


def test_the_answers_show_what_each_card_holds(stand_in):
    used = asyncio.run(Runner.smi(stand_in, QUERIES[1]))
    assert used == [["0", "GPU-0", "16000", "500"], ["1", "GPU-1", "16000", "3000"]]
    assert asyncio.run(Runner.smi(stand_in, QUERIES[2])) == [[str(os.getpid()), "GPU-1", "3000"]]


def test_a_program_that_is_not_the_stand_in_is_still_started(tmp_path):
    missing = SimpleNamespace(config=SimpleNamespace(nvidia_smi=str(tmp_path / "no-such-program")))
    with pytest.raises(OSError):
        asyncio.run(Runner.smi(missing, QUERIES[1]))
