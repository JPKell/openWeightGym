"""Private receipt projections retain correct aggregate quality scores."""

import importlib.util
import json
from pathlib import Path
import sqlite3


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/collect-benchmark-receipts.py'
spec = importlib.util.spec_from_file_location('benchmark_receipts', SOURCE)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_quality_score_uses_full_measurements_before_private_projection():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE benchmark_runs (id, model, model_id, benchmark, fingerprint, subject,
                                     environment, state, created, started, ended);
        CREATE TABLE benchmark_samples (run, sample, measurements, at);
        CREATE TABLE benchmark_evidence (run, sample, evidence);
        CREATE TABLE benchmark_definitions (id, formula, server);
    ''')
    db.execute('INSERT INTO benchmark_definitions VALUES (?,?,?)', ('json_schema', 'accuracy', 'reuse'))
    db.execute('INSERT INTO benchmark_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
        'quality-1', 'model', 'model-id', 'json_schema', 'fingerprint',
        json.dumps({'model_id': 'model-id', 'protocol': {'version': 4}, 'server': 'reuse'}),
        json.dumps({'engine_hash': 'engine', 'machine_hash': 'machine'}),
        'completed', 1, 2, 3))
    db.execute('INSERT INTO benchmark_samples VALUES (?,?,?,?)', (
        'quality-1', 'case-1', json.dumps({'success': True, 'checks': 2, 'correct': 2,
                                         'passed': True, 'json_valid': True, 'schema_valid': True,
                                         'value_checks': 2, 'value_correct': 2,
                                         'input_tokens': 100, 'output_tokens': 20,
                                         'request_seconds': .1, 'server_reused': False}), 2))
    db.execute('INSERT INTO benchmark_samples VALUES (?,?,?,?)', (
        'quality-1', 'case-2', json.dumps({'success': True, 'checks': 2, 'correct': 2,
                                         'passed': True, 'json_valid': True, 'schema_valid': True,
                                         'value_checks': 2, 'value_correct': 2,
                                         'input_tokens': 100, 'output_tokens': 20,
                                         'request_seconds': .1, 'server_reused': True}), 3))
    row = collector.receipt(db, 'json_schema', 'quality-1', 'model', 'completed')
    assert row['metrics']['accuracy'] == 100
    assert row['metrics']['passed_cases'] == 2
    assert row['score'] == 100
    assert all(row['checks'].values())
    assert not {'checks', 'correct', 'passed'} & row['samples'][0]['values'].keys()
