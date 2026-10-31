"""Offline transfer, coordinated rollback and maintenance isolation contracts."""
import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from db import Database, migrate
from db import migration
from db.maintenance import Barrier, Gate, HEADER
from db.schemas import CHAT, SETTINGS
from db.transfer import MigrationError, copy_store, evidence, reader, preflight
from loadcoach.backups import new_id
from loadcoach.log import Log
from loadcoach.users import Users
from loadcoach.groups import read_group
from loadcoach.batches import Batches
from loadcoach.models import Models
from loadcoach.settings import Changes
from weightroom.chat_store import Conversations
from benchmarks.store import Benchmarks
from test_weightroom import desk, sign_in  # noqa: F401


def test_host_inventory_includes_front_identity_stores_with_partial_bindings(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "systemctl", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b""))
    settings = tmp_path / "settings"
    settings.mkdir()
    config = settings / "weightroom.toml"
    config.write_text('data = "../data"\n')

    for contents in (
        'data = "../data"\n',
        'data = "../data"\n[databases.stores.chat]\nengine = "sqlite"\n'
        'path = "../data/chat.db"\n[databases.bindings]\n"weightroom.chat" = "chat"\n',
    ):
        config.write_text(contents)
        references = {row["binding"]: row for row in migration.host_inventory(tmp_path)["references"]}
        for name in ("accounts", "roles"):
            assert references[f"weightroom.{name}"]["database"]["path"] == str((tmp_path / "data" / f"{name}.db").resolve())
        stores = migration.host_inventory(tmp_path)['stores'].values()
        assert any(row['name'] == 'weightroom.default_weightroom_accounts' for row in stores)


def test_service_inventory_accepts_explicit_installation_without_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "systemctl", lambda *args, **kw: SimpleNamespace(
        stdout=b"!/home/operator\n" if "--property=WorkingDirectory" in args else b"123\n"))
    original = Path.read_bytes
    command = ["/usr/bin/python", str(tmp_path / ".venv/bin/loadcoach"), "--config", str(tmp_path / "settings/loadcoach.toml")]
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"\0".join(s.encode() for s in command)
                        if str(self) == "/proc/123/cmdline" else original(self))
    assert migration.service_uses_installation("loadcoach", tmp_path)
    command[-1] = "/another/installation/settings/loadcoach.toml"
    assert not migration.service_uses_installation("loadcoach", tmp_path)
    command[-1] = str(tmp_path / "settings/loadcoach.toml")
    command[1] = "/another/installation/.venv/bin/loadcoach"
    assert not migration.service_uses_installation("loadcoach", tmp_path)


def test_host_inventory_ignores_retired_mirror_but_requires_current_key(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "systemctl", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    (tmp_path / "settings").mkdir()
    config = tmp_path / "settings/voice-gateway.toml"
    config.write_text('mirror_file="../data/users.json"\n')
    assert migration.host_inventory(tmp_path)["artifacts"] == []
    config.write_text('mirror_file="../data/users.json"\nkey_file="../keys/current.key"\n')
    with pytest.raises(MigrationError, match="artifact is missing"):
        migration.host_inventory(tmp_path)


def seeded(path, tmp_path):
    source = Database.sqlite(path)
    changes = Changes(source)
    changes.put("weightroom", "timezone", "America/Vancouver", "owner")
    users = Users(source, {"admin": read_group("admin", {"can": ["superuser"]})})
    users.add("owner", "admin")
    users.change("owner", enabled=True, password="private-password")
    key = users.new_key("owner", "preserved")
    log = Log(source, keep_text=10)
    first = log.add("request", "asked", "owner", "local", text={"nonascii": "música"}, amount=12.50, ordered={"z": 1, "a": 2})
    log.add("request", "done", "owner", "local", of=first, answer_tokens=3)
    with log.db:
        log.db.execute("INSERT INTO texts(entry,asked,said) VALUES(?,?,?)", (first, '{"body":"private prompt"}', '{"body":"private answer"}'))
    models = Models(source)
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"retained-model")
    models.put("tiny", {"flags": {"model": str(artifact)}, "gpus": 0})
    batches = Batches(source)
    file = batches.put_file("owner", "requests.jsonl", "batch", b"\x00\xff\n")
    batch = batches.start("owner", "/v1/chat/completions", file["id"], [{"custom_id": "one", "body": {"model": "tiny", "messages": []}}])
    batches.keep(batch["id"], 0, 200, "r", {"choices": [], "number": 1.0})
    chats = Conversations(source)
    number = chats.start("stable-user", "A conversation")
    chats.say(number, "user", "hello", attachment={"data": "inline"})
    chats.say(number, "assistant", "retained answer")
    # Deleted IDs must not be reused after a round trip.
    with source.transaction() as connection:
        connection.execute("UPDATE sqlite_sequence SET seq=900 WHERE name='messages'")
    benchmarks = Benchmarks(source)
    run = benchmarks.create("stable-user", "tiny", "load")
    benchmarks.activate(run, {"fingerprint": "fp", "subject": {}, "environment": {}})
    benchmarks.plan(run, {"samples": ["load"]})
    benchmarks.keep(run, "load", {"load_seconds": 1.0}, "raw answer", "raw log", {"case": "proof"})
    for repository in (changes, users, log, models, batches, chats, benchmarks):
        repository.db.close()
    return source, key


