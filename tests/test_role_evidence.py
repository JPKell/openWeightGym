"""Benchmark-informed routing with offline models and no production inference."""

import copy
import sqlite3
import time

import pytest

from loadcoach.role_evidence import RoleEvidence
from loadcoach.roles import check_role
from test_app import add_user, fake_cards, fake_llama_server, gateway, in_an_empty_folder, model  # noqa: F401
from test_outside import service  # noqa: F401


ENV = {"engine_hash": "engine", "machine_hash": "machine"}


def snapshot(scores, identities=None, direction="high", version=1, kind="accuracy"):
    return {"definitions": [{"id": kind, "name": kind, "formula": "score", "direction": direction, "version": version}],
            "results": [{"model_id": (identities or {}).get(name, (name, name))[0],
                         "fingerprint": (identities or {}).get(name, (name, name))[1], "benchmark": kind,
                         "version": version, "run": "run_" + name, "measured_at": time.time(), "score": score,
                         "formula": "score", **ENV} for name, score in scores.items()]}


def evidence():
    return RoleEvidence(sqlite3.connect(":memory:"))


def test_normalization_directions_weights_ties_and_missing_policy():
    kept = evidence()
    kept.accept(snapshot({"a": 10, "b": 20}))
    kept.accept(snapshot({"a": 2, "b": 8}, direction="low", kind="latency"))
    role = {"models": {"a": 1, "b": 1, "unknown": 3, "disabled": 0}, "benchmarks": {"accuracy": 1, "latency": 3}}
    ids = {name: (name, name) for name in role['models']}
    ranked = kept.rank(role, ids, ENV)
    assert ranked['a']['score'] == 1.75 and ranked['b']['score'] == 1.25
    assert [point['share'] for point in ranked['a']['evidence']] == [.25, .75]
    assert sum(point['contribution'] for point in ranked['a']['evidence']) == ranked['a']['quality']
    assert ranked['unknown']['score'] == 3 and ranked['unknown']['quality'] is None
    assert 'manual weight' in ranked['unknown']['fallback'] and 'disabled' not in ranked
    assert all(point['contribution'] is None for point in ranked['unknown']['evidence'])
    assert kept.rank(dict(role, missing='exclude'), ids, ENV)['unknown']['evidence_excluded']
    # Normalization never uses a model that the caller cannot use.
    narrow = kept.rank(role, {'a': ('a', 'a')}, ENV)
    assert narrow['a']['score'] == 1.5 and narrow['b']['quality'] is None
    kept.accept(snapshot({'a': 4, 'b': 4}))
    assert kept.rank({'models': {'a': 1, 'b': 1}, 'benchmarks': {'accuracy': 1}}, ids, ENV)['a']['quality'] == .5


def test_fingerprint_protocol_age_environment_formula_and_partial_updates():
    kept = evidence()
    current = snapshot({'a': 1, 'b': 2})
    kept.accept(current)
    role = {'models': {'a': 1, 'b': 1}, 'benchmarks': {'accuracy': 1}}
    ids = {'a': ('a', 'a'), 'b': ('b', 'b')}
    assert kept.rank(role, dict(ids, a=('a', 'changed')), ENV)['a']['quality'] is None
    changed = dict(ENV, engine_hash='new-engine')
    warned = kept.rank(role, ids, changed)['a']
    assert warned['quality'] == 0 and warned['evidence'][0]['warnings']
    excluded = kept.rank(dict(role, stale='exclude', missing='exclude'), ids, changed)['a']
    assert excluded['evidence_excluded'] and excluded['evidence'][0]['reason'] == 'stale evidence excluded'
    assert kept.rank(dict(role, max_age_days=1), ids, ENV, now=time.time()+172800)['a']['evidence'][0]['warnings']
    # Refreshing one model preserves the other and rejects older replacement measurements.
    update = snapshot({'a': 3})
    kept.accept(update)
    kept.accept(current)
    assert kept.rank(role, ids, ENV)['a']['quality'] == 1
    assert kept.rank(role, ids, ENV)['b']['evidence'][0]['raw'] == 2
    definitions = copy.deepcopy(current['definitions'])
    definitions[0]['version'] = 2
    kept.accept({'definitions': definitions, 'results': []})
    assert kept.rank(role, ids, ENV)['a']['quality'] is None
    definitions[0].update(version=1, formula='different_score')
    kept.accept({'definitions': definitions, 'results': []})
    assert kept.rank(role, ids, ENV)['b']['quality'] is None


