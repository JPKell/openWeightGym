"""Explicit stateless, non-streaming OpenAI Responses subset over normal chat admission."""

import base64
import binascii
import json
import secrets
import time

import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route

from turnstile.extractions import PRIVATE, read_json
from weightroom.extraction import MAX_BODY, Problem, image_upload, schema_checked


def chat_request(body):
    """Validate the stateless Responses subset and build an ordinary chat request."""
    allowed = {'model', 'input', 'instructions', 'text', 'store', 'stream', 'max_output_tokens', 'temperature', 'top_p'}
    if body.keys() - allowed:
        raise Problem('This stateless Responses adapter supports model, input, instructions, text.format, store:false, stream:false and sampling limits only.', 400, 'unsupported_parameter')
    if body.get('store', False) is not False or body.get('stream', False) is not False:
        raise Problem('Responses currently requires store:false and stream:false.', 400, 'unsupported_parameter')
    if not isinstance(body.get('model'), str) or not body['model']:
        raise Problem('Responses requires a model.')
    messages = []
    if 'instructions' in body:
        if not isinstance(body['instructions'], str):
            raise Problem('instructions must be a string.')
        messages.append({'role': 'system', 'content': body['instructions']})
    items = body.get('input')
    if isinstance(items, str):
        messages.append({'role': 'user', 'content': items})
    elif isinstance(items, list) and 1 <= len(items) <= 100:
        for item in items:
            if not isinstance(item, dict) or item.keys() - {'type', 'role', 'content'} or item.get('type', 'message') != 'message' or item.get('role') not in ('user', 'assistant', 'system', 'developer'):
                raise Problem('input must contain ordinary message items; tools and prior response IDs are unsupported.')
            content = item.get('content')
            if isinstance(content, str):
                messages.append({'role': item['role'], 'content': content})
                continue
            if not isinstance(content, list) or not 1 <= len(content) <= 20:
                raise Problem('Message content must be text or a bounded input content array.')
            converted = []
            for part in content:
                if not isinstance(part, dict):
                    raise Problem('Invalid input content.')
                if part.get('type') == 'input_text' and set(part) == {'type', 'text'} and isinstance(part['text'], str):
                    converted.append({'type': 'text', 'text': part['text']})
                elif part.get('type') == 'input_image' and not part.keys() - {'type', 'image_url', 'detail'}:
                    url, detail = part.get('image_url'), part.get('detail', 'auto')
                    if not isinstance(url, str) or not url.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,')) or detail not in ('auto', 'low', 'high'):
                        raise Problem('This Responses adapter accepts inline PNG, JPEG and WebP data URLs; remote images and file IDs are unsupported.')
                    try:
                        image_upload('input-image', base64.b64decode(url.split(',', 1)[1], validate=True))
                    except (ValueError, binascii.Error) as error:
                        raise Problem(str(error) if isinstance(error, Problem) else 'Invalid image data URL.') from None
                    converted.append({'type': 'image_url', 'image_url': {'url': url, 'detail': detail}})
                else:
                    raise Problem('Unsupported Responses input content type.')
            messages.append({'role': item['role'], 'content': converted})
    else:
        raise Problem('input must be a string or a nonempty message array.')
    sent = {'model': body['model'], 'messages': messages, 'stream': False}
    if 'max_output_tokens' in body:
        if type(body['max_output_tokens']) is not int or not 1 <= body['max_output_tokens'] <= 32768:
            raise Problem('max_output_tokens must be 1–32768.')
        sent['max_tokens'] = body['max_output_tokens']
    for key in ('temperature', 'top_p'):
        if key in body:
            if type(body[key]) not in (int, float) or not 0 <= body[key] <= (2 if key == 'temperature' else 1):
                raise Problem('Invalid ' + key + '.')
            sent[key] = body[key]
    if 'text' in body:
        text = body['text']
        if not isinstance(text, dict) or set(text) != {'format'} or not isinstance(text['format'], dict):
            raise Problem('Use text.format for Responses output formatting.')
        format_ = text['format']
        if format_ in ({'type': 'text'}, {'type': 'json_object'}):
            if format_['type'] == 'json_object':
                sent['response_format'] = format_
        elif format_.get('type') == 'json_schema' and not format_.keys() - {'type', 'name', 'schema', 'strict', 'description'}:
            if not isinstance(format_.get('name'), str) or not 1 <= len(format_['name']) <= 64 or type(format_.get('strict', False)) is not bool:
                raise Problem('A JSON schema needs name, schema and an optional strict boolean.')
            sent['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': format_['name'], 'strict': format_.get('strict', False), 'schema': schema_checked(format_.get('schema'))}}
        else:
            raise Problem('Unsupported Responses output format.')
    return sent


