"""OpenAI Responses adaptation over ordinary chat admission and cancellation."""

import base64
import binascii
import json
import re
import secrets
import time
from urllib.parse import quote

import anyio
import httpx
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from turnstile.extractions import PRIVATE, read_json
from weightroom.extraction import MAX_BODY, Problem, image_upload, schema_checked
from weightroom.response_store import normalized_input, owner_of
from loadcoach.groups import LEVELS


FUNCTION_NAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def valid_function_name(value):
    """Bound names to the Responses function identifier vocabulary."""
    return isinstance(value, str) and FUNCTION_NAME.fullmatch(value) is not None


def function_tools(body):
    """Validate bounded function schemas and translate Responses tools and choices."""
    sent = {}
    tools = body.get('tools', [])
    if not isinstance(tools, list) or len(tools) > 128:
        raise Problem('tools must contain at most 128 function definitions.')
    names, definitions = set(), []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get('type') != 'function' or tool.keys() - {'type', 'name', 'description', 'parameters', 'strict'}:
            raise Problem('Only function tools are supported.', 400, 'unsupported_parameter')
        if not valid_function_name(tool.get('name')) or tool['name'] in names:
            raise Problem('Function names must be unique and contain 1–64 letters, digits, underscores or dashes.')
        names.add(tool['name'])
        if type(tool.get('strict', False)) is not bool or not isinstance(tool.get('description', ''), str):
            raise Problem('A function needs a string description and optional strict boolean.')
        parameters = tool.get('parameters', {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False})
        function = {'name': tool['name'], 'parameters': schema_checked(parameters, require_all=tool.get('strict', False))}
        for key in ('description', 'strict'):
            if key in tool:
                function[key] = tool[key]
        definitions.append({'type': 'function', 'function': function})
    if 'tools' in body:
        sent['tools'] = definitions
    if 'tool_choice' in body:
        choice = body['tool_choice']
        if isinstance(choice, str) and choice in ('auto', 'none', 'required'):
            sent['tool_choice'] = choice
        elif isinstance(choice, dict) and set(choice) == {'type', 'name'} and choice['type'] == 'function' and valid_function_name(choice['name']) and choice['name'] in names:
            sent['tool_choice'] = {'type': 'function', 'function': {'name': choice['name']}}
        else:
            raise Problem('tool_choice must be auto, none, required or a declared function name.')
    if 'parallel_tool_calls' in body:
        if type(body['parallel_tool_calls']) is not bool:
            raise Problem('parallel_tool_calls must be a boolean.')
        sent['parallel_tool_calls'] = body['parallel_tool_calls']
    return sent


def append_input(messages, item):
    """Validate and translate one Responses message or function item into chat history."""
    if not isinstance(item, dict):
        raise Problem('Input items must be objects.')
    if item.get('type') == 'function_call':
        if item.keys() - {'type', 'id', 'status', 'call_id', 'name', 'arguments'} or not isinstance(item.get('call_id'), str) or not 1 <= len(item['call_id']) <= 128 or not valid_function_name(item.get('name')) or not isinstance(item.get('arguments'), str):
            raise Problem('A function_call needs call_id, name and string arguments.')
        call = {'id': item['call_id'], 'type': 'function', 'function': {'name': item['name'], 'arguments': item['arguments']}}
        if messages and messages[-1]['role'] == 'assistant':
            messages[-1].setdefault('tool_calls', []).append(call)
        else:
            messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
        return
    if item.get('type') == 'function_call_output':
        if item.keys() - {'type', 'id', 'call_id', 'output'} or not isinstance(item.get('call_id'), str) or not 1 <= len(item['call_id']) <= 128 or not isinstance(item.get('output'), str):
            raise Problem('A function_call_output needs call_id and string output.')
        messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': item['output']})
        return
    if not isinstance(item, dict) or item.keys() - {'type', 'role', 'content', 'id', 'status'} or item.get('type', 'message') != 'message' or item.get('role') not in ('user', 'assistant', 'system', 'developer'):
        raise Problem('Unsupported Responses input item.')
    content = item.get('content')
    if isinstance(content, str):
        messages.append({'role': item['role'], 'content': content})
        return
    if not isinstance(content, list) or not 1 <= len(content) <= 20:
        raise Problem('Message content must be text or a bounded input content array.')
    converted = []
    for part in content:
        if not isinstance(part, dict):
            raise Problem('Invalid input content.')
        if part.get('type') in ('input_text', 'output_text') and set(part) == {'type', 'text'} and isinstance(part['text'], str):
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


