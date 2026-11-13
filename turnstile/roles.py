"""Roles, their evidence and reliability, kept on the front machine (milestone 83).

A caller may name a role, such as `chat` or `code:fast`, where a model's name would go. What a role
is, and the rule that picks one of its models, are in loadcoach/roles.py. Turnstile picks here,
before anything is sent on: from the roles, the benchmark evidence and the reliability kept on this
machine, and retained model facts (`/loadcoach/facts`). In a fleet, the background node monitor
refreshes these facts; a chat selects its node locally without querying the fleet. Single-node
installations retain their short live-facts cache. Compute is sent a plain model name, and the
chosen node is woken if needed and permitted.

Every answer to a model request passes through here and is judged on its way with the rule compute
used to judge it by (reliability.answer_outcome). Compute says with each answer which request it
was and which model answered, and how a request it refused ended. A model that keeps failing in a
role is preferred less there, and after three faults in a row is kept out of it until a recovery
check passes or an operator resets it.

The addresses are LoadCoach's old ones, so the Roles and Reliability pages and any harness ask as
before. Compute's tables were copied here once, by `weightroom roles-from`, and are left there as
the way back.
"""

from __future__ import annotations

import asyncio
from loadcoach import observability as observation
import json
from urllib.parse import quote

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.capture import said_wrong
from loadcoach.groups import LEVELS
from loadcoach.log import Log
from loadcoach.reliability import ANSWERED, OUTCOME, Reliability, answer_outcome
from loadcoach.role_evidence import RoleEvidence
from loadcoach.roles import STRENGTHS, Roles, check_role, fast_preference_seconds, loaded_factors, ordered, pick, routing_score, seconds_to_answer
from loadcoach.users import User
from loadcoach.waiting import completion, workload
from turnstile.accounts import address, error, lacks, read_json, signed_in
from turnstile.hosted import TRACE

FACTS_FOR = 3  # seconds what compute said of its models is used before it is asked again
TRIES = 3  # the models of a role tried for one request, when the one picked will not load
PICKED = ("x-loadcoach-role", "x-loadcoach-because", "x-loadcoach-role-context")  # what turnstile tells compute of a pick. Never taken from a caller


def judged(path: str, upstream: httpx.Response, whole: bytes, complete: bool, broke: str) -> str:
    """How a request ended, for reliability. `broke` is "error" or "timeout" if compute stopped answering part-way, and `complete` false if the caller left first."""
    if said := upstream.headers.get(OUTCOME):
        return said  # compute knew before it answered: it refused the request, or the model would not load
    if broke:
        return broke  # as when the model server stops part-way. A compute restarting in the middle of an answer looks the same from here
    if not complete:
        return "cancelled"
    if upstream.status_code >= 400:
        return "error" if upstream.status_code >= 500 else "refused"
    if "content-encoding" in upstream.headers:
        return "unscored"  # compressed bytes say nothing
    if said_wrong(whole):
        return "error"  # a 200 with an error inside, as an outside service may answer
    kind = upstream.headers.get("content-type", "")
    if path.startswith('/v1/images/'):
        from mirrorwall.stream import completed
        if 'text/event-stream' in kind:
            return 'success' if completed(whole) else 'cancelled'
        try:
            return 'success' if json.loads(whole).get('data') else 'unscored'
        except (ValueError,AttributeError):
            return 'unscored'
    if path.startswith('/v1/audio/'):
        from soundroom.stream import completed as audio_completed
        if 'text/event-stream' in kind:
            return 'success' if audio_completed(whole) else 'cancelled'
        return 'success'  # a whole recording or transcript, complete and not an error
    if "text/event-stream" in kind and b"data: [DONE]" not in whole:
        return "cancelled"  # it ended early without breaking: someone stopped it
    return answer_outcome(whole, kind, path)


def needs_of(request: Request, asked: dict) -> dict:
    """What a request needs of the model that answers it: to see, to take tools, room for what was sent, and to stay on compute.

    Work at a sensitive level stays. A trace remembers its level on compute, which holds a later call
    in it to that level whatever it says: compute refuses to send such a call to a hosted model.
    """
    said = [one for one in asked.get("messages") or [] if isinstance(one, dict)]
    pictures = any(isinstance(part, dict) and part.get("type") == "image_url" for one in said if isinstance(one.get("content"), list) for part in one["content"])
    image_count = sum(isinstance(part, dict) and part.get('type') == 'image_url' for one in said if isinstance(one.get('content'), list) for part in one['content'])
    counted = [{**one, 'content': [({'type': 'image_url'} if isinstance(part, dict) and part.get('type') == 'image_url' else part)
                                  for part in one['content']]} if isinstance(one.get('content'), list) else one for one in said]
    stays = "x-loadcoach-log" in request.headers or request.headers.get("x-loadcoach-sensitivity", "normal") != "normal"
    # A rough count of the tokens sent: three bytes to a token. The model server remains the judge of what fits.
    return {"pictures": pictures, "json_schema": isinstance(asked.get('response_format'), dict) and asked['response_format'].get('type') == 'json_schema',
            "tools": bool(asked.get("tools")), "tokens": len(json.dumps(counted)) // 3 + 2048 * image_count, "stays": stays,
            "work": workload(asked)}


