from __future__ import annotations

import importlib.util
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SUPPORT_PATH = ROOT / "scripts" / "install-suite-support.py"
SPEC = importlib.util.spec_from_file_location("install_suite_support", SUPPORT_PATH)
assert SPEC and SPEC.loader
support = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = support
SPEC.loader.exec_module(support)


@pytest.fixture(scope="module")
def local_postgres(tmp_path_factory):
    """A disposable PostgreSQL server with SCRAM authentication over TCP."""
    binary = os.environ.get("OWG_TEST_POSTGRES_BINDIR")
    initdb = str(Path(binary) / "initdb") if binary else shutil.which("initdb")
    if not initdb or os.geteuid() == 0:
        pytest.skip("requires PostgreSQL server binaries and an unprivileged test user")
    bindir = Path(initdb).parent
    work = tmp_path_factory.mktemp("installer-postgresql")
    sockets = Path(tempfile.mkdtemp(prefix="owg-pg-socket-"))
    data = work / "data"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    initialized = subprocess.run([initdb, "-D", str(data), "-U", "postgres", "--auth-local=trust",
                                  "--auth-host=scram-sha-256", "--no-locale", "-E", "UTF8", "--no-sync"],
                                 capture_output=True, text=True)
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    started = subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(work / "server.log"),
                              "-o", f"-h 127.0.0.1 -p {port} -k {sockets} -F -c jit=off", "-w", "start"],
                             capture_output=True, text=True)
    assert started.returncode == 0, started.stdout + started.stderr + (work / "server.log").read_text()
    commands = work / "commands"
    commands.mkdir()
    sudo = commands / "sudo"
    sudo.write_text('''#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} != -n ]] || shift
[[ ${1:-} == -u && ${2:-} == postgres ]] || exit 91
shift 2
exec "$@"
''')
    sudo.chmod(0o755)
    (commands / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n")
    (commands / "systemctl").chmod(0o755)
    environment = {**os.environ, "PGHOST": str(sockets), "PGPORT": str(port), "PGUSER": "postgres",
                   "PATH": f"{commands}{os.pathsep}{bindir}{os.pathsep}{os.environ['PATH']}",
                   "PYTHONPATH": str(ROOT), "OWG_NO_TTY": "1", "OWG_PLAIN": "1"}
    def admin(statement):
        answer = subprocess.run(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "postgres"],
                                input=statement, env=environment, text=True, capture_output=True)
        assert answer.returncode == 0, answer.stderr
        return answer.stdout.strip()
    try:
        yield {"environment": environment, "admin": admin, "port": port}
    finally:
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                       capture_output=True, timeout=30)
        shutil.rmtree(sockets)


def provision_live_postgres(root, server, user, *, expect_success=True):
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SUPPORT_PATH, root / "scripts/install-suite-support.py")
    suite = (ROOT / "scripts/install-suite.sh").read_text()
    functions = [re.search(r'(?m)^as_postgres\(\).*$', suite)[0]]
    for name in ("repair_postgresql", "provision_postgresql", "check_postgresql_login"):
        functions.append(re.search(rf"(?ms)^{name}\(\) \{{.*?^\}}", suite)[0])
    source = '''set -Eeuo pipefail
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
ROOT=$INSTALL PY=$TEST_PYTHON USER=$TEST_USER PROVISION_POSTGRESQL=1 DRY_RUN=0 CHECK_ONLY=0 PG_CONNECTIONS_CHECKED=0
declare -A DSNS=()
REQUIRED_DSNS=(accounts roles chat benchmarks plugins alerts schedules)
hostname() { printf pgtest; }
note() { printf '%s\\n' "$*"; }
die() { ui_die "$*"; }
try_step() { local label=$1; shift; ui_step "$label" -- "$@"; }
''' + "\n".join(functions) + '\nprovision_postgresql\n'
    environment = {**server["environment"], "INSTALL": str(root), "TEST_PYTHON": sys.executable,
                   "TEST_REPO": str(ROOT), "TEST_USER": user, "OWG_LOG": str(root / "install.log")}
    answer = subprocess.run(["bash", "-c", source], env=environment, text=True, capture_output=True, timeout=45)
    if expect_success:
        assert answer.returncode == 0, answer.stdout + answer.stderr + (root / "install.log").read_text()
    return answer


