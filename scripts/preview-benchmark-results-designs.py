#!/usr/bin/env python3
"""Render three portable LLM Results proposals using the registered quality suites."""

import argparse
import datetime
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks import BENCHMARKS, MODULES  # noqa: E402
from weightroom.benchmark_views import metric_sections, results_workspace  # noqa: E402


def sample_data():
    """Invent clearly labelled scores; retain actual suite names, formulas and KPI contracts."""
    groups = {
        'Structured data': ['extraction', 'json_schema', 'tables'],
        'Reasoning': ['arithmetic', 'reasoning'],
        'Long context': ['retrieval', 'code_retrieval'],
        'Instructions': ['ifeval', 'hierarchy', 'turns', 'abstention'],
        'Tools': ['tool_use', 'tool_recovery'],
    }
    definitions = {b['id']: b for b in BENCHMARKS if b.get('quality') and not b.get('embedding')}
    benchmarks = []
    for group, ids in groups.items():
        for key in ids:
            b = definitions[key]
            benchmarks.append({**b, 'group': group, 'green': 'high',
                'score_label': 'Strict prompts passed' if key == 'ifeval' else
                               'Tasks completed' if b.get('native_tools') else 'Correct field checks',
                'dataset': MODULES[key].PROTOCOL.get('dataset', key),
                'grader': MODULES[key].PROTOCOL.get('grader_version', 1),
                'metric_sections': metric_sections(b)})
    # These are illustrative profiles, not measured claims about any named model.
    profiles = [
        ('Qwen3-32B', 'Qwen', '32B', [96, 94, 91, 93, 88, 92, 90, 91, 89, 87, 95, 90, 83]),
        ('DeepSeek-R1-Distill-Qwen-14B', 'DeepSeek', '14B', [91, 86, 94, 97, 96, 86, 84, 80, 75, 82, 89, 73, 67]),
        ('Llama-3.3-70B-Instruct', 'Llama', '70B', [94, 92, 89, 90, 85, 95, 91, 93, 92, 90, 94, 88, 83]),
        ('Qwen3-30B-A3B', 'Qwen', '30B · 3B active', [93, 90, 86, 91, 85, 88, 87, 87, 85, 82, 91, 85, 75]),
        ('Gemma-3-27B', 'Gemma', '27B', [92, 89, 84, 88, 83, 91, 82, 90, 87, 86, 92, 80, 75]),
        ('Mistral-Small-3.1-24B', 'Mistral', '24B', [90, 91, 85, 86, 80, 87, 86, 88, 83, 85, 88, 87, 83]),
        ('Phi-4-14B', 'Phi', '14B', [88, 85, 91, 95, 92, 80, 83, 82, 78, 79, 84, 77, 67]),
        ('Qwen3-8B', 'Qwen', '8B', [88, 87, 79, 84, 78, 83, 81, 85, 79, 77, 88, 80, 67]),
        ('Gemma-3-12B', 'Gemma', '12B', [85, 82, 77, 80, 75, 86, 76, 83, 81, 79, 86, 72, 58]),
        ('Llama-3.1-8B-Instruct', 'Llama', '8B', [82, 78, 70, 74, 65, 79, 74, 79, 72, 70, 80, 68, 50]),
        ('Qwen3-4B', 'Qwen', '4B', [75, 76, 65, 69, 58, 71, 68, 74, 63, 60, 77, 57, 0]),
        ('Qwen3-0.6B', 'Qwen', '0.6B', [None] * 13),
    ]
    cases = {'json_schema': 27, 'extraction': 36, 'arithmetic': 40, 'retrieval': 20,
             'code_retrieval': 20, 'ifeval': 541, 'tool_use': 60, 'tool_recovery': 12,
             'abstention': 20, 'tables': 40, 'turns': 48, 'reasoning': 48, 'hierarchy': 56}
    checks = {'extraction': 288, 'arithmetic': 128, 'retrieval': 200,
              'abstention': 120, 'tables': 136, 'turns': 192, 'reasoning': 144, 'hierarchy': 224}
    models, latest = [], {}
    snapshot = datetime.datetime(2026, 11, 13, 10, 42, tzinfo=datetime.timezone(datetime.timedelta(hours=-8)))
    for index, (name, family, params, scores) in enumerate(profiles):
        models.append(dict(id=name, family=family, parameters=params, quant='Q4_K_M', kind='llm'))
        latest[name] = {}
        for bi, (b, wanted) in enumerate(zip(benchmarks, scores)):
            if wanted is None or (index, b['id']) in {(8, 'code_retrieval'), (10, 'hierarchy')}:
                continue
            key, n = b['id'], cases[b['id']]
            denominator = n if key == 'ifeval' or b.get('native_tools') else checks.get(key, n * 6)
            score = round(wanted / 100 * denominator) / denominator * 100
            passed = round(n * max(0, score - 14) / 100)
            metrics = {}
            for ki, (metric, _, unit) in enumerate(b['kpis']):
                metrics[metric] = round(min(100, max(0, score + ((ki * 7 + index) % 13 - 4))), 1) if unit == '%' else None
            metrics.update(accuracy=score, case_accuracy=100 * passed / n, cases=n,
                           passed_cases=passed, available_cases=n, max_input_tokens=8192,
                           capped_cases=1 if index > 6 else 0,
                           uncapped_cases=n - (1 if index > 6 else 0), unsupported_cases=0,
                           recorded_samples=n, input_tokens=n * 2048, output_tokens=n * 380,
                           reasoning_tokens=n * 190 if index in (1, 6) else None,
                           reasoning_token_cases=n if index in (1, 6) else 0,
                           tool_calls=n * 2, native_call_attempts=n * 2,
                           unnecessary_retries=index % 3, no_call_cases=10, calls_when_unneeded=index % 3,
                           invented_fields=index % 7, absent_fields=60,
                           forced_cases=27, forced_available_cases=27, forced_unavailable_cases=0,
                           forced_capped_cases=0, matched_pairs=27,
                           capped_checks=3 if index > 6 else 0)
            # Populate the actual registry fields, including counts, deltas and throughput.
            # These are sample observations; relationships between their totals remain consistent.
            metrics.update(invention=100 * (index % 7) / 60,
                           invention_rate=100 * (index % 7) / 60,
                           no_call_cases=10, calls_when_unneeded=index % 3,
                           unneeded_call_rate=10 * (index % 3),
                           max_input_tokens=65536 if key in ('retrieval', 'code_retrieval') else 8192)
            if key == 'json_schema':
                free_values = min(100, score + 2.4)
                forced_values = min(100, free_values + (3.7 if index % 3 else -1.2))
                unsupported = 2 if index == 8 else 0
                unavailable = 1 if index == 10 else 0
                metrics.update(recorded_samples=54, json_accuracy=100 * min(n, passed+4) / n,
                    schema_accuracy=100 * min(n, passed+2) / n, value_accuracy=free_values,
                    unsupported_cases=unsupported, forced_unavailable_cases=unavailable,
                    forced_available_cases=n-unsupported-unavailable, matched_pairs=n-unsupported-unavailable,
                    forced_capped_cases=1 if index > 6 else 0,
                    forced_accuracy=min(100, score+4.2), forced_schema_accuracy=100,
                    forced_value_accuracy=forced_values, paired_free_value_accuracy=free_values,
                    paired_forced_value_accuracy=forced_values, forced_value_delta=forced_values-free_values,
                    free_output_tokens_per_second=round(39.8 / (1 + index * .11), 3),
                    forced_output_tokens_per_second=round(43.2 / (1 + index * .13), 3))
            if key == 'ifeval':
                metrics.update(prompt_strict_accuracy=score, instruction_strict_accuracy=min(100, score+2.8),
                    prompt_loose_accuracy=min(100, score+4.1), instruction_loose_accuracy=min(100, score+6.3),
                    accuracy=min(100, score+2.8), passed_cases=round(n * score / 100), case_accuracy=score)
            if b.get('native_tools'):
                metrics['task_completion'] = score
            if key == 'tool_use':
                metrics['recovery_accuracy'] = None  # This suite injects no recovery errors.
            status = 'settings' if (index, key) in {(3, 'extraction'), (7, 'tables'), (9, 'turns')} else \
                     'protocol' if (index, key) in {(4, 'reasoning'), (6, 'json_schema')} else 'current'
            if (index, key) == (8, 'tool_recovery'):
                status, score = 'unsupported', None
                metrics = {k: None for k, _, _ in b['kpis']}
                metrics.update(cases=n, available_cases=0, unsupported_cases=n)
            if (index, key) == (10, 'retrieval'):
                status, score = 'partial', None
                metrics = {k: None for k, _, _ in b['kpis']}
                metrics.update(cases=n, available_cases=6)
            labels = {'current': 'Current', 'settings': 'Settings changed', 'protocol': 'Older protocol',
                      'unsupported': 'Unsupported', 'partial': 'Partial'}
            latest[name][key] = dict(id=f'DEMO-{index + 1:02d}-{key}', score=score, metrics=metrics,
                status=status, status_label=labels[status], state='running' if status == 'partial' else 'completed',
                current_fingerprint=status != 'settings', version=max(1, b['version'] - (status == 'protocol')),
                latest_version=b['version'], rerun=['Benchmark protocol changed'] if status == 'protocol' else [],
                node=('atlas', 'orion', 'nova')[index % 3], context=131072 if key in ('retrieval', 'code_retrieval') else 32768,
                ended=(snapshot - datetime.timedelta(hours=index * 3 + bi, days=3 if status != 'current' else 0)).timestamp())
    return dict(models=models, benchmarks=benchmarks, latest=latest, snapshot=snapshot.isoformat(), groups=groups)