def identity(facts: dict, name: str, role: str = "") -> tuple[str, str, str]:
    """The identity a model's reliability in a role is kept under: its model ID and fingerprint, as compute says them."""
    fact = facts["models"].get(name) or {}
    return (fact.get("model_id") or "", fact.get("fingerprint") or "", role)


def passed_over(seen: list[dict]) -> str:
    """Why none of a role's models can answer, each in a few words."""
    return "; ".join(f"{one['name']}: {one['why_not']}" for one in seen) or "it has no model with a weight above 0"


def explain_choices(seen: list[dict], picked, strength) -> None:
    """Say of each model of a role why it was, or was not, the one picked."""
    chosen = next((one for one in seen if picked and one['name'] == picked[0]), None)
    for one in seen:
        one["estimated_seconds"] = one['estimate']['seconds'] if 'estimate' in one else seconds_to_answer(one)
        one['fast_preference_seconds'] = fast_preference_seconds(one)
        if one['why_not']:
            decision = one['why_not']
        elif one is chosen:
            decision = 'selected: ' + picked[1]
        elif chosen and strength == 'fast':
            timing = f"{one['estimated_seconds']:.1f} s" if one['estimated_seconds'] is not None else 'unavailable'
            decision = (f"weighted speed comparison {fast_preference_seconds(one):.1f} versus {fast_preference_seconds(chosen):.1f} "
                        f"for {chosen['name']}, including loaded reuse; timing estimate before preference is {timing}")
        elif chosen and strength == 'available' and not one['warm']:
            decision = 'not warm; a permitted warm candidate is available' if chosen['warm'] else 'none is warm; lower preference or listed later in a tie'
        elif chosen and strength is None:
            decision = (f"ordinary-role score {one['routing_score']:.3g} versus {chosen['routing_score']:.3g} "
                        f"for {chosen['name']}, including loaded-model bonuses; ties favor a boosted resident then list order")
        elif chosen and one['score'] < chosen['score']:
            decision = f"lower preference score ({one['score']:.3g} versus {chosen['score']:.3g})"
        elif chosen and one['score'] == chosen['score'] and seen.index(one) > seen.index(chosen):
            decision = 'equal preference score; listed later'
        else:
            decision = 'lower preference or listed later in a tie'
        one['decision'] = decision


