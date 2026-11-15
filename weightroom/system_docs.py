"""Discovered Markdown guides and cached source references, read without importing services.

The docs have a searchable home and sections, each with an overview and topics of its own. A topic shows what is
written for it, as a template in pages/docs/SECTION/TOPIC.html, sections of a Markdown file in the
repository (so that the web and the repository say the same thing), or both. Code references live in
their own section. Only the home and Web app section are open to every account.
"""

import ast
import posixpath
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from markupsafe import Markup

from weightroom import doc_markdown

ROOT = Path(__file__).resolve().parents[1]


def _metadata(path: Path, *, section=False) -> dict:
    """Validate guide metadata at discovery, before any page can be searched or rendered."""
    if path.resolve() != path or not path.is_relative_to(ROOT):
        raise ValueError('Documentation metadata must be a regular repository file.')
    found, _ = doc_markdown.front_matter(path.read_text(encoding='utf-8'))
    allowed = {'title', 'summary', 'order', 'covers', 'aliases', 'include', 'parts', 'audience', 'generated', 'source', 'code', 'route'}
    if found.keys() - allowed:
        raise ValueError(f'{path}: unknown documentation metadata: {sorted(found.keys() - allowed)}')
    for name in ('title', 'summary'):
        if not isinstance(found.get(name), str) or not found[name].strip():
            raise ValueError(f'{path}: {name} is required.')
    if type(found.get('order', 0)) is not int:
        raise ValueError(f'{path}: order must be a whole number.')
    if found.get('audience', 'admin' if section else 'public') not in ('public', 'admin'):
        raise ValueError(f'{path}: audience must be public or admin.')
    for name in ('covers', 'aliases', 'parts'):
        if not isinstance(found.get(name, []), list) or any(not isinstance(value, str) for value in found.get(name, [])):
            raise ValueError(f'{path}: {name} must be a list of strings.')
    if any(not value.startswith('/') or '?' in value or '#' in value or '..' in value for value in found.get('covers', [])):
        raise ValueError(f'{path}: covers must contain local page paths.')
    if any(not re.fullmatch(r'/docs/[a-z0-9/-]+', value) or '..' in value for value in found.get('aliases', [])):
        raise ValueError(f'{path}: aliases must be documentation paths.')
    # Validate even hidden text: a malformed private block must not become public later.
    doc_markdown.audience_text(path.read_text(encoding='utf-8'), True)
    return found


def include_file(guide: Path, value: str) -> str:
    """Resolve only maintained repository Markdown, refusing history, escapes and symlinks."""
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f'{guide}: include must be a relative repository file.')
    target = Path(posixpath.normpath(str(guide.parent / value)))
    if not target.is_relative_to(ROOT) or target.resolve() != target or not target.is_file():
        raise ValueError(f'{guide}: include leaves the repository or is not a regular file.')
    name = target.name
    if (target.suffix.lower() not in ('.md', '.txt') or name.endswith(('-plan.md', '-work.md'))
            or name in ('loadcoach.md', 'agent-status.md') or name.startswith('application-sweep-')
            or any('evidence' in part.lower() for part in target.relative_to(ROOT).parts)):
        raise ValueError(f'{guide}: plans, history and evidence cannot be included in guides.')
    return target.relative_to(ROOT).as_posix()


