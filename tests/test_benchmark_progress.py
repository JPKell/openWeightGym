"""Progress distinguishes fixed plans, adaptive probes and uncertain completion times."""

from benchmarks.progress import progress


def run(kind='load', **changes):
    return {'benchmark': kind, 'state': 'running', 'started': 100, 'ended': None,
            'subject': {'settings': {'flags': {'ctx-size': 2048}}},
            'samples': [{'at': 120, 'values': {'context_tokens': 2048, 'success': True,
                                             'load_seconds': 10, 'request_seconds': 2}}],
            'messages': [], 'plan': None, **changes}


def test_fixed_progress_deduplicates_contexts_and_estimates_remaining_work():
    result = progress(run(), 150)
    assert result['completed'] == 1 and result['total'] == 3
    assert result['elapsed_seconds'] == 50 and result['eta_seconds'] == 24
    assert 33 < result['percent'] < 34
    ongoing = run(messages=[{'at': 140, 'text': 'load-2048-1: requesting an isolated probe'}])
    assert progress(ongoing, 150)['eta_seconds'] == 14
    assert progress(ongoing, 180)['eta_overdue']


def test_waiting_paused_completed_and_unmeasured_times_are_explicit():
    assert progress(run(samples=[]), 150)['eta_seconds'] is None
    assert progress(run(state='paused'), 150)['eta_seconds'] is None
    assert progress(run(started=None), 150)['elapsed_seconds'] == 0
    done = progress(run(state='completed', ended=130), 150)
    assert done['percent'] == 100 and done['eta_seconds'] == 0 and done['elapsed_seconds'] == 30
    failed = progress(run(state='failed', ended=125), 150)
    assert failed['percent'] < 100 and failed['eta_seconds'] is None


def test_frozen_quality_plan_and_effective_speed_context_determine_totals():
    quality = progress(run('extraction', plan={'cases': [{}] * 9}), 150)
    assert quality['total'] == 9 and quality['remaining_samples'] == 8
    # Effective context from the sample excludes the larger speed input sizes.
    speed = progress(run('speed'), 150)
    assert speed['total'] == 3
    assert progress(run('speed', samples=[]), 150)['total'] is None


def test_context_search_has_no_false_fixed_percentage():
    adaptive = run('context', samples=[{'at': 120, 'values': {
        'context_tokens': 2048, 'success': False, 'load_seconds': 10}}])
    result = progress(adaptive, 150)
    assert result['adaptive'] and result['percent'] is None and result['total'] is None
    assert result['remaining_samples'] > 0 and result['eta_seconds'] > 0
    first_pass = progress(run('context', state='completed', ended=120), 150)
    assert first_pass['remaining_samples'] == 0 and first_pass['percent'] == 100
