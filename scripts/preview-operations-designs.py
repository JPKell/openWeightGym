"""Render three sample-only Status/Telemetry proposals as a self-contained HTML preview."""

import argparse
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def render(telemetry=False):
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    name = 'telemetry-design' if telemetry else 'operations-designs'
    context = {}
    if telemetry:
        from weightroom.navigation import navigation
        abilities = ['manage-models', 'manage-settings', 'manage-users', 'manage-groups', 'front-manage-plugins']
        context = dict(person=SimpleNamespace(name='Preview', me={}, can=abilities),
                       nav=navigation('/telemetry', abilities, True), strip=False)
    output = pages.get_template('telemetry_design.html' if telemetry else 'operations_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', f'{name}.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        charts_javascript=(static / 'echarts.min.js').read_text(),
        javascript=(static / f'{name}.js').read_text(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="ops-icon-\1"', (static / 'icons.svg').read_text()),
        **context,
    ).replace('/static/icons.svg?v=3#', '#ops-icon-')
    if telemetry:
        # Bundle the real shell's appearance and navigation without live service scripts.
        def script(match):
            asset = match.group(1).split('?')[0]
            if asset == 'themes.js':
                return '<script>' + (static / asset).read_text() + '</script>'
            if asset == 'navigation.js':
                return '<script>document.addEventListener("DOMContentLoaded", () => {' + (static / asset).read_text() + '});</script>'
            return ''
        output = re.sub(r'<script src="/static/([^"]+)"[^>]*></script>', script, output)
        output = re.sub(r'<link[^>]+href="/static/[^"]+"[^>]*>', '', output)
        output = output.replace('data-theme-user="Preview"', 'data-theme-user=""')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--telemetry', action='store_true', help='Render the refined Telemetry workspace')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    args.output = args.output or ROOT / 'build/operations-designs' / ('telemetry.html' if args.telemetry else 'index.html')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(telemetry=args.telemetry))
    print(args.output.resolve())
