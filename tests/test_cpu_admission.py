"""CPU admission uses observed resident RAM and existing host/service limits."""
import asyncio
import time

import pytest

from loadcoach.config import Config, checked, model_places
from loadcoach.host_memory import memory_snapshot
from loadcoach.models import Models
from loadcoach.runner import Lock, Refusal, Runner, Server, ServiceRamCapacity, Ticket, server_environment
from test_app import ask, fake_llama_server, gateway, in_an_empty_folder, model


def runner_with_cpu(monkeypatch, free=1800):
    models = Models(':memory:')
    for name in ('first', 'next', 'gpu'):
        models.put(name, model(name + '.gguf', 1 if name == 'gpu' else 0))
    runner = Runner(Config(host_ram_headroom_mib=512), None, models=models)
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total': 8192, 'available': free, 'service_available': 2400,
        'service_limit': 4096, 'service_used': 1696})
    return runner


def test_cpu_fit_uses_measured_ram_and_live_service_gate(monkeypatch):
    runner = runner_with_cpu(monkeypatch)
    settings = runner.models.get('next')
    runner.models.resident_measure('next', settings, 1000)
    assert runner.fit('next', settings, set()) == (settings, (), [])
    runner.config.host_ram_headroom_mib = 800
    assert runner.fit('next', settings, set()) is None
    runner.config.host_ram_headroom_mib = 0
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total': 8192, 'available': 6000, 'service_available': 1000})
    assert runner.fit('next', settings, set()) == (settings, (), [])
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total': 8192, 'available': 6000, 'service_available': 0})
    assert runner.fit('next', settings, set()) is None


def test_cpu_eviction_cannot_take_gpu_active_held_or_locked_memory(monkeypatch):
    runner = runner_with_cpu(monkeypatch, free=900)
    settings = runner.models.get('next')
    runner.models.resident_measure('next', settings, 1000)
    old = Server('first', (), runner.models.get('first'), ready=True, rss_mib=1200)
    gpu = Server('gpu', ('GPU0',), runner.models.get('gpu'), ready=True, rss_mib=5000)
    runner.servers = {'first': old, 'gpu': gpu}
    assert runner.fit('next', settings, set()) == (settings, (), [old])
    call = Ticket('first', 'u', 'here', 'normal', server=old)
    runner.running.append(call)
    assert runner.fit('next', settings, set()) is None
    runner.release(call)
    runner.holds['first', 'voice'] = time.monotonic() + 60
    assert runner.fit('next', settings, set()) is None
    runner.holds.clear()
    runner.models.set_locked('first', True)
    assert runner.fit('next', settings, set()) is None
    runner.models.set_locked('first', False)
    old.ready = False
    assert runner.fit('next', settings, set()) is None


def test_host_headroom_does_not_reduce_the_independent_service_ceiling(monkeypatch):
    runner = runner_with_cpu(monkeypatch, free=2000)
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total':8192, 'available':2000, 'service_available':1000})
    room = runner.host_memory()
    assert room['free'] == 1488 and room['service_available'] == 1000


def test_process_rss_estimate_is_not_compared_to_positive_cgroup_room(monkeypatch):
    runner = runner_with_cpu(monkeypatch, free=7000)
    settings = runner.models.get('next')
    runner.models.resident_measure('next', settings, 1504)
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total': 8192, 'available': 7000, 'service_available': 200,
        'service_limit': 1500, 'service_used': 1300})
    assert runner.ram_required('next', settings) == 1719
    assert runner.host_memory()['free'] >= 1719
    assert runner.fit('next', settings, set()) == (settings, (), [])


def test_cpu_rechecks_room_after_replaced_children_exit(monkeypatch):
    runner = runner_with_cpu(monkeypatch, free=600)
    settings = runner.models.get('next')
    runner.models.resident_measure('next', settings, 1000)
    child = Server('next', (), settings)
    old = Server('first', (), runner.models.get('first'), ready=True, rss_mib=1500)
    runner.servers = {'next':child}
    ticket = Ticket('next', 'owner', 'here', 'normal')
    runner.waiting.append(ticket)
    stopped = []
    async def stop(server):
        stopped.append(server.model)
    async def unexpected_start(server):
        raise AssertionError('Native child must not start before the fresh RAM check')
    runner.stop, runner.start = stop, unexpected_start
    asyncio.run(runner.bring_up(child, [old]))
    assert stopped == ['first', 'next'] and ticket.error.status == 503
    assert ticket.error.retry_after == 10 and 'no longer fits' in str(ticket.error)
    assert not runner.servers


