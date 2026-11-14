"""Render three self-contained System logs proposals with browser-local sample data."""

import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    now = datetime(2026, 11, 13, 14, 32, 18, tzinfo=ZoneInfo('America/Vancouver'))
    messages = [
        ('info', 'Request completed · 328 tokens in 4.82 s · 68.0 tok/s'),
        ('debug', 'Scheduler checked queue: 0 waiting, 1 active worker'),
        ('info', 'Loaded qwen3-32b · context 32,768 · GPU memory 23.4 GiB'),
        ('warning', 'GPU memory headroom below configured limit: 1.2 GiB available'),
        ('info', 'Health check passed · worker ready to accept requests'),
        ('error', 'Worker connection closed during generation · request 8041'),
        ('info', 'Retrying request 8041 on the next available worker'),
        ('info', 'Request accepted · model qwen3-32b · request 8041'),
        ('debug', 'KV cache allocated: 4,096 MiB · type q8_0'),
        ('warning', 'Queue wait exceeded 10 s · 3 requests waiting'),
        ('info', 'Model unloaded after idle timeout · released 23.4 GiB'),
        ('info', 'Settings reloaded · configuration is valid'),
        ('error', 'Model load failed: insufficient device memory · qwen3-32b'),
        ('info', 'Starting worker · llama.cpp · port 8091'),
        ('unclassified', 'llama_model_loader: loaded metadata with 42 key-value pairs and 771 tensors'),
        ('info', 'Request completed · 612 tokens in 8.96 s · 68.3 tok/s'),
    ]
    logs = []
    sources = ['loadcoach', 'kernel', 'mirrorwall', 'soundroom', 'weightroom']
    for node_index, node in enumerate(['atlas', 'orion', 'front']):
        for i in range(168):
            source = 'loadcoach' if i % 5 < 3 else sources[1 + (i // 5) % 4]
            level, message = messages[i % len(messages)]
            if source == 'kernel':
                level, message = [('info', 'NVRM: GPU 0000:01:00.0 initialized'),
                                  ('warning', 'nvme nvme0: I/O latency above expected threshold'),
                                  ('unclassified', 'audit: type=1130 service start pid=1')][i % 3]
            elif source == 'mirrorwall':
                message = f'Image worker · {message}'
            elif source == 'soundroom':
                message = f'Audio worker · {message}'
            elif source == 'weightroom':
                level, message = 'info', 'GET /status · 200 OK · 18 ms'
            at = now - timedelta(seconds=i * 19 + node_index * 3)
            raw = f'{at.isoformat()} {node} {source}[{4210 + node_index}]: '
            raw += (f'{level.upper()} ' if level != 'unclassified' else '') + message
            if level == 'error':
                raw += '\nTraceback (most recent call last):\n  File "worker.py", line 214, in generate\n    await worker.receive()\nConnectionError: worker connection closed' if 'connection' in message else '\nCUDA error: out of memory\nRequested 24576 MiB; available 23218 MiB.'
            logs.append(dict(id=f'{node}-{i}', at=at.isoformat(), epoch=int(at.timestamp()),
                             node=node, source=source, level=level, message=message, raw=raw))
    return dict(now=now.isoformat(), timezone='America/Vancouver', logs=logs)


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('system_logs_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'system-logs.css', 'system-logs-designs.css')),
        javascript=(static / 'system-logs-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        nodes=['atlas', 'orion', 'front'], models=['qwen3-32b', 'llama-3.3-70b'],
        log_text='',
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/system-logs-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
