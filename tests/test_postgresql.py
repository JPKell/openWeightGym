"""Repository contracts against a disposable PostgreSQL database selected by the test runner."""

from __future__ import annotations

import os
import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from benchmarks.store import Benchmarks
from db import Database, DatabaseRegistry, migrate
from db.recovery import RecoveryStore, inspect_store
from db.schemas import SPOTTER, TASKS, TOOL_SCHEMAS
from loadcoach.batches import Batches
from loadcoach.app import build_app
from loadcoach.config import Config
from loadcoach.groups import read_group
from loadcoach.log import Log
from loadcoach.backups import create_backup, new_id
from loadcoach.database_admin import DatabaseSource as AdminSource, catalogs, make_sql_dump
from loadcoach.models import Models
from loadcoach.outside import Services
from loadcoach.reliability import Reliability
from loadcoach.role_evidence import RoleEvidence
from loadcoach.roles import Roles
from loadcoach.settings import Changes
from loadcoach.usage import report
from loadcoach.users import Users
from front_helpers import loadcoach_through
from plugins._registry import Registry
from weightroom.chat_store import Conversations
from weightroom.alerts import Alerts
from weightroom.schedules import Schedules
from loadcoach.schedule_grants import ScheduleGrants


def test_postgresql_db1_history_import_is_idempotent_and_refuses_conflicts(postgres, tmp_path):
    import sqlite3
    from db.consolidation import readonly
    from db.consolidation_import import telemetry, reliability

    log = Log(postgres)
    users = Users(postgres, {"admin": read_group("admin", {"can": ["superuser"]})})
    users.add("owner", "admin")
    kept = Reliability(log.db)
    host = "a" * 64
    with log.db:
        log.db.execute("INSERT INTO machine_hosts VALUES(?,?,?,?)", (host, "test", '[]', 2))
    source = tmp_path / "history.db"
    with sqlite3.connect(source) as old:
        old.executescript('CREATE TABLE readings(at REAL PRIMARY KEY,numbers TEXT);')
        old.execute("INSERT INTO readings VALUES(?,?)", (1, '{"memory":10}'))
    with readonly(source) as old:
        assert telemetry(old, log.db, host, apply=True)["after"] == 1
        assert telemetry(old, log.db, host, apply=True)["missing"] == 0
        with log.db:
            log.db.execute("UPDATE machine_telemetry SET numbers='{}'")
        with pytest.raises(ValueError, match="conflict"):
            telemetry(old, log.db, host, apply=True)
    past = Log(str(tmp_path / "reliability.db"))
    previous = Reliability(past.db)
    previous.record(7, "owner", "small", ("mid", "fp", ""), 0, "success", now=10, owner_id=users.identity("owner"))
    past.db.close()
    log.take("compute", [{"id": 7, "at": 10, "kind": "request", "what": "asked", "user": "owner",
        "caller": "test", "model": "small", "trace": None, "level": None,
        "detail": '{"model_id":"mid"}', "text": None, "hash": None}], [], 0)
    with readonly(tmp_path / "reliability.db") as old:
        assert reliability(old, users.db, kept.db, apply=True)["after"] == 1
        assert reliability(old, users.db, kept.db, apply=True)["missing"] == 0
    log.db.close(); users.db.close()


