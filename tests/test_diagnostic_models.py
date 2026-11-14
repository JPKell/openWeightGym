"""Both log explanation forms offer only the caller's local chat LLMs."""

import re

import httpx
import pytest

from loadcoach.log import Log
from test_weightroom import desk_with_machine, sign_in  # noqa: F401


@pytest.mark.parametrize('with_chat', [True, False])
def test_diagnostic_model_choices_exclude_media_embeddings_and_remote_routes(desk_with_machine, tmp_path, monkeypatch, with_chat):
    server, _, _, _, _ = desk_with_machine
    sign_in(server, 'jordan')
    number = Log(str(tmp_path / 'data/accounts.db')).add(
        'model', 'failed', 'jordan', model='chat', text={'output': 'worker stopped'})
    chat = [dict(id='chat', owned_by='loadcoach', capabilities={'chat': True}),
            dict(id='vision-chat', owned_by='loadcoach', capabilities={'chat': True, 'images': True})]
    other = [
        dict(id='images', owned_by='loadcoach', capabilities={'chat': False, 'image_generation': True}),
        dict(id='music', owned_by='loadcoach', capabilities={'chat': False, 'audio': 'music'}),
        dict(id='speech', owned_by='loadcoach', capabilities={'chat': False, 'audio': 'speech'}),
        dict(id='classifier', owned_by='loadcoach', capabilities={'chat': False, 'classifier': 'kev'}),
        dict(id='embed', owned_by='loadcoach', embedding=True, capabilities={'chat': True, 'embeddings': True}),
        dict(id='hosted', owned_by='hosted', capabilities={'chat': True}),
        dict(id='role', owned_by='role', capabilities={'chat': True}),
        dict(id='unknown', owned_by='loadcoach'),
    ]
    request = httpx.AsyncClient.request
    async def catalog(client, method, url, *args, **kwargs):
        if httpx.URL(url).path == '/v1/models':
            return httpx.Response(200, json={'data': [*(chat if with_chat else []), *other]})
        return await request(client, method, url, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, 'request', catalog)
    for path in ('/output?source=loadcoach', f'/log/{number}'):
        response = server.get(path)
        assert response.status_code == 200
        if with_chat:
            select = re.search(r'<select\b[^>]*id="ask-model"[^>]*>(.*?)</select>', response.text, re.S)
            assert select is not None
            assert re.findall(r'<option>(.*?)</option>', select[1]) == ['chat', 'vision-chat']
        else:
            assert 'id="ask-model"' not in response.text
            assert 'There is no model on compute that you may ask.' in response.text
