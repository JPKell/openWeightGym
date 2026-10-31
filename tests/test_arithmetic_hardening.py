"""Real saved-answer regressions and independent arithmetic-v3 answer-key checks."""
import hashlib
import json
import csv
import io
import asyncio
import calendar
import copy
import sqlite3
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import quality
from benchmarks.suites import arithmetic
from benchmarks.store import Benchmarks


FIXTURES = json.loads((Path(__file__).parent / 'fixtures/arithmetic_audit_20261014.json').read_text())


@pytest.mark.parametrize('sample', FIXTURES, ids=lambda sample: sample['model']+':'+sample['sample'])
def test_audited_saved_answers_preserve_real_field_credit(sample):
    assert hashlib.sha256(sample['output'].encode()).hexdigest() == sample['output_sha256']
    answer = quality.final_answer(sample['output'], sample['prompt'])
    values, checks = quality.grade_fields(sample['case'], answer)
    assert (values['correct'], values['checks'], values['passed']) == (sample['correct'], sample['checks'], sample['passed'])
    assert sum(check['passed'] for check in checks) == sample['correct']


def test_arithmetic_v3_has_varied_seeded_cases_and_preserves_the_shared_grader():
    cases = arithmetic.cases(131072)
    assert cases == arithmetic.cases(131072)
    assert Counter(case['category'] for case in cases) == dict.fromkeys(('word', 'table', 'conversion', 'percent', 'calendar'), 8)
    assert len({case['id'] for case in cases}) == 40
    assert sum(len(case['expected']) for case in cases) == 128
    entities = [entity for case in cases for entity in case['entities']]
    assert len(entities) == len(set(entities))
    smaller = arithmetic.cases(2048)
    for large, small in zip(cases, smaller):
        assert {key: value for key, value in large.items() if key not in ('context', 'outputs')} == {key: value for key, value in small.items() if key not in ('context', 'outputs')}
        assert large['outputs'] == 8192 and small['outputs'] == 1024
        assert large['inputs'] == small['inputs'] == 0
        assert all(type(value) in (int, str) for value in large['expected'].values())
        assert arithmetic.grade(large, json.dumps(large['expected']))[0]['passed']
        for field in large['expected']:
            wrong = {**large['expected'], field: True}
            values, _ = arithmetic.grade(large, json.dumps(wrong))
            assert not values['passed'] and values['correct'] == len(wrong) - 1
    assert arithmetic.PROTOCOL['version'] == arithmetic.DEFINITION['version'] == 3
    assert arithmetic.PROTOCOL['dataset'] == 'arithmetic-v3' and arithmetic.PROTOCOL['seed'] == arithmetic.SEED
    assert arithmetic.PROTOCOL['grader_version'] == quality.PROTOCOL['grader_version'] == 3
    assert arithmetic.PROTOCOL['constrained_decoding'] is False


def test_purchase_keys_reconcile_with_independent_decimal_invoices():
    for case in arithmetic.cases(8192):
        if case['category'] != 'word':
            continue
        facts, expected = case['source'], case['expected']
        # Separate item discount and tax line items, rather than the generator's fraction product.
        invoice = Decimal(facts['quantity']) * facts['price_cents']
        discount = invoice * Decimal(facts['discount']) / 100
        tax = (invoice - discount) * Decimal(facts['tax']) / 100
        paid = invoice - discount + tax + facts['shipping_cents']
        assert paid == expected['share_cents'] * facts['people']
        assert invoice == expected['subtotal_cents']
        assert expected['share_cents'] + expected['left_cents'] == facts['budget_cents']


def test_table_keys_match_sql_queries_over_the_actual_csv():
    counts = set()
    for case in arithmetic.cases(8192):
        if case['category'] != 'table':
            continue
        text = case['request']['question']
        content = text.split('\n', 1)[1].split('\n\n', 1)[0]
        rows = list(csv.DictReader(io.StringIO(content)))
        assert len(rows) == 30 and len({row['row'] for row in rows}) == 30
        with sqlite3.connect(':memory:') as connection:
            connection.execute('CREATE TABLE ledger(row TEXT,team TEXT,units INTEGER,cents INTEGER)')
            connection.executemany('INSERT INTO ledger VALUES(?,?,?,?)', [(row['row'], row['team'], int(row['units']), int(row['cents'])) for row in rows])
            source = case['source']
            count, units = connection.execute('SELECT COUNT(*),SUM(units) FROM ledger WHERE team=? AND cents>=?', (source['selected_team'], source['threshold_cents'])).fetchone()
            highest = connection.execute('SELECT row FROM ledger ORDER BY units*cents DESC,row LIMIT 1').fetchone()[0]
            winner = connection.execute('SELECT team FROM ledger GROUP BY team ORDER BY SUM(units*cents) DESC,team LIMIT 1').fetchone()[0]
        assert case['expected'] == dict(selected_rows=count, selected_units=units, highest_row=highest, winning_team=winner)
        assert 0 < count < 30
        counts.add(count)
    assert len(counts) >= 5  # no fixed count field a lazy solver can always copy


