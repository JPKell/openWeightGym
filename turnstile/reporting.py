"""Bounded, independent readers for front-owned event and benchmark reports.

The writer connection belongs to turnstile's event loop. A report gets its own
read-only connection in a worker thread so an expensive query cannot pause model
traffic or hold up log ingestion.
"""

from __future__ import annotations

import asyncio
import copy
import time
from contextlib import contextmanager

from db import Database
REPORT_SECONDS = 5.0


class ReportTimeout(Exception):
    """A report exceeded its independent read deadline."""


@contextmanager
def reader(store, deadline: float):
    """Open an independent read-only report connection with a statement deadline."""
    source = store.db.database
    if source.engine == "sqlite":
        if path := source.path:
            target = Database.sqlite(path.resolve().as_uri() + "?mode=ro")
            connection = target.connect(uri=True, isolation_level=None, timeout=1)
        else:
            # A disposable in-memory test store needs an independent snapshot too.
            connection = Database.sqlite(":memory:").connect(isolation_level=None)
            store.db.raw.backup(connection.raw)
        connection.execute("PRAGMA query_only = ON")
        connection.raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    else:
        connection = source.connect(isolation_level=None, connect_timeout=2)
        connection.execute("SET default_transaction_read_only = on")
        connection.execute(f"SET statement_timeout = {int(max(1, (deadline - time.monotonic()) * 1000))}")
    view = copy.copy(store)
    view.db = connection
    try:
        yield view
    finally:
        connection.close()


async def run(store, work, limit: float = REPORT_SECONDS):
    """Run a pure read with a wall-clock and SQL deadline; never use the writer connection."""
    deadline = time.monotonic() + limit

    def perform():
        """Run the report and close its private connection after success or failure."""
        with reader(store, deadline) as view:
            return work(view)

    try:
        return await asyncio.wait_for(asyncio.to_thread(perform), timeout=limit)
    except TimeoutError as problem:
        raise ReportTimeout from problem
    except Exception as problem:
        # PostgreSQL's millisecond statement timeout can fire just before the Python
        # deadline. SQLite's progress handler reports an interrupted query.
        timed_out = (time.monotonic() >= deadline - 0.05 or getattr(problem, "sqlstate", None) == "57014"
                     or "interrupted" in str(problem).lower())
        if timed_out and store.db.database.is_error(problem):
            raise ReportTimeout from problem
        raise
