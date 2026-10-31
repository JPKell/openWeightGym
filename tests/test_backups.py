import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from loadcoach.app import build_app
from loadcoach.backups import (
    BackupFile,
    compute_plan,
    create_backup,
    front_plan,
    inventory,
    manifest,
    manifests,
    new_id,
)
from loadcoach.config import Config
from db import Database
from db.recovery import RecoveryStore, inspect_store


def database(path: Path, tables: dict[str, int]) -> None:
    db = sqlite3.connect(path)
    try:
        for name, count in tables.items():
            db.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY)')
            db.executemany(f'INSERT INTO "{name}" DEFAULT VALUES', [()] * count)
        db.commit()
    finally:
        db.close()


def test_backup_is_manifested_owner_only_and_restored_in_isolation(tmp_path):
    data, settings, plugins = tmp_path / "data", tmp_path / "settings", tmp_path / "plugins"
    data.mkdir()
    settings.mkdir()
    (plugins / "hermes").mkdir(parents=True)
    database(data / "chat.db", {"conversations": 2, "messages": 5})
    database(data / "benchmarks.db", {"benchmark_runs": 3, "benchmark_samples": 11})
    database(data / "plugins.db", {"uses": 1})
    (data / "users.json").write_text("[]")
    (data / "secret.json").write_text('"never archive me"')
    (settings / "weightroom.toml").write_text('data = "../data"\nkey_file = "../keys/weightroom.key"\n')
    (settings / ".env").write_text("SECRET=never")
    (plugins / "hermes" / "settings.json").write_text('{"profile":"gandalf"}')

    files, excluded = front_plan(data, str(settings / "weightroom.toml"), str(plugins))
    backup_id = new_id(1_700_000_000, b"\x01\x02\x03")
    made = create_backup(
        data / "backups",
        backup_id,
        "front",
        files,
        inventory(files, excluded),
        excluded,
        ["weightroom.service"],
        10,
    )

    assert made["verification"]["state"] == "verified"
    assert made["verification"]["counts"] == {
        "conversations": 2,
        "messages": 5,
        "benchmark_runs": 3,
        "benchmark_samples": 11,
    }
    folder = data / "backups" / backup_id
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "archive.tar.gz").stat().st_mode & 0o777 == 0o600
    listed = manifest(data / "backups", backup_id)
    assert listed["archive_size"] > 0
    assert listed["payload_size"] == sum(item["size"] for item in listed["files"])
    with tarfile.open(folder / "archive.tar.gz") as archive:
        names = archive.getnames()
        embedded = json.loads(archive.extractfile("manifest.json").read())
    assert "payload/data/chat.db" in names and "payload/data/plugins.db" in names and "payload/plugins/hermes/settings.json" in names
    assert not any("secret.json" in name or ".env" in name or "keys" in name for name in names)
    assert "raw API keys" in embedded["secret_policy"]["excluded"]


def test_retention_runs_only_after_a_verified_backup(tmp_path):
    source = tmp_path / "settings.json"
    source.write_text("{}")
    files = [BackupFile(source, "data/settings.json", "settings")]
    root = tmp_path / "backups"
    ids = [new_id(1_700_000_000 + offset, bytes([0, 0, offset])) for offset in range(3)]
    for backup_id in ids:
        create_backup(root, backup_id, "front", files, inventory(files, []), [], [], 2)
    assert [row["id"] for row in manifests(root)] == list(reversed(ids[-2:]))


def test_inline_secrets_and_symlinked_inputs_are_refused(tmp_path):
    secret_settings = tmp_path / "unsafe.toml"
    secret_settings.write_text('api_token = "not-for-an-archive"\n')
    with pytest.raises(ValueError, match="inline secret field api_token"):
        create_backup(
            tmp_path / "backups",
            new_id(1_700_000_000, b"\x07\x08\x09"),
            "front",
            [BackupFile(secret_settings, "settings/unsafe.toml", "settings")],
            [], [], [], 1,
        )


def test_required_configured_store_is_reported_and_cannot_be_silently_omitted(tmp_path):
    store = RecoveryStore("front.chat", Database.sqlite(tmp_path / "missing.db"), ("weightroom.chat",))
    row = inspect_store(store)
    assert row["status"] == "missing" and row["required"] is True
    backup_id = new_id(1_700_000_000, b"\x09\x08\x07")
    with pytest.raises(ValueError, match="required SQLite store front.chat is missing"):
        create_backup(
            tmp_path / "backups", backup_id, "front", [], [row], [], [], 1, stores=[store]
        )
    assert not (tmp_path / "backups" / backup_id).exists()


