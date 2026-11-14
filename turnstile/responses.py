"""OpenAI Responses adaptation over ordinary chat admission and cancellation."""

import base64
import binascii
import json
import secrets
import time

import anyio
import httpx
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from turnstile.extractions import PRIVATE, read_json
from weightroom.extraction import MAX_BODY, Problem, image_upload, schema_checked


def chat_request(body):
    """Validate Responses input and build an ordinary chat request."""
    allowed = {'model', 'input', 'instructions', 'text', 'store', 'stream', 'max_output_tokens', 'temperature', 'top_p'}
    if body.keys() - allowed:
        raise Problem('Unsupported Responses parameter.', 400, 'unsupported_parameter')
    if body.get('store', False) is not False:
        raise Problem('Responses currently requires store:false.', 400, 'unsupported_parameter')
    if type(body.get('stream', False)) is not bool:
        raise Problem('stream must be a boolean.')
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
    sent = {'model': body['model'], 'messages': messages, 'stream': body.get('stream', False)}
    if sent['stream']:
        sent['stream_options'] = {'include_usage': True}
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


def final_body(request, output, *, identifier=None, created=None, model=None, usage=None,
               status='completed', error=None, incomplete=None):
    """Build the common final object for streamed and ordinary Responses."""
    usage = usage or {}
    return {'id': identifier or 'resp_' + secrets.token_hex(16), 'object': 'response',
            'created_at': int(time.time()) if created is None else created, 'status': status,
            'error': error, 'incomplete_details': incomplete, 'model': model or request['model'],
            'output': output, 'store': False, 'parallel_tool_calls': False, 'tools': [],
            'tool_choice': 'none', 'instructions': request.get('instructions'),
            'max_output_tokens': request.get('max_output_tokens'),
            'text': request.get('text', {'format': {'type': 'text'}}),
            'usage': {'input_tokens': usage.get('prompt_tokens', 0),
                      'output_tokens': usage.get('completion_tokens', 0),
                      'total_tokens': usage.get('total_tokens', 0),
                      'input_tokens_details': {'cached_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)},
                      'output_tokens_details': {'reasoning_tokens': (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)}}}


def completion_status(reason):
    """Map chat termination to Responses completion and truncation semantics."""
    return ('completed', None) if reason == 'stop' else ('incomplete', {
        'reason': 'max_output_tokens' if reason == 'length' else 'content_filter'})


def response_body(chat, request):
    """Convert chat text, reasoning and refusals using the shared object builder."""
    try:
        choice = chat['choices'][0]
        message = choice['message']
        if not isinstance(message, dict) or not isinstance(message.get('content') or '', str):
            raise Problem('Upstream returned an invalid chat message.', 502, 'invalid_upstream_response')
        if message.get('tool_calls'):
            raise Problem('Tool output is unsupported by this Responses subset.', 502, 'unsupported_output')
        status, incomplete = completion_status(choice.get('finish_reason'))
        output = []
        reasoning = message.get('reasoning_content')
        if reasoning:
            if not isinstance(reasoning, str):
                raise Problem('Upstream returned invalid reasoning.', 502, 'invalid_upstream_response')
            output.append({'id': 'rs_' + secrets.token_hex(16), 'type': 'reasoning',
                           'summary': [{'type': 'summary_text', 'text': reasoning}]})
        content = []
        if message.get('content') or not message.get('refusal'):
            content.append({'type': 'output_text', 'text': message.get('content') or '', 'annotations': [], 'logprobs': []})
        if message.get('refusal'):
            content.append({'type': 'refusal', 'refusal': message['refusal']})
        output.append({'id': 'msg_' + secrets.token_hex(16), 'type': 'message',
                       'role': 'assistant', 'status': status, 'content': content})
        return final_body(request, output, model=chat.get('model'), usage=chat.get('usage'),
                          status=status, incomplete=incomplete)
    except (KeyError, TypeError, IndexError, AttributeError):
        raise Problem('Upstream returned an invalid chat response.', 502, 'invalid_upstream_response') from None


async def chat_events(answer):
    """Parse bounded SSE data frames, including multiple data lines and usage-only chunks."""
    data, size = [], 0
    async for line in answer.aiter_lines():
        if line.startswith('data:'):
            value = line[5:].lstrip(' ')
            size += len(value)
            if size > MAX_BODY:
                raise Problem('Upstream event is too large.', 502, 'invalid_upstream_response')
            data.append(value)
        elif not line and data:
            raw = '\n'.join(data)
            data, size = [], 0
            if raw == '[DONE]':
                return
            try:
                chunk = json.loads(raw)
            except ValueError:
                raise Problem('Upstream returned invalid SSE JSON.', 502, 'invalid_upstream_response') from None
            if not isinstance(chunk, dict):
                raise Problem('Upstream returned invalid SSE data.', 502, 'invalid_upstream_response')
            if chunk.get('error'):
                error = chunk['error']
                raise Problem(error.get('message', 'Upstream stream failed.'), 502, error.get('code', 'upstream_error'))
            yield chunk
    if data:
        raise Problem('Upstream stream ended inside an event.', 502, 'upstream_disconnected')


class ResponseStream:
    """Translate ordered chat deltas into Responses events with stable item identities."""
    def __init__(self, request):
        """Initialize one response and its per-output event state."""
        self.request = request
        self.identifier, self.created = 'resp_' + secrets.token_hex(16), int(time.time())
        self.output, self.sequence = [], 0
        self.reasoning, self.message, self.parts = None, None, {}
        self.reasoning_done = False
        self.model, self.usage, self.finish = request['model'], {}, None

    def body(self, status='in_progress', error=None):
        """Build a snapshot with the same common fields as non-streaming output."""
        incomplete = completion_status(self.finish)[1] if status == 'incomplete' else None
        return final_body(self.request, self.output, identifier=self.identifier, created=self.created,
                          model=self.model, usage=self.usage, status=status, error=error, incomplete=incomplete)

    def event(self, type_, **fields):
        """Encode a snapshot immediately so later mutations cannot change sent events."""
        payload = {'type': type_, 'sequence_number': self.sequence, **fields}
        self.sequence += 1
        return 'event: ' + type_ + '\ndata: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'

    def end_reasoning(self):
        """Close a reasoning item before any subsequent message output starts."""
        if not self.reasoning or self.reasoning_done:
            return []
        self.reasoning_done = True
        item = self.reasoning
        fields = {'item_id': item['id'], 'output_index': self.output.index(item), 'summary_index': 0}
        return [self.event('response.reasoning_summary_text.done', **fields, text=item['summary'][0]['text']),
                self.event('response.reasoning_summary_part.done', **fields, part=item['summary'][0]),
                self.event('response.output_item.done', output_index=fields['output_index'], item=item)]

    def delta(self, kind, value):
        """Accumulate text/refusal/reasoning and emit its item, part and delta events."""
        if not isinstance(value, str):
            raise Problem('Upstream returned a non-text delta.', 502, 'invalid_upstream_response')
        events = []
        if kind == 'reasoning_content':
            if self.message:
                raise Problem('Upstream reasoning arrived after its message.', 502, 'invalid_upstream_response')
            if not self.reasoning:
                self.reasoning = {'id': 'rs_' + secrets.token_hex(16), 'type': 'reasoning', 'summary': []}
                self.output.append(self.reasoning)
                events.append(self.event('response.output_item.added', output_index=len(self.output)-1, item=self.reasoning))
                self.reasoning['summary'].append({'type': 'summary_text', 'text': ''})
                events.append(self.event('response.reasoning_summary_part.added', item_id=self.reasoning['id'],
                                         output_index=len(self.output)-1, summary_index=0, part=self.reasoning['summary'][0]))
            self.reasoning['summary'][0]['text'] += value
            events.append(self.event('response.reasoning_summary_text.delta', item_id=self.reasoning['id'],
                                     output_index=self.output.index(self.reasoning), summary_index=0, delta=value))
            return events
        events += self.end_reasoning()
        if not self.message:
            self.message = {'id': 'msg_' + secrets.token_hex(16), 'type': 'message', 'role': 'assistant',
                            'status': 'in_progress', 'content': []}
            self.output.append(self.message)
            events.append(self.event('response.output_item.added', output_index=len(self.output)-1, item=self.message))
        index = self.output.index(self.message)
        if kind not in self.parts:
            part = {'type': 'refusal', 'refusal': ''} if kind == 'refusal' else {
                'type': 'output_text', 'text': '', 'annotations': [], 'logprobs': []}
            self.parts[kind] = len(self.message['content'])
            self.message['content'].append(part)
            events.append(self.event('response.content_part.added', item_id=self.message['id'], output_index=index,
                                     content_index=self.parts[kind], part=part))
        part_index = self.parts[kind]
        self.message['content'][part_index]['refusal' if kind == 'refusal' else 'text'] += value
        fields = {'item_id': self.message['id'], 'output_index': index, 'content_index': part_index, 'delta': value}
        if kind != 'refusal':
            fields['logprobs'] = []
        events.append(self.event('response.' + ('refusal' if kind == 'refusal' else 'output_text') + '.delta', **fields))
        return events

    def chunk(self, chunk):
        """Consume one upstream chat chunk without manufacturing usage or token counts."""
        self.model = chunk.get('model') or self.model
        if chunk.get('usage'):
            self.usage = chunk['usage']
        events = []
        try:
            for choice in chunk.get('choices', []):
                if choice.get('index', 0) != 0:
                    continue
                delta = choice.get('delta') or {}
                if delta.get('tool_calls') or delta.get('function_call'):
                    raise Problem('Tool output is unsupported by this Responses subset.', 502, 'unsupported_output')
                for kind in ('reasoning_content', 'content', 'refusal'):
                    if delta.get(kind) is not None and (delta[kind] or kind != 'reasoning_content'):
                        events += self.delta(kind, delta[kind])
                if choice.get('finish_reason'):
                    self.finish = choice['finish_reason']
        except (KeyError, TypeError, AttributeError):
            raise Problem('Upstream returned an invalid chat chunk.', 502, 'invalid_upstream_response') from None
        return events

    def done(self):
        """Close output parts and emit a terminal response with final-chunk usage."""
        if self.finish is None:
            raise Problem('Upstream stream ended before completion.', 502, 'upstream_disconnected')
        events = self.end_reasoning()
        if not self.message:
            events += self.delta('content', '')
        status, _ = completion_status(self.finish)
        self.message['status'] = status
        for kind, index in self.parts.items():
            part = self.message['content'][index]
            fields = {'item_id': self.message['id'], 'output_index': self.output.index(self.message), 'content_index': index}
            events.append(self.event('response.' + ('refusal' if kind == 'refusal' else 'output_text') + '.done',
                                     **fields, **({'refusal': part['refusal']} if kind == 'refusal' else {'text': part['text'], 'logprobs': []})))
            events.append(self.event('response.content_part.done', **fields, part=part))
        events.append(self.event('response.output_item.done', output_index=self.output.index(self.message), item=self.message))
        events.append(self.event('response.' + status, response=self.body(status)))
        return events

    async def events(self, chunks, first):
        """Yield lifecycle events and turn mid-stream failures into a final failed response."""
        yield self.event('response.created', response=self.body())
        yield self.event('response.in_progress', response=self.body())
        try:
            for event in self.chunk(first):
                yield event
            async for chunk in chunks:
                for event in self.chunk(chunk):
                    yield event
            for event in self.done():
                yield event
        except (httpx.HTTPError, Problem) as error:
            details = {'message': str(error) if isinstance(error, Problem) else 'Model gateway disconnected.',
                       'code': getattr(error, 'code', 'upstream_disconnected')}
            if self.message:
                self.message['status'] = 'incomplete'
            yield self.event('error', **details, param=None)
            yield self.event('response.failed', response=self.body('failed', details))


class ClosingStream(StreamingResponse):
    """Own the upstream context even when the client disconnects before body iteration."""
    def __init__(self, content, upstream, **kwargs):
        """Attach an already-open upstream stream to its downstream response."""
        super().__init__(content, **kwargs)
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        """Always close the upstream so normal LoadCoach cancellation releases admission."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.__aexit__(None, None, None)


async def stream_response(service, body, sent, headers):
    """Open and validate upstream status before sending the first downstream byte."""
    context = service.door.stream('POST', '/v1/chat/completions', json=sent, headers=headers, timeout=None)
    answer = await context.__aenter__()
    handed_off = False
    try:
        forwarded = {key: value for key, value in answer.headers.items() if key.startswith(('x-loadcoach-', 'x-suite-'))}
        if not answer.is_success:
            await answer.aread()
            return preserved_response(answer, forwarded)
        chunks = chat_events(answer)
        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            raise Problem('Upstream returned an empty stream.', 502, 'invalid_upstream_response') from None
        stream = ResponseStream(body)
        response = ClosingStream(stream.events(chunks, first), context, media_type='text/event-stream',
                                 headers={**PRIVATE, **forwarded})
        handed_off = True
        return response
    finally:
        if not handed_off:
            with anyio.CancelScope(shield=True):
                await context.__aexit__(None, None, None)


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
            if sent['stream']:
                return await stream_response(service, body, sent, headers)
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
