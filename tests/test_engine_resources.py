"""Mixed RAM/VRAM admission and owned process trees, using only CPU fixtures."""
import asyncio
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace

import pytest

import engines
from engines.contracts import Launch
from engines.strata import model, resources
from loadcoach.config import Config
from loadcoach.models import Models
from loadcoach.processes import ProcessGroup, adopt_orphans
from loadcoach.runner import ConfiguredRamCapacity, Refusal, Runner, Server, Ticket


@pytest.fixture
def hybrid(tmp_path, monkeypatch):
    monkeypatch.setitem(engines._ENGINES, "strata", SimpleNamespace(id="strata", model=model,
        host_ram_budget=resources.host_ram_budget))
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    for name in model.TOKENIZER_FILES:
        (tokenizer/name).write_text("fixture")
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(b"fixture")
    settings = {"engine": "strata", "flags": {"model": str(weights), "ctx-size": 512},
                "strata": {"tokenizer": str(tokenizer)}, "gpus": 1}
    library = Models(":memory:")
    library.put("hybrid", settings)
    library.put("cpu", {"flags": {"model": str(weights), "ctx-size": 512}, "gpus": 0})
    runner = Runner(Config(gpus=("CUDA0", "CUDA1"), card_margin=100, host_ram_headroom_mib=128,
        engines={"strata": {"enabled": True, "allow_host_ram": True, "host_ram_mib": 2048,
                            "source": "/fixture/source", "python": "/fixture/python", "executable": "/fixture/native"}}),
        None, models=library)
    runner.seen = {"cards": {0: {"total": 16000, "used": 0}, 1: {"total": 16000, "used": 0}}, "pids": {}}
    memory = {"total": 32000, "available": 8000, "service_available": 8000}
    monkeypatch.setattr("loadcoach.runner.memory_snapshot", lambda: dict(memory))
    return runner, settings, memory


def test_hybrid_requires_both_memory_pools_and_explicit_opt_in(hybrid):
    runner, settings, memory = hybrid
    assert runner.fit("hybrid", settings, set()) == (settings, ("CUDA0",), [])
    assert runner.ram_required("hybrid", settings) == 2048
    # A measured tiny RSS must never shrink the explicit full-cap reservation.
    runner.models.resident_measure("hybrid", settings, 128)
    assert runner.ram_required("hybrid", settings) == 2048
    memory["available"] = 2048
    assert runner.fit("hybrid", settings, set()) is None
    memory["available"] = 8000
    for available in (None, 0, 2047):
        memory["service_available"] = available
        assert runner.fit("hybrid", settings, set()) is None
    memory["service_available"] = 8000
    runner.config.host_ram_limit_mib = 2047
    assert runner.fit("hybrid", settings, set()) is None
    runner.config.host_ram_limit_mib = 0
    runner.config.engines["strata"]["allow_host_ram"] = False
    with pytest.raises(ValueError, match="explicit allow_host_ram"):
        runner.fit("hybrid", settings, set())
    assert runner.config.disable_ram_offload is True  # no llama.cpp policy mutation


def test_hybrid_host_fit_does_not_override_a_failed_vram_fit(hybrid):
    runner, settings, memory = hybrid
    runner.models.measured("hybrid", settings, [15950])
    assert runner.host_memory()["free"] > 2048
    assert runner.fit("hybrid", settings, set()) is None


def test_hybrid_reload_can_use_verified_clean_cache_but_cpu_and_reservations_are_unchanged(hybrid):
    runner, settings, memory = hybrid
    memory.update(service_available=0, service_reclaimable_available=8000)
    assert runner.fit("hybrid", settings, set()) == (settings, ("CUDA0",), [])
    assert runner.fit("cpu", runner.models.get("cpu"), set()) is None
    memory['service_reclaimable_available'] = 2047
    assert runner.fit("hybrid", settings, set()) is None
    memory['service_reclaimable_available'] = None
    assert runner.fit("hybrid", settings, set()) is None
    memory['service_reclaimable_available'] = 8000
    server = Server("hybrid", ("CUDA0",), settings, ready=True, rss_mib=128)
    runner.servers["hybrid"] = server
    assert runner.host_memory(cache_admission=True)['hybrid_reserved'] == 2048
    assert runner.host_memory(cache_admission=True)['service_free'] == 5952