def test_postgresql_front_task_creation_finish_and_transfer(postgres, tmp_path):
    import asyncio
    import httpx
    import secrets
    from starlette.requests import Request
    from turnstile.tasks import FrontTasks
    from db.transfer import copy_store

    users = Users(postgres, {"admin": read_group("admin", {"can": ["superuser"]})})
    users.add('owner', 'admin')
    log, batches = Log(postgres), Batches(postgres)
    evidence = RoleEvidence(log.db)
    async def ask(user, method, path, *args, **kwargs):
        values = {'/v1/models': {'data': [{'id': 'small', 'owned_by': 'loadcoach', 'model_id': 'mid', 'content_sha256': 'a' * 64}]},
                  '/loadcoach/probe-subject/small': {'fingerprint': 'fp', 'subject': {'model_id': 'mid', 'settings': {'flags': {}}}, 'front_probe_dispatch': 1},
                  '/loadcoach/probe-environment': {'engine_hash': 'engine', 'machine_hash': 'machine'}}
        return httpx.Response(200, json=values[path]) if path in values else httpx.Response(404, json={})
    async def dispatch(*args):
        return None
    tasks = FrontTasks(batches, users, log, evidence, ask, dispatch, ['compute'], True)
    def request(path, body, headers=()):
        content = json.dumps(body).encode()
        async def receive():
            return {'type': 'http.request', 'body': content, 'more_body': False}
        return Request({'type': 'http', 'method': 'POST', 'path': path, 'headers': list(headers), 'query_string': b''}, receive)
    number, token = 'batch_' + secrets.token_hex(12), secrets.token_urlsafe(32)
    body = {'id': number, 'token': token, 'model': 'small'}
    async def run():
        assert (await tasks.create(request('/loadcoach/tasks', body), users.by_name('owner'))).status_code == 201
        assert (await tasks.create(request('/loadcoach/tasks', body), users.by_name('owner'))).status_code == 200
        finish = {'state': 'completed', 'result': {'ok': True}}
        assert (await tasks.finish(request('', finish), tasks.get(number))).status_code == 200
        assert (await tasks.finish(request('', finish), tasks.get(number))).status_code == 200
        assert batches.db.execute('SELECT COUNT(*) FROM files').fetchone()[0] == 2
        assert batches.db.execute('SELECT COUNT(*) FROM front_task_finishes').fetchone()[0] == 1
    asyncio.run(run())
    batches.db.close(); log.db.close(); users.db.close()
    target = Database.sqlite(tmp_path / 'front-copy.db')
    assert copy_store(postgres, target)['state'] == 'verified'
    with target.connection() as copied:
        assert copied.execute('SELECT machine FROM external_tasks WHERE id=?', (number,)).fetchone() == ('compute',)
        assert copied.execute('SELECT COUNT(*) FROM front_task_finishes').fetchone() == (1,)


def test_postgresql_recurring_schedules_grants_and_transfer(postgres, tmp_path):
    from db import Database
    from db.transfer import copy_store
    from test_schedules import GROUPS, NOW, spec

    users = Users(postgres, GROUPS)
    users.add('owner', 'admin')
    grants = ScheduleGrants(users.db, users)
    value = spec(missed='latest')
    grants.put('a'*32, 'x'*40, value, users.by_name('owner'))
    kept = Schedules(postgres)
    kept.create('a'*32, 'Daily', users.identity('owner'), 'owner', value, 'x'*40, NOW)
    due = kept.rows()[0]['next_at']
    kept.claim(due+2*86400)
    kept.claim(due+2*86400)
    assert len(kept.occurrences()) == 3 and len(kept.occurrences(active=True)) == 1
    number = kept.occurrences(active=True)[0]['id']
    kept.finish(number, 'completed', 'Fixture completion', due+2*86400)
    kept.db.close(); users.db.close()
    target = Database.sqlite(tmp_path/'schedule-copy.db')
    assert copy_store(postgres, target)['state'] == 'verified'
    restored = Schedules(target)
    assert restored.occurrences()[0]['state'] == 'completed'
    assert restored.rows()[0]['grant_token'] == 'x'*40
    identities = Users(target, GROUPS)
    assert ScheduleGrants(identities.db, identities).authorized('a'*32, 'x'*40)[0].name == 'owner'
    restored.db.close(); identities.db.close()


def test_postgresql_alert_episodes_and_transfer(postgres, tmp_path):
    from db import Database
    from db.transfer import copy_store

    kept = Alerts(postgres)
    reading = {'key': 'gpu:0', 'active': True, 'title': 'GPU heat', 'explanation': '90°C'}
    kept.observe('GPU temperature', [reading], 100)
    identifier = kept.rows(open_only=True)[0]['id']
    kept.acknowledge(identifier, 'owner')
    kept.observe('GPU temperature', [reading], 110)
    kept.observe('GPU temperature', None, 120)
    assert kept.rows(open_only=True)[0]['state'] == 'unknown'
    kept.observe('GPU temperature', [dict(reading, active=False)], 130)
    assert not kept.rows(open_only=True) and kept.rows()[0]['acknowledged_by'] == 'owner'
    kept.db.close()
    target = Database.sqlite(tmp_path / 'alerts.db')
    assert copy_store(postgres, target)['state'] == 'verified'
    restored = Alerts(target)
    assert restored.rows()[0]['id'] == identifier and restored.rows()[0]['resolved_at'] == 130
    restored.db.close()


