"""Durable external tasks and scoped access to model probes.

A client owns its work plan and results. With front task ownership enabled, Turnstile retains
durable task state and LoadCoach executes signed scoped probes; legacy installations retain a
local one-item batch. Compute checks current permissions and exposes one isolated probe at a time.
Task tokens only authorise their own task; they are never accepted as general API keys.
"""

import asyncio
import copy
import hashlib
import json
import re
import secrets

import httpx

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from db import migrate
from db.schemas import TASKS

from loadcoach.probes import Probes
from loadcoach.runner import Refusal
from engines import engine_for

ENDPOINT = '/loadcoach/task'
# Compatibility for the offline split-benchmarks utility. Live constructors use the
# versioned migration stream; the script needs the initial table DDL for its private copy.
SCHEMA = '''CREATE TABLE IF NOT EXISTS external_tasks (
    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, model TEXT NOT NULL,
    fingerprint TEXT NOT NULL, subject TEXT NOT NULL, environment TEXT NOT NULL
);'''


def error(status, message, retry=False):
    """Build a JSON failure response using the service error envelope."""
    return JSONResponse({'error': {'message': message}}, status_code=status,
                        headers={'retry-after': '1'} if retry else None)


class Tasks:
    """HTTP boundary for client-managed batches; no client-specific state or definitions."""

    def __init__(self, batches, runner, users, who, draining, log, on_result=None):
        """Bind durable task storage, native probes and current account authorization."""
        self.batches, self.runner, self.users, self.who, self.log = batches, runner, users, who, log
        self.on_result = on_result
        self.dispatched = {}  # work the front keeps records for: in memory only here, never a compute batch
        self.front_kept = {}  # permission snapshot while the same probe server is kept
        self.receipts = {}  # bounded completed probe receipts, indexed by task/attempt
        if batches.db is not None:
            migrate(batches.db.database, batches.db, "loadcoach.tasks", TASKS)
            with batches.db:
                for number, name, subject, created in batches.db.execute('SELECT e.id,e.model,e.subject,b.created FROM benchmark_tasks e JOIN batches b ON b.id=e.id WHERE e.model_id IS NULL').fetchall():
                    identity = json.loads(subject).get('model_id') or runner.models.legacy_identity(name,created)
                    if identity:
                        batches.db.execute('UPDATE benchmark_tasks SET model_id=? WHERE id=?',(identity,number))
        self.probes = Probes(runner, draining, self.allowed)

    def get(self, number):
        """Return one durable compute task, or None when the task is absent."""
        if self.batches.db is None:
            return None
        row = self.batches.db.execute('SELECT model,fingerprint,subject,environment,model_id FROM benchmark_tasks WHERE id=?', (number,)).fetchone()
        batch = self.batches.get(number)
        if not row or not batch:
            return None
        current = self.runner.models.name_for(row[4]) if row[4] else row[0]
        return {'id': number, 'model': current, 'model_at_submit': row[0], 'model_id': row[4], 'fingerprint': row[1], 'subject': json.loads(row[2]),
                'environment': json.loads(row[3]), 'user': batch['user'], 'status': batch['status']}

    @staticmethod
    def public(task):
        """Never expose configured server credentials through model/task snapshots."""
        task = copy.deepcopy(task)
        flags = task.get('subject', {}).get('settings', {}).get('flags', {})
        for key in flags:
            if any(word in key for word in ('api-key','token','password','secret')):
                flags[key] = '[redacted]'
        return task

    def allowed(self, number):
        """Check the task owner's current ability to execute its pinned subject."""
        task = self.dispatched.get(number) or self.front_kept.get(number) or self.get(number)
        user = self.users.by_name(task['user']) if task else None
        if task and task['subject'].get('owner_id') and self.users.identity(task['user']) != task['subject']['owner_id']:
            return False  # recreating a deleted username never inherits its durable work
        return bool(task and task['model'] and task['status'] == 'in_progress' and user and user.can('benchmark') and user.group.may_use(task['model']))

    async def authenticate(self, request):
        """A scoped token may inspect/finish its task; current permissions still gate every probe."""
        number = request.path_params['number']
        task = self.get(number)
        if task is None:
            return error(404, 'No such external task.')
        token = request.headers.get('x-loadcoach-task', '')
        if token:
            saved = self.batches.db.execute('SELECT token_hash FROM benchmark_tasks WHERE id=?', (number,)).fetchone()[0]
            if secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), saved):
                return task
        user = await self.who(request)
        if isinstance(user, Response):
            return user
        if task['user'] != user.name:
            return error(404, 'No such external task.')
        return task

    async def body(self, request, *, allow_large_evaluation=False):
        """Read and validate the bounded task operation JSON body."""
        raw = await request.body()
        # Long retrieval archives travel as text, before native tokenization/padding.
        # Only the authenticated probe path opts in; other task bodies retain 64 KiB.
        if allow_large_evaluation and len(raw) > 1048576:
            raise ValueError('Evaluation requests may be at most 1 MiB.')
        if not allow_large_evaluation and len(raw) > 65536:
            raise ValueError('Task requests may be at most 64 KiB.')
        body = json.loads(raw or b'{}')
        if not isinstance(body, dict):
            raise ValueError('Send a JSON object.')
        if allow_large_evaluation and len(raw) > 65536 and not isinstance(body.get('evaluation'), dict):
            raise ValueError('Task requests may be at most 64 KiB.')
        return body

    async def environment(self, request):
        """Describe the native probe environment used to bind task evidence."""
        user = await self.who(request)
        if isinstance(user, Response):
            return user
        try:
            selected = engine_for(request.query_params.get("engine", "llama_cpp"))
            if not selected.capabilities({}).get("native_probes") and not selected.capabilities({}).get("load_probes"):
                return error(400, "This engine has no qualified native probe environment; llama.cpp evidence is not substituted.")
            return JSONResponse(await self.probes.identify(selected.id))
        except (OSError, ValueError) as problem:
            return error(503, str(problem))

    async def subject(self, request):
        """Resolve the task's pinned model and benchmark subject."""
        user = await self.who(request)
        if isinstance(user, Response):
            return user
        name = request.path_params['name']
        if not user.can('benchmark'):
            return error(403, 'Your account may not run model measurements.')
        if self.runner.models.get(name) is None or not user.group.may_use(name):
            return error(404, 'The selected local model is not available.')
        try:
            mark, subject = self.probes.subject(name)
            return JSONResponse(self.public({'fingerprint':mark, 'subject':subject, 'front_probe_dispatch': 1}))
        except (ValueError, OSError) as problem:
            return error(400,str(problem))

    async def interrupt(self, request):
        """Stop this task's current probe without cancelling its durable batch."""
        task = await self.authenticate(request)
        if isinstance(task, Response):
            return task
        await self.probes.drop_kept(task['id'])
        work = self.probes.working.get(task['id'])
        if work:
            if not work.cancelling():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        return JSONResponse({'ok':True})

    async def create(self, request, user=None):
        """Create an idempotent, one-item batch for an external tool under the caller's identity."""
        if self.batches.db is None:
            return error(410, 'The front keeps benchmark tasks for this node; submit them through turnstile.')
        user = await self.who(request) if user is None else user
        if isinstance(user, Response):
            return user
        if not user.can('benchmark'):
            return error(403, 'Your account may not run model measurements.')
        try:
            body = await self.body(request)
            number, token, model = body.get('id'), body.get('token'), body.get('model')
            if not isinstance(number, str) or not re.fullmatch(r'batch_[a-f0-9]{24}', number):
                raise ValueError('Provide a random batch_ ID with 24 hexadecimal digits.')
            if not isinstance(token, str) or not 32 <= len(token) <= 128:
                raise ValueError('Provide a random task token of 32 to 128 characters.')
            if 'model_id' in body:
                identity = body['model_id']
                if not isinstance(identity,str):
                    raise ValueError('model_id must be a stable local model ID.')
                model = self.runner.models.name_for(identity)
            if not isinstance(model, str) or self.runner.models.get(model) is None or not user.group.may_use(model):
                return error(404, 'The selected local model is not available.')
            found = self.get(number)
            if found:
                digest = self.batches.db.execute('SELECT token_hash FROM benchmark_tasks WHERE id=?', (number,)).fetchone()[0]
                if found['user'] != user.name or found['model'] != model or digest != hashlib.sha256(token.encode()).hexdigest():
                    return error(409, 'That task ID is already in use.')
                return JSONResponse(self.public(found))
            if self.batches.get(number):
                return error(409, 'That batch ID is already in use.')
            fingerprint, subject = self.probes.subject(model)
            subject['owner_id'] = user.identity
            environment = await self.probes.identify(subject['settings'].get('engine', 'llama_cpp'))
            metadata = body.get('metadata', {})
            if not isinstance(metadata, dict):
                raise ValueError('Task metadata must be an object.')
            line = {'custom_id': 'task', 'body': {'model': model}}
            content = json.dumps(dict(line, method='POST', url=ENDPOINT)).encode()
            file = self.batches.put_file(user.name, 'task.jsonl', 'batch', content)
            self.batches.start(user.name, ENDPOINT, file['id'], [line], metadata, number=number)
            with self.batches.db:
                self.batches.db.execute('INSERT INTO benchmark_tasks(id,token_hash,model,fingerprint,subject,environment,model_id) VALUES(?,?,?,?,?,?,?)',
                    (number, hashlib.sha256(token.encode()).hexdigest(), model, fingerprint, json.dumps(subject), json.dumps(environment), subject.get('model_id')))
            self.log.add('task', 'created', user.name, '', task=number, model=model, model_id=subject.get('model_id'))
            return JSONResponse(self.public(self.get(number)), status_code=201)
        except (ValueError, OSError) as problem:
            return error(400, str(problem))

    async def status(self, request):
        """Return task state and current authorization to its scoped caller."""
        task = await self.authenticate(request)
        if isinstance(task, Response):
            return task
        await self.users.fresh()  # the owner's permissions as they are now, from front
        task['allowed'] = self.allowed(task['id'])
        task['current_fingerprint'] = self.runner.models.fingerprint(self.runner.models.get(task['model']), task['model'])
        try:
            task['current_environment'] = await self.probes.identify(task['subject']['settings'].get('engine', 'llama_cpp'))
        except (ValueError, OSError) as problem:
            return error(503, str(problem), retry=True)
        return JSONResponse(self.public(task))

    async def probe(self, request):
        """Execute one authorized idempotent low-priority native probe."""
        task = await self.authenticate(request)
        if isinstance(task, Response):
            return task
        await self.users.fresh()
        return await self.run_probe(request, task)

    async def run_probe(self, request, task, body=None):
        """Common native validation/admission for legacy and signed front-dispatched probes."""
        if not self.allowed(task['id']):
            return error(403, 'The task ended or its owner no longer has model-measurement permission.')
        try:
            body = await self.body(request, allow_large_evaluation=True) if body is None else body
            multimedia = task['subject']['settings'].get('engine') in ('sdcpp', 'audiocpp')
            for name, low, high in [('context',0 if multimedia else 128,100000000),('inputs',0,100000000),('outputs',0,32768),('warmup_outputs',0,4096)]:
                value = body.get(name, 0)
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f'{name} must be an integer between {low} and {high}.')
                body[name] = value
            if body['inputs'] + max(body['outputs'],body['warmup_outputs']) > body['context']:
                raise ValueError('The input and output must fit the requested context.')
            evaluation = body.get('evaluation')
            dialogue = body.get('dialogue')
            decisions = body.get('decisions')
            if decisions is not None:
                if not isinstance(decisions, dict):
                    raise ValueError('decisions must contain a native Classifier request object.')
                from loadcoach.classifier import validate, model_facts, limits
                facts = model_facts(task['subject']['settings'])
                if not facts or evaluation is not None or dialogue is not None or any(body.get(key) for key in ('inputs', 'outputs', 'warmup_outputs')):
                    raise ValueError('Typed decision probes require a Classifier model and no generation workload.')
                body['decisions'] = validate({**decisions, 'model': task['model']}, limits(facts)['choices'])
            if type(body.get('reuse', False)) is not bool or body.get('reuse') and evaluation is None and dialogue is None and decisions is None:
                raise ValueError('reuse is a boolean, and only a conversation or evaluation may keep its model server.')
            embedding, warmup = body.get('embedding', False), body.get('warmup_embeddings', False)
            if type(embedding) is not bool or type(warmup) is not bool:
                raise ValueError('embedding and warmup_embeddings must be booleans.')
            if embedding != bool(task['subject']['settings']['flags'].get('embedding')):
                raise ValueError('The probe must use the model\'s configured embedding or generation mode.')
            if embedding and (evaluation is not None or dialogue is not None or body['outputs'] or body['warmup_outputs'] or body['inputs'] > 131072):
                raise ValueError('Embedding probes have no generation/evaluation, and allow up to 131072 input tokens.')
            if warmup and (not embedding or not body['inputs']):
                raise ValueError('Embedding warm-up needs an embedding input.')
            if dialogue is not None:
                self.probes.validate_dialogue(dialogue)
                if evaluation is not None or body['inputs'] or not body['outputs'] or body['warmup_outputs']:
                    raise ValueError('Native conversations require outputs, inputs=0, no evaluation and no warm-up.')
            if evaluation is not None:
                if not isinstance(evaluation,dict):
                    raise ValueError('An evaluation needs a question or messages.')
                self.probes.evaluation_temperature(evaluation)
                self.probes.evaluation_sampling(evaluation)
                if 'messages' in evaluation:
                    self.probes.validate_dialogue(evaluation)
                    if body['inputs'] or not body['outputs'] or body['warmup_outputs']:
                        raise ValueError('Conversation evaluations require outputs, inputs=0 and no warm-up.')
                else:
                    if not isinstance(evaluation.get('question'),str) or not evaluation['question'].strip():
                        raise ValueError('An evaluation needs a question string or messages.')
                    if set(evaluation) & {'tools','response_format'}:
                        raise ValueError('Tools and response_format require a messages evaluation.')
                    blocks = evaluation.get('blocks',[])
                    if not isinstance(blocks,list) or len(blocks)>32 or not all(isinstance(block,str) for block in blocks):
                        raise ValueError('Evaluation blocks must be at most 32 strings.')
                    if not isinstance(evaluation.get('filler',''),str):
                        raise ValueError('Evaluation filler must be a string.')
                    if body['inputs']>131072 or not body['outputs'] or body['warmup_outputs']:
                        raise ValueError('Evaluations allow up to 131072 input tokens, require output tokens and have no warm-up.')
            elif dialogue is None and not embedding and bool(body['inputs']) != bool(body['outputs']):
                raise ValueError('Provide both input and output tokens, or neither for a load-only probe.')
            if evaluation is None and (not isinstance(body.get('text',''),str) or len(body.get('text','')) > 16000 or (body['inputs'] and not body.get('text'))):
                raise ValueError('Provide a short text to tokenize for an inference probe.')
            if multimedia and (body['context'] or body['inputs'] or body['outputs'] or body['warmup_outputs'] or body.get('evaluation') or body.get('dialogue')):
                raise ValueError('Image and audio models support load-only measurements; context is unavailable.')
            if await self.probes.identify(task['subject']['settings'].get('engine', 'llama_cpp')) != task['environment']:
                raise ValueError('Engine or machine changed during the task; submit a new task.')
            result = await self.probes.probe(request, task, body)
            return JSONResponse(result)
        except Refusal as problem:
            return error(problem.status, str(problem), retry=True)
        except (ValueError, OSError, httpx.HTTPError) as problem:
            return error(400, str(problem))

    async def dispatch(self, request):
        """A body-bound front capability for exactly one probe, never a durable compute task."""
        said = getattr(request.state, 'said', {})
        if said.get('purpose') != 'front-probe' or not said.get('caller'):
            return error(403, 'Only front may dispatch its validated task probes.')
        user = await self.who(request)
        if isinstance(user, Response):
            return user
        try:
            raw = await request.body()
            if len(raw) > 1100000:
                return error(413, 'Probe dispatch is too large.')
            envelope = json.loads(raw)
            task, options, attempt = envelope['task'], envelope['options'], envelope['attempt']
            if not isinstance(task, dict) or not isinstance(options, dict) or not isinstance(attempt, str) or not re.fullmatch(r'[a-f0-9]{64}', attempt):
                raise ValueError('Invalid single-probe dispatch.')
            number = task['id']
            if not isinstance(number, str) or not re.fullmatch(r'batch_[a-f0-9]{24}', number):
                raise ValueError('Invalid task ID.')
            if task.get('user') != user.name or task.get('subject', {}).get('owner_id') != user.identity or task.get('status') != 'in_progress':
                return error(403, 'The signed caller must own this active task.')
            await self.users.fresh()
            current = self.runner.models.name_for(task.get('model_id'))
            if not current or not user.can('benchmark') or not user.group.may_use(current):
                return error(403, 'The caller may not measure this model.')
            mark, subject = self.probes.subject(current)
            if mark != task.get('fingerprint') or subject.get('model_id') != task.get('model_id'):
                raise ValueError('Model identity, weights or settings changed; submit a new task.')
            if await self.probes.identify(subject['settings'].get('engine', 'llama_cpp')) != task.get('environment'):
                raise ValueError('Engine or machine changed; submit a new task.')
            if self.get(number) is not None:
                return error(409, 'A legacy compute task already owns this ID.')
            stamp = hashlib.sha256(json.dumps({"id": number, "model_id": task.get('model_id'),
                "fingerprint": task.get('fingerprint'), "subject": task.get('subject'),
                "environment": task.get('environment'), "options": options}, sort_keys=True).encode()).hexdigest()
            previous = self.receipts.get((number, attempt))
            if previous:
                if previous[0] != stamp:
                    return error(409, 'A probe attempt cannot change its frozen request.')
                return JSONResponse(previous[1])
            if number in self.dispatched:
                return error(409, 'The earlier probe is still being cleaned up.', retry=True)
            # Run with trusted local settings, not caller-supplied executable flags/paths.
            task = {**task, 'model': current, 'subject': {**subject, 'owner_id': user.identity}}
            self.dispatched[number] = task
            try:
                result = await self.run_probe(request, task, options)
                if result.status_code == 200:
                    if self.probes.kept and self.probes.kept['task'] == number:
                        self.front_kept.clear()
                        self.front_kept[number] = task
                    self.receipts[(number, attempt)] = (stamp, json.loads(result.body))
                    while len(self.receipts) > 128:
                        self.receipts.pop(next(iter(self.receipts)))
                return result
            finally:
                self.dispatched.pop(number, None)
        except (ValueError, KeyError, TypeError, OSError) as problem:
            return error(400, str(problem))

    async def dispatch_interrupt(self, request):
        """Interrupt an active scoped probe and await cleanup."""
        said = getattr(request.state, 'said', {})
        if said.get('purpose') != 'front-probe' or not said.get('caller'):
            return error(403, 'Only front may interrupt its task probes.')
        user = await self.who(request)
        if isinstance(user, Response):
            return user
        number = request.path_params['number']
        task = self.dispatched.get(number)
        # A kept server has no running dispatch; front signed this owner-checked control.
        if task and task['subject'].get('owner_id') != user.identity:
            return error(403, 'This is another account\'s task.')
        await self.probes.drop_kept(number)
        self.front_kept.pop(number, None)
        work = self.probes.working.get(number)
        if work:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        return JSONResponse({'ok': True})

    async def finish(self, request):
        """Idempotently close the external batch, even if its submitter has since lost access."""
        task = await self.authenticate(request)
        if isinstance(task, Response):
            return task
        try:
            body = await self.body(request)
            state = body.get('state')
            if state not in ('completed','failed','cancelled'):
                raise ValueError('Choose completed, failed or cancelled.')
            await self.probes.drop_kept(task['id'])
            await self.users.fresh()
            evidence = None  # for turnstile, which keeps role evidence on the front machine
            if self.on_result and state == 'completed' and task['status'] in ('in_progress', 'completed') and body.get('role_evidence') is not None:
                evidence = self.on_result(task, body['role_evidence'])
            if task['status'] in ('in_progress','cancelling'):
                if state == 'cancelled':
                    self.batches.cancelling(task['id'])
                else:
                    self.batches.keep(task['id'], 0, 200 if state == 'completed' else 500, '', body.get('result', {}))
                self.batches.finish(task['id'])
                self.log.add('task', state, task['user'], '', task=task['id'])
            return JSONResponse({**self.public(self.get(task['id'])), **({'role_evidence': evidence} if evidence else {})})
        except ValueError as problem:
            return error(400, str(problem))

    def recover(self):
        """Interrupted task creation is closed rather than sent to the inference batch worker."""
        for number in self.batches.unfinished():
            if self.batches.get(number)['endpoint'] == ENDPOINT and self.get(number) is None:
                self.batches.keep(number, 0, 500, '', {'error': {'message': 'Task creation was interrupted.'}})
                self.batches.finish(number)

    def routes(self):
        """Register authenticated task creation, execution and completion endpoints."""
        return [Route('/loadcoach/front-probes', self.dispatch, methods=['POST']),
                Route('/loadcoach/front-probes/{number}/interrupt', self.dispatch_interrupt, methods=['POST']),
                Route('/loadcoach/probe-environment', self.environment),
                Route('/loadcoach/probe-subject/{name:path}', self.subject),
                Route('/loadcoach/tasks', self.create, methods=['POST']),
                Route('/loadcoach/tasks/{number}/interrupt', self.interrupt, methods=['POST']),
                Route('/loadcoach/tasks/{number}', self.status),
                Route('/loadcoach/tasks/{number}/probe', self.probe, methods=['POST']),
                Route('/loadcoach/tasks/{number}/finish', self.finish, methods=['POST'])]
