"""Run the guide's exact example payloads through isolated real gateway and console routes."""

import json
from pathlib import Path
import re
import shlex

from test_app import fake_llama_server, gateway, add_user, model  # noqa: F401
from test_docs_tools import docs_gateway  # noqa: F401
from test_weightroom import desk  # noqa: F401


GUIDE = Path(__file__).resolve().parents[1] / 'docs/guide/api/examples.md'


def commands():
    """Parse the published shell quotes, keeping environment expansion explicit and local."""
    values = {'OWG_API': 'http://front:8442', 'OWG_CONSOLE': 'http://testserver',
              'OWG_KEY': 'example-key', 'OWG_MODEL': 'small'}
    result = []
    for block in re.findall(r'```sh\n(.*?)```', GUIDE.read_text(), re.S):
        args = shlex.split(block.replace('\\\n', ''))
        args = [re.sub(r'\$(OWG_[A-Z]+)', lambda match: values[match[1]], arg) for arg in args]
        url = next(arg for arg in args if arg.startswith('http'))
        data = json.loads(args[args.index('-d') + 1]) if '-d' in args else None
        params = [args[i + 1].split('=', 1) for i, arg in enumerate(args) if arg == '--data-urlencode']
        result.append((url, data, params))
    return result


def test_chat_and_responses_examples_use_normal_gateway_admission(fake_llama_server, tmp_path, monkeypatch):
    examples = commands()
    monkeypatch.chdir(tmp_path)
    with gateway(fake_llama_server, models={'small': model('small.gguf', **{'docs-examples': True})}) as caller:
        headers = add_user(caller, 'example-user', 'admin')
        chat = caller.post(examples[0][0], json=examples[0][1], headers=headers)
        assert chat.status_code == 200, chat.text
        assert isinstance(chat.json()['choices'][0]['message']['content'], str)
        response = caller.post(examples[1][0], json=examples[1][1], headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['object'] == 'response' and body['status'] == 'completed' and body['store'] is False
        assert any(part['type'] == 'output_text' for item in body['output'] if item['type'] == 'message'
                   for part in item['content'])
        assert caller.get('/v1/responses/' + body['id'], headers=headers).status_code == 404


def test_search_example_uses_the_same_visible_library(docs_gateway):
    server, users, _ = docs_gateway
    url, _, params = commands()[2]
    response = server.get(url, params=params, headers={'authorization': 'Bearer ' + users.new_key('jordan')})
    assert response.status_code == 200, response.text
    hits = response.json()['hits']
    assert 1 <= len(hits) <= 3
    assert all(set(('title', 'url', 'heading', 'snippet')) <= hit.keys() and '#' in hit['url'] for hit in hits)
