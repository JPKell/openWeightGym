"""Seeded archive retrieval with scoped amendments, joins and deliberate omissions."""

import random
from datetime import date, timedelta

from .. import quality

SEED = 20261014
REGIONS = ('start', 'middle', 'end')
STORIES = ('exhibition', 'survey', 'festival', 'workshop')
SAMPLING = {'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
            'presence_penalty':0.0,'frequency_penalty':0.0}
DEFINITION = quality.definition(
    'retrieval', 'Long-context retrieval', 'Recover and combine facts across amended archives.',
    'Four seeded stories at each of 1024, 4096, 8192, 32768 and 65536 input tokens, capped '
    'by usable context: 20 distinct cases and 200 fields. Dated amendments include withdrawn '
    'and wrong-project records; three fields join facts and two require null. Varied archive '
    'padding includes near misses and long appendices. Invention and evidence-position '
    'accuracy accompany field accuracy. Temperature 1, seed 42 and explicit neutral native '
    'filters allow up to 32,768 output tokens; the field grader is unchanged.',
    [quality.FORMAT, quality.ABSENT, ('invention', 'Absent fields given an invented value', '%'),
     ('invented_fields', 'Invented absent fields', ''), ('absent_fields', 'Absent fields checked', ''),
     *[(f'{region}_accuracy', f'Evidence near the {region}: correct fields', '%') for region in REGIONS]])
DEFINITION['kpis'] += [('uncapped_accuracy','Correct fields without output caps','%'), ('uncapped_cases','Cases without output caps','count')]
PROTOCOL = {**quality.PROTOCOL, 'version': 5, 'outputs':32768, 'dataset': 'retrieval-v3', 'seed': SEED,
            'input_sizes': [1024, 4096, 8192, 32768, 65536], 'variants': 4,
            'temperature':1.0,'generation_seed':42,'sampling':{'temperature':1.0,**SAMPLING}}


def archive(facts, seed):
    """300 scoped entries; only the prefix fitting each native gap is sent.

    The long appendix is roughly 15,000 words, not a tokenizer-independent token count.
    Native padding starts at the same prefix in every gap, as it did in retrieval-v1.
    """
    rng = random.Random(seed)
    project, year = facts['project'], facts['year']
    topics = ('lighting', 'storage', 'printing', 'ventilation', 'seating', 'transport')
    roles = ('archivist', 'steward', 'cashier', 'technician', 'registrar', 'porter')
    entries = []
    for index in range(300):
        ref = f'{project}.A{index+1:03}'
        topic, role = rng.choice(topics), rng.choice(roles)
        count = 6000 + index * 7 + rng.randrange(7)
        scope = f'{project}/{year-1}' if index % 3 == 0 else f'{project}-{index+1}/{year}'
        variants = (
            f'attendance {count}; room {facts["old_room"]}; the {topic} register was filed.',
            f'{facts["predecessor"]} served as {role}, not director. The {topic} inspection covered {count} items.',
            f'{count} cents were reserved for {topic}; no appointment was made.',
            f'Room {facts["other_room"]} received {count} notices about {topic}.',
            f'Access code ARCH-{ref}; sponsor Trust-{ref}; {topic} work remains provisional.',
            f'The {role} checked {count} {topic} records and signed the separate inventory.',
        )
        entry = f'\n--- archive {ref} ---\n{scope}: ' + variants[index % len(variants)]
        if index == 60:
            appendix = []
            for section in range(800):
                subject = rng.choice(topics)
                appendix.append(
                    f'Section {section+1}: the {subject} crew inspected shelves and recorded routine '
                    'maintenance observations before returning the numbered forms to storage.')
            entry += '\nMaintenance appendix, no target-project amendments:\n' + '\n'.join(appendix)
        entries.append(entry)
    return '\n'.join(entries)


