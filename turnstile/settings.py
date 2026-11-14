"""Settings of the front's own programs (the console), kept on the front, behind the existing settings HTTP contract."""

from starlette.responses import JSONResponse

from loadcoach.settings import CHANGEABLE, Changes, FRONT_PROGRAMS as PROGRAMS, program_name, wrong_kind
from loadcoach.platform_services import SERVICE_SETTINGS
from loadcoach.tasks import error

class ConsoleSettings:
    """Live settings of the front's own programs and their compatibility endpoints.

    Console settings remain available independently of compute-node availability.
    """
    def __init__(self, source, log, ask, enabled=True, *, programs=PROGRAMS, voice_nodes=lambda: ()):
        """Bind the console's program settings and their authenticated management API."""
        self.changes, self.log, self.ask, self.enabled = Changes(source), log, ask, enabled
        self.programs = programs
        self.voice_nodes = voice_nodes

    async def handle(self, request, user):
        """Read or change the console's program settings through the shared settings contract."""
        path = request.url.path
        if path == '/loadcoach/tools' and request.method == 'GET' and 'toollocker' in self.programs:
            if not user or not user.can('manage-settings'):
                return error(403, 'Tools require manage-settings.')
            import json
            known = next((one for one in self.changes.everything() if one['program'] == 'toollocker'), None)
            db = self.changes.db
            schemas = ([{'name': name, 'schema': json.loads(schema)} for name, schema in
                        db.execute('SELECT tool,schema FROM tool_schemas ORDER BY tool')]
                       if 'tool_schemas' in db.database.tables(db) else [])
            return JSONResponse({'program': known, 'tools': schemas})
        if path == '/loadcoach/settings' and request.method == 'GET':
            if not user or not user.can('manage-settings'):
                return error(403, 'Settings require manage-settings.')
            programs = [one for one in self.changes.everything() if one['program'] in self.programs]
            for program in programs:
                for setting in program['settings']:
                    if program['program'] == 'turnstile' and setting['name'] == 'preferred_voice_node':
                        setting.update(select=True, editor='text', choices=[('', 'Automatic'), *((name, name) for name in self.voice_nodes())])
            return JSONResponse({'programs': sorted(programs, key=lambda one: one['program']), 'compute_unavailable': False})
        parts = path.split('/')
        if len(parts) not in (4, 5) or parts[1:3] != ['loadcoach', 'settings']:
            return None
        program = program_name(parts[3])
        if program not in self.programs:
            return None
        if not user:
            return error(401, 'Missing or wrong API key.')
        if len(parts) == 5 and parts[4] == 'changes' and request.method == 'GET':
            if not user.can('manage-settings') and not user.can('follow-settings'):
                return error(403, 'Settings followers require follow-settings.')
            return JSONResponse(self.changes.of(program))
        import json
        try:
            raw = await request.body()
            if len(raw) > 65536:
                raise ValueError('Settings requests may be at most 64 KiB.')
            body = json.loads(raw or b'{}')
            if not isinstance(body, dict):
                raise ValueError('Send a settings object.')
            if len(parts) == 5 and parts[4] == 'seen' and request.method == 'POST':
                if not user.can('follow-settings'):
                    return error(403, 'Settings followers require follow-settings.')
                if set(body) - {'told', 'refused'} or not isinstance(body.get('told'), dict) or not isinstance(body.get('refused', {}), dict):
                    raise ValueError('told and refused must be objects.')
                self.changes.seen(program, body['told'], body.get('refused'))
                return JSONResponse({})
            if len(parts) != 4 or request.method != 'POST':
                return error(405, 'Unsupported settings operation.')
            if not user.can('manage-settings'):
                return error(403, 'Settings require manage-settings.')
            known = next((one for one in self.changes.everything() if one['program'] == program), None)
            if known is None:
                return error(404, 'This program has not reported its settings on front.')
            current = {one['name']: one['value'] for one in known['settings']}
            if program == 'voice-gateway' and body.get('telephone') is not None:
                from voice.telephone_settings import validate
                validate(body['telephone'])
            if program == 'turnstile' and {'models_folder', 'models_storage_folder'} & body.keys():
                from turnstile.artifacts import checked_roots
                effective = {**current, **self.changes.of(program), **body}
                for name, value in body.items():
                    if value is None:
                        entry = next((row for row in known['settings'] if row['name'] == name), {})
                        effective[name] = entry.get('file', '')
                checked_roots({name: effective.get(name, '') for name in ('models_folder', 'models_storage_folder')})
            for name, value in body.items():
                if name not in CHANGEABLE[program]:
                    return error(403, 'This setting belongs in the program file.')
                if program == 'weightroom' and name in SERVICE_SETTINGS and value is not None and type(value) is not bool:
                    raise ValueError(f'{name} must be true or false.')
                if value is not None and name in current and (why := wrong_kind(value, current[name])):
                    raise ValueError(f'{name}: {why}')
                if program == 'turnstile' and name == 'response_max_tool_calls' and value is not None and (type(value) is not int or not 1 <= value <= 10000):
                    raise ValueError('response_max_tool_calls must be from 1 to 10000.')
                if program == 'turnstile' and name == 'response_max_tool_seconds' and value is not None and (type(value) not in (int, float) or not 0 < value <= 86400):
                    raise ValueError('response_max_tool_seconds must be above zero and at most 86400.')
                if program == 'turnstile' and name == 'response_keep_days' and value is not None and (type(value) not in (int, float) or not 0 < value <= 3650):
                    raise ValueError('response_keep_days must be above zero and at most 3650 days.')
                if name == 'component_versions_every' and value is not None:
                    from loadcoach.component_versions import interval_seconds
                    interval_seconds(value)
                if program == 'turnstile' and name == 'image_adult_previews' and value is not None and type(value) is not bool:
                    raise ValueError('image_adult_previews must be true or false.')
                if program == 'turnstile' and name == 'preferred_voice_node' and value and value not in self.voice_nodes():
                    raise ValueError('Choose a node that reports Voice installed, or Automatic.')
                if program == 'weightroom' and name == 'alert_gpu_temperature' and value is not None and (type(value) not in (int, float) or not 50 <= value <= 110):
                    raise ValueError('alert_gpu_temperature must be from 50 through 110 degrees Celsius.')
                if program == 'weightroom' and value is not None:
                    from loadcoach.observability import ALERT_LIMITS, alert_value
                    if name in ALERT_LIMITS:
                        alert_value(name, value)
            # Changes.put/take_away commit separately: validate the entire request first.
            for name, value in body.items():
                self.changes.take_away(program, name) if value is None else self.changes.put(program, name, value, user.name)
            self.log.add('settings', 'changed', user.name, program=program,
                         **{name: '(telephone settings updated)' if name == 'telephone' else value for name, value in body.items()})
            return JSONResponse({'programs': [one for one in self.changes.everything() if one['program'] in self.programs]})
        except (ValueError, TypeError) as problem:
            return error(400, str(problem))
