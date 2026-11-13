"""Groups: what the members of each may do, and what one account may do that differs from its group.

A group is a set of abilities and a few limits. Groups are kept in the same file as the users,
not in the configuration file, so that one can be added or changed while LoadCoach runs. The
groups in the configuration file are the first set: they are taken over once, into an empty
table, so that a new install does not start with none.

An account starts from its group. It can be given abilities on top, have abilities taken away,
and have three limits made tighter: the models it may use, how many requests it may have at
once, and how sensitive its work may be. It can also be shown more or fewer of the ToolLocker's
tools than its group. That is its `own` settings, a small dictionary:

    {"add": ["drain"], "remove": ["lock"], "models": ["small"], "max_requests": 2, "max_sensitivity": "normal",
     "add_tools": ["nas__*"], "remove_tools": ["code__diff"]}

Nothing here knows about keys, passwords or the web.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase

from db import migrate
from db.schemas import GROUPS

# What a group's `can` list may contain.
ABILITIES = {
    "front-plugins": "use external adapter models through turnstile",
    "front-manage-plugins": "manage plugin instances, model aliases, installers and health controls on front",
    "hold": "keep a model loaded",
    "lock": "take the lock",
    "break-lock": "end a lock someone else holds",
    "unload": "unload every model",
    "high": "ask for high priority",
    "load": "cause a model to be loaded, which may replace an idle one",
    "benchmark": "run benchmarks on models one may use",
    "rate-answers": "rate anyone's answered request as good or bad, with an explanation; meant for an app that judges answers",
    "hosted": "use a hosted model, which sends the request off this machine",
    "manage-hosted": "add, change and remove outside services and the models they offer: where requests may be sent off this machine",
    "unlogged": "send a request that leaves nothing behind: no entry in the log, no number, no kept text",
    "see-all": "see every caller in the status, not only oneself",
    "clear-line": "turn away every request waiting in line",
    "drain": "stop new requests being taken so that the line empties, and let them in again",
    "stop": "stop a request of someone else's, in line or being answered",
    "see-log": "read everyone's entries in the event log, not only one's own",
    "see-summaries": "select group and total Home summaries, including request metadata but not captured text",
    "see-capture": "read the text kept of sensitive requests",
    "manage-users": "add, change and remove users, within what one may do oneself",
    "manage-groups": "add, change and remove groups, within what one may do oneself",
    "manage-models": "add, change and remove models, and see, move and delete their files",
    "restart-services": "start, stop and restart services on a compute node, and read their output",
    "power": "restart a compute node, put it to sleep or switch it off",
    "manage-settings": "see every program's settings, and change those that can be changed from the site",
    "superuser": "do everything a person can be allowed to do, here and on the front machine, also what is added later",
    "follow-settings": "read what was changed in a program's settings and say what it runs with; meant for a program's own key",
    "sign-in": "have someone else's sign-in checked, for a session that can do all that person may; meant for weightroom's own key",
    "app-sign-in": "have someone else's sign-in checked here, for a session that can do no more than this account; meant for an app's own key",
    "mirror-users": "read the models for front's plugins and the evidence for alerts; meant for the front machine",
}
# Abilities that existed once. A group may still list one, so that an old file still loads. It does nothing.
RETIRED = {"act-for"}
# An ability starting with this is not LoadCoach's. It is passed to the front machine untouched.
FRONT_PREFIX = "front-"

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# How sensitive a piece of work is, least first. A request says its level; a group caps it.
LEVELS = ("normal", "sensitive", "restricted")

@dataclass(frozen=True)
class Group:
    """What the members of one group may do. Built from a [groups.NAME] section."""

    can: frozenset[str] = frozenset()
    models: tuple[str, ...] | None = None  # None means every model
    priority: str = "normal"  # the priority of a request that does not ask for one
    max_lock: float = 0  # the longest lock in seconds; 0 means no limit
    max_requests: int = 0  # requests in line or running at once, per user; 0 means no limit
    max_sensitivity: str = "normal"  # the highest level in LEVELS its users may ask for
    max_hosted_spend: float = 0  # what one user's hosted answers may cost in any 24 hours; 0 means no limit
    session_hours: float = 12  # how long a sign-in lasts before the password is asked for again
    second_step: bool = False  # True if signing in also needs a code from an authenticator app

    max_hold: float = 120  # longest model hold in seconds, renewed while in use
    # The ToolLocker's tools its members are shown, as names or patterns such as "nas__*". None means every tool.
    tools: tuple[str, ...] | None = None
    hidden_tools: tuple[str, ...] = ()  # taken away for one account, whatever `tools` says

    def may_use(self, model: str) -> bool:
        """Return whether the group's model allowlist permits this model."""
        return self.models is None or model in self.models

    def may_see_tool(self, name: str) -> bool:
        """Whether a tool, by the name a harness sees, is shown. Its level is checked as well, elsewhere."""
        shown = self.tools is None or any(fnmatchcase(name, pattern) for pattern in self.tools)
        return shown and not any(fnmatchcase(name, pattern) for pattern in self.hidden_tools)


