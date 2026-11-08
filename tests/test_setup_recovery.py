"""Installer failure paths: credentials, interrupted setup, ownership and safe removal."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib

import pytest

from loadcoach.config import load_config
from weightroom import pairing

ROOT = Path(__file__).parents[1]


def script_functions(name: str) -> str:
    """Load the actual Bash functions without executing their command-line entry point."""
    return (ROOT / name).read_text().rsplit('\nmain "$@"', 1)[0]


def one_function(name: str, function: str) -> str:
    """Extract one real function when its script also has executable top-level setup."""
    text = (ROOT / name).read_text()
    found = (re.search(rf"(?m)^{re.escape(function)}\(\) \{{ .*\}}$", text)
             or re.search(rf"(?ms)^{re.escape(function)}\(\) \{{.*?^\}}", text))
    assert found, function
    return found[0]


def shell(tmp_path, source: str, *, env=None):
    """Run a fixture-only shell with no terminal or access to the real user service manager."""
    environment = {**os.environ, "OWG_NO_TTY": "1", "OWG_PLAIN": "1",
                   "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
                   "OWG_LOG": str(tmp_path / "install.log"), "TEST_REPO": str(ROOT), **(env or {})}
    return subprocess.run(["bash", "-c", source], cwd=ROOT, env=environment, text=True,
                          capture_output=True, timeout=30)


def support_module():
    """Import the installer's real Python boundary for config and voice provisioning tests."""
    spec = importlib.util.spec_from_file_location("review_installer_support", ROOT / "scripts/install-suite-support.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_firewall_ports_follow_custom_ports_and_all_gateway_listeners(tmp_path):
    settings = tmp_path / 'settings'
    settings.mkdir()
    (settings / 'weightroom.toml').write_text('host = "0.0.0.0"\nport = 18440\n[turnstile]\nhost = "::"\nports = [18442,18443,18444,18445]\n')
    (settings / 'toollocker.toml').write_text('host = "127.0.0.1"\nport = 19443\n')
    for name, port in [('loadcoach', 18442), ('spotter', 18444), ('voice', 18445)]:
        (settings / f'{name}.toml').write_text(f'host = "192.0.2.84"\nport = {port}\n')
    (settings / 'voice-gateway.toml').write_text('host = "::1"\nport = 8446\ndesktop_host = "192.0.2.42"\ndesktop_port = 18447\npublic_port = 8081\n')
    support = support_module()
    assert support.firewall_ports(tmp_path, list(support.SERVICE_CONFIG)) == [8081, 18440, 18442, 18443, 18444, 18445, 18447]


@pytest.mark.parametrize('host,expected', [('127.0.0.1', []), ('127.0.1.2', []), ('::1', []), ('localhost', []),
                                          ('0.0.0.0', [18444]), ('::', [18444]), ('192.0.2.84', [18444])])
def test_firewall_ports_merge_defaults_and_skip_loopback_listeners(tmp_path, host, expected):
    settings = tmp_path / 'settings'
    (settings / 'defaults').mkdir(parents=True)
    (settings / 'defaults/spotter.toml').write_text('host = "0.0.0.0"\nport = 18444\n')
    (settings / 'spotter.toml').write_text(f'host = "{host}"\n')
    assert support_module().firewall_ports(tmp_path, ['spotter']) == expected


@pytest.mark.parametrize('port', ['true', '0', '65536', '"8444"'])
def test_firewall_ports_refuse_invalid_ports_without_suggesting_rules(tmp_path, port):
    (tmp_path / 'settings').mkdir()
    (tmp_path / 'settings/spotter.toml').write_text(f'host = "0.0.0.0"\nport = {port}\n')
    with pytest.raises(ValueError, match='TCP port'):
        support_module().firewall_ports(tmp_path, ['spotter'])


@pytest.mark.parametrize('topology,expected', [
    ('all', {'loadcoach', 'spotter', 'weightroom', 'turnstile', 'toollocker'}),
    ('front', {'weightroom', 'turnstile', 'toollocker'}),
    ('compute', {'loadcoach', 'spotter'}),
])
@pytest.mark.parametrize('voice', [False, True])
def test_bootstrap_assigns_toollocker_to_front_with_optional_voice(tmp_path, topology, expected, voice):
    """The public installer keeps MCP on front regardless of the voice selection."""
    answer = shell(tmp_path, script_functions('install.sh') + '''
parse_arguments --topology "$TEST_TOPOLOGY"
VOICE=$TEST_VOICE
services_for
printf '%s\\n' "$SERVICES"
''', env={'TEST_TOPOLOGY': topology, 'TEST_VOICE': str(int(voice))})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    if voice:
        expected = expected | ({'voice'} if topology == 'compute' else {'voice-gateway'} if topology == 'front' else {'voice', 'voice-gateway'})
    assert set(answer.stdout.strip().split(',')) == expected


def test_explicit_missing_uninstall_target_does_not_fall_back(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / ".install-receipt").write_text(f"version=2\nmode=user\ndir={other}\ncreated_dir=1\nservices=\n")
    (other / "keep-me").write_text("program belonging to another installation")
    pointer = tmp_path / "config/openweightgym"
    pointer.mkdir(parents=True)
    (pointer / "install").write_text(str(other))
    answer = shell(tmp_path, '"$TEST_REPO/uninstall.sh" --dir "$MISSING" --yes',
                   env={"MISSING": str(tmp_path / "missing")})
    assert answer.returncode != 0
    assert "so nothing was touched" in answer.stderr and "--dir" in answer.stderr
    assert (other / "keep-me").exists()


def test_step_logs_are_private_and_never_include_credential_arguments(tmp_path):
    log = tmp_path / "install.log"
    log.write_text("prior run\n")
    log.chmod(0o644)
    answer = shell(tmp_path, '''set -euo pipefail
umask 022
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
ui_step 'Authenticated check' -- true -H 'Authorization: Bearer lc_do_not_record'
ui_step 'A failure' -- bash -c 'exit 7'
''')
    assert answer.returncode == 7
    assert "lc_do_not_record" not in log.read_text()
    assert "Authorization" not in log.read_text()
    assert "Authenticated check" in log.read_text() and "prior run" in log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_postgresql_retry_keeps_existing_connections_and_adds_only_missing_stores(tmp_path):
    dsns = tmp_path / "install/keys/postgresql"
    dsns.mkdir(parents=True)
    original = {}
    for name in ("accounts", "chat", "benchmarks", "plugins", "alerts", "schedules"):
        path = dsns / f"{name}.dsn"
        original[path] = f"postgresql://existing:keep_{name}@127.0.0.1/original_{name}\n"
        path.write_text(original[path])
        path.chmod(0o600)
    answer = shell(tmp_path, '''set -euo pipefail
ROOT=$INSTALL PY=$TEST_PYTHON USER=fixture PROVISION_POSTGRESQL=1 DRY_RUN=0
declare -A DSNS=()
REQUIRED_DSNS=(accounts roles chat benchmarks plugins alerts schedules)
note() { printf '%s\\n' "$*"; }
die() { printf '%s\\n' "$*" >&2; exit 1; }
hostname() { printf fixture; }
uname() { printf 'fixture\n'; }
psql() { return 99; }
createdb() { return 99; }
repair_postgresql() { :; }
check_postgresql_login() { :; }
sudo() {
  [[ $1 == -n ]] && shift
  [[ $1 == -u && $2 == postgres ]] || return 98
  shift 2
  case "$1" in
    psql)
      if [[ $2 == -Atqc ]]; then
        if [[ $3 == 'SHOW port' ]]; then printf '5432\\n'; return 0; fi
        if [[ $3 == *"rolname = 'suite_fixture_fixture'"* ]]; then printf '1\\n'; fi
      else
        cat >/dev/null
        printf 'create role\\n' >>"$ATTEMPTS"
      fi ;;
    createdb) printf '%s\\n' "$*" >>"$ATTEMPTS" ;;
    *) return 97 ;;
  esac
}
''' + one_function("scripts/install-suite.sh", "as_postgres") + "\n" + one_function("scripts/install-suite.sh", "provision_postgresql") + '''
provision_postgresql
provision_postgresql
for key in "${REQUIRED_DSNS[@]}"; do
  [[ ${DSNS[$key]} == "$ROOT/keys/postgresql/$key.dsn" ]]
done
''', env={"INSTALL": str(tmp_path / "install"), "TEST_PYTHON": sys.executable,
          "ATTEMPTS": str(tmp_path / "attempts")})

    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert (tmp_path / "attempts").read_text().splitlines() == [
        "create role", "createdb --owner suite_fixture_fixture_2 suite_fixture_fixture_2_roles"]
    for path, content in original.items():
        assert path.read_text() == content
    roles = dsns / "roles.dsn"
    assert roles.read_text().startswith("postgresql://suite_fixture_fixture_2:")
    assert stat.S_IMODE(roles.stat().st_mode) == 0o600
    assert stat.S_IMODE(dsns.stat().st_mode) == 0o700


@pytest.mark.parametrize("length", [5, 60, 100])
def test_postgresql_names_stay_distinct_with_long_prefixes(tmp_path, length):
    answer = shell(tmp_path, '''set -euo pipefail
ROOT=$INSTALL PY=$TEST_PYTHON USER=fixture PROVISION_POSTGRESQL=1 DRY_RUN=0
declare -A DSNS=()
REQUIRED_DSNS=(accounts roles telemetry benchmarks)
psql() { :; }
createdb() { :; }
repair_postgresql() { :; }
check_postgresql_login() { :; }
die() { exit 1; }
sudo() {
  [[ $1 == -n ]] && shift
  shift 2
  if [[ $1 == createdb ]]; then printf '%s\\n' "${!#}" >>"$DATABASES"
  elif [[ $* == *'SHOW port'* ]]; then printf '5432\\n'
  elif [[ $2 != -Atqc ]]; then cat >/dev/null; fi
}
''' + one_function("scripts/install-suite.sh", "as_postgres") + "\n" + one_function("scripts/install-suite.sh", "provision_postgresql") + '\nprovision_postgresql\n',
                   env={"INSTALL": str(tmp_path / "install"), "TEST_PYTHON": sys.executable,
                        "SUITE_DATABASE_PREFIX": "p" * length, "DATABASES": str(tmp_path / "databases")})
    assert answer.returncode == 0, answer.stderr
    names = (tmp_path / "databases").read_text().splitlines()
    assert len(set(names)) == 4 and all(len(name) <= 60 for name in names)
    assert [name.rsplit("_", 1)[-1] for name in names] == ["accounts", "roles", "telemetry", "benchmarks"]


def test_database_configuration_traceback_is_saved_in_the_installer_log(tmp_path):
    install = tmp_path / "install"
    (install / "scripts").mkdir(parents=True)
    (install / "settings").mkdir()
    shutil.copy2(ROOT / "scripts/install-suite-support.py", install / "scripts/install-suite-support.py")
    config = install / "settings/weightroom.toml"
    original = 'host = "127.0.0.1"\n'
    config.write_text(original)
    suite = (ROOT / "scripts/install-suite.sh").read_text()
    start = suite.index('\nif [[ $DATABASE != keep ]]; then\n')
    end = suite.index('\nif ((!DRY_RUN)); then\n', start)
    answer = shell(tmp_path, '''set -euo pipefail
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
ui_init
ROOT=$INSTALL PY=$TEST_PYTHON DATABASE=postgresql DRY_RUN=0
SELECTED=(weightroom)
declare -A DSNS=([accounts]="$INSTALL/keys/accounts.dsn")
declare -A WAS_CONFIGURED=([weightroom]=1)
declare -A CONFIG_BACKUPS=()
die() { ui_die "install-suite: $*" "" 2; }
''' + one_function("scripts/install-suite.sh", "try_step") + "\n" +
                   one_function("scripts/install-suite.sh", "has_service") + suite[start:end],
                   env={"INSTALL": str(install), "TEST_PYTHON": sys.executable, "PYTHONPATH": str(ROOT)})

    assert answer.returncode != 0
    log = (tmp_path / "install.log").read_text()
    assert "Configuring weightroom databases (postgresql)" in log
    assert "Traceback (most recent call last)" in log
    assert "no PostgreSQL DSN file was provided for roles" in log
    assert "no PostgreSQL DSN file was provided for roles" in answer.stderr
    assert config.read_text() == original


def test_cuda_download_uses_the_published_runtime_asset_name(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "llama-server").write_text("fixture")
    (bundle / "llama-server").chmod(0o755)
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        target.add(bundle, arcname="bundle")
    answer = shell(tmp_path, '''set -euo pipefail
BUILD=b12008 OS_ARCH=x86_64 RELEASES=https://github.com/ggml-org/llama.cpp/releases/download
ui_download() { printf '%s\\n' "$1" >>"$URLS"; cp "$ARCHIVE" "$2"; }
ui_step() { shift 2; "$@"; }
ui_die() { exit 1; }
''' + one_function("scripts/install-llamacpp.sh", "asset_for") + "\n" +
                   one_function("scripts/install-llamacpp.sh", "download_release") +
                   '\ndownload_release cuda-13.4 "$TARGET"\n',
                   env={"ARCHIVE": str(archive), "URLS": str(tmp_path / "urls"), "TARGET": str(tmp_path / "runtime")})
    assert answer.returncode == 0, answer.stderr
    urls = (tmp_path / "urls").read_text().splitlines()
    assert urls[1].endswith("/b12008/cudart-llama-b12008-bin-ubuntu-cuda-13.4-x64.tar.gz")
    assert (tmp_path / "runtime/llama-server").is_file()


@pytest.mark.parametrize("wanted,taken", [("GPU Box!", set()), ("", {"node"}), ("9060xt", set())])
def test_paired_names_are_accepted_by_the_worker(tmp_path, wanted, taken):
    name = pairing.node_name(wanted, taken)
    (tmp_path / "data").mkdir()
    config = tmp_path / "loadcoach.toml"
    config.write_text(f'host = "127.0.0.1"\nport = 8442\ngpus = []\ndata = "data"\nmachine_name = "{name}"\n')
    assert load_config(config).machine_name == name


@pytest.mark.parametrize("name", ["weightroom", "machine", "voice-service"])
def test_paired_keys_never_reuse_front_credentials(tmp_path, name):
    (tmp_path / "settings").mkdir()
    (tmp_path / "keys").mkdir()
    config = tmp_path / "settings/weightroom.toml"
    config.write_text('compute = "http://127.0.0.1:9442"\n[turnstile]\nports = [8442,8443,8444]\n')
    (tmp_path / "settings/loadcoach.toml").write_text('port = 9442\n')
    original = tmp_path / "keys" / (name + ".key")
    original.write_text("lc_existing_front_credential\n")
    joined = pairing.join(config, "10.0.0.5", name)
    assert joined["machine_key"] != original.read_text().strip()
    key = tmp_path / "keys/machines" / (joined["name"] + ".key")
    assert key.read_text().strip() == joined["machine_key"]
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert original.read_text() == "lc_existing_front_credential\n"


def test_public_system_receipt_does_not_require_private_data_access(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / ".install-receipt").write_text("version=2\nmode=system\nservice_user=fixture-service\nservices=loadcoach\n")
    data = install / "data"
    data.mkdir()
    data.chmod(0)
    try:
        answer = shell(tmp_path, '''set -euo pipefail
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
ui_sudo() { exit 99; }
receipt_load "$INSTALL"
receipt_present
printf '%s %s' "$(receipt_get mode)" "$(receipt_get service_user)"
''', env={"INSTALL": str(install)})
    finally:
        data.chmod(0o700)
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout == "system fixture-service"


def test_legacy_receipt_migration_publishes_only_metadata(tmp_path):
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    (install / "data/install-receipt").write_text("version=1\nmode=user\nservices=weightroom\npassword=never-publish\n")
    answer = shell(tmp_path, '''set -euo pipefail
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
receipt_load "$INSTALL"
receipt_publish "$INSTALL"
''', env={"INSTALL": str(install)})
    assert answer.returncode == 0, answer.stderr
    receipt = install / ".install-receipt"
    assert "services=weightroom" in receipt.read_text() and "never-publish" not in receipt.read_text()
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o644


def test_uninstall_keeps_recovery_metadata_with_data_then_purges_only_that_install(tmp_path):
    install = tmp_path / "install"
    for name in ("settings", "keys", "data", "models", "runtimes", "code", ".venv"):
        (install / name).mkdir(parents=True)
    (install / "settings/loadcoach.toml").write_text("port = 8442\n")
    (install / "data/keep-me").write_text("retained state")
    (install / "keys/machine.key").write_text("retained secret")
    units = tmp_path / "units"
    units.mkdir()
    (units / "loadcoach.service").write_text("fixture unit")
    pointer = tmp_path / "metadata/install"
    pointer.parent.mkdir()
    pointer.write_text(str(install))
    command = tmp_path / "bin/owg"
    command.parent.mkdir()
    command.symlink_to(install / "scripts/owg")
    (install / ".install-receipt").write_text(f"version=2\nstate=complete\nmode=user\ndir={install}\ncreated_dir=1\n"
        f"services=loadcoach\nunit_dir={units}\npointer={pointer}\nowg_link={command}\n")
    source = script_functions("uninstall.sh") + '''
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
owg_systemctl() { return 1; }
load_ui() { source "$TEST_REPO/scripts/lib/ui.sh"; source "$TEST_REPO/scripts/lib/detect.sh"; }
main --dir "$INSTALL" --yes "$@"
'''
    kept = shell(tmp_path, source, env={"INSTALL": str(install)})
    assert kept.returncode == 0, kept.stderr
    assert (install / ".install-receipt").exists() and pointer.exists()
    assert (install / "data/keep-me").read_text() == "retained state"
    assert (install / "keys/machine.key").read_text() == "retained secret"
    assert not (install / "code").exists() and not (units / "loadcoach.service").exists()
    assert not command.is_symlink()
    purged = shell(tmp_path, source.replace('main --dir "$INSTALL" --yes "$@"',
                   'main --dir "$INSTALL" --yes --everything --no-backup'), env={"INSTALL": str(install)})
    assert purged.returncode == 0, purged.stderr
    assert not install.exists() and not pointer.exists()


def test_failed_service_control_returns_failure_after_trying_other_services(tmp_path):
    answer = shell(tmp_path, '''set -euo pipefail
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
owg_systemctl() { printf '%s\\n' "$*" >>"$ATTEMPTS"; [[ $2 != loadcoach.service ]]; }
''' + one_function("scripts/owg", "control") + '\ncontrol restart loadcoach weightroom\n',
                   env={"ATTEMPTS": str(tmp_path / "attempts")})
    assert answer.returncode == 1
    assert (tmp_path / "attempts").read_text().splitlines() == ["restart loadcoach.service", "restart weightroom.service"]


@pytest.fixture
def remote_owg(tmp_path):
    local_bin, remote_bin = tmp_path / 'bin', tmp_path / 'remote-bin'
    local_bin.mkdir()
    remote_bin.mkdir()
    ssh = local_bin / 'ssh'
    ssh.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >"$SSH_ATTEMPTS"\n'
                   'PATH="$REMOTE_BIN:/usr/bin:/bin" bash -c "${!#}"\n')
    remote = remote_bin / 'owg'
    remote.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >"$REMOTE_ATTEMPTS"\nexit "${REMOTE_EXIT:-0}"\n')
    local_control = local_bin / 'systemctl'
    local_control.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >"$LOCAL_ATTEMPTS"\nexit 99\n')
    for program in (ssh, remote, local_control):
        program.chmod(0o755)
    return {'PATH': f'{local_bin}:/usr/bin:/bin', 'REMOTE_BIN': str(remote_bin),
            'SSH_ATTEMPTS': str(tmp_path / 'ssh-attempts'), 'REMOTE_ATTEMPTS': str(tmp_path / 'remote-attempts'),
            'LOCAL_ATTEMPTS': str(tmp_path / 'local-attempts')}


