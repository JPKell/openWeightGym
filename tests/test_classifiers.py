"""Decision GGUF recognition, safe sources, typed contracts and inference accounting."""

import asyncio
import json
import struct
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

from engines.llama_cpp.gguf import facts, read_metadata
from engines.llama_cpp.adapter import LlamaCpp
from engines.llama_cpp.model import effective
from loadcoach.classifier import Invalid, validate, shim_request, shim_response, adapt_response, model_facts
from loadcoach.classifier_sources import check_source
from loadcoach.file_index import describe, listed_in
from loadcoach.files import model_files_in
from loadcoach.log import token_counts
from loadcoach.platform_services import PlatformServices
from test_app import fake_llama_server, gateway, model, add_user, in_an_empty_folder  # noqa: F401


def decision_file(path, kind='laya', template=True):
    """Write a real GGUF v3 metadata header without allocating tensors."""
    metadata = {'general.architecture': 'bert', 'general.name': 'Classifier fixture',
                'bert.decision.type': kind, 'bert.context_length': 8192,
                'bert.decision.temperature.choice': .75, 'bert.decision.max_head_tokens': 512}
    if template:
        metadata['tokenizer.chat_template.systemone'] = '{{ state }} {{ options }}'
    def text(value):
        raw = value.encode()
        return struct.pack('<Q', len(raw)) + raw
    raw = b'GGUF' + struct.pack('<IQQ', 3, 0, len(metadata))
    for key, value in metadata.items():
        kind_ = 8 if isinstance(value, str) else 6 if isinstance(value, float) else 4
        raw += text(key) + struct.pack('<I', kind_) + (text(value) if kind_ == 8 else struct.pack('<f' if kind_ == 6 else '<I', value))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(raw)
    return str(path)


def native():
    return {'model': 'test', 'state': 'I was charged twice.', 'questions': {
        'route': {'type': 'choice', 'instructions': 'Choose a team.', 'criteria': {'billing': None, 'support': 'Other issues'}},
        'urgent': {'type': 'noul', 'instructions': 'Is this urgent?'},
        'rating': {'type': 'score', 'instructions': 'Urgency?', 'criteria': ['low', 'medium', 'high']}}}


def test_metadata_recognizes_classifier_anywhere_and_requires_systemone(tmp_path):
    path = decision_file(tmp_path / 'llm' / 'example.gguf')
    found = facts(read_metadata(path))
    assert found['what'] == 'classifier' and found['decision_type'] == 'laya'
    assert found['decision_temperatures'] == {'choice': .75}
    settings = {'flags': {'model': path, 'ctx-size': 8192}}
    caps = LlamaCpp().capabilities(settings)
    assert caps['classifier'] == 'laya' and not any(caps[k] for k in ('chat', 'embeddings', 'raw_completion', 'streaming'))
    flags = effective(settings, {'jinja': True, 'temp': .3, 'batch-size': 256, 'ubatch-size': 128})['flags']
    assert flags['ubatch-size'] == flags['batch-size'] == 8192 and 'jinja' not in flags
    decision_file(path, template=False)
    assert not model_facts(settings)  # cached metadata invalidates when the same path changes


def test_nested_sources_are_inert_and_refuse_custom_code(tmp_path):
    root = tmp_path / 'classifier'
    decision_file(root / 'repo' / 'model.gguf')
    config = root / 'repo' / 'config.json'
    config.write_text('{"model_type":"qwen3"}')
    (config.parent / 'head.safetensors').touch()
    (config.parent / 'custom.py').write_text('raise RuntimeError("never run")')
    listed = model_files_in(str(root))
    assert config in listed and all(file.suffix != '.py' for file in listed)
    assert listed_in(root, config) and describe(config)['conversion'] == 'refused'
    (config.parent / 'custom.py').unlink()
    config.write_text('{"auto_map":{"AutoModel":"custom.Model"}}')
    with pytest.raises(ValueError, match='remote code'):
        check_source(config.parent)
    config.write_text('{}')
    assert describe(config)['conversion'] == 'available'


