"""The event log: append-only audit records in the configured event store.

Every entry says when, what kind of thing happened, who did it and from where. Anything more
specific to that kind of event is kept beside it as a small JSON object.

Entries are only ever added, never changed. Each one carries a hash of itself and of the entry
before it, so the entries form a chain, and a later change to any of them shows.

Since milestone 91 the log that is kept is the front machine's. Compute writes its entries
without the chain, sends them to turnstile within seconds (`unsent` and `sent`), and keeps a day
of them for its own checks during requests. Front takes them (`take`), keeping compute's numbers
beside its own, and chains them as they arrive, with its own entries in the same chain.

For most requests only facts are kept, such as how long they took and how many tokens. For
requests marked sensitive, the text is kept too. What that text looks like is in capture.py.

Beside the events there is a small table, `texts`, with the last few requests in full, whatever
their level. It is for finding out what went wrong, not a record: it is outside the chain, and
its oldest row goes whenever a new one comes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

from db import DatabaseSource, database, local_path, migrate
from db.schemas import LOG

# What llama-server reports at the end of an answer, and what the OpenAI format calls the same.
COUNTS = {
    "prompt_tokens": re.compile(rb'"(?:prompt_n|prompt_tokens)":\s*(\d+)'),
    "answer_tokens": re.compile(rb'"(?:predicted_n|completion_tokens)":\s*(\d+)'),
    "tokens_per_second": re.compile(rb'"predicted_per_second":\s*([\d.]+)'),
    "cost": re.compile(rb'"cost":\s*([\d.eE+-]+)'),  # a hosted service's price for the answer, in dollars
}
DECIMALS = {"tokens_per_second": 1, "cost": 6}  # the counts that are not whole numbers, and how fine to keep them

COLUMNS = "id, at, kind, what, user, caller, model, trace, level, detail, text, hash"
SENT_KEPT = 86400  # seconds compute keeps an entry after front has it, for its own checks
MACHINE_SHIFT = 40  # public IDs for secondary machines fit exactly in JavaScript's 53-bit integers
LEGACY = "compute"  # the old default node's name: its requests kept unprefixed numbers, which now resolve through `node_aliases`
FRONT = "front"  # the range of the front machine's own requests, such as hosted answers, numbered by front's entries


def machine_slot(name: str) -> int:
    """A stable, nonzero 12-bit machine prefix; configured names are checked for collisions at startup."""
    return (int.from_bytes(hashlib.sha256(name.encode()).digest()[:2], "big") & 0xfff) or 1


def public_number(machine: str | None, source: int) -> int:
    """Keep the primary's request links intact; give each additional machine, and front's own requests (machine None), a separate range.

    Front numbers its entries with its own counter, so a hosted answer's entry number would otherwise meet a
    compute request with the same number as compute's counter catches up."""
    if machine == LEGACY:
        return source
    machine = machine or FRONT
    if not 0 < source < 1 << MACHINE_SHIFT:
        raise ValueError("A compute request number exceeds the public ID range")
    return machine_slot(machine) << MACHINE_SHIFT | source


def origin_number(number: int, names: tuple[str, ...], former: dict[str, str] | None = None) -> tuple[str, int] | None:
    """Resolve a public request ID to its configured machine and local number.

    An unprefixed number is the old default node's, which `former` (old name → node) may have renamed."""
    if number < 1 << MACHINE_SHIFT:
        return (former or {}).get(LEGACY, LEGACY), number
    slot, local = number >> MACHINE_SHIFT, number & ((1 << MACHINE_SHIFT) - 1)
    return next(((name, local) for name in names if name != LEGACY and machine_slot(name) == slot), None)


def token_counts(tail: bytes) -> dict:
    """Pick the token counts out of the last part of an answer. Missing ones are left out.

    The answer itself is not parsed or kept. Only these numbers are looked for, and where a
    number appears more than once the last one wins, since the summary comes at the end.
    """
    found = {}
    for name, pattern in COUNTS.items():
        if matches := pattern.findall(tail):
            found[name] = round(float(matches[-1]), DECIMALS[name]) if name in DECIMALS else int(matches[-1])
    return found


def chain_hash(before: str, row: tuple) -> str:
    """The hash that ties one entry to the one before it.

    `row` is the entry's columns from `at` to `text`, exactly as stored. Changing any of them, or
    the entry before, gives a different hash.
    """
    return hashlib.sha256(json.dumps([before, *row], separators=(",", ":")).encode()).hexdigest()