def test_ordinary_cpu_load_uses_positive_service_room_without_rss_comparison(monkeypatch):
    runner = runner_with_cpu(monkeypatch, free=6000)
    settings = runner.models.get('next')
    runner.models.resident_measure('next', settings, 1504)
    child = Server('next', (), settings)
    runner.servers = {'next': child}
    async def start(server):
        pass
    async def measure(server):
        server.rss_mib = 1504
    async def version(settings=None):
        return 'test'
    runner.start, runner.measure, runner.version = start, measure, version
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total':8192,'available':6000,'service_available':200,
        'service_limit':1500,'service_used':1300})
    asyncio.run(runner.bring_up(child, []))
    assert child.ready and child.rss_mib == 1504


def test_ordinary_cpu_load_rechecks_exhausted_service_room_before_start(monkeypatch):
    runner = runner_with_cpu(monkeypatch, free=6000)
    settings = runner.models.get('next')
    child = Server('next', (), settings)
    runner.servers = {'next': child}
    ticket = Ticket('next', 'owner', 'here', 'normal')
    runner.waiting.append(ticket)
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total':8192,'available':6000,'service_available':0,
        'service_limit':1500,'service_used':1500})
    async def start(server):
        raise AssertionError('Native child must not start at an exhausted service ceiling')
    async def stop(server):
        pass
    runner.start, runner.stop = start, stop
    asyncio.run(runner.bring_up(child, []))
    assert ticket.error.status == 503 and not runner.servers


def test_cpu_readiness_rejects_observed_rss_above_configured_limit(monkeypatch):
    runner = runner_with_cpu(monkeypatch, free=6000)
    runner.config.host_ram_limit_mib = 128
    settings = runner.models.get('next')
    runner.models.resident_measure('next', settings, 32)
    child = Server('next', (), settings)
    runner.servers = {'next':child}
    ticket = Ticket('next', 'owner', 'here', 'normal')
    runner.waiting.append(ticket)
    stopped = []
    async def start(server):
        pass
    async def measure(server):
        server.rss_mib = 300  # actual process and children exceed the admitted estimate
    async def stop(server):
        stopped.append(server.model)
    runner.start, runner.measure, runner.stop = start, measure, stop
    asyncio.run(runner.bring_up(child, []))
    assert ticket.error.status == 503 and ticket.error.retry_after == 10
    assert 'configured host RAM limit' in str(ticket.error)
    assert stopped == ['next'] and not runner.servers and not child.ready


def test_unmeasured_cpu_load_reserves_room_until_ready(monkeypatch):
    runner = runner_with_cpu(monkeypatch)
    runner.servers['first'] = Server('first', (), runner.models.get('first'))
    assert runner.host_memory()['free'] == 0
    assert runner.fit('next', runner.models.get('next'), set()) is None


def test_measured_ram_survives_restart_and_invalidates_with_files(tmp_path):
    path = tmp_path / 'model.gguf'
    path.write_bytes(b'first')
    db = str(tmp_path / 'models.db')
    settings = model(str(path), 0)
    models = Models(db)
    models.put('cpu', settings)
    assert models.resident_measure('cpu', settings, 300) == 300
    assert models.measurements('cpu', settings) == []
    assert models.resident_measure('cpu', settings, 250) == 300
    models.db.close()
    models = Models(db)
    assert models.resident_measure('cpu', settings) == 300
    models.measured('cpu', settings, None)
    assert models.resident_measure('cpu', settings) == 300
    path.write_bytes(b'changed bytes')
    assert models.resident_measure('cpu', settings) is None


def test_cpu_idle_timer_preserves_holds_locks_global_lock_and_gpu(monkeypatch):
    runner = runner_with_cpu(monkeypatch)
    runner.config.cpu_idle_minutes = .01
    old = Server('first', (), runner.models.get('first'), ready=True, idle_since=time.monotonic()-10)
    gpu = Server('gpu', ('GPU0',), runner.models.get('gpu'), ready=True, idle_since=old.idle_since)
    runner.servers = {'first': old, 'gpu': gpu}
    stopped = []
    async def stop(server):
        stopped.append(server.model)
    runner.stop = stop
    async def exercise():
        runner.hold('first', 'voice', 60)
        await runner.evict_idle_cpu()
        runner.unhold('first', 'voice')
        runner.models.set_locked('first', True)
        await runner.evict_idle_cpu()
        runner.models.set_locked('first', False)
        runner.lock = Lock('x', 'owner', 'here', 60, False, False, time.monotonic()+60)
        await runner.evict_idle_cpu()
        runner.lock = None
        assert stopped == []
        await runner.evict_idle_cpu()
    asyncio.run(exercise())
    assert stopped == ['first'] and list(runner.servers) == ['gpu']