@pytest.fixture
def postgres():
    dsn_file = os.environ.get("SUITE_TEST_POSTGRES_DSN_FILE")
    if not dsn_file:
        pytest.skip("set SUITE_TEST_POSTGRES_DSN_FILE to run PostgreSQL repository contracts")
    database = Database.postgresql(dsn_file, "test-postgresql", "test PostgreSQL")
    with database.connection() as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND usename = current_user AND backend_type = 'client backend'"
        )
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")
        connection.commit()
    return database


def test_postgresql_operational_health_and_backup_identity(postgres, tmp_path, backup_keys):
    from db.health import stores_health
    from loadcoach.backups import manifests

    with postgres.transaction() as connection:
        connection.execute("CREATE TABLE health_fixture (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO health_fixture VALUES (1)")
    store = RecoveryStore("health", postgres, ("loadcoach.core", "toollocker.identity"))
    row = stores_health([store], "compute")[0]
    assert row["state"] == "reachable"
    assert row["facts"]["connection_limit"] >= row["facts"]["connections"] > 0
    assert row["facts"]["autovacuum"] and not row["facts"]["read_only"]
    assert row["services"] == ["loadcoach", "toollocker"]
    root = tmp_path / "backups"
    backup_keys.configure(root)
    create_backup(root, new_id(), "compute", [], [], [], [], 0, stores=[store])
    evidence = manifests(root)
    assert stores_health([store], "compute", evidence)[0]["backup"]["state"] == "fresh"
    evidence[0]["stores"][0]["source_identity"] = "different-database"
    assert stores_health([store], "compute", evidence)[0]["backup"]["state"] == "missing"
    evidence[0]["stores"][0].pop("source_identity")
    assert stores_health([store], "compute", evidence)[0]["backup"]["state"] == "missing"


def test_postgresql_health_with_an_unprivileged_login(postgres, monkeypatch):
    import uuid
    from psycopg import sql
    from db.health import stores_health

    role = "health_" + uuid.uuid4().hex
    admin = postgres.connect(isolation_level=None)
    admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
        sql.Identifier(role), sql.Literal("disposable-test-password")).as_string())
    reader = Database.postgresql(postgres.store.dsn_file, "reader")
    monkeypatch.setattr(reader, "connect", lambda **options: postgres.connect(
        user=role, password="disposable-test-password", **options))
    store = RecoveryStore("reader", reader, ("loadcoach.core",))
    try:
        row = stores_health([store], "compute")[0]
        assert row["state"] == "reachable" and row["facts"]["connections"] >= 2
        admin.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(role)).as_string())
        row = stores_health([store], "compute")[0]
        assert row["state"] == "warning" and row["facts"]["read_only"]
        assert any("writers may fail" in warning for warning in row["warnings"])
    finally:
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)).as_string())
        admin.close()