def discover() -> tuple[tuple[dict, ...], dict, dict, dict]:
    """Discover sections, topics and old addresses from folder metadata, with no hand-kept catalogue."""
    sections, aliases, source_pages, documents = [], {}, {}, {}
    for index in (ROOT / 'docs/guide').glob('*/_section.md'):
        meta = _metadata(index, section=True)
        name = meta.get('route', index.parent.name)
        if not isinstance(name, str) or not re.fullmatch('[a-z0-9-]+', name):
            raise ValueError(f'{index}: invalid section route.')
        url = '/docs/' + name
        section = {**meta, 'slug': name, 'url': url, 'public': meta.get('audience') == 'public',
                   'audience': meta.get('audience', 'admin'), 'guide': index.relative_to(ROOT).as_posix(),
                   'doc': index.relative_to(ROOT).as_posix(), 'parts': (), 'source': None}
        topics = []
        for path in index.parent.glob('*.md'):
            if path.name.startswith('_'):
                continue
            data = _metadata(path)
            topic_url = url + '/' + path.stem
            source = data.get('source')
            topic = {**data, 'slug': path.stem, 'url': topic_url, 'source': source,
                     'audience': 'admin' if section['audience'] == 'admin' else data.get('audience', section['audience']),
                     'guide': path.relative_to(ROOT).as_posix(), 'parts': tuple(data.get('parts', ())),
                     'doc': include_file(path, data['include']) if 'include' in data else path.relative_to(ROOT).as_posix(),
                     'code_url': '/docs/code/' + data['code'] if data.get('code') else None}
            if source or data.get('generated'):
                topic['doc'] = None
            if topic['code_url']:
                source_pages[topic_url] = topic['code_url']
            for alias in data.get('aliases', []):
                if alias in aliases:
                    raise ValueError(f'Duplicate documentation alias: {alias}')
                aliases[alias] = topic_url
            documents[topic['guide']] = topic_url
            if topic['doc']:
                documents.setdefault(topic['doc'], url if topic['parts'] else topic_url)
                if not topic['parts']:
                    documents[topic['doc']] = topic_url
            topics.append(topic)
        topics.sort(key=lambda topic: (topic.get('order', 0), topic['slug']))
        section.update(topics=tuple(topics), by_slug={topic['slug']: topic for topic in topics})
        documents[section['doc']] = url
        sections.append(section)
    sections.sort(key=lambda section: (section.get('order', 0), section['slug']))
    urls = {item['url'] for section in sections for item in (section, *section['topics'])}
    if len(urls) != sum(1 + len(section['topics']) for section in sections) or urls.intersection(aliases):
        raise ValueError('Documentation routes or aliases overlap.')
    for section in sections:
        legacy = aliases.get(section['url'] + '/source')
        section['code_url'] = legacy
    return tuple(sections), aliases, source_pages, documents


SECTIONS = ()
BY_SLUG, ALIASES, CODE_ALIASES, SOURCE_PAGES, MOVED, DOC_PAGES = {}, {}, {}, {}, {}, {}
_GUIDE_REVISION = None


def refresh() -> None:
    """Pick up added and edited guide files without restarting; retain imported lookup dictionaries."""
    global SECTIONS, _GUIDE_REVISION
    paths = sorted((ROOT / 'docs/guide').glob('*/*.md'))
    revision = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
    if revision == _GUIDE_REVISION:
        return
    sections, aliases, source_pages, documents = discover()
    SECTIONS = sections
    for target, values in ((BY_SLUG, {section['slug']: section for section in sections}), (ALIASES, aliases),
                           (CODE_ALIASES, {key: value for key, value in aliases.items() if value.startswith('/docs/code/')}),
                           (MOVED, {key.removeprefix('/docs/'): value for key, value in aliases.items() if key.count('/') == 2}),
                           (SOURCE_PAGES, source_pages), (DOC_PAGES, documents)):
        target.clear()
        target.update(values)
    _GUIDE_REVISION = revision


refresh()


def visible(can) -> tuple[dict, ...]:
    """Select sections and topics for this audience before indexing or rendering any prose."""
    refresh()
    admin = 'manage-settings' in can
    return tuple(section | {'topics': tuple(topic for topic in section['topics'] if admin or topic['audience'] == 'public'),
                            'by_slug': {topic['slug']: topic for topic in section['topics'] if admin or topic['audience'] == 'public'}}
                 for section in SECTIONS if admin or section['audience'] == 'public')