@pytest.mark.parametrize('arguments,expected,exit_code', [
    (['--host', 'fixture@compute', 'loadcoach', 'spotter'], ['restart', 'loadcoach', 'spotter'], 0),
    (['--host=fixture@compute'], ['restart'], 0),
    (['loadcoach', '--host', 'fixture@compute'], ['restart', 'loadcoach'], 17),
])
def test_owg_restarts_remote_services_and_returns_the_remote_status(tmp_path, remote_owg, arguments, expected, exit_code):
    answer = shell(tmp_path, '"$TEST_REPO/scripts/owg" restart ' + shlex.join(arguments),
                   env={**remote_owg, 'REMOTE_EXIT': str(exit_code)})
    assert answer.returncode == exit_code, answer.stdout + answer.stderr
    assert Path(remote_owg['SSH_ATTEMPTS']).read_text().splitlines()[:5] == [
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'fixture@compute']
    assert Path(remote_owg['REMOTE_ATTEMPTS']).read_text().splitlines() == expected
    assert not Path(remote_owg['LOCAL_ATTEMPTS']).exists()


@pytest.mark.parametrize('arguments', [
    ['--host'], ['--host='], ['--host', '-oProxyCommand=anything'],
    ['--host', 'host;anything'], ['--host', 'compute', '--host', 'other'],
    ['loadcoach', '--host', 'compute', 'spotter;anything'],
])
def test_owg_rejects_invalid_remote_restarts_before_controlling_any_service(tmp_path, remote_owg, arguments):
    answer = shell(tmp_path, '"$TEST_REPO/scripts/owg" restart ' + shlex.join(arguments), env=remote_owg)
    assert answer.returncode != 0
    assert not Path(remote_owg['SSH_ATTEMPTS']).exists()
    assert not Path(remote_owg['LOCAL_ATTEMPTS']).exists()


