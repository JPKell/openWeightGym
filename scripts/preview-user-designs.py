"""Render three portable Users proposals (the overview and an account's page) with interactive sample data."""

import argparse
import math
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loadcoach.groups import ABILITIES, LEVELS, OWN_KEY  # noqa: E402  the real vocabulary, so the preview cannot drift from it

# weightroom.app adds these two; importing it would need the whole console's dependencies.
FRONT_ABILITIES = {'front-wake': 'wake compute from the first page while it is off', 'front-voice': 'use voice conversations'}

MODELS = ['qwen3-32b', 'qwen3-coder-30b', 'gemma-3-27b', 'llama-3.3-70b', 'mistral-small-3.2', 'whisper-large-v3']

GROUPS = [
    dict(name='admins', can=['use-tools', 'superuser'], priority='high', max_requests=8, max_sensitivity='restricted', models='*', tools='*'),
    dict(name='staff', can=['use-tools', 'load', 'hold', 'high', 'benchmark', 'hosted', 'see-all', 'see-summaries', 'rate-answers', 'front-voice', 'front-wake'],
         priority='normal', max_requests=4, max_sensitivity='sensitive', models='*', tools='*'),
    dict(name='researchers', can=['use-tools', 'load', 'hold', 'benchmark', 'see-summaries', 'front-voice'], priority='normal', max_requests=3,
         max_sensitivity='normal', models=['qwen3-32b', 'qwen3-coder-30b', 'gemma-3-27b', 'llama-3.3-70b'], tools=['web.search', 'files.read']),
    dict(name='apps', can=['use-tools', 'load', 'hosted'], priority='low', max_requests=6, max_sensitivity='normal', models=['qwen3-32b', 'qwen3-coder-30b', 'mistral-small-3.2', 'whisper-large-v3'], tools='*'),
    dict(name='guests', can=['use-tools', 'front-voice'], priority='low', max_requests=1, max_sensitivity='normal', models=['mistral-small-3.2'], tools=[]),
]


def daily(peak: float, phase: int, weekdays: bool = True) -> list[int]:
    """Twenty-eight days of requests with a gentle wave and quieter weekends, so the sparklines look like real use."""
    return [max(0, round(peak * (0.55 + 0.35 * math.sin((day + phase) / 3.1)) * (0.35 if weekdays and day % 7 in (5, 6) else 1))) for day in range(28)]


def key(label: str, made: str, ident: str) -> dict:
    """One API key as LoadCoach lists it: no secret, only its label, when it was made and the start of its hash."""
    return dict(label=label, created=made, id=ident)


def users() -> list[dict]:
    """Sample accounts covering each state the pages show: invited, disabled, programs with and without keys, and own settings."""
    blank = dict(add=[], remove=[], add_tools=[], remove_tools=[])
    return [
        dict(name='jpk', group='admins', enabled=True, password='set', second_step=True, sessions=2, keys=[key('laptop-cli', '2026-09-02', 'k-7f3a91')],
             own=blank, daily=daily(46, 1), tokens=[1_840_000, 512_000], spend=0.84, allowance=10.0),
        dict(name='maya.r', group='staff', enabled=True, password='set', second_step=True, sessions=1, keys=[],
             own={**blank, 'add': ['see-log'], 'priority': 'high'}, daily=daily(31, 4), tokens=[920_000, 288_000], spend=1.92, allowance=5.0),
        dict(name='priya', group='staff', enabled=True, password='set', second_step=False, sessions=3, keys=[key('notebook', '2026-10-21', 'k-19b0c4')],
             own=blank, daily=daily(54, 7), tokens=[2_410_000, 731_000], spend=4.61, allowance=5.0),
        dict(name='dev.chen', group='researchers', enabled=True, password='temporary', second_step=False, sessions=0, keys=[],
             own=blank, daily=[0] * 28, tokens=[0, 0], spend=0.0, allowance=0.0),
        dict(name='lena.k', group='researchers', enabled=True, password='set', second_step=True, sessions=1, keys=[],
             own={**blank, 'remove': ['hold'], 'add_tools': ['code.run'], 'max_requests': 2}, daily=daily(18, 10), tokens=[640_000, 151_000], spend=0.0, allowance=0.0),
        dict(name='sam', group='researchers', enabled=False, password='set', second_step=False, sessions=0, keys=[],
             own=blank, daily=[0] * 21 + [3, 5, 2, 0, 0, 0, 0], tokens=[38_000, 9_000], spend=0.0, allowance=0.0),
        dict(name='home-assistant', group='apps', enabled=True, password='', second_step=False, sessions=0,
             keys=[key('ha-production', '2026-06-14', 'k-a2d5e8'), key('ha-staging', '2026-08-30', 'k-c40e17')],
             own={**blank, 'models': ['mistral-small-3.2', 'whisper-large-v3']}, daily=daily(120, 2, weekdays=False), tokens=[3_920_000, 402_000], spend=0.0, allowance=0.0),
        dict(name='code-review-bot', group='apps', enabled=True, password='', second_step=False, sessions=0, keys=[key('ci', '2026-07-03', 'k-5be21f')],
             own={**blank, 'add': ['rate-answers'], 'priority': 'normal'}, daily=daily(64, 5), tokens=[5_110_000, 980_000], spend=2.35, allowance=3.0),
        dict(name='open-webui', group='apps', enabled=True, password='', second_step=False, sessions=0, keys=[key('webui', '2026-05-19', 'k-0d9c6a')],
             own=blank, daily=daily(88, 8), tokens=[2_700_000, 840_000], spend=0.0, allowance=0.0),
        dict(name='nightly-sync', group='apps', enabled=True, password='', second_step=False, sessions=0, keys=[],
             own=blank, daily=[0] * 28, tokens=[0, 0], spend=0.0, allowance=0.0),
        dict(name='visitor', group='guests', enabled=True, password='set', second_step=False, sessions=0, keys=[],
             own={**blank, 'remove_tools': ['*']}, daily=daily(4, 3), tokens=[41_000, 12_000], spend=0.0, allowance=0.0),
    ]


def render() -> str:
    """Return a portable preview; no console, compute or external assets are contacted."""
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    every = {**ABILITIES, **FRONT_ABILITIES}
    # The live account page's grouping (user_parts.html). An ability missing from it is shown under Other.
    grouping = pages.get_template('user_parts.html').module.ability_categories
    listed = {name for _, names in grouping for name in names}
    categories = [(title, [name for name in names if name in every]) for title, names in grouping]
    categories.append(('Other', sorted(set(every) - listed)))
    sample = dict(viewer='jpk', today='2026-11-11', abilities=every, own_key=sorted(OWN_KEY), levels=list(LEVELS),
                  categories=[[title, names] for title, names in categories if names], models=MODELS, groups=GROUPS, users=users())
    return pages.get_template('users_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'usage.css', 'users.css', 'user-designs.css')),
        javascript=(static / 'user-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(),
        sample=sample,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/user-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
