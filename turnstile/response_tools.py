"""Responses MCP protocol over the caller-key shared tool loop and ordinary model admission."""

import asyncio
import json
import secrets
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import anyio
from starlette.responses import JSONResponse

from turnstile.extractions import PRIVATE
from turnstile.responses import (ClosingStream, ResponseStream, chat_events, response_body, problem_response,
                                 valid_function_name, completion_status)
from turnstile.tool_loop import (ToolClient, ToolLimit, ToolTurn, approval_needed, configuration,
                                 exact_result, model_result)
from weightroom.extraction import Problem


def prepare(body, request, service, user, store):
    """Validate MCP-only fields and remove approval inputs from the ordinary chat adapter view."""
    tools = body.get('tools', [])
    items = body.get('input')
    approvals = [item for item in items if isinstance(item, dict) and item.get('type') == 'mcp_approval_response'] if isinstance(items, list) else []
    configs = [tool for tool in tools if isinstance(tool, dict) and tool.get('type') == 'mcp'] if isinstance(tools, list) else []
    if not configs and not approvals:
        return None
    if request.headers.get('x-loadcoach-log', '').lower() == 'off':
        raise Problem('Server-run ToolLocker tools require logging; X-LoadCoach-Log: off is unavailable.')
    if approvals and not configs:
        previous = body.get('previous_response_id')
        if not store or not isinstance(previous, str) or not previous:
            raise Problem('MCP approvals require a stored previous_response_id.')
        old = store.one(user, previous, own=True)
        configs = [dict(tool) for tool in old['request'].get('tools', []) if tool.get('type') == 'mcp']
        for tool in configs:
            if tool.get('server_url'):
                tool['server_url'] = urlsplit(tool['server_url']).path
        body['tools'] = [*tools, *configs]
        tools = body['tools']
    if len(configs) != 1:
        raise Problem('Use one ToolLocker MCP definition per response.')
    config = configuration(configs[0], request.url, getattr(service, 'tool_url', 'http://turnstile.internal:8443/mcp'))
    if (config['require_approval'] != 'never' or approvals) and body.get('store', True) is not True:
        raise Problem('MCP approval mode requires store:true.')
    if approvals and (not isinstance(items, list) or not 1 <= len(items) <= 100):
        raise Problem('Use at most 100 input items.')
    for item in approvals:
        if (item.keys() - {'type', 'id', 'approval_request_id', 'approve', 'reason'} or
                not isinstance(item.get('approval_request_id'), str) or not 1 <= len(item['approval_request_id']) <= 100
                or type(item.get('approve')) is not bool or not isinstance(item.get('reason', ''), str) or len(item.get('reason', '')) > 2048):
            raise Problem('An mcp_approval_response needs approval_request_id, approve and an optional reason.')
    pending = {}
    if approvals:
        previous = body.get('previous_response_id')
        if not store or not isinstance(previous, str) or not previous:
            raise Problem('MCP approvals require a stored previous_response_id.')
        pending = store.pending_approvals(user, previous)
        identifiers = [item['approval_request_id'] for item in approvals]
        if len(set(identifiers)) != len(identifiers) or any(identifier not in pending for identifier in identifiers):
            raise Problem('No pending MCP approval has this ID.', 404, 'approval_not_found')
    ordinary = {**body, 'tools': [tool for tool in tools if tool not in configs]}
    empty = False
    if approvals:
        ordinary['input'] = [item for item in items if item not in approvals]
        empty = not ordinary['input']
        if empty:
            ordinary['input'] = ''
    return SimpleNamespace(config=config, approvals=approvals, pending=pending, ordinary=ordinary, empty=empty)