def assert_postgres_connections(root):
    import psycopg
    paths = sorted((root / "keys/postgresql").glob("*.dsn"))
    assert len(paths) == 7
    for path in paths:
        assert path.stat().st_mode & 0o077 == 0
        with psycopg.connect(path.read_text().strip(), connect_timeout=3) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("replacement_role", [False, True])
def test_real_postgres_reinstall_after_database_removal_keeps_credentials(tmp_path, local_postgres, replacement_role):
    root = tmp_path / "install"
    user = "fixture_" + tmp_path.name[-8:].replace("-", "_")
    provision_live_postgres(root, local_postgres, user)
    assert_postgres_connections(root)
    paths = sorted((root / "keys/postgresql").glob("*.dsn"))
    original = {path: path.read_bytes() for path in paths}
    from urllib.parse import urlsplit
    parsed = [urlsplit(content.decode().strip()) for content in original.values()]
    role = parsed[0].username
    for uri in parsed:
        local_postgres["admin"](f'DROP DATABASE "{uri.path.lstrip("/")}";')
    local_postgres["admin"](f'DROP ROLE "{role}";')
    if replacement_role:
        local_postgres["admin"](f'CREATE ROLE "{role}" LOGIN PASSWORD \'a-new-unrecorded-password\';')
    provision_live_postgres(root, local_postgres, user)
    assert_postgres_connections(root)
    assert {path: path.read_bytes() for path in paths} == original


@pytest.mark.parametrize("one_working_file", [False, True])
def test_real_postgres_password_recovery_preserves_existing_data(tmp_path, local_postgres, one_working_file):
    import psycopg
    from urllib.parse import urlsplit, urlunsplit
    root = tmp_path / "install"
    user = "fixture_" + tmp_path.name[-8:].replace("-", "_")
    provision_live_postgres(root, local_postgres, user)
    accounts = root / "keys/postgresql/accounts.dsn"
    with psycopg.connect(accounts.read_text().strip()) as connection:
        connection.execute("CREATE TABLE retained_user_data (value TEXT)")
        connection.execute("INSERT INTO retained_user_data VALUES ('keep this data')")
    uri = urlsplit(accounts.read_text().strip())
    password = "new-valid-password"
    local_postgres["admin"](f'ALTER ROLE "{uri.username}" PASSWORD \'{password}\';')
    if one_working_file:
        roles = root / "keys/postgresql/roles.dsn"
        current = urlsplit(roles.read_text().strip())
        roles.write_text(urlunsplit((current.scheme, f"{current.username}:{password}@{current.hostname}:{current.port}", current.path, "", "")) + "\n")
    provision_live_postgres(root, local_postgres, user)
    assert_postgres_connections(root)
    with psycopg.connect(accounts.read_text().strip()) as connection:
        assert connection.execute("SELECT value FROM retained_user_data").fetchall() == [("keep this data",)]
    assert password not in (root / "install.log").read_text()


@pytest.mark.parametrize("problem", ["foreign-owner", "foreign-port", "privileged-role"])
def test_real_postgres_repair_refuses_unmanaged_database_changes(tmp_path, local_postgres, problem):
    from urllib.parse import urlsplit, urlunsplit
    root = tmp_path / "install"
    user = "fixture_" + problem.replace("-", "_")
    provision_live_postgres(root, local_postgres, user)
    path = root / "keys/postgresql/accounts.dsn"
    uri = urlsplit(path.read_text().strip())
    expected = "privileged"
    if problem == "foreign-owner":
        local_postgres["admin"]("CREATE DATABASE foreign_database OWNER postgres;")
        changed = urlunsplit((uri.scheme, uri.netloc, "/foreign_database", "", "")) + "\n"
        path.write_text(changed)
        expected = "owner differs"
    elif problem == "foreign-port":
        wrong_port = 1 if uri.port == 65535 else uri.port + 1
        netloc = f"{uri.username}:{uri.password}@127.0.0.1:{wrong_port}"
        path.write_text(urlunsplit((uri.scheme, netloc, uri.path, "", "")) + "\n")
        expected = "server port differs"
    else:
        local_postgres["admin"](f'ALTER ROLE "{uri.username}" SUPERUSER;')
    changed = path.read_text()
    answer = provision_live_postgres(root, local_postgres, user, expect_success=False)
    assert answer.returncode != 0 and path.read_text() == changed
    assert expected in (root / "install.log").read_text()
    assert uri.password not in answer.stdout + answer.stderr + (root / "install.log").read_text()


