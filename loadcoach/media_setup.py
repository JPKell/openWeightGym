"""Read-only setup reviews for fixed image/audio profiles; no native process is started."""

import os

from engines import engine_for
from loadcoach.model_setup import digest


def profile_key(library, name):
    """Bind a saved envelope to settings and file revisions, independently of pinned cards."""
    settings = library.get(name)
    engine = engine_for(settings)
    if engine.id not in ('sdcpp', 'audiocpp'):
        return None
    stamps = []
    for role, path in engine.model.files_of(settings).items():
        try:
            info = os.stat(path)
            stamp = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        except OSError:
            stamp = None
        stamps.append([role, path, stamp])
    return digest([library.identity(name), library.file_revision(name),
                   {key: value for key, value in settings.items() if key != 'gpus'}, stamps])


def preview(name, candidate, library, runner):
    """Use the ordinary engine validation and admission without pretending media has tokens."""
    before = library.setup(name)
    errors, warnings, artifacts, stamps = [], [], [], []
    settings = candidate.get('settings')
    effective = {}
    try:
        if candidate.keys() != {'settings', 'order'} or type(candidate['order']) is not int or not -(2**31) <= candidate['order'] < 2**31:
            raise ValueError('A configuration contains settings and a whole-number sort order.')
        engine = engine_for(settings)
        engine.model.check_model(name, settings, runner.engine_cards(settings))
        effective = engine.model.effective(settings, {})
        installed = engine.describe(runner.config)
        if not installed['available']:
            errors.append(installed['why'])
        for role, path in engine.model.files_of(settings).items():
            info = os.stat(path)
            if not os.path.isfile(path):
                raise ValueError(f'{role} must be a regular file.')
            stamps.append([role, path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            artifacts.append({'role': role, 'path': path, 'facts': {}, 'compatibility': 'fixed profile; checked again at load'})
    except (OSError, ValueError, TypeError, KeyError) as problem:
        errors.append(str(problem))
    related = runner.replacements(name, effective) if effective and not errors else []
    server = runner.serving(name, effective) if effective and not errors else None
    reusable = server and server.ready and not server.dead() and runner.answers(server, effective)
    protected = [one for one in related if not one.ready or runner.busy(one) or runner.pinned(one)]
    fitted = runner.fit(name, effective, set(), configured=effective) if effective and not errors and not protected and not reusable else None
    measure = library.measure(name, effective) if effective else None
    if not measure or (settings.get('engine') == 'sdcpp' and not (measure.get('image_profile') or {}).get('qualified')):
        warnings.append('This fixed memory envelope is unmeasured or unqualified; ordinary admission reserves conservatively and checks the real workload at load.')
    placement = {'state': 'reuse loaded server' if reusable else 'candidate placement' if fitted else 'deferred / unavailable',
                 'cards': list(server.cards) if reusable else list(fitted[1]) if fitted else [], 'context': None,
                 'request_context': None, 'leaves': [] if reusable or not fitted else [one.model for one in fitted[2]],
                 'stays': list(runner.servers), 'protected': [one.model for one in protected], 'loaded': [],
                 'measured': bool(measure), 'note': 'Fixed profile; live admission rechecks device and host RAM.'}
    changes = [{'field': key, 'before': before['settings'].get(key), 'after': settings.get(key),
                'before_set': key in before['settings'], 'after_set': key in settings}
               for key in sorted(before['settings'].keys() | settings.keys()) if before['settings'].get(key) != settings.get(key)] if isinstance(settings, dict) else []
    if before['order'] != candidate.get('order'):
        changes.append({'field': 'sort order', 'before': before['order'], 'after': candidate.get('order'), 'before_set': True, 'after_set': True})
    return {'candidate': candidate, 'before': before, 'model_id': library.identity(name), 'file_revision': library.file_revision(name),
            'effective': effective, 'errors': errors, 'warnings': warnings, 'artifacts': artifacts, 'changes': changes,
            'placement': placement, 'command': '', 'idle_minutes': runner.config.model_idle_minutes,
            'token': digest([library.identity(name), library.file_revision(name), before, candidate, stamps,
                             runner.config.engines, runner.engine_cards(effective) if effective else []]),
            'qualification': 'Static fixed-profile review; workload qualification remains separate.'}
