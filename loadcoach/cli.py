"""The `loadcoach` command.

    loadcoach                         run the server
    loadcoach models ...              look after models, straight in the database file
    loadcoach log                     read the event log, straight from the same file
    loadcoach log verify              check that the log has not been altered
    loadcoach status|unlock|clear     ask the LoadCoach running on this machine
    loadcoach drain WHY | drain off   take no new requests so that the line empties, and take them again
    loadcoach stop ID                 end one request, by the ID that `status` shows
    loadcoach peek ID                 see the last of what one request has been answered so far

The `models` and `log` commands read and write the file themselves. They need no key and work
whether or not LoadCoach is running.

The users and groups are kept on the control node since milestone 76. The `user` and `group`
commands below are the `weightroom` command's: it opens the control node's accounts and hands
them to these (see weightroom/app.py), along with `log` for what was done to them.

The line and the lock exist only inside a running LoadCoach, so `status`, `unlock` and `clear`
ask it over HTTP. They sign in with a token LoadCoach leaves in a file only its owner can read.

`loadcoach --help` lists every command, and each has a page of its own, such as
`loadcoach models add --help`. Those pages are drawn by Typer from the functions at the end of
this file.
"""

from __future__ import annotations

import csv
import getpass
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

import httpx
import typer
import uvicorn

from loadcoach.app import LOOPBACK, build_app
from loadcoach.capture import resolve
from loadcoach.config import CLI, Config, load_config
from loadcoach.settings import Changes, front_programs, program_name, store_retired, written
from loadcoach.log import Log
from loadcoach.models import Models, check_model, check_order
from loadcoach.statement import OVERLAP, replace_key
from loadcoach.users import Users


def table(rows: list[list[str]]) -> None:
    """Print rows in columns, each as wide as its longest entry. The first row is the heading."""
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


# ---- users: straight in the file ------------------------------------------------------------


def user_command(users: Users, action: str, arguments: list[str], log: Log | None = None, password: str | None = None) -> None:
    """Carry out one `weightroom user ...` command. Raises ValueError with a message on a mistake.

    These changes do not pass through the web side, so each one is written to the event log here.
    """

    def note(what: str, **detail) -> None:
        """Record an account-management command in the audit log when available."""
        if log:
            log.add("user", what, "command-line", **detail)

    expects = {
        "list": [], "add": ["NAME", "GROUP"], "group": ["NAME", "GROUP"], "enable": ["NAME"],
        "disable": ["NAME"], "remove": ["NAME"], "key": ["NAME", "LABEL"], "unkey": ["NAME", "ID"],
        "password": ["NAME"], "temporary": ["NAME"], "forget-code": ["NAME"], "import": ["FILE"], "show": ["NAME"],
        "adjust": ["NAME", "KEY=VALUE ..."],
    }  # fmt: skip
    if action not in expects or (len(arguments) != len(expects[action]) and not (action == "adjust" and len(arguments) > 1)):
        usage = "\n".join(f"  weightroom user {name} {' '.join(needs)}".rstrip() for name, needs in expects.items())
        raise ValueError(f"The user commands are:\n{usage}")

    if action == "list":
        rows = [["NAME", "GROUP", "ENABLED", "PASSWORD", "CODE", "SIGNED IN", "KEYS"]]
        for user in users.listing():
            keys = ", ".join(f"{key['label'] or '-'} ({key['id']})" for key in user["keys"])
            password = "temporary" if user["password"] == "temporary" else "set" if user["password"] else "-"
            rows.append([user["name"], user["group"], "yes" if user["enabled"] else "no", password,
                         "set up" if user["second_step"] else "-", str(user["sessions"] or "-"), keys or "-"])  # fmt: skip
        return table(rows)

    if action == "import":
        for name, group in import_users(users, Path(arguments[0])):
            note("added", name=name, group=group)
        return

    name = arguments[0]
    if action == "add":
        users.add(name, arguments[1])
        note("added", name=name, group=arguments[1])
        return print(f"Added {name} to {arguments[1]}. Their API key, shown only this once:\n{users.new_key(name, 'first')}")
    if not users.exists(name):
        raise ValueError(f"No user named {name!r}.")
    if action == "show":
        (told,) = users.listing(only=name)
        print(f"{name} is in {told['group']}" + ("" if told["enabled"] else ", and is disabled") + ".")
        print("Its own, on top of its group: " + (json.dumps(told["own"]) if told["own"] else "nothing"))
        print("So it may: " + (", ".join(told["can"]) or "nothing"))
    elif action == "adjust":
        # What the account has that differs from its group. Each word changes one part; an empty value takes that part away.
        own = {**users.own(name), **settings_from(arguments[1:], lists=("add", "remove", "models", "add_tools", "remove_tools"))}
        own = {key: value for key, value in own.items() if value not in (None, [])}
        users.change(name, own=own)
        note("changed", name=name, own=own)
        print(f"{name} now has of its own: {json.dumps(own) if own else 'nothing'}\nSo it may: {', '.join(users.listing(only=name)[0]['can']) or 'nothing'}")
    elif action == "group":
        users.change(name, group=arguments[1])
        note("changed", name=name, group=arguments[1])
        print(f"{name} is now in {arguments[1]}.")
    elif action in ("enable", "disable"):
        users.change(name, enabled=action == "enable")
        note("changed", name=name, enabled=action == "enable")
        print(f"{name} is {action}d.")
    elif action == "remove":
        users.remove(name)
        note("removed", name=name)
        print(f"Removed {name} and their keys.")
    elif action == "key":
        note("key-added", name=name, label=arguments[1])
        print(f"A new key for {name}, shown only this once:\n{users.new_key(name, arguments[1])}")
    elif action == "unkey":
        if len(arguments[1]) < 8 or not users.remove_key(name, arguments[1]):
            raise ValueError(f"{name} has no key with id {arguments[1]!r}. `weightroom user list` shows the ids.")
        note("key-removed", name=name, key=arguments[1])
        print(f"Removed that key of {name}.")
    elif action == "password":
        if password is None:
            password = getpass.getpass(f"New password for {name}: ")
            if len(password) < 8 or password != getpass.getpass("Again: "):
                raise ValueError("Not changed: the password must be at least 8 characters, typed the same twice.")
        elif len(password) < 8:  # given by a script, which has checked it was typed the same twice
            raise ValueError("Not changed: the password must be at least 8 characters.")
        users.change(name, password=password)
        users.end_sessions(name)
        note("changed", name=name, password=True)
        print(f"Password set for {name}.")
    elif action == "temporary":
        note("changed", name=name, temporary_password=True)
        print(f"A temporary password for {name}, shown only this once. At their next sign-in they must choose their own:\n"
              f"{users.temporary_password(name)}")  # fmt: skip
    elif action == "forget-code":
        users.change(name, second_step=False)
        note("changed", name=name, second_step=False)
        print(f"{name}'s authenticator is forgotten. If their group asks for a code, they set one up at their next sign-in.")


