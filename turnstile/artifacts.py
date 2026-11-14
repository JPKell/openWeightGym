"""Optional model-file storage on the control node, independent of inference workers."""

import asyncio
import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from loadcoach import hub
from loadcoach.config import NO_STORAGE, model_places, shared_places, storage_places
from loadcoach.files import SPARE, carry, model_files_in, room, sha256_of
from loadcoach.file_index import FileIndex, tier_of
from loadcoach.gguf import facts, read_metadata
from mirrorwall.assets import supported, image_asset, facts as image_facts
from soundroom.model import audio_asset, facts as audio_facts


def checked_roots(values):
    """Validate optional, separate absolute cache and storage roots before saving."""
    if set(values) - {'models_folder', 'models_storage_folder'}:
        raise ValueError('Only cache and storage locations can be changed here.')
    roots = {name: values.get(name, '') for name in ('models_folder', 'models_storage_folder')}
    if any(not isinstance(value, str) or value and not Path(value).is_absolute() for value in roots.values()):
        raise ValueError('Cache and storage locations must be full paths, or empty to disable them.')
    if all(roots.values()):
        cache, storage = (Path(value).resolve() for value in roots.values())
        if cache == storage:
            return roots  # one folder for both: every file in it is in the cache and in storage at once
        cache_places = [Path(folder) for folder in model_places(str(cache), {}).values()]
        storage_places = [Path(folder) for folder in model_places('', {}, str(storage)).values()]
        if any(a == b or a in b.parents or b in a.parents for a in cache_places for b in storage_places):
            raise ValueError('Cache and storage must be one folder, or two folders that do not hold one another.')
    return roots


