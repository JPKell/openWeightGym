import json
import fcntl
import io
import sqlite3
import subprocess
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
from loadcoach.backup_crypto import ARCHIVE
from loadcoach.backup_restore import verify
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


def test_backup_is_manifested_owner_only_and_restored_in_isolation(tmp_path, backup_keys):
    data, settings, plugins = tmp_path / "data", tmp_path / "settings", tmp_path / "plugins"
    data.mkdir()
    backup_keys.configure(data / "backups")
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
    assert (folder / ARCHIVE).stat().st_mode & 0o777 == 0o600
    assert not (folder / "archive.tar.gz").exists()
    assert (folder / ARCHIVE).read_bytes().startswith(b"age-encryption.org/v1\n")
    assert not any(path.name == 'verification.key' for path in folder.rglob('*'))
    listed = manifest(data / "backups", backup_id)
    assert listed["archive_size"] > 0
    assert listed["payload_size"] == sum(item["size"] for item in listed["files"])
    staged = tmp_path / "restored"
    checked = verify(folder / ARCHIVE, backup_keys.identity, output=staged,
                     expected_sha256=made['archive_sha256'])
    embedded = json.loads((staged / 'manifest.json').read_text())
    names = [path.relative_to(staged).as_posix() for path in staged.rglob('*') if path.is_file()]
    assert checked['authenticated'] and checked['recovery_key_tested'] and not checked['live_data_changed']
    assert not embedded['protection']['recovery_key_tested']
    assert "payload/data/chat.db" in names and "payload/data/plugins.db" in names and "payload/plugins/hermes/settings.json" in names
    assert not any("secret.json" in name or ".env" in name or "keys" in name for name in names)
    assert "raw API keys" in embedded["secret_policy"]["excluded"]


def test_retention_runs_only_after_a_verified_backup(tmp_path, backup_keys):
    source = tmp_path / "settings.json"
    source.write_text("{}")
    files = [BackupFile(source, "data/settings.json", "settings")]
    root = tmp_path / "backups"
    backup_keys.configure(root)
    ids = [new_id(1_700_000_000 + offset, bytes([0, 0, offset])) for offset in range(3)]
    for backup_id in ids:
        create_backup(root, backup_id, "front", files, inventory(files, []), [], [], 2)
    assert [row["id"] for row in manifests(root)] == list(reversed(ids[-2:]))


def test_inline_secrets_and_symlinked_inputs_are_refused(tmp_path, backup_keys):
    backup_keys.configure(tmp_path / 'backups')
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


def test_required_configured_store_is_reported_and_cannot_be_silently_omitted(tmp_path, backup_keys):
    backup_keys.configure(tmp_path / 'backups')
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


def test_settings_compatibility_alias_is_recorded_and_canonical_file_is_archived(tmp_path, backup_keys):
    data, settings = tmp_path / "data", tmp_path / "settings"
    data.mkdir()
    backup_keys.configure(data / 'backups')
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
    restored = tmp_path / 'restored'
    verify(data / 'backups' / backup_id / ARCHIVE, backup_keys.identity, output=restored)
    assert (restored / 'payload/settings/toollocker.toml').is_file()
    assert not (restored / 'payload/settings/tooldoor.toml').exists()

    safe = tmp_path / "safe.json"
    safe.write_text("{}")
    linked = tmp_path / "linked.json"
    linked.symlink_to(safe)
    backup_keys.configure(tmp_path / 'backups')
    with pytest.raises(ValueError, match="symlink"):
        create_backup(
            tmp_path / "backups",
            new_id(1_700_000_001, b"\x07\x08\x0a"),
            "front",
            [BackupFile(linked, "plugins/demo/settings.json", "plugin settings")],
            [], [], [], 1,
        )