def test_unit_updates_back_up_customizations_and_skip_identical_copies(tmp_path):
    install, units = tmp_path / "install", tmp_path / "units"
    (install / "services").mkdir(parents=True)
    units.mkdir()
    (install / "services/loadcoach.service").write_text("[Service]\nExecStart=%h/ai/openWeightGym/.venv/bin/loadcoach\n")
    target = units / "loadcoach.service"
    original = f"[Service]\nExecStart={install}/.venv/bin/loadcoach\nEnvironment=KEEP_FOR_RECOVERY=1\n"
    target.write_text(original)
    source = '''set -euo pipefail
ROOT=$INSTALL UNIT_DIR=$UNITS PY=$TEST_PYTHON DRY_RUN=0
die() { exit 1; }
''' + one_function("scripts/install-suite.sh", "render_unit") + '\nrender_unit loadcoach\n'
    environment = {"INSTALL": str(install), "UNITS": str(units), "TEST_PYTHON": sys.executable}
    first = shell(tmp_path, source, env=environment)
    assert first.returncode == 0, first.stderr
    backups = list(units.glob("*.before-suite-install-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert "KEEP_FOR_RECOVERY" not in target.read_text()
    assert shell(tmp_path, source, env=environment).returncode == 0
    assert list(units.glob("*.before-suite-install-*")) == backups


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="requires the systemd unit verifier")
def test_all_service_templates_are_valid_with_spaces_in_installation_paths(tmp_path):
    install = tmp_path / "install with spaces"
    services = ("loadcoach", "weightroom", "turnstile", "spotter", "toollocker", "voice", "voice-gateway")
    shutil.copytree(ROOT / "services", install / "services")
    (install / ".venv/bin").mkdir(parents=True)
    (install / ".env").touch()
    for name in services:
        program = install / ".venv/bin" / name
        program.write_text("#!/usr/bin/env bash\nexit 0\n")
        program.chmod(0o755)
    units = tmp_path / "config/systemd/user"
    units.mkdir(parents=True)
    answer = shell(tmp_path, '''set -euo pipefail
ROOT=$INSTALL UNIT_DIR=$UNITS PY=$TEST_PYTHON DRY_RUN=0
die() { exit 1; }
''' + one_function("scripts/install-suite.sh", "render_unit") + '''
for service in loadcoach weightroom turnstile spotter toollocker voice voice-gateway; do render_unit "$service"; done
systemd-analyze --user --man=no verify "$UNIT_DIR"/*.service
''', env={"INSTALL": str(install), "UNITS": str(units), "TEST_PYTHON": sys.executable})
    assert answer.returncode == 0, answer.stdout + answer.stderr


def test_virtualenv_and_settings_do_not_complete_an_install(tmp_path):
    install = tmp_path / "install"
    (install / ".venv/bin").mkdir(parents=True)
    (install / ".venv/bin/python").touch(mode=0o755)
    (install / "settings").mkdir()
    (install / "settings/loadcoach.toml").write_text("port = 9442\n")
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
receipt_load "$INSTALL"
is_installed "$INSTALL"
''', env={"INSTALL": str(install)})
    assert answer.returncode == 1


def test_default_discovery_finds_a_public_system_receipt(tmp_path):
    install = tmp_path / "system-install"
    install.mkdir()
    (install / ".install-receipt").write_text("mode=system\nservice_user=fixture-service\nstate=pending\n")
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
parse_arguments
CHECKOUT=$OTHER_CHECKOUT SYS_OPT=$INSTALL
locate_installation
printf '%s %s %s' "$DIR" "$SYSTEM" "$OWG_OWNER"
''', env={"INSTALL": str(install), "OTHER_CHECKOUT": str(tmp_path / "other")})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout == f"{install} 1 fixture-service"


