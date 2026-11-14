"""Optional seven-day span storage and thirty-day, rebuildable metric rollups.

The audit chain is untouched. A bounded daemon thread owns a separate connection;
slow/full disks can lose optional data but cannot block the ASGI request loop.
"""

from __future__ import annotations

import json
import math
import threading
import contextvars
import time
from collections import Counter, deque

from loadcoach.observability import FIELDS, ID, LABEL, POLLS, PROCESS, SPAN

SOURCE_FRESH_SECONDS = 60

BUCKETS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
    1800.0,
    float("inf"),
)
MAX_RECORDS = 4096
MAX_BYTES = 8 * 1024 * 1024
# What control node keeps, set from weightroom.toml and stored beside the spans so every writer and pruner
# of that store follows it. A store without a policy row (compute's spool) keeps everything.
POLICY = {"keep_days": 7, "success_percent": 100, "max_spans": 0, "quiet_routes": list(POLLS)}
SPOOL_SECONDS = 3600  # how long a node keeps spans control node has acknowledged


def policy(connection) -> dict:
    """The store's span policy, or the defaults when none is set or it cannot be read."""
    try:
        row = connection.execute("SELECT value FROM observation_policy WHERE name='spans'").fetchone()
        return {**POLICY, **(json.loads(row[0]) if row else {})}
    except Exception:
        return dict(POLICY)