def import_users(users: Users, file: Path) -> list[list[str]]:
    """Add many users from a file of `name,group` lines. All of them, or none if any line is wrong.

    Prints `name,group,key` for each, which is the only time the keys can be seen. Returns who
    was added.
    """
    with file.open(newline="") as lines:
        wanted = [
            [cell.strip() for cell in row]
            for row in csv.reader(lines)
            if row and not row[0].startswith("#") and [cell.strip() for cell in row] != ["name", "group"]
        ]
    problems = []
    for number, row in enumerate(wanted, 1):
        try:
            if len(row) != 2:
                raise ValueError(f"expected name,group but got {','.join(row)!r}")
            users.check_new(*row)
            if [other[0] for other in wanted].count(row[0]) > 1:
                raise ValueError(f"{row[0]!r} appears more than once")
        except ValueError as problem:
            problems.append(f"  entry {number}: {problem}")
    if problems:
        raise ValueError("Nobody was added, because:\n" + "\n".join(problems))
    out = csv.writer(sys.stdout)
    out.writerow(["name", "group", "key"])
    for name, group in wanted:
        users.add(name, group)
        out.writerow([name, group, users.new_key(name, "first")])
    return wanted


# ---- groups: straight in the file ------------------------------------------------------------


def settings_from(words: list[str], lists: tuple[str, ...] = ()) -> dict:
    """KEY=VALUE words as a dictionary. A value is a number, true, false or a list if it reads as one.

    For a key named in `lists`, `a,b,c` is a list too. An empty value gives None, which is how a
    setting is taken away.
    """
    told: dict = {}
    for word in words:
        key, equals, value = word.partition("=")
        if not equals or not key:
            raise ValueError(f"{word!r} is not KEY=VALUE.")
        try:
            told[key] = None if value == "" else json.loads(value)
        except ValueError:
            told[key] = value.split(",") if key in lists and value != "*" else value
    return told


def group_toml(name: str, section: dict) -> str:
    """One group, written the way it would be in a configuration file."""
    return "\n".join([f"[groups.{name}]", *(f"{key} = {json.dumps(value)}" for key, value in section.items())]) + "\n"


def group_command(users: Users, action: str, arguments: list[str], log: Log | None = None) -> None:
    """Carry out one `weightroom group ...` command. Raises ValueError with a message on a mistake.

    A change made here counts from the next request, at LoadCoach and at every other program
    that reads the same file. Nothing is restarted.
    """
    groups = users.groups
    expects = {"list": "", "show": "NAME", "export": "", "add": "NAME [KEY=VALUE ...]", "set": "NAME KEY=VALUE [KEY=VALUE ...]", "remove": "NAME"}
    usage = "The group commands are:\n" + "\n".join(f"  weightroom group {name} {needs}".rstrip() for name, needs in expects.items())
    usage += "\nKEY is one of can, models, priority, max_lock, max_hold, max_requests, max_sensitivity, max_hosted_spend, session_hours, second_step."

    if action == "list" and not arguments:
        return table([["NAME", "MEMBERS", "MAY"]] + [[name, str(len(users.members(name))), ", ".join(groups.stored(name).get("can", [])) or "-"] for name in groups])
    if action == "export" and not arguments:
        return print("\n".join(group_toml(name, groups.stored(name)) for name in groups), end="")
    if action not in expects or not arguments or (action in ("show", "remove") and len(arguments) != 1) or (action == "set" and len(arguments) < 2):
        raise ValueError(usage)

    name, existing = arguments[0], groups.stored(arguments[0])
    told = settings_from(arguments[1:], lists=("can", "models", "tools"))
    if action == "add":
        if existing is not None:
            raise ValueError(f"There is already a group named {name!r}.")
        groups.put(name, {key: value for key, value in told.items() if value is not None})
        if log:
            log.add("group", "added", "command-line", name=name, **told)
        return print(f"Added the group {name}.")
    if existing is None:
        raise ValueError(f"There is no group named {name!r}. `weightroom group` lists them.")
    if action == "show":
        print(group_toml(name, existing), end="")
    elif action == "set":
        groups.put(name, {key: value for key, value in {**existing, **told}.items() if value is not None})
        if log:
            log.add("group", "changed", "command-line", name=name, **told)
        print(f"Changed the group {name}. It counts from its members' next request.")
    elif action == "remove":
        if inside := users.members(name):
            raise ValueError(f"{len(inside)} user{'s are' if len(inside) != 1 else ' is'} in {name}: {', '.join(inside[:5])}. Move them first: weightroom user group NAME GROUP")
        groups.remove(name)
        if log:
            log.add("group", "removed", "command-line", name=name)
        print(f"Removed the group {name}.")


# ---- models: straight in the file ------------------------------------------------------------


def as_toml(name: str, settings: dict, order: int | None = None) -> str:
    """One model's settings, written the way they would be in a configuration file."""
    lines = [f"[models.{json.dumps(name)}]"]
    if order is not None:
        lines.append(f"order = {order}")
    for key in ("engine", "gpus", "alone", "allow_split", "min_context"):
        if key in settings:
            lines.append(f"{key} = {json.dumps(settings[key])}")
    lines += ["", f"[models.{json.dumps(name)}.flags]"]
    lines += [f"{flag} = {json.dumps(value)}" for flag, value in settings["flags"].items()]
    for short, adapter in settings.get("adapters", {}).items():  # each adapter is a small table of its own
        lines += ["", f"[models.{json.dumps(name)}.adapters.{json.dumps(short)}]"] + [f"{key} = {json.dumps(value)}" for key, value in adapter.items()]
    return "\n".join(lines) + "\n"