# Abilities meant for a program's own key. With them a key can have any password tried, or read
# password hashes, which no person needs. They are never part of "everything".
OWN_KEY = frozenset({"sign-in", "app-sign-in", "mirror-users", "follow-settings"})
# Everything a person can be allowed to do.
EVERYTHING = Group(can=frozenset(ABILITIES) - OWN_KEY, max_sensitivity=LEVELS[-1])
NOTHING = Group(models=())  # what a group that no longer exists grants: no ability and no model


def read_group(name: str, section: dict) -> Group:
    """Check one [groups.NAME] section and turn it into a Group. Raises ValueError with the reason."""
    known = {"can", "models", "tools", "priority", "max_lock", "max_requests", "max_sensitivity", "max_hosted_spend", "session_hours", "second_step", "max_hold"}  # fmt: skip
    if extra := section.keys() - known:
        raise ValueError(f"group {name!r}: unknown setting {sorted(extra)}")
    can = [ability for ability in section.get("can", []) if ability not in RETIRED]
    if unknown := [ability for ability in can if ability not in ABILITIES and not ability.startswith(FRONT_PREFIX)]:
        raise ValueError(f"group {name!r}: unknown ability {unknown}. Known: {sorted(ABILITIES)}")
    # "superuser" stands for every ability there is. It is spelled out here, once, so that nothing
    # that asks "may this user do X" needs to know about it, and one added later is included.
    almighty = "superuser" in can
    if almighty:
        can = [*can, *EVERYTHING.can]
    allowed = section.get("models", "*")
    # The names are not checked against the models there are: a model can be added later.
    if allowed != "*" and (not isinstance(allowed, list) or not all(isinstance(model, str) for model in allowed)):
        raise ValueError(f'group {name!r}: models must be "*" or a list of model names')
    tools = section.get("tools", "*")  # nor are tools: a tool server can be added later
    if tools != "*" and (not isinstance(tools, list) or not all(isinstance(tool, str) and tool for tool in tools)):
        raise ValueError(f'group {name!r}: tools must be "*" or a list of tool names and patterns')
    priority = section.get("priority", "normal")
    if priority not in ("high", "normal", "low"):
        raise ValueError(f"group {name!r}: priority must be high, normal or low")
    max_lock, max_requests = section.get("max_lock", 0), section.get("max_requests", 0)
    max_hosted_spend = section.get("max_hosted_spend", 0)
    if type(max_requests) is not int or type(max_lock) not in (int, float) or type(max_hosted_spend) not in (int, float):
        raise ValueError(f"group {name!r}: max_lock, max_requests and max_hosted_spend must be numbers")
    if min(max_lock, max_requests, max_hosted_spend) < 0:
        raise ValueError(f"group {name!r}: max_lock, max_requests and max_hosted_spend must be 0 or more")
    max_sensitivity = section.get("max_sensitivity", LEVELS[-1] if almighty else "normal")  # everything includes the most sensitive work
    if max_sensitivity not in LEVELS:
        raise ValueError(f"group {name!r}: max_sensitivity must be one of {', '.join(LEVELS)}")
    session_hours, second_step = section.get("session_hours", 12), section.get("second_step", False)
    if isinstance(session_hours, bool) or not isinstance(session_hours, int | float) or session_hours <= 0:
        raise ValueError(f"group {name!r}: session_hours must be a number above 0")
    if not isinstance(second_step, bool):
        raise ValueError(f"group {name!r}: second_step must be true or false")
    max_hold = section.get("max_hold", 120)
    if type(max_hold) not in (int, float) or not 0 < max_hold <= 86400:
        raise ValueError("max_hold must be above zero and at most 86400 seconds")
    return Group(
        frozenset(can),
        None if allowed == "*" else tuple(allowed),
        priority,
        max_lock,
        max_requests,
        max_sensitivity,
        max_hosted_spend,
        session_hours,
        second_step,
        max_hold,
        None if tools == "*" else tuple(tools),
    )


