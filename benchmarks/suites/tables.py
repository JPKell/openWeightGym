"""Seeded table queries with exact integer keys; no generated code is executed."""

import csv
import io

from .. import quality

SEED = 20261015
OUTPUTS = 32768
SAMPLING = {'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
            'presence_penalty':0.0,'frequency_penalty':0.0}
CATEGORIES = ('filter', 'group', 'rank', 'join', 'dirty')
DEFINITION = quality.definition('tables', 'Table reasoning',
    'Filter, group, rank and join records, including explicitly defined dirty data.',
    'Forty fixed-seed cases with 136 independent scored checks. Eight cases cover each of filters, group '
    'totals, ranks, joins and dirty data. Half use CSV and half Markdown; tables have 10, 20 '
    'or 30 physical rows. Exact integer and ordered-list keys are computed from those same '
    'records. Group and rank answers derived from scored fields remain auxiliary whole-case checks. '
    'Tie-breaking, duplicate rows, blank cells and thousands separators are explicit. '
    'Complete unpadded questions use temperature 1 and explicit neutral native filters, with up to '
    '32,768 output tokens where context permits; real-model qualification is pending.',
    [quality.FORMAT, ('auxiliary_accuracy', 'Derived fields correct', '%'),
     ('csv_accuracy', 'CSV checks correct', '%'),
     ('markdown_accuracy', 'Markdown checks correct', '%'),
     *[(kind + '_accuracy', kind.title() + ' checks correct', '%') for kind in CATEGORIES]])
PROTOCOL = {**quality.PROTOCOL, 'version': 8, 'dataset': 'tables-v6', 'seed': SEED,
            'cases': 40, 'checks': 136, 'grader_version': 4,
            'rows': [10, 20, 30], 'formats': ['csv', 'markdown'],
            'outputs': OUTPUTS, 'temperature':1.0, 'generation_seed':42,
            'sampling': {'temperature':1.0, **SAMPLING}}


def render(rows, columns, format):
    """Render the generated table in the format selected by the case."""
    if format == 'csv':
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().rstrip('\n')
    def line(values):
        # Generated cells contain commas but no pipes/newlines; keep rendering data-only.
        """Render one table row using the selected delimiter and quoting rules."""
        return '| ' + ' | '.join(str(value) for value in values) + ' |'
    return '\n'.join([line(columns), line(['---'] * len(columns)),
                      *[line(row[column] for column in columns) for row in rows]])


def number(cell):
    """Parse a bounded numeric table answer for deterministic comparison."""
    return int(str(cell).replace(',', '')) if cell != '' else 0


def solve(category, rows, options):
    """Pure reference queries over the very rows rendered in the question."""
    if category == 'filter':
        chosen = [r for r in rows if r['group'] == options['group'] and r['cents'] >= options['threshold']]
        return dict(matching_rows=len(chosen), matching_units=sum(r['units'] for r in chosen),
                    matching_cents=sum(r['units'] * r['cents'] for r in chosen))
    if category == 'group':
        totals = {group: sum(r['units'] * r['cents'] for r in rows if r['group'] == group)
                  for group in options['groups']}
        return dict(totals=[totals[g] for g in options['groups']],
                    greatest_group=min(totals, key=lambda g: (-totals[g], g)),
                    groups_above=sum(total > options['threshold'] for total in totals.values()))
    if category == 'rank':
        ranked = sorted(rows, key=lambda r: (-r['units'] * r['cents'], r['id']))
        return dict(third_id=ranked[2]['id'] if len(ranked) >= 3 else None,
                    top_three=[r['id'] for r in ranked[:3]],
                    named_position=next((n for n,r in enumerate(ranked, 1) if r['id'] == options['named']), None))
    if category == 'join':
        prices = {r['product']: r['cents'] for r in options['prices']}
        selected = [r for r in rows if r['group'] == options['group']]
        revenue = lambda r: r['units'] * prices[r['product']]
        return dict(selected_cents=sum(revenue(r) for r in selected),
                    greatest_order=min(rows, key=lambda r: (-revenue(r), r['id']))['id'],
                    orders_above=sum(revenue(r) > options['threshold'] for r in rows))
    if category != 'dirty':
        raise ValueError('Unknown table query category.')
    unique = {}
    for row in rows:
        unique.setdefault(row['id'], row)
    return dict(distinct_rows=len(unique), total_cents=sum(number(r['units']) * number(r['cents']) for r in unique.values()),
                blank_rows=sum(r['units'] == '' or r['cents'] == '' for r in unique.values()))



