"""Embedding throughput and latency; no generated tokens or first-token metric."""
from ..scoring import medians

DEFINITION = {
    'id': 'embedding_speed', 'name': 'Embedder speed', 'embedding': True,
    'summary': 'Measure input tokens per second and request latency for one embedding at a time.',
    'formula': 'embedding_tokens_per_second', 'score_unit': 'tokens/s',
    'method': 'Test distinct input lengths of 128, 512, 2,048 and 8,192 tokens, capped by configured/trained '
              'context with 32 tokens of headroom and a matching completed Embedder context result. '
              'Retain the measured process context separately from maximum proven input. For each size, start a fresh '
              'isolated process three times, run one unscored embedding warm-up and measure a second '
              'complete embedding with prompt reuse disabled. Timing covers the local HTTP request '
              'through receiving the vector, excluding tokenization and model startup. Report input '
              'tokens/s, embeddings/s, latency, dimensions and GPU memory or CPU resident RAM. The headline is median input '
              'throughput at the largest tested size up to 512 tokens, or the smallest available size. '
              'This is serial single-input throughput, not batched throughput or retrieval quality.',
    'kpis': [('embedding_tokens_per_second', 'Input throughput', 'tokens/s'),
             ('embeddings_per_second', 'Embeddings', 'vectors/s'), ('request_seconds', 'Request time', 's'),
             ('input_tokens', 'Input', 'tokens'), ('embedding_dimensions', 'Vector dimensions', 'count'),
             ('peak_gpu_mib', 'Peak GPU memory', 'MiB'), ('peak_ram_mib', 'Peak resident RAM', 'MiB'),
             ('repetitions', 'Repetitions', 'count')],
}
PROTOCOL = {'version': 2, 'embedding': True, 'speed_inputs': [128, 512, 2048, 8192], 'reserve_tokens': 32,
            'repetitions': 3, 'warmup_embeddings': True, 'timing': 'local request including vector response'}


def input_sizes(context):
    """Return the bounded input token counts used for embedding speed measurements."""
    return sorted({min(size, context) for size in PROTOCOL['speed_inputs']})


async def measure(worker, run, ceiling):
    """Run the embedding input speed protocol through the existing benchmark probe."""
    context = min(int(run['subject']['settings']['flags']['ctx-size']), ceiling)
    input_limit = context - PROTOCOL['reserve_tokens']
    for saved in worker.store.listing():
        if (saved['model'] == run['model'] and saved['fingerprint'] == run['fingerprint']
            and saved['environment'] == run['environment'] and saved['benchmark'] == 'embedding_context'
            and saved['state'] == 'completed'):
            context = min(context, int(saved['metrics']['context_tokens']))
            input_limit = min(context - PROTOCOL['reserve_tokens'], int(saved['metrics']['max_context_tokens']))
            break
    for size in input_sizes(input_limit):
        for repeat in range(PROTOCOL['repetitions']):
            await worker.probe(run['id'], f'embedding-speed-{size}-{repeat}', context, size, 0, repeat,
                               warmup_embeddings=True)


def summarize(samples):
    """Summarize retained embedding input speed samples and supported scores."""
    good = [sample['values'] for sample in samples if sample['values'].get('success')]
    if not good:
        return {}
    sizes = sorted({sample['input_tokens'] for sample in good})
    reference = max((size for size in sizes if size <= 512), default=min(sizes))
    return medians([sample for sample in good if sample['input_tokens'] == reference])
