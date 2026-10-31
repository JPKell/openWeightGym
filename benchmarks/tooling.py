"""Bounded mock-tool conversations. No model-selected code, files or network calls run."""

import asyncio
import json

from jsonschema import Draft202012Validator

from . import quality

PROTOCOL = {**quality.PROTOCOL, 'version': 1, 'grader_version': 1,
            'transport': 'native chat completions with function calls',
            'tool_choice': 'auto', 'parallel_tool_calls': False,
            'constrained_decoding': 'native function-call defaults; no benchmark answer grammar',
            'max_turns': 8, 'max_calls': 12, 'temperature': 0, 'seed': 42,
            'cache_prompt': False, 'mock_world': 'fresh per case; replay unfinished cases'}
KPIS = [('selection_accuracy', 'Correct tool selections', '%'),
        ('argument_accuracy', 'Correct argument objects', '%'),
        ('task_completion', 'Tasks completed', '%'), ('recovery_accuracy', 'Recovery after injected errors', '%'),
        ('unnecessary_retries', 'Unnecessary retries', 'count'), ('tool_calls', 'Tool calls', 'count'),
        ('input_tokens', 'Input tokens across turns', 'tokens'), ('output_tokens', 'Output tokens across turns', 'tokens'),
        ('cases', 'Cases', 'count'), ('available_cases', 'Executed cases', 'count'),
        ('unsupported_cases', 'Unsupported cases', 'count'), ('capped_cases', 'Hit the output limit', 'count')]


def definition(key, name, summary):
    """Describe the deterministic synthetic tool benchmark contract."""
    return {'id': key, 'name': name, 'summary': summary, 'quality': True, 'native_tools': True,
            'formula': 'task_completion', 'score_unit': '%', 'kpis': KPIS,
            'method': 'Twelve fixed tasks with distractor tools and exact typed arguments. Native function calls '
                      'are executed against an in-memory world; actual results and errors are sent back as tool messages. '
                      'At most eight model turns and twelve calls per task, temperature 0, seed 42, thinking as served, '
                      'up to 8,192 output tokens per turn. Each turn uses a fresh isolated process without prompt caching. '
                      'The score is completed tasks (%); selection, arguments, retries and cumulative tokens are separate. '
                      'Recovery requires observing the injected error and then completing the task. Unsupported tool '
                      'templates/configurations receive no score. This tests these mock workflows, not unrestricted agent safety.'}


def tool(name, description, **properties):
    """Build the JSON schema and description of one fixture-only tool."""
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}}}


TOOLS = {
    'weather': tool('weather', 'Current temperature, in Celsius, for a city.', city={'type': 'string'}),
    'forecast': tool('forecast', 'Tomorrow\'s forecast for a city.', city={'type': 'string'}),
    'convert_length': tool('convert_length', 'Convert integer metres to centimetres.', metres={'type': 'integer'}),
    'convert_currency': tool('convert_currency', 'Convert cents between CAD and USD.', cents={'type': 'integer'}, currency={'enum': ['CAD', 'USD']}),
    'find_record': tool('find_record', 'Find the current record ID by customer name.', customer={'type': 'string'}),
    'read_record': tool('read_record', 'Read a private record by its current record ID.', record_id={'type': 'string'}),
    'public_record': tool('public_record', 'Read the public summary of a record by ID.', record_id={'type': 'string'}),
    'create_note': tool('create_note', 'Create a note for a record. Reuse request_id when retrying; it prevents duplicate notes.',
                        record_id={'type': 'string'}, text={'type': 'string'}, request_id={'type': 'string'}),
}