def story(source, size, variant):
    """Construct dated source records, then shuffle their physical placement."""
    rng = source.random
    project = f'PRJ-{source.number(100000, 999999)}'
    year = 2030 + variant
    director, predecessor, successor = (source.person() for _ in range(3))
    old_room, room, other_room, event = (source.place() for _ in range(4))
    opening = date(year, 5, rng.randrange(1, 15))
    closing = opening + timedelta(days=rng.randrange(17, 49))
    attendance = rng.sample(range(130, 590), 5)
    exhibits, ratio = rng.randrange(21, 60), rng.randrange(1100, 3900)
    receipts = exhibits * ratio
    withdrawn = variant in (1, 3)
    facts = dict(project=project, year=year, director=director, predecessor=predecessor,
                 successor=successor, old_room=old_room, room=room, other_room=other_room,
                 opening=opening.isoformat(), closing=closing.isoformat(), event=event,
                 attendance=attendance, exhibits=exhibits, receipts_cents=receipts,
                 withdrawn=withdrawn)
    scope = f'{project}/{year}'
    records = [
        (f'{scope} {STORIES[variant]} register, issued {year}-01-01: Director {director}. Opens {opening}. '
         f'Attendance {attendance[0]}. Room {old_room}.', ('director', 'opening_date')),
        (f'{scope}, amendment A1 issued {year}-01-05: attendance {attendance[1]}; room {room}. '
         f'{project}-X/{year}, issued {year}-01-19: attendance {attendance[4]}; room {other_room}.',
         ('final_room',)),
        (f'{scope}, amendment A2 issued {year}-01-09: attendance {attendance[2]}. '
         f'Receipts {receipts} cents. {predecessor} handed responsibility to {successor}.',
         ('cents_per_exhibit', 'successor')),
        (f'{scope}, amendment A3 issued {year}-01-13: attendance {attendance[3]}. '
         f'{scope}, separate closing report issued {year}-02-01: closes {closing} with {event}.',
         ('elapsed_days', 'closing_event')),
        (f'{scope}, issued {year}-01-15: ' +
         ('A3 is withdrawn; A2 remains in force.' if withdrawn else 'A3 remains in force.') +
         f' Inspected exhibits {exhibits}. Earlier responsibility holder: {predecessor}. '
         f'{project}/{year-1}, issued {year}-01-22: attendance {attendance[4]}; room {other_room}.',
         ('cents_per_exhibit', 'successor')),
    ]
    winning = 2 if withdrawn else 3
    positions = {key: [] for key in ('director', 'opening_date', 'final_attendance', 'final_room',
                                    'elapsed_days', 'cents_per_exhibit', 'successor', 'closing_event',
                                    'sponsor', 'access_code')}
    order = list(range(len(records)))
    rng.shuffle(order)
    blocks = []
    for index, record in enumerate(order):
        text, fields = records[record]
        blocks.append('--- document ---\n' + text)
        for field in fields:
            positions[field].append(index)
        if record in (winning, 4):
            positions['final_attendance'].append(index)
        if record == 0:
            positions['elapsed_days'].append(index)
    expected = dict(director=director, opening_date=opening.isoformat(),
                    final_attendance=attendance[winning], final_room=room,
                    elapsed_days=(closing-opening).days, cents_per_exhibit=ratio,
                    successor=successor, closing_event=event, sponsor=None, access_code=None)
    question = quality.json_request(
        f'For {scope} only, use the latest issued amendment for each field; ignore withdrawn '
        'amendments and other projects/years. Return director, opening_date (YYYY-MM-DD), '
        'final_attendance, final_room, elapsed_days (closing minus opening, exclude start), '
        'cents_per_exhibit (receipts cents divided by inspected exhibits), successor '
        '(who took over from the earlier responsibility holder), closing_event, sponsor, '
        'access_code. Use integers for counts, days and cents. If a requested fact is not '
        'stated for this project/year, return JSON null. No rounding is needed.')
    wrong = [old_room, other_room, predecessor,
             *[value for index, value in enumerate(attendance) if index != winning],
             *[f'ARCH-{project}.A{index+1:03}' for index in range(300)],
             *[f'Trust-{project}.A{index+1:03}' for index in range(300)]]
    case = dict(id=f'retrieve-{size}-{variant+1}', category=STORIES[variant],
                request={'question': question, 'blocks': blocks}, expected=expected,
                source=facts, positions=positions,
                kinds={key: ('absent' if value is None else 'combined' if key in
                       ('elapsed_days', 'cents_per_exhibit', 'successor') else
                       'corrected' if key.startswith('final_') else 'plain')
                       for key, value in expected.items()},
                distractors=wrong, entities=[project, director, predecessor, successor,
                                            old_room, room, other_room, event])
    return case