def section_of(group: Group) -> dict:
    """A group as it would be written in a settings file. The reverse of read_group."""
    # "superuser" was spelled out when the group was read. Written back as the one word, it
    # goes on meaning everything, also what is added later.
    front = sorted(ability for ability in group.can if ability.startswith(FRONT_PREFIX) and ability not in ABILITIES)
    can = ["superuser", *front, *sorted(group.can & OWN_KEY)] if "superuser" in group.can else sorted(group.can)
    return {
        "can": can,
        "models": "*" if group.models is None else list(group.models),
        "tools": "*" if group.tools is None else list(group.tools),
        "priority": group.priority,
        "max_lock": group.max_lock,
        "max_hold": group.max_hold,
        "max_requests": group.max_requests,
        "max_sensitivity": group.max_sensitivity,
        "max_hosted_spend": group.max_hosted_spend,
        "session_hours": group.session_hours,
        "second_step": group.second_step,
    }


# ---- one account: what it has that its group has not, and the other way round ----------------


OWN_SETTINGS = ("add", "remove", "models", "max_requests", "max_sensitivity", "priority", "add_tools", "remove_tools")


def check_own(own: dict) -> None:
    """Raise ValueError, saying why, if these could not be an account's own settings."""
    if not isinstance(own, dict) or own.keys() - set(OWN_SETTINGS):
        raise ValueError(f"An account's own settings may be {', '.join(OWN_SETTINGS[:-1])} and {OWN_SETTINGS[-1]}.")
    if own.get("priority", "normal") not in ("high", "normal", "low"):
        raise ValueError("priority must be high, normal or low.")
    for part in ("add", "remove", "models", "add_tools", "remove_tools"):
        if not isinstance(own.get(part, []), list) or not all(isinstance(one, str) for one in own.get(part, [])):
            raise ValueError(f"{part} must be a list of names.")
    if unknown := [ability for ability in [*own.get("add", []), *own.get("remove", [])] if ability not in ABILITIES and not ability.startswith(FRONT_PREFIX)]:
        raise ValueError(f"Unknown ability {unknown}. Known: {sorted(ABILITIES)}")
    if type(own.get("max_requests", 1)) is not int or own.get("max_requests", 1) < 1:
        raise ValueError("max_requests must be a whole number, 1 or more.")
    if own.get("max_sensitivity", LEVELS[0]) not in LEVELS:
        raise ValueError(f"max_sensitivity must be one of {', '.join(LEVELS)}")


def adjusted(group: Group, own: dict) -> Group:
    """A group as it is for one account: with what the account was given, without what was taken away, as tight as the tighter of the two, and at its own priority."""
    if not own:
        return group
    can = set(group.can) | set(own.get("add", ()))
    if "superuser" in own.get("add", ()):
        can |= EVERYTHING.can
    if taken := set(own.get("remove", ())) & can:
        can -= taken | {"superuser"}  # with something taken away it is no longer everything, and must not be read as that
    models = group.models
    if "models" in own:  # the account's list, and of that only what its group allows
        models = tuple(model for model in own["models"] if group.may_use(model))
    # Being given everything includes the most sensitive work. Otherwise the group's level, or a lower one of the account's own.
    level = LEVELS[-1] if "superuser" in own.get("add", ()) else group.max_sensitivity
    level = min(level, own.get("max_sensitivity", level), key=LEVELS.index)
    most = min(filter(None, (group.max_requests, own.get("max_requests", 0))), default=0)  # 0 is no limit, so it is left out
    # Tools shown to it: its group's and those added for it, less those taken away. A group shown every tool has none to add.
    tools = None if group.tools is None else tuple(dict.fromkeys([*group.tools, *own.get("add_tools", ())]))
    hidden = tuple(dict.fromkeys([*group.hidden_tools, *own.get("remove_tools", ())]))
    # The priority of its requests that do not ask for one: the account's own if it has one, either way from its group's.
    return replace(group, can=frozenset(can), models=models, max_requests=most, max_sensitivity=level, priority=own.get("priority", group.priority),
                   tools=tools, hidden_tools=hidden)  # fmt: skip


def limited(person: Group, app: Group) -> Group:
    """What a session may do that was made through an app: only what both the person and the app's own account may."""
    both = None if person.models is None and app.models is None else tuple(
        model for model in (person.models if person.models is not None else app.models) if person.may_use(model) and app.may_use(model)
    )  # fmt: skip
    return replace(person, can=(person.can & app.can) - OWN_KEY, models=both, max_hold=min(person.max_hold, app.max_hold), max_sensitivity=min(person.max_sensitivity, app.max_sensitivity, key=LEVELS.index),
                   tools=fewer_tools(person.tools, app.tools), hidden_tools=tuple(dict.fromkeys([*person.hidden_tools, *app.hidden_tools])))  # fmt: skip


def covered(pattern: str, by: tuple[str, ...]) -> bool:
    """Whether every tool `pattern` stands for is among those `by` stands for, as near as patterns allow.

    A pattern counts as covered when it is itself one of the names the other list's patterns
    match, so "nas__*" is covered by "nas__*" and by "*", but not by "nas__read_file".
    """
    return any(fnmatchcase(pattern, other) for other in by)


