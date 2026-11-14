"""Settings of the control node's own programs (the console), kept on the control node, behind the existing settings HTTP contract."""

from starlette.responses import JSONResponse

from loadcoach.settings import CHANGEABLE, Changes, FRONT_PROGRAMS as PROGRAMS, live, program_name
from loadcoach.setting_catalog import validate_patch
from loadcoach.tasks import error

class ConsoleSettings:
    """Live settings of the control node's own programs and their compatibility endpoints.

    Console settings remain available independently of compute-node availability.
    """
    def __init__(self, source, log, ask, enabled=True, *, programs=PROGRAMS, voice_nodes=lambda: (), defaults=None):
        """Bind the console's program settings and their authenticated management API."""
        self.changes, self.log, self.ask, self.enabled = Changes(source), log, ask, enabled
        self.programs = programs
        self.voice_nodes = voice_nodes
        self.defaults = defaults or {}

    def live(self, program, name, fallback):
        """A validated read bound to this control node's settings authority."""
        return live(program, name, self.defaults.get(program, {}).get(name, fallback), changes=self.changes)

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
                return error(404, 'This program has not reported its settings on the control node.')
            current = {one['name']: one['value'] for one in known['settings']}
            for name in body:
                if name not in CHANGEABLE[program]:
                    return error(403, 'This setting belongs in the program file.')
            # A reset validates the effective file fallback as part of the same POST.
            defaults = {row['name']: row.get('file', row['value']) for row in known['settings']}
            effective = {**current, **self.changes.of(program),
                         **{name: defaults.get(name) for name, value in body.items() if value is None}}
            validate_patch(program, effective, body, context={'voice_nodes': self.voice_nodes})
            # Changes.put/take_away commit separately: validate the entire request first.
            for name, value in body.items():
                self.changes.take_away(program, name) if value is None else self.changes.put(program, name, value, user.name)
            self.log.add('settings', 'changed', user.name, program=program,
                         **{name: '(telephone settings updated)' if name == 'telephone' else value for name, value in body.items()})
            return JSONResponse({'programs': [one for one in self.changes.everything() if one['program'] in self.programs]})
        except (ValueError, TypeError) as problem:
            return error(400, str(problem))
