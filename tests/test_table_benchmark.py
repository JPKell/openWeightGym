"""Independent SQL oracles, data rendering, dirty-cell rules and shortcut resistance."""
import asyncio
import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest

from benchmarks import quality
from benchmarks import store as store_module
from benchmarks.suites import tables
from benchmarks.store import Benchmarks


def read_table(text, format):
    if format == 'csv':
        return list(csv.DictReader(io.StringIO(text)))
    lines = [[cell.strip() for cell in line.strip('|').split('|')] for line in text.splitlines()]
    assert lines[1] == ['---'] * len(lines[0])
    return [dict(zip(lines[0], cells)) for cells in lines[2:]]


def printed(case):
    """Parse the request text, without using the generator's source rows or options."""
    question = case['request']['question']
    marker = re.search(r'\n(CSV|MARKDOWN) table:\n', question)
    assert marker is not None, case['id']
    format = marker[1].lower()
    body = question[marker.end():]
    query_start = re.search(r'\n(?:Select group=|Return |Rank all rows)', body)
    assert query_start is not None, case['id']
    table, query = body[:query_start.start()], body[query_start.start()+1:]
    prices = None
    if case['category'] == 'join':
        assert table.startswith('Orders:\n')
        orders, prices_text = table[len('Orders:\n'):].split('\nPrices:\n')
        table = orders
        prices = read_table(prices_text, format)
    return read_table(table, format), prices, query


def parsed(case):
    rows, prices, _ = printed(case)
    source = case['source']
    assert source['format'] in case['request']['question'].lower()
    assert tables.render(source['rows'], source['columns'], source['format']) in case['request']['question']
    if prices is not None:
        assert tables.render(source['options']['prices'], ['product', 'cents'], source['format']) in case['request']['question']
    return rows


def oracle(case):
    category = case['category']
    rows, prices, query = printed(case)
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE ledger (position INTEGER, id TEXT, grp TEXT, units TEXT, cents TEXT, product TEXT)')
        db.executemany('INSERT INTO ledger VALUES (?,?,?,?,?,?)', [(n, r['id'], r.get('group'), r['units'], r.get('cents'), r.get('product'))
                                                                for n,r in enumerate(rows)])
        if category == 'filter':
            match = re.search(r'^Select group=([^ ]+) AND cents>=(\d+)\.', query)
            assert match is not None
            values = db.execute('SELECT count(*), coalesce(sum(units),0), coalesce(sum(units*cents),0) FROM ledger WHERE grp=? AND CAST(cents AS INTEGER)>=?',
                                (match[1],int(match[2]))).fetchone()
            return dict(zip(('matching_rows','matching_units','matching_cents'),values))
        if category == 'group':
            match = re.search(r'exact group order: ([^.]+)\. Return greatest_group.*strictly greater than (\d+)', query)
            assert match is not None
            order = match[1].split(', ')
            totals = dict(db.execute('SELECT grp,sum(units*cents) AS total FROM ledger GROUP BY grp').fetchall())
            winner = db.execute('SELECT grp FROM ledger GROUP BY grp ORDER BY sum(units*cents) DESC,grp LIMIT 1').fetchone()[0]
            assert set(order) == set(totals)
            return dict(totals=[totals[group] for group in order], greatest_group=winner,
                        groups_above=sum(v>int(match[2]) for v in totals.values()))
        if category == 'rank':
            match = re.search(r'named_position \(one-based rank of ([^)]+)\)', query)
            assert match is not None
            ranked = [r[0] for r in db.execute('SELECT id FROM ledger ORDER BY units*cents DESC,id')]
            return dict(third_id=ranked[2], top_three=ranked[:3], named_position=ranked.index(match[1])+1)
        if category == 'join':
            match = re.search(r'for group=([^)]+)\).*strictly greater than (\d+)', query)
            assert match is not None and prices is not None
            db.execute('CREATE TABLE prices (product TEXT,cents INTEGER)')
            db.executemany('INSERT INTO prices VALUES (?,?)', [(r['product'],r['cents']) for r in prices])
            view = ' FROM ledger l JOIN prices p USING(product)'
            total = db.execute('SELECT sum(l.units*p.cents)'+view+' WHERE l.grp=?',(match[1],)).fetchone()[0]
            greatest = db.execute('SELECT id'+view+' ORDER BY l.units*p.cents DESC,id LIMIT 1').fetchone()[0]
            above = db.execute('SELECT count(*)'+view+' WHERE l.units*p.cents>?',(int(match[2]),)).fetchone()[0]
            return dict(selected_cents=total,greatest_order=greatest,orders_above=above)
        # SQLite applies a different implementation of first-row deduplication and numeric conversion.
        values = db.execute("""WITH first AS (SELECT *,row_number() OVER (PARTITION BY id ORDER BY position) AS n FROM ledger)
                             SELECT count(*),sum(CAST(replace(units,',','') AS INTEGER)*CAST(replace(cents,',','') AS INTEGER)),
                                    sum(CASE WHEN units='' OR cents='' THEN 1 ELSE 0 END) FROM first WHERE n=1""").fetchone()
        return dict(zip(('distinct_rows','total_cents','blank_rows'),values))
    finally:
        db.close()


