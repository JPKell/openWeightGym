"""Render three designs of /voice/desktop with the real console frame and setup content."""

import argparse
import re
import sys
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def render(base_url='http://10.77.10.42:8440'):
    """Keep the preview offline; explicit navigation uses the requested console origin."""
    from weightroom.navigation import navigation

    origin = urlsplit(base_url)
    if origin.scheme not in ('http', 'https') or not origin.netloc or origin.username or origin.password:
        raise ValueError('Use an HTTP or HTTPS console URL without credentials.')
    base_url = f'{origin.scheme}://{origin.netloc}'
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    html = pages.get_template('voice_download_designs.html').render(
        person={'name':'Preview', 'me':{}}, strip=False,
        nav=navigation('/voice/desktop', ['front-voice', 'manage-models', 'manage-settings'], False),
        design_css=(static / 'voice-download-designs.css').read_text(),
        design_javascript=(static / 'voice-download-designs.js').read_text(),
        download_css=(static / 'voice-download.css').read_text(),
        download_javascript=(static / 'voice-download.js').read_text(),
    )
    # Use frame.html itself, with local assets inlined and backend behavior omitted.
    def script(match):
        name = match[1].split('?')[0].rsplit('/', 1)[-1]
        if name not in ('themes.js', 'navigation.js'):
            return ''
        source = (static / name).read_text()
        if name == 'navigation.js':
            source = "document.addEventListener('DOMContentLoaded', () => {\n" + source + '\n});'
        return '<script>' + source + '</script>'

    html = re.sub(r'<script src="(/static/[^\"]+)"[^>]*></script>', script, html)
    html = re.sub(r'<link rel="stylesheet" href="/static/([^\"]+)"[^>]*>',
                  lambda match: '<style>' + (static / match[1].split('?')[0]).read_text() + '</style>', html)
    html = re.sub(r'<link\b[^>]*>', '', html)
    html = re.sub(r'<form method="post" action="/sign-out".*?</form>', '', html, flags=re.S)
    html = html.replace('data-theme-user="Preview"', 'data-theme-user=""')
    svg = re.sub(r'\bid="([^\"]+)"', r'id="vdl-icon-\1"', (static / 'icons.svg').read_text())
    html = html.replace('<body>', '<body><div hidden>' + svg + '</div>') if '<body>' in html else html.replace('<main ', '<div hidden>' + svg + '</div><main ', 1)
    html = html.replace('/static/icons.svg?v=3#', '#vdl-icon-')
    return re.sub(r'href="(/[^\"]*)"', lambda match: 'href="' + escape(base_url, quote=True) + match[1] + '"', html)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/voice-download-designs/index.html')
    parser.add_argument('--base-url', default='http://10.77.10.42:8440', help='Console origin for download/account/browser links')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args.base_url))
    print(args.output.resolve())
