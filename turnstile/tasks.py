"""Benchmark task records, kept on the front for every node; a node receives only signed individual probes.

The front creates every new task and schedule, on whichever node it names. Task and schedule
IDs the front does not know are older records still held by a node, and keep their route there.
"""

import asyncio
import hashlib
import json
import re
import secrets
import time
from urllib.parse import quote

from starlette.responses import JSONResponse
from starlette.requests import Request

from db import migrate
from db.schemas import TASK_RECORDS
from loadcoach.tasks import ENDPOINT, Tasks, error
from loadcoach.role_evidence import RoleEvidence
from loadcoach.schedule_grants import ScheduleGrants
from loadcoach.schedule_time import validate, is_due, occurrence_ids


DESTINATION_OFF = ("Choosing a node is turned off. Turn on 'Allow callers to choose the node' in Settings → General, "
                   "or leave the node out and the front will pick one.")


def refusal(answer) -> str:
    """The reason a node gave for refusing, from its JSON error, or "" (no answer, or not a 4xx)."""
    if answer is None or not 400 <= answer.status_code < 500:
        return ""
    try:
        said = answer.json().get("error", {})
    except ValueError:
        return ""
    return said.get("message", "") if isinstance(said, dict) and isinstance(said.get("message", ""), str) else ""


class TaskRecords:
    """Scoped benchmark tasks the front keeps for every node, their dispatch and durable evidence publication."""
    body = Tasks.body
    public = staticmethod(Tasks.public)

    def __init__(self, batches, users, log, evidence, ask, dispatch, names, wake_node=None, destination_allowed=lambda: True):
        """Bind durable task records to current account checks and signed node dispatch.

        `destination_allowed()` says whether a caller may name the node (Settings → General).
        """
        self.batches, self.users, self.log, self.evidence = batches, users, log, evidence
        self.ask, self.dispatch, self.names = ask, dispatch, names
        self.destination_allowed = destination_allowed
        self.wake_node = wake_node
        self.working = {}  # outgoing requests; loss/restart never rebinds their machine
        self.publication_cursor = ''
        migrate(batches.db.database, batches.db, "turnstile.benchmark_tasks", TASK_RECORDS)
        self.grants = ScheduleGrants(batches.db, users)

    def get(self, number):
        """Read one task record and its retained execution metadata."""
        row = self.batches.db.execute("SELECT model,fingerprint,subject,environment,model_id,machine,owner_id FROM benchmark_tasks WHERE id=?", (number,)).fetchone()
        batch = self.batches.get(number)
        return None if not row or not batch else {
            "id": number, "model": row[0], "model_at_submit": row[0], "fingerprint": row[1],
            "subject": json.loads(row[2]), "environment": json.loads(row[3]), "model_id": row[4],
            "machine": row[5], "owner_id": row[6], "user": batch["user"], "status": batch["status"],
        }

    @staticmethod
    def identity(task):
        """Read the task owner's stable account identity."""
        return task.get('owner_id') or task['subject'].get('owner_id')

    def owner(self, task):
        """Resolve the task owner and recheck current execution permission."""
        user = self.users.by_identity(self.identity(task))
        return user if user and task.get('model') and user.can("benchmark") and user.group.may_use(task["model"]) else None

    def authenticate(self, request, task, user):
        """Validate the scoped task token without granting general API access."""
        token = request.headers.get("x-loadcoach-task", "")
        saved = self.batches.db.execute("SELECT token_hash FROM benchmark_tasks WHERE id=?", (task["id"],)).fetchone()[0]
        if token and secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), saved):
            return True
        return bool(user and user.identity == self.identity(task))

    def visible(self, user, row):
        """Check whether the caller may inspect or control the selected task."""
        owners = self.batches.db.execute('SELECT owner_id FROM benchmark_tasks WHERE id=? UNION '
            'SELECT e.owner_id FROM benchmark_tasks e JOIN batches b ON b.id=e.id WHERE ? IN (b.input_file,b.output_file,b.error_file)',
            (row['id'], row['id'])).fetchall()
        return not owners or any(owner and owner == user.identity for owner, in owners)

    async def select(self, user, model, identity=None, location=None):
        """Fresh inventories only. Equal display names need verified equal weights."""
        if location is not None and location not in self.names:
            raise ValueError("The selected compute location is not configured.")
        answers = await asyncio.gather(*(self.ask(user, "GET", "/v1/models", machine=name) for name in self.names))
        candidates = []
        for name, answer in zip(self.names, answers):
            if answer is not None and answer.status_code == 200:
                for one in answer.json().get("data", []):
                    if one.get("owned_by") == "loadcoach" and (one.get("id") == model or identity and one.get("model_id") == identity):
                        candidates.append((name, one))
        if len(candidates) > 1:
            hashes = {one.get("content_sha256") for _, one in candidates}
            if None in hashes or "" in hashes or len(hashes) != 1:
                raise ValueError("Duplicate model names need matching verified weights hashes.")
        if location is not None:
            candidates = [(name, one) for name, one in candidates if name == location]
        if identity:
            candidates = [(name, one) for name, one in candidates if one.get("model_id") == identity]
        if hasattr(self, 'locations'):
            candidates = [(name, one) for name, one in candidates if self.locations.enabled(name, one.get('model_id'))]
        if not candidates:
            raise ValueError("The selected local model is unavailable on its machine.")
        name, model = candidates[0]
        if model.get("capabilities", {}).get("native_probes") is False and not model.get("capabilities", {}).get("load_probes"):
            raise ValueError("This engine does not support native benchmark probes; no task was created.")
        if not user.group.may_use(model["id"]):
            raise ValueError("The selected local model is not permitted.")
        subject = await self.ask(user, "GET", "/loadcoach/probe-subject/" + quote(model["id"], safe=""), machine=name)
        engine = model.get('engine_id', 'llama_cpp')
        environment = await self.ask(user, "GET", "/loadcoach/probe-environment" + ('?engine=' + quote(engine) if engine != 'llama_cpp' else ''), machine=name)
        if subject is None or environment is None or subject.status_code != 200 or environment.status_code != 200:
            raise ValueError("The chosen machine could not freeze the probe subject/environment."
                             + "".join(f" {name} says: {reason}" for reason in [refusal(subject), refusal(environment)] if reason))
        frozen = subject.json()
        if frozen.get('front_probe_dispatch') != 1:
            raise ValueError('This machine needs the matching front-probe dispatch deployment first.')
        if frozen["subject"].get("model_id") != model.get("model_id"):
            raise ValueError("The model inventory changed during task creation; retry.")
        return name, model, frozen, environment.json()

    async def create(self, request, user, chosen_by_caller=True):
        """Validate and create an idempotent benchmark task on the node it names.

        A `machine` in the body is the caller's choice of node, refused while choosing a node is
        turned off. A schedule resolving its own node passes `chosen_by_caller=False`.
        """
        if not user or not user.can("benchmark"):
            return error(403, "Your account may not run measurements.")
        try:
            body = await self.body(request)
            number, token, model = body.get("id"), body.get("token"), body.get("model")
            if not isinstance(number, str) or not re.fullmatch(r"batch_[a-f0-9]{24}", number) or not isinstance(token, str) or not 32 <= len(token) <= 128:
                raise ValueError("Provide a random batch ID and a scoped task token.")
            old = self.get(number)
            digest = hashlib.sha256(token.encode()).hexdigest()
            if old:
                saved = self.batches.db.execute("SELECT token_hash FROM benchmark_tasks WHERE id=?", (number,)).fetchone()[0]
                if (self.identity(old) != user.identity or saved != digest or body.get("model_id", old["model_id"]) != old["model_id"]
                        or not body.get("model_id") and model != old["model_at_submit"]
                        or body.get("machine", old["machine"]) != old["machine"]):
                    return error(409, "That task ID already has an owner.")
                return JSONResponse(self.public(old))
            if self.batches.get(number):
                return error(409, "That ID already belongs to a batch.")
            if body.get("machine") and chosen_by_caller and not self.destination_allowed():
                return error(400, DESTINATION_OFF)
            name, selected, frozen, environment = await self.select(user, model, body.get("model_id"), body.get("machine"))
            if body.get('expected_fingerprint') and (frozen['fingerprint'] != body['expected_fingerprint'] or any(environment.get(key)!=body.get('expected_environment',{}).get(key) for key in ('engine_hash','machine_hash'))):
                raise ValueError('The frozen model or environment changed during task submission.')
            # An ID already accepted before the cutover must retain its compute owner.
            legacy = await self.ask(user, "GET", "/v1/batches/" + number, machine=name)
            if legacy is None or legacy.status_code not in (200, 404):
                return error(503, "Cannot establish this ID's legacy owner; retry.", retry=True)
            if legacy.status_code == 200:
                return error(409, "A legacy task already owns that ID.")
            metadata = body.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("Task metadata must be an object.")
            # The submitting identity and definition version are frozen by front.
            definition = next((one for one in self.evidence.definitions() if one["id"] == metadata.get("benchmark")), None)
            # The benchmark application supplies its run's current protocol version;
            # routing's last published definition can lag a just-deployed protocol.
            version = metadata.get('benchmark_version', definition['version'] if definition else None)
            metadata = {**metadata, "benchmark_version": version if type(version) is int and version > 0 else None}
            subject = {**frozen["subject"], "owner_id": user.identity, "content_sha256": selected.get("content_sha256")}
            file = "file-" + secrets.token_hex(12)
            line = {"model": selected["id"]}
            now = time.time()
            with self.batches.db:
                if metadata.get('benchmark'):
                    from benchmarks.eligibility import reservation_key
                    key = reservation_key(subject['model_id'], name, metadata['benchmark'])
                    self.batches.db.execute("DELETE FROM benchmark_task_reservations WHERE key=? AND task IN (SELECT id FROM batches WHERE status IN ('completed','failed','cancelled','expired'))", (key,))
                    if not self.batches.db.execute('INSERT INTO benchmark_task_reservations(key,task) VALUES(?,?) ON CONFLICT(key) DO NOTHING', (key,number)).rowcount:
                        return error(409, 'Matching benchmark work is already creating, queued or running.')
                self.batches.db.execute("INSERT INTO files(id,user,filename,purpose,created,content) VALUES(?,?,?,?,?,?)",
                    (file, user.name, "task.jsonl", "batch", now, json.dumps({"custom_id": "task", "body": line, "method": "POST", "url": ENDPOINT}).encode()))
                self.batches.db.execute("INSERT INTO batches(id,user,endpoint,input_file,status,created,metadata) VALUES(?,?,?,?,'in_progress',?,?)", (number, user.name, ENDPOINT, file, now, json.dumps(metadata)))
                self.batches.db.execute("INSERT INTO batch_lines(batch,line,custom_id,body,model_id) VALUES(?,0,'task',?,?)", (number, json.dumps(line), subject["model_id"]))
                self.batches.db.execute("INSERT INTO benchmark_tasks(id,token_hash,model,fingerprint,subject,environment,model_id,machine,owner_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (number, digest, selected["id"], frozen["fingerprint"], json.dumps(subject), json.dumps(environment), subject["model_id"], name, user.identity))
            self.log.add("task", "created", user.name, task=number, machine=name, owner="front")
            return JSONResponse(self.public(self.get(number)), status_code=201)
        except (ValueError, KeyError, TypeError) as problem:
            return error(400, str(problem))
        except Exception as problem:
            if self.batches.db.database.driver.is_error(problem):
                if self.get(number):
                    return error(409, 'That task ID was accepted concurrently; retry the same request.')
            raise

    async def schedule_inventory(self, owner):
        """Inspect permitted responding catalogues without waking or loading a node."""
        from turnstile.machines import logical_catalogue
        answers = await asyncio.gather(*(self.ask(owner, 'GET', '/v1/models', machine=name) for name in self.names))
        found = {name: answer.json() for name, answer in zip(self.names, answers) if answer is not None and answer.status_code == 200}
        models = logical_catalogue(found)
        environments = {}
        for logical in models:
            for location in logical.get('locations', []):
                location['enabled'] = not hasattr(self, 'locations') or self.locations.enabled(location['machine'], location.get('model_id'))
                engine = location.get('engine_id', 'llama_cpp')
                key = location['machine'] + ':' + engine
                if key not in environments:
                    answer = await self.ask(owner, 'GET', '/loadcoach/probe-environment?engine=' + quote(engine, safe=''), machine=location['machine'])
                    environments[key] = answer.json() if answer is not None and answer.status_code == 200 else {}
        return {'models': models, 'unavailable': [name for name in self.names if name not in found], 'environments': environments}

    async def schedule(self, request, user):
        """Front authorizes calendar work before it creates a task or compute backup."""
        parts = request.url.path.split('/')
        identifier = parts[3] if len(parts) > 3 else None
        known = identifier and self.batches.db.execute('SELECT 1 FROM schedule_grants WHERE id=?', (identifier,)).fetchone()
        if identifier and not known:
            return None  # an older schedule a node still holds keeps its route there
        if request.method != 'POST':
            return error(405, 'Schedules use POST.')
        try:
            body = await self.body(request)
            if len(parts) == 5 and parts[4] == 'inspect':
                owner, spec = self.grants.authorized(identifier, request.headers.get('x-suite-schedule', ''))
                if spec['kind'] == 'backup':
                    raise ValueError('Backups have no benchmark catalogue.')
                if 'due_at' in body:
                    due = body['due_at']
                    if not is_due(spec, due) or due > time.time():
                        raise ValueError('This timestamp is not a due occurrence.')
                    owner, spec = self.grants.authorized(identifier, request.headers.get('x-suite-schedule', ''), due)
                    if waiting := await self.prepare_schedule(request, owner, spec, identifier, due):
                        return waiting
                return JSONResponse(await self.schedule_inventory(owner), headers={'cache-control':'no-store'})
            if len(parts) == 5 and parts[4] == 'dispatch':
                owner, spec = self.grants.authorized(identifier, request.headers.get('x-suite-schedule', ''))
                due = body.get('due_at')
                if not is_due(spec, due) or due > time.time():
                    raise ValueError('This timestamp is not a due occurrence.')
                owner, spec = self.grants.authorized(identifier, request.headers.get('x-suite-schedule', ''), due)
                number, backup_id = occurrence_ids(identifier, due)
                old = self.get(number) if spec['kind'] != 'backup' else None
                if old:
                    # Lost replies do not wake an already accepted task's node.
                    if old['owner_id'] != owner.identity:
                        raise PermissionError('This occurrence belongs to another account.')
                    return JSONResponse(self.public(old))
                chosen = body.get('target') if spec['kind'] == 'outstanding' else None
                if spec['kind'] == 'outstanding':
                    if not isinstance(chosen, dict) or not chosen.get('model_id') or not chosen.get('benchmark'):
                        raise ValueError('An outstanding occurrence needs one frozen target.')
                    if spec.get('benchmarks') and chosen['benchmark'] not in spec['benchmarks']:
                        raise PermissionError('The selected benchmark is outside the schedule scope.')
                if waiting := await self.prepare_schedule(request, owner, spec, identifier, due):
                    return waiting
                if spec['kind'] == 'backup':
                    existing = await self.ask(owner, 'GET', '/loadcoach/backups/' + backup_id, machine='compute')
                    if existing is not None and existing.status_code == 200 and existing.json().get('verification', {}).get('state') == 'verified':
                        return JSONResponse(existing.json())
                    answer = await self.ask(owner, 'POST', '/loadcoach/backups', json.dumps({'id': backup_id, 'retention': spec['retention']}).encode(), machine='compute')
                    return error(503, 'Compute is unavailable.', retry=True) if answer is None else JSONResponse(answer.json(), status_code=answer.status_code)
                from benchmarks.eligibility import resolve_location
                if chosen is not None:
                    inventory = await self.schedule_inventory(owner)
                    logical = next((row for row in inventory['models'] if any(one.get('model_id')==chosen['model_id'] and one['machine']==chosen['machine'] and one.get('enabled',True) for one in row['locations'])), None)
                    if logical is None:
                        return error(503, 'The frozen target node or model is unavailable.', retry=True)
                    scope_ids = {logical.get('model_id'), *(one.get('model_id') for one in logical['locations'])}
                    if spec.get('model_ids') and not scope_ids.intersection(spec['model_ids']):
                        raise PermissionError('The frozen model is outside the authorized scope.')
                    if spec.get('machine') in inventory['unavailable']:
                        return error(503, 'Preferred node inventory is unknown.', retry=True)
                    machine, model, frozen, environment = await self.select(owner, None, chosen['model_id'], chosen['machine'])
                    if frozen['fingerprint'] != chosen['fingerprint'] or any(environment.get(key) != chosen['environment'].get(key) for key in ('engine_hash', 'machine_hash')):
                        raise ValueError('The selected model or environment changed before submission.')
                    benchmark = chosen['benchmark']
                    reason = chosen.get('fallback_reason', '')
                else:
                    inventory = await self.schedule_inventory(owner)
                    logical = next((row for row in inventory['models'] if spec['model_id'] == row.get('model_id') or any(one.get('model_id') == spec['model_id'] for one in row['locations'])), None)
                    if not logical:
                        return error(503, 'The scheduled model inventory is unavailable.', retry=True)
                    try:
                        resolved, reason = resolve_location(logical, spec.get('machine',''), inventory['unavailable'])
                    except ValueError as problem:
                        return error(503, str(problem), retry=True)
                    machine, model, _, _ = await self.select(owner, None, resolved['model_id'], resolved['machine'])
                    benchmark = spec['benchmark']
                payload = {'id': number, 'token': body.get('task_token'), 'model': model['id'], 'model_id': model['model_id'], 'machine': machine, **({'expected_fingerprint':chosen['fingerprint'],'expected_environment':chosen['environment']} if chosen else {}),
                           'metadata': {'benchmark': benchmark, 'schedule': identifier, 'due_at': due,
                                        'preferred_node': spec.get('machine', ''), 'resolved_node': machine, 'fallback_reason': reason}}
                encoded = json.dumps(payload).encode()
                async def receive():
                    """Replay the encoded schedule request to the existing task handler."""
                    return {'type': 'http.request', 'body': encoded, 'more_body': False}
                delegated = Request({**request.scope, 'path': '/loadcoach/tasks'}, receive)
                response = await self.create(delegated, owner, chosen_by_caller=False)
                if response.status_code < 300:
                    return JSONResponse({**json.loads(response.body), 'schedule_resolution':{
                        'preferred_node':spec.get('machine',''), 'resolved_node':machine, 'fallback_reason':reason}},status_code=response.status_code)
                return response
            if not user or not user.can('manage-settings'):
                return error(403, 'Schedule management requires manage-settings.')
            if len(parts) == 4:
                if type(body.get('enabled')) is not bool:
                    raise ValueError('enabled must be true or false.')
                self.grants.enable(identifier, body['enabled'])
                return JSONResponse({'ok': True})
            if len(parts) != 3 and not (len(parts) == 5 and parts[4] == 'edit'):
                return error(404, 'No such schedule operation.')
            spec = validate(body.get('spec'))
            if not self.grants.permitted(user, spec):
                return error(403, 'The owner is not permitted to schedule this work.')
            name = ''
            if spec['kind'] == 'benchmark':
                _, model, _, _ = await self.select(user, None, spec['model_id'])
                name = model['id']
            if identifier:
                self.grants.replace(identifier, body.get('token'), spec, user, body.get('enabled'))
            else:
                self.grants.put(body.get('id'), body.get('token'), spec, user)
            return JSONResponse({'id': identifier or body['id'], 'model_name': name}, status_code=200 if identifier else 201)
        except PermissionError as problem:
            return error(403, str(problem))
        except (ValueError, TypeError, KeyError, OverflowError) as problem:
            return error(400, str(problem))

    async def prepare_schedule(self, request, owner, spec, identifier, due):
        """Only a validated due grant may request its configured execution node's wake."""
        if self.wake_node is None or 'x-turnstile-no-wake' in request.headers:
            return None
        machine = 'compute' if spec['kind'] == 'backup' else spec.get('machine') or 'compute'
        return await self.wake_node(owner, machine, identifier, due, request.headers.get('x-suite-schedule', ''))

    async def refresh(self, task):
        """Refresh the task's pinned model availability from its owning compute node."""
        owner = self.users.by_identity(self.identity(task))
        if not owner:
            return None
        answer = await self.ask(owner, "GET", "/v1/models", machine=task["machine"])
        if answer is None or answer.status_code != 200:
            return None
        current = next((one for one in answer.json().get("data", []) if one.get("model_id") == task["model_id"]), None)
        if current:
            task["model"], task["user"] = current["id"], owner.name
        else:
            task['model'] = None  # an answering catalogue proves removal, unlike an outage
        return current

    async def interrupt(self, task):
        """Cancel active front dispatch and interrupt its scoped compute probe."""
        work = self.working.get(task["id"])
        if work:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        user = self.users.by_identity(self.identity(task))
        if user:
            await self.dispatch(user, "/loadcoach/front-probes/" + task["id"] + "/interrupt", {}, task["machine"])

    async def publish_pending(self, task_id=None):
        """Publish a bounded page of durable evidence after current owner checks."""
        if task_id:
            rows = self.batches.db.execute('SELECT task,snapshot FROM benchmark_task_evidence WHERE published=0 AND task=?', (task_id,)).fetchall()
        else:
            rows = self.batches.db.execute('SELECT task,snapshot FROM benchmark_task_evidence WHERE published=0 AND task>? ORDER BY task LIMIT 20', (self.publication_cursor,)).fetchall()
            if not rows and self.publication_cursor:
                self.publication_cursor = ''
                rows = self.batches.db.execute('SELECT task,snapshot FROM benchmark_task_evidence WHERE published=0 ORDER BY task LIMIT 20').fetchall()
        for number, snapshot in rows:
            if not task_id:
                self.publication_cursor = number
            task = self.get(number)
            if task:
                user = self.users.by_identity(self.identity(task))
                if user and user.can('benchmark'):
                    if not await self.refresh(task):
                        if task.get('model') is not None:
                            continue  # no answering catalogue: retain for bounded retry
                    if self.owner(task):  # recheck revocation after the inventory await
                        self.evidence.accept(json.loads(snapshot), definitions=False)
            with self.batches.db:
                self.batches.db.execute("UPDATE benchmark_task_evidence SET published=1 WHERE task=?", (number,))

    async def retry_publication(self):
        """Retry the bounded durable evidence queue after startup or a transient failure."""
        try:
            await self.publish_pending()
        except Exception:
            print('turnstile: task evidence publication failed; retained for retry.', flush=True)

    async def finish(self, request, task):
        """Persist an idempotent task outcome and queue validated role evidence."""
        try:
            body = await self.body(request)
            state = body.get("state")
            if state not in ("completed", "failed", "cancelled"):
                raise ValueError("Choose completed, failed or cancelled.")
            serialized_finish = json.dumps(body, sort_keys=True)
            await self.interrupt(task)
            snapshot = None
            if state == "completed" and body.get("role_evidence"):
                snapshot = RoleEvidence.validate(body["role_evidence"])
                metadata = self.batches.get(task["id"])["metadata"]
                if len(snapshot["results"]) != 1 or any(
                    one["run"] != task["id"] or one["model_id"] != task["model_id"] or one["fingerprint"] != task["fingerprint"] or
                    one["benchmark"] != metadata.get("benchmark") or one["version"] != metadata.get("benchmark_version") or
                    any(one.get(key) != task["environment"].get(key) for key in ("engine_hash", "machine_hash")) for one in snapshot["results"]):
                    raise ValueError("Evidence must match the frozen task/model/definition/environment.")
            with self.batches.db:
                previous = self.batches.db.execute('SELECT payload FROM benchmark_task_finishes WHERE task=?', (task['id'],)).fetchone()
                if previous and previous[0] != serialized_finish:
                    return error(409, 'A task finish cannot replace its retained result.')
                self.batches.db.execute('INSERT INTO benchmark_task_finishes(task,payload) VALUES(?,?) ON CONFLICT DO NOTHING', (task['id'], serialized_finish))
                if snapshot and task["status"] in ("in_progress", "completed"):
                    saved = self.batches.db.execute("SELECT snapshot FROM benchmark_task_evidence WHERE task=?", (task["id"],)).fetchone()
                    serialized = json.dumps(snapshot, sort_keys=True)
                    if saved and saved[0] != serialized:
                        raise ValueError("A completed task cannot replace its retained evidence.")
                    self.batches.db.execute("INSERT INTO benchmark_task_evidence(task,snapshot) VALUES(?,?) ON CONFLICT DO NOTHING", (task["id"], serialized))
                claimed = self.batches.db.execute("UPDATE batches SET status='finishing' WHERE id=? AND status IN ('in_progress','cancelling')", (task['id'],)).rowcount
                if claimed:
                    result = None if state == "cancelled" else {"status_code": 200 if state == "completed" else 500, "request_id": "", "body": body.get("result", {})}
                    file = None
                    if result:
                        file = "file-" + secrets.token_hex(12)
                        content = json.dumps({"id": "batch_req_" + task["id"][6:] + "_0", "custom_id": "task", "response": result, "error": None}).encode()
                        self.batches.db.execute("INSERT INTO files(id,user,filename,purpose,created,content) VALUES(?,?,?,?,?,?)", (file, task["user"], task["id"] + "_output.jsonl", "batch_output", time.time(), content))
                        self.batches.db.execute("UPDATE batch_lines SET result=? WHERE batch=? AND line=0", (json.dumps(result), task["id"]))
                    column = "error_file" if state == "failed" else "output_file"
                    self.batches.db.execute(f"UPDATE batches SET status=?,ended=?,{column}=? WHERE id=?", ("cancelled" if state == "cancelled" else "completed", time.time(), file, task["id"]))
            await self.publish_pending()
            return JSONResponse(self.public(self.get(task["id"])))
        except ValueError as problem:
            return error(400, str(problem))

    async def handle(self, request, user):
        """Authenticate and dispatch the selected front-task operation."""
        path = request.url.path
        if path.startswith("/loadcoach/front-probes"):
            return error(404, "No public dispatch route.")
        if path == '/loadcoach/schedules' or path.startswith('/loadcoach/schedules/'):
            return await self.schedule(request, user)
        if path == "/loadcoach/tasks" and request.method == "POST":
            return await self.create(request, user)
        parts = path.split("/")
        if len(parts) not in (4, 5) or parts[1:3] != ["loadcoach", "tasks"]:
            return None
        task = self.get(parts[3])
        if task is None:
            return None
        if not self.authenticate(request, task, user):
            return error(404, "No such external task of yours.")
        action = parts[4] if len(parts) == 5 else "status"
        if action == "finish" and request.method == "POST":
            return await self.finish(request, task)
        if action == "interrupt" and request.method == "POST":
            await self.interrupt(task)
            return JSONResponse({"ok": True})
        if action == 'status' and request.method == 'GET' and task['status'] not in ('in_progress', 'cancelling'):
            return JSONResponse(self.public({**task, 'allowed': False, 'current_fingerprint': task['fingerprint'], 'current_environment': task['environment']}))
        current = await self.refresh(task)
        owner = self.owner(task)
        if action == "status" and request.method == "GET":
            if not owner or task["status"] not in ("in_progress", "cancelling"):
                return JSONResponse(self.public({**task, "allowed": False, "current_fingerprint": task["fingerprint"], "current_environment": task["environment"]}))
            if not current:
                return error(503, "The frozen task machine/model is unavailable.", retry=True)
            engine = task['subject']['settings'].get('engine', 'llama_cpp')
            environment = await self.ask(self.users.by_identity(self.identity(task)), "GET", "/loadcoach/probe-environment" + ('?engine=' + quote(engine) if engine != 'llama_cpp' else ''), machine=task["machine"])
            if environment is None or environment.status_code != 200:
                return error(503, "The frozen task machine is unavailable.", retry=True)
            return JSONResponse(self.public({**task, "allowed": bool(owner and task["status"] == "in_progress"), "current_fingerprint": current.get("fingerprint"), "current_environment": environment.json()}))
        if action == "probe" and request.method == "POST":
            if not owner or task["status"] != "in_progress":
                return error(403, "The task ended or its owner lost measurement permission.")
            if not current:
                return error(503, "The frozen machine/model is unavailable.", retry=True)
            if task["id"] in self.working:
                return error(409, "The previous probe is still active.", retry=True)
            try:
                options = await self.body(request, allow_large_evaluation=True)
            except ValueError as problem:
                return error(400, str(problem))
            attempt = request.headers.get("x-loadcoach-probe-id") or secrets.token_hex(32)
            envelope = {"task": task, "options": options, "attempt": attempt}
            outgoing = asyncio.create_task(self.dispatch(owner, "/loadcoach/front-probes", envelope, task["machine"]))
            self.working[task["id"]] = outgoing
            try:
                while not outgoing.done():
                    await asyncio.sleep(.1)
                    if not self.owner(task) or self.batches.get(task["id"])["status"] != "in_progress" or await request.is_disconnected():
                        outgoing.cancel()
                        return error(409, "Probe interrupted; retry the incomplete sample.", retry=True)
                answer = await outgoing
                return error(503, "The frozen machine is unavailable.", retry=True) if answer is None else JSONResponse(answer.json(), status_code=answer.status_code)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                return error(409, 'Probe interrupted; retry the incomplete sample.', retry=True)
            finally:
                outgoing.cancel()
                await asyncio.gather(outgoing, return_exceptions=True)
                self.working.pop(task["id"], None)
        return error(405, "Unsupported task operation.")
