"""Additive installer choices and isolated optional-runtime installation contracts."""
from argparse import Namespace
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

from engines.strata import install as strata
from test_setup_recovery import ROOT, one_function, script_functions, shell


def component_plan(tmp_path, args="", *, selection="", confirmations="", legacy=False):
    install = tmp_path / "installed suite"
    install.mkdir()
    (install / ".install-receipt").write_text(
        "state=complete\nmode=user\ntopology=all\nservices=loadcoach,weightroom,turnstile\n"
        "llama=auto\nstrata=install\nsdcpp=install\nvoice=0\n")
    if legacy:
        (install / ".install-receipt").write_text("services=loadcoach\nmode=user\n")
        (install / ".venv/bin").mkdir(parents=True)
        (install / ".venv/bin/python").symlink_to(sys.executable)
        (install / "settings").mkdir()
        (install / "settings/loadcoach.toml").write_text('front = "http://console:8442"\nmachine_name = "gpu-two"\n')
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
parse_arguments --components ''' + args + '''
ui_init
receipt_load "$INSTALL"
DIR=$INSTALL
ui_checklist() { printf '%s\\n' "$@" >"$ROWS"; printf -v "$1" '%s' "$SELECTION"; }
ui_confirm() { [[ "$1" == 'Install these components?' || "$1" == *"$CONFIRMATIONS"* && -n $CONFIRMATIONS ]]; }
ui_choose() { printf -v "$1" '%s' 2; }
ui_input() { printf -v "$1" '%s' 8192; }
apply_components() {
  printf '%s\\n' "$SERVICES" "$LLAMA" "$STRATA_ACTION" "$SDCPP_ACTION" "$SDCPP_BACKEND" "$AUDIOCPP_ACTION" "$STRATA_HOST_RAM_MIB" "$FRONT_URL" "$MACHINE_NAME" >"$PLAN"
}
install_components
''', env={"INSTALL": str(install), "PLAN": str(tmp_path / "plan"), "ROWS": str(tmp_path / "rows"),
          "SELECTION": selection, "CONFIRMATIONS": confirmations})
    return answer, tmp_path / "plan", tmp_path / "rows"


def test_cli_additions_union_services_without_repeating_original_engines(tmp_path):
    answer, plan, _ = component_plan(tmp_path, "--yes --services spotter,toollocker --audiocpp install --audiocpp-backend cpu")
    assert answer.returncode == 0, answer.stderr
    assert plan.read_text().splitlines()[:6] == [
        "loadcoach,weightroom,turnstile,spotter,toollocker", "skip", "keep", "keep", "cuda", "install"]


def test_menu_offers_only_missing_services_and_retains_existing_ones(tmp_path):
    answer, plan, rows = component_plan(tmp_path, selection="2", confirmations="stable-diffusion.cpp")
    assert answer.returncode == 0, answer.stderr
    offered = rows.read_text()
    assert "loadcoach|" not in offered and "weightroom|" not in offered
    assert "voice|" in offered and "toollocker|" in offered
    assert plan.read_text().splitlines()[:6] == [
        "loadcoach,weightroom,turnstile,voice,voice-gateway", "skip", "keep", "install", "cpu", "keep"]


def test_empty_component_selection_changes_nothing(tmp_path):
    answer, plan, _ = component_plan(tmp_path, "--yes")
    assert answer.returncode == 0, answer.stderr
    assert not plan.exists() and "No additional components" in answer.stdout


def test_legacy_node_keeps_its_console_address_and_machine_name(tmp_path):
    answer, plan, _ = component_plan(tmp_path, "--yes --services spotter", legacy=True)
    assert answer.returncode == 0, answer.stderr
    assert plan.read_text().splitlines()[0] == "loadcoach,spotter"
    assert plan.read_text().splitlines()[-2:] == ["http://console:8442", "gpu-two"]


@pytest.mark.parametrize("args", ["--services unknown", "--services loadcoach,unknown"])
def test_unknown_component_is_rejected_before_installation(tmp_path, args):
    answer, plan, _ = component_plan(tmp_path, "--yes " + args)
    assert answer.returncode != 0
    assert not plan.exists() and "Cannot add unknown" in answer.stderr


def test_voice_flag_adds_voice_and_gateway(tmp_path):
    answer, plan, _ = component_plan(tmp_path, "--yes --voice")
    assert answer.returncode == 0, answer.stderr
    assert plan.read_text().splitlines()[0].endswith(",voice,voice-gateway")


def test_explicit_llama_skip_does_not_prompt_to_install_it(tmp_path):
    answer, plan, _ = component_plan(tmp_path, "--llama skip --services spotter", confirmations="llama.cpp")
    assert answer.returncode == 0, answer.stderr
    assert plan.read_text().splitlines()[1] == "skip"


@pytest.mark.parametrize("arguments", [
    "--strata wrong", "--sdcpp-backend rocm", "--audiocpp wrong", "--strata-host-ram-mib -1",
    "--services voice", "--topology front --strata install",
])
def test_invalid_runtime_arguments_fail_before_installing(tmp_path, arguments):
    answer = shell(tmp_path, script_functions("install.sh") + '''