def test_loadcoach_manages_and_previews_compute_backups(tmp_path, backup_keys):
    database_path = tmp_path / "data" / "loadcoach.db"
    database_path.parent.mkdir()
    backup_keys.configure(database_path.parent / 'backups')
    with TestClient(build_app(Config(database=str(database_path)))) as client:
        listed = client.get("/loadcoach/backups")
        assert listed.status_code == 200
        assert any(row["kind"].startswith("models, requests") for row in listed.json()["inventory"])  # the users are front's
        backup_id = new_id(1_700_000_000, b"\x04\x05\x06")
        made = client.post("/loadcoach/backups", json={"id": backup_id, "retention": 3})
        preview = client.get(f"/loadcoach/backups/{backup_id}")

    assert made.status_code == 201 and made.json()["verification"]["state"] == "verified", made.text
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


def small_backup(tmp_path, backup_keys, *, backup_id=None, retention=2):
    root = backup_keys.configure(tmp_path / 'backups')
    source = tmp_path / 'settings.json'
    source.write_text('{"private-data":"backup-content-canary"}')
    made = create_backup(root, backup_id or new_id(), 'front',
                         [BackupFile(source, 'data/settings.json', 'settings')], [], [], [], retention)
    return root / made['id'] / ARCHIVE, made


def test_unconfigured_or_missing_age_never_snapshots_or_rotates(tmp_path, monkeypatch, backup_keys):
    from loadcoach import backup_crypto, backups
    source = tmp_path / 'private.json'
    source.write_text('{}')
    root = tmp_path / 'backups'
    called = []
    monkeypatch.setattr(backups, '_snapshot', lambda *args: called.append(args))
    with pytest.raises(ValueError, match='not configured'):
        create_backup(root, new_id(), 'front', [BackupFile(source, 'data/file.json', 'settings')], [], [], [], 1)
    assert not called and not manifests(root)
    backup_keys.configure(root)
    monkeypatch.setattr(backup_crypto.shutil, 'which', lambda name: None)
    with pytest.raises(ValueError, match='Install age'):
        create_backup(root, new_id(), 'front', [BackupFile(source, 'data/file.json', 'settings')], [], [], [], 1)
    assert not called and not manifests(root)


@pytest.mark.parametrize('value', ['AGE-SECRET-KEY-private-canary', 'age1' + 'q' * 58, 'age1plugin-secret'])
def test_invalid_public_recipients_are_rejected_without_echoing_values(tmp_path, backup_keys, value):
    from loadcoach.backup_crypto import recipients
    root = backup_keys.configure(tmp_path / 'backups')
    (root / 'recipients.txt').write_text(value)
    with pytest.raises(ValueError) as caught:
        recipients(root)
    assert value not in str(caught.value)


def test_shared_or_symlinked_key_configuration_is_rejected(tmp_path, backup_keys):
    from loadcoach.backup_crypto import recipients
    root = backup_keys.configure(tmp_path / 'backups')
    public = root / 'recipients.txt'
    public.chmod(0o666)
    with pytest.raises(ValueError, match='permissions'):
        recipients(root)
    public.unlink()
    public.symlink_to(backup_keys.identity)
    with pytest.raises(ValueError, match='symlink'):
        recipients(root)