def cases(limit, recovery=False):
    """Answer keys and fault schedules stay on front, outside every model request."""
    result = []
    for n, customer in enumerate(('Mira Chen', 'Oren Vale', 'Priya Pike'), 1):
        rid, current = f'R-{n}A', f'R-{n}B'
        common = {'customer': customer, 'record_id': rid, 'current_id': current, 'units': 17 + n,
                  'temperature': 8 + n, 'note': f'Delivery {n} confirmed', 'request_id': f'NOTE-{n}'}
        call = lambda name, **args: {'name': name, 'arguments': args}
        find = call('find_record', customer=customer)
        read = call('read_record', record_id=rid)
        write = call('create_note', record_id=rid, text=common['note'], request_id=common['request_id'])
        if recovery:
            tasks = [
                ('transient', f'Use current weather for Cedar {n}. Return temperature_c. Recover if the service asks you to retry.',
                 ['weather', 'forecast'], [call('weather', city=f'Cedar {n}')]*2, {'temperature_c': common['temperature']}),
                ('changed_id', f'Find {customer}\'s record, read its units, and return units. Follow a changed-ID response.',
                 ['find_record', 'read_record', 'public_record'], [find, read, call('read_record', record_id=current)], {'units': common['units']}),
                ('denied', f'Find {customer}\'s record. Read it privately if permitted, otherwise use its public summary. Return units.',
                 ['find_record', 'read_record', 'public_record'], [find, read, call('public_record', record_id=rid)], {'units': common['units']}),
                ('lost_receipt', f'Find {customer}\'s record and create exactly one note with text "{common["note"]}" and request_id "{common["request_id"]}". Return note_id. Recover a lost receipt without duplicating the note.',
                 ['find_record', 'create_note', 'read_record'], [find, write, write], {'note_id': f'N-{n}'}),
            ]
        else:
            tasks = [
                ('selection', f'Use current weather for Cedar {n}. Return temperature_c.',
                 ['forecast', 'weather'], [call('weather', city=f'Cedar {n}')], {'temperature_c': common['temperature']}),
                ('arguments', f'Use a tool to convert {12+n} metres to centimetres. Return centimetres.',
                 ['convert_currency', 'convert_length'], [call('convert_length', metres=12+n)], {'centimetres': (12+n)*100}),
                ('multi_read', f'Find {customer}\'s record and read its units. Return units.',
                 ['public_record', 'read_record', 'find_record'], [find, read], {'units': common['units']}),
                ('multi_write', f'Find {customer}\'s record and create exactly one note with text "{common["note"]}" and request_id "{common["request_id"]}". Return note_id.',
                 ['read_record', 'create_note', 'find_record'], [find, write], {'note_id': f'N-{n}'}),
            ]
        for category, question, names, steps, expected in tasks:
            result.append({'id': f'{category}-{n}', 'category': category, 'context': min(limit, 131072),
                           'outputs': quality.room(limit), 'inputs': 0, 'question': question,
                           'tools': [TOOLS[name] for name in names], 'steps': steps, 'expected': expected,
                           'world': {**common, 'number': n, 'fault': category if recovery else None}})
    return result


class World:
    """Fixed handlers only. Calls never reach ToolLocker, the filesystem or a subprocess."""
    def __init__(self, case):
        """Create isolated in-memory tool fixtures and empty dialogue observations."""
        self.case, self.data = case, case['world']
        self.calls, self.notes, self.injected = [], {}, False

    def call(self, name, arguments):
        """Execute a synthetic tool action against in-memory fixture data only."""
        schema = next((t['function']['parameters'] for t in self.case['tools'] if t['function']['name'] == name), None)
        if schema is None:
            return {'error': 'unknown_tool', 'retryable': False}
        if not Draft202012Validator(schema).is_valid(arguments):
            return {'error': 'invalid_arguments', 'retryable': False}
        self.calls.append((name, arguments))
        data, fault = self.data, self.data['fault']
        if name == 'weather':
            if arguments['city'] != f'Cedar {data["number"]}':
                return {'error': 'unknown_city', 'retryable': False}
            if fault == 'transient' and not self.injected:
                self.injected = True
                return {'error': 'temporarily_unavailable', 'retryable': True}
            return {'temperature_c': data['temperature']}
        if name == 'forecast':
            return {'temperature_c': data['temperature'] + 5}
        if name == 'convert_length':
            return {'centimetres': arguments['metres'] * 100}
        if name == 'convert_currency':
            return {'cents': arguments['cents'] * 2}
        if name == 'find_record':
            return {'record_id': data['record_id']} if arguments['customer'] == data['customer'] else {'error': 'not_found', 'retryable': False}
        rid = arguments['record_id']
        if fault == 'changed_id' and name == 'read_record' and rid == data['record_id']:
            self.injected = True
            return {'error': 'record_moved', 'record_id': data['current_id'], 'retryable': False}
        if fault == 'denied' and name == 'read_record' and rid == data['record_id']:
            self.injected = True
            return {'error': 'permission_denied', 'alternative': 'public_record', 'retryable': False}
        if rid != (data['current_id'] if fault == 'changed_id' else data['record_id']):
            return {'error': 'not_found', 'retryable': False}
        if name in ('read_record', 'public_record'):
            return {'units': data['units']}
        request_id = arguments['request_id']
        payload = {'record_id': rid, 'text': arguments['text']}
        if request_id in self.notes and self.notes[request_id] != payload:
            return {'error': 'idempotency_conflict', 'retryable': False}
        self.notes[request_id] = payload
        if fault == 'lost_receipt' and not self.injected:
            self.injected = True
            return {'error': 'receipt_lost', 'retryable': True, 'instruction': 'Retry with the same request_id.'}
        return {'note_id': f'N-{data["number"]}'}