parse_arguments ''' + arguments + '''
[[ -n $ARGUMENT_ERROR ]]
''')
    assert answer.returncode == 0, answer.stderr


def test_initial_plan_passes_engine_choices_and_backends_to_suite(tmp_path):
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
parse_arguments --yes --topology all --strata install --strata-host-ram-mib 8192 --sdcpp install --sdcpp-backend vulkan --audiocpp install --audiocpp-backend cpu
ui_init
DIR=$INSTALL HOST_ADDRESS=127.0.0.1 UV=/fixture/uv
save_machine_key() { :; }
as_owner() { printf '%s\\n' "$@"; }
setting_up
''', env={"INSTALL": str(tmp_path)})
    assert answer.returncode == 0, answer.stderr
    for flag, value in (("strata", "install"), ("strata-host-ram-mib", "8192"), ("sdcpp-backend", "vulkan"), ("audiocpp-backend", "cpu")):
        assert f"--{flag}\n{value}\n" in answer.stdout


def test_component_failure_keeps_pending_selection_then_resumes(tmp_path):
    install = tmp_path / "installed"
    install.mkdir()
    (install / "pyproject.toml").touch()
    driver = script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
load_ui() { CHECKOUT=$INSTALL; }
warm_up() { HOST_ADDRESS=127.0.0.1; }
find_uv() { UV=/fixture/uv; }
as_owner() {
  if [[ $1 == */install-suite.sh ]]; then
    printf '%s\\n' "$@" >"$ARGS"
    return "$CHILD_STATUS"
  fi
  "$@"
}
link_owg() { :; }
check_services() { :; }
main --yes --dir "$INSTALL"
'''
    receipt = install / ".install-receipt"
    receipt.write_text("state=pending\noperation=components\nmode=user\ntopology=compute\n"
                      "services=loadcoach,spotter\nfront_url=http://console:8442\nllama=skip\n"
                      "audiocpp=install\naudiocpp_backend=vulkan\n")
    env = {"INSTALL": str(install), "ARGS": str(tmp_path / "args"), "CHILD_STATUS": "9"}
    failed = shell(tmp_path, driver, env=env)
    assert failed.returncode != 0, failed.stdout
    assert "state=pending" in receipt.read_text()
    assert "operation=components" in receipt.read_text()
    completed = shell(tmp_path, driver, env={**env, "CHILD_STATUS": "0"})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "state=complete" in receipt.read_text() and "services=loadcoach,spotter" in receipt.read_text()
    arguments = (tmp_path / "args").read_text()
    assert "--keep-existing-units" in arguments
    assert "--audiocpp\ninstall" in arguments and "--audiocpp-backend\nvulkan" in arguments


@pytest.mark.parametrize("symlink", [False, True])
def test_addition_keeps_existing_unit_memory_ceiling(tmp_path, symlink):
    units = tmp_path / "units"
    units.mkdir()
    unit = units / "loadcoach.service"
    original = "[Service]\nMemoryMax=8G\nMemorySwapMax=0\n"
    unit.write_text(original)
    if symlink:
        source = tmp_path / "custom-loadcoach.service"
        unit.rename(source)
        unit.symlink_to(source)
    answer = shell(tmp_path, '''set -euo pipefail
