"""Render three offline image-generation workspaces; never submit real image jobs."""

import argparse
import base64
import re
import runpy
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def render():
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    samples = {name: 'data:image/webp;base64,' + base64.b64encode(
        (static / 'generation-samples' / f'{name}.webp').read_bytes()).decode()
        for name in ('tiger', 'apple', 'neon')}
    model = runpy.run_path(str(ROOT / 'mirrorwall/model.py'))
    return pages.get_template('generation_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'generation.css', 'generation-designs.css')),
        javascript=(static / 'generation-designs.js').read_text(),
        profiles=(ROOT / 'mirrorwall/static/image-profiles.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), samples=samples,
        samplers=model['SAMPLERS'], schedulers=model['SCHEDULERS'],
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="generation-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#generation-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/generation-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
