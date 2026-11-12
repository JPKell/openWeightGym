"""Render three Schedules proposals with portable assets and browser-local sample data."""

import argparse
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    common = dict(timezone='America/Vancouver', owner='jordan', missed='skip', retention=14,
                  model='qwen3-32b', benchmark='ifeval', machine='atlas', model_ids=[], benchmarks=[])
    jobs = [
        dict(id='nightly', name='Nightly recovery', kind='backup', time='02:00', weekdays=list(range(7)),
             enabled=True, last='completed', message='Paired recovery set saved.', at='2026-11-11T02:00'),
        dict(id='coverage', name='Fill benchmark gaps', kind='outstanding', time='03:00', weekdays=list(range(5)),
             enabled=True, last='deferred', message='Preferred node atlas is asleep. Waiting for it to become available.', at='2026-11-11T03:00'),
        dict(id='quality', name='Qwen quality check', kind='benchmark', time='06:00', weekdays=list(range(5)),
             enabled=True, last='completed', message='IFEval completed for Qwen3 32B on atlas.', at='2026-11-11T06:00'),
        dict(id='regression', name='Llama regression', kind='benchmark', time='22:00', weekdays=[1, 3],
             enabled=True, model='llama-3.3-70b', benchmark='mmlu', machine='orion', last='failed',
             message='Benchmark failed: the worker connection closed. Open Runs to inspect the submitted work.', at='2026-11-10T22:00'),
        dict(id='weekend', name='Weekend model upkeep', kind='outstanding', time='04:00', weekdays=[5, 6],
             enabled=True, machine='orion', last='completed', message='One outdated benchmark completed.', at='2026-11-08T04:00'),
        dict(id='weekly', name='Weekly recovery', kind='backup', time='01:30', weekdays=[6], enabled=False,
             retention=4, missed='latest', last='completed', message='Paired recovery set saved.', at='2026-11-08T01:30'),
    ]
    return dict(now='2026-11-11T10:00', jobs=[common | job for job in jobs])


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('schedule_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'schedules.css')),
        javascript=(static / 'schedule-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="schedule-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#schedule-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/schedule-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