def test_postgresql_llama_engine_roundtrip_preserves_identity_and_measurements(postgres, tmp_path):
    import hashlib
    weights = tmp_path / 'engine-fixture.gguf'
    weights.write_bytes(b'C09 persistence fixture, not native weights')
    legacy = {'gpus': 1, 'flags': {'model': str(weights), 'ctx-size': 2048}}
    models = Models(postgres)
    try:
        models.put('legacy', legacy)
        stat = weights.stat()
        models.keep_hash(str(weights), hashlib.sha256(weights.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime)
        identity = models.identity('legacy')
        fingerprint = models.fingerprint(models.get('legacy'), 'legacy')
        models.measured('legacy', legacy, [512])
        models.resident_measure('legacy', legacy, 256)
        models.put('legacy', {'engine': 'llama_cpp', **legacy})
        explicit = models.get('legacy')
        assert explicit['engine'] == 'llama_cpp'
        assert models.identity('legacy') == identity and models.file_revision('legacy') == 0
        assert models.fingerprint(explicit, 'legacy') == fingerprint
        assert models.measure('legacy')['mib'] == [512]
        assert models.resident_measure('legacy', explicit) == 256
        models.db.close()
        models = Models(postgres)
        assert models.stored('legacy')['engine'] == 'llama_cpp'
        assert models.fingerprint(models.get('legacy'), 'legacy') == fingerprint
        changes = Changes(postgres)
        try:
            engines = {'llama_cpp': {'enabled': True, 'executable': '/fixture/llama-server'}}
            changes.put('loadcoach', 'engines', engines, 'owner')
            assert changes.of('loadcoach')['engines'] == engines
        finally:
            changes.db.close()
    finally:
        models.db.close()


def test_postgresql_loadcoach_repositories(postgres, tmp_path):
    changes = Changes(postgres)
    changes.put("weightroom", "timezone", "UTC", "owner")
    assert changes.of("weightroom") == {"timezone": "UTC"}

    services = Services(postgres, tmp_path)
    services.put("remote", {"url": "https://example.test", "models": {}})
    assert services.names() == ["remote"]

    users = Users(postgres, {"plain": read_group("plain", {})})
    users.add("pat", "plain")
    users.change("pat", enabled=True, password="long-enough")
    key = users.new_key("pat", "test")
    assert users.by_key(key).name == "pat"
    users.rename("pat", "pat-renamed")
    assert users.by_key(key).name == "pat-renamed"

    log = Log(postgres, keep_text=2)
    request = log.add("request", "asked", "pat-renamed", "local", model="small")
    ending = log.add("request", "done", "pat-renamed", "local", model="small", of=request,
                     prompt_tokens=10, answer_tokens=4, tokens_per_second=2.5, cost=.01)
    assert ending > request > 0
    assert log.about(request)[-1]["answer_tokens"] == 4
    assert log.usage("pat-renamed")["prompt_tokens"] == 10
    from loadcoach.log import public_number
    assert log.requests(None, 10, completed=True)["requests"][0]["id"] == public_number(None, request)
    assert log.paces()["speed"]["small"] == 2.5
    assert log.spent("pat-renamed") == .01
    usage = report(log.db, {"period": "1", "timezone": "UTC"}, groups={"pat-renamed": "plain"})
    assert usage["totals"]["requests"] == 1
    assert log.db.execute("SELECT data_type FROM information_schema.columns WHERE table_name='events' AND column_name='detail'").fetchone()[0] == "text"
    assert log.verify()[1] is None

    models = Models(postgres)
    models.put("small", {"flags": {"model": str(tmp_path / "small.gguf")}, "gpus": 0})
    assert models.identity("small") and models.names() == ["small"]
    roles = Roles(postgres)
    roles.put("chat", {"models": {"small": 1}})
    roles.put("code", {"models": {"small": 1}})
    assert roles.names() == ["chat", "code"]

    reliability = Reliability(log.db)
    evidence = RoleEvidence(log.db)
    identity = models.identity("small")
    reliability.record(request, "pat-renamed", "small", (identity, "fp", "chat"), 0, "success")
    assert reliability.summary((identity, "fp", "chat"))["counts"]["success"] == 1
    assert reliability.report()[0]["model_id"] == identity
    evidence.accept({"definitions": [{"id": "quality", "name": "Quality", "formula": "score",
                                       "direction": "high", "version": 1}],
                     "results": [{"model_id": identity, "fingerprint": "fp", "benchmark": "quality",
                                  "run": "run", "measured_at": time.time(), "score": 1.0,
                                  "formula": "score", "version": 1, "engine_hash": None,
                                  "machine_hash": None, "speed": None, "load": None}]})
    assert evidence.definitions()[0]["id"] == "quality"

    batches = Batches(postgres)
    upload = batches.put_file("pat-renamed", "requests.jsonl", "batch", b"{}")
    batch = batches.start("pat-renamed", "/v1/chat/completions", upload["id"],
                          [{"custom_id": "one", "body": {"model": "small"}}])
    batches.keep(batch["id"], 0, 200, "request", {"ok": True})
    assert batches.get(batch["id"])["request_counts"] == {"total": 1, "completed": 1, "failed": 0}
    assert batches.db.execute("SELECT data_type FROM information_schema.columns WHERE table_name='batch_lines' AND column_name='body'").fetchone()[0] == "jsonb"
    migrate(postgres, batches.db, "loadcoach.tasks", TASKS)
    batches.db.execute("INSERT INTO external_tasks VALUES(?,?,?,?,?,?,?)",
                       (batch["id"], "hash", "small", "fp", "{}", "{}", identity))
    batches.db.commit()


def test_postgresql_front_and_service_repositories(postgres, tmp_path):
    chats = Conversations(postgres)
    number = chats.start("identity", "Hello")
    chats.say(number, "user", "Hi")
    chats.answering(number, 0, "identity", "small")
    assert chats.get(number, "identity")["messages"][0]["content"] == "Hi"
    assert chats.cut_off()[0]["conversation"] == number
    chats.session(number, "plugin/instance/profile", "native-session")
    assert chats.get(number, "identity")["sessions"] == {"plugin/instance/profile": "native-session"}
    chats.answered(number, 0)
    assert chats.cut_off() == []

    benchmarks = Benchmarks(postgres)
    run = benchmarks.create("identity", "small", "load")
    assert benchmarks.get(run)["state"] == "creating"
    benchmarks.activate(run, {"fingerprint": "fp", "subject": {"settings": {"flags": {}}}, "environment": {}})
    benchmarks.plan(run, {"samples": ["load"]})
    benchmarks.keep(run, "load", {"success": True, "load_seconds": 1.0}, "answer", "log", {"case": {}})
    assert benchmarks.plan(run) == {"samples": ["load"]}
    assert benchmarks.samples(run)[0]["values"]["load_seconds"] == 1.0
    benchmarks.paused(True)
    assert benchmarks.paused() is True

    root, data = tmp_path / "plugins", tmp_path / "plugin-data"
    root.mkdir()
    registry = Registry(root, data, postgres)
    actor = {"identity": "identity", "name": "pat", "can": ["see-log"]}
    registry.record(actor, "alias", "plugin", "instance", "profile", "done")
    assert registry.activity(actor)[0]["model"] == "alias"

    connection = postgres.connect()
    migrate(postgres, connection, "spotter.history", SPOTTER)
    connection.execute("INSERT INTO readings VALUES(?,?)", (time.time(), "{}"))
    migrate(postgres, connection, "toollocker.tools", TOOL_SCHEMAS)
    connection.execute("INSERT INTO tool_schemas VALUES(?,?)", ("server__tool", "{}"))
    connection.commit()
    assert set(postgres.tables(connection)) >= {"readings", "tool_schemas", "uses"}


def test_mixed_store_registry_keeps_sqlite_and_postgresql_independent(postgres, tmp_path):
    registry = DatabaseRegistry.from_config(
        {"stores": {
            "local": {"engine": "sqlite", "path": "chat.db"},
            "server": {"engine": "postgresql", "dsn_file": os.environ["SUITE_TEST_POSTGRES_DSN_FILE"]},
        }, "bindings": {"weightroom.chat": "local", "weightroom.benchmarks": "server"}},
        tmp_path,
    )
    assert registry.database("weightroom.chat").engine == "sqlite"
    assert registry.database("weightroom.benchmarks").engine == "postgresql"


def test_loadcoach_application_starts_and_serves_from_postgresql(postgres, tmp_path):
    # Both LoadCoach's own store and front's accounts on PostgreSQL, with turnstile in front as on the two machines.
    accounts = Users(postgres, {"admin": read_group("admin", {"can": ["superuser"]})})
    with loadcoach_through(tmp_path, accounts, database=postgres, keys=str(tmp_path), token_file=str(tmp_path / "loadcoach.token"),
                           model_idle_minutes=0) as client:
        assert client.get("/loadcoach/status").status_code == 200
        created = client.post("/loadcoach/users", json={"name": "owner", "group": "admin"})
        assert created.status_code == 201
        headers = {"authorization": "Bearer " + created.json()["key"]}
        assert client.get("/loadcoach/users", headers=headers).json()["users"][0]["name"] == "owner"


def test_postgresql_backup_is_listed_restored_and_exported_without_credentials(postgres, tmp_path, backup_keys):
    backup_keys.configure(tmp_path / 'backups')
    changes = Changes(postgres)
    changes.put("weightroom", "timezone", "UTC", "owner")
    with postgres.transaction() as connection:
        connection.execute('CREATE TABLE "wild*name" (value TEXT NOT NULL)')
        connection.execute('CREATE TABLE "wildXname" (value TEXT NOT NULL)')
        connection.execute('INSERT INTO "wild*name" VALUES (?)', ("only-star",))
        connection.execute('INSERT INTO "wildXname" VALUES (?)', ("not-selected",))
    store = RecoveryStore("compute.core", postgres, ("loadcoach.core",))

    inventory = inspect_store(store)
    assert inventory["status"] == "included"
    assert inventory["schema_versions"]["loadcoach.settings"] == 1
    backup_id = new_id(1_700_000_000, b"\x0a\x0b\x0c")
    made = create_backup(
        tmp_path / "backups",
        backup_id,
        "compute",
        [],
        [inventory],
        [],
        ["loadcoach.service"],
        1,
        stores=[store],
    )
    recorded = made["stores"][0]
    assert recorded["engine"] == "postgresql"
    assert recorded["verification"]["state"] == "verified"
    assert recorded["verification"]["counts"]["public.settings_changed"] == 1
    assert recorded["verification"]["schema_versions"] == recorded["schema_versions"]
    assert recorded["verification"]["restore_list_entries"] > 0
    from loadcoach.backup_crypto import ARCHIVE
    from loadcoach.backup_restore import verify
    recovered = verify(tmp_path / 'backups' / backup_id / ARCHIVE, backup_keys.identity,
                       output=tmp_path / 'staged-recovery')
    assert recovered['authenticated'] and recovered['recovery_key_tested']
    assert recovered['postgresql_restore_required'] and not recovered['live_data_changed']
    serialized = json.dumps(made)
    assert "suite_p2_test" not in serialized and "postgresql://" not in serialized

    source = AdminSource("core", None, "LoadCoach", database=postgres, bindings=("loadcoach.core",))
    catalog = catalogs({"core": source}, "compute")[0]
    assert catalog["engine"] == "PostgreSQL"
    assert any(table["name"] == "public.settings_changed" and table["records"] == 1 for table in catalog["tables"])
    dump, method = make_sql_dump(source, "public.settings_changed")
    try:
        text = dump.read_text()
        assert method == "pg_dump plain SQL"
        assert "CREATE TABLE public.settings_changed" in text and "UTC" in text
        assert "CREATE TABLE public.suite_schema_migrations" not in text
    finally:
        dump.unlink(missing_ok=True)
    exact, _ = make_sql_dump(source, "public.wild*name")
    try:
        text = exact.read_text()
        assert "only-star" in text and "not-selected" not in text
        assert 'CREATE TABLE public."wild*name"' in text
    finally:
        exact.unlink(missing_ok=True)


def test_offline_sqlite_postgresql_sqlite_roundtrip_preserves_identity_audit_and_blobs(postgres, tmp_path):
    from db.transfer import copy_store, evidence, reader, sequences
    from test_database_migration import seeded
    source, key = seeded(tmp_path / "original.db", tmp_path)
    # Retain raw answers/logs even though PostgreSQL TEXT cannot store NUL.
    kept = Benchmarks(source)
    kept.keep('nul-fixture', 'nul', {}, 'música\0answer', 'driver\0log', {})
    kept.keep('plain-fixture', 'plain', {}, 'unchanged', 'unchanged', {})
    kept.keep('plain-fixture', 'plain', {}, 'ignored\0retry', 'ignored\0retry', {})
    assert kept.samples('plain-fixture')[0]['output'] == 'unchanged'
    kept.db.close()
    source_bytes = source.path.read_bytes()
    with reader(source) as original:
        before = evidence(original)
    migrated = copy_store(source, postgres)
    assert migrated["state"] == "verified" and migrated["chain"]["checked"] == 1
    assert source.path.read_bytes() == source_bytes
    with reader(postgres) as target:
        assert evidence(target) == before and sequences(target)["messages"] == 900
    assert Users(postgres, {"admin": read_group("admin", {"can": ["superuser"]})}).by_key(key).name == "owner"
    target_benchmarks = Benchmarks(postgres)
    assert target_benchmarks.samples('nul-fixture')[0]['output'] == 'música\0answer'
    assert target_benchmarks.samples('nul-fixture')[0]['log'] == 'driver\0log'
    target_benchmarks.keep('native-nul', 'nul', {}, 'native\0output', 'native\0log', {})
    target_benchmarks.db.close()
    # A new event must extend the original hashes without recalculating old ones.
    log = Log(postgres)
    assert log.verify() == (1, None)
    log.add("migration", "accepted", "owner")
    assert log.verify() == (2, None)
    roundtrip = Database.sqlite(tmp_path / "roundtrip.db")
    returned = copy_store(postgres, roundtrip)
    assert returned["chain"]["checked"] == 2
    assert Log(roundtrip).verify() == (2, None)
    returned_benchmarks = Benchmarks(roundtrip)
    assert returned_benchmarks.samples('native-nul')[0]['output'] == 'native\0output'
    returned_benchmarks.db.close()
    chats = Conversations(roundtrip)
    chats.say(1, "assistant", "after roundtrip")
    assert chats.db.execute("SELECT MAX(id) FROM messages").fetchone()[0] == 901


def test_postgresql_parallel_event_writers_extend_one_valid_chain(postgres):
    from concurrent.futures import ThreadPoolExecutor
    logs = [Log(postgres) for _ in range(4)]
    def append(index):
        for number in range(10):
            logs[index].add("parallel", "proof", must=True, index=index, number=number)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(4)))
    assert logs[0].verify() == (39, None)
    for log in logs:
        log.db.close()


