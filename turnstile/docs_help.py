"""The docs-help role configures the existing Responses loop and adapts its chat protocol."""

import asyncio
from contextvars import ContextVar
import json
from pathlib import Path
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from turnstile.extractions import PRIVATE, read_json
from turnstile.tool_loop import ToolClient
from weightroom.extraction import MAX_BODY, Problem

NATIVE = ContextVar('docs_help_native_admission', default=False)
MARKER = '[DOCS_NOT_DOCUMENTED]'
PROMPT = Path(__file__).resolve().parents[1] / 'weightroom/docs_help_prompt.md'


def answer_text(result):
    """Read only assistant output text, excluding reasoning and exact tool payloads."""
    return '\n'.join(part.get('text', '') for item in result.get('output', []) if item.get('type') == 'message'
                     for part in item.get('content', []) if part.get('type') == 'output_text')


async def prepare(body, request, service, user):
    """Restrict docs-help to its canonical prompt and the caller's offered docs/code sources."""
    if body.get('model') != 'docs-help':
        return None
    if not user.can('use-tools'):
        raise Problem('Ask requires use-tools. Use Documentation Search instead.', 403, 'tools_not_allowed')
    if not getattr(service, 'docs_role', lambda: False)():
        raise Problem('The docs-help role is not configured. Ask an administrator to set it in Roles.', 409, 'docs_role_missing')
    if body.keys() - {'model', 'input', 'stream', 'store', 'max_output_tokens', 'temperature', 'top_p'}:
        raise Problem('docs-help chooses its own instructions and source tools; send only input and ordinary output options.')
    if body.get('store', False) is not False:
        raise Problem('docs-help keeps no stored conversation. Send store:false.')
    items = body.get('input')
    if isinstance(items, str):
        items = [{'role': 'user', 'content': items}]
    if (not isinstance(items, list) or not 1 <= len(items) <= 20 or any(
            not isinstance(item, dict) or item.keys() - {'role', 'content', 'type'} or
            item.get('type', 'message') != 'message' or item.get('role') not in ('user', 'assistant') or
            not isinstance(item.get('content'), str) or len(item['content']) > 6000 for item in items)
            or sum(len(item['content']) for item in items) > 20000 or items[-1]['role'] != 'user'
            or not items[-1]['content'].strip()):
        raise Problem('Ask needs a question and at most 20 text messages, each at most 6,000 characters and 20,000 total.')
    authorization = request.headers.get('authorization', '')
    client = ToolClient(getattr(service, 'tool_http', None) or service.door,
                        getattr(service, 'tool_url', 'http://turnstile.internal:8443/mcp'), authorization,
                        'docs-help-' + secrets.token_hex(16), request.headers.get('x-loadcoach-sensitivity', 'normal'))
    try:
        async with asyncio.timeout(8):
            offered = await client.listing()
    except TimeoutError:
        raise Problem('ToolLocker is not answering. Use Documentation Search.', 503, 'toollocker_unavailable') from None
    allowed = sorted((tool['name'] for tool in offered if tool['name'].startswith('docs__') or
                      user.can('manage-settings') and tool['name'].startswith('code__')),
                     key=lambda name: (not name.startswith('docs__'), name))
    if 'docs__search' not in allowed or 'docs__read' not in allowed:
        raise Problem('The account needs the docs search/read tools. Use Search or ask an administrator.', 403, 'docs_tools_unavailable')
    body.update(input=items, store=False, instructions=PROMPT.read_text(), tools=[{
        'type': 'mcp', 'server_label': 'toollocker', 'allowed_tools': allowed[:128], 'require_approval': 'never'}])
    return items[-1]['content']


def finished(result, question, service):
    """Record a signalled documentation gap once and remove its private display marker."""
    if result.get('status') == 'completed' and MARKER in answer_text(result):
        service.docs_gaps.record(question)
    for item in result.get('output', []):
        if item.get('type') == 'message':
            for part in item.get('content', []):
                if part.get('type') == 'output_text':
                    part['text'] = part['text'].replace(MARKER, '').strip()


def replacement(request, body):
    """Give the existing Responses handler a validated adapted body with the same caller headers."""
    raw = json.dumps(body).encode()
    async def receive():
        """Supply the one adapted JSON body without a second HTTP authentication hop."""
        return {'type': 'http.request', 'body': raw, 'more_body': False}
    return Request({**request.scope, 'path': '/v1/responses', 'raw_path': b'/v1/responses'}, receive)


def chat_result(result):
    """Translate one completed help response into the ordinary chat completion envelope."""
    usage = result.get('usage') or {}
    return {'id': result['id'], 'object': 'chat.completion', 'created': result['created_at'],
            'model': 'docs-help', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer_text(result)},
            'finish_reason': 'stop' if result['status'] == 'completed' else 'length'}],
            'usage': {'prompt_tokens': usage.get('input_tokens', 0), 'completion_tokens': usage.get('output_tokens', 0),
                      'total_tokens': usage.get('total_tokens', 0)}}


