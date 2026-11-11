"""Integrated graphics cannot enter discovery, placements, APIs, telemetry or setup."""

import asyncio
import ctypes
from pathlib import Path
import struct
import subprocess

import pytest

from loadcoach import devices, gpu_type
from loadcoach.devices import hardware
from loadcoach.config import Config, load_config
from loadcoach.models import Models
from loadcoach.runner import Runner
from test_gpu_inventory import pci
from test_app import fake_llama_server  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
IGPU = 'AMD@0000:0e:00.0'


@pytest.mark.parametrize('driver,integrated', [('amdgpu', True), ('amdgpu', False), ('xe', True), ('xe', False), ('i915', True), ('i915', False)])
def test_driver_classification_uses_topology_flags_not_vram_size(monkeypatch, driver, integrated):
    def ioctl(fd, number, request, direction=3):
        if driver == 'amdgpu':
            assert number == 0x45 and direction == 1
            pointer, size, query = struct.unpack_from('=QII', request)
            assert query == 0x16 and size == 144
            ctypes.memmove(pointer + 136, struct.pack('=Q', int(integrated)), 8)
        elif driver == 'xe':
            assert number == 0x40
            size = struct.unpack_from('=I', request, 12)[0]
            if not size:
                struct.pack_into('=I', request, 12, 24)
            else:
                pointer = struct.unpack_from('=Q', request, 16)[0]
                ctypes.memmove(pointer, struct.pack('=IIQQ', 2, 0, 0, int(not integrated)), 24)
        else:
            assert number == 0x79
            item = struct.unpack_from('=Q', request, 8)[0]
            size = ctypes.c_int32.from_address(item + 8)
            if not size.value:
                size.value = 104
            else:
                pointer = ctypes.c_uint64.from_address(item + 16).value
                result = bytearray(104)
                struct.pack_into('=I', result, 0, 1)
                struct.pack_into('=H', result, 16, int(not integrated))
                ctypes.memmove(pointer, bytes(result), len(result))
    monkeypatch.setattr(gpu_type, '_ioctl', ioctl)
    assert gpu_type._driver_integrated(7, driver) is integrated


@pytest.mark.parametrize('device,integrated', [
    ({'vendor': 'AMD', 'device_id': '0x164e'}, True),
    ({'vendor': 0x1002, 'device_id': 0x164e}, True),
    ({'description': 'AMD Raphael [Radeon 610M]'}, True),
    ({'description': 'Intel UHD Graphics 770'}, True),
    ({'description': 'Intel Iris Xe Graphics'}, True),
    ({'description': 'Intel Iris Xe MAX Graphics'}, False),
    ({'description': 'AMD Radeon RX 7600M'}, False),
    ({'description': 'AMD Radeon PRO W7900'}, False),
    ({'description': 'Intel Arc A770'}, False),
    ({'description': 'AMD GPU (0x164e)'}, True),
    ({'description': 'NVIDIA RTX 6000 Ada'}, False),
    ({'integrated': False, 'description': 'Integrated device label'}, False),
])
def test_positive_metadata_preserves_discrete_gpus(device, integrated):
    assert gpu_type.is_integrated(device) is integrated


def test_discovery_omits_integrated_graphics_without_renumbering_nvidia(tmp_path, monkeypatch):
    pci(tmp_path, '0000:01:00.0', '0x10de', driver='nvidia')
    pci(tmp_path, '0000:07:00.0', '0x10de', driver='nvidia')
    pci(tmp_path, '0000:0e:00.0', '0x1002', driver='amdgpu')
    (tmp_path / '0000:0e:00.0/device').write_text('0x164e')
    (tmp_path / '0000:0e:00.0/mem_info_vram_total').write_text(str(512 * 2**20))
    monkeypatch.setattr(devices, 'query', lambda command: '')
    assert [gpu['id'] for gpu in hardware(pci_root=tmp_path)] == ['CUDA0', 'CUDA1']
    assert len(hardware(pci_root=tmp_path, include_integrated=True)) == 3


def test_alias_fallback_cannot_resurrect_an_integrated_only_machine(monkeypatch):
    monkeypatch.setattr(devices, 'hardware', lambda **kwargs: [
        {'id': IGPU, 'pci': '0000:0e:00.0', 'vendor': 'AMD', 'integrated': True}])
    monkeypatch.setattr('engines.llama_cpp.devices.detect', lambda server, **kwargs: [
        {'id': 'Vulkan0', 'description': 'Radeon 610M (0000:0e:00.0)'}])
    assert devices.discover('mock-server') == []
    monkeypatch.setattr(devices, 'hardware', lambda **kwargs: [])
    assert devices.discover('mock-server') == []


def test_legacy_device_list_filters_integrated_without_changing_backend_ordinals(monkeypatch):
    from engines.llama_cpp import devices as llama_devices
    monkeypatch.setattr(llama_devices.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout='  Vulkan0: Intel UHD Graphics 770\n  Vulkan1: Intel Arc A770\n  CUDA0: NVIDIA RTX 6000\n'))
    assert [device['id'] for device in llama_devices.detect('mock-server')] == ['Vulkan1', 'CUDA0']


@pytest.mark.parametrize('automatic', [True, False])
def test_saved_inventory_and_policies_cannot_reenable_integrated_gpus(tmp_path, monkeypatch, automatic):
    found = [{'id': 'CUDA0', 'vendor': 'NVIDIA', 'pci': '0000:01:00.0', 'cuda_index': 0},
             {'id': IGPU, 'vendor': 'AMD', 'pci': '0000:0e:00.0', 'integrated': True, 'llama_device': 'Vulkan0'}]
    monkeypatch.setattr('loadcoach.config.discover', lambda *args, **kwargs: found)
    path = tmp_path / 'loadcoach.toml'
    text = ('' if automatic else f'gpus = ["CUDA0", "{IGPU}", "Vulkan0"]\n') + f'disabled_gpus = ["{IGPU}"]\n[engine_gpus]\nllama_cpp = ["{IGPU}"]\n'
    path.write_text(text)
    config = load_config(path)
    assert config.gpus == ('CUDA0',)
    assert config.gpu_details == found[:1]
    assert config.disabled_gpus == [] and config.engine_gpus == {'llama_cpp': []}
    assert path.read_text() == text


