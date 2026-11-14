"""The documentation's sections and topics, and a cached source reference, read without importing services.

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


def _topic(slug: str, title: str, summary: str, source: str | None = None, doc: str | None = None, parts: tuple[str, ...] = ()) -> dict:
    """One topic: `source` names its code reference in SOURCES, `doc` a Markdown file and `parts` the `## ` sections of it to show (all when empty)."""
    return {"slug": slug, "title": title, "summary": summary, "source": source, "doc": doc, "parts": parts}


def _section(slug: str, title: str, summary: str, topics: tuple[dict, ...], public: bool = False) -> dict:
    """One section and its topics, each given its canonical address beneath the docs home."""
    url = "/docs/" + slug
    topics = tuple(topic | {"url": f"/docs/{slug}/{topic['slug']}"} for topic in topics)
    return {"slug": slug, "title": title, "summary": summary, "url": url, "public": public,
            "topics": topics, "by_slug": {topic["slug"]: topic for topic in topics}}


BENCHMARK_GUIDE = "benchmarks/README.md"
SECTIONS = (
    _section("system", "System reference", "How openWeightGym's parts fit together: which machine runs what, how a request travels between them, and the ideas every part shares.", (
        _topic("models", "Models", "Model files and their metadata, configuration and presets, the roles that stand in for a model's name, and projectors and embedding models.", source="models"),
        _topic("accounts", "Accounts and groups", "Users and groups, API keys and sessions, and the abilities and sensitivity levels that decide what each account may do.", source="accounts"),
        _topic("logs", "Logs", "Request records, what is kept of captured content, system and plugin logs, and the reliability evidence built from them.", source="logs"),
        _topic("hosted-providers", "Hosted providers", "Model services elsewhere whose models are offered here: their configuration, credentials, routing and limits, and costs and allowances.", source="hosted-providers"),
    )),
    _section("api", "API reference", "The model API that OpenAI-compatible clients use, openWeightGym's own endpoints, and the headers both accept.", (
        _topic("openai", "OpenAI compatibility", "Which OpenAI endpoints openWeightGym implements and their limits, and which it does not implement."),
        _topic("custom", "Custom endpoints", "openWeightGym's own endpoints, read from the route declarations in the source as it is now."),
        _topic("headers", "Headers", "The request headers that change how work is admitted, traced and logged, and the headers that responses carry."),
        _topic("source", "Code reference", "The modules that declare the API's routes, with each handler's documentation.", source="api"),
    )),
    _section("web-app", "Web app", "Using the web console: chat, collections, workflows, voice and media, tools, and your API keys.", (
        _topic("classifier", "Classifier", "Ask typed questions and compare uncalibrated decision probabilities.", doc="docs/decision-model-plan.md", parts=("Using Classifier",)),
        _topic("chat", "Chat", "Start a conversation, choose its model and change its options."),
        _topic("image-extraction", "Image extraction", "Describe images, extract text and receipts, review corrections and run independent image batches.", doc="docs/image-extraction-plan.md", parts=("Using Image extraction", "Tasks and output", "Storage and recovery")),
        _topic("collections", "Collections", "Keep private sources and ask questions answered from them."),
        _topic("workflows", "Workflows", "Run research and drafting as repeatable steps you review."),
        _topic("voice", "Voice and media", "Talk with a model by voice, and make images, music, sounds and speech."),
        _topic("tools", "Tools", "Give an MCP client or agent the tools your account may use."),
        _topic("api-keys", "API keys", "Create a key and connect an OpenAI-compatible client to the model API."),
    ), public=True),
    _section("modules", "Modules", "The programs openWeightGym is made of: what each one does, where it runs, and its code.", (
        _topic("weightroom", "WeightRoom", "The web console on front: its pages, chat, collections and workflows, administration and the benchmark worker.", source="weightroom"),
        _topic("turnstile", "Turnstile", "Front's door: it signs callers in, checks what they may do, and sends each request to the machine that should do the work, waking it when needed.", source="turnstile"),
        _topic("loadcoach", "LoadCoach", "Runs on each worker: admits and schedules work, keeps models resident on its GPUs, and serves inference.", source="loadcoach"),
        _topic("toollocker", "ToolLocker", "The MCP gateway on front: which tools exist, who may use them, and how they run.", source="toollocker", doc="toollocker/README.md"),
        _topic("voice", "Voice", "The voice gateway on front and the speech service on compute: clients, recognition and synthesis.", source="voice", doc="voice/README.md"),
        _topic("spotter", "Spotter", "Runs on each host: telemetry, service health, and the service and power controls front may use.", source="spotter"),
        _topic("plugins", "Plugins", "Adapters that offer external agents and services as models at the same /v1 address.", source="plugins", doc="plugins/README.md"),
        _topic("mirrorwall", "Mirror Wall", "Image generation with stable-diffusion.cpp: how an image is made, the models and their parts, and adding one.", source="mirrorwall"),
        _topic("soundroom", "SoundRoom", "Music, sounds, speech and transcription with audio models, and each person's private audio library on front.", source="soundroom"),
    )),
    _section("benchmarks", "Benchmarks", "How models are measured: what a run does, what each benchmark tests, how scores are kept, and how the benchmark worker is run.", (
        _topic("runs", "How a run works", "From choosing a model to a saved result.", doc=BENCHMARK_GUIDE, parts=("From a selected model to a result",)),
        _topic("measures", "What is measured", "Each benchmark: what it tests, how it is scored and what its score does not show.", doc=BENCHMARK_GUIDE,
               parts=("What the benchmarks measure", "Tool use and recovery (P4)", "General conversation probe (Part 4, milestone 101)",
                      "Multi-turn consistency (Part 6, milestone 102)", "Extraction and retrieval answer room (2026-10-16)",
                      "Paired JSON and schema (Part 7, milestone 103)", "Expanded tool use (Part 8, milestone 104)")),
        _topic("scores", "Scores and results", "The benchmark pages, how scores are calculated, and what makes two results comparable.", doc=BENCHMARK_GUIDE, parts=("Pages, scores and result identity",)),
        _topic("operation", "Files and operation", "Ports, endpoints, files and the day-to-day running of the benchmark worker.", doc=BENCHMARK_GUIDE, parts=("Key ports and endpoints", "Files and operation")),
        _topic("source", "Code reference", "The benchmark package: plans, the queue, suites and scoring.", source="benchmarks"),
    )),
    _section("data", "Data and backups", "Where openWeightGym keeps its data, which program owns each store, and how to back it up, restore it and move it to another database engine.", (
        _topic("stores", "Stores and ownership", "Each database, the machine it lives on and the programs that write it."),
        _topic("backups", "Backups and restore", "Recovery sets from both machines, what they leave out, and keeping identities through a reinstall."),
        _topic("engines", "SQLite and PostgreSQL", "The two database engines, and moving a store from one to the other."),
        _topic("source", "Code reference", "The shared storage layer, backups and database administration.", source="data"),
    )),
    _section("operations", "Deployment and operations", "Installing openWeightGym, where each part runs, its settings, pairing nodes and updating installations.", (
        _topic("installation", "Installation", "The installer, one machine or several, adding a GPU node, the owg command, and uninstalling.", doc="docs/installation.md"),
        _topic("topology", "Where things run", "Which programs run on front and which on each worker, and what happens while a worker is away.", doc="docs/README.md", parts=("Where things run",)),
        _topic("telephone", "Telephone", "Twilio setup, public routes, caller access, SMS registration and troubleshooting.", doc="docs/telephone.md"),
        _topic("settings", "Settings", "Where settings come from, model file places, and what is kept private.", doc="docs/README.md", parts=("Settings, data and privacy",)),
        _topic("deploying", "Node installation and updates", "Pairing nodes from Compute nodes, terminal updates, and serving the console over HTTPS on the LAN.", doc="docs/operations.md",
               parts=("Node installation and updates", "Console over HTTPS on the LAN")),
        _topic("source", "Code reference", "Install and deploy scripts, and the settings code.", source="operations"),
    )),
)


def _separate_code(sections):
    """Keep written guides intact while collecting their source inventories in one section."""
    guides, code, aliases, source_pages = [], {}, {}, {}
    for section in sections:
        topics = []
        for topic in section['topics']:
            source = topic['source']
            if source:
                title = section['title'] if topic['slug'] == 'source' else topic['title']
                code.setdefault(source, _topic(source, title, f'Source modules, classes and functions behind {title}.', source=source))
                source_pages[topic['url']] = '/docs/code/' + source
                if topic['slug'] == 'source':
                    aliases[topic['url']] = source_pages[topic['url']]
            if topic['slug'] != 'source':
                topics.append(topic | {'source': None, 'code_url': source_pages.get(topic['url'])})
        guide = section | {'topics': tuple(topics), 'by_slug': {topic['slug']: topic for topic in topics},
                           'code_url': source_pages.get(section['url'] + '/source')}
        guides.append(guide)
    guides.append(_section('code', 'Code references', 'Source modules, classes and functions, grouped by component.', tuple(code.values())))
    return tuple(guides), aliases, source_pages


SECTIONS, CODE_ALIASES, SOURCE_PAGES = _separate_code(SECTIONS)
BY_SLUG = {section["slug"]: section for section in SECTIONS}
# Where each topic lived before the docs had sections, so that old links and bookmarks still arrive.
MOVED = {"weightroom": "/docs/modules/weightroom", "turnstile": "/docs/modules/turnstile", "loadcoach": "/docs/modules/loadcoach",
         "toollocker": "/docs/modules/toollocker", "voice": "/docs/modules/voice", "spotter": "/docs/modules/spotter",
         "plugins": "/docs/modules/plugins", "mirrorwall": "/docs/modules/mirrorwall", "collections": "/docs/web-app/collections",
         "models": "/docs/system/models", "accounts": "/docs/system/accounts", "logs": "/docs/system/logs",
         "hosted-providers": "/docs/system/hosted-providers"}
# A link in a Markdown file to another file that the docs show goes to that page; whole files go to their topic.
# A file shown only in parts goes to the section showing them.
DOC_PAGES = {topic["doc"]: section["url"] for section in SECTIONS for topic in section["topics"] if topic["doc"]}
DOC_PAGES |= {topic["doc"]: topic["url"] for section in SECTIONS for topic in section["topics"] if topic["doc"] and not topic["parts"]}


def visible(can) -> tuple[dict, ...]:
    """The sections this account may read: all of them with manage-settings, otherwise the Web app alone."""
    return tuple(section for section in SECTIONS if section["public"] or "manage-settings" in can)


def _links(source: str):
    """How links in the Markdown file `source` are shown on the web: other repository files become their docs page or plain text."""
    folder = posixpath.dirname(source)

    def resolve(href: str) -> str | None:
        """The link's new address, or None to show only its text."""
        if not href or href.startswith(("#", "/")) or urlsplit(href).scheme:
            return href
        return DOC_PAGES.get(posixpath.normpath(posixpath.join(folder, href.split("#", 1)[0])))
    return resolve


