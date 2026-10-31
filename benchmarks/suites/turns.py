"""Fixed histories measure retained instructions/facts and authorized update lifetimes."""

import json

from .. import quality

SEED = 20261016
OUTPUTS = 32768
SAMPLING = {'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
            'presence_penalty':0.0,'frequency_penalty':0.0}
CATEGORIES = ('unit_rules','record_facts','settings_permanent','settings_temporary')
STANDARD = 4  # per category: one event and plain archive notes, as in version 1; the other eight are hard
DEFINITION = quality.definition('turns','Multi-turn consistency',
    'Keep early facts and instructions, apply lasting changes and expire one-answer exceptions.',
    'Forty-eight fixed-seed histories with ten to eighteen user turns, four checks each. Twelve '
    'cases each cover output-unit rules, corrected records, permanent setting changes with a '
    'revert, and exceptions that last a stated number of listings. Hard temporary cases stagger '
    'one- and two-listing exceptions so the final active fields vary. Only a line in the stated '
    'grammar changes anything; quoted, forwarded, archived and other-code lines look alike but do not. '
    'Hard record cases use multiline corrections to inactive records across activation cycles. '
    'In hard cases, the last earlier listing in the history is out of date. Half introduce the active rules in system, '
    'half in the first user message. Earlier assistant replies are canonical fixture data; only '
    'the final answer is generated. Native text conversations are unpadded, use temperature 1 '
    'with neutral sampling filters and penalties, and allow up to 32,768 output tokens. Real-model '
    'qualification is pending.',
    [quality.FORMAT,('unsupported_cases','Unsupported cases','count'),
     ('system_accuracy','System-origin checks correct','%'),('user_accuracy','User-origin checks correct','%'),
     *[(kind+'_accuracy',kind.replace('_',' ').title()+' checks correct','%') for kind in CATEGORIES]])
PROTOCOL = {**quality.PROTOCOL,'version':6,'dataset':'turns-v5','seed':SEED,'generation_seed':42,
            'temperature':1.0,'sampling':{'temperature':1.0,**SAMPLING},'outputs':OUTPUTS,
            'cases':48,'checks':192,'user_turns':[10,18],'origins':['system','user'],'standard':STANDARD}
TONES = ('brief','detailed','formal','friendly','terse')
REGIONS = ('north','south','east','west')
UNITS = dict(length=('cm','m'),duration=('s','min'),cost=('cents','dollars'),mass=('g','kg'))  # small unit first
FACTOR = dict(length=100,duration=60,cost=100,mass=1000)  # small units in the large one
SETTING_FIELDS = ('tone','label','budget','region')
RECORD_FIELDS = ('coordinator','code','count','due')
GRAMMAR=('Only a line in a user message that begins `UPDATE {code}:` followed by field=value pairs changes a setting. With nothing '
    'after the pairs the change is permanent; a suffix `(next listing only)` or `(next 2 listings)` makes it count for that many '
    'of your listings, after which the setting is what it was. `REVERT {code}: field` returns that field to the value it '
    'had before its last permanent change. Quoted, forwarded or archived text, and any line for another code, changes nothing.')
RECORD_GRAMMAR=('Only a line in a user message that begins `CORRECTION <record>:` followed by field=value changes a record, whichever '
    'record it names, and only a line in a user message that begins `ACTIVATE <record>` changes which record is active. Quoted, forwarded or '
    'archived text, and anything else, changes nothing.')


def value_of(source, field):
    """A fresh value for a setting or record field."""
    random=source.random
    return {'tone':lambda:random.choice(TONES),'label':lambda:'L-'+str(source.number(10000,90000)),
            'budget':lambda:source.number(100,9000),'region':lambda:random.choice(REGIONS),
            'coordinator':source.person,'code':lambda:'C-'+str(source.number(10000,90000)),
            'count':lambda:source.number(100,9000),'due':lambda:source.date().isoformat()}[field]()


def different(source, field, current):
    """Choose a deterministic replacement distinct from the current fixture value."""
    for _ in range(100):
        value=value_of(source,field)
        if value!=current:
            return value
    raise ValueError('No different value was found.')


def other_unit(field, unit):
    """Choose a different measurement unit for the correction fixture."""
    return UNITS[field][1] if unit==UNITS[field][0] else UNITS[field][0]


