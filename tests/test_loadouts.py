"""Named fits use metadata/fake processes only; no real model or device is exercised."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest
import httpx
from typer.testing import CliRunner

from db import Database
from db.transfer import copy_store
from loadcoach.config import Config
from loadcoach.groups import Group
from loadcoach.loadouts import Conflict, Loadouts
from loadcoach.models import Models
from loadcoach.runner import Refusal, Runner, Server, Ticket
from test_app import GROUPS, add_user, fake_llama_server, gateway, in_an_empty_folder, model  # noqa: F401
from test_model_setup import SUPPORTED, native  # noqa: F401
from test_postgresql import postgres  # noqa: F401
from test_weightroom import gguf


def rig(tmp_path, source=None, gpu=False):
    library = Models(source or tmp_path / 'models.db')
    for name in ('one', 'two', 'three', 'old'):
        path = tmp_path / (name + '.gguf')
        gguf(path, general__architecture='qwen', qwen__context_length=8192)
        library.put(name, model(str(path), ['GPU0'] if gpu else 0))
    runner = Runner(Config(gpus=('GPU0',), max_wait=2), None, models=library)
    runner.seen = {'cards': {0: {'total': 10000, 'used': 0}}, 'pids': {}}
    return library, runner, Loadouts(library, runner)


def chosen(name='one', context=1024, cards=None):
    return {'model': name, 'cards': cards or [], 'context': context}


def test_save_versions_and_stable_models_survive_reopen_and_rename(tmp_path):
    library, runner, sets = rig(tmp_path)
    first = sets.save('Work', [chosen()])
    with pytest.raises(Conflict):
        sets.save('Work', [chosen('two')])
    changed = sets.save('Work', [chosen('two')], version=first['version'])
    assert changed['version'] == 2
    library.rename('two', 'renamed')
    reopened = Loadouts(Models(tmp_path / 'models.db'), runner)
    assert reopened.listed()[0]['models'][0]['name'] == 'renamed'
    assert reopened.preview('Work', SUPPORTED)['can_request']
    library.remove('renamed')
    library.put('renamed', model(str(tmp_path / 'two.gguf'), 0))
    report = reopened.preview('Work', SUPPORTED)
    assert not report['can_apply'] and 'stable model ID' in ' '.join(report['errors'])
    with pytest.raises(Conflict):
        sets.delete('Work', 1)
    sets.delete('Work', 2)
    assert sets.listed() == []


@pytest.mark.parametrize('selection', [[], [chosen(), chosen()], [chosen(cards=['missing'])],
    [{**chosen(), 'context': True}], [{**chosen(), 'context': 0}], [{**chosen(), 'context': 2**31}],
    [{**chosen(), 'cards': ['GPU0', 'GPU0']}], [{**chosen(), 'code': 'forbidden'}]])
def test_invalid_sets_do_not_write_definitions_or_settings(tmp_path, selection):
    library, _, sets = rig(tmp_path)
    before = library.setup('one')
    with pytest.raises(ValueError):
        sets.save('Work', selection)
    assert not sets.listed() and library.setup('one') == before


def test_atomic_apply_undo_and_stale_second_member_roll_back_the_first(tmp_path):
    library, _, sets = rig(tmp_path)
    sets.save('Work', [chosen(), chosen('two', 4096)])
    report = sets.preview('Work', SUPPORTED)
    before = {name: library.setup(name) for name in ('one', 'two')}
    assert report['can_apply'] and report['can_request']
    library.put('two', model(str(tmp_path / 'two.gguf'), 0, threads=3))
    with pytest.raises(ValueError, match='model changed'):
        sets.apply(report)
    assert library.setup('one') == before['one'] and library.setup_undo('one') is None
    report = sets.preview('Work', SUPPORTED)
    before = {name: library.setup(name) for name in ('one', 'two')}
    sets.apply(report)
    assert library.get('one')['flags']['ctx-size'] == 1024 and library.get('two')['flags']['ctx-size'] == 4096
    undo = sets.preview('Work', SUPPORTED, undo=True)
    assert undo['can_apply'] and not undo['can_request']
    sets.apply(undo)
    assert {name: library.setup(name) for name in before} == before and sets.get('Work')['undo'] is None


def test_joint_fit_releases_snapshot_memory_without_mutating_live_servers(tmp_path):
    library, runner, sets = rig(tmp_path, gpu=True)
    for name in ('one', 'two', 'three'):
        library.measured(name, library.get(name), [2000])
    library.measured('old', library.get('old'), [5000])
    old = Server('old', ('GPU0',), library.get('old'), ready=True, mib=[5000],
                 process=SimpleNamespace(pid=42, returncode=None))
    runner.servers['old'] = old
    runner.seen = {'cards': {0: {'total': 10000, 'used': 5000}}, 'pids': {42: {0: 5000}}}
    seen = copy.deepcopy(runner.seen)
    sets.save('Work', [chosen(name, 2048, ['GPU0']) for name in ('one', 'two', 'three')])
    report = sets.preview('Work', SUPPORTED)
    assert report['can_request'] and report['joint']['leaves'] == ['old']
    assert runner.servers == {'old': old} and runner.seen == seen and not runner.holds
    busy = Ticket('old', 'someone', 'here', 'normal', server=old)
    runner.running.append(busy)
    refused = sets.preview('Work', SUPPORTED)
    assert not refused['can_request'] and runner.running == [busy] and runner.servers['old'] is old
    runner.running.clear()
    library.set_locked('old', True)
    assert not sets.preview('Work', SUPPORTED)['can_request']
    library.set_locked('old', False)
    runner.holds['old', 'voice'] = time.monotonic() + 60
    assert not sets.preview('Work', SUPPORTED)['can_request']


def test_unmeasured_members_on_one_card_cannot_replace_each_other(tmp_path):
    library, runner, sets = rig(tmp_path, gpu=True)
    sets.save('Work', [chosen(name, 2048, ['GPU0']) for name in ('one', 'two')])
    before = [library.setup(name) for name in ('one', 'two')]
    report = sets.preview('Work', SUPPORTED)
    assert report['can_apply'] and not report['can_request']
    assert 'Unmeasured placements reserve whole cards' in ' '.join(report['joint']['errors'])
    assert not runner.servers and before == [library.setup(name) for name in ('one', 'two')]


def test_set_holds_renew_expire_and_release_without_ending_manual_holds(tmp_path, monkeypatch):
    library, runner, sets = rig(tmp_path)
    async def run():
        models = {library.identity('one'): library.setup('one')}
        runner.servers['one'] = Server('one', (), library.get('one'), ready=True)
        runner.hold('one', 'owner', 1000)
        identity = sets.lease('Work', 'owner', 'stable-owner', models)
        assert runner.held('one') and runner.pinned(runner.servers['one'])
        with pytest.raises(LookupError):
            sets.release(identity, 'stranger')
        first = runner.loadout_leases[identity]['until']
        library.set_place('one', 4)  # display order cannot invalidate a runtime hold
        sets.renew(identity, 'stable-owner')
        assert runner.loadout_leases[identity]['until'] >= first
        sets.release(identity, 'stable-owner')
        assert ('one', 'owner') in runner.holds and runner.held('one')
        identity = sets.lease('Work', 'owner', 'stable-owner', models)
        clock = runner.loadout_leases[identity]['until'] + 1
        monkeypatch.setattr('loadcoach.runner.time.monotonic', lambda: clock)
        assert runner.held('one') and not runner.loadout_leases and runner.holds
    asyncio.run(run())


def test_runtime_hold_stays_with_the_loaded_process_after_model_removal_and_recreation(tmp_path):
    library, runner, sets = rig(tmp_path)
    async def run():
        original = Server('one', (), library.get('one'), ready=True, identity=library.identity('one'))
        runner.servers['one'] = original
        identity = sets.lease('Work', 'owner', 'owner-id', {library.identity('one'): library.setup('one')})
        library.remove('one')
        assert runner.pinned(original)
        library.put('one', model(str(tmp_path / 'one.gguf'), 0))
        replacement = Server('one', (), library.get('one'), ready=True)
        assert not runner.pinned(replacement) and runner.pinned(original)
        assert runner.serving('one') is None  # the old held process cannot answer for the new ID
        with pytest.raises(Conflict):
            sets.renew(identity, 'owner-id')
        sets.release(identity, 'owner-id')
        assert not runner.pinned(original)
    asyncio.run(run())


def test_failed_native_admission_releases_set_holds_but_retains_atomic_configuration_and_undo(tmp_path):
    library, runner, sets = rig(tmp_path)
    sets.save('Work', [chosen(), chosen('two', 4096)])
    report = sets.preview('Work', SUPPORTED)
    async def run():
        async def acquire(name, settings):
            if name == 'two':
                raise Refusal(502, 'Simulated native failure')
            server = runner.servers[name] = Server(name, (), settings, ready=True)
            request = Ticket(name, 'owner', 'here', 'normal', server=server)
            runner.running.append(request)
            return request
        with pytest.raises(Refusal, match='native failure'):
            await sets.request(report, 'owner', 'stable-owner', acquire)
        assert not runner.loadout_leases and not runner.running
        assert all(library.setup(one['name']) == one['candidate'] for one in report['models'])
        assert sets.preview('Work', SUPPORTED, undo=True)['can_apply']
    asyncio.run(run())


def test_cancelling_a_set_request_releases_its_preparing_hold(tmp_path):
    _, runner, sets = rig(tmp_path)
    sets.save('Work', [chosen()])
    report = sets.preview('Work', SUPPORTED)
    async def run():
        entered = asyncio.Event()
        async def acquire(name, settings):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(sets.request(report, 'owner', 'owner-id', acquire))
        await entered.wait()
        assert runner.held() and runner.loadout_leases
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not runner.loadout_leases and not runner.held()
        assert sets.preview('Work', SUPPORTED, undo=True)['can_apply']
    asyncio.run(run())


def test_postgresql_saved_sets_and_apply_undo_use_the_existing_model_store(tmp_path, postgres):  # noqa: F811
    library, _, sets = rig(tmp_path, postgres)
    sets.save('Work', [chosen(), chosen('two', 4096)])
    report = sets.preview('Work', SUPPORTED)
    assert report['can_apply'] and report['can_request']
    before = library.setup('one')
    sets.apply(report)
    assert sets.get('Work')['undo'] and library.setup('one') != before
    expected = sets.get('Work')
    library.db.close()
    target = Database.sqlite(tmp_path / 'transferred.db')
    assert copy_store(postgres, target)['state'] == 'verified'
    restored = Models(target)
    copied = Loadouts(restored, Runner(Config(gpus=('GPU0',)), None, models=restored))
    assert copied.get('Work') == expected
    copied.apply(copied.preview('Work', SUPPORTED, undo=True))
    assert restored.setup('one') == before


def test_api_cpu_set_request_renew_release_and_ordinary_hold_are_independent(native):  # noqa: F811
    with gateway(native, {'one': model(gguf('one.gguf'), 0), 'two': model(gguf('two.gguf'), 0)}) as caller:
        owner = add_user(caller, 'owner', 'admin')
        stranger = add_user(caller, 'other', 'admin', by=owner)
        plain = add_user(caller, 'plain', 'plain', by=owner)
        post = lambda **body: caller.post('/loadcoach/loadouts', headers=owner, json=body)
        assert caller.get('/loadcoach/loadouts', headers=plain).status_code == 403
        assert post(action='save', name='Work', models=[chosen(), chosen('two', 1024)]).status_code == 200
        preview = post(action='preview', name='Work').json()
        assert preview['can_request'], preview
        assert post(action='request', name='Work', token='stale').status_code == 409
        ready = post(action='request', name='Work', token=preview['token'], acknowledge=True)
        assert ready.status_code == 200 and ready.json()['seconds'] == 600, ready.text
        lease = ready.json()['lease']
        assert caller.post('/loadcoach/loadouts', headers=stranger, json={'action': 'release', 'lease': lease}).status_code == 404
        assert post(action='renew', lease=lease).status_code == 200
        assert caller.post('/loadcoach/hold', headers=owner, json={'model': 'one', 'seconds': 600}).status_code == 400
        assert caller.post('/loadcoach/hold', headers=owner, json={'model': 'one', 'seconds': 30}).status_code == 200
        assert post(action='release', lease=lease).status_code == 200
        status = caller.get('/loadcoach/status', headers=owner).json()
        assert len(status['holds']) == 1 and status['holds'][0]['model'] == 'one' and 'loadout' not in status['holds'][0]
        models = caller.get('/loadcoach/models', headers=owner).json()['models']
        assert all(one['loaded'] == 'ready' and one['cards'] == [] and one['context_loaded'] == 1024 for one in models)


def test_cli_commands_preserve_review_tokens_versions_and_explicit_cpu(tmp_path, monkeypatch):
    from loadcoach.cli import app
    calls = []
    def respond(request):
        calls.append((request.method, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={'token': 'reviewed', 'accepted': True})
    monkeypatch.setattr('loadcoach.cli.running', lambda _: httpx.Client(transport=httpx.MockTransport(respond), base_url='http://local'))
    selection = tmp_path / 'set.json'
    selection.write_text(json.dumps([chosen()]))
    ask = lambda *args: CliRunner().invoke(app, ['loadouts', *args], obj=lambda: (Config(), None, None))
    for command in ('list', 'save', 'preview', 'apply', 'request', 'renew', 'release', 'delete'):
        assert ask(command, '--help').exit_code == 0
    assert ask('save', 'Work', str(selection), '--version', '2').exit_code == 0
    assert calls[-1][1] == {'action': 'save', 'name': 'Work', 'models': [chosen()], 'version': 2}
    assert ask('preview', 'Work', '--undo').exit_code == 0 and calls[-1][1]['undo'] is True
    assert ask('request', 'Work', 'reviewed', '--acknowledge').exit_code == 0
    assert calls[-1][1]['token'] == 'reviewed' and calls[-1][1]['acknowledge'] is True
    assert ask('apply', 'Work', 'reviewed', '--undo', '--acknowledge').exit_code == 0
    for action in ('renew', 'release'):
        assert ask(action, 'lease-id').exit_code == 0 and calls[-1][1]['lease'] == 'lease-id'
    assert ask('delete', 'Work', '2').exit_code == 0 and calls[-1][1]['version'] == 2


def test_requesting_a_set_preserves_the_callers_model_access(native):  # noqa: F811
    groups = {**GROUPS, 'narrow': Group(can=frozenset({'load', 'manage-models'}), models=('one',))}
    with gateway(native, {'one': model(gguf('one.gguf'), 0), 'two': model(gguf('two.gguf'), 0)}, groups=groups) as caller:
        owner = add_user(caller, 'owner', 'admin')
        restricted = add_user(caller, 'restricted', 'narrow', by=owner)
        body = {'action': 'save', 'name': 'Work', 'models': [chosen(), chosen('two')]}
        assert caller.post('/loadcoach/loadouts', headers=owner, json=body).status_code == 200
        preview = caller.post('/loadcoach/loadouts', headers=restricted, json={'action': 'preview', 'name': 'Work'}).json()
        refused = caller.post('/loadcoach/loadouts', headers=restricted,
            json={'action': 'request', 'name': 'Work', 'token': preview['token'], 'acknowledge': True})
        assert refused.status_code == 403
        assert all(one['flags']['ctx-size'] == 2048 for one in caller.get('/loadcoach/models', headers=owner).json()['models'])
        assert not caller.get('/loadcoach/status', headers=owner).json()['holds']
