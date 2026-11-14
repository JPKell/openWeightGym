import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from starlette.testclient import TestClient

from loadcoach.app import build_app
from loadcoach.config import Config
from loadcoach.database_admin import DatabaseSource, catalogs, configured_sources, make_sql_dump, sqlite_sources
from db import Database, DatabaseRegistry
from db.health import backup_signal, stores_health
from db.recovery import RecoveryStore


def test_online_sqlite_backup_counts_match_the_copied_snapshot_during_a_write(tmp_path, monkeypatch):
    from db import recovery
    source = tmp_path / 'live.db'
    with sqlite3.connect(source) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE items(id INTEGER PRIMARY KEY)')
        connection.execute('INSERT INTO items VALUES(1)')
    connection.close()
    metadata = recovery._sqlite_metadata
    written = False
    def write_after_metadata(connection, path):
        nonlocal written
        result = metadata(connection, path)
        if path == source and not written:
            written = True
            with sqlite3.connect(source) as writer:
                writer.execute('INSERT INTO items VALUES(2)')
            writer.close()
        return result
    monkeypatch.setattr(recovery, '_sqlite_metadata', write_after_metadata)
    record = recovery.backup_store(RecoveryStore('core', Database.sqlite(source), ('loadcoach.core',)), tmp_path / 'payload')
    assert written and record['verification']['counts'] == {'items': 1}
    assert recovery.sqlite_metadata(source)['counts'] == {'items': 2}


