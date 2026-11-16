"""Refresh editable package registration when an installed checkout's pyproject changes.

An editable install can retain an old package map after git pull. Reinstall only this project,
without changing dependencies, before OWG starts services from a newly added package.
"""

import argparse
import hashlib
from pathlib import Path
import subprocess
import tomllib


def refresh(root: Path, run=subprocess.run):
    """Register the current package list once per project metadata revision; fail before restart."""
    project = root / 'pyproject.toml'
    if not project.is_file():
        return
    content = project.read_bytes()
    settings = tomllib.loads(content.decode())
    if settings.get('project', {}).get('name', '').lower() != 'openweightgym':
        return
    marker = root / '.venv/.owg-project-metadata'
    digest = hashlib.sha256(content).hexdigest()
    if marker.is_file() and marker.read_text().strip() == digest:
        return
    python = root / '.venv/bin/python'
    run([str(python), '-m', 'pip', 'install', '--no-deps', '--editable', str(root)],
        cwd=root, check=True, timeout=300)
    packages = sorted({name.split('.')[0] for name in settings.get('tool', {}).get('setuptools', {}).get('packages', [])})
    # Isolated mode excludes cwd and PYTHONPATH: the installed package map itself must work.
    probe = 'import importlib.util; names=' + repr(packages) + '; missing=[n for n in names if importlib.util.find_spec(n) is None]; assert not missing, missing'
    run([str(python), '-I', '-c', probe], cwd=root.parent, check=True, timeout=30)
    marker.write_text(digest + '\n')
    marker.chmod(0o600)


def main():
    """Refresh editable package discovery for the installed checkout or report a repair error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    try:
        refresh(args.root.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as problem:
        parser.exit(1, f'Could not refresh installed package metadata ({type(problem).__name__}). Retry owg update before restarting.\n')


if __name__ == '__main__':
    main()