def test_turnstile_uses_shared_postgresql_plugin_store(postgres, tmp_path):
    from turnstile.app import Settings as TurnstileSettings, build_app as build_turnstile
    root = tmp_path / "plugins"
    root.mkdir()
    settings = TurnstileSettings(compute="127.0.0.1", data=str(tmp_path / "data"), plugins_dir=str(root), databases={
        "stores": {"shared_plugins": {"engine": "postgresql", "dsn_file": os.environ["SUITE_TEST_POSTGRES_DSN_FILE"]}},
        "bindings": {"weightroom.plugins": "shared_plugins"}})
    with TestClient(build_turnstile(settings)) as client:
        registry = client.app.state.plugins.registry
        assert registry.db.database.engine == "postgresql"
        registry.record({"identity": "stable", "name": "owner", "can": []}, "alias", "plugin", "instance", "profile", "done")
        assert registry.db.execute("SELECT COUNT(*) FROM uses").fetchone()[0] == 1
        assert not (tmp_path / "data/plugins.db").exists()
def test_retired_compute_transfers_across_engines_without_recreating_front_tables(postgres, tmp_path):
    from test_database_consolidation import retirement_fixture
    from db.consolidation_retire import retire
    from db.schemas import RETIRED_COMPUTE_TABLES
    from db.transfer import copy_store, tables
    source, proof = retirement_fixture(tmp_path)
    retire(source.path, proof, apply=True)
    # The deployed retired compute retained users v3; later account-owned tables belong to front.
    with source.transaction() as connection:
        connection.execute('DROP TABLE spending_allowances')
        connection.execute('DROP TABLE client_accesses')
        connection.execute("DELETE FROM suite_schema_migrations WHERE stream='loadcoach.users' AND version>3")
    assert copy_store(source, postgres)['state'] == 'verified'
    with postgres.connection() as connection:
        assert not set(RETIRED_COMPUTE_TABLES) & set(tables(connection))
        assert connection.execute('SELECT name,settings FROM models').fetchall() == [('kept', '{"flags":{}}')]
        assert 'spending_allowances' not in tables(connection)
        assert connection.execute("SELECT MAX(version) FROM suite_schema_migrations WHERE stream='loadcoach.users'").fetchone() == (3,)
    back = Database.sqlite(tmp_path / 'returned.db')
    assert copy_store(postgres, back)['state'] == 'verified'


