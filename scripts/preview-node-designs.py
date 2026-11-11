"""Render three portable Compute nodes proposals with interactive sample data."""

import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    nodes = [
        dict(name='atlas', role='Primary inference', state='ready', lifecycle='active', cpu='AMD Threadripper PRO', ram=256,
             cards=[dict(id='A', device='NVIDIA RTX 6000 Ada', vram=48, models='Qwen3 32B', vendor='nvidia'),
                    dict(id='B', device='NVIDIA RTX 6000 Ada', vram=48, models='No loaded models', vendor='nvidia')]),
        dict(name='nova', role='Image & audio', state='ready', lifecycle='active', cpu='AMD Ryzen 9 7950X', ram=128,
             cards=[dict(id='A', device='NVIDIA RTX 4090', vram=24, models='FLUX.1 dev', vendor='nvidia')]),
        dict(name='orion', role='Overflow inference', state='sleeping', lifecycle='active', cpu='AMD Ryzen 9 9950X', ram=128,
             cards=[dict(id='A', device='AMD Radeon PRO W7900', vram=48, models='No loaded models', vendor='amd')]),
        dict(name='lyra', role='Driver maintenance', state='ready', lifecycle='disabled', cpu='Intel Xeon W', ram=192,
             cards=[dict(id='A', device='NVIDIA A6000', vram=48, models='No loaded models', vendor='nvidia'),
                    dict(id='B', device='NVIDIA A6000', vram=48, models='No loaded models', vendor='nvidia')]),
        dict(name='front-cpu', role='CPU inference', state='ready', lifecycle='active', cpu='AMD Ryzen 9 7900', ram=64, cards=[]),
        dict(name='vega', role='Previous workstation', state='offline', lifecycle='retired', cpu='Intel Core i9', ram=64,
             cards=[dict(id='A', device='NVIDIA RTX 3090', vram=24, models='No loaded models', vendor='nvidia')]),
    ]
    return pages.get_template('node_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'node-designs.css')),
        javascript=(static / 'node-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(),
        nodes=nodes,
        # Keep the preview command intentionally inert, with the real installer's argument shape.
        pairing_command='curl -fsSL https://example.invalid/install.sh | bash -s -- --join https://console.example.invalid --code DEMO-ONLY',
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/node-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
