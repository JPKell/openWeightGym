"""Sixty native tool decisions over the existing fixed in-memory handlers."""

from collections import Counter
import json

from jsonschema import Draft202012Validator

from .. import quality, tooling

SEED=20261015
OUTPUTS=32768
SAMPLING={'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
          'presence_penalty':0.0,'frequency_penalty':0.0}
SIZES={'one_call':15,'no_call':10,'two_calls':10,'derived_arguments':10,'near_miss':10,'follow_up':5}
SYSTEM=('Function use is optional: use only the supplied functions needed for the task. '
        'Do not guess unavailable tool results or call an unrelated function. '
        'Use provided tool results as data. Finish with a JSON object containing the requested keys.')
DEFINITION=tooling.definition('tool_use','Tool use','Choose, parameterize and finish controlled native function workflows.')
DEFINITION['method']=('Sixty fixed-seed cases: single calls, no-call controls, two calls, derived arguments, '
    'near-miss functions and supplied-result follow-ups, each with three to eight function descriptions. '
    'Names, JSON-schema validity, required values, extra arguments, call counts and observed dependencies '
    'are checked separately. Only fixed in-memory handlers run; no real tools or model-generated code. '
    'The headline is strict completed tasks. Native requests use temperature 1, seed 42 and explicit neutral '
    'sampling controls, with at most 32,768 output tokens per turn where context permits. '
    'Expanded real-model qualification is pending.')
DEFINITION['kpis']=[*tooling.KPIS,
    ('valid_argument_accuracy','Arguments valid against function schema','%'),
    ('required_value_accuracy','Required argument values correct','%'),
    ('no_extra_argument_accuracy','Calls without extra arguments','%'),
    ('exact_call_count_accuracy','Exact required call count','%'),
    ('native_call_attempts','Native call attempts','count'),
    ('no_call_cases','Cases requiring no new calls','count'),
    ('calls_when_unneeded','No-call cases with function calls','count'),
    ('unneeded_call_rate','No-call cases given calls','%')]
PROTOCOL={**tooling.PROTOCOL,'version':3,'grader_version':2,'dataset':'tool-use-v2','cases':60,
          'source_seed':SEED,'categories':SIZES,'function_descriptions':[3,8],
          'completion':'all checks, exact call count and actual mock outcomes',
          'outputs':OUTPUTS,'temperature':1.0,'generation_seed':42,
          'sampling':{'temperature':1.0,**SAMPLING}}


def action(name,**arguments):
    """Describe one expected synthetic tool action and its arguments."""
    return dict(name=name,arguments=arguments)


