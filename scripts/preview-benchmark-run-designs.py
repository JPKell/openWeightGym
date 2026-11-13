"""Render three portable, interactive benchmark run detail proposals. No live APIs."""

import argparse
import hashlib
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    samples = []
    categories = ['Direct fields', 'Cross-reference', 'Absent fields']
    for index in range(48):
        budget = (2048, 8192, 16384, 32768)[index // 12]
        category = categories[(index % 12) // 4]
        state = ('unsupported' if index in (44, 45) else 'unavailable' if index in (46, 47)
                 else 'fail' if index in (10, 22, 33, 38, 42) else 'pass')
        correct = 3 if state == 'fail' else 0 if state in ('unsupported', 'unavailable') else 4
        source = f'Archive record {index + 1:02d}\nSupplier: Northline Works\nInvoice: NW-{1040 + index}\nTotal: CAD {120 + index}.50\nDue date: not supplied.'
        expected = {'supplier': 'Northline Works', 'invoice': f'NW-{1040 + index}',
                    'total': f'{120 + index}.50', 'due_date': None}
        answer = dict(expected, due_date='2026-12-01') if state == 'fail' else expected
        available = state in ('pass', 'fail')
        prompt = ('Extract supplier, invoice, total and due_date as JSON. Use null for missing fields.\n\n'
                  + source + '\n[Archive padding omitted in this illustrative fixture.]')
        values = dict(context_tokens=budget, input_tokens=budget - 128, output_tokens=84 + index % 7,
                      prompt_tokens_per_second=round(1850 / (1 + index // 12 * .16), 1),
                      generation_tokens_per_second=round(68.4 - index // 12 * 4.2 + index % 4 * .6, 1),
                      ttft_seconds=round((budget - 128) / (1850 / (1 + index // 12 * .16)), 3),
                      load_seconds=.284 if index else 2.417, request_seconds=round(budget / 1400 + 1.4, 3),
                      peak_gpu_mib=6150 + index // 12 * 1350, peak_ram_mib=1840 + index // 12 * 180,
                      success=available, passed=state == 'pass', correct=correct, checks=4,
                      embedding_tokens_per_second=round(2760 - index // 12 * 160 + index % 4 * 12, 1),
                      embeddings_per_second=round(2760 / (budget - 128), 3), embedding_dimensions=1024)
        if not available:
            for name in ('input_tokens', 'output_tokens', 'prompt_tokens_per_second', 'generation_tokens_per_second',
                         'ttft_seconds', 'load_seconds', 'request_seconds', 'peak_gpu_mib', 'peak_ram_mib',
                         'embedding_tokens_per_second', 'embeddings_per_second', 'embedding_dimensions'):
                values[name] = None
            values['error'] = ('Input exceeds supported context for this configuration.' if state == 'unsupported'
                               else 'Connection closed before a response was received.')
        samples.append(dict(id=f'S{index + 1:03d}', case=f'invoice-{index % 12 + 1:02d}',
                            title=f'{category} / invoice {index % 12 + 1:02d}', category=category,
                            state=state, budget=budget, values=values,
                            baseline='fail' if index in (4, 10, 18, 22, 30, 33, 38) else 'pass',
                            evidence=dict(prompt=prompt, prompt_tokens_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                                          answer=json.dumps(answer, indent=2) if available else '',
                                          case=dict(id=f'invoice-{index % 12 + 1:02d}', expected=expected),
                                          checks=[dict(field=field, expected=value, actual=answer[field],
                                                       passed=value == answer[field]) for field, value in expected.items()] if available else [],
                                          resource=dict(device='gpu', peak_ram_mib=values['peak_ram_mib'])),
                            output=json.dumps(answer, indent=2) if available else '',
                            log=f'[probe] context={budget}\n[server] ' + ('request completed; receipt retained' if available else values['error'])))
    return dict(id='DEMO-20261113-1042', model='Qwen3 8B', fingerprint='f7b12d38a09e6c258f4a873d1bc069e421ed2a61558a9f0cb7463d920ea5c817',
                engine='llama.cpp', engine_hash='c4b70e91267a', node='atlas', host='atlas.local',
                hardware='RTX 4090 · 24 GiB', created='13 Nov 2026, 10:42 PST', ended='10:50:14 PST',
                duration='8m 14s', version=3, samples=samples,
                subject=dict(settings={'engine': 'llama_cpp', 'flags': {'ctx-size': 32768, 'n-gpu-layers': 99,
                    'cache-type-k': 'q8_0', 'cache-type-v': 'q8_0', 'temp': 0, 'seed': 42},
                    'file': 'Qwen3-8B-Q4_K_M.gguf'}, protocol={'dataset': 'extraction-v3', 'grader_version': 3,
                    'constrained_decoding': False, 'server': 'native chat completions'}),
                environment=dict(engine='llama.cpp', engine_hash='c4b70e91267a', machine={'host': 'atlas.local'},
                                 backend='CUDA', gpu='NVIDIA RTX 4090', driver='sample driver', device='gpu'),
                messages=[{'at': '10:42:00', 'text': 'Run created by jordan · manual request'},
                          {'at': '10:42:02', 'text': 'Recorded model settings and environment frozen'},
                          {'at': '10:42:03', 'text': 'Model loaded · native chat completions'},
                          {'at': '10:49:56', 'text': '44 executed · 2 unsupported · 2 unavailable'},
                          {'at': '10:50:14', 'text': 'Completed · 48 sample receipts retained'}])


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('benchmark_run_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'benchmark-run.css', 'benchmark-run-designs.css')),
        javascript=(static / 'benchmark-run-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/benchmark-run-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
