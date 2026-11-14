"""Choose recorded benchmark evidence without hiding it when model settings change."""

from loadcoach.log import LEGACY  # the old default node, which rows and catalogues without a node name mean


def metric_sections(benchmark):
    """Group every configured metric, preserving the registry's order and labels."""
    sections = []
    json_groups = {'accuracy': 'Free decoding', 'recorded_samples': 'Coverage',
                   'forced_accuracy': 'Forced decoding', 'matched_pairs': 'Matched pairs',
                   'free_output_tokens_per_second': 'Throughput'}
    label = 'Overall & coverage'
    for index, metric in enumerate(benchmark['kpis']):
        if benchmark['id'] == 'json_schema':
            label = json_groups.get(metric[0], label)
        elif index == 7:
            label = 'Detailed measurements'
        if not sections or sections[-1]['name'] != label:
            sections.append(dict(name=label, metrics=[]))
        sections[-1]['metrics'].append(metric)
    return sections


def results_workspace(models, measurement_models, benchmarks, benchmark, latest, query):
    """Present authorised saved scores without changing grading or the complete heatmap."""
    import math

    def scored(run):
        return bool(run) and type(run.get('score')) in (int, float) and math.isfinite(run['score'])

    def review(run, definition):
        return bool(run) and (run.get('current_fingerprint') is False or bool(run.get('rerun'))
                              or run.get('version', 1) != definition['version'])

    def compatible(run, definition):
        return scored(run) and run.get('version', 1) == definition['version']

    search = query.get('q', '').strip()[:200]
    evidence = query.get('evidence', 'all')
    if evidence not in ('all', 'current', 'review', 'gaps'):
        evidence = 'all'
    baseline = next((model['id'] for model in models if model['id'] == query.get('baseline')), '')
    rows = []
    for model in measurement_models:
        run = latest.get(model['id'], {}).get(benchmark['id'])
        if search.casefold() not in (model['id'] + ' ' + str(model.get('family', ''))).casefold():
            continue
        if (evidence == 'current' and (not scored(run) or review(run, benchmark))
                or evidence == 'review' and not review(run, benchmark)
                or evidence == 'gaps' and scored(run)):
            continue
        rows.append(model)
    def score_order(model):
        run = latest.get(model['id'], {}).get(benchmark['id'])
        return (not scored(run), run['score'] * (1 if benchmark.get('green') == 'low' else -1) if scored(run) else 0)
    rows.sort(key=score_order)

    def leaders(candidates, definition):
        ranked = [dict(model=model['id'], run=run) for model in candidates
                  if compatible(run := latest.get(model['id'], {}).get(definition['id']), definition)]
        ranked.sort(key=lambda row: row['run']['score'], reverse=definition.get('green') != 'low')
        base = latest.get(baseline, {}).get(definition['id'])
        prior, rank = None, 0
        for index, row in enumerate(ranked, 1):
            if row['run']['score'] != prior:
                rank = index
            prior = row['run']['score']
            row.update(rank=rank, delta=row['run']['score']-base['score'] if compatible(base, definition) else None,
                       bar=row['run']['score'] if definition.get('score_unit') == '%' and 0 <= row['run']['score'] <= 100 else None)
        return ranked

    pairs = [(latest.get(model['id'], {}).get(definition['id']), definition)
             for model in models for definition in benchmarks]
    count = sum(scored(run) for run, _ in pairs)
    ranked = leaders(rows, benchmark)
    reasoning = next((definition for definition in benchmarks if definition['id'] == 'reasoning'), None)
    specialists = leaders(rows, reasoning) if reasoning else []
    return dict(benchmark={**benchmark, 'metric_sections': metric_sections(benchmark)},
                models=rows, search=search, evidence=evidence, baseline=baseline,
                model_count=len(models), benchmark_count=len(benchmarks), scored=count, total=len(pairs),
                coverage=round(100*count/len(pairs)) if pairs else None,
                review=sum(review(run, definition) for run, definition in pairs),
                gaps=sum(not scored(latest.get(model['id'], {}).get(definition['id']))
                         for model in rows for definition in benchmarks),
                leaders=ranked, leader=ranked[0] if ranked else None,
                specialist=specialists[0] if specialists else None, reasoning=reasoning,
                baseline_scored=compatible(latest.get(baseline, {}).get(benchmark['id']), benchmark))