@pytest.mark.parametrize('change', [
    lambda b: b.update(state=None), lambda b: b.update(questions={}),
    lambda b: b.update(questions={str(i): b['questions']['urgent'] for i in range(65)}),
    lambda b: b['questions']['route'].update(criteria={'one': None}),
    lambda b: b['questions']['rating'].update(criteria=['one']),
    lambda b: b['questions']['urgent'].update(criteria={'maybe': None}),
    lambda b: b.update(stream=True), lambda b: b.update(state='a' * 65536),
    lambda b: b.update(images=['https://example.com/private.png']),
])
def test_invalid_native_requests_are_rejected(change):
    body = native()
    change(body)
    with pytest.raises(Invalid):
        validate(body)


def test_native_messages_and_per_model_choices():
    body = native()
    del body['state']
    body['messages'] = [{'role': 'user', 'content': 'hello'}]
    assert validate(body)['state'] == {'messages': body['messages']}
    with pytest.raises(Invalid, match='2–2'):
        validate({'model': 'a', 'state': 'a', 'questions': {'q': {'type': 'choice', 'instructions': 'q', 'criteria': dict.fromkeys('abc')}}}, 2)


def test_modernbert_uses_its_native_fragment_prefix_without_rejected_metadata(monkeypatch):
    monkeypatch.setattr('loadcoach.classifier.model_facts', lambda _: {'decision_type': 'laya', 'architecture': 'modern-bert',
        'decision_tokenizer_pre': 'mmbert', 'decision_metaspace_prepend': 'always'})
    settings = {'flags': {'model': 'julia.gguf', 'ctx-size': 8192}}
    flags = effective(settings, {})['flags']
    assert flags['override-kv'] == ['tokenizer.ggml.metaspace.prepend_scheme=str:never']
    assert settings['flags'] == {'model': 'julia.gguf', 'ctx-size': 8192}


def test_pointer_batches_survive_llama_embedding_startup_clamping(monkeypatch):
    """Pointer heads exceeding 512 tokens need matching logical and physical batches."""
    monkeypatch.setattr('loadcoach.classifier.model_facts', lambda _: {'decision_type': 'kev'})
    flags = effective({'flags': {'model': 'kev.gguf', 'ctx-size': 8192}}, {})['flags']
    assert flags['batch-size'] == flags['ubatch-size'] == 8192


def test_shim_preserves_bounds_distributions_zero_tokens_and_streaming():
    source = {'model': 'test', 'messages': [{'role': 'user', 'content': 'hello'}], 'response_format': {
        'type': 'json_schema', 'json_schema': {'name': 'test', 'schema': {'type': 'object', 'properties': {
            'route': {'type': 'string', 'enum': ['billing', 'support']}, 'urgent': {'type': 'boolean'},
            'rating': {'type': 'integer', 'minimum': 1, 'maximum': 3}}}}}}
    sent, mappings = shim_request(source)
    answer = {'answers': {'route': {'choice': 'billing', 'probabilities': {'billing': .9, 'support': .1}},
        'urgent': {'noul': .7}, 'rating': {'score': 1.6, 'probabilities': {'0': .1, '1': .2, '2': .7}}}, 'usage': {'input_tokens': 99, 'output_tokens': 0}}
    result = shim_response(answer, sent, mappings)
    assert json.loads(result['choices'][0]['message']['content']) == {'route': 'billing', 'urgent': True, 'rating': 3}
    assert result['usage'] == {'prompt_tokens': 99, 'completion_tokens': 0, 'total_tokens': 99}
    assert len(result['choices'][0]['logprobs']['content']) == 3
    stream = asyncio.run(adapt_response(JSONResponse(answer), sent, mappings, True))
    assert b'chat.completion.chunk' in stream.body and stream.body.endswith(b'data: [DONE]\n\n')
    assert token_counts(json.dumps(answer).encode()) == {'prompt_tokens': 99, 'answer_tokens': 0}


def test_classifier_service_policy_covers_native_page_files_and_shim_models():
    policy = PlatformServices(classifiers=False)
    for path in ('/chat/classifier', '/chat/classifier/api/run', '/v1/systemone', '/v1/classifier', '/owg/classifier-tool'):
        assert not policy.path_enabled(path, 'POST')
    assert not policy.kind_enabled('classifier')
    assert not policy.model_enabled({'capabilities': {'classifier': 'kev'}})
    assert policy.model_enabled({'capabilities': {'chat': True}})


