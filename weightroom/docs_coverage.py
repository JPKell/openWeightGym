"""One coverage report for the documentation test and command-line check."""

import ast

from weightroom import docs_reference, system_docs


def report():
    """List uncovered navigation pages and source descriptions, using stable gap identities."""
    from weightroom.navigation import NAV_SECTIONS
    paths = {path for category, groups in NAV_SECTIONS.items()
             for _, routes in groups for path in routes}
    covered = {path for section in system_docs.SECTIONS for topic in (section, *section['topics'])
               for path in topic.get('covers', ())}
    gaps = {'page:' + path for path in paths - covered}
    emitted = {(group['path'], row['method'], row['path']): row
               for group in system_docs.api_reference(system_docs.source_files('api')) for row in group['rows']}
    endpoints = set()
    for relative in system_docs.source_files('api'):
        path = system_docs.ROOT / relative
        stat = path.stat()
        for row in system_docs._api_source(relative, stat.st_mtime_ns, stat.st_size):
            if row['path'].startswith(('/v1/', '/owg/')):
                identity = (relative, row['method'], row['path'])
                endpoints.add(identity)
                if identity not in emitted:
                    gaps.add('endpoint:' + ':'.join(identity) + ':inventory')
                if not emitted.get(identity, row)['summary']:
                    gaps.add('endpoint:' + ':'.join(identity) + ':summary')
    # Inspect literal declarations independently: a reader bug cannot silently remove a route.
    for folder in ('loadcoach', 'turnstile', 'weightroom', 'mirrorwall', 'soundroom', 'toollocker', 'voice', 'plugins'):
        for path in (system_docs.ROOT / folder).rglob('*.py'):
            if 'vendor' in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ('Route', 'route', 'WebSocketRoute') and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                        and node.args[0].value.startswith(('/v1/', '/owg/'))
                        and node.args[0].value != '/v1/{path:path}'):
                    relative = path.relative_to(system_docs.ROOT).as_posix()
                    if not any(item[0] == relative and item[2] == node.args[0].value for item in emitted):
                        gaps.add('route:' + relative + ':' + node.args[0].value)
    abilities = docs_reference.abilities()
    gaps.update('ability:' + row['name'] for row in abilities if not row['description'].strip())
    settings = docs_reference.settings()
    gaps.update('setting:' + row['program'] + ':' + row['key'] for row in settings if not row['meaning'].strip())
    return {'pages': len(paths), 'covered_pages': len(paths & covered), 'endpoints': len(endpoints),
            'abilities': len(abilities), 'settings': len(settings), 'gaps': sorted(gaps)}