def replay_settings(initial, events):
    """The state each listing shows. Events: ('set',values), ('temp',values,count), ('revert',field), ('list',).

    A permanent change remembers the value it replaced; a revert returns the field to the one before the last
    change. An exception covers its next `count` listings and then lapses.
    """
    state=dict(initial)
    before={f:[] for f in initial}
    exceptions=[]
    shown=[]
    for kind,*rest in events:
        if kind=='set':
            for field,value in rest[0].items():
                before[field].append(state[field])
                state[field]=value
        elif kind=='temp':
            exceptions.append([rest[0],rest[1]])
        elif kind=='revert':
            state[rest[0]]=before[rest[0]].pop()
        elif kind=='list':
            seen=dict(state)
            for values,_ in exceptions:
                seen.update(values)
            shown.append(seen)
            exceptions=[[v,n-1] for v,n in exceptions if n>1]
    return shown


def replay_records(records, active, events):
    """Events: ('correct',id,field,value), ('activate',id), ('list',). A listing shows the active record."""
    records={k:dict(v) for k,v in records.items()}
    shown=[]
    for kind,*rest in events:
        if kind=='correct':
            records[rest[0]][rest[1]]=rest[2]
        elif kind=='activate':
            active=rest[0]
        elif kind=='list':
            shown.append(dict(records[active]))
    return shown


