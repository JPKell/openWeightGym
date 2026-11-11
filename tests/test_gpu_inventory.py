"""Physical vendor discovery and engine allocations without real drivers or GPU execution."""

import asyncio
from pathlib import Path

import pytest

from loadcoach import devices
from loadcoach.devices import hardware
from loadcoach.config import Config, checked, load_config
from loadcoach.models import Models
from loadcoach.runner import Refusal, Runner, Server
from engines import engine_for
from test_app import fake_llama_server, gateway, in_an_empty_folder, model  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401


def pci(root, address, vendor, *, driver='', device_class='0x030000'):
    path = root / address
    path.mkdir(parents=True)
    for name, value in {'vendor': vendor, 'device': '0x1234', 'class': device_class}.items():
        (path / name).write_text(value)
    if driver:
        target = root / 'drivers' / driver
        target.mkdir(parents=True, exist_ok=True)
        (path / 'driver').symlink_to(target, target_is_directory=True)


def test_hardware_detects_all_vendors_without_any_engine(tmp_path, monkeypatch):
    pci(tmp_path, '0000:01:00.0', '0x10de', driver='nvidia')
    pci(tmp_path, '0000:07:00.0', '0x10de', driver='nvidia')
    pci(tmp_path, '0000:09:00.0', '0x1002', driver='amdgpu')
    pci(tmp_path, '0000:0a:00.0', '0x8086', driver='xe')
    pci(tmp_path, '0000:0b:00.0', '0x8086', device_class='0x020000')  # network card
    pci(tmp_path, '0000:0c:00.0', '0x8086', driver='intel_vpu', device_class='0x120000')  # NPU
    monkeypatch.setattr(devices, 'query', lambda command: '')
    found = hardware(pci_root=tmp_path)
    assert [one['vendor'] for one in found] == ['NVIDIA', 'NVIDIA', 'AMD', 'Intel']
    assert [one['id'] for one in found[:2]] == ['CUDA0', 'CUDA1']
    assert all(one['pci'] for one in found)
    monkeypatch.setattr(devices, 'hardware', lambda **kwargs: found)
    path = tmp_path / 'loadcoach.toml'
    path.write_text('[engines.llama_cpp]\nenabled = false\n')
    config = load_config(path)
    assert list(config.gpus) == [one['id'] for one in found]
    assert config.gpu_details == found


def test_headless_nvidia_is_merged_by_pci_and_uses_physical_uuid(tmp_path, monkeypatch):
    pci(tmp_path, '0000:01:00.0', '0x10de', driver='nvidia')
    monkeypatch.setattr(devices, 'query', lambda command:
        '1, 00000000:07:00.0, GPU B, GPU-b\n0, 00000000:01:00.0, GPU A, GPU-a\n')
    found = hardware(pci_root=tmp_path)
    assert len(found) == 2
    assert [one['id'] for one in found] == ['CUDA0', 'CUDA1']
    config = Config(gpus=('CUDA0', 'CUDA1'), gpu_details=found)
    around = devices.cuda_environment(config, ('CUDA1',))
    assert around['CUDA_VISIBLE_DEVICES'] == 'GPU-b'
    assert devices.cuda_environment(config, ())['CUDA_VISIBLE_DEVICES'] == ''
    launch = engine_for().launch(config, 12345, {'flags': {}}, ('CUDA1',))
    assert launch.command[launch.command.index('--device') + 1] == 'CUDA0'
    assert launch.environment['CUDA_VISIBLE_DEVICES'] == 'GPU-b'


def test_vendor_without_driver_is_visible_but_not_mistaken_for_cuda(tmp_path, monkeypatch):
    pci(tmp_path, '0000:02:00.0', '0x8086')
    pci(tmp_path, '0000:03:00.0', '0x1002')
    monkeypatch.setattr(devices, 'query', lambda command: '')
    found = hardware(pci_root=tmp_path)
    config = Config(gpus=tuple(one['id'] for one in found), gpu_details=found)
    assert {one['vendor'] for one in found} == {'AMD', 'Intel'}
    assert devices.allowed(config, 'sdcpp') == ()
    for card in config.gpus:
        assert devices.telemetry_index(config, card) is None
        with pytest.raises(ValueError, match='requires a CUDA GPU'):
            devices.cuda_environment(config, (card,))


