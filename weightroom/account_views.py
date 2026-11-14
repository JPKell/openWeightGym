"""Personal settings desk within the existing console navigation."""

import json
import time
from urllib.parse import quote
from zoneinfo import available_timezones

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.preferences import DEFAULTS

FIELDS = {
    'appearance': ('show_telemetry', 'text_size', 'motion'),
    'workspace': ('start_page', 'chat_model', 'timezone', 'date_format', 'clock'),
    'privacy': ('alert_security', 'alert_spending', 'alert_jobs'),
}


def chat_models(person):
    """Match the chat composer's permitted text models and role strengths."""
    details = person.me.get('chat', {}).get('models', {})
    models = [name for name in person.me.get('models', []) if not any(
        details.get(name, {}).get(kind) for kind in ('image_generation', 'audio', 'classifier'))]
    return models + [role + strength for role in person.me.get('roles', []) for strength in ('', ':best', ':available', ':fast')]


def device_name(agent):
    """Recognizable coarse device labels; legacy sessions keep an honest fallback."""
    browser = next((label for token, label in [('Edg/', 'Edge'), ('Firefox/', 'Firefox'), ('Chrome/', 'Chrome'),
                                              ('Safari/', 'Safari')] if token in agent), 'Browser or application')
    system = next((label for token, label in [('Android', 'Android'), ('iPhone', 'iPhone'), ('iPad', 'iPad'),
                                              ('Windows', 'Windows'), ('Macintosh', 'macOS'), ('Linux', 'Linux')]
                   if token in agent), '')
    return browser + (' · ' + system if system else '')


def routes(for_people, page, ask, typed, go, platform, workflows):
    """Reuse authenticated account APIs and the established origin-checked form wrapper."""
    async def render(request, person, section=None, said='', failed=False):
        section = section or request.url.path.rsplit('/', 1)[-1]
        context = dict(me=person.me, said=said or request.query_params.get('said', ''), failed=failed)
        if section == 'workspace':
            context.update(zones=sorted(available_timezones()), default_models=chat_models(person),
                           destinations=[(path, label) for path, label in [('/status', 'Status'), ('/chat', 'Text chat'),
                                          ('/requests', 'My model requests')] if platform().path_enabled(path)])
            stored = person.me.get('choices', {}).get('start_page', '/status')
            if stored not in {path for path, _ in context['destinations']}:
                context['destinations'].append((stored, stored + ' · currently unavailable'))
        elif section == 'privacy':
            response = await feed(request, person)
            if response.status_code == 200:
                context['notices'] = json.loads(response.body)
            else:
                context.update(notices={'items': [], 'unread': 0}, notices_error='Your notices are temporarily unavailable. Retry shortly.')
        return page(request, 'account_' + section + '.html', person, status=400 if failed else 200, **context)

    async def save(request, person):
        section = request.path_params['section']
        if section not in FIELDS:
            return Response('Unknown preference section.', status_code=404)
        form = await typed(request)
        chosen = {key: (key in form if type(DEFAULTS[key]) is bool else form.get(key, DEFAULTS[key]))
                  for key in FIELDS[section]}
        if chosen.get('start_page') and not platform().path_enabled(chosen['start_page']):
            if chosen['start_page'] != person.me.get('choices', {}).get('start_page'):
                return await render(request, person, section, 'That starting page is disabled.', True)
            chosen.pop('start_page')
        allowed = chat_models(person)
        if chosen.get('chat_model') and chosen['chat_model'] not in allowed:
            if chosen['chat_model'] != person.me.get('choices', {}).get('chat_model'):
                return await render(request, person, section, 'Choose a model or role available to your account.', True)
            # Retain an unavailable stored default without granting access to it.
            chosen.pop('chat_model')
        answer = await ask('POST', '/loadcoach/me', person.key, json=chosen)
        if answer.status_code != 200:
            return await render(request, person, section, answer.json()['error']['message'], True)
        return go('/account/' + section + '?said=Preferences+saved.')

    async def end_session(request, person):
        identifier = request.path_params.get('identifier')
        answer = await ask('DELETE' if identifier else 'POST',
                           '/loadcoach/sessions/' + (quote(identifier, safe='') if identifier else 'end-others'), person.key)
        message = 'Sign-in ended.' if identifier else 'Other sign-ins ended.'
        if not answer.is_success:
            message = answer.json()['error']['message']
        return go('/account?' + ('failed=1&' if not answer.is_success else '') + 'said=' + quote(message))

    async def feed(request, person):
        answer = await ask('GET', '/loadcoach/account-notices', person.key)
        if not answer.is_success:
            return JSONResponse({'error': {'message': 'Notices are unavailable.'}}, status_code=503)
        data = answer.json()
        choices = person.me.get('choices', DEFAULTS)
        if choices.get('alert_jobs'):
            rows = workflows.store.db.execute("SELECT id,title,state,updated,level FROM workflows WHERE owner=? "
                                              "AND state IN ('completed','review_research','review_draft','failed_research','failed_draft') "
                                              "AND updated>=? ORDER BY updated DESC LIMIT 25",
                                              (person.me.get('identity', person.name), data['since'])).fetchall()
            for identifier, title, state, updated, level in rows:
                if workflows.knowledge.permitted(person.me, level):
                    label = {'completed':'Completed', 'review_research':'Research ready for review', 'review_draft':'Draft ready for review',
                             'failed_research':'Research failed', 'failed_draft':'Draft failed'}[state]
                    data['items'].append(dict(id='workflow-' + identifier, title=title + ' · ' + label, at=updated,
                                              category='Work', href='/workflows/' + quote(identifier, safe=''),
                                              unread=updated > choices.get('notices_read_at', 0)))
        data['items'].sort(key=lambda item: item['at'] or time.time(), reverse=True)
        data['items'] = data['items'][:50]
        data['unread'] = sum(item['unread'] for item in data['items'])
        return JSONResponse(data, headers={'cache-control': 'private, no-store'})

    async def read_notices(request, person):
        answer = await ask('POST', '/loadcoach/me', person.key, json={'notices_read_at': time.time()})
        if not answer.is_success:
            return await render(request, person, 'privacy', 'Could not mark notices as read. Retry shortly.', True)
        return go('/account/privacy?said=Notices+marked+as+read.')

    async def export(request, person):
        audio = await ask('GET', '/soundroom/preferences', person.key)
        if not audio.is_success:
            return Response('Audio preferences could not be exported. Retry shortly.', status_code=503)
        return JSONResponse({'version': 1, 'preferences': {key: value for key, value in person.me.get('choices', {}).items()
                                                          if key in DEFAULTS and key != 'notices_read_at'},
                             'audio': audio.json()}, headers={'content-disposition': 'attachment; filename="my-preferences.json"',
                                                             'cache-control': 'private, no-store'})

    return [Route('/account/' + section, for_people(render)) for section in ('keys', 'workspace', 'privacy')] + [
        Route('/account/preferences/{section}', for_people(save), methods=['POST']),
        Route('/account/sessions/{identifier}/remove', for_people(end_session), methods=['POST']),
        Route('/account/sessions/end-others', for_people(end_session), methods=['POST']),
        Route('/account/notices', for_people(feed)),
        Route('/account/notices/read', for_people(read_notices), methods=['POST']),
        Route('/account/preferences.json', for_people(export)),
    ]