def run_briefing(detail, definition):
    """Present retained evidence; never recalculate the configured run score or grading."""
    import math
    from statistics import median

    def numeric(value):
        return type(value) in (int, float) and math.isfinite(value)

    plan = detail.get('plan') or {}
    protocol = detail.get('subject', {}).get('protocol', {})
    quality = bool(plan or definition.get('quality'))
    embedding = bool(protocol.get('embedding') or definition.get('embedding'))
    kind = detail['benchmark'].removeprefix('embedding_')
    samples, rows = detail.get('samples', []), []
    counts = dict(passed=0, failed=0, unsupported=0, unavailable=0, ungraded=0, executed=0)
    for index, sample in enumerate(samples, 1):
        values = sample['values']
        state = ('unsupported' if values.get('unsupported') else 'unavailable' if values.get('success') is not True
                 else 'failed' if quality and values.get('passed') is False
                 else 'ungraded' if quality and values.get('passed') is not True else 'passed')
        counts[state] += 1
        counts['executed'] += values.get('success') is True and not values.get('unsupported')
        evidence = sample.get('evidence') or {}
        resource = evidence.get('resource') or {}
        cpu_ram = values.get('peak_ram_mib')
        if cpu_ram is None:
            cpu_ram = resource.get('peak_ram_mib')
        budget = values.get('context_tokens') if kind == 'load' else values.get('target_input_tokens')
        if budget is None:
            budget = values.get('input_tokens') if quality or kind == 'speed' else values.get('context_tokens')
        rows.append(dict(index=index, id=sample['id'], state=state, category=values.get('category', ''),
                         budget=budget, cpu_ram=cpu_ram,
                         cpu=values.get('device') == 'cpu' or 'peak_ram_mib' in values or resource.get('device') == 'cpu'))
    has_cpu = any(row['cpu'] for row in rows)
    metric = ('checks' if quality else 'load_seconds' if kind == 'load' else
              'embedding_tokens_per_second' if embedding and kind == 'speed' else
              'generation_tokens_per_second' if kind == 'speed' else 'request_seconds')
    metric_label, metric_unit = {'checks': ('Correct checks', '%'), 'load_seconds': ('Median load time', 's'),
        'embedding_tokens_per_second': ('Median input throughput', 'tokens/s'),
        'generation_tokens_per_second': ('Median write speed', 'tokens/s'),
        'request_seconds': ('Median request time', 's')}[metric]
    groups = {}
    for sample, row in zip(samples, rows):
        if numeric(row['budget']) and row['budget'] >= 0:
            groups.setdefault(row['budget'], []).append(sample['values'])
    chart = []
    for budget, values in sorted(groups.items()):
        supported = [v for v in values if not v.get('unsupported')]
        if quality:
            checks = sum(v['checks'] for v in supported if numeric(v.get('checks')))
            correct = sum(v['correct'] for v in supported if numeric(v.get('correct')))
            value = 100 * correct / checks if checks else None
        else:
            measured = [v[metric] for v in supported if v.get('success') is True and numeric(v.get(metric))]
            value = median(measured) if measured else None
        chart.append(dict(budget=budget, value=value))
    summary = ("No samples recorded for this run." if not samples else
               "Partial evidence. No final score." if detail['state'] != 'completed' else
               "Every recorded case passed." if quality and counts['passed'] == len(samples) else
               "Saved results, with the evidence behind them.")
    metrics = detail.get('metrics', {})
    # Prefer the suite's saved summary; label a sample-median fallback explicitly.
    timing = ('load_seconds' if kind == 'load' else 'request_seconds' if embedding else 'ttft_seconds')
    timing_value = metrics.get(timing)
    timing_label = 'Load time' if timing == 'load_seconds' else 'Request time' if embedding else 'First token'
    timing_note = 'seconds · saved benchmark summary'
    if not numeric(timing_value):
        measured = [sample['values'][timing] for sample in samples
                    if sample['values'].get('success') is True and not sample['values'].get('unsupported')
                    and numeric(sample['values'].get(timing))]
        timing_value = median(measured) if measured else None
        timing_label = 'Median ' + timing_label.lower()
        timing_note = 'seconds · successful recorded attempts'
    chart_title = ('Measurements by context capacity' if kind == 'load' else 'Request time by tested context' if kind == 'context' else 'Performance across input sizes')
    chart_budget_label = 'Context capacity (tokens)' if kind in ('load', 'context') else 'Input budget (tokens)'
    return dict(name=definition.get('name', detail['benchmark']), unit=definition.get('score_unit', ''),
                method=definition.get('method', ''), quality=quality, embedding=embedding,
                rows=rows, counts=counts, cpu=has_cpu, planned=len(plan.get('cases', [])),
                chart=chart, chart_label=metric_label, chart_unit=metric_unit,
                chart_title=chart_title, chart_budget_label=chart_budget_label,
                chart_note='Field-check accuracy across supported attempts; unavailable checks retain the grader’s treatment.' if quality else
                           'Median successful measurement within each recorded budget. Missing measurements remain unavailable.',
                summary=summary, timing_value=timing_value, timing_label=timing_label, timing_note=timing_note)