def sample(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        db.executescript(
            """
            CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, payload BLOB, note TEXT);
            CREATE INDEX items_name ON items(name);
            INSERT INTO items VALUES (1, 'one', X'00ff', NULL);
            INSERT INTO items VALUES (2, 'two''s', X'', 'line one\nline two');
            """
        )
        db.commit()
    finally:
        db.close()


def test_sqlite_catalog_and_exact_table_dump_round_trip(tmp_path):
    path = tmp_path / "example.db"
    sample(path)
    source = DatabaseSource("example.db", path, "test data")

    listed = catalogs({source.id: source}, "front")[0]
    assert listed["engine"] == "SQLite" and listed["machine"] == "front"
    assert listed["size"] >= path.stat().st_size
    assert listed["tables"] == [
        {"name": "items", "records": 2, "size": listed["tables"][0]["size"], "indexes": 1,
         "schema": "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, payload BLOB, note TEXT)"}
    ]
    assert listed["tables"][0]["size"] > 0

    dump, method = make_sql_dump(source, "items")
    try:
        restored = sqlite3.connect(tmp_path / "restored.db")
        restored.executescript(dump.read_text())
        assert restored.execute("SELECT * FROM items ORDER BY id").fetchall() == [
            (1, "one", b"\x00\xff", None),
            (2, "two's", b"", "line one\nline two"),
        ]
        assert restored.execute("SELECT name FROM sqlite_schema WHERE type='index'").fetchone()[0] == "items_name"
        restored.close()
        assert method in {"sqlite3 command", "Python sqlite3 fallback"}
    finally:
        dump.unlink(missing_ok=True)

    complete, _ = make_sql_dump(source)
    try:
        restored = sqlite3.connect(tmp_path / "complete.db")
        restored.executescript(complete.read_text())
        assert restored.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
        restored.close()
    finally:
        complete.unlink(missing_ok=True)


def test_discovery_ignores_links_and_database_endpoint_exports_sql(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    primary = data / "loadcoach.db"
    with TestClient(build_app(Config(database=str(primary)))) as client:
        linked = data / "linked.db"
        linked.symlink_to(primary.name)
        sources = sqlite_sources(data, {"loadcoach.db": "primary"}, primary)
        assert set(sources) == {"loadcoach.db"}

        listed = client.get("/loadcoach/databases")
        dumped = client.get("/loadcoach/databases/loadcoach.db/dump?table=models")
        missing = client.get("/loadcoach/databases/loadcoach.db/dump?table=not_a_table")

    assert listed.status_code == 200
    database = listed.json()["databases"][0]
    assert database["name"] == "loadcoach.db"
    assert any(table["name"] == "models" for table in database["tables"])  # the users are the control node's
    assert dumped.status_code == 200 and "CREATE TABLE models" in dumped.text
    assert dumped.headers["content-disposition"].endswith('filename="loadcoach-models.sql"')
    assert missing.status_code == 404


def test_postgresql_binding_does_not_discover_a_nonexistent_sqlite_fallback(tmp_path):
    settings = tmp_path / 'settings'
    settings.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    fallback = settings / '../data/accounts.db'
    registry = DatabaseRegistry.from_config({
        'stores': {'accounts': {'engine': 'postgresql', 'dsn_file': 'accounts.dsn'}},
        'bindings': {'weightroom.accounts': 'accounts'},
    }, settings, {'weightroom.accounts': fallback})

    sources = configured_sources(registry, data, {'accounts.db': 'Accounts'})
    assert set(sources) == {'accounts'}
    assert sources['accounts'].expected and sources['accounts'].database.engine == 'postgresql'
    assert not fallback.exists()

    # A real leftover remains available for explicit inspection/export.
    sample(data / 'accounts.db')
    sources = configured_sources(registry, data, {'accounts.db': 'Accounts'})
    assert set(sources) == {'accounts', 'accounts.db'}
    assert not sources['accounts.db'].expected
    assert sources['accounts.db'].path == (data / 'accounts.db').resolve()


def test_unused_store_settings_are_not_discovered_files_but_active_missing_stores_remain(tmp_path):
    missing = tmp_path / 'missing.db'
    registry = DatabaseRegistry.from_config({'stores': {
        'unused_sqlite': {'engine': 'sqlite', 'path': 'not-created.db'},
        'unused_postgresql': {'engine': 'postgresql', 'dsn_file': 'unused.dsn'},
    }}, tmp_path, {'weightroom.chat': missing})
    sources = configured_sources(registry, tmp_path, {})
    assert set(sources) == {'missing.db'}
    assert sources['missing.db'].expected
    assert not missing.exists()


def test_health_is_read_only_and_keeps_missing_and_corrupt_stores_visible(tmp_path):
    healthy, missing, corrupt = (tmp_path / name for name in ("healthy.db", "missing.db", "corrupt.db"))
    sample(healthy)
    corrupt.write_bytes(b"not a database")
    before = healthy.read_bytes()
    stores = [RecoveryStore(path.stem, Database.sqlite(path), ("weightroom.chat",))
              for path in (healthy, missing, corrupt)]
    rows = stores_health(stores, "front", now=100000)
    assert [row["state"] for row in rows] == ["reachable", "unavailable", "unavailable"]
    assert all(row["backup"]["state"] == "missing" for row in rows)
    assert rows[0]["services"] == ["weightroom"]
    assert healthy.read_bytes() == before and not missing.exists()


def test_backup_age_rejects_failed_future_and_rebound_store_evidence(tmp_path):
    from copy import deepcopy

    store = RecoveryStore("chat", Database.sqlite(tmp_path / "chat.db"), ("weightroom.chat",))
    now = 200000
    record = {"name": "chat", "engine": "sqlite", "display_name": str(store.database.path),
              "bindings": ["weightroom.chat"], "verification": {"state": "verified"}}
    good = {"schema": 2, "id": "good", "created_at": datetime.fromtimestamp(now - 90, UTC).isoformat(),
            "archive_size": 500, "verification": {"state": "verified"}, "stores": [record]}
    assert backup_signal(store, [good], now)["state"] == "fresh"
    equivalent = deepcopy(good)
    equivalent['stores'][0]['display_name'] = str(store.database.path.parent / 'settings' / '..' / store.database.path.name)
    assert backup_signal(store, [equivalent], now)['state'] == 'fresh'
    stale = deepcopy(good)
    stale["created_at"] = datetime.fromtimestamp(now - 86401, UTC).isoformat()
    assert backup_signal(store, [stale], now)["state"] == "stale"
    assert backup_signal(store, [stale, good], now)["id"] == "good"
    for key, value in (("engine", "postgresql"), ("display_name", "/different/chat.db"),
                       ("name", "other"), ("bindings", []), ("verification", {"state": "failed"})):
        changed = deepcopy(good)
        changed["stores"][0][key] = value
        assert backup_signal(store, [changed], now)["state"] == "missing"
    for key, value in (("created_at", "invalid"), ("created_at", "1970-01-03T00:00:00"),
                       ("created_at", datetime.fromtimestamp(now + 1, UTC).isoformat()),
                       ("archive_size", 0), ("schema", 1), ("verification", {"state": "failed"})):
        changed = {**good, key: value}
        assert backup_signal(store, [changed], now)["state"] == "missing"
    assert backup_signal(store, [None, {**good, "verification": "broken"}, {**good, "stores": [None]},
                               {**good, "stores": [{**record, "bindings": [None]}]}], now)["state"] == "missing"


def test_failed_postgresql_probe_never_exposes_driver_credentials(tmp_path, monkeypatch):
    target = Database.postgresql(tmp_path / "private.dsn", "broken")
    def fail(**options):
        raise RuntimeError("password=secret-value postgres://user:secret-value@host/db")
    monkeypatch.setattr(target, "connect", fail)
    row = stores_health([RecoveryStore("broken", target, ("loadcoach.core",))], "compute")[0]
    assert row["state"] == "unavailable" and row["backup"]["state"] == "unknown"
    assert "secret-value" not in str(row)


def test_health_endpoint_avoids_full_catalog_scans(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Health must not count application rows")
    monkeypatch.setattr("loadcoach.app.database_catalogs", forbidden)
    with TestClient(build_app(Config(database=str(tmp_path / "core.db")))) as client:
        result = client.get("/loadcoach/databases/health")
    assert result.status_code == 200
    assert result.json()["stores"][0]["state"] == "reachable"
