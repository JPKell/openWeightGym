"""Presentation metadata only; services retain their settings and validation contracts."""

import json
from zoneinfo import available_timezones

TIMEZONES = [(json.dumps(name), name) for name in sorted(available_timezones())]

from loadcoach.setting_catalog import SECTION_ORDER, entries

# Retain the public metadata shape for older callers and nested voice editors.
LABELS = {entry.name: (entry.label, entry.section, entry.unit, entry.control)
          for entry in entries()}


def describe(setting):
    """Friendly metadata and editor choice without altering setting values."""
    name = setting['name']
    fallback = (setting.get('label', name.replace('_', ' ').capitalize()), 'Additional settings', '', '')
    if name.startswith('greetings.'):
        fallback = (setting.get('label', name), 'Speech', '', 'text')
    label, section, unit, control = LABELS.get(name, fallback)
    editor = 'text' if name in ('models_folder', 'models_storage_folder') else setting.get('editor', '')
    return {'label': label, 'section': section, 'unit': unit, 'control': control, 'editor': editor,
            **({'choices': TIMEZONES} if name == 'timezone' else {})}


def sections(programs, *, compute_node=False):
    """Group editable rows by purpose while preserving one program POST per fragment."""
    result = {}
    for program in programs:
        for setting in program['settings']:
            if setting['when'] == 'file':
                continue
            title = describe(setting)['section']
            if compute_node:
                if title == 'Power and idle' or setting['name'] in ('sample_every', 'component_versions_every'):
                    title = 'Telemetry, power and idle'
                elif title == 'Alerts and monitoring':
                    continue
            group = result.setdefault(title, {})
            fragment = group.setdefault(program['program'], {**program, 'settings': []})
            fragment['settings'].append(setting)
    order = [*SECTION_ORDER, *(title for title in result if title not in SECTION_ORDER)]
    return [{'title': title, 'programs': list(result[title].values())}
            for title in order if title in result]
