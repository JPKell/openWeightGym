from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SUPPORT_PATH = ROOT / "scripts" / "install-suite-support.py"
SPEC = importlib.util.spec_from_file_location("install_suite_support", SUPPORT_PATH)
assert SPEC and SPEC.loader
support = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = support
SPEC.loader.exec_module(support)


def test_front_toollocker_audit_inherits_owner_store_without_creating_another(tmp_path):
    config = tmp_path / 'toollocker.toml'
    config.write_text('front = "weightroom.toml"\nport = 9443\n')
    (tmp_path / 'weightroom.toml').write_text('data = "front-data"\n')
    assert support.configured_targets(config, 'toollocker') == [('identity', 'sqlite', tmp_path / 'front-data/accounts.db')]
    original = config.read_text()
    assert support.configure(config, 'toollocker', 'sqlite', []) == 0
    assert config.read_text() == original
    import pytest
    with pytest.raises(ValueError, match='owning database'):
        support.configure(config, 'toollocker', 'postgresql', [])


def test_database_configuration_replaces_managed_tables_and_keeps_other_settings(tmp_path):
    config = tmp_path / "weightroom.toml"
    config.write_text(
        'host = "127.0.0.1"\n\n'
        '[databases.stores.old]\nengine = "sqlite"\npath = "old.db"\n\n'
        '[databases.bindings]\n"weightroom.chat" = "old"\n\n'
        '[turnstile]\nhost = "0.0.0.0"\nports = [8442]\n'
    )

    support.configure(config, "weightroom", "sqlite", [])
    first = config.read_text()
    support.configure(config, "weightroom", "sqlite", [])

    assert config.read_text() == first
    assert 'host = "127.0.0.1"' in first
    assert '[turnstile]' in first
    assert '[databases.stores.old]' not in first
    assert '[databases.stores.chat]' in first
    assert '"weightroom.plugins" = "plugins"' in first


def test_installer_audit_includes_front_roles_store_with_default_and_partial_bindings(tmp_path):
    settings = tmp_path / "settings"
    settings.mkdir()
    config = settings / "weightroom.toml"
    config.write_text('data = "../data"\n')
    expected = tmp_path / "data" / "roles.db"

    assert ("roles", "sqlite", expected) in support.configured_targets(config, "weightroom")

    config.write_text('data = "../data"\n[databases.stores.chat]\nengine = "sqlite"\npath = "../data/chat.db"\n[databases.bindings]\n"weightroom.chat" = "chat"\n')
    assert any(name == "default_weightroom_roles" and engine == "sqlite" and path == expected
               for name, engine, path in support.configured_targets(config, "weightroom"))

    expected.parent.mkdir()
    expected.write_text("not a SQLite database")
    assert support.audit(tmp_path, ["weightroom"]) == 2


def test_sqlite_compatibility_rejects_a_newer_schema(tmp_path):
    database = tmp_path / "future.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE suite_schema_migrations("
        "stream TEXT, version INTEGER, name TEXT, applied REAL, PRIMARY KEY(stream, version))"
    )
    connection.execute(
        "INSERT INTO suite_schema_migrations VALUES (?, ?, ?, 0)",
        ("loadcoach.users", 999, "future users"),
    )
    connection.commit()
    connection.close()

    findings = support.sqlite_findings(database)

    assert any(item.level == "ERROR" and "newer than this version of openWeightGym supports" in item.detail for item in findings)