@pytest.mark.parametrize("action,choice,child_status,arguments", [
    ("continue", 0, 0, ""),
    ("restart", 1, 0, ""),
    ("quit", 2, 0, ""),
    ("cancel", 1, 3, ""),
    ("failure", 1, 7, ""),
    ("continue", 2, 0, "--yes"),
    ("continue", 2, 0, "--repair"),
])
def test_interrupted_install_choices_control_whether_setup_runs(tmp_path, action, choice, child_status, arguments):
    install = tmp_path / "install"
    (install / "settings").mkdir(parents=True)
    (install / "settings/weightroom.toml").write_text('data = "../data"\n')
    (install / "pyproject.toml").touch()
    receipt = install / ".install-receipt"
    original = "state=pending\nmode=user\ncreated_dir=1\ntopology=front\nadmin=sam\ndatabase=postgresql\nvoice=1\n"
    receipt.write_text(original)
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
load_ui() { CHECKOUT=$INSTALL; }
ui_banner() { :; }
ui_choose() {
  printf '%s\\n' "$@" >>"$OPTIONS"
  printf 'menu\\n' >>"$EVENTS"
  printf -v "$1" '%s' "$MENU_CHOICE"
}
uninstall_components() {
  [[ $1 == --for-reinstall ]]
  printf 'uninstall\\n' >>"$EVENTS"
  return "$CHILD_STATUS"
}
warm_up() { printf 'warm-up\\n' >>"$EVENTS"; }
game_plan() {
  [[ -z $TOPOLOGY && -z $ADMIN && $VOICE == 0 && $DATABASE == keep ]]
  printf 'new-plan\\n' >>"$EVENTS"
  TOPOLOGY=front ADMIN=alex
}
account() { printf 'account:%s:%s:%s:%s\\n' "$TOPOLOGY" "$ADMIN" "$DATABASE" "$VOICE" >>"$EVENTS"; }
loading_plates() { services_for; write_receipt pending; }
setting_up() { printf 'install\\n' >>"$EVENTS"; }
finishing() { :; }
spotting() { :; }
finale() { printf 'complete\\n' >>"$EVENTS"; }
main --dir "$INSTALL" "$@"
'''.replace('main --dir "$INSTALL" "$@"', f'main --dir "$INSTALL" {arguments}'),
                   env={"INSTALL": str(install), "EVENTS": str(tmp_path / "events"),
                        "OPTIONS": str(tmp_path / "options"), "MENU_CHOICE": str(choice),
                        "CHILD_STATUS": str(child_status)})

    events = (tmp_path / "events").read_text().splitlines()
    if arguments:
        assert "menu" not in events
    else:
        options = (tmp_path / "options").read_text()
        assert "Continue previous install|" in options
        assert "Uninstall components and start again|" in options
        assert "Quit|" in options
        assert events[0] == "menu"
    assert (answer.returncode != 0) == (action == "failure"), answer.stderr
    if action in {"quit", "cancel", "failure"}:
        assert events == (["menu"] if action == "quit" else ["menu", "uninstall"])
        assert receipt.read_text() == original
    else:
        assert events[-2:] == ["install", "complete"]
        assert "state=complete" in receipt.read_text()
        if action == "continue":
            assert "account:front:sam:postgresql:1" in events
            assert "uninstall" not in events and "new-plan" not in events
        else:
            assert events[:4] == ["menu", "uninstall", "warm-up", "new-plan"]
            assert "account:front:alex:keep:0" in events
            assert "created_dir=1" in receipt.read_text()


@pytest.mark.parametrize("for_reinstall", [False, True])
@pytest.mark.parametrize("stage", ["empty-selection", "decline", "delete-not-typed"])
def test_cancelled_uninstall_stops_recovery_without_changing_normal_exit_status(tmp_path, stage, for_reinstall):
    answer = shell(tmp_path, script_functions("uninstall.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
parse_arguments "$@"
ui_init
ITEMS=("$ITEM")
declare -A LABEL=([$ITEM]=Fixture) ABOUT=([$ITEM]=fixture) DEFAULT=([$ITEM]=off)
declare -A REMOVE=([$ITEM]=1)
BACKUP_FILE=''
if [[ $STAGE == empty-selection ]]; then choose; else confirm; fi
printf 'should not reach removal' >"$REMOVED"
'''.replace('parse_arguments "$@"', 'parse_arguments --for-reinstall' if for_reinstall else 'parse_arguments'),
                   env={"STAGE": stage, "ITEM": "program" if stage == "decline" else "settings",
                        "REMOVED": str(tmp_path / "removed")})
    assert answer.returncode == (3 if for_reinstall else 0), answer.stderr
    assert not (tmp_path / "removed").exists()


