"""Admin documentation and a cached source reference, read without importing services."""

import ast
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TOPICS = (
    ("api", "API reference", ("OpenAI compatibility", "Unsupported APIs", "Custom endpoints", "Request headers")),
    ("weightroom", "WeightRoom", ("Web console", "Chat and conversations", "Administration", "Accessibility and appearance")),
    ("turnstile", "Turnstile", ("Authentication and authorization", "Request routing", "Machine placement", "Wake and recovery")),
    ("loadcoach", "LoadCoach", ("Model admission and scheduling", "GPU residency", "Inference APIs", "Request lifecycle")),
    ("toollocker", "ToolLocker", ("Tool catalogue", "MCP transport", "Permissions", "Execution and results")),
    ("voice", "Voice", ("Voice gateway", "Desktop app", "Web streams", "Recognition and synthesis", "Greetings and administration")),
    ("spotter", "Spotter", ("Machine telemetry", "Service health", "Alerts", "Power controls")),
    ("hosted-providers", "Hosted providers", ("Provider configuration", "Credentials", "Routing and limits", "Costs and allowances")),
    ("plugins", "Plugins", ("Plugin contract", "Adapters", "Installation", "Activity and errors")),
    ("logs", "Logs", ("Request records", "Captured content", "System and plugin logs", "Reliability evidence")),
    ("models", "Models", ("Files and metadata", "Configuration and presets", "Roles and selection", "Projectors and embeddings")),
    ("mirrorwall", "Mirror Wall (images)", ("How an image is made", "Models and their parts", "Adding a model", "Reference images")),
    ("benchmarks", "Benchmarks", ("Suites and cases", "Runs and results", "Scoring and qualification", "Schedules")),
    ("data", "Data and backups", ("Databases and ownership", "Backups and restore", "Retention and migrations")),
    ("accounts", "Accounts and groups", ("Users and groups", "API keys and sessions", "Permissions and sensitivity")),
    ("collections", "Collections and workflows", ("Document collections", "Retrieval", "Prompt versions", "Workflow state")),
    ("operations", "Deployment and operations", ("Topology", "Installation and deployment", "Configuration", "Health and recovery")),
)

BY_SLUG = {slug: {"slug": slug, "title": title, "headings": headings} for slug, title, headings in TOPICS}
LINKS = [(title, "/docs/" + slug) for slug, title, _ in TOPICS]

# Fixed source ownership keeps query parameters from becoming arbitrary file reads.
SOURCES = {
    "api": ("loadcoach/app.py", "loadcoach/tasks.py", "soundroom/packages.py", "turnstile/app.py",
            "turnstile/accounts.py", "turnstile/events.py", "turnstile/roles.py", "turnstile/observations.py",
            "turnstile/allowances.py", "turnstile/tasks.py", "turnstile/settings.py", "turnstile/hosted.py",
            "soundroom/front.py", "mirrorwall/front.py", "plugins/_turnstile.py", "plugins/_runtime.py",
            "voice/gateway.py", "voice/service.py", "spotter/app.py", "toollocker/door.py"),
    "weightroom": ("weightroom",), "turnstile": ("turnstile",),
    "loadcoach": ("loadcoach",), "toollocker": ("toollocker",),
    "voice": ("voice",), "spotter": ("spotter",), "plugins": ("plugins",),
    "benchmarks": ("benchmarks",), "data": ("db", "loadcoach/backups.py", "loadcoach/database_admin.py"),
    "hosted-providers": ("loadcoach/outside.py", "turnstile/hosted.py", "turnstile/allowances.py"),
    "logs": ("loadcoach/log.py", "loadcoach/capture.py", "loadcoach/diagnostics.py", "loadcoach/reliability.py", "turnstile/events.py",
             "loadcoach/observability.py", "loadcoach/observation_store.py", "turnstile/observations.py"),
    "mirrorwall": ("mirrorwall", "engines/sdcpp"),
    "models": ("engines", "mirrorwall", "soundroom", "loadcoach/models.py", "loadcoach/model_setup.py", "loadcoach/roles.py", "loadcoach/files.py", "loadcoach/hub.py"),
    "accounts": ("loadcoach/identity_bundle.py", "loadcoach/users.py", "loadcoach/groups.py", "loadcoach/statement.py", "turnstile/accounts.py"),
    "collections": ("weightroom/knowledge.py", "weightroom/knowledge_store.py", "weightroom/workflows.py", "weightroom/workflow_store.py"),
    "operations": ("scripts", "weightroom/deploy.py", "loadcoach/config.py", "loadcoach/settings.py"),
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
    if path in ('/loadcoach/facts', '/loadcoach/lock', '/loadcoach/unlock', '/loadcoach/hold', '/loadcoach/unload') or path == '/loadcoach/loadouts' and method == 'POST':
        headers.append('X-LoadCoach-Lock')
    if path in ('/loadcoach/hold', '/loadcoach/unload', '/loadcoach/lock') or path == '/loadcoach/loadouts' and method == 'POST':
        headers.append('X-LoadCoach-Priority (when acquiring admission)')
    if path == '/loadcoach/sign-in':
        headers.append('X-LoadCoach-From (trusted sign-in callers only)')
    if path.startswith('/loadcoach/tasks/'):
        headers.append('X-LoadCoach-Task')
        if path.endswith('/probe') and method == 'POST':
            headers.append('X-LoadCoach-Probe-ID (front-owned tasks)')
    if path.endswith('/dispatch') and path.startswith('/loadcoach/schedules/'):
        headers.append('X-Suite-Schedule (internal scoped grant)')
    if path.startswith('/mcp'):
        headers.extend(('X-ToolLocker-Sets', 'X-LoadCoach-Sensitivity', 'X-LoadCoach-Trace', 'Mcp-Session-Id'))
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
                summary = descriptions.get(name, '').split('\n', 1)[0]
                for method in methods or ():
                    if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'WS'):
                        rows.append({'method': method, 'path': path, 'summary': summary, 'line': node.lineno,
                                     'headers': api_headers(relative, path, method)})
        for child in ast.iter_child_nodes(node):
            visit(child, bindings)

    visit(tree, {})
    return tuple(sorted(rows, key=lambda row: (row['path'], row['method'])))


def api_reference(files: tuple[str, ...]) -> list[dict]:
    """Inventory allowlisted custom HTTP/WebSocket APIs; compatibility is documented separately."""
    groups = []
    for relative in files:
        if relative not in SOURCES['api']:
            continue
        stat = (ROOT / relative).stat()
        rows = [row for row in _api_source(relative, stat.st_mtime_ns, stat.st_size) if not row['path'].startswith('/v1/')]
        if rows:
            groups.append({'path': relative, 'rows': rows})
    return groups
