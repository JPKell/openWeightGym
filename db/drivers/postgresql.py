"""PostgreSQL implementation loaded only for configured PostgreSQL stores."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ..database import DatabaseConfigurationError, DriverUnavailable


class PostgreSQLDriver:
    """Open psycopg connections while keeping DSNs out of ordinary configuration."""

    engine = "postgresql"
    placeholder = "%s"

    @staticmethod
    def is_error(problem: BaseException) -> bool:
        """Return whether the exception belongs to this database driver."""
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(problem, psycopg.Error)

    def connect(self, store, **options):
        """Read an owner-only DSN file at connection time and open it with psycopg."""
        try:
            import psycopg
        except ImportError:
            raise DriverUnavailable(
                "PostgreSQL store configured, but psycopg is not installed; install openWeightGym's postgresql extra"
            ) from None
        secret = Path(store.dsn_file)
        try:
            mode = secret.stat().st_mode
            if mode & 0o077:
                raise DatabaseConfigurationError(f"{secret}: PostgreSQL DSN file must be owner-readable only")
            dsn = secret.read_text().strip()
        except OSError as problem:
            raise DatabaseConfigurationError(f"{secret}: PostgreSQL DSN file cannot be read: {problem}") from None
        if not dsn:
            raise DatabaseConfigurationError(f"{secret}: PostgreSQL DSN file is empty")
        selected = {**store.options, **options}
        isolation = selected.pop("isolation_level", object())
        clean = {key: value for key, value in selected.items() if key not in {"check_same_thread", "uri"}}
        connection = psycopg.connect(dsn, **clean)
        from psycopg.types.json import set_json_loads

        # Repositories share the same serialized-JSON contract on both engines. PostgreSQL
        # still stores/indexes JSONB, while returning its canonical JSON text to callers.
        set_json_loads(lambda value: value.decode() if isinstance(value, bytes) else value, connection)
        if isolation is None:
            connection.autocommit = True
        return connection

    def quote_identifier(self, name: str) -> str:
        """Quote a PostgreSQL identifier without requiring a live connection."""
        return '"' + name.replace('"', '""') + '"'

    def tables(self, connection) -> list[str]:
        """List application tables on the active PostgreSQL search path."""
        rows = connection.execute(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = ANY (current_schemas(false)) ORDER BY tablename"
        )
        return [row[0] for row in rows]

    def json(self, value):
        """Wrap JSON for psycopg without importing it for SQLite-only installations."""
        try:
            from psycopg.types.json import Jsonb
        except ImportError:
            raise DriverUnavailable(
                "PostgreSQL store configured, but psycopg is not installed; install openWeightGym's postgresql extra"
            ) from None
        return Jsonb(value)

    def upsert_sql(self, database, table: str, columns: list[str], conflict: list[str], update: list[str]) -> str:
        """Build PostgreSQL's ON CONFLICT form behind the shared contract."""
        quote = database.quote_identifier
        inserted = ", ".join(quote(name) for name in columns)
        collided = ", ".join(quote(name) for name in conflict)
        changed = ", ".join(f"{quote(name)} = excluded.{quote(name)}" for name in update)
        action = f"DO UPDATE SET {changed}" if changed else "DO NOTHING"
        return f"INSERT INTO {quote(table)} ({inserted}) VALUES ({database.placeholders(len(columns))}) ON CONFLICT ({collided}) {action}"

    @contextmanager
    def migration_lock(self, connection, name: str):
        """Hold a session advisory lock for one logical migration stream."""
        was_autocommit = connection.autocommit
        if was_autocommit:
            connection.autocommit = False
        locked = False
        try:
            connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (name,))
            locked = True
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if locked:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))
                connection.commit()
            if was_autocommit:
                connection.autocommit = True