def fewer_tools(mine: tuple[str, ...] | None, theirs: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """The tools in both lists, as near as patterns allow: those of `mine` that `theirs` covers, and the other way round."""
    if mine is None or theirs is None:
        return theirs if mine is None else mine
    return tuple(dict.fromkeys([*(one for one in mine if covered(one, theirs)), *(one for one in theirs if covered(one, mine))]))


def beyond(actor: Group, wanted: Group) -> str | None:
    """What `wanted` allows that `actor` may not do themselves, in words. None if nothing.

    This is the rule that nobody hands out what they do not hold. Someone who can do everything
    can hand out anything, the abilities meant for a program's own key included.
    """
    if "superuser" in actor.can:
        return None
    if more := sorted(wanted.can - actor.can):
        return f"the abilit{'y' if len(more) == 1 else 'ies'} {', '.join(more)}"
    if "hold" in wanted.can and wanted.max_hold > actor.max_hold:
        return "a longer model hold"
    if wanted.priority == "high" and actor.priority != "high" and "high" not in actor.can:
        return "high priority"
    if LEVELS.index(wanted.max_sensitivity) > LEVELS.index(actor.max_sensitivity):
        return f"work at the {wanted.max_sensitivity} level"
    if actor.models is not None and (wanted.models is None or set(wanted.models) - set(actor.models)):
        return "models you may not use yourself"
    # Tools: every one handed out must be among one's own, and none taken away from oneself may be shown to them.
    if actor.tools is not None and (wanted.tools is None or not all(covered(pattern, actor.tools) for pattern in wanted.tools)):
        return "tools you may not see yourself"
    if any(wanted.may_see_tool(pattern) for pattern in actor.hidden_tools):
        return "tools you may not see yourself"
    return None


# ---- the groups there are --------------------------------------------------------------------


class Groups:
    """The groups, kept in the database. A change is written at once and counts from the next request.

    It can be asked like a dictionary: `name in groups`, `groups.get(name)`, and going through it
    gives the names.
    """

    def __init__(self, db: sqlite3.Connection, first: dict[str, Group]) -> None:
        """Bind the groups table and seed validated initial groups when absent."""
        self.db = db
        self.read: dict[str, Group] = {}  # a group as it is stored -> the same group, checked and ready to ask
        self.taken_over = 0  # how many came from the configuration file just now. Above 0 once, at most
        migrate(db.database, db, "loadcoach.groups", GROUPS)
        with db:
            if first and db.execute("SELECT 1 FROM groups LIMIT 1").fetchone() is None:
                # Two programs starting at once may both get here. Each group is then kept once.
                rows = [(name, json.dumps(section_of(group))) for name, group in first.items()]
                self.taken_over = db.executemany("INSERT INTO groups (name, settings) VALUES (?, ?) ON CONFLICT(name) DO NOTHING", rows).rowcount

    def __iter__(self):
        """Iterate over the underlying records in their defined order."""
        return iter([name for (name,) in self.db.execute("SELECT name FROM groups ORDER BY name")])

    def __contains__(self, name: str) -> bool:
        """Return whether the named record exists."""
        return self.stored(name) is not None

    def stored(self, name: str) -> dict | None:
        """One group as it would be written in a settings file, or None if there is no such group."""
        row = self.db.execute("SELECT settings FROM groups WHERE name = ?", (name,)).fetchone()
        return row and json.loads(row[0])

    def get(self, name: str, default: Group | None = None) -> Group | None:
        """One group, ready to ask what it allows. It is read from the file each time, so a change made by another program is seen."""
        row = self.db.execute("SELECT settings FROM groups WHERE name = ?", (name,)).fetchone()
        if row is None:
            return default
        if row[0] not in self.read:  # checking takes longer than reading, so each form a group has had is checked once
            self.read[row[0]] = read_group(name, json.loads(row[0]))
        return self.read[row[0]]

    def put(self, name: str, section: dict) -> Group:
        """Add a group, or replace its settings. Raises ValueError, saying why, if they could not be a group's."""
        if not NAME.fullmatch(name):
            raise ValueError(f"{name!r}: a group name is lowercase letters, digits, dot, dash or underscore, at most 64.")
        group = read_group(name, section)
        with self.db:
            self.db.execute("INSERT INTO groups (name, settings) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET settings=excluded.settings", (name, json.dumps(section)))
        return group

    def remove(self, name: str) -> None:
        """Delete the named group and its associated spending allowance."""
        with self.db:
            if 'spending_allowances' in self.db.database.tables(self.db):
                self.db.execute("DELETE FROM spending_allowances WHERE scope='group' AND subject=?", (name,))
            self.db.execute("DELETE FROM groups WHERE name = ?", (name,))
