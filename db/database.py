"""The small database facade used by openWeightGym repositories and maintenance tools."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, TypeAlias


class DatabaseConfigurationError(ValueError):
    """A database store or binding cannot be used safely."""


class DriverUnavailable(RuntimeError):
    """A configured database driver is not installed in this openWeightGym environment."""


@dataclass(frozen=True)
class Store:
    """One named database endpoint without embedding credentials in normal settings."""

    name: str
    engine: str
    path: str = ""
    dsn_file: str = ""
    display_name: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate store engine and location invariants after configuration construction."""
        engine = self.engine.lower().strip()
        object.__setattr__(self, "engine", engine)
        if engine not in {"sqlite", "postgresql"}:
            raise DatabaseConfigurationError(f"store {self.name!r}: unsupported database engine {self.engine!r}")
        if engine == "sqlite" and (not self.path or self.dsn_file):
            raise DatabaseConfigurationError(f"store {self.name!r}: SQLite needs path and cannot use dsn_file")
        if engine == "postgresql" and (not self.dsn_file or self.path):
            raise DatabaseConfigurationError(f"store {self.name!r}: PostgreSQL needs dsn_file and cannot use path")
        if not isinstance(self.options, dict):
            raise DatabaseConfigurationError(f"store {self.name!r}: options must be a table")


class Database:
    """A configured store that opens connections through its engine driver."""

    def __init__(self, store: Store) -> None:
        """Bind a validated store definition to its selected database driver."""
        self.store = store
        if store.engine == "sqlite":
            from .drivers.sqlite import SQLiteDriver

            self.driver = SQLiteDriver()
        elif store.engine == "postgresql":
            from .drivers.postgresql import PostgreSQLDriver

            self.driver = PostgreSQLDriver()
        else:  # Store validates this, but keep the boundary closed for constructed subclasses.
            raise DatabaseConfigurationError(f"unsupported database engine {store.engine!r}")

    @classmethod
    def sqlite(cls, path: str | Path, name: str = "sqlite") -> Database:
        """Make a SQLite database target for legacy path-based callers."""
        return cls(Store(name=name, engine="sqlite", path=str(path)))

    @classmethod
    def postgresql(cls, dsn_file: str | Path, name: str = "postgresql", display_name: str = "") -> Database:
        """Make a PostgreSQL target whose secret DSN is read only when connecting."""
        return cls(Store(name=name, engine="postgresql", dsn_file=str(dsn_file), display_name=display_name))

    @property
    def name(self) -> str:
        """Return the non-secret logical store name."""
        return self.store.name

    @property
    def engine(self) -> str:
        """Return the selected driver name."""
        return self.store.engine

    @property
    def display_name(self) -> str:
        """Return safe connection text for logs and management pages."""
        return self.store.display_name or self.store.name

    @property
    def path(self) -> Path | None:
        """Return a SQLite filesystem path, or None for memory, URI and server stores."""
        if self.engine != "sqlite" or self.store.path == ":memory:" or self.store.path.startswith("file:"):
            return None
        return Path(self.store.path)

    def connect(self, **options):
        """Open one DB-API connection using store defaults plus call-specific options."""
        from .connection import Connection

        return Connection(self, self.driver.connect(self.store, **options))

    @contextmanager
    def connection(self, **options) -> Iterator[Any]:
        """Open and always close a connection whose transaction is caller-managed."""
        connection = self.connect(**options)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, **options) -> Iterator[Any]:
        """Open, commit or roll back, and close one transaction-owned connection."""
        connection = self.connect(**options)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def placeholders(self, count: int) -> str:
        """Return the driver's comma-separated parameter placeholders."""
        if count < 0:
            raise ValueError("placeholder count cannot be negative")
        return ", ".join(self.driver.placeholder for _ in range(count))

    def quote_identifier(self, name: str) -> str:
        """Quote one trusted schema identifier through the selected driver."""
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("database identifiers must be non-empty text without NUL bytes")
        return self.driver.quote_identifier(name)

    def json(self, value: Any):
        """Adapt a Python JSON value for a parameter sent through this driver."""
        return self.driver.json(value)

    def upsert_sql(self, table: str, columns: list[str], conflict: list[str], update: list[str]) -> str:
        """Build a safely quoted parameterized upsert for the selected engine."""
        if not columns or not conflict or any(name not in columns for name in [*conflict, *update]):
            raise ValueError("upsert columns must include non-empty conflict and update selections")
        return self.driver.upsert_sql(self, table, columns, conflict, update)

    def tables(self, connection) -> list[str]:
        """List user tables through driver-specific schema inspection."""
        return self.driver.tables(connection)

    def migration_lock(self, connection, name: str):
        """Hold the driver's exclusive schema-migration lock."""
        return self.driver.migration_lock(connection, name)

    def is_error(self, problem: BaseException) -> bool:
        """Return whether an exception came from the selected DB-API driver."""
        return self.driver.is_error(problem)


DatabaseSource: TypeAlias = Database | str | Path


def database(source: DatabaseSource) -> Database:
    """Normalize a legacy SQLite path or an existing Database facade."""
    return source if isinstance(source, Database) else Database.sqlite(source)


def connect(source: DatabaseSource, **options):
    """Open a connection without letting repositories import an engine module."""
    return database(source).connect(**options)


def local_path(source: DatabaseSource) -> Path | None:
    """Return the local SQLite path represented by a source, when it has one."""
    return database(source).path


def is_memory(source: DatabaseSource) -> bool:
    """Return whether a source opens an independent in-memory SQLite database."""
    target = database(source)
    return target.engine == "sqlite" and target.store.path == ":memory:"


def adopt(connection):
    """Wrap a caller-supplied legacy SQLite connection in openWeightGym's connection contract."""
    from .connection import Connection

    return connection if isinstance(connection, Connection) else Connection(Database.sqlite(":memory:", "supplied"), connection)