def test_cgroup_ancestor_high_limit_and_available_host_ram(tmp_path):
    proc, cgroup = tmp_path/'proc', tmp_path/'cgroup'
    (proc/'self').mkdir(parents=True)
    child = cgroup/'parent/worker'
    child.mkdir(parents=True)
    (proc/'meminfo').write_text('MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n')
    (proc/'self/cgroup').write_text('0::/parent/worker\n')
    for folder, current, high, maximum in ((cgroup, 3000, 'max', 'max'),
            (child.parent, 2000, '2500', '3000'), (child, 500, '1500', '2200')):
        (folder/'memory.current').write_text(str(current*2**20))
        (folder/'memory.high').write_text('max' if high == 'max' else str(int(high)*2**20))
        (folder/'memory.max').write_text('max' if maximum == 'max' else str(int(maximum)*2**20))
    memory = memory_snapshot(proc, cgroup)
    assert memory == {'total':8192, 'available':4096, 'service_available':500,
                      'service_reclaimable_available':500,
                      'service_limit':2500, 'service_used':2000, 'service_scope':'ancestor'}
    (child.parent/'memory.current').write_text(str(1000*2**20))
    assert memory_snapshot(proc, cgroup)['service_scope'] == 'self'


@pytest.mark.parametrize('excluded', ['', 'file_mapped', 'file_dirty', 'file_writeback', 'shmem', 'unevictable', 'malformed'])
def test_service_cache_room_is_separate_conservative_and_bounded_by_every_ancestor(tmp_path, excluded):
    proc, cgroup = tmp_path/'proc', tmp_path/'cgroup'
    (proc/'self').mkdir(parents=True)
    worker = cgroup/'parent/worker'
    worker.mkdir(parents=True)
    (proc/'meminfo').write_text('MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n')
    (proc/'self/cgroup').write_text('0::/parent/worker\n')
    values = {'anon': 100, 'kernel': 100, 'file': 1800, 'inactive_file': 300, 'active_file': 1500,
              'file_mapped': 0, 'file_dirty': 0, 'file_writeback': 0, 'shmem': 0, 'unevictable': 0}
    if excluded in values:
        values[excluded] = 1800
    statistics = '\n'.join(f'{key} {value*2**20}' for key,value in values.items())
    if excluded == 'malformed': statistics = 'inactive_file 1887436800'
    for folder, limit in ((cgroup, 4000), (worker.parent, 2500), (worker, 3000)):
        (folder/'memory.current').write_text(str(2000*2**20))
        (folder/'memory.high').write_text(str(limit*2**20))
        (folder/'memory.max').write_text(str((limit+100)*2**20))
        (folder/'memory.stat').write_text(statistics)
    memory = memory_snapshot(proc, cgroup)
    assert memory['service_available'] == 500  # unchanged legacy/observed charge
    assert memory['service_reclaimable_available'] == (2300 if excluded == '' else 500)
    # The next ancestor may be tighter even if the worker has clean cache.
    (worker.parent/'memory.stat').write_text('')
    assert memory_snapshot(proc, cgroup)['service_reclaimable_available'] == 500


def test_only_isolated_cpu_probe_classifies_own_unit_service_exhaustion(monkeypatch):
    runner = runner_with_cpu(monkeypatch)
    server = Server('first', (), runner.models.get('first'), rss_mib=1100)
    memory = {'total':8192,'available':4096,'service_available':0,
              'service_limit':1500,'service_used':1500,'service_scope':'self'}
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda:memory)
    with pytest.raises(ServiceRamCapacity) as measured:
        runner.assert_cpu_memory(server, isolated_probe=True)
    assert (measured.value.used_mib, measured.value.limit_mib) == (1500, 1500)
    with pytest.raises(Refusal) as ordinary:
        runner.assert_cpu_memory(server)
    assert type(ordinary.value) is Refusal and ordinary.value.status == 503
    memory['service_scope'] = 'ancestor'
    with pytest.raises(Refusal) as ancestor:
        runner.assert_cpu_memory(server, isolated_probe=True)
    assert type(ancestor.value) is Refusal and ancestor.value.status == 503
    memory.update(service_scope='self',available=100)
    with pytest.raises(Refusal) as host:
        runner.assert_cpu_memory(server, isolated_probe=True)
    assert type(host.value) is Refusal and host.value.status == 503
    runner.assert_cpu_memory(Server('gpu', ('GPU0',), runner.models.get('gpu')), isolated_probe=True)


