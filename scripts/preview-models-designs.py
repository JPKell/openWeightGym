"""Render three standalone Models inventory designs with browser-local sample data."""

import argparse
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    nodes = [
        dict(id='atlas', hardware='2 × RTX 6000 Ada', memory='96 GiB VRAM', count=30, online=True),
        dict(id='orion', hardware='2 × RTX 4090', memory='48 GiB VRAM', count=27, online=True),
        dict(id='nova', hardware='RTX 5090', memory='32 GiB VRAM', count=24, online=True),
        dict(id='cedar', hardware='Radeon PRO W7900', memory='48 GiB VRAM', count=21, online=True),
        dict(id='vega', hardware='RTX 3090', memory='24 GiB VRAM', count=18, online=False),
        dict(id='lyra', hardware='Ryzen 9 · CPU', memory='128 GiB RAM', count=15, online=True),
        dict(id='scout', hardware='RTX 4070', memory='12 GiB VRAM', count=0, online=True),
    ]
    catalogue = [
        ('Qwen3 8B', 8), ('Qwen3 14B', 14), ('Qwen3 32B', 32), ('Qwen3 30B A3B', 30),
        ('Llama 3.1 8B', 8), ('Llama 3.3 70B', 70), ('Gemma 3 4B', 4), ('Gemma 3 12B', 12),
        ('Gemma 3 27B', 27), ('Mistral Small 24B', 24), ('DeepSeek R1 8B', 8),
        ('DeepSeek R1 14B', 14), ('DeepSeek R1 32B', 32), ('Phi 4 14B', 14), ('Phi 4 Mini', 4),
        ('Qwen2.5 Coder 7B', 7), ('Qwen2.5 Coder 14B', 14), ('Qwen2.5 Coder 32B', 32),
        ('Qwen2.5 72B', 72), ('Llama 3.2 3B', 3), ('Llama 3.2 1B', 1), ('Gemma 3 1B', 1),
        ('QwQ 32B', 32), ('Mistral Nemo 12B', 12), ('Codestral 22B', 22), ('StarCoder2 15B', 15),
        ('Granite 3.3 8B', 8), ('OLMo 2 13B', 13), ('SmolLM2 1.7B', 1.7), ('Ministral 8B', 8),
        ('BGE M3', 1), ('Nomic Embed Text', 1), ('FLUX.1 dev', 12), ('SDXL Base', 7),
        ('FLUX.1 schnell', 12), ('Stable Diffusion 3.5', 8), ('Whisper Large v3', 2),
        ('Kokoro', 1), ('Piper', 1), ('Parler TTS', 1),
    ]
    models = []
    for i, (name, size) in enumerate(catalogue):
        kind = 'llm' if i < 32 else 'image' if i < 36 else 'audio'
        quant = 'Q4_K_M' if i < 30 else 'F16'
        models.append(dict(id=f'model-{i}', name=name, kind=kind,
                           family=name.split()[0], quant=quant, size=size,
                           role='Embedding' if 30 <= i < 32 else 'Vision' if name.startswith('Gemma 3')
                           else 'Code' if 'Coder' in name or 'Codestral' in name else 'General',
                           disk=round(size * (.62 if quant == 'Q4_K_M' else 1.9), 1),
                           fingerprint=f'{i + 321:012x}73b418a9', order=i + 1, placements=[]))
    for ni, node in enumerate(nodes):
        # Rotated subsets give each node its own inventory, including less widely installed models.
        indices = [(j + ni * 5) % len(models) for j in range(node['count'])]
        for j, index in enumerate(indices):
            model = models[index]
            state = 'in-use' if ni == 0 and j == 0 else 'loaded' if ni < 4 and j == 1 else 'installed'
            model['placements'].append(dict(node=node['id'], state=state,
                context=32768 if model['kind'] == 'llm' else None,
                min_context=4096 if model['kind'] == 'llm' else None,
                memory=round(model['disk'] * 1.18 + 1, 1),
                engine='llama.cpp' if model['kind'] == 'llm' else 'stable-diffusion.cpp' if model['kind'] == 'image' else 'audio.cpp',
                last_used='2 min ago' if state == 'in-use' else '18 min ago' if state == 'loaded'
                else 'Never' if j % 7 == 4 else f'{j + 1} h ago'))
    return dict(nodes=nodes, models=[model for model in models if model['placements']])


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('models_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'models.css', 'models-designs.css')),
        javascript=(static / 'models-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="inventory-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#inventory-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/models-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
