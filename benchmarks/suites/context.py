"""Context benchmark: definition, measurement protocol and saved-sample summary."""

DEFINITION = {
    "id": "context", "name": "Context",
    "summary": ("Find the largest context the model can actually use on the available GPUs. "
                "Each attempt loads the model, reads a test prompt that nearly fills that context, "
                "and generates a short answer. Reading the full prompt can take much longer than loading the model."),
    "formula": "max_context_tokens", "score_unit": "tokens",
    "method": ("Wait for the GPUs to be available, then load a fresh model process at its trained context limit "
               "(or its configured context if no trained limit is recorded). Tokenize a fixed test passage and repeat "
               "its tokens to fill the chosen context minus 32 tokens reserved for the answer and bookkeeping. "
               "For example, a 131,072-token context is tested with 131,040 input tokens and a 16-token answer. "
               "Prompt caching is disabled: every input token must be processed from scratch. This reading phase, "
               "called prefill, builds the model's KV cache and can take minutes at large contexts. "
               "Load time measures startup separately; first-token time includes reading the prompt, and read tokens/s "
               "measures that processing speed. Stop the model after each attempt. If the largest context passes, finish; "
               "if it fails because of capacity, test smaller contexts and narrow the range between passing and failing sizes. "
               "Report the largest passing context, peak GPU memory, and a recommended context with 1,024 tokens of headroom. "
               "This checks capacity to process input and generate output, not answer quality or recall. "
               "The model's saved settings are unchanged."),
    "kpis": [("max_context_tokens", "Maximum context", "tokens"), ("recommended_context_tokens", "Recommended context", "tokens"),
             ("input_tokens", "Input processed", "tokens"), ("output_tokens", "Output generated", "tokens"),
             ("peak_gpu_mib", "Peak GPU memory", "MiB"), ("probes", "Probes", "count")],
}

PROTOCOL = {'context_output_tokens': 16, 'context_reserve_tokens': 32, 'context_search': 'ceiling-first'}


def next_context(samples: list[dict], ceiling: int) -> int | None:
    """Try the ceiling first; after a capacity failure, bisect at 1,024-token resolution."""
    step = min(1024, max(128, ceiling // 4))
    points = {int(s['values']['context_tokens']): bool(s['values']['success']) for s in samples}
    if not points:
        return ceiling
    low = max((size for size, ok in points.items() if ok), default=0)
    high = min((size for size, ok in points.items() if not ok), default=ceiling + step)
    if low == ceiling or (low and high - low <= step) or high <= 128:
        return None
    probe = min(ceiling, max(128, ((low + high) // 2 // step) * step))
    if probe in points:
        return 128 if not low and 128 not in points else None
    return probe


async def measure(worker, run: dict, ceiling: int) -> None:
    """Fill successively chosen context sizes and retain only finished probes."""
    number = run['id']
    while (context := next_context(worker.store.samples(number), ceiling)) is not None:
        await worker.probe(number, f'context-{context}', context, context - PROTOCOL['context_reserve_tokens'],
                           PROTOCOL['context_output_tokens'], capacity_is_result=True)
    if not any(s['values']['success'] for s in worker.store.samples(number)):
        raise ValueError('No tested context successfully processed a prompt and generated an answer.')


def summarize(samples: list[dict]) -> dict:
    """Report the largest passing context and a separate recommendation with headroom."""
    good = [s['values'] for s in samples if s['values'].get('success')]
    if not good:
        return {}
    best = max(good, key=lambda sample: sample['context_tokens'])
    return {**best, 'max_context_tokens': best['context_tokens'],
            'recommended_context_tokens': max(128, best['context_tokens'] - 1024), 'probes': len(samples)}