def actions(message):
    """Keep malformed argument strings as failures, rather than repairing model output."""
    for call in message.get('tool_calls') or []:
        function = call.get('function', {}) if isinstance(call, dict) else {}
        if not isinstance(function, dict):
            function = {}
        raw = function.get('arguments')
        try:
            parsed = quality.strict_json(raw) if isinstance(raw, str) else None
        except (ValueError, RecursionError):
            parsed = None
        yield {'name': function.get('name'), 'arguments': parsed}


def answer(output, prompt=''):
    # Native assistant messages are stored as JSON; reasoning markers inside strings are data.
    """Extract the assistant's final answer from retained tool-dialogue output."""
    return output


def call_format(calls):
    """A malformed native envelope cannot be sent back as an assistant/tool exchange."""
    if not isinstance(calls, list):
        return False
    ids = []
    for call in calls:
        function = call.get('function') if isinstance(call, dict) else None
        if (not isinstance(function, dict) or call.get('type') != 'function'
            or not isinstance(call.get('id'), str) or not call['id']
            or not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), str)):
            return False
        ids.append(call['id'])
    return len(ids) == len(set(ids))


def grade(case, output):
    """Replay native decisions against the same world; no supplied result can forge credit."""
    try:
        turns = quality.strict_json(output)
        if not isinstance(turns, list):
            turns = []
    except (ValueError, RecursionError):
        turns = []
    world, calls, checks, retries, seen = World(case), [], [], 0, {}
    final, successful, after_error = '', [], []
    for message in turns[:PROTOCOL['max_turns']]:
        if not isinstance(message, dict):
            break
        selected = list(actions(message))
        if not selected:
            final = message.get('content') or ''
            break
        if not call_format(message['tool_calls']):
            checks.append({'field': 'tool_call_format', 'expected': 'function calls with distinct nonempty IDs, names and argument strings',
                           'actual': message['tool_calls'], 'passed': False})
            break
        for action in selected:
            if len(calls) >= PROTOCOL['max_calls']:
                break
            key = json.dumps(action, sort_keys=True)
            if key in seen and not seen[key].get('retryable'):
                retries += 1
            response = world.call(action['name'], action['arguments'])
            seen[key] = response
            calls.append(action)
            if 'error' not in response:
                successful.append(action)
                if world.injected:
                    after_error.append(action)
    for i in range(max(len(calls), len(case['steps']))):
        expected = case['steps'][i] if i < len(case['steps']) else None
        actual = calls[i] if i < len(calls) else None
        for field in ('name', 'arguments'):
            checks.append({'field': f'call-{i+1}.{field}', 'expected': expected and expected[field],
                           'actual': actual and actual[field], 'passed': bool(expected and actual and quality.same(actual[field], expected[field]))})
    fields, final_checks = quality.grade_fields({'expected': case['expected']}, quality.final_answer(final) if isinstance(final, str) else '')
    # A guessed final answer does not complete a tool task. Every required distinct call must succeed.
    required = []
    for step in case['steps']:
        if step not in required:
            required.append(step)
    # Error steps are prerequisites for recovery, but are deliberately not successful calls.
    required = [step for step in required if not (case['world']['fault'] in ('changed_id', 'denied') and step['name'] == 'read_record' and step['arguments']['record_id'] == case['world']['record_id'])]
    completed = fields['passed'] and all(step in successful for step in required)
    if 'note_id' in case['expected']:
        completed = completed and len(world.notes) == 1 and next(iter(world.notes.items()), None) == (
            case['world']['request_id'], {'record_id': case['world']['record_id'], 'text': case['world']['note']})
    recovery = bool(case['world']['fault'])
    completed = completed and (not recovery or (world.injected and case['steps'][-1] in after_error))
    recovered = recovery and world.injected and completed
    checks += final_checks + [{'field': 'task_completed', 'expected': True, 'actual': bool(completed), 'passed': bool(completed)}]
    if recovery:
        checks.append({'field': 'recovered_after_injected_error', 'expected': True, 'actual': bool(recovered), 'passed': bool(recovered)})
    selected = sum(c['passed'] for c in checks if c['field'].endswith('.name'))
    arguments = sum(c['passed'] for c in checks if c['field'].endswith('.arguments'))
    denominator = max(len(calls), len(case['steps']))
    return {'correct': sum(c['passed'] for c in checks), 'checks': len(checks), 'passed': all(c['passed'] for c in checks) and retries == 0,
            'selection_correct': selected, 'argument_correct': arguments, 'call_checks': denominator,
            'completed': bool(completed), 'recovery_case': recovery, 'recovered': bool(recovered),
            'injected_error_seen': world.injected, 'unnecessary_retries': retries, 'tool_calls': len(calls)}, checks