def test_backup_inventory_reads_versions_without_integrity_or_row_scans(tmp_path, monkeypatch):
    from db import recovery
    path = tmp_path / 'accounts.db'
    with sqlite3.connect(path) as connection:
        connection.executescript('CREATE TABLE items(value); INSERT INTO items VALUES(1); '
                                 'CREATE TABLE suite_schema_migrations(stream,version); '
                                 "INSERT INTO suite_schema_migrations VALUES('accounts',1),('accounts',2);")
    before = path.read_bytes()
    connect, queries = sqlite3.connect, []

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(recovery.sqlite3, 'connect', traced)
    row = inspect_store(RecoveryStore('accounts', Database.sqlite(path), ('weightroom.accounts',)))
    assert row['status'] == 'included' and row['schema_versions'] == {'accounts': 2}
    assert not any('integrity_check' in query.lower() or 'count(' in query.lower() for query in queries)
    assert path.read_bytes() == before
    # Actual recovery verification still checks the contents independently.
    assert recovery.sqlite_metadata(path)['counts']['items'] == 1
    assert any('integrity_check' in query.lower() for query in queries)


def test_unreachable_postgresql_inventory_never_exposes_dsn_credentials(tmp_path):
    secret = tmp_path / "broken.dsn"
    secret.write_text("postgresql://private:visible-secret@[\n")
    secret.chmod(0o600)
    store = RecoveryStore("compute.core", Database.postgresql(secret, display_name="Private DB"),
                          ("loadcoach.core",))
    row = inspect_store(store)
    assert row["status"] == "unreachable"
    assert row["source"] == "Private DB"
    assert "visible-secret" not in row["error"] and "postgresql://" not in row["error"]


def test_settings_compatibility_alias_is_recorded_and_canonical_file_is_archived(tmp_path):
    data, settings = tmp_path / "data", tmp_path / "settings"
    data.mkdir()
    settings.mkdir()
    database(data / "loadcoach.db", {"users": 1, "models": 2, "events": 3})
    canonical = settings / "toollocker.toml"
    canonical.write_text('data = "../data"\n')
    alias = settings / "tooldoor.toml"
    alias.symlink_to(canonical.name)

    files, excluded = compute_plan(str(data / "loadcoach.db"), str(canonical))
    rows = inventory(files, excluded)
    alias_row = next(row for row in rows if row["source"] == str(alias))
    assert alias not in [item.source for item in files]
    assert canonical in [item.source for item in files]
    assert alias_row["status"] == "recorded"
    assert alias_row["target"] == canonical.name

    backup_id = new_id(1_700_000_002, b"\x07\x08\x0b")
    made = create_backup(data / "backups", backup_id, "compute", files, rows, excluded, [], 1)
    assert made["verification"]["state"] == "verified"
    with tarfile.open(data / "backups" / backup_id / "archive.tar.gz") as archive:
        names = archive.getnames()
    assert "payload/settings/toollocker.toml" in names
    assert "payload/settings/tooldoor.toml" not in names

    safe = tmp_path / "safe.json"
    safe.write_text("{}")
    linked = tmp_path / "linked.json"
    linked.symlink_to(safe)
    with pytest.raises(ValueError, match="symlink"):
        create_backup(
            tmp_path / "backups",
            new_id(1_700_000_001, b"\x07\x08\x0a"),
            "front",
            [BackupFile(linked, "plugins/demo/settings.json", "plugin settings")],
            [], [], [], 1,
        )


def test_loadcoach_manages_and_previews_compute_backups(tmp_path):
    database_path = tmp_path / "data" / "loadcoach.db"
    database_path.parent.mkdir()
    with TestClient(build_app(Config(database=str(database_path)))) as client:
        listed = client.get("/loadcoach/backups")
        assert listed.status_code == 200
        assert any(row["kind"].startswith("models, requests") for row in listed.json()["inventory"])  # the users are front's
        backup_id = new_id(1_700_000_000, b"\x04\x05\x06")
        made = client.post("/loadcoach/backups", json={"id": backup_id, "retention": 3})
        preview = client.get(f"/loadcoach/backups/{backup_id}")

    assert made.status_code == 201 and made.json()["verification"]["state"] == "verified"
    assert set(made.json()["verification"]["counts"]) >= {"models", "events"}
    assert preview.status_code == 200 and preview.json()["id"] == backup_id


def test_compute_inventory_includes_unreachable_companion_service_stores(tmp_path):
    settings = tmp_path / "settings"
    data = tmp_path / "data"
    settings.mkdir()
    data.mkdir()
    loadcoach_path = settings / "loadcoach.toml"
    loadcoach_path.write_text('data = "../data"\n')
    spotter_path = settings / "spotter.toml"
    spotter_path.write_text(
        '''
data = "../data"
[databases.stores.telemetry]
engine = "postgresql"
dsn_file = "../keys/missing.dsn"
display_name = "Telemetry PostgreSQL"
[databases.bindings]
"spotter.history" = "telemetry"
'''
    )
    config = Config(database=str(data / "loadcoach.db"), data="../data", path=str(loadcoach_path))
    with TestClient(build_app(config)) as client:
        rows = client.get("/loadcoach/backups").json()["inventory"]
    telemetry = next(row for row in rows if row.get("bindings") == ["spotter.history"])
    assert telemetry["status"] == "unreachable"
    assert telemetry["source"] == "Telemetry PostgreSQL"
    assert "cannot be read" in telemetry["error"]
