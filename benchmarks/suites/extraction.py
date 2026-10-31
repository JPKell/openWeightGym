"""Extract eight typed facts about one order, told partly in prose and partly in a messy table, among near misses of that order."""

import datetime

from .. import quality

KINDS=('email','notes','invoice')
FIELDS=('customer','delivery_date','units','venue','contact','email','invoice','total_cents')
SAMPLING={'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
          'presence_penalty':0.0,'frequency_penalty':0.0}
DEFINITION=quality.definition('extraction','Information extraction','Extract names, dates, numbers and identifiers from prose and a messy table in increasingly long input.',
    'Thirty-six generated cases, each used once: twelve at each of 1024, 2048 and 8192 input tokens where capacity permits. Each asks for eight facts about one order. '
    'Its facts are split between a ragged pipe table and an email, meeting notes or an invoice in running text, and one fact is told only by pointing at another order. '
    'Three more orders beside it differ by a digit, a day or a first name. Dates, amounts and counts must be put in a stated form. '
    'One or two of the eight facts are not in the text and must be null. Exact field and whole-case accuracy are retained. '
    'Temperature 1, seed 42 and explicit neutral native filters allow thinking with up to 32,768 output tokens. The answer is found whatever is round it.',
    [quality.FORMAT,quality.ABSENT])
DEFINITION['kpis'] += [('uncapped_accuracy','Correct fields without output caps','%'), ('uncapped_cases','Cases without output caps','count')]
PROTOCOL={**quality.PROTOCOL,'version':4,'outputs':32768,'dataset':'extraction-v5','seed':3,'input_sizes':[1024,2048,8192],'kinds':list(KINDS),'variants':4,
          'temperature':1.0,'generation_seed':42,'sampling':{'temperature':1.0,**SAMPLING}}

MONTHS=('January','February','March','April','May','June','July','August','September','October','November','December')
ONES=('one','two','three','four','five','six','seven','eight','nine')
TENS=('twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety')
MAILBOXES=('orders','accounts','desk','office','bookings','supply')
TRADES=('mill','works','trading','group','freight','stores')
# Each kind of document: what joins its sentences, how it opens, and one sentence for each fact. Only the facts
# it is given are told. No sentence names the key that is asked for.
PROSE={
    'email':(' ','From: {writer}\nSubject: order {ref}\n\nHi all, I took this one by phone on {ordered}.',
        {'customer':'It is for {customer}.','units':'They need {units} units.','venue':'Everything goes to the {venue}.',
         'delivery_date':'It has to be there on {delivery_date}.','contact':'{contact} will take it in on the day.','email':'Changes go to {email}.',
         'invoice':'Billing has raised {invoice}.','total_cents':'The total comes to {total_cents}.'}),
    'notes':('\n- ','Ops call, notes by {writer}. Item: {ref}, logged {ordered}.',
        {'venue':'Bound for the {venue}.','delivery_date':'Due {delivery_date}.','customer':'Buyer is {customer}.',
         'total_cents':'Agreed at {total_cents}.','invoice':'Finance matched it to {invoice}.','units':'{units} units in all.',
         'email':'Send paperwork to {email}.','contact':'{contact} will be there to sign for it.'}),
    'invoice':(' ','Statement for order {ref}, entered by {writer} on {ordered}.',
        {'invoice':'It is billed under {invoice}.','total_cents':'The sum owed is {total_cents}.','customer':'The buyer is {customer}.',
         'contact':'{contact} takes the goods in.','email':'Queries are answered at {email}.','units':'The goods are {units} units.',
         'delivery_date':'They arrive on {delivery_date}.','venue':'They are left at the {venue}.'}),
}
# How a fact is told when it is the same as another order's: by pointing at that order.
SAME={'venue':'It goes to the same place as order {other}.','delivery_date':'It is due on the same day as order {other}.',
      'contact':'The same person takes it in as for order {other}.'}
# Padding is cut off wherever a document begins, so each document opens with a line that says a new one starts.
NEW='--- document ---\n'
TABLE='| Orders in hand ||||||||\n| Ref | Client | Due | Qty | Where | Contact / mailbox | Bill | Amount'