def summarize(samples):
    """Aggregate tool-dialogue checks without executing model-supplied code."""
    values = [s['values'] for s in samples]
    supported = [v for v in values if not v.get('unsupported')]
    result = {'cases': len(values), 'unsupported_cases': len(values)-len(supported),
              'available_cases': sum(bool(v.get('success')) for v in supported)}
    if not supported:
        return result  # Missing score, rather than zero correctness for an unsupported model.
    checks = sum(v.get('call_checks', 0) for v in supported)
    recovery = sum(bool(v.get('recovery_case')) for v in supported)
    result.update(selection_accuracy=100*sum(v.get('selection_correct', 0) for v in supported)/checks if checks else 0,
                  argument_accuracy=100*sum(v.get('argument_correct', 0) for v in supported)/checks if checks else 0,
                  task_completion=100*sum(bool(v.get('completed')) for v in supported)/len(supported),
                  capped_cases=sum(bool(v.get('generation_capped')) for v in supported))
    if recovery:
        result['recovery_accuracy'] = 100*sum(bool(v.get('recovered')) for v in supported)/recovery
    for key in ('unnecessary_retries', 'tool_calls', 'input_tokens', 'output_tokens'):
        result[key] = sum(v.get(key, 0) for v in supported)
    return result


def breakdown(samples):
    """Group retained tool-dialogue outcomes by their case dimensions."""
    groups = {}
    for sample in samples:
        groups.setdefault(sample['values'].get('category', ''), []).append(sample)
    return [{'category': category, **summarize(group)} for category, group in sorted(groups.items())]


async def measure(worker, run, ceiling, protocol, make_cases, grader=None):
    """Run a synthetic tool dialogue through the bounded native probe."""
    number = run['id']
    if run['subject'].get('protocol', {}).get('implementation_sha256') != protocol.get('implementation_sha256'):
        raise ValueError('Benchmark implementation changed after submission; submit a new run.')
    plan = worker.store.plan(number)
    if plan is None:
        plan = worker.store.plan(number, {'protocol': protocol, 'signature': quality.signature(protocol),
                                        'cases': make_cases(quality.context_limit(worker, run, ceiling))})
    if plan['signature'] != quality.signature(protocol):
        raise ValueError('Benchmark protocol changed; submit a new run instead of mixing case versions.')
    flags = run['subject']['settings']['flags']
    unsupported = (not run['subject'].get('metadata', {}).get('takes_tools')
                   or flags.get('jinja') is False or flags.get('no-jinja') is True)
    for case in plan['cases']:
        if worker.store.has_sample(number, case['id']):
            continue
        await worker.check(number)
        if unsupported:
            worker.store.keep(number, case['id'], {'success': False, 'unsupported': True, 'category': case['category'],
                'error': 'The recorded model template does not advertise tools, or jinja is explicitly disabled.'}, '', '', {'case': case, 'checks': []})
            continue
        await evaluate(worker, number, case, grader)