def _links(source: str):
    """Rewrite repository links to their one documentation home, retaining valid section anchors."""
    folder = posixpath.dirname(source)
    def resolve(href: str) -> str | None:
        """Keep web links and map maintained files to their corresponding visible article."""
        if not href or href.startswith('/') or urlsplit(href).scheme:
            return href
        relative, _, anchor = href.partition('#')
        name = posixpath.normpath(posixpath.join(folder, relative)) if relative else source
        target = DOC_PAGES.get(name)
        if anchor and target:
            path = ROOT / name
            stat = path.stat()
            found = _anchor_inventory(name, stat.st_mtime_ns, stat.st_size)
            for section in SECTIONS:
                for topic in (section, *section['topics']):
                    if topic.get('doc') != name:
                        continue
                    for title, identifiers in found:
                        if topic['parts'] and title not in topic['parts']:
                            continue
                        ident = next((one for one in identifiers if one == anchor), None)
                        ident = ident or next((one for one in identifiers if one.endswith('-' + anchor)), None)
                        if ident:
                            return topic['url'] + '#' + ident
            # A maintained file can link to history that is deliberately absent from Docs.
            # Keep its relevant section reachable without publishing a broken fragment.
        return target
    return resolve


@lru_cache(maxsize=128)
def _anchor_inventory(relative: str, modified: int, size: int) -> tuple:
    """Find rendered anchors without recursive link rewriting or importing any service."""
    found = doc_markdown.document((ROOT / relative).read_text(encoding='utf-8'), admin=True)
    return (('', ('overview',)), *(
        (section['title'], (section['id'], *re.findall(r'id="([^"]+)"', str(section['body']))))
        for section in found['sections']))


@lru_cache(maxsize=128)
def _written(relative: str, modified: int, size: int, admin: bool = False) -> dict:
    """Render one file revision separately for each audience, including filtered private blocks."""
    if (ROOT / relative).resolve() != ROOT / relative:
        raise ValueError('Documentation cannot follow symlinks.')
    return doc_markdown.document((ROOT / relative).read_text(encoding='utf-8'), links=_links(relative), admin=admin)


def written(topic: dict, *, admin: bool = False) -> dict | None:
    """Render a guide or its maintained include, refusing silently missing section selections."""
    if not topic.get('doc'):
        return None
    if topic.get('include'):
        include_file(ROOT / topic['guide'], topic['include'])
    stat = (ROOT / topic['doc']).stat()
    found = _written(topic['doc'], stat.st_mtime_ns, stat.st_size, admin)
    if not topic['parts']:
        return found
    sections = [section for section in found['sections'] if section['title'] in topic['parts']]
    if set(topic['parts']) != {section['title'] for section in sections}:
        raise ValueError('An included documentation section no longer exists: ' + topic['doc'])
    return {'title': topic['title'], 'overview': Markup(''), 'sections': sections,
            'contents': [(section['id'], section['title']) for section in sections]}


# Fixed source ownership keeps query parameters from becoming arbitrary file reads.
SOURCES = {
    "api": ("loadcoach/app.py", "loadcoach/tasks.py", "soundroom/packages.py", "turnstile/app.py",
            "turnstile/accounts.py", "turnstile/events.py", "turnstile/roles.py", "turnstile/observations.py",
            "turnstile/extractions.py", "turnstile/responses.py", "turnstile/docs_help.py", "turnstile/classifier.py", "turnstile/batches.py",
            "turnstile/allowances.py", "turnstile/tasks.py", "turnstile/settings.py", "turnstile/hosted.py",
            "soundroom/front.py", "soundroom/runtime.py", "mirrorwall/front.py", "mirrorwall/runtime.py", "mirrorwall/video_api.py", "plugins/_turnstile.py", "plugins/_runtime.py",
            "voice/gateway.py", "voice/service.py", "spotter/app.py", "toollocker/door.py", "toollocker/remote_settings.py", "weightroom/docs_views.py"),
    "weightroom": ("weightroom",), "turnstile": ("turnstile",), "soundroom": ("soundroom",),
    "loadcoach": ("loadcoach",), "toollocker": ("toollocker",),
    "voice": ("voice",), "spotter": ("spotter",), "plugins": ("plugins",),
    "benchmarks": ("benchmarks",), "data": ("db", "loadcoach/backups.py", "loadcoach/backup_crypto.py", "loadcoach/backup_restore.py", "loadcoach/database_admin.py"),
    "hosted-providers": ("loadcoach/outside.py", "turnstile/hosted.py", "turnstile/allowances.py"),
    "logs": ("loadcoach/log.py", "loadcoach/capture.py", "loadcoach/diagnostics.py", "loadcoach/reliability.py", "turnstile/events.py",
             "loadcoach/observability.py", "loadcoach/observation_store.py", "turnstile/observations.py"),
    "mirrorwall": ("mirrorwall", "engines/sdcpp"),
    "models": ("engines", "mirrorwall", "soundroom", "loadcoach/models.py", "loadcoach/model_setup.py", "loadcoach/roles.py", "loadcoach/files.py", "loadcoach/hub.py"),
    "accounts": ("loadcoach/identity_bundle.py", "loadcoach/users.py", "loadcoach/groups.py", "loadcoach/statement.py", "turnstile/accounts.py"),
    "operations": ("scripts", "weightroom/pairing.py", "loadcoach/config.py", "loadcoach/settings.py"),
}