def routes(caller, service, create, fallback):
    """Expose role readiness and administrator gap management alongside the help chat adapter."""
    async def status(request):
        """Report docs-help role readiness without waking compute or exposing model configuration."""
        user = caller(request.headers.get('authorization', '').removeprefix('Bearer '))
        if not user:
            return JSONResponse({'error': {'message': 'Missing or wrong API key.'}}, 401, headers=PRIVATE)
        if not user.can('use-tools'):
            return JSONResponse({'error': {'message': 'Ask requires use-tools.'}}, 403, headers=PRIVATE)
        return JSONResponse({'configured': bool(getattr(service, 'docs_role', lambda: False)())}, headers=PRIVATE)

    async def gaps(request):
        """List or dismiss anonymous documentation questions; requires manage-settings."""
        user = caller(request.headers.get('authorization', '').removeprefix('Bearer '))
        if not user:
            return JSONResponse({'error': {'message': 'Missing or wrong API key.'}}, 401, headers=PRIVATE)
        if not user.can('manage-settings'):
            return JSONResponse({'error': {'message': 'Requires manage-settings.'}}, 403, headers=PRIVATE)
        if request.method == 'POST':
            try:
                body = await read_json(request, 1024)
                identifier = body.get('dismiss')
                if body.keys() != {'dismiss'} or not isinstance(identifier, str) or len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
                    raise Problem('Choose one gap to dismiss.')
                service.docs_gaps.dismiss(identifier)
            except Problem as error:
                from turnstile.responses import problem_response
                return problem_response(error)
        return JSONResponse({'gaps': service.docs_gaps.listing()}, headers=PRIVATE)

    async def chat(request):
        """Run docs-help chat through Responses, leaving ordinary model chat on its existing route."""
        from turnstile.responses import problem_response, ClosingStream
        if NATIVE.get():
            return await fallback(request)
        try:
            try:
                body = await request.json()
            except (ValueError, UnicodeError):
                return await fallback(request)
            if not isinstance(body, dict) or body.get('model') != 'docs-help':
                return await fallback(request)
            if body.keys() - {'model', 'messages', 'stream', 'max_tokens', 'temperature', 'top_p', 'stream_options'}:
                raise Problem('docs-help chat accepts text messages and ordinary output options.')
            if 'stream_options' in body and body['stream_options'] != {'include_usage': True}:
                raise Problem('Only stream_options.include_usage:true is supported.')
            sent = {key: value for key, value in body.items() if key not in ('messages', 'max_tokens', 'stream_options')}
            sent.update(input=body.get('messages'), store=False)
            if 'max_tokens' in body:
                sent['max_output_tokens'] = body['max_tokens']
            response = await create(replacement(request, sent))
            if response.status_code != 200:
                return response
            if not sent.get('stream'):
                result = json.loads(response.body)
                if result['status'] == 'failed':
                    return JSONResponse({'error': result['error']}, 502, headers=PRIVATE)
                return JSONResponse(chat_result(result), headers={key: value for key, value in response.headers.items()
                                                                 if key.lower() not in ('content-length', 'content-type')})
            async def content():
                """Translate existing Responses text events while retaining stream cancellation ownership."""
                buffered = ''
                async for raw in response.body_iterator:
                    buffered += raw.decode() if isinstance(raw, bytes) else raw
                    while '\n\n' in buffered:
                        frame, buffered = buffered.split('\n\n', 1)
                        data = '\n'.join(line[6:] for line in frame.splitlines() if line.startswith('data: '))
                        if not data:
                            continue
                        event = json.loads(data)
                        if event['type'] == 'response.output_text.delta':
                            yield 'data: ' + json.dumps({'object': 'chat.completion.chunk', 'model': 'docs-help',
                                'choices': [{'index': 0, 'delta': {'content': event['delta']}, 'finish_reason': None}]}) + '\n\n'
                        elif event['type'] in ('response.completed', 'response.failed', 'response.incomplete'):
                            final = chat_result(event['response'])
                            if event['type'] == 'response.failed':
                                yield 'data: ' + json.dumps({'error': event['response']['error']}) + '\n\n'
                            else:
                                final.update(object='chat.completion.chunk', choices=[{'index': 0, 'delta': {},
                                             'finish_reason': final['choices'][0]['finish_reason']}])
                                yield 'data: ' + json.dumps(final) + '\n\n'
                yield 'data: [DONE]\n\n'
            # ClosingStream owns the shared loop's cancellation handle even if this adapter disconnects.
            return ClosingStream(content(), response.upstream, media_type='text/event-stream', headers=PRIVATE)
        except Problem as error:
            return problem_response(error)
    result = [Route('/turnstile/docs/status', status), Route('/turnstile/docs/gaps', gaps, methods=['GET', 'POST'])]
    if fallback:
        result.append(Route('/v1/chat/completions', chat, methods=['POST']))
    return result
