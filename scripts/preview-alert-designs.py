"""Render three portable Alerts proposals with browser-local sample episodes."""

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    def at(clock):
        return datetime.fromisoformat('2026-11-14T' + clock + ':00-08:00').timestamp()

    common = dict(state='active', manual=False, first_at=at('09:10'), last_at=at('10:42'),
                  checked_at=at('10:42'), resolved_at=None, recovery='',
                  acknowledged_at=None, acknowledged_by=None)
    episodes = [
        dict(id='gpu-atlas', title='GPU temperature above threshold', source='GPU temperature',
             explanation='atlas · GPU 0 reports 89°C against the configured 85°C threshold. The condition remains open until a fresh numeric reading falls below the threshold.'),
        dict(id='service-orion', title='LoadCoach service failed', source='services',
             explanation='orion · loadcoach.service reports ActiveState=failed. Inspect the service before restarting; an available source is not proof of a healthy service.'),
        dict(id='memory-atlas', title='Model worker killed by memory pressure', source='memory kills', manual=True,
             explanation='atlas · systemd recorded Result=oom-kill for a model worker invocation. Review this incident explicitly; acknowledgement does not complete the review.'),
        dict(id='spend-provider', title='Provider spending ceiling reached', source='spending',
             acknowledged_at=at('10:20'), acknowledged_by='jordan',
             explanation='The Research provider account has $52.40 in recorded costs over 24 hours against its $50.00 ceiling. In-flight requests and unknown costs are excluded.'),
        dict(id='model-qwen', title='Repeated model failures', source='model failures',
             acknowledged_at=at('10:22'), acknowledged_by='sam',
             explanation='Qwen3 32B · atlas · research role is excluded for repeated failures. Successful recovery, an operator reset or revision retirement closes this episode.'),
        dict(id='gpu-orion', title='GPU reading unavailable', source='GPU temperature', state='unknown',
             last_at=at('10:30'),
             explanation='orion · GPU 1 last reported 87°C. The node is now unavailable. The earlier heat episode stays open because missing readings cannot establish recovery.'),
    ]
    closed = [
        ('closed-heat', 'GPU temperature returned below threshold', 'GPU temperature', False,
         'atlas · GPU 1 previously exceeded 85°C.', 'Fresh reading: 76°C, below the configured 85°C threshold.'),
        ('closed-service', 'Spotter service recovered', 'services', False,
         'atlas · spotter.service previously reported a failed state.', 'The service now reports ActiveState=active.'),
        ('closed-memory', 'Memory-kill incident reviewed', 'memory kills', True,
         'orion · a model worker was killed by memory pressure.', 'Reviewed by jordan; this records review, not proof of memory recovery.'),
        ('closed-spend', 'Recorded spending below ceiling', 'spending', False,
         'Research provider account previously reached its 24-hour ceiling.', 'The recorded rolling 24-hour total is now below the configured ceiling.'),
        ('closed-model', 'Model exclusion reset', 'model failures', False,
         'Llama 3.3 70B was excluded from the draft role.', 'An operator reset the exclusion for this model revision.'),
        ('closed-observation', 'Observation forwarding recovered', 'observability', False,
         'atlas · observation forwarding was unavailable.', 'Fresh evidence confirms forwarding is available again.'),
    ]
    for index, (identifier, title, source, manual, explanation, recovery) in enumerate(closed):
        episodes.append(dict(id=identifier, title=title, source=source, manual=manual,
                             explanation=explanation, recovery=recovery,
                             state='reviewed' if manual else 'recovered', first_at=at('07:00'),
                             last_at=at('08:00'), checked_at=at(f'09:{40-index:02}'),
                             resolved_at=at(f'09:{40-index:02}'),
                             acknowledged_at=at('08:10') if manual else None,
                             acknowledged_by='jordan' if manual else None))
    names = ('services', 'model failures', 'GPU temperature', 'spending',
             'memory kills', 'kernel journal', 'observability')
    return dict(now=at('10:42'), episodes=[common | row for row in episodes],
                sources=[dict(source=name, available=name != 'GPU temperature', checked_at=at('10:42'))
                         for name in names])


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    # The live timestamp filter keeps absent values blank; the browser localizes sample facts.
    def timestamp(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime('%d %b %Y %H:%M UTC') if value else ''
    pages.filters['timestamp'] = timestamp
    static = ROOT / 'weightroom/static'
    return pages.get_template('alert_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'alert-designs.css')),
        javascript=(static / 'alert-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="alert-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#alert-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/alert-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
