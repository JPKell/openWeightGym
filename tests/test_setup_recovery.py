"""Installer failure paths: credentials, interrupted setup, ownership and safe removal."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
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
    found = re.search(rf"(?ms)^{re.escape(function)}\(\) \{{.*?^\}}", (ROOT / name).read_text())
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
    assert "explicit --dir" in answer.stderr
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
source "$TEST_REPO/scripts/lib/receipt.sh"
owg_systemctl() { return 1; }
load_ui() { source "$TEST_REPO/scripts/lib/ui.sh"; }
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


def test_virtualenv_and_settings_do_not_complete_an_install(tmp_path):
    install = tmp_path / "install"
    (install / ".venv/bin").mkdir(parents=True)
    (install / ".venv/bin/python").touch(mode=0o755)
    (install / "settings").mkdir()
    (install / "settings/loadcoach.toml").write_text("port = 9442\n")
    answer = shell(tmp_path, script_functions("install.sh") + '''
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
source "$TEST_REPO/scripts/lib/receipt.sh"
parse_arguments
CHECKOUT=$OTHER_CHECKOUT SYS_OPT=$INSTALL
locate_installation
printf '%s %s %s' "$DIR" "$SYSTEM" "$OWG_OWNER"
''', env={"INSTALL": str(install), "OTHER_CHECKOUT": str(tmp_path / "other")})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout == f"{install} 1 fixture-service"


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


def test_selected_speech_bundles_still_require_matching_checksums(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("recovery_voice_models", ROOT / "scripts/voice-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts/voice-models.py"))
    monkeypatch.setattr(module.urllib.request, "urlretrieve", lambda *args: pytest.fail("unexpected download"))
    (tmp_path / "voice").mkdir()
    manifest = {"bundles": [{"name": name, "url": "https://invalid.example/unused", "files": {
        "model.onnx": hashlib.sha256(b"fixture model").hexdigest()}} for name in ("needed", "unused")]}
    (tmp_path / "voice/models.json").write_text(json.dumps(manifest))
    model = tmp_path / "models/needed/model.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"fixture model")
    module.install(tmp_path / "models", bundles=["needed"])
    model.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="Installed checksum differs"):
        module.install(tmp_path / "models", bundles=["needed"])
    with pytest.raises(ValueError, match="Unknown speech bundles"):
        module.install(tmp_path / "models", bundles=["unknown"])


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
