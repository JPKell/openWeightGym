"""Demand-driven residency and score-bounded reuse, using isolated fake inference only."""
import time

import pytest

from loadcoach.config import Config
from loadcoach.models import Models
from loadcoach.roles import check_role, fast_preference_seconds, pick, seconds_to_answer
from loadcoach.runner import Runner, Server, Ticket
from test_app import add_user, alive, ask, fake_cards, fake_llama_server, gateway, in_an_empty_folder, model


def candidate(name, score, loaded=False, bonus=25):
    return dict(name=name, score=score, resident=loaded, warm=loaded, load=20, speed=20,
                loaded_factor=1+bonus/100 if loaded else 1, fast_loaded_factor=2 if loaded else 1)


def test_loaded_reuse_has_a_score_boundary_and_does_not_depend_on_rank():
    best = candidate('best', 100)
    assert pick(None, [best, candidate('resident', 80, True)])[0] == 'resident'
    assert pick(None, [best, candidate('resident', 79.9, True)])[0] == 'best'
    assert pick(None, [best, candidate('second', 95), candidate('third', 90, True)])[0] == 'third'
    assert pick(None, [best, candidate('resident', 90, True, bonus=0)])[0] == 'best'
    assert pick('best', [best, candidate('resident', 90, True)])[0] == 'best'
    assert pick('available', [best, candidate('resident', 1, True)])[0] == 'resident'
    assert pick(None, [best, candidate('hosted', 90)])[0] == 'best'


def test_fast_reuse_is_stronger_without_falsifying_timing_estimates():
    loaded = candidate('loaded', 90, True)
    loaded['speed'] = 5  # 80 s generation, compared with a cold model's 60 s load+generation
    cold = candidate('cold', 100)
    cold['load'] = 40
    assert seconds_to_answer(loaded) == 80 and seconds_to_answer(cold) == 60
    assert fast_preference_seconds(loaded) == 40
    assert pick('fast', [cold, loaded])[0] == 'loaded'
    loaded['fast_loaded_factor'] = 1
    assert pick('fast', [cold, loaded])[0] == 'cold'
    loaded['fast_loaded_factor'] = 2
    loaded['speed'] = 1  # resident preference cannot overcome arbitrarily slow generation
    assert pick('fast', [cold, loaded])[0] == 'cold'
    loaded['warm'] = False  # resident but all slots busy: there is no reload cost
    assert seconds_to_answer(loaded) == 400


@pytest.mark.parametrize('bonus', [-1, 101, True, '25', float('nan'), float('inf')])
def test_invalid_loaded_bonus_is_rejected(bonus):
    with pytest.raises(ValueError, match='Loaded-model bonus'):
        check_role('code', {'models': {'local': 1}, 'loaded_bonus': bonus}, {'local'})


@pytest.mark.parametrize('bonus', [-1, 501, True, '100', float('nan'), float('inf')])
def test_invalid_fast_loaded_bonus_is_rejected(bonus):
    with pytest.raises(ValueError, match='Fast loaded-model bonus'):
        check_role('code', {'models': {'local': 1}, 'fast_loaded_bonus': bonus}, {'local'})


def test_default_replacement_protects_active_held_locked_and_loading_models():
    models = Models(':memory:')
    old_settings = model('old.gguf', ['GPU0'])
    models.put('old', old_settings)
    runner = Runner(Config(gpus=('GPU0',)), None, models=models)
    assert runner.config.model_idle_minutes == 0
    runner.seen = {'cards':{0:{'total':16000, 'used':0}}, 'pids':{}}
    old = Server('old', ('GPU0',), old_settings, ready=True, mib=[13000])
    runner.servers['old'] = old
    assert runner.make_room('GPU0', 14000, []) == [old]
    ticket = Ticket('old', 'user', 'here', 'normal', server=old)
    runner.running.append(ticket)
    assert runner.make_room('GPU0', 14000, []) is None
    runner.release(ticket)
    assert runner.make_room('GPU0', 14000, []) == [old]  # no cooldown after finishing
    runner.holds['old', 'voice'] = time.monotonic()+60
    assert runner.make_room('GPU0', 14000, []) is None
    runner.holds.clear()
    models.set_locked('old', True)
    assert runner.make_room('GPU0', 14000, []) is None
    models.set_locked('old', False)
    old.ready = False
    assert runner.make_room('GPU0', 14000, []) is None
    models.db.close()


def test_last_model_stays_resident_and_is_replaced_on_demand_without_idle_wait(fake_llama_server, fake_cards):
    # Use Config's production default rather than gateway's test-specific override.
    models = {name:model(name+'.gguf', ['GPU0'], mib=13000) for name in ('first', 'next')}
    with gateway(fake_llama_server, models, nvidia_smi=fake_cards[0], model_idle_minutes=Config().model_idle_minutes, max_wait=5) as client:
        first = ask(client, 'first').json()
        assert ask(client, 'first').json()['pid'] == first['pid']
        assert alive(first['pid'])  # retained for reuse, no automatic unload
        next_answer = ask(client, 'next')
        assert next_answer.status_code == 200
        assert not alive(first['pid']) and alive(next_answer.json()['pid'])


def test_role_residency_bonus_selects_capable_loaded_model_and_preserves_explicit_choice(fake_llama_server):
    models = {name:model(name+'.gguf', 1) for name in ('best', 'middle', 'loaded', 'weak')}
    with gateway(fake_llama_server, models) as client:
        owner = add_user(client, 'owner', 'admin')
        weights = {'best':100, 'middle':95, 'loaded':90, 'weak':10}
        assert client.post('/loadcoach/roles/code', headers=owner, json={'models':weights}).status_code == 201
        assert client.post('/v1/chat/completions', headers=owner, json={'model':'loaded'}).status_code == 200
        preview = client.get('/loadcoach/pick?model=code', headers=owner).json()
        assert preview['picked'] == 'loaded' and '25%' in preview['because']
        chosen = next(row for row in preview['models'] if row['name']=='loaded')
        assert chosen['resident'] and chosen['loaded_factor'] == 1.25 and chosen['routing_score'] > preview['models'][0]['score']
        routed = client.post('/v1/chat/completions', headers=owner, json={'model':'code'})
        assert routed.headers['x-loadcoach-picked'] == 'loaded'
        assert client.get('/loadcoach/pick?model=code:best', headers=owner).json()['picked'] == 'best'
        explicit = client.post('/v1/chat/completions', headers=owner, json={'model':'best'})
        assert explicit.status_code == 200 and 'x-loadcoach-role' not in explicit.headers
        assert client.post('/loadcoach/roles/code', headers=owner, json={'models':weights, 'loaded_bonus':0}).status_code == 200
        assert client.get('/loadcoach/pick?model=code', headers=owner).json()['picked'] == 'best'
