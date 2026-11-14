"""Secret-free settings history and portable, additive override transfers."""
import json

from loadcoach.setting_catalog import get, validate
from loadcoach.settings import redacted

MAX_BYTES = 65536


def transferable(program, name, value):
    entry = get(program, name)
    return bool(entry and entry.tier != 'file' and entry.kind != 'secret' and not entry.retired
                and not entry.derived and redacted(value, name) == value and '(not shown)' not in json.dumps(value))


def export_rows(node, programs):
    return [{'node':node, 'program':program['program'], 'name':row['name'], 'value':row['changed']['value']}
            for program in programs for row in program['settings'] if row.get('changed')
            and not row.get('retired') and not row.get('derived')
            and transferable(program['program'], row['name'], row['changed']['value'])]


def parse(text, nodes, *, retired=()):
    """Validate every imported row before any service is asked to write an override."""
    if len(text.encode()) > MAX_BYTES:
        raise ValueError('Settings imports may be at most 64 KiB.')
    document = json.loads(text)
    if not isinstance(document, dict) or set(document) != {'version','overrides'} or type(document['version']) is not int or document['version'] != 1 or not isinstance(document['overrides'], list):
        raise ValueError('Use a version 1 settings export with an overrides list.')
    if len(document['overrides']) > 1000:
        raise ValueError('Import at most 1,000 settings at a time.')
    groups, ignored, seen = {}, [], set()
    for row in document['overrides']:
        if not isinstance(row, dict) or set(row) != {'node','program','name','value'}:
            raise ValueError('Each override needs node, program, name and value.')
        node, program, name, value = (row[key] for key in ('node','program','name','value'))
        if not all(isinstance(item, str) for item in (node, program, name)):
            raise ValueError('Node, program and setting names must be text.')
        if node not in nodes or program not in ({'weightroom','turnstile','toollocker','voice-gateway'} if node == 'front' else {'loadcoach','spotter','voice','machines'}):
            raise ValueError('Choose a configured node and a program owned by that node.')
        identity = node, program, name
        if identity in seen:
            raise ValueError('An import may name each setting only once.')
        seen.add(identity)
        entry = get(program, name)
        if entry and (entry.retired or entry.derived or identity in retired):
            ignored.append({'node':node, 'program':program, 'name':name})
            continue
        if not transferable(program, name, value):
            raise ValueError(f'{program}.{name} cannot be imported: only editable, non-secret settings are transferable.')
        if value is not None:
            validate(program, name, value)
        groups.setdefault((node,program), {})[name] = value
    return groups, ignored


def history_rows(events):
    """Expose only catalogued settings; old logs without prior values remain viewable."""
    rows = []
    for event in events:
        if event.get('kind') != 'settings' or event.get('what') != 'changed':
            continue
        program, node = event.get('program', ''), event.get('machine') or 'front'
        if program.startswith('machines:'):
            node, program = program.removeprefix('machines:'), 'machines'
        if program == 'model-files':
            program = 'turnstile' if node == 'front' else 'loadcoach'
        old = event.get('old')
        for name, value in event.items():
            entry = get(program, name)
            if not entry:
                continue
            before = old.get(name) if isinstance(old, dict) else None
            rows.append({'id':event['id'], 'at':event['at'], 'user':event.get('user',''),
                         'node':node, 'program':program, 'name':name,
                         'old':'(not shown)' if entry.kind == 'secret' else redacted(before, name),
                         'new':'(not shown)' if entry.kind == 'secret' else redacted(value, name),
                         'old_known':isinstance(old, dict) and name in old,
                         'can_revert':isinstance(old, dict) and name in old and transferable(program, name, before)})
    return rows