@pytest.mark.parametrize('change', [lambda s: s['results'][0].update(score=float('nan')),
    lambda s: s['results'][0].update(measured_at=time.time()+10000),
    lambda s: s['definitions'][0].update(direction='random'), lambda s: s.update(results='no'),
    lambda s: s['results'][0].update(speed=-1)])
def test_bad_snapshots_do_not_replace_accepted_evidence(change):
    kept = evidence()
    original = snapshot({'a': 2})
    kept.accept(original)
    bad = copy.deepcopy(original)
    change(bad)
    with pytest.raises(ValueError):
        kept.accept(bad)
    assert kept.rank({'models': {'a': 1}, 'benchmarks': {'accuracy': 1}}, {'a': ('a', 'a')}, ENV)['a']['evidence'][0]['raw'] == 2


def catalogue(caller, owner):
    until = time.monotonic()+3
    while time.monotonic() < until:
        models = caller.get('/v1/models', headers=owner).json()['data']
        local = [one for one in models if one['owned_by'] == 'loadcoach']
        if all(one.get('fingerprint') for one in local):
            return {one['id']: (one['model_id'], one['fingerprint']) for one in local}
        time.sleep(.02)
    raise AssertionError(models)


def test_choices_update_without_inference_survive_restart_and_preserve_explicit_selection(fake_llama_server, tmp_path):
    models = {name: model(name+'.gguf', 1) for name in ('a', 'b')}
    database = str(tmp_path/'routing.db')
    with gateway(fake_llama_server, models, database=database) as caller:
        owner = add_user(caller, 'owner', 'admin')
        plain = add_user(caller, 'plain', 'plain', by=owner)
        ids = catalogue(caller, owner)
        first = snapshot({'a': 10, 'b': 20}, ids)
        assert caller.post('/loadcoach/role-evidence', headers=plain, json=first).status_code == 403
        assert caller.post('/loadcoach/role-evidence', headers=owner, json=first).status_code == 200
        role = {'models': {'a': 1, 'b': 1}, 'benchmarks': {'accuracy': 1}, 'missing': 'manual'}
        assert caller.post('/loadcoach/roles/work', headers=owner, json=role).status_code == 201
        picked = caller.get('/loadcoach/pick?model=work:best', headers=owner).json()
        assert picked['picked'] == 'b' and picked['models'][0]['quality'] == 1
        assert all(one['decision'] and one['fit'] for one in picked['models'])
        changed = snapshot({'a': 30}, ids)
        assert caller.post('/loadcoach/role-evidence', headers=owner, json=changed).status_code == 200
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] == 'a'
        # Preview and evidence updates did not load a model.
        assert not caller.get('/loadcoach/status', headers=owner).json()['loaded']
        explicit = caller.post('/v1/chat/completions', headers=owner, json={'model':'b','messages':[]})
        assert explicit.status_code == 200 and 'x-loadcoach-role' not in explicit.headers
        result = caller.post('/v1/chat/completions', headers=owner, json={'model':'work:best','messages':[]})
        assert result.headers['x-loadcoach-picked'] == 'a'
        # Renames preserve benchmark settings and stable evidence identity.
        assert caller.post('/loadcoach/model-rename', headers=owner, json={'name':'a','to':'renamed'}).status_code == 200
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] == 'renamed'
        saved = caller.get('/loadcoach/roles', headers=owner).json()['roles'][0]
        assert saved['benchmarks'] == {'accuracy': 1}
    # LoadCoach answers from the durable snapshot without any WeightRoom service.
    with gateway(fake_llama_server, {}, database=database) as caller:
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] == 'renamed'
        assert caller.post('/loadcoach/users/plain', headers=owner, json={'own':{'models':['b']}}).status_code == 200
        assert caller.get('/loadcoach/pick?model=work:best', headers=plain).json()['picked'] == 'b'


@pytest.mark.parametrize('settings', [{'benchmarks': []}, {'missing': 'zero'}, {'stale':'ignore'}, {'max_age_days':float('nan')}, {'requires': {'tokens': 1.5}}, {'requires': {'tools': 1}}])
def test_invalid_role_preferences(settings):
    with pytest.raises(ValueError):
        check_role('work', {'models': {'a':1}, **settings}, {'a'})