def ask_units(source, units):
    """A request for totals of two entries per quantity in mixed units, and the answer in the units that apply."""
    parts=[]
    answer={}
    for field,unit in units.items():
        small,big=UNITS[field]
        factor=FACTOR[field]
        total=source.random.randrange(3,99)*factor
        first=source.random.randrange(1,total//factor)*factor
        parts.append(f'{field}: '+(f'{first//factor} {big} and {total-first} {small}' if source.random.random()<.5 else f'{total-first} {small} and {first//factor} {big}'))
        answer[field]=total//factor if unit==big else total
    return 'List the totals. Entries to add:\n'+'\n'.join(parts),answer


def lookalike(source, real):
    """A line that would change something, quoted or forwarded so that it does not."""
    return source.random.choice((f'Forwarded from {source.person()}: "{real}"',f'> {real}',
        f'Archive note A-{source.number(10000,90000)} quotes this line: {real}'))


def archive(source):
    """Build the synthetic archived record used in the conversation case."""
    return f'Archive note A-{source.number(10000,90000)} concerns {source.person()} on an unrelated project. File it separately.'


def history(category, hard, source):
    """The opening text, the initial state, the real events (the last is the final listing) and the lines that change nothing."""
    random=source.random
    if category=='record_facts':
        ids=['R-'+str(source.number(10000,90000)) for _ in range(3)]
        records={i:{f:value_of(source,f) for f in RECORD_FIELDS} for i in ids}
        opening=('Project records: '+json.dumps(records)+f'. The active record is {ids[0]}. When the user asks you to list the active record, '
            'return its coordinator, code, count and due. '+RECORD_GRAMMAR)
        events=[]
        if hard:
            first,final_id,other=random.sample(ids,3)
            current={record:dict(fields) for record,fields in records.items()}
            def correction(record,field):
                """Render a correction to a previously supplied synthetic fact."""
                value=different(source,field,current[record][field])
                current[record][field]=value
                return ('correct',record,field,value)
            # The stale listing names the final record, but precedes two of its corrections.
            events=[correction(first,'count'),('activate',final_id),
                    correction(final_id,'coordinator'),correction(final_id,'code'),('list',),
                    ('activate',other),correction(final_id,'count'),correction(final_id,'due'),
                    ('activate',final_id)]
        else:
            events=[('correct',ids[0],'count',different(source,'count',records[ids[0]]['count']))]
        final=([e[1] for e in events if e[0]=='activate'] or [ids[0]])[-1]  # the record the final listing shows
        final_record=replay_records(records,ids[0],events+[('list',)])[-1]
        decoys=[(lookalike(source,f'CORRECTION {final}: {f}={different(source,f,final_record[f])}'),f) for f in RECORD_FIELDS] if hard else []
        if hard:
            decoys.append((f"{final}'s coordinator is now {source.person()}.",None))
        return opening,(records,ids[0]),events+[('list',)],decoys
    units=category=='unit_rules'
    fields=tuple(UNITS) if units else SETTING_FIELDS
    code='K-'+str(source.number(10000,90000))
    initial={f:random.choice(UNITS[f]) for f in UNITS} if units else {f:value_of(source,f) for f in SETTING_FIELDS}
    opening=(f'Settings {code}: '+('output units ' if units else '')+json.dumps(initial)+'. '+
        ('Conversions: 1 m = 100 cm; 1 min = 60 s; 1 dollar = 100 cents; 1 kg = 1000 g. When the user asks you to list the totals, add the two entries for each quantity and give each total as a whole number in the unit now set. '
         if units else 'When the user asks you to list the settings, return tone, label, budget and region. ')+GRAMMAR.format(code=code))
    now=dict(initial)
    def change(field):
        """Update the fixture's synthetic fact and add its correction turn."""
        now[field]=other_unit(field,now[field]) if units else different(source,field,now[field])
        return {field:now[field]}
    if units:  # one lasting change, and when hard one exception for the next listing
        a,b=random.sample(fields,2)
        events=[('set',change(a))]
        if hard:
            events+=[('temp',{b:other_unit(b,now[b])},1),('list',)]
    elif category=='settings_temporary':
        if hard:
            first,second,third,fourth=random.sample(fields,4)
            # A and B expire after the second listing; C and D remain at the final listing.
            events=[('temp',{first:different(source,first,now[first])},2),
                    ('temp',{second:different(source,second,now[second])},2),('list',),
                    ('temp',{third:different(source,third,now[third])},2),
                    ('temp',{fourth:different(source,fourth,now[fourth])},2),('list',)]
        else:
            events=[('temp',{f:different(source,f,now[f])},1) for f in random.sample(fields,1)]
            events.append(('list',))
    else:
        if hard:
            doubled,other,third,fourth=random.sample(fields,4)
            # One REVERT after two changes must reveal the intermediate, not initial, value.
            # The stale listing precedes that revert and two other independent changes.
            events=[('set',change(doubled)),('set',change(other)),('set',change(doubled)),
                    ('list',),('revert',doubled),('set',change(third)),('set',change(fourth))]
        else:
            events=[('set',change(fields[0]))]
    decoys=[]
    if hard:
        final_settings=replay_settings(initial,events+[('list',)])[-1]
        def decoy_value(field):
            """Choose a distractor distinct from the currently authoritative value."""
            return other_unit(field,final_settings[field]) if units else different(source,field,final_settings[field])
        for field in fields:
            decoys.append((lookalike(source,f'UPDATE {code}: {field}={decoy_value(field)}'),field))
        field=random.choice(fields)
        decoys.append((f'UPDATE K-{source.number(10000,90000)}: {field}={decoy_value(field)}',None))
    return opening,initial,events+[('list',)],decoys,code


def line_of(event, code):
    """Find the synthetic record's line in the rendered source material."""
    kind,*rest=event
    if kind=='set':
        return f'UPDATE {code}: '+', '.join(f'{f}={v}' for f,v in rest[0].items())
    if kind=='temp':
        return f'UPDATE {code}: '+', '.join(f'{f}={v}' for f,v in rest[0].items())+(' (next listing only)' if rest[1]==1 else f' (next {rest[1]} listings)')
    if kind=='revert':
        return f'REVERT {code}: {rest[0]}'
    if kind=='correct':
        return f'CORRECTION {rest[0]}: {rest[1]}={rest[2]}'
    return f'ACTIVATE {rest[0]}'


def build(category, hard, index, source):
    """One history as turns, a final question and its key; None if its last listing is not out of date."""
    made=history(category,hard,source)
    opening,initial,events,decoys=made[:4]
    code=made[4] if len(made)>4 else ''
    units=category=='unit_rules'
    if category=='record_facts':
        shown=replay_records(*initial,events)
    else:
        shown=replay_settings(initial,[e for e in events])
    lines=[]
    answers=iter(shown)
    final=None
    for position,event in enumerate(events):
        if event[0]=='list':
            if units:
                question,answer=ask_units(source,next(answers))
            else:
                question,answer=('List the active record.' if category=='record_facts' else 'List the current settings.'),next(answers)
            if position==len(events)-1:
                final,key=question,answer
            else:
                lines.append((question,json.dumps(answer)))
        else:
            lines.append((line_of(event,code),'Noted.'))
    if hard and not units and (len(shown)<2 or shown[-2]==shown[-1] or shown[-1]==(initial[0][initial[1]] if category=='record_facts' else initial)):
        return None
    for text,field in decoys:  # a lookalike for a field comes after the last real change to it, so obeying it is wrong at the end
        touching={line_of(e,code) for e in events if field and ((e[0] in ('set','temp') and field in e[1]) or (e[0]=='revert' and e[1]==field)
                                                                or (e[0]=='correct' and e[2]==field))}
        floor=max([i+1 for i,line in enumerate(lines) if line[0] in touching],default=0)
        lines.insert(source.random.randrange(floor,len(lines)+1),(text,'Noted.'))
    for _ in range(max(0,10+index%9-(len(lines)+2))):
        lines.insert(source.random.randrange(len(lines)+1),(archive(source),'Noted.'))
    return opening,lines,final,key,initial


def staggered_temporary(index):
    """Replace one hard case after its legacy draws, using a separate fixed case stream."""
    source=quality.Source(SEED+1000+index)
    fields=list(SETTING_FIELDS)
    source.random.shuffle(fields)
    first,second,third,fourth=fields
    code='K-'+str(source.number(10000,90000))
    initial={field:value_of(source,field) for field in SETTING_FIELDS}
    opening=(f'Settings {code}: '+json.dumps(initial)+'. When the user asks you to list the settings, '
             'return tone, label, budget and region. '+GRAMMAR.format(code=code))
    first_value=different(source,first,initial[first])
    final_value=different(source,first,initial[first])
    while final_value==first_value:
        final_value=different(source,first,initial[first])
    values={field:different(source,field,initial[field]) for field in (second,third,fourth)}
    # At the second listing B, C and D are visible. By the final listing B has expired,
    # A has returned, and C/D expire according to a different schedule in each tier.
    duration=((2,2),(1,2),(2,1))[(index-STANDARD)%3]
    events=[('temp',{first:first_value},1),('temp',{second:values[second]},2),('list',),
            ('temp',{third:values[third]},duration[0]),
            ('temp',{fourth:values[fourth]},duration[1]),('list',),
            ('temp',{first:final_value},1),('list',)]
    shown=replay_settings(initial,events)
    if sum(shown[-2][field]!=shown[-1][field] for field in SETTING_FIELDS)<2:
        raise ValueError('The final temporary state must defeat the stale listing.')
    lines=[]
    answers=iter(shown)
    for event in events[:-1]:
        if event[0]=='list':
            lines.append(('List the current settings.',json.dumps(next(answers))))
        else:
            lines.append((line_of(event,code),'Noted.'))
    key=shown[-1]
    for field in SETTING_FIELDS:
        decoy=lookalike(source,f'UPDATE {code}: {field}={different(source,field,key[field])}')
        floor=max((i+1 for i,(line,_) in enumerate(lines)
                   if line.startswith(f'UPDATE {code}: {field}=')),default=0)
        lines.insert(source.random.randrange(floor,len(lines)+1),(decoy,'Noted.'))
    other=code
    while other==code:
        other='K-'+str(source.number(10000,90000))
    field=source.random.choice(SETTING_FIELDS)
    lines.insert(source.random.randrange(len(lines)+1),
                 (f'UPDATE {other}: {field}={different(source,field,key[field])}','Noted.'))
    for _ in range(max(0,10+index%9-(len(lines)+2))):
        lines.insert(source.random.randrange(len(lines)+1),(archive(source),'Noted.'))
    return opening,lines,'List the current settings.',key,initial


def dense_records(index):
    """Replace one hard record history after legacy draws, using a separate fixed case stream."""
    source=quality.Source(SEED+2000+index)
    ids=['R-'+str(source.number(10000,90000)) for _ in range(3)]
    original={record:{field:value_of(source,field) for field in RECORD_FIELDS} for record in ids}
    current={record:dict(fields) for record,fields in original.items()}
    first,final_id,other=ids[0],*source.random.sample(ids[1:],2)
    fields=list(RECORD_FIELDS)
    source.random.shuffle(fields)
    a,b,c,d=fields
    opening=('Project records: '+json.dumps(original)+f'. The active record is {first}. When the user asks you to list the active record, '
             'return its coordinator, code, count and due. '+RECORD_GRAMMAR)
    lines=[]
    shown=[]
    active=first

    def correct(record,field):
        """Record a correction in the dense conversation fixture."""
        previous=current[record][field]
        value=different(source,field,previous)
        while value==original[record][field]:
            value=different(source,field,previous)
        current[record][field]=value
        return f'CORRECTION {record}: {field}={value}'

    def turn(*commands):
        """Append one synthetic conversation turn and update fixture bookkeeping."""
        lines.append(('\n'.join(commands),'Noted.'))

    def activate(record):
        """Activate the selected synthetic record in the dense fixture."""
        nonlocal active
        active=record
        turn(f'ACTIVATE {record}')

    def listing():
        """Render the current synthetic record list for the conversation fixture."""
        answer=dict(current[active])
        shown.append(answer)
        lines.append(('List the active record.',json.dumps(answer)))

    # Both inactive records receive valid updates, and the final record is revisited after
    # two canonical listings. Each multiline correction is a separate authorized line.
    turn(correct(first,b),correct(other,a),correct(final_id,c))
    activate(final_id)
    turn(correct(final_id,a),correct(other,d))
    listing()
    activate(other)
    turn(correct(final_id,b),correct(final_id,d),correct(other,c))
    activate(final_id)
    listing()
    activate(first)
    late_fields=fields if index%2==0 else fields[1:]
    turn(*(correct(final_id,field) for field in late_fields),correct(first,a))
    activate(final_id)
    key=dict(current[final_id])
    if min(sum(prior[field]!=key[field] for field in RECORD_FIELDS) for prior in shown)<2:
        raise ValueError('Earlier record listings must be materially stale.')
    if sum(original[final_id][field]!=key[field] for field in RECORD_FIELDS)!=4:
        raise ValueError('Initial active-record values must not supply the final key.')
    for field in RECORD_FIELDS:
        wrong=different(source,field,key[field])
        text=lookalike(source,f'CORRECTION {final_id}: {field}={wrong}')
        last=max((i+1 for i,(line,_) in enumerate(lines)
                  if f'CORRECTION {final_id}: {field}=' in line),default=0)
        lines.insert(source.random.randrange(last,len(lines)+1),(text,'Noted.'))
    person=different(source,'coordinator',key['coordinator'])
    lines.insert(source.random.randrange(len(lines)+1),(f"{final_id}'s coordinator is now {person}.",'Noted.'))
    return opening,lines,'List the active record.',key,(original,first)


def cases(limit):
    """Build deterministic multi-turn consistency fixtures from the case seed."""
    source=quality.Source(SEED)
    result=[]
    for category in CATEGORIES:
        for index in range(12):
            for _ in range(200):
                if made:=build(category,index>=STANDARD,index,source):
                    break
            else:
                raise ValueError('No history with an out-of-date last listing was found.')
            if category=='settings_temporary' and index>=STANDARD:
                made=staggered_temporary(index)
            if category=='record_facts' and index>=STANDARD:
                made=dense_records(index)
            opening,lines,final,key,initial=made
            origin='system' if index%2==0 else 'user'
            messages=[dict(role='system',content=opening),dict(role='user',content='Let us continue with that.')] if origin=='system' else [dict(role='user',content=opening)]
            messages.append(dict(role='assistant',content='Understood.'))
            for user,reply in lines:
                messages.extend([dict(role='user',content=user),dict(role='assistant',content=reply)])
            messages.append(dict(role='user',content=quality.json_request(final)))
            result.append(dict(id=f'{category}-{index+1}',category=category,context=limit,inputs=0,
                outputs=min(OUTPUTS,limit//2),
                request={'messages':messages,'temperature':1.0,'sampling':SAMPLING},expected=key,
                source=dict(origin=origin,user_turns=sum(m['role']=='user' for m in messages),initial=initial)))
    return result


def grade(case, answer):
    """Apply data-only multi-turn consistency checks to the final answer."""
    metrics,checks = quality.grade_fields(case,answer)
    metrics['instruction_origin'] = case['source']['origin']
    return metrics,checks


def summarize(samples):
    """Summarize retained multi-turn consistency samples and supported scores."""
    supported = [s for s in samples if not s['values'].get('unsupported')]
    result = quality.summarize(supported)
    if samples:
        result.update(cases=len(samples),unsupported_cases=len(samples)-len(supported),
                      available_cases=sum(bool(s['values'].get('success')) for s in supported))
    for origin in ('system','user'):
        chosen = [s for s in supported if s['values'].get('instruction_origin')==origin]
        if chosen:
            result[origin+'_accuracy'] = quality.summarize(chosen)['accuracy']
    for category in CATEGORIES:
        chosen = [s for s in supported if s['values'].get('category')==category]
        if chosen:
            result[category+'_accuracy'] = quality.summarize(chosen)['accuracy']
    return result


def breakdown(samples):
    """Group retained multi-turn consistency outcomes by case dimensions."""
    groups = {}
    for sample in samples:
        values = sample['values']
        key = values.get('target_input_tokens',0),values.get('category','')
        groups.setdefault(key,[]).append(sample)
    return [dict(input_tokens=size,category=category,**summarize(group))
            for (size,category),group in sorted(groups.items())]


async def measure(worker,run,ceiling):
    """Run the multi-turn consistency protocol through the existing benchmark probe."""
    await quality.measure(worker,run,ceiling,PROTOCOL,cases,grade)
