"""Shared settings destinations and catalogue-based presentation, independent of services."""
from loadcoach.setting_catalog import get
from loadcoach.settings import written

PAGES = {
    'general': ('General settings', '/settings'),
    'requests': ('Requests and queue', '/settings/requests'),
    'retention': ('Retention and privacy', '/settings/retention'),
    'alerts': ('Alerts and notifications', '/settings/alerts'),
    'security': ('Security and sign-in', '/settings/security'),
    'limits': ('Uploads and limits', '/settings/limits'),
    'video': ('Video settings', '/settings/video'),
    'downloads': ('Downloads and credentials', '/settings/downloads'),
    'machine': ('Machine configuration', '/settings/machine'),
    'history': ('Settings history', '/settings/history'),
}
SURFACES = {'settings.html':'/settings', 'compute_nodes.html':'/compute-nodes', 'voice_admin.html':'/system/voice',
            'soundroom_system.html':'/system/audio', 'image_settings.html':'/settings/image',
            'tools.html':'/tools', 'allowances.html':'/allowances'}
RETURNS = {url for _, url in PAGES.values()} | {'/tools', '/allowances'}


def filtered(programs, page):
    """Keep program ownership and POST names while selecting one logical settings page."""
    result = []
    for program in programs:
        rows = [row for row in program['settings']
                if row.get('when') != 'file' and
                (get(program['program'], row['name']).page if get(program['program'], row['name']) else 'general') == page]
        if rows:
            result.append({**program, 'settings': rows})
    return result


def surface(path):
    """Identify pages using the shared settings workspace and navigation."""
    return path.startswith('/settings') or path in ('/compute-nodes', '/system/voice', '/system/audio', '/tools', '/allowances')


def node_rows(node, programs, page):
    """Node settings are evidence and links here; editing stays with the selected node."""
    return [{'node': node, 'program': program['program'], 'setting': row,
             'label': get(program['program'], row['name']).label if get(program['program'], row['name']) else row['name'],
             'value_text': written(row['changed']['value'] if row.get('changed') else row['value'])}
            for program in filtered(programs, page) for row in program['settings']]