def set_policy(connection, keep_days: int, success_percent: int, max_spans: int, quiet_routes=tuple(POLLS)) -> None:
    """Record what this store keeps: days of spans, the share of all-successful traces, a row cap (0 is
    none), and the routes whose successful traces are not kept when a trace starts there."""
    check_policy(keep_days, success_percent, max_spans, quiet_routes)
    value = json.dumps({"keep_days": keep_days, "success_percent": success_percent, "max_spans": max_spans,
                        "quiet_routes": list(quiet_routes)})
    connection.execute("INSERT INTO observation_policy VALUES ('spans', ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (value,))


def check_policy(keep_days, success_percent, max_spans, quiet_routes=tuple(POLLS)) -> None:
    """Refuse a span policy outside its ranges, naming the setting."""
    if (not isinstance(quiet_routes, (list, tuple)) or len(quiet_routes) > 64
            or any(not isinstance(one, str) or not LABEL.fullmatch(one.replace("/", ".") or "x") or not one.startswith("/")
                   for one in quiet_routes)):
        raise ValueError("observation_quiet_routes must be a list of at most 64 routes, each starting with /")
    if type(keep_days) is not int or not 1 <= keep_days <= 30:
        raise ValueError("observation_keep_days must be a whole number of days from 1 through 30")
    if type(success_percent) is not int or not 0 <= success_percent <= 100:
        raise ValueError("observation_success_percent must be a whole number from 0 through 100")
    if type(max_spans) is not int or not 0 <= max_spans <= 100_000_000:
        raise ValueError("observation_max_spans must be a whole number from 0 (no cap) through 100,000,000")


def kept(record, success_percent: int) -> bool:
    """Every span that did not succeed is kept; of successful ones, the same share of traces, whole traces at a time."""
    return record["outcome"] != "success" or int(record["trace_id"][:8], 16) % 100 < success_percent


def clean(record, machine: str) -> dict:
    """Validate signed metadata without accepting arbitrary fields, content or dimensions."""
    if not isinstance(record, dict):
        raise ValueError("Observation must be an object.")
    for key, pattern in (("request_id", ID), ("trace_id", ID), ("id", SPAN)):
        if not isinstance(record.get(key), str) or not pattern.fullmatch(record[key]):
            raise ValueError("Invalid observation identity.")
    result = {k: record[k] for k in ("request_id", "trace_id", "id")}
    for key in ("at", "seconds"):
        value = record.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("Invalid observation timing.")
        result[key] = value
    for key in (
        "parent_id",
        "process",
        "release",
        "service",
        "operation",
        "owner_id",
        "user",
        "key_id",
        "application",
        "application_id",
        "work",
        "route",
        "outcome",
        "error_code",
    ):
        value = record.get(key, "")
        if (
            not isinstance(value, str)
            or len(value) > 128
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Invalid observation metadata.")
        result[key] = value
    if result["outcome"] not in (
        "underway",
        "success",
        "error",
        "refused",
        "cancelled",
        "unknown",
    ):
        raise ValueError("Invalid observation outcome.")
    if result["service"] not in (
        "turnstile",
        "loadcoach",
        "weightroom",
        "toollocker",
        "adapter",
        "voice",
        "voice-gateway",
        "fixture",
    ):
        raise ValueError("Unknown observation service.")
    result["operation"] = (
        result["operation"] if LABEL.fullmatch(result["operation"]) else "other"
    )
    for key in FIELDS:
        value = record.get(key)
        if (
            isinstance(value, str)
            and len(value) <= 128
            or isinstance(value, bool)
            or type(value) is int
            and abs(value) < 1 << 53
            or type(value) is float
            and math.isfinite(value)
            and 0 <= value < 1e15
        ):
            result[key] = value
    result.update(
        version=1,
        machine=machine[:128],
        status=min(max(int(record.get("status", 0)), 0), 599),
        finished=record.get("finished", True) is True,
        ingress=record.get("ingress") is True,
        truncated=record.get("truncated") is True,
    )
    return result


SETTLE = 600  # seconds a trace must be still before the sweep judges it: compute forwards in batches


def quiet(connection, record) -> bool:
    """A successful span of a trace the sweep found to be a routine poll is not stored when it arrives late."""
    return record["outcome"] in ("success", "underway") and bool(
        connection.execute("SELECT 1 FROM observation_quiet WHERE trace_id=?", (record["trace_id"],)).fetchone())


def sweep(connection, routes, now=None, *,stop=None) -> int:
    """Remove routine polls: traces where every span succeeded and every service entered through a quiet route.

    Each service starts its own `ingress` span, so a chat request whose routing looks up
    /loadcoach/facts also has turnstile's /v1/... ingress and is kept. A trace is judged once
    nothing in it has arrived for SETTLE seconds; its id goes into observation_quiet so spans that
    arrive later are not stored. Each sweep starts where the last ended, the first from the oldest.
    """
    now = time.time() if now is None else now
    routes = set(routes)
    if not routes:
        return 0
    row = connection.execute("SELECT value FROM observation_policy WHERE name='swept'").fetchone()
    since, until = (float(row[0]) - SETTLE if row else 0.0), now - SETTLE
    recent = {one for (one,) in connection.execute("SELECT DISTINCT trace_id FROM observation_spans WHERE received>=?", (until,))}
    traces = {}
    for trace, outcome, data in connection.execute(
            "SELECT trace_id, outcome, data FROM observation_spans WHERE received>=? AND received<?", (since, until)):
        if stop and stop.is_set():
            return 0
        verdict = traces.setdefault(trace, [True, False])  # still quiet, entered at least once
        if not verdict[0]:
            continue
        if outcome != "success":
            verdict[0] = False
            continue
        span = json.loads(data)
        if span.get("ingress"):
            verdict[1] = True
            verdict[0] = span.get("route") in routes
    polls = [trace for trace, (still, entered) in traces.items() if still and entered and trace not in recent]
    for start in range(0, len(polls), 500):  # short transactions, so audit writers are never held up long
        if stop and stop.is_set():
            return start
        chunk = polls[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        with connection:
            connection.execute(f"DELETE FROM observation_spans WHERE trace_id IN ({marks})", chunk)
            connection.executemany("INSERT INTO observation_quiet VALUES (?, ?) ON CONFLICT(trace_id) DO NOTHING",
                                   [(trace, now) for trace in chunk])
        time.sleep(0.001)
    with connection:
        connection.execute("INSERT INTO observation_policy VALUES ('swept', ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                           (str(until),))
    return len(polls)


def ingest(connection, records, machine: str, forwarded: bool = False) -> int:
    """Store bounded records idempotently; callers must open an explicit transaction for metrics."""
    if not isinstance(records, list) or len(records) > 500:
        raise ValueError("At most 500 observations may be ingested.")
    now, count = time.time(), 0
    completed = []
    share = policy(connection)["success_percent"]
    for index, raw in enumerate(records):
        if index and index % 16 == 0:
            time.sleep(
                0
            )  # Yield the GIL between optional batches for response-loop fairness.
        record = clean(raw, machine)
        text = json.dumps(record, separators=(",", ":"))
        if len(text.encode()) > 4096:
            raise ValueError("Observation metadata is too large.")
        key = record["trace_id"] + record["id"]
        if not forwarded and quiet(connection, record):
            if record["finished"]:
                completed.append(record)  # counted in the metrics, not stored
            continue
        if not kept(record, share):
            # Not stored, still counted in the metrics. A retried duplicate of such a span cannot be
            # recognised, so it would count twice; forwarding retries only after a failed acknowledgement.
            if record["finished"] and not forwarded:
                completed.append(record)
            continue
        inserted = connection.execute(
            "INSERT INTO observation_spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (
                key,
                record["request_id"],
                record["trace_id"],
                record["owner_id"],
                record["service"],
                machine,
                record["application"],
                record["outcome"],
                record.get("error_code", ""),
                record["at"],
                now,
                int(record["finished"]),
                int(not forwarded),
                text,
            ),
        ).rowcount
        ended = inserted and record["finished"]
        if not inserted and record["finished"]:
            ended = connection.execute(
                "UPDATE observation_spans SET outcome=?,error_code=?,received=?,finished=1,sent=?,data=? "
                "WHERE id=? AND finished=0",
                (
                    record["outcome"],
                    record.get("error_code", ""),
                    now,
                    int(not forwarded),
                    text,
                    key,
                ),
            ).rowcount
        if ended and not forwarded:
            completed.append(record)
        count += int(bool(inserted or ended))
    rollups(connection, completed)
    return count


def receive(source, records, machine: str, health=None) -> int:
    """Commit forwarded optional evidence off the ASGI loop on its own bounded connection."""
    connection = (
        source.connect(timeout=0.25)
        if source.engine == "sqlite"
        else source.connect(connect_timeout=1)
    )
    try:
        if source.engine == "postgresql":
            connection.execute("SET statement_timeout = 1000")
            connection.commit()
        if not isinstance(records, list) or len(records) > 500:
            raise ValueError("At most 500 observations may be ingested.")
        count = 0
        for offset in range(0, len(records), 16):
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                count += ingest(connection, records[offset : offset + 16], machine)
            time.sleep(
                0.001
            )  # Let required audit writers acquire the shared SQLite lock.
        with connection:
            if health is not None:
                if not isinstance(health, dict):
                    raise ValueError("Observation health must be an object.")
                allowed = (
                    "process",
                    "recording",
                    "pending",
                    "bytes",
                    "written",
                    "dropped",
                    "last_write",
                    "high_water_bytes",
                    "backlog_count",
                    "backlog_age_seconds",
                    "counter_scope",
                    "active",
                )
                safe = {key: value for key, value in health.items() if key in allowed}
                encoded = json.dumps(safe, allow_nan=False)
                if len(encoded.encode()) > 2048:
                    raise ValueError("Observation health is too large.")
                connection.execute(
                    "INSERT INTO observation_sources VALUES (?,?,?) ON CONFLICT(machine) DO UPDATE SET at=excluded.at,data=excluded.data",
                    (machine, time.time(), encoded),
                )
            return count
    finally:
        connection.close()


def optional_spool(connection) -> bool:
    """Use WAL NORMAL only for optional node data; audit/control node connections keep their policy."""
    if (
        connection.engine == "sqlite"
        and connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    ):
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        return True
    return False


def acknowledge(source, rows) -> bool:
    """Commit bounded optional acknowledgements off-loop; failures leave records retryable."""
    if (
        not isinstance(rows, list)
        or len(rows) > 500
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or len(row["id"]) != 48
            or any(c not in "0123456789abcdef" for c in row["id"])
            or type(row.get("finished")) is not bool
            for row in rows
        )
    ):
        raise ValueError("Invalid observation acknowledgement.")
    connection = None
    try:
        connection = (
            source.connect(timeout=0.25)
            if source.engine == "sqlite"
            else source.connect(connect_timeout=1)
        )
        if source.engine == "postgresql":
            connection.execute("SET statement_timeout = 1000")
            connection.commit()
        else:
            optional_spool(connection)
        with connection:
            # Log's shared connection uses autocommit; a context manager alone is insufficient.
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "UPDATE observation_spans SET sent=1 WHERE id=? AND finished=?",
                [(row["id"], int(row["finished"])) for row in rows],
            )
        return True
    except Exception as problem:
        from loadcoach.observability import diagnostic

        diagnostic("observation_ack_failed", type(problem).__name__)
        return False
    finally:
        if connection is not None:
            connection.close()


def metric_deltas(record):
    """Yield fixed histogram keys and finite deltas without touching a database."""
    metrics = {"seconds": record["seconds"]}
    metrics.update(
        {
            k: record[k]
            for k in (
                "first_output_seconds",
                "queue_seconds",
                "readiness_seconds",
                "prompt_tokens",
                "answer_tokens",
            )
            if type(record.get(k)) in (float, int) and record[k] >= 0
        }
    )
    if (
        record["operation"] == "model_attempt"
        and type(record.get("attempt")) is int
        and record["attempt"] > 1
    ):
        metrics["fallbacks"] = 1
    if record["operation"] in ("dispatch", "hosted_attempt", "adapter_dispatch"):
        metrics["inference_dispatches"] = 1
    operation = "ingress" if record.get("ingress") else record["operation"]
    for name, value in metrics.items():
        bucket = next(
            (i for i, edge in enumerate(BUCKETS) if value <= edge), len(BUCKETS) - 1
        )
        yield (
            (
                int(record["at"] // 60) * 60,
                record["owner_id"],
                record["service"],
                record["machine"],
                operation,
                record["outcome"],
                int(record.get("benchmark") is True),
                name,
                bucket,
            ),
            value,
        )


def rollups(connection, records) -> None:
    """Merge a bounded batch before SQL, reducing lock time and CPU without changing counts."""
    combined = {}
    for record in records:
        for key, value in metric_deltas(record):
            entry = combined.setdefault(key, [0, 0.0])
            entry[0] += 1
            entry[1] += value
    if combined:
        connection.executemany(
            "INSERT INTO observation_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(minute,owner_id,service,machine,operation,outcome,benchmark,metric,bucket) "
            "DO UPDATE SET count=observation_metrics.count+excluded.count,total=observation_metrics.total+excluded.total",
            [(*key, count, total) for key, (count, total) in combined.items()],
        )


class Store:
    """A nonblocking, byte-and-count-bounded optional span writer for one service instance."""

    def __init__(self, source, machine: str = "front", forwarded: bool = False):
        """Use the already-migrated log database and the existing signed node forwarding path."""
        self.source, self.machine, self.forwarded = source, machine, forwarded
        self.queue, self.bytes = deque(), 0
        self.lock, self.wake = threading.Lock(), threading.Event()
        self.thread, self.stopping = None, False
        self.dropped, self.written, self.last_write = Counter(), 0, None
        self.active, self.high_water = {}, 0
        self.writing = False
        self.last_prune = 0.0

    def submit(self, record) -> None:
        """Enqueue without waiting for disk; prefer errors when pressure samples successes."""
        if not record:
            return
        encoded = json.dumps(record, separators=(",", ":")).encode()
        with self.lock:
            key = record["trace_id"] + record["id"]
            if record.get("finished", True):
                self.active.pop(key, None)
            if self.stopping:
                self.dropped["shutdown"] += 1
                return
            if len(self.queue) >= MAX_RECORDS or self.bytes + len(encoded) > MAX_BYTES:
                self.dropped["queue_full"] += 1
                return
            if (
                len(self.queue) > MAX_RECORDS * 0.9
                and record.get("outcome") == "success"
            ):
                self.dropped["sampled_success"] += 1
                return
            self.queue.append((record, len(encoded)))
            if not record.get("finished", True) and len(self.active) < MAX_RECORDS:
                self.active[key] = record["at"]
            self.bytes += len(encoded)
            self.high_water = max(self.high_water, self.bytes)
            if self.thread is None:
                self.thread = threading.Thread(
                    target=contextvars.Context().run,
                    args=(self.run,),
                    daemon=True,
                    name="suite-observations",
                )
                self.thread.start()
            if len(self.queue) >= 100:
                self.wake.set()

    def run(self) -> None:
        """Batch writes on an independent connection; failures are counted, never rethrown."""
        connection = None
        wal_spool, checkpointed = False, 0.0
        try:
            while True:
                self.wake.wait(1)
                self.wake.clear()
                with self.lock:
                    batch = [
                        self.queue.popleft() for _ in range(min(16, len(self.queue)))
                    ]
                    self.bytes -= sum(size for _, size in batch)
                    stopping = self.stopping
                    self.writing = bool(batch)
                    if self.queue:
                        self.wake.set()
                    if not batch:
                        self.thread = None
                        return
                if batch:
                    try:
                        if connection is None:
                            connection = (
                                self.source.connect(timeout=0.25)
                                if self.source.engine == "sqlite"
                                else self.source.connect(connect_timeout=1)
                            )
                            if self.source.engine == "postgresql":
                                connection.execute("SET statement_timeout = 250")
                                connection.commit()
                            elif self.forwarded:
                                wal_spool = optional_spool(connection)
                        with connection:
                            connection.execute("BEGIN IMMEDIATE")
                            records = [r for r, _ in batch]
                            if self.forwarded:
                                pending = connection.execute(
                                    "SELECT COUNT(*),COALESCE(SUM(LENGTH(data)),0) FROM observation_spans WHERE sent=0"
                                ).fetchone()
                                if (
                                    pending[0] + len(records) > MAX_RECORDS
                                    or pending[1] + sum(size for _, size in batch)
                                    > MAX_BYTES
                                ):
                                    raise OverflowError(
                                        "Optional forwarding budget reached."
                                    )
                            written = ingest(
                                connection, records, self.machine, self.forwarded
                            )
                            for service in {r["service"] for r in records}:
                                connection.execute(
                                    "INSERT INTO observation_sources VALUES (?,?,?) ON CONFLICT(machine) DO UPDATE SET at=excluded.at,data=excluded.data",
                                    (
                                        self.machine + ":" + service,
                                        time.time(),
                                        json.dumps(
                                            {"process": PROCESS, "service": service}
                                        ),
                                    ),
                                )
                        with self.lock:
                            self.written += written
                            self.last_write = time.time()
                        if wal_spool and time.monotonic() - checkpointed >= 0.1:
                            checkpointed = time.monotonic()
                            try:
                                connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                            except Exception as problem:
                                from loadcoach.observability import diagnostic

                                diagnostic(
                                    "observation_checkpoint_failed",
                                    type(problem).__name__,
                                )
                        if time.monotonic() - self.last_prune >= 3600:
                            self.last_prune = time.monotonic()
                            try:
                                prune(connection, forwarded=self.forwarded)
                            except Exception as problem:
                                from loadcoach.observability import diagnostic

                                diagnostic(
                                    "observation_retention_failed",
                                    type(problem).__name__,
                                )
                    except Exception as problem:
                        with self.lock:
                            self.dropped[
                                "spool_full"
                                if isinstance(problem, OverflowError)
                                else "writer_error"
                            ] += len(batch)
                        if connection:
                            connection.close()
                            connection = None
                        from loadcoach.observability import diagnostic

                        diagnostic("observation_write_failed", type(problem).__name__)
                    finally:
                        with self.lock:
                            self.writing = False
                    time.sleep(
                        0.001
                    )  # Yield between short optional transactions, never while holding a lock.
                if stopping and not self.queue:
                    break
        finally:
            if connection:
                connection.close()

    def flush(self, timeout: float = 2) -> bool:
        """Wait a bounded time for queued writes outside the application event loop."""
        deadline = time.monotonic() + timeout
        self.wake.set()
        while time.monotonic() < deadline:
            with self.lock:
                empty = not self.queue and not self.writing
            if empty:
                return True
            time.sleep(0.005)
        return False

    def close(self) -> None:
        """Stop within two seconds; expose pending optional loss instead of hanging shutdown."""
        with self.lock:
            self.stopping = True
        self.wake.set()
        if self.thread:
            self.thread.join(2)

    def health(self) -> dict:
        """Return bounded process-local observation health with explicit counter identity."""
        with self.lock:
            return {
                "process": PROCESS,
                "recording": True,
                "pending": len(self.queue),
                "bytes": self.bytes,
                "written": self.written,
                "dropped": dict(self.dropped),
                "last_write": self.last_write,
                "high_water_bytes": self.high_water,
                "active": len(self.active),
                "counter_scope": "process lifetime",
            }


def search(
    connection, owner: str | None, filters: dict, limit: int = 100, offset: int = 0
) -> dict:
    """Read indexed, owner-scoped span metadata; callers supply an independent deadline reader."""
    where, values = [], []
    if owner is not None:
        where.append("owner_id=?")
        values.append(owner)
    for name in (
        "request_id",
        "trace_id",
        "service",
        "machine",
        "application",
        "outcome",
        "error_code",
    ):
        value = filters.get(name, "")
        if value:
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("Filter values must be at most 128 characters.")
            where.append(name + "=?")
            values.append(value)
    now = time.time()
    since = float(
        filters.get("since")
        or (0 if filters.get("request_id") or filters.get("trace_id") else now - 3600)
    )
    until = float(filters.get("until") or now)
    if not math.isfinite(since) or not math.isfinite(until) or since > until:
        raise ValueError("Choose a finite increasing time range.")
    where += ["at>=?", "at<=?"]
    values += [since, until]
    clause = " WHERE " + " AND ".join(where)
    total = connection.execute(
        "SELECT COUNT(*) FROM observation_spans" + clause, tuple(values)
    ).fetchone()[0]
    rows = connection.execute(
        "SELECT data,received,finished FROM observation_spans"
        + clause
        + " ORDER BY at,id LIMIT ? OFFSET ?",
        (*values, min(max(limit, 1), 1000), max(offset, 0)),
    ).fetchall()
    result = []
    for data, received, finished in rows:
        row = json.loads(data)
        if not finished:
            source = connection.execute(
                "SELECT data FROM observation_sources WHERE machine=?",
                (row.get("machine", "") + ":" + row["service"],),
            ).fetchone()
            if source is None and row["service"] in ("loadcoach", "adapter"):
                source = connection.execute(
                    "SELECT data FROM observation_sources WHERE machine=?",
                    (row.get("machine", ""),),
                ).fetchone()
            process = json.loads(source[0]).get("process") if source else None
            row["outcome"] = (
                "underway" if process and process == row.get("process") else "unknown"
            )
            row["uncertainty"] = (
                "No ending observed; duration alone does not prove remote work stopped."
            )
        result.append(row)
    return {
        "spans": result,
        "total": total,
        "since": since,
        "until": until,
        "limit": min(max(limit, 1), 1000),
        "truncated": total > offset + len(result),
    }


def prune(connection, now=None, forwarded: bool = False, *,stop=None) -> None:
    """Expire only optional spans/projections; required audit evidence is never touched.

    Spans go after the store's keep_days, and the oldest beyond its max_spans (spans that arrived in
    one batch share an arrival time, so the cap can be passed by up to one batch). A node that forwards
    (`forwarded`) also lets go of what control node has acknowledged after an hour: control node keeps the copy.
    Control node then sweeps out routine polls (`sweep`).
    """
    now = time.time() if now is None else now
    rules = policy(connection)
    with connection:
        connection.execute(
            "DELETE FROM observation_spans WHERE received<?", (now - rules["keep_days"] * 86400,)
        )
        if forwarded:
            connection.execute("DELETE FROM observation_spans WHERE sent=1 AND received<?", (now - SPOOL_SECONDS,))
        else:  # a poll's late spans arrive within minutes; a day of remembered traces is plenty
            connection.execute("DELETE FROM observation_quiet WHERE at<?", (now - 86400,))
        if rules["max_spans"]:
            connection.execute(
                "DELETE FROM observation_spans WHERE received < (SELECT received FROM observation_spans "
                "ORDER BY received DESC LIMIT 1 OFFSET ?)", (rules["max_spans"] - 1,))
        connection.execute(
            "DELETE FROM observation_metrics WHERE minute<?", (now - 30 * 86400,)
        )
    if not forwarded:
        sweep(connection, rules["quiet_routes"], now,stop=stop)


def metrics(connection, owner: str | None, seconds: int = 3600, now=None) -> dict:
    """Merge fixed histogram counts across machines; percentiles are bucket upper bounds."""
    now = time.time() if now is None else now
    until = int(now // 60) * 60 + 60
    since = max(0, int((now - min(max(seconds, 60), 30 * 86400)) // 60) * 60)
    where, values = (" AND owner_id=?", (owner,)) if owner is not None else ("", ())
    rows = connection.execute(
        "SELECT service,machine,operation,outcome,metric,bucket,SUM(count),SUM(total) "
        "FROM observation_metrics WHERE minute>=? AND minute<? AND benchmark=0"
        + where
        + " GROUP BY service,machine,operation,outcome,metric,bucket",
        (since, until, *values),
    ).fetchall()
    groups = {}
    for service, machine, operation, outcome, metric, bucket, count, total in rows:
        # PostgreSQL sums a BIGINT as NUMERIC, which arrives as a Decimal: JSON cannot carry it, nor float arithmetic mix with it.
        count, total = int(count or 0), float(total or 0)
        key = (service, operation, metric)
        group = groups.setdefault(
            key,
            {
                "service": service,
                "operation": operation,
                "metric": metric,
                "count": 0,
                "total": 0.0,
                "outcomes": {},
                "histogram": [0] * len(BUCKETS),
            },
        )
        group["count"] += count
        group["total"] += total
        group["outcomes"][outcome] = group["outcomes"].get(outcome, 0) + count
        group["histogram"][bucket] += count
    for group in groups.values():
        group["per_second"] = (
            group["total"]
            if group["metric"]
            in ("prompt_tokens", "answer_tokens", "fallbacks", "inference_dispatches")
            else group["count"]
        ) / max(1, until - since)
        group["error_percent"] = (
            100 * group["outcomes"].get("error", 0) / max(1, group["count"])
        )
        group["cancelled_percent"] = (
            100 * group["outcomes"].get("cancelled", 0) / max(1, group["count"])
        )
        for percentile in (50, 95):
            target = math.ceil(group["count"] * percentile / 100)
            cumulative = 0
            group["p" + str(percentile)] = None
            for count, edge in zip(group["histogram"], BUCKETS):
                cumulative += count
                if cumulative >= target:
                    group["p" + str(percentile)] = edge if math.isfinite(edge) else None
                    break
    return {
        "since": since,
        "until": until,
        "groups": list(groups.values()),
        "benchmark_excluded": True,
        "basis": "Minute-aligned windows; histogram percentile upper bounds; optional loss/sampling may reduce coverage.",
    }


def sources(connection) -> list[dict]:
    """Read node-reported observation health; stale sources never imply recovery."""
    return [
        {
            "machine": machine,
            "received": at,
            "fresh": time.time() - at < SOURCE_FRESH_SECONDS
            and json.loads(data).get("recording") is True,
            **json.loads(data),
        }
        for machine, at, data in connection.execute(
            "SELECT machine,at,data FROM observation_sources"
        )
        if "service" not in json.loads(data)
    ]


def tree(rows) -> list[dict]:
    """Order explicit parents before children with bounded cycle detection and visible gaps."""
    records = {(r["trace_id"], r["id"]): dict(r) for r in rows}
    ordered, emitted = [], set()

    def add(key, visiting):
        """Walk at most 32 parents; externally supplied trace parents are never authority."""
        if key in emitted:
            return
        row = records[key]
        parent = (row["trace_id"], row.get("parent_id"))
        cycle = key in visiting or len(visiting) >= 32
        linked = (
            parent in records
            and not row.get("ingress")
            and records[parent].get("owner_id") == row.get("owner_id")
        )
        if linked and not cycle:
            add(parent, visiting | {key})
        if key in emitted:
            return
        row["depth"] = (
            min(32, records[parent].get("depth", -1) + 1) if linked and not cycle else 0
        )
        row["parent_unavailable"] = bool(row.get("parent_id") and not linked) or cycle
        emitted.add(key)
        ordered.append(row)

    for key in sorted(records, key=lambda k: (records[k]["at"], k)):
        add(key, set())
    return ordered
