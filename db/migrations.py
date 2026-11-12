"""Versioned, transactional schema migrations shared by openWeightGym repositories."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .database import Database

MigrationBody = str | Callable[[Database, object], None]


@dataclass(frozen=True)
class Migration:
    """One ordered schema migration and its SQL or callable body."""
    version: int
    name: str
    sqlite: MigrationBody
    postgresql: MigrationBody | None = None
    formerly: str | None = None  # the name this migration was recorded under before it was renamed

    def body(self, engine: str) -> MigrationBody:
        """Return the engine-specific migration SQL or callable body."""
        return self.postgresql if engine == "postgresql" and self.postgresql is not None else self.sqlite


def columns(database: Database, connection, table: str) -> set[str]:
    """Return the columns of one existing table through engine catalogues."""
    if database.engine == "sqlite":
        return {row[1] for row in connection.execute(f"PRAGMA table_info({database.quote_identifier(table)})")}
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ANY (current_schemas(false)) AND table_name = ?",
        (table,),
    )
    return {row[0] for row in rows}


def add_column(database: Database, connection, table: str, column: str, definition: str) -> None:
    """Add a missing column using the active engine's schema metadata."""
    if column not in columns(database, connection, table):
        connection.execute(
            f"ALTER TABLE {database.quote_identifier(table)} ADD COLUMN "
            f"{database.quote_identifier(column)} {definition}"
        )


# Streams recorded under an earlier key: new key -> former key. A store still recorded under the
# former key has its history moved to the new key before anything is applied, so finished
# migrations are not run again (docs/peer-nodes-plan.md, Phase 1).
RENAMED_STREAMS = {"turnstile.benchmark_tasks": "turnstile.tasks"}


def run(database: Database, connection, stream: str, migrations: Iterable[Migration]) -> None:
    """Apply missing migrations in order while holding the engine's migration lock.

    History recorded under a stream's former key (RENAMED_STREAMS) or a migration's former name
    (`Migration.formerly`) is moved to the current key and name first.
    """
    ordered = list(migrations)
    versions = [migration.version for migration in ordered]
    if versions != sorted(set(versions)) or any(version < 1 for version in versions):
        raise ValueError(f"{stream}: migration versions must be unique positive numbers in order")
    with database.migration_lock(connection, stream):
        connection.execute(
            "CREATE TABLE IF NOT EXISTS suite_schema_migrations ("
            "stream TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, applied DOUBLE PRECISION NOT NULL, "
            "PRIMARY KEY(stream, version))"
        )
        former = RENAMED_STREAMS.get(stream)
        if former and not connection.execute(
                "SELECT 1 FROM suite_schema_migrations WHERE stream = ?", (stream,)).fetchone():
            connection.execute("UPDATE suite_schema_migrations SET stream = ? WHERE stream = ?", (stream, former))
        applied = {version: name for version, name in connection.execute(
            "SELECT version, name FROM suite_schema_migrations WHERE stream = ?", (stream,)
        )}
        for migration in ordered:
            if migration.version in applied and migration.formerly and applied[migration.version] == migration.formerly:
                connection.execute("UPDATE suite_schema_migrations SET name = ? WHERE stream = ? AND version = ?",
                                   (migration.name, stream, migration.version))
                applied[migration.version] = migration.name
            if migration.version in applied:
                if applied[migration.version] != migration.name:
                    raise RuntimeError(
                        f"{stream} migration {migration.version} was {applied[migration.version]!r}, "
                        f"not {migration.name!r}"
                    )
                continue
            body = migration.body(database.engine)
            if callable(body):
                body(database, connection)
            else:
                connection.executescript(body)
            connection.execute(
                "INSERT INTO suite_schema_migrations(stream, version, name, applied) VALUES (?, ?, ?, ?)",
                (stream, migration.version, migration.name, time.time()),
            )


def version(connection, stream: str) -> int:
    """Return the current schema version of one migration stream."""
    try:
        row = connection.execute(
            "SELECT MAX(version) FROM suite_schema_migrations WHERE stream = ?", (stream,)
        ).fetchone()
    except Exception:
        return 0
    return int(row[0] or 0)
