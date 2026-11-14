"""The event log's pages, answered on the front machine (milestone 91).

Compute sends turnstile every entry it writes (loadcoach/front.py), and turnstile keeps them in its
own log, chained with its own entries (loadcoach/log.py, `take`). These addresses were LoadCoach's
and keep their paths and answers: the log, one entry, the requests and one request, usage, and
one's own activity. A request is known by compute's number for it, which callers were given.

What only compute knows of a request still under way, how far it has got and what has come back so
far, is compute's to say: Status, Peek and Stop are still asked of it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.capture import resolve
from loadcoach.log import Log
from loadcoach.log import MACHINE_SHIFT
from loadcoach.diagnostics import presentation, related, distribution
from loadcoach.usage import ENDINGS
from loadcoach.usage import report as usage_report, summary_owner
from loadcoach.users import User, Users
from loadcoach.request_filters import parse as request_filters, STATUSES
from loadcoach.groups import LEVELS
from turnstile.accounts import address, error, lacks, signed_in
from turnstile.reporting import ReportTimeout, run as run_report

HOME_TTL = 180  # a Home summary may lag the live log by three minutes; Usage never uses this cache


def routes(log: Log, users: Users, caller, ask_compute, prices, model_choices=lambda user: ()) -> list[Route]:
    """The log's addresses, read from `log`, front's. `caller(key)` is who a key belongs to, and `ask_compute(user, method, path)` asks compute without waking it.

    `prices()` are what each hosted model costs, from front's own services, for an answer recorded without its cost.
    """
    home_cache: dict[tuple, tuple[float, bytes]] = {}

    async def report(work):
        """Run the report against an independent bounded read-only database connection."""
        try:
            return await run_report(log, work)
        except ReportTimeout:
            return error(503, "The report took too long. Try a narrower range or retry shortly.")

    def page_of(request: Request) -> tuple[int, int] | Response:
        """Validate and bound report pagination parameters."""
        try:
            return (min(max(int(request.query_params.get('limit', 25)), 1), 1000),
                    min(max(int(request.query_params.get('page', 1)), 1), 1_000_000))
        except ValueError:
            return error(400, 'Page and limit must be whole numbers.')

    async def events(request: Request, user: User) -> Response:
        """The event log: one's own entries, or with `see-log`, anyone's.

        `after=ID` gives what is newer than that entry, for following along, and `before=ID`
        what is older, for going back. `limit`, `kind`, `what`, `model`, `trace` and, with
        `see-log`, `user` narrow it down. `text=1` includes the text kept of
        sensitive requests, for a user with `see-capture`.
        """
        asked = request.query_params
        from loadcoach.event_filters import FIELD_NAMES

        if asked.get('q_field') and asked['q_field'] not in FIELD_NAMES:
            return error(400, 'Choose a supported event search field.')
        if asked.get('order', 'id') not in ('id', 'time'):
            return error(400, 'Choose a supported event ordering.')
        try:
            after, limit = int(asked.get("after", 0)), min(max(int(asked.get("limit", 100)), 1), 1000)
            before = int(asked.get("before", 0))
        except ValueError:
            return error(400, "after, before and limit must be whole numbers.")
        with_text = asked.get("text") not in (None, "", "0")
        if with_text and (denied := lacks(user, "see-capture")):
            return denied
        whose = asked.get("user") if user.can("see-log") else user.name
        try:
            zone = ZoneInfo(asked.get('timezone') or 'UTC')

            def moment(key):
                """Parse the requested event timestamp for an exact log range."""
                if not asked.get(key):
                    return None
                value = datetime.fromisoformat(asked[key])
                return (value.replace(tzinfo=zone) if value.tzinfo is None else value).timestamp()
            since, until = moment('start'), moment('end')
            if since is not None and until is not None and since > until:
                raise ValueError
        except (ValueError, ZoneInfoNotFoundError):
            return error(400, 'Choose a valid date and time range and timezone.')
        memberships = users.memberships(None if user.can('see-log') else user.name)
        members = [name for name, group in memberships.items() if group == asked['group']] if asked.get('group') else None
        entries = await report(lambda view: view.read(after, limit, whose, asked.get("kind"), asked.get("trace"), with_text,
                                                      asked.get("what"), asked.get("model"), before, since=since, until=until, members=members,
                                                      node=asked.get('node'), q=asked.get('q'), q_field=asked.get('q_field', ''), order_by=asked.get('order', 'id')))
        if isinstance(entries, Response):
            return entries
        return JSONResponse({"events": [{**entry, "machine": log.former.get(entry.get("machine"), entry.get("machine")) or "front"} for entry in resolve(entries)]})

    async def event_choices(request: Request, user: User) -> Response:
        """List authorized event filter values and current group membership."""
        owner = None if user.can('see-log') else user.name
        memberships = users.memberships(owner)
        choices = await report(lambda view: view.choices(owner))
        if isinstance(choices, Response):
            return choices
        choices['node'] = ['front', *log.machine_names]
        for name in choices['user']:
            memberships.setdefault(name, '(unknown)')
        return JSONResponse({'choices': choices, 'user_groups': memberships, 'groups': sorted(set(memberships.values()))})

    async def event_one(request: Request, user: User) -> Response:
        """One entry of the log, whole, and every later entry that says it is `of` it. One's own, or with `see-log`, anyone's.

        The text kept of sensitive work is a person's words, and is left out for anyone without `see-capture`.
        """
        entries = await report(lambda view: view.about(request.path_params["entry"], with_text=True))
        if isinstance(entries, Response):
            return entries
        if not entries:
            return error(404, f"There is no entry {request.path_params['entry']}.")
        if entries[0]["user"] != user.name and (denied := lacks(user, "see-log")):
            return denied
        if not user.can("see-capture"):
            for entry in entries:
                if entry["kind"] in ("request", "tool"):
                    entry.pop("text", None)
        return JSONResponse({"entry": entries[0], "after": entries[1:]})

    async def request_counts(request: Request, user: User) -> Response:
        """Status totals from records already received on front; never wake or poll workers."""
        owner = None if user.can('see-all') else user.name
        at = time.time()
        counts = await report(lambda view: view.request_counts(owner, at))
        if isinstance(counts, Response):
            return counts
        return JSONResponse({'counts': counts, 'at': at, 'scope': 'all' if owner is None else 'mine'},
                            headers={'cache-control': 'private, no-store'})

    async def requests_list(request: Request, user: User) -> Response:
        """List retained request facts visible to the caller."""
        paged = page_of(request)
        if isinstance(paged, Response):
            return paged
        limit, number = paged
        try:
            owner = summary_owner(user, request.query_params)
            filters = request_filters(request.query_params)
        except PermissionError as problem:
            return error(403, str(problem))
        except ValueError as problem:
            return error(400, str(problem))
        group = request.query_params.get('group', '').strip() if request.query_params.get('summary') == 'group' else ''
        memberships = users.memberships(owner)
        found = await report(lambda view: view.requests(owner, limit, (number - 1) * limit,
                                                       request.query_params.get('completed') == '1', group, memberships, filters))
        if isinstance(found, Response):
            return found
        for row in found['requests']:
            row['state'] = row['outcome'] or 'under way, or no ending on record'
            row['can_open'] = row['user'] == user.name or user.can('see-log')
        access = users.client_accesses(None if user.can('see-log') else user.identity or user.name)
        choices = {'model': sorted(model_choices(user)), 'user': sorted(memberships),
                   'ip': access['ips'], 'status': list(STATUSES), 'node': ['front', *log.machine_names], 'level': list(LEVELS)}
        return JSONResponse({**found, 'limit': limit, 'choices': choices, 'accesses': access['rows']})

    async def request_facts(request: Request, user: User) -> Response:
        """Everything on record about one request: when it was asked and how it ended. One's own, or with `see-log`, anyone's.

        `text` is what it sent and what it was answered, if it is among the latest that are kept in
        full: one's own, or with `see-capture`, anyone's.
        """
        number = request.path_params["entry"]
        entries = await report(lambda view: view.about(number, request=True))
        if isinstance(entries, Response):
            return entries
        if not entries or (entries[0]["kind"], entries[0]["what"]) != ("request", "asked"):
            return error(404, f"There is no request {number}.")
        asked = entries[0]
        if asked["user"] != user.name and (denied := lacks(user, "see-log")):
            return denied
        text = await report(lambda view: view.text_of(number))
        if isinstance(text, Response):
            return text
        kept = text is not None
        if kept and asked["user"] != user.name:
            if not user.can("see-capture"):
                text = None
            elif not any(entry["what"] == "peeked" and entry["user"] == user.name for entry in entries):
                # Reading someone else's words is worth a record. One record, however often the page is opened.
                log.add("request", "peeked", user.name, address(request), asked["model"], of=number)
        ended = next((entry for entry in reversed(entries[1:]) if entry["kind"] == "request" and entry["what"] in ENDINGS), None)
        # How far one still under way has got is compute's to say. Not woken for it.
        machine = asked.get("machine")  # none for front's own requests, such as hosted answers: compute has nothing to say of them
        local = number & ((1 << MACHINE_SHIFT) - 1) if number >= 1 << MACHINE_SHIFT else number  # an unprefixed number is the old default node's
        machine = log.former.get(machine, machine) if machine else machine
        live = None if ended or machine is None else await ask_compute(user, "GET", f"/loadcoach/diagnostics/{local}", machine=machine)
        state = None if ended else live.json()["state"] if live is not None and live.status_code == 200 else "under way"
        observed = live.json().get('diagnostics') if live is not None and live.status_code == 200 else None
        details = await report(lambda view: (
            related(view, asked, ended, user.can('see-log')),
            distribution(view, asked, ended, None if user.can('see-log') else user.name)
            if request.query_params.get('summary') == '1' else None,
        ))
        if isinstance(details, Response):
            return details
        observed_trace = None
        if asked.get('request_id'):
            from loadcoach.observation_store import search
            observed_trace = await report(lambda view: search(view.db, None if user.can('see-log') else user.identity or user.name,
                                                            {'request_id': asked['request_id'], 'since': 0}, 1000))
            if isinstance(observed_trace, Response):
                return observed_trace
        return JSONResponse({
            "id": number, "asked": asked, "text": text,
            "text_kept": kept,  # true also when it is kept and this user may not read it
            "ended": ended,
            "looked_at": [entry for entry in entries[1:] if entry["what"] == "peeked"],
            "tools": [entry for entry in entries[1:] if entry["kind"] == "tool"],
            "state": state,  # None: it is over
            "live_available": live is not None and live.status_code == 200,
            "may_stop": not ended and (asked["user"] == user.name or user.can("stop")),
            "diagnostics": presentation(asked, ended, observed),
            "related": details[0],
            "distribution": details[1],
            "observation": observed_trace,
        })

    async def usage(request: Request, user: User) -> Response:
        """Daily and grouped token totals, restricted like the event log and without captured text."""
        try:
            owner = summary_owner(user, request.query_params)
            memberships, costs = users.memberships(owner), prices()
            all_memberships = users.memberships() if user.can('see-summaries') else {}

            def build(view):
                """Build the requested usage report using the independent reader."""
                result = usage_report(view.db, request.query_params, owner, memberships, costs, log.former)
                if user.can('see-summaries'):
                    recorded = view.db.execute(f"SELECT DISTINCT {view.json_value('detail', 'group')} FROM events WHERE kind = 'request'")
                    result['summary_groups'] = sorted(set(all_memberships.values()) | {row[0] for row in recorded if row[0]})
                return result

            result = await report(build)
            if isinstance(result, Response):
                return result
        except PermissionError as problem:
            return error(403, str(problem))
        except ValueError as problem:
            return error(400, str(problem))
        return JSONResponse({**result, "keep_days": log.keep_days, "everyone": owner is None})

    async def account_events(request: Request, user: User) -> Response:
        """Personal account history stays scoped to the caller, including administrators."""
        paged = page_of(request)
        if isinstance(paged, Response):
            return paged
        limit, number = paged
        query, kind = request.query_params.get('q', ''), request.query_params.get('kind', '')
        if len(query) > 200 or kind not in ('', 'request', 'user'):
            return error(400, 'Choose a supported activity type and search of at most 200 characters.')
        found = await report(lambda view: view.personal_events(user.name, limit, (number - 1) * limit, query, kind))
        if isinstance(found, Response):
            return found
        return JSONResponse({**found, 'events': resolve(found['events'])})

    def fresh(handler):
        """Have compute send what it has not sent yet, then answer: a page shows what has just happened. Compute is not woken for it."""
        async def asked(request: Request, user: User) -> Response:
            """Run one fresh report query without replacing the cached aggregate report."""
            home = request.method == 'GET' and request.query_params.get('_home') == '1' and request.url.path in ('/loadcoach/usage', '/loadcoach/requests')
            key = (request.url.path, user.identity or user.name, user.name, user.group_name,
                   tuple(sorted(user.group.can)), tuple(sorted(request.query_params.multi_items()))) if home else None
            now = time.monotonic()
            if key is not None and (hit := home_cache.get(key)) and now - hit[0] < HOME_TTL:
                return Response(hit[1], media_type='application/json', headers={'x-turnstile-report-cache': 'hit', 'cache-control': 'private, no-store'})
            await asyncio.gather(*(asyncio.wait_for(ask_compute(user, "POST", "/loadcoach/send-log",
                                                               machine=name), timeout=5)
                                   for name in log.machine_names), return_exceptions=True)
            answer = await handler(request, user)
            if key is not None and answer.status_code == 200:
                if len(home_cache) >= 256:
                    home_cache.clear()
                home_cache[key] = (time.monotonic(), answer.body)
                answer.headers['x-turnstile-report-cache'] = 'miss'
                answer.headers['cache-control'] = 'private, no-store'
            return answer
        return asked

    def route(path: str, handler) -> Route:
        """Register the handler behind the service authentication wrapper."""
        return Route(path, signed_in(caller, log, fresh(handler)))

    return [Route("/loadcoach/requests/counts", signed_in(caller, log, request_counts)),
            route("/loadcoach/events", events), route("/loadcoach/events/{entry:int}", event_one),
            route("/loadcoach/event-choices", event_choices), route("/loadcoach/requests", requests_list),
            route("/loadcoach/requests/{entry:int}", request_facts), route("/loadcoach/usage", usage),
            route("/loadcoach/account-events", account_events)]
