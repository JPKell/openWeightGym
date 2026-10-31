"""Imported cases use the normal durable worker; their keys never reach native inference."""

import copy
import json
from pathlib import Path

import pytest

from benchmarks import custom, regressions
from benchmarks.store import Benchmarks
from db import Database
from db.transfer import copy_store
from test_app import add_user, fake_cards, fake_llama_server, in_an_empty_folder, model  # noqa: F401
from benchmark_clients import gateway
from test_benchmarks import bench_model, start, wait
from test_postgresql import postgres  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401


def dataset(version=1):
    return {'format':'suite-cases/v1', 'id':'my-checks', 'name':'My <checks>', 'version':version,
            'cases':[{'id':'alpha', 'question':'Return only the JSON object with value 7.',
                      'assertions':[{'id':'value', 'type':'json', 'expected':{'value':7}},
                                    {'id':'shape', 'type':'schema', 'expected':{'type':'object', 'required':['value'],
                                      'properties':{'value':{'type':'integer'}}, 'additionalProperties':False}}]}]}


@pytest.mark.parametrize('value,accepted', [(' {"value":7} ',True), ('```json\n{"value":7}\n```',False),
    ('{"value":true}',False), ('{"value":7,"value":7}',False), ('{"value":NaN}',False),
    ('{"value":7,"extra":1}',False), ('null',False)])
def test_strict_json_and_structural_assertions(value, accepted):
    case = custom.validate(dataset())['cases'][0]
    metrics, checks = custom.grade(case, value)
    assert metrics['passed'] is accepted and all(type(check['passed']) is bool for check in checks)
    assert metrics['checks'] == 2


@pytest.mark.parametrize('change', ['duplicate-case','duplicate-check','schema-ref','schema-regex','invalid-schema',
    'code','tools','case-count','outputs','bad-format','bad-version','nonfinite','deep'])
def test_invalid_imports_are_rejected(change):
    value = dataset()
    case = value['cases'][0]
    if change == 'duplicate-case': value['cases'].append(copy.deepcopy(case))
    elif change == 'duplicate-check': case['assertions'].append(copy.deepcopy(case['assertions'][0]))
    elif change == 'schema-ref': case['assertions'][1]['expected'] = {'$ref':'http://outside.invalid/schema'}
    elif change == 'schema-regex': case['assertions'][1]['expected'] = {'type':'string','pattern':'(a+)+$'}
    elif change == 'invalid-schema': case['assertions'][1]['expected'] = {'type':7}
    elif change == 'code': case['assertions'][0]['type'] = 'python'
    elif change == 'tools': case['tools'] = []
    elif change == 'case-count': value['cases'] = [copy.deepcopy(case)] * 129
    elif change == 'outputs': case['outputs'] = True
    elif change == 'bad-format': value['format'] = 'unknown'
    elif change == 'bad-version': value['version'] = 0
    elif change == 'nonfinite': case['assertions'][0]['expected'] = float('inf')
    elif change == 'deep':
        nested = []
        for _ in range(22): nested = [nested]
        case['assertions'][0]['expected'] = nested
    with pytest.raises(ValueError): custom.validate(value)


def test_exact_assertion_is_not_json_or_fuzzy():
    case = {'assertions':[{'id':'word','type':'exact','expected':'READY'}]}
    assert custom.grade(case,'READY')[0]['passed']
    assert not custom.grade(case,'Ready')[0]['passed']
    assert not custom.grade(case,'"READY"')[0]['passed']


def repository_contract(source):
    store = Benchmarks(source)
    first = store.import_dataset(dataset(), 'owner')
    assert first['created'] and not store.import_dataset(dataset(), 'owner')['created']
    changed = dataset(); changed['cases'][0]['question'] = 'Changed.'
    with pytest.raises(ValueError, match='different content'): store.import_dataset(changed, 'owner')
    number = store.create('owner', 'small', first['benchmark'])
    initial = store.execution(number)['subject']['protocol']
    second = dataset(2); second['cases'][0]['question'] = 'New version.'
    store.import_dataset(second, 'owner')
    store.activate(number, {'fingerprint':'abc','environment':{}, 'subject':{'settings':{'gpus':0,'flags':{'ctx-size':2048}}}})
    assert store.execution(number)['subject']['protocol']['version'] == 1
    assert store.module(first['benchmark'], initial).cases(2048)[0]['request']['question'] == dataset()['cases'][0]['question']
    assert next(b for b in store.definitions() if b['id'] == first['benchmark'])['version'] == 2
    store.configure(first['benchmark'], False, 'case_accuracy')
    assert store.get(number)['version'] == 1 and store.get(number)['latest_version'] == 2
    store.db.close()
    reopened = Benchmarks(source)
    assert reopened.execution(number)['subject']['protocol']['dataset_sha256'] == first['sha256']
    return reopened


