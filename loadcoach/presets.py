"""Shared preset identity and target-file preservation, independent of catalogue labels."""

import copy
import hashlib
import json
import os
import re
from decimal import Decimal
from functools import lru_cache

from loadcoach.gguf import facts, read_metadata


def normal(text: str) -> str:
    """Ignore case and word separators, while retaining model version decimal points."""
    return re.sub(r'[\s_-]+', '-', text.strip().casefold())


@lru_cache(maxsize=512)
def described(path: str, stamp: tuple) -> dict:
    """Read only GGUF metadata; changing the file expires this small header cache."""
    try:
        return read_metadata(path)
    except (OSError, ValueError):
        return {}


def group(path: str, identity: str) -> dict:
    """Share by declared model name/architecture/parameter size, otherwise by the same file.

    Quantisation and catalogue aliases are not model identities. Incomplete metadata only
    establishes same-file sharing; a missing file stays local to the stable catalogue ID.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return {'key': 'entry:' + identity, 'name': 'This model entry', 'basis': 'entry'}
    metadata = described(path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    name, size, architecture = (metadata.get(field) for field in
                                ('general.name', 'general.size_label', 'general.architecture'))
    if all(isinstance(value, str) and value.strip() for value in (name, size, architecture)) and facts(metadata).get('what') == 'model':
        # Some converters append the quant to general.name. Strip only a terminal quant,
        # preserving names such as Instruct, Thinking, Heretic and their version/date.
        label = re.sub(r'[\s._-]+(?:UD[\s._-]+)?(?:(?:I?Q)\d[\w]*|BF16|F16|F32)$', '', name, flags=re.IGNORECASE)
        size_key = re.sub(r'\d+(?:\.\d+)?', lambda match: format(Decimal(match[0]).normalize(), 'f'), normal(size))
        values = [normal(label), normal(architecture), size_key]
        scope = hashlib.sha256(json.dumps(values).encode()).hexdigest()
        return {'key': 'family:' + scope, 'name': f'{label} · {size}', 'basis': 'family'}
    return {'key': file_scope(path), 'name': os.path.basename(path), 'basis': 'file'}


def file_scope(path: str) -> str:
    """A stable same-path fallback, also readable after a file gains complete metadata."""
    return 'file:' + hashlib.sha256(os.path.realpath(path).encode()).hexdigest()


def engine_scope(settings: dict, scope: str) -> str:
    """Retain legacy llama.cpp scopes; other engines cannot inherit those presets."""
    from engines import engine_for
    selected = engine_for(settings).id
    return scope if selected == "llama_cpp" else f"engine:{selected}:{scope}"


def for_target(configuration: dict, target: dict) -> dict:
    """Reuse the preset's tuning/placement/order, retaining the target's selected artifacts."""
    from engines import engine_for
    selected = engine_for(target)
    if engine_for(configuration.get("settings", {})).id != selected.id:
        raise ValueError("Presets cannot be applied across inference engines.")
    result = copy.deepcopy(configuration)
    result['settings'] = selected.model.retarget(result['settings'], copy.deepcopy(target))
    return result


def migrate(database, connection) -> None:
    """Move legacy entry presets into shared scopes once, preserving conflicting names.

    The schema migration lock/transaction covers the whole conversion on both engines.
    No tensor weights are read and neither model configuration nor undo is modified.
    """
    connection.execute('''CREATE TABLE IF NOT EXISTS shared_model_presets (
        scope TEXT NOT NULL, name TEXT NOT NULL, settings TEXT NOT NULL,
        updated DOUBLE PRECISION NOT NULL, PRIMARY KEY(scope,name))''')
    models = {identity: (name, json.loads(settings)) for identity, name, settings in
              connection.execute('SELECT identity,name,settings FROM models')}
    rows = connection.execute('SELECT model_id,name,settings,updated FROM model_presets ORDER BY updated DESC,model_id,name').fetchall()
    for identity, label, raw, updated in rows:
        configuration = json.loads(raw)
        source, settings = models.get(identity, (identity, configuration['settings']))
        scope = group(settings['flags'].get('model', ''), identity)['key']
        chosen, attempt = label, 0
        while connection.execute('SELECT 1 FROM shared_model_presets WHERE scope=? AND name=?', (scope, chosen)).fetchone():
            attempt += 1
            suffix = f' [{source}{" " + str(attempt) if attempt > 1 else ""}]'
            chosen = label[:max(1, 80-len(suffix))] + suffix[-79:]
        connection.execute('INSERT INTO shared_model_presets VALUES (?,?,?,?)', (scope, chosen, raw, updated))
    connection.execute('DELETE FROM model_presets')
