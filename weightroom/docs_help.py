"""Native and streamed Docs Ask presentation over caller-key Responses requests."""

import json
import re
from urllib.parse import urlsplit

from markupsafe import Markup
from starlette.responses import Response
from starlette.routing import Route

from turnstile.docs_help import MARKER, answer_text
from turnstile.responses import ClosingStream
from weightroom import system_docs
from weightroom.doc_markdown import render


def history_items(value):
    """Accept only bounded tab-supplied user/assistant text, never instructions or tool results."""
    try:
        if len(value) > 40000:
            raise ValueError
        rows = json.loads(value or '[]')
        if not isinstance(rows, list) or len(rows) > 12 or any(
                not isinstance(row, dict) or row.keys() != {'role', 'content'} or
                row['role'] not in ('user', 'assistant') or not isinstance(row['content'], str) or
                len(row['content']) > 6000 for row in rows):
            raise ValueError
        while sum(len(row['content']) for row in rows) > 16000:
            rows.pop(0)
        return rows
    except (ValueError, TypeError, KeyError):
        raise ValueError('The tab conversation is invalid. Clear the conversation and try again.') from None


def presentation(text, person):
    """Render escaped Markdown and reuse library result rows for permitted local citations."""
    visible = {item['url']: item for section in system_docs.visible(person.can) for item in (section, *section['topics'])}
    def link(url):
        """Keep only visible canonical local docs links; never turn model text into an external action."""
        parsed = urlsplit(url)
        return url if not parsed.scheme and not parsed.netloc and parsed.path in visible else None
    text = text.replace(MARKER, '').strip()[:20000]
    sources, seen = [], set()
    for url in re.findall(r'\]\(([^\s)]+)\)', text):
        if link(url) and url not in seen:
            seen.add(url)
            item = visible[urlsplit(url).path]
            sources.append({'url': url, 'title': item['title'], 'category': 'Source', 'summary': item['summary'], 'excerpt': ''})
    return render(text, 'docs-answer', links=link), sources


def error_message(answer):
    """Explain expected help failures without exposing provider diagnostics or private source data."""
    try:
        error = answer.json().get('error') or {}
    except (ValueError, AttributeError):
        error = {}
    code = error.get('code') if isinstance(error, dict) else ''
    if code == 'docs_role_missing':
        return 'The docs-help role is not configured. Ask an administrator to set it in Roles.'
    if code in ('toollocker_unavailable', 'invalid_tool_response'):
        return 'ToolLocker is not answering. Use Search while it is unavailable.'
    if code in ('docs_tools_unavailable', 'tools_not_allowed'):
        return 'This account cannot use the required documentation tools. Use Search or ask an administrator.'
    if answer.status_code == 429 or 'allowance' in str(code) or 'spending' in str(code):
        return 'The request was refused by allowance or capacity policy. Check your usage or ask an administrator, then retry.'
    if answer.status_code == 403:
        return 'The help model is unavailable to this account. Ask an administrator to check its model access and allowance.'
    return 'The help model could not finish the answer. Use Search or try again.'