def test_every_rendered_table_matches_an_independent_sql_answer_key():
    cases = tables.cases(131072)
    assert cases == tables.cases(131072)
    assert len(cases) == 40 and sum(len(c['expected']) for c in cases) == 120
    assert Counter(c['category'] for c in cases) == dict.fromkeys(tables.CATEGORIES,8)
    assert Counter(c['source']['format'] for c in cases) == {'csv':20,'markdown':20}
    frozen=[{'id':c['id'],'source':c['source'],'expected':c['expected'],'question':c['request']['question']} for c in cases]
    assert hashlib.sha256(json.dumps(frozen,sort_keys=True,separators=(',',':')).encode()).hexdigest() == \
        'bf3d90a7c94295d53cb531cc7d3a5d1c2579aee8ea0bf402c6f1a0178683b8df'
    assert tables.PROTOCOL['version']==8 and tables.PROTOCOL['dataset']=='tables-v6'
    assert tables.PROTOCOL['grader_version']==4 and tables.PROTOCOL['checks']==136
    assert quality.PROTOCOL['grader_version']==3
    assert tables.PROTOCOL['rows']==[10,20,30]
    assert tables.PROTOCOL['temperature']==1.0 and tables.PROTOCOL['generation_seed']==42
    assert tables.PROTOCOL['sampling']=={'temperature':1.0,**tables.SAMPLING}
    assert all(c['outputs']==32768 and c['request']['temperature']==1.0 and
               c['request']['sampling']==tables.SAMPLING for c in cases)
    ids = set()
    for case, small in zip(cases,tables.cases(2048)):
        assert len(case['source']['rows']) in (10,20,30)
        assert case['request'] == small['request'] and case['expected'] == small['expected']
        assert case['inputs'] == 0 and small['outputs'] == 1024
        assert oracle(case) == case['expected'],case['id']
        assert json.dumps(case['expected'],sort_keys=True) not in case['request']['question']
        assert len(parsed(case)) == len(case['source']['rows'])
        if case['category'] != 'dirty':
            current = {r['id'] for r in case['source']['rows']}
            assert not ids & current
            ids |= current
        else:
            assert len({r['id'] for r in case['source']['rows']}) < len(case['source']['rows'])
            assert all(',' in r['label'] for r in parsed(case))
            assert any(r['units'] == '' for r in parsed(case)) and any(',' in r['cents'] for r in parsed(case))


