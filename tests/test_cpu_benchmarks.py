"""CPU and hybrid text probes retain host RAM evidence alongside their assigned devices."""

import asyncio
from pathlib import Path

import pytest

from benchmark_clients import gateway
from loadcoach.probes import Probes
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder, model  # noqa: F401
from test_benchmarks import start, wait
from test_weightroom import gguf
import loadcoach.probes as probe_module


@pytest.fixture
def native_moe(tmp_path, monkeypatch):
    """Real fake-server processes with simulated kernel caps; native cgroups have separate tests."""
    from loadcoach.engine_memory import MemoryGroup
    room = {'total': 32000, 'available': 12000, 'service_available': 12000}
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: dict(room))
    groups = []
    class Cap:
        def __init__(self, budget):
            self.path = tmp_path / ('engine-' + str(len(groups)))
            self.budget_mib, self.closed = budget, False
        def verify(self):
            assert not self.closed
        def observation(self):
            return {'current_mib': room.get('charge_mib', 128), 'peak_mib': room.get('charge_mib', 128), 'events': {}}
        def close(self):
            self.closed = True
    def create(budget):
        group = Cap(budget)
        groups.append(group)
        return group
    monkeypatch.setattr(MemoryGroup, 'create', create)
    spawn = asyncio.create_subprocess_exec
    async def simulated_cap(*command, **kwargs):
        if any(str(arg).endswith('/engine_child.py') for arg in command):
            command = command[command.index('--') + 1:]
        return await spawn(*command, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', simulated_cap)
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=1024)
    settings = {'small': {'gpus': ['GPU0'], 'host_ram_mib': 2048,
                         'flags': {'model': 'small.gguf', 'ctx-size': 1024, 'gpu-layers': 'all',
                                   'cpu-moe': True, 'moe-cache-mib': 512, 'moe-cache-cpu-share': .5, 'mib': 1000}}}
    return settings, room, groups


