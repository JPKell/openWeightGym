"""CPU text models use native probes with measured RAM and no GPU telemetry."""

from pathlib import Path

import pytest

from benchmark_clients import gateway
from loadcoach.probes import Probes
from test_app import add_user, fake_llama_server, in_an_empty_folder, model  # noqa: F401
from test_benchmarks import start, wait
from test_weightroom import gguf
import loadcoach.probes as probe_module


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


def test_cpu_text_cancel_releases_process_and_lock(fake_llama_server):
    settings = cpu_model()
    settings['small']['flags']['benchmark-delay'] = 1
    with gateway(fake_llama_server, models=settings) as caller:
        owner = add_user(caller, 'owner', 'admin')
        number = start(caller, owner, 'speed')
        wait(caller, owner, number,
             lambda row: bool(caller.get('/loadcoach/status', headers=owner).json()['loaded']))
        response = caller.post('/v1/batches/' + number + '/cancel', headers=owner)
        assert response.status_code == 200
        wait(caller, owner, number, lambda row: row['state'] == 'cancelled')
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert status['lock'] is None and status['loaded'] == []
