"""Batches, on the front machine: files handed in, and their requests answered later, one after another.

Each request of a batch goes through turnstile's own front door, in the name of whoever handed it in, at
low priority and under the batch's number as its trace. So a role is picked for it, a hosted model may answer
it, and compute only ever sees a request like any other. The store is the one compute used
(loadcoach/batches.py), kept in front's accounts store, so that a batch outlives either machine being off.
"""

import asyncio
from loadcoach import observability as observation
import time

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from loadcoach import batches as rules
from loadcoach.batches import ENDPOINTS, MOST_BYTES, Batches, lines_of, uploaded
from loadcoach.users import User
from turnstile.accounts import address, error, read_json


class Pins:
    """What `Batches.start` pins a queued request by: the model ID compute gave its name when the batch began."""

    def __init__(self, ids: dict[str, str]) -> None:
        """Bind stable model identity lookups used by the batches the front keeps."""
        self.ids = ids

    def identity(self, name: str) -> str | None:
        """Resolve a current model name to its pinned stable identity."""
        return self.ids.get(name)

    def legacy_identity(self, name, created):
        """Resolve an older model reference using retained name history."""
        return None


class FrontBatches:
    """Durable batches the front keeps, executed through authenticated model APIs."""
    def __init__(self, store: Batches, log, ask_compute, door: httpx.AsyncClient, internal_key) -> None:
        """`ask_compute(user, method, path)` asks compute as someone. `door` is turnstile's own front door, and `internal_key(name)` the key that opens it for one account."""
        self.store, self.log, self.ask_compute, self.door, self.internal_key = store, log, ask_compute, door, internal_key
        self.working: dict[str, asyncio.Task] = {}
        self.carrying: set[asyncio.Task] = set()
        self.in_flight: dict[str, dict] = {}  # batch number -> the request under way for it, for the status page
        self.task_owner = None  # front task history additionally checks immutable account identity
        self.image_work = None  # optional image progress/storage hook; admission stays in LoadCoach
        self.audio_work = None  # the same for SoundRoom's audio

    # ---- what compute calls its models -----------------------------------------------------------

    async def model_catalogue(self, user: User) -> tuple[list[dict], bool]:
        """Return permitted local models and whether every configured node answered."""
        answer = (await self.door.get("/v1/models", headers={"authorization": "Bearer " + self.internal_key(user.name)})
                  if self.door else await self.ask_compute(user, "GET", "/v1/models"))
        if answer is None or answer.status_code != 200:
            return [], False
        catalogue = answer.json()
        return ([one for one in catalogue.get("data", []) if one.get("owned_by") == "loadcoach"],
                not catalogue.get("unavailable_machines"))

    async def model_ids(self, user: User) -> dict[str, str]:
        """Pin equal weights across machines as one model; retain local IDs for unhashed files."""
        models, _ = await self.model_catalogue(user)
        return {one["id"]: "sha256:" + one["content_sha256"] if one.get("content_sha256") else one["model_id"]
                for one in models if one.get("content_sha256") or one.get("model_id")}

    # ---- the work --------------------------------------------------------------------------------

    def line_outcome(self, batch, line, status, request_id, result, *, duration=None):
        """Record request metadata and failures, without prompts or generated output."""
        details = dict(batch=batch['id'], line=line['line'] + 1, custom_id=line['custom_id'],
                       endpoint=line.get('endpoint') or batch['endpoint'], status=status, inference_request_id=request_id)
        if duration is not None:
            details['duration'] = duration
        failure = result.get('error') if isinstance(result, dict) else None
        if status >= 400 and failure:
            if isinstance(failure, dict):
                details.update(error=str(failure.get('message') or failure.get('code') or 'Request failed.'),
                               code=str(failure.get('code') or ''))
            else:
                details['error'] = str(failure)
        self.log.add('batch_request', 'failed' if status >= 400 else 'done', batch['user'],
                     model=line['body'].get('model'), trace=batch['id'], **details)

    def work(self, number: str, user_for) -> None:
        """Execute queued batch lines and persist their durable outcomes."""
        batch = self.store.get(number)
        self.log.add('batch', 'running', batch['user'], trace=number, batch=number,
                     endpoint=batch['endpoint'], **batch['request_counts'])
        task = self.working[number] = asyncio.create_task(self.work_through(number, user_for))
        self.carrying.add(task)
        task.add_done_callback(self.carrying.discard)

    async def work_through(self, number: str, user_for) -> None:
        """Answer the requests of one batch that have no answer yet, one after another, and end the batch when all have one."""
        store = self.store
        batch = store.get(number)
        headers = {"authorization": "Bearer " + self.internal_key(batch["user"]), "x-loadcoach-priority": "low", "x-loadcoach-trace": number}
        waiting = None
        def deferred(line, reason, **details):
            """Record one transition into a wait, rather than every polling attempt."""
            nonlocal waiting
            key = (line['line'], reason)
            if waiting != key:
                self.log.add('batch', 'deferred', batch['user'], model=line['body'].get('model'),
                             trace=number, batch=number, line=line['line'] + 1, custom_id=line['custom_id'],
                             reason=reason, **details)
                waiting = key
        def keep(line, status, request_id, result, **details):
            store.keep(number, line['line'], status, request_id, result)
            self.line_outcome(batch, line, status, request_id, result, **details)
        try:
            while store.get(number)["status"] == "in_progress":
                if not (line := store.next_line(number, on_failure=lambda *args: self.line_outcome(batch, *args))):
                    break
                endpoint = line.get('endpoint') or batch['endpoint']
                body = {**line['body'], 'stream': False}
                if endpoint == '/v1/responses':
                    original = line['body']
                    mcp = isinstance(original.get('tools'), list) and any(isinstance(tool, dict) and tool.get('type') == 'mcp' for tool in original['tools'])
                    if 'stream' in original or original.get('store') is True or 'previous_response_id' in original or mcp:
                        keep(line, 400, '', {'error': {'message': 'Responses batch lines must be stateless: no stream, store:true, previous_response_id or MCP tools.',
                            'type': 'invalid_request_error', 'code': 'unsupported_parameter'}})
                        continue
                    body.setdefault('store', False)
                current_user=user_for(batch["user"])
                if current_user is None or self.task_owner and not self.task_owner(current_user,batch):
                    keep(line, 401, "", {"error": {"message": "The account this was handed in by is gone or disabled."}})
                    continue
                if line.get("model_id"):  # pinned when it was handed in: asked of the model that was meant, under whatever it is called now
                    models, complete = await self.model_catalogue(current_user)
                    current = next((one["id"] for one in sorted(models, key=lambda one: one["id"] != body["model"]) if line["model_id"] in
                                    (one.get("model_id"), "sha256:" + one["content_sha256"] if one.get("content_sha256") else None)), None)
                    if current is None:
                        if not complete:  # A partial inventory cannot establish that the pinned model was deleted.
                            deferred(line, 'Waiting for a complete model inventory before retrying.')
                            store.not_begun(number, line["line"])
                            await asyncio.sleep(rules.AGAIN_AFTER)
                            continue
                        keep(line, 404, "", {"error": {"message": "The recorded model was removed.", "code": "model_not_found"}})
                        continue
                    body["model"] = current
                self.in_flight[number] = {"line": line["line"], "model": body["model"], "since": time.monotonic()}
                began = time.monotonic()
                try:
                    with observation.job("batch_line", number+":"+str(line["line"]), user_for(batch["user"]), getattr(self.log, "observations", None)):
                        observation.annotate(benchmark=bool(batch.get('metadata', {}).get('benchmark')))
                        if self.image_work and endpoint.startswith('/v1/images/'):
                            answer=await self.image_work.execute_line(number,line,body,current_user,headers)
                        elif self.audio_work and endpoint.startswith('/v1/audio/'):
                            answer = await self.audio_work.execute_line(number, line, body, current_user, headers)
                        else:
                            answer = await self.door.post(endpoint, json=body, headers=headers, timeout=None)
                except httpx.HTTPError as problem:
                    deferred(line, 'Inference connection unavailable; waiting to retry.', error_type=type(problem).__name__)
                    store.not_begun(number, line["line"])
                    await asyncio.sleep(rules.AGAIN_AFTER)
                    continue
                finally:
                    self.in_flight.pop(number, None)
                if answer.headers.get("x-turnstile") in ("compute-off", "busy") or answer.status_code == 504 or answer.status_code in (429, 503) and "retry-after" in answer.headers:
                    # Not now: compute is off or waking, turnstile is full, it waited too long, or the account has too much under way. Not a try, and not an answer.
                    deferred(line, 'Waiting for inference capacity before retrying.', status=answer.status_code)
                    store.not_begun(number, line["line"])
                    await asyncio.sleep(rules.AGAIN_AFTER)
                    continue
                try:
                    said = answer.json()
                except ValueError:
                    said = {"text": answer.text}
                keep(line, answer.status_code, answer.headers.get("x-loadcoach-request-id", ""), said,
                     duration=time.monotonic() - began)
                waiting = None
        except asyncio.CancelledError:
            if store.get(number)["status"] != "cancelling":
                self.log.add('batch', 'interrupted', batch['user'], trace=number, batch=number,
                             reason='Front stopped during execution. Unanswered requests will resume after restart.')
                raise  # turnstile is stopping. Nothing is ended: the batch is gone on with at the next start
        except Exception as problem:
            self.log.add('batch', 'failed', batch['user'], trace=number, batch=number,
                         error=f'Batch worker stopped unexpectedly ({type(problem).__name__}). Unanswered requests remain saved for restart.',
                         error_type=type(problem).__name__)
            raise
        if store.get(number)["status"]=='paused':
            return
        ended = store.finish(number)
        self.log.add("batch", ended["status"], batch["user"], "", trace=number, batch=number, **ended["request_counts"])

    def resume(self, user_for) -> None:
        """At start: go on with every batch that had not ended."""
        for number in self.store.unfinished():
            if self.store.get(number)["endpoint"] in ENDPOINTS:
                batch = self.store.get(number)
                self.log.add('batch', 'resumed', batch['user'], trace=number, batch=number,
                             reason='Continuing retained batch work after restart.', **batch['request_counts'])
                self.work(number, user_for)

    def status(self, running: list[dict]) -> dict:
        """For the status page: how many batches have not ended, and each that is executing now. `running` are compute's running requests,
        in which a batch's request is found by whose it is and which model it asks: a request still waiting or turned away is not executing.
        They are marked with the batch they belong to, as they are listed."""
        now = time.monotonic()
        active = []
        for number, one in self.in_flight.items():
            batch = self.store.get(number)
            ids = []
            for entry in running:
                if entry.get("batch_id") == number and entry.get("user") == (batch or {}).get("user"):
                    ids.append(entry["id"])
            if batch and ids:
                active.append({"id": number, "model": one["model"], "user": batch["user"], "caller": "batch", "seconds": round(now - one["since"], 1),
                               "request_ids": ids, "href": "/batches", "description": "running batch"})
        return {"open": self.store.open(), "active": active}

    # ---- the addresses ---------------------------------------------------------------------------

    @staticmethod
    def as_openai(one: dict) -> dict:
        """Render a retained batch in the compatible OpenAI batch response shape."""
        return {key: value for key, value in one.items() if key != "user"}

    def theirs(self, user: User, one: dict | None, *, manage=False) -> dict | Response:
        """A file or a batch, if it is this user's. Otherwise the 404 that says it is not there: nobody learns what others have."""
        allowed = one and one['user'] == user.name and (manage or self.task_owner is None or self.task_owner(user, one))
        return one if allowed else error(404, "There is no such file or batch of yours.")

    async def file_upload(self, request: Request, user: User) -> Response:
        """Validate and retain one batch input file."""
        body = await request.body()
        if len(body) > MOST_BYTES:
            return error(413, f"A file may be up to {MOST_BYTES // 2**20} MB.")
        try:
            form = uploaded(request.headers.get("content-type", ""), body)
            (name, content), purpose = form["file"], form["purpose"][1].decode()
        except (ValueError, KeyError, TypeError, AttributeError):
            return error(400, "Upload a file as a form with two fields: `purpose`, and `file`.")
        if purpose != "batch":
            return error(400, "Only files for batches are kept here: purpose is `batch`.")
        file=self.store.put_file(user.name, name or "batch.jsonl", "batch", content)
        if self.image_work:
            self.image_work.store.bind_upload(file['id'],user.identity)
        return JSONResponse(self.as_openai(file))

    async def uploads(self, request: Request, user: User) -> Response:
        """List batch input uploads belonging to the caller."""
        return JSONResponse({"object": "list", "data": [self.as_openai(one) for one in self.store.files(user.name) if not isinstance(self.theirs(user, one), Response)]})

    async def upload_one(self, request: Request, user: User) -> Response:
        """Read or delete an upload only after owner and active-use checks."""
        found = self.theirs(user, self.store.file(request.path_params["number"]))
        if isinstance(found, Response):
            return found
        if request.method == "DELETE":
            self.store.remove_file(found["id"])
            return JSONResponse({"id": found["id"], "object": "file", "deleted": True})
        if request.url.path.endswith("/content"):
            return Response(self.store.content(found["id"]), media_type="application/jsonl")
        return JSONResponse(self.as_openai(found))

    async def batch_start(self, request: Request, user: User, user_for) -> Response:
        """Validate the uploaded lines and create a durable batch."""
        asked = await read_json(request, {"input_file_id", "endpoint", "completion_window", "metadata"})
        if isinstance(asked, Response):
            return asked
        file = self.theirs(user, self.store.file(str(asked.get("input_file_id", ""))))
        if isinstance(file, Response):
            return file
        endpoint = asked.get("endpoint")
        if endpoint not in ENDPOINTS:
            return error(400, f"endpoint is one of {', '.join(ENDPOINTS)}.")
        try:
            lines = lines_of(self.store.content(file["id"]) or b"", endpoint)
        except ValueError as problem:
            return error(400, str(problem))
        self.store.models = Pins(await self.model_ids(user))  # the models as compute names them now, so that a name changing hands later is noticed
        batch = self.store.start(user.name, endpoint, file["id"], lines, asked.get("metadata") if isinstance(asked.get("metadata"), dict) else None)
        if self.image_work and endpoint.startswith('/v1/images/'):
            self.image_work.store.bind_job(batch['id'],user.identity,'',lines[0]['body']['model'],{'request':lines[0]['body']})
        if self.audio_work and endpoint.startswith('/v1/audio/'):
            self.audio_work.store.bind_job(batch['id'], user.identity, '', lines[0]['body']['model'], {'request': lines[0]['body']})
        self.log.add("batch", "started", user.name, address(request), trace=batch['id'], batch=batch["id"], requests=len(lines), endpoint=endpoint)
        self.work(batch["id"], user_for)
        return JSONResponse(self.as_openai(batch))

    async def batch_list(self, request: Request, user: User) -> Response:
        """List durable batches visible to the authenticated caller."""
        if request.url.path.startswith("/v1/"):
            return JSONResponse({"object": "list", "data": [self.as_openai(one) for one in self.store.listing(user.name) if not isinstance(self.theirs(user, one), Response)], "has_more": False})
        return JSONResponse({"batches": [one for one in self.store.listing(None if user.can("see-all") else user.name)
                                       if user.can('see-all') or not isinstance(self.theirs(user, one), Response)]})

    async def batch_one(self, request: Request, user: User) -> Response:
        """Return or cancel the selected authorized durable batch."""
        found = self.store.get(request.path_params["number"])
        if request.method == "POST" and found and user.can("stop"):  # whoever may stop someone else's request may cancel their batch
            found = {**found, "user": user.name}
        found = self.theirs(user, found, manage=request.method == 'POST' and user.can('stop'))
        if isinstance(found, Response):
            return found
        if request.method == "POST" and found["status"] == "in_progress":
            owner = self.store.get(found['id'])['user']
            self.log.add("batch", "cancelling", owner, address(request), trace=found['id'], batch=found["id"], actor=user.name)
            self.store.cancelling(found["id"])
            if found["id"] in self.working and not self.working[found["id"]].done():
                self.working[found["id"]].cancel()  # the request it is waiting for is given up, and the task ends the batch
            else:
                ended = self.store.finish(found["id"])
                self.log.add('batch', ended['status'], owner, address(request), trace=found['id'],
                             batch=found['id'], actor=user.name, **ended['request_counts'])
        return JSONResponse(self.as_openai(self.store.get(found["id"])))

    # ---- accounts --------------------------------------------------------------------------------

    def busy(self, name: str) -> bool:
        """Whether this account has a batch that has not ended."""
        return any(self.store.get(number)["user"] == name for number in self.store.unfinished())

    def renamed(self, name: str, new: str) -> None:
        """Update batch attribution after an account rename."""
        with self.store.db:
            for table in ("files", "batches"):
                self.store.db.execute(f"UPDATE {table} SET user = ? WHERE user = ?", (new, name))
