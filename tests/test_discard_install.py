"""Failed installation cleanup preserves working components and cancels automatic retries."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys

import pytest

from test_setup_recovery import ROOT, one_function, script_functions, shell


spec = importlib.util.spec_from_file_location("discard_install", ROOT / "scripts/discard-install.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def folder(root, name, content="unfinished"):
    path = root / name
    path.mkdir(parents=True)
    (path / "fixture").write_text(content)
    return path


@pytest.fixture
def pending(tmp_path):
    root = tmp_path / "installed suite"
    root.mkdir()
    units = tmp_path / "units"
    units.mkdir()
    (units / "loadcoach.service").write_text(f'ExecStart="{root}/.venv/bin/loadcoach"\n')
    # A listed but uninstalled addition must stop being treated as installed after cancellation.
    (units / "spotter.service").write_text('ExecStart=/another/installation/.venv/bin/spotter\n')
    (root / ".install-receipt").write_text(
        f"version=2\nstate=pending\nmode=user\noperation=components\nunit_dir={units}\n"
        "services=loadcoach,spotter,voice\ntopology=compute\nstrata=install\nsdcpp=install\n"
        "audiocpp=install\nllama=source\nvoice=1\ncreated_dir=1\n")
    (root / "settings").mkdir()
    (root / "settings/loadcoach.toml").write_text('host="127.0.0.1"\n')
    for name in ("models", "keys", "data", ".venv", ".python", ".tools"):
        folder(root, name, "keep shared files")
    return root


def receipt(root):
    return dict(line.split("=", 1) for line in (root / ".install-receipt").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def test_preview_then_discard_keeps_completed_components_and_is_idempotent(pending):
    root = pending
    partial = [folder(root, name) for name in (
        "runtimes/strata/releases/failed-cuda", "runtimes/sdcpp/source/new",
        "runtimes/sdcpp/build/new-cuda", "runtimes/audiocpp/build/new-cpu",
        "runtimes/audiocpp/releases/.stage-aborted", "runtimes/llamacpp/source/b999",
        "runtimes/llamacpp/releases/b999-cuda.partial")]
    complete = folder(root, "runtimes/strata/releases/finished-cuda")
    (complete / "installation.json").write_text('{"revision":"finished"}')
    original = (root / ".install-receipt").read_bytes()
    cleanup.discard(root)
    assert (root / ".install-receipt").read_bytes() == original
    assert all(path.exists() for path in partial)
    cleanup.discard(root, apply=True)
    assert all(not path.exists() for path in partial)
    assert complete.is_dir()
    assert receipt(root)["state"] == "cancelled"
    assert receipt(root)["services"] == "loadcoach"
    assert receipt(root)["strata"] == receipt(root)["sdcpp"] == receipt(root)["audiocpp"] == "keep"
    assert receipt(root)["llama"] == "skip" and receipt(root)["voice"] == "0"
    for name in ("models", "keys", "data", ".venv", ".python", ".tools"):
        assert (root / name / "fixture").read_text() == "keep shared files"
    backup, = root.glob(".install-receipt.before-discard-*")
    assert backup.read_bytes() == original and stat.S_IMODE(backup.stat().st_mode) == 0o600
    cleanup.discard(root, apply=True)
    assert list(root.glob(".install-receipt.before-discard-*")) == [backup]


@pytest.mark.parametrize("engine", ["sdcpp", "audiocpp", "llamacpp"])
def test_completed_release_preserves_its_source_and_build(pending, engine):
    source_name = "b123" if engine == "llamacpp" else "a" * 40
    release_name = source_name + "-cuda"
    release = folder(pending, f"runtimes/{engine}/releases/{release_name}")
    source = folder(pending, f"runtimes/{engine}/source/{source_name}")
    build = folder(pending, f"runtimes/{engine}/build/{release_name}")
    if engine == "llamacpp":
        (release / "llama-server").touch()
    else:
        (release / "installation.json").write_text(json.dumps({"revision": source_name}))
    cleanup.discard(pending, apply=True)
    assert all(path.is_dir() for path in (release, source, build))


@pytest.mark.parametrize("reference", ["current", "configured"])
def test_active_release_without_manifest_is_kept(pending, reference):
    active = folder(pending, "runtimes/strata/releases/active-cuda")
    if reference == "current":
        (pending / "runtimes/strata/current").symlink_to(active)
    else:
        (pending / "settings/loadcoach.toml").write_text(
            '[engines.strata]\nenabled=true\nexecutable="runtimes/strata/releases/active-cuda/build/strata"\n')
    cleanup.discard(pending, apply=True)
    assert active.is_dir()


@pytest.mark.parametrize("redirect", ["runtimes", "runtimes/strata", "runtimes/strata/releases",
                                      "runtimes/strata/releases/failed"])
def test_cleanup_never_follows_redirected_runtime_directories(pending, tmp_path, redirect):
    outside = folder(tmp_path, "external", "do not delete")
    folder(outside, "releases/nested")
    link = pending / redirect
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    cleanup.discard(pending, apply=True)
    assert link.is_symlink() and (outside / "fixture").read_text() == "do not delete"
    assert (outside / "releases/nested").exists()


def test_internal_symlink_is_unlinked_without_removing_its_target(pending, tmp_path):
    partial = folder(pending, "runtimes/strata/releases/failed")
    outside = folder(tmp_path, "external")
    (partial / "link").symlink_to(outside, target_is_directory=True)
    cleanup.discard(pending, apply=True)
    assert not partial.exists() and outside.exists()


def test_unselected_engine_and_malformed_settings_are_not_removed(pending):
    partial = folder(pending, "runtimes/strata/releases/failed")
    checkpoint = pending / ".install-receipt"
    checkpoint.write_text(checkpoint.read_text().replace("strata=install", "strata=keep"))
    assert partial not in cleanup.partial_builds(pending, receipt(pending))
    (pending / "settings/loadcoach.toml").write_text("not valid toml")
    original = checkpoint.read_bytes()
    with pytest.raises(ValueError):
        cleanup.discard(pending, apply=True)
    assert partial.exists() and checkpoint.read_bytes() == original


def test_removal_failure_leaves_checkpoint_pending(pending, monkeypatch):
    folder(pending, "runtimes/strata/releases/failed")
    def denied(path):
        raise PermissionError("fixture failure")
    monkeypatch.setattr(cleanup.shutil, "rmtree", denied)
    with pytest.raises(PermissionError):
        cleanup.discard(pending, apply=True)
    assert receipt(pending)["state"] == "pending"


@pytest.mark.parametrize("state", ["complete", "cancelled"])
def test_no_pending_plan_changes_nothing(pending, state):
    checkpoint = pending / ".install-receipt"
    checkpoint.write_text(checkpoint.read_text().replace("state=pending", "state=" + state))
    partial = folder(pending, "runtimes/strata/releases/failed")
    original = checkpoint.read_bytes()
    cleanup.discard(pending, apply=True)
    assert partial.exists() and checkpoint.read_bytes() == original


def test_legacy_receipt_stays_private(pending):
    legacy = pending / "data/install-receipt"
    (pending / ".install-receipt").rename(legacy)
    legacy.chmod(0o600)
    cleanup.discard(pending, apply=True)
    assert "state=cancelled" in legacy.read_text()
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o600
    assert not (pending / ".install-receipt").exists()


def test_running_installer_cannot_be_discarded(pending):
    partial = folder(pending, "runtimes/strata/releases/failed")
    start = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
    checkpoint = pending / ".install-receipt"
    checkpoint.write_text(checkpoint.read_text() + f"installer_pid={os.getpid()}\ninstaller_start={start}\n")
    with pytest.raises(ValueError, match="still running"):
        cleanup.discard(pending, apply=True)
    assert partial.exists() and receipt(pending)["state"] == "pending"
    checkpoint.write_text(checkpoint.read_text().replace("installer_start=" + start, "installer_start=0"))
    cleanup.discard(pending, apply=True)
    assert not partial.exists() and "installer_pid" not in receipt(pending)


def test_no_finished_services_does_not_mark_cancelled_setup_as_installed(pending, tmp_path):
    shutil.rmtree(tmp_path / "units")
    cleanup.discard(pending, apply=True)
    (pending / ".venv/bin").mkdir()
    (pending / ".venv/bin/python").symlink_to(sys.executable)
    answer = shell(tmp_path, '''
source "$TEST_REPO/scripts/lib/receipt.sh"
''' + one_function("install.sh", "is_installed") + '''
receipt_load "$INSTALL"
is_installed "$INSTALL"
''', env={"INSTALL": str(pending)})
    assert answer.returncode == 1


def test_cleanup_failure_retry_does_not_resume_installation(tmp_path):
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/detect.sh"
parse_arguments --dir "$INSTALL" --discard-install
set_retry
printf '%s\\n' "$OWG_RETRY"
''', env={"INSTALL": str(tmp_path / "missing")})
    assert answer.returncode == 0, answer.stderr
    assert "--discard-install" in answer.stdout and "Retry cleanup:" in answer.stdout
    assert "carries on where it stopped" not in answer.stdout


def test_pending_menu_discards_and_exits_without_resuming(tmp_path):
    answer = shell(tmp_path, one_function("install.sh", "interrupted_menu") + '''
ACTION='' ASSUME_YES=0 DIR=/fixture RECOVERY_ACTION=continue
ui_section() { :; }; ui_choose() { printf -v "$1" '%s' 2; }
discard_install() { printf 'discarded\\n'; }
interrupted_menu
printf '%s\\n' "$RECOVERY_ACTION"
''')
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.splitlines() == ["discarded", "quit"]


@pytest.mark.parametrize("confirm", [False, True])
def test_installer_discard_confirms_and_bypasses_setup(pending, tmp_path, confirm):
    (pending / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/discard-install.py", pending / "scripts")
    (pending / ".venv/bin").mkdir()
    (pending / ".venv/bin/python").symlink_to(sys.executable)
    partial = folder(pending, "runtimes/strata/releases/failed")
    answer = shell(tmp_path, script_functions("install.sh") + '''
load_ui() {
  source "$TEST_REPO/scripts/lib/ui.sh"
  source "$TEST_REPO/scripts/lib/detect.sh"
  source "$TEST_REPO/scripts/lib/receipt.sh"
  CHECKOUT=$INSTALL
  ui_confirm() { [[ $CONFIRM == yes ]]; }
}
warm_up() { exit 89; }
interrupted_menu() { exit 90; }
ui_confirm() { [[ $CONFIRM == yes ]]; }
main --dir "$INSTALL" --discard-install
''', env={"INSTALL": str(pending), "CONFIRM": "yes" if confirm else "no"})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert partial.exists() != confirm
    assert receipt(pending)["state"] == ("cancelled" if confirm else "pending")


def test_cancelled_install_no_longer_resumes_saved_engine_choices(pending, tmp_path):
    cleanup.discard(pending, apply=True)
    (pending / ".venv/bin").mkdir()
    (pending / ".venv/bin/python").symlink_to(sys.executable)
    answer = shell(tmp_path, script_functions("install.sh") + '''
load_ui() {
  source "$TEST_REPO/scripts/lib/ui.sh"
  source "$TEST_REPO/scripts/lib/detect.sh"
  source "$TEST_REPO/scripts/lib/receipt.sh"
  CHECKOUT=$INSTALL
}
warm_up() { :; }
interrupted_menu() { exit 90; }
setting_up() { exit 91; }
apply_components() { exit 92; }
installed_menu() { printf 'ordinary installed menu\\n'; }
main --dir "$INSTALL"
''', env={"INSTALL": str(pending)})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert "ordinary installed menu" in answer.stdout


def test_owg_exposes_discard_only_for_pending_plan(tmp_path):
    answer = shell(tmp_path, '''
source "$TEST_REPO/scripts/lib/receipt.sh"
''' + one_function("scripts/owg", "menu") + '''
ui_banner() { :; }; version() { :; }; commit() { :; }; ui_note() { :; }
status() { :; }; programs() { :; }; UI_ASK=1
ui_choose() { printf '%s\\n' "$@"; printf -v "$1" '%s' "$(($# - 4))"; }
RECEIPT[state]=pending
menu
RECEIPT[state]=complete
menu
''')
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.count("Discard failed install|") == 1


def test_owg_discard_dispatches_to_installer(tmp_path):
    install = tmp_path / "suite"
    (install / "scripts/lib").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/owg", install / "scripts/owg")
    for name in ("ui", "detect", "receipt"):
        shutil.copy2(ROOT / f"scripts/lib/{name}.sh", install / "scripts/lib")
    (install / "install.sh").write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    (install / "install.sh").chmod(0o755)
    answer = shell(tmp_path, '"$INSTALL/scripts/owg" discard-install --yes', env={"INSTALL": str(install)})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.splitlines() == ["--discard-install", "--dir", str(install), "--yes"]


def test_system_discard_handles_administrator_owned_partial_builds(pending, tmp_path):
    (pending / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/discard-install.py", pending / "scripts")
    (pending / ".venv/bin").mkdir()
    (pending / ".venv/bin/python").symlink_to(sys.executable)
    partial = folder(pending, "runtimes/llamacpp/source/b999")
    answer = shell(tmp_path, '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
''' + one_function("install.sh", "discard_install") + '''
DIR=$INSTALL SYSTEM=1 SVC=fixture ASSUME_YES=1
receipt_load "$DIR"
ui_sudo() { printf 'sudo requested\\n'; }
set_retry() { [[ $ACTION == discard-install ]]; }
owner_environment() { [[ $1 == fixture ]]; OWNER_ENV=("OWG_TEST_OWNER=fixture"); }
as_owner() { "$@"; }
sudo() { [[ $1 == -n ]]; shift; printf 'privileged cleanup\\n'; "$@"; }
ui_confirm() { exit 99; }
discard_install
''', env={"INSTALL": str(pending)})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert answer.stdout.count("privileged cleanup") == 2
    assert not partial.exists() and receipt(pending)["state"] == "cancelled"