def test_worker_publishes_only_its_task_and_does_not_change_definitions(fake_llama_server, fake_cards):
    from benchmark_clients import gateway as benchmark_gateway
    from test_benchmarks import bench_model, start, wait
    with benchmark_gateway(fake_llama_server, models=bench_model(), nvidia_smi=fake_cards[0]) as caller:
        owner = add_user(caller, 'owner', 'admin')
        # This creates the definition snapshot before the first measurement.
        assert caller.post('/benchmarks/publish', headers=owner).status_code == 200
        number = start(caller, owner, 'context')
        run = wait(caller, owner, number, lambda one: one['state'] == 'completed')
        role = {'models': {'small': 1}, 'benchmarks': {'context': 1}, 'missing': 'exclude', 'stale': 'exclude'}
        assert caller.post('/loadcoach/roles/work', headers=owner, json=role).status_code == 201
        picked = caller.get('/loadcoach/pick?model=work:best', headers=owner).json()
        assert picked['picked'] == 'small' and picked['models'][0]['evidence'][0]['raw'] == 2048
        token = caller.app.state.benchmarks.state.store.token(number)
        scoped = {'x-loadcoach-task': token}
        exported = caller.app.state.benchmarks.state.store.role_snapshot(number=number)
        exported['results'][0]['model_id'] = 'another-model'
        assert caller.post('/loadcoach/tasks/'+number+'/finish', headers=scoped, json={'state':'completed', 'role_evidence':exported}).status_code == 400
        assert caller.post('/loadcoach/role-evidence', headers=scoped, json=exported).status_code == 401
        exported['results'][0]['model_id'] = run['model_id']
        exported['definitions'][0]['direction'] = 'low'
        assert caller.post('/loadcoach/tasks/'+number+'/finish', headers=scoped, json={'state':'completed', 'role_evidence':exported}).status_code == 200
        definitions = caller.get('/loadcoach/roles', headers=owner).json()['benchmarks']
        assert next(one for one in definitions if one['id'] == 'context')['direction'] == 'high'
        # Formula/direction changes reuse saved samples and immediately publish new scores.
        assert caller.post('/benchmarks/definitions', headers=owner, json={'benchmark':'context', 'enabled':True,
            'formula':'max_context_tokens * parameter_count', 'green':'low'}).status_code == 200
        updated = caller.get('/loadcoach/pick?model=work:best', headers=owner).json()
        assert updated['models'][0]['evidence'][0]['raw'] == 2048 * 123456
        assert caller.get('/benchmarks/'+number, headers=owner).json()['samples'] == run['samples']


def test_requirements_and_impossible_gpu_placement_are_explained(fake_llama_server, tmp_path):
    projector = tmp_path/'projector.gguf'; projector.touch()
    models = {'blind':model('blind.gguf', 1), 'seeing':model('seeing.gguf', 1, mmproj=str(projector)), 'impossible':model('impossible.gguf', 3)}
    with gateway(fake_llama_server, models) as caller:
        owner = add_user(caller, 'owner', 'admin')
        assert caller.post('/loadcoach/roles/look', headers=owner, json={'models':{'blind':3,'impossible':2,'seeing':1}, 'requires':{'pictures':True}}).status_code == 201
        preview = caller.get('/loadcoach/pick?model=look:best', headers=owner).json()
        assert preview['picked'] == 'seeing'
        reasons = {one['name']:one['why_not'] for one in preview['models']}
        assert reasons['blind'] == 'it cannot see pictures'
        # An impossible model without the capability also fails the image requirement first.
        assert caller.post('/loadcoach/roles/work', headers=owner, json={'models':{'impossible':2,'blind':1}}).status_code == 201
        preview = caller.get('/loadcoach/pick?model=work:best', headers=owner).json()
        assert preview['picked'] == 'blind' and 'too few enabled cards' in preview['models'][0]['why_not']