@lru_cache(maxsize=32)
def _written(relative: str, modified: int, size: int) -> dict:
    """One Markdown file made into a document, cached for its current revision."""
    return doc_markdown.document((ROOT / relative).read_text(encoding="utf-8"), links=_links(relative))


def written(topic: dict) -> dict | None:
    """What the repository has written for a topic: the whole Markdown file, or only the sections it names."""
    if not topic.get("doc"):
        return None
    stat = (ROOT / topic["doc"]).stat()
    found = _written(topic["doc"], stat.st_mtime_ns, stat.st_size)
    if not topic["parts"]:
        return found
    sections = [section for section in found["sections"] if section["title"] in topic["parts"]]
    return {"title": found["title"], "overview": Markup(""), "sections": sections,
            "contents": [(section["id"], section["title"]) for section in sections]}


# Fixed source ownership keeps query parameters from becoming arbitrary file reads.
SOURCES = {
    "api": ("loadcoach/app.py", "loadcoach/tasks.py", "soundroom/packages.py", "turnstile/app.py",
            "turnstile/accounts.py", "turnstile/events.py", "turnstile/roles.py", "turnstile/observations.py",
            "turnstile/extractions.py", "turnstile/responses.py",
            "turnstile/allowances.py", "turnstile/tasks.py", "turnstile/settings.py", "turnstile/hosted.py",
            "soundroom/front.py", "mirrorwall/front.py", "plugins/_turnstile.py", "plugins/_runtime.py",
            "voice/gateway.py", "voice/service.py", "spotter/app.py", "toollocker/door.py"),
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