@pytest.mark.parametrize('kind', ['load', 'speed'])
def test_native_moe_benchmarks_retain_joint_ram_vram_and_settings(fake_llama_server, fake_cards, native_moe, kind):
    settings, _, groups = native_moe
    with gateway(fake_llama_server, models=settings, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        number = start(caller, owner, kind)
        run = wait(caller, owner, number, lambda row: row['state'] in ('completed', 'failed'), seconds=40)
        assert run['state'] == 'completed', run['reason']
        assert run['subject']['settings'] == settings['small']
        assert run['samples']
        for sample in run['samples']:
            values, resource = sample['values'], sample['evidence']['resource']
            assert values['success'] and values['device'] == 'hybrid'
            assert values['peak_gpu_mib'] == 1000
            assert values['peak_ram_mib'] >= values['rss_mib'] > 0
            assert resource['host_ram_budget_mib'] == values['host_ram_budget_mib'] == 2048
            assert resource['peak_gpu_mib'] == values['peak_gpu_mib']
            assert resource['peak_ram_mib'] == values['peak_ram_mib']
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert not status['loaded'] and status['lock'] is None
    assert groups and all(group.budget_mib == 2048 and group.closed for group in groups)


def test_native_moe_probe_refuses_insufficient_host_room_before_loading(fake_llama_server, fake_cards, native_moe):
    from test_tasks import create
    settings, room, groups = native_moe
    room['available'] = 1024
    with gateway(fake_llama_server, models=settings, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        task = create(caller, owner)
        response = caller.post('/loadcoach/tasks/' + task['id'] + '/probe',
                               headers={'x-loadcoach-task': task['token']}, json={'context': 512})
        assert response.status_code == 503 and 'hybrid host/service RAM budget' in response.text
        assert not groups
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert not status['loaded'] and status['lock'] is None


def test_native_moe_probe_checks_the_child_unified_memory_policy(fake_llama_server, fake_cards, native_moe, monkeypatch):
    from test_tasks import create
    settings, _, groups = native_moe
    monkeypatch.setenv('GGML_CUDA_ENABLE_UNIFIED_MEMORY', '0')
    with gateway(fake_llama_server, models=settings, nvidia_smi=fake_cards[0], disable_ram_offload=False) as caller:
        owner = add_user(caller, 'owner', 'admin')
        task = create(caller, owner)
        response = caller.post('/loadcoach/tasks/' + task['id'] + '/probe',
                               headers={'x-loadcoach-task': task['token']}, json={'context': 512})
        assert response.status_code == 400 and 'Disable CUDA unified memory' in response.text
        assert not groups


def test_native_moe_chat_uses_the_same_flags_and_cap(fake_llama_server, fake_cards, native_moe):
    settings, _, groups = native_moe
    with gateway(fake_llama_server, models=settings, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        response = caller.post('/v1/chat/completions', headers=owner,
                               json={'model': 'small', 'messages': [{'role': 'user', 'content': 'Hello'}]})
        assert response.status_code == 200, response.text
        command = response.json()['arguments']
        assert '--cpu-moe' in command and command[command.index('--moe-cache-mib') + 1] == '512'
        assert command[command.index('--moe-cache-cpu-share') + 1] == '0.5'
        assert len(groups) == 1 and groups[0].budget_mib == 2048
    assert groups[0].closed


def test_native_moe_probe_retains_a_ram_limit_failure_and_releases_both_pools(fake_llama_server, fake_cards, native_moe, monkeypatch):
    from test_tasks import create
    settings, room, groups = native_moe
    complete = Probes.completion
    async def over_budget(self, *args, **kwargs):
        result = await complete(self, *args, **kwargs)
        room['charge_mib'] = 2049
        return result
    monkeypatch.setattr(Probes, 'completion', over_budget)
    with gateway(fake_llama_server, models=settings, nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        task = create(caller, owner)
        response = caller.post('/loadcoach/tasks/' + task['id'] + '/probe',
                               headers={'x-loadcoach-task': task['token']},
                               json={'context': 512, 'inputs': 8, 'outputs': 8, 'text': 'Test'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert not result['values']['success']
        assert result['values']['capacity_basis'] == 'measured_configured_ram'
        assert result['evidence']['resource']['device'] == 'hybrid'
        assert result['values']['peak_gpu_mib'] == 1000
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert not status['loaded'] and status['lock'] is None
    assert groups and all(group.closed for group in groups)


def cpu_model():
    gguf('small.gguf', general__architecture='tiny', tiny__context_length=1024)
    return {'small': model('small.gguf', 0, **{'ctx-size': 1024, 'gpu-layers': 0})}


@pytest.mark.parametrize('kind', ['load', 'context', 'speed'])
def test_cpu_text_measurements_complete_with_native_counts_and_ram(fake_llama_server, monkeypatch, kind):
    def no_gpu_telemetry(*args, **kwargs):
        raise AssertionError('CPU probes must not require GPU telemetry')
    monkeypatch.setattr(Probes, 'outside_processes', no_gpu_telemetry)
    settings = cpu_model()
    with gateway(fake_llama_server, models=settings) as caller:
        owner = add_user(caller, 'owner', 'admin')
        number = start(caller, owner, kind)
        run = wait(caller, owner, number, lambda row: row['state'] in ('completed', 'failed'), seconds=40)
        assert run['state'] == 'completed', run['reason']
        assert run['score'] > 0 and run['samples']
        assert run['subject']['settings']['flags']['gpu-layers'] == 0
        for sample in run['samples']:
            values = sample['values']
            assert values['success'] and values['device'] == 'cpu' and values['cards'] == []
            assert values['peak_gpu_mib'] is None
            assert values['peak_ram_mib'] >= values['rss_mib'] > 0
            assert sample['evidence']['resource'] == {
                'device': 'cpu', 'peak_ram_mib': values['peak_ram_mib'], 'rss_mib': values['rss_mib']}
            if kind != 'load':
                assert values['input_tokens'] > 0 and values['output_tokens'] > 0
                assert values['prompt_tokens_per_second'] > 0 and values['generation_tokens_per_second'] > 0
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert status['lock'] is None and status['loaded'] == []


def test_cpu_text_context_search_retains_measured_ram_failures(fake_llama_server, monkeypatch):
    def measured(pid):
        arguments = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        context = int(arguments[arguments.index(b'--ctx-size') + 1])
        return 700 if context > 512 else 64
    monkeypatch.setattr(probe_module, 'resident_mib', measured)
    with gateway(fake_llama_server, models=cpu_model(), host_ram_limit_mib=512) as caller:
        owner = add_user(caller, 'owner', 'admin')
        number = start(caller, owner, 'context')
        run = wait(caller, owner, number, lambda row: row['state'] in ('completed', 'failed'), seconds=40)
        assert run['state'] == 'completed', run['reason']
        assert run['metrics']['max_context_tokens'] == 512
        failed = [sample for sample in run['samples'] if not sample['values']['success']]
        assert failed
        assert all(sample['values']['capacity_basis'] == 'measured_configured_ram'
                   and sample['values']['peak_ram_mib'] == 700 for sample in failed)
        assert caller.get('/loadcoach/status', headers=owner).json()['lock'] is None


@pytest.mark.parametrize('hybrid', [False, True])
def test_cpu_and_hybrid_text_cancel_releases_process_and_lock(fake_llama_server, fake_cards, native_moe, hybrid):
    settings = native_moe[0] if hybrid else cpu_model()
    settings['small']['flags']['benchmark-delay'] = 1
    with gateway(fake_llama_server, models=settings, nvidia_smi=fake_cards[0] if hybrid else 'no-such-program') as caller:
        owner = add_user(caller, 'owner', 'admin')
        number = start(caller, owner, 'speed')
        wait(caller, owner, number,
             lambda row: bool(caller.get('/loadcoach/status', headers=owner).json()['loaded']))
        response = caller.post('/v1/batches/' + number + '/cancel', headers=owner)
        assert response.status_code == 200
        # Cancellation acknowledgement can precede child cleanup; verify the physical release too.
        wait(caller, owner, number, lambda row: row['state'] == 'cancelled'
             and not caller.get('/loadcoach/status', headers=owner).json()['loaded']
             and caller.get('/loadcoach/status', headers=owner).json()['lock'] is None)
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert status['lock'] is None and status['loaded'] == []
    if hybrid:
        assert native_moe[2] and all(group.closed for group in native_moe[2])