def source_files(slug: str) -> tuple[str, ...]:
    """List allowlisted Python/browser sources, excluding vendors, bundles and symlinks."""
    found = set()
    for relative in SOURCES.get(slug, ()):
        path = ROOT / relative
        candidates = (*path.rglob('*.py'), *path.rglob('*.js')) if path.is_dir() else (path,)
        for candidate in candidates:
            if not candidate.is_file() or candidate.is_symlink() or candidate.name.endswith('.min.js'):
                continue
            relative_path = candidate.relative_to(ROOT)
            if {'vendor', '__pycache__'}.intersection(relative_path.parts):
                continue
            if candidate.resolve() != ROOT / relative_path:
                continue
            found.add(relative_path.as_posix())
    return tuple(sorted(found))


def _javascript_reference(source: str, relative: str) -> dict:
    """Read attached JSDoc summaries for named browser helpers without executing scripts."""
    definitions = []
    for match in re.finditer(r'^[ \t]*/\*\*(.*?)\*/\s*\n[ \t]*(?P<declaration>[^\n]+)', source, re.M | re.S):
        declaration = match['declaration']
        function = re.match(r'(?:async\s+)?function\s+([\w$]+)\s*(\([^\n]*?\))', declaration)
        arrow = re.match(r'(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s+)?(\([^\n]*?\)|[\w$]+)\s*=>', declaration)
        method = re.match(r'([\w$]+)\s*(\([^\n]*?\))\s*\{', declaration)
        cls = re.match(r'class\s+([\w$]+)', declaration)
        found = function or arrow or method or cls
        if found is None:
            continue
        doc = '\n'.join(line.strip().removeprefix('*').strip() for line in match[1].strip().splitlines())
        definitions.append({'name': found[1], 'line': source.count('\n', 0, match.start('declaration')) + 1,
                            'kind': 'class' if cls else 'function',
                            'signature': '' if cls else found[2] if found[2].startswith('(') else '(' + found[2] + ')',
                            'doc': doc})
    return {'path': relative, 'doc': '', 'definitions': definitions}


@lru_cache(maxsize=512)
def _source_reference(relative: str, modified: int, size: int) -> dict:
    """Parse one source revision; stat keys invalidate cached documentation after edits."""
    source = (ROOT / relative).read_text(encoding='utf-8')
    if relative.endswith('.js'):
        return _javascript_reference(source, relative)
    tree = ast.parse(source, filename=relative)
    definitions = []

    def visit(node, parents=()):
        """Collect qualified definitions in source order, including nested route handlers."""
        for child in ast.iter_child_nodes(node):
            names = parents
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = (*parents, child.name)
                definitions.append({
                    'name': '.'.join(names), 'line': child.lineno,
                    'kind': 'class' if isinstance(child, ast.ClassDef) else 'async def' if isinstance(child, ast.AsyncFunctionDef) else 'def',
                    'signature': '' if isinstance(child, ast.ClassDef) else '(' + ast.unparse(child.args) + ')',
                    'doc': ast.get_docstring(child) or 'Documentation is missing.',
                })
            visit(child, names)

    visit(tree)
    return {'path': relative, 'doc': ast.get_docstring(tree) or '', 'definitions': definitions}


