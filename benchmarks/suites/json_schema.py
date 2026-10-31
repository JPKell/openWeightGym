"""Paired free/native-constrained JSON, with free answers as the headline.

Here the format is what is asked for: once the model's reasoning is left out, its answer must be
bare valid JSON. A fence round it fails. What the values would have scored with the format let
go is kept beside it, so that a model that knows the answer and dresses it wrongly can be told
from one that does not know.
"""

import json
import math
import re
from jsonschema import Draft202012Validator
from .. import quality

SAMPLING={'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
          'presence_penalty':0.0,'frequency_penalty':0.0}
OUTPUTS=32768

DEFINITION=quality.definition('json_schema','JSON & schema','Return valid JSON matching a schema and the requested values.',
    'Twenty-seven deterministic ledger questions require revision selection, joins, filtering and '
    'exact normalization. Each has a native JSON-schema partner using the same question and '
    'schema. Free/forced results, matched-pair value differences, coverage and '
    'output tokens per request-second are separate. Unsupported forced formats receive no '
    'correctness score and never fall back to free decoding. Real grammar/model qualification '
    'is pending.',
    [('json_accuracy','Free valid JSON','%'),('schema_accuracy','Free schema compliant','%'),
     ('value_accuracy','Free values right, format aside','%'),
     ('recorded_samples','Recorded samples','count'),('forced_cases','Forced samples','count'),
     ('forced_available_cases','Forced executed','count'),('unsupported_cases','Forced unsupported','count'),
     ('forced_unavailable_cases','Forced unavailable (not unsupported)','count'),
     ('forced_capped_cases','Forced hit output limit','count'),
     ('forced_accuracy','Forced correct checks','%'),('forced_schema_accuracy','Forced schema compliant','%'),
     ('forced_value_accuracy','Forced values right, format aside','%'),('matched_pairs','Executed matched pairs','count'),
     ('paired_free_value_accuracy','Matched free values right','%'),
     ('paired_forced_value_accuracy','Matched forced values right','%'),
     ('forced_value_delta','Matched forced minus free values','pp'),
     ('free_output_tokens_per_second','Free output tokens / request-second','tokens/s'),
     ('forced_output_tokens_per_second','Forced output tokens / request-second','tokens/s')])
DEFINITION['kpis']=[(key,'Free '+label if key in {k[0] for k in quality.KPIS} else label,unit)
                    for key,label,unit in DEFINITION['kpis']]
PROTOCOL={**quality.PROTOCOL,'version':6,'cases':54,'pairs':27,'dataset':'json-schema-paired-v5',
          'free_dataset':'json-schema-ledger-v3','free_request_protocol':5,'jsonschema_version':'4.26.0',
          'temperature':1.0,'generation_seed':42,'sampling':{'temperature':1.0,**SAMPLING},
          'outputs':OUTPUTS,
          'constrained_decoding':'paired: original free completion and native JSON schema',
          'headline':'free checks only','pair_delta':'executed matching pairs only'}