def test_runtime_inventory_and_launches_exclude_integrated_devices():
    config = Config(gpus=('CUDA0', IGPU), gpu_details=[
        {'id': 'CUDA0', 'vendor': 'NVIDIA', 'cuda_index': 0},
        {'id': IGPU, 'vendor': 'AMD', 'integrated': True, 'llama_device': 'Vulkan0'}])
    runner = Runner(config, None, models=Models(':memory:'))
    assert runner.cards == ('CUDA0',)
    assert runner.engine_cards('llama_cpp') == ('CUDA0',)
    assert list(runner.status()['memory']) == ['CUDA0']
    with pytest.raises(ValueError, match='integrated graphics'):
        devices.cuda_environment(config, (IGPU,))


def test_worker_apis_and_all_engine_choices_share_the_filtered_inventory(tmp_path, fake_llama_server):
    from front_helpers import front_of, machine_key
    from loadcoach.app import build_app
    from loadcoach.users import Users
    from test_app import GROUPS, add_user
    config = Config(gpus=('CUDA0', IGPU), nvidia_smi='no-such-program', llama_server=fake_llama_server,
                    machine_key_file=machine_key(tmp_path),
                    gpu_details=[{'id': 'CUDA0', 'vendor': 'NVIDIA', 'cuda_index': 0},
                                 {'id': IGPU, 'vendor': 'AMD', 'integrated': True}],
                    disabled_gpus=[IGPU], engine_gpus={'llama_cpp': ['CUDA0', IGPU]})
    with front_of(lambda front: build_app(config, front=front), Users(':memory:', GROUPS), tmp_path) as client:
        headers = add_user(client, 'admin', 'admin')
        for path in ('/loadcoach/status', '/loadcoach/models', '/loadcoach/cards'):
            response = client.get(path, headers=headers)
            assert response.is_success, (path, response.text)
            assert IGPU not in response.text, path
            cards = response.json()['cards']
            if path == '/loadcoach/cards':
                cards = [card['id'] for card in cards]
            assert cards == ['CUDA0']
        report = client.get('/loadcoach/models', headers=headers).json()
        assert all(IGPU not in cards for cards in report['engine_cards'].values())
        settings = client.get('/loadcoach/settings', headers=headers).json()
        rows = next(program['settings'] for program in settings['programs'] if program['program'] == 'loadcoach')
        assert next(row['value'] for row in rows if row['name'] == 'gpus') == ['CUDA0']


def test_telemetry_and_retained_history_cannot_restore_integrated_gpu_tiles():
    from weightroom.app import chart
    from weightroom.telemetry_workspace import fleet_summary, history_groups, hosts_view
    source = {'fingerprint': 'a' * 64, 'hostname': 'node', 'machines': ['compute'], 'state': 'ready', 'age': 1,
              'telemetry': {'cards': [{'card': 0, 'model': 'RTX 5060 Ti', 'memory_total': 16 * 2**30},
                                      {'card': 2, 'model': 'Radeon 610M', 'memory_total': 512 * 2**20}],
                            'processor': {}, 'memory': {}, 'disks': []}}
    host = hosts_view([source], 10)[0]
    assert [gpu['card'] for gpu in host['gpus']] == [0]
    assert fleet_summary([host])['gpus'] == 1
    groups = history_groups(host, {'series': {'GPU 0 load': [10], 'GPU 2 load': [50]}}, chart)
    assert [group['id'] for group in groups] == ['processor', 'gpu-0']


def test_monitor_filters_integrated_readings_before_caching_them():
    from turnstile.machines import Machines
    async def status(name, path):
        return {'data': []} if path == '/v1/models' else {'machine_fingerprint': 'a' * 64, 'memory': {}, 'loaded': []}
    async def telemetry(name, path):
        return {'machine_fingerprint': 'a' * 64, 'cards': [{'card': 0, 'model': 'RTX 6000'},
                                                       {'card': 2, 'integrated': True}]}
    machines = Machines(['compute'], status, ask_telemetry=telemetry)
    asyncio.run(machines.poll())
    snapshot = machines.hosts()[0]['telemetry']
    assert snapshot['cards'] == [{'card': 0, 'model': 'RTX 6000'}]
    assert snapshot['excluded_gpu_ids'] == [2]


def test_installer_does_not_select_an_integrated_only_machine(tmp_path):
    pci(tmp_path / 'pci', '0000:0e:00.0', '0x1002')
    (tmp_path / 'pci/0000:0e:00.0/device').write_text('0x164e')
    drm = tmp_path / 'drm/card0'
    drm.mkdir(parents=True)
    (drm / 'device').symlink_to(tmp_path / 'pci/0000:0e:00.0', target_is_directory=True)
    # No access to this workstation's driver tools; the real helper reads only fake sysfs.
    code = '''source "$1"
nvidia-smi() { return 1; }
lspci() { return 1; }
detect_gpu "$2"
printf '%s %s %s' "$GPU_KIND" "$GPU_COUNT" "$LLAMA_BACKEND"
'''
    result = subprocess.run(['/bin/bash', '-c', code, 'test', str(ROOT / 'scripts/lib/detect.sh'), str(tmp_path / 'drm')],
                            capture_output=True, text=True, check=True)
    assert result.stdout == 'none 0 cpu'
