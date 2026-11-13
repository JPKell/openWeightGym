"""Extraction contracts, durable ownership, corrections, exports and failure boundaries."""

import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import time

import httpx
from PIL import Image
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.extractions import ExtractionService
from turnstile.responses import chat_request, response_body
from weightroom.extraction import (Problem, SCHEMAS, image_upload, specification, schema_checked,
                                  render_output, request_body, validate_output, warnings_for, MARKDOWN)


def raster():
    out = io.BytesIO()
    Image.new('RGB', (80, 120), 'white').save(out, 'PNG')
    return out.getvalue()


def receipt():
    return {**dict.fromkeys(SCHEMAS['receipt']['properties']), 'merchant': 'Cedar Market', 'date': '2026-11-13',
            'currency': 'CAD', 'subtotal': '22.40', 'tax': '1.12', 'total': '23.52',
            'items': [{'description': 'Coffee', 'quantity': '2', 'unit_price': '4.50', 'amount': '9.00'},
                      {'description': 'Tea and pastry', 'quantity': '1', 'unit_price': '13.40', 'amount': '13.40'}]}


@pytest.fixture
def extraction(tmp_path):
    users = Users(str(tmp_path / 'accounts.db'), {'members': Group(can=frozenset({'load', 'sign-in'}))})
    for name in ('alice', 'bob'):
        users.add(name, 'members')
    keys = {name: users.new_key(name) for name in ('alice', 'bob')}
    calls = []
    async def model(request):
        if request.url.path == '/loadcoach/me':
            return httpx.Response(200, json={'chat': {'models': {name: {'vision': True, 'json_schema': True} for name in ('vision', 'slow', 'broken', 'nontext', 'length', 'refusal')}}})
        asked = json.loads(request.content); calls.append((asked, dict(request.headers)))
        if asked['model'] == 'slow':
            await asyncio.sleep(20)
        task = asked['response_format']['json_schema']['name'].removeprefix('extraction_')
        data = {'receipt': receipt(), 'text': {'lines': ['Left', 'Right'], 'layout_rows': [['Left', 'Right']], 'markdown_lines': ['## Notes', '', 'Left', 'Right']},
                'table': {'columns': ['Item', 'Amount'], 'rows': [['Coffee', '9.00']]},
                'describe': {'description': 'A receipt.', 'objects': ['paper'], 'visible_text': 'Cedar Market'},
                'custom': {'merchant': 'Cedar Market'}}[task]
        message = {'content': 'broken' if asked['model'] == 'broken' else json.dumps(data)}
        if asked['model'] == 'nontext':
            message['content'] = [{'type': 'text', 'text': 'Unsupported provider shape'}]
        if asked['model'] == 'refusal':
            message['refusal'] = 'Refused'
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'length' if asked['model'] == 'length' else 'stop'}],
            'usage': {'prompt_tokens': 80, 'completion_tokens': 40, 'total_tokens': 120}},
            headers={'x-loadcoach-request-id': '42', 'x-loadcoach-model-id': 'model-one', 'x-loadcoach-build': 'test-build'})
    service = ExtractionService(str(tmp_path / 'accounts.db'), users.by_key)
    @asynccontextmanager
    async def lifespan(app):
        service.store.interrupted()
        service.door = httpx.AsyncClient(transport=httpx.MockTransport(model), base_url='http://inference')
        yield
        await service.close()
        await service.door.aclose()
    app = Starlette(routes=service.routes(users.by_key), lifespan=lifespan)
    with TestClient(app, headers={'authorization': 'Bearer ' + keys['alice']}) as client:
        yield client, service, users, keys, calls


def asset(client):
    response = client.post('/owg/extraction-assets', files={'image': ('receipt.png', raster(), 'image/png')})
    assert response.status_code == 201, response.text
    return response.json()['id']


def start(client, source, **settings):
    response = client.post('/owg/extractions', json={'asset_id': source, 'model': 'vision', 'task': 'receipt', 'output_format': 'json', **settings})
    assert response.status_code in (200, 202), response.text
    return response.json()