def test_real_postgres_retained_connections_are_repaired_before_the_initial_audit(tmp_path, local_postgres):
    from urllib.parse import urlsplit
    root = tmp_path / "install"
    provision_live_postgres(root, local_postgres, "fixture_preflight")
    (root / "scripts/lib").mkdir()
    shutil.copy2(ROOT / "scripts/lib/ui.sh", root / "scripts/lib/ui.sh")
    shutil.copy2(ROOT / "scripts/lib/detect.sh", root / "scripts/lib/detect.sh")
    (root / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "0.1.0"\n')
    (root / "settings").mkdir()
    config = root / "settings/weightroom.toml"
    config.write_text('data = "../data"\n')
    files = sorted((root / "keys/postgresql").glob("*.dsn"))
    support.configure(config, "weightroom", "postgresql", [f"{path.stem}={path}" for path in files])
    role = urlsplit(files[0].read_text().strip()).username
    local_postgres["admin"](f'ALTER ROLE "{role}" PASSWORD \'invalid-for-retained-files\';')
    plan = (ROOT / "scripts/install-suite.sh").read_text().split("\ninstall_postgresql_packages\n", 1)[0]
    runner = root / "scripts/preflight.sh"
    runner.write_text(plan + "\n")
    environment = {**local_postgres["environment"], "OWG_LOG": str(root / "preflight.log"),
                   "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
                   "PATH": f"{Path(sys.executable).parent}{os.pathsep}{local_postgres['environment']['PATH']}"}
    answer = subprocess.run(["bash", str(runner), "--services", "weightroom,turnstile", "--database", "postgresql",
                             "--provision-postgresql", "--non-interactive", "--yes"],
                            env=environment, text=True, capture_output=True, timeout=45)
    assert answer.returncode == 0, answer.stdout + answer.stderr + (root / "preflight.log").read_text()
    assert "0 error(s)" in answer.stdout
    assert_postgres_connections(root)


@pytest.mark.skipif(shutil.which("uv") is None, reason="the bootstrap lifecycle requires uv")
@pytest.mark.parametrize("engine", ["sqlite", "postgresql"])
def test_piped_bootstrap_install_repair_uninstall_and_reinstall_preserves_data(tmp_path, request, engine):
    """Exercise real entry points, Git, Python packaging and SQLite without host services."""
    source = tmp_path / "source"
    source.mkdir()
    paths = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    for relative in filter(None, paths):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for args in (["init", "-b", "main"], ["config", "user.name", "Installer fixture"],
                 ["config", "user.email", "fixture@example.invalid"], ["add", "."], ["commit", "-m", "fixture"]):
        subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "main").symlink_to(source, target_is_directory=True)
    home = tmp_path / "home with spaces"
    home.mkdir()
    install = home / "openWeightGym"
    commands = tmp_path / "commands"
    commands.mkdir()
    # Calls that could affect the real machine fail; service-manager discovery is offline.
    for command in ("systemctl", "sudo"):
        (commands / command).write_text("#!/usr/bin/env bash\nexit 91\n")
    server = request.getfixturevalue("local_postgres") if engine == "postgresql" else None
    if server:
        for manager in ('apt-get', 'pacman', 'dnf', 'yum', 'zypper'):
            (commands / manager).write_text("#!/usr/bin/env bash\nexit 0\n")
        (commands / "sudo").write_text('''#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} != -n ]] || shift
if [[ ${1:-} == true ]]; then exit 0; fi
if [[ ${1:-} == -u && ${2:-} == postgres ]]; then shift 2; exec "$@"; fi
if [[ ${1:-} == systemctl && ${2:-} == enable ]]; then exit 0; fi
if [[ ${1:-} == test && ${2:-} == -f && ${3:-} == */PG_VERSION ]]; then exit 0; fi
if [[ ${1:-} == env ]]; then shift; while [[ ${1:-} == *=* ]]; do shift; done; fi
case "${1:-}" in apt-get|pacman|dnf|yum|zypper) exec "$@";; esac
exit 91
''')
    (commands / "loginctl").write_text('#!/usr/bin/env bash\n[[ $1 == show-user ]] && { echo no; exit 0; }; exit 91\n')
    real_uv = shlex.quote(shutil.which("uv"))
    interpreter = shlex.quote(sys.executable)
    bare_interpreter = shlex.quote(sys._base_executable)
    (commands / "uv").write_text(f'''#!/usr/bin/env bash
set -euo pipefail
if [[ ${{1:-}} == python && ${{2:-}} == install ]]; then exit 0; fi
if [[ ${{1:-}} == python && ${{2:-}} == find ]]; then printf '%s\\n' {bare_interpreter}; exit 0; fi
if [[ ${{1:-}} == venv ]]; then exec {real_uv} venv --seed --python {interpreter} "${{!#}}"; fi
exec {real_uv} "$@"
''')
    for command in commands.iterdir():
        command.chmod(0o755)
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(raw), **kwargs)
        def log_message(self, *args):
            pass
    web_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=web_server.serve_forever, daemon=True)
    thread.start()
    inherited = server["environment"] if server else os.environ
    environment = {**inherited, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                   "XDG_STATE_HOME": str(home / ".local/state"), "OWG_NO_TTY": "1", "OWG_PLAIN": "1",
                   "OWG_LOG": str(tmp_path / "bootstrap.log"), "OWG_ADMIN_PASSWORD": "fixture-password",
                   "OWG_RAW": f"http://127.0.0.1:{web_server.server_port}", "OWG_REPO": str(source),
                   "PATH": str(commands) + os.pathsep + inherited["PATH"]}
    environment.pop("PYTHONPATH", None)
    environment.pop("OWG_OWNER", None)
    environment.pop("OWG_OWNER_LOG", None)
    bootstrap = (ROOT / "install.sh").read_text()
    def run(*arguments):
        answer = subprocess.run(["bash", "-s", "--", "--dir", str(install), *arguments],
                                input=bootstrap, env=environment, cwd=home, text=True,
                                capture_output=True, timeout=180)
        assert answer.returncode == 0, answer.stdout + answer.stderr + (tmp_path / "bootstrap.log").read_text()
    fresh = ("--yes", "--topology", "front", "--database", engine, "--admin", "sam", "--llama", "skip", "--no-model", "--no-linger", "--no-browser")
    from loadcoach.users import Users
    from db import Database
    def accounts():
        store = Database.postgresql(install / "keys/postgresql/accounts.dsn") if engine == "postgresql" else str(install / "data/accounts.db")
        return Users(store, {})
    try:
        run(*fresh)
        receipt = install / ".install-receipt"
        assert "state=complete" in receipt.read_text()
        assert 'services=weightroom,turnstile,toollocker\n' in receipt.read_text()
        assert (home / '.config/systemd/user/toollocker.service').exists()
        assert not (install / 'settings/loadcoach.toml').exists()
        assert not (install / 'data/loadcoach.db').exists()
        gateway = tomllib.loads((install / 'settings/toollocker.toml').read_text())
        front = tomllib.loads((install / 'settings/weightroom.toml').read_text())
        assert (gateway['front'], gateway['host'], gateway['port']) == ('weightroom.toml', '127.0.0.1', 9443)
        assert front['turnstile']['keep_console_settings_on_front'] is True
        assert front['turnstile']['toollocker_url'] == 'http://127.0.0.1:9443'
        assert support.configured_targets(install / 'settings/toollocker.toml', 'toollocker') == [
            ('identity', engine, install / ('keys/postgresql/accounts.dsn' if server else 'data/accounts.db'))]
        unit = home / ".config/systemd/user/weightroom.service"
        command = next(line.split('=', 1)[1] for line in unit.read_text().splitlines() if line.startswith('ExecStart='))
        assert shlex.split(command) == [str(install / '.venv/bin/weightroom'), '--config', str(install / 'settings/weightroom.toml')]
        if shutil.which('systemd-analyze'):
            verified = subprocess.run(['systemd-analyze', '--user', '--man=no', 'verify', str(unit)],
                                      env=environment, capture_output=True, text=True, timeout=30)
            assert verified.returncode == 0, verified.stdout + verified.stderr
        users = accounts()
        password = users.password_hash("sam")
        assert password and users.by_name("sam").can("superuser")
        users.db.close()
        retained = install / "data/retained-note"
        retained.write_text("keep this user data")
        machine_key = (install / "keys/machine.key").read_bytes()
        if server:
            from urllib.parse import urlsplit
            role = urlsplit((install / "keys/postgresql/accounts.dsn").read_text().strip()).username
            server["admin"](f'ALTER ROLE "{role}" PASSWORD \'unrecorded-password-after-failure\';')
            receipt.write_text(receipt.read_text().replace('state=complete', 'state=pending'))
            run(*fresh)
            assert_postgres_connections(install)
        units = home / ".config/systemd/user"
        shutil.rmtree(units)
        run("--repair", "--yes", "--no-browser")
        assert all((units / f'{name}.service').exists() for name in ('weightroom', 'turnstile', 'toollocker'))
        run("--uninstall", "--yes")
        assert not (install / ".venv").exists() and not (install / "install.sh").exists()
        assert retained.read_text() == "keep this user data"
        run(*fresh)
        assert "state=complete" in receipt.read_text()
        assert retained.read_text() == "keep this user data"
        assert (install / "keys/machine.key").read_bytes() == machine_key
        users = accounts()
        assert users.password_hash("sam") == password
        users.db.close()
    finally:
        web_server.shutdown()
        web_server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('owner,target', [
    ('data = "front-data"\n', 'front-data/accounts.db'),
    ('', 'accounts.db'),
    ('[databases.stores.authority]\nengine = "sqlite"\npath = "authority.db"\n'
     '[databases.bindings]\n"weightroom.accounts" = "authority"\n', 'authority.db'),
])
def test_front_toollocker_audit_inherits_owner_store_without_creating_another(tmp_path, owner, target):
    config = tmp_path / 'toollocker.toml'
    config.write_text('front = "weightroom.toml"\nport = 9443\n')
    (tmp_path / 'weightroom.toml').write_text(owner)
    assert support.configured_targets(config, 'toollocker') == [('identity', 'sqlite', tmp_path / target)]
    original = config.read_text()
    assert support.configure(config, 'toollocker', 'sqlite', []) == 0
    assert config.read_text() == original
    import pytest
    with pytest.raises(ValueError, match='owning database'):
        support.configure(config, 'toollocker', 'postgresql', [])


