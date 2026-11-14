"""Owner-scoped, restart-visible optional classifier conversion jobs."""

import asyncio
import json
from pathlib import Path
import re
import shutil
import signal
import sys
import urllib.request

from loadcoach.classifier_sources import check_source
from loadcoach.database_jobs import DatabaseJobs

REFERENCE_COMMIT = 'fec868b6b44ac5fddac64ff87d318ccceaff27ea'


class ClassifierJobs:
    """Keep tooling outside model libraries and preserve the configured runtime."""

    def __init__(self, root, server, places, changed=lambda *paths: None):
        self.root = Path(root)
        self.server = server
        self.places = places
        self.changed = changed
        self.jobs = DatabaseJobs(self.root / 'jobs')

    def server_signature(self):
        """Identify the configured binary without executing it during inventory reads."""
        path = Path(shutil.which(str(self.server)) or self.server).expanduser().resolve()
        try:
            info = path.stat()
            return [str(path), info.st_size, info.st_mtime_ns]
        except OSError:
            return None

    def status(self):
        """Report tools readiness only after the installation receipt exists."""
        try:
            receipt = json.loads((self.root / 'installed.json').read_text())
        except (OSError, ValueError):
            receipt = {}
        signature = self.server_signature()
        installed = bool(receipt and (self.root / 'venv/bin/python').is_file()
                         and signature and receipt.get('server_signature') == signature)
        active = next((row for folder in self.jobs.root.iterdir() if (folder / 'job.json').is_file()
                       and (row := self.jobs.read(folder.name)).get('state') in ('queued', 'running')), None)
        return {'id': 'classifier_conversion', 'label': 'Classifier conversion tools',
                'installed': installed, 'enabled': True, 'available': installed,
                'version': receipt.get('commit', ''), 'build': receipt.get('commit', ''),
                'installing': bool(active),
                'why': active.get('progress', 'Queued') if active else '' if installed else
                    'The configured engine changed. Reinstall tools to match its build.' if receipt else
                    'Install on demand; CPU tools are separate from the inference runtime.'}

    async def command(self, identifier, *args, cwd=None, env=None):
        """Run an owned process group and terminate all children on service shutdown."""
        with (self.jobs.folder(identifier) / 'worker.log').open('ab') as log:
            process = await asyncio.create_subprocess_exec(*map(str, args), cwd=cwd, env=env,
                stdout=log, stderr=log, start_new_session=True)
            try:
                code = await process.wait()
            except asyncio.CancelledError:
                try:
                    __import__('os').killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), 15)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        __import__('os').killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                raise
        if code:
            raise ValueError(f'Conversion tool exited with status {code}; inspect the owning node’s job log.')

    async def install(self, owner):
        """Pin conversion source to the configured llama-server build, inside the tools directory."""
        if self.status()['installing']:
            raise ValueError('Wait for the active classifier tools operation to finish.')
        row = self.jobs.create(owner, 'classifier-tools', 'CPU conversion tools')

        async def worker(identifier):
            """Resolve a build commit, install isolated CPU dependencies and write the receipt last."""
            try:
                signature = self.server_signature()
                process = await asyncio.create_subprocess_exec(self.server, '--version',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                raw, _ = await process.communicate()
                match = re.search(r'commit ([a-f0-9]{7,40})', raw.decode(errors='replace'))
                if not match:
                    raise ValueError('The configured llama-server does not report a Git commit.')
                short = match[1]

                def resolve():
                    """Resolve only the reported commit through the official upstream API."""
                    with urllib.request.urlopen('https://api.github.com/repos/ggml-org/llama.cpp/commits/' + short, timeout=30) as response:
                        commit = json.load(response)['sha']
                    if not re.fullmatch('[a-f0-9]{40}', commit) or not commit.startswith(short):
                        raise ValueError('Could not verify the installed build’s source commit.')
                    return commit
                commit = await asyncio.to_thread(resolve)
                source = self.root / 'llama.cpp'
                source.mkdir(exist_ok=True)
                self.jobs.update(identifier, progress='Fetching the installed build’s converter', converter_commit=commit)
                if not (source / '.git').is_dir():
                    await self.command(identifier, 'git', 'init', source)
                    await self.command(identifier, 'git', '-C', source, 'remote', 'add', 'origin', 'https://github.com/ggml-org/llama.cpp.git')
                await self.command(identifier, 'git', '-C', source, 'fetch', '--depth=1', 'origin', commit)
                await self.command(identifier, 'git', '-C', source, 'checkout', '--detach', commit)
                venv = self.root / 'venv'
                if not (venv / 'bin/python').is_file():
                    await self.command(identifier, sys.executable, '-m', 'venv', venv)
                python = venv / 'bin/python'
                self.jobs.update(identifier, progress='Installing CPU conversion dependencies')
                uv = shutil.which('uv')
                pip = [uv, 'pip', 'install', '--python', python] if uv else [python, '-m', 'pip', 'install']
                await self.command(identifier, *pip, 'torch', '--index-url', 'https://download.pytorch.org/whl/cpu')
                reference = 'tinyjev @ git+https://github.com/ankit-aglawe/tinyjev.git@' + REFERENCE_COMMIT
                await self.command(identifier, *pip, source / 'gguf-py', 'numpy', 'safetensors', 'sentencepiece',
                    'transformers>=4.57,<5', 'protobuf', reference, 'mistral-common', 'peft', 'zstandard')
                if not signature or signature != self.server_signature():
                    raise ValueError('The configured llama-server changed during installation. Retry to use its new build.')
                receipt = {'commit': commit, 'server': str(self.server), 'server_signature': signature,
                           'torch': 'CPU', 'tinyjev_commit': REFERENCE_COMMIT}
                (self.root / 'installed.json').write_text(json.dumps(receipt))
                self.jobs.update(identifier, progress='Ready', receipt=receipt)
            except Exception as problem:
                self.jobs.update(identifier, state='failed', error=str(problem)[:500])
        return self.jobs.start(row, worker)

    async def convert(self, owner, path):
        """Accept a known inert bundle and reserve a sibling output without touching the sources."""
        if not self.status()['available']:
            raise ValueError('Install Classifier conversion tools in Components on this node first.')
        candidate = Path(path)
        source = candidate if candidate.is_dir() else candidate.parent
        roots = [Path(folder).resolve() for folder in self.places().values() if Path(folder).name == 'classifier']
        resolved = source.resolve()
        root = next((root for root in roots if root in resolved.parents and len(resolved.relative_to(root).parts) <= 5), None)
        if not root or source.is_symlink():
            raise ValueError('Choose a source bundle inside this node’s Classifier library.')
        check_source(source)
        output = root / (source.name + '-decision')
        if output.exists():
            raise ValueError('This bundle already has conversion output; inspect or move it before retrying.')
        row = self.jobs.create(owner, 'classifier-conversion', source.name)

        async def worker(identifier):
            """Run conversion and parity in the tools venv, then refresh the normal file index."""
            try:
                receipt = json.loads((self.root / 'installed.json').read_text())
                self.jobs.update(identifier, progress='Converting and checking 30 reference questions', output=str(output))
                # Run the installed OWG module as a script with its checkout on sys.path; no runtime override is installed.
                script = 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("loadcoach.classifier_convert",run_name="__main__")'
                await self.command(identifier, self.root / 'venv/bin/python', '-c', script, Path(__file__).resolve().parent.parent,
                    '--source', source, '--output', output, '--converter', self.root / 'llama.cpp',
                    '--server', self.server, '--commit', receipt['commit'])
                manifest = json.loads((output / 'manifest.json').read_text())
                self.jobs.update(identifier, state=manifest['state'], progress=manifest['state'],
                                 parity=manifest['parity'], manifest=str(output / 'manifest.json'))
            except Exception as problem:
                self.jobs.update(identifier, state='failed', error=str(problem)[:500])
            finally:
                if output.is_dir():
                    await asyncio.to_thread(self.changed, *[path for path in output.iterdir() if path.is_file()])
        return self.jobs.start(row, worker)

    async def close(self):
        """Cancel conversion subprocesses and preserve interrupted receipts across restart."""
        for task in list(self.jobs.tasks):
            task.cancel()
        await self.jobs.close()