def test_identified_integrated_device_is_excluded_without_hiding_unknown_discrete_cards(tmp_path, monkeypatch):
    pci(tmp_path, '0000:01:00.0', '0x1002')
    (tmp_path / '0000:01:00.0/device').write_text('0x164e')
    pci(tmp_path, '0000:02:00.0', '0x1002')
    monkeypatch.setattr(devices, 'query', lambda command: '')
    assert [card['pci'] for card in hardware(pci_root=tmp_path)] == ['0000:02:00.0']
    config = Config(gpus=('integrated', 'legacy'), gpu_details=[{'id': 'integrated', 'integrated': True}])
    assert Runner(config, None).cards == ('legacy',)
    assert devices.allowed(config, 'llama_cpp') == ('legacy',)


@pytest.mark.parametrize('engine_id', ['sdcpp', 'audiocpp'])
def test_native_children_only_see_the_assigned_card(engine_id, tmp_path, monkeypatch):
    import json
    executable = tmp_path / 'native'
    executable.write_bytes(b'fixture executable; never started')
    executable.chmod(0o700)
    engine = engine_for(engine_id)
    monkeypatch.setattr(engine, 'check_available', lambda *args: None)
    if engine_id == 'sdcpp':
        monkeypatch.setattr('mirrorwall.engine.build_identity', lambda options: 'fixture')
    config = Config(gpus=('CUDA0', 'CUDA1'),
        gpu_details=[{'id': 'CUDA0', 'vendor': 'NVIDIA', 'cuda_index': 0, 'uuid': 'GPU-a'},
                     {'id': 'CUDA1', 'vendor': 'NVIDIA', 'cuda_index': 1, 'uuid': 'GPU-b'}],
        engines={engine_id: {'enabled': True, 'executable': str(executable)}})
    settings = {'engine': engine_id, 'gpus': ['CUDA1'], 'flags': {'model': 'fixture'}, engine_id: {}}
    launch = engine.launch(config, 12345, settings, ('CUDA1',))
    try:
        assert launch.environment['CUDA_VISIBLE_DEVICES'] == 'GPU-b'
        assert json.loads(Path(launch.command[-1]).read_text())['cards'] == ['CUDA1']
        assert json.loads(Path(launch.command[-1]).read_text())['monitor_cards'] == ['GPU-b']
    finally:
        launch.cleanup()


def test_engine_reservations_apply_to_placement_split_and_start():
    config = Config(gpus=('CUDA0', 'CUDA1'), engine_gpus={'llama_cpp': ['CUDA0'], 'sdcpp': ['CUDA1'], 'audiocpp': []})
    runner = Runner(config, None, models=Models(':memory:'))
    assert runner.engine_cards('llama_cpp') == ('CUDA0',)
    assert runner.engine_cards('sdcpp') == ('CUDA1',)
    assert runner.engine_cards('audiocpp') == ()
    text = {'flags': {'model': 'mock.gguf', 'ctx-size': 512}, 'gpus': 'any', 'allow_split': True}
    assert len(runner.layouts(text)) == 1  # never retry on a GPU reserved for images
    image = {'engine': 'sdcpp', 'flags': {'model': 'mock.gguf'}, 'gpus': 1, 'sdcpp': {'split_mode': 'single'}}
    runner.hybrid_budget = lambda settings: 0
    assert runner.place('image', image, set())[0] == ('CUDA1',)
    pinned = {**image, 'gpus': ['CUDA0']}
    assert runner.place('image', pinned, set()) is None
    with pytest.raises(Refusal, match='reserved for another engine'):
        asyncio.run(runner.start(Server('image', ('CUDA0',), image)))
    config.disabled_gpus = ['CUDA1']
    assert runner.engine_cards('sdcpp') == ()


def test_assignments_follow_the_same_physical_card_after_renumbering():
    config = Config(gpus=('CUDA0', 'CUDA1'), engine_gpus={'llama_cpp': ['GPU-a'], 'sdcpp': ['GPU-b']},
        gpu_details=[{'id': 'CUDA0', 'vendor': 'NVIDIA', 'cuda_index': 0, 'uuid': 'GPU-b'},
                     {'id': 'CUDA1', 'vendor': 'NVIDIA', 'cuda_index': 1, 'uuid': 'GPU-a'}])
    runner = Runner(config, None, models=Models(':memory:'))
    assert runner.engine_cards('llama_cpp') == ('CUDA1',)
    assert runner.engine_cards('sdcpp') == ('CUDA0',)
    config.disabled_gpus = ['GPU-a']
    assert runner.cards == ('CUDA0',)
    assert runner.engine_cards('llama_cpp') == ()


@pytest.mark.parametrize('policy', [{'unknown': ['CUDA0']}, {'sdcpp': ['CUDA0', 'CUDA0']}, {'sdcpp': 'CUDA0'}])
def test_invalid_engine_policy_is_rejected(policy):
    with pytest.raises(SystemExit):
        checked(Config(engine_gpus=policy), Path('fixture'))


