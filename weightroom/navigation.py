"""Site categories and their pages, limited to the signed-in person's abilities."""

from weightroom.system_docs import BY_SLUG as DOC_SECTIONS, visible as visible_docs


def navigation(path: str, can: list[str], machine: bool) -> dict:
    """Choose the category and page for the current route.

    Detail pages keep their parent highlighted. Voice chat, its desktop app download and administration are local pages.
    In Docs the links are its sections, and `children` holds the topics of the section being read, shown beneath it.
    """
    groups = [
        {"name": "Chat", "links": [("Conversations", "/chat"), ("Image generation", "/chat/images"), ("Music", "/chat/music"), ("Sounds", "/chat/sounds"), ("Speech", "/chat/speech")]},
        {"name": "Models", "links": []},
        # Collections and workflows keep their own addresses; they are here because they are used to compare models.
        {"name": "Benchmark", "links": [("Runs", "/benchmarks/runs"), ("Results", "/benchmarks/results"), ("Node measurements", "/benchmarks/node-measurements"), ("Compare", "/benchmarks/compare"), ("Collections", "/collections"), ("Workflows", "/workflows")]},
        {"name": "Logs", "links": [("Usage", "/usage"), ("Requests", "/requests"), ("Batches", "/batches"), ("AI Logs", "/log"), ("Reliability", "/reliability")]},
        {"name": "System", "links": [("Status", "/status")]},
        {"name": "Settings", "links": []},
    ]
    if "front-voice" in can or "superuser" in can:
        groups[0]["links"].append(("Voice", '/voice'))
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
    groups.append({"name": "Docs", "links": [(section["title"], section["url"]) for section in docs]})
    if "restart-services" in can:
        groups[3]["links"].append(("System logs", "/output"))
    groups[4]["links"].append(("Telemetry", "/telemetry"))
    if "manage-settings" in can:
        groups[4]["links"].append(("Components", "/system/components"))
        groups[4]["links"].append(("Voice", "/system/voice/status"))
        groups[4]["links"].append(("Alerts", "/alerts"))
        groups[4]["links"].append(("Schedules", "/schedules"))
        groups[5]["links"].insert(0, ("Compute nodes", "/compute-nodes"))
    for ability, label, url in [("manage-users", "Users", "/users"), ("manage-groups", "Groups", "/groups"), ("manage-settings", "Tools", "/tools"), ("front-manage-plugins", "Plugins", "/plugins"), ("manage-settings", "Backups", "/backups"), ("manage-settings", "DB", "/databases"), ("manage-settings", "General", "/settings")]:
        if ability in can:
            groups[4 if url in ('/backups', '/databases') else 5]["links"].append((label, url))
    # Everyone may open it: members see their own and their group's allowance, managers edit them.
    groups[5]["links"].append(("Spending allowances", "/allowances"))
    settings_order = ['Compute nodes', 'Users', 'Groups', 'Spending allowances', 'Tools', 'Plugins', 'Voice', 'Audio', 'Image', 'General']
    groups[5]['links'].sort(key=lambda link: settings_order.index(link[0]))
    groups[4]['links'].sort(key=lambda link: ['Status', 'Telemetry', 'Components', 'Voice', 'Alerts', 'Schedules', 'DB', 'Backups'].index(link[0]))
    for group in groups:
        first_download = next((url for _, url in group['links'] if url in ('/hub', '/civitai')), None)
        group['headings'] = {first_download: 'Downloads'} if first_download else {}
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
    elif path.startswith(("/chat/images", "/chat/music", "/chat/sounds", "/chat/speech", "/models/audio")):
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
        active = '/docs' if parts[1:] in ([], ['system']) else '/' + '/'.join(parts)
        section = DOC_SECTIONS.get(parts[1] if len(parts) > 1 else 'system')
        if section in docs:
            children = {section['url']: [(topic['title'], topic['url']) for topic in section['topics']]}
    if root == "/account":
        links = [("Account", "/account"), ("Appearance", "/account/appearance"), ("Audio", "/account/audio"), ("Logs", "/account/logs")]
        active = next((url for _, url in links[1:] if path.rstrip('/') == url), '/account')
        return {"groups": [group for group in groups if group['links']], "section": "My account", "links": links, "active": active, "children": {}, "headings": {}}
    return {"groups": [group for group in groups if group["links"]], "section": selected["name"] if selected else "", "links": selected["links"] if selected else [],
            "active": active if selected else "", "children": children if selected else {}, "headings": selected['headings'] if selected else {}}