def model_command(models: Models, config: Config, action: str, arguments: list[str], log: Log | None = None) -> None:
    """Carry out one `loadcoach models ...` command. Raises ValueError with a message on a mistake.

    A change made here is seen by a running LoadCoach at its next request. Nothing is restarted.
    """
    expects = {
        "list": "", "show": "NAME", "export": "", "add": "NAME FILE [FLAG=VALUE ...]",
        "set": "NAME FLAG=VALUE [FLAG=VALUE ...]", "remove": "NAME", "rename": "NAME NEW", "forget-measure": "NAME",
        "order": "NAME NUMBER|default",
    }  # fmt: skip
    usage = "The models commands are:\n" + "\n".join(f"  loadcoach models {name} {needs}".rstrip() for name, needs in expects.items())
    usage += "\nFLAG is one of llama-server's, without the dashes, or `engine`, `gpus`, `alone`, `allow_split`, `min_context`. An empty VALUE takes a flag away."
    usage += '\nAdapters are given whole, as JSON: adapters=\'{"terse": {"file": "/path/terse.gguf"}}\'. A request for NAME+terse gets that one alone.'

    def changed(name: str, settings: dict, what: str) -> None:
        """Check the settings, keep them, and write the change to the event log."""
        check_model(name, {**settings, "flags": {**config.model_defaults, **settings["flags"]}}, config.gpus)
        models.put(name, settings)
        if log:
            log.add("model", what, "command-line", model=name, **settings)

    def given(pairs: list[str], settings: dict) -> dict:
        """Apply FLAG=VALUE pairs. A value is read as a number, true, false or a list if it is one."""
        flags = dict(settings["flags"])
        settings = {**settings, "flags": flags}
        for pair in pairs:
            flag, equals, value = pair.partition("=")
            if not equals or not flag:
                raise ValueError(usage)
            if flag == "order":
                raise ValueError("Sort order is separate from inference flags: use `loadcoach models order NAME NUMBER` or `default`.")
            where = settings if flag in ("engine", "gpus", "alone", "allow_split", "adapters", "min_context") else flags
            if value == "":
                where.pop(flag, None)
                continue
            try:
                where[flag] = json.loads(value)
            except ValueError:
                where[flag] = value
        return settings

    if action == "list" and not arguments:
        rows = [["NAME", "ORDER", "CARDS", "CONTEXT", "SIZE", "MEASURED MiB", "FILE"]]
        for name in models.names():
            settings, measure = models.stored(name), models.measure(name)
            file = Path(settings["flags"]["model"])
            size = f"{file.stat().st_size / 2**30:.1f} GB" if file.is_file() else "MISSING"
            cards = str(settings.get("gpus", "all")) + (" alone" if settings.get("alone") else "")
            rows.append([name, str(models.place(name)), cards, str(settings["flags"].get("ctx-size", "-")), size, " + ".join(map(str, measure["mib"])) if measure else "-", str(file)])
        return table(rows)
    if action == "export" and not arguments:
        return print("\n".join(as_toml(name, models.stored(name), models.place(name)) for name in models.names()), end="")
    if action not in expects or not arguments or action in ("show", "remove", "forget-measure") and len(arguments) != 1:
        raise ValueError(usage)

    name, existing = arguments[0], models.stored(arguments[0])
    if action == "add":
        if existing or len(arguments) < 2:
            raise ValueError(f"There is already a model named {name!r}." if existing else usage)
        changed(name, given(arguments[2:], {"flags": {"model": str(Path(arguments[1]).absolute())}}), "added")
        return print(f"Added {name}. It can be asked for from now on.")
    if existing is None:
        raise ValueError(f"There is no model named {name!r}. `loadcoach models` lists them.")
    if action == "rename":
        if len(arguments) != 2:
            raise ValueError(usage)
        new = arguments[1]
        models.rename(name, new)
        if log:
            log.add("model", "renamed", "command-line", model=new, was=name, followed=[])
        # The groups and accounts that list it by name are on the control node. A rename on the site goes through turnstile, which has them follow.
        print(f"Renamed {name} to {new}. Callers must ask for it by the new name from now on. Groups and accounts on the control node"
              " machine that list it by its old name do not follow a rename made here: change them there.")
    elif action == "show":
        print(as_toml(name, existing, models.place(name)), end="")
    elif action == "order":
        if len(arguments) != 2:
            raise ValueError(usage)
        try:
            order = None if arguments[1] == "default" else int(arguments[1])
        except ValueError:
            raise ValueError("Sort order is a whole number or `default` (10).") from None
        check_order(order)
        models.set_place(name, order)
        if log:
            log.add("model", "ordered", "command-line", model=name, order=models.place(name))
        print(f"{name} has sort order {models.place(name)}. Its inference settings are unchanged.")
    elif action == "forget-measure":
        forgotten = models.forget_measure(name)
        print(f"Forgot what {name} measured. It has its cards to itself at its next load, and is measured afresh." if forgotten else f"{name} has no measure.")
    elif action == "set":
        changed(name, given(arguments[1:], existing), "changed")
        print(f"Changed {name}. If it is loaded, it is loaded afresh when it is next idle and asked for.")
    elif action == "remove":
        models.remove(name)
        if log:
            log.add("model", "removed", "command-line", model=name)
        print(f"Removed {name}. Requests already under way or in line for it are completed. Its file is left where it is.")


# ---- the event log: straight from the file --------------------------------------------------


def log_line(event: dict) -> str:
    """One entry on one line: when, what, who, then whatever else is known about it."""
    more = {
        key: value
        for key, value in event.items()
        if key not in ("id", "at", "kind", "what", "user", "text")
        and value not in (None, "", [])
        and (key, value) != ("level", "normal")
    }
    return (
        f"{event['at'].replace('T', ' ')}  {event['id']:<6} {event['kind']:9} {event['what']:12} {event['user'] or '-':14} "
        + "  ".join(f"{key}={value}" for key, value in more.items())
    ).rstrip()


def log_command(log: Log, lines: int, follow: bool, text: bool = False, as_json: bool = False, **only) -> None:
    """Print the latest entries, and with `follow`, keep printing new ones as they are made.

    With `text`, the text kept of sensitive requests is shown under each entry that has any.
    With `as_json`, the entries are printed as one JSON list, for another program to read.
    """
    events, last = resolve(log.read(limit=lines, with_text=text, **only)), 0
    if as_json:
        return print(json.dumps(events, indent=2))
    while True:
        for event in events:
            print(log_line(event), flush=True)
            if "text" in event:
                print("    " + json.dumps(event["text"], indent=2, ensure_ascii=False).replace("\n", "\n    "), flush=True)
            last = event["id"]
        if not follow:
            return
        time.sleep(1)
        events = log.read(after=last, limit=1000, with_text=text, **only) if last else log.read(limit=lines, with_text=text, **only)
        events = resolve(events)


def verify_command(log: Log) -> None:
    """Check that no entry in the log has been changed or removed from the middle."""
    checked, broken = log.verify()
    if broken is not None:
        raise ValueError(
            f"The log has been altered. Entry {broken} does not follow from the one before it. "
            f"The {checked} entries before it are intact."
        )
    print(f"The log is intact: {checked} entries each follow from the one before.")


