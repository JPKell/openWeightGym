"""Fleet database inventory, export orchestration and owner-scoped operation receipts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
from starlette.responses import JSONResponse, FileResponse, StreamingResponse

from loadcoach.database_admin import async_summaries, merge_inventory, make_sql_dump, source_identity
from loadcoach.database_jobs import DatabaseJobs
from loadcoach.database_operations import OperationRefused
from db.recovery import checksum

MAX_EXPORT_BYTES = 2 * 1024 ** 3


def owner_of(person):
    return person.me.get('identity') or person.name


class DatabaseConsole:
    def __init__(self, sources, health, local_operations, ask, client, root):
        self.sources, self.health, self.local = sources, health, local_operations
        self.ask, self.client = ask, client
        self.jobs = DatabaseJobs(root)

    @staticmethod
    def remote_path(machine, operation):
        return '/turnstile/machines/' + quote(machine, safe='') + '/databases/' + operation

    async def snapshot(self, person):
        inventory_error = ''
        try:
            answer = await self.ask('GET', '/turnstile/machines?include_retired=1', person.key, timeout=5)
            if not answer.is_success or not isinstance(answer.json().get('machines'), list):
                raise ValueError()
            configured = answer.json()['machines']
        except Exception:
            configured = []
            inventory_error = 'The configured compute-node inventory is unavailable. Complete fleet exports are blocked.'
        semaphore = asyncio.Semaphore(4)

        async def remote(node):
            result = dict(node, databases=[], tools={}, health=[], answering=False, error='')
            if node.get('lifecycle') == 'retired' or node.get('configured') is False:
                result['error'] = 'Retired or unconfigured; excluded from active scope.'
                return result
            async with semaphore:
                try:
                    answer = await self.ask('GET', self.remote_path(node['name'], 'inventory'), person.key, timeout=15)
                    if not answer.is_success:
                        raise ValueError()
                    found = answer.json()
                    if not all('identity' in row for row in found.get('databases', [])):
                        raise ValueError()
                    result.update(databases=found['databases'], tools=found.get('tools', {}), health=found.get('health', []), answering=True)
                    for row in [*result['databases'], *result['health']]:
                        row['machine'] = node['name']
                except Exception:
                    result['error'] = f"{node['name']} is not answering with a supported database inventory. No other node was used."
            return result
        front_rows, front_health, remote_nodes = await asyncio.gather(
            async_summaries(self.sources(), 'front'), self.health(),
            asyncio.gather(*(remote(node) for node in configured)))
        from loadcoach.database_admin import dump_tools
        nodes = [dict(name='front', answering=True, databases=front_rows, health=front_health, tools=dump_tools(), error='', lifecycle='enabled'), *remote_nodes]
        health = [store for node in nodes for store in node['health']]
        for node in nodes:
            for row in node['databases']:
                matching = [store for store in node['health'] if store['engine'].lower() == row['engine'].lower()
                            and set(store.get('bindings', [])) & set(row.get('bindings', []))
                            and store['display_name'] == row['path']]
                row['backup'] = matching[0]['backup'] if matching else {'state': 'unknown', 'age_seconds': None}
        rows = merge_inventory([row for node in nodes for row in node['databases']])
        for row in rows:
            row['key'] = hashlib.sha256((row.get('identity') or row['machine'] + '/' + row['id']).encode()).hexdigest()[:24]
            row['detail_url'] = f"/databases/{quote(row['machine'], safe='')}/{quote(row['id'], safe='')}/details"
            row['dump_url'] = f"/databases/{quote(row['machine'], safe='')}/{quote(row['id'], safe='')}/dump"
        active = [row for row in rows if row['expected']]
        active_nodes = [node for node in nodes if node.get('lifecycle') != 'retired' and node.get('configured', True)]
        return dict(databases=active, discovered=[row for row in rows if not row['expected']],
                    nodes=[{key: value for key, value in node.items() if key not in {'databases', 'health'}} for node in nodes],
                    store_health=health, inventory_error=inventory_error, checked_at=time.time(),
                    complete=not inventory_error and all(node['answering'] for node in active_nodes) and all(not row['error'] for row in active))

    async def stream(self, person, machine, operation, media_type=None):
        request = self.client.build_request('GET', self.remote_path(machine, operation),
            headers={'authorization': f'Bearer {person.key}', 'x-turnstile-no-wake': '1'}, timeout=httpx.Timeout(910, connect=2))
        answer = await self.client.send(request, stream=True)
        if not answer.is_success:
            await answer.aclose()
            raise OperationRefused('The owning node refused the download or is unreachable.')
        async def chunks():
            try:
                async for chunk in answer.aiter_bytes():
                    yield chunk
            finally:
                await answer.aclose()
        headers = {key: value for key, value in answer.headers.items() if key in {'content-disposition', 'x-suite-dump-method'}}
        return StreamingResponse(chunks(), media_type=media_type or answer.headers.get('content-type'), headers={**headers, 'cache-control': 'no-store'})

    async def export(self, person, body):
        snapshot = await self.snapshot(person)
        scope = body.get('scope')
        partial = body.get('partial') is True
        if scope == 'all':
            if not snapshot['complete'] and not partial:
                raise OperationRefused('The fleet inventory is incomplete. Restore node access or explicitly choose a partial export.')
            selected = [row for row in snapshot['databases'] if not row['error']]
        elif isinstance(scope, list) and scope:
            indexed = {row['key']: row for row in [*snapshot['databases'], *snapshot['discovered']]}
            if len(scope) > 200 or any(key not in indexed for key in scope):
                raise OperationRefused('The selection changed. Refresh inventory before export.')
            selected = [indexed[key] for key in dict.fromkeys(scope)]
            if any(row['error'] for row in selected):
                raise OperationRefused('A selected database is unavailable.')
        else:
            raise OperationRefused('Choose all databases or an explicit selection.')
        if not selected:
            raise OperationRefused('No reachable database stores are selected.')
        omissions = [dict(machine=node['name'], reason=node['error']) for node in snapshot['nodes'] if not node['answering'] and node.get('lifecycle') != 'retired']
        omissions += [dict(machine=row['machine'], database=row['id'], reason=row['error']) for row in snapshot['databases'] if row['error']]
        if snapshot['inventory_error']:
            omissions.append({'reason': snapshot['inventory_error']})
        row = self.jobs.create(owner_of(person), 'export', selected)
        self.local.audit('export-started', owner_of(person), job=row['id'], databases=len(selected), partial=partial)
        folder = self.jobs.folder(row['id'])

        async def worker(identifier):
            outcomes, total = [], 0
            manifest = {'schema': 1, 'kind': 'SQL export', 'consistency': 'independent per-database snapshots',
                        'requested_scope': scope, 'partial_requested': partial, 'omissions': omissions, 'stores': outcomes}
            try:
                for store in selected:
                    if self.jobs.cancelled(identifier):
                        break
                    if shutil.disk_usage(folder).free < max(store['size'] or 0, 64 * 1024 ** 2) * 2 + 64 * 1024 ** 2:
                        raise OperationRefused('Insufficient local space to build the SQL archive.')
                    started = time.time()
                    target = folder / (store['key'] + '.sql')
                    entry = dict(machine=store['machine'], database=store['id'], identity=store['identity'],
                                 engine=store['engine'], consumers=store['consumers'], snapshot_started_at=started, state='running')
                    outcomes.append(entry)
                    self.jobs.update(identifier, outcomes=outcomes)
                    if store['machine'] == 'front':
                        source = self.sources().get(store['id'])
                        if source is None or await asyncio.to_thread(source_identity, source) != store['identity']:
                            raise OperationRefused('A selected front store changed identity; export stopped.')
                        temporary, method = await asyncio.to_thread(make_sql_dump, source)
                        try:
                            if temporary.stat().st_size + total > MAX_EXPORT_BYTES:
                                raise OperationRefused('Export exceeds the 2 GiB archive input limit. Export stores separately.')
                            await asyncio.to_thread(shutil.move, str(temporary), target)
                        finally:
                            temporary.unlink(missing_ok=True)
                    else:
                        from urllib.parse import urlencode
                        path = self.remote_path(store['machine'], 'stores/' + quote(store['id'], safe='') + '/dump') + '?' + urlencode({'identity': store['identity']})
                        remote_request = self.client.build_request('GET', path, headers={'authorization': f'Bearer {person.key}', 'x-turnstile-no-wake': '1'}, timeout=httpx.Timeout(910, connect=2))
                        answer = await self.client.send(remote_request, stream=True)
                        try:
                            if not answer.is_success:
                                raise OperationRefused('A selected node refused its dump. Check its binding, engine and tools.')
                            method = answer.headers.get('x-suite-dump-method', '')
                            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                            with os.fdopen(descriptor, 'wb') as output:
                                async for chunk in answer.aiter_bytes():
                                    if self.jobs.cancelled(identifier):
                                        raise OperationRefused('Export cancelled during download.')
                                    total += len(chunk)
                                    if total > MAX_EXPORT_BYTES or shutil.disk_usage(folder).free < 64 * 1024 ** 2:
                                        raise OperationRefused('Export exceeded its disk-space budget. Export stores separately.')
                                    output.write(chunk)
                        finally:
                            await answer.aclose()
                    if store['machine'] == 'front':
                        total += target.stat().st_size
                    entry.update(state='complete', snapshot_completed_at=time.time(), size=target.stat().st_size,
                                 sha256=await asyncio.to_thread(checksum, target), file=store['machine'] + '/' + store['id'] + '.sql', method=method)
                    self.jobs.update(identifier, outcomes=outcomes)
                if self.jobs.cancelled(identifier):
                    return
                archive = folder / 'export.zip'
                def package():
                    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as output, ZipFile(output, 'w', ZIP_DEFLATED) as bundle:
                        for entry in outcomes:
                            key = next(store['key'] for store in selected if store['identity'] == entry['identity'])
                            bundle.write(folder / (key + '.sql'), entry['file'])
                        bundle.writestr('manifest.json', json.dumps(manifest, indent=2))
                await asyncio.to_thread(package)
                self.jobs.update(identifier, download='export.zip', download_expires_at=time.time() + 86400,
                                 state='partial' if omissions else 'complete', completed_at=time.time(), outcomes=outcomes)
                self.local.audit('export-finished', owner_of(person), job=identifier, databases=len(outcomes), partial=bool(omissions))
            except Exception as problem:
                if outcomes and outcomes[-1]['state'] == 'running':
                    outcomes[-1]['state'] = 'failed'
                self.jobs.update(identifier, outcomes=outcomes, state='cancelled' if self.jobs.cancelled(identifier) else 'failed',
                                 error=str(problem) if isinstance(problem, OperationRefused) else 'Export failed. Check the owning node, tools, permissions and disk space.')
                self.local.audit('export-failed', owner_of(person), job=identifier)
            finally:
                for path in folder.glob('*.sql'):
                    path.unlink(missing_ok=True)
                if not self.jobs.read(identifier).get('download'):
                    (folder / 'export.zip').unlink(missing_ok=True)
        return self.jobs.start(row, worker)

    async def preview(self, person, body):
        snapshot = await self.snapshot(person)
        indexed = {row['key']: row for row in snapshot['databases']}
        keys = body.get('scope')
        if not isinstance(keys, list) or not keys or len(keys) > 100 or any(key not in indexed for key in keys):
            raise OperationRefused('Select configured databases from a fresh inventory.')
        groups = {}
        for key in dict.fromkeys(keys):
            store = indexed[key]
            if store['error']:
                raise OperationRefused('A selected store is unreachable. Refresh and select reachable stores.')
            groups.setdefault(store['machine'], []).append({'id': store['id'], 'identity': store['identity']})
        plan, stores = [], []
        for machine, scope in groups.items():
            request = dict(action=body.get('action'), cutoff=body.get('cutoff'), mode=body.get('mode', 'full'), scope=scope)
            if machine == 'front':
                review = await asyncio.to_thread(self.local.preview, owner_of(person), request)
            else:
                answer = await self.ask('POST', self.remote_path(machine, 'preview'), person.key, json=request, timeout=60)
                if not answer.is_success:
                    raise OperationRefused(answer.json().get('error', {}).get('message', 'The owning node refused its maintenance preview.'))
                review = answer.json()
            plan.append({'machine': machine, 'review': review['review']})
            stores.extend({'machine': machine, **store} for store in review['stores'])
        if body.get('action') == 'purge' and not any(store['count'] for store in stores):
            raise OperationRefused('No eligible telemetry readings match this cutoff. Other tables remain protected.')
        row = self.jobs.create(owner_of(person), 'review', keys, plan=plan)
        return dict(review=row['id'], action=body.get('action'), stores=stores, expires_at=row['expires_at'])

    async def execute(self, person, body):
        if body.get('confirmed') is not True:
            raise OperationRefused('Confirm the reviewed purge or maintenance locks first.')
        plan = self.jobs.consume(body.get('review'), owner_of(person))
        row = self.jobs.create(owner_of(person), 'maintenance', plan)

        async def worker(identifier):
            outcomes = []
            for node in plan:
                if self.jobs.cancelled(identifier):
                    break
                outcome = dict(machine=node['machine'], state='starting')
                outcomes.append(outcome)
                self.jobs.update(identifier, outcomes=outcomes)
                try:
                    if node['machine'] == 'front':
                        running = await self.local.execute(owner_of(person), node['review'], True)
                    else:
                        outcome['state'] = 'unknown'
                        answer = await self.ask('POST', self.remote_path(node['machine'], 'execute'), person.key,
                            json={'review': node['review'], 'confirmed': True}, timeout=30)
                        if not answer.is_success:
                            if answer.status_code < 500:
                                outcome['state'] = 'failed'
                            raise OperationRefused('Node did not confirm execution. Inspect that node before retrying; no automatic retry was sent.')
                        running = answer.json()
                    outcome.update(job=running['id'], state='running')
                    self.jobs.update(identifier, outcomes=outcomes)
                    started = time.monotonic()
                    while True:
                        if node['machine'] == 'front':
                            if self.jobs.cancelled(identifier):
                                self.local.jobs.update(running['id'], cancel_requested=True)
                            status = self.local.jobs.get(running['id'], owner_of(person))
                        else:
                            if self.jobs.cancelled(identifier):
                                await self.ask('POST', self.remote_path(node['machine'], 'jobs/' + running['id'] + '/cancel'), person.key, json={}, timeout=5)
                            answer = await self.ask('GET', self.remote_path(node['machine'], 'jobs/' + running['id']), person.key, timeout=8)
                            if not answer.is_success:
                                raise OperationRefused('The maintenance node stopped answering. Inspect its receipt before retrying; the job may still be running.')
                            status = answer.json()
                        if status['state'] not in {'queued', 'running', 'cancelling'}:
                            outcome.update(state=status['state'], outcomes=status['outcomes'], error=status.get('error', ''))
                            break
                        if time.monotonic() - started > 3600:
                            raise OperationRefused('The maintenance job is still running after one hour. Inspect the owning node receipt.')
                        await asyncio.sleep(.5)
                    self.jobs.update(identifier, outcomes=outcomes)
                    if outcome['state'] != 'complete':
                        self.jobs.update(identifier, state='cancelled' if self.jobs.cancelled(identifier) else 'partial' if len(outcomes) > 1 or outcome['state'] == 'cancelled' else 'failed')
                        break
                except Exception as problem:
                    outcome.update(state='unknown' if outcome.get('job') or outcome['state'] == 'unknown' else 'failed',
                        error=str(problem) if isinstance(problem, OperationRefused) else 'Node execution failed or its result is unknown. Check that node before retrying.')
                    self.jobs.update(identifier, outcomes=outcomes, state='partial' if len(outcomes) > 1 else 'failed')
                    break
        return self.jobs.start(row, worker)

    async def api(self, request, person):
        if 'manage-settings' not in person.can:
            return JSONResponse({'error': {'message': 'Database operations require manage-settings.'}}, status_code=403)
        operation = request.path_params['operation'].split('/')
        owner = owner_of(person)
        try:
            if request.method == 'GET' and operation == ['snapshot']:
                return JSONResponse(await self.snapshot(person), headers={'cache-control': 'no-store'})
            if request.method == 'POST' and operation == ['export']:
                return JSONResponse(await self.export(person, await request.json()), status_code=202)
            if request.method == 'POST' and operation == ['preview']:
                return JSONResponse(await self.preview(person, await request.json()), headers={'cache-control': 'no-store'})
            if request.method == 'POST' and operation == ['execute']:
                return JSONResponse(await self.execute(person, await request.json()), status_code=202)
            if request.method == 'GET' and operation == ['jobs']:
                # Fleet maintenance jobs are discovered per owning node; exports are front receipts.
                try:
                    inventory = await self.ask('GET', '/turnstile/machines?include_retired=1', person.key, timeout=5)
                    nodes = inventory.json().get('machines', []) if inventory.is_success else []
                except Exception:
                    nodes = []
                jobs = [{**job, 'machine': 'front', 'type': 'export' if job['kind'] == 'export' else 'coordinator'} for job in self.jobs.recent(owner)]
                jobs += [{**job, 'machine': 'front', 'type': 'maintenance'} for job in self.local.jobs.recent(owner)]
                async def remote_jobs(node):
                    if node['name'] == 'front' or node.get('lifecycle') == 'retired' or node.get('configured') is False:
                        return []
                    try:
                        result = await self.ask('GET', self.remote_path(node['name'], 'jobs'), person.key, timeout=5)
                        return [{**job, 'machine': node['name'], 'type': 'maintenance'} for job in result.json().get('jobs', [])] if result.is_success else []
                    except Exception:
                        return []
                for rows in await asyncio.gather(*(remote_jobs(node) for node in nodes)):
                    jobs.extend(rows)
                return JSONResponse({'jobs': sorted(jobs, key=lambda row: row['created_at'], reverse=True)[:30]}, headers={'cache-control': 'no-store'})
            if len(operation) in {2, 3} and operation[0] == 'jobs':
                row = self.jobs.get(operation[1], owner)
                if request.method == 'GET' and len(operation) == 2:
                    return JSONResponse(self.jobs.public(row), headers={'cache-control': 'no-store'})
                if request.method == 'GET' and operation[2:] == ['download']:
                    if row.get('download') != 'export.zip' or row.get('download_expires_at', 0) < time.time():
                        raise OperationRefused('This archive is unavailable or its 24-hour download window expired.')
                    return FileResponse(self.jobs.folder(row['id']) / 'export.zip', filename='owg-databases-' + row['id'] + '.zip', headers={'cache-control': 'no-store'})
                if request.method == 'POST' and operation[2:] == ['cancel']:
                    self.jobs.update(row['id'], cancel_requested=True)
                    return JSONResponse({'notice': 'Cancellation requested; completed work remains recorded.'})
            return JSONResponse({'error': {'message': 'Unknown database console operation.'}}, status_code=404)
        except PermissionError:
            return JSONResponse({'error': {'message': 'This job belongs to another account.'}}, status_code=403)
        except Exception as problem:
            return JSONResponse({'error': {'message': str(problem) if isinstance(problem, OperationRefused) else 'Database operation failed or is no longer available. Refresh inventory and retry.'}}, status_code=409)
