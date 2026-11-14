"""Reference facts read from declarations and shipped defaults, never configured values."""

import ast
import json
import re
import tomllib
from functools import lru_cache
from pathlib import Path


def revisions(paths):
    """Key generated references on every contributing source revision."""
    return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(paths))


@lru_cache(maxsize=8)
def _abilities(revision):
    """Read literal ability dictionaries without importing the console or its services."""
    result = {}
    for relative, _, _ in revision:
        tree = ast.parse(Path(relative).read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in ('ABILITIES', 'FRONT_ABILITIES') for target in node.targets):
                result.update(ast.literal_eval(node.value))
    return tuple({'name': name, 'description': description, 'id': 'ability-' + name,
                  'url': '/docs/system/abilities#ability-' + name} for name, description in sorted(result.items()))


def abilities():
    """The same abilities the account editor offers, with canonical citation anchors."""
    from weightroom.system_docs import ROOT
    return _abilities(revisions([ROOT / 'loadcoach/groups.py', ROOT / 'weightroom/app.py']))


def _comments(text):
    """Associate TOML keys with their preceding comments and table description."""
    comments, pending, table, table_help = {}, [], '', ''
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            pending.append(stripped[1:].strip())
        elif stripped.startswith('[') and stripped.endswith(']'):
            table = stripped.strip('[]').replace('"', '')
            table_help = ' '.join(pending)
            pending = []
        elif match := re.match(r'([A-Za-z0-9_.-]+)\s*=', stripped):
            name = '.'.join(filter(None, (table, match[1])))
            comments[name] = ' '.join(pending) or table_help
            pending = []
        elif stripped:
            pending = []
        elif pending:
            pending = []
    return comments


@lru_cache(maxsize=8)
def _settings(revision):
    """Read only the named shipped defaults and catalogue presentation metadata."""
    from loadcoach.setting_catalog import get
    from weightroom.doc_markdown import slug
    result = []
    for relative, _, _ in revision:
        if not relative.endswith('.toml'):
            continue
        with open(relative, encoding='utf-8') as source:
            text = source.read()
        values, comments = tomllib.loads(text), _comments(text)
        program = relative.rsplit('/', 1)[-1].removesuffix('.toml')
        def visit(values, parents=()):
            """Keep nested defaults addressable without reading a site's overrides."""
            for name, value in values.items():
                parts = (*parents, name)
                key = '.'.join(parts)
                if isinstance(value, dict) and value:
                    visit(value, parts)
                    continue
                owner, setting = (parts[0], '.'.join(parts[1:])) if parts[0] == 'turnstile' else (program, key)
                definition = get(owner, setting)
                meaning = (comments.get(key, '') or next((comments['.'.join(parts[:length])] for length in range(len(parts)-1, 0, -1) if comments.get('.'.join(parts[:length]))), '')
                           or (definition.help or definition.label if definition else ''))
                ident = 'setting-' + slug(owner + '-' + setting)
                result.append({'program': owner, 'key': setting, 'default': value,
                    'display_default': json.dumps(value, ensure_ascii=False), 'meaning': meaning,
                    'label': definition.label if definition else '', 'id': ident,
                    'url': '/docs/operations/settings-reference#' + ident,
                    'source': 'settings/defaults/' + relative.rsplit('/', 1)[-1]})
        visit(values)
    return tuple(sorted(result, key=lambda row: (row['program'], row['key'])))


def settings():
    """Generate defaults and meanings; the live settings directory is never a data source."""
    from weightroom.system_docs import ROOT
    paths = [*(ROOT / 'settings/defaults').glob('*.toml'), ROOT / 'weightroom/setting_labels.py', ROOT / 'loadcoach/setting_catalog.py']
    return _settings(revisions(paths))


@lru_cache(maxsize=8)
def _handler_descriptions(revision):
    """Collect handler summaries and literal ability checks across the API source allowlist."""
    from collections import defaultdict
    found, by_name = {}, defaultdict(list)
    for path, _, _ in revision:
        source = Path(path).read_text(encoding='utf-8')
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            abilities = set()
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Attribute) and call.func.attr == 'can' and call.args:
                    value = call.args[0]
                elif isinstance(call.func, ast.Name) and call.func.id in ('lacks', 'need') and len(call.args) >= 2:
                    value = call.args[1]
                else:
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    abilities.add(value.value)
            data = {'summary': (ast.get_docstring(node) or '').split('\n', 1)[0], 'abilities': sorted(abilities)}
            # Relative names are supplied independently of the checkout's absolute location.
            from weightroom.system_docs import ROOT
            relative = Path(path).relative_to(ROOT).as_posix()
            found[(relative, node.name)] = data
            if data['summary']:
                by_name[node.name].append(data)
        for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
            for node in cls.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    data = found[(relative, node.name)]
                    found[(relative, cls.name + '.' + node.name)] = data
                    by_name[cls.name + '.' + node.name].append(data)
    for name, options in by_name.items():
        if len({item['summary'] for item in options}) == 1:
            found[('', name)] = options[0]
    return found


def handler_descriptions():
    """Invalidate imported handler descriptions together with all allowlisted API revisions."""
    from weightroom import system_docs
    return _handler_descriptions(revisions([system_docs.ROOT / name for name in system_docs.source_files('api') if name.endswith('.py')]))


def page_map(can):
    """Generate the authorized navigation-page map from covers metadata and live navigation labels."""
    from weightroom import system_docs
    from weightroom.navigation import NAV_SECTIONS, navigation
    visible = system_docs.visible(can)
    topics = [item for section in visible for item in (section, *section['topics'])]
    labels = {url: label for group in navigation('/docs', list(can), False, tool_chat=True)['groups'] for label, url in group['links']}
    # My account is a separate menu rather than a main navigation group.
    labels.update({url: label for label, url in navigation('/account', list(can), False)['links']})
    result = []
    for category, groups in NAV_SECTIONS.items():
        for _, paths in groups:
            for path in paths:
                if path not in labels:
                    continue
                guides = [{'title': topic['title'], 'url': topic['url']} for topic in topics if path in topic.get('covers', [])]
                result.append({'path': path, 'title': labels[path], 'category': category, 'guides': guides})
    return result