def response_body(chat, request):
    """Convert completed, incomplete or refused chat output into Responses shape."""
    try:
        choice = chat['choices'][0]
        message = choice['message']
        if not isinstance(message, dict) or not isinstance(message.get('content') or '', str):
            raise Problem('Upstream returned an invalid chat message.', 502, 'invalid_upstream_response')
        if message.get('tool_calls'):
            raise Problem('Tool output is unsupported by this Responses subset.', 502, 'unsupported_output')
        incomplete = choice.get('finish_reason') != 'stop'
        content = [{'type': 'refusal', 'refusal': message['refusal']}] if message.get('refusal') else [
            {'type': 'output_text', 'text': message.get('content') or '', 'annotations': [], 'logprobs': []}]
        usage = chat.get('usage') or {}
        return {'id': 'resp_' + secrets.token_hex(16), 'object': 'response', 'created_at': int(time.time()),
                'status': 'incomplete' if incomplete else 'completed', 'error': None,
                'incomplete_details': {'reason': 'max_output_tokens' if choice.get('finish_reason') == 'length' else 'content_filter'} if incomplete else None,
                'model': chat.get('model', request['model']), 'output': [{'id': 'msg_' + secrets.token_hex(16), 'type': 'message',
                    'role': 'assistant', 'status': 'incomplete' if incomplete else 'completed', 'content': content}],
                'store': False, 'parallel_tool_calls': False, 'tools': [], 'tool_choice': 'none',
                'instructions': request.get('instructions'), 'max_output_tokens': request.get('max_output_tokens'),
                'text': request.get('text', {'format': {'type': 'text'}}),
                'usage': {'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': usage.get('completion_tokens', 0),
                    'total_tokens': usage.get('total_tokens', 0), 'input_tokens_details': {'cached_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)},
                    'output_tokens_details': {'reasoning_tokens': (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)}}}
    except (KeyError, TypeError, IndexError, AttributeError):
        raise Problem('Upstream returned an invalid chat response.', 502, 'invalid_upstream_response') from None


def routes(caller, service):
    """Register explicit stateless Responses creation on the model gateway."""
    async def create(request):
        """Authenticate and execute Responses creation through normal chat admission."""
        authorization = request.headers.get('authorization', '')
        if not caller(authorization.removeprefix('Bearer ')):
            return JSONResponse({'error': {'message': 'Missing or wrong API key.'}}, 401, headers=PRIVATE)
        try:
            body = await read_json(request, MAX_BODY)
            sent = chat_request(body)
            headers = {key: value for key, value in request.headers.items()
                       if key == 'authorization' or key.startswith(('x-loadcoach-', 'x-suite-node'))}
            answer = await service.door.post('/v1/chat/completions', json=sent, headers=headers, timeout=None)
            forwarded = {key: value for key, value in answer.headers.items() if key.startswith(('x-loadcoach-', 'x-suite-'))}
            if not answer.is_success:
                return preserved_response(answer, forwarded)
            try:
                result = answer.json()
            except ValueError:
                raise Problem('Upstream returned invalid JSON.', 502, 'invalid_upstream_response') from None
            return JSONResponse(response_body(result, body), headers={**PRIVATE, **forwarded})
        except httpx.HTTPError:
            return JSONResponse({'error': {'message': 'Model gateway is unavailable.', 'code': 'gateway_unavailable'}}, 503, headers=PRIVATE)
        except Problem as error:
            return JSONResponse({'error': {'message': str(error), 'type': 'invalid_request_error', 'code': error.code}}, error.status, headers=PRIVATE)
    return [Route('/v1/responses', create, methods=['POST'])]


def preserved_response(answer, headers):
    """Retain ordinary upstream status, error body and routing headers."""
    from starlette.responses import Response
    return Response(answer.content, answer.status_code, headers={**PRIVATE, **headers}, media_type=answer.headers.get('content-type', 'application/json'))