# ---- the line and the lock: ask the running LoadCoach ---------------------------------------


def running(config: Config) -> httpx.Client:
    """A connection to the LoadCoach running on this machine, signed in as the command line."""
    try:
        token = Path(config.token_file).read_text()
    except OSError:
        raise ValueError(f"LoadCoach does not seem to be running: there is no {config.token_file}") from None
    host = "127.0.0.1" if config.host in LOOPBACK | {"0.0.0.0", "::"} else config.host
    return httpx.Client(
        base_url=f"http://{host}:{config.port}", headers={"authorization": f"Bearer {token}"}, timeout=10
    )


def ask(server: httpx.Client, method: str, path: str, body: dict | None = None) -> dict:
    """One request to the running LoadCoach. Raises ValueError with its message if it says no."""
    try:
        answer = server.request(method, path, json=body)
    except httpx.HTTPError as problem:
        raise ValueError(f"LoadCoach did not answer: {type(problem).__name__}") from None
    if not answer.is_success:  # 200, or 202 for something that was started and runs on
        raise ValueError(f"LoadCoach said {answer.status_code}: {answer.json()['error']['message']}")
    return answer.json()


def show_status(status: dict) -> None:
    """Print what is loaded, who holds the lock, and the line."""
    print("LOADED")
    table([["  MODEL", "ON", "STATE"]] + [
        ["  " + model["model"], "+".join(model["gpus"]) or "CPU", model["state"]] for model in status["loaded"]
    ] if status["loaded"] else [["  nothing"]])  # fmt: skip
    if status.get("draining"):
        print(f"\nDRAINING since {datetime.fromtimestamp(status['draining']['since']):%H:%M}, asked by {status['draining']['by']}: {status['draining']['why']}")
        print("  New requests are turned away. End it with: loadcoach drain off")
    if status.get("holds"):
        print("\nMODEL HOLDS")
        table([["  MODEL", "USER", "LEFT"]] + [
            ["  " + hold["model"], hold["user"], f"{hold['seconds_left']:g} s"] for hold in status["holds"]])
    lock = status["lock"]
    print("\nLOCK")
    print(f"  held by {lock['user']} from {lock['caller']}, {lock['seconds_left']:g} s left"
          + (", turning others away" if lock["refuse"] else "") if lock else "  none")  # fmt: skip
    for title in ("running", "waiting", "hosted"):
        if title == "hosted" and not status.get("hosted"):
            continue  # shown only while a request is being answered off this machine
        print(f"\n{title.upper()}")
        rows = [["  " + str(entry.get("id") or "-"), entry["model"] or ("lock request" if entry["priority"] == "lock" else "unload"),
                 entry["user"], entry["caller"], entry["priority"], f"{entry['seconds']:g} s"]
                for entry in status[title]]  # fmt: skip
        table([["  ID", "WHAT", "USER", "FROM", "PRIORITY", "FOR"]] + rows if rows else [["  nobody"]])


def gigabytes(count: int | None) -> str:
    """Format an optional byte count in gigabytes for CLI output."""
    return "" if count is None else f"{count / 2**30:.1f} GB"


def server_command(server: httpx.Client, action: str, entry: str = "", *words: str, place: str = "") -> None:
    """Carry out one command against a running LoadCoach."""
    if action == "drain" and entry == "off":
        ask(server, "POST", "/loadcoach/drain", {"on": False})
        print("Requests are taken again.")
    elif action == "drain":  # everything after the word is the reason, as it will be told to callers
        status = ask(server, "POST", "/loadcoach/drain", {"why": " ".join([entry, *words])})
        print(f"No new requests are taken. {status['busy']} already here will be finished. End it with: loadcoach drain off")
    elif action == "stop":
        ask(server, "POST", f"/loadcoach/stop/{entry}")
        print(f"Stopped request {entry}. The others carry on.")
    elif action == "peek":
        seen = ask(server, "GET", f"/loadcoach/peek/{entry}")
        print(f"Request {seen['id']} of {seen['user']} on {seen['model']}: {seen['state']}.")
        for part, text in seen["said"].items():  # the end of each part is where a loop shows
            if isinstance(text, str) and text:
                print(f"\n{part.upper()}, the last {min(len(text), 2000)} of {len(text)} characters kept:\n{text[-2000:]}")
    elif action == "status":
        show_status(ask(server, "GET", "/loadcoach/status"))
    elif action == "unlock":
        had_lock = ask(server, "GET", "/loadcoach/status")["lock"]
        ask(server, "POST", "/loadcoach/unlock")  # no token of its own: it breaks whatever lock there is
        print(f"Broke the lock held by {had_lock['user']}." if had_lock else "There was no lock.")
    elif action == "clear":
        removed = ask(server, "POST", "/loadcoach/clear")["removed"]
        print(f"Turned away {removed} waiting request{'s' * (removed != 1)}. Answers under way carry on.")
    elif action == "search":
        found = ask(server, "GET", "/loadcoach/hub?search=" + quote(" ".join([entry, *words])))["found"]
        table([["REPOSITORY", "DOWNLOADS", "LIKES", "MADE"], *([one["repository"], str(one["downloads"]), str(one["likes"]), one["made"]] for one in found)])
    elif action == "download" and not words:  # only a repository: what it holds
        seen = ask(server, "GET", "/loadcoach/hub?repository=" + quote(entry))
        table([["FILE", "SIZE", "PARTS", "HERE"], *([row["file"], gigabytes(row["size"]), str(row["parts"]), row["here"] or ""] for row in seen["files"])])
        print("\nPlaces: " + ", ".join(f"{one['name']} ({gigabytes(one['free'])} free)" for one in seen["places"]))
    elif action == "download":
        started = ask(server, "POST", "/loadcoach/downloads", {"repository": entry, "files": list(words), **({"place": place} if place else {})})
        print(f"Bringing {len(started['downloading'])} file{'s' * (len(started['downloading']) != 1)}, {gigabytes(started['size'])}, down to {started['to']}. See how far with: loadcoach downloads")
    elif action == "downloads" and entry:
        if entry not in ("stop", "resume", "discard") or len(words) != 1:
            raise ValueError("Use `loadcoach downloads`, or `loadcoach downloads stop|resume|discard PATH`.")
        path = words[0]
        if entry == "resume":
            listed = ask(server, "GET", "/loadcoach/downloads")["downloads"]
            match = next((one for one in listed if one["path"] == path), None)
            if match is None:
                raise ValueError(f"No download has path {path!r}.")
            ask(server, "POST", "/loadcoach/downloads", {
                "repository": match["repository"], "files": [match["file"]], "place": match["place"]})
            print(f"Continuing {path}.")
        else:
            ask(server, "POST", f"/loadcoach/downloads/{entry}", {"path": path})
            print(f"{'Stopped' if entry == 'stop' else 'Discarded'} {path}.")
    elif action == "downloads":
        listed = ask(server, "GET", "/loadcoach/downloads")["downloads"]
        rows = [[one["path"], one["repository"], one["place"], one["state"], gigabytes(one["done"]), gigabytes(one["of"]), one.get("why", "")] for one in listed]
        table([["PATH", "REPOSITORY", "PLACE", "STATE", "HERE", "OF", "WHY"], *rows])
    elif action == "fingerprints":
        found = ask(server, "GET", "/loadcoach/fingerprints" + ("?start=" + quote(entry) if entry else ""))["fingerprints"]
        if not found:
            print(f"No fingerprint begins {entry!r}." if entry else "No fingerprint has been recorded yet.")
        elif len(found) == 1:
            print(json.dumps(found[0], indent=2, sort_keys=True))
        else:
            rows = [[one["fingerprint"], one["first"], one.get("model") or "-",
                     (one.get("files", {}).get("model") or {}).get("path", "-")] for one in found]
            table([["FINGERPRINT", "FIRST SEEN", "MODEL", "FILE"], *rows])


