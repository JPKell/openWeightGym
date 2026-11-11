"""Planning evidence and RAM/device pools without native inference or real devices."""

from weightroom.app import fit_line, fit_cpu_mib
from weightroom.fit import workspace
from loadcoach.models import Models, at_context
from test_weightroom import gguf


def render(known, status=None):
    return workspace(known, status or {}, fit_line, fit_cpu_mib)


def test_integrated_inventory_does_not_duplicate_system_ram():
    known = {'cards': ['AMD@0000:00:01.0'], 'devices': [
        {'id': 'AMD@0000:00:01.0', 'vendor': 'AMD', 'device_id': '0x164e'}],
        'models': [{'name': 'cpu', 'gpus': 0, 'flags': {'ctx-size': 8192}, 'size': 1024**3}]}
    data = render(known, {'system_memory': {'total': 32768, 'headroom': 4096, 'free': 16384}})
    assert [pool['name'] for pool in data['resources']] == ['System RAM']
    assert data['models'][0]['placements'] == [{'cards': [], 'label': 'System RAM'}]
    assert data['resources'][0]['budget'] == 16384


def test_unknown_discrete_memory_is_not_unlimited_and_gpu_nodes_always_show_ram():
    data = render({'cards': ['GPU0'], 'devices': [{'id': 'GPU0', 'vendor': 'AMD', 'description': 'Discrete GPU'}], 'models': []})
    assert [pool['id'] for pool in data['resources']] == ['GPU0', '__cpu__']
    assert all(pool['budget'] is None for pool in data['resources'])


def test_ram_budget_reconciles_existing_models_policy_and_service_exhaustion():
    status = {'system_memory': {'total': 32768, 'headroom': 2048, 'free': 8192,
                               'hybrid_reserved': 4096, 'configured_limit': 12000},
              'loaded': [{'gpus': [], 'rss_mib': 1024}, {'gpus': ['GPU0'], 'rss_mib': 5000}]}
    assert render({'cards': [], 'models': []}, status)['resources'][0]['budget'] == 12000
    status['system_memory']['service_available'] = 0
    assert render({'cards': [], 'models': []}, status)['resources'][0]['budget'] == 0
    pool = render({'cards': [], 'models': []}, {'system_memory': {'total': 65536, 'available': 51200,
                   'headroom': 8192, 'free': 10240, 'configured_limit': 10240}})['resources'][0]
    assert pool['other'] == 14336 and pool['policy_reserved'] == 32768


def test_fixed_and_hybrid_profiles_reserve_host_budget_and_unqualified_images_stay_unknown():
    model = {'name': 'picture', 'engine': 'sdcpp', 'gpus': ['GPU0'], 'flags': {'ctx-size': 0},
             'host_ram_budget_mib': 8192, 'measurements': [{'context': 0, 'mib': [2000]}], 'image_profile': {'qualified': True}}
    known = {'cards': ['GPU0'], 'models': [model]}
    one = render(known)['models'][0]
    assert one['fixed'] and one['ram_points'] == [[0, 8192]] and not one['unknown']
    model['image_profile']['qualified'] = False
    assert render(known)['models'][0]['unknown']
    model.update(engine='strata', flags={'ctx-size': 8192}, measurements=[{'context': 8192, 'mib': [2000]}])
    assert render(known)['models'][0]['ram_points'] == [[0, 8192]]


def test_ram_curves_include_resident_only_points_and_invalidate_with_profile_changes(tmp_path):
    library = Models(tmp_path / 'models.db')
    library.put('cpu', {'gpus': 0, 'flags': {'model': gguf(tmp_path / 'cpu.gguf'), 'ctx-size': 8192}})
    for context, rss in [(2048, 1000), (8192, 2000)]:
        library.resident_measure('cpu', at_context(library.get('cpu'), context), rss)
    points = library.resident_measurements('cpu')
    assert points == [{'context': 2048, 'rss_mib': 1000}, {'context': 8192, 'rss_mib': 2000}]
    data = render({'cards': [], 'models': [{'name': 'cpu', **library.get('cpu'), 'resident_measurements': points}]})
    assert data['models'][0]['ram_points'] == [[2048, 1164], [8192, 2264]]
    library.put('cpu', {**library.stored('cpu'), 'flags': {**library.stored('cpu')['flags'], 'threads': 4}})
    assert library.resident_measurements('cpu') == []


def test_unified_memory_needs_host_evidence_and_disabled_engine_cards_are_not_choices():
    model = {'name': 'language', 'engine': 'llama_cpp', 'gpus': 1, 'flags': {'ctx-size': 8192},
             'measurements': [{'context': 8192, 'mib': [2000]}]}
    known = {'cards': ['GPU0', 'GPU1'], 'engine_cards': {'llama_cpp': ['GPU1']},
             'memory_policy': {'ram_offload': True}, 'models': [model]}
    one = render(known)['models'][0]
    assert one['placements'] == [{'cards': ['GPU1'], 'label': 'GPU1'}]
    assert one['unknown'] and one['spill']
    model['resident_measurements'] = [{'context': 8192, 'rss_mib': 1000}]
    assert not render(known)['models'][0]['unknown']


def test_audio_default_exclusivity_matches_the_engine_contract():
    model = {'name': 'audio', 'engine': 'audiocpp', 'gpus': ['GPU0'], 'flags': {'ctx-size': 0},
             'host_ram_budget_mib': 1024, 'measurements': [{'context': 0, 'mib': [2000]}]}
    assert render({'cards': ['GPU0'], 'models': [model]})['models'][0]['exclusive']
    model['alone'] = False
    assert not render({'cards': ['GPU0'], 'models': [model]})['models'][0]['exclusive']
