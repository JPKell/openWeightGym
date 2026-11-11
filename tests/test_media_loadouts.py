"""Mixed LLM/media definitions and reviewed admission using metadata and fake ready servers."""

import asyncio
from dataclasses import replace

import pytest

from engines import engine_for
from loadcoach.loadouts import Conflict, Loadouts
from loadcoach.media_setup import profile_key
from loadcoach.runner import Server, Ticket
from test_loadouts import rig, chosen
from test_model_setup import SUPPORTED
from test_weightroom import gguf


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    library, runner, sets = rig(tmp_path)
    monkeypatch.setattr('loadcoach.runner.memory_snapshot', lambda: {'total': 65536, 'available': 60000,
                         'service_available': 60000, 'service_limit': 65536})
    for engine in ('sdcpp', 'audiocpp'):
        monkeypatch.setattr(engine_for(engine), 'describe', lambda config: {'available': True, 'why': ''})
    image = {'engine': 'sdcpp', 'flags': {'model': gguf(tmp_path / 'image.gguf'), 'ctx-size': 0}, 'gpus': 0,
             'sdcpp': {'family': 'sdxl', 'components': {}, 'host_ram_mib': 2048, 'limits': {'max_references': 1}}}
    audio = {'engine': 'audiocpp', 'flags': {'model': gguf(tmp_path / 'audio.gguf'), 'ctx-size': 0}, 'gpus': 0,
             'audiocpp': {'family': 'whisper', 'kind': 'transcription', 'path': str(tmp_path / 'audio.gguf'), 'host_ram_mib': 1024}}
    library.put('image', image); library.put('audio', audio)
    return library, runner, sets


def fixed(library, name):
    return {'model': name, 'cards': [], 'profile': profile_key(library, name)}


def test_mixed_definitions_reopen_and_load_at_exact_profiles_with_guarded_undo(mixed):
    library, runner, sets = mixed
    saved = sets.save('Mixed', [chosen(), fixed(library, 'image'), fixed(library, 'audio')])
    assert 'context' not in saved['models'][1] and saved['models'][1]['engine'] == 'sdcpp'
    assert Loadouts(library, runner).get('Mixed') == saved
    report = sets.preview('Mixed', SUPPORTED)
    assert report['can_apply'] and report['can_request'], report
    assert report['models'][1]['placement']['context'] is None
    assert library.get('image')['flags']['ctx-size'] == 0
    async def run():
        async def acquire(name, settings):
            server = runner.servers[name] = Server(name, (), settings, ready=True)
            ticket = Ticket(name, 'owner', 'here', 'normal', server=server)
            runner.running.append(ticket)
            return ticket
        lease = await sets.request(report, 'owner', 'owner-id', acquire)
        assert len(runner.loadout_leases[lease]['models']) == 3
        assert sets.renew(lease, 'owner-id')['state'] == 'ready'
        sets.release(lease, 'owner-id')
        undo = sets.preview('Mixed', SUPPORTED, undo=True)
        assert undo['can_apply']
        sets.apply(undo)
    asyncio.run(run())


def test_fixed_profile_change_or_file_revision_refuses_old_selection(mixed):
    library, _, sets = mixed
    entry = fixed(library, 'image')
    sets.save('Image', [entry])
    settings = library.stored('image')
    library.put('image', {**settings, 'sdcpp': {**settings['sdcpp'], 'defaults': {'width': 512}}})
    assert not sets.preview('Image', SUPPORTED)['can_apply']
    with pytest.raises(Conflict, match='profile changed'):
        sets.save('New', [entry])
    with pytest.raises(Conflict):
        sets.save('Tokens', [{'model': 'image', 'cards': [], 'context': 8192}])


def test_review_tokens_are_bound_to_node_and_resource_policy(mixed):
    library, runner, sets = mixed
    sets.save('Work', [chosen()])
    token = sets.preview('Work', SUPPORTED)['token']
    runner.config = replace(runner.config, machine_name='other-node')
    assert sets.preview('Work', SUPPORTED)['token'] != token
    token = sets.preview('Work', SUPPORTED)['token']
    runner.config = replace(runner.config, host_ram_limit_mib=10000)
    assert sets.preview('Work', SUPPORTED)['token'] != token


def test_changing_only_media_cards_does_not_invalidate_its_envelope(mixed):
    library, _, _ = mixed
    key = profile_key(library, 'image')
    library.put('image', {**library.stored('image'), 'gpus': ['GPU0']})
    assert profile_key(library, 'image') == key


@pytest.mark.parametrize('during_load', [True, False])
def test_file_changes_cannot_make_a_stale_profile_ready_or_renewable(mixed, during_load):
    library, runner, sets = mixed
    sets.save('Audio', [fixed(library, 'audio')])
    report = sets.preview('Audio', SUPPORTED)
    from pathlib import Path
    path = Path(library.get('audio')['flags']['model'])
    async def run():
        async def acquire(name, settings):
            server = runner.servers[name] = Server(name, (), settings, ready=True)
            if during_load:
                path.write_bytes(path.read_bytes() + b'changed')
            ticket = Ticket(name, 'owner', 'here', 'normal', server=server)
            runner.running.append(ticket)
            return ticket
        if during_load:
            with pytest.raises(Conflict):
                await sets.request(report, 'owner', 'owner-id', acquire)
            assert not runner.loadout_leases and not runner.running
        else:
            lease = await sets.request(report, 'owner', 'owner-id', acquire)
            path.write_bytes(path.read_bytes() + b'changed')
            with pytest.raises(Conflict, match='fixed profile changed'):
                sets.renew(lease, 'owner-id')
            sets.release(lease, 'owner-id')
    asyncio.run(run())
