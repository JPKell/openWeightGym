"""One read-only collection-research-to-draft workflow inside the existing chat process."""

from loadcoach import observability as observation

import asyncio
import json
import math

import httpx
from starlette.responses import JSONResponse

from .knowledge import ANSWER_SYSTEM, Problem, cited_answer, text
from .workflow_store import BUILTINS, Workflows, builtin, digest


class Workflow:
    """Resumable research and drafting with pinned models and cited private sources."""

    def __init__(self, database, compute, knowledge):
        """Bind private workflow storage, local inference and active attempt tracking."""
        self.store = Workflows(database)
        self.compute, self.knowledge = compute, knowledge
        self.client = (
            None  # the existing conversation app's in-process ASGI client, set at build
        )
        self.jobs = {}
        self.slots = asyncio.Semaphore(2)

    def owned(self, user, number):
        """Resolve an owner-private workflow under current sensitivity clearance."""
        if not user.info.get("identity"):
            raise Problem("A current stable account identity is required.", 403)
        run = self.store.one(user.name, number)
        if not run:
            raise Problem("You have no such workflow.", 404)
        if not self.knowledge.permitted(user.info, run["level"]):
            raise Problem(
                "Your group may no longer work at this workflow’s sensitivity.", 403
            )
        return run

    async def call(self, method, path, authorization, **kwargs):
        """Send a private no-wake workflow request and reject unsuccessful responses."""
        response = await self.client.request(
            method, path, headers=self.knowledge.headers(authorization), **kwargs
        )
        if not response.is_success:
            raise Problem(
                response.json()
                .get("error", {})
                .get("message", "The source operation was refused."),
                response.status_code,
            )
        return response.json()

    async def live_sources(self, user, authorization, run):
        """Revalidate the pinned collection and every retained source reference."""
        collection = self.knowledge.owned(user, run["collection"])
        if collection["revision"] != run["revision"]:
            raise Problem(
                "The collection embedding space changed. Create a new workflow.", 409
            )
        sources = []
        for reference in run["sources"]:
            data = await self.call(
                "GET",
                f"/loadcoach/collections/{run['collection']}/sources/{reference['id']}",
                authorization,
            )
            source = data["source"]
            if any(
                source[key] != reference[key]
                for key in ("document", "digest", "generation")
            ):
                raise Problem("An input source changed. Create a new workflow.", 409)
            sources.append(source)
        return collection, sources

    async def usage(self, user, authorization, number, attempt):
        """Reconcile only ordinary request records; unpriced/missing costs stay unknown."""
        receipts = attempt["receipts"]
        records = []
        for receipt in receipts:
            record = {
                "request": receipt["request"],
                "cost": None,
                "prompt_tokens": None,
                "output_tokens": None,
            }
            try:
                response = await self.compute.get(
                    "/loadcoach/requests/" + receipt["request"],
                    headers=self.knowledge.headers(authorization),
                    timeout=2,
                )
                if response.is_success:
                    data = response.json()
                    ended = (
                        (data.get("ended") or {})
                        if data.get("asked", {}).get("trace") == receipt.get("trace")
                        else {}
                    )
                    for target, source in (
                        ("cost", "cost"),
                        ("prompt_tokens", "prompt_tokens"),
                        ("output_tokens", "answer_tokens"),
                    ):
                        value = ended.get(source)
                        if (
                            (
                                target == "cost"
                                and type(value) in (int, float)
                                or target != "cost"
                                and type(value) is int
                            )
                            and math.isfinite(value)
                            and value >= 0
                        ):
                            record[target] = value
            except (httpx.HTTPError, ValueError, TypeError):
                pass
            records.append(record)
        usage = {
            "requests": records,
            "cost": sum(r["cost"] for r in records)
            if records and all(r["cost"] is not None for r in records)
            else None,
        }
        self.store.usage(user.name, number, attempt["id"], usage)

    @observation.jobbed("workflow", 3)
    async def run(self, user, authorization, number, attempt):
        """Execute one pinned research or draft attempt and reconcile permitted usage."""
        step = attempt["step"]
        token = attempt["epoch"]
        try:
            async with asyncio.timeout(300), self.slots:
                run = self.owned(user, number)
                if run["config"].get("version") != 1:
                    raise Problem(
                        "This workflow protocol is unsupported. Create a new run.", 409
                    )
                collection = self.knowledge.owned(user, run["collection"])
                if collection["revision"] != run["revision"]:
                    raise Problem(
                        "The collection embedding space changed. Create a new workflow.",
                        409,
                    )
                if step == "research" and not run["sources"]:
                    result = await self.call(
                        "POST",
                        f"/loadcoach/collections/{run['collection']}/query",
                        authorization,
                        json={"question": run["question"], "minimum": run["minimum"]},
                    )
                    for request in result["requests"]:
                        self.store.receipt(
                            user.name,
                            number,
                            attempt["id"],
                            {
                                "request": str(request),
                                "stage": "retrieval",
                                "trace": "collection-" + run["collection"],
                            },
                        )
                    result["sources"] = result["sources"][
                        : run["config"]["source_limit"]
                    ]
                    if not result["sources"]:
                        self.store.finish(
                            user.name,
                            number,
                            attempt,
                            "no_evidence",
                            error="No relevant source evidence; no research or draft model was invoked.",
                        )
                        return
                    if not result["requests"]:
                        raise Problem(
                            "Retrieval did not provide a request receipt. No research model was started.",
                            502,
                        )
                    if not self.store.sources(
                        user.name, number, token, result["sources"]
                    ):
                        raise Problem(
                            "The workflow changed before research started.", 409
                        )
                    run = self.owned(user, number)
                collection, sources = await self.live_sources(user, authorization, run)
                if not sources:
                    raise Problem(
                        "This workflow has no retained source references.", 409
                    )
                catalog = await self.knowledge.catalog(authorization)
                pinned = run["config"]["models"][step]
                found = next(
                    (m for m in catalog if m.get("model_id") == pinned["model_id"]),
                    None,
                )
                if not found:
                    raise Problem(
                        "The selected model is unavailable or no longer permitted.", 409
                    )
                model = self.knowledge.local(catalog, found["id"], False)
                if any(model[k] != pinned[k] for k in ("fingerprint", "build")):
                    raise Problem(
                        "The selected model changed. Create a new workflow with its new configuration.",
                        409,
                    )
                data = {
                    "question": run["question"],
                    "sources": [
                        {k: s[k] for k in ("id", "name", "page", "line", "text")}
                        for s in sources
                    ],
                }
                if step == "draft":
                    data["accepted_research"] = run["research"]["answer"]
                messages = [
                    {
                        "role": "system",
                        "content": run["config"]["systems"][step]["content"],
                    },
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
                ]
                outputs = run["config"]["outputs"][step]
                if sum(
                    len(m["content"].encode()) for m in messages
                ) + outputs + 64 > user.options(model["id"]).get("context", 8192):
                    raise Problem(
                        "The pinned model has too little conservative context. Use shorter prompts/sources or a larger-context model in a new workflow."
                    )
                await self.knowledge.fresh(user, authorization, collection)
                headers = self.knowledge.headers(authorization, collection)
                headers["x-loadcoach-trace"] = (
                    f"workflow-{number}-{step}-{attempt['ordinal']}"
                )

                def received(response):
                    """Retain a bounded traceable request receipt when inference headers arrive."""
                    request = response.headers.get("x-loadcoach-request-id", "")
                    if request.isdigit() and len(request) <= 20:
                        self.store.receipt(
                            user.name,
                            number,
                            attempt["id"],
                            dict(
                                request=request,
                                stage=step,
                                trace=headers["x-loadcoach-trace"],
                                model_id=response.headers.get(
                                    "x-loadcoach-model-id", ""
                                ),
                                fingerprint=response.headers.get(
                                    "x-loadcoach-fingerprint", ""
                                ),
                                build=response.headers.get("x-loadcoach-build", ""),
                                status=response.status_code,
                            ),
                        )

                reply, _ = await self.knowledge.post(
                    "/v1/chat/completions",
                    {
                        "model": model["id"],
                        "messages": messages,
                        "stream": False,
                        "max_tokens": outputs,
                        "temperature": run["config"]["temperature"],
                    },
                    headers,
                    model,
                    received,
                )
                answer = cited_answer(reply, sources)
                await self.knowledge.fresh(user, authorization, collection)
                if not self.knowledge.store.current(
                    user.name, run["collection"], run["revision"], sources
                ):
                    raise Problem(
                        "A source changed during generation. The result was not accepted.",
                        409,
                    )
                state = "failed_" + step if answer["abstain"] else "review_" + step
                self.store.finish(
                    user.name,
                    number,
                    attempt,
                    state,
                    answer,
                    "The model abstained; review the sources or create a revised workflow."
                    if answer["abstain"]
                    else "",
                )
        except asyncio.CancelledError:
            self.store.finish(
                user.name,
                number,
                attempt,
                "paused_" + step,
                error="Cancelled; resume explicitly with current authorization.",
            )
            raise
        except (Problem, httpx.HTTPError, TimeoutError, ValueError) as why:
            message = (
                str(why)
                if isinstance(why, ValueError)
                else "Inference timed out or compute was unavailable. Resume to retry this step."
            )
            self.store.finish(
                user.name, number, attempt, "failed_" + step, error=message[:500]
            )
        finally:
            saved = next(
                (
                    a
                    for a in self.store.attempts(user.name, number)
                    if a["id"] == attempt["id"]
                ),
                None,
            )
            if saved:
                try:
                    await self.usage(user, authorization, number, saved)
                except asyncio.CancelledError:
                    pass

    async def cancel(self, owner, number):
        """Cancel and join the owner's selected active workflow task."""
        if job := self.jobs.get(number):
            if job[0] == owner:
                job[1].cancel()
                await asyncio.gather(job[1], return_exceptions=True)

    async def list_create(self, request, user):
        """List owner-private workflows or validate a new pinned workflow."""
        if not user.info.get("identity"):
            raise Problem("A current stable account identity is required.", 403)
        if request.method == "GET":
            return JSONResponse(
                {
                    "workflows": [
                        r
                        for r in self.store.listing(user.name)
                        if self.knowledge.permitted(user.info, r["level"])
                    ],
                    "prompts": self.store.prompts(user.name),
                    "builtins": [builtin(k) for k in BUILTINS],
                }
            )
        body = await self.knowledge.body(
            request,
            {
                "title",
                "question",
                "minimum",
                "collection",
                "research_model",
                "draft_model",
                "research_prompt",
                "draft_prompt",
            },
        )
        title = text(body.get("title"), "Title", 120)
        question = text(body.get("question"), "Question", 500)
        minimum = body.get("minimum", 0.65)
        if (
            type(minimum) not in (int, float)
            or not math.isfinite(minimum)
            or not 0 <= minimum <= 1
        ):
            raise Problem("Minimum cosine similarity must be a number from 0 to 1.")
        collection = self.knowledge.owned(
            user, text(body.get("collection"), "Collection id", 64)
        )
        authorization = request.headers.get("authorization", "")
        catalog = await self.knowledge.catalog(authorization)
        config = {
            "version": 1,
            "source_limit": 3,
            "temperature": 0,
            "outputs": {"research": 1024, "draft": 2048},
            "models": {},
            "prompts": {},
            "systems": {},
        }
        for step in BUILTINS:
            model = self.knowledge.local(catalog, body.get(step + "_model"), False)
            config["models"][step] = {
                k: model[k] for k in ("id", "model_id", "fingerprint", "build")
            }
            prompt = self.store.prompt(
                user.name, body.get(step + "_prompt", builtin(step)["id"]), step
            )
            if not prompt:
                raise Problem("Choose a private prompt version of the matching kind.")
            config["prompts"][step] = prompt
            system = ANSWER_SYSTEM + "\n\nWriting task:\n" + prompt["body"]
            config["systems"][step] = {"content": system, "digest": digest(system)}
        number = self.store.create(
            user.name, title, question, minimum, collection, config
        )
        return JSONResponse({"id": number}, status_code=201)

    async def prompts(self, request, user):
        """Validate and create an immutable owner-private prompt version."""
        if not user.info.get("identity"):
            raise Problem("A current stable account identity is required.", 403)
        body = await self.knowledge.body(request, {"name", "kind", "body"})
        kind = body.get("kind")
        if not isinstance(kind, str) or kind not in BUILTINS:
            raise Problem("Prompt kind must be research or draft.")
        number = self.store.version(
            user.name,
            text(body.get("name"), "Name", 80),
            kind,
            text(body.get("body"), "Prompt", 4000),
        )
        return JSONResponse({"id": number}, status_code=201)

    async def one(self, request, user):
        """Read or control an owner-private workflow using its current execution state."""
        number = request.path_params["workflow"]
        run = self.owned(user, number)
        if request.method == "DELETE":
            await self.cancel(user.name, number)
            self.store.remove(user.name, number)
            return JSONResponse({"deleted": True})
        if request.method == "GET":
            return JSONResponse(
                {
                    **run,
                    "attempts": self.store.attempts(user.name, number),
                    "active": number in self.jobs,
                }
            )
        body = await self.knowledge.body(request, {"action", "step"})
        action = body.get("action")
        authorization = request.headers.get("authorization", "")
        if action == "accept":
            self.store.accept(user.name, number, body.get("step"))
        elif action == "cancel":
            await self.cancel(user.name, number)
            self.store.pause(user.name, number)
        elif action == "usage":
            for attempt in self.store.attempts(user.name, number):
                await self.usage(user, authorization, number, attempt)
        elif action == "start":
            if number in self.jobs:
                raise Problem("A workflow step is already running.", 409)
            if len(self.jobs) >= 20:
                raise Problem("The workflow queue is full; retry later.", 429)
            gate = self.knowledge.maintenance
            if gate and gate.token():
                raise Problem("Database maintenance is in progress; retry later.", 503)
            attempt = self.store.begin(user.name, number)
            if attempt is None:
                raise Problem("You have no such workflow.", 404)
            if gate:
                gate.active += 1
            task = asyncio.create_task(self.run(user, authorization, number, attempt))
            self.jobs[number] = (user.name, task)

            def finished(done):
                """Release a completed workflow job and its maintenance activity reservation."""
                if self.jobs.get(number) == (user.name, done):
                    self.jobs.pop(number, None)
                if gate:
                    gate.active -= 1

            task.add_done_callback(finished)
        else:
            raise Problem("Choose start, cancel, accept or usage.")
        return JSONResponse({"saved": True})

    def routes(self, route):
        """Register the owner-private workflow and prompt-version endpoints."""

        def guarded(handler):
            """Wrap workflow endpoints with consistent private error responses."""

            async def handle(request, user):
                """Translate workflow validation or compute failures into private API responses."""
                try:
                    response = await handler(request, user)
                except Problem as why:
                    response = JSONResponse(
                        {"error": {"message": str(why), "type": "workflow_error"}},
                        status_code=why.status,
                    )
                except ValueError as why:
                    response = JSONResponse(
                        {"error": {"message": str(why), "type": "workflow_error"}},
                        status_code=400,
                    )
                except httpx.HTTPError:
                    response = JSONResponse(
                        {
                            "error": {
                                "message": "Compute is unavailable.",
                                "type": "workflow_error",
                            }
                        },
                        status_code=503,
                    )
                response.headers["cache-control"] = "no-store"
                return response

            return handle

        return [
            route(path, guarded(handler), method)
            for path, handler, methods in (
                ("/loadcoach/workflows", self.list_create, ["GET", "POST"]),
                ("/loadcoach/workflows/prompts", self.prompts, ["POST"]),
                (
                    "/loadcoach/workflows/{workflow}",
                    self.one,
                    ["GET", "POST", "DELETE"],
                ),
            )
            for method in methods
        ]

    async def forget(self, owner):
        """Cancel and delete all workflows and prompt versions belonging to an owner."""
        for run in self.store.listing(owner):
            await self.cancel(owner, run["id"])
        self.store.forget(owner)

    async def close(self):
        """Cancel active attempts and close workflow HTTP and database resources."""
        for owner, task in list(self.jobs.values()):
            task.cancel()
        await asyncio.gather(
            *(t for _, t in list(self.jobs.values())), return_exceptions=True
        )
        if self.client:
            await self.client.aclose()
        self.store.db.close()
