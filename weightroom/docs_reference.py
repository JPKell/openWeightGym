"""Reference facts read from declarations and shipped defaults, never configured values."""

import ast
import json
import re
import tomllib
from functools import lru_cache


def revisions(paths):
    """Key generated references on every contributing source revision."""
    return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(paths))


@lru_cache(maxsize=8)
def _abilities(revision):
    """Read literal ability dictionaries without importing the console or its services."""
    result = {}
    for relative, _, _ in revision:
        tree = ast.parse(open(relative, encoding='utf-8').read())
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
                meaning = comments.get(key, '') or (definition.help or definition.label if definition else '')
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