@pytest.mark.parametrize("entry_point", ["checkout", "bootstrap", "mixed-checkout"])
def test_restart_removes_selected_components_refetches_code_and_keeps_data(tmp_path, entry_point):
    install = tmp_path / "install"
    for folder in ("settings", "keys", "data", "models", ".venv"):
        (install / folder).mkdir(parents=True)
    kept = {"settings/weightroom.toml": 'data = "../data"\n',
            "keys/machine.key": "retained-key", "data/chat.db": "retained-data",
            "models/model.gguf": "retained-model"}
    for name, content in kept.items():
        (install / name).write_text(content)
    (install / "pyproject.toml").touch()
    (install / "old-code").touch()
    units = tmp_path / "config/systemd/user"
    units.mkdir(parents=True)
    unit = units / "weightroom.service"
    unit.write_text(f"ExecStart={install}/.venv/bin/weightroom\n")
    (install / ".install-receipt").write_text(f"state=pending\nmode=user\ncreated_dir=1\n"
        f"topology=all\nadmin=sam\nservices=weightroom\nunit_dir={units}\n")
    uninstaller = install / "uninstall.sh"
    uninstaller.write_text(script_functions("uninstall.sh") + '''
load_ui() {
  source "$TEST_REPO/scripts/lib/ui.sh"
  source "$TEST_REPO/scripts/lib/detect.sh"
  source "$TEST_REPO/scripts/lib/receipt.sh"
  owg_systemctl() { return 1; }
  ui_confirm() { return 0; }
}
main "$@"
''')
    uninstaller.chmod(0o755)
    remote_uninstaller = tmp_path / "remote-uninstall.sh"
    shutil.copy2(uninstaller, remote_uninstaller)
    if entry_point != "checkout":
        uninstaller.write_text('#!/usr/bin/env bash\nprintf "stale helper was called" >"$LEGACY_CALLED"\nexit 2\n')
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
load_ui() { CHECKOUT=$FIXTURE_CHECKOUT; }
curl() { cp "$REMOTE_UNINSTALLER" "${!#}"; }
ui_choose() { printf -v "$1" '%s' 1; }
warm_up() { :; }
game_plan() {
  [[ -z $CHECKOUT && -z $TOPOLOGY && -z $ADMIN && $DATABASE == keep ]]
  [[ ! -e $INSTALL/old-code && ! -e $INSTALL/.venv && ! -e $UNIT ]]
  TOPOLOGY=front ADMIN=alex
}
account() { :; }
git() {
  if [[ $1 == clone ]]; then mkdir -p "${!#}/.git"
  else
    [[ $* == *reset* ]]
    printf '[project]\\nname = "fixture"\\n' >"$INSTALL/pyproject.toml"
  fi
}
loading_plates() { fetch_code; services_for; write_receipt pending; }
setting_up() { :; }
finishing() { :; }
spotting() { :; }
finale() { printf RESTARTED; }
main --dir "$INSTALL" --no-browser
''', env={"INSTALL": str(install), "UNIT": str(unit),
          "FIXTURE_CHECKOUT": "" if entry_point == "bootstrap" else str(install),
          "REMOTE_UNINSTALLER": str(remote_uninstaller), "LEGACY_CALLED": str(tmp_path / "legacy-called")})

    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert "RESTARTED" in answer.stdout
    assert not (tmp_path / "legacy-called").exists()
    for name, content in kept.items():
        assert (install / name).read_text() == content
    assert not unit.exists() and not (install / "old-code").exists()
    assert (install / ".git").is_dir() and (install / "pyproject.toml").exists()
    receipt = (install / ".install-receipt").read_text()
    assert "state=complete" in receipt and "topology=front" in receipt and "admin=alex" in receipt


@pytest.mark.parametrize("entry_point", ["checkout", "bootstrap", "mixed-checkout"])
@pytest.mark.parametrize("child_status", [0, 3, 7])
def test_uninstall_handoff_uses_the_running_installers_source_and_preserves_status(tmp_path, entry_point, child_status):
    install = tmp_path / "older install"
    checkout = tmp_path / "current checkout"
    install.mkdir()
    checkout.mkdir()
    old = install / "uninstall.sh"
    old.write_text('#!/usr/bin/env bash\nprintf stale >"$LEGACY_CALLED"\nexit 2\n')
    old.chmod(0o755)
    helper = checkout / "uninstall.sh"
    helper.write_text(script_functions("uninstall.sh") + '''
parse_arguments "$@"
printf '%s\\n' "$OWG_RAW" "$OWG_REF" "$DIR" "$FOR_REINSTALL" >"$HANDOFF"
exit "$CHILD_STATUS"
''')
    helper.chmod(0o755)
    work = tmp_path / "temporary downloads"
    work.mkdir()
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
parse_arguments --dir "$INSTALL" --ref feature/recovery
export -n OWG_REF
CHECKOUT=$FIXTURE_CHECKOUT
ui_init
curl() {
  printf '%s\\n' "$@" >"$FETCH_ARGUMENTS"
  cp "$CURRENT_HELPER" "${!#}"
}
uninstall_components --for-reinstall
''', env={"INSTALL": str(install), "FIXTURE_CHECKOUT": str(checkout) if entry_point == "checkout" else (str(install) if entry_point == "mixed-checkout" else ""),
          "CURRENT_HELPER": str(helper), "HANDOFF": str(tmp_path / "handoff"),
          "FETCH_ARGUMENTS": str(tmp_path / "fetch-arguments"), "LEGACY_CALLED": str(tmp_path / "legacy-called"),
          "CHILD_STATUS": str(child_status), "OWG_RAW": "https://example.invalid/fork/openWeightGym",
          "TMPDIR": str(work)})

    assert answer.returncode == child_status, answer.stdout + answer.stderr
    assert (tmp_path / "handoff").read_text().splitlines() == [
        "https://example.invalid/fork/openWeightGym", "feature/recovery", str(install), "1"]
    assert not (tmp_path / "legacy-called").exists()
    assert "printf stale" in old.read_text()
    assert list(work.iterdir()) == []
    fetch = tmp_path / "fetch-arguments"
    if entry_point != "checkout":
        assert "https://example.invalid/fork/openWeightGym/feature/recovery/uninstall.sh" in fetch.read_text().splitlines()
    else:
        assert not fetch.exists()


@pytest.mark.parametrize("failure", ["download", "syntax"])
def test_uninstaller_fetch_failure_never_runs_partial_or_installed_helpers(tmp_path, failure):
    install = tmp_path / "install"
    install.mkdir()
    old = install / "uninstall.sh"
    old.write_text('#!/usr/bin/env bash\nprintf stale >"$CALLED"\n')
    old.chmod(0o755)
    partial = tmp_path / "partial.sh"
    partial.write_text('printf partial >"$CALLED"\n' + ('if then\n' if failure == "syntax" else ''))
    work = tmp_path / "downloads"
    work.mkdir()
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
parse_arguments --dir "$INSTALL"
CHECKOUT=''
ui_init
curl() {
  cp "$PARTIAL" "${!#}"
  [[ $FAILURE != download ]]
}
uninstall_components --for-reinstall
''', env={"INSTALL": str(install), "PARTIAL": str(partial), "FAILURE": failure,
          "CALLED": str(tmp_path / "called"), "TMPDIR": str(work)})

    assert answer.returncode != 0
    assert not (tmp_path / "called").exists()
    assert old.exists() and list(work.iterdir()) == []


@pytest.mark.parametrize("assume_yes", [False, True])
def test_installer_forwards_uninstall_confirmation_choice(tmp_path, assume_yes):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    helper = checkout / "uninstall.sh"
    helper.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" >"$ARGUMENTS"\n')
    helper.chmod(0o755)
    answer = shell(tmp_path, script_functions("install.sh") + '''
