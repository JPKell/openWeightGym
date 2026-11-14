#!/usr/bin/env python3
"""Install a pinned whisper.cpp runtime through the ordinary optional-engine workflow."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from soundroom.install import configure, digest, run
from engines.build import build_environment

REVISION = '48f628a84833905ee4a0658ee6d4a5c915ce1997'  # v1.8.7
REPOSITORY = 'https://github.com/ggml-org/whisper.cpp.git'
PROGRAMS = ('whisper-server', 'whisper-cli')


def verify(folder):
    """Verify release identities and required server flags without loading weights."""
    manifest = json.loads((folder/'installation.json').read_text())
    for name in PROGRAMS:
        file = folder/'bin'/name
        if not file.is_file() or not os.access(file, os.X_OK) or digest(file) != manifest['executables'][name]:
            raise ValueError('whisper.cpp executable verification failed: '+name)
    answer = run([folder/'bin/whisper-server', '--help'], capture_output=True, text=True, timeout=30)
    help_text = answer.stdout+answer.stderr
    for flag in ('--model', '--host', '--port', '--no-gpu', '--language', '--threads'):
        if flag not in help_text:
            raise ValueError('whisper.cpp does not expose '+flag)
    return manifest


def configured(root):
    """Resolve only the runtime configured for this installation."""
    file = root/'settings/loadcoach.toml'
    options = tomllib.loads(file.read_text()).get('engines', {}).get('whispercpp', {}) if file.exists() else {}
    return Path(options['manifest']).parent if options.get('enabled') and options.get('manifest') else None


def main():
    """Build, verify and select an immutable native release without restarting services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--prefix', type=Path)
    parser.add_argument('--backend', choices=('cuda', 'vulkan', 'cpu'), default='cuda')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--jobs', type=int, default=6)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--update', action='store_true', help='Update only an already configured runtime, preserving its backend and prefix')
    parser.add_argument('--runtime-only', action='store_true', help='Install native binaries without activating an engine unsupported by the currently installed application')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.jobs <= 32:
        parser.error('Use 1–32 build jobs')
    args.root = args.root.expanduser().resolve()
    prefix = (args.prefix or args.root/'runtimes/whispercpp').expanduser().resolve()
    if args.update:
        folder = configured(args.root)
        if folder is None:
            return
        installed = verify(folder)
        args.backend = installed['backend']
        prefix = folder.resolve().parent.parent
    if args.check:
        folder = prefix/'current' if args.prefix else configured(args.root)
        print(json.dumps(verify(folder), indent=2) if folder else 'whisper.cpp: not installed (optional)')
        return
    target = prefix/'releases'/(REVISION+'-'+args.backend)
    print(f'whisper.cpp: {args.backend}; revision {REVISION}; target {target}')
    if args.dry_run:
        return
    if not target.exists():
        environment = build_environment(args.backend, 'whisper.cpp')
        source = args.source.resolve() if args.source else prefix/'source'/REVISION
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            run(['git', 'clone', REPOSITORY, source])
            run(['git', '-C', source, 'checkout', '--detach', REVISION])
        revision = run(['git', '-C', source, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        if revision != REVISION:
            raise ValueError('Source checkout is not at the pinned whisper.cpp revision')
        run(['git', '-C', source, 'diff', '--quiet', 'HEAD', '--'])
        build = prefix/'build'/(REVISION+'-'+args.backend)
        flags = ['-DCMAKE_BUILD_TYPE=Release', '-DBUILD_SHARED_LIBS=OFF', '-DWHISPER_BUILD_TESTS=OFF',
                 '-DWHISPER_BUILD_SERVER=ON', '-DGGML_NATIVE=OFF',
                 '-DGGML_CUDA='+('ON' if args.backend == 'cuda' else 'OFF'),
                 '-DGGML_VULKAN='+('ON' if args.backend == 'vulkan' else 'OFF')]
        if args.backend == 'cuda':
            flags += ['-DCMAKE_CUDA_COMPILER='+environment['CUDACXX'], '-DCMAKE_CUDA_ARCHITECTURES=native']
        run(['cmake', '-S', source, '-B', build, *flags], env=environment)
        run(['cmake', '--build', build, '--target', *PROGRAMS, '--parallel', args.jobs], env=environment)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=target.parent) as directory:
            stage = Path(directory)
            (stage/'bin').mkdir()
            for name in PROGRAMS:
                shutil.copy2(build/'bin'/name, stage/'bin'/name)
            manifest = {'version': 1, 'release': 'v1.8.7', 'repository': REPOSITORY, 'revision': REVISION,
                        'backend': args.backend, 'build_flags': flags, 'build_cache_sha256': digest(build/'CMakeCache.txt'),
                        'executables': {name: digest(stage/'bin'/name) for name in PROGRAMS}}
            (stage/'installation.json').write_text(json.dumps(manifest, indent=2)+'\n')
            verify(stage)
            stage.rename(target)
    verify(target)
    link = prefix/'.current-new'
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    link.replace(prefix/'current')
    if not args.runtime_only:
        configure(args.root/'settings/loadcoach.toml', target/'bin/whisper-server', target/'bin/whisper-cli',
                  target/'installation.json', args.root/'data/installation/whispercpp-backups', engine='whispercpp')
    receipt = args.root/'.install-receipt'
    if receipt.is_file():
        before = receipt.read_text()
        lines = [line for line in before.splitlines() if not line.startswith('whispercpp_version=')]
        temporary = receipt.with_name('.install-receipt.whispercpp-new')
        temporary.write_text('\n'.join([*lines, 'whispercpp_version='+REVISION])+'\n')
        temporary.chmod(receipt.stat().st_mode & 0o777)
        temporary.replace(receipt)
    print('whisper.cpp installed'+(' without activation' if args.runtime_only else ' and configured')+'. No service was restarted; model weights are separate.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit('whisper.cpp installation failed: '+str(error)) from None
