"""Settings of the front's own programs (the console), kept on the front, behind the existing settings HTTP contract."""

from starlette.responses import JSONResponse

from loadcoach.settings import CHANGEABLE, Changes, FRONT_PROGRAMS as PROGRAMS, program_name, wrong_kind
from loadcoach.tasks import error

class ConsoleSettings:
    """Live settings of the front's own programs and their compatibility endpoints.

    `enabled` is keep_console_settings_on_front: when false these requests go on to compute as before.
    """
    def __init__(self, source, log, ask, enabled=False, *, programs=PROGRAMS):
        """Bind the console's program settings and their authenticated management API."""
        self.changes, self.log, self.ask, self.enabled = Changes(source), log, ask, enabled
        self.programs = programs

    async def handle(self, request, user):
        """Read or change the console's program settings through the shared settings contract."""
        if not self.enabled:
            return None
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
            remote = await self.ask(user, 'GET', path)
            programs = [one for one in remote.json().get('programs', []) if one['program'] not in self.programs] if remote is not None and remote.status_code == 200 else []
            programs += [one for one in self.changes.everything() if one['program'] in self.programs]
            return JSONResponse({'programs': sorted(programs, key=lambda one: one['program']),
                                 'compute_unavailable': remote is None or remote.status_code != 200})
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
                if value is not None and name in current and (why := wrong_kind(value, current[name])):
                    raise ValueError(f'{name}: {why}')
                if name == 'component_versions_every' and value is not None:
                    from loadcoach.component_versions import interval_seconds
                    interval_seconds(value)
                if program == 'turnstile' and name == 'image_adult_previews' and value is not None and type(value) is not bool:
                    raise ValueError('image_adult_previews must be true or false.')
                if program == 'weightroom' and name == 'alert_gpu_temperature' and value is not None and (type(value) not in (int, float) or not 50 <= value <= 110):
                    raise ValueError('alert_gpu_temperature must be from 50 through 110 degrees Celsius.')
                if program == 'weightroom' and value is not None:
                    from loadcoach.observability import ALERT_LIMITS, alert_value
                    if name in ALERT_LIMITS:
                        alert_value(name, value)
            # Changes.put/take_away commit separately: validate the entire request first.
            for name, value in body.items():
                self.changes.take_away(program, name) if value is None else self.changes.put(program, name, value, user.name)
            self.log.add('settings', 'changed', user.name, program=program, **body)
            return JSONResponse({'programs': [one for one in self.changes.everything() if one['program'] in self.programs]})
        except (ValueError, TypeError) as problem:
            return error(400, str(problem))
