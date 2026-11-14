"""Render three portable Plugin logs proposals using retained-record sample data."""

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    now = datetime(2026, 11, 13, 14, 30, tzinfo=ZoneInfo('America/Vancouver'))
    rows = []
    for group in range(24):
        instance = ('jeeves', 'research', 'sandbox')[group % 3]
        user = ('jordan', 'pat', 'alex', 'sam')[group % 4]
        for turn in range(3):
            index = group * 3 + turn
            rows.append(dict(
                id=2084 - index, at=(now - timedelta(hours=group * 9, minutes=turn * 8 + 3)).timestamp(),
                user=user, model=f'hermes:{instance}', plugin='hermes', instance=instance,
                profile=('default', 'research', 'development')[group % 3],
                session=None if group == 23 else f'sess-{group + 1:04d}-c38e-a4f6',
                request=None if index == 71 else f'req-{2084 - index}-8b32-4d91-bf63',
                outcome='failed' if index % 11 == 0 else 'interrupted' if index % 9 == 0 else 'completed',
            ))
    return dict(now=now.timestamp(), zone='America/Vancouver', rows=rows)


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('plugin_log_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'plugin-logs.css', 'plugin-log-designs.css')),
        javascript=(static / 'plugin-log-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="plugin-log-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#plugin-log-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/plugin-log-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