def chat_request(body):
    """Validate Responses input and build an ordinary chat request."""
    allowed = {'model', 'input', 'instructions', 'text', 'store', 'stream', 'max_output_tokens', 'temperature', 'top_p', 'previous_response_id', 'background', 'truncation', 'tools', 'tool_choice', 'parallel_tool_calls'}
    if body.keys() - allowed:
        raise Problem('Unsupported Responses parameter.', 400, 'unsupported_parameter')
    if type(body.get('store', True)) is not bool:
        raise Problem('store must be a boolean.')
    if body.get('background', False) is not False or body.get('truncation', 'disabled') != 'disabled':
        raise Problem('Background Responses and automatic truncation are unsupported.', 400, 'unsupported_parameter')
    if 'previous_response_id' in body and (not isinstance(body['previous_response_id'], str) or not body['previous_response_id'] or len(body['previous_response_id']) > 100):
        raise Problem('previous_response_id must be a response ID.')
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
            append_input(messages, item)
    else:
        raise Problem('input must be a string or a nonempty input-item array.')
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
    sent.update(function_tools(body))
    return sent


def final_body(request, output, *, identifier=None, created=None, model=None, usage=None,
               status='completed', error=None, incomplete=None):
    """Build the common final object for streamed and ordinary Responses."""
    usage = usage or {}
    return {'id': identifier or 'resp_' + secrets.token_hex(16), 'object': 'response',
            'created_at': int(time.time()) if created is None else created, 'status': status,
            'error': error, 'incomplete_details': incomplete, 'model': model or request['model'],
            'output': output, 'store': request.get('store', True), 'previous_response_id': request.get('previous_response_id'),
            'parallel_tool_calls': request.get('parallel_tool_calls', True), 'tools': request.get('tools', []),
            'tool_choice': request.get('tool_choice', 'auto' if request.get('tools') else 'none'), 'instructions': request.get('instructions'),
            'max_output_tokens': request.get('max_output_tokens'),
            'text': request.get('text', {'format': {'type': 'text'}}),
            'usage': {'input_tokens': usage.get('prompt_tokens', 0),
                      'output_tokens': usage.get('completion_tokens', 0),
                      'total_tokens': usage.get('total_tokens', 0),
                      'input_tokens_details': {'cached_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)},
                      'output_tokens_details': {'reasoning_tokens': (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)}}}


def completion_status(reason):
    """Map chat termination to Responses completion and truncation semantics."""
    return ('completed', None) if reason in ('stop', 'tool_calls', 'function_call') else ('incomplete', {
        'reason': 'max_output_tokens' if reason == 'length' else 'content_filter'})


def response_body(chat, request):
    """Convert chat text, reasoning and refusals using the shared object builder."""
    try:
        choice = chat['choices'][0]
        message = choice['message']
        if not isinstance(message, dict) or not isinstance(message.get('content') or '', str):
            raise Problem('Upstream returned an invalid chat message.', 502, 'invalid_upstream_response')
        status, incomplete = completion_status(choice.get('finish_reason'))
        output = []
        reasoning = message.get('reasoning_content')
        if reasoning:
            if not isinstance(reasoning, str):
                raise Problem('Upstream returned invalid reasoning.', 502, 'invalid_upstream_response')
            output.append({'id': 'rs_' + secrets.token_hex(16), 'type': 'reasoning',
                           'summary': [{'type': 'summary_text', 'text': reasoning}]})
        content = []
        if message.get('content') or not message.get('refusal') and not message.get('tool_calls'):
            content.append({'type': 'output_text', 'text': message.get('content') or '', 'annotations': [], 'logprobs': []})
        if message.get('refusal'):
            content.append({'type': 'refusal', 'refusal': message['refusal']})
        if content:
            output.append({'id': 'msg_' + secrets.token_hex(16), 'type': 'message',
                           'role': 'assistant', 'status': status, 'content': content})
        for call in message.get('tool_calls') or []:
            function = call['function']
            if not isinstance(call.get('id'), str) or not FUNCTION_NAME.fullmatch(function.get('name', '') or '') or not isinstance(function.get('arguments'), str):
                raise Problem('Upstream returned an invalid function call.', 502, 'invalid_upstream_response')
            output.append({'type': 'function_call', 'id': 'fc_' + secrets.token_hex(16), 'call_id': call['id'],
                           'name': function['name'], 'arguments': function['arguments'], 'status': status})
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
    def __init__(self, request, on_done=None):
        """Initialize one response and its per-output event state."""
        self.request, self.on_done = request, on_done
        self.identifier, self.created = 'resp_' + secrets.token_hex(16), int(time.time())
        self.output, self.sequence, self.calls = [], 0, {}
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
            if self.message or self.reasoning_done or self.calls:
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

    def function_delta(self, call):
        """Accumulate parallel function arguments under stable response and call IDs."""
        index, function = call.get('index', 0), call.get('function') or {}
        if type(index) is not int or not 0 <= index < 128:
            raise Problem('Upstream returned an invalid function index.', 502, 'invalid_upstream_response')
        events = self.end_reasoning()
        if index not in self.calls:
            if not isinstance(call.get('id'), str) or not call['id'] or not valid_function_name(function.get('name')):
                raise Problem('Upstream function calls need an ID and name in their first delta.', 502, 'invalid_upstream_response')
            item = self.calls[index] = {'type': 'function_call', 'id': 'fc_' + secrets.token_hex(16),
                'call_id': call['id'], 'name': function['name'], 'arguments': '', 'status': 'in_progress'}
            self.output.append(item)
            events.append(self.event('response.output_item.added', output_index=len(self.output)-1, item=item))
        else:
            item = self.calls[index]
            if call.get('id') and call['id'] != item['call_id']:
                raise Problem('Upstream changed a function call ID.', 502, 'invalid_upstream_response')
            item['name'] += function.get('name') or ''
        arguments = function.get('arguments') or ''
        if not isinstance(arguments, str):
            raise Problem('Upstream function arguments must be text.', 502, 'invalid_upstream_response')
        item['arguments'] += arguments
        if arguments:
            events.append(self.event('response.function_call_arguments.delta', item_id=item['id'],
                                     output_index=self.output.index(item), delta=arguments))
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
                if delta.get('function_call'):
                    raise Problem('Upstream returned a legacy function call without an ID.', 502, 'unsupported_output')
                for kind in ('reasoning_content', 'content', 'refusal'):
                    if delta.get(kind) is not None and (delta[kind] or kind != 'reasoning_content'):
                        events += self.delta(kind, delta[kind])
                for call in delta.get('tool_calls') or []:
                    events += self.function_delta(call)
                if choice.get('finish_reason'):
                    self.finish = choice['finish_reason']
        except (KeyError, TypeError, AttributeError):
            raise Problem('Upstream returned an invalid chat chunk.', 502, 'invalid_upstream_response') from None
        return events

    def close_output(self):
        """Close one model step while keeping output open for a subsequent server-tool step."""
        if self.finish is None:
            raise Problem('Upstream stream ended before completion.', 502, 'upstream_disconnected')
        events = self.end_reasoning()
        if not self.message and not self.calls:
            events += self.delta('content', '')
        status, _ = completion_status(self.finish)
        if self.message:
            self.message['status'] = status
        for kind, index in self.parts.items():
            part = self.message['content'][index]
            fields = {'item_id': self.message['id'], 'output_index': self.output.index(self.message), 'content_index': index}
            events.append(self.event('response.' + ('refusal' if kind == 'refusal' else 'output_text') + '.done',
                                     **fields, **({'refusal': part['refusal']} if kind == 'refusal' else {'text': part['text'], 'logprobs': []})))
            events.append(self.event('response.content_part.done', **fields, part=part))
        if self.message:
            events.append(self.event('response.output_item.done', output_index=self.output.index(self.message), item=self.message))
        for item in self.calls.values():
            events += self.close_call(item, status)
        return events, status

    def close_call(self, item, status):
        """Close the arguments and final item of a client-run function call."""
        if not valid_function_name(item['name']):
            raise Problem('Upstream returned an invalid function name.', 502, 'invalid_upstream_response')
        item['status'] = status
        fields = {'item_id': item['id'], 'output_index': self.output.index(item)}
        return [self.event('response.function_call_arguments.done', **fields, arguments=item['arguments']),
                self.event('response.output_item.done', output_index=fields['output_index'], item=item)]

    def done(self):
        """Close output parts and emit a terminal response with final-chunk usage."""
        events, status = self.close_output()
        result = self.body(status)
        if self.on_done:
            self.on_done(result)
        events.append(self.event('response.' + status, response=result))
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
            for item in self.calls.values():
                item['status'] = 'incomplete'
            yield self.event('error', **details, param=None)
            result = self.body('failed', details)
            if self.on_done and details['code'] != 'storage_limit':
                try:
                    self.on_done(result)
                except Problem:
                    pass  # Revoked/deleted identities and exhausted quotas cannot retain new state.
            yield self.event('response.failed', response=result)


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


async def stream_response(service, body, sent, headers, on_done=None):
    """Open and validate upstream status before sending the first downstream byte."""
    context = service.door.stream('POST', '/v1/chat/completions', json=sent, headers=headers, timeout=None)
    answer = await context.__aenter__()
    handed_off = False
    try:
        forwarded = {key: value for key, value in answer.headers.items() if key.startswith(('x-loadcoach-', 'x-suite-'))}
        if not answer.is_success:
            await answer.aread()
            if on_done:
                on_done(final_body(body, [], status='failed', error={'message': 'Inference request failed.', 'code': 'upstream_error'}), answer.headers)
            return preserved_response(answer, forwarded)
        chunks = chat_events(answer)
        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            raise Problem('Upstream returned an empty stream.', 502, 'invalid_upstream_response') from None
        stream = ResponseStream(body, (lambda result: on_done(result, answer.headers)) if on_done else None)
        response = ClosingStream(stream.events(chunks, first), context, media_type='text/event-stream',
                                 headers={**PRIVATE, **forwarded})
        handed_off = True
        return response
    finally:
        if not handed_off:
            with anyio.CancelScope(shield=True):
                await context.__aexit__(None, None, None)


def problem_response(error):
    """Use the ordinary private API error envelope for validation and lifecycle failures."""
    return JSONResponse({'error': {'message': str(error), 'type': 'invalid_request_error', 'code': error.code}},
                        error.status, headers=PRIVATE)


def routes(caller, service):
    """Register creation and the private stored Responses lifecycle on the model gateway."""
    def authenticated(request):
        """Resolve the current bearer identity for every read, write and continuation."""
        user = caller(request.headers.get('authorization', '').removeprefix('Bearer '))
        if not user:
            raise Problem('Missing or wrong API key.', 401, 'invalid_api_key')
        return user

    async def create(request):
        """Execute creation and persist final state through the normal chat admission path."""
        attempted = False
        try:
            user = authenticated(request)
            body = await read_json(request, MAX_BODY)
            from loadcoach.request_tools import tools_refusal
            denied = tools_refusal(user, body)
            if denied and isinstance(body.get('model'), str):
                checked = await service.door.get('/v1/models/' + quote(body['model'], safe=''),
                    headers={'authorization': request.headers.get('authorization', '')}, timeout=30)
                if not checked.is_success:
                    return preserved_response(checked, {})
                return denied
            store = getattr(service, 'responses', None)
            from turnstile.response_tools import prepare as prepare_tools
            tool_context = prepare_tools(body, request, service, user, store)
            sent = chat_request(tool_context.ordinary if tool_context else body)
            if tool_context and tool_context.empty:
                sent['messages'].pop()
            storing = body.get('store', True)
            if storing and request.headers.get('x-loadcoach-log', '').lower() == 'off':
                raise Problem('Stored Responses require logging. Send store:false with X-LoadCoach-Log: off.')
            if (storing or body.get('previous_response_id')) and store is None:
                raise Problem('Responses storage is unavailable.', 503, 'storage_unavailable')
            level = request.headers.get('x-loadcoach-sensitivity', 'normal')
            if level not in LEVELS:
                raise Problem('Invalid X-LoadCoach-Sensitivity level.')
            if LEVELS.index(level) > LEVELS.index(user.group.max_sensitivity):
                raise Problem('This sensitivity level is not allowed.', 403, 'sensitivity_not_allowed')
            if body.get('previous_response_id'):
                prior, previous_level = store.history(user, body['previous_response_id'])
                level = max(level, previous_level, key=LEVELS.index)
                first = 1 if 'instructions' in body else 0
                sent['messages'][first:first] = prior
            saved = {**body, 'input': normalized_input(body['input']), 'store': storing}
            if storing:
                store.capacity(owner_of(user), len(json.dumps(saved).encode()))
            headers = {key: value for key, value in request.headers.items()
                       if key == 'authorization' or key.startswith(('x-loadcoach-', 'x-suite-'))}
            if request.headers.get('x-loadcoach-log') != 'off' or level != 'normal':
                headers['x-loadcoach-sensitivity'] = level
            def finished(result, response_headers):
                """Keep the final object at its effective level with the ordinary request link."""
                if storing:
                    current = caller(request.headers.get('authorization', '').removeprefix('Bearer '))
                    if not current or owner_of(current) != owner_of(user):
                        raise Problem('Caller authorization changed.', 403, 'authorization_changed')
                    actual = service.tool_level(current, headers.get('x-loadcoach-trace')) if hasattr(service, 'tool_level') and headers.get('x-loadcoach-trace') else level
                    stored_level = max(level, actual if actual in LEVELS else level, key=LEVELS.index)
                    store.save(user, saved, result, stored_level, response_headers.get('x-loadcoach-request-id'))
            attempted = True
            if tool_context:
                from turnstile.response_tools import respond as tool_response
                return await tool_response(tool_context, body, sent, headers, service, user, finished, store)
            if sent['stream']:
                return await stream_response(service, body, sent, headers, finished)
            answer = await service.door.post('/v1/chat/completions', json=sent, headers=headers, timeout=None)
            forwarded = {key: value for key, value in answer.headers.items() if key.startswith(('x-loadcoach-', 'x-suite-'))}
            if not answer.is_success:
                if storing:
                    finished(final_body(body, [], status='failed', error={'message': 'Inference request failed.', 'code': 'upstream_error'}), answer.headers)
                return preserved_response(answer, forwarded)
            try:
                result = response_body(answer.json(), body)
            except (ValueError, Problem) as error:
                problem = error if isinstance(error, Problem) else Problem('Upstream returned invalid JSON.', 502, 'invalid_upstream_response')
                if storing:
                    finished(final_body(body, [], status='failed', error={'message': str(problem), 'code': problem.code}), answer.headers)
                raise problem
            finished(result, answer.headers)
            return JSONResponse(result, headers={**PRIVATE, **forwarded})
        except httpx.HTTPError:
            if attempted and storing:
                finished(final_body(body, [], status='failed', error={'message': 'Model gateway is unavailable.', 'code': 'gateway_unavailable'}), {})
            return problem_response(Problem('Model gateway is unavailable.', 503, 'gateway_unavailable'))
        except Problem as error:
            return problem_response(error)

    async def lifecycle(request):
        """Read, delete or page normalized inputs with current identity and clearance."""
        try:
            user = authenticated(request)
            store = service.responses
            identifier = request.path_params['identifier']
            if request.method == 'DELETE':
                store.remove(user, identifier)
                return JSONResponse({'id': identifier, 'object': 'response.deleted', 'deleted': True}, headers=PRIVATE)
            record = store.one(user, identifier)
            if request.url.path.endswith('/input_items'):
                try:
                    limit = int(request.query_params.get('limit', 20))
                except ValueError:
                    raise Problem('limit must be an integer from 1 to 100.') from None
                order = request.query_params.get('order', 'desc')
                if not 1 <= limit <= 100 or order not in ('asc', 'desc'):
                    raise Problem('Use limit 1–100 and order asc or desc.')
                items = record['request']['input']
                if order == 'desc':
                    items = list(reversed(items))
                after = request.query_params.get('after')
                if after:
                    index = next((index for index, item in enumerate(items) if item['id'] == after), None)
                    if index is None:
                        raise Problem('Unknown input_items cursor.')
                    items = items[index+1:]
                page = items[:limit]
                return JSONResponse({'object': 'list', 'data': page, 'first_id': page[0]['id'] if page else None,
                                     'last_id': page[-1]['id'] if page else None, 'has_more': len(items)>limit}, headers=PRIVATE)
            return JSONResponse(record['response'], headers=PRIVATE)
        except Problem as error:
            return problem_response(error)
    return [Route('/v1/responses', create, methods=['POST']),
            Route('/v1/responses/{identifier}/input_items', lifecycle),
            Route('/v1/responses/{identifier}', lifecycle, methods=['GET', 'DELETE'])]


def preserved_response(answer, headers):
    """Retain ordinary upstream status, error body and routing headers."""
    from starlette.responses import Response
    return Response(answer.content, answer.status_code, headers={**PRIVATE, **headers}, media_type=answer.headers.get('content-type', 'application/json'))