def render():
    data = sample_data()
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters['timestamp'] = lambda value: datetime.datetime.fromtimestamp(
        value, datetime.timezone(datetime.timedelta(hours=-8))).strftime('%d %b · %H:%M PST')
    colours = {(m['id'], b['id']): heat_colour(result['score'])
               for m in data['models'] for b in data['benchmarks']
               if (result := data['latest'].get(m['id'], {}).get(b['id'])) and result['score'] is not None}
    return pages.get_template('benchmark_results_designs.html').render(
        data=data, colours=colours,
        results=results_workspace(data['models'], data['models'], data['benchmarks'],
                                  next(b for b in data['benchmarks'] if b['id'] == 'json_schema'),
                                  data['latest'], {'baseline': 'Qwen3-8B'}),
        css='\n'.join((static / file).read_text() for file in
                      ('tabler.min.css', 'style.css', 'themes.css', 'benchmark-results.css', 'benchmark-results-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        javascript=(static / 'benchmark-results-designs.js').read_text(),
        chart_library=(static / 'echarts.min.js').read_text(),
        heatmap_javascript=(static / 'benchmark-heatmap.js').read_text())


def heat_colour(score):
    """Use one fixed 0–100 accuracy scale; avoid inflating small differences within columns."""
    stops = [(242, 245, 247), (204, 227, 227), (146, 202, 195), (74, 163, 152)]
    position = min(2.9999, max(0, score / 100 * 3))
    low, fraction = int(position), position % 1
    return '#' + ''.join(f'{round(a + (b-a)*fraction):02x}' for a, b in zip(stops[low], stops[low+1]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/benchmark-results-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
