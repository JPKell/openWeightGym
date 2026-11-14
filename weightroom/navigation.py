"""Site categories and their pages, limited to the signed-in person's abilities."""

from weightroom.system_docs import BY_SLUG as DOC_SECTIONS, visible as visible_docs
from loadcoach.platform_services import PlatformServices


NAV_SECTIONS = {
    'Interact': (
        ('Chat & analysis', ('/chat', '/voice', '/chat/classifier')),
        ('Images', ('/chat/images', '/chat/extraction')),
        ('Audio', ('/chat/speech', '/chat/transcribe', '/chat/music', '/chat/sounds')),
        ('Desktop apps', ('/voice/desktop',)),
    ),
    'Models': (
        ('Catalogue', ('/models', '/models/images', '/models/audio', '/cloud-apis')),
        ('Routing & capacity', ('/roles', '/dry-fit')),
        ('Files & provenance', ('/files', '/fingerprints')),
        ('Downloads', ('/hub', '/civitai')),
    ),
    'Benchmark': (
        ('Run benchmarks', ('/benchmarks/runs', '/benchmarks/node-measurements')),
        ('Results', ('/benchmarks/results', '/benchmarks/compare')),
        ('Test assets', ('/collections', '/workflows')),
        ('Administration', ('/benchmarks/manage',)),
    ),
    'Logs': (
        ('Overview', ('/usage', '/reliability')),
        ('Model activity', ('/requests', '/batches', '/responses')),
        ('Diagnostics', ('/log', '/plugins/activity', '/output')),
    ),
    'System': (
        ('Monitoring', ('/status', '/telemetry', '/alerts')),
        ('Services', ('/system/components', '/system/voice/telephone', '/system/voice/status')),
        ('Automation', ('/schedules',)),
        ('Data & recovery', ('/databases', '/backups')),
    ),
    'Settings': (
        ('Platform', ('/settings', '/compute-nodes')),
        ('Access & spending', ('/users', '/groups', '/allowances')),
        ('Tools & plugins', ('/tools', '/plugins')),
        ('Media', ('/settings/image', '/system/audio', '/system/voice')),
    ),
    'Docs': (
        ('', ('/docs',)),
        ('Reference', ('/docs/system', '/docs/api', '/docs/modules')),
        ('Guides', ('/docs/web-app', '/docs/benchmarks', '/docs/operations', '/docs/data')),
        ('Development', ('/docs/code',)),
    ),
    'My account': (
        ('Profile', ('/account', '/account/keys')),
        ('Preferences', ('/account/appearance', '/account/audio', '/account/workspace', '/account/privacy')),
        ('Activity', ('/account/logs',)),
    ),
}


def grouped_links(links, section):
    """Order visible links by task and put headings before each populated group."""
    available = {url: label for label, url in links}
    ordered, headings = [], {}
    for title, paths in NAV_SECTIONS[section]:
        visible = [(available.pop(url), url) for url in paths if url in available]
        if visible:
            headings[visible[0][1]] = title
            ordered.extend(visible)
    # Future links remain reachable until their section is added to the catalogue.
    remaining = sorted(((label, url) for url, label in available.items()), key=lambda item: item[0].casefold())
    if remaining:
        headings[remaining[0][1]] = 'Other'
        ordered.extend(remaining)
    return ordered, headings


