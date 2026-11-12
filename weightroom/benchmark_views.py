"""Choose recorded benchmark evidence without hiding it when model settings change."""


def model_kind(model: dict) -> str:
    """Classify model metadata without confusing language-model vision with image generation."""
    capabilities = model.get('capabilities', {})
    engine = model.get('engine_id') or model.get('engine')
    if engine == 'sdcpp' or capabilities.get('image_generation'):
        return 'image'
    if engine == 'audiocpp' or capabilities.get('audio'):
        return 'audio'
    return 'llm'


def latest_results(runs: list[dict]) -> dict[str, dict[str, dict]]:
    """Prefer the latest completed matching run, then the latest completed saved run.

    Input is newest first. Evidence retains its original fingerprint; a historical
    fallback is never relabelled as a measurement of the current configuration.
    """
    result = {}
    for run in runs:
        if run["state"] != "completed" or run.get('retired'):
            continue
        by_benchmark = result.setdefault(run["model"], {})
        prior = by_benchmark.get(run["benchmark"])
        if prior is None or run.get("current_fingerprint") and not prior.get("current_fingerprint"):
            by_benchmark[run["benchmark"]] = run
    return result


def comparison_ranks(columns, benchmarks, latest, selected):
    """Competition ranks use full precision and only the current benchmark protocol."""
    for column in columns:
        column['ranks'] = {}
    for bench in benchmarks:
        scores = [run['score'] for results in latest.values() if (run := results.get(bench['id']))
                  and run['score'] is not None and run['version'] == bench['version']]
        for column in columns:
            run = column['results'].get(bench['id'])
            if run and run['score'] is not None and run['version'] == bench['version']:
                column['ranks'][bench['id']] = 1 + sum(
                    score > run['score'] if bench.get('green') == 'high' else score < run['score'] for score in scores)
    available = {key for column in columns for key in column['ranks'] if key in selected}
    for column in columns:
        ranks = [rank for key, rank in column['ranks'].items() if key in selected]
        column['average_rank'] = sum(ranks) / len(ranks) if ranks else None
        column['rank_count'] = len(ranks)
        column['rank_missing'] = any(key not in column['ranks'] for key in available)