def representative_indices(rows, target):
    """One deterministic, case-independent row policy with structural coverage."""
    anchors = set()
    if 'group' in rows[0]:
        seen_groups = {}
        for position, row in enumerate(rows):
            if seen_groups.get(row['group'], 0) < 2:
                anchors.add(position)
                seen_groups[row['group']] = seen_groups.get(row['group'], 0) + 1
    else:
        first = {}
        for position, row in enumerate(rows):
            if row['id'] in first:
                anchors.update((first[row['id']], position))
                break
            first[row['id']] = position
        for field in ('units', 'cents'):
            anchors.add(next(position for position, row in enumerate(rows) if row[field] == ''))
        anchors.add(next(position for position, row in enumerate(rows)
                         if ',' in row['units'] or ',' in row['cents']))
    assert len(anchors) <= target
    chosen = set(anchors)
    for position in range(0, len(rows), 2):
        if len(chosen) >= target:
            break
        chosen.add(position)
    for position in range(len(rows)):
        if len(chosen) >= target:
            break
        chosen.add(position)
    assert len(chosen) == target
    return sorted(chosen)


def cases(limit):
    """Build deterministic table reasoning fixtures from the case seed."""
    source = quality.Source(SEED)
    result = []
    for category in CATEGORIES:
        for index in range(8):
            serial = len(result) + 1
            prefix = f'T{serial:02}'
            count = (30, 80, 200)[(serial - 1) % 3]
            format = 'csv' if index % 2 == 0 else 'markdown'
            groups = [prefix + '-' + letter for letter in 'ABCD']
            rows = [dict(id=f'{prefix}-{n+1:03}', group=groups[n % 4],
                         units=source.random.randrange(1, 41), cents=source.random.randrange(50, 2501)) for n in range(count)]
            source.random.shuffle(rows)
            options = dict(groups=groups, group=groups[index % 4], threshold=1000)
            columns = ['id', 'group', 'units', 'cents']
            intro = f'{source.person()} is checking the ledger for {source.place()}. '
            rules = ('All quantities and prices are whole numbers. cents is a unit price in integer cents; '
                     'a row total is units*cents. Do not round. ')
            if category == 'filter':
                candidates = sorted(r['cents'] for r in rows if r['group'] == options['group'])
                options['threshold'] = candidates[len(candidates)//2]
                query = (f"Select group={options['group']} AND cents>={options['threshold']}. Return matching_rows "
                         '(count), matching_units (sum of units), matching_cents (sum of row totals), all over those selected rows.')
            elif category == 'group':
                totals = sorted(sum(r['units']*r['cents'] for r in rows if r['group']==g) for g in groups)
                options['threshold'] = (totals[1]+totals[2])//2
                query = (f'Return totals as a list of group row-total sums in this exact group order: {", ".join(groups)}. '
                         f'Return greatest_group (greatest total; tie by lexicographically smallest group), and groups_above '
                         f"(number of groups whose total is strictly greater than {options['threshold']}).")
            elif category == 'rank':
                options['named'] = rows[-1]['id']
                query = (f"Rank all rows by units*cents descending, then id ascending to break ties. Return third_id, "
                         f"top_three (three row IDs in ranked order), and named_position (one-based rank of {options['named']}).")
            elif category == 'join':
                products = [prefix + '-P' + str(n+1) for n in range(10)]
                options['prices'] = [dict(product=p, cents=source.random.randrange(100, 4001)) for p in products]
                for row in rows:
                    row.pop('cents')
                    row['product'] = source.random.choice(products)
                columns = ['id', 'group', 'product', 'units']
                options['threshold'] = 30000
                rules = ('Join Orders.product to Prices.product exactly. Prices has one row per product and every order has a price. '
                         'An order total is units multiplied by the joined price in integer cents. ')
                query = (f"Return selected_cents (sum of order totals for group={options['group']}), greatest_order "
                         '(ID with greatest total over all orders; tie by lexicographically smallest ID), '
                         f"and orders_above (count of all orders whose total is strictly greater than {options['threshold']}).")
            elif category == 'dirty':
                rows = rows[:count-count//10]
                columns = ['id', 'label', 'units', 'cents']
                for n,row in enumerate(rows):
                    row.pop('group')
                    row['label'] = f'Lot, section {n+1}'
                    row['units'] = '' if n % 7 == 0 else f'{source.random.randrange(1000, 9001):,}'
                    row['cents'] = '' if n % 11 == 0 else f'{source.random.randrange(1000, 9001):,}'
                rows.extend(dict(row) for row in rows[:count//10])
                source.random.shuffle(rows)
                rules = ('Deduplicate by id, keeping only the first occurrence (duplicates have identical values). '
                         'A blank units or cents cell is zero; commas inside numbers are thousands separators. '
                         'Commas in labels are ordinary label text, not column separators. ')
                query = ('Return distinct_rows (number after deduplication), total_cents (sum of units*cents after deduplication), '
                         'and blank_rows (number of distinct rows with either numeric cell blank, counted once each).')
            # Consume the original 30/80/200-row draws and the v5 20/40/60 prefix.
            # Then apply the same structural coverage and even-position policy to
            # every case; retain indices so the printed projection is auditable.
            rows = rows[:{30: 20, 80: 40, 200: 60}[count]]
            parent_rows = len(rows)
            row_indices = representative_indices(rows, parent_rows // 2)
            rows = [rows[position] for position in row_indices]
            if category in ('filter', 'group', 'rank'):
                if category == 'filter':
                    candidates = sorted(r['cents'] for r in rows if r['group'] == options['group'])
                    options['threshold'] = candidates[len(candidates)//2]
                    query = (f"Select group={options['group']} AND cents>={options['threshold']}. Return matching_rows "
                             '(count), matching_units (sum of units), matching_cents (sum of row totals), all over those selected rows.')
                elif category == 'group':
                    totals = sorted(sum(r['units']*r['cents'] for r in rows if r['group']==g) for g in groups)
                    options['threshold'] = (totals[1]+totals[2])//2
                    query = (f'Return totals as a list of group row-total sums in this exact group order: {", ".join(groups)}. '
                             f'Return greatest_group (greatest total; tie by lexicographically smallest group), and groups_above '
                             f"(number of groups whose total is strictly greater than {options['threshold']}).")
                elif category == 'rank':
                    options['named'] = rows[-1]['id']
                    query = (f"Rank all rows by units*cents descending, then id ascending to break ties. Return third_id, "
                             f"top_three (three row IDs in ranked order), and named_position (one-based rank of {options['named']}).")
            table = render(rows, columns, format)
            if category == 'join':
                table = 'Orders:\n' + table + '\nPrices:\n' + render(options['prices'], ['product', 'cents'], format)
            question = intro + rules + f'\n{format.upper()} table:\n' + table + '\n' + query
            result.append(dict(id=f'{category}-{index+1}', category=category, context=limit, inputs=0,
                outputs=min(OUTPUTS,limit//2), request={'question': quality.json_request(question),
                    'temperature':1.0,'sampling':SAMPLING},
                expected=solve(category, rows, options),
                source=dict(rows=rows, columns=columns, options=options, format=format,
                            selection={'parent_rows': parent_rows, 'indices': row_indices})))
    return result


def grade(case, answer):
    """Apply data-only table reasoning checks to the final answer."""
    base, fields = quality.grade_fields(case, answer)
    by_field = {check['field']: check for check in fields}
    category = case['category']
    auxiliary = ()
    if category in ('group', 'rank'):
        field = 'totals' if category == 'group' else 'top_three'
        actual = by_field[field]['actual']
        checks = []
        for index, expected in enumerate(case['expected'][field]):
            value = actual[index] if isinstance(actual, list) and index < len(actual) else None
            checks.append({'field': f'{field}[{index}]', 'expected': expected, 'actual': value,
                           'passed': isinstance(actual, list) and index < len(actual)
                           and quality.same(value, expected, True)})
        if category == 'group':
            auxiliary = ('greatest_group', 'groups_above')
        else:
            checks.append(dict(by_field['named_position']))
            auxiliary = ('third_id',)
    else:
        checks = [dict(check) for check in fields]
    correct = sum(bool(check['passed']) for check in checks)
    metrics = {**base, 'correct': correct, 'checks': len(checks),
               'passed': bool(base['passed'] and correct == len(checks)),
               'auxiliary_correct': sum(bool(by_field[field]['passed']) for field in auxiliary),
               'auxiliary_checks': len(auxiliary),
               'auxiliary_passed': all(by_field[field]['passed'] for field in auxiliary),
               'table_format': case['source']['format']}
    return metrics, checks


def summarize(samples):
    """Summarize retained table reasoning samples and supported scores."""
    result = quality.summarize(samples)
    auxiliary = sum(s['values'].get('auxiliary_checks', 0) for s in samples)
    if auxiliary:
        result['auxiliary_accuracy'] = 100 * sum(s['values'].get('auxiliary_correct', 0) for s in samples) / auxiliary
    for format in ('csv', 'markdown'):
        chosen = [s for s in samples if s['values'].get('table_format') == format]
        if chosen:
            result[format + '_accuracy'] = quality.summarize(chosen)['accuracy']
    for category in CATEGORIES:
        chosen = [s for s in samples if s['values'].get('category') == category]
        if chosen:
            result[category + '_accuracy'] = quality.summarize(chosen)['accuracy']
    return result


async def measure(worker, run, ceiling):
    """Run the table reasoning protocol through the existing benchmark probe."""
    await quality.measure(worker, run, ceiling, PROTOCOL, cases, grade)