def _parsed(relative: str) -> dict:
    """One source's reference, through the cache keyed on its current revision."""
    stat = (ROOT / relative).stat()
    return _source_reference(relative, stat.st_mtime_ns, stat.st_size)


def source_reference(slug: str, selected: str = '') -> dict:
    """Return a topic's modules grouped by folder, each with a one-line summary, and the selected module parsed in full."""
    files = source_files(slug)
    if selected and selected not in files:
        raise ValueError('Documentation source not found.')
    groups: dict[str, list] = {}
    for relative in files:
        try:
            parsed = _parsed(relative)
            summary, count = parsed['doc'].split('\n', 1)[0], len(parsed['definitions'])
        except (OSError, SyntaxError, UnicodeError):
            summary, count = '', 0  # an unreadable file is still listed; selecting it reports the problem
        folder, _, name = relative.rpartition('/')
        groups.setdefault(folder, []).append({'path': relative, 'name': name, 'summary': summary, 'count': count})
    reference = {'files': files, 'groups': [{'folder': folder, 'modules': modules} for folder, modules in groups.items()],
                 'module': _parsed(selected) if selected else None}
    if slug == 'api':
        reference['api'] = api_reference(files)
    return reference


def api_headers(relative: str, path: str, method: str) -> tuple[str, ...]:
    """Document route-specific headers; common transport headers are explained once on the page."""
    headers = []
    loadouts = (path == '/loadcoach/loadouts' or path.endswith('/control/loadouts')) and method == 'POST'
    if path in ('/loadcoach/facts', '/loadcoach/lock', '/loadcoach/unlock', '/loadcoach/hold', '/loadcoach/unload') or loadouts:
        headers.append('X-LoadCoach-Lock')
    if path in ('/loadcoach/hold', '/loadcoach/unload', '/loadcoach/lock') or loadouts:
        headers.append('X-LoadCoach-Priority (when acquiring admission)')
    if path == '/loadcoach/sign-in':
        headers.append('X-LoadCoach-From (trusted sign-in callers only)')
    if path.startswith('/loadcoach/tasks/'):
        headers.append('X-LoadCoach-Task')
        if path.endswith('/probe') and method == 'POST':
            headers.append('X-LoadCoach-Probe-ID (benchmark tasks)')
    if path.endswith('/dispatch') and path.startswith('/loadcoach/schedules/'):
        headers.append('X-Suite-Schedule (internal scoped grant)')
    if path.startswith('/mcp'):
        headers.extend(('X-ToolLocker-Sets', 'X-LoadCoach-Sensitivity', 'X-LoadCoach-Trace', 'Mcp-Session-Id'))
    if path == '/owg/extractions' and method == 'POST':
        headers.append('Idempotency-Key')
    if path.startswith('/owg/extraction-tools/'):
        headers.append('X-Suite-Statement (internal caller-bound delegation)')
    if path == '/voice/prompts' and method == 'GET':
        headers.append('If-None-Match (standard HTTP)')
    if relative == 'voice/gateway.py':
        if path in ('/voice/inbound', '/twilio/voice', '/voice/stream', '/twilio/media'):
            headers.append('X-Twilio-Signature')
        if path in ('/browser/sessions', '/browser/sessions/{sid}/stream'):
            headers.append('Origin; Cookie (browser stream, standard HTTP)')
        if path == '/desktop/stream':
            headers.append('Authorization: Bearer API_KEY; Origin must be absent')
    if relative == 'voice/service.py' and path in ('/voice/stream', '/voice/sessions/{sid}'):
        headers.append('Authorization: Bearer SESSION_TOKEN')
    if path == '/soundroom/assets/{identifier}/content':
        headers.append('Range (standard HTTP)')
    if relative == 'plugins/_runtime.py':
        headers.extend(('Authorization: Bearer PAIRING_KEY', 'X-Suite-Instance', 'X-Suite-User-ID (trusted adapter owner scope)'))
    return tuple(headers)


