"""Presentation metadata only; services retain their settings and validation contracts."""

import json
from zoneinfo import available_timezones

TIMEZONES = [(json.dumps(name), name) for name in sorted(available_timezones())]

# Unknown keys retain their current editor under Additional settings.
LABELS = {
    'wait': ('Wait for a node', 'Requests and queue', 's', 'number'),
    'max_wait': ('Queue wait limit', 'Requests and queue', 's', 'number'),
    'max_estimated_wait': ('Estimated wait limit', 'Requests and queue', 's', 'number'),
    'load_timeout': ('Model load timeout', 'Requests and queue', 's', 'number'),
    'keep_days': ('Event retention', 'Logs and retention', 'days', 'number'),
    'keep_text': ('Request text retention', 'Logs and retention', '', 'number'),
    'batch_keep_days': ('Batch retention', 'Logs and retention', 'days', 'number'),
    'backup_retention': ('Backups to keep', 'Logs and retention', '', 'number'),
    'unlogged': ('Allow requests without logs', 'Logs and retention', '', 'switch'),
    'sleep_after': ('Sleep after idle', 'Power and idle', 'min', 'number'),
    'busy_gpu': ('GPU activity threshold', 'Power and idle', '%', 'number'),
    'busy_processor': ('CPU activity threshold', 'Power and idle', '%', 'number'),
    'model_idle_minutes': ('GPU model idle timeout', 'Power and idle', 'min', 'number'),
    'cpu_idle_minutes': ('CPU model idle timeout', 'Power and idle', 'min', 'number'),
    'alert_gpu_temperature': ('GPU temperature alert', 'Alerts and monitoring', '°C', 'number'),
    'observation_error_percent': ('Request error threshold', 'Alerts and monitoring', '%', 'number'),
    'observation_p95_seconds': ('Request latency threshold', 'Alerts and monitoring', 's', 'number'),
    'observation_min_samples': ('Minimum request samples', 'Alerts and monitoring', '', 'number'),
    'observation_window_seconds': ('Observation window', 'Alerts and monitoring', 's', 'number'),
    'observation_alert_hold_seconds': ('Alert hold time', 'Alerts and monitoring', 's', 'number'),
    'third_party_services': ('Third-party services to track', 'Alerts and monitoring', '', 'services'),
    'sample_every': ('Sampling interval', 'Alerts and monitoring', 's', 'number'),
    'call_timeout': ('Tool call timeout', 'Tools', 's', 'number'),
    'toolsets': ('Tool collections', 'Tools', '', 'table'),
    'tool_levels': ('Tool access levels', 'Tools', '', 'table'),
    'timezone': ('Time zone', 'Display and costs', '', 'text'),
    'mock_input_rate': ('Input token estimate', 'Display and costs', '$ / M tokens', 'number'),
    'mock_output_rate': ('Output token estimate', 'Display and costs', '$ / M tokens', 'number'),
    'artifact_machine': ('File download node', 'Model files', '', 'text'),
    'models_folder': ('Active model cache', 'Model files', '', 'text'),
    'models_storage_folder': ('Model storage library', 'Model files', '', 'text'),
    'files_every': ('Check model files every', 'Logs and retention', 'min', 'number'),
    'host_ram_headroom_mib': ('Host RAM headroom', 'RAM policy', 'MiB', 'number'),
    'host_ram_limit_mib': ('Host RAM limit', 'RAM policy', 'MiB', 'number'),
    'disable_ram_offload': ('Disable RAM offload', 'RAM policy', '', 'switch'),
    'card_margin': ('GPU memory headroom', 'RAM policy', 'MiB', 'number'),
    'model_defaults': ('Inherited loading flags', 'Advanced', '', 'table'),
    'max_active_sessions': ('Active sessions', 'Sessions and limits', '', 'number'),
    'max_starting_sessions': ('Starting sessions', 'Sessions and limits', '', 'number'),
    'max_session_minutes': ('Session duration limit', 'Sessions and limits', 'min', 'number'),
    'idle_seconds': ('Idle session timeout', 'Sessions and limits', 's', 'number'),
    'hold_seconds': ('Model hold duration', 'Sessions and limits', 's', 'number'),
    'hold_every': ('Renew model hold every', 'Sessions and limits', 's', 'number'),
    'hold_after_last_call': ('Hold after the last call', 'Sessions and limits', 's', 'number'),
    'startup_seconds': ('Startup timeout', 'Sessions and limits', 's', 'number'),
    'speech_threshold': ('Speech detection threshold', 'Speech', '', 'number'),
    'model_parallel': ('Model request capacity', 'Sessions and limits', '', 'table'),
    'profile': ('Conversation profile', 'Direct conversations', '', 'text'),
    'modes': ('Conversation modes', 'Direct conversations', '', 'text'),
    'soundroom_profile': ('Audio model', 'Speech', '', 'text'),
    'hermes_model': ('Model', 'Hermes agent', '', 'text'),
    'hermes_system_prompt': ('Instructions', 'Hermes agent', '', 'text'),
    'hermes_max_turns': ('Agent turn limit', 'Hermes agent', '', 'number'),
    'hermes_context_tokens': ('Context limit', 'Hermes agent', 'tokens', 'number'),
    'hermes_max_output_tokens': ('Output limit', 'Hermes agent', 'tokens', 'number'),
    'hermes_reasoning_effort': ('Reasoning effort', 'Hermes agent', '', 'text'),
    'profiles.default.model': ('Model', 'Direct conversations', '', 'text'),
    'profiles.default.system_prompt': ('Instructions', 'Direct conversations', '', 'text'),
    'profiles.default.modes': ('Conversation modes', 'Direct conversations', '', 'text'),
    'profiles.default.max_output_tokens': ('Output limit', 'Direct conversations', 'tokens', 'number'),
    'profiles.default.max_context_tokens': ('Context limit', 'Direct conversations', 'tokens', 'number'),
    'profiles.default.speaker': ('Speaker', 'Speech', '', 'text'),
}


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


def sections(programs):
    """Group editable rows by purpose while preserving one program POST per fragment."""
    result = {}
    for program in programs:
        for setting in program['settings']:
            if setting['when'] == 'file':
                continue
            title = describe(setting)['section']
            group = result.setdefault(title, {})
            fragment = group.setdefault(program['program'], {**program, 'settings': []})
            fragment['settings'].append(setting)
    order = ['Requests and queue', 'Logs and retention', 'Power and idle', 'Alerts and monitoring',
             'Tools', 'Display and costs', 'Model files', 'Direct conversations', 'Hermes agent',
             'Sessions and limits', 'Speech', 'RAM policy', 'Additional settings', 'Advanced']
    return [{'title': title, 'programs': list(result[title].values())}
            for title in order if title in result]
