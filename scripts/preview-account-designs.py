#!/usr/bin/env python3
"""Render three portable My Account proposals with isolated sample interactions."""

import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = [
    ('account', 'Account', 'Identity, password & sign-ins', '01'),
    ('keys', 'API keys', 'Credentials for your applications', '02'),
    ('appearance', 'Appearance', 'Theme, display & accessibility', '03'),
    ('audio', 'Audio', 'Voices, models & downloads', '04'),
    ('workspace', 'Workspace', 'Starting page & regional defaults', '05'),
    ('privacy', 'Alerts & privacy', 'Personal notices & data visibility', '06'),
    ('logs', 'Logs', 'Your requests & account activity', '07'),
]


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('account_designs.html').render(
        sections=SECTIONS,
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'users.css', 'account-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        javascript=(static / 'account-designs.js').read_text(),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/account-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