# ---- the command itself ---------------------------------------------------------------------
#
# Typer turns each function below into a command. A function's arguments are the command's
# arguments, and its description is what `--help` shows. The work itself is done by the
# functions above, which know nothing of Typer.

IN_THE_FILE, ASKED = "In the file: these work whether or not LoadCoach is running", "Asked of the LoadCoach running on this machine"
# `metavar` is what an argument is called in a help page.
Name = Annotated[str, typer.Argument(metavar="NAME", help="The user's name.")]
Group = Annotated[str, typer.Argument(metavar="GROUP", help="A group from the settings file.")]
Model = Annotated[str, typer.Argument(metavar="NAME", help="The model's name, as callers ask for it.")]
Request = Annotated[str, typer.Argument(metavar="ID", help="The request's number, as `loadcoach status` shows it.")]

app = typer.Typer(**CLI)
users_cli = typer.Typer(**CLI, no_args_is_help=True)
groups_cli = typer.Typer(**CLI)
models_cli = typer.Typer(**CLI)
loadouts_cli = typer.Typer(**CLI, no_args_is_help=True)
log_cli = typer.Typer(**CLI)
app.add_typer(models_cli, name="models", rich_help_panel=IN_THE_FILE, help="The models there are, and their settings. Alone, it lists them.")
app.add_typer(loadouts_cli, name="loadouts", rich_help_panel=ASKED, help="Save, review and request named model sets with renewable ten-minute holds.")
app.add_typer(log_cli, name="log", rich_help_panel=IN_THE_FILE, help="Read the event log. `log verify` checks that it has not been altered.")


def opened(ctx: typer.Context) -> tuple[Config | None, Users | None, Log]:
    """The settings, the accounts and the log a command works on.

    For `loadcoach`: its settings file and its event log. It has no accounts. For `weightroom`,
    which reuses the user, group and log commands here: the control node's accounts and their log.
    """
    if ctx.find_root().obj:
        return ctx.find_root().obj()  # opened only when a command runs, so that its help needs no settings
    config = load_config(Path(ctx.find_root().params["config"]))
    return config, None, Log(config.database, config.keep_days)


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option(metavar="FILE", help="The settings file.")] = Path("settings/loadcoach.toml"),
    replace_key: Annotated[bool, typer.Option("--replace-key", help="Set the machine key this machine shares with the control node, typed or piped in, and stop.")] = False,
    emergency: Annotated[bool, typer.Option("--emergency", help="Run on 127.0.0.1 only, for anyone on this machine, keeping no log: for when the control node is down.")] = False,
) -> None:
    # Each paragraph is one line here, because Typer keeps the line breaks it is given.
    """LoadCoach: one OpenAI-compatible door in front of local model servers.

    With no command it runs the server, which is what its service does.

    The commands are of two kinds. Models and the log are kept in a file, and those commands read and write it themselves: they need no key and work when LoadCoach is stopped. The line and the lock exist only inside a running LoadCoach, so those commands ask it, with a token it leaves for this machine. The users and groups are kept on the control node: see `weightroom user --help` there.

    --replace-key sets the key turnstile signs with, for when this machine and the control node have fallen out of step. The control node's Compute nodes page replaces it on both at once.

    --emergency runs LoadCoach for when the control node is down: on 127.0.0.1 only, with no key asked of anyone on this machine, and no log. Stop the service first. Reach it from elsewhere through ssh: ssh -L 8442:127.0.0.1:8442 compute

    Every command has a page of its own: loadcoach models add --help
    """
    if replace_key:
        return key_replaced(config)
    if ctx.invoked_subcommand is None:
        serve(config, emergency)


def key_replaced(file: Path) -> None:
    """Set the machine key by hand. The programs here read it on every request, so it counts at once; the one before counts for a while."""
    path = load_config(file).machine_key_file
    if not path:
        raise ValueError(f"{file} names no machine_key_file.")
    new = (getpass.getpass("The new machine key, as it is on the control node: ") if sys.stdin.isatty() else sys.stdin.read()).strip()
    replace_key(path, new)
    print(f"Replaced the key in {path}. The one before counts for {OVERLAP // 60} more minutes. Turnstile on the control node must have the same key.")


def serve(file: Path, emergency: bool = False) -> None:
    """Run the server, unless the settings would leave it open to anyone or unable to do what they say.

    In an emergency, with the control node down: on 127.0.0.1 only, where anyone on this machine
    may do anything, with no machine key and no log. The service must be stopped first, since two
    LoadCoaches would each think the cards their own.
    """
    config = load_config(file)
    if emergency:
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", "loadcoach.service"]).returncode == 0:
            raise SystemExit("loadcoach.service is running. Stop it first: systemctl --user stop loadcoach")
        config = replace(config, host="127.0.0.1", machine_key_file="", front="")
        print(f"loadcoach: EMERGENCY. http://127.0.0.1:{config.port} answers anyone on this machine, and keeps no log. Stop it with ctrl-C.", flush=True)
        return uvicorn.run(build_app(config, emergency=True), host=config.host, port=config.port)
    if config.host not in LOOPBACK and not config.machine_key_file:
        raise SystemExit(f"host {config.host} is reachable from other machines, so set machine_key_file in {file}: then only what turnstile signs is answered. See docs/installation.md.")
    uvicorn.run(build_app(config), host=config.host, port=config.port)