class FrontFiles:
    """Use the shared file formats and copy/download helpers without starting an engine."""

    def __init__(self, roots, log, index=None, every=lambda: 0):
        """Read the control node's current file roots on each operation and retain active downloads.

        `index` is where the list of the control node's files is kept between looks (none: in memory), and
        `every` how many seconds a look is good for, 0 meaning until a Refresh asks again.
        """
        self.roots, self.log, self.every = roots, log, every
        self.index = FileIndex(index, describe=lambda path: self.describe(path)[0], accept=self.path)
        self.web = httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.fetching, self.tasks, self.receiving = {}, set(), set()
        self.moving, self.transfer_failures = {}, {}
        self.cleanup, self.stopping = {}, threading.Event()
        # An upgraded installation may predate this kind. Create only its child
        # beneath an already mounted, configured root, never a missing root.
        for name, folder in self.places().items():
            if name in ('classifier', 'storage_classifier') and Path(folder).parent.is_dir():
                Path(folder).mkdir(exist_ok=True)

    def places(self):
        """Resolve configured tiers; empty paths leave control node storage disabled."""
        roots = checked_roots(self.roots())
        return model_places(roots['models_folder'], {}, roots['models_storage_folder'])

    def path(self, value):
        """Allow only supported files inside the control node's explicitly configured locations."""
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not supported(path):
            raise ValueError('Choose a model file from the configured control node folders.')
        resolved = path.resolve()
        if not any(Path(folder).resolve() in resolved.parents for folder in self.places().values()):
            raise ValueError('That file is outside the configured control node folders.')
        return path

    def describe(self, path):
        """Read the shared model, image or audio metadata without loading the model."""
        if 'classifier' in path.parts and path.suffix.lower() != '.gguf':
            from loadcoach.classifier_sources import source_facts
            return source_facts(path), {}
        if audio_asset(path):
            return audio_facts(path), {}
        if image_asset(path):
            return image_facts(path), {}
        metadata = read_metadata(str(path))
        description = facts(metadata)
        if 'classifier' in path.parts and description.get('what') == 'model':
            from loadcoach.classifier_sources import source_facts
            description = {**description, **source_facts(path)}
        return description, metadata

    def inventory(self, refresh=False):
        """List the control node's available folders and files from the kept index, reporting inaccessible locations inline."""
        spots = self.places()
        places, files, problems, shared = [], [], [], shared_places(spots)
        for name, folder in spots.items():
            if not Path(folder).is_dir():
                problems.append(f'{folder} is not available. Check the folder or mount on the control node.')
                continue
            try:
                places.append({'name': name, 'folder': folder, 'tier': tier_of(name), 'shared': name in shared, **room(folder), 'files': 0})
            except OSError as problem:
                problems.append(f'{folder}: {problem}')
        index = self.index.current({place['name']: place['folder'] for place in places}, refresh=refresh, every=self.every())
        problems += index.get('problems', [])
        for place in places:
            for kept in index['files'].values():
                if kept['place'] != place['name']:
                    continue
                place['files'] += kept['size']
                files.append({**{key: value for key, value in kept.items() if key != 'mtime'},
                              'used_by': [], 'last_used': None, 'moving': self.moving.get(kept['path']), 'shared': place['name'] in shared,
                              'transfer_failure': self.transfer_failures.get(kept['path'])})
        return {'places': places, 'files': files, 'cleanup': dict(self.cleanup), 'problems': problems,
                'checked': FileIndex.checked(index)}

    def changed(self, *paths):
        """Put the kept list right for files control node has just made, changed or removed."""
        spots = {name: folder for name, folder in self.places().items() if Path(folder).is_dir()}
        self.index.update(list(paths), spots)

    async def request(self, user, method, address, body=b''):
        """Serve the existing artifact contract locally under the caller's model permission."""
        def response(value, status=200):
            """Return a finite JSON response compatible with authenticated worker helpers."""
            return httpx.Response(status, json=value)

        if not user.can('manage-models'):
            return response({'error': {'message': 'Your account may not manage model files.'}}, 403)
        url = urlsplit(address)
        query = {name: values[-1] for name, values in parse_qs(url.query).items()}
        action = url.path.removeprefix('/loadcoach/')
        try:
            asked = json.loads(body or b'{}')
            if not isinstance(asked, dict):
                raise ValueError('Send a settings object.')
            if method == 'GET' and action == 'files':
                return response(await asyncio.to_thread(self.inventory, query.get('refresh') == '1'))
            if method == 'GET' and action == 'models':
                return response({'models': []})
            if method == 'GET' and action in ('file', 'files/hash'):
                path = self.path(query.get('path', ''))
                if action == 'files/hash':
                    return response({'sha256': await asyncio.to_thread(sha256_of, str(path))})
                description, metadata = await asyncio.to_thread(self.describe, path)
                return response({'path': str(path), 'size': path.stat().st_size, 'facts': description, 'metadata': metadata})
            if method == 'POST' and action == 'files/cleanup':
                return self.start_cleanup(user, asked)
            if method == 'POST' and action in ('files/copy', 'files/move', 'files/delete'):
                if self.cleanup.get('state') == 'running':
                    raise ValueError('Wait for cache maintenance to finish before moving or deleting files.')
                path = self.path(asked.get('path', ''))
                if not path.is_file():
                    raise ValueError('That file is no longer available on the control node.')
                if str(path) in self.moving:
                    return response({'error': {'message': 'That file is already being copied or moved.'}}, 409)
                if action == 'files/delete':
                    if asked.get('name') != path.name:
                        raise ValueError('Type the file name to confirm its deletion.')
                    size = path.stat().st_size
                    await asyncio.to_thread(path.unlink)
                    self.log.add('file', 'deleted', user.name, machine='front', path=str(path))
                    await asyncio.to_thread(self.changed, path)
                    return response({'deleted': str(path), 'freed': size})
                folder = self.places().get(asked.get('to'))
                if folder is None:
                    raise ValueError('Choose a configured destination folder on the control node.')
                relative = Path(asked.get('relative', path.name))
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('The destination must stay inside the selected folder.')
                target = self.path(str(Path(folder) / relative))
                if target.exists():
                    return response({'error': {'message': 'The destination already exists. No file was overwritten.'}}, 409)
                target.parent.mkdir(parents=True, exist_ok=True)
                if room(str(target.parent))['free'] < path.stat().st_size + SPARE:
                    return response({'error': {'message': 'The destination needs enough free space plus 1 GiB headroom.'}}, 409)
                return self.start_transfer(user, path, target, asked['to'], copy=action == 'files/copy')
            if method == 'GET' and action == 'downloads':
                incomplete = {}
                for place, folder in self.places().items():
                    for part in Path(folder).glob('*--*/*.part'):
                        if part.is_symlink():
                            continue
                        path = str(part)[:-5]
                        self.path(path)
                        incomplete[path] = {'path': path, 'repository': part.parent.name.replace('--', '/'),
                                            'file': Path(path).name, 'place': place, 'state': 'stopped',
                                            'done': part.stat().st_size, 'of': None, 'speed': 0}
                return response({'downloads': [{key: value for key, value in entry.items() if key not in ('task', 'start', 'since')}
                                               for entry in {**incomplete, **self.fetching}.values()]})
            if method == 'GET' and action == 'hub':
                repository = query.get('repository', '')
                if not repository:
                    if query.get('paged') == '1':
                        if query.get('source') == 'civitai':
                            from mirrorwall.civitai import search_page
                            return response(await search_page(self.web, query.get('search', ''),
                                adult=query.get('adult') == 'true', cursor=query.get('cursor', ''),
                                kind=query.get('kind', ''), base=query.get('base', '')))
                        sort, direction = hub.search_order(query)
                        return response(await hub.search_page(self.web, query['search'], cursor=query.get('cursor', ''), sort=sort, direction=direction, kind=query.get('kind', ''))
                                        if query.get('search') else {'found': [], 'next_cursor': ''})
                    if query.get('source') == 'civitai':
                        from mirrorwall.civitai import search
                        return response({'found': await search(self.web, query['search'], adult=query.get('adult') == 'true')
                                         if query.get('search') else []})
                    return response({'found': await hub.search(self.web, query['search']) if query.get('search') else []})
                if not hub.REPOSITORY.fullmatch(repository):
                    raise ValueError('A repository is written OWNER/NAME.')
                rows = hub.rows(await hub.files(self.web, repository, classifier=query.get('kind') == 'classifier'))
                if query.get('kind') == 'classifier':
                    for row in rows:
                        row['decision_ready'] = row['file'].lower().endswith('.gguf') and await hub.decision_header(self.web, repository, row['file'])
                spots = self.places()
                for row in rows:
                    names = [Path(part['path']).name for part in row['parts']]
                    row['here'] = next((name for name, folder in spots.items()
                                        if all((Path(folder) / hub.folder_of(repository) / name).exists() for name in names)), None)
                    row['parts'] = len(names)
                return response({'repository': repository, 'files': rows, 'places': [
                    {'name': name, 'free': room(folder)['free']} for name, folder in sorted(storage_places(spots).items()) if Path(folder).is_dir()]})
            if method == 'POST' and action == 'downloads':
                return await self.download(user, asked)
            if method == 'POST' and action in ('downloads/stop', 'downloads/discard'):
                path = self.path(asked.get('path', ''))
                current = self.fetching.get(str(path), {})
                if action.endswith('stop'):
                    if not current.get('task'):
                        raise ValueError('No download to that path is running.')
                    current['task'].cancel()
                    await asyncio.gather(current['task'], return_exceptions=True)
                else:
                    if current.get('task'):
                        raise ValueError('Stop the download before discarding it.')
                    path.with_name(path.name + '.part').unlink(missing_ok=True)
                    self.fetching.pop(str(path), None)
                return response({})
            return response({'error': {'message': 'Choose a compute node for model configuration or cache maintenance.'}}, 409)
        except hub.Refused as problem:
            return response({'error': {'message': str(problem)}}, problem.status)
        except (OSError, ValueError, TypeError, KeyError) as problem:
            return response({'error': {'message': str(problem)}}, 400)

    def start_transfer(self, user, source, target, place, *, copy):
        """Accept a copy or move immediately and publish progress outside the HTTP request."""
        path = str(source)
        progress = {'to': place, 'done': 0, 'of': source.stat().st_size, 'copy': copy}
        self.moving[path] = progress
        self.transfer_failures.pop(path, None)

        async def run():
            """Keep the source until publication succeeds and retain errors for the file listing."""
            try:
                await asyncio.to_thread(carry, source, target, progress)
                if not copy:
                    await asyncio.to_thread(source.unlink)
                self.log.add('file', 'copied' if copy else 'moved', user.name,
                             machine='front', path=path, to=str(target))
                await asyncio.to_thread(self.changed, source, target)
            except (OSError, ValueError) as problem:
                self.transfer_failures[path] = str(problem)
                self.log.add('file', 'copy-failed' if copy else 'move-failed', user.name,
                             machine='front', path=path, to=str(target), why=str(problem))
            finally:
                self.moving.pop(path, None)

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return httpx.Response(202, json={'copying' if copy else 'moving': path, 'to': place,
                                       'path': str(target), 'target': str(target), 'size': progress['of']})

    def start_cleanup(self, user, asked):
        """Consolidate cache or clear only files with a verified retained storage copy."""
        if self.tasks or self.receiving:
            return httpx.Response(409, json={'error': {'message': 'Wait for active control node file transfers to finish.'}})
        mode = asked.get('mode')
        if mode not in ('clear-cache', 'promote'):
            raise ValueError('Choose cache consolidation or verified cache cleanup.')
        spots = self.places()
        # A folder that is the cache and storage at once is left alone: its files are the storage copies themselves.
        spots = {name: folder for name, folder in spots.items() if name not in shared_places(spots)}
        cached = {name: Path(folder) for name, folder in spots.items() if not name.startswith('storage_')}
        stored = {name.removeprefix('storage_'): Path(folder) for name, folder in spots.items() if name.startswith('storage_')}
        if mode == 'clear-cache' and not stored:
            raise ValueError('The cache and storage are one folder: there is no separate cache to clear.' if shared_places(self.places())
                             else 'Configure a storage library before clearing the cache.')
        keep, promote = asked.get('keep', []), asked.get('promote', [])
        if not isinstance(keep, list) or not isinstance(promote, list):
            raise ValueError('Keep and promote must be lists of model-file paths.')
        protected = {str(self.path(path).resolve()) for path in keep}
        sources = [self.path(path) for path in promote]
        if any(not source.is_file() or not any(root.resolve() in source.resolve().parents for root in stored.values()) for source in sources):
            raise ValueError('Promoted files must be in the control node storage.')
        self.cleanup = {'mode': mode, 'state': 'running', 'since': time.time(), 'saved': 0, 'removed': 0,
                        'freed': 0, 'promoted': 0, 'protected': 0, 'failures': []}

        def work():
            """Copy and verify outside the event loop; never remove the only verified copy."""
            operations = []
            if mode == 'promote':
                for source in sources:
                    place, root = next((name, root) for name, root in stored.items() if root.resolve() in source.resolve().parents)
                    operations.append((source, cached.get(place), source.relative_to(root)))
            else:
                for place, root in cached.items():
                    operations.extend((source, stored.get(place), source.relative_to(root)) for source in model_files_in(str(root)))
            for source, folder, relative in operations:
                try:
                    if self.stopping.is_set():
                        raise InterruptedError('Control node file maintenance stopped.')
                    if folder is None:
                        raise ValueError(f'{source.name}: no matching {"cache" if mode == "promote" else "storage"} folder.')
                    target = self.path(str(folder / relative))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    size = source.stat().st_size
                    if not target.exists():
                        if room(str(target.parent))['free'] < size + SPARE:
                            raise ValueError(f'{target.parent}: not enough free space.')
                        carry(source, target, {'done': 0, 'of': size})
                        self.cleanup['promoted' if mode == 'promote' else 'saved'] += 1
                    if target.stat().st_size != size or sha256_of(str(source), self.stopping) != sha256_of(str(target), self.stopping):
                        raise ValueError(f'{source.name}: cache and storage copies differ. Both were kept.')
                    if mode == 'clear-cache':
                        if str(source.resolve()) in protected:
                            self.cleanup['protected'] += 1
                        else:
                            if self.stopping.is_set():
                                raise InterruptedError('Control node file maintenance stopped.')
                            source.unlink()
                            self.cleanup['removed'] += 1
                            self.cleanup['freed'] += size
                except (OSError, ValueError) as problem:
                    self.cleanup['failures'].append(str(problem))

        async def run():
            """Publish terminal maintenance state and retain progress for the Files page."""
            try:
                await asyncio.to_thread(work)
                self.cleanup.update(state='complete', ended=time.time())
                self.log.add('file', 'cache-cleaned', user.name, machine='front', **self.cleanup)
            except asyncio.CancelledError:
                self.cleanup.update(state='failed', failure='Control node file maintenance stopped.')
                raise
            except (OSError, ValueError) as problem:
                self.cleanup.update(state='failed', failure=str(problem))
            finally:
                await asyncio.to_thread(self.index.stale)

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return httpx.Response(202, json=dict(self.cleanup))

    async def receive(self, user, place, relative, pieces, size, digest, progress, source=''):
        """Write a file sent from a compute node into a control node place, and publish it only once its size and SHA-256 match.

        `pieces` yields its bytes. Until then it is a hidden file beside where it goes, which no listing
        shows; a failed or interrupted copy leaves nothing behind.
        """
        if self.cleanup.get('state') == 'running':
            raise ValueError('Wait for cache maintenance to finish before copying files to the control node.')
        folder = self.places().get(place)
        if folder is None or not Path(folder).is_dir():
            raise ValueError(f'Control node has no folder for {place}. Set its locations in Settings.')
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or len(parts) > 6 or any(part in ('.', '..') for part in parts):
            raise ValueError('The destination must stay inside the selected folder.')
        target = self.path(str(Path(folder).joinpath(*parts)))
        if target.exists() or str(target) in self.receiving:
            raise ValueError(f'{place} already has {relative}. No file was overwritten.')
        target.parent.mkdir(parents=True, exist_ok=True)
        if room(str(target.parent))['free'] < size + SPARE:
            raise ValueError(f'{place} on the control node needs room for the file plus 1 GiB to spare.')
        self.receiving.add(str(target))
        part = target.with_name(f'.{target.name}.receiving')
        try:
            hashed, done = hashlib.sha256(), 0
            with open(part, 'wb') as file:
                async for piece in pieces:
                    done += len(piece)
                    if done > size:
                        raise ValueError(f'{target.name} is larger than it was said to be.')
                    hashed.update(piece)
                    await asyncio.to_thread(file.write, piece)
                    progress['done'] = done
                await asyncio.to_thread(os.fsync, file.fileno())
            if done != size or hashed.hexdigest() != digest:
                raise ValueError(f'{target.name} did not arrive whole: its size or SHA-256 differs from the original.')
            if target.exists():
                raise ValueError(f'{place} already has {relative}. No file was overwritten.')
            os.replace(part, target)
            self.log.add('file', 'received', user.name, machine='front', path=str(target), source=source)
            await asyncio.to_thread(self.changed, target)
            return target
        finally:
            part.unlink(missing_ok=True)
            self.receiving.discard(str(target))

    async def download(self, user, asked):
        """Download verified public repository files into control node storage; the cache is never a destination."""
        repository, names, place = asked.get('repository', ''), asked.get('files'), asked.get('place')
        if self.cleanup.get('state') == 'running':
            raise ValueError('Wait for cache maintenance to finish before downloading files.')
        if not isinstance(repository, str) or not hub.REPOSITORY.fullmatch(repository) or not isinstance(names, list) or not names:
            raise ValueError('Choose a repository, files and a destination.')
        stored = storage_places(self.places())
        if not stored:
            raise ValueError(NO_STORAGE)
        folder = stored.get(place)
        if place == 'storage_classifier' and folder and Path(folder).parent.is_dir():
            Path(folder).mkdir(exist_ok=True)
        if folder is None or not Path(folder).is_dir():
            raise ValueError('Downloads go into storage, never the cache. Choose a control node storage folder that exists.')
        entries = hub.chosen(await hub.files(self.web, repository, classifier=Path(folder).name == 'classifier'), names)
        targets = [self.path(str(Path(folder) / hub.folder_of(repository) / Path(entry['path']).name)) for entry in entries]
        if any(target.with_name(target.name + '.part').is_symlink() for target in targets):
            raise ValueError('A partial download cannot be a symbolic link.')
        if len(set(targets)) != len(targets) or any(target.exists() or self.fetching.get(str(target), {}).get('task') for target in targets):
            raise ValueError('A selected file already exists or is downloading. No download was started.')
        owed = sum(max(0, entry['of'] - entry['done']) for entry in self.fetching.values() if entry.get('task'))
        if room(folder)['free'] < sum(entry['size'] for entry in entries) + owed + SPARE:
            raise ValueError('There is not enough free space for these downloads plus 1 GiB headroom.')
        for entry, target in zip(entries, targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            progress = self.fetching[str(target)] = {'path': str(target), 'repository': repository, 'file': target.name,
                                                    'place': place, 'state': 'coming', 'done': 0, 'of': entry['size'], 'speed': 0}

            async def receive(entry=entry, target=target, progress=progress):
                """Keep transfer state and incomplete files if stopped or interrupted."""
                try:
                    await hub.fetch(self.web, repository, entry, target, '', progress)
                    self.fetching.pop(str(target), None)
                    self.log.add('file', 'downloaded', user.name, machine='front', path=str(target))
                    await asyncio.to_thread(self.changed, target)
                except asyncio.CancelledError:
                    progress.update(state='stopped')
                    raise
                except (OSError, httpx.HTTPError, hub.Refused) as problem:
                    progress.update(state='failed', why=str(problem))
                finally:
                    progress.pop('task', None)

            task = asyncio.create_task(receive())
            progress['task'] = task
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return httpx.Response(202, json={'downloading': [str(target) for target in targets], 'to': place,
                                       'size': sum(entry['size'] for entry in entries)})

    async def close(self):
        """Stop transfers before closing the control node storage HTTP client."""
        self.stopping.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.web.aclose()
