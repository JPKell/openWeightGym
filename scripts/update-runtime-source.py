"""Put this checkout first in service source overrides without changing other runtime settings."""

import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import uuid

PROGRAMS = ('loadcoach', 'weightroom', 'turnstile', 'toollocker', 'spotter', 'voice',
            'voice-gateway', 'frontdesk', 'tooldoor')


def words(value):
    return shlex.split(value, comments=False)


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def source_path(value, root):
    """Keep custom library paths after the current checkout; repeated updates are idempotent."""
    first, _, _ = value.partition(':')
    if first == str(root):
        return value
    return str(root) + (':' + value if value else '')


def logical_lines(text):
    raw = ''
    for line in text.splitlines(keepends=True):
        raw += line
        if line.rstrip('\n').endswith('\\'):
            continue
        yield raw, raw.replace('\\\n', ' ').strip()
        raw = ''
    if raw:
        yield raw, raw.replace('\\\n', ' ').strip()


def rewrite(text, root, *, environment_file=False):
    """Only PYTHONPATH assignments change. Environment resets and unrelated tokens stay intact."""
    result, section = [], ''
    for raw, line in logical_lines(text):
        if line.startswith('['):
            section = line
        key, separator, value = line.partition('=')
        if environment_file:
            if key.strip() == 'PYTHONPATH' and separator:
                values = words(value)
                previous = ' '.join(values)
                updated = source_path(previous, root)
                if previous != updated:
                    raw = 'PYTHONPATH=' + quoted(updated) + '\n'
        elif section == '[Service]' and key.strip() == 'Environment' and separator:
            tokens = words(value)
            changed = False
            for index, token in enumerate(tokens):
                if token.startswith('PYTHONPATH='):
                    updated = 'PYTHONPATH=' + source_path(token.partition('=')[2], root)
                    changed |= updated != token
                    tokens[index] = updated
            if changed:
                # Percent specifiers in existing values retain systemd's normal interpretation.
                raw = 'Environment=' + ' '.join(quoted(token) for token in tokens) + '\n'
        result.append(raw)
    return ''.join(result)


def directives(text):
    section = ''
    for _, line in logical_lines(text):
        if line.startswith('['):
            section = line
        key, separator, value = line.partition('=')
        if section == '[Service]' and separator and not line.startswith(('#', ';')):
            yield key.strip(), value.strip()


def expanded(value):
    return value.replace('%h', str(Path.home())).replace('%%', '%')


def plan(root, unit_dir, programs):
    """Inspect only services installed from ROOT, including their drop-ins and environment files."""
    changes = {}
    for program in programs:
        unit = unit_dir / (program + '.service')
        if not unit.is_file() or unit.is_symlink() and unit.resolve() == Path('/dev/null'):
            continue
        commands = [value for key, value in directives(unit.read_text()) if key == 'ExecStart' and value]
        if not commands:
            continue
        executable = Path(expanded(words(commands[-1])[0].lstrip('-')))
        if executable.parent.resolve() != (root / '.venv/bin').resolve():
            continue  # Another installation's unit must retain its source and settings.
        for path in (unit, *sorted(unit_dir.glob(program + '.service.d/*.conf'))):
            text = path.read_text()
            for key, value in directives(text):
                if key == 'ExecStart' and value:
                    command = Path(expanded(words(value)[0].lstrip('-')))
                    if command.parent.resolve() != (root / '.venv/bin').resolve():
                        raise ValueError(f'{path} overrides the service command outside this checkout; update that override before restarting.')
                if key == 'UnsetEnvironment' and 'PYTHONPATH' in words(value):
                    raise ValueError(f'{path} removes PYTHONPATH; update that override before restarting.')
                if key == 'EnvironmentFile':
                    for name in words(value):
                        optional = name.startswith('-')
                        name = expanded(name.lstrip('-'))
                        matches = sorted(Path('/').glob(name.lstrip('/'))) if '*' in name else [Path(name)]
                        for environment in matches:
                            if optional and not environment.exists():
                                continue
                            previous = environment.read_text()
                            updated = rewrite(previous, root, environment_file=True)
                            if previous != updated:
                                changes[environment.resolve()] = previous, updated
            updated = rewrite(text, root)
            if text != updated:
                changes[path.resolve()] = text, updated
    return changes


def replace(path, contents):
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def apply(changes, backup_root):
    if not changes:
        return
    backup = backup_root / uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    manifest = backup / 'manifest.json'
    manifest.write_text(json.dumps({str(path): f'{index:02d}-{path.name}'
                                   for index, path in enumerate(changes)}, indent=2) + '\n')
    manifest.chmod(0o600)
    written = []
    try:
        for index, (path, (before, after)) in enumerate(changes.items()):
            if path.read_text() != before:
                raise ValueError(f'{path} changed during the update; retry the update.')
            saved = backup / f'{index:02d}-{path.name}'
            saved.write_text(before)
            saved.chmod(0o600)
            replace(path, after)
            written.append(path)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    except Exception:
        for path in reversed(written):
            if path.read_text() == changes[path][1]:
                replace(path, changes[path][0])
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=False)
        raise
    print(f'Updated {len(changes)} source override(s). Rollback copies: {backup}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--unit-dir', type=Path, required=True)
    parser.add_argument('--service', action='append', choices=PROGRAMS)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if ':' in str(root) or '%' in str(root):
        parser.error('The checkout path cannot contain a colon or a systemd percent specifier.')
    try:
        changes = plan(root, args.unit_dir, args.service or PROGRAMS)
        if args.apply:
            state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
            apply(changes, state / 'openweightgym/runtime-source-updates')
        else:
            for path in changes:
                print(f'Would select current checkout in {path}')
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Could not select updated service source: {error}\n')


if __name__ == '__main__':
    main()