def navigation(path: str, can: list[str], machine: bool, services: PlatformServices | None = None) -> dict:
    """Choose the category and page for the current route.

    Detail pages keep their parent highlighted. Voice chat, its desktop app download and administration are local pages.
    In Docs the links are its sections, and `children` holds the topics of the section being read, shown beneath it.
    """
    services = services or PlatformServices()
    groups = [
        {"name": "Interact", "links": [("Text chat", "/chat"), ("Classifier", "/chat/classifier"), ("Image generation", "/chat/images"), ("Image extraction", "/chat/extraction"), ("Music", "/chat/music"), ("Sounds", "/chat/sounds"), ("Voice generation", "/chat/speech"), ("Transcribe", "/chat/transcribe")]},
        {"name": "Models", "links": []},
        # Collections and workflows keep their own addresses; they are here because they are used to compare models.
        {"name": "Benchmark", "links": [("Runs", "/benchmarks/runs"), ("Results", "/benchmarks/results"), ("Node measurements", "/benchmarks/node-measurements"), ("Compare", "/benchmarks/compare"), ("Collections", "/collections"), ("Workflows", "/workflows")]},
        {"name": "Logs", "links": [("Usage", "/usage"), ("Reliability", "/reliability"), ("Model requests", "/requests"), ("Batches", "/batches"), ("Responses", "/responses"), ("Event log", "/log")]},
        {"name": "System", "links": [("Status", "/status")]},
        {"name": "Settings", "links": []},
    ]
    if "front-voice" in can or "superuser" in can:
        groups[0]["links"].append(("Voice chat", '/voice'))
        groups[0]["links"].append(("Voice desktop app", '/voice/desktop'))
    if "manage-models" in can:
        groups[1]["links"] = [("Models", "/models"), ("Images", "/models/images"), ("Audio", "/models/audio"), ("Roles", "/roles")]
    if "manage-hosted" in can:  # the services elsewhere whose models are offered here
        groups[1]["links"].append(("Cloud APIs", "/cloud-apis"))
    if "manage-models" in can:
        groups[1]["links"].extend([("Files", "/files"), ("Dry fit", "/dry-fit"), ("Fingerprints", "/fingerprints"),
                                   ("Hugging Face", "/hub"), ("Civitai", "/civitai")])
    if set(can) & {"front-plugins", "front-manage-plugins", "see-log"}:
        groups[3]["links"].append(("Plugin logs", "/plugins/activity"))
    if "manage-settings" in can:
        groups[5]["links"].extend([("Voice", "/system/voice"), ("Audio", "/system/audio"), ("Image", "/settings/image")])
        groups[2]["links"].append(("Manage", "/benchmarks/manage"))
    docs = visible_docs(can)
    groups.append({"name": "Docs", "links": [('Documentation', '/docs'), *[(section["title"], section["url"]) for section in docs]]})
    if "restart-services" in can:
        groups[3]["links"].append(("System logs", "/output"))
    groups[4]["links"].append(("Telemetry", "/telemetry"))
    if "manage-settings" in can:
        groups[4]["links"].append(("Components", "/system/components"))
        groups[4]["links"].extend([('Telephone', '/system/voice/telephone'), ("Voice status", "/system/voice/status")])
        groups[4]["links"].append(("Alerts", "/alerts"))
        groups[4]["links"].append(("Schedules", "/schedules"))
        groups[5]["links"].insert(0, ("Compute nodes", "/compute-nodes"))
    for ability, label, url in [("manage-users", "Users", "/users"), ("manage-groups", "Groups", "/groups"), ("manage-settings", "Tools", "/tools"), ("front-manage-plugins", "Plugins", "/plugins"), ("manage-settings", "Backups", "/backups"), ("manage-settings", "Databases", "/databases"), ("manage-settings", "General", "/settings")]:
        if ability in can:
            groups[4 if url in ('/backups', '/databases') else 5]["links"].append((label, url))
    # Everyone may open it: members see their own and their group's allowance, managers edit them.
    groups[5]["links"].append(("Spending allowances", "/allowances"))
    for group in groups:
        group['links'] = [link for link in group['links'] if services.path_enabled(link[1])]
        group['links'], group['headings'] = grouped_links(group['links'], group['name'])
    root = "/" + path.strip("/").split("/")[0]
    active = {"/": "/status", "/file": "/files", "/downloads": "/hub", "/measure-models": "/models", "/outside": "/cloud-apis",
              "/control": "/status", "/ask": "/log", "/machines": "/telemetry"}.get(root, root)
    if path.rstrip("/") in ("/plugins/activity", "/voice/desktop", "/system/components"):
        active = path.rstrip("/")
    if root == "/benchmarks":
        active = path.rstrip("/")
        if active == "/benchmarks":
            active = "/benchmarks/runs"
        if active.startswith('/benchmarks/run/'):
            active = '/benchmarks/runs'
    if path.startswith("/models/images"):
        active = "/models/images"
    elif path.startswith(("/chat/classifier", "/chat/images", "/chat/extraction", "/chat/music", "/chat/sounds", "/chat/speech", "/chat/transcribe", "/models/audio")):
        active = "/" + "/".join(path.strip("/").split("/")[:2])
    selected = next((group for group in groups if any(url == active for _, url in group["links"])), None)
    if root == "/plugins" and active != "/plugins/activity":
        selected = groups[5]
    if root == '/system' and path.startswith(('/system/voice', '/system/audio')):
        selected, active = groups[5], '/' + '/'.join(path.strip('/').split('/')[:2])
    if path == '/system/voice/status':
        selected, active = groups[4], path
    if path.startswith('/settings/image'):
        selected, active = groups[5], '/settings/image'
    children = {}
    if root == '/docs':
        selected = next((group for group in groups if group['name'] == 'Docs'), None)
        parts = path.strip('/').split('/')
        active = '/' + '/'.join(parts)
        section = DOC_SECTIONS.get(parts[1]) if len(parts) > 1 else None
        if section in docs:
            children = {section['url']: [(topic['title'], topic['url']) for topic in section['topics']]}
    if root == "/account":
        links = [(label, url) for label, url in [("Account", "/account"), ('API keys', '/account/keys'), ("Appearance", "/account/appearance"), ("Audio", "/account/audio"), ('Workspace', '/account/workspace'), ('Alerts & privacy', '/account/privacy'), ("Logs", "/account/logs")]
                 if services.path_enabled(url)]
        links, headings = grouped_links(links, 'My account')
        active = next((url for _, url in links[1:] if path.rstrip('/') == url), '/account')
        return {"groups": [group for group in groups if group['links']], "section": "My account", "links": links, "active": active, "children": {}, "headings": headings}
    return {"groups": [group for group in groups if group["links"]], "section": selected["name"] if selected else "", "links": selected["links"] if selected else [],
            "active": active if selected else "", "children": children if selected else {}, "headings": selected['headings'] if selected else {}}