def test_hybrid_post_eviction_recheck_uses_the_same_verified_cache_room(hybrid):
    runner, settings, memory = hybrid
    memory.update(service_available=512, service_reclaimable_available=8000)
    server = Server("hybrid", ("CUDA0",), settings)
    runner.servers["hybrid"] = server
    started = []
    async def start(one):
        started.append(one)
    async def measure(one):
        one.rss_mib = 128
    async def version(*args):
        return 'fixture'
    runner.start, runner.measure, runner.version = start, measure, version
    asyncio.run(runner.bring_up(server, []))
    assert started == [server] and server.ready


def test_hybrid_reservations_cover_loading_resident_and_retiring_servers(hybrid):
    runner, settings, memory = hybrid
    server = Server("hybrid", ("CUDA0",), settings, rss_mib=256)
    runner.servers["hybrid"] = server
    assert runner.host_memory()["hybrid_reserved"] == 2048
    assert runner.host_memory()["free"] == 8000-128-2048
    cpu = runner.models.get("cpu")
    assert runner.fit("cpu", cpu, set()) is None  # initial hybrid load serializes host use
    server.ready = True
    assert runner.host_memory()["hybrid_reserved"] == 2048
    runner.config.host_ram_limit_mib = 2300
    assert runner.host_memory()["free"] == 252
    runner.retiring.add(server)
    del runner.servers["hybrid"]
    assert runner.host_memory()["hybrid_reserved"] == 2048
    assert runner.place("hybrid", settings, set()) is None
    runner.config.host_ram_limit_mib = 0
    assert runner.place("hybrid", settings, set())[0] == ("CUDA1",)


def test_hybrid_can_reclaim_only_idle_unprotected_cpu_ram(hybrid):
    runner, settings, memory = hybrid
    memory["available"] = 1000
    old = Server("cpu", (), runner.models.get("cpu"), ready=True, rss_mib=1800)
    runner.servers["cpu"] = old
    assert runner.fit("hybrid", settings, set())[2] == [old]
    ticket = Ticket("cpu", "owner", "fixture", "normal", server=old)
    runner.running.append(ticket)
    assert runner.fit("hybrid", settings, set()) is None
    runner.release(ticket)
    runner.holds["cpu", "owner"] = time.monotonic()+60
    assert runner.fit("hybrid", settings, set()) is None
    runner.holds.clear()
    runner.models.set_locked("cpu", True)
    assert runner.fit("hybrid", settings, set()) is None


def test_an_idle_engine_holding_host_ram_makes_way_for_a_cpu_model(hybrid):
    runner, settings, memory = hybrid
    resident = Server("hybrid", ("CUDA0",), settings, ready=True, rss_mib=900)
    runner.servers["hybrid"] = resident
    runner.config.host_ram_limit_mib = 2100  # the idle reservation leaves too little for the CPU model
    cpu = runner.models.get("cpu")
    assert runner.fit("cpu", cpu, set())[2] == [resident]
    ticket = Ticket("hybrid", "owner", "fixture", "normal", server=resident)
    runner.running.append(ticket)
    assert runner.fit("cpu", cpu, set()) is None  # one at work keeps its room
    runner.release(ticket)
    runner.holds["hybrid", "owner"] = time.monotonic()+60
    assert runner.fit("cpu", cpu, set()) is None