def test_conversions_percentages_and_dates_obey_inverse_constraints():
    leap = 0
    for case in arithmetic.cases(8192):
        facts, answer = case['source'], case['expected']
        if case['category'] == 'conversion':
            if 'duration' in facts:
                hours, rest = divmod(answer['duration_seconds'], 3600)
                minutes, seconds = divmod(rest, 60)
                assert [hours, minutes, seconds] == facts['duration']
            for dimension, field, factor in (('distance', 'distance_mm', 1000), ('mass', 'mass_grams', 1000), ('payment', 'payment_cents', 100)):
                if dimension in facts:
                    major, minor, count = facts[dimension]
                    assert answer[field] % count == 0
                    each = answer[field] // count
                    assert divmod(each, factor) == (major, minor * 10 if dimension == 'distance' else minor)
        elif case['category'] == 'percent':
            assert answer['change_tenths_percent'] * facts['start_cents'] == 1000 * (facts['end_cents'] - facts['start_cents'])
            assert answer['original_cents'] * (100 + facts['rise']) == facts['shown_cents'] * 100
            assert answer['final_units'] * 10000 == facts['stock'] * (100 + facts['first']) * (100 - facts['second'])
        elif case['category'] == 'calendar':
            start, end = date.fromisoformat(facts['start']), date.fromisoformat(facts['end'])
            leap += calendar.isleap(start.year)
            assert start.month == 2 and end.month == 3
            assert answer['elapsed_days'] == calendar.monthrange(start.year, 2)[1] - start.day + end.day
            weekday = date.fromisoformat(facts['weekday']).isoweekday()
            assert answer['weekday_name'] == ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')[weekday-1]
            assert date.fromisoformat(answer['shifted_date']).toordinal() == date.fromisoformat(facts['anchor']).toordinal() + facts['offset']
    assert leap == 4


def lazy_arithmetic(case):
    """Skip discount/tax and conversions; inspect only ten rows; assume February has 28 days."""
    facts = case['source']
    if case['category'] == 'word':
        total = facts['quantity'] * facts['price_cents']
        share = (total + facts['shipping_cents']) // facts['people']
        return dict(subtotal_cents=total, share_cents=share, left_cents=facts['budget_cents']-share)
    if case['category'] == 'table':
        rows = facts['rows'][:10]
        selected = [row for row in rows if row['team'] == facts['selected_team'] and row['cents'] >= facts['threshold_cents']]
        totals = {team:sum(row['units']*row['cents'] for row in rows if row['team']==team) for team in {row['team'] for row in rows}}
        return dict(selected_rows=len(selected), selected_units=sum(row['units'] for row in selected),
                    highest_row=min(rows,key=lambda row:(-row['units']*row['cents'],row['row']))['row'],
                    winning_team=min(totals,key=lambda team:(-totals[team],team)))
    if case['category'] == 'percent':
        return dict(change_tenths_percent=facts['end_cents']-facts['start_cents'], original_cents=facts['shown_cents'],
                    final_units=facts['stock']*(100+facts['first']-facts['second'])//100)
    if case['category'] == 'calendar':
        start, end = date.fromisoformat(facts['start']), date.fromisoformat(facts['end'])
        return dict(elapsed_days=28-start.day+end.day, weekday_name='Monday', shifted_date=facts['anchor'])
    return {field:0 for field in ('duration_seconds','distance_mm','mass_grams','payment_cents')}


def test_lazy_answerers_do_not_get_high_arithmetic_scores():
    cases = arithmetic.cases(131072)
    for answerer in (lambda case: dict.fromkeys(case['expected']), lazy_arithmetic):
        score = quality.summarize([{'values': {'success': True, **arithmetic.grade(case, json.dumps(answerer(case)))[0]}} for case in cases])
        assert score['accuracy'] < 50 and score['passed_cases'] == 0


def test_v2_results_stay_intact_and_unfinished_v2_plans_are_not_mixed(tmp_path):
    store = Benchmarks(tmp_path/'arithmetic.db')
    fixture = next(sample for sample in FIXTURES if sample['passed'])
    legacy = {**arithmetic.PROTOCOL, 'version': 2, 'dataset': 'arithmetic-v2', 'cases':20, 'outputs':4096}
    legacy.pop('seed')
    number = store.create('owner', 'model', 'arithmetic')
    store.activate(number, {'fingerprint':'fixture','environment':{},'subject':{'protocol':legacy,'settings':{'flags':{'ctx-size':8192}}}})
    # Seed a historical subject: activation intentionally always uses today's protocol.
    subject = store.execution(number)['subject']
    subject['protocol'] = legacy
    with store.db:
        store.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?', (json.dumps(subject), number))
    store.plan(number, {'protocol':legacy,'signature':quality.signature(legacy),'cases':[fixture['case']]})
    values = {'success':True,'correct':3,'checks':3,'passed':True}
    store.keep(number, fixture['sample'], values, fixture['output'], '', {'case':fixture['case'],'prompt':fixture['prompt']})
    store.finish(number, 'completed')
    before = copy.deepcopy(store.samples(number, True))
    assert store.regrade() == 0 and store.samples(number, True) == before
    run = store.get(number, True)
    assert run['version'] == 2 and run['latest_version'] == 3 and run['score'] == 100
    # Also exercise the continuation gate with a persisted, unfinished historical run.
    store.finish(number, 'paused')
    run = store.get(number, True)
    calls = []
    async def evaluate(*args):
        calls.append(args)
    worker = SimpleNamespace(store=store, evaluate=evaluate)
    with pytest.raises(ValueError, match='protocol changed'):
        asyncio.run(arithmetic.measure(worker, run, 8192))
    assert not calls and store.plan(number)['protocol']['version'] == 2
    assert store.samples(number, True) == before
    store.db.close()