def day(date,long):
    """A date as people write it: `12 April 2027` or `Apr 12th, 2027`."""
    if long:
        return f'{date.day} {MONTHS[date.month-1]} {date.year}'
    ending='th' if 10<date.day<14 else {1:'st',2:'nd',3:'rd'}.get(date.day%10,'th')
    return f'{MONTHS[date.month-1][:3]} {date.day}{ending}, {date.year}'


def count(number):
    """A number from 21 to 99 in words."""
    return TENS[number//10-2]+('-'+ONES[number%10-1] if number%10 else '')


def order(source,like=None):
    """One order. With `like`, a near miss of that order: every value is close to it and none is the same.

    A count of units is the one value that may come up again in another case: there are too few of them.
    Each order that is not a near miss has a place name and a mail domain to itself, so that its near misses can keep them.
    """
    r=source.random
    step=datetime.timedelta(1)
    near=lambda value,steps:source.fresh(lambda:value+r.choice(steps))
    digit,days=(-100,-10,-1,1,10,100),[step*n for n in (-4,-3,-2,-1,1,2,3,4)]
    if like is None:
        due,domain=source.date(),source.fresh(lambda:f'{r.choice(quality.PLACE_A).lower()}{r.choice(TRADES)}.example')
        return {'ref':source.number(1100,9900),'writer':source.person(),'ordered':near(due,[-step*n for n in range(10,26)]),
                'customer':source.person(),'delivery_date':due,'units':r.randrange(24,97),'venue':source.place(source.fresh(lambda:r.choice(quality.PLACE_A))),'contact':source.person(),
                'email':source.fresh(lambda:r.choice(MAILBOXES)+'@'+domain),
                'invoice':source.number(1100,9900),'total_cents':source.number(100000,999000)}
    return {'ref':near(like['ref'],digit),'writer':source.person(),'ordered':near(like['ordered'],days),
            'customer':source.person(like['customer'].split()[1]),'delivery_date':near(like['delivery_date'],days),
            'units':like['units']+r.choice((-2,-1,1,2)),'venue':source.place(like['venue'].split()[0]),'contact':source.person(like['contact'].split()[1]),
            'email':source.fresh(lambda:r.choice(MAILBOXES)+'@'+like['email'].split('@')[1]),
            'invoice':near(like['invoice'],digit),'total_cents':near(like['total_cents'],(-1000,-100,-10,10,100,1000))}


def answer(o):
    """The eight facts of an order in the form that is asked for."""
    return {**{field:o[field] for field in FIELDS},'delivery_date':o['delivery_date'].isoformat(),'invoice':f'INV-{o["invoice"]}'}


def written(o,long,words):
    """The order's values as a document writes them, which is not the form that is asked for."""
    return {**o,'ref':f'ORD-{o["ref"]}','ordered':day(o['ordered'],long),'delivery_date':day(o['delivery_date'],long),
            'units':count(o['units']) if words else o['units'],'invoice':f'INV-{o["invoice"]}',
            'total_cents':f'{o["total_cents"]//100:,}.{o["total_cents"]%100:02} dollars'}


def prose(kind,o,fields,long,words,same=None):
    """An email, notes or an invoice that tells these facts of an order. `same` is a fact and another order: that fact is told by pointing at the other order."""
    joiner,opening,sentences=PROSE[kind]
    told=[SAME[field] if same and field==same[0] else text for field,text in sentences.items() if field in fields]
    return joiner.join([opening]+told).format(**written(o,long,words),other=same and f'ORD-{same[1]["ref"]}')


def row(o,fields,long,words):
    """One line of the table, with these facts of an order. Any other cell is empty, and empty cells at the end are dropped."""
    w={field:value if field in fields or field=='ref' else '' for field,value in written(o,long,words).items()}
    cells=[w['ref'],w['customer'],w['delivery_date'],w['units'],w['venue'],' / '.join(filter(None,[w['contact'],w['email']])),w['invoice'],w['total_cents']]
    return ('| '+' | '.join(map(str,cells))).rstrip(' |')


def document(source,kind):
    """One case without its size: the order asked about, three near misses of it, and what is expected.

    Each order has a document of its own and a row in the one table, and each of its facts is in
    one of the two. One fact of the order asked about is the same as the first near miss's, and is
    told only by pointing at that order.
    """
    r=source.random
    target=order(source)
    others=[order(source,target) for _ in range(3)]
    absent=r.sample(FIELDS,r.choice((1,2)))
    same=r.choice([field for field in SAME if field not in absent])
    others[0][same]=target[same]
    long,words=r.random()<.5,r.random()<.5
    blocks,rows=[],[]
    for o in [target,*others]:
        told=[field for field in FIELDS if o is not target or field not in absent]
        tabled=[field for field in told if r.random()<.5 and not (o is target and field==same)]
        rows.append(row(o,tabled,long,words))
        blocks.append(prose(kind,o,[field for field in told if field not in tabled],long,words,(same,others[0]) if o is target else None))
    r.shuffle(rows)
    blocks.append(TABLE+'\n'+'\n'.join(rows))
    r.shuffle(blocks)
    blocks=[NEW+block for block in blocks]
    question=quality.json_request(f'These documents are about several orders. What is said about one order may be spread over more than one document. Answer for order ORD-{target["ref"]} only. '
        'Give customer (full name of the buyer), delivery_date (when the goods are due, as YYYY-MM-DD), units (integer), venue (the place the goods go to), '
        'contact (full name of the person who receives the goods on the day), email, invoice (as INV-0000), total_cents (the amount in cents, as an integer). '
        'Where a value is given by pointing at another order, give the value itself. Use null for a key when nothing about that order says. Give one flat object with those eight keys.')
    return {'request':{'question':question,'blocks':blocks},
            'expected':{field:None if field in absent else value for field,value in answer(target).items()},
            'kinds':{field:'absent' if field in absent else 'combined' if field==same else 'normalised' if field in ('delivery_date','total_cents') or field=='units' and words else 'plain' for field in FIELDS},
            'distractors':[*(value for o in others for field,value in answer(o).items() if not (o is others[0] and field==same)),
                           *(o[key] if key=='writer' else o[key].isoformat() for o in (target,*others) for key in ('writer','ordered'))]}


def cases(limit):
    """Thirty-six different cases, twelve at each length. Every draw is made whatever the limit, so a case is the same at any limit."""
    source,result,room,sizes=quality.Source(PROTOCOL['seed']),[],min(PROTOCOL['outputs'],limit//2),set()
    # The padding is sixteen more orders, none of them asked about.
    filler='\n'+''.join(NEW+prose(KINDS[n%3],order(source),FIELDS,n%2==0,n%4==0)+'\n\n' for n in range(16))
    for wanted in PROTOCOL['input_sizes']:
        size=min(wanted,limit-room)
        for kind in KINDS:
            for n in range(PROTOCOL['variants']):
                made=document(source,kind)
                if size not in sizes:
                    made['request']['filler']=filler
                    made['request'].update(temperature=1.0,sampling=dict(SAMPLING))
                    result.append({'id':f'extract-{size}-{kind}-{n+1}','category':kind,'context':size+room,'inputs':size,'outputs':room,**made})
        sizes.add(size)
    return result


async def measure(worker,run,ceiling):
    """Run the structured fact extraction protocol through the existing benchmark probe."""
    await quality.measure(worker,run,ceiling,PROTOCOL,cases,grade)

grade=quality.grade_fields
def summarize(samples):
    """Summarize retained structured fact extraction samples and supported scores."""
    result=quality.summarize(samples)
    if samples:
        whole=[s for s in samples if not s['values'].get('generation_capped')]
        result['uncapped_cases']=len(whole)
        result['qualification_valid']=(len(whole)==len(samples) and
            all(sample['values'].get('success') is True and
                not sample['values'].get('unsupported') and
                not sample['values'].get('unavailable') for sample in samples))
        if whole:
            result['uncapped_accuracy']=quality.summarize(whole)['accuracy']
    return result