def test_headline_has_136_independent_components_and_derived_fields_are_auxiliary():
    samples=[]
    totals=Counter()
    for case in tables.cases(131072):
        metrics,checks=tables.grade(case,json.dumps(oracle(case)))
        assert metrics['passed'] and metrics['correct']==metrics['checks']==len(checks)
        assert len({check['field'] for check in checks})==len(checks)
        assert all(check['passed'] for check in checks)
        category=case['category']
        totals[category]+=len(checks)
        names=[check['field'] for check in checks]
        if category=='group':
            assert names==[f'totals[{index}]' for index in range(4)]
            assert (metrics['auxiliary_correct'],metrics['auxiliary_checks'])==(2,2)
        elif category=='rank':
            assert names==[f'top_three[{index}]' for index in range(3)]+['named_position']
            assert (metrics['auxiliary_correct'],metrics['auxiliary_checks'])==(1,1)
        else:
            assert names==list(case['expected'])
            assert (metrics['auxiliary_correct'],metrics['auxiliary_checks'])==(0,0)
        samples.append({'values':{**metrics,'category':category}})
    assert totals=={'filter':24,'group':32,'rank':32,'join':24,'dirty':24}
    assert sum(totals.values())==tables.PROTOCOL['checks']==136
    summary=tables.summarize(samples)
    assert summary['accuracy']==summary['case_accuracy']==summary['auxiliary_accuracy']==100


@pytest.mark.parametrize(('category','field'),[('group','totals'),('rank','top_three')])
def test_list_elements_get_partial_credit_and_keep_loose_value_semantics(category,field):
    case=next(case for case in tables.cases(131072) if case['category']==category)
    expected=case['expected'][field]
    answer={**case['expected'],field:expected[:2]}
    metrics,checks=tables.grade(case,json.dumps(answer))
    assert metrics['correct']==(2 if category=='group' else 3)
    assert metrics['checks']==4 and not metrics['passed']
    assert [check['passed'] for check in checks[:3]]==[True,True,False]
    wrong=list(expected)
    wrong[0]=True if category=='group' else 123
    metrics,checks=tables.grade(case,json.dumps({**case['expected'],field:wrong}))
    assert metrics['correct']==3 and metrics['checks']==4 and not metrics['passed']
    assert checks[0]['passed'] is False
    loose=list(expected)
    loose[0]=f' {loose[0]:,} ' if category=='group' else f' {loose[0]} '
    metrics,checks=tables.grade(case,json.dumps({**case['expected'],field:loose}))
    assert metrics['correct']==metrics['checks']==4 and metrics['passed']
    assert checks[0]['passed'] is True


def test_derived_answers_never_add_headline_credit_but_gate_whole_case_pass():
    cases=tables.cases(131072)
    group=next(case for case in cases if case['category']=='group')
    rank=next(case for case in cases if case['category']=='rank')
    metrics,checks=tables.grade(group,json.dumps({**group['expected'],'totals':[0,0,0,0]}))
    assert metrics['correct']==0 and metrics['auxiliary_correct']==2 and not metrics['passed']
    assert all(check['field'].startswith('totals[') for check in checks)
    metrics,checks=tables.grade(rank,json.dumps({**rank['expected'],'top_three':['wrong']*3}))
    assert metrics['correct']==1 and metrics['auxiliary_correct']==1 and not metrics['passed']
    assert [check['field'] for check in checks]==['top_three[0]','top_three[1]','top_three[2]','named_position']
    metrics,_=tables.grade(group,json.dumps({**group['expected'],'greatest_group':'wrong','groups_above':-1}))
    assert metrics['correct']==4 and metrics['auxiliary_correct']==0 and not metrics['passed']
    metrics,_=tables.grade(rank,json.dumps({**rank['expected'],'third_id':'wrong'}))
    assert metrics['correct']==4 and metrics['auxiliary_correct']==0 and not metrics['passed']