def test_front_toollocker_audit_resolves_the_owners_defaults(tmp_path):
    """Audit and gateway startup must agree when the owner inherits its accounts binding."""
    from toollocker.door import shared_store
    config = tmp_path / 'toollocker.toml'
    config.write_text('front = "weightroom.toml"\n')
    (tmp_path / 'weightroom.toml').write_text('')
    (tmp_path / 'defaults').mkdir()
    (tmp_path / 'defaults/weightroom.toml').write_text('data = "retained-front"\n')
    assert support.configured_targets(config, 'toollocker')[0][2] == shared_store(config, {'front': 'weightroom.toml'})[1].path


def test_front_toollocker_preflight_runs_without_installed_runtime_packages(tmp_path):
    """A retained front gateway must not import Typer or ASGI dependencies before reinstalling Python packages."""
    settings = tmp_path / 'settings'
    settings.mkdir()
    (settings / 'weightroom.toml').write_text('data = "../data"\n')
    (settings / 'toollocker.toml').write_text('front = "weightroom.toml"\nport = 9443\n')
    answer = subprocess.run([sys._base_executable, str(SUPPORT_PATH), 'audit', '--root', str(tmp_path),
                             '--services', 'toollocker'], capture_output=True, text=True, timeout=10)
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert str(tmp_path / 'data/accounts.db') in answer.stdout
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('selection', ['--services', '--preset'])
@pytest.mark.parametrize('preset,services,stores', [
    ('front', {'weightroom', 'turnstile', 'toollocker', 'voice-gateway'},
     {'accounts', 'roles', 'chat', 'benchmarks', 'plugins', 'alerts', 'schedules'}),
    ('compute', {'loadcoach', 'spotter', 'voice'}, {'core', 'telemetry'}),
    ('all', {'loadcoach', 'spotter', 'voice', 'weightroom', 'turnstile', 'toollocker', 'voice-gateway'},
     {'core', 'telemetry', 'accounts', 'roles', 'chat', 'benchmarks', 'plugins', 'alerts', 'schedules'}),
])
def test_service_presets_plan_toollocker_and_its_owning_stores(tmp_path, selection, preset, services, stores):
    """Both lower-level preset entry points assign gateway ownership and PostgreSQL stores together."""
    scripts = tmp_path / 'scripts'
    (scripts / 'lib').mkdir(parents=True)
    for name in ('ui.sh', 'detect.sh'):
        shutil.copy2(ROOT / 'scripts/lib' / name, scripts / 'lib' / name)
    shutil.copy2(SUPPORT_PATH, scripts / SUPPORT_PATH.name)
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "fixture"\nversion = "0.1.0"\n')
    commands = tmp_path / 'commands'
    commands.mkdir()
    (commands / 'systemctl').write_text('#!/usr/bin/env bash\nexit 1\n')
    (commands / 'systemctl').chmod(0o755)
    runner = scripts / 'plan.sh'
    plan = (ROOT / 'scripts/install-suite.sh').read_text().split('\ninstall_postgresql_packages\n', 1)[0]
    runner.write_text(plan + '''
printf '%s\\n' "$SELECTED_CSV" >"$ROOT/selected"
printf '%s\\n' "${REQUIRED_DSNS[@]}" >"$ROOT/stores"
''')
    environment = {**os.environ, 'HOME': str(tmp_path / 'home'), 'OWG_NO_TTY': '1', 'OWG_PLAIN': '1',
                   'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'XDG_STATE_HOME': str(tmp_path / 'state'),
                   'OWG_LOG': str(tmp_path / 'install.log'), 'PYTHONPATH': str(ROOT),
                   'PATH': f'{commands}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ["PATH"]}'}
    answer = subprocess.run(['bash', str(runner), selection, preset, '--database', 'postgresql',
                             '--provision-postgresql', '--dry-run', '--non-interactive', '--yes'],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert set((tmp_path / 'selected').read_text().strip().split(',')) == services
    assert set((tmp_path / 'stores').read_text().splitlines()) == stores


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


@pytest.mark.parametrize("services", ["weightroom", "turnstile", "front"])
@pytest.mark.parametrize("provision", [False, True])
def test_front_postgresql_plan_supplies_every_configured_store(tmp_path, services, provision):
    install = tmp_path / "suite"
    scripts = install / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/lib/ui.sh", scripts / "lib/ui.sh")
    shutil.copy2(ROOT / "scripts/lib/detect.sh", scripts / "lib/detect.sh")
    shutil.copy2(SUPPORT_PATH, scripts / SUPPORT_PATH.name)
    (install / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "0.1.0"\n')
    # Run real argument validation and planning, stopping before package/service changes.
    plan = (ROOT / "scripts/install-suite.sh").read_text().split("\ninstall_postgresql_packages\n", 1)[0]
    runner = scripts / "plan.sh"
    runner.write_text(plan + '''
collect_dsns
mkdir -p "$ROOT/settings"
printf 'host = "127.0.0.1"\\n' > "$ROOT/settings/weightroom.toml"
arguments=(configure --config "$ROOT/settings/weightroom.toml" --service weightroom --engine postgresql)
for key in "${!DSNS[@]}"; do arguments+=(--dsn "$key=${DSNS[$key]}"); done
"$PY" "$ROOT/scripts/install-suite-support.py" "${arguments[@]}"
''')
    names = {"accounts", "roles", "chat", "benchmarks", "plugins", "alerts", "schedules"}
    arguments = ["bash", str(runner), "--services", services, "--database", "postgresql",
                 "--dry-run", "--non-interactive", "--yes"]
    dsn_dir = install / "keys/postgresql" if provision else tmp_path / "provided"
    if provision:
        arguments.append("--provision-postgresql")
    else:
        for name in sorted(names):
            arguments += ["--dsn", f"{name}={dsn_dir / (name + '.dsn')}"]
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "OWG_NO_TTY": "1", "OWG_PLAIN": "1",
                   "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
                   "OWG_LOG": str(tmp_path / "install.log"), "PYTHONPATH": str(ROOT),
                   "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    answer = subprocess.run(arguments, env=environment, text=True, capture_output=True, timeout=30)

    assert answer.returncode == 0, answer.stdout + answer.stderr
    config = install / "settings/weightroom.toml"
    databases = tomllib.loads(config.read_text())["databases"]
    assert set(databases["stores"]) == names
    for name, store in databases["stores"].items():
        assert store["engine"] == "postgresql"
        assert (config.parent / store["dsn_file"]).resolve() == dsn_dir / f"{name}.dsn"
        assert databases["bindings"][f"weightroom.{name}"] == name


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
    (install / "weightroom").mkdir()
    for filename in ("__init__.py", "settings_file.py"):  # the installer's TOML editing
        (install / "weightroom" / filename).write_text((ROOT / "weightroom" / filename).read_text())
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
    (install / "weightroom").mkdir()
    for filename in ("__init__.py", "settings_file.py"):  # the installer's TOML editing
        (install / "weightroom" / filename).write_text((ROOT / "weightroom" / filename).read_text())
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

    assert answer.returncode == 78  # UI_STOPPED: the reason and next steps are on screen
    assert "does not move data between database engines" in answer.stdout
    assert "Keep the data where it is: --database keep" in answer.stdout


def test_one_machine_settings_move_compute_ports_and_fill_in_folders(tmp_path, monkeypatch):
    import socket
    import tomllib
    monkeypatch.setattr(socket, "gethostname", lambda: "Box.lan")
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
    # Its node is a peer named for the host, and LoadCoach signs with that name.
    assert "compute" not in made["weightroom"] and made["loadcoach"]["machine_name"] == "box"
    assert made["weightroom"]["machines"]["box"] == {"compute": "http://127.0.0.1:9442", "spotter": "http://127.0.0.1:9444",
                                                    "machine_key_file": "../keys/machine.key"}
    assert made["weightroom"]["turnstile"]["ports"] == [8442, 8443, 8444]
    assert (made["toollocker"]["port"], made["spotter"]["port"]) == (9443, 9444)
    assert made["toollocker"]["servers"]["files"]["command"][-1] == str(root / "data" / "shared")


def test_a_compute_node_listens_on_its_address_and_asks_front(tmp_path):
    import tomllib
    values = support.topology_values("loadcoach", "compute", tmp_path, "192.168.1.50", "http://192.168.1.10:8442/", "", "")
    support.render(ROOT / "settings/examples/loadcoach.toml", tmp_path / "loadcoach.toml", tmp_path, tmp_path, values)
    made = tomllib.loads((tmp_path / "loadcoach.toml").read_text())
    assert (made["host"], made["port"], made["front"]) == ("192.168.1.50", 8442, "http://192.168.1.10:8442")
    # A console with no compute node yet has none until one pairs.
    assert support.topology_values("weightroom", "front", tmp_path, "192.168.1.10", "", "", "") == {}


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