def test_classifier_discovery_merges_tags_and_pipeline_with_stable_private_paging():
    """OR discovery keeps ordering, deduplicates matches and binds cursors to credentials."""
    import httpx
    from loadcoach.hub import search_page, Refused
    seen = []
    def respond(request):
        seen.append(dict(request.url.params))
        offset = int(request.url.params.get('cursor', '0'))
        rows = [{'id': f'example/model-{i:02}', 'downloads': 100 - i, 'likes': i, 'createdAt': '2026-01-01'}
                for i in range(offset, min(35, offset + 20))]
        headers = {'link': '<https://huggingface.co/api/models?cursor=20>; rel="next"'} if offset == 0 else {}
        return httpx.Response(200, json=rows, headers=headers)
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            first = await search_page(http, 'example', 'owner-token', kind='classifier')
            second = await search_page(http, 'example', 'owner-token', first['next_cursor'], kind='classifier')
            again = await search_page(http, 'example', 'owner-token', first['next_cursor'], kind='classifier')
            assert len(first['found']) == 20 and len(second['found']) == 15 and again['found'] == second['found']
            assert not {row['repository'] for row in first['found']} & {row['repository'] for row in second['found']}
            with pytest.raises(Refused):
                await search_page(http, 'example', 'different-token', first['next_cursor'], kind='classifier')
    asyncio.run(check())
    assert {query.get('filter') for query in seen} >= {'decision-model', 'system-one', 'typed-decisions', 'jev'}
    assert any(query.get('pipeline_tag') == 'text-classification' for query in seen)


def test_ready_badge_reads_large_complete_headers_without_accepting_truncated_text(tmp_path):
    """Native tokenizer metadata can exceed 8 MiB; partial templates cannot earn readiness."""
    import httpx
    from loadcoach.hub import decision_header
    path = Path(decision_file(str(tmp_path / 'large.gguf')))
    raw = bytearray(path.read_bytes())
    struct.pack_into('<Q', raw, 16, struct.unpack_from('<Q', raw, 16)[0] + 1)
    name, padding = b'general.description', b'x' * (9 * 2**20)
    raw += struct.pack('<Q', len(name)) + name + struct.pack('<I', 8) + struct.pack('<Q', len(padding)) + padding
    path.write_bytes(raw[:-1])
    with pytest.raises(ValueError, match='middle'):
        read_metadata(str(path))
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(206, content=bytes(raw)))) as http:
            assert await decision_header(http, 'example/decision', 'large.gguf')
    asyncio.run(check())


