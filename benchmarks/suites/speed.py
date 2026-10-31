"""Speed benchmark: definition, measurement protocol and saved-sample summary."""

from ..scoring import medians

DEFINITION = {
    "id": "speed", "name": "Speed",
    "summary": ("Measure how quickly a loaded model reads input and writes output, and how long you wait for its first token. "
                "Test several prompt lengths with repeated measurements; model startup is recorded separately."),
    "formula": "generation_tokens_per_second", "score_unit": "tokens/s",
    "method": ("Test 512, 2,048 and 8,192 input tokens where the model's context has room for 256 output tokens. "
               "A matching completed Context result can limit the tested context to a size known to fit. "
               "For each size, start a fresh model process three times. Each attempt first processes the prompt "
               "and generates 16 warm-up tokens, then measures the same input with a 256-token output budget. "
               "Prompt caching is disabled for both requests, so the measured input is read again in full. "
               "Read tokens/s measures input processing; write tokens/s measures output generation. "
               "First-token time starts after loading and includes reading the prompt; request time also includes "
               "generating the answer. The headline score is the median write speed at the largest tested input "
               "up to 2,048 tokens, or the smallest available size if none qualify. All sizes, repetitions, "
               "token counts and load times remain in the sample details."),
    "kpis": [("prompt_tokens_per_second", "Read", "tokens/s"), ("generation_tokens_per_second", "Write", "tokens/s"),
             ("ttft_seconds", "First token", "s"), ("request_seconds", "Request time", "s"),
             ("peak_gpu_mib", "Peak GPU memory", "MiB"), ("input_tokens", "Input", "tokens"),
             ("output_tokens", "Output", "tokens"), ("repetitions", "Repetitions", "count")],
}

PROTOCOL = {'speed_inputs': [512, 2048, 8192], 'speed_outputs': 256, 'repetitions': 3, 'warmup_outputs': 16}


async def measure(worker, run: dict, ceiling: int) -> None:
    """Measure each supported input size after a separate warm-up, resuming missing samples."""
    settings = run['subject']['settings']
    context = min(int(settings['flags']['ctx-size']), ceiling)
    fits = [result for result in worker.store.listing() if result['model'] == run['model'] and result['fingerprint'] == run['fingerprint']
            and result['benchmark'] == 'context' and result['state'] == 'completed' and result['environment'] == run['environment']]
    if fits:
        context = min(context, int(fits[0]['metrics']['max_context_tokens']))
    sizes = [size for size in PROTOCOL['speed_inputs'] if size + PROTOCOL['speed_outputs'] < context]
    if not sizes:
        raise ValueError('Speed needs at least 769 context tokens for its smallest input and output.')
    for size in sizes:
        for repeat in range(PROTOCOL['repetitions']):
            await worker.probe(run['id'], f'speed-{size}-{repeat}', context, size, PROTOCOL['speed_outputs'], repeat,
                               warmup_outputs=PROTOCOL['warmup_outputs'])


def summarize(samples: list[dict]) -> dict:
    """Use medians at the largest tested input no greater than 2,048, or the smallest available."""
    good = [s['values'] for s in samples if s['values'].get('success')]
    if not good:
        return {}
    sizes = sorted({sample['input_tokens'] for sample in good})
    reference = max((size for size in sizes if size <= 2048), default=min(sizes))
    return medians([sample for sample in good if sample['input_tokens'] == reference])
