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
    pages = max(1, (len(terminal) + size - 1) // size)
    page = max(1, min(integer('page', 1), pages))
    return {'items': terminal[(page-1)*size:page*size], 'page': page, 'pages': pages, 'size': size, 'total': len(terminal)}