ROOT=$INSTALL UNIT_DIR=$UNITS PY=$TEST_PYTHON DRY_RUN=0 KEEP_EXISTING_UNITS=1
''' + one_function("scripts/install-suite.sh", "render_unit") + '\nrender_unit loadcoach\n',
        env={"INSTALL": str(tmp_path), "UNITS": str(units), "TEST_PYTHON": sys.executable})
    assert answer.returncode == 0, answer.stderr
    assert unit.read_text() == original


@pytest.mark.parametrize("script,function,choice", [("install.sh", "installed_menu", 2), ("scripts/owg", "menu", 6)])
def test_both_menus_expose_install_components(tmp_path, script, function, choice):
    answer = shell(tmp_path, '''set -euo pipefail
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
UI_ASK=1 DIR=$TEST_REPO
version() { :; }
commit() { :; }
status() { :; }
programs() { printf loadcoach; }
receipt_get() { printf loadcoach; }
ui_choose() { printf '%s\\n' "$@"; printf -v "$1" '%s' "$CHOICE"; }
install_components() { printf 'components dispatched'; }
main() { [[ $1 == install ]]; printf 'components dispatched'; }
''' + one_function(script, function) + '\n' + function,
        env={"CHOICE": str(choice)})
    assert answer.returncode == 0, answer.stderr
    assert "Install components|" in answer.stdout and "components dispatched" in answer.stdout


@pytest.mark.parametrize("services", ["loadcoach", "weightroom"])
def test_suite_plans_optional_engines_only_on_model_nodes(tmp_path, services):
    install = tmp_path / "suite"
    (install / "scripts/lib").mkdir(parents=True)
    for name in ("ui.sh", "detect.sh"):
        shutil.copy2(ROOT / "scripts/lib" / name, install / "scripts/lib" / name)
    for name in ("install-suite.sh", "install-suite-support.py"):
        shutil.copy2(ROOT / "scripts" / name, install / "scripts" / name)
    shutil.copytree(ROOT / "services", install / "services")
    shutil.copytree(ROOT / "settings/examples", install / "settings/examples")
    (install / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "0.1.0"\n')
    env = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "OWG_LOG": str(tmp_path / "log"), "OWG_NO_TTY": "1", "OWG_PLAIN": "1", "PYTHONPATH": str(ROOT),
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    answer = subprocess.run(["bash", str(install / "scripts/install-suite.sh"), "--services", services,
        "--database", "keep", "--dry-run", "--non-interactive", "--yes", "--strata", "install",
        "--strata-host-ram-mib", "8192", "--sdcpp", "install", "--sdcpp-backend", "cpu",
        "--audiocpp", "install", "--audiocpp-backend", "vulkan"], env=env, capture_output=True, text=True, timeout=30)
    if services == "weightroom":
        assert answer.returncode != 0 and "Strata requires loadcoach" in answer.stderr
    else:
        assert answer.returncode == 0, answer.stdout + answer.stderr
        assert "engines/strata/install.py" in answer.stdout and "--host-ram-mib 8192" in answer.stdout
        assert "install-sdcpp.py" in answer.stdout and "--backend cpu" in answer.stdout
        assert "soundroom/install.py" in answer.stdout and "--backend vulkan" in answer.stdout
    assert not (install / "runtimes").exists() and not (install / ".venv").exists()


def test_owg_install_dispatches_to_installer_without_losing_arguments(tmp_path):
    install = tmp_path / "installed suite"
    (install / "scripts/lib").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/owg", install / "scripts/owg")
    for name in ("ui", "detect", "receipt"):
        shutil.copy2(ROOT / f"scripts/lib/{name}.sh", install / "scripts/lib")
    (install / "install.sh").write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    (install / "install.sh").chmod(0o755)
    answer = shell(tmp_path, '"$INSTALL/scripts/owg" install --yes --services spotter --strata install', env={"INSTALL": str(install)})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.splitlines() == ["--components", "--dir", str(install), "--yes", "--services", "spotter", "--strata", "install"]


@pytest.fixture
def strata_install(tmp_path, monkeypatch):
    root = tmp_path / "suite"
    (root / "settings").mkdir(parents=True)
    (root / "services").mkdir()
    shutil.copy2(ROOT / "services/loadcoach-strata.conf", root / "services")
    config = root / "settings/loadcoach.toml"
    config.write_text('host = "127.0.0.1"\n# keep this\n[engines.sdcpp]\nenabled = false\n')
    args = Namespace(root=root, prefix=None, unit_dir=tmp_path / "units", host_ram_mib=8192, jobs=2, check=False, dry_run=False)
    commands = []
    fail = []

    def run(command, **kwargs):
        command = [str(value) for value in command]
        commands.append(command)
        if command[:2] == ["git", "clone"]:
            (Path(command[-1]) / "serve").mkdir(parents=True)
        elif "checkout" in command:
            (Path(command[2]) / "serve/server.py").write_text("reviewed fixture")
        elif command[:2] == ["cmake", "--build"]:
            if fail:
                raise subprocess.CalledProcessError(1, command)
            build = Path(command[2]); build.mkdir()
            for name in ("strata", "libstrata_kq.so"):
                path = build / name; path.write_text("native fixture"); path.chmod(0o755)
        elif command[1:3] == ["-m", "venv"]:
            venv = Path(command[-1]); (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
            (venv / "bin/python").touch(); (venv / "bin/python").chmod(0o755)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(strata, "run", run)
    monkeypatch.setattr(strata.shutil, "which", lambda tool: "/fixture/" + tool)
    monkeypatch.setattr(strata, "verify_source", lambda source: (source / "serve/server.py").read_text())
    return args, config, commands, fail


def test_strata_build_is_pinned_private_and_does_not_fetch_models(strata_install):
    args, config, commands, _ = strata_install
    original = config.read_text()
    strata.install(args)
    parsed = tomllib.loads(config.read_text())
    assert parsed["engines"]["sdcpp"] == {"enabled": False}
    assert "# keep this" in config.read_text()
    node = parsed["engines"]["strata"]
    assert node["enabled"] and node["allow_host_ram"] and node["host_ram_mib"] == 8192
    assert strata.REVISION in node["source"]
    assert any(command[-2:] == ["--detach", strata.REVISION] for command in commands)
    assert any("-DSTRATA_ENABLE_CUDA=ON" in command for command in commands)
    assert not any("setup.py" in " ".join(command) or "huggingface" in " ".join(command) for command in commands)
    assert next((args.root / "data/installation/strata-backups").iterdir()).read_text() == original
    dropin = (args.unit_dir / "loadcoach.service.d/50-owg-strata.conf").read_text()
    assert "Delegate=memory" in dropin and "MemoryMax=" not in dropin
    count = len(commands)
    strata.install(args)
    assert not any(command[0] in ("git", "cmake") for command in commands[count:])
    Path(node["executable"]).write_text("tampered")
    with pytest.raises(ValueError, match="artifacts changed"):
        strata.install(args)


def test_failed_strata_build_preserves_settings_and_can_retry(strata_install):
    args, config, commands, fail = strata_install
    original = config.read_bytes()
    fail.append(True)
    with pytest.raises(subprocess.CalledProcessError):
        strata.install(args)
    assert config.read_bytes() == original
    assert not args.unit_dir.exists()
    fail.clear()
    strata.install(args)
    assert sum(command[:2] == ["git", "clone"] for command in commands) == 1


def test_strata_dry_run_makes_no_build_or_config_changes(strata_install):
    args, config, commands, _ = strata_install
    args.dry_run = True
    original = config.read_bytes()
    strata.install(args)
    assert not commands and config.read_bytes() == original
    assert not (args.root / "runtimes").exists()


def test_later_llama_install_updates_only_its_setting_and_backs_up(tmp_path):
    (tmp_path / "settings").mkdir()
    config = tmp_path / "settings/loadcoach.toml"
    original = 'llama_server = "/old/server"\n# keep\nport = 1234\n'
    config.write_text(original)
    executable = tmp_path / "new server"
    executable.touch(); executable.chmod(0o755)
    answer = subprocess.run([sys.executable, str(ROOT / "scripts/install-suite-support.py"), "llama-server", "--root", str(tmp_path), "--executable", str(executable)], capture_output=True, text=True)
    assert answer.returncode == 0, answer.stderr
    assert tomllib.loads(config.read_text()) == {"llama_server": str(executable), "port": 1234}
    assert "# keep" in config.read_text()
    assert next(config.parent.glob("*.before-llama-install-*")).read_text() == original