@pytest.mark.parametrize('damage', ['flip', 'truncate', 'append'])
def test_corrupt_ciphertext_is_never_staged(tmp_path, backup_keys, damage):
    archive, _ = small_backup(tmp_path, backup_keys)
    content = archive.read_bytes()
    assert b'backup-content-canary' not in content
    content = content[:-1] + bytes([content[-1] ^ 1]) if damage == 'flip' else content[:len(content)//2] if damage == 'truncate' else content + b'extra'
    archive.write_bytes(content)
    output = tmp_path / 'restored'
    with pytest.raises(ValueError, match='authentication'):
        verify(archive, backup_keys.identity, output=output)
    assert not output.exists()
    assert (tmp_path / 'settings.json').read_text() == '{"private-data":"backup-content-canary"}'


def test_wrong_key_existing_output_and_independent_hash_are_refused(tmp_path, backup_keys):
    archive, _ = small_backup(tmp_path, backup_keys)
    wrong = tmp_path / 'wrong.key'
    subprocess.run(['age-keygen', '-o', str(wrong)], check=True, capture_output=True)
    wrong.chmod(0o600)
    with pytest.raises(ValueError, match='authentication'):
        verify(archive, wrong)
    with pytest.raises(ValueError, match='SHA-256'):
        verify(archive, backup_keys.identity, expected_sha256='0' * 64)
    output = tmp_path / 'occupied'
    output.mkdir()
    with pytest.raises(ValueError, match='new staging'):
        verify(archive, backup_keys.identity, output=output)
    assert not list(output.iterdir())


def test_failed_roundtrip_preserves_previous_sets_and_removes_private_work(tmp_path, backup_keys, monkeypatch):
    from loadcoach import backups
    archive, _ = small_backup(tmp_path, backup_keys)
    root = archive.parent.parent
    previous = set(root.iterdir())
    def fail(*args):
        raise ValueError('Injected authentication failure')
    monkeypatch.setattr(backups, 'decrypt', fail)
    with pytest.raises(ValueError, match='authentication failure'):
        small_backup(tmp_path, backup_keys, retention=1)
    assert set(root.iterdir()) == previous and archive.exists()


def test_backup_creation_lock_and_unsafe_archive_paths_fail_before_writes(tmp_path, backup_keys):
    root = backup_keys.configure(tmp_path / 'backups')
    with (root / '.backup.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='Another backup'):
            create_backup(root, new_id(), 'front', [], [], [], [], 1)
    source = tmp_path / 'settings.json'
    source.write_text('{}')
    for name in ('../escape.json', '/absolute.json', 'data/../escape', 'data//file'):
        with pytest.raises(ValueError, match='unsafe path'):
            create_backup(root, new_id(), 'front', [BackupFile(source, name, 'settings')], [], [], [], 1)
    with pytest.raises(ValueError, match='duplicate'):
        create_backup(root, new_id(), 'front', [BackupFile(source, 'data/a', 'settings')] * 2, [], [], [], 1)
    assert not (tmp_path / 'escape.json').exists()


@pytest.mark.parametrize('name,kind', [('payload/../../escape', tarfile.REGTYPE),
                                    ('/escape', tarfile.REGTYPE), ('payload/link', tarfile.SYMTYPE),
                                    ('payload/link', tarfile.LNKTYPE), ('payload/pipe', tarfile.FIFOTYPE),
                                    ('payload/file', 'duplicate')])
def test_restore_refuses_unsafe_tar_members(tmp_path, name, kind):
    from loadcoach.backups import _safe_extract
    archive = tmp_path / 'malicious.tar.gz'
    with tarfile.open(archive, 'w:gz') as saved:
        member = tarfile.TarInfo(name)
        member.type = tarfile.REGTYPE if kind == 'duplicate' else kind
        member.linkname = '../../escape'
        saved.addfile(member, io.BytesIO())
        if kind == 'duplicate':
            saved.addfile(member, io.BytesIO())
    target = tmp_path / 'staged'
    target.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsafe'):
        _safe_extract(archive, target)
    assert not (tmp_path / 'escape').exists()


def test_legacy_archives_are_labeled_and_preserved_during_encrypted_rotation(tmp_path, backup_keys):
    archive, made = small_backup(tmp_path, backup_keys)
    root = archive.parent.parent
    old = root / new_id(1_700_000_000, b'\x01\x02\x03')
    old.mkdir(mode=0o700)
    legacy = {**made, 'schema': 2}
    legacy.pop('protection')
    legacy['id'] = old.name
    (old / 'manifest.json').write_text(json.dumps(legacy))
    (old / 'archive.tar.gz').write_bytes(b'legacy-private-content')
    row = manifest(root, old.name)
    assert row['verification']['state'] == 'legacy-unencrypted' and not row['protection']['encrypted']
    small_backup(tmp_path, backup_keys, retention=1)
    assert old.is_dir() and (old / 'archive.tar.gz').read_bytes() == b'legacy-private-content'


def test_ciphertext_authentication_does_not_bypass_manifest_or_extraction_limits(tmp_path, backup_keys):
    from loadcoach.backup_crypto import decrypt
    archive, _ = small_backup(tmp_path, backup_keys)
    plain = tmp_path / 'plain.tar.gz'
    decrypt(archive, backup_keys.identity, plain)
    bad = tmp_path / 'bad.tar.gz'
    with tarfile.open(plain) as original, tarfile.open(bad, 'w:gz') as changed:
        for member in original:
            content = original.extractfile(member).read() if member.isfile() else None
            if member.name == 'manifest.json':
                value = json.loads(content)
                value['files'][0]['archive'] = '../../escape'
                content = json.dumps(value).encode()
                member.size = len(content)
            changed.addfile(member, io.BytesIO(content) if content is not None else None)
    encrypted = tmp_path / 'bad.age'
    subprocess.run(['age', '-r', backup_keys.recipient, '-o', str(encrypted), str(bad)], check=True, capture_output=True)
    output = tmp_path / 'restored'
    with pytest.raises(ValueError, match='unsafe path'):
        verify(encrypted, backup_keys.identity, output=output)
    assert not output.exists() and not (tmp_path / 'escape').exists()
    with pytest.raises(ValueError, match='size limit'):
        verify(archive, backup_keys.identity, output=output, max_bytes=1)
    assert not output.exists()


def test_retirement_gate_can_authenticate_encrypted_recovery_assets(tmp_path, backup_keys):
    import shutil
    from db.consolidation_retire import recovery_assets
    recovery, outside = tmp_path / 'recovery', tmp_path / 'outside'
    recovery.mkdir()
    outside.mkdir()
    backup_id = new_id()
    for side, names in [('compute', ('loadcoach', 'spotter')), ('front', ('accounts', 'roles', 'benchmarks'))]:
        root = backup_keys.configure(tmp_path / side / 'backups')
        files = []
        for name in names:
            source = recovery / (name + '.db')
            database(source, {'example': 2})
            files.append(BackupFile(source, 'data/' + source.name, 'service data', database=True))
        create_backup(root, backup_id, side, files, [], [], [], 0)
        shutil.copytree(root / backup_id, recovery / side)
        staged = tmp_path / side / 'staged'
        verify(root / backup_id / ARCHIVE, backup_keys.identity, output=staged)
        for name in names:
            shutil.copy2(staged / 'payload/data' / (name + '.db'), recovery / (name + '.db'))
            if side == 'compute':
                shutil.copy2(staged / 'payload/data' / (name + '.db'), outside / (name + '.db'))
    (outside / 'retention.json').write_text(json.dumps({'owner_instruction_required': True, 'backup_id': backup_id}))
    with pytest.raises(ValueError, match='offline age identity'):
        recovery_assets(recovery, outside)
    assets, snapshots, identifier = recovery_assets(recovery, outside, backup_keys.identity)
    assert identifier == backup_id and len(snapshots) == 5
    assert sum(name.endswith('.age') for name in assets) == 2


def test_corrupt_sidecar_is_reported_as_failed_without_exposing_contents(tmp_path, backup_keys):
    archive, made = small_backup(tmp_path, backup_keys)
    made['files'][0].pop('archive')
    made['private-canary'] = 'sidecar-secret-canary'
    (archive.parent / 'manifest.json').write_text(json.dumps(made))
    rows = manifests(archive.parent.parent)
    assert rows[0]['verification']['state'] == 'failed'
    assert 'sidecar-secret-canary' not in json.dumps(rows)
