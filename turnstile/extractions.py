"""Control-node-owned vision jobs; GPU admission and every inference attempt use normal APIs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import email
import email.policy
import json
import secrets
import time
from urllib.parse import quote

import httpx
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from weightroom.extraction import (MAX_BODY, MAX_OUTPUT, MIMES, MARKDOWN, Problem, image_upload, render_output,
                                  request_body, specification, validate_output, warnings_for)
from weightroom.extraction_store import Extractions, ACTIVE

PRIVATE = {'cache-control': 'private, no-store', 'x-content-type-options': 'nosniff'}


async def bounded_body(request, limit=MAX_BODY):
    """Read a request without buffering beyond the configured byte limit."""
    content = bytearray()
    async for part in request.stream():
        if len(content) + len(part) > limit:
            raise Problem('Extraction request is too large.', 413, 'request_too_large')
        content.extend(part)
    return bytes(content)


async def read_json(request, limit=65536):
    """Decode a bounded JSON object or raise a public validation error."""
    try:
        value = json.loads(await bounded_body(request, limit))
    except (ValueError, UnicodeError, RecursionError):
        raise Problem('Send a valid JSON object.') from None
    if not isinstance(value, dict):
        raise Problem('Send a JSON object.')
    return value


class ExtractionService:
    """Orchestrate caller-private jobs through ordinary inference admission."""
    def __init__(self, source, caller):
        """Open control node persistence and bounded orchestration slots."""
        self.store = Extractions(source)
        self.caller = caller
        self.door = None
        self.working = {}
        self.slots = asyncio.Semaphore(4)
        self.owner_slots = {}
        self.owner_jobs = {}
        self.maintenance = None
        self.role_models = lambda role: {}

    async def catalogue(self, authorization):
        """List permitted models and roles with vision and schema support."""
        response = await self.door.get('/loadcoach/me', headers={'authorization': authorization, 'x-turnstile-no-wake': '1'}, timeout=15)
        if not response.is_success:
            raise Problem('Cannot read permitted vision models.', response.status_code, 'catalogue_unavailable')
        info = response.json()
        models = [{'id': name, 'hosted': options.get('hosted', False), 'json_schema': options.get('json_schema', False),
                   'model_id': options.get('model_id'), 'provider': options.get('provider', 'Local'),
                   'context': options.get('context', 8192)}
                  for name, options in info.get('chat', {}).get('models', {}).items()
                  if options.get('vision') and options.get('json_schema')]
        # Roles are routed by the normal image/schema candidate filter; no role bypasses it.
        capable = {model['id'] for model in models}
        models += [{'id': name, 'hosted': False, 'json_schema': True, 'role': True,
                    'provider': 'Role · may use configured cloud candidates'} for name in info.get('roles', [])
                   if any(candidate in capable for candidate in self.role_models(name))]
        return models

    def public(self, job):
        """Build the caller-visible result, warnings, exports and provenance."""
        spec, data = job['spec'], job['result']
        warnings = warnings_for(spec, data) if job['state'] in ('completed', 'needs_review') else []
        return {'id': job['id'], 'asset_id': job['asset'], 'status': job['state'], 'spec': spec,
                'asset': self.store.asset(job['owner'], job['asset']),
                'task': spec['task'], 'output_format': spec['output_format'], 'data': data,
                'text': render_output(spec, data) if job['state'] in ('completed', 'needs_review') else '',
                'preview_html': MARKDOWN.render(render_output(spec, data)) if spec['output_format'] == 'markdown' and job['state'] in ('completed', 'needs_review') else '',
                'raw': job['raw'], 'blocks': [], 'warnings': warnings, 'error': job['error'],
                'provenance': job['provenance'], 'usage': job['usage'], 'revision': job['revision'],
                'reviewed': job['reviewed'], 'created': job['created'], 'expires': job['expires'],
                'status_url': '/owg/extractions/' + job['id']}

    def owned(self, user, identifier):
        """Find a job using the stable authenticated owner identity."""
        found = self.store.one(user.identity or user.name, identifier)
        if not found:
            raise Problem('No extraction of yours has this ID.', 404, 'extraction_not_found')
        return found

    async def submit(self, user, authorization, value, idempotency='', inference_headers=None):
        """Validate a request and start at most one task per idempotency key."""
        spec = specification(value)
        owner = user.identity or user.name
        if spec.get('retry_of'):
            self.owned(user, spec['retry_of'])
        if not isinstance(idempotency, str) or len(idempotency) > 120 or any(ord(char) < 33 for char in idempotency):
            raise Problem('Idempotency-Key must contain at most 120 non-space printable characters.')
        if spec['model'] not in {model['id'] for model in await self.catalogue(authorization)}:
            raise Problem('Choose a permitted vision model with JSON Schema support.', 400, 'unsupported_model')
        job, fresh = self.store.create(owner, spec, idempotency or secrets.token_hex(16))
        if job is None:
            raise Problem('This idempotency key has expired. Submit a new key.', 409)
        if fresh:
            task = asyncio.create_task(self.execute(job, authorization, inference_headers or {}))
            self.working[job['id']] = task
            self.owner_jobs[owner] = self.owner_jobs.get(owner, 0) + 1
            def ended(finished):
                """Release in-memory task and per-owner orchestration bookkeeping."""
                self.working.pop(job['id'], None)
                self.owner_jobs[owner] -= 1
                if not self.owner_jobs[owner]:
                    self.owner_jobs.pop(owner, None)
                    self.owner_slots.pop(owner, None)
                if not finished.cancelled():
                    finished.exception()
            task.add_done_callback(ended)
        return job

    async def execute(self, job, authorization, inference_headers):
        """Run one bounded attempt with fresh authorization and retained evidence."""
        owner, identifier, spec = job['owner'], job['id'], job['spec']
        raw, usage, provenance = '', {}, {}
        counted = False
        owner_slot = self.owner_slots.setdefault(owner, asyncio.Semaphore(2))
        try:
            async with asyncio.timeout(900), owner_slot, self.slots:
                if self.maintenance:
                    await self.maintenance.wait()
                    self.maintenance.active += 1
                    counted = True
                current = self.caller(authorization.removeprefix('Bearer '))
                if not current or (current.identity or current.name) != owner:
                    raise Problem('Caller authorization changed before execution.', 403, 'authorization_changed')
                available = {model['id']: model for model in await self.catalogue(authorization)}
                if spec['model'] not in available:
                    raise Problem('Vision/schema capability is no longer available.', 400, 'unsupported_model')
                asset = self.store.asset(owner, spec['asset_id'], content=True)
                if not asset:
                    raise Problem('Source image is no longer available.', 404, 'asset_not_found')
                if not self.store.state(owner, identifier, 'running'):
                    return
                headers = {**inference_headers, 'authorization': authorization, 'x-loadcoach-trace': inference_headers.get('x-loadcoach-trace', identifier)}
                if spec['node']:
                    headers['x-suite-node'] = spec['node']
                body = request_body(spec, asset)
                selected = available[spec['model']]
                if not selected.get('hosted') and not selected.get('role'):
                    body['chat_template_kwargs'] = {'enable_thinking': False}
                provenance = {'asset_sha256': asset['digest'], 'model': spec['model'], 'schema_version': 1,
                              'prompt_version': 1, 'coordinate_source': 'unavailable', 'attempts': 1,
                              'original_dimensions': [asset['width'], asset['height']], 'input_transform': 'EXIF-oriented, metadata-free PNG'}
                async with self.door.stream('POST', '/v1/chat/completions', json=body, headers=headers, timeout=None) as answer:
                    content = bytearray()
                    async for part in answer.aiter_bytes():
                        if len(content) + len(part) > MAX_OUTPUT + 65536:
                            raise Problem('Inference response is too large.', 502, 'output_too_large')
                        content.extend(part)
                    provenance.update(request_id=answer.headers.get('x-loadcoach-request-id'),
                        node=answer.headers.get('x-suite-node') or answer.headers.get('x-loadcoach-node'),
                        model_id=answer.headers.get('x-loadcoach-model-id'), build=answer.headers.get('x-loadcoach-build'),
                        fingerprint=answer.headers.get('x-loadcoach-configured-fingerprint') or answer.headers.get('x-loadcoach-fingerprint'),
                        hosted=bool(answer.headers.get('x-loadcoach-service')),
                        requested_model=spec['model'],
                        model=answer.headers.get('x-loadcoach-picked') or spec['model'],
                        provider=answer.headers.get('x-loadcoach-service'))
                    if not answer.is_success:
                        raise Problem(f'Inference refused extraction (HTTP {answer.status_code}). See ordinary request diagnostics.', answer.status_code, 'inference_refused')
                    try:
                        result = json.loads(content)
                        usage = result.get('usage') or {}
                        choice = result['choices'][0]
                        message = choice['message']
                        raw = message.get('content') or ''
                        if not isinstance(raw, str):
                            raw = json.dumps(raw, ensure_ascii=False)
                            raise Problem('Model returned non-text structured output.', 502, 'invalid_structured_output')
                        if message.get('refusal'):
                            raise Problem('The model refused this extraction.', 422, 'model_refusal')
                        if choice.get('finish_reason') != 'stop':
                            raise Problem('Model output is incomplete. Retry explicitly with a smaller image/task.', 422, 'incomplete_output')
                        data = json.loads(raw)
                    except Problem:
                        raise
                    except (KeyError, IndexError, TypeError, ValueError):
                        raise Problem('Model returned invalid structured output.', 502, 'invalid_structured_output') from None
                validate_output(spec, data)
                warnings = warnings_for(spec, data)
                self.store.finish(owner, identifier, data, raw, provenance, usage, bool(warnings))
        except asyncio.CancelledError:
            self.store.state(owner, identifier, 'cancelled', {'code': 'cancelled', 'message': 'Cancelled. Any completed upstream work may still be charged.'})
            raise
        except TimeoutError:
            self.store.failed(owner, identifier, {'code': 'timeout', 'message': 'Extraction timed out. Upstream outcome may be unknown; retry explicitly.'}, raw, provenance, usage)
        except (httpx.HTTPError, Problem) as error:
            self.store.failed(owner, identifier, {'code': getattr(error, 'code', 'upstream_disconnected'),
                              'message': str(error) if isinstance(error, Problem) else 'Inference connection ended. Upstream outcome may be unknown.'}, raw, provenance, usage)
        except Exception:
            self.store.failed(owner, identifier, {'code': 'extraction_failed', 'message': 'Extraction failed. Check control node service diagnostics.'}, raw, provenance, usage)
            import traceback
            traceback.print_exc()
        finally:
            if counted:
                self.maintenance.active -= 1

    async def cancel(self, user, identifier):
        """Cancel pending work and await release of its inference connection."""
        job = self.owned(user, identifier)
        if job['state'] in ACTIVE:
            self.store.state(job['owner'], identifier, 'cancelled', {'code': 'cancelled', 'message': 'Cancelled by the caller.'})
            task = self.working.get(identifier)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return self.owned(user, identifier)

    async def close(self):
        """Stop owned tasks and close persistence before gateway shutdown."""
        tasks = list(self.working.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.db.close()

    async def forget(self, owner):
        """Cancel and erase extraction records for a removed account."""
        for identifier, task in list(self.working.items()):
            if self.store.one(owner, identifier):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.store.forget(owner)

    def routes(self, caller, delegation=None):
        """Register authenticated workflow APIs and signed tool delegation."""
        def bound(handler):
            """Wrap a workflow handler with caller authentication and safe errors."""
            async def checked(request):
                """Authenticate the account before handling an owned operation."""
                user = caller(request.headers.get('authorization', '').removeprefix('Bearer '))
                if not user:
                    return JSONResponse({'error': {'message': 'Missing or wrong API key.', 'code': 'unauthorized'}}, 401, headers=PRIVATE)
                try:
                    return await handler(request, user)
                except Problem as error:
                    return JSONResponse({'error': {'message': str(error), 'code': error.code}}, error.status, headers=PRIVATE)
                except httpx.HTTPError:
                    return JSONResponse({'error': {'message': 'Model gateway is unavailable.', 'code': 'gateway_unavailable'}}, 503, headers=PRIVATE)
            return checked

        async def models(request, user):
            """Return the authorized vision and schema model catalogue."""
            return JSONResponse({'models': await self.catalogue(request.headers.get('authorization', ''))}, headers=PRIVATE)

        async def assets(request, user):
            """Validate one bounded image upload and persist its owned source."""
            raw = await bounded_body(request)
            content_type = request.headers.get('content-type', '')
            if content_type.lower().startswith('multipart/form-data'):
                message = email.message_from_bytes(b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + raw, policy=email.policy.HTTP)
                if not message.is_multipart():
                    raise Problem('Invalid multipart image upload.')
                parts = list(message.iter_parts())
                if len(parts) != 1 or parts[0].get_param('name', header='content-disposition') not in ('image', 'file'):
                    raise Problem('Upload exactly one image field.')
                name, data = parts[0].get_filename(), parts[0].get_payload(decode=True)
            else:
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or set(payload) != {'name', 'data'} or not isinstance(payload['data'], str):
                        raise ValueError()
                    name, data = payload['name'], base64.b64decode(payload['data'], validate=True)
                except (ValueError, TypeError, binascii.Error):
                    raise Problem('Send an image multipart field, or name and base64 data.') from None
            asset = await asyncio.to_thread(image_upload, name, data)
            return JSONResponse(self.store.add_asset(user.identity or user.name, asset), 201, headers=PRIVATE)

        async def source(request, user):
            """Download or delete an owned source under reference restrictions."""
            identifier, owner = request.path_params['asset'], user.identity or user.name
            if request.method == 'DELETE':
                self.store.remove_asset(owner, identifier)
                return Response(status_code=204, headers=PRIVATE)
            asset = self.store.asset(owner, identifier, content=True)
            if not asset:
                raise Problem('No source image of yours has this ID.', 404, 'asset_not_found')
            prepared = request.query_params.get('prepared') == '1'
            return Response(base64.b64decode(asset['prepared' if prepared else 'original']),
                            media_type='image/png' if prepared else asset['mime'], headers=PRIVATE)

        async def jobs(request, user):
            """Submit a job or page through the caller's retained results."""
            if request.method == 'POST':
                job = await self.submit(user, request.headers.get('authorization', ''), await read_json(request), request.headers.get('idempotency-key', ''),
                    {key: value for key, value in request.headers.items() if key in ('x-loadcoach-trace', 'x-loadcoach-sensitivity', 'x-loadcoach-log', 'x-loadcoach-priority')})
                return JSONResponse(self.public(job), 202 if job['state'] in ACTIVE else 200, headers=PRIVATE)
            listed, cursor = self.store.listing(user.identity or user.name, request.query_params.get('cursor', ''))
            return JSONResponse({'data': [self.public(job) for job in listed], 'next_cursor': cursor}, headers=PRIVATE)

        async def one(request, user):
            """Inspect or remove one caller-owned extraction."""
            identifier = request.path_params['job']
            if request.method == 'DELETE':
                self.store.remove(user.identity or user.name, identifier)
                return Response(status_code=204, headers=PRIVATE)
            return JSONResponse(self.public(self.owned(user, identifier)), headers=PRIVATE)

        async def cancel(request, user):
            """Cancel pending work and await release of its inference connection."""
            return JSONResponse(self.public(await self.cancel(user, request.path_params['job'])), headers=PRIVATE)

        async def review(request, user):
            """Validate corrections and save them against the expected revision."""
            job = self.owned(user, request.path_params['job'])
            body = await read_json(request, MAX_OUTPUT + 1024)
            if set(body) != {'revision', 'data', 'reviewed'} or type(body['revision']) is not int or type(body['reviewed']) is not bool:
                raise Problem('Review requires revision, data and a reviewed boolean.')
            data = validate_output(job['spec'], body['data'])
            warnings = warnings_for(job['spec'], data)
            updated = self.store.review(job['owner'], job['id'], body['revision'], data, body['reviewed'], bool(warnings))
            return JSONResponse(self.public(updated), headers=PRIVATE)

        async def export(request, user):
            """Render the selected current or original revision as a safe download."""
            job = self.owned(user, request.path_params['job'])
            if job['state'] not in ('completed', 'needs_review'):
                raise Problem('Only completed results can be exported.', 409)
            format_ = request.query_params.get('format', job['spec']['output_format'])
            data = job['result']
            if request.query_params.get('revision') == 'original':
                row = self.store.db.execute('SELECT data FROM extraction_revisions WHERE job=? AND revision=1', (job['id'],)).fetchone()
                data = json.loads(row[0])
            output = render_output(job['spec'], data, format_)
            mime, extension = MIMES[format_]
            return Response(output, media_type=mime, headers={**PRIVATE,
                'content-disposition': f'attachment; filename="{job["id"]}-r{1 if request.query_params.get("revision") == "original" else job["revision"]}.{extension}"'})

        async def presets(request, user):
            """List, create or remove caller-owned extraction presets."""
            owner = user.identity or user.name
            if request.method == 'GET':
                return JSONResponse({'presets': self.store.presets(owner)}, headers=PRIVATE)
            if request.method == 'DELETE':
                with self.store.db:
                    self.store.db.execute('DELETE FROM extraction_presets WHERE id=? AND owner=?', (request.path_params['preset'], owner))
                return Response(status_code=204, headers=PRIVATE)
            body = await read_json(request)
            if set(body) != {'name', 'spec'} or not isinstance(body['spec'], dict):
                raise Problem('Preset requires name and spec.')
            spec = specification({**body['spec'], 'asset_id': 'preset'})
            identifier = self.store.save_preset(owner, body['name'], spec)
            return JSONResponse({'id': identifier}, 201, headers=PRIVATE)

        async def tool(request):
            """Verify caller delegation and dispatch a bounded extraction tool."""
            proof_headers = {}
            try:
                if not delegation:
                    raise Problem('Trusted extraction tools are unavailable.', 403)
                raw = await bounded_body(request, 65536)
                user, authorization, proof_headers = delegation(request, raw)
                body = json.loads(raw)
                from toollocker.extraction import PRESETS, descriptions
                from jsonschema import validate, ValidationError
                name = request.path_params['tool']
                descriptor = next((entry for entry in descriptions() if entry['name'] == name), None)
                if not descriptor or not user.group.may_see_tool('extraction__' + name):
                    raise Problem('Unknown extraction tool.', 404)
                arguments = body.get('arguments')
                try:
                    validate(arguments, descriptor['inputSchema'])
                except ValidationError:
                    raise Problem('Invalid extraction tool arguments.') from None
                level = body.get('sensitivity', 'normal')
                from loadcoach.groups import LEVELS
                if level not in LEVELS or LEVELS.index(level) > LEVELS.index(user.group.max_sensitivity):
                    raise Problem('This extraction sensitivity is not allowed.', 403)
                if name in PRESETS:
                    spec = {key: value for key, value in arguments.items() if key != 'schema_json'}
                    spec.update(task=PRESETS[name])
                    spec.setdefault('output_format', 'json' if PRESETS[name] in ('receipt', 'table', 'custom') else 'plain')
                    if name == 'extract_image_fields':
                        spec['schema'] = json.loads(arguments['schema_json'])
                    job = await self.submit(user, authorization, spec, inference_headers={'x-loadcoach-sensitivity': level})
                else:
                    job = await self.cancel(user, arguments['job_id']) if name == 'cancel_extraction' else self.owned(user, arguments['job_id'])
                public = self.public(job)
                # Full artifacts stay behind the ordinary caller-authenticated API.
                return JSONResponse({'id': job['id'], 'status': job['state'], 'warnings': public['warnings'],
                    'summary': public['text'][:3000], 'error': public['error'], 'usage': public['usage'],
                    'export': '/owg/extractions/' + job['id'] + '/export', 'status_url': public['status_url']}, headers={**PRIVATE, **proof_headers})
            except (ValueError, Problem) as error:
                return JSONResponse({'error': {'message': str(error), 'code': getattr(error, 'code', 'invalid_tool_call')}},
                                    getattr(error, 'status', 400), headers={**PRIVATE, **proof_headers})

        return [Route('/owg/extraction-tools/{tool}', tool, methods=['POST']),
            Route('/owg/extraction-models', bound(models)), Route('/owg/extraction-assets', bound(assets), methods=['POST']),
            Route('/owg/extraction-assets/{asset}', bound(source), methods=['GET', 'DELETE']),
            Route('/owg/extraction-presets', bound(presets), methods=['GET', 'POST']),
            Route('/owg/extraction-presets/{preset}', bound(presets), methods=['DELETE']),
            Route('/owg/extractions', bound(jobs), methods=['GET', 'POST']),
            Route('/owg/extractions/{job}', bound(one), methods=['GET', 'DELETE']),
            Route('/owg/extractions/{job}/cancel', bound(cancel), methods=['POST']),
            Route('/owg/extractions/{job}/review', bound(review), methods=['PATCH']),
            Route('/owg/extractions/{job}/export', bound(export))]
