"""No system controls: cgroup contracts use private temporary control-file fixtures."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from loadcoach.engine_memory import MemoryGroup, delegated_parent, enter, MIB


@pytest.fixture
def controls(tmp_path):
    proc, mount = tmp_path/"proc", tmp_path/"cgroup"
    (proc/"self").mkdir(parents=True)
    current = mount/"fixture.service/supervisor"
    current.mkdir(parents=True)
    (proc/"self/cgroup").write_text("0::/fixture.service/supervisor\n")
    parent = current.parent
    for name, value in {"cgroup.procs": "", "cgroup.controllers": "memory pids", "cgroup.subtree_control": "",
                        "memory.max": str(512*MIB), "memory.swap.max": "0"}.items():
        (parent/name).write_text(value)
    return proc, mount, parent


def test_requires_explicit_delegated_supervisor_and_finite_no_swap_parent(controls):
    proc, mount, parent = controls
    assert delegated_parent(proc, mount) == parent
    for name, bad in (("cgroup.procs", "123"), ("cgroup.controllers", "pids"),
                      ("memory.max", "max"), ("memory.swap.max", "max")):
        path = parent/name
        before = path.read_text()
        path.write_text(bad)
        with pytest.raises(ValueError):
            MemoryGroup.create(64, proc=proc, mount=mount)
        assert not list(parent.glob("engine-*"))
        path.write_text(before)
    for membership in ("0::/fixture.service\n", "0::/other/supervisor\n", "0::/../../outside/supervisor\n", ""):
        (proc/"self/cgroup").write_text(membership)
        with pytest.raises(ValueError):
            delegated_parent(proc, mount)


def test_child_cap_never_enlarges_worker_limits_and_refuses_changed_controls(controls):
    proc, mount, parent = controls
    for budget in (True, 0, -1, 513):
        with pytest.raises(ValueError):
            MemoryGroup.create(budget, proc=proc, mount=mount)
    assert (parent/"cgroup.subtree_control").read_text() == ""
    group = MemoryGroup.create(64, proc=proc, mount=mount)
    assert group.path.parent == parent and group.path.name.startswith("engine-")
    assert (group.path/"memory.max").read_text() == str(64*MIB)
    assert (group.path/"memory.swap.max").read_text() == "0"
    assert (group.path/"memory.oom.group").read_text() == "1"
    assert (parent/"memory.max").read_text() == str(512*MIB)
    for name, bad in (("memory.max", "max"), ("memory.swap.max", "1"), ("memory.oom.group", "0")):
        path = group.path/name
        before = path.read_text()
        path.write_text(bad)
        with pytest.raises(ValueError):
            group.verify()
        path.write_text(before)


def test_child_attaches_only_to_generated_sibling_and_verifies_membership(controls, monkeypatch):
    proc, mount, parent = controls
    group = MemoryGroup.create(64, proc=proc, mount=mount)
    real_write = Path.write_text
    def write(path, value, *args, **kwargs):
        result = real_write(path, value, *args, **kwargs)
        if path == group.path/"cgroup.procs":
            real_write(proc/"self/cgroup", "0::/fixture.service/"+group.path.name+"\n")
        return result
    monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(ValueError, match="generated child"):
        enter(str(parent/"supervisor"), 64, proc=proc, mount=mount)
    enter(str(group.path), 64, proc=proc, mount=mount)
    assert (group.path/"cgroup.procs").read_text() == str(os.getpid())


def test_live_cgroup_cannot_be_removed_and_observation_is_explicit(controls, monkeypatch):
    proc, mount, parent = controls
    group = MemoryGroup.create(64, proc=proc, mount=mount)
    (group.path/"cgroup.events").write_text("populated 1\nfrozen 0\n")
    with pytest.raises(RuntimeError, match="reservation remains held"):
        group.close()
    assert not group.closed
    (group.path/"memory.current").write_text(str(20*MIB))
    (group.path/"memory.peak").write_text(str(40*MIB))
    (group.path/"memory.events.local").write_text("oom 1\noom_kill 1\n")
    assert group.observation() == {"current_mib": 20, "peak_mib": 40, "events": {"oom": 1, "oom_kill": 1}}
    (group.path/"cgroup.events").write_text("populated 0\nfrozen 0\n")
    removed = []
    monkeypatch.setattr(Path, "rmdir", lambda path: removed.append(path))
    group.close()
    group.close()
    assert group.closed and removed == [group.path]


def test_missing_delegation_refuses_runner_launch_before_spawning(monkeypatch):
    import asyncio
    from engines import engine_for
    from engines.contracts import Launch
    from loadcoach.config import Config
    from loadcoach.models import Models
    from loadcoach.runner import Runner, Server, Refusal
    engine = engine_for()
    monkeypatch.setattr(engine, "host_ram_budget", lambda *args: 96)
    monkeypatch.setattr(engine, "launch", lambda *args: Launch(["/fixture/native"], {}, process_group=True, memory_mib=96))
    def unavailable(*args):
        raise ValueError("fixture delegation unavailable")
    monkeypatch.setattr(MemoryGroup, "create", unavailable)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: pytest.fail("No engine may spawn without its cap"))
    runner = Runner(Config(gpus=("CUDA0",)), None, models=Models(":memory:"))
    server = Server("fixture", ("CUDA0",), {"flags":{"model":"/fixture.gguf","ctx-size":512},"gpus":1})
    async def exercise():
        with pytest.raises(Refusal, match="delegation unavailable"):
            await runner.start(server)
        assert server.process is None and not runner.retiring
    asyncio.run(exercise())


@pytest.mark.skipif(os.environ.get('OWG_NATIVE_MEMORY') != '1', reason='Opt-in native systemd qualification')
def test_native_systemd_engine_child_enters_its_cap_and_cleans_up():
    """Exercise real Linux controls and the actual pre-exec helper without a container."""
    root = Path(__file__).resolve().parents[1]
    code = '''
import subprocess,sys
from pathlib import Path
from loadcoach.engine_memory import MemoryGroup,delegated_parent
parent=delegated_parent()
before=(parent/'memory.max').read_text()
group=MemoryGroup.create(32)
try:
    answer=subprocess.run([sys.executable,'-I','-B','loadcoach/engine_child.py',
        '--cgroup',str(group.path),'--memory-mib','32','--',sys.executable,'-I','-c',
        "from pathlib import Path;print(Path('/proc/self/cgroup').read_text())"],
        text=True,capture_output=True,check=True)
    assert group.path.name in answer.stdout,answer.stdout
    assert group.observation()['peak_mib']>0
    assert (parent/'memory.max').read_text()==before
finally:
    group.close()
assert not group.path.exists()
print('native bounded engine launch and cleanup passed')
'''
    answer = subprocess.run(['systemd-run', '--user', '--wait', '--pipe', '--collect', '--quiet',
        '--unit=owg-engine-memory-test-' + uuid.uuid4().hex[:12], '--working-directory=' + str(root),
        '-p', 'Delegate=memory', '-p', 'DelegateSubgroup=supervisor',
        '-p', 'MemoryMax=128M', '-p', 'MemorySwapMax=0', sys.executable, '-c', code],
        text=True, capture_output=True, timeout=30)
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert 'native bounded engine launch and cleanup passed' in answer.stdout
