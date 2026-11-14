"""Presentation metadata only; services retain their settings and validation contracts."""

import json
from zoneinfo import available_timezones

TIMEZONES = [(json.dumps(name), name) for name in sorted(available_timezones())]

from loadcoach.setting_catalog import SECTION_ORDER, entries, get as catalog_entry
from loadcoach.settings import written

# Retain the public metadata shape for older callers and nested voice editors.
LABELS = {entry.name: (entry.label, entry.section, entry.unit, entry.control)
          for entry in entries()}


def describe(setting, program=None):
    """Friendly metadata and editor choice without altering setting values."""
    name = setting['name']
    fallback = (setting.get('label', name.replace('_', ' ').capitalize()), 'Additional settings', '', '')
    if name.startswith('greetings.'):
        fallback = (setting.get('label', name), 'Speech', '', 'text')
    definition = catalog_entry(program, name) if program else None
    label, section, unit, control = ((definition.label, definition.section, definition.unit, definition.control) if definition else LABELS.get(name, fallback))
    editor = 'text' if control == 'plain-text' or name in ('models_folder', 'models_storage_folder') else setting.get('editor', '')
    return {'label': setting.get('label', label), 'section': section, 'unit': unit, 'control': control, 'editor': editor,
            'min': definition.min if definition else None, 'max': definition.max if definition else None,
            'ceiling': definition.ceiling if definition else None,
            'help': setting.get('says') or setting.get('help') or (definition.help if definition else ''),
            **({'choices': TIMEZONES} if name == 'timezone' else {}),
            **({'choices': [(written(value), value) for entry in entries() if entry.name == name for value in entry.choices]} if any(entry.name == name and entry.choices for entry in entries()) else {})}


def sections(programs, *, compute_node=False):
    """Group editable rows by purpose while preserving one program POST per fragment."""
    result = {}
    for program in programs:
        for setting in program['settings']:
            if setting['when'] == 'file':
                continue
            title = describe(setting, program['program'])['section']
            if compute_node:
                if title == 'Power and idle' or setting['name'] in ('sample_every', 'component_versions_every'):
                    title = 'Telemetry, power and idle'
                elif title == 'Alerts and monitoring':
                    continue
            group = result.setdefault(title, {})
            fragment = group.setdefault(program['program'], {**program, 'settings': []})
            fragment['settings'].append(setting)
    order = [*SECTION_ORDER, *(title for title in result if title not in SECTION_ORDER)]
    return [{'title': title, 'programs': list(result[title].values()), **({'open':False} if title == 'Advanced' else {})}
            for title in order if title in result]
