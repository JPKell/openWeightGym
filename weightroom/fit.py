"""Node-local Dry fit data. Estimates never replace LoadCoach's live joint admission."""

from itertools import combinations
import math

from loadcoach.gpu_type import is_integrated


def workspace(known, status, fit_line, fit_cpu_mib):
    """Normalize the catalogue, physical pools and conservative measured reservations."""
    details = {one['id']: one for one in known.get('devices') or []}
    cards = [card for card in known.get('cards') or [] if not is_integrated(details.get(card, {'id': card}))]
    resources = []
    for card in cards:
        memory = (status.get('memory') or {}).get(card) or {}
        total = memory.get('total')
        resources.append({'id': card, 'name': card + ' · VRAM', 'total': total,
                          'budget': max(0, total - (memory.get('others') or 0) - (status.get('margin') or 0)) if total else None,
                          'other': memory.get('others') or 0, 'reserve': status.get('margin') or 0})
    ram = status.get('system_memory') or {}
    total = ram.get('total')
    headroom = ram.get('headroom') or 0
    loaded = sum(one.get('rss_mib') or 0 for one in status.get('loaded', []) if not one.get('gpus'))
    budget = None
    physical_room = None
    if total and ram.get('free') is not None:
        model_room = max(0, ram['free'] + loaded + (ram.get('hybrid_reserved') or 0))
        physical_room = min(max(0, total - headroom), max(0, ram['available'] - headroom + loaded) if ram.get('available') is not None else model_room)
        budget = min(physical_room, model_room)
        if ram.get('configured_limit') is not None:
            budget = min(budget, ram['configured_limit'])
        if ram.get('service_available') == 0:
            budget = 0
    # System RAM stays visible even when no CPU model is currently configured.
    resources.append({'id': '__cpu__', 'name': 'System RAM', 'total': total, 'budget': budget,
                      'other': max(0, total - headroom - physical_room) if total and physical_room is not None else None,
                      'policy_reserved': max(0, physical_room - budget) if physical_room is not None else 0,
                      'reserve': headroom, 'configured_limit': ram.get('configured_limit'),
                      'service_limit': ram.get('service_limit'), 'service_available': ram.get('service_available')})
    models = []
    for model in known.get('models', []):
        flags = {**(known.get('defaults') or {}), **model.get('flags', {})} if model.get('engine', 'llama_cpp') == 'llama_cpp' else model.get('flags', {})
        line = fit_line(model, cards)
        fixed = model.get('engine') in ('sdcpp', 'audiocpp', 'whispercpp')
        kind = 'rerank' if flags.get('reranking') else 'classifier' if model.get('capabilities', {}).get('classifier') else 'image' if model.get('engine') == 'sdcpp' else 'audio' if model.get('engine') in ('audiocpp', 'whispercpp') else 'llm'
        maximum = model.get('flags', {}).get('ctx-size')
        maximum = maximum if type(maximum) is int and maximum > 0 else 0
        minimum = min(maximum, max(1, model.get('min_context') or 1024)) if maximum else 0
        context = model.get('context_loaded') or maximum
        need = line['need']
        allowed = (known.get('engine_cards') or {}).get(model.get('engine', 'llama_cpp'), cards)
        eligible = [card for card in line['cards'] if card in cards and card in allowed]
        placements = [{'cards': list(group), 'label': ' + '.join(group)} for group in combinations(eligible, need)][:64] if type(need) is int and need > 0 else []
        if need == 0:
            placements = [{'cards': [], 'label': 'System RAM'}]
        original = model.get('cards') or (model.get('gpus') if isinstance(model.get('gpus'), list) else [])
        chosen = next((index for index, place in enumerate(placements) if place['cards'] == original), 0)
        rss = model.get('resident_measurements') or []
        if not rss:
            rss = [{'context': point['context'], 'rss_mib': point['rss_mib']} for point in model.get('measurements', []) if point.get('rss_mib') is not None]
        budget_ram = model.get('host_ram_budget_mib') or 0
        if budget_ram:
            ram_points = [[0, budget_ram]]
            ram_note = 'Fixed host RAM reservation'
        elif rss:
            ram_points = [[point['context'], math.ceil(point['rss_mib'] * 1.1) + 64] for point in rss]
            ram_note = 'Measured RSS plus admission growth margin' if need == 0 else 'Measured host RSS plus margin'
        elif need == 0:
            amount, _ = fit_cpu_mib(model)
            ram_points = [[0, amount]] if amount else []
            ram_note = 'Provisional fixed first-load bound; measure context growth'
        else:
            ram_points = []
            ram_note = 'Host allocation unmeasured'
        gpu_points = line['points']
        unknown = (not placements or (need > 0 and not gpu_points) or (need == 0 and not ram_points))
        # Unified-memory policy alone does not establish a measurable spill envelope.
        spill = bool(model.get('engine', 'llama_cpp') == 'llama_cpp' and (known.get('memory_policy') or {}).get('ram_offload') and need)
        if spill and not ram_points:
            unknown = True
        if fixed and kind == 'image' and not (model.get('image_profile') or {}).get('qualified'):
            unknown = True
        if not maximum and not fixed:
            unknown = True
        models.append({'id': model.get('model_id') or model['name'], 'name': model['name'],
                       'local_name': model.get('local_name', model['name']), 'engine': model.get('engine', 'llama_cpp'),
                       'kind': kind, 'profile': model.get('loadout_profile'), 'fixed': fixed,
                       'min': minimum, 'max': maximum, 'context': min(maximum, max(minimum, context)) if maximum else None,
                       'selected': model.get('loaded') == 'ready', 'locked': False, 'placement': chosen,
                       'placements': placements, 'points': gpu_points, 'ram_points': ram_points,
                       'unknown': unknown, 'guessed': line['guessed'], 'exclusive': bool(model.get('alone', model.get('engine') in ('audiocpp', 'whispercpp'))),
                       'ram_note': ram_note, 'spill': spill, 'enabled': model.get('enabled', True),
                       'profile_label': (model.get('sdcpp') or model.get('audiocpp') or model.get('whispercpp') or {}).get('family', '') if fixed else '',
                       'parallel': flags.get('parallel', 1),
                       'shared_context': bool(flags.get('kv-unified') and not flags.get('no-kv-unified'))})
    return {'models': models, 'resources': resources, 'ram_offload': bool((known.get('memory_policy') or {}).get('ram_offload')),
            'max_members': 16, 'node': status.get('machine')}
