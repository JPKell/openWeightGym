"""Benchmark handlers and worker hosted inside WeightRoom, with no network listener.

The internal ASGI adapter keeps authenticated handlers independently testable. Only calls
to LoadCoach cross HTTP; WeightRoom explicitly owns this app's lifespan.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from db import DatabaseSource
from turnstile.reporting import ReportTimeout, run as run_report
from .store import Benchmarks, TERMINAL
from .worker import BenchmarkWorker, Retry
from . import custom, quality, regressions


@dataclass(frozen=True)
class Settings:
    """Configuration for the benchmark API and its authenticated openWeightGym connection."""
    compute: str
    database: DatabaseSource = 'data/benchmarks.db'


def error(status, message):
    """Build a JSON failure response using the service error envelope."""
    return JSONResponse({'error': {'message': message}}, status_code=status)


from benchmarks.eligibility import evidence_matches, compatible


def build_app(settings: Settings, client: httpx.AsyncClient | None = None):
    """Keep results locally and authenticate each UI request against LoadCoach."""
    store = Benchmarks(settings.database)
    # Through turnstile, which never wakes compute for these: a benchmark waits for compute to be up, as before.
    compute = client or httpx.AsyncClient(base_url=settings.compute, timeout=httpx.Timeout(3900, connect=5), headers={'x-turnstile-no-wake': '1'})
    worker = BenchmarkWorker(store, compute)
    creating = set()
    outstanding_lock = asyncio.Lock()

    async def ask(request, method, path, **kwargs):
        """Send one authenticated benchmark management request to the configured openWeightGym API."""
        response = await compute.request(method, path, headers={'authorization': request.headers.get('authorization',''),
                                                              'x-turnstile-no-wake': '1'}, **kwargs)
        response.raise_for_status()
        return response.json()

    async def body(request, maximum=65536, strict=False):
        """Read and validate the benchmark operation's JSON object."""
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > maximum:
                raise ValueError(f'Requests may be at most {maximum} bytes.')
        value = quality.strict_json(raw.decode('utf-8')) if strict else json.loads(raw or b'{}')
        if not isinstance(value, dict):
            raise ValueError('Send a JSON object.')
        return value

    def may(user, ability):
        """Check the caller's current benchmark permissions."""
        return ability in user['can'] or 'superuser' in user['can']

    def current_model(run, catalogue):
        """Resolve only the permitted stable identity; legacy servers retain name behavior."""
        if not run:
            return None
        if run.get('model_id'):
            for model in catalogue:
                if model.get('model_id') == run['model_id']:
                    return model
                if place := next((place for place in model.get('locations', []) if place.get('model_id') == run['model_id']), None):
                    return {**model, **place, 'id': model['id']}
            return None
        if any(model.get('model_id') for model in catalogue):
            return None
        return next((model for model in catalogue if model['id']==run['model']),None)

    async def retired_ids(request, user):
        """Return stable model identities whose retained benchmark history is retired."""
        if not may(user, 'manage-models'):
            return set()
        try:
            models = (await ask(request, 'GET', '/loadcoach/retired-models'))['models']
            store.bind_models(models)
            return {model['model_id'] for model in models}
        except httpx.HTTPStatusError:
            return set()  # older compute servers cannot prove removal

    async def listing(request, user):
        """List benchmark definitions and retained runs visible to the caller."""
        try:
            catalogue = await ask(request, 'GET', '/v1/models')
            visible = {model['id']:model for model in catalogue['data'] if model['owned_by']=='loadcoach'}
            compute_unavailable = not catalogue.get('available_machines', ['compute'])
            store.bind_models(list(visible.values()))
        except httpx.HTTPStatusError as problem:
            if problem.response.status_code != 503:
                raise
            catalogue, visible, compute_unavailable = {'data': []}, {}, True
        except httpx.HTTPError:
            catalogue, visible, compute_unavailable = {'data': []}, {}, True
        retired = await retired_ids(request, user) if not compute_unavailable else set()
        runs_read = await run_report(store, lambda view: view.listing())
        publication_warning = ''
        if may(user, 'manage-models') and not compute_unavailable:
            publication_warning = await refresh_snapshot(request, user, list(visible.values()), runs=runs_read)
        try:
            environment = await ask(request, 'GET', '/loadcoach/probe-environment')
        except httpx.HTTPError:
            environment = {}
        environments = {('compute', 'llama_cpp'): environment}
        runs = []
        for run in runs_read:
            current = current_model(run,list(visible.values()))
            if compute_unavailable or (run.get('location') or 'compute') in catalogue.get('unavailable_machines', []) or current or run.get('model_id') in retired:
                run['retired'] = not compute_unavailable and current is None
                run['model_at_run'], run['model'] = run['model'], current['id'] if current else run['model']
                run['current_fingerprint'] = bool(current and run['fingerprint'] == current.get('fingerprint'))
                location = run.get('location') or 'compute'
                engine = run.get('subject', {}).get('settings', {}).get('engine', 'llama_cpp')
                environment_key = (location, engine)
                if environment_key not in environments:
                    try:
                        environments[environment_key] = await ask(request, 'GET', '/turnstile/machines/' + quote(location, safe='') + '/probe-environment?engine=' + quote(engine, safe=''))
                    except httpx.HTTPError:
                        environments[environment_key] = {}
                current_environment = environments[environment_key]
                run['rerun'] = ([] if compute_unavailable else
                                [key.removesuffix('_hash') for key in ('engine_hash','machine_hash') if current_environment.get(key) != run['environment'].get(key)])
                if run['version'] != run['latest_version']:  # made under an older version of the benchmark: not to be compared with newer ones
                    run['rerun'].append(f"benchmark version {run['version']}, now {run['latest_version']}")
                if not run.get('current_protocol', True):
                    run['rerun'].append('benchmark implementation')
                if not run.get('current_server', True):
                    run['rerun'].append('benchmark server mode')
                run['may_control'] = not compute_unavailable and (run.get('location') or 'compute') not in catalogue.get('unavailable_machines', []) and may(user,'benchmark') and (run['user']==user['name'] or may(user,'stop'))
                runs.append(run)
        return JSONResponse({'definitions':store.definitions(), 'runs':runs, 'paused':store.paused(), 'environment':environment,
                             'role_evidence_warning': publication_warning, 'compute_unavailable': compute_unavailable,
                             'catalogue': catalogue})

    async def detail(request, user):
        """Return one retained benchmark run and its sample evidence."""
        try:
            catalogue = await ask(request, 'GET', '/v1/models')
            visible = [model for model in catalogue['data'] if model['owned_by']=='loadcoach']
            compute_unavailable = not catalogue.get('available_machines', ['compute'])
            store.bind_models(visible)
        except httpx.HTTPError:
            visible, compute_unavailable = [], True
        retired = await retired_ids(request, user) if not compute_unavailable else set()
        run = store.get(request.path_params['number'], True)
        current = current_model(run,visible)
        if not run or not compute_unavailable and not current and run.get('model_id') not in retired:
            return error(404, 'No such benchmark run.')
        run['retired'] = not compute_unavailable and current is None
        run['model_at_run'],run['model']=run['model'],current['id'] if current else run['model']
        run['current_fingerprint']=bool(current and run['fingerprint']==current.get('fingerprint'))
        run['compute_unavailable'] = compute_unavailable
        flags = run.get('subject', {}).get('settings', {}).get('flags', {})
        if flags:
            run['subject']['settings']['flags'] = {key:('[redacted]' if any(word in key for word in ('api-key','token','password','secret')) else value) for key,value in flags.items()}
        return JSONResponse(run)

    async def start(request, user):
        """Validate and queue one benchmark run for the selected model."""
        if not may(user,'benchmark'):
            return error(403, 'Your account may not run benchmarks.')
        asked = await body(request)
        definition = next((one for one in store.definitions() if one['id']==asked.get('benchmark') and one['enabled']),None)
        targets = asked.get('targets')
        if targets is None:
            targets = [{'model': model} for model in asked.get('models', [])] if isinstance(asked.get('models'), list) else None
        if (not definition or not isinstance(targets, list) or not 1 <= len(targets) <= 100 or
                not all(isinstance(target, dict) and isinstance(target.get('model'), str)
                        and (target.get('machine') is None or isinstance(target.get('machine'), str)) for target in targets)):
            return error(400, 'Choose an enabled benchmark and one to 100 models.')
        catalogue = await ask(request, 'GET', '/v1/models')
        visible = {model['id']: model for model in catalogue['data'] if model['owned_by'] == 'loadcoach'}
        selected, skipped, seen = [], [], set()
        for target in targets:
            logical = visible.get(target['model'])
            if logical is None:
                return error(404, 'A selected local model is not available.')
            machine = target.get('machine')
            places = logical.get('locations', [])
            place = next((one for one in places if one.get('machine') == machine), None) if machine else None
            if machine and place is None and (places or machine != 'compute'):
                return error(404, f"{logical['id']} is not available at {machine}.")
            if not machine and places:
                place = places[0]
            machine = machine or (place or {}).get('machine', 'compute')
            if machine in catalogue.get('unavailable_machines', []):
                return error(409, f"{machine} is unavailable; refresh its inventory before queueing.")
            if place is not None and not place.get('enabled', True):
                return error(400, f"{logical['id']} is disabled at {machine}.")
            capabilities = (place or logical).get('capabilities', logical.get('capabilities', {}))
            if capabilities.get('load_probes') and definition['id'] != 'load':
                return error(400, f"{logical['id']}: image/audio models support load-time benchmarks only; context is unavailable.")
            if capabilities.get('native_probes') is False and not (definition['id'] == 'load' and capabilities.get('load_probes')):
                return error(400, f"{logical['id']}: this engine does not support native benchmark probes; no run was queued.")
            local_name = (place or {}).get('id', logical['id'])
            # A node's copy has its own model ID; another node's ID would name nothing there.
            local_id = place.get('model_id') if place is not None else logical.get('model_id')
            key = (logical['id'], machine or '')
            if key in seen:
                continue
            seen.add(key)
            # Embedding and generation measurements have different native transports and scores.
            embedding = (place or logical).get('embedding', logical.get('embedding', False))
            if definition['id'] != 'load' and bool(definition.get('embedding')) != bool(embedding):
                skipped.append(logical['id'] + (f' at {machine}' if target.get('machine') else ''))
                continue
            selected.append({'model': logical['id'], 'local_name': local_name, 'model_id': local_id, 'machine': machine or '',
                             'pinned': bool(target.get('machine'))})
        if not selected:
            return error(400, 'Quality benchmarks do not apply to embedding models.' if definition.get('quality') else
                         'Choose embedding models for embedder benchmarks, and other models for generation benchmarks.')
        result = await queue_runs(request, user, definition, selected)
        worker.wake.set()
        if result['errors'] and not (result['runs'] or result['active'] or result['pending']):
            failure = result['errors'][0]
            return JSONResponse({**result, 'skipped': skipped, 'error': {'message': failure['message']}},
                                status_code=failure['status'])
        return JSONResponse({**result, 'skipped': skipped}, status_code=207 if result['errors'] or result['pending'] else 202)

    async def queue_runs(request, user, definition, selected):
        """Create LoadCoach tasks for already-authorized, type-compatible targets."""
        result = {'runs': [], 'active': [], 'errors': [], 'pending': [], 'outcomes': []}
        for target in selected:
            outcome = {'model': target['model'], 'machine': target['machine'] or 'compute', 'benchmark': definition['id']}
            number = store.create(user['name'], target['model'], definition['id'], target['model_id'], location=target['machine'], reserve=True)
            if number is None:
                existing = store.active_for(target['model_id'] or target['model'], target['machine'], definition['id'], by_name=not target['model_id'])
                result['active'].append({**outcome, 'run': existing})
                result['outcomes'].append({**outcome, 'run': existing, 'state': 'already_active'})
                continue
            creating.add(number)
            try:
                task = await ask(request, 'POST', '/loadcoach/tasks', json={'id':number,'token':store.token(number),
                    'model':target['local_name'], **({'model_id':target['model_id']} if target['model_id'] else {}),
                    # Only a node the caller chose is sent as `machine`; the front may refuse that choice
                    # (Settings → General). Otherwise the node's own model ID places the task.
                    **({'machine': target['machine']} if target['machine'] and (target.get('pinned') or not target['model_id']) else {}),
                    'metadata':{'benchmark':definition['id'],'benchmark_version':definition['version'],'model':target['model'],
                                **({'machine': target['machine']} if target['machine'] else {})}})
                store.activate(number, task)
                result['runs'].append(number)
                result['outcomes'].append({**outcome, 'run': number, 'state': 'queued'})
            except httpx.HTTPStatusError as problem:
                try:
                    reason = problem.response.json()['error']['message']
                except (ValueError, KeyError, TypeError):
                    reason = 'Compute rejected this benchmark.'
                store.finish(number, 'failed', reason)
                failure = {**outcome, 'run': number, 'status': problem.response.status_code, 'message': reason}
                result['errors'].append(failure)
                result['outcomes'].append({**failure, 'state': 'failed'})
            except httpx.HTTPError:
                # Keep the intent and reservation: compute may have accepted the task.
                # Reconciliation uses this same task ID/token, never a second POST.
                reason = 'LoadCoach task acceptance is unconfirmed; checking the existing task before retrying.'
                store.message(number, reason)
                pending = {**outcome, 'run': number, 'status': 503, 'message': reason}
                result['pending'].append(pending)
                result['outcomes'].append({**pending, 'state': 'creating'})
            finally:
                creating.discard(number)
                worker.wake.set()
        return result

    async def outstanding(request, user):
        """Queue every enabled benchmark lacking current completed or already queued evidence for one model."""
        if not may(user, 'benchmark'):
            return error(403, 'Your account may not run benchmarks.')
        asked = await body(request)
        if not isinstance(asked.get('model'), str):
            return error(400, 'Choose one local model.')
        async with outstanding_lock:
            return await queue_outstanding(request, user, asked['model'])

    async def queue_outstanding(request, user, name):
        """Queue missing eligible model and benchmark combinations."""
        catalogue = await ask(request, 'GET', '/v1/models')
        visible = {model['id']: model for model in catalogue['data'] if model['owned_by'] == 'loadcoach'}
        logical = visible.get(name)
        if logical is None:
            return error(404, 'That local model is not available.')
        if logical.get('capabilities', {}).get('native_probes') is False and not logical.get('capabilities', {}).get('load_probes') and not logical.get('locations'):
            return error(400, 'This engine does not support native benchmark probes; no run was queued.')
        store.bind_models(list(visible.values()))
        definitions = [definition for definition in store.definitions() if definition['enabled']
                       and (not logical.get('capabilities', {}).get('load_probes') or definition['id'] == 'load')]
        runs = store.listing()
        environments = {}

        async def environment(machine: str) -> dict:
            """Resolve the current probe environment for a candidate model."""
            key = machine or 'compute'
            if key not in environments:
                path = ('/loadcoach/probe-environment' if key == 'compute' else
                        '/turnstile/machines/' + quote(key, safe='') + '/probe-environment')
                path += '?engine=' + quote(logical.get('engine_id', 'llama_cpp'), safe='')
                try:
                    environments[key] = await ask(request, 'GET', path)
                except httpx.HTTPError:
                    environments[key] = None
            return environments[key]

        locations = [place for place in logical.get('locations', []) if place.get('enabled', True)
                     and (place.get('capabilities', logical.get('capabilities', {})).get('native_probes') is not False
                          or place.get('capabilities', logical.get('capabilities', {})).get('load_probes'))]
        plans = []
        for definition in definitions:
            if definition.get('quality'):
                place = next((one for one in locations if not one.get('embedding', logical.get('embedding', False))), None)
                if place is None and logical.get('locations'):
                    continue
                if place is None and logical.get('embedding'):
                    continue
                plans.append((definition, {'model': logical['id'], 'local_name': (place or {}).get('id', logical['id']),
                                           'model_id': place.get('model_id') if place is not None else logical.get('model_id'),
                                           'machine': (place or {}).get('machine', '')}, place))
            else:
                places = locations if logical.get('locations') else [None]
                for place in places:
                    if (definition['id'] != 'load' and bool(definition.get('embedding')) !=
                            bool((place or logical).get('embedding', logical.get('embedding', False)))):
                        continue
                    plans.append((definition, {'model': logical['id'], 'local_name': (place or {}).get('id', logical['id']),
                                               'model_id': place.get('model_id') if place is not None else logical.get('model_id'),
                                               'machine': (place or {}).get('machine', '')}, place))

        queued, fresh, active, unavailable, errors, pending = [], [], [], [], [], []
        for definition, target, place in plans:
            if not compatible({**logical, **(place or {})}, definition):
                continue
            candidates = [run for run in runs if run['benchmark'] == definition['id']
                          and current_model(run, [logical])
                          and (not target['model_id'] or run.get('model_id') == target['model_id'])
                          and (run.get('location') or 'compute') == (target['machine'] or 'compute')]
            current_environment = await environment(target['machine'])
            label = definition['id'] + (f" at {target['machine']}" if target['machine'] else '')
            if current_environment is None or not all(current_environment.get(key) for key in ('engine_hash', 'machine_hash')):
                unavailable.append(label)
                continue

            current_completed = False
            current_active = False
            for run in candidates:
                if evidence_matches(run, definition, (place or logical).get('fingerprint'), current_environment):
                    current_completed |= run['state'] == 'completed'
                    current_active |= run['state'] not in TERMINAL
            if current_completed:
                fresh.append(label)
            elif current_active:
                active.append(label)
            else:
                result = await queue_runs(request, user, definition, [target])
                queued.extend(result['runs'])
                if result['active']:
                    active.append(label)
                errors.extend({**item, 'benchmark': label} for item in result['errors'])
                pending.extend({**item, 'benchmark': label} for item in result['pending'])
        worker.wake.set()
        return JSONResponse({'runs': queued, 'queued': len(queued), 'fresh': fresh, 'active': active,
                             'unavailable': unavailable, 'errors': errors, 'pending': pending},
                            status_code=207 if errors or unavailable or pending else 202)

    async def control(request, user):
        """Pause, resume or cancel the selected benchmark run."""
        if not may(user,'benchmark'):
            return error(403, 'Your account may not control benchmarks.')
        asked = await body(request)
        action, number = asked.get('action'), asked.get('run')
        catalogue = await ask(request,'GET','/v1/models') if number else {'data':[]}
        visible = [model for model in catalogue['data'] if model['owned_by']=='loadcoach']
        if number:
            store.bind_models(visible)
        run = store.get(number) if isinstance(number,str) else None
        if number and (not run or not current_model(run,visible)):
            return error(404, 'No such benchmark run.')
        if run:
            if run['user'] != user['name'] and not may(user,'stop'):
                return error(403, 'You may only control your own benchmark runs.')
            if action not in ('pause','resume','stop') or run['state'] in TERMINAL:
                return error(400, 'This run cannot take that action.')
            if run['state'] == 'creating' and action != 'stop':
                return error(409, 'Task acceptance is still being checked. Wait before pausing or resuming.')
            if action == 'stop':
                store.finish(number,'cancelled',f'Stopped by {user["name"]}.')
            else:
                store.state(number,'paused' if action=='pause' else 'queued',f'{action.title()} requested by {user["name"]}.')
        else:
            if not may(user,'stop'):
                return error(403, 'Shared queue controls require the stop ability.')
            if action not in ('pause','resume','clear'):
                return error(400, 'Choose pause, resume or clear.')
            if action == 'clear':
                for run in store.listing():
                    if run['state'] not in TERMINAL:
                        store.finish(run['id'],'cancelled',f'Queue cleared by {user["name"]}.')
            else:
                store.paused(action=='pause')
        if action != 'resume' and worker.task and (not number or number==worker.current):
            try:
                await worker.request(worker.current, 'POST', '/interrupt')
            except (httpx.HTTPError, Retry):
                pass  # the persisted control is enforced when LoadCoach is reachable again
        worker.wake.set()
        return JSONResponse({'ok':True})

    async def configure(request, user):
        """Validate and save benchmark scoring and execution settings."""
        if not may(user,'manage-settings'):
            return error(403, 'Definition changes require the manage-settings ability.')
        asked = await body(request)
        if not isinstance(asked.get('formula'),str) or type(asked.get('enabled')) is not bool:
            return error(400, 'Give an enabled checkbox and a score formula.')
        store.configure(asked.get('benchmark'),asked['enabled'],asked['formula'],asked.get('green'),asked.get('server'))
        publication_warning = ''
        if may(user, 'manage-models'):
            publication_warning = await refresh_snapshot(request, user)
        return JSONResponse({'ok':True, 'role_evidence_warning':publication_warning})

    async def dataset_import(request, user):
        """Validate and retain an immutable custom benchmark dataset."""
        if not may(user, 'manage-settings'):
            return error(403, 'Importing case sets requires manage-settings.')
        dataset = await body(request, custom.MAX_BYTES, strict=True)
        result = store.import_dataset(dataset, user['name'])
        warning = await refresh_snapshot(request, user) if may(user, 'manage-models') else ''
        worker.wake.set()
        return JSONResponse({**result, 'role_evidence_warning':warning}, status_code=201 if result['created'] else 200)

    async def dataset_export(request, user):
        """Export the selected immutable custom benchmark dataset."""
        if not may(user, 'manage-settings'):
            return error(403, 'Exporting case sets requires manage-settings.')
        module = store.module(request.path_params['suite'], {'version': int(request.path_params['version'])})
        if not module.DEFINITION.get('custom'):
            return error(400, 'Choose an imported dataset.')
        return JSONResponse(module.dataset)

    async def dataset_delete(request, user):
        """Retire the selected custom dataset without rewriting retained results."""
        if not may(user, 'manage-settings'):
            return error(403, 'Deleting case sets requires manage-settings.')
        try:
            gone = store.delete_dataset(request.path_params['suite'])
        except ValueError as why:
            return error(409, str(why))
        warning = await refresh_snapshot(request, user) if may(user, 'manage-models') else ''
        return JSONResponse({'deleted': gone, 'role_evidence_warning': warning})

    async def regression_comparison(request, user):
        """Compare compatible retained benchmark runs by case and check."""
        catalogue = (await ask(request, 'GET', '/v1/models'))['data']
        store.bind_models(catalogue)
        retired = await retired_ids(request, user)
        selected = []
        for key in ('run', 'baseline'):
            run = store.get(request.query_params.get(key, ''), True)
            if not run or not current_model(run, catalogue) and run.get('model_id') not in retired:
                return error(404, 'No such benchmark run.')
            selected.append(run)
        return JSONResponse(regressions.compare(*selected))

    async def refresh_snapshot(request, user, catalogue=None, *, runs=None):
        """Refresh the role-selection evidence snapshot from retained results."""
        try:
            await publish_snapshot(request, user, catalogue, runs=runs)
            return ''
        except (httpx.HTTPError, ValueError, OverflowError):
            return 'Role evidence could not be refreshed. LoadCoach retains its last accepted snapshot.'

    async def publish_snapshot(request, user, catalogue=None, *, runs=None):
        """Publish validated role evidence through the existing openWeightGym API."""
        if not may(user, 'manage-models'):
            return error(403, 'Publishing role evidence requires manage-models.')
        if catalogue is None:
            catalogue = (await ask(request, 'GET', '/v1/models'))['data']
        store.bind_models(catalogue)
        model_ids = {one.get('model_id') for one in catalogue if one['owned_by'] == 'loadcoach'}
        snapshot = await run_report(store, lambda view: view.role_snapshot(model_ids, runs=runs))
        return JSONResponse(await ask(request, 'POST', '/loadcoach/role-evidence', json=snapshot))

    def route(path, handler, method='GET'):
        """Register the handler behind the service authentication wrapper."""
        async def checked(request):
            """Authorize the benchmark handler and apply the shared response policy."""
            try:
                user = await ask(request, 'GET', '/loadcoach/me')
                return await handler(request,user)
            except httpx.HTTPStatusError as problem:
                return Response(problem.response.content,status_code=problem.response.status_code,media_type='application/json')
            except httpx.HTTPError:
                return error(503, 'LoadCoach is unavailable.')
            except ReportTimeout:
                return error(503, 'Benchmark history took too long to read. Retry shortly.')
            except (ValueError, OverflowError, RecursionError) as problem:
                return error(400, str(problem)[:1000])
        return Route(path,checked,methods=[method])

    @asynccontextmanager
    async def lifespan(app):
        """Start background workers and release service resources on shutdown."""
        worker.creating = creating
        task = asyncio.create_task(worker.run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            try:
                # Do not hold up WeightRoom shutdown if compute has gone away.
                await asyncio.wait_for(worker.reconcile(), 5)
            except TimeoutError:
                pass  # persisted completion is retried on the next start
            finally:
                if client is None:
                    await compute.aclose()
                store.db.close()

    app = Starlette(lifespan=lifespan,routes=[route('/benchmarks',listing),route('/benchmarks',start,'POST'),
        route('/benchmarks/outstanding',outstanding,'POST'),
        route('/benchmarks/control',control,'POST'),route('/benchmarks/definitions',configure,'POST'),
        route('/benchmarks/datasets',dataset_import,'POST'),route('/benchmarks/datasets/{suite}/{version}',dataset_export),route('/benchmarks/datasets/{suite}',dataset_delete,'DELETE'),
        route('/benchmarks/regressions',regression_comparison),
        route('/benchmarks/publish',publish_snapshot,'POST'),route('/benchmarks/{number}',detail)])
    app.state.store, app.state.worker = store, worker
    return app
