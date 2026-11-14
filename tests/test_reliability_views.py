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