def legacy_free_cases(limit):
    """Produce fixed natural-language requests with an explicit JSON Schema and answer key."""
    result=[]
    for n in range(3):
        examples=[
            ('types',f'Person Mira Chen is {27+n} years old and is an active member. Their middle name is unknown. Use keys name, age, active, middle.',
             {'name':'Mira Chen','age':27+n,'active':True,'middle':None},
             {'name':{'type':'string'},'age':{'type':'integer','minimum':0},'active':{'type':'boolean'},'middle':{'type':['string','null']}}),
            ('array',f'Keep the labels red, blue, red in that order, including the duplicate. Priority is high. Count is {3+n}. Use labels, priority, count.',
             {'labels':['red','blue','red'],'priority':'high','count':3+n},
             {'labels':{'type':'array','items':{'type':'string'},'minItems':3,'maxItems':3},'priority':{'enum':['low','high']},'count':{'type':'integer','minimum':3,'maximum':5}}),
            ('nested',f'Order O-{81+n} has two units of item pen at price 1.25. It is not shipped. Use order with keys id and items; each item has sku, quantity, price. Include shipped outside order.',
             {'order':{'id':f'O-{81+n}','items':[{'sku':'pen','quantity':2,'price':1.25}]},'shipped':False},
             {'order':{'type':'object','properties':{'id':{'type':'string'},'items':{'type':'array','items':{'type':'object','properties':{'sku':{'type':'string'},'quantity':{'type':'integer'},'price':{'type':'number'}},'required':['sku','quantity','price'],'additionalProperties':False}}},'required':['id','items'],'additionalProperties':False},'shipped':{'type':'boolean'}}),
            ('escaping',f'The title is Café "Aurora" {n+1}. The path is C:\\archive\\notes.txt. The message is two lines: first, then second. Use title, path, message.',
             {'title':f'Café "Aurora" {n+1}','path':'C:\\archive\\notes.txt','message':'first\nsecond'},
             {'title':{'type':'string'},'path':{'type':'string'},'message':{'type':'string'}}),
            ('empty',f'Sensor S-{n+1} has no readings, no assigned operator, and a zero error count. Use id, readings, operator, errors.',
             {'id':f'S-{n+1}','readings':[],'operator':None,'errors':0},
             {'id':{'type':'string'},'readings':{'type':'array','items':{'type':'number'},'maxItems':0},'operator':{'type':'null'},'errors':{'type':'integer','minimum':0}}),
        ]
        for category,question,expected,properties in examples:
            schema={'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
            question=quality.json_request(question)+'\nRequired JSON Schema: '+json.dumps(schema,ensure_ascii=False)
            result.append({'id':f'{category}-{n+1}','category':category,'context':limit,'inputs':0,'outputs':quality.room(limit),
                           'request':{'question':question},'schema':schema,'expected':expected})
    return result


def free_cases(limit):
    """Scoped event replay and exact normalization, with the same paired scoring contract.

    The old fifteen requests remain available for compatibility tests. New requests require
    joins, withdrawals and stable ordering rather than directly copying a displayed answer.
    """
    result=[]
    for n in range(3):
        batch=f'B-{71+n}'
        records=[]
        # No final answer table appears in the prompt. The answer is computed independently
        # from the generated source records; uncommitted and other-batch rows are decoys.
        for i in range(8):
            records.append({'batch':batch,'id':f'R{i+1}','revision':1,'committed':True,
                'label':['red','blue','green','amber'][i%4],'units':i+n+1,
                'price_cents':125+25*(i%3),'priority':'high' if i%3==0 else 'low',
                'void':False})
        records += [dict(records[1],revision=2,units=7+n),
                    dict(records[3],revision=2,void=True),
                    dict(records[1],revision=3,units=99,committed=False),
                    dict(records[0],batch=batch+'-X',revision=9,units=999)]
        # Latest committed revision per ID, target batch only; void rows disappear.
        latest={}
        for row in records:
            if row['batch']==batch and row['committed'] and row['revision']>latest.get(row['id'],{}).get('revision',0):
                latest[row['id']]=row
        kept=[latest[key] for key in sorted(latest) if not latest[key]['void']]
        source=json.dumps(records[::2]+records[1::2],ensure_ascii=False)
        rules=(f'For batch {batch} only, select the highest committed revision per id; ignore '
               'uncommitted revisions, then exclude void records. Sort surviving IDs ascending. '
               'Repeated labels must remain repeated. Prices are integer cents. Source rows:\n'+source+'\n')
        total=sum(r['units']*r['price_cents'] for r in kept)
        examples=[
            ('types',rules+'Use name for the batch ID, age for the sum of surviving units, active '
             'for whether ANY surviving high-priority row has at least 8 units, and middle for '
             'the label of R4 if it survives, otherwise null.',
             {'name':batch,'age':sum(r['units'] for r in kept),
              'active':any(r['priority']=='high' and r['units']>=8 for r in kept),'middle':None},
             {'name':{'type':'string'},'age':{'type':'integer'},'active':{'type':'boolean'},'middle':{'type':['string','null']}}),
            ('array',rules+'Use labels for labels of surviving rows with at least 5 units, '
             'priority for high if the surviving high-priority unit sum exceeds the low-priority '
             'unit sum (otherwise low), and count for the total price in cents of ALL surviving rows.',
             {'labels':[r['label'] for r in kept if r['units']>=5],
              'priority':'high' if sum(r['units'] for r in kept if r['priority']=='high')>sum(r['units'] for r in kept if r['priority']=='low') else 'low',
              'count':total},
             {'labels':{'type':'array','items':{'type':'string'}},'priority':{'enum':['low','high']},'count':{'type':'integer'}}),
            ('nested',rules+'Use order with keys id (batch ID) and items. Each item contains sku '
             '(record ID), quantity (units), price (unit price in cents). Include only surviving '
             'high-priority rows. Use shipped outside order: true only if ALL surviving rows have '
             'at least 3 units. Do not include totals or extra keys.',
             {'order':{'id':batch,'items':[{'sku':r['id'],'quantity':r['units'],'price':r['price_cents']} for r in kept if r['priority']=='high']},
              'shipped':all(r['units']>=3 for r in kept)},
             {'order':{'type':'object','properties':{'id':{'type':'string'},'items':{'type':'array','items':{'type':'object','properties':{'sku':{'type':'string'},'quantity':{'type':'integer'},'price':{'type':'integer'}},'required':['sku','quantity','price'],'additionalProperties':False}}},'required':['id','items'],'additionalProperties':False},'shipped':{'type':'boolean'}}),
            ('escaping',rules+'Use title equal to the batch ID, one space, then the literal quoted '
             'word "ready". Use path beginning C:\\archive\\ followed by the surviving ID with '
             'the largest extended price (units times cents; ties use smallest ID) and .txt. '
             'Use message with exactly two lines: the surviving IDs joined by commas without '
             'spaces, then the total price in cents written in decimal.',
             {'title':batch+' "ready"','path':'C:\\archive\\'+min(kept,key=lambda r:(-r['units']*r['price_cents'],r['id']))['id']+'.txt',
              'message':','.join(r['id'] for r in kept)+'\n'+str(total)},
             {'title':{'type':'string'},'path':{'type':'string'},'message':{'type':'string'}}),
            ('empty',rules+'Use id for the batch ID; readings for surviving IDs whose units are '
             'greater than 7; operator for the label of the voided R4 if it survives, otherwise '
             'null; errors for the number of surviving rows whose units are negative.',
             {'id':batch,'readings':[r['id'] for r in kept if r['units']>7],'operator':None,
              'errors':sum(r['units']<0 for r in kept)},
             {'id':{'type':'string'},'readings':{'type':'array','items':{'type':'string'}},'operator':{'type':['string','null']},'errors':{'type':'integer'}}),
        ]
        for category,question,expected,properties in examples:
            schema={'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
            result.append({'id':f'{category}-{n+1}','category':category,'context':limit,'inputs':0,
                'outputs':min(OUTPUTS,limit//2),'request':{'question':quality.json_request(question)+'\nRequired JSON Schema: '+json.dumps(schema,ensure_ascii=False),
                    'temperature':1.0,'sampling':SAMPLING},
                'schema':schema,'expected':expected})
    # Protocol v6 gives each of the original fifteen questions its own ledger identity.
    # Earlier frozen v5 cases and retained runs remain unchanged.
    for case in result:
        old_batch=f"B-{70+int(case['id'].rsplit('-',1)[1])}"
        namespace=old_batch+'-'+case['category']
        def unique(value):
            """Replace fixture namespaces recursively so generated records remain distinct."""
            if isinstance(value,str):
                value=value.replace(old_batch,namespace)
                return re.sub(r'\bR([1-8])\b',lambda match:namespace+'-R'+match[1],value)
            if isinstance(value,list):
                return [unique(item) for item in value]
            if isinstance(value,dict):
                return {key:unique(item) for key,item in value.items()}
            return value
        case['request']['question']=unique(case['request']['question'])
        case['expected']=unique(case['expected'])
    return result+expanded_free_cases(limit)


def expanded_free_cases(limit):
    """Twelve distinct revision ledgers add sixty factual free-answer checks."""
    result=[]
    labels=('red','blue','green','amber')
    for category in ('revision_audit','cost_audit','group_audit','rank_audit'):
        kind=('revision_audit','cost_audit','group_audit','rank_audit').index(category)
        for n in range(3):
            batch=f'C-{kind+1}{n+1}'
            rows=[]
            for i in range(10):
                rows.append({'batch':batch,'id':f'{batch}-R{i+1:02d}','revision':1,'committed':True,
                             'label':labels[(i+n+kind)%4],
                             'units':(i*3+n*2+kind)%11+1,
                             'price_cents':95+25*((i+2*kind+n)%5),
                             'priority':'high' if (i+n+kind)%3==0 else 'low','void':False})
            revised=f'{batch}-R{2+(n+kind)%6:02d}'
            voided=f'{batch}-R{7+(n+kind)%3:02d}'
            revised_row=next(row for row in rows if row['id']==revised)
            voided_row=next(row for row in rows if row['id']==voided)
            rows.extend([dict(revised_row,revision=2,units=revised_row['units']+3,
                              price_cents=revised_row['price_cents']+15),
                         dict(voided_row,revision=2,void=True),
                         dict(rows[0],revision=3,units=999,committed=False),
                         dict(rows[1],batch=batch+'-OTHER',revision=9,units=999)])
            latest={}
            for row in rows:
                if row['batch']==batch and row['committed'] and row['revision']>latest.get(row['id'],{}).get('revision',0):
                    latest[row['id']]=row
            kept=[latest[key] for key in sorted(latest) if not latest[key]['void']]
            source=json.dumps(rows[::2]+rows[1::2],ensure_ascii=False)
            rules=(f'For batch {batch} only, keep the highest committed revision per ID, '
                   'then omit voided IDs. Ignore uncommitted and other-batch rows. '
                   'Use only the resulting rows for calculations; audit identifiers may name '
                   'the selected revision or excluded void row. '
                   'Source rows:\n'+source+'\n')
            if category=='revision_audit':
                question=(rules+'Return surviving_ids sorted ascending, survivor_count, '
                          'revised_id (the surviving ID with revision 2), voided_id, and sum_units.')
                expected={'surviving_ids':[row['id'] for row in kept],
                          'survivor_count':len(kept),'revised_id':revised,
                          'voided_id':voided,'sum_units':sum(row['units'] for row in kept)}
                properties={'surviving_ids':{'type':'array','items':{'type':'string'}},
                            'survivor_count':{'type':'integer'},'revised_id':{'type':'string'},
                            'voided_id':{'type':'string'},'sum_units':{'type':'integer'}}
            elif category=='cost_audit':
                question=(rules+'Extended cents means units times price_cents. Return total_cents '
                          'for all survivors, high_cents for high-priority survivors, blue_cents '
                          'for blue-label survivors, largest_id and smallest_id by extended cents '
                          '(ties use the smallest ID).')
                ranked=sorted(kept,key=lambda row:(row['units']*row['price_cents'],row['id']))
                expected={'total_cents':sum(row['units']*row['price_cents'] for row in kept),
                          'high_cents':sum(row['units']*row['price_cents'] for row in kept if row['priority']=='high'),
                          'blue_cents':sum(row['units']*row['price_cents'] for row in kept if row['label']=='blue'),
                          'largest_id':min(kept,key=lambda row:(-row['units']*row['price_cents'],row['id']))['id'],
                          'smallest_id':ranked[0]['id']}
                properties={key:{'type':'integer'} for key in ('total_cents','high_cents','blue_cents')}
                properties.update(largest_id={'type':'string'},smallest_id={'type':'string'})
            elif category=='group_audit':
                question=(rules+'Return red_count, blue_count, green_count and amber_count '
                          'for surviving rows. Return most_common_label; ties use alphabetical order.')
                counts={label:sum(row['label']==label for row in kept) for label in labels}
                expected={label+'_count':counts[label] for label in labels}
                expected['most_common_label']=min(labels,key=lambda label:(-counts[label],label))
                properties={label+'_count':{'type':'integer'} for label in labels}
                properties['most_common_label']={'type':'string'}
            else:
                question=(rules+'Rank survivors by units descending, ties by ID ascending. '
                          'Return top_unit_id and second_unit_id. Return bottom_price_id '
                          'for the smallest unit price, ties by ID ascending. Also return '
                          'high_count and units_at_least_6_count.')
                ranked=sorted(kept,key=lambda row:(-row['units'],row['id']))
                expected={'top_unit_id':ranked[0]['id'],'second_unit_id':ranked[1]['id'],
                          'bottom_price_id':min(kept,key=lambda row:(row['price_cents'],row['id']))['id'],
                          'high_count':sum(row['priority']=='high' for row in kept),
                          'units_at_least_6_count':sum(row['units']>=6 for row in kept)}
                properties={key:{'type':'string'} for key in ('top_unit_id','second_unit_id','bottom_price_id')}
                properties.update(high_count={'type':'integer'},units_at_least_6_count={'type':'integer'})
            schema={'type':'object','properties':properties,'required':list(properties),
                    'additionalProperties':False}
            result.append({'id':f'{category}-{n+1}','category':category,'context':limit,'inputs':0,
                           'outputs':min(OUTPUTS,limit//2),
                           'request':{'question':quality.json_request(question)+'\nRequired JSON Schema: '+json.dumps(schema,ensure_ascii=False),
                                      'temperature':1.0,'sampling':SAMPLING},
                           'schema':schema,'expected':expected})
    return result


def cases(limit):
    """Build deterministic strict JSON and schema adherence fixtures from the case seed."""
    free=[dict(case,decoding='free',pair=case['id']) for case in free_cases(limit)]
    forced=[]
    for case in free:
        forced.append({**case,'id':case['id']+'-forced','decoding':'forced','request':{
            'messages':[{'role':'user','content':case['request']['question']}],
            'temperature':1.0,'sampling':SAMPLING,
            'response_format':{'type':'json_schema','json_schema':{
                'name':'benchmark_case','strict':True,'schema':case['schema']}}}})
    return free+forced


def grade(case,answer):
    """Keep syntax and schema success separate from semantically correct field values."""
    metrics,checks=quality.grade_fields(case,answer,strict=True)
    aside,_=quality.grade_fields(case,answer)  # the same values, found in whatever dress they came
    try:
        value=quality.strict_json(answer)
        valid=True
        errors=list(Draft202012Validator(case['schema']).iter_errors(value))
        schema_ok=not errors
    except (ValueError,RecursionError):
        valid=schema_ok=False
        errors=[]
    checks.extend([{'field':'JSON syntax','passed':valid}, {'field':'JSON Schema','passed':schema_ok,
                   'errors':[error.message for error in errors]}])
    metrics.update(correct=sum(c['passed'] for c in checks),checks=len(checks),passed=metrics['passed'] and schema_ok,
                   json_valid=valid,schema_valid=schema_ok,value_correct=aside['correct'],value_checks=aside['checks'])
    metrics.update(decoding=case.get('decoding','free'),pair=case.get('pair',case.get('id')))
    return metrics,checks


async def measure(worker,run,ceiling):
    """Run the strict JSON and schema adherence protocol through the existing benchmark probe."""
    await quality.measure(worker,run,ceiling,PROTOCOL,cases,grade)


def qualities(samples):
    """Extract strict JSON and schema outcomes from retained sample evidence."""
    result=quality.summarize(samples)
    if samples:
        result.update(json_accuracy=100*sum(s['values'].get('json_valid',False) for s in samples)/len(samples),
                      schema_accuracy=100*sum(s['values'].get('schema_valid',False) for s in samples)/len(samples))
        fields=sum(s['values'].get('value_checks',0) for s in samples)
        result['value_accuracy']=100*sum(s['values'].get('value_correct',0) for s in samples)/fields if fields else None
    return result


def mode_summary(samples,forced=False):
    """Summarize JSON/schema outcomes for one response-format mode."""
    supported=[s for s in samples if not s['values'].get('unsupported')
               and (not forced or s['values'].get('success'))]
    result=qualities(supported)
    if samples:
        result.update(cases=len(samples),available_cases=sum(bool(s['values'].get('success')) for s in supported),
                      unsupported_cases=sum(bool(s['values'].get('unsupported')) for s in samples),
                      passed_cases=sum(bool(s['values'].get('passed')) for s in supported))
    return result


def summarize(samples):
    """Summarize retained strict JSON and schema adherence samples and supported scores."""
    free=[s for s in samples if s['values'].get('decoding')!='forced']
    forced=[s for s in samples if s['values'].get('decoding')=='forced']
    result=mode_summary(free)
    if not samples:
        return result
    measured=mode_summary(forced,True)
    result.update(recorded_samples=len(samples),forced_cases=len(forced),
                  forced_available_cases=sum(bool(s['values'].get('success')) and not s['values'].get('unsupported') for s in forced),
                  unsupported_cases=sum(bool(s['values'].get('unsupported')) for s in forced),
                  forced_unavailable_cases=sum(not s['values'].get('success') and not s['values'].get('unsupported') for s in forced),
                  forced_capped_cases=sum(bool(s['values'].get('generation_capped')) for s in forced))
    for key in ('accuracy','schema_accuracy','value_accuracy'):
        if key in measured:
            result['forced_'+key]=measured[key]
    available=lambda group:{s['values'].get('pair',s.get('id')):s['values'] for s in group
        if s['values'].get('success') and not s['values'].get('unsupported')}
    before,after=available(free),available(forced)
    pairs=[key for key in before.keys() & after.keys() if key is not None
           and before[key].get('value_checks',0)>0 and before[key]['value_checks']==after[key].get('value_checks')]
    result['matched_pairs']=len(pairs)
    if pairs:
        total=sum(before[key]['value_checks'] for key in pairs)
        result['paired_free_value_accuracy']=100*sum(before[key]['value_correct'] for key in pairs)/total
        result['paired_forced_value_accuracy']=100*sum(after[key]['value_correct'] for key in pairs)/total
        result['forced_value_delta']=result['paired_forced_value_accuracy']-result['paired_free_value_accuracy']
    for name,group in (('free',free),('forced',forced)):
        timed=[s['values'] for s in group if s['values'].get('success') and not s['values'].get('unsupported')
               and type(s['values'].get('output_tokens')) is int and s['values']['output_tokens']>=0
               and type(s['values'].get('request_seconds')) in (int,float)
               and 0<s['values']['request_seconds'] and math.isfinite(s['values']['request_seconds'])]
        if timed:
            result[name+'_output_tokens_per_second']=sum(v['output_tokens'] for v in timed)/sum(v['request_seconds'] for v in timed)
    return result


def breakdown(samples):
    """Group retained strict JSON and schema adherence outcomes by case dimensions."""
    groups={}
    for sample in samples:
        values=sample['values']
        mode=values.get('decoding','free')
        key=(values.get('target_input_tokens',0),values.get('category',''),mode)
        groups.setdefault(key,[]).append(sample)
    return [dict(input_tokens=size,category=category+' · '+mode,**mode_summary(group,mode=='forced'))
            for (size,category,mode),group in sorted(groups.items())]