def finished(client, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get('/owg/extractions/' + identifier).json()
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(.02)
    raise AssertionError('Extraction did not finish')


def test_real_contract_builds_image_schema_request_and_keeps_provenance(extraction):
    client, _, _, _, calls = extraction
    source = asset(client)
    job = finished(client, start(client, source)['id'])
    assert job['status'] == 'completed'
    assert job['data']['total'] == '23.52'
    assert job['usage']['total_tokens'] == 120 and job['provenance']['request_id'] == '42'
    sent, headers = calls[-1]
    assert sent['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    assert sent['response_format']['json_schema']['strict'] is True
    assert 'tools' not in sent and headers['x-loadcoach-trace'] == job['id']
    assert client.get('/owg/extraction-assets/' + source).content == raster()
    assert client.get('/owg/extraction-assets/' + source).headers['cache-control'] == 'private, no-store'


def test_idempotency_prevents_duplicate_inference_and_rejects_conflict(extraction):
    client, _, _, _, calls = extraction
    spec = {'asset_id': asset(client), 'model': 'vision', 'task': 'receipt', 'output_format': 'json'}
    headers = {'idempotency-key': 'same-request'}
    first = client.post('/owg/extractions', json=spec, headers=headers).json()
    finished(client, first['id'])
    second = client.post('/owg/extractions', json=spec, headers=headers).json()
    assert first['id'] == second['id'] and len(calls) == 1
    assert client.post('/owg/extractions', json={**spec, 'task': 'text'}, headers=headers).status_code == 409


def test_corrections_keep_original_and_reject_stale_revision(extraction):
    client, _, _, _, _ = extraction
    job = finished(client, start(client, asset(client))['id'])
    data = {**job['data'], 'tax': None}
    path = '/owg/extractions/' + job['id']
    changed = client.patch(path + '/review', json={'revision': 1, 'data': data, 'reviewed': False})
    assert changed.status_code == 200 and changed.json()['status'] == 'needs_review'
    assert changed.json()['revision'] == 2
    assert client.patch(path + '/review', json={'revision': 1, 'data': data, 'reviewed': True}).status_code == 409
    assert client.get(path + '/export?format=json').json()['tax'] is None
    assert client.get(path + '/export?format=json&revision=original').json()['tax'] == '1.12'
    for format_, mime in [('plain', 'text/plain'), ('layout', 'text/plain'), ('markdown', 'text/markdown'), ('csv', 'text/csv')]:
        answer = client.get(path + '/export?format=' + format_)
        assert answer.status_code == 200 and answer.headers['content-type'].startswith(mime)
        assert 'Coffee' in answer.text
    assert client.get(path + '/export?format=html').status_code == 400


def test_ownership_survives_rename_and_prevents_recreated_account_access(extraction):
    client, service, users, keys, _ = extraction
    source = asset(client)
    job = finished(client, start(client, source)['id'])
    other = {'authorization': 'Bearer ' + keys['bob']}
    assert client.get('/owg/extractions/' + job['id'], headers=other).status_code == 404
    assert client.get('/owg/extraction-assets/' + source, headers=other).status_code == 404
    assert client.post('/owg/extractions', json={'asset_id': source, 'model': 'vision'}, headers=other).status_code == 404
    users.rename('alice', 'renamed')
    assert client.get('/owg/extractions/' + job['id']).status_code == 200
    users.remove('renamed'); users.add('alice', 'members')
    client.headers['authorization'] = 'Bearer ' + users.new_key('alice')
    assert client.get('/owg/extractions/' + job['id']).status_code == 404


@pytest.mark.parametrize('model,code', [('broken', 'invalid_structured_output'), ('nontext', 'invalid_structured_output'), ('length', 'incomplete_output'), ('refusal', 'model_refusal')])
def test_invalid_incomplete_and_refused_outputs_never_succeed(extraction, model, code):
    client, _, _, _, _ = extraction
    job = finished(client, start(client, asset(client), model=model)['id'])
    assert job['status'] == 'failed' and job['error']['code'] == code
    assert client.get('/owg/extractions/' + job['id'] + '/export').status_code == 409


def test_cancel_recovery_and_delete_never_resurrect_jobs(extraction):
    client, service, _, _, _ = extraction
    source = asset(client)
    job = start(client, source, model='slow')
    path = '/owg/extractions/' + job['id']
    assert client.delete(path).status_code == 409
    assert client.delete('/owg/extraction-assets/' + source).status_code == 409
    assert client.post(path + '/cancel').json()['status'] == 'cancelled'
    assert client.post(path + '/cancel').json()['status'] == 'cancelled'
    assert client.delete(path).status_code == 204
    assert client.get('/owg/extraction-assets/' + source).status_code == 404
    owner = 'restart-owner'
    stored = service.store.add_asset(owner, image_upload('test.png', raster()))
    abandoned, _ = service.store.create(owner, specification({'asset_id': stored['id'], 'model': 'vision'}), 'restart')
    service.store.interrupted()
    assert service.store.one(owner, abandoned['id'])['error']['code'] == 'interrupted'


def test_custom_schema_and_presets_are_validated_and_private(extraction):
    client, _, _, keys, _ = extraction
    schema = {'type': 'object', 'properties': {'merchant': {'type': ['string', 'null']}}, 'required': ['merchant'], 'additionalProperties': False}
    source = asset(client)
    job = finished(client, start(client, source, task='custom', schema=schema)['id'])
    assert job['data'] == {'merchant': 'Cedar Market'}
    preset = client.post('/owg/extraction-presets', json={'name': 'Merchant', 'spec': {'model': 'vision', 'task': 'custom', 'schema': schema}})
    assert preset.status_code == 201
    assert len(client.get('/owg/extraction-presets').json()['presets']) == 1
    assert client.get('/owg/extraction-presets', headers={'authorization': 'Bearer ' + keys['bob']}).json()['presets'] == []
    assert client.delete('/owg/extraction-presets/' + preset.json()['id']).status_code == 204


@pytest.mark.parametrize('data', [b'<svg/>', b'not an image', b''])
def test_upload_checks_bytes_not_filename(extraction, data):
    client, _, _, _, _ = extraction
    assert client.post('/owg/extraction-assets', files={'image': ('image.png', data, 'image/png')}).status_code in (400, 413)


@pytest.mark.parametrize('schema', [
    {'$ref': 'https://example.com/schema'}, {'type': 'array', 'items': {'type': 'string'}},
    {'type': 'object', 'properties': {'a': {'type': 'string'}}, 'required': [], 'additionalProperties': False},
    {'type': 'object', 'properties': {'a': {'type': 'string', 'pattern': '(a+)+$'}}, 'required': ['a'], 'additionalProperties': False},
])
def test_custom_schema_rejects_unsafe_or_unsupported_shapes(schema):
    with pytest.raises(Problem):
        schema_checked(schema)


def test_decimal_reconciliation_refunds_and_formula_safe_csv():
    spec = specification({'asset_id': 'a', 'model': 'vision', 'task': 'receipt'})
    data = receipt()
    assert warnings_for(spec, data) == []
    data['merchant'] = ' =HYPERLINK("bad")'
    assert "' =HYPERLINK" in render_output(spec, data, 'csv')
    data['tax'] = '1.13'
    assert not warnings_for(spec, data)  # one-cent rounding tolerance is explicit
    data['tax'] = '1.14'
    assert any(entry['code'] == 'totals_mismatch' for entry in warnings_for(spec, data))
    data = receipt(); data['total'] = '-23.52'
    validate_output(spec, data)  # refund values remain valid data even when review is needed
    data = receipt(); data['items'][0]['quantity'] = '3'
    assert any(entry['code'] == 'line_amount_mismatch' for entry in warnings_for(spec, data))


def test_responses_subset_translates_content_schema_and_usage():
    source = 'data:image/png;base64,' + base64.b64encode(raster()).decode()
    body = {'model': 'vision', 'store': False, 'input': [{'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'Read this.'}, {'type': 'input_image', 'image_url': source}]}],
        'text': {'format': {'type': 'json_schema', 'name': 'text', 'strict': True, 'schema': SCHEMAS['text']}}}
    sent = chat_request(body)
    assert sent['messages'][0]['content'][1]['image_url']['url'] == source
    assert sent['response_format']['json_schema']['schema'] == SCHEMAS['text']
    result = response_body({'model': 'vision', 'choices': [{'message': {'content': 'hello'}, 'finish_reason': 'stop'}],
                            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}, body)
    assert result['output'][0]['content'][0]['text'] == 'hello' and result['usage']['total_tokens'] == 5
    result = response_body({'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}],
                            'usage': {'prompt_tokens_details': None, 'completion_tokens_details': None}}, body)
    assert result['status'] == 'incomplete' and result['incomplete_details']['reason'] == 'max_output_tokens'
    for malformed in ({'choices': []}, {'choices': [{'message': {'content': ['invalid']}}]}, {'choices': [{'message': None}]}):
        with pytest.raises(Problem) as invalid:
            response_body(malformed, body)
        assert invalid.value.status == 502
    for extra in ({'stream': True}, {'store': True}, {'previous_response_id': 'resp_123'}, {'tools': []}):
        with pytest.raises(Problem):
            chat_request({**body, **extra})


def test_layout_rendering_is_bounded_and_markdown_never_loads_images_or_html():
    spec = specification({'asset_id': 'a', 'model': 'vision', 'task': 'text'})
    data = {'lines': ['Left', 'Right'], 'layout_rows': [['Left', 'Right'], ['A', 'B']], 'markdown_lines': ['## Heading']}
    assert render_output(spec, data, 'plain') == 'Left\nRight'
    assert render_output(spec, data, 'layout') == 'Left   Right\nA      B'
    assert render_output(spec, data, 'markdown') == '## Heading'
    html = MARKDOWN.render('<script>bad()</script>\n\n![x](https://example.com/x.png)\n\n[x](javascript:bad())')
    assert '<script>' not in html and '<img' not in html and 'href="javascript:' not in html
    with pytest.raises(Problem):
        validate_output(spec, {**data, 'layout_rows': [['x'] * 9]})


def test_nonfinite_custom_data_and_malformed_schema_are_rejected():
    schema = {'type': 'object', 'properties': {'a': {'type': 'number'}}, 'required': ['a'], 'additionalProperties': False}
    spec = specification({'asset_id': 'a', 'model': 'vision', 'task': 'custom', 'schema': schema})
    with pytest.raises(Problem, match='non-JSON'):
        validate_output(spec, {'a': float('nan')})
    with pytest.raises(Problem):
        schema_checked({**schema, 'required': None})


def test_role_context_estimate_does_not_count_base64_image_bytes_as_text():
    from starlette.requests import Request
    from turnstile.roles import needs_of
    request = Request({'type': 'http', 'headers': []})
    body = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'Read this'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'a' * 100000}}]}],
            'response_format': {'type': 'json_schema'}}
    needs = needs_of(request, body)
    assert needs['pictures'] and needs['json_schema']
    assert 2048 < needs['tokens'] < 2500 and needs['work']['prompt_tokens'] is None
    assert len(body['messages'][0]['content'][1]['image_url']['url']) > 100000