def test_front_native_alias_shim_permissions_and_service_disable(fake_llama_server):
    path = decision_file('classifier/repo/decision.gguf', 'kev')
    with gateway(fake_llama_server, {'test': model(path, 0), 'text': model('text.gguf', 0)}) as client:
        owner = add_user(client, 'owner', 'admin')
        response = client.get('/v1/models', headers=owner)
        metadata = next(m for m in response.json()['data'] if m['id'] == 'test')
        assert metadata['type'] == 'classifier' and metadata['architecture']['output_modalities'] == ['decisions']
        for endpoint in ('/v1/systemone', '/v1/classifier'):
            answer = client.post(endpoint, headers=owner, json=native())
            assert answer.status_code == 200, answer.text
            assert answer.json()['usage'] == {'input_tokens': 99, 'output_tokens': 0}
        body = {'model': 'test', 'messages': [{'role': 'user', 'content': 'hello'}], 'response_format': {'type': 'json_schema',
            'json_schema': {'name': 'decision', 'schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}}}}
        answer = client.post('/v1/chat/completions', headers=owner, json=body)
        assert answer.status_code == 200, answer.text
        assert json.loads(answer.json()['choices'][0]['message']['content']) == {'ok': True}
        assert client.post('/v1/systemone', headers=owner, json={**native(), 'model': 'text'}).status_code == 501
        assert client.post('/v1/chat/completions', headers=owner, json={'model': 'test', 'messages': []}).status_code == 400
        client.front.state.routing.platform = lambda: PlatformServices(classifiers=False)
        assert client.post('/v1/systemone', headers=owner, json=native()).status_code == 403
        assert client.post('/v1/chat/completions', headers=owner, json=body).status_code == 403


def test_cloud_classifier_native_shim_local_first_access_and_accounting(fake_llama_server, tmp_path, monkeypatch):
    """A hosted mapping keeps caller restrictions and only receives explicit cold-local overflow."""
    import httpx
    from loadcoach.groups import Group
    from test_app import GROUPS, events
    path = decision_file('classifier/repo/local.gguf', 'kev')
    groups = {**GROUPS, 'restricted-models': Group(models=('different',), can=frozenset({'hosted', 'load'}))}
    with gateway(fake_llama_server, {'test': model(path, 0)}, groups=groups, keys=str(tmp_path / 'keys')) as client:
        owner = add_user(client, 'owner', 'admin')
        denied = add_user(client, 'limited', 'restricted-models', by=owner)
        result = client.post('/loadcoach/services/decisions', headers=owner,
            json={'url': 'https://provider.invalid/v1', 'models': {'cloud': {'theirs': 'vendor/model', 'systemone': True, 'in': 2}}})
        assert result.status_code == 201, result.text
        key_file = tmp_path / 'keys/decisions.key'
        key_file.parent.mkdir(exist_ok=True)
        key_file.write_text('provider-test-key')
        calls = []

        async def send(request, **kwargs):
            calls.append(request)
            payload = {'answers': {
                'route': {'type': 'choice', 'choice': 'billing', 'probabilities': {'billing': .9, 'support': .1}},
                'urgent': {'type': 'noul', 'noul': .8},
                'rating': {'type': 'score', 'score': 1, 'probabilities': {'0': .1, '1': .8, '2': .1}}},
                'usage': {'input_tokens': 100, 'output_tokens': 0}}
            class Body(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield json.dumps(payload).encode()
            return httpx.Response(200, request=request, headers={'content-type': 'application/json'}, stream=Body())
        monkeypatch.setattr(client.front.state.routing.hosted.client, 'send', send)
        body = {**native(), 'model': 'cloud'}
        assert client.post('/v1/classifier', headers=owner, json=body).status_code == 200
        assert calls[-1].url.path == '/v1/systemone'
        assert json.loads(calls[-1].content)['model'] == 'vendor/model'
        assert calls[-1].headers['authorization'] == 'Bearer provider-test-key'
        assert client.post('/v1/systemone', headers=denied, json=body).status_code in (403, 404)
        assert client.post('/v1/systemone', headers={**owner, 'x-loadcoach-sensitivity': 'sensitive'}, json=body).status_code == 403
        shim = {'model': 'cloud', 'messages': [{'role': 'user', 'content': 'Example'}], 'response_format': {
            'type': 'json_schema', 'json_schema': {'name': 'route', 'schema': {'type': 'object', 'properties': {
                'route': {'type': 'string', 'enum': ['billing', 'support']}, 'urgent': {'type': 'boolean'},
                'rating': {'type': 'integer', 'minimum': 1, 'maximum': 3}}}}}}
        result = client.post('/v1/chat/completions', headers=owner, json=shim)
        assert result.status_code == 200 and result.json()['usage']['completion_tokens'] == 0
        assert client.post('/v1/systemone', headers={**owner, 'x-loadcoach-offload': 'cloud'}, json=native()).headers.get('x-loadcoach-hosted') == 'cloud'
        # Once loaded, the local model answers even when overflow is explicitly permitted.
        assert client.post('/v1/systemone', headers=owner, json=native()).status_code == 200
        count = len(calls)
        assert client.post('/v1/systemone', headers={**owner, 'x-loadcoach-offload': 'cloud'}, json=native()).status_code == 200
        assert len(calls) == count
        hosted = [entry for entry in events(client, owner, kind='request') if entry.get('service') == 'decisions' and 'prompt_tokens' in entry]
        assert hosted and hosted[-1]['prompt_tokens'] == 100 and hosted[-1]['answer_tokens'] == 0
