"""Render three Usage dashboard proposals as one standalone page, with sample data and the live chart script."""

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from weightroom import display  # noqa: E402

DIMENSIONS = ('user', 'group', 'model', 'source', 'outcome', 'level')
END = date(2026, 11, 11)
DAYS = 30
RATES = {'input': 0.6, 'output': 2.4}  # mock USD per million tokens
# Each sample model: its source, input and output tokens of a typical request, and what a provider charges per million.
MODELS = {
    'qwen3-32b': ('local', 2600, 640, None), 'gemma-4-27b': ('local', 1900, 520, None),
    'gpt-oss-20b': ('local', 3100, 900, None), 'smollm-135m': ('local', 420, 90, None),
    'mistral-large': ('hosted', 3400, 820, (2.0, 6.0)), 'deepseek-v3': ('hosted', 5200, 1100, (0.27, 1.1)),
}
# Each sample person: their group, how much they use, and the models they reach for.
PEOPLE = {
    'jordan': ('admins', 1.0, ['qwen3-32b', 'mistral-large', 'gemma-4-27b', 'smollm-135m']),
    'maya': ('research', 1.4, ['qwen3-32b', 'deepseek-v3', 'gpt-oss-20b']),
    'priya': ('research', 0.9, ['gpt-oss-20b', 'gemma-4-27b', 'deepseek-v3']),
    'sam': ('product', 0.6, ['gemma-4-27b', 'mistral-large']),
    'nightly-evals': ('automation', 2.2, ['smollm-135m', 'qwen3-32b', 'gpt-oss-20b', 'gemma-4-27b']),
    'former-intern': ('', 0.25, ['qwen3-32b']),  # a deleted account: its older requests have no recorded group
}
OUTCOMES = [('done', .93), ('failed', .025), ('refused', .02), ('hung up', .015), ('stopped', .01)]
LEVEL = {'done': 'info', 'stopped': 'info', 'hung up': 'warning', 'refused': 'warning', 'failed': 'error'}


def empty() -> dict:
    """One total, day or breakdown row at zero, as loadcoach/usage.py starts them."""
    return {**dict.fromkeys(('requests', 'input_tokens', 'output_tokens', 'total_tokens', 'missing_usage', 'inferred_group',
                             'fallback_input_tokens', 'fallback_output_tokens'), 0),
            **{f'{source}_cost': 0.0 for source in ('recorded', 'model', 'fallback')},
            **{f'{source}_requests': 0 for source in ('recorded', 'model', 'fallback')}}


def records() -> list[dict]:
    """A month of request totals per day, person, model and outcome, rising slowly and quieter at weekends."""
    chance = random.Random(11)
    rows = []
    for offset in range(DAYS):
        day = END - timedelta(days=DAYS - 1 - offset)
        pace = (0.45 if day.weekday() >= 5 else 1.0) * (0.75 + offset / DAYS * 0.5)
        for user, (group, appetite, models) in PEOPLE.items():
            if user == 'former-intern' and offset > 12:
                continue
            for rank, model in enumerate(models):
                source, prompt, reply, price = MODELS[model]
                requests = round(chance.uniform(6, 22) * appetite * pace / (rank + 1))
                for outcome, share in OUTCOMES:
                    count = round(requests * share * chance.uniform(.6, 1.4)) if outcome != 'done' else round(requests * share)
                    if not count:
                        continue
                    done = outcome == 'done'
                    inputs = round(count * prompt * chance.uniform(.7, 1.3))
                    outputs = round(count * reply * chance.uniform(.6, 1.4)) if done else round(count * reply * .15)
                    missing = count if outcome == 'hung up' else 0
                    rows.append({'date': day.isoformat(), 'user': user, 'group': group, 'model': model, 'source': source,
                                 'outcome': outcome, 'level': LEVEL[outcome], 'requests': count, 'input_tokens': inputs,
                                 'output_tokens': outputs, 'missing_usage': missing, 'price': price,
                                 # Hosted providers report most charges; the rest are priced from the model's rates.
                                 'reported': source == 'hosted' and model == 'mistral-large'})
    return rows


