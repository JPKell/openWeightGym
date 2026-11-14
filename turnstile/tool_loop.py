"""Caller-key ToolLocker access and bounded model/tool turns shared by API and console."""

import asyncio
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from loadcoach.groups import LEVELS
from weightroom.extraction import Problem

MODEL_RESULT_BYTES = 64 * 1024
TOOL_PATH = re.compile(r'/mcp(?:/[a-z0-9][a-z0-9._-]{0,63})?')


def configuration(tool, origin, own_url):
    """Accept only this the control node's ToolLocker and a narrowing allowlist/approval policy."""
    if not isinstance(tool, dict) or tool.keys() - {'type', 'server_label', 'server_url', 'allowed_tools', 'require_approval'} or tool.get('type') != 'mcp' or tool.get('server_label') != 'toollocker':
        raise Problem('Only this server\'s ToolLocker is available.', 400, 'unsupported_parameter')
    url = tool.get('server_url') or '/mcp'
    if not isinstance(url, str):
        raise Problem('Only this server\'s ToolLocker is available.')
    try:
        parsed, current, own = urlsplit(url), urlsplit(str(origin)), urlsplit(str(own_url))
    except ValueError:
        raise Problem('Only this server\'s ToolLocker is available.', 400, 'unsupported_parameter') from None
    try:
        foreign = parsed.netloc and (parsed.hostname != current.hostname or parsed.port not in (None, current.port, own.port))
    except ValueError:
        foreign = True
    if (parsed.scheme not in ('', 'http', 'https') or foreign or parsed.username or parsed.password or parsed.query
            or parsed.fragment or not TOOL_PATH.fullmatch(parsed.path)):
        raise Problem('Only this server\'s ToolLocker is available.', 400, 'unsupported_parameter')
    allowed = tool.get('allowed_tools')
    if allowed is not None and (not isinstance(allowed, list) or len(allowed)>128 or any(not isinstance(name, str) or not name or len(name)>128 for name in allowed)):
        raise Problem('allowed_tools must be a list of at most 128 tool names.')
    approval = tool.get('require_approval', 'never')
    if approval not in ('never', 'always'):
        if (not isinstance(approval, dict) or len(approval) != 1 or next(iter(approval)) not in ('never', 'always')
                or not isinstance(next(iter(approval.values())), dict) or set(next(iter(approval.values()))) != {'tool_names'}):
            raise Problem('Use never, always, or an approval filter with tool_names.')
        names = next(iter(approval.values()))['tool_names']
        if not isinstance(names, list) or len(names)>128 or any(not isinstance(name, str) or not name or len(name)>128 for name in names):
            raise Problem('Approval tool_names must be a bounded list of names.')
    return {**tool, 'path': parsed.path, 'require_approval': approval}


def approval_needed(policy, name):
    """Apply the client's exact all/none/named-tool approval policy."""
    if isinstance(policy, str):
        return policy == 'always'
    if 'always' in policy:
        return name in policy['always']['tool_names']
    return name not in policy['never']['tool_names']


def exact_result(result):
    """Keep every MCP content part, error flag and extra field in one exact JSON representation."""
    return json.dumps(result, ensure_ascii=False, separators=(',', ':'))


def model_result(result):
    """Bound only the model-facing copy; the exact tool result remains separately retained."""
    raw = exact_result(result).encode()
    if len(raw) <= MODEL_RESULT_BYTES:
        return raw.decode()
    return raw[:MODEL_RESULT_BYTES].decode(errors='ignore') + '\n[Tool result truncated to 64 KiB for the model; full output is retained.]'


