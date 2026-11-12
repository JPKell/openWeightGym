"""Render three paired fingerprint directory/detail proposals with local sample data."""

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def samples() -> list[dict]:
    records = []
    models = [('qwen3-32b', 'Qwen3-32B', 'Q6_K', 26.9),
              ('llama-3.3-70b', 'Llama-3.3-70B-Instruct', 'Q4_K_M', 42.5),
              ('gemma-3-27b', 'gemma-3-27b-it', 'Q5_K_M', 19.3),
              ('qwen3-coder-30b', 'Qwen3-Coder-30B-A3B', 'Q8_0', 32.5),
              ('mistral-small-3.2', 'Mistral-Small-3.2-24B', 'Q6_K', 19.8),
              ('deepseek-r1-14b', 'DeepSeek-R1-Distill-Qwen-14B', 'Q8_0', 15.7),
              ('nomic-embed', 'nomic-embed-text-v1.5', 'F16', .274),
              ('qwen2.5-vl-7b', 'Qwen2.5-VL-7B-Instruct', 'Q8_0', 8.1)]
    for version in range(3):
        for index, (name, filename, quant, gib) in enumerate(models):
            number = version * len(models) + index
            path = f'/srv/models/{filename}-{quant}.gguf'
            context = [32768, 16384, 8192][version]
            files = {'model': dict(path=path, sha256=hashlib.sha256(filename.encode()).hexdigest(),
                                   size=round(gib * 1024**3), says=dict(name=filename, architecture=name.split('-')[0],
                                   quantization=quant, layers=64 if index == 0 else 48,
                                   trained_context=40960 if index == 0 else 131072))}
            flags = {'model': path, 'ctx-size': context, 'batch-size': 2048, 'ubatch-size': 512,
                     'cache-type-k': 'q8_0', 'cache-type-v': 'q8_0', 'flash-attn': 'on', 'temp': 0.6,
                     'top-p': 0.95, 'seed': 0}
            if index in (2, 7):
                projector = f'/srv/models/mmproj-{filename}-F16.gguf'
                files['mmproj'] = dict(path=projector, sha256=hashlib.sha256(projector.encode()).hexdigest(),
                                       size=851443712, says=dict(architecture='clip', quantization='F16'))
                flags['mmproj'] = projector
            records.append(dict(fingerprint=hashlib.sha256(f'{name}:{context}'.encode()).hexdigest(),
                                first=(datetime(2026, 11, 12, 16, 42, tzinfo=timezone.utc) - timedelta(hours=number * 19)).isoformat(),
                                model=name, files=files,
                                settings=dict(gpus=[0, 1] if index == 1 else [0], alone=False, flags=flags)))
    return records


def render() -> str:
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    # The empty DOM templates share the live components; values are filled by the preview script.
    pages.filters.update(timestamp=str, size=str, count=str,
                         shown=lambda value: value if isinstance(value, str) else json.dumps(value))
    static = ROOT / 'weightroom/static'
    return pages.get_template('fingerprint_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'fingerprint.css', 'fingerprint-designs.css')),
        javascript=(static / 'fingerprint-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), sample=samples())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/fingerprint-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
