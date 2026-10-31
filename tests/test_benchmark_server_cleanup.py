"""A quality run must release its shared probe server when stopped or restarted."""

import time

import pytest

from benchmark_clients import gateway
from benchmarks.suites import tables
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder
from test_benchmarks import bench_model, start, wait
from test_weightroom import gguf


def test_dispatch_reaps_dead_probe_reservation_without_reaping_calibration():
    import asyncio
    from types import SimpleNamespace
    import httpx
    from loadcoach.config import Config
    from loadcoach.runner import Runner, Server

    async def scenario():
        async with httpx.AsyncClient() as client:
            runner = Runner(Config(gpus=['GPU0', 'GPU1']), client, models=SimpleNamespace())
            runner.seen = {'cards': {0: {'total': 16000, 'used': 0},
                                     1: {'total': 16000, 'used': 0}}, 'pids': {}}
            exited = SimpleNamespace(pid=-1, returncode=-9)
            probe = Server('probe', ('GPU0',), process=exited, probing=True)
            calibration = Server('calibration', ('GPU1',), process=exited, calibrating=True)
            runner.servers.update(probe=probe, calibration=calibration)
            assert runner.free('GPU0') < 0
            dispatcher = asyncio.create_task(runner.dispatch())
            try:
                runner.wake.set()
                for _ in range(20):
                    await asyncio.sleep(.01)
                    if 'probe' not in runner.servers:
                        break
                assert 'probe' not in runner.servers
                assert runner.free('GPU0') == 16000 - runner.config.card_margin
                assert runner.servers['calibration'] is calibration
            finally:
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize('dead', [True, False])
def test_probe_drops_dead_or_replaced_kept_server_before_reuse(dead):
    import asyncio
    from types import SimpleNamespace
    from loadcoach.probes import Probes
    from loadcoach.runner import Server

    async def scenario():
        old = Server('small', ('GPU0',), process=SimpleNamespace(returncode=-9 if dead else None))
        replacement = object()
        lock, old_ticket = object(), object()
        runner = SimpleNamespace(servers={'small': old if dead else replacement}, lock=lock,
                                 admitting=0, running=[], waiting=[], loads=[],
                                 held=lambda: False, wake=asyncio.Event())
        runner.holder = lambda: runner.lock
        acquired, released, stopped = [], [], []
        async def stop(server):
            stopped.append(server)
        async def acquire(ticket):
            acquired.append(ticket)
            runner.lock = ticket.wants
        runner.stop, runner.acquire, runner.release = stop, acquire, released.append
        probe = Probes(runner, lambda: False, lambda _: True)
        watch = asyncio.create_task(asyncio.sleep(60))
        probe.kept = dict(task='run', server=old, lock=lock, ticket=old_ticket, watch=watch)
        async def sample(task, options, kept):
            assert kept is None
            return {'fresh': True}
        async def connected():
            return False
        probe.sample = sample
        try:
            answer = await probe.probe(SimpleNamespace(is_disconnected=connected),
                                       {'id': 'run', 'user': 'owner', 'subject': {'settings': {}}}, {})
            assert answer == {'fresh': True}
            assert stopped == [old] and released[0] is old_ticket and len(acquired) == 1
            if not dead:
                assert runner.servers['small'] is replacement
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

    asyncio.run(scenario())


def test_stopped_reused_quality_run_releases_server_and_stays_stopped_after_restart(
    fake_llama_server, fake_cards, tmp_path, monkeypatch
):
    original = tables.cases
    monkeypatch.setattr(tables, "cases", lambda limit: original(limit)[:8])
    models = bench_model(.3)
    gguf("small.gguf", general__architecture="tiny", tiny__context_length=32768,
         general__parameter_count=123456)
    models["small"]["flags"]["ctx-size"] = 32768
    database = str(tmp_path / "cleanup.db")

    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0], database=database) as caller:
        owner = add_user(caller, "owner", "admin")
        number = start(caller, owner, "tables")
        sampled = wait(caller, owner, number, lambda run: run["samples_count"] >= 2, seconds=30)
        assert sampled["samples"][1]["values"]["server_reused"] is True
        assert caller.post("/benchmarks/control", headers=owner,
                           json={"run": number, "action": "stop"}).status_code == 200
        stopped = wait(caller, owner, number, lambda run: run["state"] == "cancelled", seconds=15)
        assert stopped["samples_count"] >= 2
        deadline = time.monotonic() + 5
        while True:
            status = caller.get("/loadcoach/status", headers=owner).json()
            if status["lock"] is None or time.monotonic() >= deadline:
                break
            time.sleep(.03)
        assert status["running"] == [] and status["lock"] is None

    with gateway(fake_llama_server, models=models, nvidia_smi=fake_cards[0], database=database) as caller:
        recovered = caller.get("/benchmarks/" + number, headers=owner).json()
        assert recovered["state"] == "cancelled"
        assert recovered["samples"] == stopped["samples"]
        status = caller.get("/loadcoach/status", headers=owner).json()
        assert status["running"] == [] and status["lock"] is None