@lru_cache(maxsize=64)
def _api_source(relative: str, modified: int, size: int) -> tuple[dict, ...]:
    """Read literal route declarations and bounded literal comprehensions without executing source."""
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'), filename=relative)
    descriptions = {node.name: ast.get_docstring(node) or '' for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    constructors = {target.id: node.value.func.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    for target in node.targets if isinstance(target, ast.Name)}
    rows = []

    def resolve(node, bindings):
        """Substitute only the literal AST bindings of a route comprehension."""
        if isinstance(node, ast.Name) and node.id in bindings:
            return bindings[node.id]
        return node

    def literal(node, bindings):
        """Read constants, containers and string concatenation; never evaluate calls or attributes."""
        node = resolve(node, bindings)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [literal(part, bindings) for part in node.elts]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = literal(node.left, bindings), literal(node.right, bindings)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
        return None

    def visit(node, bindings):
        """Collect explicit routes, expanding only fixed literal lists with no filters or async work."""
        if isinstance(node, ast.ListComp):
            contexts = [bindings]
            for generator in node.generators:
                if generator.ifs or generator.is_async or not isinstance(generator.iter, (ast.Tuple, ast.List)):
                    return
                contexts_next = []
                for context in contexts:
                    for item in generator.iter.elts:
                        targets = generator.target.elts if isinstance(generator.target, (ast.Tuple, ast.List)) else [generator.target]
                        values = item.elts if isinstance(generator.target, (ast.Tuple, ast.List)) and isinstance(item, (ast.Tuple, ast.List)) else [item]
                        if len(targets) == len(values) and all(isinstance(target, ast.Name) for target in targets):
                            contexts_next.append({**context, **{target.id: resolve(value, context) for target, value in zip(targets, values)}})
                contexts = contexts_next
            for context in contexts:
                visit(node.elt, context)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('route', 'Route', 'WebSocketRoute') and len(node.args) >= 2:
            path = literal(node.args[0], bindings)
            if isinstance(path, str) and path.startswith('/') and path not in ('/', '/{path:path}', '/v1/{path:path}'):
                methods = ['WS'] if node.func.id == 'WebSocketRoute' else ['GET']
                if node.func.id == 'route' and len(node.args) > 2:
                    methods = [literal(node.args[2], bindings)]
                for keyword in node.keywords:
                    if keyword.arg == 'methods':
                        methods = literal(keyword.value, bindings)
                handler = resolve(node.args[1], bindings)
                if isinstance(handler, ast.Call) and handler.args:
                    handler = resolve(handler.args[-1], bindings)
                name = handler.id if isinstance(handler, ast.Name) else handler.attr if isinstance(handler, ast.Attribute) else ''
                qualified = (constructors[handler.value.id] + '.' + name if isinstance(handler, ast.Attribute)
                             and isinstance(handler.value, ast.Name) and handler.value.id in constructors else name)
                summary = descriptions.get(name, '').split('\n', 1)[0]
                for method in methods or ():
                    if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'WS'):
                        rows.append({'method': method, 'path': path, 'summary': summary, 'line': node.lineno, 'handler': qualified,
                                     'headers': api_headers(relative, path, method)})
        for child in ast.iter_child_nodes(node):
            visit(child, bindings)

    visit(tree, {})
    return tuple(sorted(rows, key=lambda row: (row['path'], row['method'])))


def api_reference(files: tuple[str, ...]) -> list[dict]:
    """Inventory declared APIs, including public v1 routes, using their handler documentation."""
    from weightroom.docs_reference import handler_descriptions
    descriptions = handler_descriptions()
    groups = []
    for relative in files:
        if relative not in SOURCES['api']:
            continue
        stat = (ROOT / relative).stat()
        rows = []
        for original in _api_source(relative, stat.st_mtime_ns, stat.st_size):
            row = dict(original)
            described = descriptions.get((relative, row['handler'])) or descriptions.get(('', row['handler']), {})
            row['summary'] = row['summary'] or described.get('summary', '')
            row['abilities'] = described.get('abilities', [])
            row['id'] = 'endpoint-' + doc_markdown.slug(relative + '-' + row['method'] + '-' + row['path'])
            row['url'] = '/docs/api/custom#' + row['id']
            rows.append(row)
        if rows:
            groups.append({'path': relative, 'rows': rows})
    return groups
