"""Compare frozen quality cases, separating failures from missing/unavailable evidence."""

from .custom import digest


def compare(current, baseline):
    """Compare compatible runs while distinguishing missing and changed checks."""
    if current['benchmark'] != baseline['benchmark']:
        raise ValueError('Choose two runs of the same benchmark.')
    left, right = current['subject'].get('protocol', {}), baseline['subject'].get('protocol', {})
    if not current.get('plan') or not baseline.get('plan'):
        raise ValueError('Case comparisons require frozen quality case plans in both runs.')
    if left != right:
        raise ValueError('Dataset, protocol, implementation or grader versions differ; these runs cannot be compared case by case.')
    def cases(run):
        """Index retained samples by deterministic case identity."""
        samples = {sample['id']: sample for sample in run['samples']}
        return {case['id']: (case, samples.get(case['id'])) for case in run['plan']['cases']}
    after, before = cases(current), cases(baseline)
    rows = []
    for name in sorted(after.keys() | before.keys()):
        new_case, new = after.get(name, (None, None))
        old_case, old = before.get(name, (None, None))
        comparable = bool(new_case and old_case and digest({k:v for k,v in new_case.items() if k != 'context'}) ==
                          digest({k:v for k,v in old_case.items() if k != 'context'}))
        def state(sample):
            """Classify a case's retained success, failure or missing state."""
            values = (sample or {}).get('values', {})
            return 'missing' if sample is None else 'unsupported' if values.get('unsupported') else 'unavailable' if values.get('success') is not True else 'pass' if values.get('passed') is True else 'fail'
        now, was = state(new), state(old)
        status = ('new failure' if was == 'pass' and now == 'fail' else 'improved' if was == 'fail' and now == 'pass' else
                  'still passing' if now == 'pass' else 'still failing') if comparable and was in ('pass','fail') and now in ('pass','fail') else 'not comparable'
        def checks(sample):
            """Extract comparable check outcomes from one retained case."""
            return {f'{index + 1}: {check.get("field", "check")}':check.get('passed')
                    for index, check in enumerate((sample or {}).get('evidence', {}).get('checks', []))}
        old_checks, new_checks = checks(old), checks(new)
        rows.append({'case': name, 'baseline': was, 'current': now, 'status': status,
                     'newly_failing_checks': [key for key in old_checks if old_checks[key] is True and new_checks.get(key) is False] if status != 'not comparable' else [],
                     'reason': '' if comparable else 'Case content, answer budget or membership differs.'})
    warnings = []
    for key in ('engine_hash', 'machine_hash'):
        if not current['environment'].get(key) or not baseline['environment'].get(key):
            warnings.append(key.removesuffix('_hash') + ' identity is unavailable in one or both runs.')
        elif current['environment'].get(key) != baseline['environment'].get(key):
            warnings.append(key.removesuffix('_hash') + ' differs; this comparison does not isolate its effect.')
    if current['state'] != 'completed' or baseline['state'] != 'completed':
        warnings.append('One or both runs are incomplete; missing cases are not regressions.')
    return {'current':current['id'], 'baseline':baseline['id'], 'rows':rows, 'warnings':warnings,
            'counts':{status:sum(row['status'] == status for row in rows) for status in ('new failure','improved','still passing','still failing','not comparable')}}