def cases(limit):
    """Build deterministic synthetic tool use fixtures from the case seed."""
    source=quality.Source(SEED)
    result=[]
    for category,count in SIZES.items():
        for index in range(count):
            number=source.number(10000,90000)
            data=dict(number=number,customer=source.person(),record_id='R-'+str(source.number(100000,900000)),
                      current_id='unused',units=source.number(100,9000),temperature=source.random.randrange(-10,31),
                      note='Note '+str(source.number(1000000,9000000)),request_id='REQ-'+str(number),fault=None)
            city=f'Cedar {number}'
            metres=source.number(2,600)
            steps=[]
            seed=[]
            initial_ids=[]
            dependent=False
            ordered=True
            required=[]
            def single(kind):
                """Build one synthetic single-call fixture with data-only expected outcomes."""
                if kind==0:
                    return f'Use current weather for {city}. Return temperature_c.',[action('weather',city=city)],{'temperature_c':data['temperature']}
                if kind==1:
                    return f'Convert {metres} metres to centimetres using a function. Return centimetres.',[action('convert_length',metres=metres)],{'centimetres':metres*100}
                if kind in (2,3):
                    public=kind==3
                    name='public_record' if public else 'read_record'
                    return f'Read the {"public summary" if public else "private record"} for record id "{data["record_id"]}". Return units.',[action(name,record_id=data['record_id'])],{'units':data['units']}
                return f'For record id "{data["record_id"]}", create exactly one note with text "{data["note"]}" and request_id "{data["request_id"]}". Return note_id.',[action('create_note',record_id=data['record_id'],text=data['note'],request_id=data['request_id'])],{'note_id':f'N-{number}'}
            if category=='one_call':
                question,steps,expected=single(index%5)
                initial_ids=[data['record_id']]
            elif category=='no_call':
                if index%2:
                    question='Schedule a calendar event with the listed functions. If no function can schedule events, return status="unsupported" without calling an unrelated function.'
                    expected={'status':'unsupported'}
                else:
                    token='TOKEN-'+str(source.number(1000000,9000000))
                    question=f'The supplied token is "{token}". Return it under echo. Do not call any function: this task uses supplied text only.'
                    expected={'echo':token}
            elif category=='two_calls':
                other=source.number(2,600)
                ordered=False
                if index%2:
                    question=(f'Convert {metres} metres and {other} metres using the length function, once for each length. '
                              'Return left_cm for the first length and right_cm for the second. Either call order is allowed.')
                    steps=[action('convert_length',metres=metres),action('convert_length',metres=other)]
                    expected=dict(left_cm=metres*100,right_cm=other*100)
                else:
                    question=f'Use current weather for {city} and convert {metres} metres to centimetres. Return temperature_c and centimetres. Either call order is allowed.'
                    steps=[action('weather',city=city),action('convert_length',metres=metres)]
                    expected=dict(temperature_c=data['temperature'],centimetres=metres*100)
            elif category=='derived_arguments':
                if index<5:
                    question=f'A length is {metres*100} centimetres. Work out its integer metres, then use the length-conversion function. Return centimetres.'
                    steps=[action('convert_length',metres=metres)]
                    expected={'centimetres':metres*100}
                else:
                    first,last=data['customer'].split(' ')
                    question=f'Find customer "{last}, {first}" (normalize to first name then last name), then read the private record using the returned ID. Return units.'
                    steps=[action('find_record',customer=data['customer']),action('read_record',record_id=data['record_id'])]
                    expected={'units':data['units']}
                    dependent=True
            elif category=='near_miss':
                variant=index%6
                if variant==0:
                    question,steps,expected=single(0)
                    required=['forecast']
                elif variant==1:
                    question=f"Use tomorrow's forecast for {city}, not current weather. Return temperature_c."
                    steps=[action('forecast',city=city)]
                    expected={'temperature_c':data['temperature']+5}
                    required=['weather']
                elif variant in (2,3):
                    question,steps,expected=single(variant)
                    initial_ids=[data['record_id']]
                    required=['public_record' if variant==2 else 'read_record']
                elif variant==4:
                    question,steps,expected=single(1)
                    required=['convert_currency']
                else:
                    cents=source.number(100,9000)
                    question=f'Convert {cents} cents to USD using the currency function. Return cents.'
                    steps=[action('convert_currency',cents=cents,currency='USD')]
                    expected={'cents':cents*2}
                    required=['convert_length']
            else:
                if index==4:
                    question=f'A length conversion for {metres} metres has already returned its result below. Use it to return centimetres; do not repeat a completed call.'
                    previous=action('convert_length',metres=metres)
                    expected={'centimetres':metres*100}
                else:
                    write=index%2==1
                    question=f'Find customer "{data["customer"]}"; its lookup result is provided below. '
                    if write:
                        question+=f'Create exactly one note with text "{data["note"]}" and request_id "{data["request_id"]}" using that returned ID. Return note_id.'
                        steps=[action('create_note',record_id=data['record_id'],text=data['note'],request_id=data['request_id'])]
                        expected={'note_id':f'N-{number}'}
                    else:
                        question+='Read the private record using that returned ID. Return units.'
                        steps=[action('read_record',record_id=data['record_id'])]
                        expected={'units':data['units']}
                    previous=action('find_record',customer=data['customer'])
                    initial_ids=[data['record_id']]
                required.append(previous['name'])
                # Read-only fixed handler supplies this seed result; it is actual fixture data.
                provisional=dict(world=data,tools=[tooling.TOOLS[previous['name']]])
                response=tooling.World(provisional).call(previous['name'],previous['arguments'])
                seed=[dict(role='assistant',content=None,tool_calls=[dict(id='seed',type='function',function={
                        'name':previous['name'],'arguments':json.dumps(previous['arguments'])})]),
                      dict(role='tool',tool_call_id='seed',content=json.dumps(response))]
            needed=list(dict.fromkeys([s['name'] for s in steps]+required))
            names=needed+[name for name in tooling.TOOLS if name not in needed]
            extras=names[len(needed):]
            source.random.shuffle(extras)
            size=source.random.randint(max(3,len(needed)),8)
            names=needed+extras[:size-len(needed)]
            source.random.shuffle(names)
            result.append(dict(id=f'{category}-{index+1}',category=category,coverage_version=2,native_evaluation=True,
                context=min(limit,131072),outputs=min(OUTPUTS,limit//2),inputs=0,question=question,
                temperature=1.0,sampling=dict(SAMPLING),
                messages=[dict(role='system',content=SYSTEM),dict(role='user',content=question),*seed],
                tools=[tooling.TOOLS[name] for name in names],steps=steps,expected=expected,world=data,
                ordered=ordered,requires_lookup=dependent,initial_record_ids=initial_ids))
    return result


def grade(case,output):
    """Apply data-only synthetic tool use checks to the final answer."""
    if case.get('coverage_version')!=2:
        return tooling.grade(case,output)  # preserved P4 v1 correctness and evidence
    try:
        messages=quality.strict_json(output)
        if not isinstance(messages,list):
            messages=[]
    except (ValueError,RecursionError):
        messages=[]
    world=tooling.World(case)
    actual=[]
    responses=[]
    checks=[]
    final=''
    known=set(case['initial_record_ids'])
    attempts=0
    ids={call['id'] for message in case['messages'] for call in message.get('tool_calls',[])}
    checks.append(dict(field='bounded_history',passed=len(messages)<=PROTOCOL['max_turns']))
    for turn,message in enumerate(messages[:PROTOCOL['max_turns']]):
        if not isinstance(message,dict) or message.get('role')!='assistant':
            checks.append(dict(field='native_assistant_message',passed=False))
            break
        if message.get('tool_calls') is not None and not isinstance(message['tool_calls'],list):
            attempts+=1  # malformed attempted call, never executed
            checks.append(dict(field='native_call_envelope',passed=False))
            break
        selected=list(tooling.actions(message))
        attempts+=len(selected)
        if not selected:
            final=message.get('content') or ''
            checks.append(dict(field='final_message_is_last',passed=turn==len(messages)-1))
            break
        calls=message.get('tool_calls')
        valid=tooling.call_format(calls) and not ids.intersection(c['id'] for c in calls)
        checks.append(dict(field='native_call_envelope',passed=valid))
        if not valid:
            break
        ids.update(c['id'] for c in calls)
        learned=set()
        for action in selected:
            if len(actual)>=PROTOCOL['max_calls']:
                checks.append(dict(field='call_limit',passed=False))
                break
            if case['requires_lookup'] and action['name'] in ('read_record','create_note'):
                observed=isinstance(action['arguments'],dict) and action['arguments'].get('record_id') in known
                checks.append(dict(field='lookup_result_observed_before_use',passed=bool(observed)))
            response=world.call(action['name'],action['arguments'])
            actual.append(action)
            responses.append(response)
            if response.get('record_id'):
                learned.add(response['record_id'])
        known.update(learned)  # only subsequent assistant turns have received these results
    expected=list(case['steps'])
    pairs=[]
    if case['ordered']:
        pairs=[(expected[i] if i<len(expected) else None,actual[i] if i<len(actual) else None)
               for i in range(max(len(expected),len(actual)))]
    else:
        remaining=list(expected)
        for got in actual:
            match=next((i for i,wanted in enumerate(remaining) if quality.same(got,wanted)),None)
            if match is None:
                match=next((i for i,wanted in enumerate(remaining) if got['name']==wanted['name']),None)
            wanted=remaining.pop(match if match is not None else 0) if remaining else None
            pairs.append((wanted,got))
        pairs.extend((wanted,None) for wanted in remaining)
    selected=arguments=schemas=required=extras=0
    for index,(wanted,got) in enumerate(pairs,1):
        params=got and got['arguments']
        schema=next((t['function']['parameters'] for t in case['tools'] if got and t['function']['name']==got['name']),None)
        name_ok=bool(wanted and got and wanted['name']==got['name'])
        schema_ok=bool(schema is not None and Draft202012Validator(schema).is_valid(params))
        values_ok=bool(wanted and isinstance(params,dict) and all(k in params and quality.same(params[k],v) for k,v in wanted['arguments'].items()))
        extras_ok=bool(wanted and isinstance(params,dict) and params.keys()==wanted['arguments'].keys())
        exact=bool(wanted and got and quality.same(wanted['arguments'],params))
        selected+=name_ok
        arguments+=exact
        schemas+=schema_ok
        required+=values_ok
        extras+=extras_ok
        for label,passed in (('name',name_ok),('schema',schema_ok),('required_values',values_ok),('no_extra_arguments',extras_ok)):
            checks.append(dict(field=f'call-{index}.{label}',passed=passed,expected=wanted,actual=got))
    count_ok=attempts==len(expected)
    _,field_checks=quality.grade_fields({'expected':case['expected']},quality.final_answer(final) if isinstance(final,str) else '')
    checks+=field_checks+[dict(field='exact_call_count',passed=count_ok,expected=len(expected),actual=attempts),
                          dict(field='actual_mock_outcomes_succeeded',passed=all('error' not in r for r in responses))]
    if any(step['name']=='create_note' for step in expected):
        wanted_notes={s['arguments']['request_id']:dict(record_id=s['arguments']['record_id'],text=s['arguments']['text'])
                      for s in expected if s['name']=='create_note'}
        checks.append(dict(field='exact_note_effects',passed=world.notes==wanted_notes))
    completed=all(c['passed'] for c in checks)
    signature=lambda item:json.dumps(item,sort_keys=True)
    expected_counts=Counter(map(signature,expected))
    retries=sum(max(0,n-expected_counts[key]) for key,n in Counter(map(signature,actual)).items()
                if n>1)
    return dict(coverage_version=2,correct=sum(c['passed'] for c in checks),checks=len(checks),passed=completed,completed=completed,
        selection_correct=selected,argument_correct=arguments,valid_argument_correct=schemas,required_value_correct=required,
        no_extra_argument_correct=extras,call_checks=len(pairs),exact_call_count=count_ok,no_call_case=not expected,
        called_when_unneeded=not expected and attempts>0,native_call_attempts=attempts,
        tool_calls=len(actual),unnecessary_retries=retries,
        recovery_case=False,recovered=False,injected_error_seen=False),checks


def summarize(samples):
    """Summarize retained synthetic tool use samples and supported scores."""
    result=tooling.summarize(samples)
    supported=[s['values'] for s in samples if not s['values'].get('unsupported') and s['values'].get('coverage_version')==2]
    if not supported:
        return result
    count=sum(v.get('call_checks',0) for v in supported)
    if count:
        for key,field in (('valid_argument_accuracy','valid_argument_correct'),('required_value_accuracy','required_value_correct'),
                          ('no_extra_argument_accuracy','no_extra_argument_correct')):
            result[key]=100*sum(v.get(field,0) for v in supported)/count
    elif len(supported)==len([s for s in samples if not s['values'].get('unsupported')]):
        result.pop('selection_accuracy',None)
        result.pop('argument_accuracy',None)
    result['exact_call_count_accuracy']=100*sum(v.get('exact_call_count',False) for v in supported)/len(supported)
    result['native_call_attempts']=sum(v.get('native_call_attempts',0) for v in supported)
    no_calls=[v for v in supported if v.get('no_call_case')]
    result.update(no_call_cases=len(no_calls),calls_when_unneeded=sum(bool(v.get('called_when_unneeded')) for v in no_calls))
    if no_calls:
        result['unneeded_call_rate']=100*result['calls_when_unneeded']/len(no_calls)
    return result


def breakdown(samples):
    """Group retained synthetic tool use outcomes by case dimensions."""
    groups={}
    for sample in samples:
        groups.setdefault(sample['values'].get('category',''),[]).append(sample)
    return [dict(category=category,**summarize(group)) for category,group in sorted(groups.items())]


async def measure(worker,run,ceiling):
    """Run the synthetic tool use protocol through the existing benchmark probe."""
    await tooling.measure(worker,run,ceiling,PROTOCOL,cases,grade)


saved_answer=tooling.answer
