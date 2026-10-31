"""Forty-eight finite puzzles with exhaustive arrangement checks and exact three-fact keys."""
import itertools
import json

from .. import quality

SEED = 20261016
CATEGORIES = ('ordering','scheduling','constraints','steps')
OUTPUTS = 32768  # a model that reasons at length needs room to finish; this suite only
SAMPLING = {'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
            'presence_penalty':0.0,'frequency_penalty':0.0}
STANDARD = 4  # puzzles per category at the size of version 1; the other eight are hard
DEFINITION = quality.definition('reasoning','Multi-step reasoning',
    'Solve ordering, scheduling, assignment and conditional-state puzzles.',
    'Forty-eight fixed-seed puzzles, twelve per category (four standard, eight hard), with three exact '
    'facts each. Finite ordering, schedule and attribute arrangements are exhaustively checked for one '
    'solution. Ordered state transitions use their current state. Whole-puzzle accuracy is separate '
    'from the 144-check headline, and so is accuracy on answers that were not cut off at the output '
    'limit. Native conversations preserve reasoning as served, output counts and optional '
    'server-reported reasoning counts; missing counts are unavailable. Temperature 1 and explicit '
    'neutral native filters are used to avoid deterministic sampler loops. '
    'No generated code or model judge runs. Real-model qualification is pending.',
    [quality.FORMAT,('unsupported_cases','Unsupported cases','count'),
     ('reasoning_token_cases','Cases reporting reasoning tokens','count'),
     ('reasoning_tokens','Reported reasoning tokens','tokens'),
     ('uncapped_accuracy','Facts correct where the answer was not cut off','%'),
     ('capped_checks','Facts in cut-off answers','count'),
     *[(kind+'_accuracy',kind.title()+' facts correct','%') for kind in CATEGORIES]])
PROTOCOL = {**quality.PROTOCOL,'version':3,'dataset':'reasoning-v2','seed':SEED,'cases':48,'checks':144,
            'outputs':OUTPUTS,'temperature':1.0,'generation_seed':42,
            'categories':list(CATEGORIES),'uniqueness':'exhaustive finite arrangements',
            'tiers':{'standard':STANDARD,'hard':12-STANDARD},
            'sampling':{'temperature':1.0,**SAMPLING}}