@pytest.mark.parametrize('outcome', ['accepted', 'failed', 'interrupted'])
def test_postgresql_private_migration_acceptance_rollback_and_recovery(postgres, tmp_path, monkeypatch, outcome, backup_keys):
    """Real engines/configuration/recovery; six repository-backed smoke fixtures."""
    import dataclasses
    from types import SimpleNamespace
    from db import migration
    from db.transfer import MigrationError, copy_store
    from test_database_migration import seeded

    (tmp_path / 'settings').mkdir()
    (tmp_path / 'data').mkdir()
    backup_keys.configure(tmp_path / 'data/backups')
    source, key = seeded(tmp_path / 'data/loadcoach.db', tmp_path)
    with source.connection() as connection:
        migrate(source, connection, 'toollocker.tools', TOOL_SCHEMAS)
        with connection:
            connection.execute('INSERT INTO tool_schemas VALUES(?,?)', ('read-only-fixture', '{"type":"object"}'))
    for name in ('loadcoach', 'spotter', 'toollocker'):
        (tmp_path / 'settings' / (name + '.toml')).write_text('data="../data"\n')
    history = Database.sqlite(tmp_path / 'data/spotter.db')
    with history.connection() as connection:
        migrate(history, connection, 'spotter.history', SPOTTER)
    monkeypatch.setattr(migration, 'systemctl', lambda *a, **kw: SimpleNamespace(returncode=1))
    fixtures = tmp_path / 'keys/smoke.json'
    migration.write_json(fixtures, {name: [{'url': 'http://fixture/' + name, 'assert': {'fixture': True}}]
                                    for name in migration.SMOKES})
    secret = tmp_path / 'keys/target.dsn'
    secret.write_text(Path(postgres.store.dsn_file).read_text())
    secret.chmod(0o600)
    target_spec = {**dataclasses.asdict(postgres.store), 'dsn_file': str(secret)}
    profile = {'schema': 1, 'hosts': {'combined': {'root': str(tmp_path)}}, 'owner': 'combined',
               'binding': 'loadcoach.core', 'target': target_spec,
               'target_space': {'host': 'combined', 'path': str(tmp_path)}, 'smoke_file': str(fixtures)}
    migration.write_json(tmp_path / 'keys/profile.json', profile)
    assert migration.load_profile(tmp_path / 'keys', 'profile') == profile
    def call(host, operation, **payload):
        return migration.handle({'root': host['root'], 'operation': operation, **payload})
    checks = []
    def smoke(filename, token):
        inventory = migration.host_inventory(tmp_path)
        selected = next(ref for ref in inventory['references'] if ref['binding'] == 'loadcoach.core')
        current = migration.database(selected['database'])
        identities = Users(current, {'admin': read_group('admin', {'can': ['superuser']})})
        try:
            assert identities.by_key(key).name == 'owner'  # sign-in identity/key fixture
            db = identities.db
            assert db.execute('SELECT name FROM models').fetchone() == ('tiny',)
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='request'").fetchone()[0] == 2
            assert db.execute("SELECT content FROM messages WHERE role='assistant'").fetchone() == ('retained answer',)
            assert db.execute('SELECT COUNT(*) FROM benchmark_samples').fetchone()[0] == 1
            assert db.execute('SELECT tool FROM tool_schemas').fetchone() == ('read-only-fixture',)
            checks.append(current.engine)
            if outcome == 'failed' and len(checks) == 1:
                raise MigrationError('Acceptance check failed: request')
            return {name: 'passed' for name in migration.SMOKES}
        finally:
            identities.db.close()
    plan = migration.preview(profile, call)
    job, report_path = new_id(), tmp_path / 'report.json'
    if outcome == 'interrupted':
        call(profile['hosts']['combined'], 'lock', job=job, token='private-token')
        assert copy_store(source, postgres)['state'] == 'verified'
        call(profile['hosts']['combined'], 'switch', job=job, changes=plan['changes']['combined'])
        migration.write_json(report_path, {'state': 'cutover', 'plan': plan})
        lease = tmp_path / 'pending.json'
        migration.write_json(lease, {'id': job})
        result = migration.recover_job({'id': job, 'report': str(report_path), 'lease': str(lease),
                                       'profile': profile, 'token': 'private-token'}, call, smoke)
        assert result['state'] == 'rolled-back' and not lease.exists()
        assert checks == ['sqlite']
    else:
        result = migration.run_job(profile, plan['approval'], job, report_path, call, smoke)
        assert result['state'] == ('complete' if outcome == 'accepted' else 'rolled-back')
        assert checks == (['postgresql'] if outcome == 'accepted' else ['postgresql', 'sqlite'])
        assert result['recovery']['combined']['verification']['state'] == 'verified'
    assert not migration._marker(tmp_path).exists()
    assert source.path.is_file()