class Log:
    """The events table. Entries are added, read, and removed when old. Nothing else."""

    def __init__(self, path: DatabaseSource, keep_days: float = 90, keep_text: int = 0, chained: bool = True) -> None:
        """`chained` false for compute, which sends its entries to front, where the chain is."""
        self.keep_days, self.keep_text, self.chained = keep_days, keep_text, chained
        self.machine_names = (LEGACY,)
        self.former: dict[str, str] = {}  # old node name → its current name, from `node_aliases` on front

        # isolation_level=None: transactions begin and end only where this class says so.
        source = database(path)
        self.db = source.connect(check_same_thread=False, isolation_level=None)
        migrate(source, self.db, "loadcoach.log", LOG)
        if file := local_path(path):
            for private in (file, str(file) + "-wal", str(file) + "-shm"):  # readable by the owner only
                if os.path.exists(private):
                    os.chmod(private, 0o600)

    def json_value(self, column: str, key: str, kind: str = "text") -> str:
        """Return an engine-specific scalar JSON expression for trusted column/key names."""
        if self.db.engine == "sqlite":
            return f"json_extract({column}, '$.{key}')"
        value = f"({column}::jsonb ->> '{key}')"
        return f"CAST({value} AS BIGINT)" if kind == "integer" else f"CAST({value} AS DOUBLE PRECISION)" if kind == "number" else value

    def add(
        self,
        kind: str,
        what: str,
        user: str = "",
        caller: str = "",
        model: str | None = None,
        trace: str | None = None,
        level: str | None = None,
        text: dict | None = None,
        must: bool = False,
        **detail,
    ) -> int:
        """Record that something happened. Returns the entry's id.

        If it cannot be written, for instance because the disk is full, a warning is printed and
        0 is returned, so that a failing log does not stop ordinary work. With `must`, the error
        is raised instead: that is for sensitive requests, which must not run unrecorded.
        """
        from loadcoach.machine_identity import fingerprint
        from loadcoach.observability import event_context, annotate
        detail.update(event_context())
        detail.setdefault("machine_fingerprint", fingerprint())
        if model and 'model_id' not in detail:
            try:
                found = self.db.execute('SELECT identity FROM models WHERE name=?',(model,)).fetchone()
                if found and found[0]:
                    detail['model_id'] = found[0]
            except Exception as problem:
                if not self.db.database.is_error(problem):
                    raise
                pass  # standalone log stores and pre-migration databases have no registry
        added = self.write((time.time(), kind, what, user, caller, model, trace, level, json.dumps(detail), text and json.dumps(text)), must)
        if kind == 'request' and what == 'asked' and added:
            machine = getattr(getattr(self, 'observations', None), 'machine', LEGACY)
            annotate(request_number=public_number(machine if not self.chained else None, added),
                     model_id=detail.get('model_id', ''), benchmark=bool(detail.get('benchmark')))
        if kind == 'request' and what in ('done', 'failed', 'refused', 'hung up', 'stopped'):
            annotate(**{k: detail[k] for k in ('prompt_tokens', 'answer_tokens') if k in detail})
            annotate(outcome='error' if what == 'failed' else 'refused' if what == 'refused' else
                     'cancelled' if what in ('hung up', 'stopped') else 'success',
                     benchmark=detail.get('reliability_outcome') == 'benchmark')
        return added

    def write(self, row: tuple, must: bool = False, machine: str | None = None, source: int | None = None) -> int:
        """Append one entry, its columns from `at` to `text` as they are stored, chained to the one before. `machine` and `source` for one from compute."""
        try:
            # IMMEDIATE takes the write lock first, so that reading the last hash and adding the
            # next entry cannot be split by another writer, such as the `loadcoach` command.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.engine == "postgresql":
                    # BEGIN alone does not serialize the read-last-hash/append pair.
                    # Readers remain available; all event writers share this table lock.
                    self.db.execute("LOCK TABLE events IN EXCLUSIVE MODE")
                last = self.chained and self.db.execute("SELECT hash FROM events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
                added = self.db.insert_id(
                    "INSERT INTO events (at, kind, what, user, caller, model, trace, level, detail, text, hash, machine, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*row, chain_hash((last and last[0]) or "", row) if self.chained else None, machine, source),
                )
                if row[1] == "request":  # a request ending is counted into usage_hours with it, or neither is kept
                    from loadcoach.usage import add_hours
                    add_hours(self.db, [(row[0], row[3], row[5], row[2], row[7], row[8])])
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        except Exception as problem:
            if not self.db.database.is_error(problem):
                raise
            if must:
                raise
            from loadcoach.observability import diagnostic
            diagnostic('audit_write_failed', type(problem).__name__, 'loadcoach')
            return 0
        return added or 0

    def read(
        self,
        after: int = 0,
        limit: int = 100,
        user: str | None = None,
        kind: str | None = None,
        trace: str | None = None,
        with_text: bool = False,
        what: str | None = None,
        model: str | None = None,
        before: int = 0,
        since: float | None = None,
        until: float | None = None,
        members: list[str] | None = None,
        node: str | None = None,
    ) -> list[dict]:
        """Entries in the order they happened.

        With `after`, the first `limit` entries newer than that id, which is how a page or the
        command line follows the log. Without it, the latest `limit`, or with `before`, the
        latest older than that id, which is how a page goes back. Captured text is left out
        unless `with_text` is set.
        """
        where, values = ["id > ?"], [after]
        if node == FRONT:
            where.append('machine IS NULL')
        elif node:
            names = self.known_as(node)
            where.append(f"machine IN ({','.join('?' for _ in names)})")
            values.extend(names)
        for expression, value in (("at >= ?", since), ("at <= ?", until)):
            if value is not None:
                where.append(expression)
                values.append(value)
        if members is not None:
            where.append(f"user IN ({','.join('?' for _ in members)})" if members else "0")
            values.extend(members)
        if before:
            where.append("id < ?")
            values.append(before)
        for column, wanted in (("user", user), ("kind", kind), ("trace", trace), ("what", what), ("model", model)):
            if wanted is not None:
                where.append(f"{column} = ?")
                values.append(wanted)
        order = "ASC" if after else "DESC"
        rows = self.db.execute(
            f"SELECT {COLUMNS}, source, machine FROM events WHERE {' AND '.join(where)} ORDER BY id {order} LIMIT ?", (*values, limit)
        ).fetchall()
        return [self.entry(row, with_text) for row in sorted(rows)]

    def choices(self, owner: str | None = None) -> dict:
        """Filter choices expose only facts in the caller's event scope."""
        where, values = (" WHERE user = ?", (owner,)) if owner is not None else ("", ())
        return {key: [row[0] for row in self.db.execute(
            f"SELECT DISTINCT {key} FROM events{where} ORDER BY {key}", values) if row[0]]
            for key in ("kind", "what", "model", "trace", "user")}

    def personal_events(self, owner: str, limit: int, offset: int = 0) -> dict:
        """Page one account's newest events without returning captured text."""
        total = self.db.execute('SELECT COUNT(*) FROM events WHERE user = ?', (owner,)).fetchone()[0]
        offset = min(max(offset, 0), max(0, (total - 1) // limit) * limit)
        rows = self.db.execute(f'SELECT {COLUMNS}, source, machine FROM events WHERE user = ? ORDER BY id DESC LIMIT ? OFFSET ?',
                               (owner, limit, offset))
        return {'total': total, 'page': offset // limit + 1, 'events': [self.entry(row) for row in rows]}

    def request_counts(self, owner: str | None, now: float) -> list[int]:
        """Recorded arrivals in rolling windows, without reading bodies or joining endings."""
        where = "kind = 'request' AND what = 'asked' AND at >= ? AND at <= ?"
        values = [now - 3600, now]
        if owner is not None:
            where += ' AND user = ?'
            values.append(owner)
        row = self.db.execute(
            "SELECT SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END), COUNT(*) FROM events WHERE " + where,
            (now - 300, now - 900, *values)).fetchone()
        return [int(value or 0) for value in row]

    def requests(self, owner: str | None, limit: int, offset: int = 0, completed: bool = False,
                 group: str = '', memberships: dict | None = None, filters: dict | None = None) -> dict:
        """Page request starts with their final outcome, never prompt or response text."""
        from loadcoach.usage import ENDINGS

        marks = ','.join('?' for _ in ENDINGS)
        # Look up each ending through events_request_parent. Materializing every
        # retained ending makes even a small recent-request page scan the history.
        parent = self.json_value("f.detail", "of", "integer")
        join = f"""FROM events a LEFT JOIN events e ON e.id = (
            SELECT MAX(f.id) FROM events f
            WHERE {parent} = COALESCE(a.source, a.id)
                AND f.kind = 'request' AND f.what IN ({marks})
                AND (f.machine = a.machine OR f.machine IS NULL AND a.machine IS NULL)
            )"""
        where = " WHERE a.kind = 'request' AND a.what = 'asked'"
        values = []
        if owner is not None:
            where += " AND a.user = ?"
            values.append(owner)
        if completed:
            where += " AND e.id IS NOT NULL"
        if group:
            # Retain the group at the time of the request. Old records fall back to current
            # membership, just as Usage does; a later account move must not rewrite history.
            recorded_group = f"COALESCE({self.json_value('e.detail', 'group')}, {self.json_value('a.detail', 'group')})"
            members = [name for name, value in (memberships or {}).items() if value == group]
            inferred = f"a.user IN ({','.join('?' for _ in members)})" if members else '0 = 1'
            where += f" AND ({recorded_group} = ? OR ({recorded_group} IS NULL AND {inferred}))"
            values.extend([group, *members])
        duration = self.db.greatest("0", "e.at - a.at")
        filters = filters or {}
        if filters.get('node') == FRONT:
            where += ' AND a.machine IS NULL'
        elif filters.get('node'):
            names = self.known_as(filters['node'])
            where += f" AND a.machine IN ({','.join('?' for _ in names)})"
            values.extend(names)
        for key, expression in (('model', 'a.model'), ('user', 'a.user'), ('ip', 'a.caller')):
            if filters.get(key):
                where += f' AND {expression} = ?'
                values.append(filters[key])
        if filters.get('level'):
            where += " AND COALESCE(NULLIF(a.level, ''), 'unrecorded') = ?"
            values.append(filters['level'])
        if filters.get('q') and not filters['q'].isdecimal():
            # Literal substring matching; SQL wildcard characters in a query are not operators.
            query = '%' + filters['q'].replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            expressions = ('a.model', 'a.user', 'a.caller', 'a.trace', self.json_value('a.detail', 'request_id'))
            where += " AND (" + " OR ".join(f"LOWER({column}) LIKE LOWER(?) ESCAPE '!'" for column in expressions) + ")"
            values.extend([query] * len(expressions))
        for key, expression, operator in (('date_min', 'a.at', '>='), ('date_max', 'a.at', '<='),
                                           ('duration_min', duration, '>='), ('duration_max', duration, '<=')):
            if key in filters:
                where += f' AND {expression} {operator} ?'
                values.append(filters[key])
                if key.startswith('duration_'):
                    where += ' AND e.id IS NOT NULL'
        if filters.get('status') == 'unfinished':
            where += ' AND e.id IS NULL'
        elif filters.get('status'):
            where += ' AND e.what = ?'
            values.append(filters['status'])
        if any(key in filters for key in ('request_min', 'request_max')) or filters.get('q', '').isdecimal():
            # SQL compares displayed public IDs, including front and historical node prefixes.
            names = {row[0] for row in self.db.execute('SELECT DISTINCT machine FROM events')}
            names.update((None, LEGACY, *self.machine_names))
            prefixes = [(name, public_number(name, 1) - 1) for name in sorted(names, key=lambda value: value or '')]
            cases = ' '.join(f'WHEN a.machine {"IS NULL" if name is None else "= ?"} THEN {prefix}'
                             for name, prefix in prefixes)
            public_id = f'(CASE {cases} ELSE 0 END + COALESCE(a.source,a.id))'
            for key, operator in (('request_min', '>='), ('request_max', '<=')):
                if key in filters:
                    where += f' AND {public_id} {operator} ?'
                    values.extend([name for name, _ in prefixes if name is not None])
                    values.append(filters[key])
            if filters.get('q', '').isdecimal():
                where += f' AND {public_id} = ?'
                values.extend([name for name, _ in prefixes if name is not None])
                values.append(int(filters['q']) if len(filters['q']) < 17 else -1)
        needs_ending = completed or group or any(key in filters for key in ('status', 'duration_min', 'duration_max'))
        total = self.db.execute("SELECT COUNT(*) " + (join if needs_ending else "FROM events a") + where,
                                [*ENDINGS, *values] if needs_ending else values).fetchone()[0]
        offset = min(max(offset, 0), max(0, (total - 1) // limit) * limit)
        rows = self.db.execute(f"""SELECT COALESCE(a.source, a.id), a.at, a.model, a.user, a.caller, e.what, a.machine,
            CASE WHEN e.id IS NOT NULL THEN {duration} END, a.level, e.detail """ + join + where +
            " ORDER BY a.id DESC LIMIT ? OFFSET ?", (*ENDINGS, *values, limit, offset))
        from loadcoach.diagnostics import number as measured
        requests = []
        for number, at, model, user, caller, outcome, machine, duration, level, raw in rows:
            detail = json.loads(raw) if raw else {}
            diagnostic = detail.get('diagnostics') or {}
            phases = diagnostic.get('phases', [])
            queue = [measured(p.get('seconds')) for p in phases if p.get('stage') == 'queue']
            requests.append(dict(id=public_number(machine, number), at=datetime.fromtimestamp(at).isoformat(timespec='seconds'),
                                 model=model, user=user, caller=caller, outcome=outcome, duration=duration,
                                 timestamp=at, node=self.former.get(machine, machine) if machine else FRONT, level=level,
                                 compute_total=measured(diagnostic.get('total_seconds')),
                                 queue_seconds=sum(queue) if queue and all(v is not None for v in queue) else None,
                                 first_output_seconds=measured(diagnostic.get('first_output_seconds')),
                                 prompt_tokens=measured(detail.get('prompt_tokens')), answer_tokens=measured(detail.get('answer_tokens')),
                                 cost=measured(detail.get('cost')), status=detail.get('status'),
                                 server_error=detail.get('reliability_outcome') in ('error', 'timeout', 'restart') or (measured(detail.get('status')) or 0) >= 500 or outcome == 'failed',
                                 benchmark=detail.get('reliability_outcome') == 'benchmark'))
        return {'total': total, 'page': offset // limit + 1, 'requests': requests}

    @staticmethod
    def entry(row: tuple, with_text: bool = False) -> dict:
        """One row of the table as it is handed out: its columns, with its details beside them."""
        number, at, kind, what, user, caller, model, trace, level, detail, text, _, source, machine = row
        details = json.loads(detail)
        if machine and machine != LEGACY and isinstance(details.get("of"), int):
            details["of"] = public_number(machine, details["of"])
        elif not machine and kind == "request" and isinstance(details.get("of"), int) and 0 < details["of"] < 1 << MACHINE_SHIFT:
            details["of"] = public_number(None, details["of"])  # how one of front's own requests ended, under its public number
        entry = {
            "id": number,
            **({"source": public_number(machine, source)} if source is not None else {}),
            **({"machine": machine} if machine else {}),
            "at": datetime.fromtimestamp(at).isoformat(timespec="seconds"),
            "kind": kind,
            "what": what,
            "user": user,
            "caller": caller,
            "model": model,
            "trace": trace,
            "level": level,
            **details,
        }
        if text and with_text:
            entry["text"] = json.loads(text)
        return entry

    def about(self, number: int, with_text: bool = False, request: bool = False) -> list[dict]:
        """One entry, and every later entry that says it is `of` that one: a request, then how it ended.

        `number` is the entry's id here, or with `request`, the request's number, which on front is
        compute's: the number it was given where it was answered. `of` always means that number.
        """
        machine = None
        if request and number >= 1 << MACHINE_SHIFT and number >> MACHINE_SHIFT == machine_slot(FRONT):
            local = number & ((1 << MACHINE_SHIFT) - 1)  # one of front's own requests: numbered by its entry here
            first = self.db.execute("SELECT id, id FROM events WHERE machine IS NULL AND id = ? AND kind = 'request' AND what = 'asked'",
                                    (local,)).fetchone()
        elif request and number >= 1 << MACHINE_SHIFT:
            slot, local = number >> MACHINE_SHIFT, number & ((1 << MACHINE_SHIFT) - 1)
            machine = next((name for name in self.machine_names if name != LEGACY and machine_slot(name) == slot), None)
            if machine is None:
                return []
            first = self.db.execute("SELECT id, source FROM events WHERE machine = ? AND source = ? AND kind = 'request' AND what = 'asked'",
                                    (machine, local)).fetchone()
        elif request:
            legacy = self.legacy_names()
            first = self.db.execute(f"""SELECT id, COALESCE(source, id), machine FROM events
                WHERE (machine IN ({','.join('?' * len(legacy))}) AND source = ? OR machine IS NULL AND id = ?)
                  AND kind = 'request' AND what = 'asked' ORDER BY source IS NULL LIMIT 1""", (*legacy, number, number)).fetchone()
            machine = first[2] if first else None
        else:
            first = self.db.execute("SELECT id, COALESCE(source, id), machine FROM events WHERE id = ?", (number,)).fetchone()
            machine = first[2] if first else None
        if not first:
            return []
        same_machine = "(machine = ? OR machine IS NULL AND CAST(? AS TEXT) IS NULL)"
        parent = self.json_value('detail', 'of', 'integer')
        rows = self.db.execute(
            f"SELECT {COLUMNS}, source, machine FROM events WHERE id >= ? AND "
            f"(id = ? OR ({same_machine} AND {parent} = ?) "
            f"OR (machine IS NULL AND {parent} = ?)) ORDER BY id",
            (first[0], first[0], machine, machine, first[1], public_number(machine, first[1])),
        )
        return [self.entry(row, with_text) for row in rows]

    # ---- the latest requests in full ----------------------------------------------------------

    def keep_asked(self, entry: int, asked: str) -> None:
        """Keep what a request sent, and let go of the oldest, so that no more than `keep_text` are kept."""
        try:
            if entry and self.keep_text:
                self.db.execute("INSERT INTO texts (entry, asked) VALUES (?, ?) ON CONFLICT(entry) DO UPDATE SET asked=excluded.asked", (entry, asked))
            # Also with nothing added: `keep_text` may have been made smaller, or 0, since the last time.
            self.db.execute("DELETE FROM texts WHERE entry NOT IN (SELECT entry FROM texts ORDER BY entry DESC LIMIT ?)", (self.keep_text,))
        except Exception as problem:
            if not self.db.database.is_error(problem):
                raise
            print(f"loadcoach: could not keep the text of request {entry}: {problem}", file=sys.stderr)

    def keep_said(self, entry: int, said: dict) -> None:
        """Keep what a request was answered, beside what it sent. Nothing happens if that is no longer kept."""
        try:
            self.db.execute("UPDATE texts SET said = ? WHERE entry = ?", (json.dumps(said), entry))
        except Exception as problem:
            if not self.db.database.is_error(problem):
                raise
            print(f"loadcoach: could not keep the answer of request {entry}: {problem}", file=sys.stderr)

    def text_of(self, entry: int) -> dict | None:
        """What a request sent and what it was answered, if it is among those still kept in full."""
        legacy_entry = entry
        if entry >= 1 << MACHINE_SHIFT and entry >> MACHINE_SHIFT == machine_slot(FRONT):
            entry = entry & ((1 << MACHINE_SHIFT) - 1)  # front's own request: its text is kept under its entry here
        elif entry >= 1 << MACHINE_SHIFT:
            slot, local = entry >> MACHINE_SHIFT, entry & ((1 << MACHINE_SHIFT) - 1)
            machine = next((name for name in self.machine_names if name != LEGACY and machine_slot(name) == slot), None)
            if machine is None:
                return None
            row = self.db.execute("SELECT id FROM events WHERE machine = ? AND source = ? AND kind = 'request' AND what = 'asked'",
                                  (machine, local)).fetchone()
            if row is None:
                return None
            entry = row[0]
        else:
            legacy = self.legacy_names()
            row = self.db.execute(f"SELECT id FROM events WHERE machine IN ({','.join('?' * len(legacy))}) AND source = ? AND kind = 'request' AND what = 'asked'",
                                  (*legacy, entry)).fetchone()
            if row is not None:
                entry = row[0]
        row = self.db.execute("SELECT asked, said FROM texts WHERE entry = ?", (entry,)).fetchone()
        if row is None and entry != legacy_entry and legacy_entry < 1 << MACHINE_SHIFT:
            row = self.db.execute("SELECT asked, said FROM texts WHERE entry = ?", (legacy_entry,)).fetchone()
        return row and {"asked": json.loads(row[0]), "said": row[1] and json.loads(row[1])}

    # ---- forwarding to the front machine ---------------------------------------------------------

    def unsent(self, limit: int = 500) -> tuple[list[dict], list[dict]]:
        """Compute's entries front does not have yet, the oldest first, and the texts of the requests among them that have ended."""
        through = self.db.execute("SELECT MAX(through) FROM forwarded").fetchone()[0] or 0
        rows = self.db.execute(f"SELECT {COLUMNS} FROM events WHERE id > ? ORDER BY id LIMIT ?", (through, limit)).fetchall()
        entries = [dict(zip(COLUMNS.split(", "), row)) for row in rows]
        ended = [of for one in entries if one["kind"] == "request" and one["what"] != "asked" and (of := json.loads(one["detail"]).get("of"))]
        texts = [{"entry": entry, "asked": asked, "said": said} for entry, asked, said in self.db.execute(
            f"SELECT entry, asked, said FROM texts WHERE entry IN ({','.join('?' for _ in ended) or 'NULL'})", ended)]
        return entries, texts

    def sent(self, through: int) -> None:
        """Front has every entry up to `through`."""
        self.db.execute("DELETE FROM forwarded")
        self.db.execute("INSERT INTO forwarded (through) VALUES (?)", (through,))

    def known_as(self, machine: str) -> tuple[str, ...]:
        """`machine` and every old name that now means it."""
        return (machine, *(old for old, new in self.former.items() if new == machine and old != machine))

    def legacy_names(self) -> tuple[str, ...]:
        """The names rows of the old default node may carry: its old name, and its new one once renamed."""
        return tuple(dict.fromkeys((LEGACY, self.former.get(LEGACY, LEGACY))))

    def take(self, machine: str, entries: list[dict], texts: list[dict], keep_text: int) -> int:
        """On front: entries a compute machine sent, chained here as they come. One already here is not taken again. Gives the last one's number there."""
        for one in entries:
            names = self.known_as(machine)
            if self.db.execute(f"SELECT 1 FROM events WHERE machine IN ({','.join('?' * len(names))}) AND source = ?",
                               (*names, one["id"])).fetchone():
                continue
            # Its columns exactly as compute stored them: its details may hold any name, `machine` among them.
            self.write(tuple(one[column] for column in COLUMNS.split(", ")[1:-1]), must=True, machine=machine, source=one["id"])
        self.keep_text = keep_text
        for one in texts:
            entry = one["entry"]
            found = self.db.execute("SELECT id FROM events WHERE machine = ? AND source = ? AND kind = 'request' AND what = 'asked'",
                                    (machine, entry)).fetchone()
            if found is None:
                continue
            entry = found[0]
            self.keep_asked(entry, one["asked"])
            if one["said"]:
                self.db.execute("UPDATE texts SET said = ? WHERE entry = ?", (one["said"], entry))
        return entries[-1]["id"] if entries else 0

    def verify(self) -> tuple[int, int | None]:
        """Check the chain. Returns how many entries were checked, and the first that fails, if any.

        The oldest entry still kept is taken on trust, because the one before it has been removed
        with age. Every entry after it must match the hash worked out from it and the one before.
        Entries made before the chain existed are skipped.
        """
        before, checked = None, 0
        for row in self.db.execute(f"SELECT {COLUMNS} FROM events WHERE hash IS NOT NULL ORDER BY id"):
            entry, stored = row[0], row[-1]
            if before is not None:
                if chain_hash(before, row[1:-1]) != stored:
                    return checked, entry
                checked += 1
            before = stored
        return checked, None

    def spent(self, user: str, seconds: float = 86400) -> float:
        """What this user's hosted answers have cost in the last `seconds`, a day unless said."""
        total = self.db.execute(
            f"SELECT SUM({self.json_value('detail', 'cost', 'number')}) FROM events WHERE user = ? AND kind = 'request' AND at > ?",
            (user, time.time() - seconds),
        ).fetchone()[0]
        # PostgreSQL sums NUMERIC as a Decimal, which JSON cannot carry: /loadcoach/me sends this.
        return float(total or 0.0)

    def paces(self) -> dict[str, dict[str, float]]:
        """How fast each model has been answering, and how long it last took to load, as the log has them.

        `speed` is tokens a second, the mean of a model's last 20 answers that say one. `load`
        is the seconds its last load took. A model that never answered, or never loaded, is
        in neither. They are what real use on this machine was, with no benchmark needed.
        """
        speeds = self.db.execute(
            f"""SELECT model, AVG(pace) FROM (
                   SELECT model, {self.json_value('detail', 'tokens_per_second', 'number')} AS pace, ROW_NUMBER() OVER (PARTITION BY model ORDER BY id DESC) AS back
                   FROM events WHERE kind = 'request' AND what = 'done' AND {self.json_value('detail', 'tokens_per_second')} IS NOT NULL
               ) recent WHERE back <= 20 GROUP BY model"""
        )
        loads = self.db.execute(
            f"SELECT model, {self.json_value('detail', 'seconds', 'number')} FROM events WHERE id IN (SELECT MAX(id) FROM events WHERE kind = 'model' AND what = 'loaded' GROUP BY model)"
        )
        prompts = self.db.execute(
            f"""SELECT model, AVG(pace) FROM (
                   SELECT model, {self.json_value('detail', 'prompt_tokens_per_second', 'number')} AS pace,
                          ROW_NUMBER() OVER (PARTITION BY model ORDER BY id DESC) AS back
                   FROM events WHERE kind = 'request' AND what = 'done'
                     AND {self.json_value('detail', 'prompt_tokens_per_second', 'number')} > 0
               ) recent WHERE back <= 20 GROUP BY model"""
        )
        return {"speed": {model: round(pace, 1) for model, pace in speeds if pace},
                "prompt_speed": {model: round(pace, 1) for model, pace in prompts if pace},
                "load": {model: took for model, took in loads if took is not None}}

    def service_cost(self, service: str, seconds: float = 86400) -> float:
        """What everyone's answers from one outside service have cost in the last `seconds`, a day unless said."""
        total = self.db.execute(
            f"SELECT SUM({self.json_value('detail', 'cost', 'number')}) FROM events WHERE kind = 'request' AND at > ? AND {self.json_value('detail', 'service')} = ?",
            (time.time() - seconds, service),
        ).fetchone()[0]
        return total or 0.0

    def usage(self, user: str, seconds: float = 86400) -> dict:
        """How much one user has asked for in the last `seconds`, a day unless said."""
        requests, prompt, answer = self.db.execute(
            f"SELECT COUNT(*), SUM({self.json_value('detail', 'prompt_tokens', 'integer')}), SUM({self.json_value('detail', 'answer_tokens', 'integer')})"
            " FROM events WHERE user = ? AND kind = 'request' AND what != 'asked' AND at > ?",
            (user, time.time() - seconds),
        ).fetchone()
        # PostgreSQL sums a BIGINT as NUMERIC, which arrives as a Decimal that JSON cannot carry.
        return {"requests": int(requests), "prompt_tokens": int(prompt or 0), "answer_tokens": int(answer or 0)}

    def last_used(self) -> dict[str, str]:
        """When each model was last asked for, as far back as the log goes."""
        rows = self.db.execute("SELECT model, MAX(at) FROM events WHERE kind = 'request' AND model IS NOT NULL GROUP BY model")
        return {model: datetime.fromtimestamp(at).isoformat(timespec="seconds") for model, at in rows}

    def trace_level(self, user: str, trace: str) -> str | None:
        """The highest level this user's trace has had so far, or None if it is new."""
        row = self.db.execute("SELECT level FROM traces WHERE user = ? AND trace = ?", (user, trace)).fetchone()
        if row is None and self.chained:
            from loadcoach.groups import LEVELS
            # Existing front logs were imported before trace levels moved here. Recover a
            # previously sensitive trace from its chained events on first use.
            levels = [value for (value,) in self.db.execute("SELECT DISTINCT level FROM events WHERE user = ? AND trace = ?", (user, trace))]
            known = [value for value in levels if value in LEVELS]
            if known:
                level = max(known, key=LEVELS.index)
                self.set_trace_level(user, trace, level)
                return level
        return row and row[0]

    def set_trace_level(self, user: str, trace: str, level: str) -> None:
        """Raise a trace's retained sensitivity level without lowering prior protection."""
        from loadcoach.groups import LEVELS
        old = self.db.execute("SELECT level FROM traces WHERE user = ? AND trace = ?", (user, trace)).fetchone()
        if old and old[0] in LEVELS and LEVELS.index(old[0]) > LEVELS.index(level):
            level = old[0]
        self.db.execute(
            "INSERT INTO traces (user, trace, level, at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (user, trace) DO UPDATE SET level = excluded.level, at = excluded.at",
            (user, trace, level, time.time()),
        )

    def seen(self, user: str, trace: str | None, text_hash: str) -> bool:
        """True if this piece of text has already been captured in this trace."""
        if trace is None:
            return False
        found = self.db.execute("SELECT 1 FROM seen WHERE user = ? AND trace = ? AND hash = ?", (user, trace, text_hash))
        return found.fetchone() is not None

    def mark_seen(self, user: str, trace: str | None, hashes: list[str]) -> None:
        """Remember captured content fingerprints for deduplication within a trace."""
        if trace is not None:
            now = time.time()
            self.db.executemany(
                "INSERT INTO seen (user, trace, hash, at) VALUES (?, ?, ?, ?) ON CONFLICT(user,trace,hash) DO NOTHING",
                [(user, trace, text_hash, now) for text_hash in hashes],
            )

    def prune(self) -> int:
        """Remove entries older than `keep_days`. Returns how many went. 0 days keeps everything."""
        if not self.keep_days:
            return 0
        oldest = time.time() - self.keep_days * 86400
        self.db.execute("DELETE FROM traces WHERE at < ?", (oldest,))
        self.db.execute("DELETE FROM seen WHERE at < ?", (oldest,))
        if self.chained:
            # Everything before the first entry that is still young enough. On front, entries from compute come in after
            # entries made here, or all at once when the log moved, so `at` is not in the order of the chain. Removing
            # only from its start keeps what is left a whole chain.
            gone = self.db.execute("DELETE FROM events WHERE id < COALESCE((SELECT MIN(id) FROM events WHERE at >= ?), (SELECT MAX(id) + 1 FROM events))", (oldest,)).rowcount
        else:  # compute: what front has, after a day. What it does not have yet is kept, however old: it is the spool
            through = self.db.execute("SELECT MAX(through) FROM forwarded").fetchone()[0] or 0
            gone = self.db.execute("DELETE FROM events WHERE at < ? AND id <= ?", (time.time() - SENT_KEPT, through)).rowcount
        names = self.legacy_names()
        self.db.execute(f"DELETE FROM texts WHERE entry NOT IN (SELECT id FROM events UNION SELECT source FROM events WHERE machine IN ({','.join('?' * len(names))}) AND source IS NOT NULL)", names)
        return gone
