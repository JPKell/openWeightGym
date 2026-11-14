"""New calls snapshot live Hermes choices while machine execution policy stays file-only."""
import asyncio
import httpx
import pytest
from loadcoach.setting_catalog import validate
from loadcoach.settings import with_changes, written
from voice.hermes import configuration


def test_new_hermes_calls_use_live_tools_and_search_without_mutating_active_calls(tmp_path):
    path = tmp_path / 'voice-gateway.toml'
    machine = {'model': 'small', 'python': '/owner/runtime', 'repository': '/owner/hermes', 'provider_key_file': '/owner/secret',
               'toolsets': ['search'], 'search_url': 'http://localhost:8888'}
    path.write_text(written({'hermes': machine}))
    settings, following = with_changes('voice-gateway', path)
    existing = configuration(settings)
    changed = {'hermes.toolsets': [], 'hermes.search_url': 'http://localhost:9999',
               'hermes.python': '/bad', 'hermes.provider_key_file': '/bad'}
    async def once():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=changed if r.method == 'GET' else {}))) as client:
            try: await asyncio.wait_for(following(client, 'http://authority', 'test-key'), .02)
            except TimeoutError: pass
    asyncio.run(once())
    new = configuration(settings)
    assert new['toolsets'] == [] and new['search_url'] == 'http://localhost:9999'
    assert existing == machine
    assert new['python'] == machine['python'] and new['provider_key_file'] == machine['provider_key_file']
    new['toolsets'].append('other')
    assert settings['hermes']['toolsets'] == []
    changed.clear(); asyncio.run(once())
    assert configuration(settings) == machine


@pytest.mark.parametrize('name,value', [('hermes.toolsets', ['search', 'search']), ('hermes.toolsets', ['a;cmd']),
    ('hermes.toolsets', [1]), ('hermes.search_url', 'file:///etc/passwd'),
    ('hermes.search_url', 'https://user:secret@example.com'), ('hermes.search_url', 'http://')])
def test_hermes_validation_does_not_echo_input(name, value):
    with pytest.raises(ValueError): validate('voice-gateway', name, value)


def test_unconfigured_hermes_still_reports_editable_choices_without_enabling_calls(tmp_path):
    import json
    path = tmp_path / 'voice-gateway.toml'
    path.write_text('')
    settings, following = with_changes('voice-gateway', path)
    assert not settings and configuration(settings) == {}
    reports = []
    changed = {'hermes.toolsets': [], 'hermes.search_url': 'http://localhost:9999'}
    def authority(request):
        if request.method == 'POST': reports.append(json.loads(request.content)['told'])
        return httpx.Response(200, json=changed if request.method == 'GET' else {})
    async def once():
        async with httpx.AsyncClient(transport=httpx.MockTransport(authority)) as client:
            try: await asyncio.wait_for(following(client, 'http://authority', 'test-key'), .02)
            except TimeoutError: pass
    asyncio.run(once())
    assert reports[-1]['hermes.toolsets']['value'] == []
    assert reports[-1]['hermes.search_url']['value'] == 'http://localhost:9999'
    assert configuration(settings) == {}
    changed.clear(); asyncio.run(once())
    assert reports[-1]['hermes.toolsets']['value'] == ['search']
    assert reports[-1]['hermes.search_url']['value'] == ''
    assert configuration(settings) == {}