parse_arguments --uninstall --dir "$CHECKOUT_PATH" "$@"
CHECKOUT=$CHECKOUT_PATH
run_uninstaller
'''.replace('"$@"\nCHECKOUT=', ('--yes' if assume_yes else '') + '\nCHECKOUT='),
                   env={"CHECKOUT_PATH": str(checkout), "ARGUMENTS": str(tmp_path / "arguments")})
    assert answer.returncode == 0, answer.stderr
    arguments = (tmp_path / "arguments").read_text().splitlines()
    assert arguments[:2] == ["--dir", str(checkout)]
    assert ("--yes" in arguments) == assume_yes


def test_repair_recreates_services_listed_in_receipt_when_units_are_missing(tmp_path):
    install = tmp_path / "install"
    (install / "scripts").mkdir(parents=True)
    (install / "settings").mkdir()
    (install / "settings/weightroom.toml").write_text('data = "../data"\n')
    (install / ".venv/bin").mkdir(parents=True)
    (install / ".venv/bin/python").symlink_to(sys.executable)
    (install / ".install-receipt").write_text("state=complete\nmode=user\nservices=weightroom,turnstile,voice-gateway\n")
    suite = install / "scripts/install-suite.sh"
    suite.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" >"$ARGUMENTS"\n')
    suite.chmod(0o755)
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
load_ui() { CHECKOUT=$INSTALL; }
warm_up() { :; }
find_uv() { UV=fixture; }
link_owg() { :; }
check_services() { :; }
main --repair --dir "$INSTALL" --yes
''', env={"INSTALL": str(install), "ARGUMENTS": str(tmp_path / "arguments")})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    arguments = (tmp_path / "arguments").read_text().splitlines()
    assert arguments[arguments.index("--services") + 1] == "weightroom,turnstile,voice-gateway"


@pytest.mark.parametrize("dirty", [False, True])
def test_bootstrap_resume_updates_clean_code_and_preserves_local_changes(tmp_path, dirty):
    source = tmp_path / "origin"
    (source / "scripts").mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
    helper = source / "scripts/install-suite.sh"
    helper.write_text("old helper\n")
    def git(*args):
        subprocess.run(["git", *args], text=True, capture_output=True, check=True)
    git("init", "-b", "main", str(source))
    git("-C", str(source), "config", "user.name", "Installer fixture")
    git("-C", str(source), "config", "user.email", "fixture@example.invalid")
    git("-C", str(source), "add", ".")
    git("-C", str(source), "commit", "-m", "old")
    install = tmp_path / "install"
    git("clone", str(source), str(install))
    helper.write_text("updated helper\n")
    git("-C", str(source), "commit", "-am", "fixed")
    (install / "keep-me").write_text("retained user data")
    if dirty:
        (install / "scripts/install-suite.sh").write_text("user changes\n")
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
parse_arguments --dir "$INSTALL"
ui_init
CHECKOUT='' RESUMING_INSTALL=1
fetch_code
''', env={"INSTALL": str(install)})
    assert (answer.returncode != 0) == dirty, answer.stdout + answer.stderr
    expected = "user changes\n" if dirty else "updated helper\n"
    assert (install / "scripts/install-suite.sh").read_text() == expected
    assert (install / "keep-me").read_text() == "retained user data"


@pytest.mark.parametrize("failure", ["database", "role"])
def test_postgresql_removal_failure_keeps_connection_files_and_stops_uninstall(tmp_path, failure):
    install = tmp_path / "install"
    (install / "keys/postgresql").mkdir(parents=True)
    dsn = install / "keys/postgresql/accounts.dsn"
    dsn.write_text("postgresql://suite_fixture:fixture@127.0.0.1/suite_fixture_accounts\n")
    (install / "data").mkdir()
    retained = install / "data/keep-me"
    retained.write_text("retained data")
    (install / ".install-receipt").write_text("state=pending\nmode=user\ncreated_dir=1\nservices=\n")
    answer = shell(tmp_path, script_functions("uninstall.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
load_ui() { :; }
owg_systemctl() { return 1; }
ui_sudo() { printf requested >"$PRIVILEGES"; }
sudo() {
  if [[ $* == *dropdb* && $FAILURE == database ]]; then return 1; fi
  if [[ $* == *dropuser* && $FAILURE == role ]]; then return 1; fi
  return 0
}
main --dir "$INSTALL" --yes --everything --no-backup
''', env={"INSTALL": str(install), "FAILURE": failure, "PRIVILEGES": str(tmp_path / "privileges")})
    assert answer.returncode != 0
    assert (tmp_path / "privileges").read_text() == "requested"
    assert dsn.exists() and retained.read_text() == "retained data"
    assert "its connection files were kept" in answer.stderr
    assert "Cool-down complete" not in answer.stdout


