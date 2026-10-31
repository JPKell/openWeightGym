"""Bounded, data-only case sets. Imports never supply Python, tools or executable graders."""

import copy
import hashlib
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from . import quality

MAX_BYTES = 1024 * 1024
IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]{0,47}')
SCHEMA_KEYS = {'type', 'enum', 'const', 'properties', 'required', 'additionalProperties', 'items',
               'minItems', 'maxItems', 'minLength', 'maxLength', 'minimum', 'maximum', 'exclusiveMinimum',
               'exclusiveMaximum', 'title', 'description'}
GRADER_VERSION = 2
IMPLEMENTATION_SHA256 = hashlib.sha256(b''.join(path.read_bytes() for path in
    (Path(__file__), Path(quality.__file__), Path(__file__).with_name('worker.py')))).hexdigest()


def encoded(value):
    """Serialize an immutable custom dataset into canonical JSON."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    """Return the SHA-256 digest of the canonical input."""
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def bounded(value, depth=0):
    """Bound nesting and containers before schema checking or recursive equality."""
    if depth > 20:
        raise ValueError('JSON nesting may be at most 20 levels.')
    if isinstance(value, (dict, list)):
        if len(value) > 1000:
            raise ValueError('A JSON object or list may contain at most 1000 entries.')
        for item in value.values() if isinstance(value, dict) else value:
            bounded(item, depth + 1)


def schema_checked(schema):
    """A structural Draft 2020-12 subset: no references, regexes, plugins or network access."""
    if type(schema) is bool:
        return
    if not isinstance(schema, dict) or schema.keys() - SCHEMA_KEYS:
        raise ValueError('Schemas support structural type/enum/const, object/array, length and numeric checks only; references and regexes are not supported.')
    for child in schema.get('properties', {}).values():
        schema_checked(child)
    for key in ('items', 'additionalProperties'):
        if key in schema:
            schema_checked(schema[key])


def validate(dataset):
    """Accept one explicit immutable version, with stable case/assertion IDs."""
    if len(encoded(dataset).encode()) > MAX_BYTES:
        raise ValueError('A dataset may be at most 1 MiB.')
    bounded(dataset)
    if not isinstance(dataset, dict) or dataset.keys() != {'format', 'id', 'name', 'version', 'cases'}:
        raise ValueError('A dataset contains format, id, name, version and cases.')
    if dataset['format'] != 'suite-cases/v1' or not isinstance(dataset['id'], str) or not IDENTIFIER.fullmatch(dataset['id']):
        raise ValueError('Use format suite-cases/v1 and an id of lowercase letters, digits, underscores or dashes, starting with a letter (up to 48 characters).')
    if not isinstance(dataset['name'], str) or not dataset['name'].strip() or len(dataset['name']) > 100:
        raise ValueError('Give the dataset a name of 1–100 characters.')
    if type(dataset['version']) is not int or not 1 <= dataset['version'] <= 2147483647:
        raise ValueError('Dataset version must be a positive 32-bit whole number.')
    if not isinstance(dataset['cases'], list) or not 1 <= len(dataset['cases']) <= 128:
        raise ValueError('A dataset contains one to 128 cases.')
    ids = set()
    for case in dataset['cases']:
        if not isinstance(case, dict) or case.keys() - {'id', 'question', 'category', 'outputs', 'assertions'} or not {'id', 'question', 'assertions'} <= case.keys():
            raise ValueError('Each case contains id, question, assertions and optionally category and outputs.')
        if not isinstance(case['id'], str) or not IDENTIFIER.fullmatch(case['id']) or case['id'] in ids:
            raise ValueError('Case IDs must be unique identifiers of up to 48 characters.')
        ids.add(case['id'])
        if not isinstance(case['question'], str) or not case['question'].strip() or not 1 <= len(case['question'].encode()) <= 65536:
            raise ValueError('Each question contains 1–65536 UTF-8 bytes.')
        if not isinstance(case.get('category', ''), str) or len(case.get('category', '')) > 80:
            raise ValueError('Case categories contain at most 80 characters.')
        if type(case.get('outputs', 512)) is not int or not 1 <= case.get('outputs', 512) <= quality.OUTPUTS:
            raise ValueError('Case outputs must be from 1 to 8192 tokens.')
        assertions = case['assertions']
        if not isinstance(assertions, list) or not 1 <= len(assertions) <= 32:
            raise ValueError('Each case contains one to 32 assertions.')
        checks = set()
        for assertion in assertions:
            if not isinstance(assertion, dict) or assertion.keys() != {'id', 'type', 'expected'}:
                raise ValueError('Each assertion contains id, type and expected.')
            if not isinstance(assertion['id'], str) or not IDENTIFIER.fullmatch(assertion['id']) or assertion['id'] in checks:
                raise ValueError('Assertion IDs must be unique within each case.')
            checks.add(assertion['id'])
            kind, expected = assertion['type'], assertion['expected']
            if kind not in ('exact', 'json', 'schema') or kind == 'exact' and (not isinstance(expected, str) or not expected.strip()):
                raise ValueError('Assertion type is exact (text), json (one strict JSON value), or schema (structural JSON Schema).')
            if kind == 'schema':
                try:
                    Draft202012Validator.check_schema(expected)
                    schema_checked(expected)
                except (ValueError, TypeError, AttributeError, SchemaError) as problem:
                    raise ValueError('Invalid bounded schema: ' + str(problem).split('\n')[0][:300]) from None
    return dataset


def grade(case, answer):
    """Strict JSON equality preserves booleans, object keys and list order; no fuzzy extraction."""
    try:
        parsed = quality.strict_json(answer)
        bounded(parsed)
        json_valid = True
    except (ValueError, RecursionError):
        parsed, json_valid = None, False
    checks = []
    for assertion in case['assertions']:
        kind, expected = assertion['type'], assertion['expected']
        if kind == 'exact':
            correct = answer == expected
        elif kind == 'json':
            correct = json_valid and quality.same(parsed, expected)
        else:
            correct = json_valid and Draft202012Validator(expected).is_valid(parsed)
        checks.append({'field': assertion['id'], 'kind': kind, 'passed': correct,
                       'expected': expected, 'actual': answer if kind == 'exact' else parsed})
    correct = sum(check['passed'] for check in checks)
    return {'checks': len(checks), 'correct': correct, 'passed': correct == len(checks), 'json_valid': json_valid}, checks


def definition(dataset, sha256):
    """Return the public benchmark definition for a validated custom dataset."""
    return {**quality.definition('custom_' + dataset['id'], dataset['name'],
        'Imported deterministic cases with exact text, strict JSON and structural schema assertions.',
        'Ask each unpadded question once, as served; grade its final answer locally. Imported data cannot execute code or tools.'),
        'custom': True, 'version': dataset['version'], 'dataset_sha256': sha256}


class Dataset:
    """The ordinary suite interface backed by a frozen database version instead of a module."""
    grade = staticmethod(grade)
    summarize = staticmethod(quality.summarize)

    def __init__(self, dataset, sha256):
        """Bind the immutable custom dataset and its retained benchmark definition."""
        self.dataset = dataset
        self.DEFINITION = definition(dataset, sha256)
        self.PROTOCOL = {**quality.PROTOCOL, 'version': dataset['version'], 'dataset': self.DEFINITION['id'],
                         'dataset_sha256': sha256, 'grader_version': GRADER_VERSION,
                         'implementation_sha256': IMPLEMENTATION_SHA256}

    def cases(self, limit):
        """Return the immutable cases belonging to this custom dataset."""
        return [{'id': case['id'], 'category': case.get('category', 'custom'), 'context': limit, 'inputs': 0,
                 'outputs': min(case.get('outputs', 512), quality.room(limit)),
                 'request': {'question': case['question']}, 'assertions': copy.deepcopy(case['assertions'])}
                for case in self.dataset['cases']]

    async def measure(self, worker, run, ceiling):
        """Evaluate a custom dataset through the existing benchmark probe contract."""
        await quality.measure(worker, run, ceiling, self.PROTOCOL, self.cases, self.grade)