def for_user(ctx: typer.Context, action: str, *arguments: str) -> None:
    """Dispatch the user-management subcommand against the configured accounts store."""
    _, users, log = opened(ctx)
    user_command(users, action, list(arguments), log)


@users_cli.command("list")
def user_list(ctx: typer.Context) -> None:
    """Everyone: their group, whether they have a password, and their keys by label and id."""
    for_user(ctx, "list")


@users_cli.command("add")
def user_add(ctx: typer.Context, name: Name, group: Group) -> None:
    """Add a user, and print their first API key. It is shown only this once."""
    for_user(ctx, "add", name, group)


@users_cli.command("group")
def user_group(ctx: typer.Context, name: Name, group: Group) -> None:
    """Move a user to another group. It counts from their next request."""
    for_user(ctx, "group", name, group)


@users_cli.command("enable")
def user_enable(ctx: typer.Context, name: Name) -> None:
    """Let a disabled user in again."""
    for_user(ctx, "enable", name)


@users_cli.command("disable")
def user_disable(ctx: typer.Context, name: Name) -> None:
    """Turn a user away from now on, keeping their keys for when they are enabled again."""
    for_user(ctx, "disable", name)


@users_cli.command("remove")
def user_remove(ctx: typer.Context, name: Name) -> None:
    """Remove a user and all their keys."""
    for_user(ctx, "remove", name)


@users_cli.command("key")
def user_key(ctx: typer.Context, name: Name, label: Annotated[str, typer.Argument(metavar="LABEL", help="What the key is for, such as laptop.")]) -> None:
    """Make another API key for a user, and print it. It is shown only this once."""
    for_user(ctx, "key", name, label)


@users_cli.command("unkey")
def user_unkey(ctx: typer.Context, name: Name, key: Annotated[str, typer.Argument(metavar="KEY", help="The key's id, as `weightroom user list` shows it in brackets.")]) -> None:
    """Remove one of a user's keys. Their others go on working."""
    for_user(ctx, "unkey", name, key)


@users_cli.command("password")
def user_password(
    ctx: typer.Context, name: Name,
    stdin: Annotated[bool, typer.Option("--stdin", help="Read it from the first line of standard input instead, for an installer.")] = False,
) -> None:
    """Set a user's password, typed here twice. It ends their sign-ins."""
    _, users, log = opened(ctx)
    user_command(users, "password", [name], log, password=sys.stdin.readline().rstrip("\n") if stdin else None)


@users_cli.command("temporary")
def user_temporary(ctx: typer.Context, name: Name) -> None:
    """Print a temporary password for a user. At their next sign-in they must choose their own."""
    for_user(ctx, "temporary", name)


@users_cli.command("forget-code")
def user_forget_code(ctx: typer.Context, name: Name) -> None:
    """Forget a user's authenticator app, for when they have lost it. They set one up again at their next sign-in."""
    for_user(ctx, "forget-code", name)


@users_cli.command("show")
def user_show(ctx: typer.Context, name: Name) -> None:
    """One user: their group, what the account has of its own, and so everything it may do."""
    for_user(ctx, "show", name)


@users_cli.command("adjust")
def user_adjust(
    ctx: typer.Context,
    name: Name,
    settings: Annotated[list[str], typer.Argument(metavar="KEY=VALUE...", help="KEY is add, remove, models, max_requests, max_sensitivity, priority, add_tools or remove_tools. A list is written a,b,c. An empty VALUE takes that part away.")],
) -> None:
    """Give one account something its group has not, take something away, or make a limit tighter.

    weightroom user adjust chat-app remove=lock models=small max_requests=2

    weightroom user adjust chat-app add=app-sign-in

    weightroom user adjust pat add_tools=nas__* remove_tools=code__diff

    weightroom user adjust chat-app remove=
    """
    for_user(ctx, "adjust", name, *settings)


GroupName = Annotated[str, typer.Argument(metavar="NAME", help="The group's name.")]
GroupSettings = Annotated[list[str], typer.Argument(metavar="KEY=VALUE...", help="KEY is can, models, tools, priority, max_lock, max_hold, max_requests, max_sensitivity, max_hosted_spend, session_hours or second_step. A list is written a,b,c. An empty VALUE takes the setting away.")]


def for_group(ctx: typer.Context, action: str, *arguments: str) -> None:
    """Dispatch the group-management subcommand against the configured accounts store."""
    _, users, log = opened(ctx)
    group_command(users, action, list(arguments), log)


@groups_cli.callback(invoke_without_command=True)
def groups_alone(ctx: typer.Context) -> None:
    """Groups, and what their members may do. A change counts from the next request: nothing is restarted.

    The groups in the settings file are only the first set, taken over once. From then on they are kept with the users, and changed here or on the site. Unlike the site, these commands are held to no rules about who may raise whom: whoever can run them owns the file.
    """
    if ctx.invoked_subcommand is None:
        for_group(ctx, "list")


@groups_cli.command("list")
def group_list(ctx: typer.Context) -> None:
    """Every group: how many are in it, and what it allows."""
    for_group(ctx, "list")


@groups_cli.command("show")
def group_show(ctx: typer.Context, name: GroupName) -> None:
    """One group's settings, written as they would be in a settings file."""
    for_group(ctx, "show", name)


@groups_cli.command("export")
def group_export(ctx: typer.Context) -> None:
    """Every group's settings, in the same form. A way to keep a copy."""
    for_group(ctx, "export")


@groups_cli.command("add")
def group_add(ctx: typer.Context, name: GroupName, settings: GroupSettings = None) -> None:
    """Add a group: weightroom group add reader can=load max_requests=2"""
    for_group(ctx, "add", name, *(settings or []))


@groups_cli.command("set")
def group_set(ctx: typer.Context, name: GroupName, settings: GroupSettings) -> None:
    """Change a group's settings: weightroom group set user can=load,lock,hosted"""
    for_group(ctx, "set", name, *settings)


@groups_cli.command("remove")
def group_remove(ctx: typer.Context, name: GroupName) -> None:
    """Remove a group that nobody is in."""
    for_group(ctx, "remove", name)


@users_cli.command("import")
def user_import(ctx: typer.Context, file: Annotated[Path, typer.Argument(metavar="FILE", help="Lines of name,group.")]) -> None:
    """Add many users from a file, all of them or none, and print each one's key."""
    for_user(ctx, "import", str(file))


