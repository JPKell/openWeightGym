"""One tool-use permission rule shared by every model admission path."""

from starlette.responses import JSONResponse

TOOLS_NOT_ALLOWED = 'Tool use is not allowed for this account. Ask an administrator for the use-tools ability.'


def wants_tools(body):
    """Recognize definitions, choices and tool history in chat and Responses requests."""
    if not isinstance(body, dict):
        return False
    if body.get('tools') or 'tool_choice' in body or body.get('functions') or 'function_call' in body:
        return True
    for field in ('messages', 'input'):
        items = body.get(field)
        if not isinstance(items, list):
            continue
        if any(isinstance(item, dict) and (item.get('tool_calls') or item.get('function_call') or
               item.get('role') in ('tool', 'function') or item.get('type') in
               ('function_call', 'function_call_output', 'mcp_approval_response')) for item in items):
            return True
    return False


def tools_refusal(user, body):
    """Return an explicit private 403 instead of silently removing a caller's tools."""
    if wants_tools(body) and 'use-tools' not in user.group.can:
        return JSONResponse({'error': {'message': TOOLS_NOT_ALLOWED, 'type': 'invalid_request_error', 'code': 'tools_not_allowed'}},
                            status_code=403, headers={'cache-control': 'private, no-store', 'x-content-type-options': 'nosniff'})
    return None