def test_hybrid_rechecks_ram_after_eviction_and_before_native_start(hybrid):
    runner, settings, memory = hybrid
    child = Server("hybrid", ("CUDA0",), settings)
    runner.servers["hybrid"] = child
    ticket = Ticket("hybrid", "owner", "fixture", "normal")
    runner.waiting.append(ticket)
    memory["available"] = 1500
    stopped = []
    async def start(server):
        pytest.fail("Must not launch with insufficient fresh RAM")
    async def stop(server):
        stopped.append(server.model)
    runner.start, runner.stop = start, stop
    asyncio.run(runner.bring_up(child, []))
    assert stopped == ["hybrid"] and ticket.error.status == 503
    assert not runner.servers and not child.ready


def test_hybrid_readiness_checks_actual_tree_rss_and_node_budget(hybrid):
    runner, settings, memory = hybrid
    server = Server("hybrid", ("CUDA0",), settings, rss_mib=2049)
    with pytest.raises(ConfiguredRamCapacity, match="engine host RAM budget"):
        runner.assert_cpu_memory(server)
    server.rss_mib = None
    with pytest.raises(Refusal, match="could not be measured"):
        runner.assert_cpu_memory(server)
    server.rss_mib = 1800
    runner.assert_cpu_memory(server)
    memory["available"] = 100
    with pytest.raises(Refusal, match="headroom"):
        runner.assert_cpu_memory(server)


def test_bounded_hybrid_readiness_uses_verified_charge_not_duplicated_process_rss(hybrid):
    runner, settings, memory = hybrid
    observation = {'current_mib': 1800, 'events': {}}
    checked = []
    group = SimpleNamespace(budget_mib=2048, verify=lambda: checked.append(True), observation=lambda: observation)
    server = Server('hybrid', ('CUDA0',), settings, rss_mib=4000, memory_group=group)
    runner.assert_cpu_memory(server)
    assert checked == [True] and server.rss_mib == 4000  # keep the diagnostic measurement
    observation['current_mib'] = 2049
    with pytest.raises(ConfiguredRamCapacity, match='engine host RAM budget'):
        runner.assert_cpu_memory(server)
    observation['current_mib'] = 1800
    observation['events'] = {'oom_kill': 1}
    with pytest.raises(Refusal, match='OOM kill'):
        runner.assert_cpu_memory(server)
    observation['events'] = {}
    group.budget_mib = 4096
    with pytest.raises(Refusal, match='admitted RAM budget'):
        runner.assert_cpu_memory(server)
    group.budget_mib = 2048
    def unavailable():
        raise OSError('Cgroup unavailable')
    group.verify = unavailable
    with pytest.raises(Refusal, match='could not be verified'):
        runner.assert_cpu_memory(server)


def test_bounded_hybrid_charge_does_not_bypass_host_or_node_limits(hybrid):
    runner, settings, memory = hybrid
    group = SimpleNamespace(budget_mib=2048, verify=lambda: None,
        observation=lambda: {'current_mib': 1800, 'events': {}})
    server = Server('hybrid', ('CUDA0',), settings, rss_mib=4000, memory_group=group)
    runner.config.host_ram_limit_mib = 2047
    with pytest.raises(ConfiguredRamCapacity, match='configured host RAM limit'):
        runner.assert_cpu_memory(server)
    runner.config.host_ram_limit_mib = 0
    memory['available'] = 100
    with pytest.raises(Refusal, match='headroom'):
        runner.assert_cpu_memory(server)


@pytest.fixture
def fork_program(tmp_path):
    file = tmp_path / "owned-fixture.py"
    file.write_text('''import os,signal,sys,time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(os.getpid(), flush=True)
    while True: time.sleep(.05)
if sys.argv[-1] == "orphan":
    time.sleep(.1)
    os._exit(7)
while True: time.sleep(.05)
''')
    return file