Pairs = Annotated[list[str], typer.Argument(metavar="FLAG=VALUE...", help="Each is FLAG=VALUE: a llama-server flag without its dashes, or `gpus`. An empty VALUE takes the flag away.")]


def for_model(ctx: typer.Context, action: str, *arguments: str) -> None:
    """Dispatch model-management operations against the configured model catalogue."""
    config, _, log = opened(ctx)
    model_command(Models(config.database, config.model_defaults, config.models), config, action, list(arguments), log)


@models_cli.callback(invoke_without_command=True)
def models_alone(ctx: typer.Context) -> None:
    """The models there are, and their settings. A change is seen by a running LoadCoach at its next request: nothing is restarted."""
    if ctx.invoked_subcommand is None:
        for_model(ctx, "list")


@models_cli.command("list")
def models_list(ctx: typer.Context) -> None:
    """Every model: its cards, its context size, and its file with its size."""
    for_model(ctx, "list")


@models_cli.command("show")
def models_show(ctx: typer.Context, name: Model) -> None:
    """One model's settings, written as they would be in a settings file."""
    for_model(ctx, "show", name)


@models_cli.command("export")
def models_export(ctx: typer.Context) -> None:
    """Every model's settings, in the same form. A way to keep a copy."""
    for_model(ctx, "export")


@models_cli.command("add")
def models_add(ctx: typer.Context, name: Model, file: Annotated[Path, typer.Argument(metavar="FILE", help="The model's .gguf file.")], settings: Pairs = None) -> None:
    """Add a model. It needs ctx-size among its settings, and can be asked for at once."""
    for_model(ctx, "add", name, str(file), *(settings or []))


@models_cli.command("set")
def models_set(ctx: typer.Context, name: Model, settings: Pairs) -> None:
    """Change a model's settings. If it is loaded, it is loaded afresh when next idle and asked for."""
    for_model(ctx, "set", name, *settings)


@models_cli.command("order")
def models_order(ctx: typer.Context, name: Model, number: Annotated[str, typer.Argument(metavar="NUMBER|default", help="Lowest first; default resets to 10. For a negative number, put -- before NAME.")]) -> None:
    """Change list order, preserving model identity, fingerprints and saved evidence."""
    for_model(ctx, "order", name, number)


@models_cli.command("rename")
def models_rename(ctx: typer.Context, name: Model, new: Annotated[str, typer.Argument(metavar="NEW", help="The name callers are to ask for from now on.")]) -> None:
    """Give a model another name. Its settings and its file stay; groups and accounts that list it follow."""
    for_model(ctx, "rename", name, new)


@models_cli.command("forget-measure")
def models_forget_measure(ctx: typer.Context, name: Model) -> None:
    """Throw away what a model measured, so that it is measured afresh at its next load."""
    for_model(ctx, "forget-measure", name)


@models_cli.command("remove")
def models_remove(ctx: typer.Context, name: Model) -> None:
    """Stop offering a model. Requests already here for it are finished. Its file is left where it is."""
    for_model(ctx, "remove", name)


@log_cli.callback(invoke_without_command=True)
def log_alone(
    ctx: typer.Context,
    lines: Annotated[int, typer.Option("-n", "--lines", help="How many of the latest entries to show.")] = 20,
    follow: Annotated[bool, typer.Option("-f", "--follow", help="Keep showing new entries as they are made.")] = False,
    user: Annotated[Optional[str], typer.Option(help="Only this user's entries.")] = None,
    kind: Annotated[Optional[str], typer.Option(help="Only this kind: request, tool, model, file, lock, line, user, refused, service, power, loadcoach, toollocker (legacy: tooldoor), spotter.")] = None,
    trace: Annotated[Optional[str], typer.Option(help="Only this piece of work.")] = None,
    text: Annotated[bool, typer.Option("--text", help="Also show the text kept of sensitive requests.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the entries as JSON, for another program.")] = False,
) -> None:
    """Read the event log: who did what, newest last."""
    if ctx.invoked_subcommand is None:
        try:
            log_command(opened(ctx)[2], lines, follow, text, as_json, user=user, kind=kind, trace=trace)
        except KeyboardInterrupt:  # how following is ended
            pass


@log_cli.command("verify")
def log_verify(ctx: typer.Context) -> None:
    """Check that no entry has been changed or taken out of the middle."""
    verify_command(opened(ctx)[2])


def settings_command(changes: Changes, action: str = "", program: str = "", name: str = "", excluded=()) -> None:
    """List what was changed in any program's settings from the site, or with `reset` take one change away."""
    if action == "reset" and program_name(program) in excluded:
        raise ValueError("The control node keeps this program's settings; use the Settings page on the control node.")
    if action == "reset":
        if not changes.take_away(program, name):
            raise ValueError(f"Nothing was changed for {name!r} of {program!r}. See what was with: loadcoach settings")
        print(f"{program}: {name} is as its file says again. LoadCoach takes that on at its next start, the other programs within the minute.")
    elif action:
        raise ValueError("loadcoach settings lists what was changed. loadcoach settings reset PROGRAM NAME takes one change away.")
    else:
        current = [one for one in changes.listing() if one[0] not in excluded]
        for program, name, value, by, at in current:
            print(f"{program:14} {name} = {written(value)}   changed by {by or 'someone at this machine'}, {at.replace('T', ' ')}")
        if not current:
            print("No local settings have been changed from the site." if excluded else
                  "Nothing was changed from the site. Every program runs with what its file says.")
        if excluded:
            print("Control node program settings are managed on the control node.")


@app.command(rich_help_panel=IN_THE_FILE)
def settings(
    ctx: typer.Context,
    action: Annotated[str, typer.Argument(help="`reset` takes a change away. Left out, the changes are listed.")] = "",
    program: Annotated[str, typer.Argument(help="The program whose setting it is, such as loadcoach or spotter.")] = "",
    name: Annotated[str, typer.Argument(help="The setting.")] = "",
) -> None:
    """What was changed in any program's settings from the site. `settings reset PROGRAM NAME` takes one change away, so that the file's value holds again."""
    config = opened(ctx)[0]
    settings_command(Changes(config.database), action, program, name,
                     excluded=front_programs(config, store_retired(config.database)))


def for_server(ctx: typer.Context, action: str, *arguments: str) -> None:
    """Dispatch server-management operations through the running local service."""
    with running(opened(ctx)[0]) as server:
        server_command(server, action, *arguments)


