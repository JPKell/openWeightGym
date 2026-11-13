"""Shared node resolution and current-evidence rules for manual and recurring work."""
import hashlib
import json

from loadcoach.log import LEGACY


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
    """A named benchmark destination is a pin; an omitted destination may be placed."""
    if preferred in unavailable:
        raise ValueError('Node '+preferred+' is unavailable; this benchmark will wait for it.')
    locations = [one for one in model.get('locations', []) if one.get('enabled', True)]
    if not locations:
        if model.get('locations'):
            raise ValueError('No enabled location for '+model['id']+'.')
        return {**model, 'machine': LEGACY}, ''  # an older single-node catalogue without locations
    chosen = next((one for one in locations if one['machine'] == preferred), locations[0])
    if preferred and chosen['machine'] != preferred:
        raise ValueError('Model '+model['id']+' has no enabled copy on node '+preferred+'.')
    return {**model, **chosen}, ''


def reservation_key(model_id, machine, benchmark):
    """One active native run per local identity, node and definition across all submitters."""
    return hashlib.sha256(json.dumps([model_id, machine or LEGACY, benchmark]).encode()).hexdigest()