def test_immutable_versions_and_submission_freeze_survive_restart(tmp_path):
    store = repository_contract(tmp_path/'cases.db')
    other = Benchmarks(tmp_path/'other.db')
    assert not any(b.get('custom') for b in other.definitions())  # no cross-app registry mutation
    other.db.close(); store.db.close()


def test_postgresql_imported_versions_and_verified_transfer(postgres, tmp_path):
    store = repository_contract(postgres)
    store.db.close()
    target = Database.sqlite(tmp_path/'copied.db')
    assert copy_store(postgres, target)['state'] == 'verified'
    restored = Benchmarks(target)
    assert restored.module('custom_my-checks', {'version':1}).dataset == dataset()
    assert restored.module('custom_my-checks').dataset == {**dataset(2), 'cases':[{**dataset(2)['cases'][0], 'question':'New version.'}]}
    restored.db.close()


def recorded(store, contents, state='completed', kind='custom_my-checks'):
    number = store.create('owner','small',kind)
    store.activate(number, {'fingerprint':'abc','environment':{}, 'subject':{'settings':{'flags':{'ctx-size':2048}}}})
    cases = store.module(kind).cases(2048)
    store.plan(number, {'cases':cases, 'protocol':store.execution(number)['subject']['protocol']})
    for case, answer in zip(cases, contents):
        metrics, checks = custom.grade(case, answer)
        store.keep(number,case['id'],{'success':True,**metrics},answer,'',{'case':case,'answer':answer,'checks':checks})
    store.finish(number,state)
    return number


def test_regression_comparison_distinguishes_failures_missing_and_incompatible_data(tmp_path):
    store = Benchmarks(tmp_path/'compare.db')
    value = dataset()
    value['cases'].append({**copy.deepcopy(value['cases'][0]), 'id':'beta'})
    store.import_dataset(value,'owner')
    baseline = recorded(store,['{"value":7}','{"value":8}'])
    current = recorded(store,['{"value":8}','{"value":7}'])
    result = regressions.compare(store.get(current,True),store.get(baseline,True))
    assert result['counts']['new failure'] == 1 and result['counts']['improved'] == 1
    assert result['rows'][0]['newly_failing_checks'] == ['1: value']
    incomplete = recorded(store,['{"value":7}'],'cancelled')
    partial = regressions.compare(store.get(incomplete,True),store.get(baseline,True))
    assert partial['counts']['not comparable'] == 1 and partial['warnings']
    with store.db:
        store.db.execute('UPDATE benchmark_samples SET measurements=? WHERE run=? AND sample=?',
                         (json.dumps({'success':False,'passed':False}),current,'alpha'))
    assert regressions.compare(store.get(current,True),store.get(baseline,True))['rows'][0]['status'] == 'not comparable'
    value['version'] = 2
    store.import_dataset(value,'owner')
    newer = recorded(store,['{"value":7}','{"value":7}'])
    with pytest.raises(ValueError,match='versions differ'): regressions.compare(store.get(newer,True),store.get(baseline,True))
    store.db.close()


def test_data_import_run_export_and_permissions_use_existing_services(fake_llama_server, fake_cards):
    models = bench_model()
    models['small']['flags']['benchmark-reply'] = '{"value":7}'
    with gateway(fake_llama_server, models=models,nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller,'owner','admin')
        plain = add_user(caller,'plain','plain',by=owner)
        assert caller.post('/benchmarks/datasets',headers=plain,json=dataset()).status_code == 403
        accepted = caller.post('/benchmarks/datasets',headers=owner,json=dataset())
        assert accepted.status_code == 201, accepted.text
        kind = accepted.json()['benchmark']
        number = start(caller,owner,kind)
        result = wait(caller,owner,number,lambda r:r['state'] in ('completed','failed'))
        assert result['state'] == 'completed',result
        assert result['metrics']['accuracy'] == 100 and result['samples_count'] == 1
        rendered = result['samples'][0]['evidence']['prompt']
        assert 'assertions' not in rendered and 'additionalProperties' not in rendered
        assert result['subject']['protocol']['dataset_sha256'] == accepted.json()['sha256']
        assert caller.get(f'/benchmarks/datasets/{kind}/1',headers=owner).json() == dataset()
        assert caller.get(f'/benchmarks/datasets/{kind}/1',headers=plain).status_code == 403
        assert caller.get('/benchmarks/regressions',headers=owner,params={'run':number,'baseline':number}).json()['counts']['still passing'] == 1
        caller.post('/loadcoach/users/plain',headers=owner,json={'own':{'models':[]}})
        assert caller.get('/benchmarks/regressions',headers=plain,params={'run':number,'baseline':number}).status_code == 404
        assert caller.post('/benchmarks/datasets',headers=owner,content=b'{"format":"a","format":"b"}').status_code == 400
        assert caller.post('/benchmarks/datasets',headers=owner,content=b' '*(custom.MAX_BYTES+1)).status_code == 400