@pytest.mark.parametrize(('limit','digest','ten_digest'), [
    (32768,'98fc4b0395c05f4e056d3db7f344df103f0a45e5874ff994bd50ca73146cb56e',
     'a9ed004e755c8d609ef1e5bf98c649a1a5d50afd784e882211ce3d15d45fbb41'),
    (65536,'225b0e9fc2414c3475e241fdb2a760a5580ee438d436160fd0813c60c67195f6',
     '386ca06524e05e5cd1449546b8088f967891275a678fe52d749d3f7441fc2263'),
    (131072,'5d3f66d9279a995c9f02beaddb902581ea7f1e92e5f779dc3ff6b7cd8139ebe7',
     '99d30ca8f5c1aa3d92b0ec9d5950919e3a0ae73a1dce0e51ce216ec01b1a8ba1'),
    (190000,'a5e67e9b03d621a89dcdc7bc33ca1fda0f7155da730adb18d3f17991493ecade',
     '6b716947ccbd310c7dfde124a471cd872d3935ac28f9e1c70d57d0f18bd349ae'),
])
def test_all_forty_cases_and_small_row_selection_are_frozen(limit,digest,ten_digest):
    cases=tables.cases(limit)
    assert hashlib.sha256(json.dumps(cases,sort_keys=True,separators=(',',':')).encode()).hexdigest()==digest
    selected=[case for case in cases if len(case['source']['rows'])==10]
    assert len(selected)==14
    assert hashlib.sha256(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()).hexdigest()==ten_digest


def test_all_forty_selected_cases_retain_frozen_draw_provenance_and_keys():
    cases=tables.cases(131072)
    assert len(cases)==40
    assert Counter(len(case['source']['rows']) for case in cases)=={10:14,20:13,30:13}
    selected=[{'id':case['id'],'rows':case['source']['rows']} for case in cases]
    assert hashlib.sha256(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()).hexdigest()== \
        '008dad169661d498237a581f7360fd68dff51a03544729a24114bbb518fef4ca'
    assert all(oracle(case)==case['expected'] for case in cases)


def test_full_output_context_ceiling_changes_no_question_key_or_rows():
    plans=[tables.cases(limit) for limit in (65536,131072,190000)]
    for group in zip(*plans):
        normalized=[{key:value for key,value in case.items() if key!='context'} for case in group]
        assert normalized[0]==normalized[1]==normalized[2]


