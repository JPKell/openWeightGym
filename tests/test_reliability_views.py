"""Reliability presentation keeps recorded scores, scopes and safe exports intact."""

import csv
import io

from weightroom.reliability_views import workspace, export_csv


def candidate(model='small', **changes):
    return dict(model=model, role='chat', fingerprint='fp', current=True,
                counts=dict(success=8, error=1, timeout=1, empty=0), success_rate=80,
                factor=.9, state='eligible', regression=False, positive=0, negative=0, **changes)


def test_summary_is_weighted_by_sample_count_and_preserves_authoritative_scores():
    other = candidate('large')
    other.update(counts=dict(success=1, error=0, timeout=0, empty=0), success_rate=100, factor=1.1)
    report = dict(rows=[candidate(), other])
    view = workspace(report, [], {})
    assert view['summary']['success_rate'] == 100 * 9 / 11
    assert [row['factor'] for row in view['rows']] == [.9, 1.1]
    assert view['summary']['faults'] == 2
    assert workspace(report, [], {'q': 'missing'})['summary']['success_rate'] is None


def test_offline_keeps_saved_fingerprints_and_marks_shared_state_unknown():
    row = candidate()
    row.update(current=False, state='recovery required', regression=True)
    view = workspace(dict(rows=[row], compute_unavailable=True), [], {})
    assert len(view['attention']) == len(view['rows']) == 1
    assert view['summary']['excluded'] is None and view['summary']['regressions'] is None
    exported = list(csv.DictReader(io.StringIO(export_csv(view['rows'], offline=True))))[0]
    assert exported['factor'] == 'unavailable' and exported['current'] == 'unknown'


def test_csv_quotes_evidence_and_neutralizes_formulas():
    row = candidate('=HYPERLINK("https://invalid.example")')
    row.update(reason='  +injected formula', ignored={'cancelled': 1}, last_request=None)
    exported = list(csv.DictReader(io.StringIO(export_csv([row]))))[0]
    assert exported['model'].startswith("'=") and exported['reason'].startswith("'  +")
    assert exported['last_request'] == '' and exported['ignored'] == '{"cancelled": 1}'


def test_issue_requests_filter_by_exact_candidate_and_outcome_with_stable_paging():
    from urllib.parse import parse_qs, urlsplit
    first, second = candidate(), candidate()
    second['role'] = 'tools'
    first['issue_requests'] = [dict(request=n, at=1000, outcome='error', recovery=False) for n in range(1, 51)]
    second['issue_requests'] = [dict(request=n, at=1000, outcome='timeout', recovery=False) for n in range(51, 101)]
    report = dict(rows=[first, second])
    view = workspace(report, [], dict(model='small'))
    issues = view['issue_view']
    assert issues['total'] == 100 and issues['rows'][0]['request'] == 100
    assert issues['rows'][-1]['request'] == 51 and issues['next']
    next_query = {key: value[0] for key, value in parse_qs(urlsplit(issues['next']).query).items()}
    page_two = workspace(report, [], next_query)['issue_view']
    assert page_two['page'] == 2 and page_two['rows'][0]['request'] == 50 and not page_two['next']
    exact_query = {key: value[0] for key, value in parse_qs(urlsplit(view['rows'][0]['issue_urls']['error']).query).items()}
    exact = workspace(report, [], exact_query)['issue_view']
    assert exact['total'] == 50 and all(one['role'] == 'chat' for one in exact['rows'])
    exact_query['issue_outcome'] = 'empty'
    assert workspace(report, [], exact_query)['issue_view']['total'] == 0
    assert workspace(report, [], dict(model='small', issue_page='invalid'))['issue_view']['page'] == 1


def test_issue_candidate_identity_separates_recreated_model_names():
    from weightroom.reliability_views import candidate_key
    old, current = candidate(), candidate()
    old.update(model_id='old-id', current=False, issue_requests=[dict(request=1, at=1000, outcome='error', recovery=False)])
    current.update(model_id='new-id', issue_requests=[dict(request=2, at=1001, outcome='error', recovery=False)])
    view = workspace(dict(rows=[old, current]), [], dict(model='small', fingerprints='all', issue_candidate=candidate_key(old)))
    assert [one['request'] for one in view['issue_view']['rows']] == [1]
