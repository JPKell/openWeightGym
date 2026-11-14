#!/usr/bin/env python3
"""Render three portable documentation designs from the current live documentation sources."""

import argparse
from pathlib import Path
import sys
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from weightroom import system_docs
from weightroom.docs_views import article


def render(base_url='http://localhost:8440'):
    """Reuse the live docs renderer, metadata, Markdown and API/source inventories without services."""
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    records, sections, aliases = [], system_docs.SECTIONS, system_docs.CODE_ALIASES
    for section in sections:
        for topic in (None, *section['topics']):
            html, text, _ = article(
                pages, section, topic, SimpleNamespace(can={'manage-settings'}), 'http://CONSOLE:8442/v1',
                system_docs.source_reference, system_docs.api_reference,
            )
            item = topic or section
            records.append({
                'url': item['url'], 'title': item['title'], 'summary': item['summary'],
                'section': section['slug'], 'category': section['title'],
                'public': section['public'], 'overview': topic is None,
                'liveUrl': item.get('live_url', item['url']),
                'codeUrl': item.get('code_url'),
                'text': text, 'html': html,
            })
    static = ROOT / 'weightroom/static'
    return pages.get_template('docs_designs.html').render(
        sections=sections, records=records, aliases=aliases, base_url=base_url.rstrip('/'),
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'docs-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        javascript=(static / 'docs-designs.js').read_text(),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/docs-designs/index.html')
    parser.add_argument('--base-url', default='http://localhost:8440', help='Console address for explicit live-console links')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args.base_url))
    print(args.output.resolve())
