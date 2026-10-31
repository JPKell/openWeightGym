"""Six evidence questions per passage: three supported values and three explicit nulls."""

from .. import quality

SEED = 20261015
ABSENCES = ('never_spoken', 'not_given', 'other_subject', 'false_premise')
LABELS = {'never_spoken': 'Never mentioned', 'not_given': 'Mentioned without a value',
          'other_subject': 'Value belongs to another project', 'false_premise': 'False premise'}
PRESENT = ('present_accuracy', 'Present facts answered correctly', '%')
INVENTION = ('invention_rate', 'Absent answers given invented values', '%')
DEFINITION = quality.definition('abstention', 'Abstention',
    'Answer from a passage and leave unsupported answers null.',
    'Twenty fixed-seed passages with six questions each: 120 field checks, half supported '
    'and half requiring null. Missing information is never mentioned, mentioned without a '
    'value, given only for a near-named project, or based on a false premise. Questions and '
    'answer keys are shuffled; passages are unpadded by the probe. Present/null accuracy '
    'and invented-value rates are reported separately. Real-model qualification is pending.',
    [quality.FORMAT, PRESENT, quality.ABSENT, INVENTION,
     *[(kind + '_accuracy', label + ': correct null', '%') for kind, label in LABELS.items()]])
PROTOCOL = {**quality.PROTOCOL, 'version': 1, 'dataset': 'abstention-v1', 'seed': SEED,
            'cases': 20, 'checks': 120, 'absent_checks': 60, 'absence_categories': list(ABSENCES)}

# Each property can be present or absent; false premises are not restricted to one answer key.
FIELDS = {
    'contact': ('contact list', 'person', 'contacts in order', 'Who is the {ordinal} contact'),
    'date': ('delivery date list', 'date', 'delivery dates in order', 'What is the {ordinal} delivery date'),
    'units': ('shipment quantities', 'integer', 'shipment crate counts in order', 'How many crates are in the {ordinal} shipment'),
    'venue': ('venue list', 'place', 'venues in order', 'What is the {ordinal} venue'),
    'budget': ('approved budget revisions', 'integer', 'approved budget revisions in whole credits, in order', 'What is the {ordinal} approved budget revision in whole credits'),
    'speaker': ('speaker list', 'person', 'speakers in order', 'Who is the {ordinal} speaker'),
}
FILLER = ('The register keeps archived projects separate from the current project. Paper copies '
          'are filed by project reference. General handling notes describe the filing process, '
          'not additional project facts. Records are read as written rather than completed from '
          'what usually happens. An approved list is not evidence of any unrecorded entry. ')


def values(source):
    """Create deterministic synthetic facts for answerable and unanswerable cases."""
    def make(field):
        """Choose one synthetic reference value for an abstention fixture."""
        kind = FIELDS[field][1]
        if kind == 'person':
            return source.person()
        if kind == 'date':
            return source.date().isoformat()
        if kind == 'place':
            return source.place()
        return source.number(100, 10000)
    return {field: [make(field), make(field)] for field in FIELDS}


def fact(project, field, entries):
    """Render one synthetic fact into the case source material."""
    return f"Project {project}: {FIELDS[field][2]}: " + ' then '.join(map(str, entries)) + '.'