async def evaluate(worker, number, case, grader=None):
    """Finish one conversation atomically. An interruption discards its private mock world."""
    world, turns, exchanges, logs, totals = World(case), [], [], [], {'success': True}
    messages = json.loads(json.dumps(case.get('messages', [
        {'role': 'system', 'content': 'Use the supplied tools to complete the task. Recover from tool errors when possible. '
         'Do not guess tool results. Finish with a JSON object containing the requested answer keys.'},
        {'role': 'user', 'content': case['question']}])))
    calls = 0
    try:
        for turn in range(PROTOCOL['max_turns']):
            await worker.check(number)
            worker.store.message(number, f'{case["id"]}: evaluating at {case["context"]:,} context tokens; tool turn {turn+1}.')
            field = 'evaluation' if case.get('native_evaluation') else 'dialogue'
            evaluation = {'messages': messages, 'tools': case['tools']}
            if 'sampling' in case:
                evaluation.update(temperature=case['temperature'], sampling=case['sampling'])
            result = await worker.isolated(number, f'{case["id"]}:turn-{turn+1}', {
                'context': case['context'], 'inputs': 0, 'outputs': case['outputs'],
                field: evaluation})
            await worker.check(number)
            values = result['values']
            for key in ('input_tokens', 'output_tokens', 'load_seconds', 'request_seconds'):
                totals[key] = totals.get(key, 0) + values.get(key, 0)
            totals['generation_capped'] = totals.get('generation_capped', False) or values.get('generation_capped', False)
            totals['peak_gpu_mib'] = max(totals.get('peak_gpu_mib', 0), values.get('peak_gpu_mib') or 0)
            exchanges.append({'turn': turn+1, 'request': json.loads(json.dumps(messages)), 'probe': result})
            logs.append(result['log'])
            if not values['success']:
                totals.update(success=False, unsupported=bool(values.get('unsupported')), error=values.get('error', 'Probe unavailable.'))
                break
            message = result['evidence']['native_message'] if case.get('native_evaluation') else json.loads(result['output'])
            turns.append(message)
            selected = list(actions(message))
            if not selected:
                break
            native_calls = message['tool_calls']
            repeated = False
            if case.get('coverage_version') == 2 and isinstance(native_calls, list):
                prior_ids = {c['id'] for m in messages for c in m.get('tool_calls', [])}
                repeated = any(c.get('id') in prior_ids for c in native_calls if isinstance(c, dict))
            if not call_format(native_calls) or repeated:
                totals['error'] = 'Model returned malformed native function calls or missing/duplicate IDs.'
                break
            ids = [c['id'] for c in native_calls]
            messages.append({key: message[key] for key in ('role', 'content', 'tool_calls') if key in message})
            for action, call_id in zip(selected, ids):
                if calls >= PROTOCOL['max_calls']:
                    break
                calls += 1
                response = world.call(action['name'], action['arguments'])
                messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(response)})
            if calls >= PROTOCOL['max_calls']:
                break
    except asyncio.CancelledError:
        import httpx
        from .worker import Retry
        try:
            await asyncio.shield(worker.request(number, 'POST', '/interrupt', timeout=5))
        except (Retry, httpx.HTTPError, asyncio.CancelledError):
            pass
        raise
    output = json.dumps(turns, ensure_ascii=False)
    metrics, checks = (grader or grade)(case, output if totals['success'] else '')
    evidence = {'case': case, 'answer': output, 'checks': checks, 'tool_exchanges': exchanges,
                'conversation': messages,
                'prompt': exchanges[0]['probe'].get('evidence', {}).get('prompt', '') if exchanges else ''}
    if 'sampling' in case:
        evidence['request_sampling'] = (exchanges[0]['probe'].get('evidence', {}).get('request_sampling')
                                        if exchanges else None)
    worker.store.keep(number, case['id'], {**totals, **metrics, 'context_tokens': case['context'], 'category': case['category']},
                      output, '\n'.join(logs), evidence)
    worker.store.message(number, f'{case["id"]}: ' + ('passed.' if metrics['passed'] else 'did not pass.'))
