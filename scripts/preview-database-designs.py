"""Render three DB proposals with sample data and browser-local operations only."""

import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]


def render() -> str:
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    guide = (ROOT / 'docs/database-storage.md').read_text()
    todo = guide.split('<!-- db-design-todo:start -->')[1].split('<!-- db-design-todo:end -->')[0]
    return pages.get_template('database_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'database-designs.css')),
        javascript=(static / 'database-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(),
        todo=MarkdownIt().render(todo),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/database-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
