"""Render three self-contained AI Logs proposals with deterministic sample events."""

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weightroom.display import timestamp

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    now = datetime(2026, 11, 13, 10, 42, tzinfo=ZoneInfo('America/Vancouver'))
    events = []

    def add(at, kind, what, user, node, model=None, trace=None, **detail):
        event = dict(id=84001 + len(events), at=at.isoformat(timespec='milliseconds'),
                     kind=kind, what=what, user=user, caller='web' if user != 'scheduler' else 'schedule',
                     machine=node, model=model, trace=trace, level='normal', **detail)
        events.append(event)
        return event['id']

    for i in range(26):
        at = now - timedelta(seconds=18 + i * 103)
        user = ['jordan', 'sam', 'alex', 'scheduler'][i % 4]
        node = ['atlas', 'orion', 'atlas', 'front'][i % 4]
        model = ['qwen3-32b', 'llama-3.3-70b', 'qwen3-8b', 'cloud-chat'][i % 4]
        trace = f'chat-{7310 - i}'
        request = add(at - timedelta(seconds=5), 'request', 'asked', user, node, model, trace,
                      input_tokens=512 + i * 71, stream=True)
        if i % 6 == 0:
            add(at - timedelta(seconds=4.8), 'routing', 'selected', user, 'front', model, trace,
                of=request, reason='Available node with sufficient capacity', target=node)
            add(at, 'request', 'failed', user, node, model, trace, of=request,
                error='CUDA out of memory while allocating the KV cache.', code='out_of_memory',
                duration=5.0, context=32768)
        elif i % 7 == 0:
            add(at, 'request', 'denied', user, node, model, trace, of=request,
                error='The user token allowance has been reached.', code='token_allowance', duration=0.02)
        else:
            if i % 3 == 0:
                add(at - timedelta(seconds=2), 'tool', 'done', user, node, model, trace,
                    of=request, tool='knowledge.search', matches=4, duration=0.18)
            add(at, 'request', 'done', user, node, model, trace, of=request,
                duration=round(0.62 + i * 0.17, 2), input_tokens=512 + i * 71,
                output_tokens=128 + i * 13, finish_reason='stop')
    add(now - timedelta(minutes=2), 'model', 'loaded', 'scheduler', 'orion', 'llama-3.3-70b',
        duration=12.4, engine='llama.cpp', context=16384)
    add(now - timedelta(minutes=4), 'user', 'signed-in', 'alex', 'front', method='password')
    add(now - timedelta(minutes=8), 'backup', 'created', 'scheduler', 'atlas', archive='atlas-2026-11-13', files=6)
    add(now - timedelta(minutes=12), 'model', 'unloaded', 'jordan', 'atlas', 'qwen3-8b', reason='Idle timeout')
    tool_request = add(now - timedelta(minutes=14, seconds=10), 'request', 'asked', 'sam', 'orion',
                       'llama-3.3-70b', 'chat-7280', input_tokens=1824)
    add(now - timedelta(minutes=14), 'tool', 'failed', 'sam', 'orion', 'llama-3.3-70b', 'chat-7280',
        of=tool_request, error='Knowledge source did not respond within 10 seconds.',
        code='tool_timeout', tool='knowledge.search')
    add(now - timedelta(minutes=14) + timedelta(milliseconds=200), 'request', 'failed', 'sam', 'orion',
        'llama-3.3-70b', 'chat-7280', of=tool_request, duration=10.2,
        error='Required knowledge tool timed out.', code='tool_timeout')
    add(now - timedelta(minutes=19), 'model', 'failed', 'scheduler', 'orion', 'llama-3.3-70b',
        error='Model file is unavailable on this node.', code='file_missing', file='models/llama-3.3-70b.gguf')
    add(now - timedelta(minutes=25), 'user', 'denied', 'alex', 'front',
        error='This account cannot change model settings.', code='permission_denied')
    return dict(now=now.isoformat(), timezone='America/Vancouver', events=events,
                user_groups={'jordan': ['admins'], 'sam': ['research'], 'alex': ['research'], 'scheduler': ['automation']})


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters['timestamp'] = timestamp
    static = ROOT / 'weightroom/static'
    data = sample_data()
    return pages.get_template('log_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'log.css', 'log-designs.css')),
        javascript=(static / 'log-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=data,
        nodes=sorted({event['machine'] for event in data['events']}),
        groups=sorted({group for groups in data['user_groups'].values() for group in groups}),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="log-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#log-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/log-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
