"""Render three offline Manage proposals using the actual benchmark definitions and controls."""

import argparse
from pathlib import Path
import sys

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks import BENCHMARKS
from weightroom.navigation import navigation


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    definitions = [dict(b, enabled=True, green='high', server='reuse') for b in BENCHMARKS]
    groups = [('Language quality', [b for b in definitions if b.get('quality') and not b.get('classifier')]),
              ('Node measurements', [b for b in definitions if not b.get('quality') and not b.get('classifier')]),
              ('Classifier', [b for b in definitions if b.get('classifier')])]
    return pages.get_template('benchmark_manage_designs.html').render(
        benchmarks=definitions, groups=groups,
        nav=navigation('/benchmarks/manage', ['manage-settings'], False),
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'benchmark-manage.css', 'benchmark-manage-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        icons=(static / 'icons.svg').read_text(),
        javascript=(static / 'benchmark-manage-designs.js').read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/benchmark-manage-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