@pytest.mark.parametrize('changes', [{'host_ram_headroom_mib': -1}, {'host_ram_headroom_mib': True},
    {'cpu_idle_minutes': float('nan')}, {'cpu_idle_minutes': -1}, {'disable_ram_offload': 1}])
def test_invalid_cpu_admission_settings(changes):
    with pytest.raises(SystemExit):
        checked(Config(**changes), 'settings')


def test_configured_cpu_ram_limit_caps_node_admission(monkeypatch):
    runner = Runner(Config(host_ram_headroom_mib=0, host_ram_limit_mib=2048), None, models=Models(':memory:'))
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {
        'total': 16384, 'available': 8192, 'service_available': None,
        'service_limit': None, 'service_used': None})
    memory = runner.host_memory()
    assert memory['configured_limit'] == 2048 and memory['free'] == 2048


def test_gpu_ram_offload_policy_is_explicit_in_server_environment():
    assert server_environment(Config(disable_ram_offload=True))['GGML_CUDA_ENABLE_UNIFIED_MEMORY'] == '0'
    assert server_environment(Config(disable_ram_offload=False))['GGML_CUDA_ENABLE_UNIFIED_MEMORY'] == '1'


def test_models_folder_defines_standard_image_and_audio_places():
    assert model_places('/srv/models', {'old': '/elsewhere'}) == {
        'llm': '/srv/models/llm', 'adapters': '/srv/models/adapters',
        'projectors': '/srv/models/projectors', 'image': '/srv/models/image', 'audio': '/srv/models/audio', 'encoders':'/srv/models/encoders'}
    assert model_places('', {'fast': '/models'}) == {'fast': '/models'}
    assert model_places('/srv/cache', {}, '/mnt/library') == {
        'llm': '/srv/cache/llm', 'adapters': '/srv/cache/adapters', 'projectors': '/srv/cache/projectors',
        'storage_llm': '/mnt/library/llm', 'storage_adapters': '/mnt/library/adapters',
        'storage_projectors': '/mnt/library/projectors', 'image': '/srv/cache/image', 'storage_image': '/mnt/library/image',
        'audio': '/srv/cache/audio', 'storage_audio': '/mnt/library/audio',
        'encoders':'/srv/cache/encoders','storage_encoders':'/mnt/library/encoders'}
    with pytest.raises(ValueError, match='full path'):
        model_places('relative/models', {})
    with pytest.raises(ValueError, match='full path'):
        model_places('/srv/cache', {}, 'relative/library')


def test_cpu_process_ram_and_inventory_are_real(fake_llama_server):
    with gateway(fake_llama_server, {'cpu': model('cpu.gguf', 0)}, cpu_idle_minutes=.002) as client:
        answer = ask(client, 'cpu')
        assert answer.status_code == 200
        status = client.get('/loadcoach/status').json()
        server = status['loaded'][0]
        assert server['gpus'] == [] and server['rss_mib'] > 0 and server['rss_peak_mib'] >= server['rss_mib']
        assert status['cpu']['concurrency'] == 1 and status['system_memory']['headroom'] == 512
        until = time.monotonic()+4
        while time.monotonic() < until and client.get('/loadcoach/status').json()['loaded']:
            time.sleep(.05)
        assert client.get('/loadcoach/status').json()['loaded'] == []


def test_runtime_cpu_settings_report_the_applied_value_and_reset(fake_llama_server):
    with gateway(fake_llama_server, {'cpu':model('cpu.gguf',0)}) as client:
        def values(response):
            programs=response.json()['programs']
            return {one['name']:one for program in programs if program['program']=='loadcoach' for one in program['settings']}
        changed=client.post('/loadcoach/settings/loadcoach',json={'host_ram_headroom_mib':513,'cpu_idle_minutes':16})
        assert changed.status_code==200
        assert values(changed)['host_ram_headroom_mib']['value']==513
        assert values(changed)['cpu_idle_minutes']['value']==16
        assert client.get('/loadcoach/status').json()['system_memory']['headroom']==513
        restored=client.post('/loadcoach/settings/loadcoach',json={'host_ram_headroom_mib':None,'cpu_idle_minutes':None})
        assert values(restored)['host_ram_headroom_mib']['value']==512
        assert values(restored)['host_ram_headroom_mib']['changed'] is None
        assert values(restored)['cpu_idle_minutes']['value']==15
