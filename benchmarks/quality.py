"""Shared, deterministic case execution and answer scoring for quality benchmarks.

A model is asked as it is served: it thinks if it would for anyone, and has room to. What it
thought is left out and only its final answer is graded. Where the answer is what matters, it is
found whatever is round it, a fence or a sentence. Where the format is what is asked, in the
JSON suite, the answer must be bare valid JSON.
"""

import datetime
import hashlib
import json
import math
import random

KPIS = [('accuracy','Correct fields','%'), ('case_accuracy','All-correct cases','%'),
        ('passed_cases','Passed cases','count'), ('cases','Cases','count'),
        ('available_cases','Executed cases','count'), ('max_input_tokens','Largest input','tokens'),
        ('capped_cases','Hit the output limit','count')]
# Shown by the suites that ask for a JSON object but score the answer in it: how often the answer came bare, as asked.
FORMAT = ('format_accuracy','Answered as bare JSON','%')
# Shown by the suites that leave some answers out of the text: how often the model said null and did not make one up.
ABSENT = ('absent_accuracy','Absent fields left null','%')
OUTPUTS = 8192  # tokens a model may write: room to think first, then answer
# `version` says how a model is asked and what it is asked. A run keeps the one it was made under, and a result
# from an older one is marked as due for a rerun. `grader_version` says how a saved answer is scored: when only
# that moves on, saved answers are scored again where they lie, with no rerun. See Benchmarks.regrade.
PROTOCOL = {'version':2, 'ignore_eos':False, 'chat_template':True, 'thinking':'as served', 'outputs':OUTPUTS,
            'constrained_decoding':False, 'context_guard_tokens':32, 'answer_extraction':'final answer, reasoning left out', 'grader_version':3}


def definition(key, name, summary, method, kpis=()):
    """Describe a quality benchmark in the same registry as the resource measurements."""
    return {'id':key,'name':name,'summary':summary,'method':method,'formula':'accuracy','score_unit':'%', 'kpis':KPIS+list(kpis), 'quality':True}


def context_limit(worker, run, ceiling):
    """Respect configured context and matching measured capacity without changing the model."""
    limit=min(ceiling,int(run['subject']['settings']['flags']['ctx-size']))
    for saved in worker.store.listing():
        if (saved['model']==run['model'] and saved['fingerprint']==run['fingerprint'] and saved['environment']==run['environment']
            and saved['benchmark']=='context' and saved['state']=='completed'):
            limit=min(limit,int(saved['metrics']['max_context_tokens']))
            break
    if limit < 1024:
        raise ValueError('Quality benchmarks need at least 1,024 context tokens.')
    return limit