def room(limit):
    """Tokens kept for the answer: the full allowance, or half of a context too small for that."""
    return min(OUTPUTS,limit//2)


def unique(solutions):
    """Reject an ambiguous or impossible puzzle instead of choosing an arbitrary key."""
    if len(solutions)!=1:
        raise ValueError('A puzzle must have exactly one complete solution.')
    return solutions[0]


def ordering_ok(order,clue):
    """Check the candidate ordering against every deterministic ordering constraint."""
    kind,a,b,n = clue
    positions = {name:i for i,name in enumerate(order)}
    if kind=='before':
        return positions[a]<positions[b]
    if kind=='gap':
        return positions[b]-positions[a]==n
    if kind=='adjacent':
        return abs(positions[b]-positions[a])==1
    if kind=='not_adjacent':
        return abs(positions[b]-positions[a])!=1
    if kind=='between':
        return min(positions[b],positions[n])<positions[a]<max(positions[b],positions[n])  # n names the other end
    if kind=='not_end':
        return 0<positions[a]<len(order)-1
    raise ValueError('Unknown ordering clue.')


def schedule_ok(hours,puzzle,clues):
    """Check a proposed schedule against the fixture's resource and time constraints."""
    meetings=puzzle['meetings']
    available=puzzle['availability']
    for meeting,hour in zip(meetings,hours):
        if any(hour not in available[person] for person in meeting['people']):
            return False
    for a,b in itertools.combinations(range(len(meetings)),2):
        if hours[a]==hours[b] and (meetings[a]['room']==meetings[b]['room'] or set(meetings[a]['people'])&set(meetings[b]['people'])):
            return False
    return all(hours[a]<hours[b] for a,b in clues)


def assignment_ok(solution,clue):
    """A solution gives, for each attribute, the value held by each person in printed order."""
    kind=clue[0]
    if kind in ('has','not_has'):
        _,person,attribute,value=clue
        return (solution[attribute][person]==value)==(kind=='has')
    if kind in ('same','differ'):
        _,first,a,second,b=clue
        return (solution[first].index(a)==solution[second].index(b))==(kind=='same')
    raise ValueError('Unknown assignment clue.')


def narrow(universe,candidates,fits,random):
    """Choose only useful true clues; verify uniqueness over the original full universe."""
    random.shuffle(candidates)
    remaining=list(universe)
    clues=[]
    for clue in candidates:
        chosen=[solution for solution in remaining if fits(solution,clue)]
        if len(chosen)<len(remaining):
            clues.append(clue)
            remaining=chosen
        if len(remaining)==1:
            break
    solution=unique([s for s in universe if all(fits(s,c) for c in clues)])
    return clues,solution


def transition(state,move):
    """Evaluate a condition and one simultaneous assignment from the current state."""
    target,condition,operand,threshold,yes,no=move
    if condition=='even':
        take=state[operand]%2==0
    elif condition=='greater':
        take=state[operand]>threshold
    elif condition=='compare':
        take=state[operand]>state[threshold]  # threshold names another variable
    else:
        raise ValueError('Unknown state condition.')
    chosen = yes if take else no
    operation,source,constant=chosen
    result=dict(state)
    if operation=='add':
        result[target]=state[source]+constant
    elif operation=='subtract':
        result[target]=state[source]-constant
    elif operation=='multiply':
        result[target]=state[source]*constant
    elif operation=='floor_div':
        result[target]=state[source]//constant
    elif operation=='mod':
        result[target]=state[source]%constant
    else:
        raise ValueError('Unknown state operation.')
    if abs(result[target])>BOUND:
        raise ValueError('A state value left its bound.')
    return result


BOUND = 10**6


def solve(category,puzzle):
    """Enumerate every finite arrangement; deterministic transitions have one final state."""
    if category=='ordering':
        solutions=[p for p in itertools.permutations(puzzle['people']) if all(ordering_ok(p,c) for c in puzzle['clues'])]
    elif category=='scheduling':
        solutions=[h for h in itertools.product(puzzle['hours'],repeat=len(puzzle['meetings'])) if schedule_ok(h,puzzle,puzzle['clues'])]
    elif category=='constraints':
        names=list(puzzle['attributes'])
        solutions=[dict(zip(names,rows)) for rows in itertools.product(*(itertools.permutations(puzzle['attributes'][n]) for n in names))
                   if all(assignment_ok(dict(zip(names,rows)),c) for c in puzzle['clues'])]
    elif category=='steps':
        state=dict(puzzle['initial'])
        for move in puzzle['moves']:
            state=transition(state,move)
        return state
    else:
        raise ValueError('Unknown reasoning category.')
    return unique(solutions)


def facts(category,puzzle,solution):
    """Three requested facts from the unique solution, never included in the model request."""
    if category=='ordering':
        return {key:solution[position-1] for key,position in puzzle['queries'].items()}
    if category=='scheduling':
        return {key:solution[meeting] for key,meeting in puzzle['queries'].items()}
    if category=='constraints':
        return {key:solution[attribute][person] for key,(attribute,person) in puzzle['queries'].items()}
    return {name:solution[name] for name in puzzle['queries']}


ORDERING_RULES=('Each person appears once in a left-to-right row. Clues are [kind, A, B, N]. '
    'before means A is left of B; gap means B is exactly N positions right of A; '
    'adjacent means consecutive in either order; not_adjacent means not consecutive; '
    'between means A is somewhere between B and the person named in N, in either order; '
    'not_end means A is at neither end. Unused items are 0 or repeat A. '
    'Queries give one-based positions; return the person at each.')
SCHEDULING_RULES=('Meetings use their given rooms and participants. Available integer '
    'start hours are listed; every participant must be available. A room or person cannot '
    'have two meetings at the same start hour. Different rooms with disjoint people may '
    'overlap. Clue [a,b] means meeting index a starts strictly before index b; meeting '
    'indices are zero-based. Return the start hour for each queried meeting.')
CONSTRAINT_RULES=('Each person owns exactly one value of every attribute; each value is used once. '
    'Person indices follow the printed people list, zero-based. [has,i,attribute,value] says '
    'person i owns that value; [not_has,i,attribute,value] says they do not. '
    '[same,attribute,value,attribute,value] says one person owns both values; '
    '[differ,attribute,value,attribute,value] says two different people own them. '
    'Queries [attribute,i] ask for the value person i owns.')
STEPS_RULES=('Run all moves in order, using the current state after each preceding move. '
    'A move is [target,condition,operand,threshold,yes,no]. even tests whether the current '
    'operand is even (threshold unused); greater tests current operand > threshold; compare '
    'tests current operand > the current value of the variable named in threshold. '
    'Choose yes or no, a triple [operation,source,constant]; assign target to the current '
    'source op constant: add, subtract, multiply, floor_div (integer division rounding '
    'toward negative infinity) or mod (the remainder, never negative for a positive constant). '
    'Only target changes. Return the final integer of each queried variable.')


def build(category,hard,source):
    """Build one deterministic multi-step reasoning fixture and its data-only checks."""
    random=source.random
    if category=='ordering':
        people=[source.person() for _ in range(random.choice((7,8)) if hard else random.choice((5,6,7)))]
        order=random.sample(people,len(people))
        candidates=[]
        for i,a in enumerate(order):
            if 0<i<len(order)-1:
                candidates.append(['not_end',a,a,0])
            for j in range(i+1,len(order)):
                b=order[j]
                candidates.append(['before',a,b,0])
                if j-i<=3:
                    candidates.append(['gap',a,b,j-i])
                if j-i==1:
                    candidates.append(['adjacent',a,b,0])
                if hard and j-i>1:
                    candidates.append(['not_adjacent',a,b,0])
                if hard:
                    for k in range(i+1,j):
                        candidates.append(['between',order[k],*random.sample((a,b),2)])
        clues,_=narrow(list(itertools.permutations(people)),candidates,ordering_ok,random)
        places=sorted(random.sample(range(2,len(people)),3)) if hard else [3,1,len(people)]
        puzzle=dict(people=people,clues=clues,queries={f'position_{n}':n for n in places})
        return puzzle,ORDERING_RULES
    if category=='scheduling':
        count,hours,rooms=(6,[9,10,11,12,13],'ABCDAB') if hard else (4,[9,10,11,12],'ABCA')
        groups=((0,1),(1,2,3),(2,3),(0,4,5),(4,6),(3,5)) if hard else ((0,1),(1,2),(2,3),(0,4))
        people=[source.person() for _ in range(7 if hard else 5)]
        meetings=[dict(name='M'+str(i+1),room=rooms[i],people=[people[x] for x in groups[i]]) for i in range(count)]
        puzzle=dict(hours=hours,rooms=sorted(set(rooms)),meetings=meetings,availability={p:hours for p in people})
        while True:
            chosen=[random.choice(hours) for _ in meetings]
            if schedule_ok(chosen,puzzle,[]):
                break
        available={}
        for person in people:
            free={chosen[i] for i,m in enumerate(meetings) if person in m['people']}
            while len(free)<3:
                free.add(random.choice(hours))
            available[person]=sorted(free)
        puzzle['availability']=available
        candidates=[[a,b] for a,b in itertools.permutations(range(count),2) if chosen[a]<chosen[b]]
        universe=[h for h in itertools.product(hours,repeat=count) if schedule_ok(h,puzzle,[])]
        clues,_=narrow(universe,candidates,lambda h,c:h[c[0]]<h[c[1]],random)
        puzzle.update(clues=clues,queries={f'meeting_{i+1}_hour':i for i in sorted(random.sample(range(count),3))})
        return puzzle,SCHEDULING_RULES
    if category=='constraints':
        pool=dict(colour=['amber','blue','green','violet'],pet=['cat','dog','finch','rabbit'],drink=['coffee','juice','milk','tea'])
        attributes={name:values for name,values in list(pool.items())[:3 if hard else 2]}
        people=[source.person() for _ in range(4)]
        held={name:random.sample(values,4) for name,values in attributes.items()}  # held[name][i] belongs to person i
        candidates=[]
        for name,values in attributes.items():
            for i in range(4):
                candidates.append(['has',i,name,held[name][i]])
                candidates.extend(['not_has',i,name,v] for v in values if v!=held[name][i])
        for first,second in itertools.combinations(attributes,2):
            for i in range(4):
                for j in range(4):
                    candidates.append(['same' if i==j else 'differ',first,held[first][i],second,held[second][j]])
        universe=[dict(zip(attributes,rows)) for rows in itertools.product(*(itertools.permutations(v) for v in attributes.values()))]
        clues,_=narrow(universe,candidates,assignment_ok,random)
        queries={f'{name}_of_{i}':[name,i] for name in attributes for i in range(4)}
        puzzle=dict(people=people,attributes=attributes,clues=clues,queries=dict(random.sample(sorted(queries.items()),3)))
        return puzzle,CONSTRAINT_RULES
    names=('a','b','c','d') if hard else ('a','b','c')
    initial={name:random.randrange(2,20) for name in names}
    moves=[]
    for n in range(10 if hard else 5):
        target=random.choice(names) if hard else ('a','b','c','a','c')[n]
        operand=random.choice(names) if hard else ('b','a','b','c','a')[n]
        if hard:
            condition=random.choice(('even','greater','compare'))
            threshold=random.choice([x for x in names if x!=operand]) if condition=='compare' else random.randrange(8,30)
            yes=[random.choice(('add','subtract','floor_div')),random.choice(names),0]
            no=[random.choice(('multiply','mod','add')),random.choice(names),0]
            for move in (yes,no):
                move[2]=random.randrange(2,10) if move[0] in ('floor_div','mod') else random.randrange(2,5) if move[0]=='multiply' else random.randrange(2,40)
        else:
            condition='even' if n%2==0 else 'greater'
            threshold=random.randrange(8,30)
            yes=['add',operand,random.randrange(2,10)]
            no=['multiply',operand,random.randrange(2,5)]
        moves.append([target,condition,operand,threshold,yes,no])
    puzzle=dict(initial=initial,moves=moves,queries={name:'final '+name for name in sorted(random.sample(names,3))})
    return puzzle,STEPS_RULES


def make(category,index,source):
    """Build until the puzzle has one solution; the same seed always reaches the same puzzle."""
    for _ in range(200):
        try:
            puzzle,rules=build(category,index>=STANDARD,source)
            solution=solve(category,puzzle)
        except ValueError:
            continue
        return puzzle,rules,facts(category,puzzle,solution)
    raise ValueError('No puzzle with one solution was found.')


def cases(limit):
    """Build deterministic multi-step reasoning fixtures from the case seed."""
    source=quality.Source(SEED)
    result=[]
    for category in CATEGORIES:
        for index in range(12):
            puzzle,rules,expected=make(category,index,source)
            question=quality.json_request(rules+'\nPuzzle data:\n'+json.dumps(puzzle,ensure_ascii=False))
            result.append(dict(id=f'{category}-{index+1}',category=category,context=limit,inputs=0,
                outputs=room(limit),request={'messages':[dict(role='user',content=question)],
                    'temperature':1.0,'sampling':SAMPLING},
                expected=expected,source=puzzle))
    return result


def grade(case,answer):
    """Apply data-only multi-step reasoning checks to the final answer."""
    return quality.grade_fields(case,answer)


def summarize(samples):
    """Summarize retained multi-step reasoning samples and supported scores."""
    supported=[s for s in samples if not s['values'].get('unsupported')]
    result=quality.summarize(supported)
    if samples:
        result.update(cases=len(samples),unsupported_cases=len(samples)-len(supported),
            reasoning_token_cases=sum('reasoning_tokens' in s['values'] for s in supported))
        reported=[s['values']['reasoning_tokens'] for s in supported if 'reasoning_tokens' in s['values']]
        if reported:
            result['reasoning_tokens']=sum(reported)
        # Told apart so that an answer cut off at the limit is not read as an inability to solve.
        cut=[s['values'] for s in supported if s['values'].get('generation_capped')]
        whole=[s['values'] for s in supported if not s['values'].get('generation_capped')]
        result['capped_checks']=sum(v.get('checks',0) for v in cut)
        if checks:=sum(v.get('checks',0) for v in whole):
            result['uncapped_accuracy']=100*sum(v.get('correct',0) for v in whole)/checks
    for category in CATEGORIES:
        chosen=[s for s in supported if s['values'].get('category')==category]
        if chosen:
            result[category+'_accuracy']=quality.summarize(chosen)['accuracy']
    return result


def breakdown(samples):
    """Group retained multi-step reasoning outcomes by case dimensions."""
    return [dict(input_tokens=0,category=category,**summarize(chosen)) for category in CATEGORIES
            if (chosen:=[s for s in samples if s['values'].get('category')==category])]


async def measure(worker,run,ceiling):
    """Run the multi-step reasoning protocol through the existing benchmark probe."""
    await quality.measure(worker,run,ceiling,PROTOCOL,cases,grade)
