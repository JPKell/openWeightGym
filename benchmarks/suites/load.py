"""Load benchmark: definition, measurement protocol and saved-sample summary."""

from ..scoring import medians

DEFINITION = {
    "id": "load", "name": "Load time",
    "summary": ("Measure model startup at different context capacities: from starting a fresh process until it is ready. "
                "This allocates context memory but does not fill it with a prompt or generate an answer."),
    "formula": "1 / load_seconds", "score_unit": "1/s",
    "method": ("Start and stop a fresh model process three times at the configured minimum, midpoint and maximum context, "
               "skipping duplicate sizes. Without an explicit minimum, use 2,048 tokens or the configured maximum if it is smaller. "
               "Time each attempt from process startup until the server reports ready, including loading the weights and "
               "allocating context memory. No test prompt is processed and no answer is generated: readiness at a large "
               "context is different from proving that the model can read a full prompt at that size. "
               "The reported load time is the median at the largest successfully loaded context; the default score is "
               "1 divided by that time, so a higher score means faster loading. Keep every attempt's duration and GPU memory or CPU resident RAM. "
               "The operating system's file cache is not cleared, so fresh processes do not necessarily read weights from disk each time."),
    "kpis": [("load_seconds", "Load time", "s"), ("context_tokens", "Context", "tokens"),
             ("peak_gpu_mib", "Peak GPU memory", "MiB"), ("peak_ram_mib", "Peak resident RAM", "MiB"),
             ("repetitions", "Repetitions", "count")],
}

PROTOCOL = {'repetitions': 3}


async def measure(worker, run: dict, ceiling: int) -> None:
    """Restart at minimum, midpoint and maximum context and record each readiness time."""
    settings = run['subject']['settings']
    # Models with no explicit minimum still get a useful context-size series.
    settings = dict(settings, min_context=settings.get('min_context', min(2048, settings['flags']['ctx-size'])))
    minimum, maximum = settings['min_context'], settings['flags']['ctx-size']
    for context in sorted({minimum, (minimum + maximum) // 2, maximum}):
        for repeat in range(PROTOCOL['repetitions']):
            await worker.probe(run['id'], f'load-{context}-{repeat}', context, 0, 0, repeat, capacity_is_result=True)
    if not any(s['values']['success'] for s in worker.store.samples(run['id'])):
        raise ValueError('The model could not load at any configured context size.')


def summarize(samples: list[dict]) -> dict:
    """Use median load time at the largest successful context, retaining other sizes as samples."""
    good = [s['values'] for s in samples if s['values'].get('success')]
    if not good:
        return {}
    reference = max(sample['context_tokens'] for sample in good)
    return medians([sample for sample in good if sample['context_tokens'] == reference])