def test_reduced_cases_have_recomputed_queries_and_coherent_dirty_and_join_tables():
    for case in tables.cases(131072):
        rows, prices, query=printed(case)
        category=case['category']
        if category=='filter':
            group,threshold=re.search(r'^Select group=([^ ]+) AND cents>=(\d+)\.',query).groups()
            candidates=sorted(int(row['cents']) for row in rows if row['group']==group)
            assert int(threshold)==candidates[len(candidates)//2]
        elif category=='group':
            groups_text,threshold=re.search(r'exact group order: ([^.]+)\. Return greatest_group.*strictly greater than (\d+)',query).groups()
            groups=groups_text.split(', ')
            totals=sorted(sum(int(row['units'])*int(row['cents']) for row in rows if row['group']==group) for group in groups)
            assert int(threshold)==(totals[1]+totals[2])//2
        elif category=='rank':
            named=re.search(r'named_position \(one-based rank of ([^)]+)\)',query)[1]
            assert named==rows[-1]['id']
        elif category=='join':
            assert prices is not None and len(prices)==len({row['product'] for row in prices})==10
            assert {row['product'] for row in rows} <= {row['product'] for row in prices}
        else:
            by_id={}
            for row in rows:
                if row['id'] in by_id:
                    assert by_id[row['id']]==row
                by_id[row['id']]=row
            assert len(by_id)<len(rows)
            assert any(row['units']=='' for row in rows)
            assert any(row['cents']=='' for row in rows)
            assert all(',' in row['label'] for row in rows)
            assert any(',' in row['units'] or ',' in row['cents'] for row in rows)


@pytest.mark.parametrize(('version','dataset'),[(4,'tables-v1'),(4,'tables-v2'),(5,'tables-v3'),(6,'tables-v4'),(7,'tables-v5')])
def test_old_table_plans_refuse_new_case_protocol(version,dataset):
    old=dict(tables.PROTOCOL,version=version,dataset=dataset)
    class Store:
        def plan(self,number,value=None):return {'protocol':old,'signature':quality.signature(old),'cases':[]}
    class Worker:store=Store()
    with pytest.raises(ValueError,match='submit a new run'):
        asyncio.run(quality.measure(Worker(),{'id':'n','subject':{}},65536,tables.PROTOCOL,tables.cases,tables.grade))


def test_filter_keys_match_the_printed_question_and_table():
    cases = [case for case in tables.cases(131072) if case['id'].startswith('filter-')]
    assert len(cases) == 8
    formats = set()
    for case in cases:
        question = case['request']['question']
        match = re.search(
            r'\n(CSV|MARKDOWN) table:\n(.*?)\nSelect group=([^\s]+) AND cents>=(\d+)\.',
            question, re.DOTALL,
        )
        assert match is not None, case['id']
        format, printed_table, group, threshold_text = match.groups()
        formats.add(format)
        if format == 'CSV':
            rows = list(csv.DictReader(io.StringIO(printed_table)))
        else:
            lines = printed_table.splitlines()
            cells = [[cell.strip() for cell in line.strip('|').split('|')] for line in lines]
            assert cells[1] == ['---'] * 4
            rows = [dict(zip(cells[0], row)) for row in cells[2:]]
        assert rows and all(list(row) == ['id', 'group', 'units', 'cents'] for row in rows)
        threshold = int(threshold_text)
        selected = [row for row in rows if row['group'] == group and int(row['cents']) >= threshold]
        answer = dict(matching_rows=len(selected),
                      matching_units=sum(int(row['units']) for row in selected),
                      matching_cents=sum(int(row['units']) * int(row['cents']) for row in selected))
        assert answer == case['expected'], case['id']
    assert formats == {'CSV', 'MARKDOWN'}


def test_table_submission_freezes_matching_sampler_and_generation_seed(tmp_path):
    store=Benchmarks(str(tmp_path/'benchmarks.db'))
    try:
        number=store.create('owner','gpt-oss-20b-q4','tables')
        store.activate(number,{'subject':{'settings':{'flags':{'ctx-size':65536}}},
                               'fingerprint':'model-fingerprint','environment':{}})
        protocol=store.execution(number)['subject']['protocol']
        case=tables.cases(65536)[1]
        assert protocol['temperature']==protocol['sampling']['temperature']==case['request']['temperature']==1.0
        assert protocol['seed']==tables.SEED and protocol['generation_seed']==42
        assert protocol['sampling']=={'temperature':1.0,**case['request']['sampling']}
    finally:
        store.db.close()


def test_store_regrades_saved_answers_without_changing_raw_cases_or_prompts(tmp_path,monkeypatch):
    legacy_protocol={**tables.PROTOCOL,'grader_version':3,'checks':120,
                     'implementation_sha256':'99d18015c600ebd36cff24dc107f8a8548527dd9afc373807fe90758cac87b27'}
    def legacy_grade(case,answer):
        metrics,checks=quality.grade_fields(case,answer)
        metrics['table_format']=case['source']['format']
        return metrics,checks
    monkeypatch.setitem(store_module.MODULES,'tables',SimpleNamespace(PROTOCOL=legacy_protocol,grade=legacy_grade))
    store=Benchmarks(str(tmp_path/'benchmarks.db'))
    try:
        number=store.create('owner','gpt-oss-20b-q4','tables')
        store.activate(number,{'subject':{'settings':{'flags':{'ctx-size':65536}}},
                               'fingerprint':'model-fingerprint','environment':{}})
        case=next(case for case in tables.cases(65536) if case['category']=='group')
        output=json.dumps(case['expected'])
        old_metrics,old_checks=legacy_grade(case,output)
        assert old_metrics['checks']==len(old_checks)==3
        original_subject=store.execution(number)['subject']
        assert original_subject['protocol']['grader_version']==3
        store.plan(number,{'protocol':legacy_protocol,'signature':quality.signature(legacy_protocol),'cases':[case]})
        store.keep(number,case['id'],{**old_metrics,'success':True,'category':case['category']},output,'native log',
                   {'case':case,'answer':output,'checks':old_checks,'prompt':case['request']['question']})
        store.finish(number,'completed')
        before=store.samples(number)[0]
        frozen_plan=store.db.execute('SELECT plan FROM benchmark_plans WHERE run=?',(number,)).fetchone()[0]
        monkeypatch.setitem(store_module.MODULES,'tables',tables)
        assert store.regrade()==1 and store.regrade()==0
        after=store.samples(number)[0]
        assert after['output']==before['output']==output and after['log']==before['log']=='native log'
        assert after['evidence']['case']==before['evidence']['case']==case
        assert after['evidence']['prompt']==before['evidence']['prompt']==case['request']['question']
        assert after['evidence']['answer']==before['evidence']['answer']==output
        assert store.db.execute('SELECT plan FROM benchmark_plans WHERE run=?',(number,)).fetchone()[0]==frozen_plan
        assert before['values']['checks']==len(before['evidence']['checks'])==3
        assert after['values']['checks']==len(after['evidence']['checks'])==4
        assert after['values']['correct']==4 and after['values']['passed']
        subject=store.execution(number)['subject']
        assert subject=={**original_subject,'protocol':{**original_subject['protocol'],'grader_version':4}}
        assert store.get(number)['current_protocol'] is False  # saved source identity remains the old hash
    finally:
        store.db.close()


def test_first_ten_rows_and_nulls_cannot_pass_the_table_suite():
    samples = []
    for case in tables.cases(131072):
        partial = tables.solve(case['category'],case['source']['rows'][:10],case['source']['options'])
        metrics,_ = tables.grade(case,json.dumps(partial))
        samples.append({'values':{**metrics,'category':case['category']}})
    result = tables.summarize(samples)
    assert result['accuracy']<50
    assert all(tables.grade(c,json.dumps(dict.fromkeys(c['expected'])))[0]['correct']==0 for c in tables.cases(131072))
    assert tables.summarize([]) == {}


def test_ties_use_lexical_ids_and_groups_and_ranks_are_one_based():
    rows = [dict(id='b',group='B',units=2,cents=10),dict(id='a',group='A',units=1,cents=20),
            dict(id='c',group='C',units=4,cents=5)]
    assert tables.solve('rank',rows,{'named':'b'}) == dict(third_id='c',top_three=['a','b','c'],named_position=2)
    assert tables.solve('group',rows,{'groups':['C','B','A'],'threshold':20}) == dict(totals=[20,20,20],greatest_group='A',groups_above=0)
    joined = [dict(id='b',group='B',units=2,product='p'),dict(id='a',group='B',units=2,product='p')]
    assert tables.solve('join',joined,dict(prices=[dict(product='p',cents=10)],group='B',threshold=20)) == dict(selected_cents=40,greatest_order='a',orders_above=0)


def test_dirty_numbers_blanks_and_first_duplicate_rules_are_exact():
    rows = [dict(id='a',label='A, B',units='1,200',cents='2,500'),
            dict(id='a',label='A, B',units='9,000',cents='9,000'),
            dict(id='b',label='C',units='',cents=''),dict(id='c',label='D',units='5',cents='')]
    assert tables.solve('dirty',rows,{}) == dict(distinct_rows=3,total_cents=3000000,blank_rows=2)
    text = tables.render(rows,['id','label','units','cents'],'csv')
    assert list(csv.DictReader(io.StringIO(text))) == rows