class Routing:
    """The roles, their evidence and reliability on this machine, and what turnstile does with them.

    `facts_for(request, user)` is what compute says of the models this user may use, or the response
    that ends the request: compute is asleep and may not be woken, or did not wake.
    `through(request, user, method, where, body, headers)` sends a request on to compute's LoadCoach,
    waking it if the caller may, and gives its answer still to be read, or the response that ends
    the request. `relay(upstream, judge)` hands that answer back as it comes, and tells `judge` how
    it ended.
    """

    def __init__(self, store, log: Log, caller, facts_for, through, relay, placed: bool = False) -> None:
        """Bind role definitions, model evidence and reliability to front request routing."""
        self.roles = Roles(store)
        self.evidence, self.reliability = RoleEvidence(self.roles.db), Reliability(self.roles.db)
        self.log, self.caller, self.facts_for, self.through, self.relay = log, caller, facts_for, through, relay
        self.placed = placed
        self.node_names = lambda: []  # the active nodes, in order, once turnstile knows them
        self.destination_allowed = lambda: True
        self.resolve_node = lambda name: name
        self.prepare_candidates = None
        self.hosted = None  # the outside services, once turnstile has made them: see turnstile/hosted.py

    async def facts_at(self, request, user):
        """Apply a caller's destination to the same verified placement facts roles use."""
        facts = await self.facts_for(request, user)
        target = getattr(request.state, 'route_node', '')
        if isinstance(facts, Response) or not target:
            return facts
        selected = {name: {**copies[target], 'machine': target}
                    for name, copies in facts.get('node_facts', {}).items() if target in copies}
        return {**facts, 'models': selected,
                'placements': {name: [target] for name, fact in selected.items() if not fact.get('why_not')}}

    def roles_for(self, models) -> list[str]:
        """The roles someone who may use `models` can ask for: those with one of them in."""
        return [role for role in self.roles.names() if set(self.roles.get(role)["models"]) & set(models)]

    def with_roles(self, listed: dict) -> dict:
        """GET /v1/models as compute answered it, with the roles this caller may ask for beside the models."""
        roles = self.roles_for(one["id"] for one in listed["data"])
        return {**listed, "data": [*listed["data"], *({"id": role, "object": "model", "created": 0, "owned_by": "role"} for role in roles)]}

    # ---- picking ------------------------------------------------------------------------------

    def looked_over(self, role: str, needs: dict, facts: dict, without=frozenset()) -> list[dict]:
        """Every model of a role, the one to prefer first, with what is known of it at this moment.

        `why_not` says why a model cannot answer this request, or is None if it can. `without` are
        models that would not load a moment ago, for this same request.
        """
        settings_role = self.roles.get(role)
        work = needs.get('work', {'prompt_tokens': needs.get('tokens', 0), 'output_tokens': 400,
                                  'basis': 'preview prompt estimate and 400 output tokens'})
        required = settings_role.get("requires", {})
        needs = {'json_schema': needs.get('json_schema', False), **{key: max(needs.get(key, 0), required.get(key, 0)) for key in ("pictures", "tools", "tokens", "stays")}}
        found = []
        for name in ordered(settings_role):
            fact = facts["models"].get(name)
            why_not = (
                "it would not load a moment ago" if name in without
                else "you may not use it, or it is not there any more" if fact is None
                else fact["why_not"]
                or ("the request may not leave this machine" if fact["hosted"] and needs["stays"]
                    else "it cannot see pictures" if needs["pictures"] and not fact["sees"]
                    else "it does not support JSON Schema output" if needs.get('json_schema') and not fact.get('capabilities', {}).get('json_schema')
                    else "it does not take tools" if needs["tools"] and not fact["tools"]
                    else "the request is longer than its context" if fact["room"] and needs["tokens"] > fact["room"]
                    else "the measured GPU fit has too little context for this request" if fact["placed"] and fact["context"] and fact["context"] < needs["tokens"]
                    else None)
            )
            fact = fact or {}
            found.append({"name": name, "weight": settings_role["models"][name], "why_not": why_not,
                          "warm": bool(not why_not and fact["warm"]), "resident": bool(not why_not and fact["resident"]),
                          **{key: fact.get(key) for key in ("speed", "prompt_speed", "load", "context", "fit")},
                          "cards": fact.get("cards", []), "evictions": fact.get("evictions", [])})
        identities = {one["name"]: identity(facts, one["name"])[:2] for one in found if not one["why_not"] and not facts["models"][one["name"]]["hosted"]}
        ranked = self.evidence.rank(settings_role, identities, facts["environment"])
        for one in found:
            one.update(ranked[one["name"]])
            if one["evidence_excluded"] and not one["why_not"]:
                one["why_not"] = one["fallback"]
            # Benchmarked timing is used only with compatible, accepted evidence.
            for point in one["evidence"]:
                if point["reason"] == "accepted":
                    for key in ("speed", "load"):
                        if point.get(key) is not None and (key != "speed" or point[key] > 0):
                            one[key] = point[key]
            one['reliability'] = self.reliability.summary(identity(facts, one['name'], role))
            one['base_score'] = one['score']
            one['score'] *= one['reliability']['factor']
            if one['reliability']['excluded'] and not one['why_not']:
                one['why_not'] = one['reliability']['reason']
            one.update(loaded_factors(settings_role, one['resident'] and not one['why_not']))
            one['routing_score'] = routing_score(one)
            if 'wait' in facts['models'].get(one['name'], {}):
                one['estimate'] = completion(facts['models'][one['name']]['wait'], work, one['speed'], one['prompt_speed'])
        return sorted(found, key=lambda one: -one["score"])

    def chosen(self, asked: tuple[str, str | None], needs: dict, facts: dict, without=frozenset()) -> tuple[list[dict], tuple[str, str] | None]:
        """A role's models as looked over, and the one picked with why. None for the pick if none can answer."""
        seen = self.looked_over(asked[0], needs, facts, without)
        picked = pick(asked[1], [one for one in seen if not one["why_not"]])
        if picked and any(one['reliability']['samples'] or one['reliability']['excluded'] for one in seen):
            picked = picked[0], picked[1] + '; ' + next(one for one in seen if one['name'] == picked[0])['reliability']['reason']
        return seen, picked

    async def theirs(self, request: Request, user: User, name) -> tuple | dict | Response | None:
        """The role and strength a name asks for, and the facts it was picked with, if it is a role this user has: (role, strength), facts.

        None for a model's name or any other. The response that ends the request if compute is asleep and is not to be woken.
        """
        asked = self.roles.asked(name) if isinstance(name, str) else None
        if asked is None:
            return None
        facts = await self.facts_at(request, user)
        if isinstance(facts, Response):
            return facts
        return None if name in facts["models"] or asked[0] not in self.roles_for(facts["models"]) else (asked, facts)

    # ---- the way through for a model request -----------------------------------------------------

    async def send_placed(self, request, user, where, body, headers, candidates, role="", aliases=None):
        """Try another eligible machine only before an answer has been handed to the caller."""
        if not self.placed:
            from loadcoach import observability as observation
            with observation.span('dispatch', attempt=1):
                result = observation.responded(await self.through(request, user, request.method, where, body, headers))
                number = result.headers.get('x-loadcoach-request-id', '')
                if number.isdigit():
                    observation.annotate(request_number=int(number))
                return result
        candidates = candidates or [""]  # a model no node lists: the only node answers for it, or turnstile says there is no route
        last = None
        for index, machine in enumerate(candidates):
            sent = body
            if aliases and aliases.get(machine):
                try:
                    asked = json.loads(body)
                    sent = json.dumps({**asked, "model": aliases[machine]}).encode()
                except (ValueError, TypeError):
                    pass
            from loadcoach import observability as observation
            with observation.span('dispatch', attempt=index + 1):
                result = observation.responded(await self.through(request, user, request.method, where, sent, headers, machine=machine))
                if result.headers.get('x-loadcoach-request-id', '').isdigit():
                    observation.annotate(request_number=int(result.headers['x-loadcoach-request-id']))
            retry = (result.status_code in (502, 503) if isinstance(result, httpx.Response)
                     else result.status_code == 503 and "retry-after" in result.headers)
            if not retry or index == len(candidates) - 1:
                return result
            if isinstance(result, httpx.Response):
                if ended := self.judge(result, user, request.url.path, role):
                    ended(b"", True, "")
                await result.aclose()
            last = result
        return last

    def judge(self, upstream: httpx.Response, user: User, path: str, role: str = "", recovery: bool = False):
        """What to tell how this answer ended, to keep it in the model's reliability. None if there is nothing to keep it under."""
        number = upstream.headers.get("x-loadcoach-request-id", "")
        model, model_id, fingerprint = (upstream.headers.get(name, "") for name in ANSWERED)
        if not number.isdigit() or not model_id or not fingerprint:
            return None  # refused before compute kept a record of it, or a request that leaves none
        key = (model_id, fingerprint, role)
        epoch = self.reliability.state(key)["epoch"]  # an operator's reset while it runs means its outcome no longer counts

        def ended(whole: bytes, complete: bool, broke: str) -> None:
            """Record the completed role-routed request's reliability outcome."""
            try:
                # ponytail: compute's request numbers are unique to one compute machine; with two, key outcomes by machine as well
                self.reliability.record(int(number), user.name, model, key, epoch, judged(path, upstream, whole, complete, broke), recovery, owner_id=user.identity or user.name)
            except Exception as problem:  # the answer has gone to the caller already: a store that cannot be written to is put on record, not raised
                if not self.reliability.db.database.is_error(problem):
                    raise
                self.log.add('reliability', 'record-failed', user.name, '', model, of=int(number), why=str(problem))

        ended.image_request = path.startswith('/v1/images/')
        ended.audio_request = path.startswith('/v1/audio/')
        return ended

    @observation.traced("routing")
    async def answer(self, request: Request, user: User, where: str, body: bytes) -> Response:
        """A request to a model, sent on to compute. For a role, the model is picked here first. The answer is judged as it passes."""
        headers = [(name, value) for name, value in request.headers.items() if name not in (*PICKED, 'x-suite-node')]
        try:
            asked = json.loads(body)
            name = asked.get("model")
        except (ValueError, AttributeError):  # not JSON, or JSON that is not an object: compute says what is wrong with it
            name = None
            asked = {}
        target = asked.pop('node', '')
        if not isinstance(target, str):
            return error(400, 'node must be a configured node name.')
        if target:
            from turnstile.tasks import DESTINATION_OFF
            if not self.destination_allowed():
                return error(400, DESTINATION_OFF)
            target = self.resolve_node(target)
            if target not in self.node_names():
                return error(404, f'No node called {target}.')
            request.state.route_node = target
        body = json.dumps(asked).encode()
        hosted = self.hosted
        if hosted and isinstance(name, str) and name in hosted.offered(user):  # a model at an outside service: asked for from here
            if target:
                return error(404, f'Node {target} does not offer model {name}.')
            return await hosted.answer(request, user, asked, name)
        trace = request.headers.get("x-loadcoach-trace")
        level = request.headers.get("x-loadcoach-sensitivity", "normal")
        effective = level
        if trace and TRACE.fullmatch(trace) and level in LEVELS:
            effective = max(level, self.log.trace_level(user.name, trace) or "normal", key=LEVELS.index)
            if LEVELS.index(effective) > LEVELS.index(user.group.max_sensitivity):
                return error(403, f"Your group may not work at the {effective} level.")
            self.log.set_trace_level(user.name, trace, effective)
            headers = [(key, value) for key, value in headers if key != "x-loadcoach-sensitivity"]
            headers.append(("x-loadcoach-sensitivity", effective))
        role = await self.theirs(request, user, name)
        if isinstance(role, Response):
            return role
        stand_in = request.headers.get("x-loadcoach-offload")
        headers = [(key, value) for key, value in headers if key != "x-loadcoach-offload"]  # compute does not answer for hosted models
        if stand_in is not None and any(key.startswith("x-loadcoach-require-") for key in request.headers):
            return error(409, "The required local model identity, fingerprint or build does not match; no inference was sent.")
        if role is None:
            if hosted and stand_in is not None and isinstance(name, str):
                if "x-loadcoach-log" in request.headers or stand_in not in hosted.offered(user):
                    return error(400, "X-LoadCoach-Offload must name a hosted model that you may use, and cannot go with a request that leaves nothing behind.")
                needs = needs_of(request, asked)
                needs["stays"] = needs["stays"] or effective != "normal"
                if await self.offload(request, user, name, stand_in, needs):
                    return await hosted.answer(request, user, asked, stand_in, instead_of=name)
            facts = await self.facts_at(request, user) if self.placed else None
            if isinstance(facts, Response):
                return facts
            if target and (name not in facts.get('models', {}) or facts['models'][name].get('why_not')):
                return error(404, f'Node {target} does not offer an enabled, permitted copy of model {name}.')
            if facts and name in facts.get("unavailable_models", []):
                refusal = error(503, f"The worker for model {name} is not answering. Try again shortly.")
                refusal.headers["retry-after"] = "5"
                return refusal
            candidates = (facts.get("placements", {}).get(name) or
                          [facts["models"][name]["machine"]] if facts and name in facts["models"] and not facts["models"][name].get("hosted") else None)
            if self.prepare_candidates and not target and candidates:
                candidates = await self.prepare_candidates(request, user, name, facts, candidates)
            upstream = await self.send_placed(request, user, where, body, headers, candidates,
                                              aliases=facts.get("location_models", {}).get(name) if facts else None)
            return upstream if isinstance(upstream, Response) else self.relay(upstream, self.judge(upstream, user, request.url.path))
        (role, strength), facts = role
        needs, required = needs_of(request, asked), self.roles.get(role).get("requires", {})
        needs["stays"] = needs["stays"] or effective != "normal"
        # The least context the picked model must be admitted with, or compute answers 502 and the next is tried.
        headers.append(("x-loadcoach-role-context", str(max(needs["tokens"], required.get("tokens", 0)))))
        without, before = set(), None
        for attempt in range(TRIES):
            seen, picked = self.chosen((role, strength), needs, facts, without)
            if picked is None:
                break
            if hosted and picked[0] in hosted.offered(user):
                return await hosted.answer(request, user, asked, picked[0], chose={"role": name, "picked": picked[0], "because": picked[1]}, role=role)
            if hosted and stand_in in hosted.offered(user) and not (needs["stays"] or required.get("stays")) \
                    and await self.offload(request, user, picked[0], stand_in, needs, facts):
                return await hosted.answer(request, user, asked, stand_in, chose={"role": name, "picked": picked[0], "because": picked[1]}, instead_of=picked[0], role=role)
            sent = json.dumps({**asked, "model": picked[0]}).encode()
            candidates = facts.get('placements', {}).get(picked[0])
            if self.prepare_candidates and not target and candidates:
                candidates = await self.prepare_candidates(request, user, picked[0], facts, candidates)
            from loadcoach import observability as observation
            with observation.span('model_attempt', attempt=attempt + 1):
                upstream = await self.send_placed(request, user, where, sent,
                                                  [*headers, ("x-loadcoach-role", name), ("x-loadcoach-because", quote(picked[1]))],
                                                  candidates, role,
                                                  facts.get("location_models", {}).get(picked[0]))
                observation.responded(upstream)
                number = upstream.headers.get('x-loadcoach-request-id', '')
                if number.isdigit():
                    observation.annotate(request_number=int(number))
            if isinstance(upstream, Response):
                return upstream
            if before is not None:
                await before.aclose()
            if upstream.status_code != 502:
                return self.relay(upstream, self.judge(upstream, user, request.url.path, role))
            if ended := self.judge(upstream, user, request.url.path, role):  # it would not load: compute said how that ended
                ended(b"", True, "")
            before = upstream
            without.add(picked[0])
        if before is not None:
            return self.relay(before, None)  # nothing else to try: the reason the last one would not load is the one to tell
        return error(503, f"No model of the role {role} can answer this request. {passed_over(seen)}.")

    async def offload(self, request: Request, user: User, name: str, stand_in: str, needs: dict, facts: dict | None = None) -> bool:
        """Whether the hosted model `stand_in` answers in place of the local `name`: the local one would have to wait, and nothing forbids leaving."""
        if needs["stays"] or request.headers.get("x-loadcoach-sensitivity", "normal") != "normal" or self.hosted.why_not(stand_in, user):
            return False
        if facts is None:
            facts = await self.facts_for(request, user)
            if isinstance(facts, Response):
                return False
        fact = facts["models"].get(name)
        # As compute had it: whenever the local model would not answer at once (not loaded, busy, others in line, someone else's lock).
        return bool(fact and not fact["hosted"] and not fact["why_not"] and not fact.get("free_now", fact["warm"]))

    async def pick_for(self, request: Request, user: User, name: str) -> dict | Response:
        """For compute, answering a batch's request in someone's name: the model this role would be picked as now, with why."""
        role = await self.theirs(request, user, name)
        if role is None or isinstance(role, Response):
            return role or {"picked": None}
        (asked, facts) = role
        seen, picked = self.chosen(asked, {}, facts)
        return {"picked": picked[0], "because": picked[1]} if picked else {"picked": None, "because": f"No model of the role {asked[0]} can answer this request. {passed_over(seen)}."}

    # ---- the addresses -------------------------------------------------------------------------

    async def role_pick(self, request: Request, user: User) -> Response:
        """What a role would pick at this moment, and why, without asking a model: `?model=chat:best`.

        `pictures`, `tools` and `tokens` in the address say what the request would need, if
        anything. Every model of the role is listed with what is known of it, so that it can
        be seen why the others were not picked.
        """
        name = request.query_params.get("model", "")
        role = await self.theirs(request, user, name)
        if isinstance(role, Response):
            return role
        if role is None:
            return error(404, f"There is no role {name!r} for you. A role is asked for as NAME, or NAME with one of :{', :'.join(STRENGTHS)}.")
        asked, facts = role
        wanted = request.query_params
        needs = {"pictures": "pictures" in wanted, "tools": "tools" in wanted, "tokens": int(wanted["tokens"]) if wanted.get("tokens", "").isdigit() else 0, "stays": "stays" in wanted}
        seen, picked = self.chosen(asked, needs, facts)
        explain_choices(seen, picked, asked[1])
        return JSONResponse({"role": asked[0], "strength": asked[1], "picked": picked and picked[0], "because": picked[1] if picked else passed_over(seen), "models": seen})

    async def role_list(self, request: Request, user: User) -> Response:
        """Every role with its models and their weights, in the order they are preferred."""
        if denied := lacks(user, "manage-models"):
            return denied
        facts = await self.facts_for(request, user)
        if isinstance(facts, Response):
            return facts
        listed = [{"name": role, **self.roles.get(role), "order": [one['name'] for one in self.looked_over(role, {}, facts)]} for role in self.roles.names()]
        definitions = self.evidence.definitions()
        return JSONResponse({"roles": listed, "strengths": list(STRENGTHS), **({"benchmarks": definitions} if definitions else {})})

    async def role_publish(self, request: Request, user: User) -> Response:
        """Benchmark evidence, published by WeightRoom's benchmarks from the store they keep on this machine."""
        if denied := lacks(user, "manage-models"):
            return denied
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            return error(413, "Role evidence snapshots may be at most 1 MiB.")
        facts = await self.facts_for(request, user)
        if isinstance(facts, Response):
            return facts
        try:
            snapshot = RoleEvidence.validate(json.loads(raw))
            permitted = {one["model_id"] for one in facts["models"].values() if not one["hosted"]}
            if any(one["model_id"] not in permitted for one in snapshot["results"]):
                return error(403, "You may only publish evidence for your permitted local models.")
            self.evidence.accept(snapshot)
        except (ValueError, OverflowError) as problem:
            return error(400, str(problem))
        self.log.add("role-evidence", "published", user.name, address(request), results=len(snapshot["results"]))
        return JSONResponse({"accepted": len(snapshot["results"])})

    async def role_put(self, request: Request, user: User) -> Response:
        """Add a role, or replace one: `models`, each model's name with its weight."""
        if denied := lacks(user, "manage-models"):
            return denied
        name, existing = request.path_params["name"], self.roles.get(request.path_params["name"])
        asked = await read_json(request, {"models", "benchmarks", "missing", "stale", "max_age_days", "requires", "loaded_bonus", "fast_loaded_bonus"})
        if isinstance(asked, Response):
            return asked
        if existing and "new" in request.query_params:
            return error(409, f"There is already a role named {name!r}.")
        facts = await self.facts_for(request, user)
        if isinstance(facts, Response):
            return facts
        try:
            check_role(name, asked, set(facts["models"]))
            if set(asked.get("benchmarks", {})) - {one["id"] for one in self.evidence.definitions()}:
                raise ValueError("Select benchmarks published by WeightRoom first.")
        except ValueError as problem:
            return error(400, str(problem))
        self.roles.put(name, asked)
        self.log.add("role", "changed" if existing else "added", user.name, address(request), name=name, **asked)
        return JSONResponse({"name": name, **asked}, status_code=200 if existing else 201)

    async def role_remove(self, request: Request, user: User) -> Response:
        """Delete a routing role after current management permission checks."""
        if denied := lacks(user, "manage-models"):
            return denied
        name = request.path_params["name"]
        if not self.roles.remove(name):
            return error(404, f"There is no role named {name!r}.")
        self.log.add("role", "removed", user.name, address(request), name=name)
        return JSONResponse({"removed": name})

    async def reliability_report(self, request: Request, user: User) -> Response:
        """Return authorized reliability summaries for the requested model and role."""
        owner = None if user.can('see-log') else user.identity or user.name
        rows = self.reliability.report(owner, role=request.query_params.get('role') or None)
        facts = await self.facts_for(request, user)
        compute_unavailable = isinstance(facts, Response) or facts.get('compute_unavailable', False)
        facts = {'models': {}} if isinstance(facts, Response) else facts
        current_names = {one["model_id"]: name for name, one in facts["models"].items()}
        # Exclusions must remain inspectable even after their recent outcomes age out
        # or are pruned. Operators see state without another user's request IDs.
        if user.can('manage-models') or user.can('see-log'):
            present = {(row['model_id'], row['fingerprint'], row['role']) for row in rows}
            for key in self.reliability.db.execute('SELECT model_id, fingerprint, role FROM reliability_state WHERE blocked_until>0').fetchall():
                name = current_names.get(key[0])
                if key in present or not name or (request.query_params.get('role') and key[2] != request.query_params['role']):
                    continue
                rows.append({'model_id': key[0], 'fingerprint': key[1], 'role': key[2], 'model': name, 'last_request': None,
                             **self.reliability.summary(key, owner=owner)})
        for row in rows:
            key = (row['model_id'], row['fingerprint'], row['role'])
            row['model'] = current_names.get(row['model_id'], row['model'])
            row['current'] = not compute_unavailable and key == identity(facts, row['model'], row['role'])
            row['may_manage'] = row['current'] and user.can('manage-models') and row['model'] in facts["models"]
            if owner is not None:
                row['factor'] = self.reliability.summary(key)['factor']
                row['reason'] = f"Shared routing preference x{row['factor']:.3f}; counts and feedback above are yours only."
        if wanted := request.query_params.get('model'):
            rows = [row for row in rows if row['model'] == wanted]
        return JSONResponse({'rows': rows, 'window_days': 7, 'sample_limit': 50,
                             'scope': 'all users' if owner is None else 'your requests',
                             'compute_unavailable': compute_unavailable})

    async def reliability_manage(self, request: Request, user: User) -> Response:
        """An operator's reset of a model's reliability in a role, or a recovery check: one short request that, if it is answered, lets the model back in."""
        if denied := lacks(user, 'manage-models'):
            return denied
        asked = await read_json(request, {'model', 'role', 'fingerprint', 'action'})
        if isinstance(asked, Response):
            return asked
        facts = await self.facts_for(request, user)
        if isinstance(facts, Response):
            return facts
        name, role = asked.get('model'), asked.get('role', '')
        if not isinstance(name, str) or not isinstance(role, str) or name not in facts["models"]:
            return error(404, 'Choose a model you may use and a role.')
        settings = self.roles.get(role) if role else {}
        if role and (not settings or name not in settings['models']):
            return error(400, 'That model is not in the selected role.')
        if role and facts["models"][name]["hosted"] and settings.get('requires', {}).get('stays'):
            return error(403, 'This role must stay on compute; a hosted recovery check is not permitted.')
        key = identity(facts, name, role)
        if not key[1] or asked.get('fingerprint') != key[1]:
            return error(409, 'The model fingerprint changed or is not yet known. Refresh before changing reliability.')
        action = asked.get('action')
        try:
            if action == 'reset':
                self.reliability.reset(key)
                self.log.add('reliability', 'reset', user.name, address(request), name, role=role, fingerprint=key[1])
                return JSONResponse({'reset': name})
            if action != 'recover':
                return error(400, 'Choose reset or recover.')
            self.reliability.begin_recovery(key)
        except ValueError as problem:
            return error(409, str(problem))
        self.log.add('reliability', 'recovery-started', user.name, address(request), name, role=role, fingerprint=key[1])
        # The normal way to compute, as this exact principal. No caller's header can claim to be
        # a recovery check: it is one only when started here. One short, low-priority check per key.
        body = json.dumps({'model': name, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}], 'max_tokens': 16, 'stream': True}).encode()
        headers = [('content-type', 'application/json'), ('x-loadcoach-priority', 'low'),
                   ('x-loadcoach-role-context', str(settings.get('requires', {}).get('tokens', 0)))]
        try:
            async with asyncio.timeout(60):
                upstream = await self.send_placed(request, user, '/v1/chat/completions', body, headers,
                                                  facts.get("placements", {}).get(name), role, facts.get("location_models", {}).get(name))
                if isinstance(upstream, Response):
                    return upstream
                async for _ in self.relay(upstream, self.judge(upstream, user, '/v1/chat/completions', role, recovery=True)).body_iterator:
                    pass
            self.reliability.recovering.discard(key)
            state = self.reliability.summary(key)
            self.log.add('reliability', 'recovery-ended', user.name, address(request), name, role=role, fingerprint=key[1], state=state['state'])
            return JSONResponse({'model': name, 'reliability': state})
        except (TimeoutError, httpx.HTTPError):
            # Cancellation of a check is ignored as a model fault; it stays excluded.
            return error(504, 'Recovery did not complete within its 60-second budget or the stream failed. The candidate remains excluded.')
        finally:
            self.reliability.recovering.discard(key)

    async def request_feedback(self, request: Request, user: User) -> Response:
        """Whether an answer was good, with an optional explanation: one's own, or anyone's with rate-answers.

        The rating nudges how much its model is preferred and is kept as the model's quality evidence.
        """
        number = request.path_params['entry']
        facts = await self.facts_for(request, user)
        if isinstance(facts, Response):
            return facts
        usable = {model_id for one in facts["models"].values()
                  for model_id in one.get("model_ids", [one["model_id"]])}
        rater = user.can('rate-answers')
        me = user.identity or user.name
        if request.method == 'GET':
            row = self.reliability.db.execute('SELECT owner_id, model_id, outcome, recovery, feedback FROM reliability_outcomes WHERE request=?', (number,)).fetchone()
            allowed = bool(row and (rater or row[0] == me) and row[1] in usable and row[2] == 'success' and not row[3])
            kept = self.reliability.rating(number) if allowed else None
            return JSONResponse({'may_feedback': allowed, 'feedback': row[4] if allowed else None,
                                 'explanation': kept['explanation'] if kept else ''})
        asked = await read_json(request, {'value', 'explanation'})
        if isinstance(asked, Response):
            return asked
        row = self.reliability.db.execute('SELECT model, model_id FROM reliability_outcomes WHERE request=?' + ('' if rater else ' AND owner_id=?'),
                                          (number,) if rater else (number, me)).fetchone()
        if not row or row[1] not in usable:
            return error(404, 'There is no permitted completed request of yours with that ID.')
        try:
            self.reliability.feedback(number, None if rater else me, asked.get('value'), asked.get('explanation', ''), rater=user.name)
        except LookupError as problem:
            return error(404, str(problem))
        except ValueError as problem:
            return error(400, str(problem))
        self.log.add('reliability', 'feedback', user.name, address(request), row[0], of=number, value=asked['value'])
        return JSONResponse({'request': number, 'feedback': asked['value']})

    async def quality_report(self, request: Request, user: User) -> Response:
        """Each rated model fingerprint's quality score from kept ratings. Explanations only for those who may read everyone's log."""
        rows = self.reliability.quality(explained=10 if user.can('see-log') else 0)
        facts = await self.facts_for(request, user)
        names = {} if isinstance(facts, Response) else {one["model_id"]: name for name, one in facts["models"].items()}
        current = set() if isinstance(facts, Response) else {(one["model_id"], one.get("fingerprint")) for one in facts["models"].values()}
        for row in rows:
            row['model'] = names.get(row['model_id'], row['model'])
            row['current'] = (row['model_id'], row['fingerprint']) in current
        return JSONResponse({'rows': rows})

    async def alert_evidence(self, request: Request, user: User) -> Response:
        """Sanitized operational facts for operators and the front collector: compute's spending, with the model failures kept here.

        No prompt, output, credential, request owner or raw exception is exported.
        """
        if not user.can('manage-settings') and not user.can('mirror-users'):
            return error(403, 'Operational alert evidence requires manage-settings or mirror-users.')
        facts = await self.facts_for(request, user)
        if isinstance(facts, Response):
            return facts
        # Every node's evidence, each asked without waking it. With one node, its refusal is the answer.
        nodes = self.node_names() if len(self.node_names()) > 1 else [""]
        said, missing, refused = {}, [], None
        for node in nodes:
            synced = await self.through(request, user, 'POST', '/loadcoach/send-log', b'', [], machine=node)  # what a node has not sent yet counts toward spending
            if not isinstance(synced, Response):
                await synced.aread()
                await synced.aclose()
            upstream = await self.through(request, user, 'GET', '/loadcoach/alert-evidence', b'', [], machine=node)
            if isinstance(upstream, Response):
                refused = refused or upstream
                missing.append(node)
                continue
            told = json.loads(await upstream.aread())
            await upstream.aclose()
            if upstream.status_code != 200:
                if len(nodes) == 1:
                    return JSONResponse(told, status_code=upstream.status_code)
                missing.append(node)
                continue
            for field, value in told.items():
                if isinstance(value, list):
                    said.setdefault(field, []).extend(value)
                elif isinstance(value, dict):
                    said.setdefault(field, {}).update(value)
                else:
                    said.setdefault(field, value)
        if len(missing) == len(nodes) and refused is not None:
            return refused
        if missing and nodes != [""]:
            said["unavailable_machines"] = missing
        names = {one["model_id"]: name for name, one in facts["models"].items()}
        faults = []
        for mid, fingerprint, role in self.reliability.db.execute('SELECT model_id, fingerprint, role FROM reliability_state').fetchall():
            key = (mid, fingerprint, role)
            name = names.get(mid)
            selected = identity(facts, name, role) if name else None
            current = bool(selected and key == selected)
            state = self.reliability.summary(key)
            active = None if selected and not selected[1] else current and state['excluded']
            faults.append({'key': 'model:' + json.dumps(key), 'active': active,
                           'title': f"Repeated model failures: {name or mid} ({role or 'direct requests'})",
                           'explanation': state['reason'],
                           'recovery': 'Successful recovery or operator reset removed the exclusion.' if current else 'This model revision is no longer configured.'})
        said.pop('spending', None)  # an older compute added it: what may be spent is front's to say
        return JSONResponse({'model failures': faults, 'spending': self.hosted.spending() if self.hosted else [], **said})

    def routes(self) -> list[Route]:
        """Register role-management and reliability endpoints behind caller authentication."""
        def route(path: str, handler, method: str = "GET") -> Route:
            """Register the handler behind the service authentication wrapper."""
            return Route(path, signed_in(self.caller, self.log, handler), methods=[method])

        return [
            route("/loadcoach/roles", self.role_list),
            route("/loadcoach/roles/{name}", self.role_put, "POST"),
            route("/loadcoach/roles/{name}", self.role_remove, "DELETE"),
            route("/loadcoach/role-evidence", self.role_publish, "POST"),
            route("/loadcoach/pick", self.role_pick),
            route("/loadcoach/reliability", self.reliability_report),
            route("/loadcoach/reliability", self.reliability_manage, "POST"),
            route("/loadcoach/requests/{entry:int}/feedback", self.request_feedback),
            route("/loadcoach/requests/{entry:int}/feedback", self.request_feedback, "POST"),
            route("/loadcoach/quality", self.quality_report),
            route("/loadcoach/alert-evidence", self.alert_evidence),
        ]