class ToolResponseStream(ResponseStream):
    """Keep one Responses lifecycle while each native model step starts fresh output items."""
    def __init__(self, request, client_names, policy):
        """Know which calls belong to the client before translating native function deltas."""
        super().__init__(request)
        self.client_names = set(client_names)
        self.policy = policy

    def begin_step(self):
        """Reset per-model assembly without replacing response identity or earlier tool outputs."""
        self.reasoning, self.message, self.parts, self.calls = None, None, {}, {}
        self.reasoning_done, self.finish, self.usage = False, None, {}

    def function_delta(self, call):
        """Expose server-run call arguments as MCP events while preserving client function items."""
        index = call.get('index', 0)
        function = call.get('function') or {}
        if index in self.calls and self.calls[index]['type'] == 'function_call' or function.get('name') in self.client_names:
            return super().function_delta(call)
        if type(index) is not int or not 0 <= index < 128:
            raise Problem('Invalid upstream tool-call index.', 502, 'invalid_upstream_response')
        events = self.end_reasoning()
        if index not in self.calls:
            if not isinstance(call.get('id'), str) or not call['id'] or not valid_function_name(function.get('name')):
                raise Problem('Upstream tool calls need an ID and name.', 502, 'invalid_upstream_response')
            pending = approval_needed(self.policy, function['name'])
            item = self.calls[index] = {'id': ('mcpr_' if pending else 'mcp_')+secrets.token_hex(16), 'type': 'mcp_approval_request' if pending else 'mcp_call', 'server_label': 'toollocker',
                'call_id': call['id'], 'name': function['name'], 'arguments': '', 'output': None, 'error': None, 'status': 'in_progress'}
            if pending:
                for field in ('output', 'error', 'status'):
                    item.pop(field)
            self.output.append(item)
            events.append(self.event('response.output_item.added', output_index=len(self.output)-1, item=item))
            if not pending:
                events.append(self.event('response.mcp_call.in_progress', item_id=item['id'], output_index=len(self.output)-1))
        else:
            item = self.calls[index]
            item['name'] += function.get('name') or ''
        delta = function.get('arguments') or ''
        if not isinstance(delta, str):
            raise Problem('Tool arguments must be string deltas.', 502, 'invalid_upstream_response')
        item['arguments'] += delta
        if delta and item['type'] != 'mcp_approval_request':
            events.append(self.event('response.mcp_call_arguments.delta', item_id=item['id'], output_index=self.output.index(item), delta=delta))
        return events

    def close_call(self, item, status):
        """Close MCP arguments now; close the item only after execution or approval handoff."""
        if item['type'] == 'function_call':
            return super().close_call(item, status)
        if item['type'] == 'mcp_approval_request':
            return []
        if not valid_function_name(item['name']):
            raise Problem('Invalid upstream tool name.', 502, 'invalid_upstream_response')
        return [self.event('response.mcp_call_arguments.done', item_id=item['id'], output_index=self.output.index(item), arguments=item['arguments'])]


def add_usage(total, usage):
    """Sum native accounting from every admitted model step, including cache and reasoning counts."""
    if not isinstance(usage, dict):
        raise Problem('Invalid upstream usage.', 502, 'invalid_upstream_response')
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        total[key] = total.get(key, 0) + (usage.get(key) or 0)
    for key, detail in (('prompt_tokens_details', 'cached_tokens'), ('completion_tokens_details', 'reasoning_tokens')):
        total.setdefault(key, {}).setdefault(detail, 0)
        total[key][detail] += (usage.get(key) or {}).get(detail, 0)


