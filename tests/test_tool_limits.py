"""Administrator tool-turn limits use the existing control node settings contract."""

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from loadcoach.settings import said
from turnstile.app import load_settings
from turnstile.settings import ConsoleSettings


def test_live_tool_limits_validate_atomically_and_reset_to_file_defaults(tmp_path):
    log = SimpleNamespace(add=lambda *args, **kwargs: None)
    settings = ConsoleSettings(str(tmp_path/'front.db'), log, None)
    settings.changes.seen('turnstile', said({'response_max_tool_calls': 25, 'response_max_tool_seconds': 900.0}, {}, {}))
    user = SimpleNamespace(name='admin', can=lambda ability: ability == 'manage-settings')
    async def handle(request):
        return await settings.handle(request, user)
    with TestClient(Starlette(routes=[Route('/loadcoach/settings/turnstile', handle, methods=['POST'])])) as client:
        assert client.post('/loadcoach/settings/turnstile', json={'response_max_tool_calls': 3, 'response_max_tool_seconds': 5.5}).status_code == 200
        assert settings.changes.of('turnstile') == {'response_max_tool_calls': 3, 'response_max_tool_seconds': 5.5}
        for name, values in [('response_max_tool_calls', [0, 10001, 1.5, True]), ('response_max_tool_seconds', [0, 86401, True])]:
            for value in values:
                answer = client.post('/loadcoach/settings/turnstile', json={name: value})
                assert answer.status_code == 400
        assert settings.changes.of('turnstile') == {'response_max_tool_calls': 3, 'response_max_tool_seconds': 5.5}
        assert client.post('/loadcoach/settings/turnstile', json={'response_max_tool_calls': None, 'response_max_tool_seconds': None}).status_code == 200
        assert settings.changes.of('turnstile') == {}
    settings.changes.db.close()


@pytest.mark.parametrize('name,value', [('response_max_tool_calls', '0'), ('response_max_tool_calls', '10001'),
                                      ('response_max_tool_calls', 'true'), ('response_max_tool_seconds', '0'),
                                      ('response_max_tool_seconds', 'nan'), ('response_max_tool_seconds', '86401')])
def test_invalid_file_limits_are_refused(tmp_path, name, value):
    path = tmp_path/'weightroom.toml'
    path.write_text('compute = "http://compute:8442"\n[turnstile]\n'+name+' = '+value+'\n')
    with pytest.raises(SystemExit, match=name):
        load_settings(path)
