"""Render three portable Node measurements proposals with sample evidence only."""

import argparse
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    nodes = [
        dict(id='atlas', hardware='2 × RTX 6000 Ada · 96 GiB', state='Ready', count=30, factor=1),
        dict(id='orion', hardware='2 × RTX 4090 · 48 GiB', state='Ready', count=27, factor=.82),
        dict(id='nova', hardware='RTX 5090 · 32 GiB', state='Ready', count=24, factor=.72),
        dict(id='cedar', hardware='Radeon PRO W7900 · 48 GiB', state='Ready', count=21, factor=.58),
        dict(id='front-cpu', hardware='Ryzen 9 · 128 GiB RAM · CPU', state='CPU only', count=15, factor=.12),
        dict(id='vega', hardware='RTX 3090 · 24 GiB', state='Unreachable', count=18, factor=.46),
        dict(id='lyra', hardware='A6000 · 48 GiB', state='Disabled', count=20, factor=.52),
        dict(id='scout', hardware='RTX 4070 · 12 GiB', state='Ready', count=0, factor=.3),
    ]
    llms = [
        ('Qwen3 8B', 8), ('Qwen3 14B', 14), ('Qwen3 32B', 32), ('Qwen3 30B A3B', 30),
        ('Llama 3.1 8B', 8), ('Llama 3.3 70B', 70), ('Gemma 3 4B', 4), ('Gemma 3 12B', 12),
        ('Gemma 3 27B', 27), ('Mistral Small 24B', 24), ('DeepSeek R1 8B', 8), ('DeepSeek R1 14B', 14),
        ('DeepSeek R1 32B', 32), ('Phi 4 14B', 14), ('Phi 4 Mini', 4), ('Qwen2.5 Coder 7B', 7),
        ('Qwen2.5 Coder 14B', 14), ('Qwen2.5 Coder 32B', 32), ('Qwen2.5 72B', 72),
        ('Llama 3.2 3B', 3), ('Llama 3.2 1B', 1), ('Gemma 3 1B', 1), ('QwQ 32B', 32),
        ('Mistral Nemo 12B', 12), ('Codestral 22B', 22), ('StarCoder2 15B', 15),
        ('Granite 3.3 8B', 8), ('OLMo 2 13B', 13), ('SmolLM2 1.7B', 2), ('Ministral 8B', 8),
    ]
    families = {
        'llm': llms,
        'embedding': [('BGE M3', 1), ('Nomic Embed Text', 1), ('Qwen3 Embedding 4B', 4),
                      ('Qwen3 Embedding 8B', 8), ('E5 Large', 1), ('Snowflake Arctic Embed', 1)],
        'image': [('FLUX.1 dev', 12), ('SDXL Base', 7), ('Stable Diffusion 3.5', 8),
                  ('FLUX.1 schnell', 12), ('PixArt Sigma', 1), ('Sana', 2)],
        'audio': [('Whisper Large v3', 2), ('Kokoro', 1), ('Piper', 1),
                  ('Parler TTS', 1), ('Bark', 1), ('MusicGen', 3)],
    }
    models, results = [], {}
    for kind, catalogue in families.items():
        for index, (name, size) in enumerate(catalogue):
            model_id = f'{kind}-{index}'
            models.append(dict(id=model_id, name=name, kind=kind, size=size,
                               format='Q4_K_M' if kind == 'llm' else 'F16', placements=[]))
            for ni, node in enumerate(nodes):
                if index >= (node['count'] if kind == 'llm' else (0 if not node['count'] else max(1, 6-ni))):
                    continue
                models[-1]['placements'].append(node['id'])
                context = int(max(4096, 131072 * node['factor'] / max(1, size / 8)) // 1024 * 1024)
                values = dict(context=context, load=round((size * 1.5 + 2 + index % 3) / node['factor'], 1),
                              speed=round((118 if kind == 'llm' else 2600) * node['factor'] / max(1, size / 8), 1),
                              prefill=round(1850 * node['factor'] / max(1, size / 8)),
                              ttft=round(2048 / (1850 * node['factor'] / max(1, size / 8)) + .08, 2))
                measurements = {}
                for mi, metric in enumerate(('context', 'load', 'speed')):
                    seed = index * 7 + ni * 3 + mi
                    state = ('missing' if seed % 13 == 8 else 'failed' if seed % 23 == 11
                             else 'stale' if seed % 11 == 6 else 'current')
                    if kind in ('image', 'audio') and metric != 'load':
                        continue
                    measurements[metric] = dict(state=state,
                        run=f'DEMO-{kind}-{index:02d}-{ni}-{metric}' if state != 'missing' else None,
                        at=None if state == 'missing' else '2026-11-09 16:24 PST' if state == 'stale' else '2026-11-11 10:42 PST')
                results[f'{model_id}/{node["id"]}'] = dict(values=values, measurements=measurements,
                    backend='CPU' if node['id'] == 'front-cpu' else 'Vulkan' if node['id'] == 'cedar' else 'CUDA',
                    engine='llama.cpp' if kind in ('llm', 'embedding') else 'sdcpp' if kind == 'image' else 'audio runtime')
    return dict(nodes=nodes, models=models, results=results)


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('node_measurement_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'node-measurements.css', 'node-measurement-designs.css')),
        javascript=(static / 'node-measurement-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="measurement-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#measurement-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/node-measurement-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