def test_sensitive_and_local_only_roles_never_use_hosted_offload(fake_llama_server, service, tmp_path):
    keys = tmp_path/'keys'; keys.mkdir()
    (keys/'rented.key').write_text('good-key')
    with gateway(fake_llama_server, {'local':model('local.gguf', 1)}, keys=str(keys)) as caller:
        owner = add_user(caller, 'owner', 'admin')
        assert caller.post('/loadcoach/services/rented', headers=owner, json={'url':service, 'models':{'remote':{'theirs':'vendor/big'}}}).status_code == 201
        role = {'models': {'local':1, 'remote':3}}
        assert caller.post('/loadcoach/roles/work', headers=owner, json=role).status_code == 201
        assert caller.get('/loadcoach/pick?model=work:best', headers=owner).json()['picked'] == 'remote'
        answer = caller.post('/v1/chat/completions', headers={**owner, 'x-loadcoach-sensitivity':'restricted'}, json={'model':'work:best', 'messages':[]})
        assert answer.status_code == 200 and answer.headers['x-loadcoach-picked'] == 'local' and 'x-loadcoach-hosted' not in answer.headers
        assert caller.post('/loadcoach/roles/work', headers=owner, json=dict(role, requires={'stays':True})).status_code == 200
        assert caller.post('/loadcoach/unload', headers=owner).status_code == 200
        answer = caller.post('/v1/chat/completions', headers={**owner, 'x-loadcoach-offload':'remote'}, json={'model':'work:best', 'messages':[]})
        assert answer.status_code == 200 and answer.headers['x-loadcoach-picked'] == 'local' and 'x-loadcoach-hosted' not in answer.headers


def test_role_preview_passes_over_a_hosted_service_above_its_spend_ceiling(fake_llama_server, service, tmp_path):
    keys = tmp_path/'keys'; keys.mkdir()
    (keys/'rented.key').write_text('good-key')
    with gateway(fake_llama_server, {'local':model('local.gguf', 1)}, keys=str(keys)) as caller:
        owner = add_user(caller, 'owner', 'admin')
        assert caller.post('/loadcoach/services/rented', headers=owner, json={'url':service, 'ceiling':.01, 'models':{'remote':{'theirs':'vendor/big'}}}).status_code == 201
        assert caller.post('/loadcoach/roles/work', headers=owner, json={'models':{'remote':3,'local':1}}).status_code == 201
        assert caller.post('/v1/chat/completions', headers=owner, json={'model':'remote','messages':[],'cost':.02}).status_code == 200
        preview = caller.get('/loadcoach/pick?model=work:best', headers=owner).json()
        assert preview['picked'] == 'local' and 'ceiling' in preview['models'][0]['why_not']


def test_coding_and_chat_weights_independently_change_actual_role_selection(fake_llama_server):
    models = {name: model(name+'.gguf', 1) for name in ('coder', 'talker')}
    with gateway(fake_llama_server, models) as caller:
        owner = add_user(caller, 'owner', 'admin')
        ids = catalogue(caller, owner)
        code = snapshot({'coder': 90, 'talker': 40}, ids, kind='code_retrieval')
        instructions = snapshot({'coder': 40, 'talker': 90}, ids, kind='ifeval')
        combined = {'definitions':code['definitions']+instructions['definitions'], 'results':code['results']+instructions['results']}
        assert caller.post('/loadcoach/role-evidence', headers=owner, json=combined).status_code == 200
        code_role = {'models':{'coder':1, 'talker':1}, 'benchmarks':{'code_retrieval':3,'ifeval':1}}
        chat_role = dict(code_role, benchmarks={'code_retrieval':1,'ifeval':3})
        assert caller.post('/loadcoach/roles/code', headers=owner, json=code_role).status_code == 201
        assert caller.post('/loadcoach/roles/chat', headers=owner, json=chat_role).status_code == 201
        for role, expected in [('code', 'coder'), ('chat', 'talker')]:
            preview = caller.get('/loadcoach/pick?model='+role+':best', headers=owner).json()
            assert preview['picked'] == expected
            assert preview['models'][0]['quality'] == .75
            answer = caller.post('/v1/chat/completions', headers=owner, json={'model':role+':best','messages':[]})
            assert answer.status_code == 200 and answer.headers['x-loadcoach-picked'] == expected
        # Changing code's weights does not alter chat; a zero explicitly ignores the code benchmark.
        assert caller.post('/loadcoach/roles/code', headers=owner, json=dict(code_role, benchmarks={'code_retrieval':0,'ifeval':1})).status_code == 200
        assert caller.get('/loadcoach/pick?model=code:best', headers=owner).json()['picked'] == 'talker'
        saved_chat = caller.get('/loadcoach/roles', headers=owner).json()['roles'][1]
        assert saved_chat['benchmarks'] == chat_role['benchmarks']