def test_paused_imported_run_resumes_its_submitted_version_after_restart(fake_llama_server, fake_cards, tmp_path):
    models = bench_model(.3)
    models['small']['flags']['benchmark-reply'] = '{"value":7}'
    value = dataset()
    value['cases'].append({**copy.deepcopy(value['cases'][0]),'id':'beta'})
    database = str(tmp_path/'resume.db')
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as caller:
        owner = add_user(caller,'owner','admin')
        assert caller.post('/benchmarks/datasets',headers=owner,json=value).status_code == 201
        number = start(caller,owner,'custom_my-checks')
        saved = wait(caller,owner,number,lambda r:r['samples_count'] == 1)['samples'][0]
        assert caller.post('/benchmarks/control',headers=owner,json={'run':number,'action':'pause'}).status_code == 200
        newer = copy.deepcopy(value); newer['version'] = 2
        newer['cases'][1]['assertions'][0]['expected'] = {'value':9}
        assert caller.post('/benchmarks/datasets',headers=owner,json=newer).status_code == 201
    with gateway(fake_llama_server,models=models,nvidia_smi=fake_cards[0],database=database) as caller:
        assert caller.post('/benchmarks/control',headers=owner,json={'run':number,'action':'resume'}).status_code == 200
        run = wait(caller,owner,number,lambda r:r['state'] in ('completed','failed'))
        assert run['state'] == 'completed' and run['metrics']['accuracy'] == 100,run
        assert run['samples'][0] == saved and len(run['samples']) == 2
        assert run['version'] == 1 and run['latest_version'] == 2


def test_manage_page_import_export_and_regression_rendering(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser,'jordan')
    loaded = browser.post('/benchmarks/manage',data={'action':'import','dataset':json.dumps(dataset())})
    assert loaded.status_code == 200 and 'Imported immutable case-set version.' in loaded.text
    assert 'My &lt;checks&gt;' in loaded.text and 'Imported</span>' in loaded.text
    exported = browser.get('/benchmarks/manage?benchmark=custom_my-checks&export=1')
    assert exported.json() == dataset() and 'attachment' in exported.headers['content-disposition']
    store = Benchmarks(tmp_path/'data'/'benchmarks.db')
    baseline = recorded(store,['{"value":7}'])
    current = recorded(store,['{"value":8}'])
    # Bind fake runs to a local model so the real visibility checks can resolve it.
    from loadcoach.models import Models
    library = Models(tmp_path/'loadcoach.db'); library.put('small',model('small.gguf',0))
    for number in (baseline,current):
        with store.db: store.db.execute('UPDATE benchmark_runs SET model_id=? WHERE id=?',(library.identity('small'),number))
    shown = browser.get(f'/benchmarks/run/{current}?baseline={baseline}')
    assert shown.status_code == 200 and 'new failure: 1' in shown.text and '1: value' in shown.text
    assert browser.post('/benchmarks/manage',data={'action':'import','dataset':json.dumps(dataset())},headers={'Origin':'https://elsewhere.invalid'}).status_code == 403
    library.db.close(); store.db.close()


def test_manage_page_deletes_an_unused_case_set_and_keeps_a_used_one(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser,'jordan')
    browser.post('/benchmarks/manage',data={'action':'import','dataset':json.dumps(dataset())})
    browser.post('/benchmarks/manage',data={'action':'import','dataset':json.dumps(dataset(2))})
    shown = browser.get('/benchmarks/manage?benchmark=custom_my-checks')
    assert 'Delete case set' in shown.text
    deleted = browser.post('/benchmarks/manage',data={'action':'delete-dataset','benchmark':'custom_my-checks'})
    assert deleted.status_code == 200 and 'Deleted the case set.' in deleted.text and 'My &lt;checks&gt;' not in deleted.text
    store = Benchmarks(tmp_path/'data'/'benchmarks.db')
    assert not store.db.execute("SELECT 1 FROM benchmark_datasets").fetchone()
    assert not any(b['id'] == 'custom_my-checks' for b in store.definitions())
    # A run is scored again from its dataset, so a set that any run used stays.
    browser.post('/benchmarks/manage',data={'action':'import','dataset':json.dumps(dataset())})
    store.create('owner','small','custom_my-checks')
    kept = browser.post('/benchmarks/manage',data={'action':'delete-dataset','benchmark':'custom_my-checks'})
    assert '1 benchmark run used this case set' in kept.text
    assert any(b['id'] == 'custom_my-checks' for b in store.definitions())
    with pytest.raises(ValueError, match='imported dataset'): store.delete_dataset('ifeval')
    store.db.close()