def room(limit):
    """Tokens kept for the answer: the full allowance, or half of a context too small for that."""
    return min(OUTPUTS,limit//2)


def sizes(limit, wanted):
    """Include a handful of distinct input budgets and the largest that leaves the answer its room."""
    return sorted({min(size,limit-room(limit)) for size in wanted})


def signature(protocol):
    """Hash the deterministic case contract used to identify benchmark evidence."""
    return hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()


async def measure(worker, run, ceiling, protocol, make_cases, grade):
    """Persist the plan, then resume missing cases; never mix checker versions in a run."""
    number=run['id']
    accepted=run['subject'].get('protocol',{}).get('implementation_sha256')
    if accepted and accepted != protocol.get('implementation_sha256'):
        raise ValueError('Benchmark implementation changed after submission; submit a new run.')
    plan=worker.store.plan(number)
    if plan is None:
        cases=make_cases(context_limit(worker,run,ceiling))
        plan=worker.store.plan(number, {'protocol':protocol,'signature':signature(protocol), 'cases':cases})
        worker.store.message(number,f'Prepared {len(cases)} deterministic cases.')
    if plan['signature'] != signature(protocol):
        raise ValueError('Benchmark protocol changed; submit a new run instead of mixing case versions.')
    for case in plan['cases']:
        await worker.evaluate(number,case,grade)


FIRST=('Mira','Oren','Jalen','Ilse','Tomas','Priya','Anders','Noor','Callum','Yuki','Rosa','Emeka','Greta','Hamid','Lucia',
       'Bram','Saoirse','Dmitri','Amara','Felix','Wren','Kofi','Marta','Idris','Hana','Pavel','Leona','Tariq','Signe','Mateo',
       'Anika','Boris','Celia','Dario','Edda','Farid','Gwen','Hugo','Ines','Jonas','Kalinda','Lars','Mei','Nico','Odile',
       'Petra','Quentin','Rhea','Soren','Talia','Umar','Vera','Wim','Xenia','Yosef','Zora','Alba','Bjorn','Cleo','Dag')
LAST=('Chen','Vale','Pike','Okafor','Lindqvist','Marsh','Abara','Novak','Reyes','Thorne','Castell','Dunmore','Egede','Farrow','Galvez',
      'Hollis','Imre','Jarrah','Kessler','Lomax','Mbeki','Nakata','Orlov','Penrose','Quist','Rahimi','Sorley','Tanaka','Ulloa','Varga',
      'Ashby','Brandt','Corwin','Delgado','Ekwueme','Fenn','Grieve','Haddad','Ivers','Jovic','Kimura','Lachance','Moreau','Nyberg','Osei',
      'Pruitt','Quinlan','Rousseau','Stavros','Trevino','Underhill','Voss','Wexler','Yilmaz','Zamora','Aldane','Burke','Calloway','Drummond','Eastman')
PLACE_A=('Cedar','Lantern','Harbour','Juniper','Granite','Saffron','Willow','Copper','Meadow','Orchard',
         'Quarry','Heron','Birch','Tallow','Marlow','Fennel','Osprey','Thistle','Alder','Pebble',
         'Bramble','Cobalt','Dapple','Ember','Flint','Gorse','Hazel','Indigo','Jasper','Kestrel',
         'Larch','Mallow','Nettle','Onyx','Plover','Quill','Rowan','Sorrel','Teasel','Umber',
         'Vetch','Wicker','Yarrow','Zephyr','Aspen','Bracken','Clover','Drift','Elder','Fallow',
         'Garnet','Hollow','Iris','Juno','Kelp','Linden','Mica','Nimbus','Opal','Pollen',
         'Amber','Basalt','Cinder','Dune','Fathom','Gable','Harrow','Islet','Jetty','Kiln',
         'Lichen','Marram','Nook','Oriel','Pumice','Rill','Shale','Tarn','Vane','Weir',
         'Bight','Cairn','Dell','Eyrie','Furrow','Glen','Hythe','Ingle','Knoll','Lea',
         'Mere','Ness','Ouse','Peak','Reed','Spinney','Tor','Vista','Wold','Yew')
PLACE_B=('Workshop','Pavilion','Hall','Depot','Gallery','Arcade','Foundry','Annex','Exchange','Terrace','Atrium','Wharf')


class Source:
    """Names, places, dates and numbers for a suite's cases, drawn from a fixed seed.

    Nothing is handed out twice, so no two cases share a person, a place or an identifier, and
    the same cases come out on every run.
    """
    def __init__(self, seed):
        """Seed deterministic synthetic fixture generation with the selected case seed."""
        self.random,self.used=random.Random(seed),set()

    def fresh(self, make):
        """Draw from `make` until it gives a value not yet handed out."""
        for _ in range(10000):
            value=make()
            if value not in self.used:
                self.used.add(value)
                return value
        raise ValueError('Nothing fresh is left to hand out.')

    def person(self, surname=None):
        """Choose a deterministic synthetic person name from this case source."""
        return self.fresh(lambda:self.random.choice(FIRST)+' '+(surname or self.random.choice(LAST)))

    def place(self, first=None):
        """Choose a deterministic synthetic place name from this case source."""
        return self.fresh(lambda:(first or self.random.choice(PLACE_A))+' '+self.random.choice(PLACE_B))

    def number(self, low, high):
        """Choose a deterministic integer within the requested range."""
        return self.fresh(lambda:self.random.randrange(low,high))

    def date(self):
        """Choose a deterministic synthetic date for a case fixture."""
        return self.fresh(lambda:datetime.date(2027,1,1)+datetime.timedelta(self.random.randrange(3000)))


def clear(case):
    """Whether no distractor in the case equals a value that is expected. Used in tests."""
    return not any(same(wrong,right) for right in case['expected'].values() for wrong in case.get('distractors',[]))


# How a model marks where its reasoning begins and ends: Qwen, DeepSeek and many others; Mistral; Gemma.
THINKING=(('<think>','</think>'),('[THINK]','[/THINK]'),('<|channel>','<channel|>'))


def final_answer(output, prompt=''):
    """What a model gave as its answer, with its reasoning left out. Empty if it was still reasoning when it stopped.

    `prompt` is what the model was sent. Some chat templates end it with the mark that opens the
    reasoning, so the mark is not in what the model wrote.
    """
    if '<|message|>' in output:
        # gpt-oss and its kin write a row of messages, each under a heading that says what it is. The answer is the
        # last one, if its heading says it is final or is for the user: "<|channel|>final", "assistant to=user".
        head,_,answer=output.rpartition('<|message|>')
        heading=head.rsplit('<|start|>',1)[-1]
        return answer.split('<|',1)[0].strip() if 'final' in heading or 'to=user' in heading else ''
    for begin,end in THINKING:
        if end in output:
            answer=output.rsplit(end,1)[1]
            return '' if begin in answer else answer.strip()
        if begin in output or prompt.rstrip().endswith(begin):
            return ''
    return output.strip()


def evaluation_answer(output, evidence):
    """Native chat content is already separated from reasoning by the server parser.

    Only raw completion output needs template-marker extraction. Re-reading a native
    final answer against a prompt ending in <think> would incorrectly discard it.
    Empty/tool-only native messages have no final answer, even if reasoning looks like JSON.
    """
    if evidence.get('native_dialogue') is True:
        message = evidence.get('native_message')
        content = message.get('content') if isinstance(message, dict) and message.get('role') == 'assistant' else None
        return content.strip() if isinstance(content, str) else ''
    return final_answer(output, evidence.get('prompt', ''))


def _pairs(items):
    """Build a JSON object while rejecting duplicate keys."""
    result={}
    for key,value in items:
        if key in result:
            raise ValueError('Duplicate key: '+key)
        result[key]=value
    return result


def _invalid(value):
    """Raise a validation error for an unsupported JSON value."""
    raise ValueError('Non-JSON number: '+value)


def _finite(value):
    """Reject nonfinite JSON numbers rather than accepting invalid evidence."""
    number=float(value)
    if not math.isfinite(number):
        raise ValueError('Nonfinite JSON number')
    return number


DECODER=json.JSONDecoder(object_pairs_hook=_pairs,parse_constant=_invalid,parse_float=_finite)


def strict_json(text):
    """Require one JSON value with finite numbers and unique object keys; reject wrappers."""
    return DECODER.decode(text)


def held_json(text):
    """The last JSON object an answer holds, whatever is round it: a fence, a sentence before or after. None if it holds none."""
    found,at=None,text.find('{')
    while at!=-1:
        try:
            found,end=DECODER.raw_decode(text,at)
        except (ValueError,RecursionError):
            end=at+1
        at=text.find('{',end)
    return found


def same(actual, expected, loose=False):
    """Compare recursively without Python's True == 1 shortcut or unordered list matching.

    `loose` is for an answer whose format is not what is being asked about: a number written as
    text counts as that number, and space round a text is let go.
    """
    if loose and isinstance(actual,str):
        actual=actual.strip()
        if type(expected) in (int,float):
            try:
                actual=float(actual.replace(',',''))
            except ValueError:
                return False
    if isinstance(expected,bool) or expected is None:
        return type(actual) is type(expected) and actual==expected
    if isinstance(expected,(int,float)):
        return type(actual) in (int,float) and actual==expected
    if isinstance(expected,list):
        return isinstance(actual,list) and len(actual)==len(expected) and all(same(a,b,loose) for a,b in zip(actual,expected))
    if isinstance(expected,dict):
        return isinstance(actual,dict) and actual.keys()==expected.keys() and all(same(actual[k],v,loose) for k,v in expected.items())
    return type(actual) is type(expected) and actual==expected


def grade_fields(case, answer, strict=False):
    """Award credit for each requested field that is right.

    Unless `strict`, the answer is what matters and not its dress: the JSON object is found
    inside a fence or a sentence, values grouped one level down, by record or by file, are taken
    out of their groups, a number may be written as text, and keys nobody asked for are let be.
    `json_valid` still says whether the answer came as it was asked for, bare JSON.
    With `strict`, the format is what is asked: bare JSON, exactly the keys, exactly the types.
    A case may name the `kinds` of its fields (`plain`, `normalised`, `absent`, `combined`). Each
    check then says its kind, and the absent fields, whose answer is `null`, are counted apart.
    """
    try:
        bare=strict_json(answer)
    except (ValueError,RecursionError):
        bare=None
    expected=case['expected']
    parsed=bare if strict or isinstance(bare,dict) else held_json(answer)
    if not strict and isinstance(parsed,dict):
        grouped=[value for key,value in parsed.items() if isinstance(value,dict) and key not in expected]
        parsed={key:value for group in grouped for key,value in group.items()} | parsed
    checks=[{'field':key,'expected':value,'actual':parsed.get(key) if isinstance(parsed,dict) else None,
             'passed':isinstance(parsed,dict) and key in parsed and same(parsed[key],value,not strict)} for key,value in expected.items()]
    correct=sum(check['passed'] for check in checks)
    passed=correct==len(checks) and isinstance(parsed,dict) and (not strict or parsed.keys()==expected.keys())
    metrics={'correct':correct,'checks':len(checks),'passed':passed,'json_valid':bare is not None}
    if 'kinds' in case:
        for check in checks:
            check['kind']=case['kinds'][check['field']]
        absent=[check['passed'] for check in checks if check['kind']=='absent']
        metrics.update(absent=len(absent),absent_correct=sum(absent))
    return metrics,checks


def summarize(samples):
    """Micro-average field correctness, while separately exposing whole-case passes and coverage."""
    values=[s['values'] for s in samples]
    if not values:
        return {}
    total=sum(v.get('checks',0) for v in values)
    passed=sum(bool(v.get('passed')) for v in values)
    absent=sum(v.get('absent',0) for v in values)
    return {'accuracy':100*sum(v.get('correct',0) for v in values)/total if total else 0,
            'case_accuracy':100*passed/len(values),'passed_cases':passed,'cases':len(values),
            'available_cases':sum(bool(v.get('success')) for v in values),
            'max_input_tokens':max(v.get('input_tokens',0) for v in values),
            'format_accuracy':100*sum(bool(v.get('json_valid')) for v in values)/len(values),
            'capped_cases':sum(bool(v.get('generation_capped')) for v in values),
            # Only a suite that leaves answers out of the text has this one.
            **({'absent_accuracy':100*sum(v.get('absent_correct',0) for v in values)/absent} if absent else {})}


def json_request(question):
    """Build the bounded model request used for a JSON benchmark case."""
    return question+'\nReturn only a JSON object with the requested keys, without Markdown or explanations.'


def breakdown(samples):
    """Expose length and category differences instead of hiding them in the aggregate score."""
    groups={}
    for sample in samples:
        values=sample['values']
        key=(values.get('target_input_tokens',0),values.get('category',''))
        groups.setdefault(key,[]).append(sample)
    return [{'input_tokens':size,'category':category,**summarize(group)}
            for (size,category),group in sorted(groups.items())]
