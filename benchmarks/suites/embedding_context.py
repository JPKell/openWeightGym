"""Single-input embedding capacity, separate from text-generation context."""
from .context import next_context

DEFINITION = {
    'id': 'embedding_context', 'name': 'Embedder context', 'embedding': True,
    'summary': 'Find the largest complete token input that produces one embedding on the selected node.',
    'formula': 'max_context_tokens', 'score_unit': 'tokens',
    'method': 'Try the trained context first, or the configured limit when training metadata is absent. '
              'Start a fresh isolated process and embed one repeated-token passage leaving 32 tokens '
              'of context headroom for the native server boundary. '
              'No answer tokens or chat template are used. Require the server to report every input token '
              'and return one finite, nonzero pooled vector. After explicit context, batch-size or memory '
              'capacity failures, search smaller sizes at up to 1,024-token resolution (128 for small limits). '
              'Keep configured pooling and batch settings; a physical batch limit can restrict usable input. '
              'Record GPU memory on GPU nodes and measured resident RAM on CPU nodes. '
              'This measures capacity under those settings, not embedding quality. Model settings are unchanged.',
    'kpis': [('max_context_tokens', 'Maximum input', 'tokens'), ('input_tokens', 'Input processed', 'tokens'),
             ('embedding_dimensions', 'Vector dimensions', 'count'), ('peak_gpu_mib', 'Peak GPU memory', 'MiB'),
             ('peak_ram_mib', 'Peak resident RAM', 'MiB'),
             ('probes', 'Probes', 'count')],
}
PROTOCOL = {'version': 2, 'embedding': True, 'context_search': 'ceiling-first', 'reserve_tokens': 32}


async def measure(worker, run, ceiling):
    """Run the embedding context capacity protocol through the existing benchmark probe."""
    while (size := next_context(worker.store.samples(run['id']), ceiling)) is not None:
        await worker.probe(run['id'], f'embedding-context-{size}', size, size - PROTOCOL['reserve_tokens'],
                           0, capacity_is_result=True)
    if not any(sample['values']['success'] for sample in worker.store.samples(run['id'])):
        raise ValueError('No tested input produced a complete embedding. Check pooling and batch settings.')


def summarize(samples):
    """Summarize retained embedding context capacity samples and supported scores."""
    good = [sample['values'] for sample in samples if sample['values'].get('success')]
    if not good:
        return {}
    best = max(good, key=lambda sample: sample['input_tokens'])
    return {**best, 'max_context_tokens': best['input_tokens'], 'probes': len(samples)}
