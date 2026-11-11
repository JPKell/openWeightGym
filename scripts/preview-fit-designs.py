"""Render three Dry fit proposals with illustrative data and browser-local interactions."""

import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]


def render() -> str:
    """Inline assets and the canonical implementation checklist into a standalone preview."""
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    guide = (ROOT / 'docs/model-image-settings-plan.md').read_text()
    todo = guide.split('<!-- fit-design-todo:start -->')[1].split('<!-- fit-design-todo:end -->')[0]
    return pages.get_template('fit_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'fit.css', 'fit-designs.css')),
        javascript=(static / 'fit-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(),
        todo=MarkdownIt().render(todo),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/fit-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