def test_reservations_unload_idle_models_reject_held_models_and_survive_restart(fake_llama_server, tmp_path):
    from test_app import add_user
    from loadcoach.settings import Changes
    with gateway(fake_llama_server, {'one': model('one.gguf', 1)}) as client:
        headers = add_user(client, 'admin', 'admin')
        assert client.post('/v1/chat/completions', json={'model': 'one', 'messages': []}, headers=headers).is_success
        occupied = client.get('/loadcoach/status', headers=headers).json()['loaded'][0]['gpus'][0]
        client.post('/loadcoach/hold', json={'model': 'one', 'seconds': 30}, headers=headers)
        policy = {'llama_cpp': [], 'sdcpp': [occupied]}
        assert client.post('/loadcoach/cards', json={'engine_gpus': policy}, headers=headers).status_code == 409
        client.delete('/loadcoach/hold/one', headers=headers)
        assert client.post('/loadcoach/cards', json={'engine_gpus': policy}, headers=headers).is_success
        assert client.get('/loadcoach/status', headers=headers).json()['loaded'] == []
        assert client.post('/v1/chat/completions', json={'model': 'one', 'messages': []}, headers=headers).status_code == 503
        assert client.get('/loadcoach/cards', headers=headers).json()['engine_gpus'] == policy
    path = tmp_path / 'restart.toml'
    path.write_text('gpus = ["GPU0", "GPU1"]\n')
    cfg = load_config(path)
    Changes(cfg.database).put('loadcoach', 'engine_gpus', policy, 'admin')
    assert load_config(path).engine_gpus == policy


def test_web_editors_only_offer_their_engines_cards(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    assert server.post('/compute-nodes/gpus', data={'machine': 'compute',
        'gpu-engine-present': ['llama_cpp', 'sdcpp', 'audiocpp'], 'gpu-engine-llama_cpp': ['A'],
        'gpu-engine-sdcpp': ['B'], 'gpu-engine-audiocpp': ['A']}).is_success
    assert server.get('/mirrorwall-models').json()['cards'] == ['B']
    assert server.get('/soundroom-models').json()['cards'] == ['A']
    shown = server.get('/compute-nodes').text
    assert 'Allow A for llama.cpp' in shown and 'Allow B for stable-diffusion.cpp' in shown
    result = server.post('/compute-nodes/gpus', data={'machine': 'compute',
        'gpu-engine-present': ['llama_cpp', 'sdcpp'], 'gpu-engine-llama_cpp': ['A'], 'gpu-engine-sdcpp': ['B']})
    assert result.is_success
    assert server.get('/mirrorwall-models').json()['cards'] == ['B']
    assert server.get('/soundroom-models').json()['cards'] == ['A']


def test_browser_saves_engine_assignments_and_image_editor_only_offers_its_card(desk):
    from test_console_browser import browser_context, playwright
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width': 390, 'height': 1000})
        page.goto('http://console.test/compute-nodes')
        page.get_by_role('checkbox', name='Allow B for llama.cpp', exact=True).uncheck()
        page.get_by_role('checkbox', name='Allow A for stable-diffusion.cpp', exact=True).uncheck()
        page.get_by_role('button', name='Save GPUs', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('GPU assignments apply')
        playwright.expect(page.get_by_role('checkbox', name='Allow B for llama.cpp', exact=True)).not_to_be_checked()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://console.test/models/images?new=1')
        playwright.expect(page.locator('[data-hardware-cards] [data-gpu-card]')).to_have_count(1)
        playwright.expect(page.locator('[data-hardware-cards] [data-gpu-card]')).to_have_value('B')
        browser.close()


def test_status_model_cards_exclude_unsupported_and_disabled_devices():
    config = Config(gpus=('CUDA0', 'CUDA1', 'AMD@0000:65:00.0'), disabled_gpus=['CUDA1'],
                    gpu_details=[{'id':'CUDA0','vendor':'NVIDIA','cuda_index':0},
                                 {'id':'CUDA1','vendor':'NVIDIA','cuda_index':1},
                                 {'id':'AMD@0000:65:00.0','vendor':'AMD'}])
    runner = Runner(config, None, models=Models(':memory:'))
    status = runner.status()
    assert status['model_cards'] == ['CUDA0']
    assert set(status['memory']) == set(config.gpus)  # hardware reporting remains complete
    config.engines = {'llama_cpp': {'enabled':False}}
    assert runner.status()['model_cards'] == []