@pytest.mark.parametrize("mode", ["normal", "orphan"])
def test_group_kills_and_reaps_native_orphans_without_touching_other_processes(fork_program, mode):
    async def exercise():
        adopt_orphans()
        unrelated = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(30)")
        process = await asyncio.create_subprocess_exec(sys.executable, str(fork_program), mode,
            start_new_session=True, stdout=asyncio.subprocess.PIPE)
        group = ProcessGroup(process)
        child = int(await asyncio.wait_for(process.stdout.readline(), 3))
        try:
            assert child in group.members()
            if mode == "orphan":
                # wait() also waits for stdout EOF, which the orphan still owns.
                for _ in range(100):
                    if process.returncode is not None:
                        break
                    await asyncio.sleep(.01)
                assert process.returncode == 7 and child in group.members()
            await group.stop(.05)
            assert group.closed and not group.members()
            assert not Path(f"/proc/{child}").exists()  # not even an adopted zombie
            assert unrelated.returncode is None
            await group.stop(.05)  # idempotent, never signal a recycled process-group ID
        finally:
            await group.stop(.05)
            unrelated.terminate()
            await unrelated.wait()
    asyncio.run(exercise())


def test_runner_stop_holds_reservations_through_repeated_cancellation(hybrid, fork_program):
    runner, settings, memory = hybrid
    async def exercise():
        adopt_orphans()
        process = await asyncio.create_subprocess_exec(sys.executable, str(fork_program), "normal",
            start_new_session=True, stdout=asyncio.subprocess.PIPE)
        child = int(await asyncio.wait_for(process.stdout.readline(), 3))
        group = ProcessGroup(process)
        server = Server("hybrid", ("CUDA0",), settings, process=process, owned_group=group, stop_seconds=.15)
        runner.servers["hybrid"] = server
        task = asyncio.create_task(runner.stop(server))
        await asyncio.sleep(.02)
        assert server in runner.retiring and not group.closed
        assert runner.host_memory()["hybrid_reserved"] == 2048
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert group.closed and server not in runner.retiring and not Path(f"/proc/{child}").exists()
        runner.servers.clear()
        assert runner.host_memory()["hybrid_reserved"] == 0
    asyncio.run(exercise())


def test_failed_cleanup_keeps_gpu_and_host_reservations(hybrid):
    runner, settings, memory = hybrid
    class FailedGroup:
        closed = False
        def members(self):
            return {}
        async def stop(self, seconds):
            raise RuntimeError("fixture cleanup failed")
    server = Server("hybrid", ("CUDA0",), settings, owned_group=FailedGroup())
    async def exercise():
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await runner.stop(server)
        assert server in runner.retiring
        assert runner.host_memory()["hybrid_reserved"] == 2048
        assert runner.status()["stopping"] == [{"model": "hybrid", "gpus": ["CUDA0"], "cleanup_failed": True}]
        assert runner.place("hybrid", settings, set()) is None
        gpu = {"flags": settings["flags"], "gpus": 1}
        assert runner.place("gpu", gpu, set())[0] == ("CUDA1",)
    asyncio.run(exercise())


def test_stop_all_retries_removed_servers_without_releasing_early(hybrid):
    runner, settings, memory = hybrid
    class RetryGroup:
        closed = False
        calls = 0
        async def stop(self, seconds):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("fixture temporary failure")
            self.closed = True
    group = RetryGroup()
    server = Server("hybrid", ("CUDA0",), settings, owned_group=group)
    async def exercise():
        with pytest.raises(RuntimeError):
            await runner.stop(server)
        assert server in runner.retiring
        await runner.stop_all()
        assert group.closed and group.calls == 2 and not runner.retiring
    asyncio.run(exercise())


def test_group_gpu_usage_and_rss_include_children_after_wrapper_exit(hybrid, monkeypatch):
    runner, settings, memory = hybrid
    process = SimpleNamespace(pid=123, returncode=7)
    group = SimpleNamespace(closed=False, members=lambda: {124: "S", 125: "S"})
    server = Server("hybrid", ("CUDA0",), settings, process=process, owned_group=group)
    runner.seen["pids"] = {124: {0: 5000}, 125: {0: 100}}
    assert runner.uses(server) == [5100]
    monkeypatch.setattr("loadcoach.runner.resident_mib", lambda pid, descendants: {124: 1000, 125: 100}[pid])
    runner.sample_resident(server)
    assert server.rss_mib == 1100 and server.rss_peak_mib == 1100


