"""Identity portability must preserve authentication without restoring private histories."""

import base64
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from loadcoach import identity_bundle as bundle
from loadcoach.groups import EVERYTHING
from loadcoach.log import Log
from loadcoach.outside import Services
from loadcoach.users import Users, hash_password, check_password

PHRASE = "a long independent test passphrase"


@pytest.fixture
def installation(tmp_path):
    root, home = tmp_path / "suite", tmp_path / "home"
    (root / "settings").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "keys").mkdir()
    home.mkdir()
    config = root / "settings/weightroom.toml"
    config.write_text('data = "../data"\nkey_file = "../keys/web.key"\n')
    (root / "keys/web.key").write_text("service-key-canary")
    users = Users(root / "data/accounts.db", {"admin": EVERYTHING})
    users.db.execute(
        "INSERT INTO users(name,grp,identity,password,enabled,totp) VALUES(?,?,?,?,?,?)",
        (
            "alice",
            "admin",
            "immutable-user",
            hash_password("a password"),
            1,
            "JBSWY3DPEHPK3PXP",
        ),
    )
    key = users.new_key("alice", "client application")
    users.db.execute(
        "INSERT INTO spending_allowances VALUES('user','immutable-user',23)"
    )
    users.db.commit()
    services = Services(root / "data/accounts.db", str(root / "keys"))
    services.put("provider", {"url": "https://provider.invalid/v1", "models": {}})
    services.db.close()
    (root / "keys/provider.key").write_text("provider-key-canary")
    log = Log(root / "data/accounts.db")
    log.add("test", "private-event", "alice", prompt="history-canary")
    log.db.close()
    users.db.close()
    adapter = home / ".local/share/suite-adapters/hermes/test"
    (adapter / "keys").mkdir(parents=True)
    (adapter / "settings.json").write_text(json.dumps({"key_file": "keys/pairing.key"}))
    (adapter / "keys/pairing.key").write_text("adapter-key-canary")
    tools = home / ".config/toollocker-host"
    tools.mkdir(parents=True)
    (tools / "host.key").write_text("tool-key-canary")
    (tools / "tools.toml").write_text('[files]\nfolder = "/public"\n')
    return root, home, key


def empty(installation):
    root, home, key = installation
    import shutil

    shutil.rmtree(root)
    shutil.rmtree(home)
    return root, home, key


def test_encrypted_reinstall_preserves_keys_password_totp_identity_and_permissions_without_history(
    installation,
):
    root, home, key = installation
    payload = bundle.collect(root, home)
    content = bundle._seal(payload, PHRASE)
    for secret in (
        key,
        "provider-key-canary",
        "adapter-key-canary",
        "history-canary",
        "immutable-user",
    ):
        assert secret.encode() not in content
    assert "history-canary" not in json.dumps(payload)
    opened = bundle._open(content, PHRASE)
    assert bundle._seal(payload, PHRASE) != content
    empty(installation)
    plan = bundle.preview(opened)
    assert not plan["conflicts"] and plan["accounts"] == "create"
    assert not root.exists() and not home.exists()  # preview has no side effects
    report = bundle.restore(opened)
    assert report["applied"] and not report["history_restored"]
    users = Users(root / "data/accounts.db", {})
    user = users.by_key(key)
    assert user.identity == "immutable-user" and user.can("superuser")
    row = users.db.execute(
        "SELECT password,totp FROM users WHERE name=?", ("alice",)
    ).fetchone()
    assert check_password(row[0], "a password") and row[1] == "JBSWY3DPEHPK3PXP"
    assert (
        users.db.execute("SELECT amount FROM spending_allowances").fetchone()[0] == 23
    )
    assert (
        users.db.execute("SELECT 1 FROM sqlite_schema WHERE name='events'").fetchone()
        is None
    )
    users.db.close()
    assert (root / "keys/provider.key").read_text() == "provider-key-canary"
    assert (
        home / ".local/share/suite-adapters/hermes/test/keys/pairing.key"
    ).read_text() == "adapter-key-canary"
    assert (home / ".config/toollocker-host/host.key").read_text() == "tool-key-canary"
    assert all(Path(f["path"]).stat().st_mode & 0o777 == 0o600 for f in opened["files"])
    assert bundle.restore(opened)["accounts"] == "unchanged"


def test_wrong_passphrase_tampering_and_truncation_fail_without_destination_changes(
    installation,
):
    root, home, _ = installation
    data = bundle._seal(bundle.collect(root, home), PHRASE)
    empty(installation)
    for encoded, phrase in [
        (data, "wrong-passphrase"),
        (data[:-1] + bytes([data[-1] ^ 1]), PHRASE),
        (data[:30], PHRASE),
        (b"bad" + data, PHRASE),
    ]:
        with pytest.raises(bundle.BundleError):
            bundle._open(encoded, phrase)
    assert not root.exists() and not home.exists()


