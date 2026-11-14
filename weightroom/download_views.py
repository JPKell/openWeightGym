"""Per-node write-only provider controls, using the existing node proxy and shared forms."""
from urllib.parse import quote

from starlette.responses import Response
from starlette.routing import Route

from loadcoach.download_keys import contracts


async def context(ask, person, choices, machine, refused=''):
    """Read fresh presence metadata; an unavailable node never becomes an empty credential."""
    selected = next((row for row in choices if row['name'] == machine), None)
    problem, status = '', {}
    if selected:
        reply = await ask('GET', '/turnstile/machines/' + quote(machine, safe='') + '/download-credentials', person.key)
        if reply.is_success:
            status = reply.json().get('credentials', {})
        else:
            problem = 'Download credentials are unavailable on this node. Check that it is running the current version, then retry.'
    else:
        problem = 'Select a configured node to manage its download credentials.'
    return {'download_credentials': contracts(),
            'download_status': {entry.name: {**status.get(entry.name, {'saved': False, 'changed': None, 'unavailable': True}),
                                            'unavailable': bool(status.get(entry.name, {}).get('refused')) or entry.name not in status,
                                            'refused': entry.name == refused} for entry in contracts()},
            'download_available': bool(selected and not problem), 'download_problem': problem,
            'download_choices': choices}


def routes(for_people, typed, go, ask, render):
    """Only settings administrators may submit credentials, directly to the chosen node."""
    async def credential(request, person):
        if 'manage-settings' not in person.can:
            return Response('Download credentials require manage-settings.', 403)
        machine, kind = request.path_params['machine'], request.path_params['kind']
        if kind not in {entry.name for entry in contracts()}:
            return Response('Choose a download credential.', 404)
        form = await typed(request)
        answer = await ask('POST', '/turnstile/machines/' + quote(machine, safe='') + '/download-credentials', person.key,
                           json={'kind': kind, 'operation': form.get('operation', 'save'), 'credential': form.get('credential', '')})
        if not answer.is_success:
            request.state.download_refused = kind
            why = answer.json().get('error', {}).get('message', 'The node could not save this credential.')
            return await render(request, person, why, True, typed_in={'machine': machine})
        message = 'Credential removed.' if form.get('operation') == 'remove' else 'Credential saved. Blank fields keep the current value.'
        return go('/settings/downloads?machine=' + quote(machine, safe='') + '&said=' + quote(message))
    return [Route('/settings/downloads/{machine}/credentials/{kind}', for_people(credential), methods=['POST'])]
