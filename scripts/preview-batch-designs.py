"""Render three Batches proposals with self-contained assets and browser-local sample data."""

import argparse
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weightroom.display import timestamp

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    """Use the Batch API's actual fields; labels are optional submission metadata."""
    rows = [
        ('Support ticket classification', 'jordan', 'in_progress', 1200, 846, 12, 'chat/completions', 0),
        ('Knowledge base embeddings', 'jordan', 'in_progress', 2400, 612, 0, 'embeddings', 1),
        ('Product descriptions', 'pat', 'in_progress', 500, 110, 3, 'chat/completions', 2),
        ('Invoice field extraction', 'jordan', 'completed', 800, 776, 24, 'chat/completions', 3),
        ('Research archive', 'cam', 'completed', 1600, 1600, 0, 'embeddings', 4),
        ('Weekly customer summaries', 'jordan', 'completed', 320, 320, 0, 'chat/completions', 5),
        ('Catalogue image captions', 'pat', 'cancelling', 200, 78, 2, 'chat/completions', 6),
        ('Document index refresh', 'jordan', 'paused', 900, 0, 0, 'embeddings', 7),
        ('Campaign copy variations', 'jordan', 'cancelled', 600, 140, 0, 'completions', 8),
        ('Audio library descriptions', 'cam', 'completed', 180, 178, 2, 'chat/completions', 9),
        ('Search relevance labels', 'jordan', 'completed', 1000, 1000, 0, 'chat/completions', 10),
        ('Legacy content migration', 'jordan', 'cancelled', 400, 64, 1, 'embeddings', 11),
    ]
    now = int(datetime.fromisoformat('2026-11-13T10:30:00-08:00').timestamp())
    batches = []
    for i, (name, user, status, total, completed, failed, endpoint, hours) in enumerate(rows, 1):
        ended = status in ('completed', 'cancelled')
        batches.append(dict(
            id=f'batch_7b4e{i:04d}a91c', user=user, status=status, endpoint='/v1/' + endpoint,
            created_at=now - hours * 3600 - 1800,
            completed_at=now - hours * 1800 if status == 'completed' else None,
            cancelled_at=now - hours * 1800 if status == 'cancelled' else None,
            request_counts=dict(total=total, completed=completed, failed=failed),
            input_file_id=f'file_input_{i:03d}',
            output_file_id=f'file_answers_{i:03d}' if ended and completed else None,
            error_file_id=f'file_errors_{i:03d}' if ended and failed else None,
            completion_window='24h', metadata=dict(name=name),
        ))
    return dict(batches=batches, viewer='jordan', can_stop=True)


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters['timestamp'] = timestamp
    static = ROOT / 'weightroom/static'
    return pages.get_template('batch_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'batches.css', 'batch-designs.css')),
        javascript='\n;\n'.join((static / name).read_text() for name in ('console.js', 'batch-designs.js')),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="batch-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#batch-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/batch-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