def test_gpu_membership_is_sampled_once_per_reading_and_never_reused_for_closed_or_replaced_groups(hybrid):
    runner, settings, _ = hybrid
    calls = []
    members = {124: 'S'}
    def scan():
        calls.append(True)
        return dict(members)
    group = SimpleNamespace(closed=False, members=scan)
    server = Server('hybrid', ('CUDA0',), settings, owned_group=group)
    runner.seen['pids'] = {124: {0: 5000}, 125: {0: 100}}
    for _ in range(100):
        assert runner.uses(server) == [5000]
    assert len(calls) == 1

    members[125] = 'S'
    runner.seen = {**runner.seen}
    assert runner.uses(server) == [5100] and len(calls) == 2
    group.closed = True
    assert runner.uses(server) is None
    server.owned_group = SimpleNamespace(closed=False, members=lambda: {125: 'S'})
    assert runner.uses(server) == [100]
    server.cards = ()
    assert runner.uses(server) is None


def test_launch_can_disable_native_output_capture_without_changing_legacy_defaults():
    assert Launch(["fixture"], {}).capture_output is True
    assert Launch(["fixture"], {}).process_group is False


def test_closed_or_recycled_process_group_is_never_signalled(monkeypatch):
    columns = ["S", "1", "123", "123", *["0"] * 15, "1"]
    monkeypatch.setattr(ProcessGroup, "_stat", staticmethod(lambda pid: list(columns)))
    group = ProcessGroup(SimpleNamespace(pid=123, returncode=0))
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("Recycled process group must not be signalled"))
    columns[19] = "2"
    group.signal(signal.SIGKILL)
    assert group.closed and group.members() == {}


def test_runner_owned_launch_readiness_failure_cleans_tree_and_discards_output(fork_program, monkeypatch):
    runner = Runner(Config(gpus=("CUDA0",), load_timeout=1), None, models=Models(":memory:"))
    settings = {"flags": {"model": "/fixture.gguf", "ctx-size": 512}, "gpus": 1}
    engine = engines.engine_for()
    monkeypatch.setattr(engine, "launch", lambda *args: Launch([sys.executable, str(fork_program), "normal"],
        dict(os.environ), process_group=True, capture_output=False, stop_seconds=.05))
    server = Server("fixture", ("CUDA0",), settings)
    async def unready(server, process):
        for _ in range(100):
            if len(server.owned_group.members()) >= 2:
                break
            await asyncio.sleep(.01)
        assert len(server.owned_group.members()) >= 2
        raise Refusal(502, "fixture readiness failed")
    runner.wait_until_ready = unready
    async def exercise():
        with pytest.raises(Refusal, match="readiness failed"):
            await runner.start(server)
        assert server.owned_group.closed and not server.owned_group.members()
        assert server.listener is None and server.said == b""
        assert not runner.retiring
    asyncio.run(exercise())


def test_repeated_cancellation_during_spawn_handoff_still_reaps_child(monkeypatch):
    runner = Runner(Config(gpus=("CUDA0",)), None, models=Models(":memory:"))
    settings = {"flags": {"model": "/fixture.gguf", "ctx-size": 512}, "gpus": 1}
    monkeypatch.setattr(engines.engine_for(), "launch", lambda *args: Launch(
        [sys.executable, "-c", "import time;time.sleep(30)"], dict(os.environ), process_group=True, capture_output=False))
    server = Server("fixture", ("CUDA0",), settings)
    async def exercise():
        real_spawn = asyncio.create_subprocess_exec
        spawned, resume = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await resume.wait()
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
        task = asyncio.create_task(runner.start(server))
        await asyncio.wait_for(spawned.wait(), 3)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert processes[0].returncode is not None and server.owned_group.closed
        assert not runner.retiring
    asyncio.run(exercise())