def cases(limit):
    """Canonical cases stay stable when a smaller context adds a capped input size."""
    source, result, room = quality.Source(SEED), [], min(PROTOCOL['outputs'],limit//2)
    sizes = sorted({min(size,limit-room) for size in PROTOCOL['input_sizes']})
    for size in [*PROTOCOL['input_sizes'], *[size for size in sizes if size not in PROTOCOL['input_sizes']]]:
        for variant in range(PROTOCOL['variants']):
            case = story(source, size, variant)
            if size not in sizes:
                continue
            case.update(context=size+room, inputs=size, outputs=room)
            case['request']['filler'] = archive(case['source'], f'{SEED}/{size}/{variant}')
            case['request'].update(temperature=1.0,sampling=dict(SAMPLING))
            result.append(case)
    return sorted(result, key=lambda case: (case['inputs'], case['id']))


def region(index):
    """Return the selected answer region from retained model output."""
    return 'start' if index == 0 else 'end' if index == 4 else 'middle'


def grade(case, answer):
    """Apply data-only long-context prose retrieval checks to the final answer."""
    metrics, checks = quality.grade_fields(case, answer)
    if 'positions' not in case:  # historical retrieval-v1 keeps its grading contract
        return metrics, checks
    metrics.update(invented_fields=0, absent_fields=metrics['absent'])
    for place in REGIONS:
        metrics.update({f'{place}_checks': 0, f'{place}_correct': 0})
    for check in checks:
        indices = sorted(case['positions'][check['field']])
        check.update(evidence_blocks=indices, positions=sorted({region(index) for index in indices}))
        if check['kind'] == 'absent':
            metrics['invented_fields'] += check['actual'] is not None
        for place in check['positions']:
            metrics[f'{place}_checks'] += 1
            metrics[f'{place}_correct'] += check['passed']
    return metrics, checks


def summarize(samples):
    """Summarize retained long-context prose retrieval samples and supported scores."""
    result = quality.summarize(samples)
    if samples:
        whole=[s for s in samples if not s['values'].get('generation_capped')]
        result['uncapped_cases']=len(whole)
        result['qualification_valid']=(len(whole)==len(samples) and
            all(sample['values'].get('success') is True and
                not sample['values'].get('unsupported') and
                not sample['values'].get('unavailable') for sample in samples))
        if whole:
            result['uncapped_accuracy']=quality.summarize(whole)['accuracy']
    values = [sample['values'] for sample in samples]
    absent = sum(value.get('absent_fields', 0) for value in values)
    if absent:
        invented = sum(value.get('invented_fields', 0) for value in values)
        result.update(invention=100*invented/absent, invented_fields=invented, absent_fields=absent)
    for place in REGIONS:
        total = sum(value.get(f'{place}_checks', 0) for value in values)
        if total:
            result[f'{place}_accuracy'] = 100*sum(value.get(f'{place}_correct', 0) for value in values)/total
    return result


def breakdown(samples):
    """Group retained long-context prose retrieval outcomes by case dimensions."""
    groups = {}
    for sample in samples:
        values = sample['values']
        groups.setdefault((values.get('target_input_tokens', 0), values.get('category', '')), []).append(sample)
    return [dict(input_tokens=size, category=category, **summarize(group))
            for (size, category), group in sorted(groups.items())]


async def measure(worker, run, ceiling):
    """Run the long-context prose retrieval protocol through the existing benchmark probe."""
    await quality.measure(worker, run, ceiling, PROTOCOL, cases, grade)
