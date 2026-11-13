"""Render three offline desktop Voice proposals into one self-contained preview."""

import argparse
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def render():
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'),
                        autoescape=select_autoescape())
    return pages.get_template('voice_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'voice-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        javascript=(static / 'voice-designs.js').read_text(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="vd-icon-\1"',
                         (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#vd-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/voice-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
