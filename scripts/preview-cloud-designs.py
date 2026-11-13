"""Render three Cloud API proposals with portable, browser-local sample accounts."""

import argparse
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    endpoints = [
        ('OpenRouter', 'https://openrouter.ai/api/v1', 'OR'),
        ('OpenAI', 'https://api.openai.com/v1', 'OA'),
        ('Together', 'https://api.together.xyz/v1', 'TO'),
        ('Groq', 'https://api.groq.com/openai/v1', 'GQ'),
        ('Fireworks', 'https://api.fireworks.ai/inference/v1', 'FW'),
    ]
    accounts = []
    for i, (provider, label, spent, ceiling, key) in enumerate([
        (0, 'Production', 18.42, 50, True), (0, 'Research', 7.86, 20, True),
        (1, 'Team workspace', 12.15, 40, True), (2, 'Evaluation', 0, 15, False),
        (3, 'Low latency', 4.20, 10, True), (1, 'Personal', 5, 5, True),
        (4, 'Batch jobs', 3.16, 25, True), (2, 'Development', .92, 10, True),
        (0, 'Sandbox', .64, None, True), (4, 'Research lab', 2.18, 15, True),
    ]):
        title, url, mark = endpoints[provider]
        name = title.lower() + '-' + label.lower().replace(' ', '-')
        accounts.append(dict(id=name, name=name, label=label, provider=title, mark=mark,
            url=url, spent=spent, ceiling=ceiling, key=key,
            models={f'{name}-fast': {'theirs': 'sample/fast', 'in': .15, 'out': .60},
                    f'{name}-reasoning': {'theirs': 'sample/reasoning', 'in': 2, 'out': 8}} if key else {}))
    return dict(accounts=accounts)


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('cloud_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'cloud-apis.css', 'cloud-designs.css')),
        javascript='\n;\n'.join((static / name).read_text() for name in ('console.js', 'cloud-designs.js')),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="cloud-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#cloud-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/cloud-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