class ToolClient:
    """Use the caller's current key on every list/call through the existing control node MCP route."""
    def __init__(self, http, url, authorization, trace, level='normal', level_reader=None):
        """Bind one caller, trace and tool set without adding a listener or service credential."""
        self.http, self.url, self.authorization, self.trace = http, str(url), authorization, trace
        self.level, self.level_reader = level, level_reader
        self.session, self.number, self.tools = '', 0, []

    def current_level(self):
        """Read ToolLocker's authoritative raised trace level before each subsequent model call."""
        raised = self.level_reader(self.trace) if self.level_reader else self.level
        if raised in LEVELS:
            self.level = max(self.level, raised, key=LEVELS.index)
        return self.level

    async def rpc(self, method, params=None):
        """Send authenticated JSON-RPC and retain the owning MCP session across calls."""
        self.number += 1
        headers = {'authorization': self.authorization, 'x-loadcoach-trace': self.trace,
                   'x-loadcoach-sensitivity': self.current_level(), 'accept': 'application/json'}
        if self.session:
            headers['mcp-session-id'] = self.session
        try:
            answer = await self.http.post(self.url, json={'jsonrpc': '2.0', 'id': self.number, 'method': method, 'params': params or {}},
                                          headers=headers, timeout=130)
            if not answer.is_success:
                raise Problem('ToolLocker is unavailable (HTTP '+str(answer.status_code)+').', answer.status_code, 'toollocker_unavailable')
            body = answer.json()
        except httpx.HTTPError:
            raise Problem('ToolLocker is not answering.', 503, 'toollocker_unavailable') from None
        except ValueError:
            raise Problem('ToolLocker returned invalid JSON.', 502, 'invalid_tool_response') from None
        if not isinstance(body, dict) or body.get('id') != self.number:
            raise Problem('ToolLocker returned an invalid JSON-RPC envelope.', 502, 'invalid_tool_response')
        if body.get('error'):
            error = body['error']
            raise Problem(error.get('message', 'ToolLocker refused this request.') if isinstance(error, dict) else str(error),
                          400, 'tool_refused')
        if not isinstance(body.get('result'), dict):
            raise Problem('ToolLocker returned an invalid result.', 502, 'invalid_tool_response')
        self.session = answer.headers.get('mcp-session-id', self.session)
        return body['result']

    async def listing(self, allowed=None):
        """Initialize and list only tools authorized by ToolLocker, then narrow that list."""
        await self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                      'clientInfo': {'name': 'openWeightGym tool loop', 'version': '1'}})
        tools, cursor, seen = [], '', set()
        while True:
            result = await self.rpc('tools/list', {'cursor': cursor} if cursor else {})
            page = result.get('tools')
            if not isinstance(page, list) or any(not isinstance(tool, dict) or not isinstance(tool.get('name'), str) for tool in page):
                raise Problem('ToolLocker returned an invalid tool catalogue.', 502, 'invalid_tool_response')
            tools.extend(page)
            cursor = result.get('nextCursor')
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen or len(seen) >= 100:
                raise Problem('ToolLocker returned an invalid catalogue cursor.', 502, 'invalid_tool_response')
            seen.add(cursor)
        self.tools = [tool for tool in tools if allowed is None or tool['name'] in allowed]
        return self.tools

    def definitions(self):
        """Translate the caller-visible MCP schemas to function definitions without renaming tools."""
        return [{'type': 'function', 'function': {'name': tool['name'], 'description': tool.get('description', ''),
                'parameters': tool.get('inputSchema', {'type': 'object', 'properties': {}})}} for tool in self.tools]

    async def call(self, name, arguments):
        """Reauthorize a call at ToolLocker and keep failures as model-visible exact tool results."""
        try:
            if not isinstance(arguments, dict):
                raise Problem('Tool arguments must be a JSON object.', 400, 'invalid_tool_arguments')
            if name not in {tool['name'] for tool in self.tools}:
                raise Problem('This tool was not offered for this turn.', 400, 'tool_refused')
            result = await self.rpc('tools/call', {'name': name, 'arguments': arguments})
        except Problem as error:
            result = {'content': [{'type': 'text', 'text': str(error)}], 'isError': True,
                      'error': {'code': error.code, 'message': str(error)}}
        self.current_level()
        return result


class ToolLimit(Exception):
    """A turn exhausted its configured call or elapsed-time budget."""
    def __init__(self, reason):
        """Keep the exact Responses incomplete reason for either shared limit."""
        self.reason = reason
        super().__init__(reason)


class ToolTurn:
    """Run normal model requests and caller-key tools with automatic or pending approvals."""
    def __init__(self, client, model_name, *, hosted=False, max_calls=25, max_seconds=900, calls=0, started=None):
        """Freeze one model turn's limits; the console can retain counters across approval requests."""
        self.client, self.model_name, self.hosted = client, model_name, hosted
        self.max_calls, self.max_seconds = max_calls, max_seconds
        self.calls, self.started = calls, time.monotonic() if started is None else started

    def remaining(self):
        """Enforce elapsed time across model admission, execution, calls and approval waits."""
        left = self.max_seconds - (time.monotonic()-self.started)
        if left <= 0:
            raise ToolLimit('max_tool_time')
        return left

    def account_call(self):
        """Count proposals as well as executions so approval modes cannot bypass the limit."""
        self.remaining()
        if self.calls >= self.max_calls:
            raise ToolLimit('max_tool_calls')
        self.calls += 1

    async def model(self, callback, messages, definitions):
        """Stop raised tool results before they can be sent to a chosen hosted model."""
        level = self.client.current_level()
        if self.hosted and level != 'normal':
            raise Problem(f'A tool result is marked {level} and may not be sent to the hosted model {self.model_name}.',
                          403, 'tool_result_too_sensitive')
        try:
            async with asyncio.timeout(self.remaining()):
                return await callback(messages, definitions)
        except TimeoutError:
            raise ToolLimit('max_tool_time') from None

    async def execute(self, call, *, count=True):
        """Run exactly one proposed call, converting malformed arguments to visible failures."""
        if count:
            self.account_call()
        try:
            arguments = json.loads(call['function']['arguments'])
        except (ValueError, KeyError, TypeError):
            arguments = None
        try:
            async with asyncio.timeout(self.remaining()):
                return await self.client.call(call['function']['name'], arguments)
        except TimeoutError:
            raise ToolLimit('max_tool_time') from None

    async def run(self, messages, definitions, model, *, client_names=(), approve=None):
        """Yield model/proposal/result steps; pending approvals and client functions end this run."""
        while True:
            result = await self.model(model, messages, definitions)
            yield {'type': 'model', 'result': result}
            try:
                choice = result['choices'][0]
                message = choice['message']
                calls = message.get('tool_calls') or []
            except (KeyError, TypeError, IndexError, AttributeError):
                raise Problem('Model returned invalid tool-loop output.', 502, 'invalid_upstream_response') from None
            if not calls or choice.get('finish_reason') not in ('stop', 'tool_calls', 'function_call'):
                return
            messages.append(message)
            pending = False
            for call in calls:
                self.account_call()
                name = call['function']['name']
                if name in client_names:
                    yield {'type': 'client_call', 'call': call}
                    pending = True
                    continue
                decision = await approve(call) if approve else True
                if decision is None:
                    yield {'type': 'approval', 'call': call}
                    pending = True
                    continue
                result = (await self.execute(call, count=False) if decision is True else
                          {'content': [{'type': 'text', 'text': str(decision) if decision else 'Tool call declined.'}], 'isError': True})
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': model_result(result)})
                yield {'type': 'tool_result', 'call': call, 'result': result}
            if pending:
                return
