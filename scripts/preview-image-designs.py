"""Render three integrated image model/workflow proposals using browser-local sample data."""

import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('image_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'image-studio.css', 'image-designs.css')),
        javascript=(static / 'image-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/image-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