def report(owner: str | None = None, filters: dict | None = None, group_by: str = 'model', keep_days: int = 90) -> dict:
    """The report loadcoach/usage.py would give for these records, with `everyone`, `keep_days` and `oldest` as Weightroom adds them."""
    filters = {key: '' for key in DIMENSIONS} | (filters or {}) | ({'user': owner} if owner else {})
    start = END - timedelta(days=DAYS - 1)
    daily = {(start + timedelta(days=index)).isoformat(): empty() for index in range(DAYS)}
    totals, breakdowns, user_models = empty(), {key: {} for key in DIMENSIONS}, {}
    choices = {key: set() for key in DIMENSIONS}
    user_groups: dict[str, set] = {}
    for row in records():
        group = row['group'] or '(unknown)'
        identity = {**{key: row[key] for key in DIMENSIONS}, 'group': group}
        if owner and row['user'] != owner:
            continue
        user_groups.setdefault(row['user'], set()).add(group)
        for key, value in identity.items():
            choices[key].add(value)
        if any(wanted and identity[key] != wanted for key, wanted in filters.items()):
            continue
        count, inputs, outputs = row['requests'], row['input_tokens'], row['output_tokens']
        values = {'requests': count, 'input_tokens': inputs, 'output_tokens': outputs, 'total_tokens': inputs + outputs,
                  'missing_usage': row['missing_usage'], 'inferred_group': count if not row['group'] else 0}
        if row['reported']:
            values.update(recorded_cost=(inputs * 2.0 + outputs * 6.0) / 1e6, recorded_requests=count)
        elif row['price']:
            values.update(model_cost=(inputs * row['price'][0] + outputs * row['price'][1]) / 1e6, model_requests=count)
        else:
            values.update(fallback_cost=(inputs * RATES['input'] + outputs * RATES['output']) / 1e6, fallback_requests=count,
                          fallback_input_tokens=inputs, fallback_output_tokens=outputs)
        targets = [totals, daily[row['date']], *(breakdowns[key].setdefault(identity[key], empty()) for key in DIMENSIONS),
                   user_models.setdefault((row['user'], row['model']), empty())]
        for target in targets:
            for key, value in values.items():
                target[key] += value
    aggregates = [values for grouped in breakdowns.values() for values in grouped.values()]
    for values in (totals, *daily.values(), *aggregates, *user_models.values()):
        for side, rate in RATES.items():
            values[f'{side}_cost'] = values[f'{side}_tokens'] * rate / 1e6
        values['total_cost'] = values['input_cost'] + values['output_cost']
        values['effective_cost'] = values['recorded_cost'] + values['model_cost'] + values['fallback_cost']
    grouped = {key: [{'name': name, **values} for name, values in sorted(rows.items(), key=lambda item: (-item[1]['total_tokens'], item[0]))]
               for key, rows in breakdowns.items()}
    return {
        'start': start.isoformat(), 'end': END.isoformat(), 'timezone': 'America/Vancouver', 'bucket': 'day', 'hourly': [],
        'group_by': group_by, 'filters': filters,
        'mock_pricing': {'currency': 'USD', 'per_tokens': 1_000_000, **{f'{side}_rate': rate for side, rate in RATES.items()}},
        'choices': {key: sorted(values) for key, values in choices.items()},
        'user_groups': {name: sorted(groups) for name, groups in user_groups.items()},
        'totals': totals, 'daily': [{'date': day, **values} for day, values in daily.items()],
        'breakdown': grouped[group_by], 'breakdowns': grouped,
        'user_models': [{'user': user, 'model': model, **values} for (user, model), values in sorted(user_models.items())],
        'oldest': (END - timedelta(days=keep_days - 1)).isoformat(), 'everyone': owner is None, 'keep_days': keep_days,
    }


SCENARIOS = {
    'everyone': ('Everyone · 30 days', lambda: report()),
    'mine': ('Your usage only', lambda: report(owner='jordan')),
    'empty': ('No matching requests', lambda: report(filters={'model': 'retired-model'})),
}


def render() -> str:
    """The proposals, with stylesheets, ECharts, the live usage and sorting scripts, and every scenario's report inlined."""
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters.update(count=display.count)
    static = ROOT / 'weightroom/static'
    return pages.get_template('usage_designs.html').render(
        css='\n'.join((static / name).read_text() for name in ('tabler.min.css', 'style.css', 'themes.css', 'usage.css', 'usage-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        javascript='\n;\n'.join((static / name).read_text() for name in ('echarts.min.js', 'sort.js', 'usage.js', 'usage-designs.js')),
        scenarios={key: (label, build()) for key, (label, build) in SCENARIOS.items()},
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/usage-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
