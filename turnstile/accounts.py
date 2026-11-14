"""The accounts, kept on the front machine: users, their keys and passwords, and the groups.

These are the addresses that look after them. They were LoadCoach's until milestone 76 and keep
the same paths, so the site and any harness ask exactly as before. Turnstile answers them itself,
from the accounts file on this machine, and writes what changes into a log of its own beside it.
Compute is asked only for what it alone knows: whether an account has work under way
before it is renamed, and the rest of what /loadcoach/me says.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from functools import partial

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.groups import ABILITIES, LEVELS, NOTHING, OWN_KEY, OWN_SETTINGS, adjusted, beyond, read_group, section_of
from loadcoach.log import Log
from loadcoach.users import SignInRefused, User, Users, check_password

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def error(status: int, message: str, code: str | None = None) -> JSONResponse:
    """Build a JSON failure response using the service error envelope."""
    return JSONResponse({"error": {"message": message, "type": "loadcoach_error", "code": code}}, status_code=status)


def key_of(request: Request) -> str:
    """The API key a request came with: as a bearer token, or in the address for a harness that cannot set headers."""
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    return key if scheme.lower() == "bearer" else request.query_params.get("key", "")


def address(request: Request) -> str:
    """Return the caller address, or a placeholder when unavailable."""
    return request.client.host if request.client else "?"


def lacks(user: User, ability: str) -> Response | None:
    """The 403 for a user without `ability`, or None if they have it."""
    return None if user.can(ability) else error(403, f"Your group may not {ABILITIES[ability]}.")


async def read_json(request: Request, allowed: set[str]) -> dict | Response:
    """The request body as a JSON object with only `allowed` fields, or the 400 saying why not."""
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 64 * 1024:
                return error(413, "Account requests may be at most 64 KiB.")
            body.extend(chunk)
        asked = json.loads(body or b"{}")
        unknown = asked.keys() - allowed
    except (ValueError, AttributeError):
        return error(400, "The body must be a JSON object.")
    return error(400, f"The body may set {sorted(allowed)}, nothing else.") if unknown else asked


def signed_in(caller, log: Log, handler):
    """Wrap a handler so it is only called for a known user, who is handed to it. `caller(key)` is who a key belongs to."""

    async def checked(request: Request) -> Response:
        """Resolve the caller's API key before dispatching an account operation."""
        user = caller(key_of(request))
        if user is None:
            log.add("refused", "401", "", address(request), path=request.url.path, why="Missing or wrong API key.")
            return error(401, "Missing or wrong API key.")
        return await handler(request, user)

    return checked