def test_caller_bound_tools_verify_signature_replay_owner_and_key_revocation(extraction, tmp_path):
    from loadcoach import statement
    from turnstile.app import Settings, build_app
    client, _, users, keys, _ = extraction
    job = finished(client, start(client, asset(client))['id'])
    key_file = tmp_path / 'machine.key'
    key_file.write_text('a private machine key only these two services know')
    gateway = build_app(Settings(data=str(tmp_path), machine_key_file=str(key_file)), users=users)
    path = '/owg/extraction-tools/extraction_status'
    body = json.dumps({'arguments': {'job_id': job['id']}, 'sensitivity': 'normal'}).encode()
    def signed(name='alice'):
        header, nonce = statement.make(key_file.read_text(), 'POST', path, body,
            caller=statement.caller_of(users.by_key(keys[name])), purpose='extraction-tool')
        return header, nonce
    with TestClient(gateway, base_url='http://front:8442') as front:
        header, nonce = signed()
        result = front.post(path, content=body, headers={statement.HEADER: header})
        assert result.status_code == 200, result.text
        assert result.headers[statement.PROOF] == statement.proof(key_file.read_text(), nonce)
        assert result.json()['id'] == job['id']
        assert front.post(path, content=body, headers={statement.HEADER: header}).status_code == 403
        other, _ = signed('bob')
        assert front.post(path, content=body, headers={statement.HEADER: other}).status_code == 404
        revoked, _ = signed()
        users.remove('alice')
        assert front.post(path, content=body, headers={statement.HEADER: revoked}).status_code == 403