def test_postgresql_usage_sums_are_plain_numbers(postgres):
    """PostgreSQL sums BIGINTs as NUMERIC; the usage /loadcoach/me sends must still be plain integers."""
    log = Log(postgres)
    log.add("request", "done", "jpk", model="m", prompt_tokens=20, answer_tokens=11)
    usage = log.usage("jpk")
    assert usage == {"requests": 1, "prompt_tokens": 20, "answer_tokens": 11}
    assert all(type(value) is int for value in usage.values()) and json.dumps(usage)


def test_postgresql_user_details_serialize_nonzero_usage(postgres, tmp_path):
    import httpx
    from turnstile.app import Settings as TurnstileSettings, build_app as build_turnstile

    settings = TurnstileSettings(
        compute='127.0.0.1', data=str(tmp_path / 'data'), plugins_dir=str(tmp_path / 'plugins'),
        groups={'admin': read_group('admin', {'can': ['superuser']})},
        databases={'stores': {'accounts': {'engine': 'postgresql', 'dsn_file': os.environ['SUITE_TEST_POSTGRES_DSN_FILE']}},
                   'bindings': {'weightroom.accounts': 'accounts'}})
    wire = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    with TestClient(build_turnstile(settings, client=wire)) as client:
        users, log = client.app.state.users, client.app.state.log
        users.add('owner', 'admin')
        key = users.new_key('owner', 'test')
        log.add('request', 'done', 'owner', model='sample', prompt_tokens=20, answer_tokens=11)
        response = client.get('/loadcoach/users/owner', headers={'authorization': 'Bearer ' + key})
        assert response.status_code == 200, response.text
        for row in response.json()['usage']:
            assert row['requests'] == 1 and row['prompt_tokens'] == 20 and row['answer_tokens'] == 11