def test_existing_different_accounts_are_never_replaced_even_with_replace_files(
    installation,
):
    root, home, key = installation
    payload = bundle.collect(root, home)
    with sqlite3.connect(root / "data/accounts.db") as con:
        con.execute("UPDATE users SET enabled=0")
    assert bundle.preview(payload)["accounts"] == "conflict"
    with pytest.raises(bundle.BundleError, match="conflicts"):
        bundle.restore(payload, replace_files=True)
    users = Users(root / "data/accounts.db", {})
    assert users.by_key(key) is None
    users.db.close()


def test_file_conflicts_need_explicit_replace_and_rollback_on_failure(
    installation, monkeypatch
):
    root, home, _ = installation
    payload = bundle.collect(root, home)
    empty(installation)
    key = root / "keys/web.key"
    key.parent.mkdir(parents=True)
    key.write_text("fresh-install-key")
    assert bundle.preview(payload)["conflicts"]
    with pytest.raises(bundle.BundleError):
        bundle.restore(payload)
    real = bundle._write
    calls = 0

    def fail(path, content, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("simulated disk failure")
        real(path, content, **kwargs)

    monkeypatch.setattr(bundle, "_write", fail)
    with pytest.raises(OSError):
        bundle.restore(payload, replace_files=True)
    assert key.read_text() == "fresh-install-key"
    assert not (root / ".identity-import-incomplete").exists()
    with sqlite3.connect(root / "data/accounts.db") as con:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    monkeypatch.setattr(bundle, "_write", real)
    bundle.restore(payload, replace_files=True)
    assert key.read_text() == "service-key-canary"


def test_export_refuses_symlinks_missing_credentials_devices_and_oversized_files(
    installation, tmp_path
):
    root, home, _ = installation
    link = root / "keys/linked.key"
    link.symlink_to(root / "keys/web.key")
    with pytest.raises(bundle.BundleError, match="Symbolic"):
        bundle.collect(root, home)
    link.unlink()
    (root / "keys/web.key").unlink()
    with pytest.raises(bundle.BundleError):
        bundle.collect(root, home)
    (root / "keys/web.key").write_text("replacement")
    with pytest.raises(bundle.BundleError):
        bundle.collect(root, home, [Path("/dev/null")])
    big = tmp_path / "big"
    big.write_bytes(b"x" * (bundle.MAX_FILE + 1))
    with pytest.raises(bundle.BundleError):
        bundle.collect(root, home, [big])


def test_archive_path_validation_and_duplicate_rows_fail_before_writing(
    installation, tmp_path
):
    root, home, _ = installation
    payload = bundle.collect(root, home)
    for change in ("traversal", "duplicate", "hash", "unknown_table", "key_owner"):
        bad = copy.deepcopy(payload)
        if change == "traversal":
            bad["files"][0]["path"] = str(root / "../escape")
        elif change == "duplicate":
            bad["files"].append(bad["files"][0])
        elif change == "hash":
            bad["files"][0]["content"] = base64.b64encode(b"changed").decode()
        elif change == "unknown_table":
            bad["identity"]["tables"]["events"] = []
        else:
            bad["identity"]["tables"]["keys"][0][1] = "missing-user"
        with pytest.raises(bundle.BundleError):
            bundle.preview(bad)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = root / "keys/web.key"
    original.unlink()
    original.symlink_to(outside / "key")
    with pytest.raises(bundle.BundleError):
        bundle.preview(payload)
    assert not (outside / "key").exists()


def test_export_preview_has_no_secret_values_and_requires_terminal_and_stopped_confirmation(
    installation, capsys, monkeypatch
):
    root, home, key = installation
    payload = bundle.collect(root, home)
    report = json.dumps(bundle.preview(payload))
    assert all(
        s not in report
        for s in (key, "provider-key-canary", "immutable-user", "JBSWY3DPEHPK3PXP")
    )
    for argv in [
        ["export", str(root / "export")],
        ["import", str(root / "missing"), "--apply"],
    ]:
        with pytest.raises(SystemExit):
            bundle.main(argv)
        assert "--services-stopped" in capsys.readouterr().err
    monkeypatch.setattr(bundle.sys.stdin, "isatty", lambda: False)
    with pytest.raises(bundle.BundleError, match="terminal"):
        bundle._passphrase(True)


def test_disabled_accounts_expired_keys_and_explicit_environment_file_preserve_policy(
    installation, tmp_path
):
    root, home, key = installation
    with sqlite3.connect(root / "data/accounts.db") as con:
        con.execute("UPDATE users SET enabled=0")
        con.execute("UPDATE keys SET expires=1")
    env = tmp_path / "service.env"
    env.write_text("PROVIDER_TOKEN=env-canary")
    payload = bundle.collect(root, home, [env])
    empty(installation)
    env.unlink()
    bundle.restore(payload)
    users = Users(root / "data/accounts.db", {})
    assert users.by_key(key) is None
    assert users.db.execute("SELECT expires FROM keys").fetchone()[0] == 1
    assert env.read_text() == "PROVIDER_TOKEN=env-canary"
    users.db.close()


def test_same_directory_installer_aliases_are_not_followed_outside_settings(
    installation, tmp_path
):
    root, home, _ = installation
    canonical = root / "settings/toollocker.toml"
    canonical.write_text("[servers]\n")
    alias = root / "settings/tooldoor.toml"
    alias.symlink_to(canonical.name)
    payload = bundle.collect(root, home)
    assert str(alias) not in {item["path"] for item in payload["files"]}
    alias.unlink()
    alias.symlink_to(tmp_path / "external.toml")
    with pytest.raises(bundle.BundleError):
        bundle.collect(root, home)


def test_interrupted_import_can_resume_only_the_same_bundle(installation):
    root, home, _ = installation
    payload = bundle.collect(root, home)
    marker = root / ".identity-import-incomplete"
    marker.write_bytes(b"unrelated-bundle")
    with pytest.raises(bundle.BundleError, match="another bundle"):
        bundle.restore(payload)
    marker.write_bytes(
        bundle.hashlib.sha256(bundle._json(payload)).hexdigest().encode()
    )
    bundle.restore(payload)
    assert not marker.exists()


def test_duplicates_and_database_files_cannot_be_smuggled_as_credentials(installation):
    root, home, _ = installation
    payload = bundle.collect(root, home)
    payload["identity"]["tables"]["users"].append(
        payload["identity"]["tables"]["users"][0]
    )
    with pytest.raises(bundle.BundleError, match="Duplicate"):
        bundle.preview(payload)
    with pytest.raises(bundle.BundleError, match="Database files"):
        bundle.collect(root, home, [root / "data/accounts.db"])


def test_process_guard_refuses_live_suite_but_not_ordinary_shells(
    installation, tmp_path
):
    root, home, _ = installation
    proc = tmp_path / "proc"
    p = proc / "999999"
    p.mkdir(parents=True)
    (p / "cwd").symlink_to(root, target_is_directory=True)
    (p / "cmdline").write_bytes(b"/bin/bash\x00-c\x00loadcoach --help\x00")
    bundle.ensure_stopped(root, home, proc)
    (p / "cmdline").write_bytes(
        b"/venv/bin/loadcoach\x00--config\x00settings/loadcoach.toml\x00"
    )
    with pytest.raises(bundle.BundleError, match="still running"):
        bundle.ensure_stopped(root, home, proc)


def test_cli_round_trip_and_private_publication(
    installation, tmp_path, monkeypatch, capsys
):
    root, home, key = installation
    destination = tmp_path / "identity.suite-id"
    monkeypatch.setattr(bundle, "_passphrase", lambda confirm: PHRASE)
    monkeypatch.setattr(bundle, "ensure_stopped", lambda *args: None)
    bundle.main(
        [
            "export",
            str(destination),
            "--root",
            str(root),
            "--home",
            str(home),
            "--services-stopped",
        ]
    )
    assert destination.stat().st_mode & 0o777 == 0o600
    bundle.main(["inspect", str(destination)])
    assert key not in capsys.readouterr().out
    empty(installation)
    bundle.main(["import", str(destination), "--root", str(root)])
    assert not root.exists()
    with pytest.raises(SystemExit):
        bundle.main(
            [
                "import",
                str(destination),
                "--root",
                str(tmp_path / "wrong"),
                "--apply",
                "--services-stopped",
            ]
        )
    assert not root.exists()
    bundle.main(
        [
            "import",
            str(destination),
            "--root",
            str(root),
            "--apply",
            "--services-stopped",
        ]
    )
    assert (root / "data/accounts.db").exists()
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        bundle._write(destination, b"never-overwrite")
    assert destination.read_bytes() == before


def test_account_file_overlap_is_rejected_before_any_destination_write(installation):
    import hashlib

    root, home, _ = installation
    payload = bundle.collect(root, home)
    config = next(
        item
        for item in payload["files"]
        if item["path"] == payload["identity"]["config"]
    )
    raw = b'data = "../keys"\n[databases.stores.accounts]\nengine = "sqlite"\npath = "../keys/web.key"\n[databases.bindings]\n"weightroom.accounts" = "accounts"\n'
    config["content"] = base64.b64encode(raw).decode()
    config["sha256"] = hashlib.sha256(raw).hexdigest()
    empty(installation)
    with pytest.raises(bundle.BundleError, match="overlap"):
        bundle.restore(payload)
    # The cooperating import lock may be created, but no secret/database destination is written.
    assert not (root / "keys").exists()


def test_configured_saved_tool_key_references_are_exported_and_restored(
    installation, tmp_path
):
    from db import Database, migrate
    from db.schemas import SETTINGS

    root, home, _ = installation
    source = Database.sqlite(root / "data/accounts.db")
    secret = tmp_path / "custom-remote.key"
    secret.write_text("custom-key-canary")
    with source.connection() as con:
        migrate(source, con, "loadcoach.settings", SETTINGS)
        con.execute(
            "INSERT INTO settings_changed VALUES(?,?,?,?,?)",
            (
                "toollocker",
                "servers",
                json.dumps({"remote": {"key_file": str(secret)}}),
                "alice",
                1234,
            ),
        )
        con.commit()
    payload = bundle.collect(root, home)
    assert str(secret) in {item["path"] for item in payload["files"]}
    empty(installation)
    secret.unlink()
    bundle.restore(payload)
    assert secret.read_text() == "custom-key-canary"
    with sqlite3.connect(root / "data/accounts.db") as con:
        assert (
            con.execute("SELECT name FROM settings_changed").fetchone()[0] == "servers"
        )


def test_file_created_after_preview_is_not_silently_replaced(installation, monkeypatch):
    root, home, _ = installation
    payload = bundle.collect(root, home)
    empty(installation)
    real = bundle._write
    key = root / "keys/web.key"

    def publish(path, content, **kwargs):
        real(path, content, **kwargs)
        if path.name == ".identity-import-incomplete":
            key.parent.mkdir(parents=True, exist_ok=True)
            key.write_text("concurrent-value")

    monkeypatch.setattr(bundle, "_write", publish)
    with pytest.raises(bundle.BundleError, match="changed after preview"):
        bundle.restore(payload)
    assert key.read_text() == "concurrent-value"
    with sqlite3.connect(root / "data/accounts.db") as con:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_removed_application_session_stays_unusable_after_restore(installation):
    root, home, key = installation
    with sqlite3.connect(root / "data/accounts.db") as con:
        con.execute("UPDATE keys SET via='removed-application'")
    payload = bundle.collect(root, home)
    empty(installation)
    bundle.restore(payload)
    users = Users(root / "data/accounts.db", {})
    assert users.by_key(key) is None
    assert (
        users.db.execute("SELECT via FROM keys").fetchone()[0] == "removed-application"
    )
    users.db.close()


def test_redundant_legacy_key_alias_is_omitted_but_referenced_symlink_fails(
    installation,
):
    root, home, _ = installation
    (root / "keys/weightroom.key").write_text("canonical-key")
    alias = root / "keys/frontdesk.key"
    alias.symlink_to("weightroom.key")
    payload = bundle.collect(root, home)
    assert str(alias) not in {item["path"] for item in payload["files"]}
    assert str(root / "keys/weightroom.key") in {
        item["path"] for item in payload["files"]
    }
    (root / "settings/weightroom.toml").write_text(
        'data="../data"\nkey_file="../keys/frontdesk.key"\n'
    )
    with pytest.raises(bundle.BundleError, match="Symbolic"):
        bundle.collect(root, home)


def test_commit_failure_rolls_back_identity_and_secret_files(installation, monkeypatch):
    from contextlib import contextmanager

    root, home, _ = installation
    payload = bundle.collect(root, home)
    empty(installation)
    source = bundle.target_store(payload)
    connection = source.connection

    class FailCommit:
        def __init__(self, con):
            self.con = con

        def __getattr__(self, name):
            return getattr(self.con, name)

        def commit(self):
            if self.con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                raise OSError("simulated failed commit")
            self.con.commit()

    @contextmanager
    def hooked(**kwargs):
        with connection(**kwargs) as con:
            yield FailCommit(con)

    monkeypatch.setattr(source, "connection", hooked)
    monkeypatch.setattr(bundle, "target_store", lambda payload: source)
    with pytest.raises(OSError, match="failed commit"):
        bundle.restore(payload)
    with sqlite3.connect(root / "data/accounts.db") as con:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert not (root / "keys/web.key").exists()
    assert not (root / ".identity-import-incomplete").exists()


def test_process_guard_handles_python_script_launchers_outside_installation_cwd(
    installation, tmp_path
):
    root, home, _ = installation
    proc = tmp_path / "proc"
    p = proc / "999998"
    p.mkdir(parents=True)
    (p / "cwd").symlink_to(home, target_is_directory=True)
    (p / "cmdline").write_bytes(
        b"/usr/bin/python3\x00" + str(root / ".venv/bin/loadcoach").encode() + b"\x00"
    )
    with pytest.raises(bundle.BundleError, match="still running"):
        bundle.ensure_stopped(root, home, proc)