class CancelRun:
    """Attach a background tool run to the downstream stream's lifetime."""
    def __init__(self, task):
        """Own the one task carrying model and tool HTTP contexts."""
        self.task = task

    async def __aexit__(self, *args):
        """Cancel and await upstream work when the caller stops receiving the response."""
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def respond(context, body, sent, headers, service, user, finished, store):
    """Run MCP calls through shared orchestration, streaming real native deltas when requested."""
    client_names = {tool['function']['name'] for tool in sent.get('tools', [])}
    state = ToolResponseStream(body, client_names, context.config['require_approval'])
    headers.setdefault('x-loadcoach-trace', state.identifier)
    key = headers.get('authorization', '').removeprefix('Bearer ')
    def current_level(trace):
        """Resolve account renames and app restrictions afresh before reading the trace."""
        current = service.caller(key) if hasattr(service, 'caller') else user
        return service.tool_level(current or user, trace) if hasattr(service, 'tool_level') else headers.get('x-loadcoach-sensitivity', 'normal')
    own_url = str(getattr(service, 'tool_url', 'http://turnstile.internal:8443/mcp'))
    target = str(httpx.URL(own_url).copy_with(path=context.config['path']))
    tools = ToolClient(getattr(service, 'tool_http', None) or service.door, target, headers['authorization'],
                       headers['x-loadcoach-trace'], headers.get('x-loadcoach-sensitivity', 'normal'), current_level)
    limits = service.tool_limits() if hasattr(service, 'tool_limits') else (25, 900)
    hosted = body['model'] in service.hosted_models() if hasattr(service, 'hosted_models') else False
    turn = ToolTurn(tools, body['model'], hosted=hosted, max_calls=limits[0], max_seconds=limits[1])
    queue, ready = asyncio.Queue(maxsize=128), asyncio.Event()
    final, first_error, response_headers, totals = None, None, {}, {}

    async def emit(events):
        """Respect downstream backpressure without buffering unbounded native output."""
        if body.get('stream'):
            for event in events:
                await queue.put(event)

    async def model(messages, definitions):
        """Use only normal chat admission with the same caller, trace and current level."""
        nonlocal response_headers
        state.begin_step()
        asked = {**sent, 'messages': messages, 'tools': definitions}
        model_headers = {**headers, 'x-loadcoach-sensitivity': tools.current_level()}
        async with service.door.stream('POST', '/v1/chat/completions', json=asked, headers=model_headers, timeout=None) as answer:
            if not response_headers:
                response_headers = {key: value for key, value in answer.headers.items() if key.startswith(('x-loadcoach-', 'x-suite-'))}
            if not answer.is_success:
                await answer.aread()
                try:
                    error = answer.json().get('error') or {}
                except ValueError:
                    error = {}
                if not isinstance(error, dict):
                    error = {'message': str(error)}
                raise Problem(error.get('message', 'Model request failed.'), answer.status_code, error.get('code') or 'upstream_error')
            if answer.headers.get('x-loadcoach-service') or answer.headers.get('x-loadcoach-hosted'):
                turn.hosted = True
            ready.set()
            if sent['stream']:
                async for chunk in chat_events(answer):
                    await emit(state.chunk(chunk))
            else:
                await answer.aread()
                try:
                    result = answer.json()
                    response_body(result, body)
                    choice = result['choices'][0]
                    message = dict(choice['message'])
                    if message.get('tool_calls'):
                        message['tool_calls'] = [{'index': index, **call} for index, call in enumerate(message['tool_calls'])]
                    state.chunk({'model': result.get('model'), 'choices': [{'delta': message, 'finish_reason': choice.get('finish_reason')}],
                                 'usage': result.get('usage') or {}})
                except (ValueError, KeyError, TypeError):
                    raise Problem('Model returned invalid JSON.', 502, 'invalid_upstream_response') from None
        events, status = state.close_output()
        await emit(events)
        usage = state.usage
        add_usage(totals, usage)
        message = {'role': 'assistant', 'content': ''.join(part.get('text', part.get('refusal', '')) for part in (state.message or {}).get('content', [])) or None}
        if state.calls:
            message['tool_calls'] = [{'id': item['call_id'], 'type': 'function', 'function': {'name': item['name'], 'arguments': item['arguments']}} for item in state.calls.values()]
        if state.reasoning:
            message['reasoning_content'] = state.reasoning['summary'][0]['text']
        return {'choices': [{'message': message, 'finish_reason': state.finish}], 'usage': usage}

    async def approve(call):
        """Pause only the calls selected by this client's approval policy."""
        return None if approval_needed(context.config['require_approval'], call['function']['name']) else True

    async def tool_result(call, result, item=None):
        """Retain exact MCP output and publish completion after ToolLocker has rechecked the call."""
        item = item or next(item for item in state.calls.values() if item['call_id'] == call['id'])
        item.update(output=None if result.get('isError') else exact_result(result),
                    status='failed' if result.get('isError') else 'completed',
                    error={'type': 'mcp_tool_execution_error', 'content': result} if result.get('isError') else None)
        fields = {'item_id': item['id'], 'output_index': state.output.index(item)}
        await emit([state.event('response.mcp_call.'+('failed' if result.get('isError') else 'completed'), **fields),
                    state.event('response.output_item.done', output_index=fields['output_index'], item=item)])

    async def run():
        """Compose list/model/approval/result events into one saved terminal Responses object."""
        nonlocal final, first_error
        status, failure, incomplete = 'completed', None, None
        cancelled = False
        try:
            await emit([state.event('response.created', response=state.body()), state.event('response.in_progress', response=state.body())])
            listed = {'id': 'mcpl_'+secrets.token_hex(16), 'type': 'mcp_list_tools', 'server_label': 'toollocker', 'tools': []}
            state.output.append(listed)
            await emit([state.event('response.output_item.added', output_index=0, item=listed),
                        state.event('response.mcp_list_tools.in_progress', item_id=listed['id'], output_index=0)])
            async with asyncio.timeout(turn.remaining()):
                available = await tools.listing(context.config.get('allowed_tools'))
            listed['tools'] = [{'name': tool['name'], 'description': tool.get('description', ''), 'input_schema': tool.get('inputSchema', {})} for tool in available]
            await emit([state.event('response.mcp_list_tools.completed', item_id=listed['id'], output_index=0),
                        state.event('response.output_item.done', output_index=0, item=listed)])
            names = {tool['name'] for tool in available}
            if names & client_names:
                raise Problem('Function tools must not shadow ToolLocker tool names.')
            definitions = [*sent.get('tools', []), *tools.definitions()]
            if len(definitions)>128 or any(not valid_function_name(tool['function']['name']) for tool in definitions):
                raise Problem('Choose a ToolLocker tool set or allowed_tools list with at most 128 valid function names.')
            messages = list(sent['messages'])
            if context.approvals:
                # Approval execution can produce output before another model request starts.
                ready.set()
                pending = dict(context.pending)
                seen = set()
                for decision in context.approvals:
                    identifier = decision['approval_request_id']
                    if identifier not in pending or identifier in seen:
                        raise Problem('No pending MCP approval has this ID.', 404, 'approval_not_found')
                    seen.add(identifier)
                    original = pending.pop(identifier)
                    call = {'id': original['call_id'], 'function': {'name': original['name'], 'arguments': original['arguments']}}
                    item = {'id': 'mcp_'+secrets.token_hex(16), 'type': 'mcp_call', 'server_label': 'toollocker',
                            'call_id': call['id'], 'name': original['name'], 'arguments': original['arguments'],
                            'approval_request_id': identifier, 'status': 'in_progress', 'output': None, 'error': None}
                    state.output.append(item)
                    await emit([state.event('response.output_item.added', output_index=len(state.output)-1, item=item),
                                state.event('response.mcp_call.in_progress', item_id=item['id'], output_index=len(state.output)-1)])
                    turn.account_call()
                    result = await turn.execute(call, count=False) if decision['approve'] else {
                        'content': [{'type': 'text', 'text': 'Tool call declined.' + (' '+decision['reason'] if decision.get('reason') else '')}], 'isError': True}
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': model_result(result)})
                    await tool_result(call, result, item)
                if pending:
                    for item in pending.values():
                        state.output.append(item)
                        await emit([state.event('response.output_item.added', output_index=len(state.output)-1, item=item),
                                    state.event('response.output_item.done', output_index=len(state.output)-1, item=item)])
                    return
            async for step in turn.run(messages, definitions, model, client_names=client_names, approve=approve):
                if step['type'] == 'model':
                    status, incomplete = completion_status(state.finish)
                elif step['type'] == 'approval':
                    call = step['call']
                    item = next(item for item in state.calls.values() if item['call_id'] == call['id'])
                    await emit([state.event('response.output_item.done', output_index=state.output.index(item), item=item)])
                elif step['type'] == 'tool_result':
                    await tool_result(step['call'], step['result'])
        except asyncio.CancelledError:
            cancelled = True
            status, failure = 'failed', {'code': 'cancelled', 'message': 'The caller disconnected; model and tool work was cancelled.'}
            raise
        except (ToolLimit, TimeoutError) as error:
            status, incomplete = 'incomplete', {'reason': getattr(error, 'reason', 'max_tool_time')}
        except (Problem, httpx.HTTPError) as error:
            status = 'failed'
            failure = {'code': getattr(error, 'code', 'gateway_unavailable'),
                       'message': str(error) if isinstance(error, Problem) else 'Model gateway is unavailable.'}
            if not ready.is_set():
                first_error = error if isinstance(error, Problem) else Problem(failure['message'], 503, failure['code'])
            await emit([state.event('error', **failure, param=None)])
        except Exception:
            import traceback
            traceback.print_exc()
            status, failure = 'failed', {'code': 'tool_loop_failed', 'message': 'The tool run failed. See control node diagnostics.'}
            if not ready.is_set():
                first_error = Problem(failure['message'], 500, failure['code'])
            await emit([state.event('error', **failure, param=None)])
        finally:
            cancelled = cancelled or bool(asyncio.current_task().cancelling())
            if status != 'completed':
                for item in state.output:
                    if item.get('status') == 'in_progress':
                        item['status'] = 'failed' if item['type'] == 'mcp_call' else 'incomplete'
                        if item['type'] == 'mcp_call':
                            item['error'] = {'type': 'mcp_tool_execution_error', 'content': {
                                'isError': True, 'content': [{'type': 'text', 'text': (failure or {}).get('message') or (incomplete or {}).get('reason', 'Tool call stopped.')} ]}}
                            if not cancelled:
                                await emit([state.event('response.mcp_call.failed', item_id=item['id'], output_index=state.output.index(item)),
                                            state.event('response.output_item.done', output_index=state.output.index(item), item=item)])
            state.usage = totals
            final = state.body(status, failure)
            final['incomplete_details'] = incomplete
            try:
                finished(final, response_headers)
            except Problem as error:
                first_error = error if not ready.is_set() or not body.get('stream') else first_error
                final.update(status='failed', error={'code': error.code, 'message': str(error)})
            except Exception:
                error = Problem('Cannot save the completed Responses record.', 503, 'storage_unavailable')
                first_error = error if not ready.is_set() or not body.get('stream') else first_error
                final.update(status='failed', error={'code': error.code, 'message': str(error)})
            if not cancelled:
                await emit([state.event('response.'+final['status'], response=final)])
            ready.set()
            if body.get('stream') and not cancelled:
                await queue.put(None)

    if not body.get('stream'):
        await run()
        if first_error:
            answer = problem_response(first_error)
            answer.headers.update(response_headers)
            return answer
        return JSONResponse(final, headers={**PRIVATE, **response_headers})
    task = asyncio.create_task(run())
    try:
        await ready.wait()
    except BaseException:
        with anyio.CancelScope(shield=True):
            await CancelRun(task).__aexit__()
        raise
    if first_error:
        await CancelRun(task).__aexit__()
        answer = problem_response(first_error)
        answer.headers.update(response_headers)
        return answer
    async def content():
        """Yield protocol events as produced; cancellation belongs to ClosingStream."""
        while (event := await queue.get()) is not None:
            yield event
    return ClosingStream(content(), CancelRun(task), media_type='text/event-stream', headers={**PRIVATE, **response_headers})