def test_database_state_distinguishes_fresh_and_populated_sqlite(tmp_path):
    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / "loadcoach.toml").write_text('data = "../data"\n')

    assert support.database_state(tmp_path, ["loadcoach"]) == ("sqlite", False)

    data = tmp_path / "data"
    data.mkdir()
    connection = sqlite3.connect(data / "loadcoach.db")
    connection.execute("CREATE TABLE users(name TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    assert support.database_state(tmp_path, ["loadcoach"]) == ("sqlite", True)


def test_postgresql_error_does_not_disclose_dsn_password(tmp_path):
    dsn = tmp_path / "core.dsn"
    secret = "do-not-print-this-password"
    dsn.write_text(f"host=127.0.0.1 port=1 dbname=missing user=suite password={secret} connect_timeout=1\n")
    dsn.chmod(0o600)

    findings = support.postgresql_findings("core", dsn)
    rendered = "\n".join(item.detail for item in findings)

    assert any(item.level == "ERROR" for item in findings)
    assert secret not in rendered


def test_postgresql_audit_requires_compatible_restore_as_well_as_dump(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from db import Database
    from db import postgresql_tools

    dsn = tmp_path / 'private.dsn'
    dsn.write_text('private fixture')
    dsn.chmod(0o600)
    class Cursor:
        def __init__(self, value):
            self.value = value
        def fetchone(self):
            return (self.value,)
        def fetchall(self):
            return []
    @contextmanager
    def connection(**options):
        yield SimpleNamespace(execute=lambda statement: Cursor(
            180006 if 'server_version_num' in statement else '18.6'))
    monkeypatch.setattr(Database, 'postgresql', lambda *a, **kw: SimpleNamespace(connection=connection))
    monkeypatch.setattr(postgresql_tools, 'tool_version', lambda tool: f'{tool} (PostgreSQL) ' + ('16.0' if tool == 'pg_restore' else '18.6'))
    findings = support.postgresql_findings('isolated', dsn, require_tools=True)
    assert any(row.level == 'ERROR' and 'pg_restore major 16' in row.detail for row in findings)
    assert not any(row.level == 'ERROR' and 'pg_dump major' in row.detail for row in findings)


def test_check_mode_preselects_an_installed_service(tmp_path):
    install = tmp_path / "suite"
    (install / "scripts").mkdir(parents=True)
    (install / "settings").mkdir()
    (install / "db").mkdir()
    (install / "pyproject.toml").write_text('[project]\nname = "loadcoach"\nversion = "0.1.0"\n')
    (install / "scripts" / SUPPORT_PATH.name).write_text(SUPPORT_PATH.read_text())
    (install / "scripts" / "install-sdcpp.py").write_text((ROOT / "scripts/install-sdcpp.py").read_text())
    (install / "mirrorwall").mkdir()
    for filename in ("__init__.py","install.py","schema.py"):
        (install / "mirrorwall" / filename).write_text((ROOT / "mirrorwall" / filename).read_text())
    (install / "soundroom").mkdir()
    (install / "soundroom" / "install.py").write_text((ROOT / "soundroom/install.py").read_text())
    for source in (ROOT / "db").glob("*.py"):
        (install / "db" / source.name).write_text(source.read_text())
    (install / "settings" / "loadcoach.toml").write_text(
        'host = "127.0.0.1"\nport = 8442\ndata = "../data"\ngpus = 0\n'
    )
    home = tmp_path / "home"
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "loadcoach.service").write_text("[Service]\nExecStart=/missing/loadcoach\n")
    environment = {**os.environ, "HOME": str(home)}
    environment.pop("XDG_CONFIG_HOME", None)

    answer = subprocess.run(
        [str(ROOT / "scripts" / "install-suite.sh"), "--check", "--root", str(install), "--non-interactive"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        timeout=30,
    )

    assert answer.returncode == 0, answer.stdout
    assert "Installed openWeightGym units: loadcoach" in answer.stdout
    assert "Selected openWeightGym units: loadcoach" in answer.stdout
    assert "audio.cpp: not installed (optional)" in answer.stdout


def test_installer_refuses_to_switch_a_populated_store(tmp_path):
    install = tmp_path / "suite"
    (install / "scripts").mkdir(parents=True)
    (install / "settings").mkdir()
    (install / "data").mkdir()
    (install / "db").mkdir()
    (install / "pyproject.toml").write_text('[project]\nname = "loadcoach"\nversion = "0.1.0"\n')
    (install / "scripts" / SUPPORT_PATH.name).write_text(SUPPORT_PATH.read_text())
    for source in (ROOT / "db").glob("*.py"):
        (install / "db" / source.name).write_text(source.read_text())
    (install / "settings" / "loadcoach.toml").write_text(
        'host = "127.0.0.1"\nport = 8442\ndata = "../data"\ngpus = 0\n'
    )
    connection = sqlite3.connect(install / "data" / "loadcoach.db")
    connection.execute("CREATE TABLE users(name TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home)}
    environment.pop("XDG_CONFIG_HOME", None)

    answer = subprocess.run(
        [
            str(ROOT / "scripts" / "install-suite.sh"),
            "--root", str(install),
            "--services", "loadcoach",
            "--database", "postgresql",
            "--dry-run",
            "--non-interactive",
            "--yes",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        timeout=30,
    )

    assert answer.returncode == 2
    assert "will not switch engines without a verified migration" in answer.stdout


def test_one_machine_settings_move_compute_ports_and_fill_in_folders(tmp_path):
    import tomllib
    root, home = tmp_path / "home" / "bob" / "openWeightGym", tmp_path / "home" / "bob"
    made = {}
    for name in ("loadcoach", "weightroom", "toollocker", "spotter"):
        example = ROOT / "settings" / "examples" / f"{name}.toml"
        values = support.topology_values(name, "all", root, "192.168.1.50", "", "", "/opt/llama/llama-server")
        support.render(example, tmp_path / f"{name}.toml", root, home, values)
        text = (tmp_path / f"{name}.toml").read_text()
        assert "/home/you" not in text and "/home/jpk" not in text
        made[name] = tomllib.loads(text)
    # turnstile keeps 8442-8444 on this machine; compute's programs sit 1000 above, which weightroom's URL says.
    assert (made["loadcoach"]["host"], made["loadcoach"]["port"], made["loadcoach"]["front"]) == ("127.0.0.1", 9442, "http://127.0.0.1:8442")
    assert made["loadcoach"]["llama_server"] == "/opt/llama/llama-server" and made["loadcoach"]["models_folder"] == str(root / "models")
    assert made["weightroom"]["compute"] == "http://127.0.0.1:9442" and made["weightroom"]["turnstile"]["ports"] == [8442, 8443, 8444]
    assert (made["toollocker"]["port"], made["spotter"]["port"]) == (9443, 9444)
    assert made["toollocker"]["servers"]["files"]["command"][-1] == str(root / "data" / "shared")


def test_a_compute_node_listens_on_its_address_and_asks_front(tmp_path):
    import tomllib
    values = support.topology_values("loadcoach", "compute", tmp_path, "192.168.1.50", "http://192.168.1.10:8442/", "", "")
    support.render(ROOT / "settings/examples/loadcoach.toml", tmp_path / "loadcoach.toml", tmp_path, tmp_path, values)
    made = tomllib.loads((tmp_path / "loadcoach.toml").read_text())
    assert (made["host"], made["port"], made["front"]) == ("192.168.1.50", 8442, "http://192.168.1.10:8442")
    # A console with no compute node yet still starts; it shows compute as not answering.
    assert support.topology_values("weightroom", "front", tmp_path, "192.168.1.10", "", "", "")["spotter"] == ""


def test_setting_a_value_keeps_comments_and_adds_what_is_missing():
    text = '# about the host\nhost = "x"  # here\n# port = 1\n\n[turnstile]\nports = [1]\n\n[other]\nhost = "y"\n'
    text = support.set_toml_value(text, "host", "127.0.0.1")
    text = support.set_toml_value(text, "port", 9442)
    text = support.set_toml_value(text, "turnstile.host", "0.0.0.0")
    text = support.set_toml_value(text, "data", "../data")
    assert text.startswith('# about the host\nhost = "127.0.0.1"\nport = 9442\n')
    assert 'ports = [1]\nhost = "0.0.0.0"\n' in text and '[other]\nhost = "y"' in text
    assert text.index('data = "../data"') < text.index("[turnstile]")
    import pytest
    with pytest.raises(ValueError, match="several lines"):
        support.set_toml_value('command = [\n  "a",\n]\n', "command", ["b"])