def cases(limit):
    """Build deterministic evidence-grounded abstention fixtures from the case seed."""
    source = quality.Source(SEED)
    result = []
    for index in range(20):
        reference = source.number(10000, 90000)
        other = source.fresh(lambda: reference + source.random.choice((-1, 1)))
        project, archive = f'P-{reference}', f'P-{other}'
        truth, old = values(source), values(source)
        fields = list(FIELDS)
        source.random.shuffle(fields)
        present, absent = fields[:3], fields[3:]
        missing = {field: ABSENCES[(index * 3 + offset) % 4] for offset, field in enumerate(absent)}
        ordinals = {field: source.random.randrange(2) for field in fields}
        expected, kinds, types, absence_kinds, questions, support = {}, {}, {}, {}, [], {}
        # Earlier near-miss values make copying the first value of the right shape fail.
        archived = [field for field in fields if field in present or missing.get(field) == 'other_subject']
        lines = [f'Archived project {archive}; this is not project {project}.']
        lines.extend(fact(archive, field, old[field]) for field in archived)
        lines.append(f'Current project {project}. Only facts explicitly attached to this reference apply.')
        for number, field in enumerate(fields, 1):
            key = f'q{number}'
            kind = missing.get(field)
            if kind == 'false_premise':
                ordinals[field] = 1
                sentence = fact(project, field, truth[field][:1]) + ' This list has exactly one entry.'
                lines.append(sentence)
            elif kind == 'not_given':
                lines.append(f'Project {project} has an approved {FIELDS[field][0]}, but its entries are not recorded.')
            elif field in present:
                sentence = fact(project, field, truth[field][:ordinals[field] + 1])
                lines.append(sentence)
                support[key] = sentence
            ordinal = ('first', 'second')[ordinals[field]]
            question = FIELDS[field][3].format(ordinal=ordinal) + f' for project {project}?'
            if FIELDS[field][1] == 'date':
                question += ' Use YYYY-MM-DD.'
            questions.append(f'{key}: {question}')
            expected[key] = truth[field][ordinals[field]] if field in present else None
            kinds[key] = 'plain' if field in present else 'absent'
            types[key] = FIELDS[field][1]
            if kind:
                absence_kinds[key] = kind
        source.random.shuffle(questions)
        target = (1600, 2800, 4200, 6000, 7800)[index % 5]
        passage = '\n'.join(lines)
        while len(passage) + len(FILLER) + 1 <= target:
            passage += '\n' + FILLER
        question = (f'Read the passage, then answer the six questions only from it. Do not infer '
                    'missing entries or borrow a value belonging to another project. Use JSON null '
                    'when the requested value is not given, including an entry that does not exist.\n'
                    f'PASSAGE\n{passage}\nEND PASSAGE\n' + '\n'.join(questions))
        distractors = [value for field in archived for value in old[field]]
        distractors += [truth[field][0] for field in absent if missing[field] == 'false_premise']
        result.append({'id': f'passage-{index+1}', 'category': 'mixed absences', 'context': limit,
            'inputs': 0, 'outputs': quality.room(limit), 'request': {'question': quality.json_request(question)},
            'expected': expected, 'kinds': kinds, 'types': types, 'absence_kinds': absence_kinds,
            'distractors': distractors, 'entities': [project, archive],
            'source': {'passage': passage, 'support': support, 'fields': fields, 'project': project, 'archive': archive}})
    return result


def grade(case, answer):
    """Apply data-only evidence-grounded abstention checks to the final answer."""
    metrics, checks = quality.grade_fields(case, answer)
    present = [check for check in checks if check['kind'] != 'absent']
    metrics.update(present=len(present), present_correct=sum(check['passed'] for check in present),
                   invented=sum(check['kind'] == 'absent' and check['actual'] is not None for check in checks))
    for kind in ABSENCES:
        selected = [check for check in checks if case['absence_kinds'].get(check['field']) == kind]
        metrics[kind] = len(selected)
        metrics[kind + '_correct'] = sum(check['passed'] for check in selected)
    return metrics, checks


def summarize(samples):
    """Summarize retained evidence-grounded abstention samples and supported scores."""
    result = quality.summarize(samples)
    if not samples:
        return result
    def rate(correct, total):
        """Return the successful-check fraction, or None when no checks exist."""
        count = sum(sample['values'].get(total, 0) for sample in samples)
        return 100 * sum(sample['values'].get(correct, 0) for sample in samples) / count if count else 0
    result.update(present_accuracy=rate('present_correct', 'present'), invention_rate=rate('invented', 'absent'))
    result.update({kind + '_accuracy': rate(kind + '_correct', kind) for kind in ABSENCES})
    return result


async def measure(worker, run, ceiling):
    """Run the evidence-grounded abstention protocol through the existing benchmark probe."""
    await quality.measure(worker, run, ceiling, PROTOCOL, cases, grade)