def routes(users: Users, log: Log, caller, ask_compute, roles_for, outside=lambda user: [], batches=None, *, hosted_models=lambda: {}, forget_extractions=None, forget_responses=None) -> list[Route]:
    """The account addresses. `caller(key)` is who a key belongs to, as turnstile says.
    `ask_compute(user, method, path, body)` asks compute as that user, or gives None if it is not answering.
    `roles_for(models)` are the roles someone who may use those models may ask for.
    `outside(user)` are the names of the hosted models that user may ask for, which are front's own."""

    signed_in_here = partial(signed_in, caller, log)

    # ---- three rules, so that nobody raises themselves through these doors ---------------------
    #
    # 1. Nobody changes their own account: its group, what it has of its own, whether it is enabled.
    #    Someone who can do everything has nothing to raise themselves to, so their group and what
    #    they have of their own are theirs to change. Disabling or removing oneself stays closed to all.
    # 2. Nobody changes the group they are in.
    # 3. Nobody hands out what they may not do themselves, to a group or to an account. And so
    #    nobody changes an account that may do more than they may: a new password for it would
    #    be a way to become it.
    #
    # The `weightroom user` and `weightroom group` commands are not held to these. Whoever can run them owns the file.
    ONES_OWN = "Nobody changes their own group, their own abilities or whether they are enabled from here. That is done on the front machine, with the weightroom command."
    ONES_GROUP = "Nobody changes the group they are in from here. That is done on the front machine, with the weightroom command."

    def own_closed(user: User) -> set[str]:
        """What of their own account this user may not change: rule 1."""
        return {"enabled"} if user.can("superuser") else {"group", "enabled", "own"}

    def too_far(user: User, name: str) -> str | None:
        """Why the account `name` is out of this user's reach, in words. None if it is within reach."""
        them = users.by_name(name, also_disabled=True)
        more = them and beyond(user.group, them.group)
        return f"{name} may do more than you: {more}. Only someone who may do that too can change that account." if more else None

    def out_of_reach(user: User, name: str) -> Response | None:
        """Refuse management of an account whose effective privileges exceed the caller's."""
        why = too_far(user, name)
        return error(403, why) if why else None

    def too_much(user: User, wanted, whose: str) -> Response | None:
        """The 403 for handing out what one may not do oneself, or None if `wanted` is within what `user` may do."""
        more = beyond(user.group, wanted)
        return error(403, f"That would give {whose} {more}, which you may not do yourself.") if more else None

    def may_manage(user: User, name: str) -> Response | None:
        """Users may look after their own keys and password. Anyone else's needs manage-users, and that they are within reach."""
        return None if user.name == name else lacks(user, "manage-users") or out_of_reach(user, name)

    # ---- signing in -------------------------------------------------------------------------

    async def sign_in(request: Request) -> Response:
        """Check a name and password, and hand back a session key: an API key that ends after some hours.

        The password arrives as plain text, so it is accepted from three places only: this machine
        itself; a caller whose own key has `sign-in`, which is weightroom passing on what someone
        typed into its page; and a caller whose own key has `app-sign-in`, which is an app doing
        the same. A session made through an app can do no more than the app's own account can.
        """
        asking = caller(key_of(request))
        trusted = asking is not None and asking.can("sign-in")
        app = asking.name if asking is not None and not trusted and asking.can("app-sign-in") else None
        if not trusted and not app and address(request) not in LOOPBACK:
            return error(403, "Sign in at the front machine's site, through an app that may sign people in, or on this machine itself.")
        asked = await read_json(request, {"name", "password", "code", "new_password"})
        if isinstance(asked, Response):
            return asked
        name, password = asked.get("name"), asked.get("password")
        if not isinstance(name, str) or not isinstance(password, str):
            return error(400, "Give a name and a password.")
        name = name.lower()  # logs, authenticator labels and the reply use the stored spelling too
        # Where the person really is, as weightroom reports it. Believed only from its key.
        seen = {"at_front_from": request.headers["x-loadcoach-from"]} if trusted and "x-loadcoach-from" in request.headers else {}
        try:
            key = await users.sign_in(name, password, asked.get("code"), asked.get("new_password"), via=app)
        except SignInRefused as refused:
            log.add("refused", "sign-in", name, address(request), why=refused.code, **seen)
            body = {"message": str(refused), "type": "loadcoach_error", "code": refused.code}
            if refused.secret:  # setting up the second step: what their authenticator app needs
                body["secret"] = refused.secret
                body["uri"] = f"otpauth://totp/LoadCoach:{name}?secret={refused.secret}&issuer=LoadCoach"
            return JSONResponse({"error": body}, status_code=refused.status)
        user = users.by_key(key)
        log.add("user", "signed-in", name, address(request), hours=user.group.session_hours, **seen, **({"via": app} if app else {}))
        ends = datetime.fromtimestamp(user.until).isoformat(timespec="seconds")
        return JSONResponse({"key": key, "name": name, "group": user.group_name, "can": sorted(user.group.can), "ends": ends}, status_code=201)

    async def sign_out(request: Request, user: User) -> Response:
        """End the sign-in this request came with. An API key is not a sign-in and is left alone."""
        ended = users.sign_out(key_of(request))
        if ended:
            log.add("user", "signed-out", user.name, address(request))
        return JSONResponse({"signed_out": ended})

    # ---- oneself ----------------------------------------------------------------------------

    async def me(request: Request, user: User) -> Response:
        """Who this key belongs to and what they may do, from here, with what compute adds when it answers."""
        group = user.group
        mine = {key: value for one in users.listing(user.name) for key, value in one.items() if key in ("keys", "sessions", "password", "second_step")}
        said = {
            "name": user.name, "identity": user.identity or user.name, "group": user.group_name, "can": sorted(group.can),
            "model_access": "*" if group.models is None else list(group.models), "models": [], "roles": [],
            "priority": group.priority, "max_lock": group.max_lock, "max_hold": group.max_hold, "max_requests": group.max_requests,
            "max_sensitivity": group.max_sensitivity, "max_hosted_spend": group.max_hosted_spend,
            "tools": "*" if group.tools is None else list(group.tools), "hidden_tools": list(group.hidden_tools),
            # When this sign-in ends. None for an API key, which does not end.
            "session_ends": user.until and datetime.fromtimestamp(user.until).isoformat(timespec="seconds"),
            "via": user.via, "choices": users.choices(user.name), "compute": False, **mine,
        }
        answer = await ask_compute(user, "GET", "/loadcoach/me")
        if answer is not None and answer.status_code == 200:
            told = answer.json()  # models, chat, usage: the nodes' to say
            said.update(told, compute=bool(set(told.get("machines", [])) - set(told.get("unavailable_machines", []))))
        hosted = [name for name in outside(user) if name not in said["models"]]
        if hosted:  # the models at outside services are front's: compute does not know them
            said["models"] = [*said["models"], *hosted]
            configured = hosted_models()
            said.setdefault("chat", {"unlogged": False, "models": {}})["models"].update({name: {
                "context": 8192, "model_id": None, "name_history": [], "vision": configured.get(name, {}).get('vision', False),
                "json_schema": configured.get(name, {}).get('json_schema', False),
                "classifier": 'systemone' if configured.get(name, {}).get('systemone') else False,
                "provider": configured.get(name, {}).get('service', 'Cloud'), "hosted": True} for name in hosted})
        said["hosted_spent"] = round(log.spent(user.name), 4)  # in the last 24 hours, from this machine's log
        if said["models"]:
            said["roles"] = roles_for(said["models"])  # kept here, for the models they may use
        return JSONResponse(said)

    async def choose(request: Request, user: User) -> Response:
        """Keep what someone has chosen for themselves, such as whether to be shown the telemetry strip."""
        chosen = await read_json(request, set(users.choices(user.name)))
        if isinstance(chosen, Response):
            return chosen
        try:
            users.choose(user.name, chosen)
        except ValueError as problem:
            return error(400, str(problem))
        return JSONResponse(users.choices(user.name))

    # ---- users ------------------------------------------------------------------------------

    async def user_list(request: Request, user: User) -> Response:
        """List permitted account metadata without exposing stored credential verifiers."""
        return lacks(user, "manage-users") or JSONResponse({"groups": list(users.groups), "users": users.listing()})

    async def user_one(request: Request, user: User) -> Response:
        """One account: its group, what it has of its own, everything it may do, and how much it has asked for.

        `usage` is counted from the event log, which is kept here. `why_not` says
        why the one asking may not change this account, in the words a change would be refused
        with. It is null if they may.
        """
        name = request.path_params["name"]
        if denied := None if user.name == name else lacks(user, "manage-users"):
            return denied
        found = users.listing(only=name)
        if not found:
            return error(404, f"No user named {name!r}.")
        try:
            days = max(1, int(request.query_params.get("days", 28)))
        except ValueError:
            return error(400, "days must be a whole number.")
        await ask_compute(user, "POST", "/loadcoach/send-log")  # what compute has not sent yet, so the count is up to date; compute is not woken
        usage = {"usage": [{"days": span, **log.usage(name, span * 86400)} for span in (1, 7, days)], "keep_days": log.keep_days}
        why_not = ONES_OWN if user.name == name and "own" in own_closed(user) else too_far(user, name)
        return JSONResponse({**found[0], **usage, "why_not": why_not})

    async def user_add(request: Request, user: User) -> Response:
        """Add an account. It gets a first key unless `key` is false, and with `temporary_password` true a password that only serves to choose one's own. Each is shown this once."""
        if denied := lacks(user, "manage-users"):
            return denied
        asked = await read_json(request, {"name", "group", "label", "own", "key", "temporary_password"})
        if isinstance(asked, Response):
            return asked
        name, group, own = str(asked.get("name", "")), str(asked.get("group", "")), asked.get("own") or {}
        try:
            users.check_new(name, group)
            if denied := too_much(user, adjusted(users.groups.get(group), own), name):  # rule 3
                return denied
            users.add(name, group)
            users.change(name, own=own)
        except (ValueError, TypeError, AttributeError) as problem:
            return error(400, str(problem))
        answer = {"name": name, "group": group}
        if asked.get("key", True) is not False:
            answer["key"] = users.new_key(name, str(asked.get("label", "first")))
        if asked.get("temporary_password") is True:
            answer["temporary_password"] = users.temporary_password(name)
        log.add("user", "added", user.name, address(request), name=name, group=group, **({"own": own} if own else {}))
        return JSONResponse(answer, status_code=201)

    async def user_change(request: Request, user: User) -> Response:
        """Change a user. One's own password is one's own to change; everything else needs manage-users.

        A password that somebody else sets is temporary: it only serves, at the next sign-in, to
        choose one's own. `temporary_password: true` makes one up and returns it.
        """
        name = request.path_params["name"]
        asked = await read_json(request, {"group", "enabled", "password", "current_password", "temporary_password", "second_step", "own"})
        if isinstance(asked, Response):
            return asked
        needs_manager = asked.keys() - {"password", "current_password"}  # only the password is one's own to change
        if denied := lacks(user, "manage-users") if needs_manager else may_manage(user, name):
            return denied
        if not users.exists(name):
            return error(404, f"No user named {name!r}.")
        if user.name == name and asked.keys() & own_closed(user):
            return error(403, ONES_OWN)  # rule 1
        if denied := None if user.name == name else out_of_reach(user, name):  # rule 3, as they are now
            return denied
        group, enabled, password = asked.get("group"), asked.get("enabled"), asked.get("password")
        if "group" in asked or "own" in asked:  # rule 3, as they would become
            try:
                becoming = adjusted(users.groups.get(group if isinstance(group, str) else users.by_name(name, True).group_name, NOTHING), asked.get("own", users.own(name)) or {})
            except (TypeError, AttributeError):
                return error(400, f"own is an object that may set {', '.join(OWN_SETTINGS)}.")
            if denied := too_much(user, becoming, name):
                return denied
        if (
            (group is not None and not isinstance(group, str))
            or (enabled is not None and not isinstance(enabled, bool))
            or (password is not None and (not isinstance(password, str) or len(password) < 8))
            or asked.get("second_step", False) is not False
            or asked.get("temporary_password", True) is not True
        ):
            return error(400, "group is a name, enabled is true or false, password is at least 8 characters, "
                              "second_step can only be set to false, temporary_password only to true.")  # fmt: skip
        own = user.name == name
        if password and own and (stored := users.password_hash(name)):
            # A sign-in that fell into the wrong hands must not be enough to change the password.
            if not await asyncio.to_thread(check_password, stored, str(asked.get("current_password"))):
                return error(403, "Give your current password as current_password to change it.")
        try:
            users.change(name, group, enabled, password, must_change=not own, second_step=asked.get("second_step"), own=asked.get("own"))
        except ValueError as problem:
            return error(400, str(problem))
        answer: dict = {"changed": sorted(asked.keys() - {"current_password"})}
        if password:  # other sign-ins of theirs end; the one making this change, if it is their own, stays
            users.end_sessions(name, but=key_of(request) if own else None)
        if asked.get("temporary_password"):
            answer["temporary_password"] = users.temporary_password(name)  # shown this once
        # What changed, and to what. Never a password itself, only that it was set.
        said = {key: True if "password" in key else value for key, value in asked.items() if key != "current_password"}
        log.add("user", "changed", user.name, address(request), name=name, **said)
        return JSONResponse(answer)

    async def user_rename(request: Request, user: User) -> Response:
        """Rename an idle account within the caller's reach, keeping its credentials and work.

        Compute is asked first: it refuses if the account has work under way there, and renames
        what it keeps under the name, such as batches. So a rename needs compute to answer.
        """
        name = request.path_params["name"]
        if denied := lacks(user, "manage-users") or out_of_reach(user, name):
            return denied
        asked = await read_json(request, {"name"})
        if isinstance(asked, Response):
            return asked
        new = asked.get("name")
        if not isinstance(new, str):
            return error(400, "Give the new user name as name.")
        if not users.exists(name):
            return error(404, f"No user named {name!r}.")
        try:
            users.check_name(new)
        except ValueError as problem:
            return error(400, str(problem))
        if batches and batches.busy(name):
            return error(409, f"{name} has a batch under way. Rename the account when it has ended.")
        there = await ask_compute(user, "POST", f"/loadcoach/users/{name}/rename", json.dumps({"name": new}).encode())
        if there is None:
            return error(503, "A node is not answering, and every node must agree that the account has no work under way before it is renamed.")
        if there.status_code != 200:
            return JSONResponse(there.json(), status_code=there.status_code)
        try:
            users.rename(name, new)
        except ValueError as problem:
            return error(400, str(problem))
        if batches:
            batches.renamed(name, new)
        log.add("user", "renamed", user.name, address(request), name=name, new_name=new)
        return JSONResponse({"renamed": name, "name": new})

    async def user_remove(request: Request, user: User) -> Response:
        """Remove a manageable account after active-work and self-removal checks."""
        name = request.path_params["name"]
        if denied := lacks(user, "manage-users"):
            return denied
        if not users.exists(name):
            return error(404, f"No user named {name!r}.")
        if user.name == name:
            return error(403, "Nobody removes their own account from here. That is done on the front machine, with the weightroom command.")  # rule 1
        if denied := out_of_reach(user, name):
            return denied
        identity = users.identity(name)
        if forget_extractions:
            await forget_extractions(identity)
        if forget_responses:
            await forget_responses(identity)
        users.remove(name)
        log.add("user", "removed", user.name, address(request), name=name)
        return JSONResponse({"removed": name, "identity": identity})

    # ---- groups -----------------------------------------------------------------------------

    GROUP_SETTINGS = {"can", "models", "tools", "priority", "max_hold", "max_lock", "max_requests", "max_sensitivity", "max_hosted_spend", "session_hours", "second_step"}

    async def group_list(request: Request, user: User) -> Response:
        """Every group with all its settings and its members, and every ability there is with what it allows.

        `why_not` says why the one asking may not change a group, in words, or is null if they may.
        `own_key` names the abilities meant for a program's own key, which "superuser" does not include.
        """
        if not user.can("manage-users") and (denied := lacks(user, "manage-groups")):
            return denied  # someone who looks after users needs to see the groups they can put people in

        def why_not(name: str, group) -> str | None:
            """Explain why the caller may not edit the selected group."""
            if not user.can("manage-groups"):
                return f"Your group may not {ABILITIES['manage-groups']}."
            more = beyond(user.group, group)
            return ONES_GROUP if name == user.group_name else f"The group {name} allows more than you may: {more}." if more else None

        listed = []
        for name in users.groups:
            group = users.groups.get(name)
            listed.append({"name": name, **section_of(group), "members": users.members(name), "why_not": why_not(name, group)})
        return JSONResponse({"groups": listed, "abilities": ABILITIES, "own_key": sorted(OWN_KEY), "levels": list(LEVELS)})

    async def group_put(request: Request, user: User) -> Response:
        """Add or replace a group. PATCH changes only the supplied settings."""
        name = request.path_params["name"]
        if denied := lacks(user, "manage-groups"):
            return denied
        asked = await read_json(request, GROUP_SETTINGS)
        if isinstance(asked, Response):
            return asked
        existing = users.groups.get(name)
        if existing and "new" in request.query_params:
            return error(409, f"There is already a group named {name!r}.")
        if existing is None and request.method == "PATCH":
            return error(404, f"No group named {name!r}.")
        if name == user.group_name:
            return error(403, ONES_GROUP)  # rule 2
        kept = {**users.groups.stored(name), **asked} if existing and request.method == "PATCH" else asked
        try:
            wanted = read_group(name, kept)
        except (ValueError, TypeError) as problem:
            return error(400, str(problem))
        if existing and (more := beyond(user.group, existing)):
            return error(403, f"The group {name} allows more than you may: {more}. Only someone who may do that too can change it.")
        if denied := too_much(user, wanted, f"the group {name}"):  # rule 3
            return denied
        try:
            users.groups.put(name, kept)
        except ValueError as problem:
            return error(400, str(problem))
        log.add("group", "changed" if existing else "added", user.name, address(request), name=name, **asked)
        return JSONResponse({"name": name, **kept}, status_code=200 if existing else 201)

    async def group_remove(request: Request, user: User) -> Response:
        """Remove a group that nobody is in."""
        name = request.path_params["name"]
        if denied := lacks(user, "manage-groups"):
            return denied
        existing = users.groups.get(name)
        if existing is None:
            return error(404, f"No group named {name!r}.")
        if more := beyond(user.group, existing):
            return error(403, f"The group {name} allows more than you may: {more}. Only someone who may do that too can remove it.")
        if inside := users.members(name):  # which also covers one's own group
            return error(409, f"{len(inside)} user{'s are' if len(inside) != 1 else ' is'} in {name}: {', '.join(inside[:5])}. Move them to another group first.")
        users.groups.remove(name)
        log.add("group", "removed", user.name, address(request), name=name)
        return JSONResponse({"removed": name})

    async def group_move_members(request: Request, user: User) -> Response:
        """Move every member to another group after checking that every resulting account is within reach."""
        source = request.path_params["name"]
        if denied := lacks(user, "manage-users"):
            return denied
        asked = await read_json(request, {"group"})
        if isinstance(asked, Response):
            return asked
        target = asked.get("group")
        if not isinstance(target, str) or source == target:
            return error(400, "Choose a different group to move the members to.")
        if users.groups.get(source) is None:
            return error(404, f"No group named {source!r}.")
        destination = users.groups.get(target)
        if destination is None:
            return error(400, f"No group named {target!r}.")
        members = users.members(source)
        if user.name in members and "group" in own_closed(user):
            return error(403, ONES_OWN)
        for name in members:
            if denied := out_of_reach(user, name):
                return denied
            if denied := too_much(user, adjusted(destination, users.own(name)), name):
                return denied
        moved = users.move_members(source, target)
        log.add("group", "members-moved", user.name, address(request), name=source, group=target, members=moved)
        return JSONResponse({"from": source, "group": target, "moved": moved})

    # ---- keys -------------------------------------------------------------------------------

    async def key_add(request: Request, user: User) -> Response:
        """Create an API key for a currently manageable account and return it once."""
        name = request.path_params["name"]
        if denied := may_manage(user, name):
            return denied
        asked = await read_json(request, {"label"})
        if isinstance(asked, Response):
            return asked
        if not users.exists(name):
            return error(404, f"No user named {name!r}.")
        label = str(asked.get("label", ""))
        log.add("user", "key-added", user.name, address(request), name=name, label=label)
        return JSONResponse({"key": users.new_key(name, label)}, status_code=201)

    async def key_remove(request: Request, user: User) -> Response:
        """Revoke the selected account key after ownership and permission checks."""
        name, key_id = request.path_params["name"], request.path_params["key_id"]
        if denied := may_manage(user, name):
            return denied
        if len(key_id) < 8 or not users.remove_key(name, key_id):
            return error(404, f"{name!r} has no key with id {key_id!r}.")
        log.add("user", "key-removed", user.name, address(request), name=name, key=key_id)
        return JSONResponse({"removed": key_id})

    def route(path: str, handler, method: str = "GET") -> Route:
        """Register the handler behind the service authentication wrapper."""
        return Route(path, signed_in_here(handler), methods=[method])

    return [
        Route("/loadcoach/sign-in", sign_in, methods=["POST"]),
        route("/loadcoach/sign-out", sign_out, "POST"),
        route("/loadcoach/me", me),
        route("/loadcoach/me", choose, "POST"),
        route("/loadcoach/users", user_list),
        route("/loadcoach/users", user_add, "POST"),
        route("/loadcoach/users/{name}", user_one),
        route("/loadcoach/users/{name}", user_change, "POST"),
        route("/loadcoach/users/{name}", user_remove, "DELETE"),
        route("/loadcoach/users/{name}/rename", user_rename, "POST"),
        route("/loadcoach/users/{name}/keys", key_add, "POST"),
        route("/loadcoach/users/{name}/keys/{key_id}", key_remove, "DELETE"),
        route("/loadcoach/groups", group_list),
        route("/loadcoach/groups/{name}/members", group_move_members, "POST"),
        Route("/loadcoach/groups/{name}", signed_in_here(group_put), methods=["POST", "PATCH"]),
        route("/loadcoach/groups/{name}", group_remove, "DELETE"),
    ]
