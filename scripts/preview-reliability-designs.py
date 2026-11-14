#!/usr/bin/env python3
"""Render three portable Reliability proposals with browser-local sample evidence."""

import argparse
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from weightroom.display import timestamp  # noqa: E402


def sample_data():
    """Illustrative observations, not measured claims about the named models."""
    profiles = [
        ('qwen3-32b', 'chat', 47, 1, 2, 0, 'eligible', False, True),
        ('llama-3.3-70b', 'reasoning', 35, 8, 5, 2, 'recovery required', True, True),
        ('qwen3-8b', 'fast', 48, 1, 0, 1, 'eligible', False, True),
        ('gemma-3-27b', 'chat', 43, 2, 4, 1, 'cooldown', False, True),
        ('deepseek-r1-14b', 'reasoning', 28, 0, 1, 0, 'eligible', False, True),
        ('mistral-small-24b', 'tools', 36, 0, 0, 0, 'eligible', False, True),
        ('qwen3-32b', 'tools', 30, 9, 4, 1, 'eligible', True, True),
        ('qwen2.5-vl-7b', '', 19, 0, 0, 0, 'eligible', False, True),
        ('llama-3.3-70b', 'reasoning', 16, 7, 3, 1, 'recovery required', False, False),
        ('qwen3-32b', 'chat', 32, 1, 0, 0, 'eligible', False, False),
    ]
    rows, quality, identities = [], [], {}
    for i, (model, role, success, error, timeout, empty, state, regression, current) in enumerate(profiles):
        fingerprint = identities.setdefault((model, current), f'{i + 1:08x}a78be04dc931b2954f06172aa4c13af58cbed658ac08c9e3f584019a')
        counts = dict(success=success, error=error, timeout=timeout, empty=empty)
        faults, total = error + timeout + empty, sum(counts.values())
        positive, negative = max(0, success // 5), faults // 2
        feedback = (positive - negative) / (positive + negative) if positive + negative else 0
        factor = round(min(1.1, max(.5, 1 - .5 * faults / total + .1 * feedback)), 3)
        rows.append(dict(id=f'candidate-{i}', model=model, role=role, fingerprint=fingerprint,
                         current=current, counts=counts, positive=positive, negative=negative,
                         factor=factor, state=state, regression=regression, last_request=8420 - i * 13,
                         last_at=f'13 Nov 2026, {10 - i // 3:02}:{42 - i * 3:02} PST',
                         ignored={'cancelled': 2, 'admission refusal': 1} if i % 3 == 0 else {},
                         may_manage=current, reason=f'{faults}/{total} recent model faults; preference ×{factor:.3f}' +
                         ('; excluded from this role until a successful recovery check or reset' if state != 'eligible' else ''),
                         own_counts={k: v // 3 for k, v in counts.items()}, own_ignored={'cancelled': 1} if i % 3 == 0 else {}))
        # Lifetime feedback is deliberately distinct from the seven-day report.
        if not any(q['fingerprint'] == fingerprint for q in quality) and role != 'tools':
            quality.append(dict(model=model, fingerprint=fingerprint, current=current,
                                good=success * 3, bad=faults + 1,
                                explanations=[dict(request=8420 - i * 13, value=-1 if faults else 1,
                                                   rater='jordan', explanation='The answer stopped before the final step.' if faults else 'Clear answer with useful examples.')]))
    return dict(rows=rows, quality=quality)


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters['timestamp'] = timestamp
    static = ROOT / 'weightroom/static'
    return pages.get_template('reliability_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'reliability.css', 'reliability-designs.css')),
        javascript=(static / 'reliability-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/reliability-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