def test_sqlite_copy_preserves_all_content_and_source(tmp_path):
    source, key = seeded(tmp_path / "source.db", tmp_path)
    original = source.path.read_bytes()
    target = Database.sqlite(tmp_path / "target.db")
    result = copy_store(source, target)
    assert result["state"] == "verified" and result["chain"]["checked"] == 1
    assert result["sequences"]["messages"] == 900
    assert source.path.read_bytes() == original and target.path.stat().st_mode & 0o077 == 0
    assert Users(target, {"admin": read_group("admin", {"can": ["superuser"]})}).by_key(key).name == "owner"
    chats = Conversations(target)
    chats.say(1, "assistant", "after migration")
    assert chats.db.execute("SELECT MAX(id) FROM messages").fetchone()[0] > 900


def test_legacy_local_event_source_migration_preserves_audit_payload(tmp_path):
    from db.schemas import LOG
    from db.transfer import check_chain
    source = Database.sqlite(tmp_path / "old.db")
    with source.connection() as connection:
        migrate(source, connection, "loadcoach.log", LOG[:5])
    # Reproduce the historical schema without running today's Log constructor.
    with source.transaction() as connection:
        from loadcoach.log import chain_hash
        payload = (1., "role", "moved-here", "operator", "cli", None, None, None, '{"roles": 2}', None)
        hashed = chain_hash("", payload)
        connection.execute("INSERT INTO events(at,kind,what,user,caller,model,trace,level,detail,text,hash,source) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (*payload, hashed, "/tmp/old-core.db"))
        before = connection.execute("SELECT id,at,detail,text,hash FROM events").fetchall()
    with source.connection() as connection:
        migrate(source, connection, "loadcoach.log", LOG)
        assert connection.execute("SELECT id,at,detail,text,hash FROM events").fetchall() == before
        assert connection.execute("SELECT source,legacy_source FROM events").fetchone() == (None, "/tmp/old-core.db")
        assert check_chain(connection) == {"checked": 0, "anchor": 1}
    target = Database.sqlite(tmp_path / "copied.db")
    assert copy_store(source, target)["state"] == "verified"
    with target.connection() as connection:
        assert connection.execute("SELECT source,legacy_source FROM events").fetchone() == (None, "/tmp/old-core.db")


def test_legacy_benchmark_nul_migration_preserves_decoded_text_and_idempotent_retries(tmp_path):
    from db.schemas import BENCHMARKS
    source = Database.sqlite(tmp_path / 'old-benchmarks.db')
    with source.connection() as connection:
        migrate(source, connection, 'weightroom.benchmarks', BENCHMARKS[:5])
        with connection:
            connection.execute('INSERT INTO benchmark_samples VALUES(?,?,?,?,?,?)',
                               ('old', 'sample', 1., '{}', 'raw\0música', 'log\0tail'))
    kept = Benchmarks(source)
    sample = kept.samples('old')[0]
    assert (sample['at'], sample['output'], sample['log']) == (1., 'raw\0música', 'log\0tail')
    assert kept.db.execute('SELECT COUNT(*) FROM benchmark_sample_encodings').fetchone() == (2,)
    kept.keep('old', 'sample', {}, 'replacement', 'replacement', {})
    assert kept.samples('old')[0]['output'] == 'raw\0música'
    kept.keep('plain', 'sample', {}, 'cmF3AG3DunNpY2E=', 'plain', {})
    assert kept.samples('plain')[0]['output'] == 'cmF3AG3DunNpY2E='
    kept.db.close()


def test_benchmark_regrade_receives_complete_decoded_nul_output(tmp_path, monkeypatch):
    kept = Benchmarks(str(tmp_path / 'benchmarks.db'))
    number = kept.create('owner', 'small', 'load')
    kept.keep(number, 'case', {'success': True}, 'raw\0answer', '', {'case': {}})
    kept.state(number, 'completed')
    answers = []
    def grade(case, answer):
        answers.append(answer)
        return {}, {}
    module = SimpleNamespace(PROTOCOL={'grader_version': 999}, saved_answer=lambda output, prompt: output, grade=grade)
    monkeypatch.setattr(kept, 'module', lambda *args: module)
    assert kept.regrade() == 1
    assert answers == ['raw\0answer']
    assert kept.samples(number)[0]['output'] == 'raw\0answer'
    kept.db.close()


def test_preflight_rejects_unknown_schemas_populated_targets_and_broken_chain(tmp_path):
    source, _ = seeded(tmp_path / "source.db", tmp_path)
    target = Database.sqlite(tmp_path / "target.db")
    target.path.write_bytes(b"operator data")
    with pytest.raises(MigrationError, match="new local file"):
        preflight(source, target)
    target.path.unlink()
    with source.transaction() as db:
        db.execute("CREATE TABLE unknown(value TEXT)")
    with pytest.raises(MigrationError, match="unrecognized"):
        preflight(source, target)
    with source.transaction() as db:
        db.execute("DROP TABLE unknown")
        db.execute("UPDATE events SET detail='{}' WHERE id=2")
    with pytest.raises(MigrationError, match="hash chain"):
        preflight(source, target)
    assert not target.path.exists()


def test_preflight_refuses_missing_artifacts_and_outdated_ledger(tmp_path):
    source, _ = seeded(tmp_path / "source.db", tmp_path)
    target = Database.sqlite(tmp_path / "target.db")
    (tmp_path / "model.gguf").unlink()
    with pytest.raises(MigrationError, match="artifact"):
        preflight(source, target)
    (tmp_path / "model.gguf").write_bytes(b"restored")
    with source.transaction() as db:
        db.execute("DELETE FROM suite_schema_migrations WHERE stream='loadcoach.log' AND version=(SELECT MAX(version) FROM suite_schema_migrations WHERE stream='loadcoach.log')")  # the latest, so the ledger is behind
    with pytest.raises(MigrationError, match="outdated"):
        preflight(source, target)


def test_sqlite_transfer_and_recovery_preserve_uri_reserved_filename_characters(tmp_path):
    from db.recovery import RecoveryStore, backup_store
    source, _ = seeded(tmp_path / "source#one?.db", tmp_path)
    target = Database.sqlite(tmp_path / "target#two?.db")
    assert copy_store(source, target)["state"] == "verified"
    recovered = backup_store(RecoveryStore("special", source, ("loadcoach.core",)), tmp_path / "payload")
    assert recovered["verification"]["state"] == "verified"


def test_configuration_changes_only_declared_bindings_and_keeps_comments(tmp_path):
    path = tmp_path / "settings.toml"
    original = 'host="127.0.0.1" # keep this\n[databases.stores.chat] # inline comment\nengine="sqlite"\npath="chat.db"\n[databases.bindings]\n"weightroom.chat"="chat"\n[turnstile]\nwait=180\n'
    path.write_text(original)
    info = {"configs": {str(path): {"sha256": migration.digest(original), "databases": {"stores": {"chat": {"engine": "sqlite", "path": "chat.db"}}, "bindings": {"weightroom.chat": "chat"}}}},
            "references": [{"config": str(path), "binding": "weightroom.chat", "endpoint": "source"}]}
    target = {"name": "target", "engine": "postgresql", "dsn_file": "/private/chat.dsn"}
    changes = migration.configuration_preview(info, "source", target)
    text, block = migration.config_text(original, changes[0]["section"])
    assert 'host="127.0.0.1" # keep this' in text and "[turnstile]\nwait=180" in text
    assert '"weightroom.chat" = "migrated_' in block and '/private/chat.dsn' in block
    assert changes[0]["section"]["stores"]["chat"] == {"engine": "sqlite", "path": "chat.db"}


@pytest.fixture
def coordinator(tmp_path, monkeypatch):
    plan = {"approval": "approved", "hosts": {"front": {"active": ["weightroom", "turnstile"]}, "compute": {"active": ["loadcoach", "toollocker", "spotter"]}},
            "source": {"database": {}}, "target": {"engine": "sqlite"}, "changes": {"front": [], "compute": []}}
    monkeypatch.setattr(migration, "preview", lambda profile, call: plan)
    events = []
    def call(host, operation, **payload):
        events.append((host["name"], operation))
        if operation == "idle":
            return {"active": 0}
        if operation == "backup":
            return {"verification": {"state": "verified"}, "id": payload["job"]}
        if operation == "copy":
            return {"state": "verified", "tables": {}}
        return {}
    profile = {"hosts": {name: {"name": name} for name in ("front", "compute")}, "owner": "compute", "smoke_file": "private"}
    return profile, call, events, tmp_path / "report.json"


def test_coordinator_accepts_only_after_verified_pair_and_restarts(coordinator):
    profile, call, events, path = coordinator
    smoke = lambda filename, token: {name: "passed" for name in migration.SMOKES}
    result = migration.run_job(profile, "approved", new_id(), path, call, smoke)
    assert result["state"] == "complete" and len(result["recovery"]) == 2
    operations = [operation for _, operation in events]
    assert operations.index("copy") > max(i for i, operation in enumerate(operations) if operation == "backup")
    assert operations.index("switch") > operations.index("copy")
    assert operations.index("unlock") > operations.index("start")
    assert json.loads(path.read_text())["state"] == "complete" and path.stat().st_mode & 0o077 == 0


def test_failed_acceptance_stops_target_restores_all_bindings_and_checks_rollback(coordinator):
    profile, call, events, path = coordinator
    attempts = []
    def smoke(filename, token):
        attempts.append(token)
        if len(attempts) == 1:
            raise MigrationError("Acceptance check failed: chat")
        return {name: "passed" for name in migration.SMOKES}
    result = migration.run_job(profile, "approved", new_id(), path, call, smoke)
    assert result["state"] == "rolled-back" and len(attempts) == 2
    assert sum(op == "restore" for _, op in events) == 2
    assert "rollback_acceptance" in result and "chat" in result["failure"]
    assert attempts[0] not in path.read_text()


def test_failed_rollback_keeps_all_traffic_closed(coordinator):
    profile, original, events, path = coordinator
    def call(host, operation, **payload):
        if operation == "restore":
            raise OSError("secret-driver-password")
        return original(host, operation, **payload)
    def smoke(*args):
        raise MigrationError("Acceptance check failed: tool")
    result = migration.run_job(profile, "approved", new_id(), path, call, smoke)
    assert result["state"] == "rollback-failed" and not any(op == "unlock" for _, op in events)
    assert "secret-driver-password" not in path.read_text()


def test_unverified_backup_never_copies_or_switches(coordinator):
    profile, original, events, path = coordinator
    def call(host, operation, **payload):
        result = original(host, operation, **payload)
        if operation == "backup":
            return {"verification": {"state": "failed"}}
        return result
    result = migration.run_job(profile, "approved", new_id(), path, call, lambda *a: {n: "passed" for n in migration.SMOKES})
    assert result["state"] == "rolled-back" and not any(op in ("copy", "switch") for _, op in events)


def test_stale_approval_never_locks_or_stops_services(coordinator):
    profile, call, events, path = coordinator
    result = migration.run_job(profile, "stale", new_id(), path, call)
    assert result["state"] == "rolled-back" and events == []


def test_host_configuration_switch_is_recoverable_after_partial_failure(tmp_path, monkeypatch):
    root = tmp_path
    (root / "settings").mkdir()
    files = [root / "settings" / (name + ".toml") for name in ("first", "second")]
    for path in files:
        path.write_text('host="localhost"\n')
    changes = [{"path": str(path), "before": __import__("hashlib").sha256(path.read_bytes()).hexdigest(), "section": {"stores": {"target": {"engine": "sqlite", "path": "new.db"}}, "bindings": {"weightroom.chat": "target"}}} for path in files]
    job = new_id()
    migration.handle({"root": str(root), "operation": "lock", "job": job, "token": "private-token"})
    original = migration._write_config
    def fail_second(path, content):
        if Path(path) == files[1]:
            raise OSError("interrupted write")
        original(path, content)
    monkeypatch.setattr(migration, "_write_config", fail_second)
    with pytest.raises(OSError):
        migration.handle({"root": str(root), "operation": "switch", "job": job, "changes": changes})
    assert "databases" in files[0].read_text() and "databases" not in files[1].read_text()
    monkeypatch.setattr(migration, "_write_config", original)
    migration.handle({"root": str(root), "operation": "restore", "job": job})
    assert all(path.read_text() == 'host="localhost"\n' for path in files)


def test_barrier_blocks_http_and_websocket_and_allows_private_acceptance(tmp_path):
    gate = Gate(str(tmp_path / "service.toml"))
    async def answer(request):
        return JSONResponse({"ok": True})
    app = Starlette(routes=[Route("/", answer)])
    app.add_middleware(Barrier, gate=gate)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        migration.write_json(gate.path, {"job": new_id(), "token": "private-token"})
        assert client.get("/").status_code == 503
        assert client.get("/", headers={HEADER: "wrong"}).status_code == 503
        assert client.get("/", headers={HEADER: "private-token"}).json() == {"ok": True}
        assert client.get("/__suite_maintenance", headers={HEADER: "private-token"}).json() == {"active": 0}
        gate.path.write_text("corrupt")
        assert client.get("/", headers={HEADER: "invalid-maintenance-marker"}).status_code == 503
        gate.path.unlink()
        assert client.get("/").status_code == 200


def test_host_inventory_finds_all_shared_identity_bindings(tmp_path, monkeypatch):
    (tmp_path / "settings").mkdir()
    (tmp_path / "data").mkdir()
    seeded(tmp_path / "data/loadcoach.db", tmp_path)
    history = Database.sqlite(tmp_path / "data/spotter.db")
    from db.schemas import SPOTTER
    with history.connection() as connection:
        migrate(history, connection, "spotter.history", SPOTTER)
    (tmp_path / "settings/loadcoach.toml").write_text('data="../data"\n')
    (tmp_path / "settings/toollocker.toml").write_text('loadcoach="loadcoach.toml"\n')
    (tmp_path / "settings/spotter.toml").write_text('loadcoach="loadcoach.toml"\ndata="../data"\n')
    monkeypatch.setattr(migration, "systemctl", lambda *args, **kw: SimpleNamespace(returncode=1))
    inventory = migration.host_inventory(tmp_path)
    refs = {ref["binding"]: ref["endpoint"] for ref in inventory["references"]}
    assert refs["loadcoach.core"] == refs["toollocker.identity"] == refs["spotter.identity"]
    assert refs["spotter.history"] != refs["loadcoach.core"]
    target = dataclasses.asdict(Database.sqlite(tmp_path / "data/new.db").store)
    changes = migration.configuration_preview(inventory, refs["loadcoach.core"], target)
    assert {binding for row in changes for binding in row["bindings"]} == {"loadcoach.core", "toollocker.identity", "spotter.identity"}


def test_migration_wizard_permission_preview_start_and_origin(desk, tmp_path, monkeypatch):
    import weightroom.app as front
    browser, _, _ = desk
    folder = tmp_path / "data/migration-profiles"
    folder.mkdir()
    (folder / "core.json").write_text("{}")
    plan = {"approval": "reviewed", "source": {"binding": "loadcoach.core"},
            "preflight": {"source_engine": "sqlite", "target_engine": "postgresql", "source_version": "3", "target_version": "16",
                          "free_bytes": 1000000, "required_bytes": 100000, "counts": {"users": 6}, "chain": {"checked": 4}, "artifacts": []},
            "views": {"compute": {"configurations": [{"path": "/settings/loadcoach.toml", "bindings": ["loadcoach.core"], "toml": 'dsn_file = "/private/core.dsn"'}]}},
            "hosts": {"compute": {"active": ["loadcoach", "toollocker", "spotter"]}}}
    launched = []
    monkeypatch.setattr(front, "migration_profile", lambda *a: {})
    monkeypatch.setattr(front, "migration_preview", lambda *a: plan)
    monkeypatch.setattr(front, "migration_launch", lambda folder, name, approval, reports: launched.append((name, approval)) or new_id())
    sign_in(browser, "pat")
    assert browser.post("/databases/migrations/core/preview").status_code == 403
    assert browser.post("/databases/migrations/core/start").status_code == 403 and launched == []
    sign_in(browser, "jordan")
    assert "Check and preview" in browser.get("/databases").text
    response = browser.post("/databases/migrations/core/preview")
    assert response.status_code == 200 and "Start reviewed migration" in response.text
    assert '/private/core.dsn' in response.text and 'name="approval" value="reviewed"' in response.text
    assert browser.post("/databases/migrations/core/start", data={"approval": "reviewed"}, headers={"origin": "https://other.test"}).status_code == 403
    response = browser.post("/databases/migrations/core/start", data={"approval": "reviewed"})
    assert response.status_code == 200 and launched == [("core", "reviewed")]
    assert "console will close during maintenance" in response.text


def test_crash_recovery_restores_snapshots_and_preserves_accepted_cutovers(tmp_path):
    report_path, lease = tmp_path / "report.json", tmp_path / "pending.json"
    profile = {"hosts": {"front": {"name": "front"}, "compute": {"name": "compute"}}, "smoke_file": "private"}
    plan = {"hosts": {"front": {"active": ["weightroom"]}, "compute": {"active": ["loadcoach"]}}}
    job = {"report": str(report_path), "lease": str(lease), "profile": profile, "id": new_id(), "token": "private-token"}
    events = []
    def call(host, operation, **payload):
        events.append(operation)
        return {"job": job["id"]} if operation == "lock-status" else {}
    smoke = lambda *a: {name: "passed" for name in migration.SMOKES}
    for state in ("cutover", "accepted"):
        events.clear()
        migration.write_json(report_path, {"state": state, "plan": plan, "acceptance": {name: "passed" for name in migration.SMOKES}})
        migration.write_json(lease, {})
        result = migration.recover_job(job, call, smoke)
        assert not lease.exists() and events.count("unlock") == 2
        if state == "accepted":
            assert result["state"] == "complete" and "restore" not in events
        else:
            assert result["state"] == "rolled-back" and events.count("restore") == 2


def test_private_acceptance_integrity_includes_referenced_secret_files(tmp_path):
    body = tmp_path / "sign-in.json"
    migration.write_json(body, {"name": "owner", "password": "first-secret"})
    smoke = tmp_path / "smoke.json"
    migration.write_json(smoke, {"sign-in": [{"body_file": str(body)}]})
    first = migration.acceptance_fingerprint(smoke)
    migration.write_json(body, {"name": "owner", "password": "second-secret"})
    assert migration.acceptance_fingerprint(smoke) != first


@pytest.mark.parametrize("accept_target", [True, False])
def test_coordinator_with_real_stores_backups_configs_and_validation(tmp_path, monkeypatch, accept_target):
    (tmp_path / "settings").mkdir()
    (tmp_path / "data").mkdir()
    source, key = seeded(tmp_path / "data/loadcoach.db", tmp_path)
    original_bytes = source.path.read_bytes()
    history = Database.sqlite(tmp_path / "data/spotter.db")
    from db.schemas import SPOTTER
    with history.connection() as connection:
        migrate(history, connection, "spotter.history", SPOTTER)
    for name in ("loadcoach", "spotter", "toollocker"):
        (tmp_path / "settings" / (name + ".toml")).write_text('data="../data"\n')
    monkeypatch.setattr(migration, "systemctl", lambda *a, **kw: SimpleNamespace(returncode=1))
    acceptance = tmp_path / "keys/acceptance.json"
    migration.write_json(acceptance, {name: [{"url": "http://fixture", "assert": {"ok": True}}] for name in migration.SMOKES})
    profile = {"schema": 1, "hosts": {"combined": {"root": str(tmp_path)}}, "owner": "combined", "binding": "loadcoach.core",
               "target": {"engine": "sqlite", "path": str(tmp_path / "data/target.db")}, "smoke_file": str(acceptance)}
    def call(host, operation, **payload):
        return migration.handle({"root": host["root"], "operation": operation, **payload})
    plan = migration.preview(profile, call)
    assert plan["approval"] == migration.preview(profile, call)["approval"]
    checks = []
    def smoke(filename, token):
        inventory = migration.host_inventory(tmp_path)
        selected = next(ref for ref in inventory["references"] if ref["binding"] == "loadcoach.core")
        current = migration.database(selected["database"])
        checks.append(current.path)
        assert Users(current, {"admin": read_group("admin", {"can": ["superuser"]})}).by_key(key).name == "owner"
        if not accept_target and len(checks) == 1:
            raise MigrationError("Acceptance check failed: request")
        return {name: "passed" for name in migration.SMOKES}
    job = new_id()
    report = migration.run_job(profile, plan["approval"], job, tmp_path / "report.json", call, smoke)
    assert report["state"] == ("complete" if accept_target else "rolled-back")
    assert report["recovery"]["combined"]["verification"]["state"] == "verified"
    assert not migration._marker(tmp_path).exists()
    assert source.path.read_bytes() == original_bytes
    inventory = migration.host_inventory(tmp_path)
    refs = {ref["binding"]: migration.database(ref["database"]).path.resolve() for ref in inventory["references"]}
    expected = tmp_path / "data/target.db" if accept_target else source.path
    assert refs["loadcoach.core"] == refs["toollocker.identity"] == refs["spotter.identity"] == expected
    assert refs["spotter.history"] == history.path
    assert (tmp_path / "data/target.db").is_file()  # retained even on rollback


def test_functional_smoke_uses_private_files_session_extraction_and_content_assertions(tmp_path, monkeypatch):
    body, headers, checks = tmp_path / "body.json", tmp_path / "headers.json", tmp_path / "checks.json"
    migration.write_json(body, {"name": "owner", "password": "fixture-password"})
    migration.write_json(headers, {"authorization": "Bearer fixture-application-key"})
    definitions = {name: [{"url": "http://fixture/" + name, "headers": {"authorization": "Bearer ${session}"}, "assert": {"ok": True}}] for name in migration.SMOKES}
    definitions["sign-in"] = [{"method": "POST", "url": "http://fixture/sign-in", "status": 201,
                                "body_file": str(body), "headers_file": str(headers), "assert": {"name": "owner"}, "extract": {"session": "key"}}]
    definitions["benchmark"] = [{"url": "http://fixture/benchmark", "contains": ["saved evidence"]}]
    migration.write_json(checks, definitions)
    visited = []
    def respond(request):
        visited.append(request.url.path)
        assert request.headers[HEADER] == "maintenance-secret"
        if request.url.path == "/sign-in":
            assert json.loads(request.content)["password"] == "fixture-password"
            assert request.headers["authorization"] == "Bearer fixture-application-key"
            return httpx.Response(201, json={"name": "owner", "key": "session-secret"})
        if request.url.path == "/benchmark":
            return httpx.Response(200, text="retained saved evidence")
        assert request.headers["authorization"] == "Bearer session-secret"
        return httpx.Response(200, json={"ok": True})
    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(respond), **kw))
    result = migration.smoke_checks(checks, "maintenance-secret", timeout=0)
    assert result == {name: "passed" for name in migration.SMOKES} and len(visited) == 6
    assert "secret" not in json.dumps(result) and "password" not in json.dumps(result)
    definitions["tool"][0]["assert"] = {"ok": False}
    migration.write_json(checks, definitions)
    with pytest.raises(MigrationError, match="Acceptance check failed: tool"):
        migration.smoke_checks(checks, "maintenance-secret", timeout=0)


def test_turnstile_resolves_weightroom_plugin_binding_without_opening_legacy_file(tmp_path):
    from turnstile.app import Settings, build_app
    root, data, target = tmp_path / "plugins", tmp_path / "data", tmp_path / "named.db"
    root.mkdir()
    settings = Settings(compute="127.0.0.1", data=str(data), plugins_dir=str(root), databases={
        "stores": {"plugin_log": {"engine": "sqlite", "path": str(target)}},
        "bindings": {"weightroom.plugins": "plugin_log"}})
    with TestClient(build_app(settings)) as client:
        assert client.app.state.plugins.registry.db.database.path == target
        assert target.is_file() and not (data / "plugins.db").exists()