def history_page(runs, query):
    """Clamp navigation, never silently discard terminal history."""
    def integer(name, default):
        """Parse and clamp a benchmark history pagination value."""
        try:
            return int(query.get(name, default))
        except (TypeError, ValueError):
            return default
    size = integer('per_page', 25)
    if size not in (10, 25, 50, 100):
        size = 25
    terminal = [run for run in runs if run['state'] in ('completed', 'failed', 'cancelled')]
    all_total = len(terminal)
    search = query.get('q', '').strip()[:200]
    node = query.get('history_node', '')
    outcome = query.get('outcome', '')
    if outcome not in ('completed', 'failed', 'cancelled'):
        outcome = ''
    terminal = [run for run in terminal if (not node or (run.get('location') or 'compute') == node)
                and (not outcome or run['state'] == outcome)
                and (not search or search.casefold() in ' '.join(str(run.get(key) or '') for key in
                     ('id', 'model', 'benchmark', 'location', 'reason')).casefold())]
    pages = max(1, (len(terminal) + size - 1) // size)
    page = max(1, min(integer('page', 1), pages))
    return {'items': terminal[(page-1)*size:page*size], 'page': page, 'pages': pages, 'size': size, 'total': len(terminal),
            'all_total': all_total, 'search': search, 'node': node, 'outcome': outcome}


def run_lanes(catalogue, inventory, models, definition, runs, location_latest, latest, may_run):
    """Use configured nodes for visibility, and only the caller's catalogue for choices."""
    from benchmarks.eligibility import compatible

    configured = {item['name']: item for item in inventory.get('machines', [])}
    available = set(catalogue.get('available_machines', []))
    unavailable = set(catalogue.get('unavailable_machines', []))
    # Front's host is visible even when it has no registered inference service.
    names = dict.fromkeys(['front', *configured, *sorted(available | unavailable)])
    candidates = {}
    for model in models:
        for place in model.get('locations') or [{'machine': 'compute'}]:
            name = place['machine']
            names.setdefault(name, None)
            local = {**model, **place, 'id': model['id']}
            if compatible(local, definition):
                candidates.setdefault(name, []).append((model, place, local))
    for run in runs:
        names.setdefault(run.get('location') or 'compute', None)
    nodes = []
    for name in names:
        report = configured.get(name, {})
        service = name != 'front' or name in configured or name in available or name in candidates
        disabled = report.get('accepting_work') is False or report.get('lifecycle') in ('disabled', 'retired')
        known = name in available or ('available_machines' not in catalogue and name in candidates and name not in unavailable)
        state = 'disabled' if disabled else 'unavailable' if name in unavailable else 'ready' if known else 'unknown'
        if not service:
            state = 'front'
        active = [run for run in runs if (run.get('location') or 'compute') == name and run['state'] == 'running']
        waiting = [run for run in runs if (run.get('location') or 'compute') == name and run['state'] not in ('running', 'completed', 'failed', 'cancelled')]
        choices = []
        for model, place, local in candidates.get(name, []):
            result = location_latest.get((model['id'], name, definition['id']))
            if not model.get('locations') and name == 'compute':
                result = result or latest.get(model['id'], {}).get(definition['id'])
            matching = next((run for run in runs if run['model'] == model['id'] and
                             (run.get('location') or 'compute') == name and run['benchmark'] == definition['id']
                             and run['state'] not in ('completed', 'failed', 'cancelled')), None)
            reason = ('Node disabled' if disabled else 'Inventory unavailable' if state != 'ready' else
                      'Model disabled here' if not place.get('enabled', True) else
                      'Already queued or running' if matching else 'Benchmark disabled' if not definition['enabled'] else
                      'Benchmark ability required' if not may_run else '')
            choices.append({'id': model['id'], 'target': {'model': model['id'], 'machine': name}, 'result': result,
                            'reason': reason, 'active': matching,
                            'missing': not result or not result.get('current_fingerprint') or bool(result.get('rerun')),
                            'context': local.get('context_size'), 'engine': local.get('engine_id') or local.get('engine') or '',
                            'kind': 'embedding' if local.get('embedding') else model_kind(local)})
        nodes.append({'name': name, 'state': state, 'service': service, 'choices': choices,
                      'running': len(active), 'queued': len(waiting), 'hostname': report.get('hostname', ''),
                      'selectable': sum(not item['reason'] for item in choices)})
    return nodes


def run_model_rows(nodes):
    """Show each authorized model once, keeping its exact node targets and evidence."""
    rows = {}
    for node in nodes:
        for choice in node['choices']:
            row = rows.setdefault(choice['id'], {'id': choice['id'], 'kind': choice['kind'],
                                                'engines': [], 'targets': [], 'selectable': 0})
            if choice['engine'] and choice['engine'] not in row['engines']:
                row['engines'].append(choice['engine'])
            row['targets'].append({'node': node, 'choice': choice})
            row['selectable'] += not choice['reason']
    return list(rows.values())


def measurement_matrix(catalogue, inventory, models, benchmarks, selected, runs, location_latest):
    """Build a caller-scoped fleet matrix without borrowing evidence from another node."""
    import math
    from benchmarks.eligibility import compatible

    def numeric(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def formatted(value, score=False):
        if not numeric(value):
            return '—'
        if value == int(value):
            return f'{int(value):,}'
        if score:
            return f'{value:.6g}'
        return f'{value:,.2f}'.rstrip('0').rstrip('.') if abs(value) >= .01 else f'{value:.4g}'

    nodes = run_lanes(catalogue, inventory, models, selected, runs, location_latest, {}, False)
    reports = {node['name']: node for node in inventory.get('machines', [])}
    labels = {'ready': 'Ready', 'unknown': 'Status unknown', 'unavailable': 'Unreachable',
              'disabled': 'Disabled', 'front': 'Front host'}
    for node in nodes:
        report = reports.get(node['name'], {})
        cards = report.get('cards') or []
        total = sum(card.get('total') or 0 for card in cards)
        node.update(id=node['name'], state_label=labels[node['state']], models=0, current=0, total=0,
                    hardware=(f'{len(cards)} GPU' + ('s' if len(cards) != 1 else '') +
                              (f' · {total / 1024:g} GiB VRAM' if total else '')) if cards else
                             report.get('hostname') or ('Front host' if node['state'] == 'front' else 'Hardware not reported'))
    measurement = selected['id'].removeprefix('embedding_')
    metric = {'context': 'max_context_tokens', 'load': 'load_seconds',
              'speed': 'embedding_tokens_per_second' if selected.get('embedding') else 'generation_tokens_per_second'}[measurement]
    value_unit = 'tokens' if measurement == 'context' else 's' if measurement == 'load' else 'tokens/s'
    # Legacy evidence without a location belongs only to implicit compute, never to a named copy.
    legacy = latest_results([run for run in runs if not run.get('location')])
    attempts = {}
    for run in runs:
        if not run.get('retired'):
            attempts.setdefault((run['model'], run.get('location') or 'compute', run['benchmark']), run)
    rows = []
    for model in models:
        placements = {place['machine']: {**model, **place} for place in model.get('locations') or [{'machine': 'compute'}]}
        cells = []
        for node in nodes:
            name = node['name']
            local = placements.get(name)
            if local:
                node['models'] += 1
                for bench in benchmarks:
                    if not compatible(local, bench):
                        continue
                    node['total'] += 1
                    saved = location_latest.get((model['id'], name, bench['id']))
                    if not model.get('locations') and name == 'compute':
                        saved = saved or legacy.get(model['id'], {}).get(bench['id'])
                    node['current'] += bool(saved and saved.get('current_fingerprint') and not saved.get('rerun')
                                            and (numeric(saved.get('score')) or any(numeric(value) for value in saved.get('metrics', {}).values())))
            result = location_latest.get((model['id'], name, selected['id']))
            if not model.get('locations') and name == 'compute':
                result = result or legacy.get(model['id'], {}).get(selected['id'])
            attempt = attempts.get((model['id'], name, selected['id']))
            if model.get('locations') and attempt and not attempt.get('location'):
                attempt = None
            value = result.get('metrics', {}).get(metric) if result else None
            score = result.get('score') if result else None
            value, score = (value if numeric(value) else None), (score if numeric(score) else None)
            if result:
                state = ('unavailable' if value is None and score is None else 'stale' if not result.get('current_fingerprint')
                         else 'rerun' if result.get('rerun') else 'current')
            elif attempt and attempt['state'] in ('failed', 'cancelled'):
                state = attempt['state']
            elif attempt and attempt['state'] not in ('completed', 'failed', 'cancelled'):
                state = 'active'
            elif local and not compatible(local, selected):
                state = 'unsupported'
            elif local:
                state = 'missing'
            elif node['state'] in ('unavailable', 'unknown'):
                state = 'unknown'
            else:
                state = 'absent'
            note = {'current': 'Current', 'stale': 'Settings changed', 'rerun': 'Rerun recommended',
                    'unavailable': 'No measurement recorded', 'failed': 'Failed', 'cancelled': 'Cancelled',
                    'active': 'Queued or running', 'unsupported': 'Not applicable', 'missing': 'Not run',
                    'unknown': 'Inventory unavailable', 'absent': 'Not on node'}[state]
            cells.append(dict(node=name, state=state, note=note, result=result or attempt,
                              value=value, score=score, value_text=formatted(value), score_text=formatted(score, score=True),
                              unit=value_unit, score_unit=selected['score_unit'], best_value=False, best_score=False))
        for field, higher in (('value', measurement != 'load'), ('score', selected.get('green') != 'low')):
            current = [cell[field] for cell in cells if cell['state'] == 'current' and cell[field] is not None]
            if current:
                best = (max if higher else min)(current)
                for cell in cells:
                    cell['best_' + field] = cell['state'] == 'current' and cell[field] == best
        rows.append(dict(id=model['id'], engine=model.get('engine_id') or model.get('engine') or '',
                         quant=model.get('quant') or '', placements=len(placements), cells=cells))
    total, current = sum(node['total'] for node in nodes), sum(node['current'] for node in nodes)
    return dict(nodes=nodes, rows=rows, metric=metric, unit=value_unit, higher=measurement != 'load',
                score_higher=selected.get('green') != 'low', current=current, total=total,
                coverage=f'{round(100 * current / total)}%' if total else '—')
