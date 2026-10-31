"""SQLite implementation of openWeightGym's database driver contract."""

from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path


class SQLiteDriver:
    """Open local SQLite stores and provide SQLite-specific primitives."""

    engine = "sqlite"
    placeholder = "?"

    @staticmethod
    def is_error(problem: BaseException) -> bool:
        """Return whether the exception belongs to this database driver."""
        return isinstance(problem, sqlite3.Error)

    def connect(self, store, **options):
        """Open SQLite without changing DB-API defaults not selected by the caller."""
        target = store.path
        if target != ":memory:" and not target.startswith("file:"):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(target, **{**store.options, **options})

    def quote_identifier(self, name: str) -> str:
        """Quote an SQLite identifier."""
        return '"' + name.replace('"', '""') + '"'

    def tables(self, connection) -> list[str]:
        """List application tables in stable order."""
        rows = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row[0] for row in rows]

    def json(self, value):
        """Encode a stable compact JSON value for SQLite text storage."""
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    def upsert_sql(self, database, table: str, columns: list[str], conflict: list[str], update: list[str]) -> str:
        """Build SQLite's ON CONFLICT form behind the shared contract."""
        quote = database.quote_identifier
        inserted = ", ".join(quote(name) for name in columns)
        collided = ", ".join(quote(name) for name in conflict)
        changed = ", ".join(f"{quote(name)} = excluded.{quote(name)}" for name in update)
        action = f"DO UPDATE SET {changed}" if changed else "DO NOTHING"
        return f"INSERT INTO {quote(table)} ({inserted}) VALUES ({database.placeholders(len(columns))}) ON CONFLICT ({collided}) {action}"

    @contextmanager
    def migration_lock(self, connection, name: str):
        """Serialize migrations with SQLite's immediate write transaction."""
        del name
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