def routes(for_people, page, ask, http, environment, unavailable, typed):
    """Register private browser forms before the generic documentation topic routes."""
    def fragment(person, text='', error='', history=()):
        """Use the same safe answer/citation macro for native, HTMX and streaming completion."""
        html, sources = presentation(text, person)
        return environment.get_template('docs_help_answer.html').render(
            answer_html=html, sources=sources, help_error=error, help_text=text.replace(MARKER, '').strip(),
            help_history=list(history))

    async def ask_page(request, person):
        """Answer a Docs question under the browser caller, with tab-only history and Search fallback."""
        context = {'help_mode': 'ask', 'question': '', 'help_history': [], 'answer_fragment': Markup(fragment(person)),
                   'help_error': '', 'role_missing': False}
        if 'use-tools' not in person.can:
            return page(request, 'docs_help.html', person, status=403, **context)
        if request.method == 'GET':
            try:
                status = await ask('GET', '/turnstile/docs/status', person.key, timeout=10)
                context['role_missing'] = status.is_success and not status.json().get('configured')
                if not status.is_success:
                    context['help_error'] = 'Help readiness is unavailable. You can still use Search.'
            except (http_error_types() + (unavailable,)):
                context['help_error'] = 'Help readiness is unavailable. You can still use Search.'
            return page(request, 'docs_help.html', person, **context)
        form = await typed(request)
        question = str(form.get('question', '')).strip()
        try:
            history = history_items(str(form.get('history', '[]')))
            if not question or len(question) > 2000:
                raise ValueError('Enter a question of at most 2,000 characters.')
        except ValueError as error:
            context.update(question=question[:2000], help_error=str(error))
            return page(request, 'docs_help.html', person, status=400, **context)
        body = {'model': 'docs-help', 'input': [*history, {'role': 'user', 'content': question}], 'store': False,
                'max_output_tokens': 2048}
        context.update(question=question, help_history=history)
        if 'text/event-stream' in request.headers.get('accept', ''):
            body['stream'] = True
            upstream = http.stream('POST', '/v1/responses', json=body, timeout=None,
                headers={'authorization': 'Bearer ' + person.key, 'x-turnstile-no-wake': '1'})
            try:
                answer = await upstream.__aenter__()
            except http_error_types():
                return Response(fragment(person, error='The model gateway is unavailable. Use Search.'), 503,
                                media_type='text/html', headers={'cache-control': 'no-store'})
            if not answer.is_success:
                await answer.aread()
                await upstream.__aexit__(None, None, None)
                return Response(fragment(person, error=error_message(answer)), answer.status_code,
                                media_type='text/html', headers={'cache-control': 'no-store'})
            async def content():
                """Stream safe progress text then one shared rendered HTMX answer fragment."""
                buffer, text, result = '', '', None
                try:
                    async for line in answer.aiter_lines():
                        if line.startswith('data: '):
                            buffer += line[6:]
                            if len(buffer) > 2**20:
                                raise ValueError('Oversized model event.')
                        elif not line and buffer:
                            event, buffer = json.loads(buffer), ''
                            if event.get('type') == 'response.output_text.delta':
                                text += event.get('delta', '')
                                yield 'event: progress\ndata: ' + json.dumps({'text': text.replace(MARKER, '')[:20000]}) + '\n\n'
                            elif event.get('type') in ('response.completed', 'response.incomplete', 'response.failed'):
                                result = event['response']
                    completed = result is not None and result.get('status') == 'completed'
                    text = answer_text(result) if result else ''
                    error = '' if completed and text.strip() else (
                        'The model returned an empty answer. Use Search or try again.' if completed else
                        'The help model could not finish the answer. Use Search or try again.')
                    rows = [*history, {'role': 'user', 'content': question}, {'role': 'assistant', 'content': text[:6000]}] if not error else history
                    yield 'event: fragment\ndata: ' + json.dumps({'html': fragment(person, text, error, rows[-12:])}) + '\n\n'
                except (http_error_types() + (ValueError, KeyError, TypeError)):
                    yield 'event: fragment\ndata: ' + json.dumps({'html': fragment(person, error='The answer stream was interrupted. Use Search or retry.')}) + '\n\n'
            return ClosingStream(content(), upstream, media_type='text/event-stream', headers={'cache-control': 'no-store', 'x-accel-buffering': 'no'})
        try:
            answer = await ask('POST', '/v1/responses', person.key, json=body, timeout=None)
            result = answer.json() if answer.is_success else {}
            text = answer_text(result)
            error = error_message(answer) if not answer.is_success else (
                'The help model could not finish the answer. Use Search or try again.' if result.get('status') != 'completed' else
                '' if text.strip() else 'The model returned an empty answer. Use Search or try again.')
        except (http_error_types() + (unavailable, ValueError, KeyError, TypeError)):
            text, error = '', 'The model gateway is unavailable. Use Search or try again.'
        rows = [*history, {'role': 'user', 'content': question}, {'role': 'assistant', 'content': text[:6000]}] if not error else history
        html = fragment(person, text, error, rows[-12:])
        if request.headers.get('hx-request') == 'true':
            return Response(html, media_type='text/html', headers={'cache-control': 'no-store'})
        context.update(answer_fragment=Markup(html), help_history=rows[-12:])
        return page(request, 'docs_help.html', person, **context)

    async def gaps_page(request, person):
        """Show or dismiss the control-node question-only list for settings administrators."""
        if 'manage-settings' not in person.can:
            return Response('Requires manage-settings.', 403)
        error, gaps = '', []
        try:
            if request.method == 'POST':
                form = await typed(request)
                answer = await ask('POST', '/turnstile/docs/gaps', person.key, json={'dismiss': str(form.get('dismiss', ''))})
            else:
                answer = await ask('GET', '/turnstile/docs/gaps', person.key)
            if answer.is_success:
                gaps = answer.json()['gaps']
            else:
                error = 'Documentation gaps are temporarily unavailable.'
        except (http_error_types() + (unavailable, ValueError, KeyError)):
            error = 'Documentation gaps are temporarily unavailable.'
        if request.headers.get('hx-request') == 'true':
            return Response(environment.get_template('docs_gaps.html').render(gaps=gaps, help_error=error),
                            media_type='text/html', headers={'cache-control': 'no-store'})
        return page(request, 'docs_help.html', person, help_mode='gaps', gaps=gaps, help_error=error)
    return [Route('/docs/ask', for_people(ask_page), methods=['GET', 'POST']),
            Route('/docs/gaps', for_people(gaps_page), methods=['GET', 'POST'])]


def http_error_types():
    """Keep httpx transport failures within the existing console fallback presentation."""
    import httpx
    return (httpx.HTTPError,)
