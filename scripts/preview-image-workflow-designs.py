"""Render three offline image workflow editors from the shared catalogue and samples; never contacts services."""

import argparse
import re
import runpy
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]

# Sample installed models and library images; the editor validates families against these.
MODELS = [
    {'id': 'klein-4b', 'label': 'Klein 4B', 'family': 'flux2-klein', 'nodes': ['atlas', 'orion']},
    {'id': 'flux2-dev', 'label': 'Flux 2 Dev', 'family': 'flux2-dev', 'nodes': ['atlas']},
    {'id': 'sdxl-base', 'label': 'SDXL base', 'family': 'sdxl', 'nodes': ['atlas', 'orion']},
    {'id': 'sdxl-photo', 'label': 'SDXL photo fine-tune', 'family': 'sdxl', 'nodes': ['orion']},
    {'id': 'krea2-turbo', 'label': 'Krea 2 Turbo', 'family': 'krea2', 'nodes': ['atlas']},
    {'id': 'qwen-image', 'label': 'Qwen Image', 'family': 'qwen-image', 'nodes': ['atlas']},
]
# Sample upscaler files by file name, as nodes report them; the Upscale step and hires pass choose from these.
UPSCALERS = [{'id': '4x-UltraSharp.pth', 'label': '4x UltraSharp', 'scale': 4, 'nodes': ['atlas', 'orion']},
             {'id': 'RealESRGAN_x4plus.pth', 'label': 'RealESRGAN x4 plus', 'scale': 4, 'nodes': ['atlas', 'orion']},
             {'id': '4x_NMKD-Siax_200k.pth', 'label': '4x NMKD Siax 200k', 'scale': 4, 'nodes': ['orion']},
             {'id': 'RealESRGAN_x4plus_anime_6B.pth', 'label': 'RealESRGAN x4 anime 6B', 'scale': 4, 'nodes': ['atlas']},
             {'id': 'RealESRGAN_x2plus.pth', 'label': 'RealESRGAN x2 plus', 'scale': 2, 'nodes': ['atlas']}]
ASSETS = [{'id': 'jacket', 'label': 'Denim jacket.png'}, {'id': 'portrait', 'label': 'Studio portrait.png'},
          {'id': 'loft', 'label': 'Brick loft.png'}, {'id': 'street', 'label': 'Street photo.png'}]


def render():
    """Render the proposals with every script, style and sample inlined."""
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    graph = runpy.run_path(str(ROOT / 'mirrorwall/graph.py'))
    return pages.get_template('image_workflow_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'workflow-editor.css', 'image-workflow-designs.css')),
        graph_javascript=(static / 'workflow-graph.js').read_text(),
        editor_javascript=(static / 'workflow-editor.js').read_text(),
        javascript=(static / 'image-workflow-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(),
        catalogue=graph['catalogue'](), samples=graph['SAMPLES'], models=MODELS, assets=ASSETS, upscalers=UPSCALERS,
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="workflow-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#workflow-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/image-workflow-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