def for_loadout(ctx: typer.Context, action: str, **body) -> None:
    """Dispatch load-out operations through the running local service."""
    with running(opened(ctx)[0]) as server:
        server.timeout = httpx.Timeout(None, connect=5)
        result = ask(server, 'GET' if action == 'list' else 'POST', '/loadcoach/loadouts',
                     None if action == 'list' else {'action': action, **body})
        print(json.dumps(result, indent=2, ensure_ascii=False))


@loadouts_cli.command('list')
def loadouts_list(ctx: typer.Context) -> None:
    """Saved selections and your active set leases. Loads nothing."""
    for_loadout(ctx, 'list')


@loadouts_cli.command('save')
def loadouts_save(ctx: typer.Context, name: str, file: Path, version: Annotated[Optional[int], typer.Option(help='Current version, required when replacing a saved selection.')] = None) -> None:
    """Save NAME from 1–16 {model,cards,context} or fixed {model,cards,profile} entries.

    Get a media profile key from /loadcoach/models loadout_profile. Empty cards select CPU.
    This CLI targets its configured local worker; control node API clients can use the node-scoped route.
    """
    if file.stat().st_size > 65536:
        raise ValueError('Selection JSON may be at most 64 KiB.')
    selected = json.loads(file.read_text())
    for_loadout(ctx, 'save', name=name, models=selected, version=version)


@loadouts_cli.command('preview')
def loadouts_preview(ctx: typer.Context, name: str, undo: bool = False) -> None:
    """Review all configurations and joint fit; copy the token for apply/request. Loads nothing."""
    for_loadout(ctx, 'preview', name=name, undo=undo)


@loadouts_cli.command('apply')
def loadouts_apply(ctx: typer.Context, name: str, token: str, undo: bool = False, acknowledge: bool = False) -> None:
    """Apply the reviewed complete configuration, or its reviewed undo. Starts no inference."""
    for_loadout(ctx, 'apply', name=name, token=token, undo=undo, acknowledge=acknowledge)


@loadouts_cli.command('request')
def loadouts_request(ctx: typer.Context, name: str, token: str, acknowledge: bool = False) -> None:
    """Apply and load the reviewed set; return only when all members are ready and held."""
    for_loadout(ctx, 'request', name=name, token=token, acknowledge=acknowledge)


@loadouts_cli.command('renew')
def loadouts_renew(ctx: typer.Context, lease: str) -> None:
    """Give your unchanged ready set another ten minutes."""
    for_loadout(ctx, 'renew', lease=lease)


@loadouts_cli.command('release')
def loadouts_release(ctx: typer.Context, lease: str) -> None:
    """Release your set lease. Ordinary holds and active requests continue."""
    for_loadout(ctx, 'release', lease=lease)


@loadouts_cli.command('delete')
def loadouts_delete(ctx: typer.Context, name: str, version: int) -> None:
    """Delete the saved version; existing runtime leases and model settings stay in force."""
    for_loadout(ctx, 'delete', name=name, version=version)


@app.command(rich_help_panel=ASKED)
def status(ctx: typer.Context) -> None:
    """What is loaded and where, who holds the lock, and who is being answered or waiting."""
    for_server(ctx, "status")


@app.command(rich_help_panel=ASKED)
def drain(ctx: typer.Context, why: Annotated[list[str], typer.Argument(metavar="WHY...", help="A few words, told to callers who are turned away. Or the one word `off`.")]) -> None:
    """Take no new requests, so that the line empties. Those already here are finished. `drain off` takes them again."""
    for_server(ctx, "drain", *why)


@app.command(rich_help_panel=ASKED)
def unlock(ctx: typer.Context) -> None:
    """Break whatever lock is held, so that the line moves again."""
    for_server(ctx, "unlock")


@app.command(rich_help_panel=ASKED)
def clear(ctx: typer.Context) -> None:
    """Turn away everyone waiting in line. Answers under way carry on."""
    for_server(ctx, "clear")


@app.command(rich_help_panel=ASKED)
def stop(ctx: typer.Context, request: Request) -> None:
    """End one request, in line or being answered. The others carry on."""
    for_server(ctx, "stop", request)


@app.command(rich_help_panel=ASKED)
def peek(ctx: typer.Context, request: Request) -> None:
    """See the last of what one request has been answered so far. A loop shows there."""
    for_server(ctx, "peek", request)


@app.command(rich_help_panel=ASKED)
def search(ctx: typer.Context, words: Annotated[list[str], typer.Argument(metavar="WORDS...", help="What to look for, such as qwen 7b.")]) -> None:
    """Find repositories at Hugging Face that hold model files, the most downloaded first."""
    for_server(ctx, "search", *words)


@app.command(rich_help_panel=ASKED)
def download(
    ctx: typer.Context,
    repository: Annotated[str, typer.Argument(metavar="REPOSITORY", help="A repository at Hugging Face, as OWNER/NAME.")],
    files: Annotated[Optional[list[str]], typer.Argument(metavar="[FILE...]", help="The files wanted. One part of a split model brings all its parts. With none, the files are listed.")] = None,
    to: Annotated[str, typer.Option(metavar="PLACE", help="The storage place they go to; never a cache place. Needed where there is more than one storage place.")] = "",
) -> None:
    """Bring model files down from Hugging Face into a storage place. It runs on inside LoadCoach; this returns at once."""
    with running(opened(ctx)[0]) as server:
        server_command(server, "download", repository, *(files or []), place=to)


@app.command(rich_help_panel=ASKED)
def downloads(
    ctx: typer.Context,
    action: Annotated[Optional[str], typer.Argument(metavar="[stop|resume|discard]", help="What to do to one listed download.")] = None,
    path: Annotated[Optional[str], typer.Argument(metavar="[PATH]", help="The exact path printed by this command.")] = None,
) -> None:
    """List downloads, or stop, resume or discard one by its path."""
    if (action is None) != (path is None):
        raise ValueError("Give both an action and the exact path, or neither to list downloads.")
    for_server(ctx, "downloads", action or "", *((path,) if path else ()))


@app.command(rich_help_panel=ASKED)
def fingerprints(
    ctx: typer.Context,
    start: Annotated[str, typer.Argument(metavar="[START]", help="Optional beginning of one fingerprint.")] = "",
) -> None:
    """Inspect retained model fingerprints, optionally narrowed by their beginning."""
    for_server(ctx, "fingerprints", start)


def main() -> None:
    """Run the LoadCoach CLI and report configuration failures in plain language."""
    try:
        app()
    except ValueError as problem:  # a mistake in what was asked for: said in one line, not as a traceback
        raise SystemExit(str(problem)) from None
    except SystemExit as done:
        if done.code:  # Typer ends a command that went well by leaving with 0. Returning says the same.
            raise