def model_kind(model: dict) -> str:
    """Classify model metadata without confusing language-model vision with image generation."""
    capabilities = model.get('capabilities', {})
    if capabilities.get('classifier') or model.get('type') == 'classifier':
        return 'classifier'
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
    terminal = [run for run in terminal if (not node or (run.get('location') or LEGACY) == node)
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
        for place in model.get('locations') or [{'machine': LEGACY}]:
            name = place['machine']
            names.setdefault(name, None)
            local = {**model, **place, 'id': model['id']}
            if compatible(local, definition):
                candidates.setdefault(name, []).append((model, place, local))
    for run in runs:
        names.setdefault(run.get('location') or LEGACY, None)
    nodes = []
    for name in names:
        report = configured.get(name, {})
        service = name != 'front' or name in configured or name in available or name in candidates
        disabled = report.get('accepting_work') is False or report.get('lifecycle') in ('disabled', 'retired')
        known = name in available or ('available_machines' not in catalogue and name in candidates and name not in unavailable)
        state = 'disabled' if disabled else 'unavailable' if name in unavailable else 'ready' if known else 'unknown'
        if not service:
            state = 'front'
        active = [run for run in runs if (run.get('location') or LEGACY) == name and run['state'] == 'running']
        waiting = [run for run in runs if (run.get('location') or LEGACY) == name and run['state'] not in ('running', 'completed', 'failed', 'cancelled')]
        choices = []
        for model, place, local in candidates.get(name, []):
            result = location_latest.get((model['id'], name, definition['id']))
            if not model.get('locations') and name == LEGACY:
                result = result or latest.get(model['id'], {}).get(definition['id'])
            matching = next((run for run in runs if run['model'] == model['id'] and
                             (run.get('location') or LEGACY) == name and run['benchmark'] == definition['id']
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
    """Build a caller-scoped compute-node matrix with location-specific evidence."""
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

    # Run studio includes front's host; measurements require an inference service.
    nodes = [node for node in run_lanes(catalogue, inventory, models, selected, runs, location_latest, {}, False)
             if node['service']]
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
              'speed': 'embedding_tokens_per_second' if selected.get('embedding') else 'generation_tokens_per_second',
              'classifier_speed': 'decisions_per_second'}[measurement]
    value_unit = 'tokens' if measurement == 'context' else 's' if measurement == 'load' else 'decisions/s' if measurement == 'classifier_speed' else 'tokens/s'
    # Legacy evidence without a location belongs only to implicit compute, never to a named copy.
    legacy = latest_results([run for run in runs if not run.get('location')])
    attempts = {}
    for run in runs:
        if not run.get('retired'):
            attempts.setdefault((run['model'], run.get('location') or LEGACY, run['benchmark']), run)
    rows = []
    for model in models:
        placements = {place['machine']: {**model, **place} for place in model.get('locations') or [{'machine': LEGACY}]}
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
                    if not model.get('locations') and name == LEGACY:
                        saved = saved or legacy.get(model['id'], {}).get(bench['id'])
                    node['current'] += bool(saved and saved.get('current_fingerprint') and not saved.get('rerun')
                                            and (numeric(saved.get('score')) or any(numeric(value) for value in saved.get('metrics', {}).values())))
            result = location_latest.get((model['id'], name, selected['id']))
            if not model.get('locations') and name == LEGACY:
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
