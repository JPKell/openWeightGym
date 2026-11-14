"""Inspect conversion bundles as inert data; repository code is never imported."""

import json
from pathlib import Path


def check_source(folder):
    """Reject custom-code bundles before any conversion subprocess can start."""
    root = Path(folder)
    for file in root.rglob('*'):
        if file.is_symlink():
            raise ValueError('Refused (remote code): source bundles may not contain symlinks.')
        if file.suffix.lower() == '.py':
            raise ValueError('Refused (remote code): Python repository files are not accepted.')
        if file.suffix.lower() == '.json' and file.name in ('config.json', 'tokenizer_config.json'):
            if file.stat().st_size > 2**20:
                raise ValueError('Source configuration exceeds 1 MiB.')
            value = json.loads(file.read_text())
            def custom(data):
                """Find custom-code configuration without executing repository files."""
                return isinstance(data, dict) and (bool(data.get('auto_map')) or bool(data.get('trust_remote_code')) or
                    any(custom(v) for v in data.values())) or isinstance(data, list) and any(custom(v) for v in data)
            if custom(value):
                raise ValueError('Refused (remote code): auto_map and trust_remote_code bundles are not accepted.')
    return root


def source_facts(path):
    """Give each inert source file the bundle's conversion readiness."""
    facts = {'what': 'classifier', 'kind': 'conversion source', 'name': Path(path).name, 'conversion': 'available'}
    try:
        check_source(Path(path).parent)
    except (OSError, ValueError) as error:
        facts.update(conversion='refused', why=str(error))
    return facts
