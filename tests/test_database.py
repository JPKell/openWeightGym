"""Contract checks for engine-neutral stores and service bindings."""

from __future__ import annotations

import sqlite3

import pytest

from db import Database, DatabaseConfigurationError, DatabaseRegistry, DriverUnavailable, Migration, migrate, schema_version
from loadcoach.config import kept_by_loadcoach


def test_sqlite_facade_owns_connections_transactions_and_schema(tmp_path):
    database = Database.sqlite(tmp_path / "one.db", "one")
    with database.transaction() as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO items VALUES (?, ?)", (1, "kept"))

    with pytest.raises(RuntimeError), database.transaction() as connection:
        connection.execute("INSERT INTO items VALUES (?, ?)", (2, "rolled back"))
        raise RuntimeError("stop")

    with database.connection() as connection:
        assert connection.execute("SELECT * FROM items").fetchall() == [(1, "kept")]
        assert database.tables(connection) == ["items"]
    assert database.placeholders(3) == "?, ?, ?"
    assert database.quote_identifier('odd"name') == '"odd""name"'
    assert database.json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_sqlite_migration_lock_and_upsert_are_driver_operations(tmp_path):
    database = Database.sqlite(tmp_path / "migration.db")
    with database.connection() as connection:
        with database.migration_lock(connection, "core"):
            connection.execute("CREATE TABLE values_by_name (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        sql = database.upsert_sql("values_by_name", ["name", "value"], ["name"], ["value"])
        with connection:
            connection.execute(sql, ("one", 1))
            connection.execute(sql, ("one", 2))
        assert connection.execute("SELECT value FROM values_by_name WHERE name = ?", ("one",)).fetchone() == (2,)


def test_registry_binds_independent_services_to_independent_stores(tmp_path):
    registry = DatabaseRegistry.from_config(
        {
            "stores": {
                "chat": {"engine": "sqlite", "path": "chat.db"},
                "metrics": {"engine": "sqlite", "path": "metrics.db"},
            },
            "bindings": {"weightroom.chat": "chat", "spotter.history": "metrics"},
        },
        tmp_path,
    )
    with registry.database("weightroom.chat").transaction() as connection:
        connection.execute("CREATE TABLE chat_only (id INTEGER)")
    with registry.database("spotter.history").transaction() as connection:
        connection.execute("CREATE TABLE metrics_only (id INTEGER)")

    assert sqlite3.connect(tmp_path / "chat.db").execute(
        "SELECT name FROM sqlite_schema WHERE name = 'chat_only'"
    ).fetchone()
    assert sqlite3.connect(tmp_path / "metrics.db").execute(
        "SELECT name FROM sqlite_schema WHERE name = 'metrics_only'"
    ).fetchone()
    assert registry.stores() == [
        {"name": "chat", "engine": "sqlite", "display_name": "chat", "path": str(tmp_path / "chat.db"), "bindings": ["weightroom.chat"]},
        {"name": "metrics", "engine": "sqlite", "display_name": "metrics", "path": str(tmp_path / "metrics.db"), "bindings": ["spotter.history"]},
    ]


def test_registry_keeps_legacy_defaults_and_rejects_unsafe_configuration(tmp_path):
    legacy = DatabaseRegistry.from_config(None, tmp_path, {"loadcoach.core": tmp_path / "loadcoach.db"})
    assert legacy.database("loadcoach.core").path == tmp_path / "loadcoach.db"

    with pytest.raises(DatabaseConfigurationError, match="unknown store"):
        DatabaseRegistry.from_config(
            {"stores": {}, "bindings": {"loadcoach.core": "missing"}}, tmp_path
        )
    with pytest.raises(DatabaseConfigurationError, match="local path"):
        DatabaseRegistry.from_config(
            {
                "stores": {"bad": {"engine": "sqlite", "path": "https://server/database"}},
                "bindings": {"loadcoach.core": "bad"},
            },
            tmp_path,
        )
    with pytest.raises(DatabaseConfigurationError, match="dsn_file"):
        DatabaseRegistry.from_config(
            {
                "stores": {"bad": {"engine": "postgresql", "dsn_file": "safe.dsn", "options": {"password": "leaked"}}},
                "bindings": {"loadcoach.core": "bad"},
            },
            tmp_path,
        )


def test_postgresql_driver_is_lazy_and_never_exposes_the_dsn(tmp_path, monkeypatch):
    secret = tmp_path / "postgres.dsn"
    secret.write_text("postgresql://private-user:private-password@db/suite\n")
    secret.chmod(0o600)
    database = Database.postgresql(secret, "core", "Main PostgreSQL")

    assert database.engine == "postgresql"
    assert database.display_name == "Main PostgreSQL"
    assert "private-password" not in repr(database.__dict__)
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)
    with pytest.raises(DriverUnavailable, match="postgresql extra"):
        database.connect()


def test_loadcoach_resolves_a_named_core_store_from_machine_configuration(tmp_path):
    settings = tmp_path / "loadcoach.toml"
    settings.write_text(
        """
data = "runtime"

[databases.stores.primary]
engine = "sqlite"
path = "stores/core.db"

[databases.bindings]
"loadcoach.core" = "primary"
"""
    )

    _, source, token = kept_by_loadcoach(settings)
    assert isinstance(source, Database)
    assert source.path == tmp_path / "stores/core.db"
    assert token == str(tmp_path / "runtime/loadcoach.cli-token")


def test_versioned_migrations_are_idempotent_transactional_and_detect_drift(tmp_path):
    database = Database.sqlite(tmp_path / "migrations.db")
    connection = database.connect()
    migrations = (
        Migration(1, "create items", "CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT NOT NULL);"),
        Migration(2, "seed items", "INSERT INTO items VALUES(1, 'kept');"),
    )
    migrate(database, connection, "test.items", migrations)
    migrate(database, connection, "test.items", migrations)
    assert schema_version(connection, "test.items") == 2
    assert connection.execute("SELECT * FROM items").fetchall() == [(1, "kept")]

    def broken(_database, db):
        db.execute("INSERT INTO items VALUES(2, 'rolled back')")
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        migrate(database, connection, "test.items", (*migrations, Migration(3, "broken", broken)))
    assert schema_version(connection, "test.items") == 2
    assert connection.execute("SELECT id FROM items ORDER BY id").fetchall() == [(1,)]
    with pytest.raises(RuntimeError, match="was 'seed items'"):
        migrate(database, connection, "test.items", (migrations[0], Migration(2, "changed name", "SELECT 1;")))