def test_backups_are_unique_and_private_before_data_is_copied(tmp_path):
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    (install / "data/keep-me").write_text("backup data")
    answer = shell(tmp_path, script_functions("uninstall.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
DIR=$INSTALL BACKUP_WANTED=1
declare -A REMOVE=([data]=1)
stop_services() { :; }
date() { printf fixed-timestamp; }
as_reader() {
  if [[ $1 == cat ]]; then stat -c '%a' "$BACKUP_FILE" >>"$MODES"; fi
  "$@"
}
backup
backup
''', env={"INSTALL": str(install), "HOME": str(tmp_path), "MODES": str(tmp_path / "modes")})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert (tmp_path / "modes").read_text().splitlines() == ["600", "600"]
    archives = list(tmp_path.glob("openWeightGym-backup-*.tar.gz"))
    assert len(archives) == 2
    for path in archives:
        with tarfile.open(path) as archive:
            assert archive.extractfile("data/keep-me").read() == b"backup data"


def test_an_install_with_nothing_to_back_up_is_not_offered_an_empty_backup(tmp_path):
    # An install that stopped early has no settings, keys or data yet; tar refuses an empty archive.
    install = tmp_path / "install"
    install.mkdir()
    answer = shell(tmp_path, script_functions("uninstall.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
DIR=$INSTALL BACKUP_WANTED='' ASSUME_YES=0 EVERYTHING=0
ITEMS=(settings)
declare -A REMOVE=([settings]=1) LABEL=([settings]="Settings and keys")
stop_services() { echo stop_services >>"$CALLS"; }
tar() { echo tar >>"$CALLS"; command tar "$@"; }
ui_confirm() { printf '%s\\n' "$1" >>"$CALLS"; }
ui_input() { echo ui_input >>"$CALLS"; }
backup
confirm
''', env={"INSTALL": str(install), "HOME": str(tmp_path), "CALLS": str(tmp_path / "calls")})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert "No backup needed" in answer.stdout
    assert (tmp_path / "calls").read_text().splitlines() == ["Go ahead?"]
    assert not list(tmp_path.glob("openWeightGym-backup-*"))


@pytest.mark.parametrize("outcome", ["cancel", "backup-failure", "stop-failure"])
def test_aborted_uninstall_restores_only_services_it_stopped(tmp_path, outcome):
    install = tmp_path / "install"
    (install / "data").mkdir(parents=True)
    retained = install / "data/keep-me"
    retained.write_text("retained data")
    (install / ".install-receipt").write_text("mode=user\nservices=weightroom,turnstile\n")
    answer = shell(tmp_path, script_functions("uninstall.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
load_ui() { :; }
inventory() { :; }
choose() { ITEMS=(data); declare -gA REMOVE=([data]=1) LABEL=([data]=Data); }
ui_confirm() { [[ $1 == 'Make a backup'* ]]; }
owg_systemctl() {
  [[ $1 == is-active ]] && return 0
  printf '%s\\n' "$*" >>"$OPERATIONS"
  [[ $OUTCOME != stop-failure || $1 != stop || $2 != weightroom.service ]]
}
tar() { [[ $OUTCOME != backup-failure ]] && command tar "$@"; }
main --dir "$INSTALL" --for-reinstall
''', env={"INSTALL": str(install), "HOME": str(tmp_path), "OUTCOME": outcome,
          "OPERATIONS": str(tmp_path / "operations")})
    assert answer.returncode == (3 if outcome == "cancel" else 78), answer.stdout + answer.stderr
    expected = ["stop turnstile.service", "stop weightroom.service"]
    if outcome != "stop-failure":
        expected.append("start weightroom.service")
    expected.append("start turnstile.service")
    assert (tmp_path / "operations").read_text().splitlines() == expected
    assert retained.read_text() == "retained data"
    if outcome == "backup-failure":
        assert not list(tmp_path.glob("openWeightGym-backup-*"))


@pytest.mark.parametrize("topology,services", [
    ("all", ["loadcoach", "weightroom", "turnstile", "voice", "voice-gateway"]),
    ("front", ["weightroom", "turnstile", "voice-gateway"]),
    ("compute", ["loadcoach", "voice"]),
])
def test_voice_setup_provisions_credentials_routes_profiles_and_only_required_bundles(tmp_path, topology, services):
    from loadcoach.config import read_settings
    from loadcoach.users import Users
    from turnstile.app import load_settings
    support = support_module()
    install = tmp_path / "install"
    shutil.copytree(ROOT / "settings/defaults", install / "settings/defaults")
    (install / "data").mkdir()
    (install / "voice").mkdir()
    shutil.copy2(ROOT / "voice/models.json", install / "voice/models.json")
    for service in set(services) - {"turnstile"}:
        support.render(ROOT / f"settings/examples/{service}.toml", install / f"settings/{service}.toml",
                       install, tmp_path, support.topology_values(service, topology, install, "10.0.0.5",
                            "http://10.0.0.1:8442", "http://10.0.0.5:8442", "", voice=True, voice_model="qwen3-1.7b"))
    if topology == "compute":
        (install / "keys").mkdir()
        (install / "keys/voice-service.key").write_text("lc_paired_front_key\n")
    calls = []
    assert support.prepare_voice(install, services, install_models=lambda folder, bundles: calls.append((folder, bundles))) == 0
    if topology == "front":
        assert calls == []
    else:
        assert calls == [(install / "models/voice", ["kokoro-multi-lang-v1_0", "sherpa-onnx-streaming-zipformer-en-2023-06-26"])]
        profile = read_settings(install / "settings/voice.toml")["profiles"]["default"]
        assert profile["model"] == "qwen3-1.7b" and profile["modes"] == ["direct"] and profile["speaker"] == 3
        assert profile["system_prompt"] and profile["max_output_tokens"] > 0
    if topology != "compute":
        door = load_settings(install / "settings/weightroom.toml")
        assert 8445 in door.ports
        assert door.upstream(8445) == ("http://127.0.0.1:9445" if topology == "all" else "http://10.0.0.5:8445")
        users = Users(str(install / "data/accounts.db"), {})
        try:
            for name, abilities in {"voice-service": ("hold", "load", "see-all"),
                                    "voice-gateway": ("app-sign-in", "load", "front-voice"),
                                    "voice-browser": ("front-voice",)}.items():
                key = install / "keys" / (name + ".key")
                user = users.by_key(key.read_text().strip())
                assert user.name == name and all(user.can(ability) for ability in abilities)
                assert not user.can("superuser") and stat.S_IMODE(key.stat().st_mode) == 0o600
        finally:
            users.db.close()


def test_voice_compute_requires_a_front_key_before_downloading_weights(tmp_path):
    support = support_module()
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings/voice.toml").write_text('key_file = "../keys/voice-service.key"\n')
    with pytest.raises(ValueError, match="front-owned service key"):
        support.prepare_voice(tmp_path, ["voice"], install_models=lambda *args, **kwargs: pytest.fail("unexpected download"))


def voice_models(tmp_path, monkeypatch, *, pinned_files, archive_files, archive_pinned, published):
    """The real voice-models.py against one fixture bundle; returns the module and a list of downloads."""
    spec = importlib.util.spec_from_file_location("fixture_voice_models", ROOT / "scripts/voice-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts/voice-models.py"))
    source = tmp_path / "source/bundle"
    source.mkdir(parents=True)
    for name, data in archive_files.items():
        (source / name).write_bytes(data)
    built = tmp_path / "bundle.tar.bz2"
    with tarfile.open(built, "w:bz2") as packed:
        packed.add(source, arcname="bundle")
    digest = hashlib.sha256(built.read_bytes()).hexdigest()
    manifest = {"bundles": [{"name": name, "url": "https://github.com/owner/repo/releases/download/models/bundle.tar.bz2",
                             "archive_sha256": digest if archive_pinned else hashlib.sha256(b"first upload").hexdigest(),
                             "files": {key: hashlib.sha256(value).hexdigest() for key, value in pinned_files.items()}}
                            for name in ("bundle", "unused")]}
    (tmp_path / "voice").mkdir()
    (tmp_path / "voice/models.json").write_text(json.dumps(manifest))
    downloads = []

    def fetch(url, target):
        downloads.append(url)
        shutil.copyfile(built, target)
    monkeypatch.setattr(module.urllib.request, "urlretrieve", fetch)
    answers = {"match": digest, "none": None, "damaged": hashlib.sha256(b"something else").hexdigest()}
    monkeypatch.setattr(module, "published_sha256",
                        lambda url: pytest.fail("asked the publisher") if published == "never" else answers[published])
    return module, downloads


PINNED = {"model.onnx": b"fixture model", "tokens.txt": b"fixture tokens"}


@pytest.mark.parametrize("case,archive_files,archive_pinned,published,warning", [
    ("pinned archive", PINNED, True, "never", None),
    ("rebuilt archive, same files", PINNED, False, "none", None),
    ("new files the publisher confirms", {**PINNED, "model.onnx": b"new model"}, False, "match",
     "WARN bundle has changed upstream: installed the new version, which matches"),
    ("new files nobody can confirm", {**PINNED, "model.onnx": b"new model"}, False, "none",
     "WARN bundle has changed upstream and its publisher lists no checksum"),
])
def test_speech_bundles_install_when_upstream_rebuilds_or_updates_them(tmp_path, monkeypatch, capsys, case,
                                                                        archive_files, archive_pinned, published, warning):
    module, downloads = voice_models(tmp_path, monkeypatch, pinned_files=PINNED, archive_files=archive_files,
                                     archive_pinned=archive_pinned, published=published)
    models = tmp_path / "models"
    module.install(models, bundles=["bundle"])
    assert {path.name: path.read_bytes() for path in (models / "bundle").iterdir()} == archive_files
    output = capsys.readouterr().out
    assert (warning in output) if warning else ("WARN" not in output)
    # What was installed is what later runs check, so an updated bundle is not downloaded again.
    module.install(models, bundles=["bundle"])
    assert len(downloads) == 1 and sorted(path.name for path in models.iterdir()) == [".bundle.files.json", "bundle"]


def test_a_download_that_differs_from_its_publishers_checksum_is_retried_then_refused(tmp_path, monkeypatch):
    module, downloads = voice_models(tmp_path, monkeypatch, pinned_files=PINNED,
                                     archive_files={**PINNED, "model.onnx": b"truncated"},
                                     archive_pinned=False, published="damaged")
    with pytest.raises(ValueError, match="download is damaged"):
        module.install(tmp_path / "models", bundles=["bundle"])
    assert len(downloads) == 2 and list((tmp_path / "models").iterdir()) == []


def test_a_damaged_installed_bundle_is_downloaded_again(tmp_path, monkeypatch):
    module, downloads = voice_models(tmp_path, monkeypatch, pinned_files=PINNED, archive_files=PINNED,
                                     archive_pinned=True, published="never")
    models = tmp_path / "models"
    model = models / "bundle/model.onnx"
    model.parent.mkdir(parents=True)
    for name, data in PINNED.items():
        (models / "bundle" / name).write_bytes(data)
    module.install(models, bundles=["bundle"])
    assert downloads == []
    model.write_bytes(b"damaged")
    module.install(models, bundles=["bundle"])
    assert len(downloads) == 1 and model.read_bytes() == b"fixture model"
    assert not (models / "unused").exists()
    with pytest.raises(ValueError, match="Unknown speech bundles"):
        module.install(models, bundles=["unknown"])


@pytest.mark.parametrize("url,release,expected", [
    ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro.tar.bz2",
     {"assets": [{"name": "other.tar.bz2", "digest": "sha256:" + "1" * 64},
                 {"name": "kokoro.tar.bz2", "digest": "sha256:" + "a" * 64}]}, "a" * 64),
    ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/old.tar.bz2",
     {"assets": [{"name": "old.tar.bz2", "digest": None}]}, None),
    ("https://example.com/bundle.tar.bz2", None, None),
    ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro.tar.bz2", OSError("rate limited"), None),
])
def test_publisher_checksums_come_from_the_github_release(monkeypatch, url, release, expected):
    spec = importlib.util.spec_from_file_location("publisher_voice_models", ROOT / "scripts/voice-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    asked = []

    def answer(request, timeout):
        asked.append(request.full_url)
        if isinstance(release, Exception):
            raise release
        import io
        return io.BytesIO(json.dumps(release).encode())
    monkeypatch.setattr(module.urllib.request, "urlopen", answer)
    assert module.published_sha256(url) == expected
    if release is None:
        assert asked == []
    else:
        assert asked == ["https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/" + url.split("/")[-2]]


def test_voice_identity_preserves_existing_policy_keys_and_disabled_accounts(tmp_path):
    from loadcoach.users import Users
    from weightroom.bootstrap import service_identity
    shutil.copytree(ROOT / "settings/defaults", tmp_path / "settings/defaults")
    (tmp_path / "data").mkdir()
    config = tmp_path / "settings/weightroom.toml"
    config.write_text('data = "../data"\n')
    key_file = tmp_path / "custom/service.key"
    first = service_identity(config, "voice-service", "system", key_file, ("hold", "load"))
    users = Users(str(tmp_path / "data/accounts.db"), {})
    try:
        users.change("voice-service", own={"add": ["hold", "load"], "models": ["small"], "max_requests": 2})
        assert service_identity(config, "voice-service", "system", key_file, ("hold", "load", "see-all")) == first
        assert users.own("voice-service") == {"add": ["hold", "load", "see-all"], "models": ["small"], "max_requests": 2}
        users.change("voice-service", enabled=False)
        with pytest.raises(ValueError, match="disabled"):
            service_identity(config, "voice-service", "system", key_file, ("hold",))
        assert users.by_name("voice-service") is None and key_file.read_text().strip() == first
    finally:
        users.db.close()


@pytest.mark.parametrize("checkpoint", ["pending", "legacy-units", "legacy-settings"])
def test_resume_finishes_an_account_interrupted_before_password_setup(tmp_path, checkpoint):
    support = support_module()
    install = tmp_path / "install"
    (install / "settings/defaults").mkdir(parents=True)
    (install / ".venv/bin").mkdir(parents=True)
    (install / "data").mkdir()
    shutil.copy2(ROOT / "settings/defaults/weightroom.toml", install / "settings/defaults/weightroom.toml")
    support.render(ROOT / "settings/examples/weightroom.toml", install / "settings/weightroom.toml", install, tmp_path,
                   support.topology_values("weightroom", "front", install, "10.0.0.1", "", "", ""))
    (install / ".venv/bin/python").symlink_to(sys.executable)
    command = install / ".venv/bin/weightroom"
    command.write_text('#!/usr/bin/env bash\nexec "$TEST_PYTHON" -c "from weightroom.app import main; main()" "$@"\n')
    command.chmod(0o755)
    helper = install / "scripts/install-suite.sh"
    helper.parent.mkdir()
    helper.write_text("#!/usr/bin/env bash\nexit 0\n")
    helper.chmod(0o755)
    if checkpoint == "legacy-units":
        units = tmp_path / "config/systemd/user"
        units.mkdir(parents=True)
        (units / "weightroom.service").write_text(f"ExecStart={install}/.venv/bin/weightroom\n")
    environment = {"INSTALL": str(install), "TEST_PYTHON": sys.executable, "OWG_ADMIN_PASSWORD": "fixture-password"}
    add = shell(tmp_path, '"$INSTALL/.venv/bin/weightroom" --config "$INSTALL/settings/weightroom.toml" user add sam superuser', env=environment)
    assert add.returncode == 0, add.stderr
    setup = '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
parse_arguments --dir "$INSTALL" --topology front --admin sam --no-model --llama skip
CREATED_DIR=1 SERVICES=weightroom,turnstile
'''
    if checkpoint == "pending":
        setup += "write_receipt pending\n"
    answer = shell(tmp_path, script_functions("install.sh") + setup + '''
load_ui() { CHECKOUT=$INSTALL; }
warm_up() { SYSTEMD=0; SYSTEMD_USER=0; LINGER=0; }
game_plan() { [[ $CHECKPOINT == legacy-settings ]] || exit 99; }
find_uv() { UV=fixture; }
loading_plates() { services_for; write_receipt pending; }
setting_up() { :; }
link_owg() { OWG_LINK=$INSTALL/owg; POINTER=$INSTALL/pointer; }
finale() { echo COMPLETE; }
main --repair --dir "$INSTALL" --yes --no-browser --topology front --admin sam --no-model --llama skip
''', env={**environment, "CHECKPOINT": checkpoint})
    assert answer.returncode == 0, answer.stderr + answer.stdout
    assert "COMPLETE" in answer.stdout
    receipt = (install / ".install-receipt").read_text()
    assert "state=complete" in receipt and "created_dir=1" in receipt and "fixture-password" not in receipt
    from loadcoach.users import Users
    users = Users(str(install / "data/accounts.db"), {})
    try:
        assert users.password_hash("sam")
        assert users.by_name("sam").can("superuser")
    finally:
        users.db.close()
