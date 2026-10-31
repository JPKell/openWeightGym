"""Shared node resolution and current-evidence rules for manual and recurring work."""
import hashlib
import json


def evidence_matches(run, definition, fingerprint, environment):
    """Match the frozen protocol and environment; formula and display edits are irrelevant."""
    return bool(fingerprint and run.get('fingerprint') == fingerprint
                and run.get('version') == definition.get('version')
                and run.get('current_protocol', True) and run.get('current_server', True)
                and all(environment.get(key) and run.get('environment', {}).get(key) == environment[key]
                        for key in ('engine_hash', 'machine_hash')))


def compatible(model, definition):
    """Preserve the native capability boundary for LLM, embedding, image and audio runs."""
    caps = model.get('capabilities', {})
    if caps.get('load_probes'):
        return definition['id'] == 'load'
    return (caps.get('native_probes') is not False and
            (definition['id'] == 'load' or bool(definition.get('embedding')) == bool(model.get('embedding'))))


def resolve_location(model, preferred='', unavailable=()):
    """Fallback only after a responding catalogue proves absence on the preferred node."""
    if preferred in unavailable:
        raise ValueError('Preferred node '+preferred+' is unavailable; its inventory is unknown.')
    locations = [one for one in model.get('locations', []) if one.get('enabled', True)]
    if not locations:
        if model.get('locations'):
            raise ValueError('No enabled location for '+model['id']+'.')
        return {**model, 'machine': 'compute'}, ''
    chosen = next((one for one in locations if one['machine'] == preferred), locations[0])
    reason = ('Model absent on '+preferred+'; using '+chosen['machine']+'.'
              if preferred and chosen['machine'] != preferred else '')
    return {**model, **chosen}, reason


def reservation_key(model_id, machine, benchmark):
    """One active native run per local identity, node and definition across all submitters."""
    return hashlib.sha256(json.dumps([model_id, machine or 'compute', benchmark]).encode()).hexdigest()
